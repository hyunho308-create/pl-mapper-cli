"""Run one stateful session that routes sheets and discovers periods."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from importlib import resources
from typing import Any, Callable

from hotel_pl_normalizer.models.exploration import WorkbookExploration

from .reader import LazyWorkbook
from .toolset import WorkbookExplorationToolset


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
    """The session's opening prompt, including sheet-routing guidance."""
    skill = (
        resources.files("hotel_pl_normalizer.prompts")
        .joinpath("workbook_exploration.md")
        .read_text(encoding="utf-8")
    )
    return "\n\n".join(
        [
            skill,
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
            "on_activity": on_activity,
        }
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
