from __future__ import annotations

from enum import Enum

from .common import StrictModel


class PeriodScenario(str, Enum):
    ACTUAL = "actual"
    PRIOR_ACTUAL = "prior_actual"
    BUDGET = "budget"
    FORECAST = "forecast"
    BLENDED = "blended"
    UNKNOWN = "unknown"


class PeriodType(str, Enum):
    MONTH = "month"
    CURRENT_PERIOD = "current_period"
    YTD = "ytd"
    FULL_YEAR = "full_year"
    TTM = "ttm"
    UNKNOWN = "unknown"


class PeriodColumnSelection(StrictModel):
    sheet_name: str | None = None
    department: str | None = None
    value_column: int
    excel_column: str
    period_label: str
    evidence: list[str] = []
    warnings: list[str] = []


class PeriodColumnSelectionMap(StrictModel):
    selection_map_id: str
    workbook_id: str
    requested_period: str
    default_selection: PeriodColumnSelection | None = None
    sheet_selections: list[PeriodColumnSelection] = []
    notes: list[str] = []


class PeriodOption(StrictModel):
    period_id: str
    label: str
    scenario: PeriodScenario
    period_type: PeriodType
    start_period: str | None = None
    end_period: str | None = None


class PeriodCatalog(StrictModel):
    catalog_id: str
    workbook_id: str
    options: list[PeriodOption]
    recommended_period_id: str | None = None
    notes: list[str] = []
