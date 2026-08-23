"""What one exploration session returns.

This replaces two stages with one answer. Sheet routing and period discovery both
ask questions about sheet names and header text, and asking them separately let
them disagree: discovery picked its five sheets with a cheap score that chose a
Table of Contents and a payroll register on one workbook, while routing already
had the vocabulary to know better.

Everything here reuses the enums those two stages already speak, so the result
can stand in for both without a translation layer.
"""

from __future__ import annotations

from .common import StrictModel

# `PeriodType` from `period_selection`, not from `common`. There are two enums of
# that name and they are not interchangeable: `common.PeriodType` is
# YTD/MTD/TTM/Budget/Forecast, while period discovery and period selection speak
# month/current_period/ytd/full_year/ttm. Importing the wrong one left every
# full-year column typed `Unknown`, because `full_year` is not one of its members
# -- and this result is meant to stand in for discovery without a translation
# layer, so it has to use discovery's vocabulary.
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

    No column bindings. Discovery answers *which periods exist*; committing one
    to a column on each tab is `period_selection`'s job and needs the department
    locations that do not exist yet at this point.
    """

    period_id: str
    label: str
    period_type: PeriodType = PeriodType.UNKNOWN
    scenario: PeriodScenario = PeriodScenario.UNKNOWN
    start_period: str | None = None
    end_period: str | None = None
    # Which of the examined sheets carried it. A period on every financial sheet
    # is safe to offer; one on a single tab usually is not, and saying which
    # sheets lets a person judge that without rerunning anything.
    sheets_present: list[str] = []
    evidence: list[str] = []


class WorkbookRouting(StrictModel):
    """Phase one: what this workbook is, and which sheets matter.

    Submitted on its own, before any period work. Routing every sheet is the
    whole job here, and mixing it with period detection made it a side effect of
    that: a session that had read five sheets marked the other sixty-five `skip`
    with the reason "not opened this session". Deciding the sheets first, and
    only then being told to look for periods, removes that failure by
    construction.
    """

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

    @property
    def financial_sheet_names(self) -> list[str]:
        """Sheets worth reading, in the order the model returned them."""
        return [
            sheet.sheet_name
            for sheet in self.sheets
            if sheet.decision in {SheetNameDecision.TRIAGE, SheetNameDecision.UNSURE}
        ]
