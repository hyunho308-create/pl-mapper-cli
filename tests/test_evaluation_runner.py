from __future__ import annotations

import json
import subprocess
from copy import deepcopy

import openpyxl
import pytest
from test_evaluation_mechanical import _excel_row, _row_ref, _write_mapped

from hotel_pl_normalizer.evaluation.report import render_evaluation_markdown
from hotel_pl_normalizer.evaluation.runner import evaluate_run
from hotel_pl_normalizer.mapping.findings import Finding

PARENT = "S1.other_expenses"
LAUNDRY = "S1.laundry_and_dry_cleaning"
UNIFORMS = "S1.rooms_all_other"


@pytest.fixture
def review_log():
    rows = [
        _excel_row(4, "Total Other Expenses", 100),
        _excel_row(5, "Laundry", 70),
        _excel_row(6, "Uniform laundry", 30),
    ]
    for row in rows:
        row["selected_values"]["prior"] = row["selected_value"]
        row["selected_value_columns"]["prior"] = 4
        row["anchors_by_period"]["prior"] = {
            "kind": "excel",
            "column_index": 4,
            "excel_column": "D",
            "display": "D",
        }
    values = {PARENT: 100, LAUNDRY: 70, UNIFORMS: 30}
    return {
        "log_version": 5,
        "source": {
            "name": "source.xlsx",
            "workbook_id": "wb-test",
            "periods": [
                {"period_id": "actual", "label": "Actual"},
                {"period_id": "prior", "label": "Prior"},
            ],
        },
        "outcome": {"accepted": True, "accounts_populated": 3},
        "evidence_rows": rows,
        "accounts": [
            {
                "coa_id": cid,
                "operation": "direct",
                "source_rows": [_row_ref(row)],
                "excluded_rows": [],
                "computed_values": {"actual": value, "prior": value},
            }
            for (cid, value), row in zip(values.items(), rows)
        ],
        "values_by_period": {"actual": values, "prior": dict(values)},
        "feedback_manifest": {"findings": []},
    }


@pytest.fixture
def run_review(tmp_path):
    def run(log, *, expected=None):
        source, mapped, log_path = (
            tmp_path / name for name in ("source.xlsx", "mapped.xlsx", "run_log.json")
        )
        book = openpyxl.Workbook()
        sheet = book.active
        sheet.title = "P&L"
        for row in log["evidence_rows"]:
            number = row["locator"]["row_index"]
            sheet.cell(number, 1, row["label"])
            for pid, column in row["selected_value_columns"].items():
                sheet.cell(number, column, row["selected_values"].get(pid))
        book.save(source)
        book.close()
        _write_mapped(mapped, log)
        log_path.write_text(json.dumps(log), encoding="utf-8")
        before = {p: p.read_bytes() for p in (source, mapped, log_path)}
        result = evaluate_run(source, log_path, mapped, expected_period_ids=expected)
        assert {p: p.read_bytes() for p in before} == before
        assert set(tmp_path.iterdir()) == set(before)
        return result

    return run


def test_review_is_local_and_never_approves(review_log, run_review, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Quick evaluation must not launch any subprocess")

    monkeypatch.setattr(subprocess, "run", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    result = run_review(review_log)
    assert result.status == "ready_for_review"
    assert {c["coa_id"] for c in result.children} == {LAUNDRY, UNIFORMS}
    text = render_evaluation_markdown(result)
    assert "Semantic review: **pending**" in text
    assert "END OF REVIEW EVIDENCE" in text
    assert "approved" not in text


@pytest.mark.parametrize("record_drop", [True, False])
def test_missing_requested_period(review_log, run_review, record_drop):
    review_log["source"]["periods"].pop()
    review_log["values_by_period"].pop("prior")
    if record_drop:
        review_log["dropped_periods"] = {
            "prior": "Could not bind the prior-year column"
        }
    result = run_review(review_log, expected=[] if record_drop else ["actual", "prior"])
    assert result.status == "issues_found"
    assert any(
        f["code"] == "missing_period" and f["periods"] == ["prior"]
        for f in result.findings
    )


def test_child_period_blank_despite_source_amount(review_log, run_review):
    review_log["values_by_period"]["prior"][LAUNDRY] = None
    result = run_review(review_log)
    finding = next(f for f in result.findings if f["code"] == "period_value_missing")
    assert finding["targets"] == [LAUNDRY]
    assert finding["periods"] == ["prior"]
    assert finding["refs"] == ["P&L!5"]


def test_same_label_period_alternative_stays_visible(review_log, run_review):
    review_log["values_by_period"]["prior"][LAUNDRY] = None
    review_log["evidence_rows"][1]["selected_values"]["prior"] = None
    alternative = deepcopy(review_log["evidence_rows"][1])
    alternative["row_key"] = "P&L!8"
    alternative["locator"].update(
        identity="excel:P&L!8", display="P&L row 8", row_index=8
    )
    alternative["selected_values"] = {"actual": None, "prior": 70}
    review_log["evidence_rows"].append(alternative)
    result = run_review(review_log)
    assert any(
        f["code"] == "period_detail_candidate" and "P&L!8" in f["refs"]
        for f in result.findings
    )


def test_dropped_detail_used_only_by_parent_is_visible(review_log, run_review):
    review_log["accounts"][0]["source_rows"].append(
        _row_ref(review_log["evidence_rows"][2])
    )
    review_log["accounts"][2].update(operation="no_value", source_rows=[])
    for values in review_log["values_by_period"].values():
        values[UNIFORMS] = None
    result = run_review(review_log)
    row = next(r for r in result.rows if r["key"] == "P&L!6")
    assert row["candidate"] and row["usage"] == "parent"
    assert row["values"] == [30, 30]


def test_summary_only_source_is_a_question_not_a_defect(review_log, run_review):
    review_log["evidence_rows"] = review_log["evidence_rows"][:1]
    review_log["accounts"] = review_log["accounts"][:1]
    review_log["accounts"][0]["child_coverage"] = "not_present"
    for values in review_log["values_by_period"].values():
        values[LAUNDRY] = values[UNIFORMS] = None
    result = run_review(review_log)
    assert result.status == "ready_for_review"
    assert all(f["severity"] == "warning" for f in result.findings)
    assert any(f["code"] == "parent_without_detail" for f in result.findings)


def test_sibling_swap_cannot_hide_behind_matching_total(review_log, run_review):
    first, second = review_log["accounts"][1:]
    first["source_rows"], second["source_rows"] = (
        second["source_rows"],
        first["source_rows"],
    )
    for values in review_log["values_by_period"].values():
        values[LAUNDRY], values[UNIFORMS] = values[UNIFORMS], values[LAUNDRY]
    result = run_review(review_log)
    child = next(c for c in result.children if c["coa_id"] == LAUNDRY)
    assert child["refs"] == ["P&L!6"]
    assert (
        next(r for r in result.rows if r["key"] == child["refs"][0])["label"]
        == "Uniform laundry"
    )
    assert {LAUNDRY, UNIFORMS} <= {d["coa_id"] for d in result.definitions}
    assert "EVERY active child" in render_evaluation_markdown(result)


def test_zero_is_distinct_from_missing_and_unbacked_child_is_included(
    review_log, run_review
):
    review_log["values_by_period"]["prior"][LAUNDRY] = 0
    review_log["accounts"][2]["source_rows"] = []
    result = run_review(review_log)
    assert any(c["coa_id"] == UNIFORMS for c in result.children)
    assert any(f["code"] == "zero_from_nonzero_source" for f in result.findings)
    assert any(f["code"] == "child_without_rows" for f in result.findings)


def test_raw_error_survives_missing_feedback_entry(review_log, run_review):
    error = Finding(
        "error", "hierarchy_complete", PARENT, {"variance": 30}, period_id="prior"
    )
    review_log["findings"] = [error.to_dict(), error.to_dict()]
    review_log["findings_by_period"] = {"prior": [error.to_dict()]}
    review_log["checks_by_period"] = {"prior": [str(error)]}
    review_log["execution_issues"] = [
        "Failed to calculate period",
        "Failed to calculate period",
    ]
    result = run_review(review_log)
    assert result.status == "issues_found"
    assert len([f for f in result.findings if f["code"] == "hierarchy_complete"]) == 1
    assert len([f for f in result.findings if f["code"] == "execution_error"]) == 1


def test_warning_resolves_rows_even_when_not_used_by_children(review_log, run_review):
    review_log["feedback_manifest"]["findings"] = [
        {
            "finding_id": "source-warning",
            "severity": "warning",
            "explanation": "Source discrepancy",
            "source_refs": ["P&L!4"],
            "affected_coa_ids": [PARENT],
            "periods": [],
        }
    ]
    result = run_review(review_log)
    assert (
        "P&L!4"
        in next(f for f in result.findings if f["code"] == "source-warning")["refs"]
    )
    assert any(r["key"] == "P&L!4" and r["values"] == [100, 100] for r in result.rows)


def test_missing_warning_reference_is_incomplete(review_log, run_review):
    review_log["feedback_manifest"]["findings"] = [
        {
            "finding_id": "missing",
            "severity": "warning",
            "source_refs": ["P&L!999"],
        }
    ]
    result = run_review(review_log)
    assert result.status == "incomplete"
    assert "MISSING:P&L!999" in render_evaluation_markdown(result)


def test_unreadable_log_returns_incomplete(tmp_path):
    path = tmp_path / "run_log.json"
    path.write_text("[]", encoding="utf-8")
    result = evaluate_run(tmp_path / "source.xlsx", path, None)
    assert result.status == "incomplete"
    assert "Cannot prepare evidence" in render_evaluation_markdown(result)


def test_missing_parent_reference_is_incomplete(review_log, run_review):
    review_log["accounts"][0]["source_rows"] = [{"row_key": "P&L!999"}]
    result = run_review(review_log)
    assert result.status == "incomplete"
    assert any(f["code"] == "missing_source_references" for f in result.findings)


def test_parent_subtraction_does_not_hide_available_detail(review_log, run_review):
    review_log["accounts"][2].update(operation="no_value", source_rows=[])
    review_log["accounts"][0]["excluded_rows"] = [_row_ref(review_log["evidence_rows"][2])]
    for values in review_log["values_by_period"].values():
        values[UNIFORMS] = None
    result = run_review(review_log)
    row = next(r for r in result.rows if r["key"] == "P&L!6")
    assert row["candidate"] and row["usage"] == "parent subtraction"


def test_absent_feedback_manifest_still_exposes_raw_errors(review_log, run_review):
    review_log.pop("feedback_manifest")
    review_log["execution_issues"] = ["Mapping calculation failed"]
    result = run_review(review_log)
    assert result.status == "incomplete"
    assert any(f["code"] == "execution_error" for f in result.findings)
