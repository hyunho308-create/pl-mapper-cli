from __future__ import annotations

import unittest

from hotel_pl_normalizer.mapping.mapper import (
    AccountSourceDecision,
    MappingOutcome,
    MappingReviewItem,
    SourceOperation,
    SourceLayerOperation,
    WorkbookMappingValidator,
    WorkbookSourcePlan,
    WorkbookStrategy,
    _outcome_from_result,
    _execute,
    _period_completeness_issues,
    _review_item_blockers,
    _review_item_warnings,
    _same_blocking_conflicts,
    _source_layer_conflict_warnings,
    _structured_exceptions,
    _unsupported_residual_remainder_warnings,
    _validation_score,
)
from hotel_pl_normalizer.pipeline import NormalizationResult
from hotel_pl_normalizer.run_log import build_run_log


class MappingOutcomeTests(unittest.TestCase):
    def test_patch_can_document_an_intentionally_unused_schedule(self) -> None:
        validator = WorkbookMappingValidator("wb", [], {"S1.test": {}})
        validator.current_plan = WorkbookSourcePlan(
            plan_id="initial",
            workbook_id="wb",
            strategy=WorkbookStrategy(
                reporting_layout="multi-tab",
                summary_source="Summary",
            ),
            decisions=[
                AccountSourceDecision(
                    coa_id="S1.test",
                    operation=SourceOperation.NO_VALUE,
                )
            ],
        )

        repaired, _ = validator._apply_patch(
            {
                "patch_id": "repair",
                "workbook_id": "wb",
                "replacements": [],
                "repair_hypothesis": "The schedule is a duplicate.",
                "expected_fix": "Document the controlling schedule.",
                "duplicate_or_supporting_schedules": [
                    "Reservations — duplicate; superseded by Rooms Detail"
                ],
            }
        )

        self.assertEqual(
            repaired.strategy.duplicate_or_supporting_schedules,
            ["Reservations — duplicate; superseded by Rooms Detail"],
        )
        patch_schema = next(
            item for item in validator.declarations() if item["name"] == "patch_mapping"
        )["parameters"]["properties"]
        self.assertIn("duplicate_or_supporting_schedules", patch_schema)

    def test_outcomes_distinguish_clean_source_coverage_and_rejection(self) -> None:
        clean = {"accepted": True, "errors": [], "warnings": []}
        source = {
            "accepted": True,
            "errors": [],
            "warnings": [
                "Actual: warning|source_discrepancy|S12.it|"
                "actual=59|expected=3117|variance=-3058"
            ],
        }
        coverage = {
            "accepted": True,
            "errors": [],
            "warnings": [
                "Actual: warning|source_detail_incomplete|S1.labor|"
                "parent=100|children=80|variance=20"
            ],
        }
        rejected = {
            "accepted": False,
            "errors": ["Actual: error|summary_math|S12.gop|variance=10"],
            "warnings": [],
        }

        self.assertEqual(_outcome_from_result(clean, []), MappingOutcome.CLEAN)
        self.assertEqual(
            _outcome_from_result(source, []), MappingOutcome.SOURCE_EXCEPTION
        )
        self.assertEqual(
            _outcome_from_result(coverage, []), MappingOutcome.COVERAGE_GAP
        )
        self.assertEqual(
            _outcome_from_result(rejected, []), MappingOutcome.REJECTED
        )

    def test_scope_exception_has_explicit_blocking_outcome(self) -> None:
        review = MappingReviewItem(
            kind="scope_exception",
            message="Operator direction is required before including this item.",
            coa_ids=["S11.other"],
            requires_human_decision=True,
        )

        blockers = _review_item_blockers([review])
        outcome = _outcome_from_result(
            {"accepted": False, "errors": blockers, "warnings": []}, [review]
        )

        self.assertEqual(outcome, MappingOutcome.SCOPE_EXCEPTION)
        self.assertEqual(len(blockers), 1)
        self.assertIn("error|scope_exception|S11.other|", blockers[0])

    def test_supported_scope_exclusion_warns_without_blocking(self) -> None:
        review = MappingReviewItem(
            kind="scope_exception",
            message="The club statement is a separately reported entity.",
            coa_ids=["S11.other"],
            source_rows=["Club!10"],
            requires_human_decision=False,
        )

        self.assertEqual(_review_item_blockers([review]), [])
        warnings = _review_item_warnings([review])
        self.assertEqual(len(warnings), 1)
        self.assertIn("warning|scope_exclusion|S11.other|", warnings[0])
        self.assertEqual(
            _outcome_from_result(
                {"accepted": True, "errors": [], "warnings": warnings},
                [review],
            ),
            MappingOutcome.SOURCE_EXCEPTION,
        )

    def test_source_exception_does_not_consume_coverage_cleanup_turn(self) -> None:
        validator = WorkbookMappingValidator("wb", [], {})
        result = {
            "accepted": True,
            "validation_attempt": 2,
            "warnings": [
                "Actual: warning|source_discrepancy|S12.it|"
                "actual=59|expected=3117|variance=-3058"
            ],
            "warning_count": 1,
            "review_items": [],
        }

        completion = validator.terminal_result("patch_mapping", result)

        self.assertEqual(completion["status"], "accepted")
        self.assertEqual(completion["outcome"], "source_exception")
        self.assertFalse(validator.warning_cleanup_pending)

    def test_coverage_gap_gets_one_focused_cleanup_turn(self) -> None:
        validator = WorkbookMappingValidator("wb", [], {})
        result = {
            "accepted": True,
            "validation_attempt": 3,
            "warnings": [
                "Actual: warning|source_detail_incomplete|S1.labor|"
                "parent=100|children=80|variance=20"
            ],
            "warning_count": 1,
            "review_items": [],
        }

        completion = validator.terminal_result("patch_mapping", result)

        self.assertIsNone(completion)
        self.assertTrue(validator.warning_cleanup_pending)
        self.assertEqual(validator.warning_cleanup_outcome, "offered")

    def test_identical_quantified_conflict_stops_only_with_same_evidence(self) -> None:
        finding = (
            "Actual: error|summary_math|S12.gop|actual=90|expected=100|"
            "variance=-10|equation=S12.departmental_profit-S12.undistributed"
        )
        previous = {
            "errors": [finding],
            "findings": [
                {
                    "rule": "summary_math",
                    "coa_ids": ["S12.gop"],
                    "source_rows": ["Summary!40"],
                }
            ],
        }
        unchanged = {
            "errors": [finding],
            "findings": list(previous["findings"]),
        }
        new_evidence = {
            "errors": [finding],
            "findings": [
                {
                    "rule": "summary_math",
                    "coa_ids": ["S12.gop"],
                    "source_rows": ["Summary!40", "Summary!41"],
                }
            ],
        }

        self.assertTrue(_same_blocking_conflicts(previous, unchanged))
        self.assertFalse(_same_blocking_conflicts(previous, new_evidence))

    def test_structured_source_exception_records_both_values_and_treatment(self) -> None:
        review = MappingReviewItem(
            kind="source_discrepancy",
            message="Preserve the independently reported Summary and IT values.",
            coa_ids=["S12.it", "S6.it"],
            source_rows=["Summary!59", "IT!3117"],
        )
        checks = {
            "actual": [
                "warning|source_discrepancy|S12.it|actual=59|expected=3117|"
                "variance=-3058|equation=S6.it"
            ]
        }

        records = _structured_exceptions(
            checks, {"actual": "2025 Actual"}, [review]
        )

        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["reported_value"], 59.0)
        self.assertEqual(records[0]["comparison_value"], 3117.0)
        self.assertEqual(records[0]["variance"], -3058.0)
        self.assertEqual(records[0]["source_rows"], ["Summary!59", "IT!3117"])
        self.assertIn("Preserve", records[0]["treatment"])

    def test_checkpoint_ranking_never_prefers_an_empty_plan(self) -> None:
        empty = {
            "missing_decision_count": 271,
            "errors": [],
            "warnings": [],
        }
        substantive = {
            "missing_decision_count": 0,
            "errors": [
                "Actual: error|summary_math|S12.gop|actual=90|expected=100|"
                "variance=-10"
            ],
            "warnings": [],
        }

        self.assertLess(_validation_score(substantive), _validation_score(empty))

    def test_cited_alternate_subtotal_becomes_structured_source_conflict(self) -> None:
        target = "S11.total_non_operating_income_and_expenses"
        plan = WorkbookSourcePlan(
            plan_id="layer-conflict",
            workbook_id="wb",
            strategy=WorkbookStrategy(
                reporting_layout="test",
                summary_source="test",
            ),
            decisions=[
                AccountSourceDecision(
                    coa_id=target,
                    operation=SourceOperation.SUM,
                    source_rows=["NonOp!10", "NonOp!11"],
                )
            ],
            review_items=[
                MappingReviewItem(
                    kind="source_discrepancy",
                    message="Use the supported components; preserve the alternate subtotal.",
                    coa_ids=[target],
                    source_rows=[
                        "NonOp!10",
                        "NonOp!11",
                        "NonOp!20",
                        "NonOp!21",
                    ],
                    selected_source_rows=["NonOp!10", "NonOp!11"],
                    alternate_source_rows=["NonOp!20", "NonOp!21"],
                    selected_source_operation=SourceLayerOperation.SUM,
                    alternate_source_operation=SourceLayerOperation.SUM,
                )
            ],
        )
        evidence = [
            {"row_key": "NonOp!10", "selected_values": {"actual": 40}},
            {"row_key": "NonOp!11", "selected_values": {"actual": 60}},
            {"row_key": "NonOp!20", "selected_values": {"actual": 70}},
            {"row_key": "NonOp!21", "selected_values": {"actual": 55}},
        ]

        warnings = _source_layer_conflict_warnings(
            plan, evidence, {target: 100.0}, "actual"
        )

        self.assertEqual(len(warnings), 1)
        self.assertIn("warning|source_layer_conflict|", warnings[0])
        self.assertIn("actual=100.0000|expected=125.0000|variance=-25.0000", warnings[0])

    def test_ffe_and_noi_are_calculated_without_model_decisions(self) -> None:
        coa = {
            "S12.total_revenue": {},
            "S12.ebitda": {},
            "S12.ffe_reserve": {},
            "S12.noi": {},
        }
        decisions = [
            AccountSourceDecision(
                coa_id="S12.total_revenue",
                operation=SourceOperation.DIRECT,
                source_rows=["Summary!10"],
            ),
            AccountSourceDecision(
                coa_id="S12.ebitda",
                operation=SourceOperation.DIRECT,
                source_rows=["Summary!20"],
            ),
        ]
        evidence = [
            {"row_key": "Summary!10", "selected_values": {"actual": 1_000_000}},
            {"row_key": "Summary!20", "selected_values": {"actual": 150_000}},
        ]

        values, issues = _execute(
            decisions,
            evidence,
            coa,
            period_id="actual",
            preserve_blanks=True,
        )

        self.assertEqual(issues, [])
        self.assertEqual(values["S12.ffe_reserve"], 40_000)
        self.assertEqual(values["S12.noi"], 110_000)

    def test_run_log_records_deterministic_accounts_as_calculations(self) -> None:
        coa = {
            "S12.total_revenue": {"account_name": "Total Revenue"},
            "S12.ebitda": {"account_name": "EBITDA"},
            "S12.ffe_reserve": {"account_name": "FF&E Reserve"},
            "S12.noi": {"account_name": "NOI"},
        }
        values = {
            "S12.total_revenue": 1_000_000.0,
            "S12.ebitda": 150_000.0,
            "S12.ffe_reserve": 40_000.0,
            "S12.noi": 110_000.0,
        }
        result = NormalizationResult(
            workbook_id="wb",
            source_name="Hotel.xlsx",
            period_label="2025 Actual",
            period_labels={"actual": "2025 Actual"},
            values=values,
            period_values={"actual": values},
            coa=coa,
            decisions=[
                AccountSourceDecision(
                    coa_id="S12.total_revenue",
                    operation=SourceOperation.DIRECT,
                    source_rows=["Summary!10"],
                ),
                AccountSourceDecision(
                    coa_id="S12.ebitda",
                    operation=SourceOperation.DIRECT,
                    source_rows=["Summary!20"],
                ),
            ],
            accepted=True,
        )

        log = build_run_log(result)
        calculated = {
            item["coa_id"]: item
            for item in log["accounts"]
            if item["operation"] == "calculated"
        }

        self.assertEqual(log["outcome"]["accounts_calculated_deterministically"], 2)
        self.assertEqual(log["outcome"]["accounts_with_a_decision"], 2)
        self.assertEqual(log["outcome"]["accounts_without_a_decision"], 0)
        self.assertEqual(log["accounts_without_a_decision"], [])
        self.assertEqual(
            calculated["S12.ffe_reserve"]["formula"],
            "0.04 * S12.total_revenue",
        )
        self.assertEqual(
            calculated["S12.noi"]["dependencies"],
            ["S12.ebitda", "S12.ffe_reserve"],
        )

    def test_period_complete_equivalent_row_blocks_incomplete_selection(self) -> None:
        plan = WorkbookSourcePlan(
            plan_id="period-coverage",
            workbook_id="wb",
            strategy=WorkbookStrategy(reporting_layout="test", summary_source="Summary"),
            decisions=[
                AccountSourceDecision(
                    coa_id="S1.test",
                    operation=SourceOperation.DIRECT,
                    source_rows=["CurrentOnly!10"],
                )
            ],
        )
        evidence = [
            {
                "row_key": "CurrentOnly!10",
                "label": "Reservations Payroll",
                "selected_values": {"current": 100.0, "prior": None},
            },
            {
                "row_key": "Complete!10",
                "label": "Reservations Payroll",
                "selected_values": {"current": 100.0, "prior": 90.0},
            },
        ]

        issues = _period_completeness_issues(
            plan,
            evidence,
            {"current": "2025 Actual", "prior": "2024 Actual"},
        )

        self.assertEqual(len(issues), 1)
        self.assertIn("error|period_detail_available|S1.test|", issues[0])
        self.assertIn("candidate_row=Complete!10", issues[0])

    def test_material_adjusted_residual_warns_by_absolute_threshold(self) -> None:
        coa = {
            "S1.parent": {"coa_id": "S1.parent"},
            "S1.other": {
                "coa_id": "S1.other",
                "parent_coa_id": "S1.parent",
                "is_residual": "true",
            },
        }
        decisions = [
            AccountSourceDecision(
                coa_id="S1.other",
                operation=SourceOperation.ADJUSTED_SUBTOTAL,
                source_rows=["Sheet!10"],
                excluded_rows=["Sheet!11"],
            )
        ]

        warnings = _unsupported_residual_remainder_warnings(
            {"S1.parent": 1_000_000.0, "S1.other": 20_000.0},
            coa,
            decisions,
        )

        self.assertEqual(len(warnings), 1)
        self.assertIn("warning|unsupported_residual_remainder|S1.parent|", warnings[0])


if __name__ == "__main__":
    unittest.main()
