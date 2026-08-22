from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, ConfigDict, model_validator


class StrictModel(BaseModel):
    """Base model for contract objects: no silent extra fields."""

    model_config = ConfigDict(extra="forbid")


class NoNumericConfidenceModel(StrictModel):
    """Discard confidence fields from artifacts written before their removal."""

    @model_validator(mode="before")
    @classmethod
    def discard_legacy_confidence(cls, value):
        if isinstance(value, dict):
            value = dict(value)
            value.pop("confidence", None)
            value.pop("layout_confidence", None)
        return value


class Severity(str, Enum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


class PeriodType(str, Enum):
    YTD = "YTD"
    MTD = "MTD"
    TTM = "TTM"
    BUDGET = "Budget"
    FORECAST = "Forecast"
    UNKNOWN = "Unknown"


class ValidationStatus(str, Enum):
    PASS = "pass"
    WARNING = "warning"
    FAIL = "fail"
    SKIPPED = "skipped"


class ReviewStatus(str, Enum):
    MODEL_SUGGESTED = "model_suggested"
    REVIEWED = "reviewed"
    NEEDS_REVIEW = "needs_review"


class MappingBasis(str, Enum):
    DIRECT_ROW = "direct_row"
    SUBTOTAL = "subtotal"
    DETAIL_SUM = "detail_sum"
    RESIDUAL = "residual"
    DERIVED = "derived"
    ADJUSTED_SUBTOTAL = "adjusted_subtotal"
    SUMMARY_CHECKSUM = "summary_checksum"
    NO_VALUE = "no_value"


class SectionType(str, Enum):
    SUMMARY = "SUMMARY"
    MAIN_PL = "MAIN_PL"
    SUPPORTING_SCHEDULE = "SUPPORTING_SCHEDULE"
    SKIP = "SKIP"
    UNKNOWN = "UNKNOWN"


class CanonicalSubsection(str, Enum):
    REVENUE = "Revenue"
    COGS = "COGS"
    LABOR = "Labor"
    OPEX = "Opex"
    EXPENSE = "Expense"
    KPI = "KPI"


class RowType(str, Enum):
    BLANK = "BLANK"
    TITLE = "TITLE"
    HEADER = "HEADER"
    DEPARTMENT_HEADER = "DEPARTMENT_HEADER"
    SUBSECTION_HEADER = "SUBSECTION_HEADER"
    DETAIL = "DETAIL"
    SUBTOTAL = "SUBTOTAL"
    GRAND_TOTAL = "GRAND_TOTAL"
    KPI = "KPI"
    CHECK = "CHECK"
    SUPPORTING_SCHEDULE = "SUPPORTING_SCHEDULE"
    UNKNOWN = "UNKNOWN"


class TargetPeriod(StrictModel):
    period_label: str
    period_type: PeriodType = PeriodType.UNKNOWN
    value_column: int | None = None


class WarningMessage(StrictModel):
    severity: Severity
    message: str
    affected_rows: list[str] = []


class ModelInfo(StrictModel):
    provider: str | None = None
    model_name: str | None = None
    prompt_version: str | None = None
