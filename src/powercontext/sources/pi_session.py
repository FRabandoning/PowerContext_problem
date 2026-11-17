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

"""Pi v3 session branch capture with durable incremental import progress."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from urllib.parse import quote

from pydantic import BaseModel, ConfigDict, JsonValue

from powercontext.limits import MAX_SOURCE_ID_LENGTH
from powercontext.sources.connectors import ConnectorRunCompletion, ConnectorRunSession, ConnectorRunStatus
from powercontext.sources.definitions import AdapterSourceDefinition
from powercontext.sources.models import Source, SourceMaterialization
from powercontext.sources.projections import TEXT_EVIDENCE_PROJECTION_KEY, TextEvidence

PI_EVENT_SOURCE_TYPE = "pi-event"
PI_SESSION_CONNECTOR_NAME = "pi-session-history"


class PiSessionInput(BaseModel):
    """One complete Pi session archive with an optional selected leaf."""

    model_config = ConfigDict(frozen=True)

    content: bytes
    leaf_id: str | None = None


class PiEventInput(BaseModel):
    """One visible event on the selected branch, retaining its full record."""

    model_config = ConfigDict(frozen=True)

    session_id: str
    event_id: str
    parent_id: str | None
    event_type: str
    role: str
    content: str
    record: dict[str, JsonValue]


class PiEventSource(Source):
    """Durable Source description for one immutable Pi session event."""

    session_id: str
    event_id: str
    parent_id: str | None
    event_type: str
    role: str
    content: str
    record: dict[str, JsonValue]


def pi_event_source_id(session_id: str, event_id: str, /) -> str:
    """Build the stable identity of one session event."""

    source_id = f"pi:{quote(session_id, safe='')}:{quote(event_id, safe='')}"
    if len(source_id) > MAX_SOURCE_ID_LENGTH:
        raise ValueError("Pi event source identity exceeds the maximum length")  # noqa: TRY003
    return source_id


def _record_checksum(record: dict[str, JsonValue]) -> str:
    encoded = json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _parse_archive(content: bytes) -> tuple[str, list[dict[str, JsonValue]]]:
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError as error:
        raise ValueError("Pi session content must be valid UTF-8") from error  # noqa: TRY003
    records: list[dict[str, JsonValue]] = []
    for line in text.splitlines():
        if not line.strip():
            continue
        record = json.loads(line)
        if not isinstance(record, dict):
            raise ValueError("Pi session records must be JSON objects")  # noqa: TRY003
        records.append(record)
    if not records:
        raise ValueError("Pi session archive must start with a session record")  # noqa: TRY003
    header = records[0]
    if header.get("type") != "session" or header.get("version") != 3:
        raise ValueError("Pi session archive must declare a version 3 session")  # noqa: TRY003
    session_id = header.get("id")
    if not isinstance(session_id, str) or not session_id:
        raise ValueError("Pi session id must be a non-empty string")  # noqa: TRY003
    seen: set[str] = set()
    for record in records[1:]:
        event_id = record.get("id")
        if not isinstance(event_id, str) or not event_id:
            raise ValueError("Pi session record id must be a non-empty string")  # noqa: TRY003
        if event_id in seen:
            raise ValueError("Pi session record ids must be unique")  # noqa: TRY003
        if "parentId" not in record:
            raise ValueError("Pi session records must declare parentId")  # noqa: TRY003
        parent_id = record["parentId"]
        if parent_id is not None and parent_id not in seen:
            raise ValueError("Pi session parentId must be null or an earlier record")  # noqa: TRY003
        seen.add(event_id)
    return session_id, records[1:]


def _text_blocks(content: object) -> list[str]:
    if isinstance(content, str):
        return [content]
    parts: list[str] = []
    if isinstance(content, list):
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text" and isinstance(block.get("text"), str):
                parts.append(block["text"])
    return parts


def _message_body(message: dict[str, JsonValue]) -> str:
    role = message.get("role")
    if role == "toolResult":
        text = "\n".join(_text_blocks(message.get("content")))
        return f"[tool_result {message.get('toolName')} {message.get('toolCallId')}] {text}"
    if role == "bashExecution":
        return f"$ {message.get('command', '')}\n{message.get('output', '')}"
    content = message.get("content")
    if isinstance(content, str):
        return content
    parts: list[str] = []
    if isinstance(content, list):
        for block in content:
            if not isinstance(block, dict):
                continue
            block_type = block.get("type")
            if block_type == "text" and isinstance(block.get("text"), str):
                parts.append(block["text"])
            elif block_type == "toolCall":
                arguments = json.dumps(
                    block.get("arguments", {}), ensure_ascii=False, sort_keys=True, separators=(",", ":")
                )
                parts.append(f"[tool_call {block.get('name')} {block.get('id')}] {arguments}")
    return "\n".join(parts)


def _visible_event(session_id: str, record: dict[str, JsonValue]) -> PiEventInput | None:
    event_type = record.get("type")
    if not isinstance(event_type, str):
        raise ValueError("Pi session record type must be a string")  # noqa: TRY003
    if event_type == "message":
        message = record.get("message")
        if not isinstance(message, dict) or not isinstance(message.get("role"), str):
            raise ValueError("Pi message records must declare a role")  # noqa: TRY003
        role = message["role"]
        body = _message_body(message)
    elif event_type in {"compaction", "branch_summary"}:
        role = event_type
        summary = record.get("summary", "")
        body = summary if isinstance(summary, str) else ""
    elif event_type == "custom_message":
        role = "custom"
        body = "\n".join(_text_blocks(record.get("content")))
    else:
        return None
    if not body:
        return None
    return PiEventInput(
        session_id=session_id,
        event_id=record["id"],
        parent_id=record["parentId"],
        event_type=event_type,
        role=role,
        content=f"{role}: {body}",
        record=record,
    )


def _branch_events(
    session_id: str, records: list[dict[str, JsonValue]], leaf_id: str | None
) -> tuple[PiEventInput, ...]:
    if not records:
        raise ValueError("Pi session archive contains no selectable node")  # noqa: TRY003
    by_id = {record["id"]: record for record in records}
    selected = leaf_id if leaf_id is not None else records[-1]["id"]
    if selected not in by_id:
        raise ValueError("selected leaf does not exist in the Pi session")  # noqa: TRY003
    path: list[dict[str, JsonValue]] = []
    cursor: str | None = selected
    while cursor is not None:
        record = by_id[cursor]
        path.append(record)
        cursor = record["parentId"]
    path.reverse()
    events = []
    for record in path:
        event = _visible_event(session_id, record)
        if event is not None:
            events.append(event)
    return tuple(events)


def parse_pi_session(value: PiSessionInput, /) -> tuple[PiEventInput, ...]:
    """Parse one archive atomically into the visible events of the selected branch."""

    session = PiSessionInput.model_validate(value)
    session_id, records = _parse_archive(session.content)
    return _branch_events(session_id, records, session.leaf_id)


class _PiEventAdapter:
    input_class = PiEventInput
    name = PI_EVENT_SOURCE_TYPE
    source_class = PiEventSource

    async def resolve(self, value: PiEventInput, /) -> PiEventSource:
        event = PiEventInput.model_validate(value)
        source_id = pi_event_source_id(event.session_id, event.event_id)
        return PiEventSource(
            name=source_id,
            definition_version="1",
            materialization=SourceMaterialization.CAPTURED,
            session_id=event.session_id,
            event_id=event.event_id,
            parent_id=event.parent_id,
            event_type=event.event_type,
            role=event.role,
            content=event.content,
            record=event.record,
        )

    async def read(self, source: PiEventSource, /) -> PiEventInput:
        return PiEventInput(
            session_id=source.session_id,
            event_id=source.event_id,
            parent_id=source.parent_id,
            event_type=source.event_type,
            role=source.role,
            content=source.content,
            record=source.record,
        )


class _PiEventTextEvidenceProjection:
    name = TEXT_EVIDENCE_PROJECTION_KEY.name
    version = TEXT_EVIDENCE_PROJECTION_KEY.version
    source_class = PiEventSource
    output_class = TextEvidence

    def project(self, source: PiEventSource, /) -> TextEvidence:
        return TextEvidence(
            source_type=PI_EVENT_SOURCE_TYPE,
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


PI_EVENT_SOURCE_DEFINITION = AdapterSourceDefinition(
    adapter=_PiEventAdapter(),
    version="1",
    projections=(_PiEventTextEvidenceProjection(),),
)


class PiSessionConnector:
    """Import the selected Pi branch incrementally through ConnectorLifecycle."""

    name = PI_SESSION_CONNECTOR_NAME
    version = "1"
    source_definitions = frozenset({PI_EVENT_SOURCE_TYPE})

    def __init__(self, path: str | Path, leaf_id: str | None = None) -> None:
        self._path = Path(path)
        self._leaf_id = leaf_id

    async def run(self, session: ConnectorRunSession, /) -> ConnectorRunCompletion:
        session_id, records = _parse_archive(self._path.read_bytes())
        imported: dict[str, str] = dict(session.checkpoint) if session.checkpoint else {}
        for record in records:
            identity = pi_event_source_id(session_id, record["id"])
            previous = imported.get(identity)
            if previous is not None and previous != _record_checksum(record):
                raise ValueError("an imported Pi event was rewritten")  # noqa: TRY003
        events = _branch_events(session_id, records, self._leaf_id)
        updated = dict(imported)
        for event in events:
            identity = pi_event_source_id(event.session_id, event.event_id)
            if identity in imported:
                continue
            await session.submit(event.event_id, PI_EVENT_SOURCE_TYPE, event)
            updated[identity] = _record_checksum(event.record)
        return ConnectorRunCompletion(status=ConnectorRunStatus.COMPLETE, checkpoint=updated)


__all__ = [
    "PI_EVENT_SOURCE_DEFINITION",
    "PiEventInput",
    "PiEventSource",
    "PiSessionConnector",
    "PiSessionInput",
    "parse_pi_session",
    "pi_event_source_id",
]