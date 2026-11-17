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

"""Propagate source withdrawal and restore across a per-scope evidence DAG."""

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, field_validator
from sqlalchemy.ext.asyncio import AsyncConnection

from powercontext.artifacts import ArtifactRef, MemoryCitation
from powercontext.builtin.artifacts.memory import MemoryService
from powercontext.builtin.persistence.codec import dump_model, load_model
from powercontext.builtin.persistence.database import AsyncDatabase
from powercontext.builtin.persistence.source_lifecycle import (
    SourceLifecycleRepository,
    StoredLifecycleNode,
)
from powercontext.builtin.runtime.models import GetMemoryEntryRequest
from powercontext.builtin.sources import validate_scope_id
from powercontext.limits import MAX_SOURCE_ID_LENGTH
from powercontext.sources import SourceRef

if TYPE_CHECKING:
    from powercontext.builtin.runtime.application import BuiltinRuntime

_POLICIES = ("all", "any")
_PURPOSES = ("generation", "audit")


class SourceLifecycleError(ValueError):
    """A lifecycle registration, event, or resolution violates the contract."""


class SourceLifecycleEvent(BaseModel):
    """One availability event applied to a registered Source node."""

    model_config = ConfigDict(extra="forbid")

    node_id: str
    revision: StrictInt = Field(ge=1)
    sequence: StrictInt = Field(ge=1)
    available: StrictBool
    reason: str

    @field_validator("node_id", "reason")
    @classmethod
    def require_non_blank(cls, value: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ValueError("must be a non-empty string that is not only whitespace")  # noqa: TRY003
        return value


def _validate_node_id(node_id: str) -> str:
    if not isinstance(node_id, str) or not node_id.strip() or node_id != node_id.strip():
        raise SourceLifecycleError("node_id must be a non-empty trimmed string")
    if len(node_id) > MAX_SOURCE_ID_LENGTH:
        raise SourceLifecycleError("node_id is too long")
    return node_id


def _validate_policy(policy: str) -> str:
    if policy not in _POLICIES:
        raise SourceLifecycleError("policy must be 'all' or 'any'")
    return policy


def _validate_parents(parents: tuple[str, ...], node_id: str) -> tuple[str, ...]:
    if not isinstance(parents, tuple) or not parents:
        raise SourceLifecycleError("a derived node requires at least one parent")
    seen: set[str] = set()
    for parent in parents:
        _validate_node_id(parent)
        if parent == node_id:
            raise SourceLifecycleError("a node cannot depend on itself")
        if parent in seen:
            raise SourceLifecycleError("duplicate parent")
        seen.add(parent)
    return parents


def _evaluate(parents: tuple[str, ...], policy: str, available: dict[str, bool]) -> bool:
    support = [available[parent] for parent in parents]
    return all(support) if policy == "all" else any(support)


def _compute_availability(
    nodes: tuple[StoredLifecycleNode, ...],
    target_source: str,
    target_available: bool,
) -> dict[str, bool]:
    by_id = {node.node_id: node for node in nodes}
    memo: dict[str, bool] = {}
    visiting: set[str] = set()

    def resolve(node_id: str) -> bool:
        if node_id in memo:
            return memo[node_id]
        if node_id in visiting:
            raise SourceLifecycleError("evidence graph contains a cycle")
        node = by_id.get(node_id)
        if node is None:
            raise SourceLifecycleError(f"missing parent node {node_id!r}")
        visiting.add(node_id)
        if node.kind == "source":
            value = target_available if node_id == target_source else node.available
        else:
            value = _evaluate(node.parents, node.policy or "all", {p: resolve(p) for p in node.parents})
        visiting.discard(node_id)
        memo[node_id] = value
        return value

    for node_id in by_id:
        resolve(node_id)
    return memo


def _decode_ref(node: StoredLifecycleNode) -> SourceRef | MemoryCitation | ArtifactRef:
    model = {"source": SourceRef, "memory": MemoryCitation, "topic": ArtifactRef}[node.kind]
    return load_model(model, node.ref_payload, kind="source-lifecycle", name=node.node_id)


class ScopedSourceLifecycleApplication:
    """Register evidence dependencies and propagate Source availability in one scope."""

    def __init__(self, runtime: BuiltinRuntime, scope_id: str, database: AsyncDatabase) -> None:
        self._runtime = runtime
        self._database = database
        self._repository = SourceLifecycleRepository()
        self.scope_id = validate_scope_id(scope_id)

    async def register_source(self, node_id: str, source_ref: SourceRef, /, *, revision: int) -> None:
        node_id = _validate_node_id(node_id)
        if type(revision) is not int or revision < 1:
            raise SourceLifecycleError("revision must be a positive integer")
        if not isinstance(source_ref, SourceRef):
            raise SourceLifecycleError("source_ref must be a SourceRef")
        await self._runtime.records.for_scope(self.scope_id).get_source(source_ref.source_type, source_ref.source_id)
        ref_payload = dump_model(source_ref, kind="source-lifecycle", name=node_id)
        async with (
            self._runtime._scope_operation(self.scope_id),
            self._runtime._locked(self.scope_id),
            self._database.transaction() as connection,
        ):
            existing = await self._repository.get(connection, self.scope_id, node_id)
            if existing is not None:
                if existing.kind == "source" and existing.ref_payload == ref_payload and existing.revision == revision:
                    return
                raise SourceLifecycleError(f"node {node_id!r} is already registered with a different binding")
            await self._repository.insert(
                connection,
                StoredLifecycleNode(
                    scope_id=self.scope_id,
                    node_id=node_id,
                    kind="source",
                    ref_payload=ref_payload,
                    revision=revision,
                    parents=(),
                    policy=None,
                    available=True,
                ),
            )

    async def register_memory(
        self,
        node_id: str,
        citation: MemoryCitation,
        /,
        *,
        parents: tuple[str, ...],
        policy: str,
    ) -> None:
        node_id = _validate_node_id(node_id)
        policy = _validate_policy(policy)
        parents = _validate_parents(parents, node_id)
        if not isinstance(citation, MemoryCitation):
            raise SourceLifecycleError("citation must be a MemoryCitation")
        await self._runtime.memory.for_scope(self.scope_id).get(GetMemoryEntryRequest(citation=citation))
        ref_payload = dump_model(citation, kind="source-lifecycle", name=node_id)
        async with self._runtime._context(self.scope_id) as context, self._runtime._locked(self.scope_id):
            async with self._database.transaction() as connection:
                existing = await self._repository.get(connection, self.scope_id, node_id)
                if existing is not None:
                    if (
                        existing.kind == "memory"
                        and existing.ref_payload == ref_payload
                        and existing.parents == parents
                        and existing.policy == policy
                    ):
                        return
                    raise SourceLifecycleError(f"node {node_id!r} is already registered with a different binding")
                available = await self._register_derived(connection, node_id, "memory", ref_payload, citation.memory_ref.revision, parents, policy)
            await self._sync_memory(
                context.artifacts.memory,
                context.artifacts.memory_artifact_id,
                citation.entry_id,
                available=available,
                reason="source-lifecycle registration",
            )

    async def register_topic(
        self,
        node_id: str,
        artifact_ref: ArtifactRef,
        /,
        *,
        parents: tuple[str, ...],
        policy: str,
    ) -> None:
        node_id = _validate_node_id(node_id)
        policy = _validate_policy(policy)
        parents = _validate_parents(parents, node_id)
        if not isinstance(artifact_ref, ArtifactRef):
            raise SourceLifecycleError("artifact_ref must be an ArtifactRef")
        await self._runtime.records.for_scope(self.scope_id).get_artifact_revision(
            artifact_ref.family, artifact_ref.artifact_id, artifact_ref.revision
        )
        ref_payload = dump_model(artifact_ref, kind="source-lifecycle", name=node_id)
        async with (
            self._runtime._scope_operation(self.scope_id),
            self._runtime._locked(self.scope_id),
            self._database.transaction() as connection,
        ):
            existing = await self._repository.get(connection, self.scope_id, node_id)
            if existing is not None:
                if (
                    existing.kind == "topic"
                    and existing.ref_payload == ref_payload
                    and existing.parents == parents
                    and existing.policy == policy
                ):
                    return
                raise SourceLifecycleError(f"node {node_id!r} is already registered with a different binding")
            await self._register_derived(connection, node_id, "topic", ref_payload, artifact_ref.revision, parents, policy)

    async def _register_derived(
        self,
        connection: AsyncConnection,
        node_id: str,
        kind: str,
        ref_payload: bytes,
        revision: int,
        parents: tuple[str, ...],
        policy: str,
    ) -> bool:
        nodes = await self._repository.load_all(connection, self.scope_id)
        by_id = {node.node_id: node for node in nodes}
        for parent in parents:
            if parent not in by_id:
                raise SourceLifecycleError(f"parent node {parent!r} is not registered")
        available_map = _compute_availability(nodes, "", False)
        available = _evaluate(parents, policy, available_map)
        await self._repository.insert(
            connection,
            StoredLifecycleNode(
                scope_id=self.scope_id,
                node_id=node_id,
                kind=kind,
                ref_payload=ref_payload,
                revision=revision,
                parents=parents,
                policy=policy,
                available=available,
            ),
        )
        return available

    async def apply(self, event: SourceLifecycleEvent, /) -> list[str]:
        if not isinstance(event, SourceLifecycleEvent):
            raise SourceLifecycleError("event must be a SourceLifecycleEvent")
        async with self._runtime._context(self.scope_id) as context, self._runtime._locked(self.scope_id):
            service = context.artifacts.memory
            memory_artifact_id = context.artifacts.memory_artifact_id
            async with self._database.transaction() as connection:
                nodes = await self._repository.load_all(connection, self.scope_id)
                by_id = {node.node_id: node for node in nodes}
                source = by_id.get(event.node_id)
                if source is None or source.kind != "source":
                    raise SourceLifecycleError(f"unknown source node {event.node_id!r}")
                if event.revision != source.revision:
                    raise SourceLifecycleError("event revision does not match the source lifecycle revision")
                replay = False
                if source.last_sequence is not None:
                    if event.sequence < source.last_sequence:
                        raise SourceLifecycleError("event sequence regressed")
                    if event.sequence == source.last_sequence:
                        if event.available == source.last_available and event.reason == source.last_reason:
                            replay = True
                        else:
                            raise SourceLifecycleError("event sequence reused with different content")
                available_map = _compute_availability(nodes, event.node_id, event.available)
                if not replay:
                    await self._repository.record_source_event(
                        connection,
                        self.scope_id,
                        event.node_id,
                        available=event.available,
                        sequence=event.sequence,
                        reason=event.reason,
                    )
                for node in nodes:
                    if node.kind != "source" and available_map[node.node_id] != node.available:
                        await self._repository.set_availability(
                            connection, self.scope_id, node.node_id, available=available_map[node.node_id]
                        )
            for node in nodes:
                if node.kind == "memory":
                    citation = load_model(MemoryCitation, node.ref_payload, kind="source-lifecycle", name=node.node_id)
                    await self._sync_memory(
                        service,
                        memory_artifact_id,
                        citation.entry_id,
                        available=available_map[node.node_id],
                        reason=event.reason,
                    )
            return sorted(node_id for node_id, available in available_map.items() if available)

    async def resolve(self, node_id: str, /, *, purpose: str) -> SourceRef | MemoryCitation | ArtifactRef:
        if purpose not in _PURPOSES:
            raise SourceLifecycleError("purpose must be 'generation' or 'audit'")
        node_id = _validate_node_id(node_id)
        async with self._database.transaction() as connection:
            node = await self._repository.get(connection, self.scope_id, node_id)
        if node is None:
            raise SourceLifecycleError(f"unknown node {node_id!r}")
        reference = _decode_ref(node)
        if purpose == "generation" and not node.available:
            raise SourceLifecycleError(f"node {node_id!r} is not available for generation")
        return reference

    async def _sync_memory(
        self,
        service: MemoryService,
        memory_artifact_id: str,
        entry_id: str,
        /,
        *,
        available: bool,
        reason: str,
    ) -> None:
        head = await service.head(memory_artifact_id)
        item = next((entry for entry in head.content.manifest.entries if entry.entry_id == entry_id), None)
        if item is None:
            raise SourceLifecycleError(f"memory entry {entry_id!r} is not present")
        target_state = "active" if available else "inactive"
        if item.state == target_state:
            return
        entry = next((candidate for candidate in await service.entries(head) if candidate.entry_id == entry_id), None)
        if entry is None:
            raise SourceLifecycleError(f"memory entry {entry_id!r} is not readable")
        if available:
            await service.reactivate(head, entries=(entry,), reason=reason)
        else:
            await service.forget(head, entries=(entry,), reason=reason)


class SourceLifecycleApplication:
    """Select the source-lifecycle service for one scope."""

    def __init__(self, runtime: BuiltinRuntime, database: AsyncDatabase) -> None:
        self._runtime = runtime
        self._database = database

    def for_scope(self, scope_id: str, /) -> ScopedSourceLifecycleApplication:
        return ScopedSourceLifecycleApplication(self._runtime, scope_id, self._database)