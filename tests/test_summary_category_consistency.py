from __future__ import annotations

import unittest

from hotel_pl_normalizer.mapping.mapper import (
    AccountSourceDecision,
    MappingReviewItem,
    OodMiscSummaryMode,
    SourceOperation,
    WorkbookSourcePlan,
    WorkbookStrategy,
    _load_coa,
    _qualify_source_discrepancies,
    _validate,
)
from hotel_pl_normalizer.output import _describe_check

SUMMARY_OOD = "S12.total_other_operated_departments_revenue"
SUMMARY_MISC = "S12.total_miscellaneous_income"
DETAIL_OOD = "S3.total_other_operated_departments_revenue"
DETAIL_MISC = "S4.total_miscellaneous_income"


def _strategy(mode: OodMiscSummaryMode) -> WorkbookStrategy:
    return WorkbookStrategy(
        reporting_layout="test",
        summary_source="test",
        ood_misc_summary_mode=mode,
    )


def _combined_fixture(
    *, include_review: bool = True, include_numeric_candidate: bool = False
):
    summary_row = "Summary!20"
    detail_ood_row = "OOD!40"
    detail_misc_row = "Misc!30"
    evidence = [
        {"row_key": summary_row, "label": "Other Income", "selected_value": 100},
        {"row_key": detail_ood_row, "label": "OOD Revenue", "selected_value": 60},
        {"row_key": detail_misc_row, "label": "Misc Income", "selected_value": 40},
    ]
    if include_numeric_candidate:
        evidence.append(
            {
                "row_key": "OOD!99",
                "label": "Unrelated supervisor payroll",
                "selected_value": 40,
            }
        )
    reviews = []
    if include_review:
        reviews.append(
            MappingReviewItem(
                kind="source_discrepancy",
                message=(
                    "Summary combines OOD and Misc while detailed schedules "
                    "report them separately."
                ),
                coa_ids=[SUMMARY_OOD, SUMMARY_MISC, DETAIL_OOD, DETAIL_MISC],
                source_rows=[summary_row, detail_ood_row, detail_misc_row],
            )
        )
    plan = WorkbookSourcePlan(
        plan_id="plan:combined-ood-misc",
        workbook_id="wb:combined-ood-misc",
        strategy=_strategy(OodMiscSummaryMode.COMBINED_IN_OOD),
        decisions=[
            AccountSourceDecision(
                coa_id=SUMMARY_OOD,
                operation=SourceOperation.DIRECT,
                source_rows=[summary_row],
            ),
            AccountSourceDecision(
                coa_id=SUMMARY_MISC,
                operation=SourceOperation.NO_VALUE,
            ),
            AccountSourceDecision(
                coa_id=DETAIL_OOD,
                operation=SourceOperation.DIRECT,
                source_rows=[detail_ood_row],
            ),
            AccountSourceDecision(
                coa_id=DETAIL_MISC,
                operation=SourceOperation.DIRECT,
                source_rows=[detail_misc_row],
            ),
        ],
        review_items=reviews,
    )
    findings = [
        (
            f"error|summary_department|{SUMMARY_OOD}|actual=100.0000|"
            f"expected=60.0000|variance=40.0000|equation={DETAIL_OOD}"
        ),
        (
            f"error|summary_department|{SUMMARY_MISC}|actual=0.0000|"
            f"expected=40.0000|variance=-40.0000|equation={DETAIL_MISC}"
        ),
    ]
    history = [
        {
            "errors": [f"Selected period: {finding}" for finding in findings],
            "warnings": [],
        }
    ]
    return plan, evidence, findings, history


class SummaryCategoryConsistencyTests(unittest.TestCase):
    def test_combined_mode_does_not_waive_same_category_checks(self) -> None:
        values = {
            SUMMARY_OOD: 100.0,
            SUMMARY_MISC: 0.0,
            DETAIL_OOD: 60.0,
            DETAIL_MISC: 40.0,
        }

        checks = _validate(
            values,
            {},
            [],
            _strategy(OodMiscSummaryMode.COMBINED_IN_OOD),
        )

        category_errors = [
            item for item in checks if item.startswith("error|summary_department|")
        ]
        self.assertEqual(len(category_errors), 2)
        self.assertFalse(
            any("error|summary_combined_ood_misc|" in item for item in checks)
        )

    def test_combined_source_presentation_qualifies_only_after_repair(self) -> None:
        plan, evidence, findings, history = _combined_fixture()
        coa = _load_coa()

        first = _qualify_source_discrepancies(
            findings, plan, evidence, coa, [], "Selected period", []
        )
        second = _qualify_source_discrepancies(
            findings, plan, evidence, coa, history, "Selected period", []
        )

        self.assertEqual(first, findings)
        self.assertEqual(len(second), 2)
        self.assertTrue(
            all(
                item.startswith("warning|source_presentation_exception|")
                for item in second
            )
        )

    def test_combined_source_presentation_requires_structured_review(self) -> None:
        plan, evidence, findings, history = _combined_fixture(include_review=False)

        result = _qualify_source_discrepancies(
            findings,
            plan,
            evidence,
            _load_coa(),
            history,
            "Selected period",
            [],
        )

        self.assertEqual(result, findings)

    def test_reviewed_numeric_candidate_does_not_block_source_exception(self) -> None:
        plan, evidence, findings, history = _combined_fixture(
            include_numeric_candidate=True
        )

        result = _qualify_source_discrepancies(
            findings,
            plan,
            evidence,
            _load_coa(),
            history,
            "Selected period",
            [],
        )

        self.assertTrue(
            all(
                item.startswith("warning|source_presentation_exception|")
                for item in result
            )
        )

    def test_source_presentation_exception_is_rendered_for_human_review(self) -> None:
        check = (
            f"warning|source_presentation_exception|{SUMMARY_MISC}|"
            "actual=0.0000|expected=40.0000|variance=-40.0000|"
            f"equation={DETAIL_MISC}"
        )

        severity, target, message = _describe_check(check)

        self.assertEqual(severity, "warning")
        self.assertEqual(target, SUMMARY_MISC)
        self.assertIn("Combined source presentation", message)


if __name__ == "__main__":
    unittest.main()
