"""The single supported Hotel P&L normalization workflow."""

from __future__ import annotations

import gc
import json
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from hotel_pl_normalizer.mapping import compact_pdf_evidence, map_workbook
from hotel_pl_normalizer.mapping.evidence import compact_workbook_evidence
from hotel_pl_normalizer.models.period_selection import (
    PeriodCatalog,
    PeriodColumnSelection,
    PeriodColumnSelectionMap,
)
from hotel_pl_normalizer.models.run import StructureRun
from hotel_pl_normalizer.models.sheet_selection import SheetNameSelectionResult
from hotel_pl_normalizer.models.workbook import WorkbookRecord
from hotel_pl_normalizer.providers import ModelClient, create_model_client
from hotel_pl_normalizer.structure.ingestion import (
    read_excel_workbook,
    read_pdf_document,
)
from hotel_pl_normalizer.structure.pdf import bind_pdf_periods, explore_pdf


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
    feedback_manifest: dict[str, Any] = field(default_factory=dict)

    @property
    def mapped_account_count(self) -> int:
        period_values = self.period_values or {"selected": self.values}
        return sum(
            1
            for coa_id in self.coa
            if any(
                value is not None and abs(value) > 0.005
                for values in period_values.values()
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


def _mapping_sheet_routing_context(selection: SheetNameSelectionResult) -> list[dict]:
    """Carry exploration judgments forward without inventing row boundaries."""
    return [
        {
            "sheet_name": item.sheet_name,
            "include_as_financial_evidence": item.include_as_financial_evidence,
            "role": item.role.value,
            "confidence": item.confidence.value,
            "evidence": list(item.evidence),
        }
        for item in selection.selections
        if item.include_as_financial_evidence
    ]


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


@dataclass
class PdfDiscoveryResult:
    """Compact PDF state retained while a web user chooses periods."""

    document_id: str
    file_hash: str
    exploration: Any
    duration_ms: int = 0
    model_calls: list[dict] = field(default_factory=list)
    tool_trace: list[dict] = field(default_factory=list)


@dataclass
class _PdfStageSnapshot:
    structure: Any
    duration_ms: int = 0
    tool_trace: list[dict] = field(default_factory=list)


def _write_pdf_failure(
    path: Path,
    exc: Exception,
    *,
    client,
    stage: str,
) -> None:
    """Persist paid PDF-stage context before the exception leaves the pipeline."""
    diagnostics = dict(getattr(exc, "diagnostics", {}) or {})
    diagnostics.setdefault("stage", stage)
    diagnostics.setdefault(
        "model_calls", list(getattr(client, "usage_history", []) or [])
    )
    diagnostics.setdefault(
        "tool_trace", list(getattr(client, "last_tool_trace", []) or [])
    )
    payload = {
        "error": f"{type(exc).__name__}: {exc}",
        **diagnostics,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")


def discover_pdf_periods(
    pdf: Path,
    *,
    output_dir: Path,
    progress: Callable[[str], None] | None = None,
) -> PdfDiscoveryResult:
    """Discover PDF periods, persist compact JSON, then release page text."""
    from hotel_pl_normalizer.models.pdf_structure import PdfExploration

    started = time.perf_counter()
    pdf = Path(pdf)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    if progress:
        progress("Reading positioned text from the PDF")
    document = read_pdf_document(pdf, source_id=f"primary:{pdf.stem}")
    if progress:
        progress("Finding financial pages and available periods")
    client = create_model_client(
        reasoning_effort="medium",
        repair_reasoning_effort="medium",
    )
    try:
        explored = explore_pdf(document, client=client)
    except Exception as exc:
        _write_pdf_failure(
            output_dir / "failure.json",
            exc,
            client=client,
            stage="pdf_period_discovery",
        )
        raise
    result = PdfDiscoveryResult(
        document_id=document.document_id,
        file_hash=document.source.file_hash,
        exploration=PdfExploration.model_validate(explored.structure),
        duration_ms=round((time.perf_counter() - started) * 1000),
        model_calls=list(client.usage_history),
        tool_trace=list(explored.tool_trace),
    )
    (output_dir / "exploration.json").write_text(
        result.exploration.model_dump_json(indent=2), encoding="utf-8"
    )
    (output_dir / "discovery.json").write_text(
        json.dumps(
            {
                "document_id": result.document_id,
                "file_hash": result.file_hash,
                "duration_ms": result.duration_ms,
                "model_calls": result.model_calls,
                "tool_trace": result.tool_trace,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    del document
    gc.collect()
    return result


def load_pdf_discovery(output_dir: Path) -> PdfDiscoveryResult:
    """Load discovery without reconstructing the positioned PDF document."""
    from hotel_pl_normalizer.models.pdf_structure import PdfExploration

    output_dir = Path(output_dir)
    metadata = json.loads(
        (output_dir / "discovery.json").read_text(encoding="utf-8")
    )
    exploration_data = json.loads(
        (output_dir / "exploration.json").read_text(encoding="utf-8")
    )
    # Compatibility for discoveries persisted before recommendations were
    # removed. The value is deliberately ignored rather than used as a default.
    exploration_data.pop("recommended_period_id", None)
    exploration = PdfExploration.model_validate(exploration_data)
    return PdfDiscoveryResult(
        document_id=metadata["document_id"],
        file_hash=metadata["file_hash"],
        exploration=exploration,
        duration_ms=int(metadata.get("duration_ms") or 0),
        model_calls=list(metadata.get("model_calls") or []),
        tool_trace=list(metadata.get("tool_trace") or []),
    )


def normalize_pdf(
    pdf: Path,
    *,
    output_dir: Path,
    selected_period_ids: list[str],
    source_name: str,
    progress: Callable[[str], None] | None = None,
    on_activity: Callable[[str], None] | None = None,
    discovery: PdfDiscoveryResult | None = None,
    limiter=None,
) -> NormalizationResult:
    """Normalize a PDF after the caller explicitly chooses discovered periods."""
    started = time.perf_counter()
    pdf = Path(pdf)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    structure_dir = output_dir / "pdf_structure"
    structure_dir.mkdir(parents=True, exist_ok=True)

    if progress:
        progress(
            "Re-reading positioned text from the PDF"
            if discovery is not None
            else "Reading positioned text from the PDF"
        )
    document = read_pdf_document(pdf, source_id=f"primary:{pdf.stem}")
    structure_client = create_model_client(
        reasoning_effort="medium",
        repair_reasoning_effort="medium",
    )
    if discovery is None:
        if progress:
            progress("Finding financial pages and available periods")
        try:
            exploration = explore_pdf(document, client=structure_client)
        except Exception as exc:
            _write_pdf_failure(
                structure_dir / "failure.json",
                exc,
                client=structure_client,
                stage="pdf_period_discovery",
            )
            raise
    else:
        if (
            document.document_id != discovery.document_id
            or document.source.file_hash != discovery.file_hash
        ):
            raise RuntimeError(
                "The uploaded PDF changed after period discovery; start a new run."
            )
        structure_client.usage_history.extend(discovery.model_calls)
        exploration = _PdfStageSnapshot(
            structure=discovery.exploration,
            duration_ms=discovery.duration_ms,
            tool_trace=list(discovery.tool_trace),
        )
    if limiter is not None:
        limiter.watch(structure_client)
    discovered = {item.period_id: item for item in exploration.structure.periods}

    requested_ids = list(dict.fromkeys(selected_period_ids))
    if not requested_ids:
        raise RuntimeError(
            "PDF normalization requires explicit selected_period_ids after discovery."
        )
    unknown = [period_id for period_id in requested_ids if period_id not in discovered]
    if unknown:
        raise ValueError("Unknown PDF period id(s): " + ", ".join(unknown))

    if progress:
        progress("Binding selected periods to displayed PDF amount columns")
    try:
        binding = bind_pdf_periods(
            document,
            exploration.structure,
            client=structure_client,
            period_ids=requested_ids,
            cancel=limiter,
        )
    except Exception as exc:
        _write_pdf_failure(
            structure_dir / "failure.json",
            exc,
            client=structure_client,
            stage="pdf_anchor_binding",
        )
        raise
    usable_ids = [
        period_id
        for period_id in requested_ids
        if any(item.period_id == period_id for item in binding.structure.bindings)
    ]
    dropped_periods = {
        period_id: "No displayed amount column could be bound on any financial page."
        for period_id in requested_ids
        if period_id not in usable_ids
    }
    if not usable_ids:
        raise RuntimeError("No selected PDF period has a usable amount-column binding.")

    evidence = compact_pdf_evidence(
        document,
        binding.structure,
        period_ids=usable_ids,
    )
    if not any(item.get("selected_value") is not None for item in evidence):
        raise RuntimeError("PDF bindings produced no labeled numeric evidence rows.")
    labels = {period_id: discovered[period_id].label for period_id in usable_ids}
    period_maps = {}
    for period_id in usable_ids:
        period_maps[period_id] = PeriodColumnSelectionMap(
            selection_map_id=f"pdf:{document.document_id}:{period_id}",
            workbook_id=document.document_id,
            requested_period=labels[period_id],
            sheet_selections=[
                PeriodColumnSelection(
                    sheet_name=f"Pages {item.start_page}-{item.end_page}",
                    value_column=max(1, round(item.right_edge * 1000)),
                    excel_column=f"x={item.right_edge:.3f}",
                    period_label=labels[period_id],
                    evidence=list(item.evidence),
                )
                for item in binding.structure.bindings
                if item.period_id == period_id
            ],
            notes=["PDF point anchors; no intermediate Excel workbook was created."],
        )

    document_id = document.document_id
    del document
    gc.collect()

    (structure_dir / "exploration.json").write_text(
        exploration.structure.model_dump_json(indent=2), encoding="utf-8"
    )
    (structure_dir / "bindings.json").write_text(
        binding.structure.model_dump_json(indent=2), encoding="utf-8"
    )
    (structure_dir / "evidence.json").write_text(
        json.dumps(evidence, indent=2), encoding="utf-8"
    )

    if progress:
        progress("Mapping the PDF statement")
    mapping_client = create_model_client(
        reasoning_effort="max",
        repair_reasoning_effort="medium",
    )
    structure_calls = list(structure_client.usage_history)
    mapping_client.usage_history.extend(structure_calls)
    if limiter is not None:
        limiter.watch(mapping_client)
    mapping = map_workbook(
        workbook_id=document_id,
        requested_period=labels[usable_ids[0]],
        periods=period_maps,
        period_labels=labels,
        evidence=evidence,
        excluded_sheets=[],
        client=mapping_client,
        on_activity=on_activity,
        cancel=limiter,
    )
    structure_cost = structure_client.estimate_cost(structure_calls)
    mapping_cost = mapping_client.estimate_cost(mapping.model_calls)
    cost_usd = round(structure_cost + mapping_cost, 6)
    return NormalizationResult(
        workbook_id=document_id,
        source_name=source_name,
        period_label=labels[usable_ids[0]],
        values=mapping.values,
        coa=mapping.coa,
        period_labels=labels,
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
        duration_ms=(discovery.duration_ms if discovery is not None else 0)
        + round((time.perf_counter() - started) * 1000),
        session_calls=mapping.session_calls,
        session_call_ms=mapping.session_call_ms,
        session_tool_calls=mapping.session_tool_calls,
        session_exhausted=mapping.session_exhausted,
        cost_usd=cost_usd,
        mapping_provider=mapping_client.provider,
        mapping_model=mapping_client.model_name,
        cost_details={
            "scope": "full_workflow_estimate",
            "provider": mapping_client.provider,
            "model": mapping_client.model_name,
            "rates_usd_per_million_tokens": mapping_client.pricing_details(),
            "structure_cost_usd": structure_cost,
            "mapping_cost_usd": mapping_cost,
        },
        evidence=evidence,
        model_calls=[*structure_calls, *mapping.model_calls],
        tool_trace=[*exploration.tool_trace, *binding.tool_trace, *mapping.tool_trace],
        mapping_selection=mapping.mapping_selection,
        structure_stages=[
            {
                "stage_name": "pdf_period_discovery",
                "duration_ms": exploration.duration_ms,
                "status": "pass",
            },
            {
                "stage_name": "pdf_anchor_binding",
                "duration_ms": binding.duration_ms,
                "status": "pass",
            },
        ],
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
    limiter=None,
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
    excluded = set(sheet_selection.excluded_sheet_names)
    sheet_routing_context = _mapping_sheet_routing_context(sheet_selection)
    included = {
        sheet.sheet_name for sheet in record.sheets if sheet.sheet_name not in excluded
    }

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
    if limiter is not None:
        limiter.watch(client)
    primary_period_id = next(iter(period_maps))
    mapping = map_workbook(
        workbook_id=workbook_id,
        requested_period=period_labels[primary_period_id],
        periods=period_maps,
        period_labels=period_labels,
        evidence=evidence,
        excluded_sheets=sorted(excluded),
        client=client,
        sheet_routing_context=sheet_routing_context,
        on_activity=on_activity,
        cancel=limiter,
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
