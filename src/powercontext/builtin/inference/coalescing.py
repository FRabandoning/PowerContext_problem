# Copyright (c) 2026 OceanBase.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Concurrent embedding coalescing and bounded vector caching."""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from dataclasses import dataclass
from functools import partial
from typing import TYPE_CHECKING

from powercontext.builtin.inference.models import EmbeddingResult, EmbeddingVector, InferenceUsage

if TYPE_CHECKING:
    from powercontext.builtin.artifacts.memory.models import EmbeddingProfile
    from powercontext.builtin.inference.protocols import EmbeddingModel


@dataclass(frozen=True, slots=True)
class _BatchOutcome:
    vectors: dict[str, EmbeddingVector]
    usage: InferenceUsage


class CoalescingEmbeddingModel:
    """Deduplicate, batch, cache, and share embeddings across concurrent callers.

    Successful vectors are cached in an LRU.  Concurrent callers asking for the
    same text wait on one provider task, so cancelling one caller never
    cancels work that another caller still needs.
    """

    def __init__(
        self,
        model: EmbeddingModel,
        *,
        max_batch_size: int = 64,
        coalesce_ms: float = 5.0,
        cache_size: int = 1024,
    ) -> None:
        if not isinstance(max_batch_size, int) or isinstance(max_batch_size, bool) or max_batch_size <= 0:
            raise ValueError("max_batch_size must be a positive integer")
        if not isinstance(coalesce_ms, (int, float)) or isinstance(coalesce_ms, bool) or coalesce_ms < 0:
            raise ValueError("coalesce_ms must be a non-negative number")
        if not isinstance(cache_size, int) or isinstance(cache_size, bool) or cache_size < 0:
            raise ValueError("cache_size must be a non-negative integer")

        self._model = model
        self._max_batch_size = max_batch_size
        self._coalesce_seconds = float(coalesce_ms) / 1000.0
        self._cache_size = cache_size
        self._cache: OrderedDict[str, EmbeddingVector] = OrderedDict()
        self._inflight: dict[str, tuple[asyncio.Task[_BatchOutcome], asyncio.Future[_BatchOutcome]]] = {}
        self._lock = asyncio.Lock()

    @property
    def profile(self) -> EmbeddingProfile:
        return self._model.profile

    async def embed(self, texts: tuple[str, ...], /) -> EmbeddingResult:
        requested = tuple(texts)
        if not requested:
            return EmbeddingResult(vectors=(), usage=InferenceUsage(requests=0))

        unique = tuple(dict.fromkeys(requested))
        resolved: dict[str, EmbeddingVector] = {}
        pending: list[str] = []
        for text in unique:
            cached = self._cache_get(text)
            if cached is not None:
                resolved[text] = cached
            elif text not in self._inflight:
                pending.append(text)

        if pending and self._coalesce_seconds > 0:
            await asyncio.sleep(self._coalesce_seconds)

        awaited_tasks: set[asyncio.Task[_BatchOutcome]] = set()
        pending = [text for text in unique if text not in resolved]

        while pending:
            missing = [text for text in pending if text not in resolved and text not in self._inflight]
            if missing:
                await self._claim_missing(missing)

            futures: list[asyncio.Future[_BatchOutcome]] = []
            seen_futures: set[int] = set()
            for text in pending:
                if text in resolved:
                    continue
                entry = self._inflight.get(text)
                if entry is None:
                    continue
                task, future = entry
                awaited_tasks.add(task)
                if id(future) not in seen_futures:
                    seen_futures.add(id(future))
                    futures.append(future)

            if not futures:
                continue

            await asyncio.gather(*(asyncio.shield(future) for future in futures))
            for future in futures:
                resolved.update(future.result().vectors)

            pending = [text for text in pending if text not in resolved]

        vectors = tuple(resolved[text] for text in requested)
        return EmbeddingResult(vectors=vectors, usage=self._aggregate_usage(awaited_tasks))

    def _cache_get(self, text: str) -> EmbeddingVector | None:
        if text not in self._cache:
            return None
        vector = self._cache.pop(text)
        self._cache[text] = vector
        return vector

    def _cache_put(self, text: str, vector: EmbeddingVector) -> None:
        if self._cache_size == 0:
            return
        self._cache.pop(text, None)
        self._cache[text] = vector
        while len(self._cache) > self._cache_size:
            self._cache.popitem(last=False)

    async def _claim_missing(self, texts: list[str]) -> None:
        async with self._lock:
            to_launch: list[str] = []
            for text in texts:
                if text in self._cache or text in self._inflight:
                    continue
                to_launch.append(text)

            for start in range(0, len(to_launch), self._max_batch_size):
                batch = tuple(to_launch[start : start + self._max_batch_size])
                task = asyncio.create_task(self._embed_batch(batch))
                futures = {text: asyncio.get_running_loop().create_future() for text in batch}
                for text, future in futures.items():
                    self._inflight[text] = (task, future)
                task.add_done_callback(partial(self._settle_batch, batch, task, futures))

    async def _embed_batch(self, texts: tuple[str, ...]) -> _BatchOutcome:
        result = await self._model.embed(texts)
        vectors = tuple(result.vectors)
        if len(vectors) != len(texts):
            raise ValueError("embedding model returned an unexpected number of vectors")
        return _BatchOutcome(dict(zip(texts, vectors, strict=True)), result.usage)

    def _settle_batch(
        self,
        texts: tuple[str, ...],
        task: asyncio.Task[_BatchOutcome],
        futures: dict[str, asyncio.Future[_BatchOutcome]],
        _done: asyncio.Task[_BatchOutcome],
    ) -> None:
        for text in texts:
            entry = self._inflight.get(text)
            if entry is not None and entry[0] is task:
                del self._inflight[text]

        if task.cancelled():
            for future in futures.values():
                if not future.done():
                    future.cancel()
            return

        error = task.exception()
        if error is not None:
            for future in futures.values():
                if not future.done():
                    future.set_exception(error)
            return

        outcome = task.result()
        for text, vector in outcome.vectors.items():
            self._cache_put(text, vector)
        for future in futures.values():
            if not future.done():
                future.set_result(outcome)

    @staticmethod
    def _aggregate_usage(tasks: set[asyncio.Task[_BatchOutcome]]) -> InferenceUsage:
        if not tasks:
            return InferenceUsage(requests=0)

        usages = [task.result().usage for task in tasks]
        input_tokens = [usage.input_tokens for usage in usages if usage.input_tokens is not None]
        output_tokens = [usage.output_tokens for usage in usages if usage.output_tokens is not None]
        return InferenceUsage(
            requests=sum(usage.requests for usage in usages),
            input_tokens=sum(input_tokens) if input_tokens else None,
            output_tokens=sum(output_tokens) if output_tokens else None,
        )
