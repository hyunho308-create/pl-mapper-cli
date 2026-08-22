"""Run one department-and-binding session over a parsed workbook.

Modelled on `exploration/agent.py`, and thin for the same reason: the session's
behaviour lives in the prompt and the toolset, not here. This assembles the
prompt, runs the loop, and reports what it cost.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from importlib import resources
from typing import Any, Callable

from hotel_pl_normalizer.models.binding import DepartmentBinding, WorkbookDepartments
from hotel_pl_normalizer.models.workbook import WorkbookRecord

from .toolset import DepartmentBindingToolset


def should_locate_departments(financial_sheets: list[str]) -> bool:
    """Is locating departments worth what it costs on this workbook?

    Only where the workbook has more than one sheet to answer for. There, a
    department is largely a sheet and the answer falls out of reads the binding
    half needs anyway -- twenty routed tabs cost about eight tool calls in total.

    On a one-sheet P&L it is the opposite. Every department is a row range, found
    only by scanning the sheet, and that scanning is where this stage spends:
    across the corpus 208 of 317 tool calls happened before `submit_departments`,
    and on the eight single-sheet workbooks it was nearly all of them -- Hotel
    Kabuki spent 27 calls on departments and 3 on the columns. Those eight are
    45 % of the corpus bill.

    What that buys is a grouping hint. The mapper is shown every row of the sheet
    with its labels and can tell a Rooms line from a Utilities line unaided, so
    the hint is the cheapest thing to give up. The column is not, and the binding
    half keeps its own read and coverage requirements either way.

    The corpus splits cleanly at one: eight workbooks route a single sheet and
    the next has ten, so there is no borderline case to arbitrate.
    """
    return len(financial_sheets) > 1


@dataclass
class BindingOutput:
    """What the stage produced, and what it cost."""

    structure: DepartmentBinding
    prompt: str
    duration_ms: int
    tool_calls: int
    reads: int
    rejections: list[str] = field(default_factory=list)
    observations: list[str] = field(default_factory=list)
    tool_trace: list[dict] = field(default_factory=list)
    # Whether this run was asked to locate departments at all, so an empty
    # department list can be told apart from a run that looked and found none.
    located_departments: bool = True


def _prompt_text(name: str) -> str:
    return (
        resources.files("hotel_pl_normalizer.prompts")
        .joinpath(name)
        .read_text(encoding="utf-8")
    )


def render_binding_prompt(
    source_filename: str,
    *,
    periods: list[tuple[str, str]],
    financial_sheets: list[str],
    locate_departments: bool = True,
) -> str:
    """The session's opening prompt.

    The chosen periods are named here as well as returned by phase one. Phase two
    needs them, but a model that knows from the start what it will be asked to
    bind reads header rows once rather than twice.

    With departments off there is no phase to withhold anything from, so the
    binding skill goes in the opening prompt rather than arriving as the result
    of a submission. The ordering exists to stop a two-job session optimising for
    the second job; with one job there is nothing to order.
    """
    if locate_departments:
        skill = _prompt_text("department_binding.md")
    else:
        skill = "\n\n".join(
            [_prompt_text("binding_only.md"), _prompt_text("period_binding.md")]
        )
    chosen = "\n".join(f"- `{period_id}` — {label}" for period_id, label in periods)
    routed = ", ".join(financial_sheets) or "none recorded; judge from list_sheets"
    return "\n\n".join(
        [
            skill,
            "## This workbook",
            f"Filename: {source_filename}",
            "",
            "Routing selected these sheets as holding P&L content. Treat it as a "
            "strong hint, not a boundary — a department on a sheet routing "
            f"skipped is accepted:\n\n{routed}",
            "",
            "The periods a person has chosen, which you are being asked to "
            f"bind:\n\n{chosen}",
            "Call `list_sheets` to begin.",
        ]
    )


def bind_departments(
    workbook: WorkbookRecord,
    *,
    client,
    period_ids: list[str],
    period_labels: dict[str, str] | None = None,
    financial_sheets: list[str] | None = None,
    # A session owes list_sheets, two submissions, and enough reads to see every
    # routed sheet's header block plus its department headings -- and a workbook
    # with twenty routed tabs needs twenty reads before it can answer anything.
    # Generous on purpose, and measured: forty was not enough for Hotel Kabuki,
    # which spent the lot and returned nothing. Tighten this from the observed
    # distribution once there is one, not from a guess.
    max_iterations: int = 80,
    max_reads: int = 120,
    locate_departments: bool | None = None,
    on_activity: Callable[[str], None] | None = None,
) -> BindingOutput:
    """Locate every department, then bind each chosen period to a column.

    `locate_departments=None` decides per workbook -- see
    `should_locate_departments`. Pass a bool to force it either way.
    """
    started = time.perf_counter()
    trace: list[dict] = []
    labels = dict(period_labels or {})
    toolset = DepartmentBindingToolset(
        workbook,
        period_ids=period_ids,
        period_labels=labels,
        financial_sheets=financial_sheets,
        max_reads=max_reads,
        locate_departments=True,
    )
    if locate_departments is None:
        # Decided from the sheets routing actually selected, which the toolset
        # has already narrowed to sheets this workbook has.
        locate_departments = should_locate_departments(toolset.financial_sheets)
    toolset.locate_departments = locate_departments
    if not locate_departments:
        toolset.departments = WorkbookDepartments()
    prompt = render_binding_prompt(
        workbook.source.original_filename,
        periods=[(period_id, labels.get(period_id, period_id)) for period_id in period_ids],
        financial_sheets=toolset.financial_sheets,
        locate_departments=locate_departments,
    )
    options: dict[str, Any] = {
        "toolset": toolset,
        "max_iterations": max_iterations,
        "trace": trace,
        "on_activity": on_activity,
    }

    try:
        raw = client.generate_json_model_with_tools(prompt, DepartmentBinding, **options)
        structure = (
            raw
            if isinstance(raw, DepartmentBinding)
            else DepartmentBinding.model_validate(raw)
        )
    except Exception:  # noqa: BLE001 - a dead session must not cost the property
        # The loop ran out of turns, or the transport failed. Whatever the
        # session had already established is worth more than an exception: with
        # departments and no bindings the mapper still runs on the workbook's
        # default column, and with nothing it does not run at all.
        salvaged = toolset.best_effort()
        if salvaged is None:
            raise
        structure = salvaged

    return BindingOutput(
        structure=structure,
        located_departments=locate_departments,
        prompt=prompt,
        duration_ms=round((time.perf_counter() - started) * 1000),
        tool_calls=len(trace),
        reads=toolset.reads,
        rejections=list(toolset.rejections),
        observations=list(toolset.observations),
        tool_trace=trace,
    )
