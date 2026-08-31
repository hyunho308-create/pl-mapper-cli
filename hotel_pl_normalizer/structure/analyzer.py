"""The structure workflow used by the standalone normalizer."""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from hotel_pl_normalizer.models.period_selection import PeriodCatalog
from hotel_pl_normalizer.models.run import (
    PipelineStatus,
    StageRun,
    StageStatus,
    StructureRun,
)
from hotel_pl_normalizer.models.sheet_selection import SheetNameSelectionResult
from hotel_pl_normalizer.models.workbook import WorkbookRecord
from hotel_pl_normalizer.providers import ModelClient, create_model_client
from hotel_pl_normalizer.structure.binding import (
    bind_periods,
    binding_to_selection_maps,
)
from hotel_pl_normalizer.structure.exploration.adapters import (
    exploration_to_period_catalog,
    exploration_to_sheet_selection,
)
from hotel_pl_normalizer.structure.exploration.agent import explore_workbook
from hotel_pl_normalizer.structure.ingestion import read_excel_workbook

from .telemetry import build_run_telemetry


class WorkbookStructureAnalyzer:
    """Discover periods, route sheets, and bind selected source columns."""

    def __init__(self, client: ModelClient | None = None) -> None:
        self.client = client or create_model_client()
        self._usage_offset = 0

    def discover_periods(
        self,
        workbook_path: Path,
        *,
        output_dir: Path,
        workbook_record: WorkbookRecord | None = None,
    ) -> StructureRun:
        """Discover selectable periods and financial sheets in one session."""
        started_at = _utc_now()
        started = time.perf_counter()
        workbook_path = Path(workbook_path)
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)

        stage_started_at, stage_started = _stage_clock()
        workbook = workbook_record or read_excel_workbook(workbook_path)
        stages = [
            StageRun(
                stage_name="ingestion",
                status=StageStatus.PASS,
                started_at=stage_started_at,
                completed_at=_utc_now(),
                duration_ms=_elapsed_ms(stage_started),
            )
        ]

        stage_started_at, stage_started = _stage_clock()
        stages.append(
            self._explore(
                workbook_path,
                workbook,
                output_dir,
                stage_started_at,
                stage_started,
            )
        )
        return self._finish(
            output_dir=output_dir,
            run_id=f"discovery:{_safe_stem(workbook_path)}",
            workbook=workbook,
            workbook_path=workbook_path,
            stages=stages,
            started_at=started_at,
            started=started,
        )

    def run(
        self,
        workbook_path: Path,
        *,
        output_dir: Path,
        discovery_run: StructureRun,
        selected_period_ids: list[str],
        workbook_record: WorkbookRecord,
    ) -> StructureRun:
        """Bind selected periods to columns using discovery's routing."""
        if not selected_period_ids:
            raise ValueError("At least one discovered period must be selected.")
        if discovery_run.workbook_id != workbook_record.workbook_id:
            raise ValueError("Period discovery belongs to a different workbook.")

        started_at = _utc_now()
        started = time.perf_counter()
        workbook_path = Path(workbook_path)
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        stages = [
            stage.model_copy()
            for stage in discovery_run.stages
            if stage.stage_name == "period_discovery"
        ]

        stage_started_at, stage_started = _stage_clock()
        stages.append(
            StageRun(
                stage_name="ingestion",
                status=StageStatus.PASS,
                started_at=stage_started_at,
                completed_at=_utc_now(),
                duration_ms=_elapsed_ms(stage_started),
            )
        )

        routing = SheetNameSelectionResult.model_validate(
            _read_stage_artifact(discovery_run, "period_discovery", "routing")
        )
        stage_started_at, stage_started = _stage_clock()
        stages.append(
            StageRun(
                stage_name="sheet_routing",
                status=StageStatus.PASS,
                started_at=stage_started_at,
                completed_at=_utc_now(),
                duration_ms=_elapsed_ms(stage_started),
                artifact_paths=_write_artifacts(
                    output_dir / "stages" / "sheet_routing",
                    {"selection": routing},
                ),
                messages=["Routing reused from workbook exploration."],
            )
        )

        stage_started_at, stage_started = _stage_clock()
        stages.append(
            self._bind(
                workbook_record,
                routing,
                discovery_run,
                selected_period_ids,
                output_dir,
                stage_started_at,
                stage_started,
            )
        )
        duration_offset = (
            discovery_run.telemetry.metrics.duration_ms
            if discovery_run.telemetry is not None
            else 0
        )
        return self._finish(
            output_dir=output_dir,
            run_id=f"run:{_safe_stem(workbook_path)}",
            workbook=workbook_record,
            workbook_path=workbook_path,
            stages=stages,
            started_at=started_at,
            started=started,
            duration_offset_ms=duration_offset,
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
        catalog = PeriodCatalog.model_validate(
            _read_stage_artifact(discovery_run, "period_discovery", "catalog")
        )
        selected = set(selected_period_ids)
        options_by_id = {option.period_id: option for option in catalog.options}
        selected_options = [
            options_by_id[period_id]
            for period_id in selected_period_ids
            if period_id in options_by_id
        ]
        labels = {option.period_id: option.label for option in selected_options}
        if set(labels) != selected:
            raise ValueError("One or more selected periods are absent from discovery.")

        output = bind_periods(
            workbook,
            client=self.client,
            periods=selected_options,
            financial_sheets=routing.included_sheet_names,
        )
        selections = binding_to_selection_maps(
            output.structure,
            workbook_id=workbook.workbook_id,
            period_ids=selected_period_ids,
            period_labels=labels,
        )
        artifacts: dict[str, Any] = {
            "selections": {
                period_id: item.model_dump(mode="json")
                for period_id, item in selections.items()
            },
            "binding": output.structure,
            "prompt": output.prompt,
        }
        if output.tool_trace:
            artifacts["tool_trace"] = output.tool_trace
        unresolved = [
            item
            for item in output.structure.unavailable
            if item.reason.startswith("Binding was not established")
        ]
        salvage_observations = [
            item
            for item in output.structure.observations
            if item.startswith(
                (
                    "Dropped bindings for ",
                    "Marked ",
                    "Removed during deterministic salvage: ",
                )
            )
        ]
        messages = [
            (
                "Binding required deterministic salvage; "
                + (
                    f"{len(unresolved)} unresolved sheet-period pair(s) were "
                    "excluded without inferring a default column."
                    if unresolved
                    else "invalid or ambiguous model claims were removed before "
                    "acceptance."
                )
                if salvage_observations
                else "Binding finished with an explicit column or unavailable "
                "outcome for every routed sheet-period pair."
            ),
            *(
                f"{period_id}: "
                f"{sum(item.period_id == period_id for item in output.structure.bindings)} "
                "sheet binding(s), "
                f"{sum(item.period_id == period_id for item in output.structure.unavailable)} "
                "sheet(s) unavailable."
                for period_id in selected_period_ids
            ),
            *output.observations,
            *(f"Rejected once: {item}" for item in output.rejections),
        ]
        return self._stage(
            output_dir,
            "period_binding",
            StageStatus.WARNING if salvage_observations else StageStatus.PASS,
            artifacts,
            stage_started_at,
            stage_started,
            messages=messages,
        )

    def _explore(
        self,
        workbook_path: Path,
        workbook: WorkbookRecord,
        output_dir: Path,
        stage_started_at: str,
        stage_started: float,
    ) -> StageRun:
        output = explore_workbook(workbook_path, client=self.client)
        catalog = exploration_to_period_catalog(
            output.structure, workbook_id=workbook.workbook_id
        )
        routing = exploration_to_sheet_selection(
            output.structure, workbook_id=workbook.workbook_id
        )
        messages = list(output.rejections)
        if not catalog.options:
            messages.append("Exploration returned no periods for this workbook.")
        return self._stage(
            output_dir,
            "period_discovery",
            StageStatus.FAIL if not catalog.options else StageStatus.PASS,
            {
                "catalog": catalog,
                "routing": routing,
                "exploration": output.structure,
                "prompt": output.prompt,
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
        artifacts: dict[str, Any],
        started_at: str,
        started: float,
        *,
        messages: list[str] | None = None,
    ) -> StageRun:
        history = list(self.client.usage_history)
        usage = history[self._usage_offset :]
        self._usage_offset = len(history)
        return StageRun(
            stage_name=name,
            status=status,
            started_at=started_at,
            completed_at=_utc_now(),
            duration_ms=_elapsed_ms(started),
            usage=usage,
            artifact_paths=_write_artifacts(
                output_dir / "stages" / name,
                artifacts,
            ),
            messages=messages or [],
        )

    @staticmethod
    def _finish(
        *,
        output_dir: Path,
        run_id: str,
        workbook: WorkbookRecord,
        workbook_path: Path,
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
            workbook_id=workbook.workbook_id,
            source_filename=workbook.source.original_filename,
            property_code=(
                workbook.workbook_metadata.detected_property_code
                or workbook_path.stem.split()[0].upper()
            ),
            requested_period="User selected",
            status=_run_status(stages),
            stages=stages,
            telemetry=telemetry,
            artifact_paths=root_paths,
        )
        _write_json(Path(root_paths["telemetry"]), telemetry)
        _write_json(Path(root_paths["run"]), run)
        return run


def _run_status(stages: list[StageRun]) -> PipelineStatus:
    if any(stage.status == StageStatus.FAIL for stage in stages):
        return PipelineStatus.FAIL
    if any(
        stage.status in {StageStatus.WARNING, StageStatus.SKIPPED}
        for stage in stages
    ):
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
    path.write_text(json.dumps(value, indent=2), encoding="utf-8")


def _read_stage_artifact(run: StructureRun, stage_name: str, key: str) -> dict:
    stage = next(item for item in run.stages if item.stage_name == stage_name)
    return json.loads(Path(stage.artifact_paths[key]).read_text(encoding="utf-8"))


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _stage_clock() -> tuple[str, float]:
    return _utc_now(), time.perf_counter()


def _elapsed_ms(started: float) -> int:
    return round((time.perf_counter() - started) * 1000)


def _safe_stem(path: Path) -> str:
    return "".join(
        character if character.isalnum() else "_" for character in path.stem
    ).strip("_").lower()
