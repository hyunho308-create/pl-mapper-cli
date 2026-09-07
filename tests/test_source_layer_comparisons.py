from types import SimpleNamespace

import pytest

from hotel_pl_normalizer.feedback import compose_feedback, load_canonical_coa
from hotel_pl_normalizer.mapping.mapper import (
    AccountSourceDecision,
    MappingReviewItem,
    _normalize_patch_review_items,
    _source_layer_comparison_issues,
    _source_layer_conflict_warnings,
    _source_layer_overlap_is_valid,
)


TARGET = "S3.total_other_operated_departments_revenue"


def comparison_review():
    return {
        "kind": "source_discrepancy", "message": "The independently supported totals differ.",
        "coa_ids": [TARGET],
        "source_rows": ["Summary!1", "Detail!1", "Detail!3", "Other!8"],
        "selected_source_rows": ["Summary!1", "Detail!3"],
        "selected_excluded_rows": ["Other!8"],
        "selected_source_operation": "adjusted_subtotal",
        "alternate_source_rows": ["Detail!1", "Detail!3"],
        "alternate_excluded_rows": [],
        "alternate_source_operation": "sum",
    }


def evidence(budget_detail=999.0):
    return [
        {"row_key": key, "selected_values": {"actual": actual, "budget": budget}}
        for key, actual, budget in [
            ("Summary!1", 1000.0, 1200.0), ("Detail!1", 799.0, budget_detail),
            ("Detail!3", 30.0, 40.0), ("Other!8", 200.0, 200.0),
        ]
    ]


def plan(review=None, source_rows=None):
    return SimpleNamespace(
        decisions=[AccountSourceDecision(
            coa_id=TARGET, operation="adjusted_subtotal",
            source_rows=source_rows or ["Summary!1", "Detail!3"], excluded_rows=["Other!8"],
        )],
        review_items=[MappingReviewItem.model_validate(review or comparison_review())],
    )


def test_shared_addition_survives_patch_normalization_and_selected_validation():
    original = comparison_review()
    assert _normalize_patch_review_items([original]) == [original]
    for period, mapped in [("actual", 830.0), ("budget", 1040.0)]:
        assert _source_layer_comparison_issues(plan(), evidence(), {TARGET: mapped}, period) == []
        assert _source_layer_conflict_warnings(plan(), evidence(), {TARGET: mapped}, period) == []


def test_shared_subtraction_is_valid():
    assert _source_layer_overlap_is_valid(
        ["Summary!1"], ["Common!2"], "adjusted_subtotal",
        ["Detail!1"], ["Common!2"], "adjusted_subtotal",
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"alternate_source_rows": ["Detail!3", "Summary!1"], "alternate_excluded_rows": ["Other!8"], "alternate_source_operation": "adjusted_subtotal"},
        {"alternate_source_rows": ["Detail!1"], "alternate_excluded_rows": ["Detail!3"], "alternate_source_operation": "adjusted_subtotal"},
        {"alternate_source_operation": "negate"},
        {"selected_source_rows": ["Summary!1", "Detail!3", "Detail!3"]},
        {"alternate_source_rows": ["Detail!1", "Detail!1", "Detail!3"]},
        {"selected_excluded_rows": ["Other!8", "Detail!3"]},
    ],
)
def test_invalid_shared_equations_remain_rejected(changes):
    raw = {**comparison_review(), **changes}
    with pytest.raises(ValueError, match="distinct equations"):
        MappingReviewItem.model_validate(raw)
    [normalized] = _normalize_patch_review_items([raw])
    assert normalized["selected_source_rows"] == []
    with pytest.raises(ValueError, match="requires selected_source_rows"):
        MappingReviewItem.model_validate(normalized)


def test_common_terms_do_not_relax_selected_equation_validation():
    issues = _source_layer_comparison_issues(plan(), evidence(), {TARGET: 930.0}, "actual")
    assert [issue.rule for issue in issues] == ["invalid_source_layer_comparison"]
    assert "does not equal the mapped target" in issues[0].note
    issues = _source_layer_comparison_issues(
        plan(source_rows=["Summary!1"]), evidence(), {TARGET: 830.0}, "actual"
    )
    assert [issue.rule for issue in issues] == ["invalid_source_layer_comparison"]
    assert "not contained in the mapped target decision" in issues[0].note


@pytest.mark.parametrize("budget_detail, visible", [(999.0, 0), (950.0, 1), (None, 1)])
def test_common_terms_suppress_only_proven_rounding_in_every_period(budget_detail, visible):
    bundle = compose_feedback(
        checks_by_period={}, review_items=[comparison_review()], exceptions=[],
        execution_issues=[], execution_issues_by_period={},
        period_labels={"actual": "Actual", "budget": "Budget"},
        coa=load_canonical_coa(), evidence_rows=evidence(budget_detail),
    )
    assert bundle.rendered_count == visible
    if not visible:
        assert bundle.inputs[0].status == "internal_only"
        assert [period.variance for period in bundle.findings[0].periods] == [1.0, 1.0]
        assert set(bundle.findings[0].source_refs) == set(comparison_review()["source_rows"])
