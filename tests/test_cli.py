from __future__ import annotations

from types import SimpleNamespace

import pytest

from hotel_pl_normalizer.cli import (
    _prompt_for_period_ids,
    _validate_period_ids,
)
from hotel_pl_normalizer.models.period_selection import CanonicalPeriod
from scripts import run_workbook
from scripts.run_workbook import _choose_period_ids

CATALOG = {
    "options": [
        {
            "period_id": "2025-01_2025-01_actual",
            "label": "January 2025 Actual",
            "scenario": "actual",
            "start_month": "2025-01",
            "end_month": "2025-01",
        },
        {
            "period_id": "2025-01_2025-06_actual",
            "label": "June 2025 YTD Actual",
            "scenario": "actual",
            "start_month": "2025-01",
            "end_month": "2025-06",
        },
        {
            "period_id": "2024-01_2024-12_actual",
            "label": "2024 Actual",
            "scenario": "actual",
            "start_month": "2024-01",
            "end_month": "2024-12",
        },
        {
            "period_id": "2025-04_2025-06_actual",
            "label": "Apr 2025–Jun 2025 Actual",
            "scenario": "actual",
            "start_month": "2025-04",
            "end_month": "2025-06",
        },
    ]
}
ALL_VALID = {item["period_id"] for item in CATALOG["options"]}


def test_explicit_period_ids_are_validated_and_deduplicated():
    assert _validate_period_ids(
        CATALOG,
        ALL_VALID,
        [
            "2024-01_2024-12_actual",
            "2025-01_2025-06_actual",
            "2024-01_2024-12_actual",
        ],
    ) == ["2024-01_2024-12_actual", "2025-01_2025-06_actual"]


def test_explicit_period_that_failed_validation_is_rejected():
    with pytest.raises(ValueError, match="failed validation"):
        _validate_period_ids(
            CATALOG,
            {"2025-01_2025-06_actual"},
            ["2024-01_2024-12_actual"],
        )


def test_interactive_selection_has_no_default():
    answers = iter(["", "2"])
    assert _prompt_for_period_ids(
        CATALOG, ALL_VALID, read=lambda _prompt: next(answers)
    ) == ["2025-01_2025-06_actual"]


def test_interactive_selection_rejects_an_empty_validated_set():
    with pytest.raises(RuntimeError, match="No discovered period passed validation"):
        _prompt_for_period_ids(CATALOG, set(), read=lambda _prompt: "1")


def test_batch_annual_policy_is_outside_main_cli_and_uses_catalog_order():
    assert _choose_period_ids(
        CATALOG, ALL_VALID, [], annual_periods=2
    ) == ["2025-01_2025-06_actual", "2024-01_2024-12_actual"]


def test_batch_annual_policy_excludes_months_and_custom_ranges():
    chosen = _choose_period_ids(CATALOG, ALL_VALID, [], annual_periods=10)
    assert chosen == ["2025-01_2025-06_actual", "2024-01_2024-12_actual"]


def test_batch_explicit_ids_replace_annual_policy():
    assert _choose_period_ids(
        CATALOG,
        ALL_VALID,
        ["2025-01_2025-01_actual"],
        annual_periods=2,
    ) == ["2025-01_2025-01_actual"]


def test_batch_policy_errors_when_no_annual_period_exists():
    with pytest.raises(RuntimeError, match="No discovered annual period"):
        _choose_period_ids(
            {"options": [CATALOG["options"][0]]},
            {"2025-01_2025-01_actual"},
            [],
            annual_periods=2,
        )


def _year_option(year: int, scenario: str = "actual") -> dict:
    return CanonicalPeriod(
        scenario=scenario, start_month=f"{year}-01", end_month=f"{year}-12"
    ).model_dump(mode="json")


def test_actual_year_selects_exact_calendar_actuals_in_current_prior_order():
    options = CATALOG["options"] + [
        _year_option(2025, "forecast"),
        _year_option(2025, "budget"),
        _year_option(2025),
        CanonicalPeriod(
            scenario="actual", start_month="2024-07", end_month="2025-06"
        ).model_dump(mode="json"),
    ]
    assert _choose_period_ids(
        {"options": options}, {item["period_id"] for item in options}, [], actual_year=2025
    ) == ["2025-01_2025-12_actual", "2024-01_2024-12_actual"]


@pytest.mark.parametrize("condition", ["missing", "invalid", "ambiguous"])
def test_actual_year_rejects_unavailable_or_ambiguous_required_year(condition):
    current = _year_option(2025)
    options = CATALOG["options"] + [_year_option(2025, "forecast")]
    if condition != "missing":
        options += [current] * (2 if condition == "ambiguous" else 1)
    valid_ids = {item["period_id"] for item in options}
    if condition == "invalid":
        valid_ids.remove(current["period_id"])
    with pytest.raises(RuntimeError, match="validated full-calendar-year Actual for 2025"):
        _choose_period_ids({"options": options}, valid_ids, [], actual_year=2025)


@pytest.mark.parametrize("prior_present", [False, True])
def test_actual_year_does_not_invent_or_include_unvalidated_prior(prior_present):
    current = _year_option(2025)
    options = [current] + ([_year_option(2024)] if prior_present else [])
    assert _choose_period_ids(
        {"options": options}, {current["period_id"]}, [], actual_year=2025
    ) == [current["period_id"]]


@pytest.mark.parametrize("other", [["--annual-periods", "1"], ["--period-id", "example"]])
def test_actual_year_cli_excludes_other_selection_modes(monkeypatch, tmp_path, other):
    monkeypatch.setattr(
        "sys.argv", ["run_workbook", "source.xlsx", str(tmp_path), "--actual-year", "2025", *other]
    )
    with pytest.raises(SystemExit) as error:
        run_workbook.main()
    assert error.value.code == 2


def test_actual_year_pdf_uses_validated_periods(monkeypatch, tmp_path):
    discovery = SimpleNamespace(
        exploration=SimpleNamespace(periods=[CanonicalPeriod(**_year_option(2025))])
    )
    monkeypatch.setattr(
        "sys.argv", ["run_workbook", "source.pdf", str(tmp_path), "--actual-year", "2025"]
    )
    monkeypatch.setattr(run_workbook, "discover_pdf_periods", lambda *args, **kwargs: discovery)
    monkeypatch.setattr(run_workbook, "validated_pdf_period_ids", lambda run: set())
    with pytest.raises(RuntimeError, match="Actual for 2025; found 0"):
        run_workbook.main()
