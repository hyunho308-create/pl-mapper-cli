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

from .stages import PdfBindingToolset, PdfExplorationToolset


@dataclass
class PdfStageOutput:
    structure: Any
    duration_ms: int
    tool_trace: list[dict] = field(default_factory=list)
    rejections: list[str] = field(default_factory=list)


def explore_pdf(document: PdfDocumentRecord, *, client, cancel=None) -> PdfStageOutput:
    toolset = PdfExplorationToolset(document)
    trace: list[dict] = []
    started = time.perf_counter()
    prompt = """You are inspecting a hotel P&L PDF directly, not an Excel conversion.

This session has two ordered phases. First route every page using contiguous page ranges. Split ranges at statement boundaries even when adjacent pages have the same role, so the controlling summary and distinct department schedules remain identifiable. For every range return include_as_financial_evidence, role, confidence, and evidence. Included roles are summary_p_and_l, department_p_and_l, financial_supporting_schedule, and unknown. Excluded roles are topline_operating_statistics, balance_sheet, payroll_statistics, and other. A financial supporting page contains selected-period financial amounts useful for mapping or reconciliation. Topline operating statistics include RN, occupancy, ADR, RevPAR, and segmentation without P&L amounts. Payroll statistics include FTE, hours, wage rates, and staffing without labor expense amounts; a page with usable labor or payroll expense is financial evidence. Unknown is included conservatively. Call inspect_document and list_pages, inspect representative pages, then submit_routing.

After routing, follow the returned period instructions. Choose one controlling core summary statement, read its full period header, and inspect up to four normal department P&L pages to confirm the periods used throughout the core document. Periods found only on auxiliary T12, monthly, trend, department, or supporting pages cannot expand the catalog. This restriction does not suppress periods displayed by the controlling summary itself: when the controlling summary is a T12 or monthly spread, return every displayed monthly amount column as a selectable monthly period, plus its TTM or Total amount column when present. Never replace the displayed months with only their aggregate. Return the controlling summary page range explicitly. Enumerate distinct amount periods using scenario plus inclusive start_month and end_month. Scenarios are only actual, budget, and forecast; a Prior Year column is an older actual. For full-calendar-year reforecasts, set actual_months to the completed Actual count from 1 through 11; omit it for pure forecasts and every non-calendar-year period. Percent and variance anchors are not periods. Use deterministic IDs and labels from the schema and exact page/line evidence. Then submit_periods. Do not map accounts or transcribe statement amounts."""
    client.generate_json_model_with_tools(
        prompt,
        PdfExploration,
        toolset=toolset,  # type: ignore[arg-type]
        max_iterations=24,
        trace=trace,
        cancel=cancel,
    )
    if toolset.submission is None:
        raise RuntimeError(
            "PDF exploration ended without an accepted submit_periods call."
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
    labels = {
        period.period_id: period.label
        for period in exploration.periods
        if period.period_id in set(period_ids)
    }
    chosen = "\n".join(f"- {period_id}: {labels.get(period_id, period_id)}" for period_id in period_ids)
    financial = ", ".join(
        f"{item.start_page}-{item.end_page}"
        for item in exploration.page_ranges
        if item.include_as_financial_evidence
    )
    prompt = f"""Bind selected PDF periods to displayed numeric anchors. This is the second and final upstream structure stage; do not map accounts or transcribe statement amounts.

Selected periods:
{chosen}

Routed financial page ranges: {financial}

Use read_page_lines to locate the full header bands and numeric_anchors on at least five representative financial pages (or all if fewer). A binding is a period_id, contiguous page range, and the numeric tokens' repeated right_edge in PDF points. Amount and percent anchors alternate frequently: bind the amount anchor only. If a page range lacks the period, mark it unavailable instead of guessing. Split page ranges whenever layouts or right edges change. Cover every routed financial page exactly once for every selected period, then call submit_bindings.

submit_bindings is a full replacement, never a patch. If it is rejected, correct the issue and resubmit the complete bindings and unavailable lists, including every previously valid range. Continue calling submit_bindings until it returns accepted=true. Never end with a bare JSON response; only an accepted submit_bindings call completes this stage."""
    client.generate_json_model_with_tools(
        prompt,
        PdfBindings,
        toolset=toolset,  # type: ignore[arg-type]
        max_iterations=24,
        trace=trace,
        cancel=cancel,
    )
    if toolset.submission is None:
        raise RuntimeError(
            "PDF binding ended without an accepted submit_bindings call."
        )
    structure = toolset.submission
    return PdfStageOutput(
        structure=structure,
        duration_ms=round((time.perf_counter() - started) * 1000),
        tool_trace=trace,
        rejections=list(toolset.rejections),
    )
