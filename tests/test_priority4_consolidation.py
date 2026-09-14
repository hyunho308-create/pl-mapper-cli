from __future__ import annotations

import hashlib
import json
from collections import Counter
from dataclasses import asdict, fields
from datetime import datetime, timezone
from importlib import resources
from pathlib import Path
from types import SimpleNamespace

import pytest

from hotel_pl_normalizer.feedback import (
    COVERAGE_GAP,
    RECONCILIATION_DIFFERENCE,
    SOURCE_PRESENTATION,
    VALIDATION_ERROR,
    PeriodComparison,
    _period_sentence,
)
from hotel_pl_normalizer.mapping.arithmetic import (
    MissingValuePolicy,
    evaluate_operation,
)
from hotel_pl_normalizer.mapping.coa import (
    DERIVED_SUMMARY_LINKS,
    DETERMINISTIC_SUMMARY_CALCULATIONS,
    SUMMARY_EQUATIONS,
    SUMMARY_LINKS,
    AccountingEquationError,
    _parse_accounting_equations,
    canonical_coa_ids,
    dependency_coefficient,
    load_accounting_equations,
    load_coa,
)
from hotel_pl_normalizer.mapping.mapper import (
    MappingReviewItem,
    WorkbookMappingValidator,
    _load_coa,
    _normalize_patch_review_items,
)
from hotel_pl_normalizer.mapping.reviews import normalize_review_item
from hotel_pl_normalizer.models.pdf import PdfDocumentRecord, PdfPage, PdfSource
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
    _build_normalization_result,
)
from hotel_pl_normalizer.providers.base import AgentToolset, ModelToolError
from hotel_pl_normalizer.structure.binding.toolset import PeriodBindingToolset
from hotel_pl_normalizer.structure.exploration.toolset import (
    WorkbookExplorationToolset,
)
from hotel_pl_normalizer.structure.pdf.stages import (
    PdfBindingToolset,
    PdfExplorationToolset,
)
from hotel_pl_normalizer.structure.pdf.toolset import PdfInspectionToolset


def _compact_bytes(value) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")


def _workbook_record() -> WorkbookRecord:
    return WorkbookRecord(
        workbook_id="wb",
        source=WorkbookSource(
            source_id="source",
            original_filename="source.xlsx",
            file_type=FileType.XLSX,
            ingested_at=datetime(2026, 9, 4, tzinfo=timezone.utc),
        ),
        workbook_metadata=WorkbookMetadata(sheet_count=1),
        sheets=[
            WorkbookSheet(
                sheet_id="sheet",
                sheet_name="P&L",
                max_row=2,
                max_column=2,
                rows=[
                    WorkbookRow(
                        row_index=1,
                        cells=[
                            CellRecord(
                                row=1,
                                column=1,
                                address="A1",
                                raw_value="Account",
                            ),
                            CellRecord(
                                row=1,
                                column=2,
                                address="B1",
                                raw_value="Actual",
                            ),
                        ],
                    ),
                    WorkbookRow(
                        row_index=2,
                        cells=[
                            CellRecord(
                                row=2,
                                column=1,
                                address="A2",
                                raw_value="Revenue",
                            ),
                            CellRecord(
                                row=2,
                                column=2,
                                address="B2",
                                raw_value=1.0,
                            ),
                        ],
                    ),
                ],
            )
        ],
    )


def _pdf_document() -> PdfDocumentRecord:
    return PdfDocumentRecord(
        document_id="pdf",
        source=PdfSource(
            source_id="source",
            original_filename="source.pdf",
            local_path="source.pdf",
            file_hash="hash",
            ingested_at=datetime(2026, 9, 4, tzinfo=timezone.utc),
        ),
        pages=[PdfPage(page_number=1, width=612.0, height=792.0)],
    )


def test_declared_tools_have_live_dispatch_paths_and_hidden_bindings_stay_hidden(
    monkeypatch,
):
    exploration = WorkbookExplorationToolset(
        SimpleNamespace(
            path=Path("source.xlsx"),
            sheets=lambda: [SimpleNamespace(sheet_name="P&L")],
        )
    )
    exploration_methods = {
        "list_sheets": "_list_sheets",
        "read_rows": "_read_rows",
        "find_text": "_find_text",
        "submit_routing": "_submit_routing",
        "submit_periods": "_submit_periods",
    }
    for name, method in exploration_methods.items():
        monkeypatch.setattr(
            exploration,
            method,
            (lambda *args, dispatched=name: {"dispatched": dispatched}),
        )
    assert [item["name"] for item in exploration.declarations()] == list(
        exploration_methods
    )
    for name in exploration_methods:
        assert exploration.dispatch(name, {}) == {"dispatched": name}

    binding = PeriodBindingToolset(
        _workbook_record(),
        period_ids=["actual"],
        financial_sheets=["P&L"],
    )
    binding_methods = {
        "list_sheet_layouts": "_list_sheet_layouts",
        "list_sheets": "_list_sheets",
        "read_rows": "_read_rows",
        "read_headers": "_read_headers",
        "find_rows": "_find_rows",
        "column_stats": "_column_stats",
        "submit_layout_bindings": "_submit_layout_bindings",
    }
    for name, method in binding_methods.items():
        monkeypatch.setattr(
            binding,
            method,
            (lambda *args, dispatched=name: {"dispatched": dispatched}),
        )
    monkeypatch.setattr(
        binding,
        "_submit_bindings",
        lambda *_args: {"dispatched": "submit_bindings"},
    )
    declared_binding = [item["name"] for item in binding.declarations()]
    assert declared_binding == list(binding_methods)
    for name in binding_methods:
        assert binding.dispatch(name, {}) == {"dispatched": name}
    assert "submit_bindings" not in declared_binding
    assert binding.dispatch("submit_bindings", {}) == {
        "dispatched": "submit_bindings"
    }

    inspection = PdfInspectionToolset(_pdf_document())
    inspection_methods = {
        "inspect_document": "inspect_document",
        "list_pages": "list_pages",
        "read_page_lines": "read_page_lines",
        "read_region": "read_region",
        "find_text": "find_text",
        "numeric_anchors": "numeric_anchors",
    }
    for name, method in inspection_methods.items():
        monkeypatch.setattr(
            inspection,
            method,
            (lambda *args, dispatched=name, **kwargs: {"dispatched": dispatched}),
        )
    assert [item["name"] for item in inspection.declarations()] == list(
        inspection_methods
    )
    for name in inspection_methods:
        assert inspection.dispatch(name, {}) == {"dispatched": name}

    pdf_exploration = PdfExplorationToolset(_pdf_document())
    pdf_exploration_methods = {
        **inspection_methods,
        "submit_routing": "_submit_routing",
        "submit_periods": "_submit_periods",
    }
    for name, method in pdf_exploration_methods.items():
        monkeypatch.setattr(
            pdf_exploration,
            method,
            (lambda *args, dispatched=name, **kwargs: {"dispatched": dispatched}),
        )
    assert [item["name"] for item in pdf_exploration.declarations()] == list(
        pdf_exploration_methods
    )
    for name in pdf_exploration_methods:
        result = pdf_exploration.dispatch(name, {})
        assert result.get("dispatched") == name

    pdf_binding = object.__new__(PdfBindingToolset)
    AgentToolset.__init__(pdf_binding)
    pdf_binding_methods = {
        **inspection_methods,
        "list_financial_layouts": "_list_financial_layouts",
        "submit_layout_bindings": "_submit_layout_bindings",
    }
    for name, method in pdf_binding_methods.items():
        monkeypatch.setattr(
            pdf_binding,
            method,
            (lambda *args, dispatched=name, **kwargs: {"dispatched": dispatched}),
        )
    monkeypatch.setattr(
        pdf_binding,
        "_submit_bindings",
        lambda *_args: {"dispatched": "submit_bindings"},
    )
    declared_pdf_binding = [item["name"] for item in pdf_binding.declarations()]
    assert declared_pdf_binding == list(pdf_binding_methods)
    for name in pdf_binding_methods:
        result = pdf_binding.dispatch(name, {})
        assert result.get("dispatched") == name
    assert "submit_bindings" not in declared_pdf_binding
    assert pdf_binding.dispatch("submit_bindings", {}) == {
        "dispatched": "submit_bindings"
    }

    mapping = WorkbookMappingValidator("wb", [], _load_coa())
    assert [item["name"] for item in mapping.declarations()] == [
        "validate_mapping",
        "patch_mapping",
    ]
    with pytest.raises(ModelToolError, match="Draft mapping is not valid"):
        mapping.dispatch("validate_mapping", {})
    with pytest.raises(ModelToolError, match="requires an initial"):
        mapping.dispatch("patch_mapping", {})
    with pytest.raises(ModelToolError, match="Unknown tool"):
        mapping.dispatch("not_declared", {})


def test_shared_coa_loader_preserves_projection_order_and_isolation():
    first = load_coa()
    second = load_coa()

    assert list(first) == canonical_coa_ids()
    assert list(first) == list(_load_coa())
    assert len(first) == 271
    assert len(_compact_bytes(first)) == 101912
    assert hashlib.sha256(_compact_bytes(first)).hexdigest() == (
        "d6fe7e74388c0f8c44424be18f5e49cf68dd86ae256a4e0f6aa261198906fd69"
    )
    assert set(next(iter(first.values()))) == {
        "department",
        "coa_id",
        "account_name",
        "hierarchy_path",
        "parent_coa_id",
        "is_residual",
        "mapping_note",
        "synonyms",
    }
    coa_id = next(iter(first))
    first[coa_id]["account_name"] = "mutated"
    assert second[coa_id]["account_name"] != "mutated"


def test_accounting_equation_resource_rebuilds_exact_legacy_constants():
    terms = load_accounting_equations()
    counts = Counter(term.relationship_kind for term in terms)
    assert counts == {
        "reported_summary_link": 12,
        "derived_summary_link": 7,
        "summary_equation": 25,
        "deterministic_calculation": 3,
    }
    resource = resources.files("hotel_pl_normalizer.data").joinpath(
        "accounting_equations.csv"
    ).read_bytes()
    assert len(resource) == 4156
    assert hashlib.sha256(resource).hexdigest() == (
        "b97b0f80950e4e27951cbe9a5addcb75ea0498ec20baaeecc9f38b0cbd5d3b82"
    )
    reconstructed = {
        "summary_links": SUMMARY_LINKS,
        "derived_summary_links": DERIVED_SUMMARY_LINKS,
        "summary_equations": SUMMARY_EQUATIONS,
        "deterministic": DETERMINISTIC_SUMMARY_CALCULATIONS,
    }
    encoded = _compact_bytes(reconstructed)
    assert len(encoded) == 3853
    assert hashlib.sha256(encoded).hexdigest() == (
        "cc89d16dd57cf7f3879fef3329326ef0dc2f1b09c9b6d2b5a74b7f675be500d8"
    )
    assert dependency_coefficient(
        "S12.total_undistributed_expenses", "S12.gop"
    ) == -1


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (
            lambda rows: rows.append(dict(rows[0])),
            "duplicate relationship target/order",
        ),
        (
            lambda rows: rows[0].update(target_coa_id="S99.unknown"),
            "unknown COA id",
        ),
        (
            lambda rows: rows[0].update(coefficient="0"),
            "finite and nonzero",
        ),
        (
            lambda rows: rows[0].update(term_order="0"),
            "term_order must be positive",
        ),
        (
            lambda rows: rows[0].update(
                source_coa_id=rows[0]["target_coa_id"]
            ),
            "dependency cycle",
        ),
    ],
)
def test_accounting_equation_loader_rejects_invalid_controlled_data(
    mutate,
    message,
):
    rows = [asdict(term) for term in load_accounting_equations()]
    mutate(rows)
    with pytest.raises(AccountingEquationError, match=message):
        _parse_accounting_equations(rows, known_coa_ids=set(canonical_coa_ids()))


@pytest.mark.parametrize(
    ("operation", "included", "excluded", "scale", "expected"),
    [
        ("direct", [2.0], [], None, 2.0),
        ("sum", [2.0, None, 3.0], [], None, 5.0),
        ("adjusted_subtotal", [10.0, None], [3.0, None], None, 7.0),
        ("negate", [2.0, None, 3.0], [], None, -5.0),
        ("ratio", [6.0, 3.0], [], None, 2.0),
        ("product", [2.0, 3.0], [], None, 6.0),
        ("scale", [2.0, None, 3.0], [], 10.0, 50.0),
        ("no_value", [], [], None, None),
    ],
)
def test_shared_arithmetic_preserves_execute_semantics(
    operation,
    included,
    excluded,
    scale,
    expected,
):
    assert evaluate_operation(
        operation,
        included,
        excluded,
        scale_factor=scale,
        missing_value_policy=MissingValuePolicy.IGNORE,
        empty_value=None,
    ) == expected


def test_shared_arithmetic_keeps_strict_source_layer_and_error_contracts():
    assert (
        evaluate_operation(
            "sum",
            [2.0, None],
            missing_value_policy=MissingValuePolicy.PROPAGATE,
            empty_value=0.0,
        )
        is None
    )
    assert evaluate_operation(
        "sum",
        [],
        missing_value_policy=MissingValuePolicy.PROPAGATE,
        empty_value=0.0,
    ) == 0.0
    with pytest.raises(ValueError, match="direct requires one row"):
        evaluate_operation(
            "direct",
            [1.0, 2.0],
            missing_value_policy=MissingValuePolicy.IGNORE,
            empty_value=0.0,
        )
    with pytest.raises(ValueError, match="ratio requires two rows$"):
        evaluate_operation(
            "ratio",
            [1.0],
            missing_value_policy=MissingValuePolicy.IGNORE,
            empty_value=0.0,
        )
    with pytest.raises(
        ValueError,
        match="ratio requires two rows and nonzero denominator",
    ):
        evaluate_operation(
            "ratio",
            [1.0, 0.0],
            missing_value_policy=MissingValuePolicy.IGNORE,
            empty_value=0.0,
        )


def test_review_adapter_preserves_typed_ids_and_unknown_historical_kinds():
    typed = MappingReviewItem(
        kind="unusual_convention",
        message="Typed note",
        coa_ids=["S1.total_rooms_revenue"],
        source_rows=["P&L!7"],
    )
    typed._review_item_id = "review:typed"
    typed_view = normalize_review_item(typed)
    legacy_view = normalize_review_item(
        {
            "kind": "retired_historical_kind",
            "message": "Legacy note",
            "coa_ids": ["S1.total_rooms_revenue"],
            "source_rows": ["P&L!7"],
            "review_item_id": "review:legacy",
            "selected_source_rows": ["P&L!7"],
            "selected_source_operation": "retired_operation",
        }
    )

    assert typed_view.review_item_id == "review:typed"
    assert typed_view.message == "Typed note"
    assert legacy_view.kind == "retired_historical_kind"
    assert legacy_view.review_item_id == "review:legacy"
    assert legacy_view.selected_source_operation == "retired_operation"


def test_malformed_optional_review_comparison_salvage_keeps_legacy_note():
    item = {
        "kind": "source_discrepancy",
        "message": "Keep this historical explanation",
        "coa_ids": ["S1.total_rooms_revenue"],
        "source_rows": ["P&L!7"],
        "selected_source_rows": ["P&L!7"],
        "alternate_source_rows": ["P&L!8"],
        "selected_source_operation": "direct",
        "alternate_source_operation": "direct",
    }

    [salvaged] = _normalize_patch_review_items([item])

    assert salvaged["message"] == item["message"]
    assert salvaged["kind"] == item["kind"]
    assert salvaged["source_rows"] == item["source_rows"]
    assert salvaged["selected_source_rows"] == []
    assert salvaged["alternate_source_rows"] == []
    assert salvaged["selected_source_operation"] is None
    assert salvaged["alternate_source_operation"] is None


@pytest.mark.parametrize(
    ("category", "rules", "variance", "explanation", "expected"),
    [
        (
            VALIDATION_ERROR,
            {"summary_department"},
            123.6,
            "",
            "The Summary amount is 124 above the independently reported department amount in FY2025.",
        ),
        (
            VALIDATION_ERROR,
            {"summary_math"},
            -123.6,
            "",
            "The reported Summary amount is 124 below the required equation in FY2025.",
        ),
        (
            VALIDATION_ERROR,
            {"hierarchy_complete"},
            123.6,
            "",
            "Child accounts are 124 below the parent in FY2025.",
        ),
        (
            COVERAGE_GAP,
            set(),
            -123.6,
            "",
            "Identified children are 124 above the parent in FY2025.",
        ),
        (
            SOURCE_PRESENTATION,
            set(),
            0.0,
            "",
            "Compared with the alternate source, the selected amount is equal in FY2025.",
        ),
        (
            RECONCILIATION_DIFFERENCE,
            set(),
            123.6,
            "",
            "The reported total is 124 higher in FY2025.",
        ),
    ],
)
def test_variance_prose_matrix_preserves_exact_direction_and_wording(
    category,
    rules,
    variance,
    explanation,
    expected,
):
    assert _period_sentence(
        category,
        [
            PeriodComparison(
                period_id="actual",
                period_label="FY2025",
                variance=variance,
            )
        ],
        rules=rules,
        explanation=explanation,
    ) == expected


def test_shared_result_builder_preserves_all_fields_and_mapping_values():
    assert tuple(item.name for item in fields(NormalizationResult)) == (
        "workbook_id",
        "source_name",
        "period_label",
        "values",
        "coa",
        "period_labels",
        "period_values",
        "residual_plugs_by_period",
        "checks_by_period",
        "execution_issues_by_period",
        "dropped_periods",
        "decisions",
        "checks",
        "execution_issues",
        "review_items",
        "source_controls",
        "accepted",
        "outcome",
        "exceptions",
        "stopped_reason",
        "duration_ms",
        "session_calls",
        "session_call_ms",
        "session_tool_calls",
        "session_exhausted",
        "cost_usd",
        "mapping_provider",
        "mapping_model",
        "cost_details",
        "evidence",
        "model_calls",
        "tool_trace",
        "mapping_selection",
        "structure_stages",
        "feedback_manifest",
    )
    mapping = SimpleNamespace(
        values={"S1.test": 1.0},
        coa={"S1.test": {"coa_id": "S1.test"}},
        values_by_period={"actual": {"S1.test": 1.0}},
        residual_plugs_by_period={"actual": {}},
        checks_by_period={"actual": []},
        execution_issues_by_period={"actual": []},
        decisions=[],
        checks=[],
        execution_issues=[],
        review_items=[],
        accepted=True,
        outcome=SimpleNamespace(value="clean"),
        exceptions=[],
        stopped_reason=None,
        session_calls=2,
        session_call_ms=[3, 4],
        session_tool_calls=5,
        session_exhausted=False,
        mapping_selection={"selected": 1},
    )
    result = _build_normalization_result(
        workbook_id="wb",
        source_name="source.xlsx",
        period_label="FY2025",
        period_labels={"actual": "FY2025"},
        dropped_periods={"prior": "unavailable"},
        mapping=mapping,
        duration_ms=10,
        cost_usd=0.25,
        mapping_client=SimpleNamespace(provider="provider", model_name="model"),
        cost_details={"scope": "test"},
        evidence=[],
        model_calls=[{"call": 1}],
        tool_trace=[{"tool": "read"}],
        structure_stages=[{"stage_name": "binding", "status": "pass"}],
    )

    assert result.values is mapping.values
    assert result.period_values is mapping.values_by_period
    assert result.outcome == "clean"
    assert result.duration_ms == 10
    assert result.mapping_provider == "provider"
    assert result.mapping_model == "model"
    assert result.mapping_selection == {"selected": 1}
    assert result.feedback_manifest == {}
