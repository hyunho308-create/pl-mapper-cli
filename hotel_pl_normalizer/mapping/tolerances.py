"""Named numeric policies shared by mapping checks and feedback matching."""

from __future__ import annotations

ZERO_EPSILON = 0.005
KPI_RATIO_TOLERANCE = 0.001
KPI_CURRENCY_TOLERANCE = 0.05


def reconciliation_tolerance(value: float) -> float:
    """Allowed accounting-equation difference for a reported value."""

    return max(5.0, abs(float(value)) * 0.00001)


def offset_match_tolerance(variance: float) -> float:
    """Allowed distance when suggesting an unused row as a possible offset."""

    return max(5.0, abs(float(variance)) * 0.005)


def source_supported_tolerance(value: float) -> float:
    """Small difference allowed when every compared amount is source-supported."""

    return max(reconciliation_tolerance(value), abs(float(value)) * 0.0001)


def feedback_match_tolerance(left: float, right: float) -> float:
    """Allowed distance when linking a legacy check to its exception record."""

    return max(0.01, abs(float(left)) * 0.00001, abs(float(right)) * 0.00001)
