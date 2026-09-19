"""Comments travel with the mapping, never in a separate commentary call."""
import pytest
from pydantic import ValidationError

from hotel_pl_normalizer.mapping.mapper import (
    ModelToolError, WorkbookMappingValidator, WorkbookSourcePlan, WorkbookStrategy,
    _validation_score,
)
from hotel_pl_normalizer.run_log import build_run_log
from test_excel_writer import build_result, canonical_ids
from hotel_pl_normalizer.output import write_normalized_workbook
from openpyxl import load_workbook


def plan(**overrides):
    return WorkbookSourcePlan(
        plan_id="initial", workbook_id="wb",
        strategy=WorkbookStrategy(reporting_layout="test", summary_source="Summary"),
        decisions=[], **overrides,
    )


def test_summary_length_is_bounded():
    assert len(plan(run_summary="a" * 500).run_summary) == 500
    with pytest.raises(ValidationError):
        plan(run_summary="a" * 501)


@pytest.mark.parametrize("fields", [{}, {"run_summary": None}, {"review_items": []}])
def test_modern_patch_requires_refreshed_comments(fields):
    validator = WorkbookMappingValidator("wb", [], {})
    validator.current_plan = plan(run_summary="Original explanation.")
    with pytest.raises(ModelToolError, match="Refresh run_summary"):
        validator._apply_patch(dict(patch_id="repair", workbook_id="wb", replacements=[], **fields))


def test_summary_only_patch_and_explicit_clear_leave_previous_plan_untouched():
    validator = WorkbookMappingValidator("wb", [], {})
    previous = plan(run_summary="Original explanation.")
    validator.current_plan = previous
    updated, _ = validator._apply_patch(dict(
        patch_id="repair", workbook_id="wb", replacements=[],
        review_items=[], run_summary="Updated explanation.",
    ))
    assert updated.run_summary == "Updated explanation."
    assert previous.run_summary == "Original explanation."
    validator.current_plan = updated
    cleared, _ = validator._apply_patch(dict(
        patch_id="clear", workbook_id="wb", replacements=[],
        review_items=[], run_summary=None,
    ))
    assert cleared.run_summary is None
    assert "run_summary" in cleared.model_fields_set


def test_failed_cleanup_restores_mapping_and_its_comments_together():
    validator = WorkbookMappingValidator("wb", [], {})
    previous = plan(run_summary="Original explanation.", review_items=[{
        "kind": "unusual_convention", "message": "Original treatment.",
        "coa_ids": ["S1.test"], "source_rows": [],
    }])
    checkpoint = dict(accepted=True, validation_attempt=1, warning_count=1,
                      warnings=["warning|coverage_unspecified|S1.test|review"], review_items=[])
    validator.current_plan = plan(run_summary="Rejected explanation.")
    validator.warning_cleanup_checkpoint_plan = previous
    validator.warning_cleanup_checkpoint_result = checkpoint
    validator.warning_cleanup_checkpoint_score = _validation_score(checkpoint)
    validator.warning_cleanup_checkpoint_attempt = 1
    validator._finish_warning_cleanup(dict(accepted=False, warning_count=2))
    assert validator.current_plan is previous
    assert validator.best_plan is previous
    assert validator.best_plan.run_summary == "Original explanation."
    assert validator.best_plan.review_items[0].message == "Original treatment."


@pytest.mark.parametrize("summary", [
    "HR payroll was included in A&G. The source does not separate management wages.",
    "=This is narrative text, not a formula.",
])
def test_model_summary_is_persisted_and_written_verbatim(tmp_path, summary):
    result = build_result(coa={i: {} for i in canonical_ids()}, run_summary=summary)
    assert build_run_log(result)["run_summary"] == summary
    book = load_workbook(write_normalized_workbook(result, tmp_path / "summary.xlsx"))
    assert book["Run Notes"]["C9"].value == summary
    assert book["Run Notes"]["C9"].data_type == "s"
    assert book["Run Notes"]["C6"].value == "Completed"
    book.close()
