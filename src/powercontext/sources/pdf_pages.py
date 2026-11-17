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

"""Whole-document PDF page capture and evidence-page retrieval through Memory."""

from __future__ import annotations

import hashlib
import io
import re
from collections import Counter
from urllib.parse import quote, unquote

from pydantic import BaseModel, ConfigDict, field_validator
from pypdf import PdfReader

from powercontext.builtin.artifacts.memory.models import MemoryCitation
from powercontext.builtin.runtime.models import GetMemoryEntryRequest, SearchMemoryRequest
from powercontext.limits import MAX_SOURCE_ID_LENGTH
from powercontext.sources.definitions import AdapterSourceDefinition
from powercontext.sources.models import Source, SourceMaterialization
from powercontext.sources.projections import TEXT_EVIDENCE_PROJECTION_KEY, TextEvidence

PDF_PAGE_SOURCE_TYPE = "pdf-page"

_TOKEN_PATTERN = re.compile(r"[a-z]+|\d+")
_STOPWORDS = frozenset(
    """
    a about above after again against all am an and any are as at be because been before being below
    between both but by can cannot could did do does doing down during each few for from further had
    has have having he her here hers herself him himself his how i if in into is it its itself just
    me more most my myself no nor not now of off on once only or other our ours ourselves out over
    own same she should so some such than that the their theirs them themselves then there these
    they this those through to too under until up very was we were what when where which while who
    whom why will with would you your yours yourself yourselves
    """.split()
)


class PDFDocumentInput(BaseModel):
    """One complete original PDF offered for page-level capture."""

    model_config = ConfigDict(frozen=True)

    document_id: str
    content: bytes

    @field_validator("document_id")
    @classmethod
    def validate_document_id(cls, value: str) -> str:
        if not value or not value.strip():
            raise ValueError("document_id must be a non-blank string")  # noqa: TRY003
        return value


class PDFPageInput(BaseModel):
    """One physical PDF page kept with its document identity and digest."""

    model_config = ConfigDict(frozen=True)

    document_id: str
    document_sha256: str
    page_number: int
    text: str


class PDFPageSource(Source):
    """Durable Source description for one physical page of one PDF revision."""

    document_id: str
    document_sha256: str
    page_number: int
    text: str


def pdf_page_source_id(document_id: str, document_sha256: str, page_number: int, /) -> str:
    """Build the stable identity of one physical page in one PDF revision."""

    source_id = f"pdf:{quote(document_id, safe='')}:{document_sha256}:{page_number}"
    if len(source_id) > MAX_SOURCE_ID_LENGTH:
        raise ValueError("PDF page source identity exceeds the maximum length")  # noqa: TRY003
    return source_id


def parse_pdf_document(value: PDFDocumentInput, /) -> tuple[PDFPageInput, ...]:
    """Parse every physical page of one PDF atomically, starting at page 1."""

    document = PDFDocumentInput.model_validate(value)
    digest = hashlib.sha256(document.content).hexdigest()
    try:
        reader = PdfReader(io.BytesIO(document.content))
        if reader.is_encrypted:
            reader.decrypt("")
        pages = tuple(
            PDFPageInput(
                document_id=document.document_id,
                document_sha256=digest,
                page_number=number,
                text=page.extract_text() or "",
            )
            for number, page in enumerate(reader.pages, start=1)
        )
    except Exception as error:
        raise ValueError("PDF document cannot be parsed completely") from error
    if not pages:
        raise ValueError("PDF document contains no physical pages")  # noqa: TRY003
    return pages


class _PDFPageAdapter:
    input_class = PDFPageInput
    name = PDF_PAGE_SOURCE_TYPE
    source_class = PDFPageSource

    async def resolve(self, value: PDFPageInput, /) -> PDFPageSource:
        page = PDFPageInput.model_validate(value)
        source_id = pdf_page_source_id(page.document_id, page.document_sha256, page.page_number)
        return PDFPageSource(
            name=source_id,
            definition_version="1",
            materialization=SourceMaterialization.CAPTURED,
            document_id=page.document_id,
            document_sha256=page.document_sha256,
            page_number=page.page_number,
            text=page.text,
        )

    async def read(self, source: PDFPageSource, /) -> PDFPageInput:
        return PDFPageInput(
            document_id=source.document_id,
            document_sha256=source.document_sha256,
            page_number=source.page_number,
            text=source.text,
        )


class _PDFPageTextEvidenceProjection:
    name = TEXT_EVIDENCE_PROJECTION_KEY.name
    version = TEXT_EVIDENCE_PROJECTION_KEY.version
    source_class = PDFPageSource
    output_class = TextEvidence

    def project(self, source: PDFPageSource, /) -> TextEvidence:
        return TextEvidence(
            source_type=PDF_PAGE_SOURCE_TYPE,
            source_id=source.name,
            content=f"{source.document_id}\n{source.text}",
            metadata={
                "document_id": source.document_id,
                "document_sha256": source.document_sha256,
                "page_number": source.page_number,
            },
        )


PDF_PAGE_SOURCE_DEFINITION = AdapterSourceDefinition(
    adapter=_PDFPageAdapter(),
    version="1",
    projections=(_PDFPageTextEvidenceProjection(),),
)


def _tokens(text: str) -> list[str]:
    return _TOKEN_PATTERN.findall(text.lower())


def _page_location(source_id: str) -> tuple[str, int] | None:
    parts = source_id.split(":")
    if len(parts) != 4 or parts[0] != "pdf" or not parts[3].isdigit():
        return None
    return unquote(parts[1]), int(parts[3])


async def search_pdf_pages(memory, question: str, limit: int = 10) -> tuple[MemoryCitation, ...]:
    """Return up to ``limit`` distinct evidence-page citations from scoped Memory."""

    if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
        raise ValueError("limit must be a positive integer")  # noqa: TRY003
    question_tokens = _tokens(question)
    content_tokens = [token for token in question_tokens if token not in _STOPWORDS]
    queries = [question]
    if content_tokens:
        queries.append(" ".join(content_tokens))
    pool_limit = max(limit, 50)
    best_rank: dict[tuple[str, str], tuple[int, MemoryCitation]] = {}
    for query in queries:
        page = await memory.search(SearchMemoryRequest(query=query, mode="auto", limit=pool_limit))
        for rank, hit in enumerate(page.hits):
            key = (hit.entry_id, hit.entry_version_id)
            citation = MemoryCitation(
                memory_ref=hit.memory_ref,
                entry_id=hit.entry_id,
                entry_version_id=hit.entry_version_id,
            )
            previous = best_rank.get(key)
            if previous is None or rank < previous[0]:
                best_rank[key] = (rank, citation)

    query_set = set(content_tokens)
    by_page: dict[tuple[str, int], tuple[tuple[int, int, int], MemoryCitation]] = {}
    for rank, citation in best_rank.values():
        record = await memory.get(GetMemoryEntryRequest(citation=citation))
        entry = record.entry
        location = None
        for reference in entry.sources:
            if reference.source_type == PDF_PAGE_SOURCE_TYPE:
                location = _page_location(reference.source_id)
                if location is not None:
                    break
        if location is None:
            continue
        page_tokens = Counter(_tokens(entry.text))
        coverage = sum(1 for token in query_set if token in page_tokens)
        frequency = sum(page_tokens[token] for token in query_set)
        score = (coverage, frequency, -rank)
        previous = by_page.get(location)
        if previous is None or score > previous[0]:
            by_page[location] = (score, citation)
    ranked = sorted(by_page.values(), key=lambda item: item[0], reverse=True)
    return tuple(citation for _, citation in ranked[:limit])


__all__ = [
    "PDF_PAGE_SOURCE_DEFINITION",
    "PDFDocumentInput",
    "PDFPageInput",
    "PDFPageSource",
    "parse_pdf_document",
    "pdf_page_source_id",
    "search_pdf_pages",
]