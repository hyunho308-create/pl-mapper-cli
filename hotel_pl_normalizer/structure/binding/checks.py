"""The only things this stage refuses, and the things it merely notices.

The split is the whole design. A rejection goes back into the session, which can
look again; an observation is recorded and the answer stands. Getting the split
wrong is expensive in one direction only:

- A wrong column means every figure the mapper reads is wrong, and nothing
  downstream can tell -- the numbers look reasonable.
- A refused answer means the mapper does not run at all.
- A missed or ragged department span means the mapper loses a grouping hint it
  can re-derive from the row labels it is already shown.

So the rejections are the ones where the answer cannot be true of this workbook,
and everything about hotels is prose. Five rejections, all arithmetic or
identity; four observations, none of which stops anything.

What is deliberately *not* here
-------------------------------
No check compares a period's identity against a column's header. The period was
found by an earlier pass and chosen by a person, so a stage that can refuse it on
semantic grounds is a stage that can take a period away after it was offered --
which is exactly the failure this replaces. Whether a header says Budget is the
model's reading, told in prose, and a sheet it cannot place a period on is
recorded as unavailable rather than argued with.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from hotel_pl_normalizer.models.binding import (
    DEPARTMENTS,
    DepartmentSpan,
    PeriodBinding,
    WorkbookBindings,
    WorkbookDepartments,
)
from hotel_pl_normalizer.models.department_location import DepartmentSectionRole
from hotel_pl_normalizer.models.workbook import WorkbookSheet

# Roles that may legitimately repeat for one department. An operator can report
# Rooms payroll on its own schedule, and F&B is routinely one consolidated block
# plus many outlets.
_REPEATABLE_ROLES = {
    DepartmentSectionRole.DETAIL,
    DepartmentSectionRole.KPI,
    DepartmentSectionRole.SUPPORTING_DETAIL,
    DepartmentSectionRole.SUMMARY,
}

# What an omitted `end_row` means when comparing two spans: the bottom of the
# sheet. Larger than any spreadsheet row so the comparisons need no special case.
_OPEN_END = 2**31 - 1


@dataclass
class CheckResult:
    """Why a submission cannot stand, or what was odd about one that can."""

    rejections: list[str] = field(default_factory=list)
    observations: list[str] = field(default_factory=list)

    @property
    def accepted(self) -> bool:
        return not self.rejections


def normalize_spans(submission: WorkbookDepartments) -> WorkbookDepartments:
    """Fill in a half-given row range instead of refusing it.

    A span with `start_row` and no `end_row` is obvious in context: it runs to
    the next department on that sheet, or to the bottom. Refusing it taught the
    model the wrong lesson on the first workbook this ran on -- told to "give
    both, or neither", it gave *neither* for ten departments and returned ten
    identical whole-sheet claims on a single-tab P&L, which is no hint at all.

    Normalising costs nothing and removes a whole class of round trip. It runs
    before the checks so everything downstream sees complete spans.
    """
    by_sheet: dict[str, list[int]] = {}
    for span in submission.departments:
        if span.start_row is not None:
            by_sheet.setdefault(span.sheet_name, []).append(span.start_row)
    for starts in by_sheet.values():
        starts.sort()

    repaired = []
    for span in submission.departments:
        if (span.start_row is None) == (span.end_row is None):
            repaired.append(span)
            continue
        if span.end_row is None:
            later = [
                start
                for start in by_sheet.get(span.sheet_name, [])
                if start > span.start_row
            ]
            # No later department on this sheet: leave the end open. `None` reads
            # as "to the bottom" everywhere downstream, which is what it means.
            repaired.append(
                span.model_copy(update={"end_row": later[0] - 1}) if later else span
            )
            continue
        earlier = [
            start for start in by_sheet.get(span.sheet_name, []) if start < span.end_row
        ]
        repaired.append(
            span.model_copy(update={"start_row": max(earlier) if earlier else 1})
        )
    return submission.model_copy(update={"departments": repaired})


def check_departments(
    submission: WorkbookDepartments,
    sheets: dict[str, WorkbookSheet],
    *,
    financial_sheets: list[str],
    read_sheets: set[str],
) -> CheckResult:
    """Could this set of spans be true of this workbook?

    `sheets` is every sheet in the workbook, so a span on a sheet routing did not
    select is fine -- routing is sometimes wrong, and a department found on a
    real sheet it skipped is the model being right. GRY lost all 156 of its
    accounts to that being an error.
    """
    result = CheckResult()
    if not submission.departments:
        result.rejections.append(
            "No department was located. Read the sheets routing selected and "
            "return at least the departments you can see."
        )
        return result

    for span in submission.departments:
        _check_span(span, sheets, result)
    _check_whole_sheet_collisions(submission.departments, result)
    if result.rejections:
        return result

    _observe_overlaps(submission.departments, result)
    _observe_duplicate_primaries(submission.departments, result)
    _observe_unclaimed_sheets(
        submission.departments, financial_sheets, read_sheets, result
    )
    _observe_missing_fnb_summary(submission.departments, result)
    return result


def _check_span(
    span: DepartmentSpan, sheets: dict[str, WorkbookSheet], result: CheckResult
) -> None:
    """One span, checked against the sheet it names. Arithmetic only."""
    if span.department not in DEPARTMENTS:
        result.rejections.append(
            f"{span.department!r} is not one of the department strings. Use one "
            f"of: {', '.join(DEPARTMENTS)}."
        )
        return
    sheet = sheets.get(span.sheet_name)
    if sheet is None:
        result.rejections.append(
            f"There is no sheet named {span.sheet_name!r} in this workbook. Use "
            "a name from list_sheets."
        )
        return
    # A half-given range was filled in by `normalize_spans`; either bound may
    # still be open, and an open bound means "to the edge of the sheet".
    if span.start_row is None or span.end_row is None:
        return
    if span.start_row > span.end_row:
        result.rejections.append(
            f"{span.department} on {span.sheet_name!r} has start_row "
            f"{span.start_row} after end_row {span.end_row}."
        )
        return
    last_row = max((row.row_index for row in sheet.rows), default=0)
    if span.start_row > last_row:
        result.rejections.append(
            f"{span.department} starts at row {span.start_row}, but "
            f"{span.sheet_name!r} has no rows past {last_row}."
        )


def _check_whole_sheet_collisions(
    spans: list[DepartmentSpan], result: CheckResult
) -> None:
    """A department claiming the entire sheet while other departments share it.

    Two spellings of one degenerate answer: several departments each declared as
    the whole sheet, or one span running from the sheet's first row to its last
    with the others inside it. Both say every row belongs to several departments
    at once, which is no hint at all, and both are what a single-tab P&L
    collapses into when the model stops looking for boundaries. Swexan returned
    Summary as rows 1-439 of a 439-row sheet with nine departments in it.

    Deliberately narrower than "one span contains the others". Departments are
    *not* always disjoint: a P&L exported by department groups Rooms revenue
    beside F&B revenue near the top and Rooms expense beside F&B expense far
    below, so a Rooms span legitimately runs from row 11 to row 288 and swallows
    a small Miscellaneous Income block on the way. Refusing that would refuse a
    correct answer, so only a span covering the *whole sheet* qualifies -- no
    real department reaches both the title rows and the last row.
    """
    by_sheet: dict[str, list[DepartmentSpan]] = {}
    for span in spans:
        by_sheet.setdefault(span.sheet_name, []).append(span)

    for sheet_name, sheet_spans in sorted(by_sheet.items()):
        if len(sheet_spans) < 2:
            continue
        whole = [span for span in sheet_spans if _covers_whole_sheet(span, sheet_spans)]
        if not whole:
            continue
        named = ", ".join(span.department for span in whole)
        result.rejections.append(
            f"On {sheet_name!r}, {named} span(s) the entire sheet while "
            f"{len(sheet_spans) - len(whole)} other department(s) also claim rows "
            "on it, so every row belongs to several departments at once. Give "
            "each department the rows it actually occupies. In a single-tab P&L "
            "the opening Summary ends at the row before the first detailed "
            "department begins, usually Rooms."
        )


def _covers_whole_sheet(
    span: DepartmentSpan, sheet_spans: list[DepartmentSpan]
) -> bool:
    """Does this span run from the top of the sheet to the bottom?

    Measured against the other spans rather than the sheet's true extent,
    because a department reaching both the first row any department claims and
    the last one is the degenerate answer whatever the sheet's real height.
    """
    others = [other for other in sheet_spans if other is not span]
    if not others:
        return False
    start = span.start_row if span.start_row is not None else 1
    end = span.end_row if span.end_row is not None else _OPEN_END
    return start <= min(
        other.start_row if other.start_row is not None else 1 for other in others
    ) and end >= max(
        other.end_row if other.end_row is not None else _OPEN_END for other in others
    )


def _observe_overlaps(spans: list[DepartmentSpan], result: CheckResult) -> None:
    """Departments whose rows run into each other.

    Noticed, not refused, and not always a mistake: a workbook that groups all
    revenue together and all expenses together interleaves its departments by
    construction, and the overlapping spans are the correct description of it.
    Where it *is* a ragged boundary, the mapper reads the row labels itself and
    can tell a Rooms line from an F&B line, so refusing would spend a round trip
    on something it handles anyway.
    """
    by_sheet: dict[str, list[DepartmentSpan]] = {}
    for span in spans:
        if span.start_row is not None:
            by_sheet.setdefault(span.sheet_name, []).append(span)

    for sheet_name, sheet_spans in sorted(by_sheet.items()):
        ordered = sorted(sheet_spans, key=lambda span: span.start_row or 0)
        clashes = [
            f"{first.department}/{second.department}"
            for first, second in zip(ordered, ordered[1:])
            if (first.end_row if first.end_row is not None else _OPEN_END)
            >= (second.start_row or 0)
        ]
        if clashes:
            result.observations.append(
                f"On {sheet_name!r}, {len(clashes)} department boundary/ies "
                f"overlap ({', '.join(clashes[:4])})."
            )


def _observe_duplicate_primaries(
    spans: list[DepartmentSpan], result: CheckResult
) -> None:
    """Two primaries for one department double-count it -- but only sometimes.

    An observation rather than a rejection because the alternative reading is
    just as common: an operator reporting one department across two schedules,
    which the mapper handles from the labels. Rejecting costs a round trip on
    something the mapper is going to sort out anyway.
    """
    primaries: dict[str, list[str]] = {}
    for span in spans:
        if span.section_role in _REPEATABLE_ROLES:
            continue
        primaries.setdefault(span.department, []).append(
            f"{span.sheet_name}"
            + (f" rows {span.start_row}-{span.end_row}" if span.start_row else "")
        )
    for department, places in sorted(primaries.items()):
        if len(places) > 1:
            result.observations.append(
                f"{department} has {len(places)} primary locations "
                f"({'; '.join(places)}). One is usually split detail."
            )


def _observe_unclaimed_sheets(
    spans: list[DepartmentSpan],
    financial_sheets: list[str],
    read_sheets: set[str],
    result: CheckResult,
) -> None:
    """Sheets routing chose that no span mentions.

    Routing already decided these were worth reading, so silence about one is
    worth surfacing. It is not worth refusing over: the mapper is shown these
    rows regardless of whether a department claims them, because routing -- not
    this stage -- decides which sheets reach it.
    """
    claimed = {span.sheet_name for span in spans}
    unclaimed = [name for name in financial_sheets if name not in claimed]
    if unclaimed:
        unread = [name for name in unclaimed if name not in read_sheets]
        detail = f" ({len(unread)} of them never opened)" if unread else ""
        result.observations.append(
            f"{len(unclaimed)} routed sheet(s) are not claimed by any "
            f"department{detail}: {', '.join(unclaimed[:8])}."
        )


def _observe_missing_fnb_summary(
    spans: list[DepartmentSpan], result: CheckResult
) -> None:
    """F&B outlets with no consolidated summary among them.

    Worth saying because the loss is quiet -- the outlets add up to something
    plausible and nothing looks wrong.
    """
    fnb = [span for span in spans if span.department == "food_and_beverage"]
    if len(fnb) > 1 and not any(
        span.section_role == DepartmentSectionRole.SUMMARY for span in fnb
    ):
        result.observations.append(
            f"{len(fnb)} F&B locations and none marked summary. If one of them "
            "is the consolidated block, the department total depends on it."
        )


def check_bindings(
    submission: WorkbookBindings,
    sheets: dict[str, WorkbookSheet],
    *,
    period_ids: list[str],
    financial_sheets: list[str],
) -> CheckResult:
    """Could these bindings be true of this workbook?

    Three rejections, all mechanical: the column has to exist and hold numbers,
    the period has to be one that was chosen, and one column cannot be two
    periods on the same sheet. Nothing here reads a header.
    """
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
                f"There is no sheet named {binding.sheet_name!r} in this "
                "workbook."
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
                    f"{len(periods)} different periods ({', '.join(sorted(periods))}). "
                    "One column holds one period."
                )

    if result.rejections:
        return result

    _observe_unbound_sheets(submission, financial_sheets, period_ids, result)
    return result


def _observe_unbound_sheets(
    submission: WorkbookBindings,
    financial_sheets: list[str],
    period_ids: list[str],
    result: CheckResult,
) -> None:
    """Sheets with neither a binding nor an unavailable note, per period.

    An observation. A sheet with no binding falls back to the workbook's usual
    column in `mapping/evidence.py`, which is usually right -- most workbooks use
    one column convention throughout -- so this is worth recording and not worth
    a round trip.
    """
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
    """Does this column carry any number anywhere on the sheet?

    Sheet-wide rather than span-scoped on purpose. A column that is empty within
    one department but populated elsewhere is an ordinary layout, and refusing it
    would take away a binding the rest of the sheet needs.
    """
    return any(
        isinstance(cell.raw_value, int | float) and not isinstance(cell.raw_value, bool)
        for row in sheet.rows
        for cell in row.cells
        if cell.column == column
    )
