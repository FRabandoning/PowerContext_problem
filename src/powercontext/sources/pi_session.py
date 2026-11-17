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

"""Pi session history ingestion with stable, citeable per-event identity.

A Pi session is an append-only journal of records forming a tree.  Each record
has an ``id`` and a ``parentId``; the evidence visible to the user is exactly
the path from the selected leaf back to the root.  Every evidence-bearing
record on that path becomes an independent captured Source whose identity is
derived from ``(session_id, event_id)`` only, so it is stable across branch
changes and independent of the event payload.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import quote

from pydantic import BaseModel, Field, JsonValue

from powercontext.limits import MAX_SOURCE_ID_LENGTH
from powercontext.sources import (
    AdapterSourceDefinition,
    ConnectorRunCompletion,
    ConnectorRunStatus,
    SourceMaterialization,
    TextEvidence,
)
from powercontext.sources.models import Source
from powercontext.sources.projections import TEXT_EVIDENCE_PROJECTION_KEY

PI_EVENT_SOURCE_NAME = "pi-event"
PI_EVENT_SOURCE_VERSION = "1"
PI_SESSION_CONNECTOR_NAME = "pi-session-history"
PI_SESSION_CONNECTOR_VERSION = "1"

NonEmptyText = Annotated[str, Field(min_length=1)]

# Record types that carry retrievable evidence.  Everything else is a control
# node (model_change, thinking_level_change, compaction bookkeeping, ...) that
# is part of the branch topology but is not itself an evidence event.
_EVIDENCE_TYPES = frozenset({"message", "compaction", "branch_summary", "custom_message"})


class PiSessionInput(BaseModel):
    """A raw Pi session archive and the optional leaf that selects a branch."""

    content: bytes
    leaf_id: str | None = None


class PiEvent(BaseModel):
    """One evidence-bearing event on the selected Pi session branch."""

    session_id: str
    event_id: str
    parent_id: str | None
    event_type: str
    role: str
    content: str
    record: dict[str, JsonValue]


class PiEventSource(Source):
    """Durable captured Source for one Pi session event."""

    source_id: str
    session_id: str
    event_id: str
    parent_id: str | None
    event_type: str
    role: str
    content: str
    record: dict[str, JsonValue]


def _validate_identity(session_id: str, event_id: str) -> None:
    if not session_id or session_id.strip() != session_id:
        raise ValueError("session_id must be a non-empty trimmed string")  # noqa: TRY003
    if not event_id or event_id.strip() != event_id:
        raise ValueError("event_id must be a non-empty trimmed string")  # noqa: TRY003


def pi_event_source_id(session_id: str, event_id: str) -> str:
    """Build a delimiter-safe, bounded identity for one Pi session event."""

    _validate_identity(session_id, event_id)
    name = f"pi:{quote(session_id, safe='')}:{quote(event_id, safe='')}"
    if len(name) > MAX_SOURCE_ID_LENGTH:
        raise ValueError("pi event source id exceeds the maximum length")  # noqa: TRY003
    return name


def _compact_json(value: JsonValue) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _text_from_blocks(content: JsonValue) -> str:
    """Collect the textual payload of a list of content blocks."""

    if isinstance(content, str):
        return content
    parts: list[str] = []
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                text = block.get("text", "")
                if isinstance(text, str):
                    parts.append(text)
    return "\n".join(parts)


def _render_content(content: JsonValue) -> str:
    """Render a message's ``content`` value, preserving tool call evidence."""

    if isinstance(content, str):
        return content
    parts: list[str] = []
    if isinstance(content, list):
        for block in content:
            if not isinstance(block, dict):
                continue
            kind = block.get("type")
            if kind in {"toolCall", "tool_call"}:
                name = block.get("name", "")
                call_id = block.get("id", "")
                arguments = block.get("arguments", {})
                parts.append(f"[tool_call {name} {call_id}] {_compact_json(arguments)}")
            elif kind in {"toolResult", "tool_result"}:
                name = block.get("toolName", block.get("name", ""))
                call_id = block.get("toolCallId", block.get("id", ""))
                parts.append(
                    f"[tool_result {name} {call_id}] {_text_from_blocks(block.get('content'))}"
                )
            elif kind == "text":
                text = block.get("text", "")
                if isinstance(text, str):
                    parts.append(text)
            # thinking, image and other non-evidence blocks are intentionally
            # not rendered as retrievable evidence.
    return "\n".join(parts)


def _event_from_record(session_id: str, record: dict[str, JsonValue]) -> PiEvent | None:
    """Convert one topology record into an evidence event, or None if control."""

    record_type = record.get("type")
    event_id = record.get("id")
    parent_id = record.get("parentId")
    if not isinstance(event_id, str) or not event_id:
        raise ValueError("session record id must be a non-empty string")  # noqa: TRY003
    if parent_id is not None and (not isinstance(parent_id, str) or not parent_id):
        raise ValueError("session record parentId must be a string or null")  # noqa: TRY003
    if record_type not in _EVIDENCE_TYPES:
        return None

    if record_type == "compaction":
        role = "compaction"
        summary = record.get("summary", "")
        content = f"compaction: {summary}"
    elif record_type == "branch_summary":
        role = "branch_summary"
        summary = record.get("summary", "")
        content = f"branch_summary: {summary}"
    elif record_type == "custom_message":
        role = "custom"
        content = f"custom: {_text_from_blocks(record.get('content'))}"
    else:
        message = record.get("message")
        if not isinstance(message, dict):
            raise ValueError("message record requires a message object")  # noqa: TRY003
        raw_role = message.get("role")
        if not isinstance(raw_role, str) or not raw_role:
            raise ValueError("message role must be a non-empty string")  # noqa: TRY003
        role = raw_role
        if role == "bashExecution":
            content = f"bashExecution: $ {message.get('command', '')}\n{message.get('output', '')}"
        elif role == "toolResult":
            name = message.get("toolName", "")
            call_id = message.get("toolCallId", "")
            content = (
                f"toolResult: [tool_result {name} {call_id}] "
                f"{_text_from_blocks(message.get('content'))}"
            )
        else:
            content = f"{role}: {_render_content(message.get('content'))}"

    return PiEvent(
        session_id=session_id,
        event_id=event_id,
        parent_id=parent_id,
        event_type=record_type,
        role=role,
        content=content,
        record=record,
    )


def _default_leaf(records: list[dict[str, JsonValue]]) -> str:
    """Choose the latest record in file order that has no children."""

    parents = {record["id"]: record.get("parentId") for record in records}
    children = {parent for parent in parents.values() if parent is not None}
    for record in reversed(records):
        record_id = record["id"]
        if record_id not in children:
            return record_id
    raise ValueError("session has no selectable leaf")  # noqa: TRY003


def parse_pi_session(value: PiSessionInput, /) -> tuple[PiEvent, ...]:
    """Parse and validate a Pi session archive, or fail atomically."""

    if not isinstance(value, PiSessionInput):
        raise ValueError("parse_pi_session requires PiSessionInput")  # noqa: TRY003

    try:
        text = value.content.decode("utf-8-sig")
    except (UnicodeDecodeError, AttributeError) as exc:
        raise ValueError("Pi session content is not valid UTF-8") from exc  # noqa: TRY003

    records: list[dict[str, JsonValue]] = []
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            parsed: Any = json.loads(line)
        except (TypeError, ValueError) as exc:
            raise ValueError("Pi session contains a malformed JSON record") from exc  # noqa: TRY003
        if not isinstance(parsed, dict):
            raise ValueError("Pi session records must be JSON objects")  # noqa: TRY003
        records.append(parsed)  # type: ignore[arg-type]

    if not records:
        raise ValueError("Pi session content is empty")  # noqa: TRY003

    header = records[0]
    if header.get("type") != "session" or header.get("version") != 3:
        raise ValueError("Pi session requires a version 3 session header")  # noqa: TRY003
    session_id = header.get("id")
    if not isinstance(session_id, str) or not session_id or session_id.strip() != session_id:
        raise ValueError("Pi session id must be a non-empty trimmed string")  # noqa: TRY003

    body = records[1:]
    if not body:
        return ()
    by_id: dict[str, dict[str, JsonValue]] = {}
    for record in body:
        record_id = record.get("id")
        if not isinstance(record_id, str) or not record_id:
            raise ValueError("session record id must be a non-empty string")  # noqa: TRY003
        if record_id in by_id:
            raise ValueError("duplicate session record id")  # noqa: TRY003
        by_id[record_id] = record

    for record in body:
        parent_id = record.get("parentId")
        if parent_id is None:
            continue
        if not isinstance(parent_id, str) or parent_id not in by_id:
            raise ValueError("session record references an unknown parent")  # noqa: TRY003
        if parent_id == record["id"]:
            raise ValueError("session record cannot be its own parent")  # noqa: TRY003

    leaf_id = value.leaf_id if value.leaf_id is not None else _default_leaf(body)
    if not isinstance(leaf_id, str) or leaf_id not in by_id:
        raise ValueError("selected leaf is not a session record")  # noqa: TRY003

    path: list[dict[str, JsonValue]] = []
    visited: set[str] = set()
    cursor: str | None = leaf_id
    while cursor is not None:
        if cursor in visited:
            raise ValueError("session graph contains a cycle")  # noqa: TRY003
        visited.add(cursor)
        record = by_id[cursor]
        path.append(record)
        cursor = record.get("parentId")
        if cursor is not None and not isinstance(cursor, str):
            raise ValueError("session record parentId must be a string or null")  # noqa: TRY003
    path.reverse()

    events: list[PiEvent] = []
    for record in path:
        event = _event_from_record(session_id, record)
        if event is not None:
            # Validate every identity before returning any event (atomicity).
            pi_event_source_id(event.session_id, event.event_id)
            events.append(event)
    return tuple(events)


class PiEventSourceAdapter:
    input_class = PiEvent
    name = PI_EVENT_SOURCE_NAME
    source_class = PiEventSource

    async def resolve(self, value: PiEvent, /) -> PiEventSource:
        name = pi_event_source_id(value.session_id, value.event_id)
        return PiEventSource(
            name=name,
            source_id=name,
            materialization=SourceMaterialization.CAPTURED,
            session_id=value.session_id,
            event_id=value.event_id,
            parent_id=value.parent_id,
            event_type=value.event_type,
            role=value.role,
            content=value.content,
            record=value.record,
        )

    async def read(self, source: PiEventSource, /) -> PiEvent:
        return PiEvent(
            session_id=source.session_id,
            event_id=source.event_id,
            parent_id=source.parent_id,
            event_type=source.event_type,
            role=source.role,
            content=source.content,
            record=source.record,
        )


class PiEventTextEvidenceProjection:
    name = TEXT_EVIDENCE_PROJECTION_KEY.name
    version = TEXT_EVIDENCE_PROJECTION_KEY.version
    source_class = PiEventSource
    output_class: type[BaseModel] = TextEvidence

    def project(self, source: PiEventSource, /) -> TextEvidence:
        return TextEvidence(
            source_type=PI_EVENT_SOURCE_NAME,
            source_id=source.name,
            content=source.content,
            metadata={
                "session_id": source.session_id,
                "event_id": source.event_id,
                "parent_id": source.parent_id,
                "event_type": source.event_type,
                "role": source.role,
            },
        )


PI_EVENT_SOURCE_ADAPTER = PiEventSourceAdapter()
PI_EVENT_SOURCE_DEFINITION = AdapterSourceDefinition(
    PI_EVENT_SOURCE_ADAPTER,
    version=PI_EVENT_SOURCE_VERSION,
    projections=(PiEventTextEvidenceProjection(),),
)


def _event_fingerprint(event: PiEvent) -> str:
    payload = json.dumps(event.model_dump(mode="json"), sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class PiSessionConnector:
    """Incrementally submit the evidence events visible from one session leaf."""

    name = PI_SESSION_CONNECTOR_NAME
    version = PI_SESSION_CONNECTOR_VERSION
    source_definitions = frozenset({PI_EVENT_SOURCE_NAME})

    def __init__(self, path: Path, leaf_id: str | None = None) -> None:
        self.path = Path(path)
        self.leaf_id = leaf_id

    async def run(self, session, /) -> ConnectorRunCompletion:
        events = parse_pi_session(
            PiSessionInput(content=self.path.read_bytes(), leaf_id=self.leaf_id)
        )

        checkpoint = session.checkpoint
        submitted: dict[str, str] = {}
        if isinstance(checkpoint, dict):
            for key, value in checkpoint.get("events", {}).items():
                if isinstance(key, str) and isinstance(value, str):
                    submitted[key] = value

        # Validate the whole branch before submitting anything.  A previously
        # submitted event whose payload changed is a conflict, not an update.
        pending: list[PiEvent] = []
        for event in events:
            fingerprint = _event_fingerprint(event)
            previous = submitted.get(event.event_id)
            if previous is not None:
                if previous != fingerprint:
                    raise ValueError("a previously submitted Pi event changed")  # noqa: TRY003
                continue
            pending.append(event)

        for event in pending:
            await session.submit(event.event_id, PI_EVENT_SOURCE_NAME, event)
            submitted[event.event_id] = _event_fingerprint(event)

        next_checkpoint: dict[str, JsonValue] = {
            "leaf": self.leaf_id,
            "events": submitted,
        }
        return ConnectorRunCompletion(
            status=ConnectorRunStatus.COMPLETE,
            checkpoint=next_checkpoint,
        )


__all__ = [
    "PI_EVENT_SOURCE_ADAPTER",
    "PI_EVENT_SOURCE_DEFINITION",
    "PI_SESSION_CONNECTOR_NAME",
    "PiEvent",
    "PiEventSource",
    "PiSessionConnector",
    "PiSessionInput",
    "parse_pi_session",
    "pi_event_source_id",
]
