"""Mechanical validation for period-column bindings."""

from __future__ import annotations

from dataclasses import dataclass, field

from hotel_pl_normalizer.models.binding import PeriodBinding, WorkbookBindings
from hotel_pl_normalizer.models.workbook import WorkbookSheet


@dataclass
class CheckResult:
    """Why a submission cannot stand, or what was odd about one that can."""

    rejections: list[str] = field(default_factory=list)
    observations: list[str] = field(default_factory=list)

    @property
    def accepted(self) -> bool:
        return not self.rejections


def check_bindings(
    submission: WorkbookBindings,
    sheets: dict[str, WorkbookSheet],
    *,
    period_ids: list[str],
    financial_sheets: list[str],
) -> CheckResult:
    """Reject only bindings that cannot be true of this workbook."""

    result = CheckResult()
    chosen = set(period_ids)
    claimed: dict[str, list[PeriodBinding]] = {}

    for binding in submission.bindings:
        if binding.period_id not in chosen:
            result.rejections.append(
                f"{binding.period_id!r} was not one of the chosen periods "
                f"({', '.join(period_ids)})."
            )
            continue
        sheet = sheets.get(binding.sheet_name)
        if sheet is None:
            result.rejections.append(
                f"There is no sheet named {binding.sheet_name!r} in this workbook."
            )
            continue
        column = _column_number(binding.excel_column)
        if column is None:
            result.rejections.append(
                f"{binding.excel_column!r} is not a column letter."
            )
            continue
        if not _column_holds_numbers(sheet, column):
            result.rejections.append(
                f"Column {binding.excel_column} on {binding.sheet_name!r} holds "
                "no numeric values at all. Read the sheet and bind a column "
                "that carries figures, or mark this sheet unavailable for "
                f"{binding.period_id!r}."
            )
            continue
        claimed.setdefault(binding.sheet_name, []).append(binding)

    for sheet_name, bindings in sorted(claimed.items()):
        by_column: dict[str, set[str]] = {}
        for binding in bindings:
            by_column.setdefault(binding.excel_column, set()).add(binding.period_id)
        for excel_column, periods in sorted(by_column.items()):
            if len(periods) > 1:
                result.rejections.append(
                    f"Column {excel_column} on {sheet_name!r} is bound to "
                    f"{len(periods)} different periods "
                    f"({', '.join(sorted(periods))}). One column holds one period."
                )

    if not result.rejections:
        _observe_unbound_sheets(submission, financial_sheets, period_ids, result)
    return result


def _observe_unbound_sheets(
    submission: WorkbookBindings,
    financial_sheets: list[str],
    period_ids: list[str],
    result: CheckResult,
) -> None:
    answered = {
        (binding.sheet_name, binding.period_id) for binding in submission.bindings
    } | {(item.sheet_name, item.period_id) for item in submission.unavailable}
    for period_id in period_ids:
        silent = [
            name for name in financial_sheets if (name, period_id) not in answered
        ]
        if silent:
            result.observations.append(
                f"{len(silent)} routed sheet(s) have no binding and no "
                f"unavailable note for {period_id!r}: {', '.join(silent[:8])}. "
                "They fall back to the workbook default column."
            )


def _column_number(excel_column: str) -> int | None:
    letters = (excel_column or "").strip().upper()
    if not letters or not letters.isalpha():
        return None
    number = 0
    for character in letters:
        number = number * 26 + ord(character) - ord("A") + 1
    return number


def _column_holds_numbers(sheet: WorkbookSheet, column: int) -> bool:
    return any(
        isinstance(cell.raw_value, int | float)
        and not isinstance(cell.raw_value, bool)
        for row in sheet.rows
        for cell in row.cells
        if cell.column == column
    )
