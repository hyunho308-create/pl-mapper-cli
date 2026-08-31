"""Adapt one binding session to the pipeline's period-selection maps."""

from __future__ import annotations

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

    A sheet explicitly marked unavailable is retained as such. New runs never
    infer a missing sheet's column from the workbook-wide modal column: every
    routed sheet-period pair must have an explicit binding or unavailable result.
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
        unavailable = {
            item.sheet_name: item.reason
            for item in structure.unavailable
            if item.period_id == period_id
        }
        notes = [f"{sheet_name}: {reason}" for sheet_name, reason in unavailable.items()]
        maps[period_id] = PeriodColumnSelectionMap(
            selection_map_id=f"{workbook_id}:period_columns:{period_id}",
            workbook_id=workbook_id,
            requested_period=label,
            default_selection=None,
            sheet_selections=selections,
            unavailable_sheets=unavailable,
            notes=notes,
        )
    return maps
