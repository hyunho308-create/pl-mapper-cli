"""The single supported Hotel P&L normalization workflow."""

from __future__ import annotations

import gc
import json
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from hotel_pl_normalizer.mapping import map_workbook
from hotel_pl_normalizer.mapping.evidence import compact_workbook_evidence
from hotel_pl_normalizer.models.period_selection import (
    PeriodCatalog,
    PeriodColumnSelectionMap,
)
from hotel_pl_normalizer.models.run import StructureRun
from hotel_pl_normalizer.models.sheet_selection import SheetNameSelectionResult
from hotel_pl_normalizer.models.workbook import WorkbookRecord
from hotel_pl_normalizer.providers import ModelClient, create_model_client
from hotel_pl_normalizer.structure.ingestion import read_excel_workbook


@dataclass
class NormalizationResult:
    """Everything needed to render and audit one normalized workbook."""

    workbook_id: str
    source_name: str
    period_label: str
    values: dict[str, float | None]
    coa: dict[str, dict]
    period_labels: dict[str, str] = field(default_factory=dict)
    period_values: dict[str, dict[str, float | None]] = field(default_factory=dict)
    residual_plugs_by_period: dict[str, dict[str, float]] = field(default_factory=dict)
    checks_by_period: dict[str, list[Any]] = field(default_factory=dict)
    execution_issues_by_period: dict[str, list[str]] = field(default_factory=dict)
    dropped_periods: dict[str, str] = field(default_factory=dict)
    decisions: list[Any] = field(default_factory=list)
    checks: list[Any] = field(default_factory=list)
    execution_issues: list[str] = field(default_factory=list)
    review_items: list[Any] = field(default_factory=list)
    accepted: bool = False
    outcome: str = "rejected"
    exceptions: list[dict[str, Any]] = field(default_factory=list)
    stopped_reason: str | None = None
    duration_ms: int = 0
    session_calls: int = 0
    session_call_ms: list[int] = field(default_factory=list)
    session_tool_calls: int = 0
    session_exhausted: bool = False
    cost_usd: float | None = None
    mapping_provider: str = ""
    mapping_model: str = ""
    cost_details: dict[str, Any] = field(default_factory=dict)
    evidence: list[dict] = field(default_factory=list)
    model_calls: list[dict] = field(default_factory=list)
    tool_trace: list[dict] = field(default_factory=list)
    mapping_selection: dict[str, Any] = field(default_factory=dict)
    structure_stages: list[dict] = field(default_factory=list)

    @property
    def mapped_account_count(self) -> int:
        return sum(
            1
            for coa_id in self.coa
            if any(
                value is not None and abs(value) > 0.005
                for values in self.period_values.values()
                for value in [values.get(coa_id)]
            )
        )

    @property
    def session_ms(self) -> int:
        return sum(self.session_call_ms)


@dataclass
class SharedWorkbook:
    """One parsed workbook retained through selection and evidence extraction."""

    path: Path
    record: WorkbookRecord | None = None
    released: bool = False

    def require(self) -> WorkbookRecord:
        if self.released:
            raise ValueError("This workbook was already released to mapping.")
        if self.record is None:
            self.record = read_excel_workbook(self.path)
        return self.record

    def release(self) -> None:
        self.record = None
        self.released = True


def shared_workbook(workbook: Path) -> SharedWorkbook:
    return SharedWorkbook(Path(workbook))


def _artifact(run: StructureRun, stage_name: str, key: str) -> dict:
    stage = next(item for item in run.stages if item.stage_name == stage_name)
    try:
        path = stage.artifact_paths[key]
    except KeyError as exc:
        raise LookupError(f"Structure run has no {stage_name}/{key} artifact") from exc
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _selected_periods(
    prior: StructureRun,
    period_ids: list[str],
) -> tuple[
    dict[str, PeriodColumnSelectionMap],
    dict[str, str],
    dict[str, str],
]:
    stored = _artifact(prior, "period_binding", "selections")
    maps: dict[str, PeriodColumnSelectionMap] = {}
    labels: dict[str, str] = {}
    dropped: dict[str, str] = {}
    for period_id in dict.fromkeys(period_ids):
        payload = stored.get(period_id)
        if payload is None:
            raise ValueError(f"Selected period is absent from binding: {period_id}")
        selection = PeriodColumnSelectionMap.model_validate(payload)
        if not selection.sheet_selections and selection.default_selection is None:
            dropped[period_id] = "; ".join(selection.notes) or "No usable column was bound."
            continue
        maps[period_id] = selection
        labels[period_id] = selection.requested_period
    if not maps:
        raise RuntimeError("None of the selected periods had a usable source column.")
    return maps, labels, dropped


def _structure_usage(prior: StructureRun) -> list[dict]:
    if prior.telemetry is None:
        return []
    return [
        {
            "provider": call.provider,
            "model_name": call.model_name,
            "prompt_token_count": int(call.prompt_tokens),
            "cached_content_token_count": int(call.cached_tokens),
            "cache_write_token_count": int(call.cache_write_tokens),
            "candidates_token_count": int(call.output_tokens),
            "thoughts_token_count": int(call.thoughts_tokens),
        }
        for call in prior.telemetry.model_calls
    ]


def estimate_workflow_cost(
    prior: StructureRun,
    mapping_calls: list[dict],
    client: ModelClient,
) -> tuple[float, dict[str, Any]]:
    """Price both workflow phases through the active supplier adapter."""
    structure_calls = _structure_usage(prior)
    structure_cost = client.estimate_cost(structure_calls)
    mapping_cost = client.estimate_cost(mapping_calls)
    return round(structure_cost + mapping_cost, 6), {
        "scope": "full_workflow_estimate",
        "provider": client.provider,
        "model": client.model_name,
        "rates_usd_per_million_tokens": client.pricing_details(),
        "structure_cost_usd": structure_cost,
        "mapping_cost_usd": mapping_cost,
    }


def analyze_workbook_structure(
    workbook: Path,
    *,
    output_dir: Path,
    discovery_run: StructureRun,
    selected_period_ids: list[str],
    parsed: SharedWorkbook,
    progress: Callable[[str], None] | None = None,
) -> StructureRun:
    from hotel_pl_normalizer.structure import WorkbookStructureAnalyzer

    if progress:
        progress("Binding selected periods to sheet columns")
    return WorkbookStructureAnalyzer().run(
        workbook,
        output_dir=output_dir,
        discovery_run=discovery_run,
        selected_period_ids=selected_period_ids,
        workbook_record=parsed.require(),
    )


def discover_workbook_periods(
    workbook: Path,
    *,
    output_dir: Path,
    parsed: SharedWorkbook,
    progress: Callable[[str], None] | None = None,
) -> StructureRun:
    from hotel_pl_normalizer.structure import WorkbookStructureAnalyzer

    if progress:
        progress("Finding available periods")
    return WorkbookStructureAnalyzer().discover_periods(
        workbook,
        output_dir=output_dir,
        workbook_record=parsed.require(),
    )


def normalize_workbook(
    workbook: Path,
    *,
    output_dir: Path,
    prior_run: StructureRun,
    selected_period_ids: list[str],
    source_name: str,
    parsed: SharedWorkbook,
    progress: Callable[[str], None] | None = None,
    on_activity: Callable[[str], None] | None = None,
) -> NormalizationResult:
    """Map the bound workbook to the Standard COA."""
    started = time.perf_counter()
    workbook = Path(workbook)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    failed = [stage.stage_name for stage in prior_run.stages if stage.status.value == "fail"]
    if failed:
        raise RuntimeError(
            "Structure analysis failed before mapping: " + ", ".join(failed)
        )

    record = parsed.require()
    parsed.release()
    period_maps, period_labels, dropped_periods = _selected_periods(
        prior_run, selected_period_ids
    )
    sheet_selection = SheetNameSelectionResult.model_validate(
        _artifact(prior_run, "sheet_routing", "selection")
    )
    skipped = set(sheet_selection.skipped_sheet_names)
    included = {sheet.sheet_name for sheet in record.sheets if sheet.sheet_name not in skipped}

    if progress:
        progress("Reading every relevant row")
    evidence = compact_workbook_evidence(
        record,
        period_maps,
        include_sheets=included,
    )
    workbook_id = record.workbook_id
    del record
    gc.collect()

    if progress:
        progress("Mapping the workbook")
    client = create_model_client(
        reasoning_effort="max",
        repair_reasoning_effort="medium",
    )
    primary_period_id = next(iter(period_maps))
    mapping = map_workbook(
        workbook_id=workbook_id,
        requested_period=period_labels[primary_period_id],
        periods=period_maps,
        period_labels=period_labels,
        evidence=evidence,
        skipped_sheets=sorted(skipped),
        client=client,
        on_activity=on_activity,
    )
    cost_usd, cost_details = estimate_workflow_cost(
        prior_run,
        mapping.model_calls,
        client,
    )
    duration_ms = round((time.perf_counter() - started) * 1000)
    if prior_run.telemetry is not None:
        duration_ms += prior_run.telemetry.metrics.duration_ms

    return NormalizationResult(
        workbook_id=workbook_id,
        source_name=source_name,
        period_label=period_labels[primary_period_id],
        values=mapping.values,
        coa=mapping.coa,
        period_labels=period_labels,
        period_values=mapping.values_by_period,
        residual_plugs_by_period=mapping.residual_plugs_by_period,
        checks_by_period=mapping.checks_by_period,
        execution_issues_by_period=mapping.execution_issues_by_period,
        dropped_periods=dropped_periods,
        decisions=mapping.decisions,
        checks=mapping.checks,
        execution_issues=mapping.execution_issues,
        review_items=mapping.review_items,
        accepted=mapping.accepted,
        outcome=mapping.outcome.value,
        exceptions=mapping.exceptions,
        stopped_reason=mapping.stopped_reason,
        duration_ms=duration_ms,
        session_calls=mapping.session_calls,
        session_call_ms=mapping.session_call_ms,
        session_tool_calls=mapping.session_tool_calls,
        session_exhausted=mapping.session_exhausted,
        cost_usd=cost_usd,
        mapping_provider=client.provider,
        mapping_model=client.model_name,
        cost_details=cost_details,
        evidence=list(evidence),
        model_calls=mapping.model_calls,
        tool_trace=mapping.tool_trace,
        mapping_selection=mapping.mapping_selection,
        structure_stages=[
            {
                "stage_name": stage.stage_name,
                "duration_ms": stage.duration_ms,
                "status": stage.status.value,
            }
            for stage in prior_run.stages
        ],
    )


def validated_period_ids(prior: StructureRun) -> set[str]:
    """Return periods from a successful discovery session."""
    stage = next(
        item for item in prior.stages if item.stage_name == "period_discovery"
    )
    if stage.status.value == "fail":
        return set()
    catalog = PeriodCatalog.model_validate(
        _artifact(prior, "period_discovery", "catalog")
    )
    return {option.period_id for option in catalog.options}
