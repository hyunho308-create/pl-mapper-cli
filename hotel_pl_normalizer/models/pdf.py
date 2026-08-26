"""Coordinate-preserving records for born-digital PDF statements.

PDFs do not contain workbook cells.  These records therefore keep the objects
the file actually gives us -- pages and positioned words -- plus deterministic
text-line groupings.  No table, period, or accounting meaning is asserted here.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from .common import Severity


@dataclass(slots=True, frozen=True)
class PdfSource:
    source_id: str
    original_filename: str
    local_path: str
    file_hash: str
    ingested_at: datetime


@dataclass(slots=True, frozen=True)
class PdfWord:
    """One extractable word and its exact page-space bounding box."""

    word_id: str
    text: str
    x0: float
    top: float
    x1: float
    bottom: float
    font_name: str | None = None
    font_size: float | None = None
    bold: bool = False
    decorative: bool = False
    numeric_value: float | None = None
    is_percent: bool = False


@dataclass(slots=True, frozen=True)
class PdfTextLine:
    """Words that share a visual baseline, in left-to-right reading order."""

    line_id: str
    line_number: int
    text: str
    x0: float
    top: float
    x1: float
    bottom: float
    word_ids: tuple[str, ...]


@dataclass(slots=True, frozen=True)
class PdfRule:
    """A drawn line or thin rectangle that may visually delimit a table."""

    rule_id: str
    orientation: str
    x0: float
    top: float
    x1: float
    bottom: float


@dataclass(slots=True)
class PdfPage:
    page_number: int
    width: float
    height: float
    rotation: int = 0
    words: list[PdfWord] = field(default_factory=list)
    text_lines: list[PdfTextLine] = field(default_factory=list)
    rules: list[PdfRule] = field(default_factory=list)


@dataclass(slots=True, frozen=True)
class PdfIngestionWarning:
    severity: Severity
    message: str
    page_number: int | None = None


@dataclass(slots=True)
class PdfDocumentRecord:
    document_id: str
    source: PdfSource
    pages: list[PdfPage] = field(default_factory=list)
    warnings: list[PdfIngestionWarning] = field(default_factory=list)

    def page(self, page_number: int) -> PdfPage:
        if page_number < 1 or page_number > len(self.pages):
            raise IndexError(
                f"Page {page_number} is outside this {len(self.pages)}-page PDF."
            )
        return self.pages[page_number - 1]
