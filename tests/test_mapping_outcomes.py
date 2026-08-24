from __future__ import annotations

import unittest

from hotel_pl_normalizer.mapping.mapper import (
    AccountSourceDecision,
    MappingOutcome,
    MappingReviewItem,
    SourceOperation,
    WorkbookMappingValidator,
    WorkbookSourcePlan,
    WorkbookStrategy,
    _outcome_from_result,
    _review_item_blockers,
    _same_blocking_conflicts,
    _source_layer_conflict_warnings,
    _structured_exceptions,
    _validation_score,
)


class MappingOutcomeTests(unittest.TestCase):
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
        )

        blockers = _review_item_blockers([review])
        outcome = _outcome_from_result(
            {"accepted": False, "errors": blockers, "warnings": []}, [review]
        )

        self.assertEqual(outcome, MappingOutcome.SCOPE_EXCEPTION)
        self.assertEqual(len(blockers), 1)
        self.assertIn("error|scope_exception|S11.other|", blockers[0])

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


if __name__ == "__main__":
    unittest.main()
