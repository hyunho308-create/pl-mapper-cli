"""Cited source subtotals checked independently of normalized COA totals."""

from __future__ import annotations

import json
import math
import re

from pydantic import Field, model_validator

from hotel_pl_normalizer.mapping.findings import Finding
from hotel_pl_normalizer.mapping.tolerances import reconciliation_tolerance
from hotel_pl_normalizer.models.common import StrictModel


class SourceControl(StrictModel):
    label: str = Field(min_length=1)
    coa_id: str
    total_row: str
    component_rows: list[str] = Field(min_length=1)
    excluded_rows: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_rows(self):
        if len(self.rows) != len(set(self.rows)):
            raise ValueError("source control rows must be distinct, including the total")
        if not self.label.strip() or any(not row.strip() for row in self.rows):
            raise ValueError("source control label and rows cannot be blank")
        return self

    @property
    def rows(self) -> list[str]:
        return [self.total_row, *self.component_rows, *self.excluded_rows]


def control_reference_issues(controls, evidence_rows, coa) -> list[str]:
    issues = []
    for control in controls:
        if control.coa_id not in coa:
            issues.append(f"source control {control.label!r}: unknown COA id {control.coa_id}")
        missing = sorted(set(control.rows) - set(evidence_rows))
        if missing:
            issues.append(f"source control {control.label!r}: unknown rows {', '.join(missing)}")
    return issues


def check_source_controls(controls, evidence, period_id) -> list[Finding]:
    """Report arithmetic only; never choose which source value is correct."""
    rows = {row["row_key"]: row for row in evidence}
    findings = []
    seen = set()
    for control in controls:
        identity = (control.total_row, frozenset(control.component_rows), frozenset(control.excluded_rows))
        if identity in seen:
            continue
        seen.add(identity)
        amounts = {
            key: (rows.get(key, {}).get("selected_values") or {}).get(period_id)
            for key in control.rows
        }
        details = {
            "label": control.label,
            "total_row": control.total_row,
            "component_rows": json.dumps(control.component_rows),
            "excluded_rows": json.dumps(control.excluded_rows),
        }
        if any(
            value is None or isinstance(value, bool)
            or not isinstance(value, (int, float)) or not math.isfinite(value)
            for value in amounts.values()
        ):
            findings.append(Finding(
                "info", "source_control_unverified", control.coa_id,
                details, period_id=period_id,
            ))
            continue
        actual = amounts[control.total_row]
        expected = sum(amounts[key] for key in control.component_rows) - sum(
            amounts[key] for key in control.excluded_rows
        )
        variance = actual - expected
        reason = _unsupported_additive_control(control, rows, actual, expected)
        if reason:
            findings.append(Finding(
                "info", "source_control_unverified", control.coa_id,
                {**details, "actual": actual, "expected": expected,
                 "variance": variance, "reason": reason}, period_id=period_id,
            ))
            continue
        # Retain rounding differences in the audit. The feedback composer
        # suppresses their display using the common reconciliation tolerance.
        if abs(variance) > 1e-7:
            findings.append(Finding(
                "info" if abs(variance) <= reconciliation_tolerance(actual) else "warning",
                "source_control_difference", control.coa_id,
                {**details, "actual": actual, "expected": expected, "variance": variance},
                period_id=period_id,
            ))
    return findings


def _unsupported_additive_control(control, rows, actual, expected):
    """Conservative guardrails, not a semantic proof of source membership.

    Optional controls outside this small additive contract remain in the audit.
    Do not normalize signs or silently fix the model's proposed equation.
    """
    if control.excluded_rows:
        return "adjusted comparison; excluded rows are not a list of ignored rows"
    try:
        sheet, number = control.total_row.rsplit("!", 1)
        components = [key.rsplit("!", 1) for key in control.component_rows]
        if any(tab != sheet or int(row) >= int(number) for tab, row in components):
            return "components are not above the subtotal on the same sheet"
    except (ValueError, TypeError):
        return "source layout does not establish an additive subtotal"
    label = str(rows.get(control.total_row, {}).get("label") or "")
    if re.search(r"\b(profit|loss|offset)\b|\bexp(?:ense)?\s+alloc", label, re.I):
        return "profit or allocation offset is not an additive expense subtotal"
    if actual * expected < 0:
        return "subtotal and component sum use opposite signs"
    return None
