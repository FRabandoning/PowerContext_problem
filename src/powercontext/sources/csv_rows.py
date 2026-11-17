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

"""Row-level CSV capture with stable, citeable row identity.

Each data record of an offline CSV becomes an independent ``CAPTURED``
Source.  Identity is derived from the document id and the primary-key value,
so it is stable across reordering and independent of physical row position.
"""

from __future__ import annotations

import csv
import io
import json
from typing import Annotated, Literal
from urllib.parse import quote

from pydantic import BaseModel, Field, JsonValue

from powercontext.limits import MAX_SOURCE_ID_LENGTH
from powercontext.sources import AdapterSourceDefinition, TextEvidence
from powercontext.sources.models import Source, SourceMaterialization
from powercontext.sources.projections import TEXT_EVIDENCE_PROJECTION_KEY

CSV_ROW_SOURCE_NAME = "csv-row"
CSV_ROW_SOURCE_VERSION = "1"

NonEmptyText = Annotated[str, Field(min_length=1)]


class CSVDocumentInput(BaseModel):
    document_id: NonEmptyText
    primary_key: NonEmptyText
    content: bytes


class CSVRowInput(BaseModel):
    document_id: str
    primary_key: str
    primary_key_value: str
    columns: tuple[str, ...]
    values: tuple[str, ...]


class CSVRowSource(Source):
    source_id: str
    document_id: str
    primary_key: str
    primary_key_value: str
    columns: tuple[str, ...]
    values: tuple[str, ...]


def _validate_document_identity(document_id: str, primary_key: str) -> None:
    if not document_id:
        raise ValueError("document_id must not be empty")  # noqa: TRY003
    if document_id != document_id.strip():
        raise ValueError("document_id must not have leading or trailing whitespace")  # noqa: TRY003
    if not primary_key:
        raise ValueError("primary_key must not be empty")  # noqa: TRY003


def csv_row_source_id(document_id: str, primary_key_value: str) -> str:
    """Build a delimiter-safe, bounded identity for one CSV row."""

    _validate_document_identity(document_id, "id")
    encoded_document = quote(document_id, safe="")
    encoded_key = quote(primary_key_value, safe="")
    source_id = f"csv:{encoded_document}:{encoded_key}"
    if len(source_id) > MAX_SOURCE_ID_LENGTH:
        raise ValueError("csv row source id exceeds the maximum length")  # noqa: TRY003
    return source_id


def parse_csv_document(value: CSVDocumentInput) -> tuple[CSVRowInput, ...]:
    """Parse and validate an entire CSV document, or fail atomically."""

    _validate_document_identity(value.document_id, value.primary_key)

    try:
        text = value.content.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ValueError("CSV content is not valid UTF-8") from exc  # noqa: TRY003

    reader = csv.reader(io.StringIO(text, newline=""), delimiter=",", quotechar='"', strict=True)
    try:
        records = list(reader)
    except csv.Error as exc:
        raise ValueError("malformed CSV content") from exc  # noqa: TRY003

    if not records:
        raise ValueError("CSV content is empty")  # noqa: TRY003

    columns = tuple(records[0])
    if not columns:
        raise ValueError("CSV header is empty")  # noqa: TRY003
    seen_columns: set[str] = set()
    for column in columns:
        if not column.strip():
            raise ValueError("CSV header fields must be non-blank")  # noqa: TRY003
        if column in seen_columns:
            raise ValueError("CSV header fields must be unique")  # noqa: TRY003
        seen_columns.add(column)
    if value.primary_key not in seen_columns:
        raise ValueError("primary_key is not a header column")  # noqa: TRY003
    primary_key_index = columns.index(value.primary_key)

    rows: list[CSVRowInput] = []
    seen_keys: set[str] = set()
    for record in records[1:]:
        # Ignore fully empty physical records after the header.
        if len(record) == 0 or (len(record) == 1 and record[0] == ""):
            continue
        if len(record) != len(columns):
            raise ValueError("CSV data record width does not match header")  # noqa: TRY003
        primary_key_value = record[primary_key_index]
        if not primary_key_value or not primary_key_value.strip():
            raise ValueError("primary key value must not be blank")  # noqa: TRY003
        if primary_key_value in seen_keys:
            raise ValueError("duplicate primary key value")  # noqa: TRY003
        seen_keys.add(primary_key_value)
        rows.append(
            CSVRowInput(
                document_id=value.document_id,
                primary_key=value.primary_key,
                primary_key_value=primary_key_value,
                columns=columns,
                values=tuple(record),
            )
        )

    # Validate every identity before returning any row (atomicity).
    for row in rows:
        csv_row_source_id(row.document_id, row.primary_key_value)
    return tuple(rows)


class CSVRowSourceAdapter:
    input_class = CSVRowInput
    name = CSV_ROW_SOURCE_NAME
    source_class = CSVRowSource

    async def resolve(self, value: CSVRowInput, /) -> CSVRowSource:
        return CSVRowSource(
            name=csv_row_source_id(value.document_id, value.primary_key_value),
            source_id=csv_row_source_id(value.document_id, value.primary_key_value),
            materialization=SourceMaterialization.CAPTURED,
            document_id=value.document_id,
            primary_key=value.primary_key,
            primary_key_value=value.primary_key_value,
            columns=value.columns,
            values=value.values,
        )

    async def read(self, source: CSVRowSource, /) -> CSVRowInput:
        return CSVRowInput(
            document_id=source.document_id,
            primary_key=source.primary_key,
            primary_key_value=source.primary_key_value,
            columns=source.columns,
            values=source.values,
        )


class CSVRowTextEvidenceProjection:
    name = TEXT_EVIDENCE_PROJECTION_KEY.name
    version = TEXT_EVIDENCE_PROJECTION_KEY.version
    source_class = CSVRowSource
    output_class: type[BaseModel] = TextEvidence

    def project(self, source: CSVRowSource, /) -> TextEvidence:
        row_object = {column: value for column, value in zip(source.columns, source.values, strict=True)}
        content = json.dumps(row_object, ensure_ascii=False, separators=(",", ":"))
        metadata: dict[str, JsonValue] = {
            "document_id": source.document_id,
            "primary_key": source.primary_key,
            "primary_key_value": source.primary_key_value,
            "columns": list(source.columns),
            "row": row_object,
        }
        return TextEvidence(
            source_type=CSV_ROW_SOURCE_NAME,
            source_id=source.source_id,
            content=content,
            metadata=metadata,
        )


CSV_ROW_SOURCE_ADAPTER = CSVRowSourceAdapter()
CSV_ROW_SOURCE_DEFINITION = AdapterSourceDefinition(
    CSV_ROW_SOURCE_ADAPTER,
    version=CSV_ROW_SOURCE_VERSION,
    projections=(CSVRowTextEvidenceProjection(),),
)


__all__ = [
    "CSV_ROW_SOURCE_ADAPTER",
    "CSV_ROW_SOURCE_DEFINITION",
    "CSVDocumentInput",
    "CSVRowInput",
    "CSVRowSource",
    "csv_row_source_id",
    "parse_csv_document",
]
