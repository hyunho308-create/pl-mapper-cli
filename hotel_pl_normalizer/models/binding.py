"""What one department-and-binding session returns.

This replaces two stages, and the shape is smaller than either of them because
of what the result is actually *for*. Everything here ends up in
`mapping/evidence.py`, which asks one question per row: which column holds this
row's value for this period. It answers it in three steps -- a department span
covering the row, then a sheet-level column, then a workbook default.

Two consequences shape the models below.

**Binding is per sheet, and only per sheet.** The old stage bound a column per
department *location*, which is why it needed a location before it could bind
anything. But a column convention belongs to a sheet: the reason two locations
disagree is that they are on different tabs, and "a one-column shift between
Summary and department tabs is common" is the whole of it.

Within one tab, every department reads the same columns -- confirmed as a
property of hotel P&Ls rather than an observation about this corpus. So
`PeriodBinding` names a sheet and nothing finer. An earlier version carried an
optional `department` for the case where one section of a tab read a different
column from the rest; that case does not occur, and the field was a degree of
freedom the model filled in inconsistently rather than a capability anything
needed.

**Department spans are a hint, not a gate.** The rows reaching the mapper are
chosen by *routing*, not by this stage, so a department nothing claims still
reaches the model with its sheet-level column. A missed span costs the mapper a
grouping hint it can re-derive from the labels in front of it. A missed or wrong
*column* costs it the figures. They are not the same kind of mistake and this
module does not treat them as one.
"""

from __future__ import annotations

from .common import StrictModel
from .department_location import DepartmentSectionRole
from .sheet_selection import CANONICAL_DEPARTMENTS

# The canonical department strings, repeated here so the schema can enumerate
# them. A model that invents `franchise_fees` produces a span nothing downstream
# recognises, and an enum is a cheaper way to say so than a validator.
DEPARTMENTS = CANONICAL_DEPARTMENTS


class DepartmentSpan(StrictModel):
    """Where one department sits: a sheet, and optionally rows within it."""

    department: str
    sheet_name: str
    # Omitted together when the whole sheet is this department, which is the
    # ordinary case in a multi-tab workbook.
    start_row: int | None = None
    end_row: int | None = None
    section_role: DepartmentSectionRole = DepartmentSectionRole.PRIMARY
    evidence: list[str] = []


class WorkbookDepartments(StrictModel):
    """Phase one: where every department sits.

    Submitted before any period work. Exploration learned that a session told
    about both its jobs at once optimises for the second and lets the first fall
    out of it -- one run marked sixty-five real schedules `skip` with the reason
    "not opened this session". Ordering the phases removes that by construction.
    """

    departments: list[DepartmentSpan] = []
    notes: list[str] = []


class PeriodBinding(StrictModel):
    """Which column on this sheet holds this period."""

    period_id: str
    sheet_name: str
    excel_column: str
    evidence: list[str] = []


class UnavailablePeriod(StrictModel):
    """A period that is genuinely not on this sheet.

    Recorded rather than argued with. The sheet keeps whatever the workbook
    default resolves to, and the run continues -- one sheet lacking one period is
    not a reason to abandon eleven departments that bound cleanly.
    """

    period_id: str
    sheet_name: str
    reason: str


class WorkbookBindings(StrictModel):
    """Phase two: a column per sheet per chosen period."""

    bindings: list[PeriodBinding] = []
    unavailable: list[UnavailablePeriod] = []
    notes: list[str] = []


class DepartmentBinding(StrictModel):
    """The whole result, assembled from both phases."""

    departments: list[DepartmentSpan] = []
    bindings: list[PeriodBinding] = []
    unavailable: list[UnavailablePeriod] = []
    notes: list[str] = []
    # What the checks observed but did not enforce. Carried so a reader of the
    # run log can see what was odd about a workbook without rerunning it.
    observations: list[str] = []
