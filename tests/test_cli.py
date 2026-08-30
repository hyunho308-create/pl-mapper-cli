from __future__ import annotations

import pytest

from hotel_pl_normalizer.cli import (
    _prompt_for_period_ids,
    _validate_period_ids,
)
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
