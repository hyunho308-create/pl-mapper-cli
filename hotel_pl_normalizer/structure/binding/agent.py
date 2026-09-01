"""Run one period-column binding session over a parsed workbook."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from importlib import resources
from typing import Any, Callable

from hotel_pl_normalizer.models.binding import WorkbookBindings
from hotel_pl_normalizer.models.period_selection import PeriodOption
from hotel_pl_normalizer.models.workbook import WorkbookRecord

from .checks import check_bindings
from .toolset import PeriodBindingToolset


@dataclass
class BindingOutput:
    """What the stage produced, and what it cost."""

    structure: WorkbookBindings
    prompt: str
    duration_ms: int
    tool_calls: int
    reads: int
    rejections: list[str] = field(default_factory=list)
    observations: list[str] = field(default_factory=list)
    tool_trace: list[dict] = field(default_factory=list)


def render_binding_prompt(
    source_filename: str,
    *,
    periods: list[PeriodOption],
    financial_sheets: list[str],
    controlling_summary_sheet: str | None = None,
) -> str:
    skill = (
        resources.files("hotel_pl_normalizer.prompts")
        .joinpath("period_binding.md")
        .read_text(encoding="utf-8")
    )
    chosen = "\n".join(
        f"- `{period.period_id}` — {period.label}; scenario={period.scenario.value}; "
        f"inclusive coverage={period.start_month} through {period.end_month}"
        + (
            f"; reforecast={period.actual_months}+{12 - period.actual_months}"
            if period.actual_months is not None
            else ""
        )
        for period in periods
    )
    routed = ", ".join(financial_sheets) or "none recorded; judge from list_sheets"
    return "\n\n".join(
        [
            skill,
            "## This workbook",
            f"Filename: {source_filename}",
            "",
            "Routing selected these sheets as financial evidence. They are the "
            "binding scope; do not bind excluded sheets:\n\n"
            f"{routed}",
            "",
            "The periods a person chose, which you are being asked to bind:"
            f"\n\n{chosen}",
            (
                "Discovery's controlling summary is "
                f"`{controlling_summary_sheet}`. Every selected period was "
                "confirmed there, so each must have a usable amount-column binding "
                "on that sheet."
                if controlling_summary_sheet
                else ""
            ),
            "Call `list_sheet_layouts` to begin.",
        ]
    )


def bind_periods(
    workbook: WorkbookRecord,
    *,
    client,
    periods: list[PeriodOption],
    financial_sheets: list[str] | None = None,
    controlling_summary_sheet: str | None = None,
    max_iterations: int = 80,
    max_reads: int = 120,
    on_activity: Callable[[str], None] | None = None,
) -> BindingOutput:
    """Bind each chosen period to one value column per routed sheet."""

    started = time.perf_counter()
    trace: list[dict] = []
    period_ids = [period.period_id for period in periods]
    toolset = PeriodBindingToolset(
        workbook,
        period_ids=period_ids,
        financial_sheets=financial_sheets,
        controlling_summary_sheet=controlling_summary_sheet,
        max_reads=max_reads,
    )
    prompt = render_binding_prompt(
        workbook.source.original_filename,
        periods=periods,
        financial_sheets=toolset.financial_sheets,
        controlling_summary_sheet=toolset.controlling_summary_sheet,
    )
    options: dict[str, Any] = {
        "toolset": toolset,
        "max_iterations": max_iterations,
        "trace": trace,
        "on_activity": on_activity,
    }

    try:
        raw = client.generate_json_model_with_tools(prompt, WorkbookBindings, **options)
        structure = (
            raw
            if isinstance(raw, WorkbookBindings)
            else WorkbookBindings.model_validate(raw)
        )
    except Exception:  # noqa: BLE001 - salvage valid bindings from a dead session
        salvaged = toolset.best_effort()
        if salvaged is None:
            raise
        structure = salvaged

    # The tool normally enforces this matrix before accepting a submission, but
    # keep the stage boundary closed as well.  A provider-returned final object
    # or an exception salvage must never bypass complete sheet-period coverage.
    final_check = check_bindings(
        structure,
        toolset.sheets,
        period_ids=period_ids,
        financial_sheets=toolset.financial_sheets,
    )
    if not final_check.accepted:
        raise RuntimeError(
            "Period binding failed final deterministic validation: "
            + " ".join(final_check.rejections)
        )
    if controlling_summary_sheet is not None:
        bound_on_anchor = {
            item.period_id
            for item in structure.bindings
            if item.sheet_name == controlling_summary_sheet
        }
        missing_anchor = [
            period_id for period_id in period_ids if period_id not in bound_on_anchor
        ]
        if missing_anchor:
            raise RuntimeError(
                "Period binding failed final deterministic validation: discovery "
                f"confirmed {', '.join(missing_anchor)} on controlling summary "
                f"{controlling_summary_sheet!r}, but no usable binding survived."
            )

    return BindingOutput(
        structure=structure,
        prompt=prompt,
        duration_ms=round((time.perf_counter() - started) * 1000),
        tool_calls=len(trace),
        reads=toolset.reads,
        rejections=list(toolset.rejections),
        observations=list(toolset.observations),
        tool_trace=trace,
    )
