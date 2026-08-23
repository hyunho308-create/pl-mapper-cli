from __future__ import annotations

from enum import Enum

from .common import StrictModel


class SheetNameDecision(str, Enum):
    TRIAGE = "triage"
    DEFER = "defer"
    SKIP = "skip"
    UNSURE = "unsure"


class SheetNameRoleHint(str, Enum):
    SUMMARY_P_AND_L = "summary_p_and_l"
    DEPARTMENT_P_AND_L = "department_p_and_l"
    SUPPORTING_SCHEDULE = "supporting_schedule"
    CHECK_OR_ANALYSIS = "check_or_analysis"
    BALANCE_SHEET = "balance_sheet"
    UNKNOWN = "unknown"


class WorkbookSheetLayout(str, Enum):
    SINGLE_TAB_P_AND_L = "single_tab_p_and_l"
    MULTI_TAB_DEPARTMENT_P_AND_L = "multi_tab_department_p_and_l"
    MIXED_OR_UNKNOWN = "mixed_or_unknown"


class SheetNameSelection(StrictModel):
    sheet_name: str
    decision: SheetNameDecision
    role_hint: SheetNameRoleHint = SheetNameRoleHint.UNKNOWN
    evidence: list[str] = []


class SheetNameSelectionResult(StrictModel):
    workbook_id: str
    workbook_layout: WorkbookSheetLayout = WorkbookSheetLayout.MIXED_OR_UNKNOWN
    layout_evidence: list[str] = []
    selections: list[SheetNameSelection]
    selected_sheet_names: list[str] = []
    deferred_sheet_names: list[str] = []
    skipped_sheet_names: list[str] = []
    unsure_sheet_names: list[str] = []
    notes: list[str] = []
