from __future__ import annotations

from hotel_pl_normalizer.feedback import (
    MAPPING_TREATMENT,
    SOURCE_PRESENTATION,
    UNCLASSIFIED_REVIEW,
    compose_feedback,
    load_canonical_coa,
)

COA = load_canonical_coa()
LABELS = {"actual": "2025 Actual", "budget": "2025 Budget"}


def _review(kind, message, coa_ids, *, source_rows=None):
    return {
        "kind": kind,
        "message": message,
        "coa_ids": coa_ids,
        "source_rows": source_rows or [],
        "selected_source_rows": source_rows or [],
        "alternate_source_rows": [],
        "requires_human_decision": False,
    }


def _exception(
    rule,
    period_id,
    target,
    variance,
    treatment,
    *,
    reported=100.0,
    comparison=90.0,
    source_rows=None,
):
    return {
        "rule": rule,
        "period_id": period_id,
        "period_label": LABELS[period_id],
        "target": target,
        "reported_value": reported,
        "comparison_value": comparison,
        "variance": variance,
        "treatment": treatment,
        "source_rows": source_rows or [],
    }


def _check(rule, target, variance, *, parent=None, children=None):
    fields = ["warning", rule, target, f"variance={variance}"]
    if parent is not None:
        fields.append(f"parent={parent}")
    if children is not None:
        fields.append(f"children={children}")
    return "|".join(fields)


def _compose(*, checks=None, reviews=None, exceptions=None, issues=None, by_period=None):
    return compose_feedback(
        checks_by_period=checks or {},
        review_items=reviews or [],
        exceptions=exceptions or [],
        execution_issues=issues or [],
        execution_issues_by_period=by_period or {},
        period_labels=LABELS,
        coa=COA,
    )


def test_source_review_joins_period_math_without_repeating_findings():
    target = "S12.total_revenue"
    message = (
        "Source layer conflict: S12 revenue uses Summary!20 at $1,250.49 instead of "
        "the alternate presentation."
    )
    reviews = [_review("source_discrepancy", message, [target], source_rows=["Summary!20"])]
    exceptions = [
        _exception(
            "source_layer_conflict",
            "actual",
            target,
            43874.12,
            message,
            reported=100000.0,
            comparison=56125.88,
            source_rows=["Summary!20"],
        ),
        _exception(
            "source_layer_conflict",
            "budget",
            target,
            -4788.0,
            message,
            reported=90000.0,
            comparison=94788.0,
            source_rows=["Summary!20"],
        ),
    ]
    checks = {
        "actual": [_check("source_layer_conflict", target, 43874.12)],
        "budget": [_check("source_layer_conflict", target, -4788.0)],
    }

    bundle = _compose(checks=checks, reviews=reviews, exceptions=exceptions)

    assert len(bundle.findings) == 1
    finding = bundle.findings[0]
    assert finding.category == SOURCE_PRESENTATION
    assert len(finding.periods) == 2
    assert "Summary revenue" in finding.rendered_text
    assert "S12" not in finding.rendered_text
    assert "Summary row 20" in finding.rendered_text
    assert "1,250" in finding.rendered_text
    assert "$" not in finding.rendered_text
    assert ".49" not in finding.rendered_text
    assert "43,874 higher in 2025 Actual" in finding.rendered_text
    assert "4,788 lower in 2025 Budget" in finding.rendered_text
    assert len(bundle.inputs) == 5
    assert sum(item.status == "superseded_by" for item in bundle.inputs) == 2
    assert bundle.unmatched_count == 0


def test_same_account_coverage_is_a_consequence_of_source_difference():
    target = "S2.cost_of_other_revenue"
    message = "The controlling subtotal differs from the explicit detail."
    reviews = [_review("source_discrepancy", message, [target])]
    exceptions = [
        _exception("source_layer_conflict", "actual", target, -1777.0, message),
        _exception(
            "source_detail_incomplete",
            "actual",
            target,
            -1777.0,
            "Preserve supported source detail and flag the incomplete coverage.",
        ),
    ]
    checks = {
        "actual": [
            _check("source_layer_conflict", target, -1777.0),
            _check(
                "source_detail_incomplete",
                target,
                -1777.0,
                parent=100.0,
                children=1877.0,
            ),
        ]
    }

    bundle = _compose(checks=checks, reviews=reviews, exceptions=exceptions)

    assert len(bundle.findings) == 1
    assert "also explains the child-to-parent coverage difference" in (
        bundle.findings[0].rendered_text
    )
    assert any(item.status == "consequence_of" for item in bundle.inputs)
    assert bundle.unmatched_count == 0


def test_only_proven_dependency_with_same_variance_is_collapsed():
    root = "S2.total_food_and_beverage_expenses"
    downstream = "S12.total_departmental_expenses"
    unrelated = "S9.total_utilities_expenses"
    message = "Detailed F&B expenses differ from the controlling total."
    reviews = [_review("source_discrepancy", message, [root])]
    exceptions = [
        _exception("source_discrepancy", "actual", root, 2024.0, message),
        _exception(
            "source_discrepancy",
            "actual",
            downstream,
            2024.0,
            "Preserve both independently reported source values for review.",
        ),
        _exception(
            "source_discrepancy",
            "actual",
            unrelated,
            2024.0,
            "An unrelated utilities presentation also differs.",
        ),
    ]

    bundle = _compose(reviews=reviews, exceptions=exceptions)

    assert len(bundle.findings) == 2
    root_finding = next(item for item in bundle.findings if item.primary_coa_id == root)
    assert downstream in root_finding.affected_coa_ids
    assert "also affects Total Departmental Expenses" in root_finding.rendered_text
    assert any(item.primary_coa_id == unrelated for item in bundle.findings)


def test_unknown_review_kind_is_visible_and_actionable():
    bundle = _compose(
        reviews=[_review("new_review_kind", "New condition from the mapper.", [])]
    )

    assert len(bundle.findings) == 1
    finding = bundle.findings[0]
    assert finding.category == UNCLASSIFIED_REVIEW
    assert finding.action_required is True
    assert finding.severity == "warning"
    assert bundle.inputs[0].status == "rendered"
    assert bundle.unmatched_count == 0


def test_flattened_execution_issue_does_not_duplicate_period_issue():
    bundle = _compose(
        issues=["2025 Actual: invalid source row", "global execution failure"],
        by_period={"actual": ["invalid source row"]},
    )

    assert len(bundle.findings) == 2
    messages = [item.rendered_text for item in bundle.findings]
    assert sum("invalid source row" in item for item in messages) == 1
    assert sum("global execution failure" in item for item in messages) == 1
    assert len(bundle.inputs) == 2


def test_distinct_mapping_treatments_are_not_deduplicated():
    target = "S2.total_food_and_beverage_expenses"
    bundle = _compose(
        reviews=[
            _review("unusual_convention", "Cost of sales is added to expense.", [target]),
            _review("unusual_convention", "Outlet allowances remain net in revenue.", [target]),
        ]
    )

    assert len(bundle.findings) == 2
    assert all(item.category == MAPPING_TREATMENT for item in bundle.findings)
    assert {item.explanation for item in bundle.findings} == {
        "Cost of sales is added to expense.",
        "Outlet allowances remain net in revenue.",
    }


def test_identical_check_text_in_two_periods_has_stable_distinct_inputs():
    target = "S1.total_rooms_expenses"
    check = _check(
        "source_detail_incomplete",
        target,
        10.0,
        parent=100.0,
        children=90.0,
    )

    bundle = _compose(checks={"actual": [check], "budget": [check]})

    assert len(bundle.findings) == 1
    assert len(bundle.findings[0].periods) == 2
    assert len({item.input_id for item in bundle.inputs}) == 2


def test_quantified_sentence_replaces_vague_difference_disclosure_clause():
    target = "S4.total_miscellaneous_income"
    message = (
        "The components total 1,021,593 versus the reported total of 1,021,527, "
        "so the reported total is retained and the six-dollar-tens difference "
        "is disclosed."
    )
    bundle = _compose(
        reviews=[_review("source_discrepancy", message, [target])],
        exceptions=[
            _exception(
                "source_layer_conflict",
                "actual",
                target,
                -66.0,
                message,
                reported=1021527.0,
                comparison=1021593.0,
            )
        ],
    )

    text = bundle.findings[0].rendered_text
    assert "six-dollar-tens" not in text
    assert "reported total is retained" in text
    assert "66 lower in 2025 Actual" in text
