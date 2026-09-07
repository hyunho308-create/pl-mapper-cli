from copy import deepcopy

import pytest

from hotel_pl_normalizer.feedback import compose_feedback, load_canonical_coa
from hotel_pl_normalizer.mapping.source_controls import (
    SourceControl, check_source_controls, control_reference_issues,
)

TARGET = "S2.total_food_and_beverage_revenue"
LABELS = {"actual": "2025 Actual", "prior": "2024 Actual"}


def control(**updates):
    return SourceControl(**{
        "label": "Event outlet net revenue", "coa_id": TARGET,
        "total_row": "Outlet!4", "component_rows": ["Outlet!1", "Outlet!2"],
        **updates,
    })


def evidence(total=90):
    return [
        {"row_key": key, "selected_values": {"actual": actual, "prior": prior}}
        for key, actual, prior in [
            ("Outlet!1", 60, 20), ("Outlet!2", 40, 30), ("Outlet!4", total, 50),
        ]
    ]


def feedback(checks, reviews=(), rows=()):
    return compose_feedback(
        checks_by_period=checks, review_items=reviews, exceptions=[],
        execution_issues=[], execution_issues_by_period={},
        period_labels=LABELS, coa=load_canonical_coa(), evidence_rows=rows,
    )


def test_unmapped_source_total_is_checked_for_each_period_without_changing_evidence():
    rows = evidence()
    original = deepcopy(rows)
    findings = check_source_controls([control()], rows, "actual")
    assert findings[0].details["variance"] == -10
    assert check_source_controls([control()], rows, "prior") == []
    assert rows == original
    note = feedback({"actual": findings}).findings[0]
    assert "$10.00 shortfall" in note.rendered_text
    assert "Outlet row 4" in note.rendered_text
    assert "2025 Actual" in note.rendered_text
    assert set(note.source_refs) == {"Outlet!1", "Outlet!2", "Outlet!4"}


@pytest.mark.parametrize("invalid", [None, float("nan"), True])
def test_missing_or_invalid_period_value_is_unverified_not_zero(invalid):
    rows = evidence()
    rows[0]["selected_values"]["prior"] = invalid
    rows[0]["selected_value"] = 20
    findings = check_source_controls([control()], rows, "prior")
    assert findings[0].rule == "source_control_unverified"
    assert feedback({"prior": findings}).rendered_count == 1


def test_missing_period_key_never_falls_back_to_primary():
    rows = evidence()
    rows[0]["selected_values"].pop("prior")
    rows[0]["selected_value"] = 20
    assert check_source_controls([control()], rows, "prior")[0].rule == "source_control_unverified"


def test_rounding_stays_in_audit_but_not_visible():
    findings = check_source_controls([control()], evidence(100.44), "actual")
    assert findings[0].severity == "info"
    bundle = feedback({"actual": findings})
    assert bundle.rendered_count == 0
    assert bundle.findings[0].periods[0].variance == pytest.approx(.44)


def test_material_second_control_on_same_account_remains_separate():
    second = control(label="Another outlet", total_row="Other!4", component_rows=["Other!1"])
    rows = evidence() + [
        {"row_key": "Other!4", "selected_values": {"actual": 300}},
        {"row_key": "Other!1", "selected_values": {"actual": 310}},
    ]
    bundle = feedback({"actual": check_source_controls([control(), second], rows, "actual")})
    assert bundle.rendered_count == 2  # equal amounts are not identity


def test_source_sheet_commas_do_not_split_row_references():
    check = control(total_row="Outlet, bar!4", component_rows=["Outlet, bar!1"])
    rows = [{"row_key": row, "selected_values": {"actual": value}}
            for row, value in [("Outlet, bar!4", 100), ("Outlet, bar!1", 80)]]
    bundle = feedback({"actual": check_source_controls([check], rows, "actual")})
    assert set(bundle.findings[0].source_refs) == {"Outlet, bar!4", "Outlet, bar!1"}


def test_same_source_equation_is_reported_once_despite_label_or_row_order():
    repeated = control(label="Another description", component_rows=["Outlet!2", "Outlet!1"])
    assert len(check_source_controls([control(), repeated], evidence(), "actual")) == 1


@pytest.mark.parametrize("updates", [
    {"component_rows": ["Outlet!4"]},
    {"component_rows": ["Outlet!1", "Outlet!1"]},
    {"excluded_rows": ["Outlet!1"]},
    {"label": " "},
])
def test_self_comparison_or_repeated_rows_are_rejected(updates):
    with pytest.raises(ValueError):
        control(**updates)


def test_unknown_row_and_account_are_validation_issues():
    assert len(control_reference_issues([control(coa_id="unknown")], [], {})) == 2


def test_explicit_exclusion_and_signed_credit_are_arithmetic_not_double_netting():
    rows = evidence(95) + [{"row_key": "Outlet!3", "selected_values": {"actual": 5}}]
    assert check_source_controls([control(excluded_rows=["Outlet!3"])], rows, "actual") == []
    rows[-1]["selected_values"]["actual"] = -5
    findings = check_source_controls([control(component_rows=["Outlet!1", "Outlet!2", "Outlet!3"])], rows, "actual")
    assert findings == []


def test_final_check_runner_and_run_log_preserve_controls_without_review_join():
    from hotel_pl_normalizer.mapping.checks import run_checks
    from hotel_pl_normalizer.mapping.mapper import (
        AccountSourceDecision, WorkbookSourcePlan, WorkbookStrategy, _structured_exceptions,
    )
    from hotel_pl_normalizer.pipeline import NormalizationResult
    from hotel_pl_normalizer.run_log import build_run_log

    coa = {TARGET: {"coa_id": TARGET, "parent_coa_id": "", "account_name": "F&B Revenue"}}
    plan = WorkbookSourcePlan(
        plan_id="initial", workbook_id="wb", strategy=WorkbookStrategy(
            reporting_layout="test", summary_source="test", ood_misc_summary_mode="separate"),
        decisions=[AccountSourceDecision(coa_id=TARGET, operation="sum", source_rows=["Outlet!1", "Outlet!2"])],
        source_controls=[control()],
    )
    checked = run_checks(plan=plan, evidence=evidence(), coa=coa, period_labels=LABELS,
                         expected_workbook_id="wb", stage="final", preserve_blanks=True)
    assert checked.values_by_period["actual"][TARGET] == 100
    checks = [item for item in checked.findings_by_period["actual"] if item.rule == "source_control_difference"]
    assert len(checks) == 1
    assert _structured_exceptions({"actual": checks}, LABELS, [{
        "kind": "source_discrepancy", "coa_ids": [TARGET], "message": "Unrelated comparison",
    }]) == []
    result = NormalizationResult(
        workbook_id="wb", source_name="test.xlsx", period_label="2025 Actual",
        values={TARGET: 100}, coa=coa, source_controls=[control()],
    )
    assert build_run_log(result)["source_controls"] == [control().model_dump(mode="json")]


def test_mapping_patch_preserves_controls_unless_explicitly_replaced():
    from hotel_pl_normalizer.mapping.mapper import (
        AccountSourceDecision, WorkbookMappingValidator, WorkbookSourcePlan, WorkbookStrategy,
    )
    coa = {TARGET: {"coa_id": TARGET, "parent_coa_id": ""}}
    validator = WorkbookMappingValidator("wb", evidence(), coa, LABELS)
    validator.current_plan = WorkbookSourcePlan(
        plan_id="initial", workbook_id="wb", strategy=WorkbookStrategy(
            reporting_layout="test", summary_source="test", ood_misc_summary_mode="separate"),
        decisions=[AccountSourceDecision(coa_id=TARGET, operation="direct", source_rows=["Outlet!1"])],
        source_controls=[control()],
    )
    patch = {"patch_id": "next", "workbook_id": "wb", "repair_hypothesis": "Adjust chosen row",
             "expected_fix": "Use the correct subtotal", "replacements": [
                 {"coa_id": TARGET, "operation": "direct", "source_rows": ["Outlet!4"]}]}
    updated, _ = validator._apply_patch(patch)
    assert updated.source_controls == [control()]
    validator.current_plan = updated
    updated, _ = validator._apply_patch({**patch, "replacements": [], "source_controls": []})
    assert updated.source_controls == []
