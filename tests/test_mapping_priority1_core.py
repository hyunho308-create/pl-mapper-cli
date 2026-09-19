import hashlib
import json

import pytest

from hotel_pl_normalizer.feedback import compose_feedback, compose_run_log_feedback
from hotel_pl_normalizer.mapping.checks import run_checks
from hotel_pl_normalizer.mapping.findings import Finding
from hotel_pl_normalizer.mapping.mapper import (
    AccountSourceDecision,
    ChildCoverage,
    MappingReviewItem,
    OodMiscSummaryMode,
    SourceOperation,
    WorkbookMappingValidator,
    WorkbookSourcePlan,
    WorkbookStrategy,
    _assign_review_item_ids,
)
from hotel_pl_normalizer.mapping.repair import AUTO_REPAIR_RULES
from hotel_pl_normalizer.mapping.rules import (
    render_rule_documentation,
    rule_registry,
)
from hotel_pl_normalizer.mapping.tolerances import (
    KPI_CURRENCY_TOLERANCE,
    KPI_RATIO_TOLERANCE,
    ZERO_EPSILON,
    feedback_match_tolerance,
    offset_match_tolerance,
    reconciliation_tolerance,
    source_supported_tolerance,
)


def _strategy():
    return WorkbookStrategy(
        reporting_layout="test",
        summary_source="test",
        ood_misc_summary_mode=OodMiscSummaryMode.SEPARATE,
    )


def test_finding_roundtrip_downgrade_and_numeric_fidelity():
    finding = Finding(
        "error",
        "summary_math",
        "S12.gop",
        {
            "actual": 100.123456789,
            "expected": 99.0,
            "variance": 1.123456789,
        },
        detail_formats={"actual": ".4f", "expected": ".4f", "variance": ".4f"},
    )

    assert finding.details["actual"] == 100.123456789
    assert "actual=100.1235" in finding.to_legacy_string()
    legacy = Finding.from_legacy(finding.to_legacy_string())
    assert legacy.to_legacy_string() == finding.to_legacy_string()

    warning = finding.downgrade(
        rule="source_discrepancy",
        note="independent layers differ",
        review_item_id="review:one",
    )
    assert warning.severity == "warning"
    assert warning.rule == "source_discrepancy"
    assert warning.details["actual"] == 100.123456789
    assert warning.review_item_id == "review:one"
    assert warning.startswith("warning|source_discrepancy|S12.gop|")


def test_review_item_ids_are_internal_distinct_and_stable_through_patch():
    reviews = [
        MappingReviewItem(
            kind="unusual_convention",
            message="Same review",
            coa_ids=["S1.parent"],
        ),
        MappingReviewItem(
            kind="unusual_convention",
            message="Same review",
            coa_ids=["S1.parent"],
        ),
    ]
    plan = _assign_review_item_ids(
        WorkbookSourcePlan(
            plan_id="initial",
            workbook_id="wb",
            strategy=_strategy(),
            decisions=[
                AccountSourceDecision(
                    coa_id="S1.parent",
                    operation=SourceOperation.DIRECT,
                    source_rows=["Sheet!1"],
                )
            ],
            review_items=reviews,
        )
    )
    initial_ids = [item.review_item_id for item in plan.review_items]
    assert None not in initial_ids
    assert len(set(initial_ids)) == 2
    assert "review_item_id" not in json.dumps(WorkbookSourcePlan.model_json_schema())

    validator = WorkbookMappingValidator(
        "wb",
        [{"row_key": "Sheet!1", "selected_value": 1.0}],
        {"S1.parent": {"coa_id": "S1.parent", "parent_coa_id": ""}},
    )
    validator.current_plan = plan
    patched, _ = validator._apply_patch(
        {
            "patch_id": "repair",
            "workbook_id": "wb",
            "replacements": [
                {
                    "coa_id": "S1.parent",
                    "operation": "sum",
                    "source_rows": ["Sheet!1"],
                }
            ],
            "repair_hypothesis": "Use the supported aggregation.",
            "expected_fix": "The decision changes without replacing reviews.",
        }
    )
    _assign_review_item_ids(patched)
    assert [item.review_item_id for item in patched.review_items] == initial_ids


def test_exact_review_id_linkage_wins_over_same_target_heuristic():
    target = "S12.total_rooms_revenue"
    reviews = [
        {
            "kind": "source_discrepancy",
            "message": "First source treatment.",
            "coa_ids": [target],
            "source_rows": ["Summary!1", "Rooms!1"],
            "review_item_id": "review:first",
        },
        {
            "kind": "source_discrepancy",
            "message": "Second source treatment.",
            "coa_ids": [target],
            "source_rows": ["Summary!2", "Rooms!2"],
            "review_item_id": "review:second",
        },
    ]
    check = Finding(
        "warning",
        "source_layer_conflict",
        target,
        {"actual": 100.0, "expected": 90.0, "variance": 10.0},
        review_item_id="review:second",
    )
    bundle = compose_feedback(
        checks_by_period={"actual": [check]},
        review_items=reviews,
        exceptions=[
            {
                "rule": "source_layer_conflict",
                "period_id": "actual",
                "period_label": "2025 Actual",
                "target": target,
                "reported_value": 100.0,
                "comparison_value": 90.0,
                "variance": 10.0,
                "treatment": "Second source treatment.",
                "source_rows": ["Summary!2", "Rooms!2"],
                "review_item_id": "review:second",
            }
        ],
        execution_issues=[],
        execution_issues_by_period={},
        period_labels={"actual": "2025 Actual"},
        coa={target: {"coa_id": target, "account_name": "Rooms Revenue"}},
    )

    second = next(
        finding
        for finding in bundle.findings
        if "Summary!2" in finding.source_refs
    )
    assert "First source treatment" not in second.rendered_text
    assert len(second.source_input_ids) == 3
    assert bundle.unmatched_count == 0


def test_registry_covers_every_live_rule_and_generates_guidance():
    emitted = {
        "coverage",
        "coverage_inconsistent",
        "coverage_review_not_completed",
        "coverage_unspecified",
        "detail_mapping_collapsed",
        "execution",
        "hierarchy_complete",
        "hierarchy_partial_with_residual",
        "invalid_source_layer_comparison",
        "kpi_math",
        "occupancy_above_capacity",
        "invalid_rooms_available",
        "non_residual_plug",
        "large_residual_plug",
        "mapping_repair_truncated",
        "non_operating_sign",
        "ood_misc_summary_mode_unknown",
        "parent_no_value_with_children",
        "period_detail_available",
        "scope_exception",
        "scope_exclusion",
        "small_source_reconciliation_difference",
        "source_detail_incomplete",
        "source_discrepancy",
        "source_layer_conflict",
        "source_control_difference",
        "source_control_unverified",
        "source_presentation_exception",
        "source_row_double_count",
        "source_row_included_and_excluded",
        "source_row_repeated",
        "summary_combined_ood_misc",
        "summary_combined_ood_misc_inactive_bucket",
        "summary_department",
        "summary_math",
        "unresolved_ambiguity",
        "unresolved_negative_residual",
        "unsupported_residual_remainder",
        "unused_financial_schedule",
    }
    registry = rule_registry()
    assert set(registry) == emitted
    assert all(policy.description and policy.resolution for policy in registry.values())
    assert {
        rule for rule, policy in registry.items() if policy.auto_repairable
    } == AUTO_REPAIR_RULES
    documentation = render_rule_documentation()
    assert all(f"## `{rule}`" in documentation for rule in emitted)


def test_detail_collapse_is_a_typed_common_finding():
    coa = {
        "S1.test": {
            "coa_id": "S1.test",
            "parent_coa_id": "",
            "is_residual": "false",
        }
    }
    evidence = [
        {
            "row_key": f"Detail!{row}",
            "label": f"Account {row}",
            "selected_values": {"actual": float(row)},
        }
        for row in range(1, 121)
    ]
    plan = WorkbookSourcePlan(
        plan_id="collapsed",
        workbook_id="wb",
        strategy=_strategy(),
        decisions=[
            AccountSourceDecision(
                coa_id="S1.test",
                operation=SourceOperation.DIRECT,
                source_rows=["Detail!1"],
            )
        ],
    )
    common = {
        "plan": plan,
        "evidence": evidence,
        "coa": coa,
        "period_labels": {"actual": "2025 Actual"},
        "expected_workbook_id": "wb",
        "preserve_blanks": False,
    }

    session = run_checks(stage="session", **common)
    final = run_checks(stage="final", **common)

    for checked in (session, final):
        finding = next(
            item
            for item in checked.global_findings
            if item.rule == "detail_mapping_collapsed"
        )
        assert isinstance(finding, Finding)
        assert finding.severity == "error"
        assert finding.target == "mapping"
        assert finding.details == {
            "substantive_sheets": 1,
            "nonzero_labeled_rows": 120,
            "mapped_detail_accounts": 1,
            "minimum_detail_accounts": 12,
        }
        assert "map identifiable child accounts" in finding.note
        assert finding in checked.final_findings_by_period()["actual"]

    assert session.global_findings == final.global_findings


def test_mapper_tool_declarations_match_canonical_golden_sha():
    coa = {
        "S1.test": {"coa_id": "S1.test", "parent_coa_id": ""},
        "S12.total_revenue": {
            "coa_id": "S12.total_revenue",
            "parent_coa_id": "",
        },
    }
    validator = WorkbookMappingValidator("wb", [], coa)
    payload = json.dumps(
        validator.declarations(),
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")

    assert hashlib.sha256(payload).hexdigest() == (
        "db02c4b5b7e3ef77898b04662ce4c5bb41c69aa30fbd0b2e991aa736c1a7dd31"
    )
    assert b"review_item_id" not in payload


@pytest.mark.parametrize("value", [0.0, 10.0, -250_000.0, 2_000_000.0])
def test_named_tolerances_preserve_previous_formulas(value):
    assert reconciliation_tolerance(value) == max(5.0, abs(value) * 0.00001)
    assert offset_match_tolerance(value) == max(5.0, abs(value) * 0.005)
    assert source_supported_tolerance(value) == max(
        max(5.0, abs(value) * 0.00001), abs(value) * 0.0001
    )
    assert feedback_match_tolerance(value, -value) == max(
        0.01, abs(value) * 0.00001
    )
    assert ZERO_EPSILON == 0.005
    assert KPI_RATIO_TOLERANCE == 0.001
    assert KPI_CURRENCY_TOLERANCE == 0.05


def test_session_and_final_share_all_non_residual_checks():
    coa = {
        "S1.parent": {
            "coa_id": "S1.parent",
            "parent_coa_id": "",
            "is_residual": "false",
        },
        "S1.child": {
            "coa_id": "S1.child",
            "parent_coa_id": "S1.parent",
            "is_residual": "false",
        },
    }
    evidence = [
        {
            "row_key": "Sheet!1",
            "label": "Parent",
            "selected_value": 10.0,
            "selected_values": {"actual": 10.0},
        },
        {
            "row_key": "Sheet!2",
            "label": "Child",
            "selected_value": 1.0,
            "selected_values": {"actual": 1.0},
        },
    ]
    plan = WorkbookSourcePlan(
        plan_id="plan",
        workbook_id="wb",
        strategy=_strategy(),
        decisions=[
            AccountSourceDecision(
                coa_id="S1.parent",
                operation=SourceOperation.DIRECT,
                source_rows=["Sheet!1"],
                child_coverage=ChildCoverage.COMPLETE,
            ),
            AccountSourceDecision(
                coa_id="S1.child",
                operation=SourceOperation.DIRECT,
                source_rows=["Sheet!2"],
            ),
        ],
    )
    common = {
        "plan": plan,
        "evidence": evidence,
        "coa": coa,
        "period_labels": {"actual": "2025 Actual"},
        "expected_workbook_id": "wb",
        "preserve_blanks": False,
    }
    session = run_checks(stage="session", **common)
    final = run_checks(stage="final", **common)

    assert session.execution_issues == final.execution_issues
    assert session.execution_issues_by_period == final.execution_issues_by_period
    assert session.global_findings == final.global_findings
    assert session.findings_by_period == final.findings_by_period
    assert [item.rule for item in session.findings_by_period["actual"]] == [
        "hierarchy_complete"
    ]


@pytest.mark.parametrize("version", [2, 3, 4])
def test_legacy_log_feedback_accepts_null_findings(version):
    target = "S1.total_rooms_revenue"
    bundle = compose_run_log_feedback(
        {
            "log_version": version,
            "source": {"period": "2025 Actual"},
            "findings": None,
            "checks": [
                f"warning|source_detail_incomplete|{target}|"
                "parent=100.00|children=90.00|variance=10.00"
            ],
            "review_items": [],
            "exceptions": [],
            "execution_issues": [],
        },
        coa={target: {"coa_id": target, "account_name": "Rooms Revenue"}},
    )
    assert bundle.rendered_count == 1
    assert bundle.unmatched_count == 0
