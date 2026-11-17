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

"""Prefix-reuse context packs for incremental context transfer.

A :class:`ContextPack` is a deterministic chunked representation of a full
context body. A :class:`ContextPackDelta` reuses a valid public prefix of a
base pack and carries only the appended suffix. Both wire forms are canonical
JSON with a SHA-256 integrity digest.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
from dataclasses import dataclass

PACK_SCHEMA = "powercontext.context-pack.v1"
DELTA_SCHEMA = "powercontext.context-pack-delta.v1"
DEFAULT_CHUNK_BYTES = 512


class ContextPackError(ValueError):
    """Raised when a pack, delta or wire payload violates the protocol."""


def _canonical_json(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")


def _digest(unsigned: dict[str, object]) -> str:
    return "sha256:" + hashlib.sha256(_canonical_json(unsigned)).hexdigest()


def _is_int(value: object) -> bool:
    return type(value) is int


@dataclass(frozen=True)
class ContextPack:
    items: tuple[bytes, ...]
    digest: str
    byte_count: int

    @classmethod
    def build(cls, items: tuple[bytes, ...]) -> ContextPack:
        normalized = tuple(bytes(item) for item in items)
        unsigned = {
            "schema": PACK_SCHEMA,
            "items": [base64.b64encode(item).decode("ascii") for item in normalized],
        }
        digest = _digest(unsigned)
        payload = {**unsigned, "digest": digest}
        # byte_count includes its own field; iterate to a fixed point.
        count = 0
        for _ in range(8):
            candidate = {**payload, "byte_count": count}
            new_count = len(_canonical_json(candidate))
            if new_count == count:
                break
            count = new_count
        return cls(normalized, digest, count)

    @classmethod
    def from_content(cls, content: str, *, chunk_bytes: int = DEFAULT_CHUNK_BYTES) -> ContextPack:
        if not _is_int(chunk_bytes) or chunk_bytes <= 0:
            raise ContextPackError("chunk_bytes must be a positive integer")  # noqa: TRY003
        data = content.encode("utf-8")
        chunks: list[bytes] = []
        offset = 0
        length = len(data)
        while offset < length:
            end = min(offset + chunk_bytes, length)
            while end > offset:
                try:
                    data[offset:end].decode("utf-8")
                except UnicodeDecodeError:
                    end -= 1
                    continue
                break
            if end == offset:
                raise ContextPackError("cannot form a valid UTF-8 chunk within the limit")  # noqa: TRY003
            chunks.append(data[offset:end])
            offset = end
        return cls.build(tuple(chunks))

    def to_bytes(self) -> bytes:
        payload = {
            "schema": PACK_SCHEMA,
            "items": [base64.b64encode(item).decode("ascii") for item in self.items],
            "digest": self.digest,
            "byte_count": self.byte_count,
        }
        return _canonical_json(payload)

    @classmethod
    def from_bytes(cls, raw: bytes) -> ContextPack:
        try:
            payload = json.loads(raw)
        except (ValueError, UnicodeDecodeError) as exc:
            raise ContextPackError("invalid pack JSON") from exc  # noqa: TRY003
        if not isinstance(payload, dict):
            raise ContextPackError("pack payload must be an object")  # noqa: TRY003
        expected_keys = {"schema", "items", "digest", "byte_count"}
        if set(payload) != expected_keys:
            raise ContextPackError("pack payload has invalid fields")  # noqa: TRY003
        if payload["schema"] != PACK_SCHEMA:
            raise ContextPackError("pack schema mismatch")  # noqa: TRY003
        if not isinstance(payload["items"], list):
            raise ContextPackError("pack items must be a list")  # noqa: TRY003
        try:
            items = tuple(base64.b64decode(value, validate=True) for value in payload["items"])
        except (binascii.Error, ValueError, TypeError) as exc:
            raise ContextPackError("invalid pack item encoding") from exc  # noqa: TRY003
        unsigned = {
            "schema": PACK_SCHEMA,
            "items": [base64.b64encode(item).decode("ascii") for item in items],
        }
        if payload["digest"] != _digest(unsigned):
            raise ContextPackError("pack digest mismatch")  # noqa: TRY003
        pack = cls(items, payload["digest"], 0)
        if not _is_int(payload["byte_count"]) or payload["byte_count"] != len(pack.to_bytes()):
            raise ContextPackError("pack byte_count mismatch")  # noqa: TRY003
        return cls(items, payload["digest"], payload["byte_count"])


@dataclass(frozen=True)
class ContextPackDelta:
    base_digest: str
    target_digest: str
    prefix_items: int
    appended: tuple[bytes, ...]

    @classmethod
    def between(cls, base: ContextPack, target: ContextPack) -> ContextPackDelta:
        prefix = 0
        maximum = min(len(base.items), len(target.items))
        while prefix < maximum and base.items[prefix] == target.items[prefix]:
            prefix += 1
        appended = target.items[prefix:]
        return cls(base.digest, target.digest, prefix, tuple(appended))

    @property
    def byte_count(self) -> int:
        return len(self.to_bytes())

    def to_bytes(self) -> bytes:
        payload = {
            "schema": DELTA_SCHEMA,
            "base_digest": self.base_digest,
            "target_digest": self.target_digest,
            "prefix_items": self.prefix_items,
            "appended": [base64.b64encode(item).decode("ascii") for item in self.appended],
        }
        return _canonical_json(payload)

    @classmethod
    def from_bytes(cls, raw: bytes) -> ContextPackDelta:
        try:
            payload = json.loads(raw)
        except (ValueError, UnicodeDecodeError) as exc:
            raise ContextPackError("invalid delta JSON") from exc  # noqa: TRY003
        if not isinstance(payload, dict):
            raise ContextPackError("delta payload must be an object")  # noqa: TRY003
        expected_keys = {"schema", "base_digest", "target_digest", "prefix_items", "appended"}
        if set(payload) != expected_keys:
            raise ContextPackError("delta payload has invalid fields")  # noqa: TRY003
        if payload["schema"] != DELTA_SCHEMA:
            raise ContextPackError("delta schema mismatch")  # noqa: TRY003
        prefix = payload["prefix_items"]
        if not _is_int(prefix) or prefix < 0:
            raise ContextPackError("delta prefix_items must be a non-negative integer")  # noqa: TRY003
        if not isinstance(payload["appended"], list):
            raise ContextPackError("delta appended must be a list")  # noqa: TRY003
        try:
            appended = tuple(base64.b64decode(value, validate=True) for value in payload["appended"])
        except (binascii.Error, ValueError, TypeError) as exc:
            raise ContextPackError("invalid delta appended encoding") from exc  # noqa: TRY003
        return cls(payload["base_digest"], payload["target_digest"], prefix, appended)


def apply_context_pack_delta(base: ContextPack, delta: ContextPackDelta) -> ContextPack:
    if delta.base_digest != base.digest:
        raise ContextPackError("delta base digest mismatch")  # noqa: TRY003
    if delta.prefix_items > len(base.items):
        raise ContextPackError("delta prefix exceeds base items")  # noqa: TRY003
    items = base.items[: delta.prefix_items] + delta.appended
    target = ContextPack.build(items)
    if target.digest != delta.target_digest:
        raise ContextPackError("delta target digest mismatch")  # noqa: TRY003
    return target


__all__ = [
    "ContextPack",
    "ContextPackDelta",
    "ContextPackError",
    "apply_context_pack_delta",
]
