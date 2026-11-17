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

"""Cross-encoder Memory reranking over one strict HTTP scoring contract."""

from __future__ import annotations

import math

import httpx

from powercontext.builtin.artifacts.memory.models import MemoryHit
from powercontext.builtin.artifacts.memory.reranking import MemoryRerankDecision, _validate_bounds
from powercontext.builtin.inference import InferenceUsage

HTTP_RERANKER_POLICY_ID = "http-cross-encoder-v1"


class HTTPMemoryReranker:
    """Score coarse candidates through batched HTTP requests with global ranks."""

    def __init__(
        self,
        endpoint: str,
        *,
        client: httpx.AsyncClient,
        batch_size: int = 32,
        policy_id: str = HTTP_RERANKER_POLICY_ID,
    ) -> None:
        if type(batch_size) is not int or batch_size < 1:
            raise ValueError("batch_size must be a positive integer")  # noqa: TRY003
        self._endpoint = endpoint
        self._client = client
        self._batch_size = batch_size
        self.policy_id = policy_id

    async def rerank(
        self,
        query: str,
        candidates: tuple[MemoryHit, ...],
        limit: int,
        /,
    ) -> MemoryRerankDecision:
        """Return the global top-limit ranks ordered by score desc then rank asc."""

        _validate_bounds(len(candidates), limit)
        scored: list[tuple[int, float]] = []
        requests = 0
        for start in range(0, len(candidates), self._batch_size):
            batch = candidates[start : start + self._batch_size]
            expected_ranks = set(range(start + 1, start + len(batch) + 1))
            response = await self._client.post(
                self._endpoint,
                json={
                    "query": query,
                    "candidates": [
                        {"rank": start + offset, "text": hit.text}
                        for offset, hit in enumerate(batch, start=1)
                    ],
                },
            )
            requests += 1
            response.raise_for_status()
            scored.extend(_parse_scores(response.json(), expected_ranks))
        scored.sort(key=lambda item: (-item[1], item[0]))
        selected = tuple(rank for rank, _ in scored[:limit])
        return MemoryRerankDecision(
            selected_ranks=selected,
            usage=InferenceUsage(requests=requests),
            discarded_rank_count=len(candidates) - len(selected),
        )


def _parse_scores(body: object, expected_ranks: set[int]) -> list[tuple[int, float]]:
    if not isinstance(body, dict) or set(body) != {"scores"}:
        raise ValueError("reranker response must contain exactly the scores field")  # noqa: TRY003
    entries = body["scores"]
    if not isinstance(entries, list):
        raise ValueError("reranker scores must be a list")  # noqa: TRY003
    scored: list[tuple[int, float]] = []
    seen: set[int] = set()
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != {"rank", "score"}:
            raise ValueError("each reranker score must contain exactly rank and score")  # noqa: TRY003
        rank = entry["rank"]
        score = entry["score"]
        if type(rank) is not int or rank not in expected_ranks or rank in seen:
            raise ValueError("reranker ranks must cover the request batch exactly once")  # noqa: TRY003
        if isinstance(score, bool) or not isinstance(score, (int, float)) or not math.isfinite(score):
            raise ValueError("reranker scores must be finite numbers")  # noqa: TRY003
        seen.add(rank)
        scored.append((rank, float(score)))
    if seen != expected_ranks:
        raise ValueError("reranker response must cover every requested rank")  # noqa: TRY003
    return scored


__all__ = ["HTTP_RERANKER_POLICY_ID", "HTTPMemoryReranker"]