from types import SimpleNamespace

import pytest

from hotel_pl_normalizer.mapping.mapper import (
    AccountSourceDecision,
    ChildCoverage,
    MappingReviewItem,
    SourceOperation,
    WorkbookStrategy,
    WorkbookSourcePlan,
    WorkbookMappingValidator,
    _apply_residual_plugs,
    _execute,
    _kpi_sanity_issues,
    _non_residual_plug_issues,
    _unresolved_negative_residuals,
    _validate,
)
from hotel_pl_normalizer.mapping.reviews import normalize_review_item
from hotel_pl_normalizer.mapping.checks import run_checks


@pytest.mark.parametrize('raw', ['-', '- ', '\u2013', '\u2014', '\u2212', None])
def test_accounting_zero_dash_is_distinct_from_missing_value(raw):
    decision = AccountSourceDecision(
        coa_id='segment', operation=SourceOperation.DIRECT, source_rows=['Schedule!6'],
    )
    values, issues = _execute(
        [decision], [{'row_key': 'Schedule!6', 'selected_value': raw}],
        {'segment': {}}, preserve_blanks=True,
    )
    assert issues == []
    assert values['segment'] == (None if raw is None else 0)


def test_impossible_occupancy_warns_even_when_division_ties():
    values = {'S12.rooms_sold': 240, 'S12.rooms_available': 100, 'S12.occupancy': 2.4}
    findings = _kpi_sanity_issues(values)
    assert [item.rule for item in findings] == ['occupancy_above_capacity']
    assert values['S12.occupancy'] == 2.4
    assert not _kpi_sanity_issues({**values, 'S12.occupancy': 1.0})


def test_sold_rooms_with_zero_capacity_warns_without_inventing_occupancy():
    values = {'S12.rooms_sold': 240, 'S12.rooms_available': 0, 'S12.occupancy': 0}
    assert [item.rule for item in _kpi_sanity_issues(values)] == ['invalid_rooms_available']
    assert not _kpi_sanity_issues({})


def test_rounding_difference_is_not_added_to_direct_residual_detail():
    coa = {
        'total': {}, 'named': {'parent_coa_id': 'total'},
        'other': {'parent_coa_id': 'total', 'is_residual': 'true'},
    }
    parent = AccountSourceDecision(
        coa_id='total', operation=SourceOperation.DIRECT,
        source_rows=['Schedule!9'], child_coverage=ChildCoverage.PARTIAL,
    )
    values = {'total': 1001, 'named': 800, 'other': 200}
    assert not _apply_residual_plugs(values, coa, [parent], max_ratio=None)
    assert values['other'] == 200
    strategy = WorkbookStrategy(
        reporting_layout='test', summary_source='test', ood_misc_summary_mode='separate',
    )
    assert not any(item.rule == 'hierarchy_partial_with_residual'
                   for item in _validate(values, coa, [parent], strategy))
    assert not _unresolved_negative_residuals(
        {'total': 1, 'named': 2, 'other': 0}, coa, [parent],
    )
    values['total'] = 1010
    assert _apply_residual_plugs(values, coa, [parent], max_ratio=None) == {'other': 10}


def test_named_leaf_cannot_be_used_as_a_parent_minus_siblings_plug():
    coa = {
        'total': {}, 'known': {'parent_coa_id': 'total'},
        'named': {'parent_coa_id': 'total', 'is_residual': 'false'},
    }
    decisions = [
        AccountSourceDecision(coa_id='total', operation='direct', source_rows=['Schedule!9']),
        AccountSourceDecision(coa_id='known', operation='direct', source_rows=['Schedule!6']),
        AccountSourceDecision(coa_id='named', operation='adjusted_subtotal',
                              source_rows=['Schedule!9'], excluded_rows=['Schedule!6']),
    ]
    plan = SimpleNamespace(decisions=decisions)
    assert [item.rule for item in _non_residual_plug_issues(plan, coa)] == ['non_residual_plug']
    coa['named']['is_residual'] = 'true'
    assert not _non_residual_plug_issues(plan, coa)
    coa['named']['is_residual'] = 'false'
    # A target's own gross amount net of its own allowance remains legitimate.
    decisions[-1] = decisions[-1].model_copy(update={'source_rows': ['Schedule!7']})
    assert not _non_residual_plug_issues(plan, coa)


@pytest.mark.parametrize("comparison", [
    {},
    {"selected_source_rows": ["Summary!9"]},
    {"alternate_source_rows": ["Detail!12"]},
])
def test_new_source_discrepancy_requires_a_numeric_comparison(comparison):
    with pytest.raises(ValueError, match="Provide the cited numeric comparison"):
        MappingReviewItem(
            kind="source_discrepancy",
            message="The totals differ by 0.44.",
            coa_ids=["total"],
            source_rows=["Summary!9", "Detail!12"],
            **comparison,
        )


def test_supported_source_discrepancy_keeps_typed_comparison():
    review = MappingReviewItem(
        kind="source_discrepancy", message="Reported totals differ.",
        coa_ids=["total"], source_rows=["Summary!9", "Detail!12"],
        selected_source_rows=["Summary!9"], alternate_source_rows=["Detail!12"],
        selected_source_operation="direct", alternate_source_operation="direct",
    )
    assert review.selected_source_rows == ["Summary!9"]
    assert review.alternate_source_rows == ["Detail!12"]


def test_nonnumeric_treatment_and_legacy_review_remain_available():
    treatment = MappingReviewItem(
        kind="unusual_convention", message="Retain the operator's presentation.",
        coa_ids=["total"],
    )
    legacy = {"kind": "source_discrepancy", "message": "Historical unverified note.",
              "coa_ids": ["total"], "source_rows": []}

    assert treatment.selected_source_rows == []
    assert normalize_review_item(legacy).message == "Historical unverified note."
    assert normalize_review_item(legacy).kind == "source_discrepancy"


@pytest.mark.parametrize("scope", [["cur"], ["missing"]])
def test_review_scope_limits_source_exceptions_and_rejects_unknown_periods(scope):
    review = MappingReviewItem(
        kind="source_discrepancy", message="Reported totals differ.", period_ids=scope,
        coa_ids=["total"], source_rows=["Summary!9", "Detail!12"],
        selected_source_rows=["Summary!9"], alternate_source_rows=["Detail!12"],
        selected_source_operation="direct", alternate_source_operation="direct",
    )
    plan = WorkbookSourcePlan(
        plan_id="p", workbook_id="wb",
        strategy=WorkbookStrategy(reporting_layout="test", summary_source="test", ood_misc_summary_mode="separate"),
        decisions=[AccountSourceDecision(coa_id="total", operation="direct", source_rows=["Summary!9"])],
        review_items=[review],
    )
    checked = run_checks(
        plan=plan, coa={"total": {}}, expected_workbook_id="wb", stage="session",
        period_labels={"cur": "2025 Actual", "pri": "2024 Actual"}, preserve_blanks=True,
        evidence=[
            {"row_key": "Summary!9", "selected_values": {"cur": 1000, "pri": 5000}},
            {"row_key": "Detail!12", "selected_values": {"cur": 750, "pri": 6000}},
        ],
    )
    assert not any(f.rule == "source_layer_conflict" for f in checked.findings_by_period["pri"])
    if scope == ["cur"]:
        assert any(f.rule == "source_layer_conflict" for f in checked.findings_by_period["cur"])
        assert checked.execution_issues == []
    else:
        assert "review items cite unknown period ids: missing" in checked.execution_issues


def test_model_review_schema_requires_selected_periods_and_short_notes():
    validator = WorkbookMappingValidator("wb", [], {"total": {}}, period_labels={"cur": "2025 Actual"})
    declaration = next(item for item in validator.declarations() if item["name"] == "validate_mapping")
    review = declaration["parameters"]["properties"]["review_items"]["items"]
    assert "period_ids" in review["required"]
    assert review["properties"]["period_ids"]["items"]["enum"] == ["cur"]
    assert review["properties"]["message"]["maxLength"] == 180
