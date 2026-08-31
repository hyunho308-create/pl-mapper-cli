from __future__ import annotations

import re
from enum import Enum

from pydantic import Field, field_validator, model_validator

from .common import StrictModel


class PeriodScenario(str, Enum):
    ACTUAL = "actual"
    FORECAST = "forecast"
    BUDGET = "budget"


_MONTH_PATTERN = re.compile(r"^(\d{4})-(0[1-9]|1[0-2])$")
_MONTH_NAMES = (
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
)
_MONTH_ABBREVIATIONS = tuple(name[:3] for name in _MONTH_NAMES)


def _month_parts(value: str) -> tuple[int, int]:
    match = _MONTH_PATTERN.fullmatch(value or "")
    if match is None:
        raise ValueError("month must use YYYY-MM with a valid calendar month")
    return int(match.group(1)), int(match.group(2))


def inclusive_month_count(start_month: str, end_month: str) -> int:
    start_year, start_number = _month_parts(start_month)
    end_year, end_number = _month_parts(end_month)
    return (end_year * 12 + end_number) - (start_year * 12 + start_number) + 1


def is_calendar_year(start_month: str, end_month: str) -> bool:
    start_year, start_number = _month_parts(start_month)
    end_year, end_number = _month_parts(end_month)
    return start_year == end_year and start_number == 1 and end_number == 12


def is_ytd(start_month: str, end_month: str) -> bool:
    start_year, start_number = _month_parts(start_month)
    end_year, end_number = _month_parts(end_month)
    return (
        start_year == end_year
        and start_number == 1
        and 1 < end_number < 12
    )


def is_ttm(start_month: str, end_month: str) -> bool:
    return inclusive_month_count(start_month, end_month) == 12 and not is_calendar_year(
        start_month, end_month
    )


def is_annual_summary_period(start_month: str, end_month: str) -> bool:
    """Whether the range belongs in annual/YTD/TTM auto-selection surfaces."""
    return (
        is_calendar_year(start_month, end_month)
        or is_ytd(start_month, end_month)
        or is_ttm(start_month, end_month)
    )


def canonical_period_id(
    scenario: PeriodScenario,
    start_month: str,
    end_month: str,
    actual_months: int | None = None,
) -> str:
    suffix = ""
    if scenario == PeriodScenario.FORECAST and actual_months:
        suffix = f"_{actual_months}p{12 - actual_months}"
    return f"{start_month}_{end_month}_{scenario.value}{suffix}"


def canonical_period_label(
    scenario: PeriodScenario,
    start_month: str,
    end_month: str,
    actual_months: int | None = None,
) -> str:
    start_year, start_number = _month_parts(start_month)
    end_year, end_number = _month_parts(end_month)
    scenario_label = scenario.value.title()

    if start_month == end_month:
        return f"{_MONTH_NAMES[end_number - 1]} {end_year} {scenario_label}"
    if is_calendar_year(start_month, end_month):
        label = f"{end_year} {scenario_label}"
        if scenario == PeriodScenario.FORECAST and actual_months:
            label += f" ({actual_months}+{12 - actual_months})"
        return label
    if is_ytd(start_month, end_month):
        return f"{_MONTH_NAMES[end_number - 1]} {end_year} YTD {scenario_label}"
    if is_ttm(start_month, end_month):
        return f"{_MONTH_NAMES[end_number - 1]} {end_year} TTM {scenario_label}"
    return (
        f"{_MONTH_ABBREVIATIONS[start_number - 1]} {start_year}–"
        f"{_MONTH_ABBREVIATIONS[end_number - 1]} {end_year} {scenario_label}"
    )


class CanonicalPeriod(StrictModel):
    """A scenario over one inclusive month range, with deterministic identity."""

    period_id: str = ""
    label: str = ""
    scenario: PeriodScenario
    start_month: str
    end_month: str
    actual_months: int | None = Field(
        default=None,
        ge=0,
        le=11,
        exclude_if=lambda value: value is None,
    )

    @field_validator("start_month", "end_month")
    @classmethod
    def validate_month(cls, value: str) -> str:
        _month_parts(value)
        return value

    @field_validator("actual_months", mode="before")
    @classmethod
    def normalize_zero_actual_months(cls, value):
        return None if value in (None, 0, "0") else value

    @model_validator(mode="after")
    def verify_and_build_identity(self):
        month_count = inclusive_month_count(self.start_month, self.end_month)
        if month_count <= 0:
            raise ValueError("start_month must be before or equal to end_month")
        if self.actual_months is not None:
            if self.scenario != PeriodScenario.FORECAST:
                raise ValueError("actual_months is only valid for forecast periods")
            if not is_calendar_year(self.start_month, self.end_month):
                raise ValueError(
                    "actual_months is only valid for full-calendar-year forecasts"
                )

        expected_id = canonical_period_id(
            self.scenario,
            self.start_month,
            self.end_month,
            self.actual_months,
        )
        expected_label = canonical_period_label(
            self.scenario,
            self.start_month,
            self.end_month,
            self.actual_months,
        )
        if self.period_id and self.period_id != expected_id:
            raise ValueError(
                f"period_id must be deterministically generated as {expected_id!r}"
            )
        if self.label and self.label != expected_label:
            raise ValueError(
                f"label must be deterministically generated as {expected_label!r}"
            )
        self.period_id = expected_id
        self.label = expected_label
        return self


class PeriodColumnSelection(StrictModel):
    sheet_name: str | None = None
    value_column: int
    excel_column: str
    period_label: str
    evidence: list[str] = []


class PeriodColumnSelectionMap(StrictModel):
    selection_map_id: str
    workbook_id: str
    requested_period: str
    # Read compatibility for artifacts created before binding became strictly
    # per sheet. New binding runs always leave this unset and never infer a
    # missing sheet's column from another sheet.
    default_selection: PeriodColumnSelection | None = None
    sheet_selections: list[PeriodColumnSelection] = []
    unavailable_sheets: dict[str, str] = {}
    notes: list[str] = []


class PeriodOption(CanonicalPeriod):
    pass


class PeriodCatalog(StrictModel):
    catalog_id: str
    workbook_id: str
    controlling_summary_sheet: str
    options: list[PeriodOption]
    notes: list[str] = []

    @model_validator(mode="before")
    @classmethod
    def discard_legacy_recommendation(cls, value):
        """Load old catalog artifacts without restoring default-selection behavior."""
        if isinstance(value, dict) and "recommended_period_id" in value:
            value = {key: item for key, item in value.items() if key != "recommended_period_id"}
        return value
