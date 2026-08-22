"""Parsed workbook structures.

Why these are dataclasses and not pydantic models
-------------------------------------------------
These are the only models in the system built hundreds of thousands of times: one
`CellRecord` and one `CellStyle` per cell. As pydantic models that cost about
1,719 bytes per cell, which turned a 1.36 MB workbook (GRY, 218,879 cells) into
791 MB of peak memory and an out-of-memory kill on a 512 MB instance.

The same fields as `slots` dataclasses cost 296 bytes. The saving is entirely the
per-instance overhead of validating data that openpyxl and xlrd already parsed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum

from .common import Severity


class FileType(str, Enum):
    XLS = "xls"
    XLSX = "xlsx"
    XLSM = "xlsm"


@dataclass(slots=True)
class WorkbookSource:
    source_id: str
    original_filename: str
    file_type: FileType
    ingested_at: datetime
    local_path: str | None = None
    file_hash: str | None = None


@dataclass(slots=True)
class WorkbookMetadata:
    sheet_count: int = 0
    detected_property_code: str | None = None
    detected_operator: str | None = None
    detected_period_label: str | None = None


@dataclass(slots=True)
class MergedRange:
    range: str
    top_left_row: int
    top_left_column: int
    value: str | int | float | bool | None = None


# Frozen because ingestion hands one instance to every cell that shares a style.
# Nothing mutated a style before this, so freezing costs nothing and turns that
# sharing from an assumption into a guarantee.
@dataclass(slots=True, frozen=True)
class CellStyle:
    bold: bool = False
    italic: bool = False
    underline: bool = False
    font_size: float | None = None
    indent: float | None = None
    horizontal_alignment: str | None = None
    fill_color: str | None = None
    font_color: str | None = None
    border_bottom: str | None = None


@dataclass(slots=True)
class CellRecord:
    row: int
    column: int
    address: str
    raw_value: str | int | float | bool | None = None
    display_value: str | None = None
    number_format: str | None = None
    is_merged: bool = False
    merged_parent: str | None = None
    style: CellStyle = field(default_factory=CellStyle)


@dataclass(slots=True)
class WorkbookRow:
    row_index: int
    cells: list[CellRecord] = field(default_factory=list)
    height: float | None = None
    hidden: bool = False


@dataclass(slots=True)
class WorkbookSheet:
    sheet_id: str
    sheet_name: str
    max_row: int
    max_column: int
    rows: list[WorkbookRow] = field(default_factory=list)
    merged_ranges: list[MergedRange] = field(default_factory=list)
    visible: bool = True


@dataclass(slots=True)
class IngestionWarning:
    severity: Severity
    message: str
    sheet_name: str | None = None
    row: int | None = None
    column: int | None = None


@dataclass(slots=True)
class WorkbookRecord:
    workbook_id: str
    source: WorkbookSource
    sheets: list[WorkbookSheet] = field(default_factory=list)
    workbook_metadata: WorkbookMetadata = field(default_factory=WorkbookMetadata)
    ingestion_warnings: list[IngestionWarning] = field(default_factory=list)
