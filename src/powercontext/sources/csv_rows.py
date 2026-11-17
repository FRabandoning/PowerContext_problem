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

"""Row-level CSV Sources with stable primary-key identity and text evidence."""

from __future__ import annotations

import csv
import io
import json
from urllib.parse import quote

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from powercontext.limits import MAX_SOURCE_ID_LENGTH
from powercontext.sources.definitions import AdapterSourceDefinition
from powercontext.sources.models import Source, SourceMaterialization
from powercontext.sources.projections import TEXT_EVIDENCE_PROJECTION_KEY, TextEvidence

CSV_ROW_SOURCE_TYPE = "csv-row"


def _validate_document_id(value: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError("document_id must be a non-empty string without surrounding whitespace")  # noqa: TRY003
    return value


def _validate_primary_key(value: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError("primary_key must be a non-empty string")  # noqa: TRY003
    return value


def _validate_primary_key_value(value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("primary_key_value must be a non-blank string")  # noqa: TRY003
    return value


def _validate_columns(columns: tuple[str, ...]) -> tuple[str, ...]:
    if not columns:
        raise ValueError("columns must not be empty")  # noqa: TRY003
    for column in columns:
        if not isinstance(column, str) or not column.strip():
            raise ValueError("header columns must be non-blank strings")  # noqa: TRY003
    if len(set(columns)) != len(columns):
        raise ValueError("header columns must be unique")  # noqa: TRY003
    return columns


class CSVDocumentInput(BaseModel):
    """One whole CSV document offered for row-level capture."""

    model_config = ConfigDict(frozen=True)

    document_id: str
    primary_key: str
    content: bytes

    @field_validator("document_id")
    @classmethod
    def _check_document_id(cls, value: str) -> str:
        return _validate_document_id(value)

    @field_validator("primary_key")
    @classmethod
    def _check_primary_key(cls, value: str) -> str:
        return _validate_primary_key(value)


class CSVRowInput(BaseModel):
    """One parsed CSV row kept verbatim in header column order."""

    model_config = ConfigDict(frozen=True)

    document_id: str
    primary_key: str
    primary_key_value: str
    columns: tuple[str, ...]
    values: tuple[str, ...]

    @field_validator("document_id")
    @classmethod
    def _check_document_id(cls, value: str) -> str:
        return _validate_document_id(value)

    @field_validator("primary_key")
    @classmethod
    def _check_primary_key(cls, value: str) -> str:
        return _validate_primary_key(value)

    @field_validator("primary_key_value")
    @classmethod
    def _check_primary_key_value(cls, value: str) -> str:
        return _validate_primary_key_value(value)

    @field_validator("columns")
    @classmethod
    def _check_columns(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _validate_columns(value)

    @model_validator(mode="after")
    def _check_row(self) -> CSVRowInput:
        if len(self.values) != len(self.columns):
            raise ValueError("row width must match the header width")  # noqa: TRY003
        if self.primary_key not in self.columns:
            raise ValueError("primary_key must exactly match one header column")  # noqa: TRY003
        return self


class CSVRowSource(Source):
    """Durable Source description for one CSV row identified by its primary key."""

    source_id: str
    document_id: str
    primary_key: str
    primary_key_value: str
    columns: tuple[str, ...]
    values: tuple[str, ...]


def csv_row_source_id(document_id: str, primary_key_value: str, /) -> str:
    """Build the delimiter-safe bounded identity of one CSV row."""

    _validate_document_id(document_id)
    _validate_primary_key_value(primary_key_value)
    source_id = f"csv:{quote(document_id, safe='')}:{quote(primary_key_value, safe='')}"
    if len(source_id) > MAX_SOURCE_ID_LENGTH:
        raise ValueError("CSV row source identity exceeds the maximum length")  # noqa: TRY003
    return source_id


def parse_csv_document(value: CSVDocumentInput, /) -> tuple[CSVRowInput, ...]:
    """Parse one CSV document atomically into verbatim primary-keyed rows."""

    document = CSVDocumentInput.model_validate(value)
    try:
        text = document.content.decode("utf-8-sig")
    except UnicodeDecodeError as error:
        raise ValueError("CSV content must be valid UTF-8") from error  # noqa: TRY003
    reader = csv.reader(io.StringIO(text, newline=""), strict=True)
    try:
        records = list(reader)
    except csv.Error as error:
        raise ValueError("CSV content is not well-formed") from error  # noqa: TRY003
    if not records:
        raise ValueError("CSV document must contain a header record")  # noqa: TRY003
    header = tuple(records[0])
    _validate_columns(header)
    if document.primary_key not in header:
        raise ValueError("primary_key must exactly match one header column")  # noqa: TRY003
    key_index = header.index(document.primary_key)

    rows: list[CSVRowInput] = []
    seen_keys: set[str] = set()
    for record in records[1:]:
        if not record:
            continue
        if len(record) != len(header):
            raise ValueError("row width must match the header width")  # noqa: TRY003
        key_value = record[key_index]
        _validate_primary_key_value(key_value)
        if key_value in seen_keys:
            raise ValueError("primary key values must be unique within one document")  # noqa: TRY003
        seen_keys.add(key_value)
        rows.append(
            CSVRowInput(
                document_id=document.document_id,
                primary_key=document.primary_key,
                primary_key_value=key_value,
                columns=header,
                values=tuple(record),
            )
        )
    return tuple(rows)


def _row_mapping(source: CSVRowSource) -> dict[str, str]:
    return dict(zip(source.columns, source.values, strict=True))


class _CSVRowAdapter:
    input_class = CSVRowInput
    name = CSV_ROW_SOURCE_TYPE
    source_class = CSVRowSource

    async def resolve(self, value: CSVRowInput, /) -> CSVRowSource:
        row = CSVRowInput.model_validate(value)
        source_id = csv_row_source_id(row.document_id, row.primary_key_value)
        return CSVRowSource(
            name=source_id,
            definition_version="1",
            materialization=SourceMaterialization.CAPTURED,
            source_id=source_id,
            document_id=row.document_id,
            primary_key=row.primary_key,
            primary_key_value=row.primary_key_value,
            columns=row.columns,
            values=row.values,
        )

    async def read(self, source: CSVRowSource, /) -> CSVRowInput:
        return CSVRowInput(
            document_id=source.document_id,
            primary_key=source.primary_key,
            primary_key_value=source.primary_key_value,
            columns=source.columns,
            values=source.values,
        )


class _CSVRowTextEvidenceProjection:
    name = TEXT_EVIDENCE_PROJECTION_KEY.name
    version = TEXT_EVIDENCE_PROJECTION_KEY.version
    source_class = CSVRowSource
    output_class = TextEvidence

    def project(self, source: CSVRowSource, /) -> TextEvidence:
        mapping = _row_mapping(source)
        return TextEvidence(
            source_type=CSV_ROW_SOURCE_TYPE,
            source_id=source.source_id,
            content=json.dumps(mapping, ensure_ascii=False, separators=(",", ":")),
            metadata={
                "document_id": source.document_id,
                "primary_key": source.primary_key,
                "primary_key_value": source.primary_key_value,
                "columns": list(source.columns),
                "row": mapping,
            },
        )


CSV_ROW_SOURCE_DEFINITION = AdapterSourceDefinition(
    adapter=_CSVRowAdapter(),
    version="1",
    projections=(_CSVRowTextEvidenceProjection(),),
)

__all__ = [
    "CSV_ROW_SOURCE_DEFINITION",
    "CSVDocumentInput",
    "CSVRowInput",
    "CSVRowSource",
    "csv_row_source_id",
    "parse_csv_document",
]