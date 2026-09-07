from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

import hotel_pl_normalizer.cli as cli_module
import hotel_pl_normalizer.pipeline as pipeline_module
import hotel_pl_normalizer.run_log as run_log_module
from hotel_pl_normalizer.cli import _write_result_artifacts
from hotel_pl_normalizer.feedback import FeedbackCompositionError
from hotel_pl_normalizer.models.run import PipelineStatus, StageRun, StructureRun
from hotel_pl_normalizer.models.workbook import (
    CellRecord,
    FileType,
    WorkbookMetadata,
    WorkbookRecord,
    WorkbookRow,
    WorkbookSheet,
    WorkbookSource,
)
from hotel_pl_normalizer.pipeline import (
    NormalizationResult,
    SharedWorkbook,
    _require_meaningful_period_evidence,
    normalize_workbook,
)
from hotel_pl_normalizer.run_log import build_run_log


PERIOD_ID = "2025-01_2025-12_actual"
PERIOD_LABEL = "2025 Actual"


def _record(value: float | None = 100.0) -> WorkbookRecord:
    return WorkbookRecord(
        workbook_id="wb_remediation",
        source=WorkbookSource(
            source_id="primary:test",
            original_filename="test.xlsx",
            file_type=FileType.XLSX,
            file_hash="source-hash",
            ingested_at=datetime(2026, 9, 4, tzinfo=timezone.utc),
        ),
        workbook_metadata=WorkbookMetadata(sheet_count=1),
        sheets=[
            WorkbookSheet(
                sheet_id="sheet:summary",
                sheet_name="Summary",
                max_row=2,
                max_column=2,
                rows=[
                    WorkbookRow(
                        row_index=1,
                        cells=[
                            CellRecord(1, 1, "A1", "Account", "Account"),
                            CellRecord(1, 2, "B1", "2025 Actual", "2025 Actual"),
                        ],
                    ),
                    WorkbookRow(
                        row_index=2,
                        cells=[
                            CellRecord(2, 1, "A2", "Rooms Revenue", "Rooms Revenue"),
                            CellRecord(2, 2, "B2", value, None if value is None else str(value)),
                        ],
                    ),
                ],
            )
        ],
    )


def _prior_run(tmp_path: Path) -> StructureRun:
    routing_path = tmp_path / "routing.json"
    routing_path.write_text(
        json.dumps(
            {
                "workbook_id": "wb_remediation",
                "selections": [
                    {
                        "sheet_name": "Summary",
                        "include_as_financial_evidence": True,
                        "role": "summary_p_and_l",
                        "confidence": "high",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    bindings_path = tmp_path / "selections.json"
    bindings_path.write_text(
        json.dumps(
            {
                PERIOD_ID: {
                    "selection_map_id": "selection",
                    "workbook_id": "wb_remediation",
                    "requested_period": PERIOD_LABEL,
                    "sheet_selections": [
                        {
                            "sheet_name": "Summary",
                            "value_column": 2,
                            "excel_column": "B",
                            "period_label": PERIOD_LABEL,
                        }
                    ],
                }
            }
        ),
        encoding="utf-8",
    )
    return StructureRun(
        run_id="run_remediation",
        workbook_id="wb_remediation",
        source_filename="test.xlsx",
        requested_period=PERIOD_LABEL,
        status=PipelineStatus.PASS,
        stages=[
            StageRun(
                stage_name="sheet_routing",
                status="pass",
                artifact_paths={"selection": str(routing_path)},
            ),
            StageRun(
                stage_name="period_binding",
                status="pass",
                artifact_paths={"selections": str(bindings_path)},
            ),
        ],
    )


def test_every_period_requires_its_own_nonzero_labeled_evidence():
    evidence = [
        {
            "label": "Rooms Revenue",
            "selected_values": {"actual": 100.0, "budget": None},
        }
    ]

    with pytest.raises(RuntimeError, match=r"Budget \[budget\]"):
        _require_meaningful_period_evidence(
            evidence,
            {"actual": "Actual", "budget": "Budget"},
        )


def test_empty_excel_period_stops_before_mapping_client_is_created(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(
        pipeline_module,
        "create_model_client",
        lambda **_kwargs: pytest.fail("mapping client should not be created"),
    )
    parsed = SharedWorkbook(tmp_path / "test.xlsx", record=_record(value=None))

    with pytest.raises(RuntimeError, match="no finite, non-zero"):
        normalize_workbook(
            tmp_path / "test.xlsx",
            output_dir=tmp_path / "work",
            prior_run=_prior_run(tmp_path),
            selected_period_ids=[PERIOD_ID],
            source_name="test.xlsx",
            parsed=parsed,
        )

    assert (tmp_path / "work" / "evidence.json").is_file()


def test_excel_mapping_exception_persists_usage_trace_and_retry_input(
    tmp_path, monkeypatch
):
    client = SimpleNamespace(
        usage_history=[{"model_name": "test-model", "total_token_count": 12}],
        last_tool_trace=[{"tool": "validate_mapping", "status": "error"}],
    )
    monkeypatch.setattr(
        pipeline_module,
        "create_model_client",
        lambda **_kwargs: client,
    )

    def fail_mapping(**_kwargs):
        raise RuntimeError("injected mapping failure")

    monkeypatch.setattr(pipeline_module, "map_workbook", fail_mapping)

    with pytest.raises(RuntimeError, match="injected mapping failure"):
        normalize_workbook(
            tmp_path / "test.xlsx",
            output_dir=tmp_path / "work",
            prior_run=_prior_run(tmp_path),
            selected_period_ids=[PERIOD_ID],
            source_name="test.xlsx",
            parsed=SharedWorkbook(tmp_path / "test.xlsx", record=_record()),
        )

    failure = json.loads((tmp_path / "work" / "failure.json").read_text())
    assert failure["stage"] == "excel_mapping"
    assert failure["model_calls"] == client.usage_history
    assert failure["tool_trace"] == client.last_tool_trace
    assert Path(failure["evidence_path"]).is_file()
    assert failure["period_labels"] == {PERIOD_ID: PERIOD_LABEL}


def test_cli_checkpoints_run_log_before_workbook_failure(tmp_path, monkeypatch):
    result = NormalizationResult(
        workbook_id="wb",
        source_name="test.xlsx",
        period_label=PERIOD_LABEL,
        period_labels={PERIOD_ID: PERIOD_LABEL},
        period_values={PERIOD_ID: {"S12.total_revenue": 100.0}},
        values={"S12.total_revenue": 100.0},
        coa={"S12.total_revenue": {}},
        accepted=True,
        outcome="clean",
    )

    def save_log(_result, path):
        path.write_text('{"saved": true}', encoding="utf-8")
        return path

    def fail_workbook(_result, _path):
        assert (tmp_path / "run_log.json").is_file()
        raise RuntimeError("injected output failure")

    monkeypatch.setattr(cli_module, "write_run_log", save_log)
    monkeypatch.setattr(cli_module, "write_normalized_workbook", fail_workbook)

    with pytest.raises(RuntimeError, match="injected output failure"):
        _write_result_artifacts(
            result,
            output_dir=tmp_path,
            source_path=tmp_path / "test.xlsx",
            selected_ids=[PERIOD_ID],
            is_pdf=False,
            work_dir=tmp_path / "work",
        )

    summary = json.loads((tmp_path / "summary.json").read_text())
    assert summary["accepted"] is False
    assert summary["outcome"] == "artifact_failure"
    assert summary["mapping_outcome"] == "clean"
    assert summary["mapping_result_saved"] is True
    assert summary["failure_stage"] == "workbook_output"


def test_unhandled_pipeline_failure_always_gets_a_rejected_summary(tmp_path):
    error = RuntimeError("injected pipeline failure")

    cli_module._write_unhandled_failure_summary(
        tmp_path,
        tmp_path / "test.xlsx",
        error,
    )

    summary = json.loads((tmp_path / "summary.json").read_text())
    assert summary == {
        "accepted": False,
        "outcome": "stage_failure",
        "stopped_reason": "RuntimeError: injected pipeline failure",
        "failure": "RuntimeError: injected pipeline failure",
        "failure_stage": "pipeline",
        "delivery_status": "failed",
        "mapping_result_saved": False,
        "source_name": "test.xlsx",
    }


def test_run_log_preserves_raw_inputs_when_feedback_composition_fails(monkeypatch):
    result = NormalizationResult(
        workbook_id="wb",
        source_name="test.xlsx",
        period_label=PERIOD_LABEL,
        values={},
        coa={},
        checks=["warning|coverage_unspecified|S12.total_revenue|review"],
        execution_issues=["raw execution issue"],
    )

    def fail_feedback(_result):
        raise FeedbackCompositionError("injected invariant failure")

    monkeypatch.setattr(run_log_module, "compose_result_feedback", fail_feedback)
    log = build_run_log(result)

    assert log["feedback_manifest"]["mode"] == "fallback"
    assert log["checks"] == result.checks
    assert log["execution_issues"] == ["raw execution issue"]
    assert "injected invariant failure" in log["feedback_manifest"][
        "composition_error"
    ]
