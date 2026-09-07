"""Conservative deterministic repair for one value-preserving cleanup."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass
from typing import Any

from hotel_pl_normalizer.mapping.findings import Finding

AUTO_REPAIR_RULES = frozenset({"source_row_included_and_excluded"})


@dataclass(frozen=True, slots=True)
class RepairContext:
    """The shared validator used to derive all post-repair findings."""

    revalidate: Callable[[Any], Sequence[Finding]] | None = None


@dataclass(frozen=True, slots=True)
class AppliedRepair:
    """One auditable decision change made without model judgment."""

    rule: str
    target: str
    reason: str
    before: dict[str, Any]
    after: dict[str, Any]
    before_plan_digest: str
    after_plan_digest: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class RepairResult:
    """A validated replacement plan and the original findings left unresolved."""

    plan: Any
    applied: tuple[AppliedRepair, ...]
    residual_findings: tuple[Finding, ...]
    before_plan_digest: str
    after_plan_digest: str

    @property
    def changed(self) -> bool:
        return bool(self.applied)


def plan_digest(plan: Any) -> str:
    """Hash the complete public plan payload for repair audit records."""

    payload = json.dumps(
        plan.model_dump(mode="json"),
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def repair(
    plan: Any,
    findings: list[Finding],
    context: RepairContext,
) -> RepairResult:
    """Apply only repairs whose answer is forced by the submitted plan.

    When a repair is applied, ``residual_findings`` comes only from the supplied
    shared-validator callback. Findings are never removed from a prior result by
    hand. A no-op keeps the original findings and does not invoke validation.
    """

    original_digest = plan_digest(plan)
    relevant = {(finding.rule, finding.target) for finding in findings}
    decisions = list(plan.decisions)
    applied: list[AppliedRepair] = []

    for index, decision in enumerate(decisions):
        target = decision.coa_id
        if (
            ("source_row_included_and_excluded", target) in relevant
            and _enum_value(decision.operation)
            in {"direct", "sum", "negate", "ratio", "product", "scale"}
        ):
            overlap = set(decision.source_rows) & set(decision.excluded_rows)
            if overlap:
                updated = decision.model_copy(
                    update={
                        "excluded_rows": [
                            row for row in decision.excluded_rows if row not in overlap
                        ]
                    }
                )
                record = _record(
                    plan,
                    decisions,
                    index,
                    decision,
                    updated,
                    rule="source_row_included_and_excluded",
                    reason=(
                        "Removed overlaps from excluded_rows because this operation "
                        "does not use exclusions."
                    ),
                )
                decisions[index] = updated
                applied.append(record)

    if applied:
        payload = plan.model_dump(mode="json")
        payload["decisions"] = [item.model_dump(mode="json") for item in decisions]
        repaired_plan = plan.__class__.model_validate(payload)
    else:
        repaired_plan = plan
    final_digest = plan_digest(repaired_plan)
    if applied and context.revalidate is None:
        raise ValueError("an applied deterministic repair requires full revalidation")
    residual = (
        tuple(context.revalidate(repaired_plan))
        if applied
        else tuple(findings)
    )
    return RepairResult(
        plan=repaired_plan,
        applied=tuple(applied),
        residual_findings=residual,
        before_plan_digest=original_digest,
        after_plan_digest=final_digest,
    )


def _record(
    plan,
    decisions,
    index,
    before,
    after,
    *,
    rule: str,
    reason: str,
) -> AppliedRepair:
    before_digest = _digest_with_decisions(plan, decisions)
    after_decisions = list(decisions)
    after_decisions[index] = after
    return AppliedRepair(
        rule=rule,
        target=before.coa_id,
        reason=reason,
        before=before.model_dump(mode="json"),
        after=after.model_dump(mode="json"),
        before_plan_digest=before_digest,
        after_plan_digest=_digest_with_decisions(plan, after_decisions),
    )


def _digest_with_decisions(plan, decisions) -> str:
    payload = plan.model_dump(mode="json")
    payload["decisions"] = [item.model_dump(mode="json") for item in decisions]
    encoded = json.dumps(
        payload,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _enum_value(value: Any) -> str:
    return str(getattr(value, "value", value))
