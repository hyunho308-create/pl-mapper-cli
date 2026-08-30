"""Deterministically identify account captions on financial rows.

Most hotel statements use one caption column throughout a sheet. Indentation
occasionally moves a child caption one column left or right, and a small number
of sheets contain a second, locally stable caption column. This module models
that layout directly instead of scoring individual strings as more or less
"business-like".
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass

from hotel_pl_normalizer.models.workbook import CellRecord, WorkbookRow

_CONTROL_LABELS = {
    "account",
    "app",
    "apply to",
    "businessunit",
    "category",
    "criteria",
    "datasrc",
    "department",
    "dimension",
    "format",
    "insert",
    "measures",
    "memberset",
    "option",
    "parameter",
    "parameters",
    "querytype",
    "range",
    "rptcurrency",
    "sqlonly",
    "suppress",
    "time",
    "top",
    "use",
}


@dataclass(frozen=True)
class LabelRegion:
    """A contiguous run whose captions use a local alternate column."""

    start_row: int
    end_row: int
    column: int


@dataclass(frozen=True)
class LabelLayout:
    """The sheet's primary caption column plus rare local exceptions."""

    primary_column: int | None
    overrides: tuple[LabelRegion, ...] = ()


@dataclass(frozen=True)
class LabelSelection:
    """One row's selected caption and the deterministic rule that selected it."""

    cell: CellRecord | None
    context: tuple[CellRecord, ...] = ()
    status: str = "blank"
    rule: str = "none"


def infer_label_layout(
    rows: Iterable[WorkbookRow],
    *,
    value_columns: set[int] | None = None,
) -> LabelLayout:
    """Infer one primary label column and only stable local exceptions.

    ``value_columns`` should be the period columns selected for mapping. When
    omitted, any numeric cell makes a row financial evidence.
    """
    financial_rows = [
        row for row in rows if _row_has_number(row, value_columns=value_columns)
    ]
    counts: Counter[int] = Counter()
    for row in financial_rows:
        for cell in _valid_text_cells(row.cells):
            counts[cell.column] += 1
    if not counts:
        return LabelLayout(primary_column=None)

    # Caption columns ordinarily precede values, so prefer the leftmost tie.
    primary = max(counts, key=lambda column: (counts[column], -column))
    return LabelLayout(
        primary_column=primary,
        overrides=tuple(_stable_overrides(financial_rows, primary)),
    )


def select_row_label(row: WorkbookRow, layout: LabelLayout) -> LabelSelection:
    """Select from the primary/adjacent band, then a stable local override."""
    valid = _valid_text_cells(row.cells)
    if not valid or layout.primary_column is None:
        return LabelSelection(cell=None)

    override = next(
        (
            region
            for region in layout.overrides
            if region.start_row <= row.row_index <= region.end_row
        ),
        None,
    )
    anchor = override.column if override is not None else layout.primary_column
    candidates = [cell for cell in valid if abs(cell.column - anchor) <= 1]
    if not candidates:
        return LabelSelection(cell=None)

    exact = [cell for cell in candidates if cell.column == anchor]
    if exact:
        selected = exact[-1]
        rule = "local_override" if override is not None else "primary"
    else:
        # Adjacent columns represent outline indentation. Prefer the more
        # indented/rightward cell when both sides contain text.
        selected = max(candidates, key=lambda cell: cell.column)
        rule = "local_override_adjacent" if override is not None else "adjacent_indent"
    context = tuple(cell for cell in candidates if cell is not selected)
    return LabelSelection(
        cell=selected,
        context=context,
        status="selected" if len(candidates) == 1 else "selected_with_context",
        rule=rule,
    )


def is_technical_label(text: str) -> bool:
    """Reject unmistakable query/report syntax, not business abbreviations."""
    stripped = _clean_text(text)
    normalized = _normalize_label(stripped)
    compact = re.sub(r"[^A-Za-z0-9]+", "", stripped)
    return bool(
        not stripped
        or normalized in _CONTROL_LABELS
        or re.fullmatch(
            r"#(?:DIV/0!|N/A|VALUE!|REF!|NAME\?|NUM!|NULL!)",
            stripped,
            flags=re.IGNORECASE,
        )
        or stripped.startswith("%,")
        or "fontbold" in normalized
        or "indentlevel" in normalized
        or re.search(r"\[[^\]]+\]\s*\.\s*\[[^\]]+\]", stripped)
        or (stripped.startswith("<<") and "[" in stripped and "]" in stripped)
        or ("!" in stripped and "$" in stripped)
        or "dep(" in stripped.lower()
        or re.fullmatch(r"[A-Z]{1,4}\d{4,}", compact)
        or re.fullmatch(r"[A-Z]+(?:_[A-Z0-9]+)+", stripped)
        or normalized.startswith("account style")
    )


def _stable_overrides(rows: list[WorkbookRow], primary: int) -> list[LabelRegion]:
    """Find runs of 2+ financial rows with a shared label outside the main band."""
    missing: list[tuple[int, int]] = []
    for row in rows:
        valid = _valid_text_cells(row.cells)
        if any(abs(cell.column - primary) <= 1 for cell in valid):
            continue
        outside = [cell for cell in valid if abs(cell.column - primary) > 1]
        if len(outside) == 1:
            missing.append((row.row_index, outside[0].column))

    regions: list[LabelRegion] = []
    run: list[tuple[int, int]] = []
    for item in missing:
        if run and (item[1] != run[-1][1] or item[0] > run[-1][0] + 1):
            _append_stable_run(regions, run)
            run = []
        run.append(item)
    _append_stable_run(regions, run)
    return regions


def _append_stable_run(regions: list[LabelRegion], run: list[tuple[int, int]]) -> None:
    if len(run) >= 2:
        regions.append(LabelRegion(run[0][0], run[-1][0], run[0][1]))


def _valid_text_cells(cells: Iterable[CellRecord]) -> list[CellRecord]:
    return [
        cell
        for cell in cells
        if isinstance(cell.raw_value, str)
        and (text := _clean_text(cell.display_value or cell.raw_value))
        and re.search(r"[A-Za-z]", text)
        and not is_technical_label(text)
    ]


def _row_has_number(row: WorkbookRow, *, value_columns: set[int] | None) -> bool:
    return any(
        _is_numeric_cell(cell)
        and (value_columns is None or cell.column in value_columns)
        for cell in row.cells
    )


def _is_numeric_cell(cell: CellRecord) -> bool:
    return isinstance(cell.raw_value, int | float) and not isinstance(
        cell.raw_value, bool
    )


def _clean_text(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _normalize_label(label: str) -> str:
    return re.sub(r"[^a-z0-9%]+", " ", _clean_text(label).lower()).strip()
