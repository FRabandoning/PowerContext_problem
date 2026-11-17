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

"""HTTP scoring-service adapter for Memory candidate reranking."""

from __future__ import annotations

import math

import httpx

from powercontext.builtin.artifacts.memory.models import MemoryHit
from powercontext.builtin.artifacts.memory.reranking import MemoryRerankDecision
from powercontext.builtin.inference import InferenceUsage

DEFAULT_POLICY_ID = "http-cross-encoder-v1"


class HTTPMemoryReranker:
    """Rerank Memory candidates using a batched HTTP scoring endpoint."""

    def __init__(
        self,
        endpoint: str,
        *,
        client: httpx.AsyncClient,
        batch_size: int = 32,
        policy_id: str = DEFAULT_POLICY_ID,
    ) -> None:
        if not isinstance(batch_size, int) or type(batch_size) is bool or batch_size <= 0:
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
        candidate_count = len(candidates)
        if candidate_count < 1:
            raise ValueError("rerank requires at least one candidate")  # noqa: TRY003
        if not isinstance(limit, int) or type(limit) is bool or limit < 1 or limit > candidate_count:
            raise ValueError("rerank limit must be between one and the candidate count")  # noqa: TRY003

        scores: dict[int, float] = {}
        requests = 0
        for start in range(0, candidate_count, self._batch_size):
            batch = list(range(start, min(start + self._batch_size, candidate_count)))
            payload = {
                "query": query,
                "candidates": [{"rank": rank + 1, "text": candidates[rank].text} for rank in batch],
            }
            response = await self._client.post(self._endpoint, json=payload)
            response.raise_for_status()
            requests += 1
            batch_scores = self._parse_scores(response, expected_ranks={rank + 1 for rank in batch})
            scores.update(batch_scores)

        ordered = sorted(scores, key=lambda rank: (-scores[rank], rank))
        return MemoryRerankDecision(
            selected_ranks=tuple(ordered[:limit]),
            usage=InferenceUsage(requests=requests),
        )

    @staticmethod
    def _parse_scores(response: httpx.Response, *, expected_ranks: set[int]) -> dict[int, float]:
        try:
            body = response.json()
        except (ValueError, UnicodeDecodeError) as exc:
            raise ValueError("invalid reranker JSON response") from exc  # noqa: TRY003
        if not isinstance(body, dict) or set(body) != {"scores"}:
            raise ValueError("reranker response must contain only scores")  # noqa: TRY003
        raw_scores = body["scores"]
        if not isinstance(raw_scores, list):
            raise ValueError("scores must be a list")  # noqa: TRY003

        parsed: dict[int, float] = {}
        for item in raw_scores:
            if not isinstance(item, dict) or set(item) != {"rank", "score"}:
                raise ValueError("each score must contain only rank and score")  # noqa: TRY003
            rank = item["rank"]
            score = item["score"]
            if type(rank) is not int:
                raise ValueError("score rank must be an integer")  # noqa: TRY003
            if isinstance(score, bool) or not isinstance(score, (int, float)):
                raise ValueError("score must be numeric")  # noqa: TRY003
            value = float(score)
            if not math.isfinite(value):
                raise ValueError("score must be finite")  # noqa: TRY003
            if rank not in expected_ranks or rank in parsed:
                raise ValueError("score rank outside batch or duplicated")  # noqa: TRY003
            parsed[rank] = value
        if set(parsed) != expected_ranks:
            raise ValueError("scores must cover the full batch")  # noqa: TRY003
        return parsed


__all__ = ["HTTPMemoryReranker"]
