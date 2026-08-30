"""Typed results for the two model-driven PDF structure stages."""

from __future__ import annotations

from enum import Enum

from pydantic import Field, model_validator

from .common import StrictModel
from .period_selection import CanonicalPeriod
from .sheet_selection import FinancialEvidenceClassification


class PdfLayout(str, Enum):
    SINGLE_PAGE_STATEMENT = "single_page_statement"
    CONSISTENT_MULTI_PAGE_STATEMENT = "consistent_multi_page_statement"
    MIXED_PAGE_LAYOUTS = "mixed_page_layouts"
    UNKNOWN = "unknown"


class PdfPageRange(StrictModel):
    start_page: int = Field(ge=1)
    end_page: int = Field(ge=1)


class PdfPageRangeDecision(FinancialEvidenceClassification):
    start_page: int = Field(ge=1)
    end_page: int = Field(ge=1)


class PdfRouting(StrictModel):
    layout: PdfLayout = PdfLayout.UNKNOWN
    layout_evidence: list[str] = []
    page_ranges: list[PdfPageRangeDecision] = []
    notes: list[str] = []


class PdfDiscoveredPeriod(CanonicalPeriod):
    pages_present: list[PdfPageRange] = []
    evidence: list[str] = []


class PdfPeriods(StrictModel):
    controlling_summary_pages: PdfPageRange
    periods: list[PdfDiscoveredPeriod] = []
    notes: list[str] = []

    @model_validator(mode="after")
    def verify_period_identity(self):
        ids = [period.period_id for period in self.periods]
        if len(ids) != len(set(ids)):
            raise ValueError("canonical period_id values must be unique")
        return self


class PdfExploration(StrictModel):
    layout: PdfLayout = PdfLayout.UNKNOWN
    layout_evidence: list[str] = []
    page_ranges: list[PdfPageRangeDecision] = []
    # Optional only so compact discoveries created before this field existed
    # can still be loaded. Every newly accepted exploration supplies it.
    controlling_summary_pages: PdfPageRange | None = None
    periods: list[PdfDiscoveredPeriod] = []
    notes: list[str] = []

    @property
    def financial_pages(self) -> list[int]:
        pages: set[int] = set()
        for item in self.page_ranges:
            if item.include_as_financial_evidence:
                pages.update(range(item.start_page, item.end_page + 1))
        return sorted(pages)


class PdfPeriodAnchorBinding(PdfPageRange):
    period_id: str
    right_edge: float = Field(gt=0)
    header_text: str
    evidence: list[str] = []


class PdfUnavailablePeriod(PdfPageRange):
    period_id: str
    reason: str


class PdfBindings(StrictModel):
    bindings: list[PdfPeriodAnchorBinding] = []
    unavailable: list[PdfUnavailablePeriod] = []
    notes: list[str] = []


class PdfStructureResult(StrictModel):
    exploration: PdfExploration
    bindings: PdfBindings
    selected_period_ids: list[str]
