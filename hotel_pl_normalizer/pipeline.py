"""The single end-to-end workflow for normalizing one workbook.

``normalize_workbook`` first analyzes workbook structure, then passes those hints
to the generic mapper. Callers do not coordinate those steps themselves.
"""

from __future__ import annotations

import gc
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from hotel_pl_normalizer.limits import RunLimiter
from hotel_pl_normalizer.mapping import map_workbook
from hotel_pl_normalizer.mapping.evidence import compact_workbook_evidence
from hotel_pl_normalizer.models.department_location import DepartmentLocationMap
from hotel_pl_normalizer.models.period_selection import (
    PeriodCatalog,
    PeriodColumnPacket,
    PeriodColumnSelectionMap,
)
from hotel_pl_normalizer.models.run import StructureRun
from hotel_pl_normalizer.models.sheet_selection import SheetNameSelectionResult
from hotel_pl_normalizer.models.workbook import WorkbookRecord
from hotel_pl_normalizer.providers.fireworks import (
    DEFAULT_FIREWORKS_MODEL,
    FIREWORKS_CACHED_INPUT_USD_PER_MTOK,
    FIREWORKS_INPUT_USD_PER_MTOK,
    FIREWORKS_OUTPUT_USD_PER_MTOK,
    FireworksJsonClient,
    estimate_fireworks_cost,
)
from hotel_pl_normalizer.providers.gemini import GeminiJsonClient
from hotel_pl_normalizer.providers.openai_api import (
    DEFAULT_OPENAI_MODEL,
    OPENAI_CACHED_INPUT_USD_PER_MTOK,
    OPENAI_INPUT_USD_PER_MTOK,
    OPENAI_OUTPUT_USD_PER_MTOK,
    OpenAIJsonClient,
    estimate_openai_cost,
)
from hotel_pl_normalizer.structure.ingestion import read_excel_workbook
from hotel_pl_normalizer.structure.periods import (
    catalog_to_selection_map,
    validate_period_catalog,
)

MAPPING_PROVIDER_ENV_VAR = "FRESH_START_MAPPING_PROVIDER"
MAPPING_REASONING_EFFORT_ENV_VAR = "FRESH_START_MAPPING_REASONING_EFFORT"
DEFAULT_MAPPING_PROVIDER = "openai"
DEFAULT_MAPPING_REASONING_EFFORT = "max"
DEFAULT_GEMINI_MAPPING_MODEL = "gemini-3.6-flash"
GEMINI_MAPPING_INPUT_USD_PER_MTOK = 1.50
GEMINI_MAPPING_OUTPUT_USD_PER_MTOK = 7.50
# Standard Gemini 3.5 Flash Lite list rates. Output includes thinking tokens.
GEMINI_STRUCTURE_INPUT_USD_PER_MTOK = 0.30
GEMINI_STRUCTURE_OUTPUT_USD_PER_MTOK = 2.50

# Context-cached input is billed at a quarter of the standard rate. Worth having
# because a tool session re-sends its whole conversation every turn, so most of
# what it pays for is context it has already sent: on the binding corpus 59 % of
# input tokens came back marked cached, and charging them at full rate overstated
# that stage by about 40 %. Fireworks has had this since it was written --
# `estimate_fireworks_cost` splits the same way -- and Gemini was simply missed.
GEMINI_CACHED_INPUT_SHARE = 0.25
GEMINI_MAPPING_CACHED_INPUT_USD_PER_MTOK = (
    GEMINI_MAPPING_INPUT_USD_PER_MTOK * GEMINI_CACHED_INPUT_SHARE
)
GEMINI_STRUCTURE_CACHED_INPUT_USD_PER_MTOK = (
    GEMINI_STRUCTURE_INPUT_USD_PER_MTOK * GEMINI_CACHED_INPUT_SHARE
)


@dataclass
class NormalizationResult:
    """Everything a caller needs to render or judge one normalized workbook."""

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
    # Periods that were asked for and could not be bound, with the reason.
    # A run that maps three of four periods is a success with a caveat, and
    # the caveat has to travel with the result or the caller cannot tell the
    # difference between a period being absent and a period being refused.
    dropped_periods: dict[str, str] = field(default_factory=dict)
    decisions: list[Any] = field(default_factory=list)
    checks: list[Any] = field(default_factory=list)
    execution_issues: list[str] = field(default_factory=list)
    review_items: list[Any] = field(default_factory=list)
    accepted: bool = False
    duration_ms: int = 0
    # Per-call timings for the mapping session. Two runs of one workbook took 241s
    # and 626s for the same answer, and the breakdown that would have said whether
    # that was more rounds or slower rounds was thrown away with the client. It is
    # kept now: `session_calls` is the round count, `session_call_ms` their
    # durations, `session_tool_calls` how many tool invocations the model made.
    session_calls: int = 0
    session_call_ms: list[int] = field(default_factory=list)
    session_tool_calls: int = 0
    # True when the loop ran out of turns rather than finishing. The error is
    # deliberately swallowed below -- a partial answer beats none -- so without
    # this flag an exhausted session is indistinguishable from a clean one.
    session_exhausted: bool = False
    # Set when a deadline, a spend cap or a person ended the run. The values below
    # are still real -- they are computed from whatever the model had submitted --
    # but they describe a mapping that was never finished, and a caller that
    # presents them as complete is misreporting.
    stopped_reason: str | None = None
    # Estimated full-workflow cost, with the pricing basis in cost_details.
    cost_usd: float | None = None
    mapping_provider: str = "openai"
    mapping_model: str = DEFAULT_OPENAI_MODEL
    cost_details: dict[str, Any] = field(default_factory=dict)
    # The full audit trail, for `run_log`. `evidence` is every row the
    # model was shown; `model_calls` is each call with its real token counts and
    # latency; `tool_trace` is what it looked at and whether the tool accepted it.
    evidence: list[dict] = field(default_factory=list)
    model_calls: list[dict] = field(default_factory=list)
    tool_trace: list[dict] = field(default_factory=list)
    mapping_selection: dict[str, Any] = field(default_factory=dict)
    structure_stages: list[dict] = field(default_factory=list)

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
    """One parsed workbook the structure and mapping stages read once between them.

    Both stages need the same record back to back, and each used to read the file
    itself -- 26s on CMI, paid twice for the same bytes. Sharing it does not raise
    the peak, because only one record exists at a time either way.

    Reading is deferred to the first `require`, so a caller that hands this to both
    stages still pays nothing if neither stage gets that far.

    `release` is the point of wrapping the record at all. It is the expensive object
    -- 580 MB on a 1.4 MB workbook -- and `normalize_workbook` drops it before the
    mapping session, which runs for minutes. Passing the bare record would leave a
    second reference alive in the caller's frame for that whole session and quietly
    undo that, so ownership is transferred here rather than shared.
    """

    path: Path
    record: WorkbookRecord | None = None
    released: bool = False

    def require(self) -> WorkbookRecord:
        if self.released:
            raise ValueError(
                "This workbook was already released to the mapping stage."
            )
        if self.record is None:
            self.record = read_excel_workbook(
                self.path, source_id=f"primary:{self.path.stem}"
            )
        return self.record

    def release(self) -> None:
        self.record = None
        self.released = True


def shared_workbook(workbook: Path) -> SharedWorkbook:
    """A workbook handle the structure and mapping stages can share one read of."""
    return SharedWorkbook(Path(workbook))


def _artifact(run: StructureRun, stage_name: str, key: str) -> dict:
    for stage in run.stages:
        if stage.stage_name == stage_name:
            paths = stage.artifact_paths or {}
            if key in paths:
                import json

                return json.loads(Path(paths[key]).read_text(encoding="utf-8"))
    raise LookupError(f"prior run has no {stage_name}/{key} artifact")


def _selected_period(
    prior: StructureRun, period_id: str
) -> tuple[PeriodColumnSelectionMap, str]:
    """Turn one saved catalog option into the mapper's existing period map."""
    packet = PeriodColumnPacket.model_validate(
        _artifact(prior, "period_selection", "packet")
    )
    catalog = PeriodCatalog.model_validate(
        _artifact(prior, "period_selection", "catalog")
    )
    option = next(
        (item for item in catalog.options if item.period_id == period_id),
        None,
    )
    if option is None:
        raise ValueError(f"Unknown saved period id: {period_id}")
    selected_catalog = catalog.model_copy(
        update={"recommended_period_id": period_id}
    )
    return (
        catalog_to_selection_map(
            packet,
            selected_catalog,
            requested_period=option.label,
        ),
        option.label,
    )


def _binding_selections(
    prior: StructureRun, period_ids: list[str]
) -> tuple[dict[str, PeriodColumnSelectionMap], dict[str, str]] | None:
    """Selection maps written directly by the combined binding stage.

    The packet path stores a `PeriodColumnPacket` and a `PeriodCatalog` and
    rebuilds the selections from them on demand. The binding stage produces
    selections as its actual output and never builds a packet, so it writes them
    whole rather than faking the inputs they would have been derived from.

    Returns None when the run came from the packet path, which is what keeps
    both alive.
    """
    try:
        stored = _artifact(prior, "department_id", "selections")
    except LookupError:
        return None
    maps: dict[str, PeriodColumnSelectionMap] = {}
    labels: dict[str, str] = {}
    for period_id in dict.fromkeys(period_ids):
        payload = stored.get(period_id)
        if payload is None:
            raise ValueError(f"Unknown saved period id: {period_id}")
        selection_map = PeriodColumnSelectionMap.model_validate(payload)
        maps[period_id] = selection_map
        labels[period_id] = selection_map.requested_period
    return maps, labels


def _selected_periods(
    prior: StructureRun, period_ids: list[str]
) -> tuple[dict[str, PeriodColumnSelectionMap], dict[str, str]]:
    bound = _binding_selections(prior, period_ids)
    if bound is not None:
        return bound
    maps = {}
    labels = {}
    for period_id in dict.fromkeys(period_ids):
        maps[period_id], labels[period_id] = _selected_period(prior, period_id)
    return maps, labels


def estimate_gemini_mapping_cost(mapping_calls: list[dict]) -> float:
    """Estimate mapping calls at the configured Gemini mapping-model rates."""
    input_tokens = sum(
        int(call.get("prompt_token_count") or 0) for call in mapping_calls
    )
    output_tokens = sum(
        int(call.get("candidates_token_count") or 0)
        + int(call.get("thoughts_token_count") or 0)
        for call in mapping_calls
    )
    cached_tokens = sum(
        min(
            int(call.get("prompt_token_count") or 0),
            max(0, int(call.get("cached_content_token_count") or 0)),
        )
        for call in mapping_calls
    )
    return round(
        (input_tokens - cached_tokens) * GEMINI_MAPPING_INPUT_USD_PER_MTOK / 1_000_000
        + cached_tokens * GEMINI_MAPPING_CACHED_INPUT_USD_PER_MTOK / 1_000_000
        + output_tokens * GEMINI_MAPPING_OUTPUT_USD_PER_MTOK / 1_000_000,
        6,
    )


def _structure_cost(prior: StructureRun) -> tuple[float, list[dict[str, Any]]]:
    """Price each structure call using the provider that actually handled it.

    Cached input is separated from fresh, because a tool-calling stage re-sends
    its whole conversation every turn and most of what it pays for is context it
    has already sent. Billing all of it at the standard rate overstated the
    binding stage by about 40 %.

    `cached_tokens` is a subset of `prompt_tokens`, not an addition to it, so the
    fresh share is the difference and the two together are still the prompt.
    """
    if prior.telemetry is None:
        return 0.0, []
    groups: dict[str, dict[str, Any]] = {}
    for call in prior.telemetry.model_calls:
        provider = call.provider or "google"
        group = groups.setdefault(
            provider,
            {
                "provider": provider,
                "models": set(),
                "input_tokens": 0,
                "cached_input_tokens": 0,
                "cache_write_tokens": 0,
                "output_tokens": 0,
                "openai_cost_usd": 0.0,
            },
        )
        if call.model_name:
            group["models"].add(call.model_name)
        prompt = int(call.prompt_tokens)
        # Clamped: a provider reporting more cached than prompt would otherwise
        # make the fresh share negative and quietly discount the bill.
        cached = min(prompt, max(0, int(call.cached_tokens)))
        group["input_tokens"] += prompt
        group["cached_input_tokens"] += cached
        cache_write = min(
            prompt - cached,
            max(0, int(getattr(call, "cache_write_tokens", 0))),
        )
        group["cache_write_tokens"] += cache_write
        group["output_tokens"] += int(call.output_tokens) + int(call.thoughts_tokens)
        if provider == "openai":
            group["openai_cost_usd"] += estimate_openai_cost([{
                "model_name": call.model_name,
                "prompt_token_count": prompt,
                "cached_content_token_count": cached,
                "cache_write_token_count": cache_write,
                "candidates_token_count": int(call.output_tokens),
                "thoughts_token_count": int(call.thoughts_tokens),
            }])

    details = []
    total = 0.0
    for provider, group in sorted(groups.items()):
        if provider == "openai":
            input_rate = OPENAI_INPUT_USD_PER_MTOK
            cached_rate = OPENAI_CACHED_INPUT_USD_PER_MTOK
            output_rate = OPENAI_OUTPUT_USD_PER_MTOK
        elif provider == "fireworks":
            input_rate = FIREWORKS_INPUT_USD_PER_MTOK
            cached_rate = FIREWORKS_CACHED_INPUT_USD_PER_MTOK
            output_rate = FIREWORKS_OUTPUT_USD_PER_MTOK
        else:
            input_rate = GEMINI_STRUCTURE_INPUT_USD_PER_MTOK
            cached_rate = GEMINI_STRUCTURE_CACHED_INPUT_USD_PER_MTOK
            output_rate = GEMINI_STRUCTURE_OUTPUT_USD_PER_MTOK
        cached_tokens = group["cached_input_tokens"]
        fresh_tokens = group["input_tokens"] - cached_tokens
        if provider == "openai":
            cost = group.pop("openai_cost_usd")
        else:
            group.pop("openai_cost_usd")
            cost = (
                fresh_tokens * input_rate
                + cached_tokens * cached_rate
                + group["output_tokens"] * output_rate
            ) / 1_000_000
        total += cost
        details.append(
            {
                **group,
                "models": sorted(group["models"]),
                "cost_usd": round(cost, 6),
                "rates_usd_per_million_tokens": {
                    "input": input_rate,
                    "cached_input": cached_rate,
                    "output": output_rate,
                },
            }
        )
    return round(total, 6), details


def _mapping_client(provider: str, model: str | None):
    if provider == "openai":
        reasoning = os.environ.get(
            MAPPING_REASONING_EFFORT_ENV_VAR, DEFAULT_MAPPING_REASONING_EFFORT
        )
        return OpenAIJsonClient(
            model_name=model,
            reasoning_effort=reasoning,
            repair_reasoning_effort=reasoning,
        )
    if provider == "fireworks":
        selected_model = model or DEFAULT_FIREWORKS_MODEL
        return FireworksJsonClient(
            model_name=selected_model,
            repair_model_name=selected_model,
        )
    if provider == "gemini":
        selected_model = model or DEFAULT_GEMINI_MAPPING_MODEL
        return GeminiJsonClient(
            model_name=selected_model,
            repair_model_name=selected_model,
        )
    raise ValueError(
        f"Unsupported mapping provider {provider!r}; use 'openai', 'fireworks', "
        "or 'gemini'."
    )


def estimate_workflow_cost(
    prior: StructureRun,
    mapping_calls: list[dict],
    *,
    mapping_provider: str,
) -> tuple[float, dict[str, Any]]:
    """Estimate structure-analysis plus mapping spend by actual provider."""
    structure_cost, structure_calls = _structure_cost(prior)
    if mapping_provider == "openai":
        mapping_cost = estimate_openai_cost(mapping_calls)
        mapping_rates = {
            "input": OPENAI_INPUT_USD_PER_MTOK,
            "cached_input": OPENAI_CACHED_INPUT_USD_PER_MTOK,
            "output": OPENAI_OUTPUT_USD_PER_MTOK,
        }
    elif mapping_provider == "fireworks":
        mapping_cost = estimate_fireworks_cost(mapping_calls)
        mapping_rates = {
            "input": FIREWORKS_INPUT_USD_PER_MTOK,
            "cached_input": FIREWORKS_CACHED_INPUT_USD_PER_MTOK,
            "output": FIREWORKS_OUTPUT_USD_PER_MTOK,
        }
    else:
        mapping_cost = estimate_gemini_mapping_cost(mapping_calls)
        mapping_rates = {
            "input": GEMINI_MAPPING_INPUT_USD_PER_MTOK,
            "cached_input": GEMINI_MAPPING_CACHED_INPUT_USD_PER_MTOK,
            "output": GEMINI_MAPPING_OUTPUT_USD_PER_MTOK,
        }
    return round(structure_cost + mapping_cost, 6), {
        "scope": "full_workflow_estimate",
        "structure": {
            "cost_usd": structure_cost,
            "providers": structure_calls,
        },
        "mapping": {
            "provider": mapping_provider,
            "cost_usd": round(mapping_cost, 6),
            "rates_usd_per_million_tokens": mapping_rates,
        },
    }


def analyze_workbook_structure(
    workbook: Path,
    *,
    output_dir: Path,
    requested_period: str = "YTD Actual",
    progress: Callable[[str], None] | None = None,
    discovery_run: StructureRun | Path | None = None,
    selected_period_ids: list[str] | None = None,
    parsed: SharedWorkbook | None = None,
) -> StructureRun:
    """Run only the three stages the workbook mapper actually consumes.

    Sheet routing, department ID and period selection produce the artifacts it
    reads. The department mapping that normally follows is discarded by this path,
    and it is 79% of the pipeline's model calls -- measured across the 8 reviewed
    properties, 162 of 206 calls and ~203s per workbook.
    """
    from hotel_pl_normalizer.structure import WorkbookStructureAnalyzer

    if progress:
        progress("Reading the workbook and locating departments")
    if isinstance(discovery_run, Path):
        discovery_run = StructureRun.model_validate_json(
            discovery_run.read_text(encoding="utf-8")
        )
    analyzer = WorkbookStructureAnalyzer()
    return analyzer.run(
        workbook,
        requested_period=requested_period,
        output_dir=output_dir,
        discovery_run=discovery_run,
        selected_period_ids=selected_period_ids,
        # Borrowed, not taken: the mapping stage needs the same record next.
        workbook_record=parsed.require() if parsed is not None else None,
    )


def discover_workbook_periods(
    workbook: Path,
    *,
    output_dir: Path,
    requested_period: str = "YTD Actual",
    progress: Callable[[str], None] | None = None,
) -> StructureRun:
    """Run only the fast, sheet-level period discovery used before the pause."""
    from hotel_pl_normalizer.structure import WorkbookStructureAnalyzer

    if progress:
        progress("Finding available periods")
    return WorkbookStructureAnalyzer().discover_periods(
        workbook,
        requested_period=requested_period,
        output_dir=output_dir,
    )


def normalize_workbook(
    workbook: Path,
    *,
    output_dir: Path,
    prior_run: StructureRun | Path | None = None,
    selected_period_id: str | None = None,
    selected_period_ids: list[str] | None = None,
    model: str | None = None,
    mapping_provider: str | None = None,
    requested_period: str = "YTD Actual",
    source_name: str | None = None,
    limiter: "RunLimiter | None" = None,
    progress: Callable[[str], None] | None = None,
    # Separate from `progress` on purpose: `progress` names the current stage and
    # replaces the last one, this appends to a running feed. Collapsing them
    # would mean either losing the history or rewriting the stage 100 times.
    on_activity: Callable[[str], None] | None = None,
    parsed: SharedWorkbook | None = None,
) -> NormalizationResult:
    """Map one workbook to the Standard COA.

    `prior_run` lets a caller supply upstream artifacts it already has, which skips
    several minutes and several model calls. Leave it None and they are computed.

    `source_name` is what the output should call the input. A server that stores
    uploads under its own filenames must pass it, or the client's result is headed
    with a path only the server understands.
    """
    started = time.perf_counter()
    reused_structure = prior_run is not None
    workbook = Path(workbook)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    if prior_run is None:
        prior = analyze_workbook_structure(
            workbook,
            output_dir=output_dir / "upstream",
            requested_period=requested_period,
            progress=progress,
            parsed=parsed,
        )
    elif isinstance(prior_run, Path):
        prior = StructureRun.model_validate_json(
            prior_run.read_text(encoding="utf-8")
        )
    else:
        prior = prior_run

    if parsed is not None:
        # Taken, not borrowed: the caller's reference is dropped here so the `del`
        # below is the last one and the record can actually be freed.
        record = parsed.require()
        parsed.release()
    else:
        record = read_excel_workbook(workbook, source_id=f"primary:{workbook.stem}")
    location_map = DepartmentLocationMap.model_validate(
        _artifact(prior, "department_id", "location_map")
    )
    requested_ids = list(selected_period_ids or [])
    if selected_period_id and not requested_ids:
        requested_ids = [selected_period_id]
    requested_ids, dropped_periods = _require_usable_structure(prior, requested_ids)
    for period_id, reason in dropped_periods.items():
        message = f"Period {period_id} was not mapped: {reason}"
        if progress:
            progress(message)
        if on_activity:
            on_activity(message)
    if requested_ids:
        period_maps, period_labels = _selected_periods(prior, requested_ids)
    else:
        period_maps = {
            "selected": PeriodColumnSelectionMap.model_validate(
                _artifact(prior, "period_selection", "selection")
            )
        }
        period_labels = {"selected": prior.requested_period}
    primary_period_id = next(iter(period_maps))
    period_label = period_labels[primary_period_id]
    sheet_selection = SheetNameSelectionResult.model_validate(
        _artifact(prior, "sheet_routing", "selection")
    )
    skipped = set(sheet_selection.skipped_sheet_names)
    included = {s.sheet_name for s in record.sheets if s.sheet_name not in skipped}

    if progress:
        progress("Reading every relevant row")
    evidence = compact_workbook_evidence(
        record, period_maps, location_map, include_sheets=included
    )
    # Everything downstream needs is now in `evidence` (a few hundred small dicts)
    # and the workbook id. The record itself is the expensive object -- 580 MB on
    # a 1.4 MB workbook -- and holding it through a mapping session that runs for
    # minutes is memory nothing is using. Release it before the session starts.
    workbook_id = record.workbook_id
    del record
    gc.collect()

    if progress:
        progress("Mapping the workbook")
    selected_provider = (
        mapping_provider
        or os.environ.get(MAPPING_PROVIDER_ENV_VAR, DEFAULT_MAPPING_PROVIDER)
    ).strip().lower()
    client = _mapping_client(selected_provider, model)
    if limiter is not None:
        limiter.watch(client)
    mapping = map_workbook(
        workbook_id=workbook_id,
        requested_period=period_label,
        locations=location_map,
        periods=period_maps,
        period_labels=period_labels,
        evidence=evidence,
        skipped_sheets=sorted(skipped),
        client=client,
        cancel=limiter,
        on_activity=on_activity,
    )
    cost_usd, cost_details = estimate_workflow_cost(
        prior,
        mapping.model_calls,
        mapping_provider=selected_provider,
    )

    duration_ms = round((time.perf_counter() - started) * 1000)
    if reused_structure and prior.telemetry is not None:
        duration_ms += prior.telemetry.metrics.duration_ms

    return NormalizationResult(
        workbook_id=workbook_id,
        source_name=source_name or workbook.name,
        period_label=period_label,
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
        duration_ms=duration_ms,
        session_calls=mapping.session_calls,
        session_call_ms=mapping.session_call_ms,
        session_tool_calls=mapping.session_tool_calls,
        session_exhausted=mapping.session_exhausted,
        stopped_reason=mapping.stopped_reason,
        cost_usd=cost_usd,
        mapping_provider=(
            "google" if selected_provider == "gemini" else selected_provider
        ),
        mapping_model=client.model_name,
        cost_details=cost_details,
        evidence=list(evidence),
        model_calls=mapping.model_calls,
        tool_trace=mapping.tool_trace,
        mapping_selection=mapping.mapping_selection,
        structure_stages=[
            {
                "stage_name": stage.stage_name,
                "duration_ms": getattr(stage, "duration_ms", None),
                "status": str(getattr(getattr(stage, "status", None), "value", "")) or None,
            }
            for stage in prior.stages
        ],
    )


def _require_usable_structure(
    prior: StructureRun, period_ids: list[str]
) -> tuple[list[str], dict[str, str]]:
    """Return the periods worth mapping, and why the others were dropped.

    This used to abort the whole run if any selected period failed validation.
    On Lotte Seattle that cost a complete, correct Actual because the Budget
    column beside it was empty: one unusable period took a good one down with
    it, and the caller got an error instead of the half that worked.

    Structural failures outside the period stages still stop everything -- a
    broken department map means nothing can be mapped. But a period that binding
    refuses is a fact about that period, so it is dropped and named while the
    rest proceed. Only an empty result is an error.
    """
    failed_stages = [stage for stage in prior.stages if stage.status.value == "fail"]
    period_stages = {"period_discovery", "period_selection"}
    if any(stage.stage_name not in period_stages for stage in failed_stages):
        raise RuntimeError(
            "Upstream structure analysis failed; mapping was not started."
        )
    if not failed_stages:
        return list(period_ids), {}
    if not period_ids:
        raise RuntimeError(
            "Upstream period analysis failed; select a validated period before mapping."
        )
    if len(period_ids) == 1:
        # Nothing to salvage: the one period asked for is the whole request.
        validated = _validate_period_subset(prior, period_ids)
        if validated.validation.status.value == "fail":
            raise RuntimeError(
                "One or more selected periods failed validation; mapping was not "
                f"started.{_failure_detail(validated)}"
            )
        return list(period_ids), {}

    usable: list[str] = []
    dropped: dict[str, str] = {}
    for period_id in period_ids:
        # Validated one at a time: the question is whether *this* period can be
        # bound, and validating the set together answers a different one.
        validated = _validate_period_subset(prior, [period_id])
        if validated.validation.status.value == "fail":
            dropped[period_id] = _failure_detail(validated).strip() or "failed validation"
        else:
            usable.append(period_id)

    if not usable:
        joined = "; ".join(f"{pid}: {why}" for pid, why in dropped.items())
        raise RuntimeError(
            f"No selected period passed validation; mapping was not started. {joined}"
        )
    return usable, dropped


def _failure_detail(catalog: PeriodCatalog) -> str:
    """The distinct error messages behind a failed validation.

    Deduplicated because the same rejection is raised once per location: SFI
    produced one sentence twelve times, which buried the fact that it was one
    problem rather than twelve.
    """
    seen: list[str] = []
    for issue in catalog.validation.issues:
        if issue.severity.value == "error" and issue.message not in seen:
            seen.append(issue.message)
    return f" Details: {'; '.join(seen)}" if seen else ""


def _validate_period_subset(
    prior: StructureRun, period_ids: list[str], *, stage_name: str = "period_selection"
) -> PeriodCatalog:
    """Validate only the options a person intends to map."""
    packet = PeriodColumnPacket.model_validate(
        _artifact(prior, stage_name, "packet")
    )
    catalog = PeriodCatalog.model_validate(
        _artifact(prior, stage_name, "catalog")
    )
    selected = [item for item in catalog.options if item.period_id in set(period_ids)]
    if len(selected) != len(set(period_ids)):
        raise RuntimeError("One or more selected periods are missing from the catalog.")
    selected_catalog = catalog.model_copy(
        update={
            "options": selected,
            "recommended_period_id": selected[0].period_id,
        }
    )
    return validate_period_catalog(packet, selected_catalog)


def validated_period_ids(prior: StructureRun) -> set[str]:
    """Return only catalog options that can safely proceed to mapping."""
    stage_name = (
        "period_selection"
        if any(item.stage_name == "period_selection" for item in prior.stages)
        else "period_discovery"
    )
    catalog = PeriodCatalog.model_validate(
        _artifact(prior, stage_name, "catalog")
    )
    stage = next(
        item for item in prior.stages if item.stage_name == stage_name
    )
    if stage.status.value != "fail":
        return {option.period_id for option in catalog.options}
    return {
        option.period_id
        for option in catalog.options
        if _validate_period_subset(
            prior, [option.period_id], stage_name=stage_name
        ).validation.status.value
        != "fail"
    }
