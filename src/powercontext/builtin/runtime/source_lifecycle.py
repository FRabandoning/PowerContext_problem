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

"""Source lifecycle: strict events and a scope-bound dependency DAG.

Source withdrawals/restores propagate through a DAG of registered derivative
nodes (memory entries and topic-memory artifacts).  The authoritative graph and
per-node state live in a dedicated relational table so they survive Runtime
restarts; the derived state is mirrored into the memory artifact manifest and a
topic-memory exclusion set so the normal search backends inherit the filter.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import Column, Integer, String, Table, Text, insert, select, update
from sqlalchemy.exc import IntegrityError

from powercontext.artifacts import ArtifactRef, MemoryCitation
from powercontext.builtin.persistence.database import AsyncDatabase
from powercontext.builtin.persistence.schema import create_tables
from powercontext.builtin.persistence.tables import SHARED_METADATA, identity_string
from powercontext.limits import MAX_SCOPE_ID_LENGTH
from powercontext.sources import SourceRef

__all__ = [
    "LifecycleApplication",
    "SourceLifecycleError",
    "SourceLifecycleEvent",
    "SourceLifecycleStore",
]


class SourceLifecycleError(Exception):
    """A lifecycle request violated its strict identity, sequence, or DAG rules."""


class SourceLifecycleEvent(BaseModel):
    """One immutable availability transition for a registered source node."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    node_id: str = Field(min_length=1)
    revision: int = Field(gt=0)
    sequence: int = Field(gt=0)
    available: bool
    reason: str = Field(min_length=1)

    @field_validator("node_id", "reason")
    @classmethod
    def _non_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be blank")
        return value


SOURCE_LIFECYCLE_TABLE = Table(
    "pc_source_lifecycle_nodes",
    SHARED_METADATA,
    Column("scope_id", identity_string(MAX_SCOPE_ID_LENGTH), primary_key=True),
    Column("node_id", String(256), primary_key=True),
    Column("family", String(32), nullable=False),
    Column("reference_json", Text, nullable=False),
    Column("parents_json", Text, nullable=False),
    Column("policy", String(8), nullable=False),
    Column("revision", Integer, nullable=False),
    Column("sequence", Integer, nullable=False),
    Column("available", Integer, nullable=False),
    Column("reason", Text),
    Column("active", Integer, nullable=False),
)


@dataclass
class LifecycleNode:
    """One registered DAG node."""

    node_id: str
    family: str
    reference: object
    parents: tuple[str, ...]
    policy: str
    revision: int
    sequence: int
    available: bool
    reason: str | None
    active: bool


def _encode_reference(reference: Any) -> str:
    return json.dumps(
        {"kind": type(reference).__name__, "data": reference.model_dump(mode="json")},
        sort_keys=True,
        separators=(",", ":"),
    )


def _decode_reference(raw: str) -> Any:
    payload = json.loads(raw)
    kind, data = payload["kind"], payload["data"]
    if kind == "ArtifactRef":
        return ArtifactRef.model_validate(data)
    if kind == "MemoryCitation":
        return MemoryCitation.model_validate(data)
    if kind == "SourceRef":
        return SourceRef.model_validate(data)
    raise SourceLifecycleError(f"unknown-reference:{kind}")


class SourceLifecycleStore:
    """Persist the lifecycle DAG and its derived state for every scope."""

    def __init__(self, database: AsyncDatabase | None) -> None:
        self._database = database
        self._initialized = False

    async def _ensure_table(self, connection: Any) -> None:
        if not self._initialized:
            await create_tables(connection, (SOURCE_LIFECYCLE_TABLE,))
            self._initialized = True

    async def load_nodes(self, scope_id: str) -> dict[str, LifecycleNode]:
        if self._database is None:
            return {}
        async with self._database.transaction() as connection:
            await self._ensure_table(connection)
            rows = (
                await connection.execute(
                    select(SOURCE_LIFECYCLE_TABLE).where(SOURCE_LIFECYCLE_TABLE.c.scope_id == scope_id)
                )
            ).mappings()
            result: dict[str, LifecycleNode] = {}
            for row in rows:
                result[str(row["node_id"])] = LifecycleNode(
                    node_id=str(row["node_id"]),
                    family=str(row["family"]),
                    reference=_decode_reference(str(row["reference_json"])),
                    parents=tuple(json.loads(str(row["parents_json"]))),
                    policy=str(row["policy"]),
                    revision=int(row["revision"]),
                    sequence=int(row["sequence"]),
                    available=bool(row["available"]),
                    reason=None if row["reason"] is None else str(row["reason"]),
                    active=bool(row["active"]),
                )
            return result

    async def insert_node(self, scope_id: str, node: LifecycleNode) -> None:
        if self._database is None:
            raise SourceLifecycleError("store-unavailable")
        async with self._database.transaction() as connection:
            await self._ensure_table(connection)
            try:
                await connection.execute(
                    insert(SOURCE_LIFECYCLE_TABLE).values(
                        scope_id=scope_id,
                        node_id=node.node_id,
                        family=node.family,
                        reference_json=_encode_reference(node.reference),
                        parents_json=json.dumps(list(node.parents), separators=(",", ":")),
                        policy=node.policy,
                        revision=node.revision,
                        sequence=node.sequence,
                        available=int(node.available),
                        reason=node.reason,
                        active=int(node.active),
                    )
                )
            except IntegrityError as error:
                raise SourceLifecycleError("duplicate-node") from error

    async def save_states(self, scope_id: str, nodes: Mapping[str, LifecycleNode]) -> None:
        if self._database is None:
            raise SourceLifecycleError("store-unavailable")
        async with self._database.transaction() as connection:
            await self._ensure_table(connection)
            for node in nodes.values():
                await connection.execute(
                    update(SOURCE_LIFECYCLE_TABLE)
                    .where(
                        SOURCE_LIFECYCLE_TABLE.c.scope_id == scope_id,
                        SOURCE_LIFECYCLE_TABLE.c.node_id == node.node_id,
                    )
                    .values(
                        sequence=node.sequence,
                        available=int(node.available),
                        reason=node.reason,
                        active=int(node.active),
                    )
                )

    async def inactive_artifact_ids(self, scope_id: str) -> frozenset[str]:
        """Artifact ids of topic-memory nodes currently outside generation."""
        if self._database is None:
            return frozenset()
        async with self._database.transaction() as connection:
            await self._ensure_table(connection)
            rows = (
                await connection.execute(
                    select(SOURCE_LIFECYCLE_TABLE.c.reference_json).where(
                        SOURCE_LIFECYCLE_TABLE.c.scope_id == scope_id,
                        SOURCE_LIFECYCLE_TABLE.c.family == "topic-memory",
                        SOURCE_LIFECYCLE_TABLE.c.active == 0,
                    )
                )
            ).scalars()
            return frozenset(_decode_reference(str(row)).artifact_id for row in rows)


class LifecycleApplication:
    """Expose the scope-bound source lifecycle service on the Runtime."""

    def __init__(self, runtime: Any) -> None:
        self._runtime = runtime

    def for_scope(self, scope_id: str) -> ScopedLifecycleService:
        return ScopedLifecycleService(self._runtime, scope_id)


class ScopedLifecycleService:
    """Register the DAG and apply strict source transitions for one scope."""

    def __init__(self, runtime: Any, scope_id: str) -> None:
        self._runtime = runtime
        self.scope_id = scope_id

    @property
    def _store(self) -> SourceLifecycleStore:
        return self._runtime._lifecycle_store

    async def register_source(self, node_id: str, source_ref: SourceRef, *, revision: int) -> None:
        self._validate_node_id(node_id)
        if revision < 1:
            raise SourceLifecycleError("revision")
        node = LifecycleNode(
            node_id=node_id,
            family="source",
            reference=source_ref,
            parents=(),
            policy="all",
            revision=revision,
            sequence=0,
            available=True,
            reason=None,
            active=True,
        )
        await self._store.insert_node(self.scope_id, node)
        await self._recompute_and_apply(await self._store.load_nodes(self.scope_id))

    async def register_memory(
        self,
        node_id: str,
        citation: MemoryCitation,
        *,
        parents: tuple[str, ...],
        policy: str,
    ) -> None:
        await self._register_derivative(node_id, citation, "memory", parents, policy)
        await self._recompute_and_apply(await self._store.load_nodes(self.scope_id))

    async def register_topic(
        self,
        node_id: str,
        reference: ArtifactRef,
        *,
        parents: tuple[str, ...],
        policy: str,
    ) -> None:
        await self._register_derivative(node_id, reference, "topic-memory", parents, policy)
        await self._recompute_and_apply(await self._store.load_nodes(self.scope_id))

    async def _register_derivative(
        self,
        node_id: str,
        reference: object,
        family: str,
        parents: tuple[str, ...],
        policy: str,
    ) -> LifecycleNode:
        self._validate_node_id(node_id)
        if not parents:
            raise SourceLifecycleError("empty-parents")
        if policy not in {"all", "any"}:
            raise SourceLifecycleError("policy")
        if node_id in parents:
            raise SourceLifecycleError("cycle")
        nodes = await self._store.load_nodes(self.scope_id)
        if node_id in nodes:
            raise SourceLifecycleError("duplicate-node")
        for parent in parents:
            if parent not in nodes:
                raise SourceLifecycleError("unknown-parent")
        for parent in parents:
            if self._reaches(nodes, parent, node_id):
                raise SourceLifecycleError("cycle")
        node = LifecycleNode(
            node_id=node_id,
            family=family,
            reference=reference,
            parents=tuple(parents),
            policy=policy,
            revision=1,
            sequence=0,
            available=True,
            reason=None,
            active=True,
        )
        await self._store.insert_node(self.scope_id, node)
        return node

    @staticmethod
    def _validate_node_id(node_id: str) -> None:
        if not isinstance(node_id, str) or not node_id.strip():
            raise SourceLifecycleError("node-id")

    @staticmethod
    def _reaches(nodes: Mapping[str, LifecycleNode], start: str, target: str) -> bool:
        seen: set[str] = set()
        stack = [start]
        while stack:
            current = stack.pop()
            if current == target:
                return True
            if current in seen:
                continue
            seen.add(current)
            node = nodes.get(current)
            if node is not None:
                stack.extend(node.parents)
        return False

    async def apply(self, event: SourceLifecycleEvent) -> list[str]:
        """Apply one strict source event and return the newly computed active set."""
        if not isinstance(event, SourceLifecycleEvent):
            raise SourceLifecycleError("event")
        nodes = await self._store.load_nodes(self.scope_id)
        node = nodes.get(event.node_id)
        if node is None:
            raise SourceLifecycleError("unknown-node")
        if event.revision != node.revision:
            raise SourceLifecycleError("revision-mismatch")
        if event.sequence < node.sequence:
            raise SourceLifecycleError("stale-sequence")
        if event.sequence == node.sequence:
            if node.available == event.available and node.reason == event.reason:
                # Idempotent replay: never re-commit state, so memory_ref is stable.
                return sorted(self._compute_active(nodes))
            raise SourceLifecycleError("duplicate-sequence")
        node.available = event.available
        node.sequence = event.sequence
        node.reason = event.reason
        await self._recompute_and_apply(nodes)
        return sorted(self._compute_active(nodes))

    async def resolve(self, node_id: str, *, purpose: str = "generation") -> object:
        """Return the original reference, guarded for generation-purpose use."""
        if purpose not in {"generation", "audit"}:
            raise SourceLifecycleError("purpose")
        nodes = await self._store.load_nodes(self.scope_id)
        node = nodes.get(node_id)
        if node is None:
            raise SourceLifecycleError("unknown-node")
        if purpose == "generation" and not node.active:
            raise SourceLifecycleError("inactive-node")
        return node.reference

    @staticmethod
    def _compute_active(nodes: Mapping[str, LifecycleNode]) -> set[str]:
        active: set[str] = set()
        for _ in range(len(nodes) + 1):
            changed = False
            for node_id, node in nodes.items():
                if node_id in active:
                    continue
                if node.parents:
                    support = [parent in active for parent in node.parents]
                    supported = all(support) if node.policy == "all" else any(support)
                else:
                    supported = node.available
                if supported:
                    active.add(node_id)
                    changed = True
            if not changed:
                break
        return active

    async def _recompute_and_apply(self, nodes: dict[str, LifecycleNode]) -> None:
        active = self._compute_active(nodes)
        to_activate: list[str] = []
        to_deactivate: list[str] = []
        for node_id, node in nodes.items():
            new_state = node_id in active
            if new_state != node.active:
                (to_activate if new_state else to_deactivate).append(node_id)
            node.active = new_state
        await self._store.save_states(self.scope_id, nodes)
        await self._apply_memory_states(nodes, to_deactivate, to_activate)

    async def _apply_memory_states(
        self,
        nodes: Mapping[str, LifecycleNode],
        to_deactivate: list[str],
        to_activate: list[str],
    ) -> None:
        targets = [
            (node_id, True) for node_id in to_deactivate if nodes[node_id].family == "memory"
        ] + [(node_id, False) for node_id in to_activate if nodes[node_id].family == "memory"]
        if not targets:
            return
        from powercontext.builtin.runtime.application import _head_or_none

        async with self._runtime._context(self.scope_id) as context, self._runtime._locked(self.scope_id):
            service = context.artifacts.memory
            updated = None
            for deactivate in (True, False):
                selected = [node_id for node_id, flag in targets if flag == deactivate]
                if not selected:
                    continue
                base = updated if updated is not None else await _head_or_none(service, context.artifacts.memory_artifact_id)
                if base is None:
                    return
                entries = {entry.entry_id: entry for entry in await service.entries(base)}
                resolved = []
                for node_id in selected:
                    citation = nodes[node_id].reference
                    entry = entries.get(citation.entry_id)
                    if entry is None or entry.entry_version_id != citation.entry_version_id:
                        raise SourceLifecycleError("citation-drift")
                    resolved.append(entry)
                if deactivate:
                    updated = await service.forget(base, entries=tuple(resolved), reason="source lifecycle withdrawal")
                else:
                    updated = await service.reactivate(base, entries=tuple(resolved), reason="source lifecycle restore")
