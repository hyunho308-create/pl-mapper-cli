"""Mechanical validation for period-column bindings."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field

from hotel_pl_normalizer.models.binding import PeriodBinding, WorkbookBindings
from hotel_pl_normalizer.models.period_selection import CanonicalPeriod
from hotel_pl_normalizer.models.workbook import WorkbookSheet
from hotel_pl_normalizer.structure.period_headers import (
    column_forbidden_markers,
    period_column_problem,
)


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
    periods: list[CanonicalPeriod] | None = None,
) -> CheckResult:
    """Require one mechanically valid outcome for every routed sheet-period pair."""

    result = CheckResult()
    chosen = set(period_ids)
    financial = set(financial_sheets)
    periods_by_id = {period.period_id: period for period in periods or []}
    latest_period_year = max(
        (int(period.end_month[:4]) for period in periods_by_id.values()),
        default=0,
    )
    claimed: dict[str, list[PeriodBinding]] = {}
    binding_pairs = Counter(
        (binding.sheet_name, binding.period_id) for binding in submission.bindings
    )
    unavailable_pairs = Counter(
        (item.sheet_name, item.period_id) for item in submission.unavailable
    )

    for (sheet_name, period_id), count in sorted(binding_pairs.items()):
        if count > 1:
            result.rejections.append(
                f"{sheet_name!r} has {count} bindings for {period_id!r}; "
                "one sheet-period pair must have exactly one outcome."
            )
    for (sheet_name, period_id), count in sorted(unavailable_pairs.items()):
        if count > 1:
            result.rejections.append(
                f"{sheet_name!r} has {count} unavailable entries for {period_id!r}; "
                "one sheet-period pair must have exactly one outcome."
            )
    for sheet_name, period_id in sorted(binding_pairs.keys() & unavailable_pairs.keys()):
        result.rejections.append(
            f"{sheet_name!r} and {period_id!r} appear in both bindings and "
            "unavailable; choose exactly one outcome."
        )

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
        if binding.sheet_name not in financial:
            result.rejections.append(
                f"{binding.sheet_name!r} is outside the routed financial-sheet scope."
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
        forbidden = column_forbidden_markers(sheet, column)
        if forbidden:
            result.rejections.append(
                f"Column {binding.excel_column} on {binding.sheet_name!r} is "
                f"explicitly marked as {', '.join(sorted(forbidden))}; a "
                "variance, percentage, POR/PAR, or ratio column cannot bind a "
                "financial period."
            )
            continue
        period = periods_by_id.get(binding.period_id)
        if period is not None:
            identity_problem = period_column_problem(
                sheet,
                period,
                binding.excel_column,
                latest_period_year=latest_period_year,
            )
            if identity_problem is not None:
                result.rejections.append(
                    f"Column {binding.excel_column} on {binding.sheet_name!r} "
                    f"cannot bind {binding.period_id!r}: {identity_problem}."
                )
                continue
        claimed.setdefault(binding.sheet_name, []).append(binding)

    for item in submission.unavailable:
        if item.period_id not in chosen:
            result.rejections.append(
                f"Unavailable entry {item.period_id!r} was not one of the chosen "
                f"periods ({', '.join(period_ids)})."
            )
        if item.sheet_name not in sheets:
            result.rejections.append(
                f"Unavailable entry names nonexistent sheet {item.sheet_name!r}."
            )
        elif item.sheet_name not in financial:
            result.rejections.append(
                f"Unavailable entry {item.sheet_name!r} is outside the routed "
                "financial-sheet scope."
            )
        if not item.reason.strip():
            result.rejections.append(
                f"Unavailable entry for {item.sheet_name!r} and {item.period_id!r} "
                "needs a reason."
            )

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

    for period_id, period in periods_by_id.items():
        confirmation = getattr(period, "department_confirmation", None)
        if confirmation is None:
            continue
        expected_column = confirmation.excel_column.strip().upper()
        confirmed = [
            binding
            for binding in submission.bindings
            if binding.period_id == period_id
            and binding.sheet_name == confirmation.sheet_name
            and binding.excel_column.strip().upper() == expected_column
        ]
        if not confirmed:
            result.rejections.append(
                f"Discovery confirmed {period_id!r} at "
                f"{confirmation.sheet_name!r}!{expected_column}; binding must "
                "retain that exact department location."
            )

    _reject_unbound_sheets(submission, financial_sheets, period_ids, result)
    return result


def _reject_unbound_sheets(
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
            result.rejections.append(
                f"{len(silent)} routed sheet(s) have no binding and no "
                f"unavailable note for {period_id!r}: {', '.join(silent[:8])}. "
                "Every routed sheet-period pair needs exactly one explicit outcome."
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
