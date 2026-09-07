from __future__ import annotations

import inspect
from copy import deepcopy
from types import SimpleNamespace

import pytest
from pydantic import BaseModel

from hotel_pl_normalizer.mapping.mapper import AccountSourceDecision
from hotel_pl_normalizer.models.evidence import (
    EvidenceLocatorKind,
    EvidenceRow,
    ExcelRowLocator,
    PdfLineLocator,
    PdfValueAnchor,
    PeriodLocationSummary,
)
from hotel_pl_normalizer.models.pdf_structure import PdfBindings
from hotel_pl_normalizer.pipeline import NormalizationResult, normalize_pdf
from hotel_pl_normalizer.providers.base import AgentToolset
from hotel_pl_normalizer.providers.openai_api import OpenAIModelClient
from hotel_pl_normalizer.run_log import build_run_log


def _legacy_excel_row() -> dict:
    return {
        "row_key": "P&L!国際!7",
        "label": "Rooms Revenue",
        "selected_value_columns": {"actual": 2, "prior": 3},
        "selected_values": {"actual": 1250.0, "prior": 1100.0},
        "selected_value_formats": {"actual": "$#,##0", "prior": "$#,##0"},
        "selected_value_column": 2,
        "selected_value": 1250.0,
        "indent": 1.0,
        "bold": False,
        "label_column": 1,
        "label_rule": "primary",
        "label_status": "selected",
        "label_context": [],
    }


def _legacy_pdf_row(anchor: float = 240.0001) -> EvidenceRow:
    payload = {
        "row_key": "Page 001!7",
        "label": "Rooms Revenue",
        "selected_value_columns": {"actual": f"x={anchor:.3f}"},
        "selected_values": {"actual": 1250.0},
        "selected_value_column": f"x={anchor:.3f}",
        "selected_value": 1250.0,
        "indent": 0.0,
        "bold": False,
        "label_x0": 40.125,
        "label_rule": "primary",
        "label_status": "selected",
        "label_context": [],
        "pdf_source": {"page": 1, "line_id": "p1:l7", "top": 20.125},
    }
    return EvidenceRow.from_legacy_dict(
        payload,
        anchors_by_period={"actual": PdfValueAnchor(anchor)},
        primary_period_id="actual",
    )


def test_legacy_excel_adapter_is_exact_and_uses_rsplit_for_sheet_names():
    payload = _legacy_excel_row()
    expected = deepcopy(payload)

    row = EvidenceRow.from_legacy_dict(payload, primary_period_id="actual")

    assert row.to_legacy_dict() == expected
    assert set(row.to_legacy_dict()) == set(expected)
    assert isinstance(row.locator, ExcelRowLocator)
    assert row.locator.sheet_name == "P&L!国際"
    assert row.row_key == "P&L!国際!7"
    assert row.display == "P&L!国際 row 7"
    payload["label"] = "mutated"
    assert row.label == "Rooms Revenue"


def test_page_like_excel_scope_is_not_inferred_as_pdf_and_identities_are_unique():
    excel = EvidenceRow.from_legacy_dict(
        {**_legacy_excel_row(), "row_key": "Page 001!7"},
        primary_period_id="actual",
    )
    pdf = _legacy_pdf_row()

    assert excel.locator.kind == EvidenceLocatorKind.EXCEL
    assert pdf.locator.kind == EvidenceLocatorKind.PDF
    assert excel.row_key == pdf.row_key == "Page 001!7"
    assert excel.identity != pdf.identity


def test_multi_period_pdf_anchors_keep_full_precision_outside_legacy_display():
    first = _legacy_pdf_row(240.0001)
    second = _legacy_pdf_row(240.0004)
    first_anchor = first.anchors_by_period["actual"]
    second_anchor = second.anchors_by_period["actual"]

    assert isinstance(first.locator, PdfLineLocator)
    assert first.identity == second.identity
    assert first_anchor is not None and second_anchor is not None
    assert first_anchor.display == second_anchor.display == "x=240.000"
    assert first_anchor.right_edge == 240.0001
    assert second_anchor.right_edge == 240.0004
    assert first.to_audit_dict()["anchors_by_period"]["actual"]["right_edge"] != (
        second.to_audit_dict()["anchors_by_period"]["actual"]["right_edge"]
    )


def test_pdf_period_summary_preserves_real_geometry_and_never_builds_excel_selections():
    bindings = PdfBindings.model_validate(
        {
            "bindings": [
                {
                    "period_id": "actual",
                    "start_page": 2,
                    "end_page": 4,
                    "right_edge": 240.0001,
                    "header_text": "2025 Actual",
                    "evidence": ["Page 2 header"],
                }
            ]
        }
    )

    summary = PeriodLocationSummary.from_pdf(
        bindings,
        {"actual": "2025 Actual"},
    )

    assert summary.prompt_lines() == [
        "Pages 2-4|right_edge=240.0001|2025 Actual"
    ]
    assert summary.locations[0].anchor.right_edge == 240.0001
    source = inspect.getsource(normalize_pdf)
    assert "PeriodColumnSelection(" not in source
    assert "PeriodColumnSelectionMap(" not in source


def test_v5_log_adds_typed_evidence_without_changing_string_source_rows():
    legacy = _legacy_excel_row()
    row = EvidenceRow.from_legacy_dict(legacy, primary_period_id="actual")
    decision = AccountSourceDecision.model_validate(
        {
            "coa_id": "S1.rooms_revenue",
            "operation": "direct",
            "source_rows": [legacy["row_key"]],
        }
    )
    result = NormalizationResult(
        workbook_id="wb",
        source_name="source.xlsx",
        period_label="2025 Actual",
        period_labels={"actual": "2025 Actual"},
        values={"S1.rooms_revenue": 1250.0},
        period_values={"actual": {"S1.rooms_revenue": 1250.0}},
        coa={
            "S1.rooms_revenue": {
                "coa_id": "S1.rooms_revenue",
                "account_name": "Rooms Revenue",
            }
        },
        decisions=[decision],
        evidence=[row],
        feedback_manifest={"rendered_count": 0},
    )

    log = build_run_log(result)

    assert log["log_version"] == 5
    assert decision.source_rows == ["P&L!国際!7"]
    assert all(isinstance(item, str) for item in decision.source_rows)
    assert {
        key: log["evidence_rows"][0][key] for key in legacy
    } == legacy
    assert log["evidence_rows"][0]["locator"]["kind"] == "excel"
    assert log["evidence_rows"][0]["anchors_by_period"]["actual"][
        "column_index"
    ] == 2
    assert log["accounts"][0]["source_rows"][0]["row_key"] == "P&L!国際!7"


class _Answer(BaseModel):
    answer: int


class _Responses:
    def __init__(self, responses):
        self._responses = iter(responses)
        self.requests: list[dict] = []

    def create(self, **request):
        self.requests.append(request)
        return next(self._responses)


def _response(response_id: str, *, output=None, output_text=""):
    return SimpleNamespace(
        id=response_id,
        status="completed",
        output=output or [],
        output_text=output_text,
        usage=None,
    )


def _call(name: str = "finish"):
    return SimpleNamespace(
        type="function_call",
        name=name,
        arguments="{}",
        call_id=f"call:{name}",
    )


def _offline_client(monkeypatch, responses: _Responses, **kwargs):
    client = OpenAIModelClient(**kwargs)
    monkeypatch.setattr(client, "_validate_environment", lambda: None)
    client._client_instance = SimpleNamespace(responses=responses)
    return client


class _TerminalToolset(AgentToolset):
    def declarations(self):
        return [{"name": "finish", "parameters": {"type": "object"}}]

    def dispatch(self, name, arguments):
        assert name == "finish" and arguments == {}
        return {"ok": True, "answer": 7}

    def terminal_result(self, name, result):
        return self.store_terminal({"answer": result["answer"]})


def test_agent_toolset_shared_lifecycle_primitives_are_optional_and_exact():
    unbudgeted = AgentToolset()
    assert not hasattr(unbudgeted, "reads")
    assert not hasattr(unbudgeted, "max_reads")
    with pytest.raises(RuntimeError, match="no global read budget"):
        unbudgeted.record_read()

    budgeted = AgentToolset(max_reads=1)
    assert budgeted.reads == 0
    assert budgeted.max_reads == 1
    assert budgeted.read_budget_result(instruction="Stop.") is None
    assert budgeted.record_read() == 1
    assert budgeted.read_budget_result(instruction="Stop.") == {
        "ok": False,
        "error": "Read budget of 1 calls is spent.",
        "instruction": "Stop.",
    }
    assert budgeted.increment_counter("submissions") == 1
    assert budgeted.increment_counter("submissions") == 2
    assert budgeted.increment_counter("repairs") == 1
    assert budgeted.counter_value("submissions") == 2
    assert budgeted.counter_value("repairs") == 1
    terminal = {"accepted": True}
    assert budgeted.store_terminal(terminal) is terminal
    assert budgeted.terminal_value is terminal
    assert budgeted.terminal_result("submit", terminal) is terminal


def test_agent_toolset_defaults_and_provider_terminal_hook(monkeypatch):
    base = AgentToolset()
    assert base.terminal_result("tool", {}) is None
    assert base.final_response_error(_Answer(answer=1)) is None
    assert base.record_rejection("no") == "no"
    assert base.rejections == ["no"]

    responses = _Responses([_response("r1", output=[_call()])])
    client = _offline_client(monkeypatch, responses)
    trace: list[dict] = []
    result = client.generate_json_model_with_tools(
        "prompt",
        _Answer,
        toolset=_TerminalToolset(),
        max_iterations=2,
        trace=trace,
    )

    assert result == _Answer(answer=7)
    assert client.last_tool_trace is trace
    assert trace == [{"tool": "finish", "arguments": {}, "ok": True}]
    assert client.usage_history[0]["terminal_tool_result"] is True


class _RetryToolset(AgentToolset):
    def declarations(self):
        return []

    def dispatch(self, name, arguments):  # pragma: no cover - no tool calls
        raise AssertionError((name, arguments))

    def final_response_error(self, result):
        return "Try the final answer again." if result.answer == 1 else None


def test_provider_final_hook_retries_with_response_chain_and_same_reasoning(monkeypatch):
    responses = _Responses(
        [
            _response("r1", output_text='{"answer":1}'),
            _response("r2", output_text='{"answer":2}'),
        ]
    )
    client = _offline_client(monkeypatch, responses, reasoning_effort="high")

    result = client.generate_json_model_with_tools(
        "prompt",
        _Answer,
        toolset=_RetryToolset(),
        max_iterations=2,
    )

    assert result == _Answer(answer=2)
    assert responses.requests[1]["previous_response_id"] == "r1"
    assert responses.requests[1]["input"] == "Try the final answer again."
    assert [item["reasoning"]["effort"] for item in responses.requests] == [
        "high",
        "high",
    ]


class _RepairToolset(AgentToolset):
    def declarations(self):
        return [{"name": "validate", "parameters": {"type": "object"}}]

    def dispatch(self, name, arguments):
        assert name == "validate" and arguments == {}
        return {
            "ok": True,
            "accepted": False,
            "error_count": 1,
            "warning_count": 0,
            "validation_attempt": 1,
            "findings": ["error"],
        }


def test_provider_rejection_keeps_trace_chain_and_switches_repair_budget(monkeypatch):
    responses = _Responses(
        [
            _response("r1", output=[_call("validate")]),
            _response("r2", output_text='{"answer":3}'),
        ]
    )
    client = _offline_client(
        monkeypatch,
        responses,
        reasoning_effort="high",
        repair_reasoning_effort="low",
    )
    client.repair_max_output_tokens = 4096
    trace: list[dict] = []

    result = client.generate_json_model_with_tools(
        "prompt",
        _Answer,
        toolset=_RepairToolset(),
        max_iterations=2,
        trace=trace,
    )

    assert result.answer == 3
    assert responses.requests[1]["previous_response_id"] == "r1"
    assert responses.requests[1]["reasoning"]["effort"] == "low"
    assert responses.requests[1]["max_output_tokens"] == 4096
    assert trace[0]["validation"]["accepted"] is False
    assert client.last_tool_trace is trace
