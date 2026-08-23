"""Adapt one binding session to the pipeline's period-selection maps."""

from __future__ import annotations

from collections import Counter

from hotel_pl_normalizer.models.binding import WorkbookBindings
from hotel_pl_normalizer.models.period_selection import (
    PeriodColumnSelection,
    PeriodColumnSelectionMap,
)


def _column_number(excel_column: str) -> int:
    number = 0
    for character in (excel_column or "").strip().upper():
        number = number * 26 + ord(character) - ord("A") + 1
    return number


def binding_to_selection_maps(
    structure: WorkbookBindings,
    *,
    workbook_id: str,
    period_ids: list[str],
    period_labels: dict[str, str] | None = None,
) -> dict[str, PeriodColumnSelectionMap]:
    """One selection map per chosen period, keyed by period id.

    A sheet with no binding is left out rather than guessed at. Evidence falls
    back to the default selection for it, which is the workbook's usual column --
    the right answer far more often than any column this stage could invent.
    """
    labels = dict(period_labels or {})
    maps: dict[str, PeriodColumnSelectionMap] = {}
    for period_id in period_ids:
        label = labels.get(period_id, period_id)
        selections = [
            PeriodColumnSelection(
                sheet_name=binding.sheet_name,
                value_column=_column_number(binding.excel_column),
                excel_column=binding.excel_column.strip().upper(),
                period_label=label,
                evidence=list(binding.evidence),
            )
            for binding in structure.bindings
            if binding.period_id == period_id
        ]
        notes = [
            f"{item.sheet_name}: {item.reason}"
            for item in structure.unavailable
            if item.period_id == period_id
        ]
        maps[period_id] = PeriodColumnSelectionMap(
            selection_map_id=f"{workbook_id}:period_columns:{period_id}",
            workbook_id=workbook_id,
            requested_period=label,
            default_selection=_default_selection(selections),
            sheet_selections=selections,
            notes=notes,
        )
    return maps


def _default_selection(
    selections: list[PeriodColumnSelection],
) -> PeriodColumnSelection | None:
    """The column this workbook uses when a sheet did not say.

    The most frequently bound column across sheets, because a workbook that puts
    YTD in column H on nine tabs almost certainly puts it there on the tenth. Ties
    break toward the leftmost column, which is the one nearer the labels.
    """
    if not selections:
        return None
    counts = Counter(
        (selection.value_column, selection.excel_column) for selection in selections
    )
    best = max(counts.items(), key=lambda item: (item[1], -item[0][0]))[0]
    template = next(
        selection
        for selection in selections
        if (selection.value_column, selection.excel_column) == best
    )
    return template.model_copy(
        update={
            "sheet_name": None,
            "evidence": [
                f"Most common bound column across {len(selections)} sheet(s)."
            ],
        }
    )
