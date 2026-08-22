"""Present one binding session in the shapes the pipeline already speaks.

`mapping/evidence.py` asks for a `DepartmentLocationMap` and a
`PeriodColumnSelectionMap` per period, and neither it nor the mapper needs to
learn that two stages became one.

The one asymmetry worth knowing: `evidence.py` resolves a row's column as
department-span, then sheet, then workbook default, and this stage only ever
populates the second. Selections carry no department, so the span-scoped lookup
never fires and every row on a sheet takes that sheet's column -- which is what a
hotel P&L does. Departments reach the mapper as a grouping hint in the
`DepartmentLocationMap`, and never as a column selector.
"""

from __future__ import annotations

from collections import Counter

from hotel_pl_normalizer.models.binding import DepartmentBinding
from hotel_pl_normalizer.models.common import ModelInfo
from hotel_pl_normalizer.models.department_location import (
    BoundaryConfidence,
    DepartmentLocation,
    DepartmentLocationKind,
    DepartmentLocationMap,
    DepartmentLocationValidation,
    ValidationStatus,
)
from hotel_pl_normalizer.models.period_selection import (
    PeriodColumnSelection,
    PeriodColumnSelectionMap,
)


def _column_number(excel_column: str) -> int:
    number = 0
    for character in (excel_column or "").strip().upper():
        number = number * 26 + ord(character) - ord("A") + 1
    return number


def _location_id(workbook_id: str, span) -> str:
    safe = "".join(c if c.isalnum() else "_" for c in span.sheet_name).strip("_").lower()
    if span.start_row is None and span.end_row is None:
        return f"{workbook_id}:{span.department}:{safe}"
    return f"{workbook_id}:{span.department}:{safe}:{span.start_row or ''}-{span.end_row or ''}"


def binding_to_location_map(
    structure: DepartmentBinding,
    *,
    workbook_id: str,
    model: ModelInfo | None = None,
    workbook_layout: str | None = None,
) -> DepartmentLocationMap:
    """The department spans, as the map department ID used to write.

    Validation is recorded as a pass carrying the session's observations. The
    session already refused anything that could not be true of this workbook, and
    an observation is by definition something that did not stop it -- writing it
    as a warning would make every downstream reader treat a normal workbook as
    suspect.
    """
    locations = [
        DepartmentLocation(
            location_id=_location_id(workbook_id, span),
            department=span.department,
            section_role=span.section_role,
            location_kind=(
                DepartmentLocationKind.SHEET
                if span.start_row is None and span.end_row is None
                else DepartmentLocationKind.RANGE
            ),
            sheet_name=span.sheet_name,
            start_row=span.start_row,
            end_row=span.end_row,
            # The session read the rows it is describing, but a boundary is a
            # hint for the mapper either way, and claiming `exact` would assert
            # more than anything here established.
            boundary_confidence=BoundaryConfidence.APPROXIMATE,
            evidence=list(span.evidence),
        )
        for span in structure.departments
    ]
    return DepartmentLocationMap(
        department_location_map_id=f"{workbook_id}:department_locations",
        workbook_id=workbook_id,
        model=model or ModelInfo(),
        workbook_layout=workbook_layout or "",
        locations=locations,
        validation=DepartmentLocationValidation(status=ValidationStatus.PASS),
        notes=[*structure.notes, *structure.observations],
    )


def binding_to_selection_maps(
    structure: DepartmentBinding,
    *,
    workbook_id: str,
    period_ids: list[str],
    period_labels: dict[str, str] | None = None,
    model: ModelInfo | None = None,
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
                # Deliberately unset. A selection carrying a department makes
                # `evidence.py` build a span-scoped override for that department's
                # rows; a sheet's columns do not change by department, so there is
                # nothing for that override to express.
                department=None,
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
            model=model or ModelInfo(),
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
            "department": None,
            "evidence": [
                f"Most common bound column across {len(selections)} sheet(s)."
            ],
        }
    )
