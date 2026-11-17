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

"""PDF page retrieval with content-addressed, citeable page identity.

Each page of a PDF becomes an independent captured Source.  Its identity
includes the SHA-256 of the entire original document, so any document revision
produces a fresh identity while an unchanged document re-imports idempotently.
"""

from __future__ import annotations

import hashlib
import io
from typing import Annotated
from urllib.parse import quote

from pypdf import PdfReader
from pydantic import BaseModel, Field

from powercontext.builtin.runtime import MemoryCitation, SearchMemoryRequest
from powercontext.limits import MAX_SOURCE_ID_LENGTH
from powercontext.sources import AdapterSourceDefinition, SourceMaterialization, TextEvidence
from powercontext.sources.models import Source
from powercontext.sources.projections import TEXT_EVIDENCE_PROJECTION_KEY

PDF_PAGE_SOURCE_NAME = "pdf-page"
PDF_PAGE_SOURCE_VERSION = "1"

NonEmptyText = Annotated[str, Field(min_length=1)]


class PDFDocumentInput(BaseModel):
    """A raw PDF document and the caller-stable id assigned to it."""

    document_id: NonEmptyText
    content: bytes


class PDFPage(BaseModel):
    """One extracted page of a PDF document."""

    document_id: str
    document_sha256: str
    page_number: int
    text: str


class PDFPageSource(Source):
    """Durable captured Source for one PDF page."""

    source_id: str
    document_id: str
    document_sha256: str
    page_number: int
    text: str


def _validate_document_id(document_id: str) -> None:
    if not document_id:
        raise ValueError("document_id must not be empty")  # noqa: TRY003
    if document_id != document_id.strip():
        raise ValueError("document_id must not have leading or trailing whitespace")  # noqa: TRY003


def pdf_page_source_id(document_id: str, document_sha256: str, page_number: int) -> str:
    """Build a delimiter-safe, bounded, content-addressed page identity."""

    _validate_document_id(document_id)
    if page_number < 1:
        raise ValueError("page_number must be positive")  # noqa: TRY003
    name = f"pdf:{quote(document_id, safe='')}:{document_sha256}:{page_number}"
    if len(name) > MAX_SOURCE_ID_LENGTH:
        raise ValueError("pdf page source id exceeds the maximum length")  # noqa: TRY003
    return name


def parse_pdf_document(value: PDFDocumentInput, /) -> tuple[PDFPage, ...]:
    """Parse and validate every page of a PDF document, or fail atomically."""

    if not isinstance(value, PDFDocumentInput):
        raise ValueError("parse_pdf_document requires PDFDocumentInput")  # noqa: TRY003
    _validate_document_id(value.document_id)

    document_sha256 = hashlib.sha256(value.content).hexdigest()
    try:
        reader = PdfReader(io.BytesIO(value.content))
        if reader.is_encrypted:
            # Pi-style PDFs that are encrypted with an empty user password are
            # readable without a secret; any other encryption is not supported.
            if reader.decrypt("") == 0:
                raise ValueError("PDF requires a non-empty password")  # noqa: TRY003
        pages = tuple(reader.pages)
        texts = tuple(page.extract_text() or "" for page in pages)
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError("PDF content is not a readable document") from exc  # noqa: TRY003

    result: list[PDFPage] = []
    for index, text in enumerate(texts, start=1):
        page = PDFPage(
            document_id=value.document_id,
            document_sha256=document_sha256,
            page_number=index,
            text=text,
        )
        # Validate every identity before returning any page (atomicity).
        pdf_page_source_id(page.document_id, page.document_sha256, page.page_number)
        result.append(page)
    return tuple(result)


class PDFPageSourceAdapter:
    input_class = PDFPage
    name = PDF_PAGE_SOURCE_NAME
    source_class = PDFPageSource

    async def resolve(self, value: PDFPage, /) -> PDFPageSource:
        name = pdf_page_source_id(value.document_id, value.document_sha256, value.page_number)
        return PDFPageSource(
            name=name,
            source_id=name,
            materialization=SourceMaterialization.CAPTURED,
            document_id=value.document_id,
            document_sha256=value.document_sha256,
            page_number=value.page_number,
            text=value.text,
        )

    async def read(self, source: PDFPageSource, /) -> PDFPage:
        return PDFPage(
            document_id=source.document_id,
            document_sha256=source.document_sha256,
            page_number=source.page_number,
            text=source.text,
        )


class PDFPageTextEvidenceProjection:
    name = TEXT_EVIDENCE_PROJECTION_KEY.name
    version = TEXT_EVIDENCE_PROJECTION_KEY.version
    source_class = PDFPageSource
    output_class: type[BaseModel] = TextEvidence

    def project(self, source: PDFPageSource, /) -> TextEvidence:
        return TextEvidence(
            source_type=PDF_PAGE_SOURCE_NAME,
            source_id=source.name,
            content=f"{source.document_id}\n{source.text}",
            metadata={
                "document_id": source.document_id,
                "document_sha256": source.document_sha256,
                "page_number": source.page_number,
            },
        )


PDF_PAGE_SOURCE_ADAPTER = PDFPageSourceAdapter()
PDF_PAGE_SOURCE_DEFINITION = AdapterSourceDefinition(
    PDF_PAGE_SOURCE_ADAPTER,
    version=PDF_PAGE_SOURCE_VERSION,
    projections=(PDFPageTextEvidenceProjection(),),
)


async def search_pdf_pages(memory, query: str, limit: int = 10) -> tuple[MemoryCitation, ...]:
    """Return up to ``limit`` distinct exact-page citations for one query."""

    if limit < 1:
        return ()
    # Over-fetch so that de-duplicating repeated hits still fills the page.
    page = await memory.search(SearchMemoryRequest(query=query, mode="fts", limit=limit * 4))
    citations: list[MemoryCitation] = []
    seen: set[tuple[str, str, str]] = set()
    for hit in page.hits:
        citation = MemoryCitation(
            memory_ref=hit.memory_ref,
            entry_id=hit.entry_id,
            entry_version_id=hit.entry_version_id,
        )
        key = (
            citation.memory_ref.model_dump_json(),
            citation.entry_id,
            citation.entry_version_id,
        )
        if key in seen:
            continue
        seen.add(key)
        citations.append(citation)
        if len(citations) >= limit:
            break
    return tuple(citations)


__all__ = [
    "PDF_PAGE_SOURCE_ADAPTER",
    "PDF_PAGE_SOURCE_DEFINITION",
    "PDFDocumentInput",
    "PDFPage",
    "PDFPageSource",
    "parse_pdf_document",
    "pdf_page_source_id",
    "search_pdf_pages",
]
