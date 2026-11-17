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

"""Persistence for the per-scope source-lifecycle evidence DAG."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from pydantic import BaseModel
from sqlalchemy import insert, select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from powercontext.builtin.persistence.codec import stored_bytes
from powercontext.builtin.persistence.tables import SOURCE_LIFECYCLE_NODES_TABLE


class StoredLifecycleNode(BaseModel):
    """One registered evidence node and its propagated availability."""

    scope_id: str
    node_id: str
    kind: str
    ref_payload: bytes
    revision: int
    parents: tuple[str, ...]
    policy: str | None
    available: bool
    last_sequence: int | None = None
    last_available: bool | None = None
    last_reason: str | None = None


class SourceLifecycleRepository:
    """Read and write lifecycle nodes for one scope."""

    async def load_all(self, connection: AsyncConnection, scope_id: str, /) -> tuple[StoredLifecycleNode, ...]:
        rows = (
            await connection.execute(
                select(SOURCE_LIFECYCLE_NODES_TABLE)
                .where(SOURCE_LIFECYCLE_NODES_TABLE.c.scope_id == scope_id)
                .order_by(SOURCE_LIFECYCLE_NODES_TABLE.c.node_id)
            )
        ).mappings()
        return tuple(_decode_row(row) for row in rows)

    async def get(self, connection: AsyncConnection, scope_id: str, node_id: str, /) -> StoredLifecycleNode | None:
        row = (
            await connection.execute(
                select(SOURCE_LIFECYCLE_NODES_TABLE).where(
                    SOURCE_LIFECYCLE_NODES_TABLE.c.scope_id == scope_id,
                    SOURCE_LIFECYCLE_NODES_TABLE.c.node_id == node_id,
                )
            )
        ).mappings().one_or_none()
        return None if row is None else _decode_row(row)

    async def insert(self, connection: AsyncConnection, node: StoredLifecycleNode, /) -> None:
        await connection.execute(
            insert(SOURCE_LIFECYCLE_NODES_TABLE).values(
                scope_id=node.scope_id,
                node_id=node.node_id,
                kind=node.kind,
                ref_payload=node.ref_payload,
                revision=node.revision,
                parents_payload=json.dumps(list(node.parents)).encode(),
                policy=node.policy,
                available=node.available,
                last_sequence=node.last_sequence,
                last_available=node.last_available,
                last_reason=node.last_reason,
            )
        )

    async def set_availability(
        self, connection: AsyncConnection, scope_id: str, node_id: str, /, *, available: bool
    ) -> None:
        await connection.execute(
            update(SOURCE_LIFECYCLE_NODES_TABLE)
            .where(
                SOURCE_LIFECYCLE_NODES_TABLE.c.scope_id == scope_id,
                SOURCE_LIFECYCLE_NODES_TABLE.c.node_id == node_id,
            )
            .values(available=available)
        )

    async def record_source_event(
        self,
        connection: AsyncConnection,
        scope_id: str,
        node_id: str,
        /,
        *,
        available: bool,
        sequence: int,
        reason: str,
    ) -> None:
        await connection.execute(
            update(SOURCE_LIFECYCLE_NODES_TABLE)
            .where(
                SOURCE_LIFECYCLE_NODES_TABLE.c.scope_id == scope_id,
                SOURCE_LIFECYCLE_NODES_TABLE.c.node_id == node_id,
            )
            .values(available=available, last_sequence=sequence, last_available=available, last_reason=reason)
        )

    async def suppressed_topic_artifact_ids(self, connection: AsyncConnection, scope_id: str, /) -> frozenset[str]:
        rows = (
            await connection.execute(
                select(SOURCE_LIFECYCLE_NODES_TABLE.c.ref_payload).where(
                    SOURCE_LIFECYCLE_NODES_TABLE.c.scope_id == scope_id,
                    SOURCE_LIFECYCLE_NODES_TABLE.c.kind == "topic",
                    SOURCE_LIFECYCLE_NODES_TABLE.c.available.is_(False),
                )
            )
        ).all()
        suppressed: set[str] = set()
        for (payload,) in rows:
            artifact_id = json.loads(stored_bytes(payload, column="ref_payload")).get("artifact_id")
            if isinstance(artifact_id, str):
                suppressed.add(artifact_id)
        return frozenset(suppressed)


def _decode_row(row: Mapping[Any, Any]) -> StoredLifecycleNode:
    parents = json.loads(stored_bytes(row["parents_payload"], column="parents_payload"))
    last_sequence = row["last_sequence"]
    last_available = row["last_available"]
    last_reason = row["last_reason"]
    return StoredLifecycleNode(
        scope_id=str(row["scope_id"]),
        node_id=str(row["node_id"]),
        kind=str(row["kind"]),
        ref_payload=stored_bytes(row["ref_payload"], column="ref_payload"),
        revision=int(row["revision"]),
        parents=tuple(str(parent) for parent in parents),
        policy=None if row["policy"] is None else str(row["policy"]),
        available=bool(row["available"]),
        last_sequence=None if last_sequence is None else int(last_sequence),
        last_available=None if last_available is None else bool(last_available),
        last_reason=None if last_reason is None else str(last_reason),
    )