from __future__ import annotations

from enum import Enum

from .common import (
    ModelInfo,
    NoNumericConfidenceModel,
    Severity,
    StrictModel,
    ValidationStatus,
)
from .compact import CompactSheetPacket
from .sheet_selection import SheetNameSelectionResult


class DepartmentLocationKind(str, Enum):
    SHEET = "sheet"
    RANGE = "range"
    UNKNOWN = "unknown"


class BoundaryConfidence(str, Enum):
    EXACT = "exact"
    APPROXIMATE = "approximate"
    UNKNOWN = "unknown"


class DepartmentSectionRole(str, Enum):
    PRIMARY = "primary"
    SUMMARY = "summary"
    DETAIL = "detail"
    KPI = "kpi"
    SUPPORTING_DETAIL = "supporting_detail"
    UNKNOWN = "unknown"


class DepartmentLocationIssueType(str, Enum):
    MISSING_LOCATION = "missing_location"
    DUPLICATE_DEPARTMENT = "duplicate_department"
    UNKNOWN_SHEET = "unknown_sheet"
    INVALID_RANGE = "invalid_range"
    NEEDS_MORE_CONTEXT = "needs_more_context"


class DepartmentContextRequest(StrictModel):
    sheet_name: str
    reason: str
    start_row: int | None = None
    end_row: int | None = None
    requested_artifact: str = "compact_sheet_packet"


class DepartmentIdentificationPacket(StrictModel):
    workbook_id: str
    source_filename: str
    sheet_name_selection: SheetNameSelectionResult
    compact_packets: list[CompactSheetPacket]
    notes: list[str] = []


class DepartmentLocation(NoNumericConfidenceModel):
    location_id: str
    department: str
    section_role: DepartmentSectionRole = DepartmentSectionRole.PRIMARY
    location_kind: DepartmentLocationKind
    sheet_name: str
    start_row: int | None = None
    end_row: int | None = None
    boundary_confidence: BoundaryConfidence = BoundaryConfidence.UNKNOWN
    evidence: list[str] = []
    open_questions: list[str] = []


class DepartmentLocationMapIssue(StrictModel):
    severity: Severity
    issue_type: DepartmentLocationIssueType
    department: str | None = None
    sheet_name: str | None = None
    message: str
    requested_context: list[DepartmentContextRequest] = []


class DepartmentLocationValidation(StrictModel):
    status: ValidationStatus
    issues: list[DepartmentLocationMapIssue] = []


class DepartmentLocationMap(StrictModel):
    department_location_map_id: str
    workbook_id: str
    model: ModelInfo = ModelInfo()
    workbook_layout: str
    locations: list[DepartmentLocation]
    validation: DepartmentLocationValidation = DepartmentLocationValidation(status=ValidationStatus.SKIPPED)
    notes: list[str] = []


class DepartmentLocationReplacement(StrictModel):
    target_location_id: str
    location: DepartmentLocation


class DepartmentLocationPatch(StrictModel):
    patch_id: str
    target_map_id: str
    workbook_id: str
    model: ModelInfo = ModelInfo()
    additions: list[DepartmentLocation] = []
    replacements: list[DepartmentLocationReplacement] = []
    remove_location_ids: list[str] = []
    rationale: str | None = None
