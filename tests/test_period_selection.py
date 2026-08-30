from __future__ import annotations

import pytest
from pydantic import ValidationError

from hotel_pl_normalizer.models.exploration import WorkbookPeriods
from hotel_pl_normalizer.models.period_selection import PeriodOption


@pytest.mark.parametrize(
    ("payload", "period_id", "label"),
    [
        (
            {"scenario": "actual", "start_month": "2025-01", "end_month": "2025-12"},
            "2025-01_2025-12_actual",
            "2025 Actual",
        ),
        (
            {"scenario": "budget", "start_month": "2025-06", "end_month": "2025-06"},
            "2025-06_2025-06_budget",
            "June 2025 Budget",
        ),
        (
            {"scenario": "actual", "start_month": "2025-01", "end_month": "2025-06"},
            "2025-01_2025-06_actual",
            "June 2025 YTD Actual",
        ),
        (
            {"scenario": "actual", "start_month": "2024-07", "end_month": "2025-06"},
            "2024-07_2025-06_actual",
            "June 2025 TTM Actual",
        ),
        (
            {"scenario": "actual", "start_month": "2024-07", "end_month": "2024-09"},
            "2024-07_2024-09_actual",
            "Jul 2024–Sep 2024 Actual",
        ),
        (
            {
                "scenario": "forecast",
                "start_month": "2025-01",
                "end_month": "2025-12",
                "actual_months": 3,
            },
            "2025-01_2025-12_forecast_3p9",
            "2025 Forecast (3+9)",
        ),
    ],
)
def test_period_identity_and_label_are_deterministic(payload, period_id, label):
    period = PeriodOption.model_validate(payload)
    assert period.period_id == period_id
    assert period.label == label


def test_zero_actual_months_normalizes_to_pure_forecast_and_is_not_serialized():
    period = PeriodOption(
        scenario="forecast",
        start_month="2025-01",
        end_month="2025-12",
        actual_months=0,
    )
    assert period.label == "2025 Forecast"
    assert "actual_months" not in period.model_dump(mode="json")


@pytest.mark.parametrize("scenario", ["actual", "budget"])
def test_actual_months_is_rejected_for_non_forecast_scenarios(scenario):
    with pytest.raises(ValidationError, match="only valid for forecast"):
        PeriodOption(
            scenario=scenario,
            start_month="2025-01",
            end_month="2025-12",
            actual_months=3,
        )


def test_actual_months_is_rejected_for_non_calendar_year_forecast():
    with pytest.raises(ValidationError, match="full-calendar-year"):
        PeriodOption(
            scenario="forecast",
            start_month="2025-01",
            end_month="2025-06",
            actual_months=3,
        )


def test_mismatched_model_supplied_identity_is_rejected():
    with pytest.raises(ValidationError, match="period_id must be deterministically"):
        PeriodOption(
            period_id="made_up",
            scenario="actual",
            start_month="2025-01",
            end_month="2025-12",
        )
    with pytest.raises(ValidationError, match="label must be deterministically"):
        PeriodOption(
            label="FY25 Actual",
            scenario="actual",
            start_month="2025-01",
            end_month="2025-12",
        )


@pytest.mark.parametrize("scenario", ["prior_actual", "blended", "unknown"])
def test_removed_scenarios_are_rejected(scenario):
    with pytest.raises(ValidationError):
        PeriodOption(
            scenario=scenario,
            start_month="2025-01",
            end_month="2025-12",
        )


def test_period_submission_rejects_duplicate_identity():
    period = {
        "scenario": "actual",
        "start_month": "2025-01",
        "end_month": "2025-12",
    }
    with pytest.raises(ValidationError, match="must be unique"):
        WorkbookPeriods(controlling_summary_sheet="Summary", periods=[period, period])
