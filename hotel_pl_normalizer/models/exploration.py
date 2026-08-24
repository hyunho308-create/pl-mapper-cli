"""Structured answers from the combined routing and period-discovery session."""

from __future__ import annotations

from .common import StrictModel

from .period_selection import PeriodScenario, PeriodType
from .sheet_selection import (
    SheetNameDecision,
    SheetNameRoleHint,
    WorkbookSheetLayout,
)


class ExploredSheet(StrictModel):
    """One sheet, and what the model decided to do about it."""

    sheet_name: str
    decision: SheetNameDecision
    role_hint: SheetNameRoleHint = SheetNameRoleHint.UNKNOWN
    # Free text, in the model's words: which rows it read and what convinced it.
    # Kept because a wrong routing decision is far easier to argue with when the
    # reason is on the record.
    evidence: list[str] = []


class DiscoveredPeriod(StrictModel):
    """A period the workbook offers.

    Discovery answers which periods exist. The user chooses from this list
    before the binding stage locates the selected columns on each routed sheet.
    """

    period_id: str
    label: str
    period_type: PeriodType = PeriodType.UNKNOWN
    scenario: PeriodScenario = PeriodScenario.UNKNOWN
    start_period: str | None = None
    end_period: str | None = None
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
        """Sheets phase two may confirm periods against.

        `triage` and `unsure` only. A `skip` is routing saying the sheet holds no
        P&L content, so reading it proves nothing about which periods the
        workbook reports, and a `defer` was not judged worth reading.
        """
        return [
            sheet.sheet_name
            for sheet in self.sheets
            if sheet.decision in {SheetNameDecision.TRIAGE, SheetNameDecision.UNSURE}
        ]


class WorkbookPeriods(StrictModel):
    """Phase two: which reporting periods the workbook offers."""

    periods: list[DiscoveredPeriod] = []
    recommended_period_id: str | None = None
    notes: list[str] = []


class WorkbookExploration(StrictModel):
    """The whole result: what this workbook is, and what it offers.

    Assembled from both phases rather than submitted directly, so downstream
    consumers see one answer and do not care that it arrived in two parts.
    """

    workbook_layout: WorkbookSheetLayout = WorkbookSheetLayout.MIXED_OR_UNKNOWN
    layout_evidence: list[str] = []
    sheets: list[ExploredSheet] = []
    periods: list[DiscoveredPeriod] = []
    recommended_period_id: str | None = None
    notes: list[str] = []
