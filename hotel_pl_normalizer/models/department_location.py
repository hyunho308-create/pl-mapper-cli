from __future__ import annotations

from enum import Enum

from .common import StrictModel


class DepartmentLocationKind(str, Enum):
    SHEET = "sheet"
    RANGE = "range"


class DepartmentSectionRole(str, Enum):
    PRIMARY = "primary"
    SUMMARY = "summary"
    DETAIL = "detail"
    KPI = "kpi"
    SUPPORTING_DETAIL = "supporting_detail"
    UNKNOWN = "unknown"


class DepartmentLocation(StrictModel):
    location_id: str
    department: str
    section_role: DepartmentSectionRole = DepartmentSectionRole.PRIMARY
    location_kind: DepartmentLocationKind
    sheet_name: str
    start_row: int | None = None
    end_row: int | None = None
    evidence: list[str] = []


class DepartmentLocationMap(StrictModel):
    department_location_map_id: str
    workbook_id: str
    workbook_layout: str
    locations: list[DepartmentLocation]
    notes: list[str] = []
