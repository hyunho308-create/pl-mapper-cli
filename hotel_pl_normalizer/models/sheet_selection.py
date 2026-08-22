from __future__ import annotations

from enum import Enum

from .common import StrictModel
from .department_location import DepartmentSectionRole


class CanonicalDepartment(str, Enum):
    SUMMARY = "summary"
    ROOMS = "rooms"
    FOOD_AND_BEVERAGE = "food_and_beverage"
    OTHER_OPERATED_DEPARTMENTS = "other_operated_departments"
    MISCELLANEOUS_INCOME = "miscellaneous_income"
    ADMINISTRATIVE_AND_GENERAL = "administrative_and_general"
    INFORMATION_AND_TELECOMMUNICATIONS_SYSTEMS = (
        "information_and_telecommunications_systems"
    )
    SALES_AND_MARKETING = "sales_and_marketing"
    PROPERTY_OPERATIONS_AND_MAINTENANCE = "property_operations_and_maintenance"
    UTILITIES = "utilities"
    MANAGEMENT_FEES = "management_fees"
    NON_OPERATING_INCOME_AND_EXPENSE = "non_operating_income_and_expense"


CANONICAL_DEPARTMENTS = tuple(item.value for item in CanonicalDepartment)


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


class SheetDepartmentHint(StrictModel):
    """A department hypothesis established while the sheet is already open."""

    department: CanonicalDepartment
    section_role: DepartmentSectionRole = DepartmentSectionRole.PRIMARY
    evidence: list[str] = []


class SheetNameSelection(StrictModel):
    sheet_name: str
    decision: SheetNameDecision
    role_hint: SheetNameRoleHint = SheetNameRoleHint.UNKNOWN
    department_hints: list[SheetDepartmentHint] = []
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
