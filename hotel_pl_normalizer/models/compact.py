from __future__ import annotations

from enum import Enum

from .common import NoNumericConfidenceModel, StrictModel


class CompactRowRole(str, Enum):
    HEADING = "heading"
    DETAIL = "detail"
    SUBTOTAL = "subtotal"
    BLANK = "blank"
    UNKNOWN = "unknown"


class CompactLabel(StrictModel):
    raw: str | None = None
    normalized: str | None = None
    source_column: int | None = None
    leftmost_text_column: int | None = None


class CompactValue(StrictModel):
    column: int
    address: str
    raw_value: str | int | float | bool | None = None
    display_value: str | None = None
    numeric_value: float | None = None
    column_header: str | None = None
    merged_header: str | None = None
    period_hint: str | None = None


class CompactTextCell(StrictModel):
    column: int
    address: str
    text: str


class CompactLayout(StrictModel):
    indent: float | None = None
    bold: bool = False
    italic: bool = False
    underline: bool = False
    font_size: float | None = None
    fill_color: str | None = None
    has_top_border: bool = False
    has_bottom_border: bool = False
    blank_above: bool = False
    blank_below: bool = False
    numeric_cell_count: int = 0
    text_cell_count: int = 0


class CompactContext(StrictModel):
    nearest_heading_above: str | None = None
    nearest_bold_heading_above: str | None = None
    active_merged_header: str | None = None
    previous_nonblank_label: str | None = None
    next_nonblank_label: str | None = None
    sheet_title_guess: str | None = None


class PeriodColumnHint(NoNumericConfidenceModel):
    column: int
    header: str
    reason: str


class CompactHeuristicHints(StrictModel):
    possible_period_columns: list[PeriodColumnHint] = []
    possible_row_role: CompactRowRole = CompactRowRole.UNKNOWN
    possible_section_boundary: bool = False
    possible_total_line: bool = False
    possible_kpi: bool = False


class CompactWindowRow(StrictModel):
    row_index: int
    label: str | None = None


class CompactWindow(StrictModel):
    rows_before: list[CompactWindowRow] = []
    rows_after: list[CompactWindowRow] = []


class CompactRow(StrictModel):
    workbook_id: str
    sheet_name: str
    row_index: int
    row_key: str
    label: CompactLabel
    values: list[CompactValue] = []
    text_cells: list[CompactTextCell] = []
    layout: CompactLayout = CompactLayout()
    context: CompactContext = CompactContext()
    heuristic_hints: CompactHeuristicHints = CompactHeuristicHints()
    raw_window: CompactWindow = CompactWindow()


class CompactSheetPacket(StrictModel):
    packet_id: str
    workbook_id: str
    sheet_name: str
    start_row: int | None = None
    end_row: int | None = None
    rows: list[CompactRow]
    notes: list[str] = []
