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

"""Cross-call Embedding request coalescing with a bounded success cache.

Concurrent calls share identical texts through in-flight work and a short
coalescing window; successful vectors are reused from an LRU cache. Each call
keeps its original order and duplicate positions, sums the usage of the distinct
base batches it waited on, and is isolated from other waiters' cancellation.
"""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from powercontext.builtin.inference.models import EmbeddingResult, EmbeddingVector, InferenceUsage

if TYPE_CHECKING:
    from powercontext.builtin.artifacts.memory.models import EmbeddingProfile
    from powercontext.builtin.inference.protocols import EmbeddingModel


@dataclass(slots=True, eq=False)
class _Batch:
    texts: tuple[str, ...]
    works: list[_TextWork]
    usage: InferenceUsage | None = None


@dataclass(slots=True)
class _TextWork:
    text: str
    future: asyncio.Future[EmbeddingVector]
    batch: _Batch | None = None


@dataclass(slots=True)
class CoalescingEmbeddingModel:
    """Share concurrent Embedding work and cache successful vectors."""

    _model: EmbeddingModel
    _max_batch_size: int
    _coalesce_seconds: float
    _cache_size: int
    _cache: OrderedDict[str, EmbeddingVector] = field(default_factory=OrderedDict, init=False)
    _inflight: dict[str, _TextWork] = field(default_factory=dict, init=False)
    _pending: dict[str, _TextWork] = field(default_factory=dict, init=False)
    _flush_handle: asyncio.TimerHandle | None = field(default=None, init=False)

    def __init__(
        self,
        model: EmbeddingModel,
        *,
        max_batch_size: int = 64,
        coalesce_ms: float = 5.0,
        cache_size: int = 1024,
    ) -> None:
        if type(max_batch_size) is not int or max_batch_size < 1:
            raise ValueError("max_batch_size must be a positive integer")  # noqa: TRY003
        if type(cache_size) is not int or cache_size < 0:
            raise ValueError("cache_size must be a non-negative integer")  # noqa: TRY003
        if coalesce_ms < 0:
            raise ValueError("coalesce_ms must be non-negative")  # noqa: TRY003
        self._model = model
        self._max_batch_size = max_batch_size
        self._coalesce_seconds = coalesce_ms / 1000.0
        self._cache_size = cache_size
        self._cache = OrderedDict()
        self._inflight = {}
        self._pending = {}
        self._flush_handle = None

    @property
    def profile(self) -> EmbeddingProfile:
        return self._model.profile

    async def embed(self, texts: tuple[str, ...], /) -> EmbeddingResult:
        texts = tuple(texts)
        if not texts:
            return EmbeddingResult(vectors=(), usage=InferenceUsage(requests=0))
        loop = asyncio.get_running_loop()
        cached: dict[str, EmbeddingVector] = {}
        works: dict[str, _TextWork] = {}
        scheduled = False
        for text in dict.fromkeys(texts):
            if text in self._cache:
                self._cache.move_to_end(text)
                cached[text] = self._cache[text]
            elif text in self._inflight:
                works[text] = self._inflight[text]
            else:
                work = _TextWork(text=text, future=loop.create_future())
                self._inflight[text] = work
                self._pending[text] = work
                works[text] = work
                scheduled = True
        if scheduled:
            self._schedule_flush(loop)
        if works:
            await asyncio.gather(*(asyncio.shield(work.future) for work in works.values()))
        batches: dict[_Batch, None] = {}
        for work in works.values():
            if work.batch is not None:
                batches[work.batch] = None
        vectors = tuple(cached[text] if text in cached else works[text].future.result() for text in texts)
        return EmbeddingResult(vectors=vectors, usage=_sum_usage(tuple(batches)))

    def _schedule_flush(self, loop: asyncio.AbstractEventLoop) -> None:
        if self._flush_handle is None:
            self._flush_handle = loop.call_later(self._coalesce_seconds, self._flush)

    def _flush(self) -> None:
        self._flush_handle = None
        pending = tuple(self._pending.values())
        self._pending.clear()
        for start in range(0, len(pending), self._max_batch_size):
            members = list(pending[start : start + self._max_batch_size])
            batch = _Batch(texts=tuple(work.text for work in members), works=members)
            for work in members:
                work.batch = batch
            asyncio.create_task(self._run_batch(batch))

    async def _run_batch(self, batch: _Batch) -> None:
        try:
            result = await self._model.embed(batch.texts)
            if len(result.vectors) != len(batch.texts):
                raise ValueError("embedding vector count does not match the batch text count")  # noqa: TRY003
        except Exception as error:
            for work in batch.works:
                self._inflight.pop(work.text, None)
                if not work.future.done():
                    work.future.set_exception(error)
            return
        batch.usage = result.usage
        for work, vector in zip(batch.works, result.vectors, strict=True):
            self._inflight.pop(work.text, None)
            if self._cache_size > 0:
                self._cache[work.text] = vector
                self._cache.move_to_end(work.text)
                while len(self._cache) > self._cache_size:
                    self._cache.popitem(last=False)
            if not work.future.done():
                work.future.set_result(vector)


def _sum_usage(batches: tuple[_Batch, ...]) -> InferenceUsage:
    requests = 0
    input_tokens: int | None = None
    output_tokens: int | None = None
    for batch in batches:
        usage = batch.usage
        if usage is None:
            continue
        requests += usage.requests
        if usage.input_tokens is not None:
            input_tokens = (input_tokens or 0) + usage.input_tokens
        if usage.output_tokens is not None:
            output_tokens = (output_tokens or 0) + usage.output_tokens
    return InferenceUsage(requests=requests, input_tokens=input_tokens, output_tokens=output_tokens)


__all__ = ["CoalescingEmbeddingModel"]