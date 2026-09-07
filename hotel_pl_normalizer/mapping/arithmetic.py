"""Shared arithmetic for cited source-row operations."""

from __future__ import annotations

from enum import Enum
from typing import Iterable


class MissingValuePolicy(str, Enum):
    """How an operation treats a missing value among cited rows."""

    PROPAGATE = "propagate"
    IGNORE = "ignore"


def evaluate_operation(
    operation,
    included: Iterable[float | None],
    excluded: Iterable[float | None] = (),
    *,
    scale_factor: float | None = None,
    missing_value_policy: MissingValuePolicy,
    empty_value: float | None,
) -> float | None:
    """Evaluate one source operation under an explicit missing-value policy."""

    op = getattr(operation, "value", operation)
    included_values = list(included)
    excluded_values = list(excluded)
    if missing_value_policy == MissingValuePolicy.PROPAGATE and any(
        value is None for value in [*included_values, *excluded_values]
    ):
        return None
    included_numbers = [value for value in included_values if value is not None]
    excluded_numbers = [value for value in excluded_values if value is not None]

    if op == "no_value":
        return empty_value
    if op == "direct":
        if len(included_values) != 1:
            raise ValueError("direct requires one row")
        return included_values[0]
    if op == "sum":
        return sum(included_numbers) if included_numbers else empty_value
    if op == "adjusted_subtotal":
        if included_numbers or excluded_numbers:
            return sum(included_numbers) - sum(excluded_numbers)
        return empty_value
    if op == "negate":
        return -sum(included_numbers) if included_numbers else empty_value
    if op == "ratio":
        if len(included_values) != 2:
            raise ValueError("ratio requires two rows")
        if any(value is None for value in included_values):
            return empty_value
        if included_values[1] == 0:
            raise ValueError("ratio requires two rows and nonzero denominator")
        return included_values[0] / included_values[1]
    if op == "product":
        if any(value is None for value in included_values):
            return empty_value
        value = 1.0
        for item in included_values:
            value *= item
        return value
    if op == "scale":
        if included_numbers:
            return sum(included_numbers) * float(scale_factor)
        return empty_value
    raise ValueError(f"unsupported operation {op}")
