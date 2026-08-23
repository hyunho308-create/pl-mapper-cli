"""What the numbers in a row span look like, column by column.

Why this is a tool and not a check
----------------------------------
The stage this belongs to decides which column holds each chosen period. Most of
that judgement comes from headers; this tool supplies the one useful numerical
cross-check and is precise about its limits.

`_candidate_looks_like_ratio_values` decides whether a column holds ratios rather
than amounts, and it will not conclude anything from a small sample: at least
five numeric values and three non-zeros before it calls a column ratio-scale.
Hotel Seattle's Management Fees location has *three* numeric rows, so that
function would never have fired on it. The model fired anyway -- one percentage
row among three was enough for it to declare column S ratio-scale, which ended
an otherwise-correct binding run.

So on that failure the rule was right and the model was wrong, and it was right
precisely because it refused to conclude anything from three values. Deleting it
would lose that. Keeping it as a veto puts a rule back in front of a judgement
the model is otherwise better at.

The resolution is to return the counts *with the sample size*, and to say in the
tool's own words when the sample is too thin to characterise anything. "3 values,
1 percentage" has to read as thin evidence rather than as a finding.

What this deliberately does not do
----------------------------------
**It does not read headers.** The session reads rows itself, with coordinates and
the sheet's merged ranges, so it has already seen the header block in its raw
form -- better than any extraction of it. The header context the old packet
builder assembled exists because that stage could not look; this one can. What a
header means, including a bare `Total` sitting after twelve Jan-Dec columns, is
prose and the model's call.

**It does not guess where the data starts.** `_first_business_row` exists because
a packet covered a whole sheet and had to find the schedule inside it. Here the
caller states the span it means, having read it.

**It does not decide anything.** Every note below states what the numbers are or
what they cannot support. None of them says a column is or is not a period.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any

from openpyxl.utils import get_column_letter

from hotel_pl_normalizer.models.workbook import CellRecord, WorkbookRow, WorkbookSheet

# Below either of these, the sample cannot characterise a column's scale. Taken
# from `_candidate_looks_like_ratio_values`, which is the judgement they encode:
# it is the floor that made that function right about Hotel Seattle.
MIN_NUMERIC_TO_CHARACTERISE = 5
MIN_NONZERO_TO_CHARACTERISE = 3

# A column needs more than one number in the span before it is worth reporting.
# One number is a stray, not a column of values.
MIN_NUMERIC_TO_REPORT = 2

# Bounds so one call cannot return a wall of columns. A hotel P&L schedule with
# more than this many numeric columns is a monthly spread, and the span-level
# count says so without listing every one.
MAX_COLUMNS = 40
MAX_SAMPLE_ROWS = 3

# Scale bands, unchanged from the packet builder. They are thresholds on
# magnitude, not on meaning: a column of values under 1.5 *may* be ratios, and
# saying so is the reader's job.
SUBUNIT_MAGNITUDE = 1.5
MATERIAL_MAGNITUDE = 10
LARGE_AMOUNT_MAGNITUDE = 1000


@dataclass(frozen=True)
class ValueSample:
    """One row's label and what this column holds on it."""

    row: int
    label: str
    value: float | None


@dataclass(frozen=True)
class ColumnFigures:
    """Every count for one column over one span, plus what they do not support."""

    column: int
    excel_column: str
    numeric_count: int = 0
    nonzero_count: int = 0
    percentage_formatted_count: int = 0
    subunit_nonzero_count: int = 0
    material_count: int = 0
    large_amount_count: int = 0
    samples: list[ValueSample] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def characterisable(self) -> bool:
        """Is there enough here to say anything about this column's scale?"""
        return (
            self.numeric_count >= MIN_NUMERIC_TO_CHARACTERISE
            and self.nonzero_count >= MIN_NONZERO_TO_CHARACTERISE
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "column": self.excel_column,
            "column_number": self.column,
            "numeric": self.numeric_count,
            "nonzero": self.nonzero_count,
            "percent_formatted": self.percentage_formatted_count,
            "under_1_5": self.subunit_nonzero_count,
            "over_10": self.material_count,
            "over_1000": self.large_amount_count,
            "samples": [
                {"row": sample.row, "label": sample.label, "value": sample.value}
                for sample in self.samples
            ],
            "notes": list(self.notes),
        }


@dataclass(frozen=True)
class SpanFigures:
    """The span itself, before any column is considered.

    Reported because it answers a different question from the columns: whether
    this range is substantive enough to characterize at all. A span with two
    labelled rows is weak evidence however clean its columns look.
    """

    sheet_name: str
    start_row: int
    end_row: int
    rows_in_span: int = 0
    labelled_rows: int = 0
    labelled_numeric_rows: int = 0
    first_numeric_row: int | None = None
    last_numeric_row: int | None = None
    columns: list[ColumnFigures] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": True,
            "sheet_name": self.sheet_name,
            "start_row": self.start_row,
            "end_row": self.end_row,
            "rows_in_span": self.rows_in_span,
            "labelled_rows": self.labelled_rows,
            "labelled_numeric_rows": self.labelled_numeric_rows,
            "first_numeric_row": self.first_numeric_row,
            "last_numeric_row": self.last_numeric_row,
            "columns": [column.as_dict() for column in self.columns],
            "notes": list(self.notes),
        }


def column_stats(
    sheet: WorkbookSheet,
    start_row: int,
    end_row: int,
    *,
    max_columns: int = MAX_COLUMNS,
) -> SpanFigures:
    """Count what every numeric column holds between two rows of one sheet.

    Rows without a label are skipped, exactly as the packet builder skips them: a
    header row of bare numbers is not evidence about a value column, and counting
    it inflates every total below.
    """
    rows = [row for row in sheet.rows if start_row <= row.row_index <= end_row]
    labelled: list[tuple[WorkbookRow, str, dict[int, CellRecord]]] = []
    for row in rows:
        label = _row_label(row)
        if not label:
            continue
        numbers = {
            cell.column: cell for cell in row.cells if _is_number(cell.raw_value)
        }
        labelled.append((row, label, numbers))

    numeric_rows = [row for row, _, numbers in labelled if numbers]
    counts = _counts_by_column(labelled)
    reported = sorted(
        column
        for column, tallies in counts.items()
        if tallies["numeric"] >= MIN_NUMERIC_TO_REPORT
    )[:max_columns]

    columns = [
        _column_figures(column, counts[column], _samples(labelled, set(reported), column))
        for column in reported
    ]
    span = SpanFigures(
        sheet_name=sheet.sheet_name,
        start_row=start_row,
        end_row=end_row,
        rows_in_span=len(rows),
        labelled_rows=len(labelled),
        labelled_numeric_rows=len(numeric_rows),
        first_numeric_row=numeric_rows[0].row_index if numeric_rows else None,
        last_numeric_row=numeric_rows[-1].row_index if numeric_rows else None,
        columns=columns,
        notes=_span_notes(len(rows), labelled, numeric_rows, counts, max_columns),
    )
    return span


def _counts_by_column(
    labelled: list[tuple[WorkbookRow, str, dict[int, CellRecord]]],
) -> dict[int, dict[str, int]]:
    counts: dict[int, dict[str, int]] = {}
    for _, _, numbers in labelled:
        for column, cell in numbers.items():
            tallies = counts.setdefault(
                column,
                {
                    "numeric": 0,
                    "nonzero": 0,
                    "percent": 0,
                    "subunit": 0,
                    "material": 0,
                    "large": 0,
                },
            )
            value = float(cell.raw_value)  # type: ignore[arg-type]
            tallies["numeric"] += 1
            if _is_percentage_format(cell.number_format):
                tallies["percent"] += 1
            if value == 0:
                continue
            magnitude = abs(value)
            tallies["nonzero"] += 1
            if magnitude <= SUBUNIT_MAGNITUDE:
                tallies["subunit"] += 1
            if magnitude >= MATERIAL_MAGNITUDE:
                tallies["material"] += 1
            if magnitude >= LARGE_AMOUNT_MAGNITUDE:
                tallies["large"] += 1
    return counts


def _column_figures(
    column: int, tallies: dict[str, int], samples: list[ValueSample]
) -> ColumnFigures:
    figures = ColumnFigures(
        column=column,
        excel_column=get_column_letter(column),
        numeric_count=tallies["numeric"],
        nonzero_count=tallies["nonzero"],
        percentage_formatted_count=tallies["percent"],
        subunit_nonzero_count=tallies["subunit"],
        material_count=tallies["material"],
        large_amount_count=tallies["large"],
        samples=samples,
    )
    # The notes read the counts, so they are attached afterwards rather than
    # computed alongside them.
    return replace(figures, notes=_column_notes(figures))


def _column_notes(figures: ColumnFigures) -> list[str]:
    """What these counts do, and do not, support.

    Every note is either a restatement of the numbers or a refusal. None of them
    concludes that a column is a percentage or a period --
    those are the reader's to decide, with the sheet in front of it.
    """
    notes: list[str] = []
    if figures.nonzero_count == 0:
        notes.append(
            f"Every one of the {figures.numeric_count} values in this span is "
            "zero, so the span carries no figures for this column."
        )
        # Nothing below adds anything once the column is empty.
        return notes

    if not figures.characterisable:
        notes.append(
            f"Only {figures.numeric_count} numeric value(s) and "
            f"{figures.nonzero_count} non-zero here — too few to characterise "
            "this column's scale. Judge it from its header, not from these "
            "counts."
        )

    if figures.percentage_formatted_count:
        share = (
            "every one of"
            if figures.percentage_formatted_count == figures.numeric_count
            else f"{figures.percentage_formatted_count} of"
        )
        notes.append(
            f"{share} the {figures.numeric_count} value(s) carry a percentage "
            "number format."
        )

    # Only stated where the sample can carry it. Below the floor these ratios are
    # noise, and reporting them is how a three-row section gets called a
    # percentage column.
    if figures.characterisable:
        if figures.large_amount_count == 0 and figures.material_count == 0:
            notes.append(
                "No value in this span reaches 10, which is unusual for a "
                "currency column on a schedule this size."
            )
        if (
            figures.subunit_nonzero_count * 5 >= figures.nonzero_count * 4
            and figures.material_count == 0
        ):
            notes.append(
                f"{figures.subunit_nonzero_count} of {figures.nonzero_count} "
                "non-zero values are at or below 1.5, and none reaches 10."
            )
    return notes


def _span_notes(
    row_count: int,
    labelled: list[tuple[WorkbookRow, str, dict[int, CellRecord]]],
    numeric_rows: list[WorkbookRow],
    counts: dict[int, dict[str, int]],
    max_columns: int,
) -> list[str]:
    notes: list[str] = []
    if not labelled:
        notes.append(
            f"None of the {row_count} row(s) in this span carries a label; the "
            "range may be header or spacer rows rather than a schedule."
        )
    elif not numeric_rows:
        notes.append(
            f"{len(labelled)} labelled row(s) and no numbers at all. This range "
            "holds labels but no figures."
        )
    # A short section is ordinary for Management Fees, Utilities, Miscellaneous
    # Income, OOD and Non-Op, so this states the count without calling it small.
    if numeric_rows:
        notes.append(
            f"{len(numeric_rows)} labelled row(s) in this span carry figures."
        )
    reportable = sum(
        1 for tallies in counts.values() if tallies["numeric"] >= MIN_NUMERIC_TO_REPORT
    )
    if reportable > max_columns:
        notes.append(
            f"{reportable} columns carry figures here; the first {max_columns} "
            "are shown. Narrow the span or read the header rows to choose "
            "between them."
        )
    return notes


def _samples(
    labelled: list[tuple[WorkbookRow, str, dict[int, CellRecord]]],
    reported: set[int],
    column: int,
) -> list[ValueSample]:
    """A few real (label, value) pairs, from the rows that are most populated.

    Ranked by how many of the reported columns a row fills, so the samples come
    from rows that show the span's shape rather than from a stray one, then
    presented in row order because that is how they read on the sheet.
    """
    ranked = sorted(
        labelled,
        key=lambda item: (-len(reported & set(item[2])), item[0].row_index),
    )[:MAX_SAMPLE_ROWS]
    return [
        ValueSample(
            row=row.row_index,
            label=label,
            value=(
                float(numbers[column].raw_value)  # type: ignore[arg-type]
                if column in numbers
                else None
            ),
        )
        for row, label, numbers in sorted(ranked, key=lambda item: item[0].row_index)
    ]


def _row_label(row: WorkbookRow) -> str | None:
    """The rightmost text cell that reads like a label, as the packets do.

    `display_value` is preferred but not required: a record built by hand, or by
    a reader that did not populate it, still has `raw_value`.

    Rightmost by column rather than by position in `cells`, because a row's cells
    are not guaranteed to be ordered and the label is the indented text nearest
    the figures.
    """
    candidates = [
        (cell.column, text)
        for cell in row.cells
        if isinstance(cell.raw_value, str)
        and (text := (cell.display_value or cell.raw_value).strip())
        and any(character.isalpha() for character in text)
    ]
    return max(candidates)[1] if candidates else None


def _is_number(value: Any) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool)


def _is_percentage_format(number_format: str | None) -> bool:
    return bool(number_format and "%" in number_format)
