from __future__ import annotations

from enum import Enum

from pydantic import model_validator

from .common import StrictModel


class FinancialEvidenceRole(str, Enum):
    """What kind of evidence a routed workbook sheet or PDF page contains."""

    SUMMARY_P_AND_L = "summary_p_and_l"
    DEPARTMENT_P_AND_L = "department_p_and_l"
    FINANCIAL_SUPPORTING_SCHEDULE = "financial_supporting_schedule"
    UNKNOWN = "unknown"
    TOPLINE_OPERATING_STATISTICS = "topline_operating_statistics"
    BALANCE_SHEET = "balance_sheet"
    PAYROLL_STATISTICS = "payroll_statistics"
    OTHER = "other"


class RoutingConfidence(str, Enum):
    HIGH = "high"
    UNCERTAIN = "uncertain"


INCLUDED_FINANCIAL_EVIDENCE_ROLES = frozenset(
    {
        FinancialEvidenceRole.SUMMARY_P_AND_L,
        FinancialEvidenceRole.DEPARTMENT_P_AND_L,
        FinancialEvidenceRole.FINANCIAL_SUPPORTING_SCHEDULE,
        FinancialEvidenceRole.UNKNOWN,
    }
)
EXCLUDED_FINANCIAL_EVIDENCE_ROLES = frozenset(
    {
        FinancialEvidenceRole.TOPLINE_OPERATING_STATISTICS,
        FinancialEvidenceRole.BALANCE_SHEET,
        FinancialEvidenceRole.PAYROLL_STATISTICS,
        FinancialEvidenceRole.OTHER,
    }
)


def financial_evidence_compatibility_error(
    include_as_financial_evidence: bool,
    role: FinancialEvidenceRole,
) -> str | None:
    """Return a precise error when inclusion and role contradict one another."""
    allowed = (
        INCLUDED_FINANCIAL_EVIDENCE_ROLES
        if include_as_financial_evidence
        else EXCLUDED_FINANCIAL_EVIDENCE_ROLES
    )
    if role in allowed:
        return None
    expected = ", ".join(sorted(item.value for item in allowed))
    return (
        f"role={role.value!r} is incompatible with "
        f"include_as_financial_evidence={include_as_financial_evidence}; "
        f"expected one of: {expected}"
    )


class FinancialEvidenceClassification(StrictModel):
    include_as_financial_evidence: bool
    role: FinancialEvidenceRole
    confidence: RoutingConfidence = RoutingConfidence.HIGH
    evidence: list[str] = []

    @model_validator(mode="after")
    def verify_role_matches_inclusion(self):
        error = financial_evidence_compatibility_error(
            self.include_as_financial_evidence,
            self.role,
        )
        if error:
            raise ValueError(error)
        return self


class WorkbookSheetLayout(str, Enum):
    SINGLE_TAB_P_AND_L = "single_tab_p_and_l"
    MULTI_TAB_DEPARTMENT_P_AND_L = "multi_tab_department_p_and_l"
    MIXED_OR_UNKNOWN = "mixed_or_unknown"


class SheetNameSelection(FinancialEvidenceClassification):
    sheet_name: str


class SheetNameSelectionResult(StrictModel):
    workbook_id: str
    workbook_layout: WorkbookSheetLayout = WorkbookSheetLayout.MIXED_OR_UNKNOWN
    layout_evidence: list[str] = []
    selections: list[SheetNameSelection]
    notes: list[str] = []

    @property
    def included_sheet_names(self) -> list[str]:
        return [
            item.sheet_name
            for item in self.selections
            if item.include_as_financial_evidence
        ]

    @property
    def excluded_sheet_names(self) -> list[str]:
        return [
            item.sheet_name
            for item in self.selections
            if not item.include_as_financial_evidence
        ]
