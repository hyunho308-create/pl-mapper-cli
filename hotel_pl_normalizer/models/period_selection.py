from __future__ import annotations

import re
from enum import Enum

from .common import (
    ModelInfo,
    NoNumericConfidenceModel,
    Severity,
    StrictModel,
    ValidationStatus,
)


class PeriodScenario(str, Enum):
    ACTUAL = "actual"
    PRIOR_ACTUAL = "prior_actual"
    BUDGET = "budget"
    FORECAST = "forecast"
    BLENDED = "blended"
    UNKNOWN = "unknown"


class PeriodType(str, Enum):
    MONTH = "month"
    CURRENT_PERIOD = "current_period"
    YTD = "ytd"
    FULL_YEAR = "full_year"
    TTM = "ttm"
    UNKNOWN = "unknown"


class PeriodColumnSample(StrictModel):
    row_index: int
    label: str | None = None
    value: float | None = None


class PeriodColumnCandidate(StrictModel):
    column: int
    excel_column: str
    header_context: list[str] = []
    numeric_count: int = 0
    nonzero_count: int = 0
    percentage_format_count: int = 0
    subunit_nonzero_count: int = 0
    material_value_count: int = 0
    percentage_scale_count: int = 0
    large_amount_count: int = 0
    sample_values: list[PeriodColumnSample] = []


class PeriodHeaderCell(StrictModel):
    coordinate: str
    value: str


class PeriodMergedHeader(StrictModel):
    range: str
    value: str


class PeriodSheetPacket(StrictModel):
    location_id: str | None = None
    sheet_name: str
    department: str | None = None
    start_row: int | None = None
    end_row: int | None = None
    header_cells: list[PeriodHeaderCell] = []
    merged_headers: list[PeriodMergedHeader] = []
    spatial_header_start_row: int | None = None
    spatial_header_end_row: int | None = None
    spatial_header_end_column: int | None = None
    spatial_header_cells: list[PeriodHeaderCell] = []
    spatial_merged_headers: list[PeriodMergedHeader] = []
    candidate_columns: list[PeriodColumnCandidate]


class PeriodColumnPacket(StrictModel):
    packet_id: str
    workbook_id: str
    source_filename: str
    requested_period: str
    sheets: list[PeriodSheetPacket]
    notes: list[str] = []


class RejectedPeriodColumn(StrictModel):
    sheet_name: str | None = None
    column: int
    excel_column: str
    reason: str


class PeriodColumnSelection(NoNumericConfidenceModel):
    sheet_name: str | None = None
    department: str | None = None
    value_column: int
    excel_column: str
    period_label: str
    evidence: list[str] = []
    warnings: list[str] = []


class PeriodColumnSelectionIssue(StrictModel):
    severity: Severity
    sheet_name: str | None = None
    message: str


class PeriodColumnSelectionValidation(StrictModel):
    status: ValidationStatus
    issues: list[PeriodColumnSelectionIssue] = []


class PeriodColumnSelectionMap(StrictModel):
    selection_map_id: str
    workbook_id: str
    requested_period: str
    model: ModelInfo = ModelInfo()
    default_selection: PeriodColumnSelection | None = None
    sheet_selections: list[PeriodColumnSelection] = []
    rejected_columns: list[RejectedPeriodColumn] = []
    validation: PeriodColumnSelectionValidation = PeriodColumnSelectionValidation(status=ValidationStatus.SKIPPED)
    notes: list[str] = []


class PeriodColumnBinding(StrictModel):
    """One column convention shared by one or more department locations."""

    location_ids: list[str]
    value_column: int
    excel_column: str
    evidence: list[str] = []


class UnavailablePeriodLocation(StrictModel):
    """A department location where a selected period could not be bound."""

    location_id: str
    reason: str
    evidence: list[str] = []


class PeriodOption(NoNumericConfidenceModel):
    period_id: str
    label: str
    scenario: PeriodScenario
    period_type: PeriodType
    start_period: str | None = None
    end_period: str | None = None
    bindings: list[PeriodColumnBinding]
    unavailable_locations: list[UnavailablePeriodLocation] = []
    warnings: list[str] = []


class PeriodCatalogIssue(StrictModel):
    severity: Severity
    period_id: str | None = None
    location_id: str | None = None
    message: str


class PeriodCatalogValidation(StrictModel):
    status: ValidationStatus
    issues: list[PeriodCatalogIssue] = []


class PeriodSheetRole(str, Enum):
    PRIMARY_CORE = "primary_core"
    ALTERNATE_CORE = "alternate_core"
    SUPPORTING = "supporting"


class PeriodSheetAssessment(StrictModel):
    """Gemini's one-time decision about a sampled sheet's discovery role."""

    location_id: str
    role: PeriodSheetRole
    reason: str
    evidence: list[str] = []


class PeriodCatalog(StrictModel):
    catalog_id: str
    workbook_id: str
    model: ModelInfo = ModelInfo()
    sheet_assessments: list[PeriodSheetAssessment] = []
    options: list[PeriodOption]
    recommended_period_id: str | None = None
    validation: PeriodCatalogValidation = PeriodCatalogValidation(
        status=ValidationStatus.SKIPPED
    )
    notes: list[str] = []


class PeriodBindingRepairPatch(StrictModel):
    """Add or replace one failed representative-location binding."""

    period_id: str
    location_id: str
    value_column: int
    excel_column: str
    evidence: list[str] = []


class PeriodUnavailableRepairPatch(StrictModel):
    """Mark one failed representative location unavailable for a period."""

    period_id: str
    location_id: str
    reason: str
    evidence: list[str] = []


class PeriodRemovalRepairPatch(StrictModel):
    """Explicitly remove a partial or unsupported discovered period."""

    period_id: str
    reason: str
    evidence: list[str]


class PeriodConceptRepairPatch(StrictModel):
    """Correct the identity of one validator-rejected period concept."""

    period_id: str
    label: str
    scenario: PeriodScenario
    period_type: PeriodType
    start_period: str | None = None
    end_period: str | None = None


class PeriodCatalogRepair(StrictModel):
    """Targeted changes to a catalog; omission always means unchanged."""

    sheet_assessment_patches: list[PeriodSheetAssessment] = []
    concept_patches: list[PeriodConceptRepairPatch] = []
    binding_patches: list[PeriodBindingRepairPatch] = []
    unavailable_patches: list[PeriodUnavailableRepairPatch] = []
    removal_patches: list[PeriodRemovalRepairPatch] = []
    notes: list[str] = []


def humanize_period_label(label: str) -> str:
    """Turn report-style period tokens into short labels for people."""
    text = re.sub(r"[._]+", " ", str(label or "")).strip()
    text = re.sub(r"\s+", " ", text)
    lowered = text.lower()

    # Bounded against digits rather than word characters: headers glue the year to
    # its qualifier ("FY2025", "Q4-2025"), and \b would find no year in any of
    # them. Only an adjacent digit disqualifies a match, so a longer number is
    # still never read as a year.
    year_match = re.search(r"(?<!\d)(20\d{2})(?!\d)", lowered)
    year = year_match.group(1) if year_match else ""
    month_match = re.search(
        r"\b(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|"
        r"jul(?:y)?|aug(?:ust)?|sep(?:tember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\b",
        lowered,
    )
    month = month_match.group(1).title()[:3] if month_match else ""

    prior = "last year" in lowered or "prior" in lowered
    if "budget" in lowered:
        scenario = "Budget"
    elif "forecast" in lowered:
        scenario = "Forecast"
    else:
        scenario = "Actual"

    if "ytd" in lowered or "year to date" in lowered or "year-to-date" in lowered:
        prefix = (
            "Last Year"
            if prior
            else scenario
            if scenario != "Actual"
            else "Current"
            if "actual" in lowered or "current" in lowered
            else year
        )
        return f"{prefix} YTD"
    if "mtd" in lowered or "month to date" in lowered or "current period" in lowered:
        prefix = (
            "Last Year"
            if prior
            else scenario
            if scenario != "Actual"
            else "Current"
            if "actual" in lowered or "current" in lowered
            else year
        )
        return f"{prefix} MTD"
    if "ttm" in lowered or "trailing 12" in lowered:
        return " ".join(part for part in (year, "TTM", scenario) if part)
    # `fy` is matched with letters excluded on both sides rather than as a bare
    # substring: property names are part of this input, and "Comfy" and "Modify"
    # both contain it. Word boundaries alone would be too strict -- "FY2025" is a
    # real header -- so only an adjacent letter disqualifies it.
    if (
        "total" in lowered
        or "full year" in lowered
        or re.search(r"(?<![a-z])fy(?![a-z])", lowered)
    ):
        return " ".join(part for part in (year, scenario) if part) or "Full Year Actual"
    if month:
        return " ".join(part for part in (month, year, scenario) if part)
    return text
