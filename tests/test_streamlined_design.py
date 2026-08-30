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
from hotel_pl_normalizer.mapping.evidence import compact_workbook_evidence
from hotel_pl_normalizer.mapping.mapper import (
    SourceOperation,
    _detail_collapse_issue,
    _load_coa,
    _primary_prompt,
    _unused_financial_schedule_issues,
)
from hotel_pl_normalizer.models.binding import WorkbookBindings
from hotel_pl_normalizer.models.exploration import WorkbookExploration
from hotel_pl_normalizer.models.period_selection import (
    PeriodColumnSelection,
    PeriodColumnSelectionMap,
    PeriodOption,
    PeriodScenario,
)
from hotel_pl_normalizer.models.sheet_selection import SheetNameSelectionResult
from hotel_pl_normalizer.models.workbook import (
    CellRecord,
    FileType,
    WorkbookMetadata,
    WorkbookRecord,
    WorkbookRow,
    WorkbookSheet,
    WorkbookSource,
)
from hotel_pl_normalizer.pipeline import _mapping_sheet_routing_context
from hotel_pl_normalizer.structure.binding import (
    bind_periods,
    binding_to_selection_maps,
)
from hotel_pl_normalizer.structure.binding.toolset import PeriodBindingToolset
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
        self.assertIn("Financial content wins", prompt)
        self.assertIn("use detailed subschedules where they support COA children", prompt)
        self.assertNotIn("Reservations statistics", prompt)
        self.assertIn("`include_as_financial_evidence=true`", prompt)
        self.assertIn("`payroll_statistics`", prompt)
        self.assertIn("included\n`unknown` role conservatively", prompt)
        self.assertNotIn("`defer`", prompt)
        self.assertNotIn("`skip`", prompt)
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
                        "include_as_financial_evidence": True,
                        "role": "summary_p_and_l",
                        "confidence": "high",
                        "evidence": ["Statement of income title"],
                    }
                ],
            },
        )
        self.assertTrue(routed["accepted"])
        self.assertIn("next_phase", routed)
        self.assertIn("Phase two", routed["next_phase"])
        self.assertIn(
            "Anchor periods, then record sheet coverage", routed["next_phase"]
        )
        self.assertIn("Do not delete a valid", routed["next_phase"])
        self.assertNotIn("Return the intersection", routed["next_phase"])

    def test_auxiliary_t12_summary_cannot_expand_controlling_periods(self) -> None:
        names = ["Summary", "Rooms", "F&B", "A&G", "Engineering", "T12"]

        class HeaderWorkbook:
            path = Path("carmel.xlsx")

            @staticmethod
            def sheets():
                return [SimpleNamespace(sheet_name=name) for name in names]

            @staticmethod
            def read_rows(sheet_name, start, end):
                return []

            @staticmethod
            def merged_ranges(sheet_name):
                return []

        toolset = WorkbookExplorationToolset(HeaderWorkbook())
        routing = {
            "workbook_layout": "multi_tab_department_p_and_l",
            "sheets": [
                {
                    "sheet_name": name,
                    "include_as_financial_evidence": True,
                    "role": (
                        "summary_p_and_l"
                        if name in {"Summary", "T12"}
                        else "department_p_and_l"
                    ),
                    "confidence": "high",
                    "evidence": ["financial header"],
                }
                for name in names
            ],
        }
        assert toolset.dispatch("submit_routing", routing)["accepted"]
        for name in ["Summary", "Rooms", "F&B", "A&G"]:
            toolset.dispatch("read_rows", {"sheet_name": name, "start_row": 1})

        core_period = {
            "scenario": "actual",
            "start_month": "2025-12",
            "end_month": "2025-12",
            "sheets_present": ["Summary", "Rooms", "F&B", "A&G", "Engineering"],
            "evidence": ["Summary December Actual"],
        }
        incomplete = toolset.dispatch(
            "submit_periods",
            {
                "controlling_summary_sheet": "Summary",
                "periods": [core_period],
            },
        )
        self.assertFalse(incomplete["accepted"])
        self.assertIn("4", incomplete["error"])

        toolset.dispatch(
            "read_rows", {"sheet_name": "Engineering", "start_row": 1}
        )
        toolset.dispatch("read_rows", {"sheet_name": "T12", "start_row": 1})
        with_t12_month = toolset.dispatch(
            "submit_periods",
            {
                "controlling_summary_sheet": "Summary",
                "periods": [
                    core_period,
                    {
                        "scenario": "actual",
                        "start_month": "2025-05",
                        "end_month": "2025-05",
                        "sheets_present": ["T12"],
                        "evidence": ["T12 May Actual"],
                    },
                ],
            },
        )
        self.assertFalse(with_t12_month["accepted"])
        self.assertIn("auxiliary T12", with_t12_month["error"])

        accepted = toolset.dispatch(
            "submit_periods",
            {
                "controlling_summary_sheet": "Summary",
                "periods": [core_period],
            },
        )
        self.assertTrue(accepted["accepted"])
        structure = accepted["structure"]
        self.assertEqual(structure["controlling_summary_sheet"], "Summary")
        self.assertEqual(len(structure["periods"]), 1)

    def test_excel_monthly_controlling_summary_cannot_collapse_to_ttm_only(self) -> None:
        names = ["Summary", "Rooms", "F&B", "A&G", "Engineering"]
        month_labels = [
            "Oct 2024", "Nov 2024", "Dec 2024", "Jan 2025", "Feb 2025",
            "Mar 2025", "Apr 2025", "May 2025", "Jun 2025", "Jul 2025",
            "Aug 2025", "Sep 2025",
        ]

        class MonthlyWorkbook:
            path = Path("monthly.xlsx")

            @staticmethod
            def sheets():
                return [SimpleNamespace(sheet_name=name) for name in names]

            @staticmethod
            def read_rows(sheet_name, start, end):
                if sheet_name != "Summary":
                    return []
                return [[
                    SimpleNamespace(coordinate=f"{chr(66 + index)}4", value=value)
                    for index, value in enumerate(month_labels)
                ]]

            @staticmethod
            def merged_ranges(sheet_name):
                return []

        toolset = WorkbookExplorationToolset(MonthlyWorkbook())
        routing = {
            "workbook_layout": "multi_tab_department_p_and_l",
            "sheets": [
                {
                    "sheet_name": name,
                    "include_as_financial_evidence": True,
                    "role": "summary_p_and_l" if name == "Summary" else "department_p_and_l",
                    "confidence": "high",
                    "evidence": ["financial header"],
                }
                for name in names
            ],
        }
        self.assertTrue(toolset.dispatch("submit_routing", routing)["accepted"])
        for name in names:
            toolset.dispatch("read_rows", {"sheet_name": name, "start_row": 1})

        ttm = {
            "scenario": "actual",
            "start_month": "2024-10",
            "end_month": "2025-09",
            "sheets_present": names,
            "evidence": ["Summary total"],
        }
        collapsed = toolset.dispatch(
            "submit_periods",
            {"controlling_summary_sheet": "Summary", "periods": [ttm]},
        )
        self.assertFalse(collapsed["accepted"])
        self.assertIn("2024-10", collapsed["error"])
        self.assertIn("2025-09", collapsed["error"])

        months = []
        for year, month in [(2024, value) for value in range(10, 13)] + [
            (2025, value) for value in range(1, 10)
        ]:
            value = f"{year:04d}-{month:02d}"
            months.append(
                {
                    "scenario": "actual",
                    "start_month": value,
                    "end_month": value,
                    "sheets_present": names,
                    "evidence": [f"Summary {value}"],
                }
            )
        complete = toolset.dispatch(
            "submit_periods",
            {
                "controlling_summary_sheet": "Summary",
                "periods": [*months, ttm],
            },
        )
        self.assertTrue(complete["accepted"])
        self.assertEqual(len(complete["structure"]["periods"]), 13)

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
                                "period_id": "2025-01_2025-12_actual",
                                "sheet_name": "P&L",
                                "excel_column": "B",
                            }
                        ]
                    },
                )
                self.assert_accepted = result["accepted"]
                return response_model.model_validate(result["structure"])

        client = ScriptedClient()
        periods = [
            PeriodOption(
                period_id="2025-01_2025-12_actual",
                label="2025 Actual",
                scenario=PeriodScenario.ACTUAL,
                start_month="2025-01",
                end_month="2025-12",
            )
        ]
        output = bind_periods(
            _record(),
            client=client,
            periods=periods,
            financial_sheets=["P&L"],
        )
        maps = binding_to_selection_maps(
            output.structure,
            workbook_id="wb_test",
            period_ids=["2025-01_2025-12_actual"],
            period_labels={"2025-01_2025-12_actual": "2025 Actual"},
        )
        self.assertTrue(client.assert_accepted)
        self.assertNotIn("department_hints", client.prompt)
        self.assertIn("scenario=actual", client.prompt)
        self.assertIn("coverage=2025-01 through 2025-12", client.prompt)
        self.assertEqual(
            maps["2025-01_2025-12_actual"].sheet_selections[0].value_column,
            2,
        )

    def test_explicitly_unavailable_sheet_never_uses_default_column(self) -> None:
        workbook = _record()
        workbook.sheets.append(
            WorkbookSheet(
                sheet_id="sheet_unavailable",
                sheet_name="Unavailable",
                max_row=1,
                max_column=2,
                rows=[
                    WorkbookRow(
                        row_index=1,
                        cells=[
                            CellRecord(
                                row=1,
                                column=1,
                                address="A1",
                                raw_value="Rooms Revenue",
                                display_value="Rooms Revenue",
                            ),
                            CellRecord(
                                row=1,
                                column=2,
                                address="B1",
                                raw_value=999.0,
                                display_value="999",
                            ),
                        ],
                    )
                ],
            )
        )
        structure = WorkbookBindings.model_validate(
            {
                "bindings": [
                    {
                        "period_id": "p1",
                        "sheet_name": "P&L",
                        "excel_column": "B",
                    }
                ],
                "unavailable": [
                    {
                        "period_id": "p1",
                        "sheet_name": "Unavailable",
                        "reason": "No 2025 annual column",
                    }
                ],
            }
        )
        maps = binding_to_selection_maps(
            structure,
            workbook_id="wb_test",
            period_ids=["p1"],
            period_labels={"p1": "2025 Actual"},
        )

        self.assertEqual(
            maps["p1"].unavailable_sheets,
            {"Unavailable": "No 2025 annual column"},
        )
        evidence = compact_workbook_evidence(
            workbook,
            maps,
            include_sheets={"P&L", "Unavailable"},
        )
        self.assertTrue(evidence)
        self.assertFalse(
            any(item["row_key"].startswith("Unavailable!") for item in evidence)
        )

    def test_mapper_prompt_carries_routing_without_department_ids(self) -> None:
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
            [
                {
                    "sheet_name": "P&L",
                    "include_as_financial_evidence": True,
                    "role": "summary_p_and_l",
                    "confidence": "high",
                    "evidence": ["Statement of income title"],
                }
            ],
        )
        self.assertNotIn("period_binding_department_hints", prompt)
        self.assertNotIn("department_hints", prompt)
        self.assertNotIn("row_boundaries", prompt)
        self.assertIn('"period_columns"', prompt)
        self.assertIn('"sheet_routing_context"', prompt)
        self.assertIn('"role":"summary_p_and_l"', prompt)
        self.assertIn('"include_as_financial_evidence":true', prompt)
        self.assertIn('"confidence":"high"', prompt)
        self.assertIn("Statement of income title", prompt)
        self.assertIn('"workbook_rows"', prompt)
        self.assertIn(
            "not supporting or duplicate merely because a consolidated row",
            prompt,
        )
        self.assertIn(
            "Using a consolidated total as a reconciliation control while mapping disjoint",
            prompt,
        )
        self.assertIn(
            "financial subschedule merely because its total is represented",
            prompt,
        )

    def test_mapping_context_keeps_only_included_financial_evidence(self) -> None:
        selection = SheetNameSelectionResult.model_validate(
            {
                "workbook_id": "wb_test",
                "selections": [
                    {
                        "sheet_name": "Summary",
                        "include_as_financial_evidence": True,
                        "role": "summary_p_and_l",
                        "confidence": "high",
                        "evidence": ["Statement totals"],
                    },
                    {
                        "sheet_name": "Reservations",
                        "include_as_financial_evidence": True,
                        "role": "department_p_and_l",
                        "confidence": "high",
                        "evidence": ["Payroll and operating expense totals"],
                    },
                    {
                        "sheet_name": "Unknown",
                        "include_as_financial_evidence": True,
                        "role": "unknown",
                        "confidence": "uncertain",
                        "evidence": ["Ambiguous schedule"],
                    },
                    {
                        "sheet_name": "Checks",
                        "include_as_financial_evidence": False,
                        "role": "other",
                        "confidence": "high",
                        "evidence": ["Formula audit"],
                    },
                ],
            }
        )

        context = _mapping_sheet_routing_context(selection)

        self.assertEqual(
            [item["sheet_name"] for item in context],
            ["Summary", "Reservations", "Unknown"],
        )
        self.assertEqual(context[1]["role"], "department_p_and_l")
        self.assertEqual(context[2]["confidence"], "uncertain")
        self.assertEqual(
            context[1]["evidence"], ["Payroll and operating expense totals"]
        )

    def test_sheet_routing_rejects_inclusion_role_contradictions(self) -> None:
        base = {
            "workbook_id": "wb_test",
            "selections": [
                {
                    "sheet_name": "P&L",
                    "include_as_financial_evidence": True,
                    "role": "summary_p_and_l",
                    "confidence": "high",
                    "evidence": [],
                }
            ],
        }
        accepted = SheetNameSelectionResult.model_validate(base)
        self.assertEqual(accepted.included_sheet_names, ["P&L"])

        contradictory = json.loads(json.dumps(base))
        contradictory["selections"][0]["role"] = "balance_sheet"
        with self.assertRaisesRegex(ValidationError, "incompatible"):
            SheetNameSelectionResult.model_validate(contradictory)

        contradictory = json.loads(json.dumps(base))
        contradictory["selections"][0]["include_as_financial_evidence"] = False
        with self.assertRaisesRegex(ValidationError, "incompatible"):
            SheetNameSelectionResult.model_validate(contradictory)

    def test_unused_department_schedule_must_be_cited_or_documented(self) -> None:
        evidence = [
            {
                "row_key": "Reservations!20",
                "label": "Total Payroll",
                "selected_values": {"p1": 200.0},
            },
            {
                "row_key": "Reservations!40",
                "label": "Total Operating Expense",
                "selected_values": {"p1": 360.0},
            },
        ]
        routing = [
            {
                "sheet_name": "Reservations",
                "include_as_financial_evidence": True,
                "role": "department_p_and_l",
                "confidence": "high",
                "evidence": ["Selected-period payroll and opex totals"],
            },
            {
                "sheet_name": "Rooms Detail",
                "include_as_financial_evidence": True,
                "role": "department_p_and_l",
                "confidence": "high",
                "evidence": ["Controlling Rooms detail schedule"],
            },
        ]

        def plan(*, cited=False, note=None):
            return SimpleNamespace(
                decisions=[
                    SimpleNamespace(
                        source_rows=["Reservations!20"] if cited else [],
                        excluded_rows=[],
                    )
                ],
                strategy=SimpleNamespace(
                    duplicate_or_supporting_schedules=[note] if note else []
                ),
            )

        issues = _unused_financial_schedule_issues(
            plan(), evidence, routing
        )
        self.assertEqual(len(issues), 1)
        self.assertIn("error|unused_financial_schedule|Reservations|", issues[0])
        self.assertIn("Reservations!20", issues[0])
        self.assertEqual(
            _unused_financial_schedule_issues(
                plan(cited=True), evidence, routing
            ),
            [],
        )
        incomplete_note = _unused_financial_schedule_issues(
            plan(note="Reservations — duplicate schedule"), evidence, routing
        )
        self.assertEqual(len(incomplete_note), 1)
        self.assertEqual(
            _unused_financial_schedule_issues(
                plan(
                    note=(
                        "Reservations — supporting duplicate; superseded by "
                        "Rooms Detail"
                    )
                ),
                evidence,
                routing,
            ),
            [],
        )

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
