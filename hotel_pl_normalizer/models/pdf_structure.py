"""Typed results for the two model-driven PDF structure stages."""

from __future__ import annotations

from enum import Enum

from pydantic import Field

from .common import StrictModel
from .period_selection import PeriodScenario, PeriodType


class PdfPageRole(str, Enum):
    FINANCIAL_STATEMENT = "financial_statement"
    SUPPORTING_SCHEDULE = "supporting_schedule"
    NON_FINANCIAL = "non_financial"
    UNKNOWN = "unknown"


class PdfLayout(str, Enum):
    SINGLE_PAGE_STATEMENT = "single_page_statement"
    CONSISTENT_MULTI_PAGE_STATEMENT = "consistent_multi_page_statement"
    MIXED_PAGE_LAYOUTS = "mixed_page_layouts"
    UNKNOWN = "unknown"


class PdfPageRange(StrictModel):
    start_page: int = Field(ge=1)
    end_page: int = Field(ge=1)


class PdfPageRangeDecision(PdfPageRange):
    role: PdfPageRole
    evidence: list[str] = []


class PdfRouting(StrictModel):
    layout: PdfLayout = PdfLayout.UNKNOWN
    layout_evidence: list[str] = []
    page_ranges: list[PdfPageRangeDecision] = []
    notes: list[str] = []


class PdfDiscoveredPeriod(StrictModel):
    period_id: str
    label: str
    period_type: PeriodType = PeriodType.UNKNOWN
    scenario: PeriodScenario = PeriodScenario.UNKNOWN
    start_period: str | None = None
    end_period: str | None = None
    pages_present: list[PdfPageRange] = []
    evidence: list[str] = []


class PdfPeriods(StrictModel):
    periods: list[PdfDiscoveredPeriod] = []
    recommended_period_id: str | None = None
    notes: list[str] = []


class PdfExploration(StrictModel):
    layout: PdfLayout = PdfLayout.UNKNOWN
    layout_evidence: list[str] = []
    page_ranges: list[PdfPageRangeDecision] = []
    periods: list[PdfDiscoveredPeriod] = []
    recommended_period_id: str | None = None
    notes: list[str] = []

    @property
    def financial_pages(self) -> list[int]:
        pages: set[int] = set()
        for item in self.page_ranges:
            if item.role in {
                PdfPageRole.FINANCIAL_STATEMENT,
                PdfPageRole.SUPPORTING_SCHEDULE,
            }:
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
