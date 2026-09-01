"""The two upstream PDF model sessions, stopping before account mapping."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from hotel_pl_normalizer.models.pdf import PdfDocumentRecord
from hotel_pl_normalizer.models.pdf_structure import (
    PdfBindings,
    PdfExploration,
)
from hotel_pl_normalizer.providers.base import ProviderRunCancelled

from .stages import PdfBindingToolset, PdfExplorationToolset


@dataclass
class PdfStageOutput:
    structure: Any
    duration_ms: int
    tool_trace: list[dict] = field(default_factory=list)
    rejections: list[str] = field(default_factory=list)


class PdfStageFailure(RuntimeError):
    """A failed PDF model stage with its already-paid diagnostic context."""

    def __init__(
        self,
        message: str,
        *,
        stage: str,
        client,
        trace: list[dict],
        rejections: list[str],
    ) -> None:
        super().__init__(message)
        self.diagnostics = {
            "stage": stage,
            "model_calls": list(getattr(client, "usage_history", []) or []),
            "tool_trace": list(trace),
            "rejections": list(rejections),
        }


def explore_pdf(document: PdfDocumentRecord, *, client, cancel=None) -> PdfStageOutput:
    toolset = PdfExplorationToolset(document)
    trace: list[dict] = []
    started = time.perf_counter()
    prompt = """You are inspecting a hotel P&L PDF directly, not an Excel conversion.

This session has two ordered phases. First route every page using contiguous page ranges. Split ranges at statement boundaries even when adjacent pages have the same role, so the controlling summary and distinct department schedules remain identifiable. For every range return include_as_financial_evidence, role, confidence, and evidence. Included roles are summary_p_and_l, department_p_and_l, financial_supporting_schedule, and unknown. Excluded roles are topline_operating_statistics, balance_sheet, payroll_statistics, and other. A financial supporting page contains selected-period financial amounts useful for mapping or reconciliation. Topline operating statistics include RN, occupancy, ADR, RevPAR, and segmentation without P&L amounts. Payroll statistics include FTE, hours, wage rates, and staffing without labor expense amounts; a page with usable labor or payroll expense is financial evidence. Unknown is included conservatively. Call inspect_document and list_pages, inspect representative pages, then submit_routing.

After routing, follow the returned period instructions. Choose one controlling core summary statement, read its full period header, and inspect up to four normal department P&L pages to confirm the periods used throughout the core document. Periods found only on auxiliary T12, monthly, trend, department, or supporting pages cannot expand the catalog. This restriction does not suppress periods displayed by the controlling summary itself: when the controlling summary is a T12 or monthly spread, return every displayed monthly amount column as a selectable monthly period, plus its TTM or Total amount column when present. Never replace the displayed months with only their aggregate. Return the controlling summary page range explicitly. Enumerate distinct amount periods using scenario plus inclusive start_month and end_month. Scenarios are only actual, budget, and forecast; a Prior Year column is an older actual. For full-calendar-year reforecasts, set actual_months to the completed Actual count from 1 through 11; omit it for pure forecasts and every non-calendar-year period. Percent and variance anchors are not periods. Omit period_id and label; the tool creates both deterministically from scenario and the inclusive dates. Include exact page/line evidence, then submit_periods. Do not map accounts or transcribe statement amounts."""
    try:
        client.generate_json_model_with_tools(
            prompt,
            PdfExploration,
            toolset=toolset,  # type: ignore[arg-type]
            max_iterations=24,
            trace=trace,
            cancel=cancel,
        )
    except ProviderRunCancelled:
        raise
    except Exception as exc:
        raise PdfStageFailure(
            f"PDF exploration failed before an accepted submit_periods call: {exc}",
            stage="pdf_period_discovery",
            client=client,
            trace=trace,
            rejections=toolset.rejections,
        ) from exc
    if toolset.submission is None:
        raise PdfStageFailure(
            "PDF exploration ended without an accepted submit_periods call.",
            stage="pdf_period_discovery",
            client=client,
            trace=trace,
            rejections=toolset.rejections,
        )
    # The terminal tool result is the validated result. A provider may also
    # parse a bare final JSON response, but that response cannot bypass the
    # routing, read-floor, and evidence checks above.
    structure = toolset.submission
    return PdfStageOutput(
        structure=structure,
        duration_ms=round((time.perf_counter() - started) * 1000),
        tool_trace=trace,
        rejections=list(toolset.rejections),
    )

def bind_pdf_periods(
    document: PdfDocumentRecord,
    exploration: PdfExploration,
    *,
    client,
    period_ids: list[str],
    cancel=None,
) -> PdfStageOutput:
    toolset = PdfBindingToolset(document, exploration, period_ids)
    trace: list[dict] = []
    started = time.perf_counter()
    selected_periods = {
        period.period_id: period
        for period in exploration.periods
        if period.period_id in set(period_ids)
    }
    chosen = "\n".join(
        (
            f"- {period_id}: {selected_periods[period_id].label}; "
            f"scenario={selected_periods[period_id].scenario.value}; "
            f"inclusive coverage={selected_periods[period_id].start_month} "
            f"through {selected_periods[period_id].end_month}"
            if period_id in selected_periods
            else f"- {period_id}"
        )
        for period_id in period_ids
    )
    financial = ", ".join(
        f"{item.start_page}-{item.end_page}"
        for item in exploration.page_ranges
        if item.include_as_financial_evidence
    )
    summary = exploration.controlling_summary_pages
    summary_pages = (
        f"{summary.start_page}-{summary.end_page}" if summary is not None else "unknown"
    )
    prompt = f"""Bind selected PDF periods to displayed numeric anchors. This is the second and final upstream structure stage; do not map accounts or transcribe statement amounts.

Selected periods:
{chosen}

Routed financial page ranges: {financial}
Controlling summary pages: {summary_pages}

Call list_financial_layouts first. It groups pages only by deterministic geometry and gives representative pages plus exact page ranges for each common numeric anchor; it does not decide period meaning. Read the full headers on representative pages for the material layout groups, using read_page_lines and numeric_anchors on at least five representative financial pages in total (or all if fewer). Every selected period was confirmed on the controlling summary and normal department schedules, so bind it on the controlling summary and at least one routed department P&L page; never mark the core document unavailable everywhere. In an explicit PERIODIC/PTD plus YEAR TO DATE/YTD layout, use inclusive coverage to choose the block before choosing Actual or Budget: a monthly period uses PERIODIC/PTD, while a full year, partial YTD, or TTM uses YEAR TO DATE/YTD. Amount and percent anchors alternate frequently: choose the amount anchor only.

Return one outcome for every layout_id and selected period using submit_layout_bindings. A layout binding names only layout_id, period_id, and the chosen listed right_edge; Python expands it to the anchor's exact page ranges and marks pages where that layout anchor is not displayed as unavailable. Use layout_unavailable only when the whole layout truly lacks the period. Use page_bindings or page_unavailable only for a verified exceptional page that must override its layout. Do not enumerate ordinary pages or copy the layout page ranges into the submission. If rejected, correct and resubmit the complete compact layout choices; at most two repairs are allowed. Never end with a bare JSON response; only an accepted submit_layout_bindings call completes this stage."""
    try:
        client.generate_json_model_with_tools(
            prompt,
            PdfBindings,
            toolset=toolset,  # type: ignore[arg-type]
            max_iterations=24,
            trace=trace,
            cancel=cancel,
        )
    except ProviderRunCancelled:
        raise
    except Exception as exc:
        raise PdfStageFailure(
            f"PDF binding failed before an accepted submit_layout_bindings call: {exc}",
            stage="pdf_anchor_binding",
            client=client,
            trace=trace,
            rejections=toolset.rejections,
        ) from exc
    if toolset.submission is None:
        raise PdfStageFailure(
            "PDF binding ended without an accepted submit_layout_bindings call.",
            stage="pdf_anchor_binding",
            client=client,
            trace=trace,
            rejections=toolset.rejections,
        )
    structure = toolset.submission
    return PdfStageOutput(
        structure=structure,
        duration_ms=round((time.perf_counter() - started) * 1000),
        tool_trace=trace,
        rejections=list(toolset.rejections),
    )
