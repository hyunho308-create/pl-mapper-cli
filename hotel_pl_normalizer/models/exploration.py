"""Structured answers from the combined routing and period-discovery session."""

from __future__ import annotations

from pydantic import model_validator

from .common import StrictModel
from .period_selection import CanonicalPeriod
from .sheet_selection import (
    FinancialEvidenceClassification,
    WorkbookSheetLayout,
)


class ExploredSheet(FinancialEvidenceClassification):
    """One sheet, and what the model decided to do about it."""

    sheet_name: str


class DiscoveredPeriod(CanonicalPeriod):
    """A period the workbook offers.

    Discovery answers which periods exist. The user chooses from this list
    before the binding stage locates the selected columns on each routed sheet.
    """

    # Which examined sheets carried this canonical period. Coverage can be
    # uneven: a controlling statement may offer a period that a supporting
    # schedule does not, and binding handles that sheet independently.
    sheets_present: list[str] = []
    evidence: list[str] = []


class WorkbookRouting(StrictModel):
    """Phase one: what this workbook is, and which sheets matter."""

    workbook_layout: WorkbookSheetLayout = WorkbookSheetLayout.MIXED_OR_UNKNOWN
    layout_evidence: list[str] = []
    sheets: list[ExploredSheet] = []
    notes: list[str] = []

    @property
    def financial_sheet_names(self) -> list[str]:
        """Sheets phase two may confirm periods against."""
        return [
            sheet.sheet_name
            for sheet in self.sheets
            if sheet.include_as_financial_evidence
        ]


class WorkbookPeriods(StrictModel):
    """Phase two: which reporting periods the workbook offers."""

    controlling_summary_sheet: str
    periods: list[DiscoveredPeriod] = []
    notes: list[str] = []

    @model_validator(mode="after")
    def verify_period_identity(self):
        ids = [period.period_id for period in self.periods]
        if len(ids) != len(set(ids)):
            raise ValueError("canonical period_id values must be unique")
        return self


class WorkbookExploration(StrictModel):
    """The whole result: what this workbook is, and what it offers.

    Assembled from both phases rather than submitted directly, so downstream
    consumers see one answer and do not care that it arrived in two parts.
    """

    workbook_layout: WorkbookSheetLayout = WorkbookSheetLayout.MIXED_OR_UNKNOWN
    layout_evidence: list[str] = []
    sheets: list[ExploredSheet] = []
    controlling_summary_sheet: str
    periods: list[DiscoveredPeriod] = []
    notes: list[str] = []
