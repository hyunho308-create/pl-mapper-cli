"""One stateful session that routes sheets and discovers periods together.

Replaces two stages. Both asked questions about sheet names and header text, and
asking them separately let them disagree -- discovery chose its five sheets with
a cheap score that picked a Table of Contents and a payroll register on one
workbook, while routing already knew better. One session makes one decision.

It is also the first stage that reads the workbook itself rather than being
handed a packet built from a full parse. That is the point: the stage this
replaces failed on East Miami because a fixed four-row header window sat one row
above the month headers, and nothing inside that window could have recovered
them. Reading rows 13-14 does.

Repair is the session, not a second prompt. When a submission is rejected the
objection goes back into the same conversation, so the model still has every row
it read. The stage this replaces re-rendered a fresh prompt for each repair pass
and made the model rediscover its own context.
"""

from __future__ import annotations

import inspect
import time
from dataclasses import dataclass, field
from importlib import resources
from typing import Any, Callable

from hotel_pl_normalizer.models.exploration import WorkbookExploration

from .reader import LazyWorkbook
from .toolset import WorkbookExplorationToolset


def _accepts(func, parameter: str) -> bool:
    """Does this callable take that keyword? Clients differ in what they report."""
    try:
        return parameter in inspect.signature(func).parameters
    except (TypeError, ValueError):
        return False


@dataclass
class ExplorationOutput:
    """What the stage produced, and what it cost."""

    structure: WorkbookExploration
    prompt: str
    duration_ms: int
    tool_calls: int
    rejections: list[str] = field(default_factory=list)
    tool_trace: list[dict] = field(default_factory=list)


def render_exploration_prompt(source_filename: str) -> str:
    """The session's opening prompt.

    The routing skill is appended verbatim rather than summarised. It is the
    accumulated knowledge of which hotel sheets matter -- which duplicate summary
    to prefer, why plain Laundry differs from Guest Laundry -- and paraphrasing
    it here would fork it.
    """
    skill = (
        resources.files("hotel_pl_normalizer.prompts")
        .joinpath("workbook_exploration.md")
        .read_text(encoding="utf-8")
    )
    routing = (
        resources.files("hotel_pl_normalizer.prompts")
        .joinpath("sheet_name_triage.md")
        .read_text(encoding="utf-8")
    )
    return "\n\n".join(
        [
            skill,
            routing,
            "## This workbook",
            f"Filename: {source_filename}",
            "Call `list_sheets` to begin.",
        ]
    )


def explore_workbook(
    workbook_path,
    *,
    client,
    # A session now owes `list_sheets`, both submissions, and a read of five
    # financial sheets before periods are accepted -- eight turns before any
    # exploration or repair. Twelve left no room: one workbook exhausted the
    # loop and produced nothing at all.
    max_iterations: int = 24,
    max_reads: int = 40,
    on_activity: Callable[[str], None] | None = None,
) -> ExplorationOutput:
    """Run one exploration session over a workbook.

    `max_iterations` bounds the conversation and `max_reads` bounds the file
    reads separately, because they fail differently: a model that keeps reading
    without submitting is a different problem from one that submits repeatedly
    and keeps being rejected.
    """
    started = time.perf_counter()
    trace: list[dict] = []

    with LazyWorkbook(workbook_path) as workbook:
        toolset = WorkbookExplorationToolset(workbook, max_reads=max_reads)
        prompt = render_exploration_prompt(workbook.path.name)
        options: dict[str, Any] = {
            "toolset": toolset,
            "max_iterations": max_iterations,
            "trace": trace,
        }
        # Only the Fireworks client reports progress mid-session; the Gemini one
        # has no such parameter. Passed conditionally so this stage runs on
        # either without the caller caring which.
        if on_activity is not None and _accepts(
            client.generate_json_model_with_tools, "on_activity"
        ):
            options["on_activity"] = on_activity
        raw = client.generate_json_model_with_tools(
            prompt, WorkbookExploration, **options
        )

    structure = (
        raw
        if isinstance(raw, WorkbookExploration)
        else WorkbookExploration.model_validate(raw)
    )
    return ExplorationOutput(
        structure=structure,
        prompt=prompt,
        duration_ms=round((time.perf_counter() - started) * 1000),
        tool_calls=len(trace),
        rejections=list(toolset.rejections),
        tool_trace=trace,
    )


def exploration_summary(output: ExplorationOutput) -> dict[str, Any]:
    """A flat view for benching one run against another."""
    structure = output.structure
    return {
        "layout": structure.workbook_layout.value,
        "sheets": len(structure.sheets),
        "financial_sheets": len(structure.financial_sheet_names),
        "periods": len(structure.periods),
        "period_labels": [period.label for period in structure.periods],
        "recommended": structure.recommended_period_id,
        "tool_calls": output.tool_calls,
        "rejections": len(output.rejections),
        "duration_ms": output.duration_ms,
    }
