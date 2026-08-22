"""The three structural stages required by the complete-workbook mapper."""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from hotel_pl_normalizer.models.common import Severity, ValidationStatus
from hotel_pl_normalizer.models.department_location import (
    DepartmentIdentificationPacket,
)
from hotel_pl_normalizer.models.period_selection import (
    PeriodCatalog,
)
from hotel_pl_normalizer.models.run import (
    PipelineStatus,
    StageRun,
    StageStatus,
    StructureRun,
)
from hotel_pl_normalizer.models.sheet_selection import SheetNameSelectionResult
from hotel_pl_normalizer.models.workbook import WorkbookRecord
from hotel_pl_normalizer.providers.fireworks import (
    DEFAULT_FIREWORKS_MODEL,
    FireworksJsonClient,
)
from hotel_pl_normalizer.providers.gemini import (
    GeminiDepartmentIdBackend,
    GeminiJsonClient,
    GeminiSheetNameTriageBackend,
)
from hotel_pl_normalizer.providers.openai_api import OpenAIJsonClient
from hotel_pl_normalizer.providers.period_catalog import PeriodCatalogBackend
from hotel_pl_normalizer.providers.workbook_tools import WorkbookToolset
from hotel_pl_normalizer.structure.binding import (
    bind_departments,
    binding_to_location_map,
    binding_to_selection_maps,
)
from hotel_pl_normalizer.structure.departments import DepartmentIdAgent
from hotel_pl_normalizer.structure.exploration.adapters import (
    exploration_to_period_catalog,
    exploration_to_sheet_selection,
)
from hotel_pl_normalizer.structure.exploration.agent import explore_workbook
from hotel_pl_normalizer.structure.ingestion import read_excel_workbook
from hotel_pl_normalizer.structure.periods import (
    LocalPeriodCatalogBackend,
    align_unambiguous_period_bindings,
    build_period_column_packet,
    build_workbook_period_discovery_packet,
    catalog_to_selection_map,
    merge_period_catalog_repair,
    merge_selected_period_binding_repair,
    normalize_obvious_supporting_assessments,
    prepare_selected_period_catalog,
    render_period_catalog_prompt,
    render_period_catalog_repair_prompt,
    render_selected_period_binding_prompt,
    render_selected_period_binding_repair_prompt,
    validate_discovery_period_catalog,
    validate_period_catalog,
    validate_selected_period_catalog,
)
from hotel_pl_normalizer.structure.representation import build_compact_sheet_packets
from hotel_pl_normalizer.structure.routing import (
    SheetNameTriageAgent,
    build_sheet_name_triage_packet,
)

from .telemetry import build_run_telemetry

DEPARTMENT_ID_TOOLS_ENV_VAR = "FRESH_START_DEPARTMENT_ID_TOOLS"
REPAIR_TOOLS_ENV_VAR = "FRESH_START_REPAIR_TOOLS"
PERIOD_PROVIDER_ENV_VAR = "FRESH_START_PERIOD_PROVIDER"
PERIOD_MODEL_ENV_VAR = "FRESH_START_PERIOD_MODEL"
PERIOD_REASONING_EFFORT_ENV_VAR = "FRESH_START_PERIOD_REASONING_EFFORT"
DEFAULT_STRUCTURE_REASONING_EFFORT = "medium"
# Exploration answers period discovery and sheet routing in one tool-calling
# session that reads the workbook itself. Set this to fall back to the two
# packet-driven stages without a code change.
LEGACY_DISCOVERY_ENV_VAR = "FRESH_START_LEGACY_DISCOVERY"

# One session that locates every department and binds every chosen period,
# replacing the department-ID and period-selection stages. Opt-in: the packet
# path is what has run in production, so it stays the default until this has
# been through an end-to-end corpus.
DEPARTMENT_BINDING_ENV_VAR = "FRESH_START_DEPARTMENT_BINDING"
# Gemini 3.5 Flash Lite by default, in line with every other structure stage and
# with what the bench measured: held to the read and coverage requirements it
# matched the verified baseline 16/16 and agreed with DeepSeek on 819 of 821
# columns, at roughly a tenth of the wall clock.
BINDING_PROVIDER_ENV_VAR = "FRESH_START_BINDING_PROVIDER"
BINDING_MODEL_ENV_VAR = "FRESH_START_BINDING_MODEL"
BINDING_REASONING_EFFORT_ENV_VAR = "FRESH_START_BINDING_REASONING_EFFORT"


@dataclass(slots=True)
class StructureBackends:
    sheet_name: Any
    department_id: Any
    period: Any
    # A raw client rather than a backend: the binding stage drives its own tool
    # session, so there is nothing for a backend wrapper to add. None means the
    # stage is unavailable and the packet path runs whatever the flag says.
    binding: Any = None

    @classmethod
    def models(
        cls,
        *,
        gemini_model: str | None = None,
        department_repair_model: str | None = None,
        period_provider: str | None = None,
        period_model: str | None = None,
    ) -> "StructureBackends":
        selected_period_provider = (
            period_provider or os.environ.get(PERIOD_PROVIDER_ENV_VAR, "openai")
        ).strip().lower()
        selected_period_model = period_model or os.environ.get(PERIOD_MODEL_ENV_VAR)
        period_reasoning = os.environ.get(
            PERIOD_REASONING_EFFORT_ENV_VAR, DEFAULT_STRUCTURE_REASONING_EFFORT
        )
        if selected_period_provider == "gemini":
            period_client = GeminiJsonClient(
                model_name=selected_period_model or gemini_model
            )
        elif selected_period_provider == "fireworks":
            period_client = FireworksJsonClient(
                model_name=selected_period_model or DEFAULT_FIREWORKS_MODEL
            )
        elif selected_period_provider == "openai":
            period_client = OpenAIJsonClient(
                model_name=selected_period_model,
                reasoning_effort=period_reasoning,
                repair_reasoning_effort=period_reasoning,
            )
        else:
            raise ValueError(
                "period_provider must be 'openai', 'gemini', or 'fireworks'."
            )
        binding_provider = (
            os.environ.get(BINDING_PROVIDER_ENV_VAR, "openai").strip().lower()
        )
        binding_model = os.environ.get(BINDING_MODEL_ENV_VAR)
        binding_reasoning = os.environ.get(
            BINDING_REASONING_EFFORT_ENV_VAR, DEFAULT_STRUCTURE_REASONING_EFFORT
        )
        if binding_provider == "fireworks":
            binding_client = FireworksJsonClient(
                model_name=binding_model or DEFAULT_FIREWORKS_MODEL
            )
        elif binding_provider == "gemini":
            binding_client = GeminiJsonClient(model_name=binding_model or gemini_model)
        elif binding_provider == "openai":
            binding_client = OpenAIJsonClient(
                model_name=binding_model,
                reasoning_effort=binding_reasoning,
                repair_reasoning_effort=binding_reasoning,
            )
        else:
            raise ValueError(
                "binding_provider must be 'openai', 'gemini', or 'fireworks'."
            )
        return cls(
            sheet_name=GeminiSheetNameTriageBackend(model_name=gemini_model),
            department_id=GeminiDepartmentIdBackend(
                model_name=gemini_model,
                repair_model_name=department_repair_model or gemini_model,
            ),
            period=PeriodCatalogBackend(period_client),
            binding=binding_client,
        )

    @classmethod
    def local(cls) -> "StructureBackends":
        from hotel_pl_normalizer.structure.departments.agent import (
            LocalDepartmentIdBackend,
        )
        from hotel_pl_normalizer.structure.routing.sheet_name_agent import (
            LocalSheetNameTriageBackend,
        )

        return cls(
            sheet_name=LocalSheetNameTriageBackend(),
            department_id=LocalDepartmentIdBackend(),
            period=LocalPeriodCatalogBackend(),
            # Deterministic backends answer from fixtures and cannot make tool
            # calls, so the binding session has no client to run on and the
            # packet path stays.
            binding=None,
        )


def _uses_local_period_backend(backend: Any) -> bool:
    """Allow lightweight wrappers around the deterministic test backend."""
    seen: set[int] = set()
    while backend is not None and id(backend) not in seen:
        seen.add(id(backend))
        if isinstance(backend, LocalPeriodCatalogBackend):
            return True
        backend = getattr(backend, "delegate", None)
    return False


def _drop_discovery_periods_rejected_as_partial(
    catalog: PeriodCatalog,
) -> PeriodCatalog:
    """Last-resort cleanup when repair leaves a core-coverage failure unchanged."""
    rejected_ids = {
        issue.period_id
        for issue in catalog.validation.issues
        if issue.severity == Severity.ERROR
        and issue.period_id is not None
        and "not evidenced on every sampled primary_core sheet" in issue.message
    }
    if not rejected_ids:
        return catalog
    options = [
        option for option in catalog.options if option.period_id not in rejected_ids
    ]
    recommended = catalog.recommended_period_id
    if recommended in rejected_ids:
        recommended = options[0].period_id if options else None
    return catalog.model_copy(
        update={
            "options": options,
            "recommended_period_id": recommended,
            "notes": [
                *catalog.notes,
                "Removed periods still rejected as partial after targeted repair.",
            ],
        }
    )


class WorkbookStructureAnalyzer:
    """Produce sheet, department, and period hints—nothing downstream."""

    def __init__(
        self,
        backends: StructureBackends | None = None,
        *,
        department_repair_passes: int = 2,
        period_repair_passes: int = 2,
        department_id_tools: bool | None = None,
        repair_tools: bool | None = None,
    ) -> None:
        self.backends = backends or StructureBackends.models()
        self.department_repair_passes = max(0, department_repair_passes)
        self.period_repair_passes = max(0, period_repair_passes)
        self.department_id_tools = (
            _env_flag(DEPARTMENT_ID_TOOLS_ENV_VAR)
            if department_id_tools is None
            else department_id_tools
        )
        self.repair_tools = (
            _env_flag(REPAIR_TOOLS_ENV_VAR)
            if repair_tools is None
            else repair_tools
        )
        self._usage_offsets: dict[int, int] = {}

    def discover_periods(
        self,
        workbook_path: Path,
        *,
        requested_period: str = "YTD Actual",
        output_dir: Path,
        workbook_record: WorkbookRecord | None = None,
    ) -> StructureRun:
        """Discover periods, optionally borrowing an already parsed workbook."""
        started_at = _utc_now()
        started = time.perf_counter()
        workbook_path = Path(workbook_path)
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        stages: list[StageRun] = []

        stage_started_at, stage_started = _stage_clock()
        if workbook_record is None:
            workbook = read_excel_workbook(
                workbook_path, source_id=f"pipeline_{workbook_path.stem}"
            )
        else:
            workbook = workbook_record
        stages.append(
            StageRun(
                stage_name="ingestion",
                status=StageStatus.PASS,
                started_at=stage_started_at,
                completed_at=_utc_now(),
                duration_ms=_elapsed_ms(stage_started),
            )
        )

        stage_started_at, stage_started = _stage_clock()
        if _can_explore(self.backends.period) and not _env_flag(
            LEGACY_DISCOVERY_ENV_VAR
        ):
            stages.append(
                self._explore(workbook_path, workbook, output_dir,
                              stage_started_at, stage_started)
            )
            return self._finish(
                output_dir=output_dir,
                run_id=f"discovery:{_safe_stem(workbook_path)}",
                workbook_id=workbook.workbook_id,
                source_filename=workbook.source.original_filename,
                property_code=(
                    workbook.workbook_metadata.detected_property_code
                    or workbook_path.stem.split()[0].upper()
                ),
                requested_period=requested_period,
                stages=stages,
                started_at=started_at,
                started=started,
            )

        packet = build_workbook_period_discovery_packet(
            workbook,
            requested_period=requested_period,
            include_candidates=_uses_local_period_backend(self.backends.period),
        )
        prompt = render_period_catalog_prompt(packet, representative=True)
        catalog = normalize_obvious_supporting_assessments(
            packet,
            self.backends.period.run(packet, prompt),
        )
        catalog = validate_discovery_period_catalog(packet, catalog)
        repairs = 0
        while (
            catalog.validation.status == ValidationStatus.FAIL
            and repairs < self.period_repair_passes
        ):
            prompt = render_period_catalog_repair_prompt(
                packet, catalog, representative=True
            )
            catalog = normalize_obvious_supporting_assessments(
                packet,
                merge_period_catalog_repair(
                    catalog,
                    self.backends.period.repair(packet, prompt),
                ),
            )
            catalog = validate_discovery_period_catalog(packet, catalog)
            repairs += 1
        if catalog.validation.status == ValidationStatus.FAIL:
            cleaned = _drop_discovery_periods_rejected_as_partial(catalog)
            if cleaned is not catalog:
                catalog = validate_discovery_period_catalog(packet, cleaned)
        stages.append(
            self._stage(
                output_dir,
                "period_discovery",
                _stage_status(catalog.validation.status),
                self.backends.period,
                {"packet": packet, "catalog": catalog, "prompt": prompt},
                stage_started_at,
                stage_started,
                repair_passes=repairs,
                messages=_issue_messages(catalog.validation.issues),
            )
        )
        return self._finish(
            output_dir=output_dir,
            run_id=f"discovery:{_safe_stem(workbook_path)}",
            workbook_id=workbook.workbook_id,
            source_filename=workbook.source.original_filename,
            property_code=(
                workbook.workbook_metadata.detected_property_code
                or workbook_path.stem.split()[0].upper()
            ),
            requested_period=requested_period,
            stages=stages,
            started_at=started_at,
            started=started,
        )

    def run(
        self,
        workbook_path: Path,
        *,
        requested_period: str = "YTD Actual",
        output_dir: Path,
        discovery_run: StructureRun | None = None,
        selected_period_ids: list[str] | None = None,
        workbook_record: WorkbookRecord | None = None,
    ) -> StructureRun:
        """`workbook_record` lets a caller that already parsed this file pass it in.

        The mapping stage needs the same record immediately afterwards, and parsing
        it twice costs a full read -- 26s on CMI -- for no gain.
        """
        started_at = _utc_now()
        started = time.perf_counter()
        workbook_path = Path(workbook_path)
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        run_id = f"run:{_safe_stem(workbook_path)}:{_safe_token(requested_period)}"
        stages: list[StageRun] = []
        if discovery_run is not None:
            stages.extend(
                stage.model_copy()
                for stage in discovery_run.stages
                if stage.stage_name == "period_discovery"
            )

        stage_started_at, stage_started = _stage_clock()
        if workbook_record is None:
            workbook = read_excel_workbook(
                workbook_path,
                source_id=f"pipeline_{workbook_path.stem}",
            )
        else:
            workbook = workbook_record
        if discovery_run is not None and discovery_run.workbook_id != workbook.workbook_id:
            raise ValueError("Period discovery belongs to a different workbook.")
        stages.append(
            StageRun(
                stage_name="ingestion",
                status=StageStatus.PASS,
                started_at=stage_started_at,
                completed_at=_utc_now(),
                duration_ms=_elapsed_ms(stage_started),
            )
        )

        stage_started_at, stage_started = _stage_clock()
        explored_routing = _explored_routing(discovery_run)
        if explored_routing is not None:
            # Exploration already decided every sheet, from the sheet contents
            # rather than the names. Re-running triage here would ask a second
            # model the same question with less evidence, and let the two
            # disagree about the same workbook.
            stages.append(
                StageRun(
                    stage_name="sheet_routing",
                    status=StageStatus.PASS,
                    started_at=stage_started_at,
                    completed_at=_utc_now(),
                    duration_ms=_elapsed_ms(stage_started),
                    artifact_paths=_write_artifacts(
                        output_dir / "stages" / "sheet_routing",
                        {"selection": explored_routing},
                    ),
                    messages=["Routing reused from workbook exploration."],
                )
            )
            sheet_output = None
        else:
            sheet_packet = build_sheet_name_triage_packet(
                workbook,
                enrich_sheet_names={sheet.sheet_name for sheet in workbook.sheets},
            )
            sheet_agent = SheetNameTriageAgent(self.backends.sheet_name)
            sheet_output = sheet_agent.run(sheet_packet)
            stages.append(
                self._stage(
                    output_dir,
                    "sheet_routing",
                    _stage_status(sheet_output.validation.status),
                    self.backends.sheet_name,
                    {
                        "packet": sheet_packet,
                        "selection": sheet_output.selection,
                        "validation": sheet_output.validation,
                        "prompt": sheet_output.prompt,
                    },
                    stage_started_at,
                    stage_started,
                    messages=_issue_messages(sheet_output.validation.issues),
                )
            )

        routing = (
            explored_routing if explored_routing is not None else sheet_output.selection
        )
        selected = routing.selected_sheet_names + routing.unsure_sheet_names

        if self._can_bind(discovery_run, selected_period_ids):
            stage_started_at, stage_started = _stage_clock()
            stages.append(
                self._bind(
                    workbook,
                    routing,
                    discovery_run,
                    selected_period_ids or [],
                    output_dir,
                    stage_started_at,
                    stage_started,
                )
            )
            return self._finish(
                output_dir=output_dir,
                run_id=run_id,
                workbook_id=workbook.workbook_id,
                source_filename=workbook.source.original_filename,
                property_code=(
                    workbook.workbook_metadata.detected_property_code
                    or workbook_path.stem.split()[0].upper()
                ),
                requested_period=requested_period,
                stages=stages,
                started_at=started_at,
                started=started,
                duration_offset_ms=(
                    discovery_run.telemetry.metrics.duration_ms
                    if discovery_run is not None and discovery_run.telemetry is not None
                    else 0
                ),
            )

        compact_packets = build_compact_sheet_packets(
            workbook,
            sheet_names=selected,
        )
        department_packet = DepartmentIdentificationPacket(
            workbook_id=workbook.workbook_id,
            source_filename=workbook.source.original_filename,
            sheet_name_selection=routing,
            compact_packets=compact_packets,
        )
        stage_started_at, stage_started = _stage_clock()
        department_agent = DepartmentIdAgent(
            self.backends.department_id,
            workbook_sheet_names={sheet.sheet_name for sheet in workbook.sheets},
            toolset=(WorkbookToolset(workbook) if self.department_id_tools else None),
            repair_toolset=(WorkbookToolset(workbook) if self.repair_tools else None),
        )
        department_output = department_agent.run(department_packet)
        repair_count = 0
        while (
            department_output.validation.status != ValidationStatus.PASS
            and repair_count < self.department_repair_passes
        ):
            department_output = department_agent.repair(
                department_packet,
                department_output.location_map,
                department_output.validation,
            )
            repair_count += 1
            if _repair_should_stop(
                department_output.validation,
                department_output.repair_improved,
            ):
                break
        location_map = department_output.location_map
        department_artifacts = {
            "packet": department_packet,
            "location_map": location_map,
            "validation": department_output.validation,
            "prompt": department_output.prompt,
        }
        if department_agent.tool_trace:
            department_artifacts["tool_trace"] = department_agent.tool_trace
        stages.append(
            self._stage(
                output_dir,
                "department_id",
                _stage_status(department_output.validation.status),
                self.backends.department_id,
                department_artifacts,
                stage_started_at,
                stage_started,
                repair_passes=repair_count,
                messages=_issue_messages(department_output.validation.issues),
            )
        )

        if department_output.validation.status != ValidationStatus.FAIL:
            stage_started_at, stage_started = _stage_clock()
            period_packet = build_period_column_packet(
                workbook,
                location_map,
                requested_period=requested_period,
            )
            if discovery_run is None:
                period_prompt = render_period_catalog_prompt(period_packet)
                period_catalog = validate_period_catalog(
                    period_packet,
                    align_unambiguous_period_bindings(
                        period_packet,
                        self.backends.period.run(period_packet, period_prompt),
                    ),
                )
            else:
                if not selected_period_ids:
                    raise ValueError("At least one discovered period must be selected.")
                discovery_catalog = PeriodCatalog.model_validate(
                    _read_stage_artifact(discovery_run, "period_discovery", "catalog")
                )
                period_prompt = render_selected_period_binding_prompt(
                    period_packet, discovery_catalog, selected_period_ids
                )
                period_catalog = prepare_selected_period_catalog(
                    discovery_catalog,
                    self.backends.period.run(
                        period_packet, period_prompt
                    ),
                    selected_period_ids,
                )
                period_catalog = validate_selected_period_catalog(
                    period_packet, period_catalog, selected_period_ids
                )
            period_repair_count = 0
            while (
                period_catalog.validation.status == ValidationStatus.FAIL
                and period_repair_count < self.period_repair_passes
            ):
                if discovery_run is None:
                    period_prompt = render_period_catalog_repair_prompt(
                        period_packet, period_catalog
                    )
                else:
                    period_prompt = render_selected_period_binding_repair_prompt(
                        period_packet,
                        discovery_catalog,
                        period_catalog,
                    )
                if discovery_run is None:
                    period_catalog = validate_period_catalog(
                        period_packet,
                        align_unambiguous_period_bindings(
                            period_packet,
                            merge_period_catalog_repair(
                                period_catalog,
                                self.backends.period.repair(
                                    period_packet, period_prompt
                                ),
                            ),
                        ),
                    )
                else:
                    period_catalog = validate_selected_period_catalog(
                        period_packet,
                        merge_selected_period_binding_repair(
                            period_catalog,
                            self.backends.period.run(period_packet, period_prompt),
                        ),
                        selected_period_ids or [],
                    )
                period_repair_count += 1
            period_map = catalog_to_selection_map(
                period_packet,
                period_catalog,
                requested_period=requested_period,
            )
            period_status = period_catalog.validation.status
            if period_map.validation.status == ValidationStatus.FAIL:
                period_status = ValidationStatus.FAIL
            elif (
                period_status == ValidationStatus.PASS
                and period_map.validation.status == ValidationStatus.WARNING
            ):
                period_status = ValidationStatus.WARNING
            stages.append(
                self._stage(
                    output_dir,
                    "period_selection",
                    _stage_status(period_status),
                    self.backends.period,
                    {
                        "packet": period_packet,
                        "catalog": period_catalog,
                        "selection": period_map,
                        "prompt": period_prompt,
                    },
                    stage_started_at,
                    stage_started,
                    repair_passes=period_repair_count,
                    messages=_issue_messages(
                        period_catalog.validation.issues,
                        period_map.validation.issues,
                    ),
                )
            )

        return self._finish(
            output_dir=output_dir,
            run_id=run_id,
            workbook_id=workbook.workbook_id,
            source_filename=workbook.source.original_filename,
            property_code=(
                workbook.workbook_metadata.detected_property_code
                or workbook_path.stem.split()[0].upper()
            ),
            requested_period=requested_period,
            stages=stages,
            started_at=started_at,
            started=started,
            duration_offset_ms=(
                discovery_run.telemetry.metrics.duration_ms
                if discovery_run is not None and discovery_run.telemetry is not None
                else 0
            ),
        )

    def _can_bind(
        self, discovery_run: StructureRun | None, selected_period_ids: list[str] | None
    ) -> bool:
        """Is the combined stage both enabled and usable for this run?

        It needs a client that can hold a tool conversation, and it needs the
        periods a person chose -- it binds what was selected rather than
        discovering afresh, so a run with no prior discovery has nothing for it
        to bind and falls back to the packet path.
        """
        if not _env_flag(DEPARTMENT_BINDING_ENV_VAR, default=True):
            return False
        if discovery_run is None or not selected_period_ids:
            return False
        client = self.backends.binding
        return client is not None and callable(
            getattr(client, "generate_json_model_with_tools", None)
        )

    def _bind(
        self,
        workbook: WorkbookRecord,
        routing: SheetNameSelectionResult,
        discovery_run: StructureRun,
        selected_period_ids: list[str],
        output_dir: Path,
        stage_started_at: str,
        stage_started: float,
    ) -> StageRun:
        """One session in place of department ID and period selection.

        Written out under the `department_id` stage name, which is deliberate on
        two counts. `pipeline.py` reads `department_id/location_map` and does not
        have to learn a new name; and `_require_usable_structure` treats a
        failure outside the period stages as fatal, which is the right severity
        here because this stage cannot fail *for a period* -- an unbindable sheet
        is recorded as unavailable and the run continues. The only way it fails
        is finding no department at all, and then there is genuinely nothing to
        map.

        The per-period selection maps go beside it as `selections`, because the
        packet path's artifacts are rebuilt from a `PeriodColumnPacket` this
        stage does not produce and should not fake.
        """
        catalog = PeriodCatalog.model_validate(
            _read_stage_artifact(discovery_run, "period_discovery", "catalog")
        )
        labels = {
            option.period_id: option.label
            for option in catalog.options
            if option.period_id in set(selected_period_ids)
        }
        output = bind_departments(
            workbook,
            client=self.backends.binding,
            period_ids=selected_period_ids,
            period_labels=labels,
            financial_sheets=routing.selected_sheet_names + routing.unsure_sheet_names,
        )
        location_map = binding_to_location_map(
            output.structure,
            workbook_id=workbook.workbook_id,
            workbook_layout=routing.workbook_layout.value,
        )
        selections = binding_to_selection_maps(
            output.structure,
            workbook_id=workbook.workbook_id,
            period_ids=selected_period_ids,
            period_labels=labels,
        )
        artifacts: dict[str, Any] = {
            "location_map": location_map,
            "selections": {
                period_id: item.model_dump(mode="json")
                for period_id, item in selections.items()
            },
            "binding": output.structure,
            "prompt": output.prompt,
        }
        if output.tool_trace:
            artifacts["tool_trace"] = output.tool_trace
        # An empty map is a failure only when the stage was asked for one. On a
        # single-sheet P&L it does not locate departments at all, and empty is
        # the correct answer rather than a missing one -- the mapper is shown
        # every row of that sheet and groups them from the labels.
        located_nothing = output.located_departments and not location_map.locations
        return self._stage(
            output_dir,
            "department_id",
            StageStatus.FAIL if located_nothing else StageStatus.PASS,
            self.backends.binding,
            artifacts,
            stage_started_at,
            stage_started,
            messages=[
                *(
                    []
                    if output.located_departments
                    else [
                        "Single-sheet workbook: departments were not located, and "
                        "the mapper groups rows from their labels instead."
                    ]
                ),
                *output.observations,
                *(f"Rejected once: {item}" for item in output.rejections),
            ],
        )

    def _explore(
        self,
        workbook_path: Path,
        workbook: WorkbookRecord,
        output_dir: Path,
        stage_started_at: str,
        stage_started: float,
    ) -> StageRun:
        """One session that routes every sheet, then finds the periods.

        Written out under the `period_discovery` stage name so nothing
        downstream has to learn a new one: `run()` still reads
        `period_discovery/catalog`, and the routing it also produces is picked
        up in place of the sheet-name triage call.

        The workbook is parsed by the caller and passed in only for its id and
        filename. Exploration itself reads the file lazily through its own
        tools, which is how it sees header rows a fixed extract had already
        discarded -- the stage this replaces returned no periods at all on East
        Miami because the month headers sat one row below its window.
        """
        exploration = explore_workbook(
            workbook_path, client=self.backends.period.client
        )
        catalog = exploration_to_period_catalog(
            exploration.structure, workbook_id=workbook.workbook_id
        )
        routing = exploration_to_sheet_selection(
            exploration.structure, workbook_id=workbook.workbook_id
        )
        messages = list(exploration.rejections)
        if not catalog.options:
            messages.append("Exploration returned no periods for this workbook.")
        return self._stage(
            output_dir,
            "period_discovery",
            StageStatus.FAIL if not catalog.options else StageStatus.PASS,
            self.backends.period,
            {
                "catalog": catalog,
                "routing": routing,
                "exploration": exploration.structure,
                "prompt": exploration.prompt,
            },
            stage_started_at,
            stage_started,
            messages=messages,
        )

    def _stage(
        self,
        output_dir: Path,
        name: str,
        status: StageStatus,
        backend: Any,
        artifacts: dict[str, Any],
        started_at: str,
        started: float,
        *,
        repair_passes: int = 0,
        messages: list[str] | None = None,
    ) -> StageRun:
        return StageRun(
            stage_name=name,
            status=status,
            started_at=started_at,
            completed_at=_utc_now(),
            duration_ms=_elapsed_ms(started),
            repair_passes=repair_passes,
            usage=self._usage(backend),
            artifact_paths=_write_artifacts(
                output_dir / "stages" / name,
                artifacts,
            ),
            messages=messages or [],
        )

    def _usage(self, backend: Any) -> list[dict[str, Any]]:
        # Most stages hold a backend wrapping a client; the binding stage holds
        # the client itself, because it drives its own tool session and there is
        # nothing for a wrapper to add. Look in both places so cost is recorded
        # either way -- a stage reporting zero tokens quietly raises the spend
        # ceiling for every call after it.
        source = getattr(backend, "client", None) or backend
        history = list(getattr(source, "usage_history", []))
        key = id(backend)
        start = self._usage_offsets.get(key, 0)
        self._usage_offsets[key] = len(history)
        return history[start:]

    @staticmethod
    def _finish(
        *,
        output_dir: Path,
        run_id: str,
        workbook_id: str,
        source_filename: str,
        property_code: str | None,
        requested_period: str,
        stages: list[StageRun],
        started_at: str,
        started: float,
        duration_offset_ms: int = 0,
    ) -> StructureRun:
        completed_at = _utc_now()
        duration_ms = _elapsed_ms(started) + duration_offset_ms
        telemetry = build_run_telemetry(
            run_id=run_id,
            started_at=started_at,
            completed_at=completed_at,
            duration_ms=duration_ms,
            stages=stages,
        )
        root_paths = {
            "telemetry": str((output_dir / "telemetry.json").resolve()),
            "run": str((output_dir / "run.json").resolve()),
        }
        run = StructureRun(
            run_id=run_id,
            workbook_id=workbook_id,
            source_filename=source_filename,
            property_code=property_code,
            requested_period=requested_period,
            status=_run_status(stages),
            stages=stages,
            telemetry=telemetry,
            artifact_paths=root_paths,
        )
        _write_json(Path(root_paths["telemetry"]), telemetry)
        _write_json(Path(root_paths["run"]), run)
        return run




def _can_explore(backend: Any) -> bool:
    """Does this period backend have a client that can run a tool session?

    Exploration is a tool-calling conversation, so it needs a real client. The
    deterministic backends used by tests and `--local` have no client at all and
    answer from fixtures, and asking them would raise rather than degrade. Those
    fall back to the packet-driven discovery stage, which is what they were
    written against.
    """
    client = getattr(backend, "client", None)
    return client is not None and callable(
        getattr(client, "generate_json_model_with_tools", None)
    )


def _explored_routing(discovery_run: StructureRun | None):
    """The routing exploration produced, if this discovery run came from it.

    Returns None for a legacy discovery run so the sheet-name triage stage still
    runs -- a prior run recorded before exploration existed has no routing to
    reuse, and re-deriving it is exactly right in that case.
    """
    if discovery_run is None:
        return None
    try:
        return SheetNameSelectionResult.model_validate(
            _read_stage_artifact(discovery_run, "period_discovery", "routing")
        )
    except (LookupError, KeyError, ValueError):
        return None


def _env_flag(name: str, *, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _repair_should_stop(validation: Any, improved: bool | None) -> bool:
    if improved is False:
        return True
    return bool(
        improved
        and not any(issue.severity == Severity.ERROR for issue in validation.issues)
    )



def _issue_messages(*issue_groups) -> list[str]:
    """Validation issues as log lines, carrying where each one happened.

    `PeriodCatalogIssue` records `location_id` and `period_id`, and both were
    dropped on the way to the run log. That is what made one corpus failure
    undiagnosable: the log said "Column S contains the total sum of
    percentage/ratio-scale values" with no way to tell which of eleven sheets
    raised it, and the sheets that were easy to check turned out to be fine.

    Repeats collapse too. The same rejection is raised once per location, so a
    twelve-department workbook logged one sentence twelve times; the locations
    are gathered onto a single line instead.
    """
    ordered: list[str] = []
    locations: dict[str, list[str]] = {}
    for issues in issue_groups:
        for issue in issues:
            text = issue.message
            if text not in locations:
                locations[text] = []
                ordered.append(text)
            # Not every issue type carries these: department-map issues have
            # neither, and asking for them by attribute raised on a real run.
            where = getattr(issue, "location_id", None) or getattr(
                issue, "period_id", None
            )
            if where and where not in locations[text]:
                locations[text].append(where)

    lines = []
    for text in ordered:
        where = locations[text]
        if not where:
            lines.append(text)
            continue
        shown = ", ".join(where[:6])
        if len(where) > 6:
            shown += f", and {len(where) - 6} more"
        lines.append(f"{text} [at: {shown}]")
    return lines


def _stage_status(status: ValidationStatus) -> StageStatus:
    return {
        ValidationStatus.PASS: StageStatus.PASS,
        ValidationStatus.WARNING: StageStatus.WARNING,
        ValidationStatus.FAIL: StageStatus.FAIL,
        ValidationStatus.SKIPPED: StageStatus.SKIPPED,
    }[status]


def _run_status(stages: list[StageRun]) -> PipelineStatus:
    if any(stage.status == StageStatus.FAIL for stage in stages):
        return PipelineStatus.FAIL
    if any(stage.status in {StageStatus.WARNING, StageStatus.SKIPPED} for stage in stages):
        return PipelineStatus.WARNING
    return PipelineStatus.PASS


def _write_artifacts(directory: Path, artifacts: dict[str, Any]) -> dict[str, str]:
    directory.mkdir(parents=True, exist_ok=True)
    paths = {}
    for name, artifact in artifacts.items():
        path = directory / f"{name}{'.md' if name.endswith('prompt') else '.json'}"
        if path.suffix == ".md":
            path.write_text(str(artifact), encoding="utf-8")
        else:
            _write_json(path, artifact)
        paths[name] = str(path.resolve())
    return paths


def _write_json(path: Path, value: Any) -> None:
    if isinstance(value, BaseModel):
        path.write_text(value.model_dump_json(indent=2), encoding="utf-8")
        return
    if isinstance(value, list):
        value = [
            item.model_dump(mode="json") if isinstance(item, BaseModel) else item
            for item in value
        ]
    path.write_text(json.dumps(value, indent=2), encoding="utf-8")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _stage_clock() -> tuple[str, float]:
    return _utc_now(), time.perf_counter()


def _elapsed_ms(started: float) -> int:
    return round((time.perf_counter() - started) * 1000)


def _safe_stem(path: Path) -> str:
    return _safe_token(path.stem)


def _safe_token(value: str) -> str:
    return "".join(
        character if character.isalnum() else "_" for character in value
    ).strip("_").lower()


def _read_stage_artifact(run: StructureRun, stage_name: str, key: str) -> dict:
    stage = next(item for item in run.stages if item.stage_name == stage_name)
    return json.loads(Path(stage.artifact_paths[key]).read_text(encoding="utf-8"))
