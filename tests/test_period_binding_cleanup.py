from datetime import datetime, timezone

import pytest

from hotel_pl_normalizer.models.binding import WorkbookBindings
from hotel_pl_normalizer.models.period_selection import PeriodOption
from hotel_pl_normalizer.models.workbook import (
    CellRecord,
    FileType,
    WorkbookMetadata,
    WorkbookRecord,
    WorkbookRow,
    WorkbookSheet,
    WorkbookSource,
)
from hotel_pl_normalizer.structure.binding.adapters import binding_to_selection_maps
from hotel_pl_normalizer.structure.binding.agent import bind_periods
from hotel_pl_normalizer.structure.binding.checks import check_bindings
from hotel_pl_normalizer.structure.binding.toolset import PeriodBindingToolset


def _sheet(name: str, value: float = 100.0) -> WorkbookSheet:
    return WorkbookSheet(
        sheet_id=f"sheet:{name}",
        sheet_name=name,
        max_row=2,
        max_column=3,
        rows=[
            WorkbookRow(
                row_index=1,
                cells=[
                    CellRecord(
                        row=1,
                        column=1,
                        address="A1",
                        raw_value="Account",
                        display_value="Account",
                    ),
                    CellRecord(
                        row=1,
                        column=2,
                        address="B1",
                        raw_value="Actual",
                        display_value="Actual",
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
                        raw_value="Rooms Revenue",
                        display_value="Rooms Revenue",
                    ),
                    CellRecord(
                        row=2,
                        column=2,
                        address="B2",
                        raw_value=value,
                        display_value=str(value),
                    ),
                ],
            ),
        ],
    )


def _workbook() -> WorkbookRecord:
    return WorkbookRecord(
        workbook_id="wb_binding_cleanup",
        source=WorkbookSource(
            source_id="primary:test",
            original_filename="test.xlsx",
            file_type=FileType.XLSX,
            ingested_at=datetime(2026, 8, 31, tzinfo=timezone.utc),
            file_hash="test",
        ),
        workbook_metadata=WorkbookMetadata(sheet_count=3),
        sheets=[_sheet("Summary"), _sheet("Rooms"), _sheet("Excluded")],
    )


def _check(payload: dict, periods: list[str] | None = None):
    workbook = _workbook()
    return check_bindings(
        WorkbookBindings.model_validate(payload),
        {sheet.sheet_name: sheet for sheet in workbook.sheets},
        period_ids=periods or ["p1"],
        financial_sheets=["Summary", "Rooms"],
    )


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        (
            {
                "bindings": [
                    {"period_id": "p1", "sheet_name": "Summary", "excel_column": "B"},
                    {"period_id": "p1", "sheet_name": "Summary", "excel_column": "B"},
                ],
                "unavailable": [
                    {"period_id": "p1", "sheet_name": "Rooms", "reason": "absent"}
                ],
            },
            "2 bindings",
        ),
        (
            {
                "bindings": [
                    {"period_id": "p1", "sheet_name": "Summary", "excel_column": "B"}
                ],
                "unavailable": [
                    {"period_id": "p1", "sheet_name": "Summary", "reason": "absent"},
                    {"period_id": "p1", "sheet_name": "Rooms", "reason": "absent"},
                ],
            },
            "both bindings and unavailable",
        ),
        (
            {
                "bindings": [
                    {"period_id": "p1", "sheet_name": "Excluded", "excel_column": "B"}
                ],
                "unavailable": [
                    {"period_id": "p1", "sheet_name": "Summary", "reason": "absent"},
                    {"period_id": "p1", "sheet_name": "Rooms", "reason": "absent"},
                ],
            },
            "outside the routed financial-sheet scope",
        ),
        (
            {
                "bindings": [
                    {"period_id": "p1", "sheet_name": "Summary", "excel_column": "B"}
                ],
                "unavailable": [
                    {"period_id": "wrong", "sheet_name": "Rooms", "reason": "absent"}
                ],
            },
            "was not one of the chosen periods",
        ),
        (
            {
                "bindings": [
                    {"period_id": "p1", "sheet_name": "Summary", "excel_column": "B"}
                ],
                "unavailable": [
                    {"period_id": "p1", "sheet_name": "Missing", "reason": "absent"}
                ],
            },
            "nonexistent sheet",
        ),
        (
            {
                "bindings": [
                    {"period_id": "p1", "sheet_name": "Summary", "excel_column": "B"}
                ],
                "unavailable": [
                    {"period_id": "p1", "sheet_name": "Excluded", "reason": "absent"}
                ],
            },
            "outside the routed financial-sheet scope",
        ),
    ],
)
def test_binding_verifier_rejects_ambiguous_or_out_of_scope_outcomes(
    payload, message
):
    result = _check(payload)
    assert not result.accepted
    assert any(message in item for item in result.rejections)


def test_binding_adapter_never_creates_a_modal_default_column():
    maps = binding_to_selection_maps(
        WorkbookBindings.model_validate(
            {
                "bindings": [
                    {"period_id": "p1", "sheet_name": "Summary", "excel_column": "B"}
                ],
                "unavailable": [
                    {"period_id": "p1", "sheet_name": "Rooms", "reason": "absent"}
                ],
            }
        ),
        workbook_id="wb_binding_cleanup",
        period_ids=["p1"],
    )
    assert maps["p1"].default_selection is None


def test_exhausted_coverage_retries_fail_closed_without_a_default_column():
    workbook = _workbook()
    toolset = PeriodBindingToolset(
        workbook,
        period_ids=["p1"],
        financial_sheets=["Summary", "Rooms"],
    )
    toolset.dispatch("read_rows", {"sheet_name": "Summary", "start_row": 1})
    payload = {
        "bindings": [
            {"period_id": "p1", "sheet_name": "Summary", "excel_column": "B"}
        ]
    }

    for _ in range(toolset.MAX_COVERAGE_REFUSALS):
        assert not toolset.dispatch("submit_bindings", payload)["accepted"]
    accepted = toolset.dispatch("submit_bindings", payload)

    assert accepted["accepted"]
    structure = WorkbookBindings.model_validate(accepted["structure"])
    assert [(item.sheet_name, item.period_id) for item in structure.unavailable] == [
        ("Rooms", "p1")
    ]
    assert "no column was inferred" in structure.unavailable[0].reason
    maps = binding_to_selection_maps(
        structure,
        workbook_id=workbook.workbook_id,
        period_ids=["p1"],
    )
    assert maps["p1"].default_selection is None
    assert maps["p1"].unavailable_sheets == {
        "Rooms": structure.unavailable[0].reason
    }


def test_explicit_unavailable_zero_schedule_counts_as_complete_coverage():
    result = _check(
        {
            "bindings": [
                {"period_id": "p1", "sheet_name": "Summary", "excel_column": "B"}
            ],
            "unavailable": [
                {
                    "period_id": "p1",
                    "sheet_name": "Rooms",
                    "reason": "Unused outlet template; selected period is blank or all zero.",
                }
            ],
        }
    )

    assert result.accepted


class _PartialFinalClient:
    def generate_json_model_with_tools(self, prompt, response_model, **kwargs):
        return response_model.model_validate(
            {
                "bindings": [
                    {
                        "period_id": "2025-01_2025-12_actual",
                        "sheet_name": "Summary",
                        "excel_column": "B",
                    }
                ]
            }
        )


def test_binding_stage_boundary_rejects_a_partial_provider_final():
    with pytest.raises(RuntimeError, match="final deterministic validation"):
        bind_periods(
            _workbook(),
            client=_PartialFinalClient(),
            periods=[
                PeriodOption(
                    scenario="actual",
                    start_month="2025-01",
                    end_month="2025-12",
                )
            ],
            financial_sheets=["Summary", "Rooms"],
        )


def test_binding_tool_rejects_bare_final_json_before_stage_exit():
    toolset = PeriodBindingToolset(
        _workbook(),
        period_ids=["p1"],
        financial_sheets=["Summary", "Rooms"],
    )

    message = toolset.final_response_error(WorkbookBindings())

    assert "submit_layout_bindings" in message
    assert "bare JSON" in message


def test_salvage_records_an_inspected_empty_column_as_unavailable():
    workbook = _workbook()
    toolset = PeriodBindingToolset(
        workbook,
        period_ids=["p1"],
        financial_sheets=["Summary", "Rooms"],
    )
    for sheet_name in ["Summary", "Rooms"]:
        toolset.dispatch(
            "read_rows", {"sheet_name": sheet_name, "start_row": 1}
        )
    payload = {
        "bindings": [
            {"period_id": "p1", "sheet_name": "Summary", "excel_column": "B"},
            {"period_id": "p1", "sheet_name": "Rooms", "excel_column": "C"},
        ]
    }

    for _ in range(toolset.MAX_REJECTIONS_PER_PHASE):
        assert not toolset.dispatch("submit_bindings", payload)["accepted"]
    accepted = toolset.dispatch("submit_bindings", payload)
    structure = WorkbookBindings.model_validate(accepted["structure"])

    assert accepted["accepted"] is True
    assert len(structure.bindings) == 1
    assert structure.unavailable[0].sheet_name == "Rooms"
    assert "contains no numeric values" in structure.unavailable[0].reason


def test_identical_excel_headers_form_one_compact_layout():
    toolset = PeriodBindingToolset(
        _workbook(),
        period_ids=["p1"],
        financial_sheets=["Summary", "Rooms"],
    )

    result = toolset.dispatch("list_sheet_layouts", {})

    assert result["ok"] is True
    assert len(result["layout_groups"]) == 1
    layout = result["layout_groups"][0]
    assert layout["sheet_names"] == ["Summary", "Rooms"]
    assert any(item["excel_column"] == "B" for item in layout["candidate_columns"])


def test_compact_excel_layout_expands_and_excludes_an_all_zero_member():
    workbook = _workbook()
    workbook.sheets[1] = _sheet("Rooms", value=0.0)
    toolset = PeriodBindingToolset(
        workbook,
        period_ids=["p1"],
        financial_sheets=["Summary", "Rooms"],
    )
    layout = toolset.dispatch("list_sheet_layouts", {})["layout_groups"][0]

    result = toolset.dispatch(
        "submit_layout_bindings",
        {
            "layout_bindings": [
                {
                    "layout_id": layout["layout_id"],
                    "period_id": "p1",
                    "excel_column": "B",
                    "evidence": ["B1 Actual"],
                }
            ],
            "layout_unavailable": [],
        },
    )

    assert result["accepted"] is True
    structure = WorkbookBindings.model_validate(result["structure"])
    assert [(item.sheet_name, item.excel_column) for item in structure.bindings] == [
        ("Summary", "B")
    ]
    assert [(item.sheet_name, item.period_id) for item in structure.unavailable] == [
        ("Rooms", "p1")
    ]
    assert "all zero" in structure.unavailable[0].reason


def test_compact_excel_layout_allows_one_verified_sheet_override():
    workbook = _workbook()
    rooms = workbook.sheets[1]
    rooms.rows[1].cells.append(
        CellRecord(
            row=2,
            column=3,
            address="C2",
            raw_value=200.0,
            display_value="200",
        )
    )
    toolset = PeriodBindingToolset(
        workbook,
        period_ids=["p1"],
        financial_sheets=["Summary", "Rooms"],
    )
    layouts = toolset.dispatch("list_sheet_layouts", {})["layout_groups"]
    assert len(layouts) == 1

    result = toolset.dispatch(
        "submit_layout_bindings",
        {
            "layout_bindings": [
                {
                    "layout_id": layouts[0]["layout_id"],
                    "period_id": "p1",
                    "excel_column": "B",
                }
            ],
            "layout_unavailable": [],
            "sheet_bindings": [
                {
                    "sheet_name": "Rooms",
                    "period_id": "p1",
                    "excel_column": "C",
                }
            ],
        },
    )

    assert result["accepted"] is True
    structure = WorkbookBindings.model_validate(result["structure"])
    assert {(item.sheet_name, item.excel_column) for item in structure.bindings} == {
        ("Summary", "B"),
        ("Rooms", "C"),
    }


def test_compact_excel_requires_one_outcome_per_layout_period():
    toolset = PeriodBindingToolset(
        _workbook(),
        period_ids=["p1", "p2"],
        financial_sheets=["Summary", "Rooms"],
    )
    layout = toolset.dispatch("list_sheet_layouts", {})["layout_groups"][0]

    result = toolset.dispatch(
        "submit_layout_bindings",
        {
            "layout_bindings": [
                {
                    "layout_id": layout["layout_id"],
                    "period_id": "p1",
                    "excel_column": "B",
                }
            ],
            "layout_unavailable": [],
        },
    )

    assert result["accepted"] is False
    assert "requires one compact outcome" in result["error"]


def test_compact_excel_rejects_explicit_scenario_mismatch():
    toolset = PeriodBindingToolset(
        _workbook(),
        period_ids=["2025-01_2025-12_budget"],
        financial_sheets=["Summary", "Rooms"],
    )
    layout = toolset.dispatch("list_sheet_layouts", {})["layout_groups"][0]

    result = toolset.dispatch(
        "submit_layout_bindings",
        {
            "layout_bindings": [
                {
                    "layout_id": layout["layout_id"],
                    "period_id": "2025-01_2025-12_budget",
                    "excel_column": "B",
                }
            ],
            "layout_unavailable": [],
        },
    )

    assert result["accepted"] is False
    assert "explicit scenario hints" in result["error"]


def test_compact_excel_stops_after_two_repairs():
    toolset = PeriodBindingToolset(
        _workbook(),
        period_ids=["p1"],
        financial_sheets=["Summary", "Rooms"],
    )
    toolset.dispatch("list_sheet_layouts", {})
    incomplete = {"layout_bindings": [], "layout_unavailable": []}

    for _ in range(3):
        assert toolset.dispatch("submit_layout_bindings", incomplete)["accepted"] is False
    with pytest.raises(RuntimeError, match="one initial submission and two repairs"):
        toolset.dispatch("submit_layout_bindings", incomplete)


def test_compact_excel_requires_every_period_on_the_controlling_summary():
    toolset = PeriodBindingToolset(
        _workbook(),
        period_ids=["p1"],
        financial_sheets=["Summary", "Rooms"],
        controlling_summary_sheet="Summary",
    )
    layout = toolset.dispatch("list_sheet_layouts", {})["layout_groups"][0]

    result = toolset.dispatch(
        "submit_layout_bindings",
        {
            "layout_bindings": [],
            "layout_unavailable": [
                {
                    "layout_id": layout["layout_id"],
                    "period_id": "p1",
                    "reason": "claimed absent",
                }
            ],
        },
    )

    assert result["accepted"] is False
    assert "controlling summary" in result["error"]
