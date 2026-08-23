from __future__ import annotations

import json
import sys
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timezone
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from pydantic import ValidationError

from hotel_pl_normalizer.cli import main as cli_main
from hotel_pl_normalizer.mapping.mapper import (
    SourceOperation,
    _detail_collapse_issue,
    _load_coa,
    _primary_prompt,
)
from hotel_pl_normalizer.models.binding import WorkbookBindings
from hotel_pl_normalizer.models.exploration import WorkbookExploration
from hotel_pl_normalizer.models.period_selection import (
    PeriodColumnSelection,
    PeriodColumnSelectionMap,
)
from hotel_pl_normalizer.models.workbook import (
    CellRecord,
    FileType,
    WorkbookMetadata,
    WorkbookRecord,
    WorkbookRow,
    WorkbookSheet,
    WorkbookSource,
)
from hotel_pl_normalizer.structure.binding.toolset import PeriodBindingToolset
from hotel_pl_normalizer.structure.binding import (
    bind_periods,
    binding_to_selection_maps,
)
from hotel_pl_normalizer.structure.exploration.agent import render_exploration_prompt
from hotel_pl_normalizer.structure.exploration.toolset import (
    WorkbookExplorationToolset,
)


def _record() -> WorkbookRecord:
    rows = [
        WorkbookRow(
            row_index=index,
            cells=[
                CellRecord(
                    row=index,
                    column=1,
                    address=f"A{index}",
                    raw_value=f"Line {index}",
                    display_value=f"Line {index}",
                ),
                CellRecord(
                    row=index,
                    column=2,
                    address=f"B{index}",
                    raw_value=float(index),
                    display_value=str(index),
                ),
            ],
        )
        for index in range(1, 6)
    ]
    return WorkbookRecord(
        workbook_id="wb_test",
        source=WorkbookSource(
            source_id="src_test",
            original_filename="test.xlsx",
            file_type=FileType.XLSX,
            ingested_at=datetime(2026, 8, 23, tzinfo=timezone.utc),
        ),
        workbook_metadata=WorkbookMetadata(sheet_count=1),
        sheets=[
            WorkbookSheet(
                sheet_id="sheet_test",
                sheet_name="P&L",
                max_row=5,
                max_column=2,
                rows=rows,
            )
        ],
    )


class StreamlinedDesignTests(unittest.TestCase):
    def test_cli_has_no_evaluation_only_routes(self) -> None:
        stdout = StringIO()
        with patch.object(sys, "argv", ["hotel-pl-normalizer", "--help"]):
            with redirect_stdout(stdout), self.assertRaises(SystemExit) as stopped:
                cli_main()
        self.assertEqual(stopped.exception.code, 0)
        help_text = stdout.getvalue()
        self.assertNotIn("--discovery-run", help_text)
        self.assertNotIn("--discover-only", help_text)

    def test_removed_model_members_stay_removed(self) -> None:
        selection_schema = PeriodColumnSelection.model_json_schema()
        self.assertNotIn("warnings", selection_schema["properties"])
        self.assertFalse(hasattr(WorkbookExploration, "financial_sheet_names"))

    def test_exploration_uses_one_current_prompt(self) -> None:
        prompt = render_exploration_prompt("test.xlsx")
        self.assertIn("two phases, in order", prompt)
        self.assertIn("submit_routing", prompt)
        self.assertIn("submit_periods", prompt)
        self.assertIn("Guest Laundry", prompt)
        self.assertNotIn("Sheet Name Triage Skill appended", prompt)
        self.assertNotIn("department_candidates", prompt)
        self.assertNotIn("needs_sheet_enrichment", prompt)
        self.assertNotIn("SheetNameSelectionResult", prompt)

    def test_exploration_keeps_routing_as_a_phase_gate(self) -> None:
        workbook = SimpleNamespace(
            path=Path("test.xlsx"),
            sheets=lambda: [SimpleNamespace(sheet_name="P&L")],
        )
        toolset = WorkbookExplorationToolset(workbook)

        premature = toolset.dispatch("submit_periods", {"periods": []})
        self.assertFalse(premature["ok"])
        self.assertIn("Routing has not been submitted", premature["error"])

        routed = toolset.dispatch(
            "submit_routing",
            {
                "workbook_layout": "single_tab_p_and_l",
                "sheets": [
                    {
                        "sheet_name": "P&L",
                        "decision": "triage",
                        "role_hint": "summary_p_and_l",
                        "evidence": ["Statement of income title"],
                    }
                ],
            },
        )
        self.assertTrue(routed["accepted"])
        self.assertIn("next_phase", routed)
        self.assertIn("Phase two", routed["next_phase"])

    def test_binding_schema_has_no_department_contract(self) -> None:
        schema = json.dumps(WorkbookBindings.model_json_schema()).lower()
        self.assertNotIn("department", schema)
        with self.assertRaises(ValidationError):
            WorkbookBindings.model_validate({"sheet_classifications": []})

    def test_binding_toolset_has_one_submission_and_accepts_it(self) -> None:
        toolset = PeriodBindingToolset(
            _record(), period_ids=["p1"], financial_sheets=["P&L"]
        )
        names = {item["name"] for item in toolset.declarations()}
        self.assertIn("submit_bindings", names)
        self.assertNotIn("submit_departments", names)

        toolset.dispatch("read_rows", {"sheet_name": "P&L", "start_row": 1})
        result = toolset.dispatch(
            "submit_bindings",
            {
                "bindings": [
                    {
                        "period_id": "p1",
                        "sheet_name": "P&L",
                        "excel_column": "B",
                    }
                ]
            },
        )
        self.assertTrue(result["accepted"])
        self.assertTrue(
            WorkbookBindings.model_validate(result["structure"]).bindings
        )

    def test_scripted_binding_reaches_the_period_selection_map(self) -> None:
        class ScriptedClient:
            def generate_json_model_with_tools(
                self, prompt, response_model, *, toolset, **_
            ):
                self.prompt = prompt
                toolset.dispatch(
                    "read_rows", {"sheet_name": "P&L", "start_row": 1}
                )
                result = toolset.dispatch(
                    "submit_bindings",
                    {
                        "bindings": [
                            {
                                "period_id": "p1",
                                "sheet_name": "P&L",
                                "excel_column": "B",
                            }
                        ]
                    },
                )
                self.assert_accepted = result["accepted"]
                return response_model.model_validate(result["structure"])

        client = ScriptedClient()
        output = bind_periods(
            _record(),
            client=client,
            period_ids=["p1"],
            period_labels={"p1": "2025 Actual"},
            financial_sheets=["P&L"],
        )
        maps = binding_to_selection_maps(
            output.structure,
            workbook_id="wb_test",
            period_ids=["p1"],
            period_labels={"p1": "2025 Actual"},
        )
        self.assertTrue(client.assert_accepted)
        self.assertNotIn("department_hints", client.prompt)
        self.assertEqual(maps["p1"].sheet_selections[0].value_column, 2)

    def test_mapper_prompt_has_no_department_handoff(self) -> None:
        periods = {
            "p1": PeriodColumnSelectionMap(
                selection_map_id="selection_test",
                workbook_id="wb_test",
                requested_period="2025 Actual",
                sheet_selections=[
                    PeriodColumnSelection(
                        sheet_name="P&L",
                        value_column=2,
                        excel_column="B",
                        period_label="2025 Actual",
                    )
                ],
            )
        }
        prompt = _primary_prompt(
            "wb_test",
            "2025 Actual",
            periods,
            {"p1": "2025 Actual"},
            [
                {
                    "row_key": "P&L!1",
                    "label": "Rooms Revenue",
                    "selected_values": {"p1": 100.0},
                }
            ],
            _load_coa(),
            [],
        )
        self.assertNotIn("period_binding_department_hints", prompt)
        self.assertNotIn("department_hints", prompt)
        self.assertIn('"period_columns"', prompt)
        self.assertIn('"workbook_rows"', prompt)

    def test_detail_collapse_guard_needs_no_department_metadata(self) -> None:
        evidence = [
            {
                "row_key": f"Detail!{index}",
                "label": f"Line {index}",
                "selected_values": {"p1": float(index)},
            }
            for index in range(1, 121)
        ]

        def plan(count: int):
            return SimpleNamespace(
                decisions=[
                    SimpleNamespace(
                        coa_id=f"S1.test_detail_{index}",
                        operation=SourceOperation.DIRECT,
                        source_rows=[f"Detail!{index}"],
                    )
                    for index in range(1, count + 1)
                ]
            )

        self.assertIn(
            "detail_mapping_collapsed", _detail_collapse_issue(plan(5), evidence)
        )
        self.assertIsNone(_detail_collapse_issue(plan(12), evidence))


if __name__ == "__main__":
    unittest.main()
