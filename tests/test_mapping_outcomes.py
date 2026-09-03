from __future__ import annotations

import unittest

from hotel_pl_normalizer.mapping.mapper import (
    AccountSourceDecision,
    MappingOutcome,
    MappingReviewItem,
    OodMiscSummaryMode,
    SourceLayerOperation,
    SourceOperation,
    WorkbookMappingValidator,
    WorkbookSourcePlan,
    WorkbookStrategy,
    _execute,
    _outcome_from_result,
    _period_completeness_issues,
    _qualify_source_discrepancies,
    _review_item_blockers,
    _review_item_warnings,
    _same_blocking_conflicts,
    _source_layer_conflict_warnings,
    _source_row_reuse_issues,
    _structured_exceptions,
    _unsupported_residual_remainder_warnings,
    _validation_score,
)
from hotel_pl_normalizer.pipeline import NormalizationResult
from hotel_pl_normalizer.run_log import build_run_log


class MappingOutcomeTests(unittest.TestCase):
    def test_source_row_reuse_allows_one_summary_to_department_chain(self) -> None:
        coa = {
            "S12.total_other_operated_departments_revenue": {},
            "S3.total_other_operated_departments_revenue": {},
            "S3.other_department_revenue": {
                "parent_coa_id": "S3.total_other_operated_departments_revenue"
            },
        }
        decisions = [
            AccountSourceDecision(
                coa_id=coa_id,
                operation=SourceOperation.DIRECT,
                source_rows=["Summary!19"],
            )
            for coa_id in coa
        ]

        self.assertEqual(_source_row_reuse_issues(decisions, coa, []), [])

    def test_source_row_reuse_allows_transitive_department_to_summary_chain(
        self,
    ) -> None:
        coa = {
            "S7.franchise_fee": {"parent_coa_id": "S7.other_expenses"},
            "S7.other_expenses": {
                "parent_coa_id": "S7.total_sales_and_marketing_expenses"
            },
            "S7.total_sales_and_marketing_expenses": {"parent_coa_id": ""},
            "S12.total_sales_and_marketing_expenses": {"parent_coa_id": ""},
            "S12.total_undistributed_expenses": {"parent_coa_id": ""},
        }
        decisions = [
            AccountSourceDecision(
                coa_id=coa_id,
                operation=SourceOperation.DIRECT,
                source_rows=["Non-Op!13"],
            )
            for coa_id in coa
        ]

        self.assertEqual(_source_row_reuse_issues(decisions, coa, []), [])

    def test_source_row_reuse_still_blocks_independent_siblings(self) -> None:
        coa = {
            "S2.food_revenue": {},
            "S2.outlet_1_food_revenue": {"parent_coa_id": "S2.food_revenue"},
            "S2.outlet_2_food_revenue": {"parent_coa_id": "S2.food_revenue"},
        }
        decisions = [
            AccountSourceDecision(
                coa_id=coa_id,
                operation=SourceOperation.DIRECT,
                source_rows=["F&B!12"],
            )
            for coa_id in (
                "S2.outlet_1_food_revenue",
                "S2.outlet_2_food_revenue",
            )
        ]

        issues = _source_row_reuse_issues(decisions, coa, [])

        self.assertEqual(len(issues), 1)
        self.assertIn("error|source_row_double_count|F&B!12|", issues[0])
        self.assertIn("S2.outlet_1_food_revenue", issues[0])
        self.assertIn("S2.outlet_2_food_revenue", issues[0])

    def test_source_row_reuse_allows_cross_layer_reclassification(self) -> None:
        coa = {
            "S12.non_operating_income": {
                "department": "Summary",
            },
            "S11.non_operating_income": {"department": "Non-Op"},
            "S11.total_non_operating_income_and_expenses": {
                "department": "Non-Op"
            },
            "S11.other": {
                "department": "Non-Op",
                "parent_coa_id": "S11.total_non_operating_income_and_expenses",
            },
        }
        decisions = [
            AccountSourceDecision(
                coa_id=coa_id,
                operation=SourceOperation.DIRECT,
                source_rows=["Non-Op!18"],
            )
            for coa_id in ("S12.non_operating_income", "S11.other")
        ]

        self.assertEqual(_source_row_reuse_issues(decisions, coa, []), [])

    def test_source_row_reuse_still_blocks_unrelated_departments(self) -> None:
        coa = {
            "S1.total_rooms_revenue": {},
            "S2.total_food_and_beverage_revenue": {},
        }
        decisions = [
            AccountSourceDecision(
                coa_id=coa_id,
                operation=SourceOperation.DIRECT,
                source_rows=["Summary!20"],
            )
            for coa_id in coa
        ]

        issues = _source_row_reuse_issues(decisions, coa, [])

        self.assertEqual(len(issues), 1)
        self.assertIn("error|source_row_double_count|Summary!20|", issues[0])

    def test_typed_source_comparison_qualifies_and_collapses_same_difference(
        self,
    ) -> None:
        summary = "S12.total_rooms_expenses"
        detail = "S1.total_rooms_expenses"
        coa = {summary: {}, detail: {}}
        plan = WorkbookSourcePlan(
            plan_id="source-difference",
            workbook_id="wb",
            strategy=WorkbookStrategy(
                reporting_layout="test",
                summary_source="Summary",
            ),
            decisions=[
                AccountSourceDecision(
                    coa_id=summary,
                    operation=SourceOperation.DIRECT,
                    source_rows=["Summary!23"],
                ),
                AccountSourceDecision(
                    coa_id=detail,
                    operation=SourceOperation.DIRECT,
                    source_rows=["Rooms!38"],
                ),
            ],
            review_items=[
                MappingReviewItem(
                    kind="source_discrepancy",
                    message="Summary and Rooms report different supported totals.",
                    coa_ids=[summary, detail],
                    source_rows=["Summary!23", "Rooms!38"],
                    selected_source_rows=["Summary!23"],
                    alternate_source_rows=["Rooms!38"],
                    selected_source_operation=SourceLayerOperation.DIRECT,
                    alternate_source_operation=SourceLayerOperation.DIRECT,
                )
            ],
        )
        checks = [
            f"error|summary_department|{summary}|actual=105884|expected=100000|"
            f"variance=5884|equation={detail}",
            f"warning|source_layer_conflict|{summary}|actual=105884|"
            "expected=100000|variance=5884|equation=selected mapped layer versus "
            "typed alternate cited layer",
        ]

        qualified = _qualify_source_discrepancies(
            checks,
            plan,
            [
                {"row_key": "Summary!23"},
                {"row_key": "Rooms!38"},
            ],
            coa,
            [],
            "2025 Actual",
            [],
        )

        self.assertEqual(len(qualified), 1)
        self.assertTrue(qualified[0].startswith("warning|source_discrepancy|"))

    def test_no_op_patch_can_finish_an_offered_coverage_review(self) -> None:
        validator = WorkbookMappingValidator("wb", [], {})
        plan = WorkbookSourcePlan(
            plan_id="initial",
            workbook_id="wb",
            strategy=WorkbookStrategy(
                reporting_layout="test",
                summary_source="Summary",
            ),
            decisions=[],
        )
        checkpoint = {
            "accepted": True,
            "validation_attempt": 1,
            "warning_count": 1,
            "warnings": [
                "Actual: warning|source_detail_incomplete|S1.test|"
                "parent=100|children=90|variance=10"
            ],
            "review_items": [],
        }
        validator.current_plan = plan
        validator.warning_cleanup_pending = True
        validator.warning_cleanup_checkpoint_plan = plan
        validator.warning_cleanup_checkpoint_result = checkpoint
        validator.warning_cleanup_checkpoint_score = _validation_score(checkpoint)
        validator.warning_cleanup_checkpoint_attempt = 1
        validator.history.append(checkpoint)

        result = validator.dispatch(
            "patch_mapping",
            {
                "patch_id": "coverage-reviewed",
                "workbook_id": "wb",
                "replacements": [],
            },
        )
        completion = validator.terminal_result("patch_mapping", result)

        self.assertTrue(result["coverage_review_completed"])
        self.assertEqual(completion["status"], "accepted")
        self.assertTrue(validator.warning_cleanup_attempted)

    def test_successful_repair_counts_as_the_focused_coverage_review(self) -> None:
        coa = {
            "S1.parent": {"coa_id": "S1.parent"},
            "S1.child": {
                "coa_id": "S1.child",
                "parent_coa_id": "S1.parent",
            },
            "S1.blocker": {"coa_id": "S1.blocker"},
        }
        validator = WorkbookMappingValidator(
            "wb",
            [{"row_key": "Sheet!10", "selected_value": 100.0}],
            coa,
        )
        first = validator.dispatch(
            "validate_mapping",
            {
                "plan_id": "initial",
                "workbook_id": "wb",
                "strategy": {
                    "reporting_layout": "test",
                    "summary_source": "Summary",
                },
                "decisions": [
                    {
                        "coa_id": "S1.parent",
                        "operation": "direct",
                        "source_rows": ["Sheet!10"],
                        "child_coverage": "partial",
                    },
                    {
                        "coa_id": "S1.child",
                        "operation": "no_value",
                        "child_coverage": "not_applicable",
                    },
                ],
            },
        )
        self.assertFalse(first["accepted"])

        repaired = validator.dispatch(
            "patch_mapping",
            {
                "patch_id": "repair",
                "workbook_id": "wb",
                "replacements": [
                    {
                        "coa_id": "S1.blocker",
                        "operation": "no_value",
                        "child_coverage": "not_applicable",
                    }
                ],
            },
        )
        completion = validator.terminal_result("patch_mapping", repaired)

        self.assertTrue(repaired["accepted"])
        self.assertTrue(repaired["coverage_review_completed"])
        self.assertTrue(validator.warning_cleanup_attempted)
        self.assertEqual(completion["status"], "accepted")

    def test_identical_duplicate_patch_replacements_are_safely_deduplicated(
        self,
    ) -> None:
        validator = WorkbookMappingValidator("wb", [], {"S1.test": {}})
        validator.current_plan = WorkbookSourcePlan(
            plan_id="initial",
            workbook_id="wb",
            strategy=WorkbookStrategy(
                reporting_layout="test",
                summary_source="Summary",
            ),
            decisions=[
                AccountSourceDecision(
                    coa_id="S1.test",
                    operation=SourceOperation.NO_VALUE,
                )
            ],
        )
        replacement = {
            "coa_id": "S1.test",
            "operation": "direct",
            "source_rows": ["Sheet!10"],
            "excluded_rows": [],
            "venue_name": None,
            "child_coverage": "not_applicable",
        }

        plan, action = validator._apply_patch(
            {
                "patch_id": "deduplicated",
                "workbook_id": "wb",
                "replacements": [replacement, replacement],
            }
        )

        self.assertEqual(action["submitted_coa_ids"], ["S1.test"])
        self.assertEqual(plan.decisions[0].source_rows, ["Sheet!10"])

    def test_malformed_optional_repair_comparison_keeps_its_review_note(self) -> None:
        validator = WorkbookMappingValidator("wb", [], {"S1.test": {}})
        validator.current_plan = WorkbookSourcePlan(
            plan_id="initial",
            workbook_id="wb",
            strategy=WorkbookStrategy(
                reporting_layout="test",
                summary_source="Summary",
            ),
            decisions=[
                AccountSourceDecision(
                    coa_id="S1.test",
                    operation=SourceOperation.NO_VALUE,
                )
            ],
        )

        plan, _ = validator._apply_patch(
            {
                "patch_id": "repair",
                "workbook_id": "wb",
                "replacements": [],
                "review_items": [
                    {
                        "kind": "source_discrepancy",
                        "message": "Keep this source presentation note.",
                        "coa_ids": ["S1.test"],
                        "source_rows": ["Sheet!10"],
                        "selected_source_rows": ["Sheet!10"],
                        "alternate_source_rows": [],
                        "selected_source_operation": "direct",
                        "alternate_source_operation": "ratio",
                    }
                ],
            }
        )

        review = plan.review_items[0]
        self.assertEqual(review.message, "Keep this source presentation note.")
        self.assertEqual(review.source_rows, ["Sheet!10"])
        self.assertEqual(review.selected_source_rows, [])
        self.assertIsNone(review.selected_source_operation)

    def test_combined_ood_misc_review_can_reuse_decision_source_rows(self) -> None:
        summary_ood = "S12.total_other_operated_departments_revenue"
        summary_misc = "S12.total_miscellaneous_income"
        detail_ood = "S3.total_other_operated_departments_revenue"
        detail_misc = "S4.total_miscellaneous_income"
        coa = {coa_id: {} for coa_id in (summary_ood, summary_misc, detail_ood, detail_misc)}
        plan = WorkbookSourcePlan(
            plan_id="combined",
            workbook_id="wb",
            strategy=WorkbookStrategy(
                reporting_layout="test",
                summary_source="Summary",
                ood_misc_summary_mode=OodMiscSummaryMode.COMBINED_IN_MISC,
            ),
            decisions=[
                AccountSourceDecision(
                    coa_id=summary_ood,
                    operation=SourceOperation.NO_VALUE,
                ),
                AccountSourceDecision(
                    coa_id=summary_misc,
                    operation=SourceOperation.DIRECT,
                    source_rows=["Summary!22"],
                ),
                AccountSourceDecision(
                    coa_id=detail_ood,
                    operation=SourceOperation.DIRECT,
                    source_rows=["OOD!10"],
                ),
                AccountSourceDecision(
                    coa_id=detail_misc,
                    operation=SourceOperation.DIRECT,
                    source_rows=["Misc!10"],
                ),
            ],
            review_items=[
                MappingReviewItem(
                    kind="source_discrepancy",
                    message="Summary combines the two independently mapped layers.",
                    coa_ids=[summary_ood, summary_misc, detail_ood, detail_misc],
                )
            ],
        )
        checks = [
            f"error|summary_department|{summary_ood}|actual=0|expected=30546|"
            f"variance=-30546|equation={detail_ood}",
            f"error|summary_department|{summary_misc}|actual=35812|expected=5268|"
            f"variance=30544|equation={detail_misc}",
        ]
        history = [
            {
                "errors": [
                    f"January 2025 Actual: {finding}" for finding in checks
                ]
            }
        ]

        qualified = _qualify_source_discrepancies(
            checks,
            plan,
            [],
            coa,
            history,
            "January 2025 Actual",
            [],
        )

        self.assertEqual(len(qualified), 2)
        self.assertTrue(
            all(
                finding.startswith("warning|source_presentation_exception|")
                for finding in qualified
            )
        )

    def test_combined_ood_misc_allows_declared_summary_only_row_overlap(self) -> None:
        summary_ood = "S12.total_other_operated_departments_revenue"
        summary_misc = "S12.total_miscellaneous_income"
        detail_ood = "S3.total_other_operated_departments_revenue"
        detail_misc = "S4.total_miscellaneous_income"
        coa = {
            coa_id: {}
            for coa_id in (summary_ood, summary_misc, detail_ood, detail_misc)
        }
        plan = WorkbookSourcePlan(
            plan_id="combined-summary-only",
            workbook_id="wb",
            strategy=WorkbookStrategy(
                reporting_layout="test",
                summary_source="Summary",
                ood_misc_summary_mode=OodMiscSummaryMode.COMBINED_IN_MISC,
                structural_scenarios=[
                    {
                        "scenario": "summary_only_department",
                        "reason": "Retail rent has no separate detail schedule.",
                        "source_rows": ["Summary!23", "Detail!152"],
                    }
                ],
            ),
            decisions=[
                AccountSourceDecision(
                    coa_id=summary_ood,
                    operation=SourceOperation.NO_VALUE,
                ),
                AccountSourceDecision(
                    coa_id=summary_misc,
                    operation=SourceOperation.SUM,
                    source_rows=["Summary!22", "Summary!23"],
                ),
                AccountSourceDecision(
                    coa_id=detail_ood,
                    operation=SourceOperation.DIRECT,
                    source_rows=["Detail!145"],
                ),
                AccountSourceDecision(
                    coa_id=detail_misc,
                    operation=SourceOperation.SUM,
                    source_rows=["Detail!147", "Summary!23"],
                ),
            ],
            review_items=[
                MappingReviewItem(
                    kind="source_discrepancy",
                    message="Summary combines OOD and Miscellaneous Income.",
                    coa_ids=[summary_ood, summary_misc, detail_ood, detail_misc],
                )
            ],
        )
        checks = [
            f"error|summary_department|{summary_ood}|actual=0|expected=30|"
            f"variance=-30|equation={detail_ood}",
            f"error|summary_department|{summary_misc}|actual=40|expected=10|"
            f"variance=30|equation={detail_misc}",
        ]
        history = [
            {
                "errors": [
                    f"January 2025 Actual: {finding}" for finding in checks
                ]
            }
        ]

        qualified = _qualify_source_discrepancies(
            checks,
            plan,
            [],
            coa,
            history,
            "January 2025 Actual",
            [],
        )

        self.assertTrue(
            all(
                finding.startswith("warning|source_presentation_exception|")
                for finding in qualified
            )
        )

    def test_combined_ood_misc_blocks_undeclared_summary_detail_overlap(self) -> None:
        summary_ood = "S12.total_other_operated_departments_revenue"
        summary_misc = "S12.total_miscellaneous_income"
        detail_ood = "S3.total_other_operated_departments_revenue"
        detail_misc = "S4.total_miscellaneous_income"
        coa = {
            coa_id: {}
            for coa_id in (summary_ood, summary_misc, detail_ood, detail_misc)
        }
        plan = WorkbookSourcePlan(
            plan_id="combined-unauthorized-overlap",
            workbook_id="wb",
            strategy=WorkbookStrategy(
                reporting_layout="test",
                summary_source="Summary",
                ood_misc_summary_mode=OodMiscSummaryMode.COMBINED_IN_MISC,
            ),
            decisions=[
                AccountSourceDecision(
                    coa_id=summary_ood,
                    operation=SourceOperation.NO_VALUE,
                ),
                AccountSourceDecision(
                    coa_id=summary_misc,
                    operation=SourceOperation.SUM,
                    source_rows=["Summary!22", "Summary!23"],
                ),
                AccountSourceDecision(
                    coa_id=detail_ood,
                    operation=SourceOperation.DIRECT,
                    source_rows=["Detail!145"],
                ),
                AccountSourceDecision(
                    coa_id=detail_misc,
                    operation=SourceOperation.SUM,
                    source_rows=["Detail!147", "Summary!23"],
                ),
            ],
            review_items=[
                MappingReviewItem(
                    kind="source_discrepancy",
                    message="Summary combines OOD and Miscellaneous Income.",
                    coa_ids=[summary_ood, summary_misc, detail_ood, detail_misc],
                )
            ],
        )
        checks = [
            f"error|summary_department|{summary_ood}|actual=0|expected=30|"
            f"variance=-30|equation={detail_ood}",
            f"error|summary_department|{summary_misc}|actual=40|expected=10|"
            f"variance=30|equation={detail_misc}",
        ]
        history = [
            {
                "errors": [
                    f"January 2025 Actual: {finding}" for finding in checks
                ]
            }
        ]

        qualified = _qualify_source_discrepancies(
            checks,
            plan,
            [],
            coa,
            history,
            "January 2025 Actual",
            [],
        )

        self.assertEqual(qualified, checks)

    def test_identical_duplicate_initial_decisions_are_safely_deduplicated(
        self,
    ) -> None:
        decision = {
            "coa_id": "S1.test",
            "operation": "direct",
            "source_rows": ["Sheet!10"],
            "excluded_rows": [],
            "venue_name": None,
            "child_coverage": "not_applicable",
        }
        validator = WorkbookMappingValidator(
            "wb",
            [{"row_key": "Sheet!10", "selected_value": 100.0}],
            {"S1.test": {"coa_id": "S1.test"}},
        )

        result = validator.dispatch(
            "validate_mapping",
            {
                "plan_id": "deduplicated",
                "workbook_id": "wb",
                "strategy": {
                    "reporting_layout": "test",
                    "summary_source": "Summary",
                },
                "decisions": [decision, decision],
                "review_items": [],
            },
        )

        self.assertEqual(len(validator.current_plan.decisions), 1)
        self.assertFalse(
            any("duplicate decision S1.test" in item for item in result["errors"])
        )

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

    def test_patch_can_preserve_newly_identified_incomplete_source_detail(self) -> None:
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
                "repair_hypothesis": "The source omits child detail.",
                "expected_fix": "Preserve the source limitation.",
                "source_detail_incomplete": [
                    "Charlotte has no separate spa child schedule."
                ],
            }
        )

        self.assertEqual(
            repaired.strategy.source_detail_incomplete,
            ["Charlotte has no separate spa child schedule."],
        )
        patch_schema = next(
            item for item in validator.declarations() if item["name"] == "patch_mapping"
        )["parameters"]["properties"]
        self.assertIn("source_detail_incomplete", patch_schema)

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

    def test_excel_percentage_format_prevents_double_normalizing_occupancy(self) -> None:
        decision = AccountSourceDecision(
            coa_id="S12.occupancy",
            operation=SourceOperation.DIRECT,
            source_rows=["Summary!16"],
        )
        coa = {"S12.occupancy": {}}
        formatted = [
            {
                "row_key": "Summary!16",
                "selected_values": {"actual": 3.2437},
                "selected_value_formats": {"actual": "0.00%"},
            }
        ]
        unformatted = [
            {
                "row_key": "Summary!16",
                "selected_values": {"actual": 3.2437},
            }
        ]

        formatted_values, _ = _execute(
            [decision], formatted, coa, period_id="actual"
        )
        unformatted_values, _ = _execute(
            [decision], unformatted, coa, period_id="actual"
        )

        self.assertAlmostEqual(formatted_values["S12.occupancy"], 3.2437)
        self.assertAlmostEqual(unformatted_values["S12.occupancy"], 0.032437)

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

    def test_run_log_records_the_same_canonical_feedback_manifest_as_output(self) -> None:
        target = "S12.total_revenue"
        result = NormalizationResult(
            workbook_id="wb",
            source_name="Hotel.xlsx",
            period_label="2025 Actual",
            period_labels={"actual": "2025 Actual"},
            values={target: 100.0},
            period_values={"actual": {target: 100.0}},
            coa={target: {"account_name": "Total Revenue"}},
            checks_by_period={
                "actual": [
                    f"warning|source_detail_incomplete|{target}|"
                    "parent=100.00|children=80.00|variance=20.00"
                ]
            },
            accepted=True,
        )

        log = build_run_log(result)

        self.assertEqual(log["log_version"], 4)
        self.assertEqual(log["outcome"]["feedback_findings"], 1)
        self.assertEqual(log["feedback_manifest"]["rendered_count"], 1)
        self.assertEqual(
            log["feedback_manifest"]["findings"][0]["destination"],
            f"coa:{target}",
        )
        self.assertEqual(result.feedback_manifest, log["feedback_manifest"])

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
