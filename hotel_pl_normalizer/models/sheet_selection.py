from __future__ import annotations

from enum import Enum

from .common import (
    ModelInfo,
    NoNumericConfidenceModel,
    Severity,
    StrictModel,
    ValidationStatus,
)


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


class SheetNameCandidate(StrictModel):
    sheet_name: str
    sheet_index: int
    visible: bool = True
    header_cells: list[str] = []


class SheetNameTriagePacket(StrictModel):
    workbook_id: str
    source_filename: str
    sheet_count: int
    sheets: list[SheetNameCandidate]


class DepartmentCandidate(NoNumericConfidenceModel):
    department: str
    evidence: list[str] = []


class SheetNameSelection(NoNumericConfidenceModel):
    sheet_name: str
    decision: SheetNameDecision
    role_hint: SheetNameRoleHint = SheetNameRoleHint.UNKNOWN
    department_candidates: list[DepartmentCandidate] = []
    needs_sheet_enrichment: bool = False
    evidence: list[str] = []
    defer_reason: str | None = None


class SheetNameSelectionResult(NoNumericConfidenceModel):
    workbook_id: str
    model: ModelInfo = ModelInfo()
    workbook_layout: WorkbookSheetLayout = WorkbookSheetLayout.MIXED_OR_UNKNOWN
    layout_evidence: list[str] = []
    selections: list[SheetNameSelection]
    selected_sheet_names: list[str] = []
    deferred_sheet_names: list[str] = []
    skipped_sheet_names: list[str] = []
    unsure_sheet_names: list[str] = []
    notes: list[str] = []


class SheetNameSelectionValidationIssue(StrictModel):
    severity: Severity
    message: str
    sheet_names: list[str] = []


class SheetNameSelectionValidation(StrictModel):
    status: ValidationStatus
    issues: list[SheetNameSelectionValidationIssue] = []
