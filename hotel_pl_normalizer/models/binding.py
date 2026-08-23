"""Models returned by the period-column binding session."""

from __future__ import annotations

from .common import StrictModel


class PeriodBinding(StrictModel):
    """Which column on one sheet holds one chosen period."""

    period_id: str
    sheet_name: str
    excel_column: str
    evidence: list[str] = []


class UnavailablePeriod(StrictModel):
    """A chosen period that is genuinely absent from one routed sheet."""

    period_id: str
    sheet_name: str
    reason: str


class WorkbookBindings(StrictModel):
    """All period-column answers for the routed financial sheets."""

    bindings: list[PeriodBinding] = []
    unavailable: list[UnavailablePeriod] = []
    notes: list[str] = []
    observations: list[str] = []
