"""Reduce a parsed workbook to the rows the mapper is actually shown.

This is the whole of the model's view of the workbook. Whatever is dropped here
cannot be mapped, cannot be cited, and cannot appear in the run log -- so the two
filters below are worth reading before changing anything upstream of them.

Label detection is imported from `structure.representation` rather than
reimplemented. The mapper has to agree with the structure stages about which
column holds account labels; a second opinion would show up as evidence citing a
different column than the packets those stages reasoned about, for the same sheet.
"""

from __future__ import annotations

from typing import Any

from hotel_pl_normalizer.models.period_selection import PeriodColumnSelectionMap
from hotel_pl_normalizer.models.workbook import WorkbookRecord
from hotel_pl_normalizer.structure.representation import (
    infer_label_layout,
    select_row_label,
)


def compact_workbook_evidence(
    workbook: WorkbookRecord,
    period_map: PeriodColumnSelectionMap | dict[str, PeriodColumnSelectionMap],
    *,
    include_sheets: set[str],
) -> list[dict[str, Any]]:
    """Expose each row once with one value for every selected period.

    Two kinds of row are withheld from the model, both to keep the prompt to a
    size a single mapping session can hold:

    - Rows with no label and no value in any selected period. Nothing to map.
    - Rows that are zero in *every* selected period and are not bold. Hotel P&Ls
      carry long runs of unused accounts, and these are the bulk of them. Bold is
      the exemption because a bold zero is usually a subtotal, and the validator
      needs subtotals present to check hierarchy even when they total zero.

    The second filter has a cost worth knowing about. A detail account that is
    genuinely zero is not mapped to 0, it is simply absent, so it reports as
    unmapped rather than as a confirmed zero. More awkwardly, a row that reads
    zero only because its period column was bound wrongly disappears here, which
    removes the evidence a reviewer would need to notice the mis-binding. Both are
    accepted deliberately: the alternative is a prompt several times larger on
    every workbook. See `_is_all_zero_filler`.
    """
    period_maps = (
        period_map if isinstance(period_map, dict) else {"selected": period_map}
    )
    columns_by_period: dict[str, dict[str, int]] = {}
    defaults_by_period: dict[str, int | None] = {}
    unavailable_by_period: dict[str, set[str]] = {}
    for period_id, selection_map in period_maps.items():
        columns: dict[str, int] = {}
        selections = list(selection_map.sheet_selections)
        if selection_map.default_selection is not None:
            selections.append(selection_map.default_selection)
        for selection in selections:
            if selection.sheet_name and selection.value_column is not None:
                columns.setdefault(selection.sheet_name, selection.value_column)
        columns_by_period[period_id] = columns
        defaults_by_period[period_id] = (
            selection_map.default_selection.value_column
            if selection_map.default_selection is not None
            else None
        )
        unavailable_by_period[period_id] = set(selection_map.unavailable_sheets)

    evidence = []
    for sheet in workbook.sheets:
        if sheet.sheet_name not in include_sheets:
            continue
        if all(
            sheet.sheet_name in unavailable_by_period[period_id]
            for period_id in period_maps
        ):
            continue
        selected_value_columns = {
            column
            for period_id in period_maps
            if sheet.sheet_name not in unavailable_by_period[period_id]
            for column in [
                columns_by_period[period_id].get(
                    sheet.sheet_name,
                    defaults_by_period[period_id],
                )
            ]
            if column is not None
        }
        label_layout = infer_label_layout(
            sheet.rows,
            value_columns=selected_value_columns,
        )
        for row in sheet.rows:
            selected_columns = {}
            selected_values = {}
            for period_id in period_maps:
                value_column = (
                    None
                    if sheet.sheet_name in unavailable_by_period[period_id]
                    else columns_by_period[period_id].get(
                        sheet.sheet_name,
                        defaults_by_period[period_id],
                    )
                )
                selected_columns[period_id] = value_column
                selected_values[period_id] = next(
                    (
                        cell.raw_value
                        for cell in row.cells
                        if cell.column == value_column
                    ),
                    None,
                )
            text_cells = [
                cell
                for cell in row.cells
                if isinstance(cell.raw_value, str) and cell.raw_value.strip()
            ]
            label_selection = select_row_label(row, label_layout)
            row_label_cell = label_selection.cell
            label = (
                " ".join(str(row_label_cell.raw_value).split())
                if row_label_cell is not None
                else ""
            )
            # Nothing to say about this row: no label to map and no figure to map
            # it to, in any selected period.
            if not label and not any(
                isinstance(value, (int, float)) and not isinstance(value, bool)
                for value in selected_values.values()
            ):
                continue
            if _is_all_zero_filler(selected_values, text_cells):
                continue
            first_period = next(iter(period_maps))
            evidence.append(
                {
                    "row_key": f"{sheet.sheet_name}!{row.row_index}",
                    "label": label,
                    "selected_value_columns": selected_columns,
                    "selected_values": selected_values,
                    # Kept for single-period callers and existing audit consumers.
                    "selected_value_column": selected_columns[first_period],
                    "selected_value": selected_values[first_period],
                    "indent": row_label_cell.style.indent
                    if row_label_cell is not None
                    else None,
                    "bold": any(cell.style.bold for cell in text_cells),
                    "label_column": (
                        row_label_cell.column if row_label_cell is not None else None
                    ),
                    "label_rule": label_selection.rule,
                    "label_status": label_selection.status,
                    "label_context": [
                        " ".join(str(cell.raw_value).split())
                        for cell in label_selection.context
                    ],
                }
            )
    return evidence


def _is_all_zero_filler(selected_values: dict[str, Any], text_cells: list[Any]) -> bool:
    """Is this a zero row the mapper does not need to see?

    True only when the row carries at least one number, every number it carries
    is zero across every selected period, and no text on the row is bold.

    Each of those three conditions is load-bearing:

    - *at least one number* -- a row with no numbers at all is a heading or a
      spacer, and headings are how the model finds section boundaries.
    - *every selected period* -- a row that is zero this year and not last year
      is a real account, and dropping it would make one period's mapping depend
      on which other periods happened to be selected alongside it.
    - *not bold* -- a bold zero is nearly always a subtotal, and the validator
      checks hierarchy against subtotals whether or not they total zero.

    Kept because unused accounts are the bulk of a hotel P&L, and carrying them
    would multiply the prompt for rows that hold no information. The cost is
    described in `compact_workbook_evidence`.
    """
    numeric_values = [
        value
        for value in selected_values.values()
        if isinstance(value, (int, float)) and not isinstance(value, bool)
    ]
    if not numeric_values:
        return False
    if not all(value == 0 for value in numeric_values):
        return False
    return not any(cell.style.bold for cell in text_cells)
