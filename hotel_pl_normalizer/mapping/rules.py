"""Controlled mapping-rule registry loaded from packaged data."""

from __future__ import annotations

import csv
from dataclasses import dataclass
from functools import lru_cache
from importlib import resources
from typing import Literal

RuleOutcome = Literal[
    "clean",
    "source_exception",
    "coverage_gap",
    "scope_exception",
    "rejected",
]


@dataclass(frozen=True, slots=True)
class RulePolicy:
    rule: str
    severity: Literal["info", "warning", "error"]
    category: str
    outcome: RuleOutcome
    description: str
    resolution: str
    auto_repairable: bool


@lru_cache(maxsize=1)
def rule_registry() -> dict[str, RulePolicy]:
    source = resources.files("hotel_pl_normalizer.data").joinpath(
        "mapping_rules.csv"
    )
    with source.open("r", encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    registry: dict[str, RulePolicy] = {}
    for row in rows:
        rule = str(row.get("rule") or "").strip()
        if not rule or rule in registry:
            raise ValueError(f"blank or duplicate mapping rule {rule!r}")
        severity = str(row.get("severity") or "").strip()
        outcome = str(row.get("outcome") or "").strip()
        if severity not in {"info", "warning", "error"}:
            raise ValueError(f"invalid severity for mapping rule {rule!r}")
        if outcome not in {
            "clean",
            "source_exception",
            "coverage_gap",
            "scope_exception",
            "rejected",
        }:
            raise ValueError(f"invalid outcome for mapping rule {rule!r}")
        registry[rule] = RulePolicy(
            rule=rule,
            severity=severity,  # type: ignore[arg-type]
            category=str(row.get("category") or "").strip(),
            outcome=outcome,  # type: ignore[arg-type]
            description=str(row.get("description") or "").strip(),
            resolution=str(row.get("resolution") or "").strip(),
            auto_repairable=str(row.get("auto_repairable") or "").casefold()
            == "true",
        )
    return registry


def get_rule_policy(rule: str) -> RulePolicy:
    try:
        return rule_registry()[rule]
    except KeyError as exc:
        raise KeyError(f"unregistered mapping rule {rule!r}") from exc


def rules_for(*, outcome: RuleOutcome | None = None, category: str | None = None):
    return frozenset(
        rule
        for rule, policy in rule_registry().items()
        if (outcome is None or policy.outcome == outcome)
        and (category is None or policy.category == category)
    )


def render_rule_documentation() -> str:
    """Generate the stable Rule Guidance section from controlled data."""

    sections = ["# Mapping Validation Rules", ""]
    for policy in rule_registry().values():
        sections.extend(
            [
                f"## `{policy.rule}`",
                "",
                f"Default severity: `{policy.severity}`  ",
                f"Category: `{policy.category}`  ",
                f"Outcome: `{policy.outcome}`  ",
                f"Auto-repairable: `{'true' if policy.auto_repairable else 'false'}`",
                "",
                f"Description: {policy.description}",
                "",
                f"Resolution: {policy.resolution}",
                "",
            ]
        )
    return "\n".join(sections).rstrip() + "\n"


def assert_registered_rules(rules) -> None:
    """Fail immediately if a live check emits an uncontrolled rule name."""

    unknown = sorted(set(rules) - set(rule_registry()))
    if unknown:
        raise ValueError("unregistered live mapping rules: " + ", ".join(unknown))
