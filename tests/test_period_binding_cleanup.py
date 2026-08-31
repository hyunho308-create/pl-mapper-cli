from datetime import datetime, timezone

import pytest

from hotel_pl_normalizer.models.binding import WorkbookBindings
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
