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

"""Deterministic context packs and prefix deltas with a canonical wire format."""

from __future__ import annotations

import base64
import hashlib
import json
import re
from dataclasses import dataclass

CONTEXT_PACK_SCHEMA = "powercontext.context-pack.v1"
CONTEXT_PACK_DELTA_SCHEMA = "powercontext.context-pack-delta.v1"

_DIGEST_PATTERN = re.compile(r"sha256:[0-9a-f]{64}")


class ContextPackError(ValueError):
    """One context pack or delta violates the wire or reconstruction contract."""


def _canonical(payload: dict) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _pack_digest(items: tuple[bytes, ...]) -> str:
    unsigned = {
        "schema": CONTEXT_PACK_SCHEMA,
        "items": [base64.b64encode(item).decode("ascii") for item in items],
    }
    return "sha256:" + hashlib.sha256(_canonical(unsigned)).hexdigest()


def _encode_items(items: tuple[bytes, ...]) -> list[str]:
    return [base64.b64encode(item).decode("ascii") for item in items]


def _decode_items(value: object) -> tuple[bytes, ...]:
    if not isinstance(value, list):
        raise ContextPackError("items must be a JSON array")  # noqa: TRY003
    items = []
    for entry in value:
        if not isinstance(entry, str):
            raise ContextPackError("items must be Base64 strings")  # noqa: TRY003
        try:
            decoded = base64.b64decode(entry, validate=True)
        except (ValueError, TypeError) as error:
            raise ContextPackError("items must be standard Base64") from error  # noqa: TRY003
        if base64.b64encode(decoded).decode("ascii") != entry:
            raise ContextPackError("items must use canonical Base64")  # noqa: TRY003
        items.append(decoded)
    return tuple(items)


def _validate_digest(value: object) -> str:
    if not isinstance(value, str) or not _DIGEST_PATTERN.fullmatch(value):
        raise ContextPackError("digest must use sha256:<lowercase hex>")  # noqa: TRY003
    return value


@dataclass(frozen=True, slots=True)
class ContextPack:
    """One ordered byte-chunk pack with a self-inclusive canonical wire size."""

    items: tuple[bytes, ...]
    digest: str
    byte_count: int

    @classmethod
    def build(cls, items: tuple[bytes, ...], /) -> ContextPack:
        items = tuple(items)
        digest = _pack_digest(items)
        unsigned = {"schema": CONTEXT_PACK_SCHEMA, "items": _encode_items(items), "digest": digest}
        count = 0
        while True:
            data = _canonical({**unsigned, "byte_count": count})
            if len(data) == count:
                return cls(items=items, digest=digest, byte_count=count)
            count = len(data)

    @classmethod
    def from_content(cls, content: str | None, /, *, chunk_bytes: int = 512) -> ContextPack:
        if type(chunk_bytes) is not int or chunk_bytes < 1:
            raise ContextPackError("chunk_bytes must be a positive integer")  # noqa: TRY003
        if content is None:
            return cls.build(())
        data = content.encode("utf-8")
        items: list[bytes] = []
        start = 0
        while start < len(data):
            end = min(start + chunk_bytes, len(data))
            if end < len(data):
                while end > start and (data[end] & 0xC0) == 0x80:
                    end -= 1
                if end == start:
                    raise ContextPackError("chunk_bytes leaves no legal UTF-8 boundary")  # noqa: TRY003
            items.append(data[start:end])
            start = end
        return cls.build(tuple(items))

    def to_bytes(self) -> bytes:
        return _canonical(
            {
                "schema": CONTEXT_PACK_SCHEMA,
                "items": _encode_items(self.items),
                "digest": self.digest,
                "byte_count": self.byte_count,
            }
        )

    @classmethod
    def from_bytes(cls, data: bytes, /) -> ContextPack:
        try:
            payload = json.loads(data)
        except (ValueError, TypeError, UnicodeDecodeError) as error:
            raise ContextPackError("pack wire payload must be JSON") from error  # noqa: TRY003
        if not isinstance(payload, dict) or set(payload) != {"schema", "items", "digest", "byte_count"}:
            raise ContextPackError("pack wire payload has unexpected fields")  # noqa: TRY003
        if payload["schema"] != CONTEXT_PACK_SCHEMA:
            raise ContextPackError("pack wire schema is not supported")  # noqa: TRY003
        items = _decode_items(payload["items"])
        digest = _validate_digest(payload["digest"])
        if digest != _pack_digest(items):
            raise ContextPackError("pack digest does not match its items")  # noqa: TRY003
        byte_count = payload["byte_count"]
        if type(byte_count) is not int or byte_count != len(data):
            raise ContextPackError("pack byte_count must equal the wire length")  # noqa: TRY003
        return cls(items=items, digest=digest, byte_count=byte_count)


@dataclass(frozen=True, slots=True)
class ContextPackDelta:
    """One prefix-preserving change between two context packs."""

    base_digest: str
    target_digest: str
    prefix_items: int
    appended: tuple[bytes, ...]

    @classmethod
    def between(cls, base: ContextPack, target: ContextPack, /) -> ContextPackDelta:
        prefix = 0
        for base_item, target_item in zip(base.items, target.items, strict=False):
            if base_item != target_item:
                break
            prefix += 1
        return cls(
            base_digest=base.digest,
            target_digest=target.digest,
            prefix_items=prefix,
            appended=target.items[prefix:],
        )

    @property
    def byte_count(self) -> int:
        return len(self.to_bytes())

    def to_bytes(self) -> bytes:
        return _canonical(
            {
                "schema": CONTEXT_PACK_DELTA_SCHEMA,
                "base_digest": self.base_digest,
                "target_digest": self.target_digest,
                "prefix_items": self.prefix_items,
                "appended": _encode_items(self.appended),
            }
        )

    @classmethod
    def from_bytes(cls, data: bytes, /) -> ContextPackDelta:
        try:
            payload = json.loads(data)
        except (ValueError, TypeError, UnicodeDecodeError) as error:
            raise ContextPackError("delta wire payload must be JSON") from error  # noqa: TRY003
        if not isinstance(payload, dict) or set(payload) != {
            "schema",
            "base_digest",
            "target_digest",
            "prefix_items",
            "appended",
        }:
            raise ContextPackError("delta wire payload has unexpected fields")  # noqa: TRY003
        if payload["schema"] != CONTEXT_PACK_DELTA_SCHEMA:
            raise ContextPackError("delta wire schema is not supported")  # noqa: TRY003
        prefix = payload["prefix_items"]
        if type(prefix) is not int or prefix < 0:
            raise ContextPackError("prefix_items must be a non-negative integer")  # noqa: TRY003
        return cls(
            base_digest=_validate_digest(payload["base_digest"]),
            target_digest=_validate_digest(payload["target_digest"]),
            prefix_items=prefix,
            appended=_decode_items(payload["appended"]),
        )


def apply_context_pack_delta(base: ContextPack, delta: ContextPackDelta, /) -> ContextPack:
    """Rebuild the exact target pack from one base pack and one delta."""

    if delta.base_digest != base.digest:
        raise ContextPackError("delta does not apply to this base pack")  # noqa: TRY003
    if delta.prefix_items > len(base.items):
        raise ContextPackError("delta prefix exceeds the base pack")  # noqa: TRY003
    rebuilt = ContextPack.build(base.items[: delta.prefix_items] + delta.appended)
    if rebuilt.digest != delta.target_digest:
        raise ContextPackError("delta does not reconstruct the target digest")  # noqa: TRY003
    return rebuilt


__all__ = [
    "CONTEXT_PACK_DELTA_SCHEMA",
    "CONTEXT_PACK_SCHEMA",
    "ContextPack",
    "ContextPackDelta",
    "ContextPackError",
    "apply_context_pack_delta",
]