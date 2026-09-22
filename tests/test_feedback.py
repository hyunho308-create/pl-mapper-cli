from __future__ import annotations

import pytest

from hotel_pl_normalizer.feedback import (
    MAPPING_TREATMENT,
    SOURCE_PRESENTATION,
    UNCLASSIFIED_REVIEW,
    compose_feedback,
    load_canonical_coa,
)
from hotel_pl_normalizer.mapping.findings import Finding

COA = load_canonical_coa()
LABELS = {"actual": "2025 Actual", "budget": "2025 Budget"}


@pytest.mark.parametrize("reverse", [False, True])
def test_typed_summary_comparison_names_both_sides_and_merges_duplicate(reverse):
    summary, detail = "S12.total_food_and_beverage_expenses", "S2.total_food_and_beverage_expenses"
    selected, alternate = (["Detail!1"], ["Summary!1"]) if reverse else (["Summary!1"], ["Detail!1"])
    review = {"kind": "source_discrepancy", "message": "Summary controls Summary; detail controls detail.",
        "mapping_treatment": "Summary components control Summary; Detail controls F&B.",
        "coa_ids": [summary, detail], "period_ids": ["actual"],
        "source_rows": ["Summary!1", "Detail!1"], "selected_source_rows": selected,
        "alternate_source_rows": alternate, "selected_source_operation": "direct",
        "alternate_source_operation": "direct"}
    bundle = compose_feedback(
        checks_by_period={"actual": [Finding("error", "summary_department", summary,
            {"actual": 10000, "expected": 6241, "variance": 3759})]},
        review_items=[review], exceptions=[], execution_issues=[], execution_issues_by_period={},
        period_labels=LABELS, coa=COA,
        evidence_rows=[{"row_key": k, "selected_values": {"actual": n}} for k,n in
                       [("Summary!1", 10000), ("Detail!1", 6241)]],
        values_by_period={"actual": {summary: 10000, detail: 6241}},
    )
    visible = [f for f in bundle.findings if f.destination != "internal_only"]
    assert len(visible) == 1
    assert visible[0].rendered_text == "F&B department expenses are below Summary by 3,759 in 2025 Actual."
    assert visible[0].affected_coa_ids == [detail]
    assert len(bundle.inputs) == 2


def test_unreached_coverage_review_is_audited_when_another_error_stopped_the_run():
    bundle = _compose(checks={"actual": [
        Finding("info", "coverage_review_not_completed", "mapping_session", note="detail review was not reached"),
        Finding("error", "summary_department", "S12.total_rooms_expenses",
                {"actual": 1000, "expected": 900, "variance": 100}),
    ]})
    assert bundle.rendered_count == 1
    assert any(f.destination == "internal_only" for f in bundle.findings)


def test_named_comparison_handles_a_missing_period_without_inventing_zero():
    review = _review("source_discrepancy", "Two reported expense totals differ.", ["S8.other_expenses"],
                     source_rows=["Integrated!10", "Schedule!20"])
    review.update(selected_source_rows=["Integrated!10"], alternate_source_rows=["Schedule!20"],
                  selected_source_operation="direct", alternate_source_operation="direct")
    bundle = _compose(reviews=[review], evidence=[
        {"row_key": "Integrated!10", "label": "Maintenance expenses", "selected_values": {"actual": 100, "budget": None}},
        {"row_key": "Schedule!20", "label": "Total maintenance", "selected_values": {"actual": 80, "budget": 90}},
    ])
    note = bundle.findings[0].rendered_text
    assert "Maintenance expenses on Integrated" in note
    assert "Total maintenance on Schedule" in note
    assert "20 higher in 2025 Actual" in note
    assert "Could not verify this comparison for 2025 Budget" in note
    assert "alternate source" not in note


@pytest.mark.parametrize("variance,direction", [(106, "below"), (-106, "above")])
def test_review_comparison_cannot_reverse_summary_department_direction(variance, direction):
    summary = "S12.total_food_and_beverage_expenses"
    detail = "S2.total_food_and_beverage_expenses"
    review = {
        "review_item_id": "review:direction", "kind": "source_discrepancy",
        "message": "The schedule and Summary differ.", "coa_ids": [summary, detail],
        "period_ids": ["actual"], "source_rows": ["Detail!1", "Summary!1"],
        "selected_source_rows": ["Detail!1"], "alternate_source_rows": ["Summary!1"],
        "selected_source_operation": "direct", "alternate_source_operation": "direct",
    }
    bundle = _compose(reviews=[review], evidence=[
        {"row_key": "Summary!1", "selected_values": {"actual": 1000}},
        {"row_key": "Detail!1", "selected_values": {"actual": 1000 - variance}},
    ], checks={"actual": [
        Finding("warning", "small_source_reconciliation_difference", summary,
                details={"rule": "summary_department", "actual": 1000,
                         "expected": 1000 - variance, "variance": variance},
                review_item_id="review:direction"),
        f"warning|small_source_reconciliation_difference|S12.total_departmental_expenses|"
        f"rule=summary_department|actual=1000|expected={1000-variance}|variance={variance}",
    ]})
    assert len(bundle.findings) == 1
    assert bundle.findings[0].rendered_text == (
        f"F&B department expenses are {direction} Summary by 106 in 2025 Actual."
    )


def test_routine_kpi_treatment_is_audited_but_real_kpi_warning_survives():
    bundle = _compose(
        reviews=[_review("unusual_convention", "Ratios calculated from rooms and revenue.", ["S12.occupancy"])],
        checks={"actual": ["warning|occupancy_above_capacity|S12.occupancy|occupancy=1.2"]},
    )
    visible = [f for f in bundle.findings if f.destination != "internal_only"]
    assert len(visible) == 1 and visible[0].severity == "warning"
    assert bundle.unmatched_count == 0


def test_small_summary_difference_routes_to_detail_and_collapses_proven_consequence():
    bundle = compose_feedback(
        checks_by_period={p: [
            f"warning|small_source_reconciliation_difference|{target}|rule=summary_department|actual=1000|expected={1000-variance}|variance={variance}"
            for target in ["S12.total_food_and_beverage_expenses", "S12.total_departmental_expenses"]
        ] for p, variance in [("prior", 106), ("current", 141)]},
        review_items=[], exceptions=[], execution_issues=[], execution_issues_by_period={},
        period_labels={"prior": "July 2025 YTD Actual", "current": "July 2026 YTD Actual"}, coa=COA,
    )
    assert len(bundle.findings) == 1
    finding = bundle.findings[0]
    assert finding.primary_coa_id == "S2.total_food_and_beverage_expenses"
    assert finding.affected_coa_ids == [finding.primary_coa_id]
    assert finding.rendered_text == "F&B department expenses are below Summary by 106 in 2025 and 141 in 2026."
    assert len(bundle.inputs) == 4
    assert {p.period_label for p in finding.periods} == {"July 2025 YTD Actual", "July 2026 YTD Actual"}


def test_review_repeated_across_periods_is_shown_once():
    review = _review("unusual_convention", "An allocation needs confirmation.", ["S1.other_expenses"])
    review["period_ids"] = ["actual", "budget"]
    finding = _compose(reviews=[review]).findings[0]
    assert finding.rendered_text == review["message"]
    assert len(finding.periods) == 2


def test_different_period_treatments_remain_distinguishable():
    first = _review("unusual_convention", "First allocation.", ["S1.other_expenses"])
    second = _review("unusual_convention", "Second allocation.", ["S1.other_expenses"])
    first["period_ids"], second["period_ids"] = ["actual"], ["budget"]
    bundle = _compose(reviews=[first, second])
    assert {f.rendered_text for f in bundle.findings} == {
        "2025 Actual: First allocation.", "2025 Budget: Second allocation."
    }


def test_routine_kpi_comment_alone_is_hidden_but_preserved_in_audit():
    bundle = _compose(reviews=[_review(
        "unusual_convention", "Ratios calculated from source counts.", ["S12.occupancy"]
    )])
    assert bundle.rendered_count == 0
    assert bundle.findings[0].destination == "internal_only"
    assert bundle.inputs[0].status == "internal_only"


def test_source_comparison_is_not_described_as_a_final_department_difference():
    summary = "S12.total_non_operating_income_and_expenses"
    detail = "S11.total_non_operating_income_and_expenses"
    review = _review("source_discrepancy", "Retain two different bases.", [summary, detail],
                     source_rows=["Summary!60", "Expenses!18"])
    review.update(selected_source_rows=["Summary!60"], alternate_source_rows=["Expenses!18"],
                  selected_source_operation="direct", alternate_source_operation="direct",
                  period_ids=["actual"], mapping_treatment="Retain two different bases.")
    bundle = compose_feedback(
        checks_by_period={}, review_items=[review],
        exceptions=[_exception("source_layer_conflict", "actual", summary, 100000,
            "Retain two different bases.", reported=1000000, comparison=900000,
            source_rows=["Summary!60", "Expenses!18"])],
        execution_issues=[], execution_issues_by_period={}, period_labels=LABELS, coa=COA,
        values_by_period={"actual": {summary: 1000000, detail: 1000000}},
    )
    finding = bundle.findings[0]
    assert finding.primary_coa_id == detail
    assert finding.affected_coa_ids == [detail]
    assert "match Summary" in finding.rendered_text
    assert "separately reported Expenses subtotal" in finding.rendered_text
    assert "100,000" in finding.rendered_text
    assert "Retain two different bases" not in finding.rendered_text


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


def _compose(*, checks=None, reviews=None, exceptions=None, issues=None, by_period=None, evidence=None):
    return compose_feedback(
        checks_by_period=checks or {},
        review_items=reviews or [],
        exceptions=exceptions or [],
        execution_issues=issues or [],
        execution_issues_by_period=by_period or {},
        period_labels=LABELS,
        coa=COA,
        evidence_rows=evidence,
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
    assert "mapped amount differs from the cited source comparison" in finding.rendered_text
    assert "S12" not in finding.rendered_text
    assert "Summary!20" in finding.source_refs
    assert "1,250" not in finding.rendered_text  # unverified model amount
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
    assert finding.destination == "run_notes"
    assert bundle.inputs[0].status == "rendered"
    assert bundle.unmatched_count == 0


def test_short_authored_review_names_only_its_explicit_periods():
    target = "S1.other_expenses"
    review = _review("unusual_convention", "Confirm the operator's allocation. " * 12, [target])
    review["period_ids"] = ["budget"]
    original = review["message"]
    finding = _compose(reviews=[review]).findings[0]
    assert [item.period_id for item in finding.periods] == ["budget"]
    assert finding.rendered_text.startswith("Confirm the operator's allocation.")
    assert "2025 Actual" not in finding.rendered_text
    assert len(finding.rendered_text) <= 180
    assert review["message"] == original  # The audit retains the full original.


def test_period_review_does_not_replace_or_relabel_numeric_errors():
    target = "S1.other_expenses"
    review = _review("unusual_convention", "Confirm the operator's allocation.", [target])
    review["period_ids"] = ["budget"]
    bundle = _compose(
        reviews=[review],
        checks={"actual": [f"error|hierarchy_complete|{target}|parent=1000|children=600|variance=400"]},
    )
    error = next(item for item in bundle.findings if item.severity == "error")
    assert "400" in error.rendered_text and "2025 Actual" in error.rendered_text
    assert [item.period_id for item in error.periods] == ["actual"]
    assert any(item.rendered_text == "Confirm the operator's allocation." for item in bundle.findings)


def test_scoped_source_comparison_does_not_borrow_other_period_evidence():
    target = "S1.total_rooms_revenue"
    review = {
        "kind": "source_discrepancy", "message": "Reported totals differ.",
        "period_ids": ["actual"], "coa_ids": [target],
        "source_rows": ["Summary!9", "Rooms!12"],
        "selected_source_rows": ["Summary!9"], "alternate_source_rows": ["Rooms!12"],
        "selected_source_operation": "direct", "alternate_source_operation": "direct",
    }
    bundle = _compose(reviews=[review], evidence=[
        {"row_key": "Summary!9", "selected_values": {"actual": 1000, "budget": 5000}},
        {"row_key": "Rooms!12", "selected_values": {"actual": 750, "budget": 6000}},
    ])
    finding = bundle.findings[0]
    assert [item.period_id for item in finding.periods] == ["actual"]
    assert "250 higher in 2025 Actual" in finding.rendered_text
    assert "Budget" not in finding.rendered_text


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
    assert "reported total is retained" not in text
    assert "66 lower in 2025 Actual" in text


def test_generic_coverage_instruction_is_replaced_by_the_actual_difference():
    target = "S1.total_rooms_expenses"
    treatment = "Preserve supported source detail and flag the incomplete coverage."
    bundle = _compose(
        checks={
            "actual": [
                _check(
                    "source_detail_incomplete",
                    target,
                    20.0,
                    parent=100.0,
                    children=80.0,
                )
            ]
        },
        exceptions=[
            _exception(
                "source_detail_incomplete",
                "actual",
                target,
                20.0,
                treatment,
            )
        ],
    )

    finding = bundle.findings[0]
    assert finding.destination == f"coa:{target}"
    assert finding.rendered_text == (
        "Identified children are 20 below the parent in 2025 Actual."
    )
    assert treatment not in finding.rendered_text


def test_derived_summary_link_collapses_a_proven_downstream_repeat():
    detail = "S3.total_other_operated_departments_revenue"
    summary = "S12.total_other_operated_departments_revenue"
    message = "The OOD schedule differs from an alternate source presentation."
    bundle = _compose(
        reviews=[_review("source_discrepancy", message, [detail])],
        exceptions=[
            _exception("source_discrepancy", "actual", detail, 125.0, message),
            _exception(
                "source_discrepancy",
                "actual",
                summary,
                125.0,
                "The same source difference reaches the Summary OOD account.",
            ),
        ],
    )

    assert len(bundle.findings) == 1
    assert summary in bundle.findings[0].affected_coa_ids
    assert "also affects Total Other Operated Departments Revenue" in (
        bundle.findings[0].rendered_text
    )
    assert any(item.status == "consequence_of" for item in bundle.inputs)


def test_duplicate_equivalent_exceptions_join_one_check_without_repetition():
    target = "S12.total_revenue"
    exception = _exception(
        "small_source_reconciliation_difference",
        "actual",
        target,
        11.0,
        "The reported total is retained.",
    )
    bundle = _compose(
        checks={
            "actual": [
                _check(
                    "small_source_reconciliation_difference",
                    target,
                    11.0,
                )
            ]
        },
        exceptions=[exception, dict(exception)],
    )

    assert len(bundle.findings) == 1
    assert len(bundle.inputs) == 3
    assert sum(item.status == "superseded_by" for item in bundle.inputs) == 1


def _source_comparison_review():
    return {
        **_review(
            "source_discrepancy", "The two source presentations differ.",
            ["S2.total_food_and_beverage_revenue"],
            source_rows=["Outlet!10", "Outlet!11", "Outlet!20"],
        ),
        "selected_source_rows": ["Outlet!10", "Outlet!11"],
        "alternate_source_rows": ["Outlet!20"],
        "selected_source_operation": "sum",
        "alternate_source_operation": "direct",
    }


def _comparison_evidence(budget=500.0):
    return [
        {"row_key": "Outlet!10", "selected_values": {"actual": 60.1, "budget": 300.0}},
        {"row_key": "Outlet!11", "selected_values": {"actual": 40.34, "budget": 200.0}},
        {"row_key": "Outlet!20", "selected_values": {"actual": 100.0, "budget": budget}},
    ]


def test_cited_rounding_difference_is_internal_without_validator_exception():
    bundle = _compose(reviews=[_source_comparison_review()], evidence=_comparison_evidence())

    assert bundle.rendered_count == 0
    assert bundle.inputs[0].status == "internal_only"
    finding = bundle.findings[0]
    assert finding.destination == "internal_only"
    assert finding.periods[0].variance == pytest.approx(0.44)
    assert finding.periods[1].variance == 0.0
    assert finding.explanation
    assert len(finding.source_refs) == 3


@pytest.mark.parametrize("budget", [490.0, None])
def test_material_or_unknown_second_period_keeps_review_visible(budget):
    bundle = _compose(reviews=[_source_comparison_review()], evidence=_comparison_evidence(budget))
    assert bundle.rendered_count == 1
    assert bundle.inputs[0].status == "rendered"
    assert "0 higher in 2025 Actual" not in bundle.findings[0].rendered_text


def test_missing_period_key_does_not_reuse_primary_value_to_hide_review():
    evidence = _comparison_evidence()
    evidence[-1]["selected_values"].pop("budget")
    evidence[-1]["selected_value"] = 500.0
    bundle = _compose(reviews=[_source_comparison_review()], evidence=evidence)
    assert bundle.rendered_count == 1


def test_rounding_word_alone_never_hides_a_review():
    review = _source_comparison_review()
    review["message"] = "A small rounding difference should be ignored."
    bundle = _compose(reviews=[review])
    assert bundle.rendered_count == 1


def test_invalid_overlapping_source_comparison_is_not_hidden():
    review = _source_comparison_review()
    review["alternate_source_rows"] = ["Outlet!10", "Outlet!11"]
    review["alternate_source_operation"] = "sum"
    bundle = _compose(reviews=[review], evidence=_comparison_evidence())
    assert bundle.rendered_count == 1


def test_adjusted_source_comparison_uses_excluded_rows():
    review = _source_comparison_review()
    review.update(
        selected_source_rows=["Outlet!20"], selected_excluded_rows=["Outlet!10"],
        selected_source_operation="adjusted_subtotal", alternate_source_rows=["Outlet!11"],
    )
    bundle = _compose(reviews=[review], evidence=_comparison_evidence())
    assert bundle.rendered_count == 0
    assert bundle.findings[0].periods[0].variance == pytest.approx(-0.44)


def test_material_validator_conflict_cannot_be_hidden_by_review_equation():
    review = _source_comparison_review()
    bundle = _compose(
        reviews=[review], evidence=_comparison_evidence(),
        exceptions=[_exception("source_layer_conflict", "actual", review["coa_ids"][0], 50.0, review["message"])],
    )
    assert bundle.rendered_count == 1


def test_presentation_and_treatment_merge_only_for_the_same_cited_adjustment():
    discrepancy = _source_comparison_review()
    evidence = _comparison_evidence(300.0)
    evidence[-1]["selected_values"]["actual"] = 60.1
    treatment = _review(
        "unusual_convention", "The separate service adjustment is included once in the expense rollup.",
        discrepancy["coa_ids"], source_rows=["Outlet!11"],
    )
    unrelated = _review(
        "source_discrepancy", "A separate schedule has an unresolved conflict.",
        discrepancy["coa_ids"], source_rows=["Other schedule!40"],
    )
    bundle = _compose(reviews=[discrepancy, treatment, unrelated], evidence=evidence)
    assert bundle.rendered_count == 2
    merged = next(item for item in bundle.findings if len(item.source_input_ids) == 2)
    assert merged.explanation == treatment["message"]
    assert merged.periods[0].variance == pytest.approx(40.34)
    assert set(merged.source_refs) == set(discrepancy["source_rows"])
    assert any(item.status == "superseded_by" for item in bundle.inputs)


def test_shared_account_and_row_do_not_merge_a_different_adjustment():
    discrepancy = _source_comparison_review()
    treatment = _review("unusual_convention", "A different treatment applies.", discrepancy["coa_ids"], source_rows=["Outlet!11"])
    bundle = _compose(reviews=[discrepancy, treatment], evidence=_comparison_evidence(490.0))
    assert bundle.rendered_count == 2


def test_separate_treatment_survives_rounding_without_a_numeric_warning():
    review = {**_source_comparison_review(), "mapping_treatment": "The facility fee is included in miscellaneous income."}
    bundle = _compose(reviews=[review], evidence=_comparison_evidence())
    finding = bundle.findings[0]
    assert finding.category == MAPPING_TREATMENT
    assert bundle.rendered_count == 1
    assert "facility fee" in finding.rendered_text
    assert "difference" not in finding.rendered_text
    assert finding.periods[0].variance == pytest.approx(.44)


def test_legacy_adjustment_treatment_reconstructed_from_labels_not_model_prose():
    review = _source_comparison_review()
    review.update(selected_source_rows=["Outlet!20"], selected_excluded_rows=["Outlet!10"],
                  selected_source_operation="adjusted_subtotal", alternate_source_rows=["Outlet!11"])
    evidence = _comparison_evidence()
    for row, label in zip(evidence, ["Facility fees", "Operating revenue", "Combined revenue"]):
        row["label"] = label
    bundle = _compose(reviews=[review], evidence=evidence)
    assert bundle.findings[0].category == MAPPING_TREATMENT
    assert bundle.findings[0].rendered_text == "Mapped from Combined revenue, less Facility fees."


def test_populated_parent_owns_note_and_combines_unsplit_coverage():
    parent = "S2.management"
    review = _review("unusual_convention", "Management wages are mapped without a service/kitchen split.",
                     [parent, "S2.service_management", "S2.kitchen_management"])
    bundle = compose_feedback(
        checks_by_period={"actual": [_check("source_detail_incomplete", parent, 100, parent=100, children=0)]},
        review_items=[review], exceptions=[], execution_issues=[], execution_issues_by_period={},
        period_labels=LABELS, coa=COA, values_by_period={"actual": {parent: 100}},
    )
    assert bundle.rendered_count == 1
    finding = bundle.findings[0]
    assert finding.primary_coa_id == parent
    assert "No child breakdown mapped: 100 in 2025 Actual" in finding.rendered_text
    assert "service/kitchen split" in finding.rendered_text
    assert len(finding.source_input_ids) == 2


def test_residual_and_comparison_share_one_note_only_on_the_same_account():
    target = "S3.total_other_operated_departments_expenses"
    review = _review("source_discrepancy", "Inactive schedule caused the gap.", [target])
    bundle = _compose(
        reviews=[review], exceptions=[_exception("source_layer_conflict", "actual", target, 20,
                                              review["message"], reported=100, comparison=80)],
        checks={"actual": [f"warning|unsupported_residual_remainder|{target}|remainder=20|ratio=0.2"]},
    )
    assert bundle.rendered_count == 1
    assert "Inactive" not in bundle.findings[0].rendered_text
    assert len(bundle.findings[0].source_input_ids) == 3
