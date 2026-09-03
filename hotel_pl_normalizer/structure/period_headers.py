"""Shared deterministic semantics for period headers.

The model interprets a workbook's reporting convention, but a few header facts
are objective enough to enforce in code: a variance or ratio subcolumn is not a
period amount column, and a wide Jan-Dec presentation is not the same layout as
a recurring PTD/YTD statement.  Keeping those facts here prevents discovery and
binding from applying subtly different definitions.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any

from openpyxl.utils.cell import column_index_from_string, range_boundaries

from hotel_pl_normalizer.models.period_selection import (
    CanonicalPeriod,
    PeriodScenario,
    inclusive_month_count,
)
from hotel_pl_normalizer.models.workbook import WorkbookSheet
from hotel_pl_normalizer.structure.monthly_spread import MONTHLY_SPREAD_THRESHOLD
from hotel_pl_normalizer.structure.representation import (
    infer_label_layout,
    is_technical_label,
    select_row_label,
)

HEADER_SCAN_ROWS = 40

_MONTHS = (
    "jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|"
    "jul(?:y)?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|"
    "nov(?:ember)?|dec(?:ember)?"
)
_MONTH_NUMBERS = {
    "jan": 1,
    "feb": 2,
    "mar": 3,
    "apr": 4,
    "may": 5,
    "jun": 6,
    "jul": 7,
    "aug": 8,
    "sep": 9,
    "oct": 10,
    "nov": 11,
    "dec": 12,
}

_HEADER_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("actual", re.compile(r"\bactuals?\b|\bact\b", re.I)),
    ("budget", re.compile(r"\bbudget\b|\bbud\b", re.I)),
    ("forecast", re.compile(r"\bforecast\b|\bfcst\b", re.I)),
    (
        "prior_year",
        re.compile(
            r"\bprior\s*year\b|\blast\s*year\b|\bprevious\s*year\b|\bLY\b",
            re.I,
        ),
    ),
    ("ytd", re.compile(r"\bytd\b|year\s*[- ]?to\s*[- ]?date", re.I)),
    (
        "ptd",
        re.compile(
            r"\bptd\b|\bperiodic\b|period\s*[- ]?to\s*[- ]?date|"
            r"\bcurrent\s+period\b|\bthis\s+month\b|\bmtd\b|"
            r"month\s*[- ]?to\s*[- ]?date",
            re.I,
        ),
    ),
    ("ttm", re.compile(r"\bttm\b|\bt12\b|trailing\s*(?:twelve|12)", re.I)),
    ("total", re.compile(r"\btotal\b|\bannual\b|\bfull\s*year\b|\bfy\b", re.I)),
    ("amount", re.compile(r"\bamount\b|\bamt\b", re.I)),
    ("percent", re.compile(r"%|\bpercent\b|\bpct\b", re.I)),
    (
        "variance",
        re.compile(
            r"\bvariance\b|\bvar\b|fav\s*/?\s*\(?unfav|"
            r"\bvs\.?\s*(?:budget|prior|actual|forecast|last\s+year)\b|"
            r"\bbetter\s*/?\s*worse\b|\bb\s*/\s*w\b",
            re.I,
        ),
    ),
    # Deliberately narrow. Occupancy, ADR and RevPAR commonly occur as rows in
    # an otherwise valid Actual/Budget amount column and must not poison it.
    ("ratio", re.compile(r"\bratio\b|\b(?:r|c)?por\b|\bpar\b", re.I)),
)

SCENARIO_MARKERS = frozenset({"actual", "budget", "forecast", "prior_year"})
COVERAGE_MARKERS = frozenset({"ytd", "ptd", "ttm", "total"})
FORBIDDEN_PERIOD_COLUMN_MARKERS = frozenset({"variance", "percent", "ratio"})
_STRONG_HEADER_MARKERS = SCENARIO_MARKERS | frozenset({"ytd", "ptd", "ttm"})


def header_markers(value: str) -> tuple[str, ...]:
    """Return normalized semantic markers found in one header-like value."""

    text = " ".join(str(value).split())
    markers = [name for name, pattern in _HEADER_PATTERNS if pattern.search(text)]
    lowered = text.lower()
    for match in re.finditer(rf"\b({_MONTHS})\b", lowered, re.I):
        markers.append(f"month:{_MONTH_NUMBERS[match.group(1)[:3].lower()]:02d}")
    for match in re.finditer(r"\b(?:19|20)\d{2}\b", text):
        markers.append(f"year:{match.group(0)}")
    for match in re.finditer(
        r"\b(0?[1-9]|1[0-2])[/.-](?:0?[1-9]|[12]\d|3[01])[/.-]((?:19|20)\d{2})\b",
        text,
    ):
        markers.extend((f"month:{int(match.group(1)):02d}", f"year:{match.group(2)}"))
    for match in re.finditer(
        r"\b((?:19|20)\d{2})-(0?[1-9]|1[0-2])-(?:0?[1-9]|[12]\d|3[01])(?=T|\b)",
        text,
    ):
        markers.extend((f"month:{int(match.group(2)):02d}", f"year:{match.group(1)}"))
    return tuple(dict.fromkeys(markers))


def period_layout_kind(values: Iterable[str]) -> str:
    """Classify only the reporting grain needed to reject an auxiliary anchor."""

    markers = {marker for value in values for marker in header_markers(str(value))}
    months = {marker for marker in markers if marker.startswith("month:")}
    if len(months) >= MONTHLY_SPREAD_THRESHOLD:
        return "monthly_spread"
    if "ytd" in markers and ("ptd" in markers or (months and len(months) <= 2)):
        return "ptd_ytd"
    return "other"


def latest_header_month(values: Iterable[str]) -> tuple[int, int] | None:
    """Best explicit as-of month in a header, for choosing a serial family's tip."""

    pairs: list[tuple[int, int]] = []
    for value in values:
        text = str(value)
        for match in re.finditer(
            r"\b((?:19|20)\d{2})-(0?[1-9]|1[0-2])-(?:0?[1-9]|[12]\d|3[01])(?=T|\b)",
            text,
        ):
            pairs.append((int(match.group(1)), int(match.group(2))))
        for match in re.finditer(
            r"\b(0?[1-9]|1[0-2])[/.-](?:0?[1-9]|[12]\d|3[01])[/.-]((?:19|20)\d{2})\b",
            text,
        ):
            pairs.append((int(match.group(2)), int(match.group(1))))
        for match in re.finditer(
            rf"\b({_MONTHS})[ '\-/]*((?:19|20)\d{{2}}|\d{{2}})\b",
            text,
            re.I,
        ):
            year = int(match.group(2))
            if year < 100:
                year += 2000
            pairs.append((year, _MONTH_NUMBERS[match.group(1)[:3].lower()]))
    return max(pairs, default=None)


def column_forbidden_markers(sheet: WorkbookSheet, column: int) -> set[str]:
    """Explicit disqualifiers that apply to a selected physical column.

    A later period header wins over stale export metadata above it.  This keeps a
    leading ``% or POR`` control row from disqualifying a column later named
    ``YTD Actual``, while still rejecting ``Budget Variance`` on the same row or
    a percent/ratio subheading immediately below a scenario label.
    """

    entries = _column_header_entries(sheet, column)
    identity_rows = [
        row
        for row, _, markers in entries
        if markers & (SCENARIO_MARKERS | COVERAGE_MARKERS)
        or any(marker.startswith(("month:", "year:")) for marker in markers)
    ]
    cutoff = max(identity_rows, default=0)
    forbidden: set[str] = set()
    for row, text, markers in entries:
        for marker in markers & FORBIDDEN_PERIOD_COLUMN_MARKERS:
            # Variance always controls. Percent/POR boilerplate can be stale
            # export metadata only when a later explicit period identity exists.
            if (
                row >= cutoff
                or marker == "variance"
                or not (
                    _is_stale_percent_or_por_control(text) or is_technical_label(text)
                )
            ):
                forbidden.add(marker)
    return forbidden


def column_scenario_markers(sheet: WorkbookSheet, column: int) -> set[str]:
    """Return the most specific scenario markers governing one amount column.

    Column headers are hierarchical. A report-wide merged title can name the
    template or saved forecast while the direct month headers below identify
    the figures actually displayed. Prefer the lowest direct scenario header.
    For a trailing Total without its own scenario, use a unanimous scenario on
    its sibling month headers rather than the older, wide report banner.

    The sibling fallback is deliberately narrow: it applies only beneath a
    merged heading spanning at least a monthly-spread width, and only when the
    selected column has a later direct period identity. Mixed sibling scenarios
    remain ambiguous and therefore return no deterministic scenario marker.
    """

    direct_entries_by_column = _direct_header_entries_by_column(sheet)
    direct_entries = direct_entries_by_column.get(column, [])
    direct_scenarios = [
        (row, markers & SCENARIO_MARKERS)
        for row, _, markers in direct_entries
        if markers & SCENARIO_MARKERS
    ]
    if direct_scenarios:
        latest_row = max(row for row, _ in direct_scenarios)
        return {
            marker
            for row, markers in direct_scenarios
            if row == latest_row
            for marker in markers
        }

    merged_scenarios: list[tuple[int, int, int, int, set[str]]] = []
    for merged in sheet.merged_ranges:
        if merged.top_left_row > HEADER_SCAN_ROWS or _is_number(merged.value):
            continue
        min_column, _, max_column, _ = range_boundaries(merged.range)
        if not min_column <= column <= max_column:
            continue
        scenarios = set(header_markers(str(merged.value or ""))) & SCENARIO_MARKERS
        if scenarios:
            merged_scenarios.append(
                (
                    merged.top_left_row,
                    max_column - min_column + 1,
                    min_column,
                    max_column,
                    scenarios,
                )
            )
    if not merged_scenarios:
        return set()

    # A later row is more specific; at the same row, a narrower merge is.
    row, span, min_column, max_column, scenarios = max(
        merged_scenarios,
        key=lambda item: (item[0], -item[1]),
    )
    identity_rows = [
        entry_row
        for entry_row, _, markers in direct_entries
        if entry_row > row and _has_period_identity(markers)
    ]
    if span < MONTHLY_SPREAD_THRESHOLD or not identity_rows:
        return set(scenarios)

    identity_row = max(identity_rows)
    sibling_scenarios: dict[int, set[str]] = {}
    for sibling_column in range(min_column, max_column + 1):
        for sibling_row, _, markers in direct_entries_by_column.get(sibling_column, []):
            found = markers & SCENARIO_MARKERS
            if row < sibling_row <= identity_row and found:
                sibling_scenarios.setdefault(sibling_row, set()).update(found)
    if not sibling_scenarios:
        return set(scenarios)

    sibling_row = max(sibling_scenarios)
    unanimous = sibling_scenarios[sibling_row]
    return set(unanimous) if len(unanimous) == 1 else set()


def _is_stale_percent_or_por_control(text: str) -> bool:
    normalized = re.sub(r"\s+", " ", text.strip()).lower().replace("$", "")
    return bool(
        re.fullmatch(r"%", normalized)
        or re.fullmatch(r"%\s*(?:or|/)\s*(?:r|c)?por", normalized)
    )


def period_column_problem(
    sheet: WorkbookSheet,
    period: CanonicalPeriod,
    excel_column: str,
    *,
    latest_period_year: int,
    controller_as_of: tuple[int, int] | None = None,
) -> str | None:
    """Why an exact model-claimed department column cannot confirm a period.

    This does not search for or choose a column. The model supplies one exact
    confirmation, and code verifies objective properties: it exists, contains
    non-zero labelled figures, is not explicitly a variance/percent/ratio
    subcolumn, and does not contradict the requested scenario or PTD/YTD grain.
    Ambiguous header wording remains model-owned rather than becoming a false
    deterministic rejection.
    """

    try:
        column = column_index_from_string((excel_column or "").strip().upper())
    except ValueError:
        return f"{excel_column!r} is not a valid Excel column"

    sheet_as_of = latest_header_month(_sheet_header_values(sheet))
    if controller_as_of is not None and sheet_as_of is not None:
        if sheet_as_of != controller_as_of:
            return (
                f"schedule ends {sheet_as_of[0]:04d}-{sheet_as_of[1]:02d}, not "
                f"the controller's {controller_as_of[0]:04d}-{controller_as_of[1]:02d}"
            )

    header_end = _last_strong_header_row(sheet)
    counts = _labelled_nonzero_counts(sheet, header_end)
    if not counts.get(column):
        return f"column {excel_column.upper()} has no non-zero labelled values"

    forbidden = column_forbidden_markers(sheet, column)
    if forbidden:
        return (
            f"column {excel_column.upper()} is explicitly marked as "
            f"{', '.join(sorted(forbidden))}"
        )

    markers = {
        marker
        for _, _, found in _column_header_entries(sheet, column)
        for marker in found
    }
    inherited_coverage = _inherited_coverage_marker(sheet, column)
    if inherited_coverage:
        markers.add(inherited_coverage)

    scenario_hints = column_scenario_markers(sheet, column)
    allowed = {
        PeriodScenario.ACTUAL: {"actual", "prior_year"},
        PeriodScenario.BUDGET: {"budget"},
        PeriodScenario.FORECAST: {"forecast"},
    }[period.scenario]
    if scenario_hints and not scenario_hints & allowed:
        return (
            f"column {excel_column.upper()} has scenario markers "
            f"{sorted(scenario_hints)}, not {period.scenario.value}"
        )

    period_year = int(period.end_month[:4])
    if period.scenario == PeriodScenario.ACTUAL:
        if (
            period_year < latest_period_year
            and scenario_hints
            and "prior_year" not in scenario_hints
            and f"year:{period_year}" not in markers
        ):
            return (
                f"column {excel_column.upper()} is not marked as Prior/Last Year "
                f"or {period_year}"
            )
        if (
            period_year == latest_period_year
            and scenario_hints == {"prior_year"}
            and f"year:{period_year}" not in markers
        ):
            return f"column {excel_column.upper()} is marked as Prior/Last Year"

    month_count = inclusive_month_count(period.start_month, period.end_month)
    if month_count == 1 and "ytd" in markers and "ptd" not in markers:
        return f"column {excel_column.upper()} is YTD, not PTD/monthly"
    if month_count > 1 and "ptd" in markers and not markers & {"ytd", "ttm", "total"}:
        return f"column {excel_column.upper()} is PTD/monthly, not YTD or annual"

    column_years = {
        int(marker.split(":", 1)[1]) for marker in markers if marker.startswith("year:")
    }
    if (
        column_years
        and period_year not in column_years
        and not (
            period.scenario == PeriodScenario.ACTUAL
            and period_year < latest_period_year
            and "prior_year" in markers
        )
    ):
        return (
            f"column {excel_column.upper()} names year(s) {sorted(column_years)}, "
            f"not {period_year}"
        )
    return None


def _inherited_coverage_marker(sheet: WorkbookSheet, column: int) -> str | None:
    """Infer a PTD/YTD band heading that is printed once across many columns.

    Exports use two conventions. Most place ``Current Period`` and ``YTD`` at
    the start of their bands; some center each heading over its Actual/Budget/LY
    block. If both headings sit over Budget columns, treat them as centered and
    use the nearest heading. Otherwise they are starts and the last heading to
    the left controls.
    """

    rows: dict[int, list[tuple[int, str]]] = {}
    for row in sheet.rows:
        if row.row_index > HEADER_SCAN_ROWS:
            continue
        for cell in row.cells:
            if _is_number(cell.raw_value) or not (text := _cell_text(cell)):
                continue
            markers = set(header_markers(text))
            for marker in ("ptd", "ytd"):
                if marker in markers:
                    rows.setdefault(row.row_index, []).append((cell.column, marker))
    choices = [
        (len({marker for _, marker in positions}), row, positions)
        for row, positions in rows.items()
        if {marker for _, marker in positions} == {"ptd", "ytd"}
    ]
    if not choices:
        return None
    _, _, positions = max(choices)
    positions = sorted(set(positions))
    centered = all(
        "budget"
        in {
            marker
            for _, _, found in _column_header_entries(sheet, heading_column)
            for marker in found
        }
        for heading_column, _ in positions
    )
    if centered:
        return min(positions, key=lambda item: abs(item[0] - column))[1]
    preceding = [item for item in positions if item[0] <= column]
    return (preceding[-1] if preceding else positions[0])[1]


def _column_header_entries(
    sheet: WorkbookSheet, column: int
) -> list[tuple[int, str, set[str]]]:
    entries: list[tuple[int, str, set[str]]] = []
    seen: set[tuple[int, str]] = set()
    for row in sheet.rows:
        if row.row_index > HEADER_SCAN_ROWS:
            continue
        for cell in row.cells:
            if cell.column != column or _is_number(cell.raw_value):
                continue
            text = _cell_text(cell)
            if not text or (row.row_index, text) in seen:
                continue
            seen.add((row.row_index, text))
            entries.append((row.row_index, text, set(header_markers(text))))
    for merged in sheet.merged_ranges:
        if merged.top_left_row > HEADER_SCAN_ROWS:
            continue
        min_column, _, max_column, _ = range_boundaries(merged.range)
        if not min_column <= column <= max_column:
            continue
        if _is_number(merged.value):
            continue
        text = " ".join(str(merged.value or "").split())
        if not text or (merged.top_left_row, text) in seen:
            continue
        seen.add((merged.top_left_row, text))
        entries.append((merged.top_left_row, text, set(header_markers(text))))
    return sorted(entries, key=lambda item: item[0])


def _direct_header_entries_by_column(
    sheet: WorkbookSheet,
) -> dict[int, list[tuple[int, str, set[str]]]]:
    """Non-merged header cells indexed by their physical column."""

    multi_column_merged_ranges: list[tuple[int, int, int, int]] = []
    for merged in sheet.merged_ranges:
        if merged.top_left_row > HEADER_SCAN_ROWS:
            continue
        min_column, min_row, max_column, max_row = range_boundaries(merged.range)
        if max_column <= min_column:
            continue
        multi_column_merged_ranges.append((min_row, max_row, min_column, max_column))

    entries: dict[int, list[tuple[int, str, set[str]]]] = {}
    for row in sheet.rows:
        if row.row_index > HEADER_SCAN_ROWS:
            continue
        for cell in row.cells:
            if any(
                min_row <= row.row_index <= max_row
                and min_column <= cell.column <= max_column
                for min_row, max_row, min_column, max_column in multi_column_merged_ranges
            ) or _is_number(cell.raw_value):
                continue
            text = _cell_text(cell)
            if text:
                entries.setdefault(cell.column, []).append(
                    (row.row_index, text, set(header_markers(text)))
                )
    return entries


def _has_period_identity(markers: set[str]) -> bool:
    return bool(
        markers & COVERAGE_MARKERS
        or any(marker.startswith(("month:", "year:")) for marker in markers)
    )


def _sheet_header_values(sheet: WorkbookSheet) -> list[str]:
    values = [
        text
        for row in sheet.rows
        if row.row_index <= HEADER_SCAN_ROWS
        for cell in row.cells
        if not _is_number(cell.raw_value)
        if (text := _cell_text(cell))
    ]
    values.extend(
        " ".join(str(merged.value).split())
        for merged in sheet.merged_ranges
        if merged.top_left_row <= HEADER_SCAN_ROWS
        and merged.value is not None
        and not _is_number(merged.value)
    )
    return values


def _last_strong_header_row(sheet: WorkbookSheet) -> int:
    """Last semantic header across stacked blocks in the first page area."""

    rows = [
        row.row_index
        for row in sheet.rows
        if row.row_index <= HEADER_SCAN_ROWS
        if any(
            set(header_markers(text)) & _STRONG_HEADER_MARKERS
            for cell in row.cells
            if not _is_number(cell.raw_value)
            if (text := _cell_text(cell))
        )
    ]
    rows.extend(
        merged.top_left_row
        for merged in sheet.merged_ranges
        if merged.top_left_row <= HEADER_SCAN_ROWS
        and merged.value is not None
        and not _is_number(merged.value)
        and set(header_markers(str(merged.value))) & _STRONG_HEADER_MARKERS
    )
    return max(rows, default=0)


def _labelled_nonzero_counts(sheet: WorkbookSheet, header_end: int) -> dict[int, int]:
    rows = [row for row in sheet.rows if row.row_index > header_end]
    layout = infer_label_layout(rows)
    counts: dict[int, int] = {}
    for row in rows:
        if select_row_label(row, layout).cell is None:
            continue
        for cell in row.cells:
            if not _is_number(cell.raw_value) or float(cell.raw_value) == 0:
                continue
            counts[cell.column] = counts.get(cell.column, 0) + 1
    return counts


def _cell_text(cell) -> str | None:
    value: Any = (
        cell.display_value if cell.display_value is not None else cell.raw_value
    )
    if value is None:
        return None
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    text = " ".join(str(value).split())
    return text[:160] or None


def _is_number(value: Any) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool)


__all__ = [
    "FORBIDDEN_PERIOD_COLUMN_MARKERS",
    "column_forbidden_markers",
    "column_scenario_markers",
    "header_markers",
    "latest_header_month",
    "period_column_problem",
    "period_layout_kind",
]
