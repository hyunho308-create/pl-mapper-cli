"""Canonical, deterministic feedback composition.

The mapper deliberately keeps two kinds of truth:

* model-authored review items explain source meaning and the chosen treatment;
* deterministic checks supply arithmetic, severity, periods, and blocking state.

Rendering either stream independently loses context or repeats the same root
cause.  This module joins them before prose is produced and enforces that every
input reaches exactly one workbook destination.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import textwrap
from dataclasses import asdict, dataclass, field, replace
from typing import Any, Iterable, Literal

from hotel_pl_normalizer.mapping.coa import (
    SUMMARY_LINKS,
    SUMMARY_EQUATIONS,
    children_by_parent,
    dependency_coefficient,
    load_coa,
)
from hotel_pl_normalizer.mapping.findings import Finding, ensure_finding
from hotel_pl_normalizer.mapping.reviews import normalize_review_items
from hotel_pl_normalizer.mapping.rules import get_rule_policy
from hotel_pl_normalizer.mapping.tolerances import (
    feedback_match_tolerance,
    reconciliation_tolerance,
)
from hotel_pl_normalizer.models.evidence import evidence_display_map

SOURCE_PRESENTATION = "Source presentation"
MAPPING_TREATMENT = "Mapping treatment"
COVERAGE_GAP = "Coverage gap"
SCOPE_EXCLUSION = "Scope exclusion"
RECONCILIATION_DIFFERENCE = "Reconciliation difference"
VALIDATION_ERROR = "Validation error"
VALIDATION_WARNING = "Validation warning"
UNCLASSIFIED_REVIEW = "Unclassified review"

DERIVED_SUMMARY_FALLBACK = (
    "No conventional Summary section was found; Summary accounts were derived "
    "from mapped department totals and checked against available whole-P&L totals."
)

SECTION_LABELS = {
    "S1": "Rooms",
    "S2": "F&B",
    "S3": "OOD",
    "S4": "Miscellaneous Income",
    "S5": "A&G",
    "S6": "IT",
    "S7": "S&M",
    "S8": "POM",
    "S9": "Utilities",
    "S10": "Management Fees",
    "S11": "Non-Operating",
    "S12": "Summary",
}

@dataclass(frozen=True, slots=True)
class PeriodComparison:
    period_id: str
    period_label: str
    selected_value: float | None = None
    comparison_value: float | None = None
    variance: float | None = None
    amount: float | None = None
    ratio: float | None = None
    occupancy: float | None = None
    rooms_sold: float | None = None
    rooms_available: float | None = None


@dataclass(slots=True)
class CanonicalFeedbackFinding:
    finding_id: str
    category: str
    severity: Literal["info", "warning", "error"]
    action_required: bool
    destination: str
    primary_coa_id: str | None
    affected_coa_ids: list[str]
    explanation: str
    source_refs: list[str]
    periods: list[PeriodComparison]
    consequences: list[str]
    source_input_ids: list[str]
    rendered_text: str

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["periods"] = [asdict(item) for item in self.periods]
        return payload


@dataclass(frozen=True, slots=True)
class FeedbackInputDisposition:
    input_id: str
    input_type: Literal[
        "review_item", "check", "execution_issue", "exception"
    ]
    finding_id: str
    status: Literal[
        "rendered", "consequence_of", "superseded_by", "internal_only"
    ]


@dataclass(slots=True)
class FeedbackBundle:
    findings: list[CanonicalFeedbackFinding]
    inputs: list[FeedbackInputDisposition]
    rendered_count: int
    unmatched_count: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "findings": [item.to_dict() for item in self.findings],
            "inputs": [asdict(item) for item in self.inputs],
            "rendered_count": self.rendered_count,
            "unmatched_count": self.unmatched_count,
        }


class FeedbackCompositionError(RuntimeError):
    """A canonical feedback manifest failed its non-loss contract."""


def fallback_feedback_manifest(result: Any, error: Exception) -> dict[str, Any]:
    """Record a renderer failure without discarding the raw feedback inputs."""
    manifest = {
        "mode": "fallback",
        "composition_error": f"{type(error).__name__}: {error}",
        "rendered_count": 0,
        "unmatched_count": (
            len(getattr(result, "checks", None) or [])
            + len(getattr(result, "review_items", None) or [])
            + len(getattr(result, "execution_issues", None) or [])
            + len(getattr(result, "exceptions", None) or [])
        ),
    }
    result.feedback_manifest = manifest
    return manifest


@dataclass(slots=True)
class _Input:
    input_id: str
    input_type: str
    payload: Any
    period_id: str | None = None


@dataclass(slots=True)
class _Check:
    source: _Input
    period_id: str
    period_label: str
    severity: str
    rule: str
    target: str
    details: dict[str, Any]
    raw: str
    review_item_id: str | None
    note: str | None = None


@dataclass(slots=True)
class _Review:
    source: _Input
    kind: str
    message: str
    coa_ids: list[str]
    source_rows: list[str]
    selected_source_rows: list[str]
    alternate_source_rows: list[str]
    requires_human_decision: bool
    review_item_id: str | None
    mapping_treatment: str | None = None
    period_ids: list[str] = field(default_factory=list)


@dataclass(slots=True)
class _Exception:
    source: _Input
    rule: str
    period_id: str
    period_label: str
    target: str
    selected_value: float | None
    comparison_value: float | None
    variance: float | None
    treatment: str
    source_rows: list[str]
    review_item_id: str | None


@dataclass(slots=True)
class _FindingBuilder:
    key: str
    category: str
    severity: str
    action_required: bool
    primary_coa_id: str | None
    explanation: str
    affected_coa_ids: list[str] = field(default_factory=list)
    source_refs: list[str] = field(default_factory=list)
    periods: dict[str, PeriodComparison] = field(default_factory=dict)
    consequences: list[str] = field(default_factory=list)
    source_input_ids: list[str] = field(default_factory=list)
    review_input_ids: list[str] = field(default_factory=list)
    rules: set[str] = field(default_factory=set)
    department_comparison: str | None = None
    alternate_subtotal: str | None = None
    comparison_subjects: tuple[str, str] | None = None

    def add_input(self, item: _Input, *, review: bool = False) -> None:
        if item.input_id not in self.source_input_ids:
            self.source_input_ids.append(item.input_id)
        if review and item.input_id not in self.review_input_ids:
            self.review_input_ids.append(item.input_id)

    def add_accounts(self, values: Iterable[str]) -> None:
        for value in values:
            if value and value not in self.affected_coa_ids:
                self.affected_coa_ids.append(value)

    def add_refs(self, values: Iterable[str]) -> None:
        for value in values:
            if value and value not in self.source_refs:
                self.source_refs.append(value)


def _plain(value: Any) -> Any:
    if isinstance(value, Finding):
        return value.to_dict()
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if isinstance(value, dict):
        return dict(value)
    return value


def _field(value: Any, name: str, default=None):
    if isinstance(value, dict):
        return value.get(name, default)
    return getattr(value, name, default)


def _stable_digest(kind: str, payload: Any, occurrence: int = 0) -> str:
    serialized = json.dumps(
        {"kind": kind, "payload": _plain(payload), "occurrence": occurrence},
        sort_keys=True,
        default=str,
        separators=(",", ":"),
    )
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()[:16]


def _inputs(kind: str, values: Iterable[Any], *, periods=None) -> list[_Input]:
    seen: dict[str, int] = {}
    output = []
    for index, value in enumerate(values):
        period_id = periods[index] if periods else None
        identity = {"payload": _plain(value), "period_id": period_id}
        signature = json.dumps(identity, sort_keys=True, default=str)
        occurrence = seen.get(signature, 0)
        seen[signature] = occurrence + 1
        output.append(
            _Input(
                input_id=f"{kind}:{_stable_digest(kind, identity, occurrence)}",
                input_type=kind,
                payload=value,
                period_id=period_id,
            )
        )
    return output


def _parse_details(parts: list[str]) -> dict[str, str]:
    return {
        key.strip(): value.strip()
        for part in parts
        if "=" in part
        for key, value in [part.split("=", 1)]
    }


def _parse_checks(
    checks_by_period: dict[str, list[Any]], period_labels: dict[str, str]
) -> tuple[list[_Check], list[_Input]]:
    raw_values = []
    raw_periods = []
    for period_id, checks in checks_by_period.items():
        for check in checks or []:
            raw_values.append(check)
            raw_periods.append(period_id)
    inputs = _inputs("check", raw_values, periods=raw_periods)
    records = []
    for source in inputs:
        finding = ensure_finding(source.payload, period_id=source.period_id)
        raw = finding.to_legacy_string()
        period_id = source.period_id or "selected"
        records.append(
            _Check(
                source=source,
                period_id=period_id,
                period_label=period_labels.get(period_id, period_id),
                severity=finding.severity,
                rule=finding.rule,
                target=finding.target,
                details=dict(finding.details),
                raw=raw,
                review_item_id=finding.review_item_id,
                note=finding.note,
            )
        )
    return records, inputs


def _parse_reviews(values: Iterable[Any]) -> tuple[list[_Review], list[_Input]]:
    materialized = list(values or [])
    inputs = _inputs("review_item", materialized)
    records = []
    for source, value in zip(inputs, normalize_review_items(materialized)):
        records.append(
            _Review(
                source=source,
                kind=value.kind,
                message=value.message.strip(),
                mapping_treatment=value.mapping_treatment,
                period_ids=value.period_ids,
                coa_ids=value.coa_ids,
                source_rows=list(dict.fromkeys([
                    *value.source_rows, *value.selected_source_rows,
                    *value.alternate_source_rows, *value.selected_excluded_rows,
                    *value.alternate_excluded_rows,
                ])),
                selected_source_rows=value.selected_source_rows,
                alternate_source_rows=value.alternate_source_rows,
                requires_human_decision=value.requires_human_decision,
                review_item_id=value.review_item_id,
            )
        )
    return records, inputs


def _number(value: Any) -> float | None:
    try:
        if value is None or isinstance(value, bool):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _parse_exceptions(values: Iterable[Any]) -> tuple[list[_Exception], list[_Input]]:
    materialized = list(values or [])
    inputs = _inputs("exception", materialized)
    records = []
    for source in inputs:
        value = source.payload
        records.append(
            _Exception(
                source=source,
                rule=str(_field(value, "rule", "") or ""),
                period_id=str(_field(value, "period_id", "selected") or "selected"),
                period_label=str(
                    _field(value, "period_label", "Selected period")
                    or "Selected period"
                ),
                target=str(_field(value, "target", "") or ""),
                selected_value=_number(_field(value, "reported_value")),
                comparison_value=_number(_field(value, "comparison_value")),
                variance=_number(_field(value, "variance")),
                treatment=str(_field(value, "treatment", "") or "").strip(),
                source_rows=list(_field(value, "source_rows", []) or []),
                review_item_id=(
                    str(_field(value, "review_item_id", "") or "") or None
                ),
            )
        )
    return records, inputs


def _category_for_rule(rule: str, severity: str) -> str:
    if severity == "error":
        return VALIDATION_ERROR
    try:
        return get_rule_policy(rule).category
    except KeyError:
        return VALIDATION_WARNING


def _category_for_exception(rule: str) -> str:
    try:
        return get_rule_policy(rule).category
    except KeyError:
        return UNCLASSIFIED_REVIEW


def _expected_review_kind(rule: str) -> str | None:
    if rule == "scope_exclusion":
        return "scope_exception"
    if _category_for_exception(rule) == SOURCE_PRESENTATION:
        return "source_discrepancy"
    return None


def _match_review(exception: _Exception, reviews: list[_Review]) -> _Review | None:
    reviews = [
        review for review in reviews
        if not review.period_ids or exception.period_id in review.period_ids
    ]
    if exception.review_item_id:
        return next(
            (
                review
                for review in reviews
                if review.review_item_id == exception.review_item_id
            ),
            None,
        )
    expected = _expected_review_kind(exception.rule)
    if expected is None:
        # Historical coverage exceptions sometimes already contain the exact
        # source-review explanation but lack its ID. Never fuzzy-match prose.
        matches = [review for review in reviews if exception.treatment
                   and review.message == exception.treatment
                   and exception.target in review.coa_ids
                   and review.kind == "source_discrepancy"]
        if len(matches) == 1:
            return matches[0]
        return None
    scored = []
    for index, review in enumerate(reviews):
        if review.kind != expected or exception.target not in review.coa_ids:
            continue
        if review.kind == "scope_exception" and review.requires_human_decision:
            continue
        score = 20
        if exception.treatment and review.message == exception.treatment:
            score += 100
        if exception.source_rows and set(exception.source_rows) == set(review.source_rows):
            score += 30
        elif exception.source_rows and set(exception.source_rows) <= set(review.source_rows):
            score += 15
        if review.coa_ids and review.coa_ids[0] == exception.target:
            score += 10
        if exception.rule == "source_layer_conflict" and review.selected_source_rows:
            score += 20
        scored.append((score, -index, review))
    return max(scored, default=(0, 0, None), key=lambda item: item[:2])[2]


def _comparison_from_exception(exception: _Exception) -> PeriodComparison:
    return PeriodComparison(
        period_id=exception.period_id,
        period_label=exception.period_label,
        selected_value=exception.selected_value,
        comparison_value=exception.comparison_value,
        variance=exception.variance,
    )


def _comparison_from_check(check: _Check) -> PeriodComparison:
    selected_value = _number(
        check.details.get("actual", check.details.get("parent"))
    )
    comparison_value = _number(
        check.details.get("expected", check.details.get("children"))
    )
    variance = _number(check.details.get("variance"))
    if variance is None and selected_value is not None and comparison_value is not None:
        variance = selected_value - comparison_value
    return PeriodComparison(
        period_id=check.period_id,
        period_label=check.period_label,
        selected_value=selected_value,
        comparison_value=comparison_value,
        variance=variance,
        amount=_number(
            check.details.get("remainder", check.details.get("plug"))
        ),
        ratio=_number(check.details.get("ratio")),
        occupancy=_number(check.details.get("occupancy")),
        rooms_sold=_number(check.details.get("rooms_sold")),
        rooms_available=_number(check.details.get("rooms_available")),
    )


def _is_summary_department_check(check: _Check) -> bool:
    return (check.rule == "summary_department"
            or check.details.get("rule") == "summary_department")


def _merge_comparison(
    existing: PeriodComparison,
    incoming: PeriodComparison,
) -> PeriodComparison:
    return PeriodComparison(
        period_id=existing.period_id,
        period_label=existing.period_label,
        selected_value=(
            existing.selected_value
            if existing.selected_value is not None
            else incoming.selected_value
        ),
        comparison_value=(
            existing.comparison_value
            if existing.comparison_value is not None
            else incoming.comparison_value
        ),
        variance=(
            existing.variance
            if existing.variance is not None
            else incoming.variance
        ),
        amount=(
            existing.amount if existing.amount is not None else incoming.amount
        ),
        ratio=existing.ratio if existing.ratio is not None else incoming.ratio,
        occupancy=incoming.occupancy if incoming.occupancy is not None else existing.occupancy,
        rooms_sold=incoming.rooms_sold if incoming.rooms_sold is not None else existing.rooms_sold,
        rooms_available=incoming.rooms_available if incoming.rooms_available is not None else existing.rooms_available,
    )


def _default_explanation(category: str, rule: str) -> str:
    if category == SOURCE_PRESENTATION:
        return "Two independently reported source presentations differ; the supported selected presentation was retained."
    if category == RECONCILIATION_DIFFERENCE:
        return "The reported total differs slightly from its related reported accounts; both reported values were retained."
    if category == COVERAGE_GAP:
        if rule == "unsupported_residual_remainder":
            return "A material all-other amount is calculated from a subtotal remainder rather than directly identified source detail."
        if rule == "large_residual_plug":
            return "A material remaining difference was assigned to the available all-other account."
        if rule == "unresolved_negative_residual":
            return "A material negative remainder was not forced into an all-other account."
        return "The mapped child accounts do not fully explain the mapped parent total."
    if category == SCOPE_EXCLUSION:
        return "A materially populated item was excluded from the Standard COA based on the workbook's supported scope."
    return rule.replace("_", " ").strip().capitalize() or "A finding requires review."


def _check_explanation(check: _Check, coa: dict[str, dict]) -> str:
    if check.rule == "source_control_difference":
        return f"{check.details.get('label', 'Source subtotal')} ({check.details.get('total_row', '')})."
    if check.rule == "source_control_unverified":
        return f"{check.details.get('label', 'Source subtotal')} ({check.details.get('total_row', '')}): missing source values prevent checking this subtotal."
    message = str(check.details.get("message") or "").strip()
    if message:
        return message
    if check.rule in {"occupancy_above_capacity", "invalid_rooms_available", "non_residual_plug"} and check.note:
        return check.note
    category = _category_for_rule(check.rule, check.severity)
    if check.rule == "summary_math":
        equation = str(check.details.get("equation") or "").strip()
        if equation:
            return f"The reported Summary amount does not equal {equation}."
        return "The reported Summary amount does not satisfy its required accounting equation."
    if check.rule == "summary_department":
        return "The independently reported Summary and department amounts do not reconcile."
    if check.rule in {"hierarchy_complete", "hierarchy_partial_with_residual"}:
        return "The reported parent and its mapped child accounts do not reconcile."
    if check.rule == "coverage_inconsistent":
        return "The mapping declares no child detail even though child accounts are populated."
    if check.rule == "parent_no_value_with_children":
        return "Child accounts are populated while their related parent account is blank."
    if check.rule == "source_row_double_count":
        accounts = _check_accounts(check, coa)
        account_text = _join_phrases(accounts)
        if account_text:
            return (
                f"{check.target} is assigned to {account_text}, which are not "
                "one connected mapping path."
            )
        return f"{check.target} may have been assigned to unrelated accounts."
    if check.rule == "source_row_repeated":
        return "The same source row appears more than once in one account calculation."
    if check.rule == "source_row_included_and_excluded":
        return "The same source row is both included and excluded in one mapping path."
    if check.rule == "invalid_source_layer_comparison":
        return "The selected-versus-alternate source comparison is not mathematically or structurally valid."
    if check.rule == "period_detail_available":
        return "An equivalent source row supplies a requested period that the selected row leaves blank."
    if check.rule == "unused_financial_schedule":
        return "A populated routed department schedule was neither mapped nor identified as a valid duplicate or supporting schedule."
    if check.rule == "non_operating_sign":
        return "The normalized non-operating income sign is inconsistent with the Standard COA convention."
    return _default_explanation(category, check.rule)


def _clean_message(
    text: str,
    coa: dict[str, dict],
    source_refs: list[str],
    *,
    quantified: bool = False,
    source_ref_displays: dict[str, str] | None = None,
) -> str:
    output = str(text or "").strip()
    for coa_id in sorted(coa, key=len, reverse=True):
        account_name = str(coa.get(coa_id, {}).get("account_name") or "").strip()
        output = output.replace(
            coa_id,
            account_name or coa_id.split(".", 1)[-1].replace("_", " "),
        )
    output = re.sub(
        r"\bSummary controls S12\b",
        "the Summary amount controls the Summary account",
        output,
        flags=re.IGNORECASE,
    )
    for section, label in SECTION_LABELS.items():
        if section != "S12":
            output = re.sub(
                rf"\bcontrols\s+{re.escape(section)}\b",
                f"controls the {label} detail",
                output,
                flags=re.IGNORECASE,
            )
        output = re.sub(rf"\b{re.escape(section)}\b", label, output)
    output = re.sub(
        r"\bSummary controls Summary\b",
        "the Summary amount controls the Summary account",
        output,
        flags=re.IGNORECASE,
    )
    for source_ref in sorted(set(source_refs), key=len, reverse=True):
        if "!" not in source_ref:
            continue
        sheet, row = source_ref.rsplit("!", 1)
        display = (source_ref_displays or {}).get(source_ref, f"{sheet} row {row}")
        output = output.replace(source_ref, display)
    output = re.sub(r"\b([\w&.-]+)!(\d+)\b", r"\1 row \2", output)
    output = output.replace("no_value", "left blank")
    output = re.sub(
        r"^(?:Note:\s*)?(?:Source layer conflict|Source discrepancy|Scope note|Mapping convention):\s*",
        "",
        output,
        flags=re.IGNORECASE,
    )
    # Model-authored explanations can contain cents or fractional percentages,
    # while the workbook deliberately presents reviewer feedback at whole-number
    # precision.  Normalize prose here as well as the structured period sentence
    # so the two parts never disagree merely because one retained decimals.
    def round_number(match: re.Match) -> str:
        number = float(match.group("number").replace(",", ""))
        return (
            f"{match.group('sign')}{number:,.0f}{match.group('percent')}"
        )

    output = re.sub(
        r"(?<![\w.])(?P<sign>-?)(?P<currency>\$?)"
        r"(?P<number>\d[\d,]*\.\d+)(?P<percent>%?)(?!\w)",
        round_number,
        output,
    )
    output = output.replace("$", "")
    if quantified:
        # A structured period sentence immediately supplies the exact amount
        # and direction, so vague model prose such as "the difference is
        # disclosed" adds repetition and can even contain awkward number words.
        output = re.sub(
            r"\s+and\s+the\s+[^.;]*\bdifference\s+(?:is|was)\s+disclosed\b",
            "",
            output,
            flags=re.IGNORECASE,
        )
    output = re.sub(r"\s+([,.;:])", r"\1", output)
    output = re.sub(r"\s{2,}", " ", output).strip(" ,;")
    if output and output[-1] not in ".?!":
        output += "."
    return output


def _rounded_number(value: float) -> str:
    return f"{abs(float(value)):,.0f}"


def _join_phrases(values: list[str]) -> str:
    if not values:
        return ""
    if len(values) == 1:
        return values[0]
    if len(values) == 2:
        return f"{values[0]} and {values[1]}"
    return f"{', '.join(values[:-1])}, and {values[-1]}"


VARIANCE_PHRASES = {
    "summary_department": (
        "{amount} above the independently reported department amount in {period}",
        "{amount} below the independently reported department amount in {period}",
        "equal to the independently reported department amount in {period}",
    ),
    "summary_math": (
        "{amount} above the required equation in {period}",
        "{amount} below the required equation in {period}",
        "equal to the required equation in {period}",
    ),
    "hierarchy": (
        "{amount} below the parent in {period}",
        "{amount} above the parent in {period}",
        "equal to the parent in {period}",
    ),
    "relative": (
        "{amount} higher in {period}",
        "{amount} lower in {period}",
        "equal in {period}",
    ),
}


def _variance_phrases(
    periods: list[PeriodComparison], phrase_kind: str
) -> list[str]:
    positive, negative, equal = VARIANCE_PHRASES[phrase_kind]
    return [
        (
            positive
            if item.variance > 0
            else negative
            if item.variance < 0
            else equal
        ).format(
            amount=_rounded_number(item.variance),
            period=item.period_label,
        )
        for item in periods
    ]


def _period_sentence(
    category: str,
    periods: list[PeriodComparison],
    *,
    rules: set[str] | None = None,
    explanation: str = "",
) -> str:
    rules = rules or set()
    if "occupancy_above_capacity" in rules:
        return " ".join(
            f"Occupancy is {item.occupancy:.2%} in {item.period_label}, above 100%."
            for item in periods if item.occupancy is not None
        )
    if "invalid_rooms_available" in rules:
        return " ".join(
            f"{item.period_label}: {item.rooms_sold:,.0f} rooms sold against "
            f"{item.rooms_available:,.0f} available room nights. Review source capacity."
            for item in periods if item.rooms_sold is not None and item.rooms_available is not None
        )
    if "source_control_difference" in rules:
        measured = [item for item in periods if item.variance is not None
                    and item.comparison_value is not None
                    and not _below_reconciliation_tolerance(item)]
        if not measured:
            return ""
        if all((item.variance < 0) == (measured[0].variance < 0) for item in measured):
            direction = "below" if measured[0].variance < 0 else "above"
            amounts = _join_phrases([
                f"{abs(item.variance):,.2f} in {item.period_label}" for item in measured
            ])
            return f"The source subtotal is {direction} its listed component total by {amounts}."
        return "The source subtotal is " + _join_phrases([
            f"{'below' if item.variance < 0 else 'above'} its listed component total "
            f"by {abs(item.variance):,.2f} in {item.period_label}" for item in measured
        ]) + "."
    if category == COVERAGE_GAP and rules & {
        "large_residual_plug",
        "unsupported_residual_remainder",
        "unresolved_negative_residual",
    }:
        measured = [item for item in periods if item.amount is not None]
        phrases = []
        for item in measured:
            ratio = (
                f" ({abs(item.ratio):.1%} of the parent)"
                if item.ratio is not None
                else ""
            )
            phrases.append(
                f"{_rounded_number(item.amount)}{ratio} in {item.period_label}"
            )
        if not phrases:
            return ""
        amounts = _join_phrases(phrases)
        if "unresolved_negative_residual" in rules:
            return f"A negative remainder of {amounts} was not forced into an all-other account."
        if "large_residual_plug" in rules:
            return f"A remaining difference of {amounts} was assigned to the all-other account."
        return (
            f"A calculated all-other remainder of {amounts} lacks directly "
            "identified source detail."
        )
    quantified = [item for item in periods if item.variance is not None]
    if category in {SOURCE_PRESENTATION, RECONCILIATION_DIFFERENCE} and quantified:
        quantified = [item for item in quantified if not _below_reconciliation_tolerance(item)]
        if not quantified:
            return ""
    if not quantified:
        if periods and category in {
            SOURCE_PRESENTATION,
            VALIDATION_ERROR,
            VALIDATION_WARNING,
            UNCLASSIFIED_REVIEW,
        }:
            return "Affected periods: " + _join_phrases(
                [item.period_label for item in periods]
            ) + "."
        return ""
    if category in {VALIDATION_ERROR, VALIDATION_WARNING}:
        if "summary_department" in rules:
            phrases = _variance_phrases(quantified, "summary_department")
            return f"The Summary amount is {_join_phrases(phrases)}."
        if "summary_math" in rules:
            phrases = _variance_phrases(quantified, "summary_math")
            return f"The reported Summary amount is {_join_phrases(phrases)}."
        if rules & {
            "hierarchy_complete",
            "hierarchy_partial_with_residual",
        }:
            phrases = _variance_phrases(quantified, "hierarchy")
            return f"Child accounts are {_join_phrases(phrases)}."
    if category == SOURCE_PRESENTATION:
        phrases = _variance_phrases(quantified, "relative")
        return (
            "Compared with the alternate source, the selected amount is "
            f"{_join_phrases(phrases)}."
        )
    if category == COVERAGE_GAP:
        if all(item.comparison_value == 0 for item in quantified):
            return "No child breakdown mapped: " + _join_phrases([
                f"{_rounded_number(item.selected_value)} in {item.period_label}"
                for item in quantified if item.selected_value is not None
            ]) + "."
        phrases = _variance_phrases(quantified, "hierarchy")
        return f"Identified children are {_join_phrases(phrases)}."
    if category == RECONCILIATION_DIFFERENCE:
        phrases = _variance_phrases(quantified, "relative")
        return f"The reported total is {_join_phrases(phrases)}."
    if category in {VALIDATION_ERROR, VALIDATION_WARNING, UNCLASSIFIED_REVIEW}:
        phrases = [
            f"{item.period_label}: {_rounded_number(item.variance)} difference"
            for item in quantified
        ]
        return f"Affected amounts: {'; '.join(phrases)}."
    return ""


def _account_name(coa_id: str | None, coa: dict[str, dict]) -> str:
    if not coa_id:
        return "the related account"
    return str(coa.get(coa_id, {}).get("account_name") or coa_id)


def _render(
    builder: _FindingBuilder,
    coa: dict[str, dict],
    source_ref_displays: dict[str, str] | None = None,
) -> str:
    if builder.department_comparison:
        measured = [item for item in builder.periods.values() if item.variance is not None]
        phrases = [
            f"{'below' if item.variance > 0 else 'above'} Summary by "
            f"{_rounded_number(item.variance)} in {item.period_label}"
            for item in builder.periods.values() if item.variance is not None
        ]
        if phrases:
            verb = "is" if builder.department_comparison.endswith("revenue") else "are"
            if all(item.variance > 0 for item in measured) or all(item.variance < 0 for item in measured):
                direction = "below" if measured[0].variance > 0 else "above"
                amounts = _join_phrases([
                    f"{_rounded_number(item.variance)} in {item.period_label}" for item in measured
                ])
                return f"{builder.department_comparison} {verb} {direction} Summary by {amounts}."
            return f"{builder.department_comparison} {verb} {_join_phrases(phrases)}."
    if builder.alternate_subtotal:
        phrases = _variance_phrases(list(builder.periods.values()), "relative")
        name = SECTION_LABELS.get((builder.primary_coa_id or "").split(".")[0], "Department")
        return (f"Mapped {name} expenses match Summary. The mapped total is "
                f"{_join_phrases(phrases)} than the separately reported "
                f"{builder.alternate_subtotal} subtotal.")
    if builder.comparison_subjects:
        selected, alternate = builder.comparison_subjects
        phrases = _variance_phrases([item for item in builder.periods.values()
                                    if item.variance is not None
                                    and not _below_reconciliation_tolerance(item)], "relative")
        sentence = f"{selected} is {_join_phrases(phrases)} than {alternate}."
        missing = [item.period_label for item in builder.periods.values() if item.variance is None]
        if missing:
            sentence += f" Could not verify this comparison for {_join_phrases(missing)}."
        treatment = builder.explanation
        if treatment and treatment != "The mapped amount differs from the cited source comparison.":
            return _clean_message(treatment, coa, builder.source_refs) + " " + sentence
        return sentence
    quantified = any(item.variance is not None for item in builder.periods.values())
    if quantified and builder.category in {SOURCE_PRESENTATION, COVERAGE_GAP, RECONCILIATION_DIFFERENCE} and not builder.review_input_ids:
        # Numeric findings state what was measured, not a model's diagnosis of
        # why the source or mapping differs. Raw explanations remain in the log.
        explanation_text = _default_explanation(builder.category, next(iter(sorted(builder.rules)), ""))
    else:
        explanation_text = builder.explanation
    explanation = _clean_message(
        explanation_text,
        coa,
        builder.source_refs,
        quantified=quantified,
        source_ref_displays=source_ref_displays,
    )
    prefix = "Needs review" if builder.severity == "error" else ""
    if builder.review_input_ids:
        # Bound the authored clause, never the calculated discrepancy sentence.
        explanation = textwrap.shorten(explanation, width=180, placeholder="...")
    first = f"{prefix}: {explanation}" if prefix else explanation
    if builder.review_input_ids and builder.periods and not quantified:
        return first + (f" {' '.join(builder.consequences)}" if builder.consequences else "")
    sentences = [first]
    period_sentence = _period_sentence(
        builder.category,
        list(builder.periods.values()),
        rules=builder.rules,
        explanation=explanation,
    )
    if period_sentence and builder.rules & {"occupancy_above_capacity", "invalid_rooms_available"}:
        return f"{prefix}: {period_sentence}" if prefix else period_sentence
    if period_sentence and "source_control_difference" in builder.rules:
        label = _clean_message(builder.explanation, coa, builder.source_refs, source_ref_displays=source_ref_displays)
        return f"{prefix + ': ' if prefix else ''}{label} {period_sentence}"
    if period_sentence and builder.category in {COVERAGE_GAP, RECONCILIATION_DIFFERENCE}:
        # Treatment, if present, is added by the structured merge below.
        return f"{prefix + ': ' if prefix else ''}{period_sentence}" + (f" {' '.join(builder.consequences)}" if builder.consequences else "")
    if (
        period_sentence
        and builder.rules & {"summary_department", "hierarchy_complete"}
        and explanation
        in {
            "The independently reported Summary and department amounts do not reconcile.",
            "The reported parent and its mapped child accounts do not reconcile.",
        }
    ):
        return f"{prefix + ': ' if prefix else ''}{period_sentence}"
    if (
        period_sentence
        and "summary_math" in builder.rules
        and explanation.startswith("The reported Summary amount does not equal ")
    ):
        equation = explanation.removeprefix(
            "The reported Summary amount does not equal "
        ).rstrip(".")
        return f"{prefix}: {period_sentence.replace('the required equation', equation)}"
    if period_sentence:
        sentences.append(period_sentence)
    if builder.consequences:
        consequence = " ".join(builder.consequences)
        if sentences and period_sentence:
            sentences[-1] = sentences[-1].rstrip(".") + f"; {consequence}"
            if sentences[-1][-1] not in ".?!":
                sentences[-1] += "."
        else:
            sentences.append(consequence)
    return " ".join(value for value in sentences if value).strip()


def _primary_for_review(review: _Review, coa: dict[str, dict], values_by_period=None) -> str | None:
    candidates = [coa_id for coa_id in review.coa_ids if coa_id in coa]
    if not candidates:
        return None
    populated = [item for item in candidates if any(
        values.get(item) not in (None, 0)
        for period, values in (values_by_period or {}).items()
        if not review.period_ids or period in review.period_ids
    )]
    if populated:
        candidates = populated
    if review.kind == "unusual_convention":
        summary = [item for item in candidates if item.startswith("S12.")]
        if summary and "summary" in review.message.casefold():
            return summary[0]
        non_summary = [item for item in candidates if not item.startswith("S12.")]
        if non_summary:
            # COA IDs are ordered responsible-account first. Older runs often
            # cite the parent followed by blank descendants; never pick the
            # deepest child merely because it is most specific.
            return non_summary[0]
    return candidates[0]


def _details_shape(details: dict[str, Any]) -> tuple[tuple[str, Any], ...]:
    numeric_keys = {
        "actual",
        "expected",
        "variance",
        "parent",
        "children",
        "plug",
        "ratio",
        "remainder",
    }
    return tuple(sorted((key, value) for key, value in details.items() if key not in numeric_keys))


def _same_number(left: float | None, right: float | None) -> bool:
    if left is None or right is None:
        return left is right
    tolerance = feedback_match_tolerance(left, right)
    return abs(left - right) <= tolerance


def _match_check_exception(
    check: _Check,
    exceptions: list[_Exception],
) -> _Exception | None:
    matches = []
    for exception in exceptions:
        if check.review_item_id or exception.review_item_id:
            if check.review_item_id != exception.review_item_id:
                continue
        if (
            exception.period_id != check.period_id
            or exception.rule != check.rule
            or exception.target != check.target
        ):
            continue
        check_variance = _number(check.details.get("variance"))
        if not _same_number(check_variance, exception.variance):
            continue
        matches.append(exception)
    if len(matches) == 1:
        return matches[0]
    if matches:
        roots = {
            (
                item.treatment,
                tuple(item.source_rows),
                item.selected_value,
                item.comparison_value,
                item.variance,
            )
            for item in matches
        }
        if len(roots) == 1:
            return matches[0]
    return None


def _dependency_coefficient(
    source: str | None,
    target: str | None,
    coa: dict[str, dict],
) -> int | float | None:
    return dependency_coefficient(source, target, coa)


def _is_downstream_consequence(
    upstream: _FindingBuilder,
    downstream: _FindingBuilder,
    coa: dict[str, dict],
) -> bool:
    if upstream.category not in {SOURCE_PRESENTATION, RECONCILIATION_DIFFERENCE}:
        return False
    if upstream.category == SOURCE_PRESENTATION and not upstream.review_input_ids:
        return False
    if downstream.review_input_ids or downstream.severity == "error":
        return False
    if downstream.category not in {
        SOURCE_PRESENTATION,
        COVERAGE_GAP,
        RECONCILIATION_DIFFERENCE,
    }:
        return False
    coefficient = _dependency_coefficient(
        upstream.primary_coa_id,
        downstream.primary_coa_id,
        coa,
    )
    if coefficient is None or not downstream.periods:
        return False
    for period_id, comparison in downstream.periods.items():
        source = upstream.periods.get(period_id)
        difference = comparison.variance
        if difference is None and upstream.primary_coa_id == downstream.primary_coa_id:
            difference = comparison.amount
        if source is None or source.variance is None or difference is None:
            return False
        if not _same_number(source.variance * coefficient, difference):
            return False
    return True


def _builder_finding_id(builder: _FindingBuilder) -> str:
    identity = {
        "category": builder.category,
        "primary_coa_id": builder.primary_coa_id,
        "source_input_ids": sorted(builder.source_input_ids),
    }
    return f"finding:{_stable_digest('finding', identity)}"


def _derived_summary_supersessions(
    reviews: list[_Review],
) -> dict[str, str]:
    """Pair the generic derived-Summary note with a more specific one."""
    output = {}
    for fallback in reviews:
        if (
            fallback.kind != "unusual_convention"
            or fallback.message != DERIVED_SUMMARY_FALLBACK
        ):
            continue
        replacement = next(
            (
                review
                for review in reviews
                if review.source.input_id != fallback.source.input_id
                and review.kind == "unusual_convention"
                and any(coa_id.startswith("S12.") for coa_id in review.coa_ids)
                and "summary" in review.message.casefold()
                and any(
                    term in review.message.casefold()
                    for term in ("derived", "integrated", "all-departments")
                )
            ),
            None,
        )
        if replacement is not None:
            output[fallback.source.input_id] = replacement.source.input_id
    return output


def _check_accounts(check: _Check, coa: dict[str, dict]) -> list[str]:
    candidates = []
    if check.target in coa:
        candidates.append(check.target)
    candidates.extend(
        item.strip()
        for item in str(check.details.get("coa_ids") or "").split(",")
        if item.strip() in coa
    )
    return list(dict.fromkeys(candidates))


def _review_comparisons(
    review: _Review,
    rows: dict[str, Any],
    labels: dict[str, str],
) -> dict[str, PeriodComparison]:
    """Use the existing source arithmetic; never infer a difference from prose."""
    from hotel_pl_normalizer.mapping.mapper import MappingReviewItem, _source_layer_value

    if review.kind != "source_discrepancy" or not labels:
        return {}
    value = normalize_review_items([review.source.payload])[0]
    if not value.selected_source_rows or not value.alternate_source_rows:
        return {}
    try:
        MappingReviewItem.model_validate({
            key: item for key, item in asdict(value).items() if key != "review_item_id"
        })
    except ValueError:
        return {}
    cited = {
        *value.selected_source_rows, *value.alternate_source_rows,
        *value.selected_excluded_rows, *value.alternate_excluded_rows,
    }
    comparisons = {}
    for period_id, label in labels.items():
        if review.period_ids and period_id not in review.period_ids:
            continue
        # Missing evidence in one period must not erase verified comparisons
        # in other periods or silently turn the missing amount into zero.
        comparisons[period_id] = PeriodComparison(
            period_id=period_id, period_label=label,
        )
        # The legacy arithmetic falls back to the primary value when a period
        # key is missing. That cannot prove a difference is small in this period.
        if any(
            row not in rows
            or (
                period_id not in (rows[row].get("selected_values") or {})
                and (rows[row].get("selected_values") or len(labels) != 1)
            )
            for row in cited
        ):
            continue
        try:
            selected = _source_layer_value(
                rows, value.selected_source_rows, value.selected_excluded_rows,
                value.selected_source_operation, period_id,
            )
            alternate = _source_layer_value(
                rows, value.alternate_source_rows, value.alternate_excluded_rows,
                value.alternate_source_operation, period_id,
            )
        except (KeyError, TypeError, ValueError):
            continue
        if selected is None or alternate is None or not all(
            math.isfinite(number) for number in (selected, alternate)
        ):
            continue
        comparisons[period_id] = PeriodComparison(
            period_id=period_id, period_label=label,
            selected_value=selected, comparison_value=alternate,
            variance=selected - alternate,
        )
    return comparisons


def _below_reconciliation_tolerance(comparison: PeriodComparison) -> bool:
    return (
        comparison.selected_value is not None
        and comparison.variance is not None
        and math.isfinite(comparison.selected_value)
        and math.isfinite(comparison.variance)
        and abs(comparison.variance) <= reconciliation_tolerance(comparison.selected_value)
    )


def _same_adjustment(
    treatment: _Review,
    discrepancy: _Review,
    comparisons: dict[str, PeriodComparison],
    rows: dict[str, Any],
) -> bool:
    """Link a treatment only when its cited adjustment explains the conflict."""
    from hotel_pl_normalizer.mapping.mapper import _source_layer_value

    if set(treatment.period_ids) != set(discrepancy.period_ids):
        return False
    if not treatment.source_rows or not set(treatment.coa_ids) & set(discrepancy.coa_ids):
        return False
    value = normalize_review_items([discrepancy.source.payload])[0]
    adjustment_rows = set(treatment.source_rows)
    if adjustment_rows & set(value.alternate_source_rows + value.alternate_excluded_rows):
        return False
    if adjustment_rows <= set(value.selected_excluded_rows):
        sign = -1
    elif adjustment_rows <= set(value.selected_source_rows):
        sign = -1 if value.selected_source_operation == "negate" else 1
    else:
        return False
    if not comparisons or not any(item.variance for item in comparisons.values()):
        return False
    for period_id, comparison in comparisons.items():
        try:
            adjustment = _source_layer_value(rows, treatment.source_rows, [], "sum", period_id)
        except (KeyError, TypeError, ValueError):
            return False
        if adjustment is None or comparison.variance is None or not _same_number(
            sign * adjustment, comparison.variance
        ):
            return False
    return True


def _review_treatment(review: _Review, rows: dict[str, Any]) -> str | None:
    """Prefer separate treatment; recover legacy adjusted equations from rows."""
    if review.mapping_treatment and review.mapping_treatment.strip():
        treatment = review.mapping_treatment.strip()
        # Legacy boilerplate describes normal sourcing, not an adjustment.
        # Keep it in the raw review, but do not turn a rounding-only comparison
        # into a visible treatment note. Restrict this to sourcing-only clauses.
        clauses = re.split(r";|\bwhile\b", treatment, flags=re.I)
        if not all(re.fullmatch(r"[^.;]+\bcontrols?\b[^.;]+\.?", clause.strip(), re.I)
                   for clause in clauses):
            return treatment
        return None
    value = normalize_review_items([review.source.payload])[0]
    if not value.selected_excluded_rows or value.selected_source_operation != "adjusted_subtotal":
        return None
    cited = value.selected_source_rows + value.selected_excluded_rows
    if not all(rows.get(key, {}).get("label") for key in cited):
        return None
    included = _join_phrases([str(rows[key]["label"]) for key in value.selected_source_rows])
    excluded = _join_phrases([str(rows[key]["label"]) for key in value.selected_excluded_rows])
    return f"Mapped from {included}, less {excluded}."


def _merge_builder(target, prior, builders, dispositions, status="superseded_by"):
    target.add_accounts(prior.affected_coa_ids)
    target.add_refs(prior.source_refs)
    target.rules.update(prior.rules)
    for input_id in prior.source_input_ids:
        if input_id not in target.source_input_ids:
            target.source_input_ids.append(input_id)
        dispositions[input_id] = (target.key, status)
    target.review_input_ids.extend(
        item for item in prior.review_input_ids if item not in target.review_input_ids
    )
    builders.pop(prior.key)


def compose_feedback(
    *,
    checks_by_period: dict[str, list[Any]] | None,
    review_items: Iterable[Any] | None,
    exceptions: Iterable[Any] | None,
    execution_issues: Iterable[str] | None,
    execution_issues_by_period: dict[str, list[str]] | None,
    period_labels: dict[str, str] | None,
    coa: dict[str, dict],
    source_ref_displays: dict[str, str] | None = None,
    evidence_rows: Iterable[Any] | None = None,
    values_by_period: dict[str, dict[str, float | None]] | None = None,
) -> FeedbackBundle:
    """Join every final feedback input into one non-lossy finding bundle."""
    labels = dict(period_labels or {})
    checks, check_inputs = _parse_checks(checks_by_period or {}, labels)
    reviews, review_inputs = _parse_reviews(review_items or [])
    exception_records, exception_inputs = _parse_exceptions(exceptions or [])
    derived_summary_supersessions = _derived_summary_supersessions(reviews)
    rows = {row["row_key"]: row for row in evidence_rows or [] if _field(row, "row_key")}
    review_comparisons = {
        review.source.input_id: _review_comparisons(review, rows, labels)
        for review in reviews
    }

    # The mapper exposes both period-native issues and a flattened, labelled
    # compatibility list.  Build from the period-native records first and add
    # only genuinely independent flattened issues; otherwise every execution
    # failure would be shown twice.
    execution_values: list[str] = []
    execution_periods: list[str | None] = []
    represented_flattened: set[str] = set()
    for period_id, issues in (execution_issues_by_period or {}).items():
        for issue in issues or []:
            rendered = str(issue)
            execution_values.append(rendered)
            execution_periods.append(period_id)
            represented_flattened.add(rendered)
            represented_flattened.add(f"{period_id}: {rendered}")
            period_label = labels.get(period_id)
            if period_label:
                represented_flattened.add(f"{period_label}: {rendered}")
    for issue in execution_issues or []:
        rendered = str(issue)
        if rendered in represented_flattened:
            continue
        execution_values.append(rendered)
        execution_periods.append(None)
    execution_inputs = _inputs(
        "execution_issue",
        execution_values,
        periods=execution_periods,
    )

    all_inputs = [
        *review_inputs,
        *check_inputs,
        *exception_inputs,
        *execution_inputs,
    ]
    dispositions: dict[str, tuple[str, str]] = {}
    builders: dict[str, _FindingBuilder] = {}

    def builder(
        key: str,
        *,
        category: str,
        severity: str,
        action_required: bool,
        primary: str | None,
        explanation: str,
    ) -> _FindingBuilder:
        if key not in builders:
            builders[key] = _FindingBuilder(
                key=key,
                category=category,
                severity=severity,
                action_required=action_required,
                primary_coa_id=primary,
                explanation=explanation,
            )
        return builders[key]

    exception_builder: dict[str, str] = {}
    # Structured exceptions are the strongest existing join between source
    # meaning and deterministic period math, so they are composed first.
    for exception in exception_records:
        matched_review = _match_review(exception, reviews)
        category = _category_for_exception(exception.rule)
        key = (
            f"review:{matched_review.source.input_id}"
            if matched_review is not None
            else "exception:"
            + _stable_digest(
                "exception_group",
                {
                    "category": category,
                    "target": exception.target,
                    "treatment": exception.treatment,
                    "source_rows": exception.source_rows,
                    "rule": exception.rule,
                },
            )
        )
        finding = builder(
            key,
            category=category,
            severity="warning",
            action_required=False,
            primary=exception.target or (
                _primary_for_review(matched_review, coa, values_by_period)
                if matched_review is not None
                else None
            ),
            explanation=(
                matched_review.message
                if matched_review is not None
                else exception.treatment
                or _default_explanation(category, exception.rule)
            ),
        )
        finding.rules.add(exception.rule)
        finding.add_input(exception.source)
        finding.add_accounts([exception.target])
        finding.add_refs(exception.source_rows)
        finding.periods[exception.period_id] = _comparison_from_exception(exception)
        dispositions[exception.source.input_id] = (key, "rendered")
        exception_builder[exception.source.input_id] = key
        if matched_review is not None:
            finding.add_input(matched_review.source, review=True)
            finding.add_accounts(matched_review.coa_ids)
            finding.add_refs(matched_review.source_rows)
            dispositions[matched_review.source.input_id] = (key, "rendered")

    # Every model review survives in the audit, including those whose cited
    # arithmetic is below tolerance and does not need a visible warning.
    for review in reviews:
        if review.source.input_id in dispositions:
            key, _ = dispositions[review.source.input_id]
            for period in review.period_ids:
                if period in labels:
                    builders[key].periods.setdefault(
                        period, PeriodComparison(period_id=period, period_label=labels[period])
                    )
            continue
        if review.source.input_id in derived_summary_supersessions:
            continue
        if review.kind == "unusual_convention":
            category, severity, action = MAPPING_TREATMENT, "info", False
        elif review.kind == "source_discrepancy":
            category, severity, action = SOURCE_PRESENTATION, "warning", False
        elif review.kind == "scope_exception" and not review.requires_human_decision:
            category, severity, action = SCOPE_EXCLUSION, "warning", False
        elif review.kind in {"scope_exception", "ambiguity"}:
            category, severity, action = VALIDATION_ERROR, "error", True
        else:
            category, severity, action = UNCLASSIFIED_REVIEW, "warning", True
        key = f"review:{review.source.input_id}"
        finding = builder(
            key,
            category=category,
            severity=severity,
            action_required=action,
            primary=_primary_for_review(review, coa, values_by_period),
            explanation=review.message or "A model review item requires attention.",
        )
        finding.add_input(review.source, review=True)
        finding.add_accounts(review.coa_ids)
        finding.add_refs(review.source_rows)
        finding.periods.update({
            period: PeriodComparison(period_id=period, period_label=labels[period])
            for period in review.period_ids if period in labels
        })
        finding.periods.update(review_comparisons[review.source.input_id])
        dispositions[review.source.input_id] = (key, "rendered")

    # Combine source presentation and mapping treatment of the same proven
    # adjustment into one explanation; preserve both raw review input IDs.
    for treatment in reviews:
        if treatment.kind != "unusual_convention" or treatment.requires_human_decision or treatment.source.input_id not in dispositions:
            continue
        matches = [
            review for review in reviews
            if review.kind == "source_discrepancy"
            and not review.requires_human_decision
            and _same_adjustment(treatment, review, review_comparisons[review.source.input_id], rows)
        ]
        if len(matches) != 1:
            continue
        if sum(
            other.kind == "unusual_convention"
            and _same_adjustment(other, matches[0], review_comparisons[matches[0].source.input_id], rows)
            for other in reviews
        ) != 1:
            continue
        key, _ = dispositions[matches[0].source.input_id]
        old_key, _ = dispositions[treatment.source.input_id]
        if key == old_key or old_key not in builders:
            continue
        finding, prior = builders[key], builders[old_key]
        finding.explanation = treatment.message
        finding.periods.update(review_comparisons[matches[0].source.input_id])
        _merge_builder(finding, prior, builders, dispositions)

    for fallback_id, replacement_id in derived_summary_supersessions.items():
        if replacement_id not in dispositions:
            continue
        key, _status = dispositions[replacement_id]
        finding = builders[key]
        fallback = next(
            review for review in reviews if review.source.input_id == fallback_id
        )
        finding.add_input(fallback.source, review=True)
        finding.add_accounts(fallback.coa_ids)
        finding.add_refs(fallback.source_rows)
        dispositions[fallback_id] = (key, "superseded_by")

    # Attach the raw deterministic check to its structured exception whenever
    # the period, rule, target, and unrounded variance agree.
    for check in checks:
        matched_exception = _match_check_exception(check, exception_records)
        if matched_exception is not None:
            key = exception_builder[matched_exception.source.input_id]
            finding = builders[key]
            if check.rule in {"occupancy_above_capacity", "invalid_rooms_available"}:
                finding.explanation = _check_explanation(check, coa)
            finding.add_input(check.source)
            finding.rules.add(check.rule)
            comparison = _comparison_from_check(check)
            if _is_summary_department_check(check):
                # Review comparisons may use detail-minus-Summary; this check
                # always uses Summary-minus-detail and controls that wording.
                finding.periods[check.period_id] = comparison
            elif check.period_id in finding.periods:
                finding.periods[check.period_id] = _merge_comparison(
                    finding.periods[check.period_id], comparison
                )
            else:
                finding.periods[check.period_id] = comparison
            dispositions[check.source.input_id] = (key, "superseded_by")
            continue

        # Blocking review checks attach to the corresponding review rather than
        # becoming a second sentence about the same decision.
        expected_kind = (
            "ambiguity"
            if check.rule == "unresolved_ambiguity"
            else "scope_exception"
            if check.rule in {"scope_exception", "scope_exclusion"}
            else None
        )
        matching_review = next(
            (
                item
                for item in reviews
                if (not item.period_ids or check.period_id in item.period_ids)
                and (
                    item.review_item_id == check.review_item_id
                    if check.review_item_id
                    else item.kind == expected_kind and check.target in item.coa_ids
                )
            ),
            None,
        )
        if matching_review is not None:
            key, _status = dispositions[matching_review.source.input_id]
            finding = builders[key]
            finding.add_input(check.source)
            finding.rules.add(check.rule)
            if _is_summary_department_check(check):
                finding.periods[check.period_id] = _comparison_from_check(check)
            else:
                finding.periods.setdefault(
                    check.period_id,
                    _comparison_from_check(check),
                )
            dispositions[check.source.input_id] = (key, "superseded_by")
            continue

        category = _category_for_rule(check.rule, check.severity)
        check_accounts = _check_accounts(check, coa)
        key = "check:" + _stable_digest(
            "check_group",
            {
                "category": category,
                "severity": check.severity,
                "rule": check.rule,
                "target": check.target,
                "shape": _details_shape(check.details),
            },
        )
        finding = builder(
            key,
            category=category,
            severity=check.severity,
            action_required=check.severity == "error",
            primary=check_accounts[0] if check_accounts else None,
            explanation=_check_explanation(check, coa),
        )
        finding.add_input(check.source)
        finding.add_accounts(check_accounts)
        finding.add_refs(
            [check.target] if "!" in check.target else []
        )
        finding.add_refs(
            value
            for key_name, value in check.details.items()
            if key_name.endswith("row") or key_name.endswith("_row")
        )
        if check.rule.startswith("source_control_"):
            finding.add_refs(json.loads(str(check.details.get("component_rows") or "[]")))
            finding.add_refs(json.loads(str(check.details.get("excluded_rows") or "[]")))
        finding.rules.add(check.rule)
        finding.periods[check.period_id] = _comparison_from_check(check)
        dispositions[check.source.input_id] = (key, "rendered")

    # Execution failures always get a visible fallback.  A workbook writer may
    # route target-less findings to Run Notes, but it may not discard them.
    for source in execution_inputs:
        period_id = source.period_id
        period_label = labels.get(period_id, period_id) if period_id else None
        explanation = str(source.payload).strip() or "The submitted mapping could not be executed."
        key = "execution:" + _stable_digest(
            "execution_group",
            {"explanation": explanation},
        )
        finding = builder(
            key,
            category=VALIDATION_ERROR,
            severity="error",
            action_required=True,
            primary=None,
            explanation=explanation,
        )
        finding.add_input(source)
        if period_id and period_label:
            finding.periods[period_id] = PeriodComparison(
                period_id=period_id,
                period_label=str(period_label),
            )
        dispositions[source.input_id] = (key, "rendered")

    # Prefer factual, code-quantified source comparisons to model-authored
    # diagnoses. A separate mapping treatment survives independently of math.
    for finding in builders.values():
        source_reviews = [review for review in reviews if review.source.input_id in finding.review_input_ids]
        if (finding.category == SOURCE_PRESENTATION and source_reviews
                and all(review.kind == "source_discrepancy" for review in source_reviews)
                and any(item.variance is not None and not _below_reconciliation_tolerance(item)
                        for item in finding.periods.values())):
            treatments = list(dict.fromkeys(filter(None, (
                _review_treatment(review, rows) for review in source_reviews
            ))))
            finding.explanation = " ".join(treatments) or "The mapped amount differs from the cited source comparison."

    # Attach an unsplit-parent explanation to that parent's coverage note.
    # This is deliberately structural, not a similarity match over prose.
    children = children_by_parent(coa)
    kpi_ids = {"S12.rooms_available", "S12.rooms_sold", "S12.occupancy", "S12.adr", "S12.revpar"}
    for treatment in list(builders.values()):
        if treatment.key not in builders or treatment.category != MAPPING_TREATMENT:
            continue
        parent = treatment.primary_coa_id
        child_ids = children.get(parent, [])
        candidates = [finding for finding in builders.values()
                      if finding.key != treatment.key
                      and finding.category == COVERAGE_GAP
                      and finding.primary_coa_id == parent
                      and "source_detail_incomplete" in finding.rules]
        if (values_by_period and child_ids
                and not any(values.get(child) not in (None, 0)
                            for values in values_by_period.values() for child in child_ids)
                and len(candidates) == 1):
            candidates[0].consequences.append(_clean_message(
                treatment.explanation, coa, treatment.source_refs,
                source_ref_displays=source_ref_displays,
            ))
            _merge_builder(candidates[0], treatment, builders, dispositions)
        elif set(treatment.affected_coa_ids) <= kpi_ids and not treatment.source_refs:
            # Legacy KPI commentary often repeats ratios with the wrong units.
            # The typed KPI checks, when present, carry the actual facts.
            candidates = [finding for finding in builders.values()
                          if finding.rules & {"occupancy_above_capacity", "invalid_rooms_available"}
                          and finding.primary_coa_id in treatment.affected_coa_ids]
            if candidates:
                _merge_builder(candidates[0], treatment, builders, dispositions)

    # Collapse only deterministically proven downstream consequences.  A shared
    # amount without a known dependency path is intentionally insufficient.
    active = list(builders.values())
    removed_keys = set()
    for downstream in active:
        if downstream.key in removed_keys:
            continue
        candidates = [
            upstream
            for upstream in active
            if upstream.key != downstream.key
            and upstream.key not in removed_keys
            and _is_downstream_consequence(upstream, downstream, coa)
        ]
        if len(candidates) != 1:
            continue
        upstream = candidates[0]
        downstream_name = _account_name(downstream.primary_coa_id, coa)
        if downstream.primary_coa_id == upstream.primary_coa_id:
            consequence = "the same source difference also explains the child-to-parent coverage difference on this account."
        else:
            consequence = f"the same source difference also affects {downstream_name}."
        if consequence not in upstream.consequences:
            upstream.consequences.append(consequence)
        upstream.add_accounts(downstream.affected_coa_ids)
        upstream.add_refs(downstream.source_refs)
        for input_id in downstream.source_input_ids:
            upstream.source_input_ids.append(input_id)
            dispositions[input_id] = (upstream.key, "consequence_of")
        removed_keys.add(downstream.key)

    for key in removed_keys:
        builders.pop(key, None)

    # A defensive fallback means an input cannot disappear even when a new rule
    # reaches this composer before explicit category guidance is added.
    for source in all_inputs:
        if source.input_id in dispositions:
            continue
        key = f"unmatched:{source.input_id}"
        finding = builder(
            key,
            category=UNCLASSIFIED_REVIEW,
            severity="warning",
            action_required=True,
            primary=None,
            explanation=str(source.payload),
        )
        finding.add_input(source)
        dispositions[source.input_id] = (key, "rendered")

    # Suppress only numerically proven financial differences. Missing period
    # evidence, invalid comparisons, human decisions, and KPI issues stay visible.
    internal_keys = set()
    kpi_ids = {"S12.rooms_available", "S12.rooms_sold", "S12.occupancy", "S12.adr", "S12.revpar"}
    for key, finding in builders.items():
        source_reviews = [review for review in reviews if review.source.input_id in finding.source_input_ids]
        if (
            finding.category not in {SOURCE_PRESENTATION, RECONCILIATION_DIFFERENCE}
            or finding.severity == "error"
            or finding.rules - {"source_layer_conflict", "source_discrepancy", "small_source_reconciliation_difference", "source_control_difference"}
            or set(finding.affected_coa_ids) & kpi_ids
            or any(review.requires_human_decision or review.kind != "source_discrepancy" for review in source_reviews)
        ):
            continue
        comparisons = dict(finding.periods)
        for review in source_reviews:
            comparisons.update(review_comparisons[review.source.input_id])
        if (
            comparisons
            and all(_below_reconciliation_tolerance(item) for item in finding.periods.values())
            and (not source_reviews or all(
                set(review.period_ids or labels) <= comparisons.keys()
                for review in source_reviews
            ))
            and all(_below_reconciliation_tolerance(item) for item in comparisons.values())
        ):
            treatments = list(dict.fromkeys(filter(None, (
                _review_treatment(review, rows) for review in source_reviews
            ))))
            if treatments:
                finding.category = MAPPING_TREATMENT
                finding.severity = "info"
                finding.explanation = " ".join(treatments)
                # Retain the comparisons in the audit, but an informational
                # treatment must not acquire a rounding-warning sentence.
                continue
            internal_keys.add(key)
            finding.periods = comparisons
            for input_id in finding.source_input_ids:
                dispositions[input_id] = (key, "internal_only")

    # Presentation-only routing. Keep the original checks and comparisons in
    # the audit; a Summary/detail difference belongs beside the detail value.
    for finding in builders.values():
        if (finding.rules == {"source_control_unverified"}
                or (finding.rules == {"coverage_review_not_completed"}
                    and finding.severity == "info")):
            internal_keys.add(finding.key)
            for input_id in finding.source_input_ids:
                dispositions[input_id] = (finding.key, "internal_only")
        if (finding.category == MAPPING_TREATMENT and finding.severity == "info"
                and finding.affected_coa_ids
                and set(finding.affected_coa_ids) <= kpi_ids):
            internal_keys.add(finding.key)
            for input_id in finding.source_input_ids:
                dispositions[input_id] = (finding.key, "internal_only")
        summary = finding.primary_coa_id
        detail = SUMMARY_LINKS.get(summary)
        detail_accounts = [detail] if detail in coa else []
        terms = SUMMARY_EQUATIONS.get(summary, [])
        if not detail_accounts and terms and all(
            coefficient == 1 and SUMMARY_LINKS.get(source) in coa
            for coefficient, source in terms
        ):
            detail_accounts = [SUMMARY_LINKS[source] for _, source in terms]
        matching_checks = [check for check in checks
                           if check.source.input_id in finding.source_input_ids]
        is_summary_comparison = any(_is_summary_department_check(check)
                                    for check in matching_checks)
        if not is_summary_comparison and finding.category == SOURCE_PRESENTATION and values_by_period:
            # A typed comparison can describe the same Summary/detail difference
            # without a summary_department check attached. Prove both amounts;
            # never infer orientation from the author's prose or first COA ID.
            for summary_id, detail_id in SUMMARY_LINKS.items():
                if not {summary_id, detail_id} <= set(finding.affected_coa_ids):
                    continue
                measured = {period: item for period, item in finding.periods.items()
                            if item.selected_value is not None and item.comparison_value is not None}
                if not measured or not all(
                    ((
                        _same_number(item.selected_value, values_by_period.get(period, {}).get(summary_id))
                        and _same_number(item.comparison_value, values_by_period.get(period, {}).get(detail_id))
                    ) or (
                        _same_number(item.selected_value, values_by_period.get(period, {}).get(detail_id))
                        and _same_number(item.comparison_value, values_by_period.get(period, {}).get(summary_id))
                    )) for period, item in measured.items()
                ):
                    continue
                finding.periods.update({period: replace(
                    item, selected_value=values_by_period[period][summary_id],
                    comparison_value=values_by_period[period][detail_id],
                    variance=values_by_period[period][summary_id] - values_by_period[period][detail_id],
                ) for period, item in measured.items()})
                detail_accounts = [detail_id]
                is_summary_comparison = True
                break
        if detail_accounts and is_summary_comparison:
            finding.primary_coa_id = detail_accounts[0]
            finding.affected_coa_ids = detail_accounts
            section = _join_phrases([SECTION_LABELS.get(account.split(".")[0], "Related")
                                     for account in detail_accounts])
            if len(detail_accounts) > 1:
                section = "Combined " + section
            kind = "revenue" if "revenue" in detail_accounts[0] else "expenses"
            finding.department_comparison = f"{section} department {kind}"
            finding.consequences = []
        elif (detail in coa and "source_layer_conflict" in finding.rules
              and detail in finding.affected_coa_ids and values_by_period
              and finding.periods and all(
                  item.selected_value is not None and item.variance is not None
                  and _same_number(values_by_period.get(period, {}).get(detail), item.selected_value)
                  and _same_number(values_by_period.get(period, {}).get(summary), item.selected_value)
                  for period, item in finding.periods.items())):
            # The source comparison still matters, but it is not a difference
            # between the final mapped Summary and department values.
            alternate_sheets = sorted({ref.rsplit("!", 1)[0]
                for review in reviews if review.source.input_id in finding.review_input_ids
                for ref in review.alternate_source_rows if "!" in ref})
            if alternate_sheets:
                finding.primary_coa_id = detail
                finding.affected_coa_ids = [detail]
                finding.alternate_subtotal = ", ".join(alternate_sheets)

        if (finding.category == SOURCE_PRESENTATION and not finding.department_comparison
                and not finding.alternate_subtotal and finding.review_input_ids
                and any(item.variance is not None for item in finding.periods.values())):
            source_review = next((review for review in reviews
                if review.source.input_id in finding.review_input_ids
                and review.selected_source_rows and review.alternate_source_rows), None)
            if source_review:
                def subject(refs):
                    sheets = sorted({ref.rsplit("!", 1)[0] for ref in refs if "!" in ref})
                    name = (str(rows.get(refs[0], {}).get("label") or "Reported amount")
                            if len(refs) == 1 else _account_name(finding.primary_coa_id, coa))
                    return f"{name} on {_join_phrases(sheets)}"
                finding.comparison_subjects = (
                    subject(source_review.selected_source_rows),
                    subject(source_review.alternate_source_rows),
                )

    # Merge only proven identical Summary/detail comparisons, keeping all raw
    # inputs and references in the audit. A different value/period stays separate.
    comparisons = {}
    for finding in list(builders.values()):
        if not finding.department_comparison:
            continue
        identity = (finding.department_comparison, tuple(sorted(
            (p, round(item.selected_value, 4), round(item.comparison_value, 4))
            for p, item in finding.periods.items()
            if item.selected_value is not None and item.comparison_value is not None
        )))
        prior = comparisons.get(identity)
        if prior is None:
            comparisons[identity] = finding
        else:
            if finding.severity == "error":
                prior.severity = "error"
                prior.action_required = True
            _merge_builder(prior, finding, builders, dispositions)

    # Shorten only when the year uniquely identifies a selected period.
    # Actual/Budget and multiple months in one year must remain distinguishable.
    years = {key: re.findall(r"\b(?:19|20)\d{2}\b", label)
             for key, label in labels.items()}
    short_labels = {
        key: matches[0] if len(matches) == 1
        and sum(other == matches for other in years.values()) == 1 else labels[key]
        for key, matches in years.items()
    }

    final_findings = []
    key_to_id = {}
    for finding in sorted(
        builders.values(),
        key=lambda item: (
            0 if item.severity == "error" else 1 if item.severity == "warning" else 2,
            item.primary_coa_id or "~",
            item.category,
            item.key,
        ),
    ):
        finding_id = _builder_finding_id(finding)
        key_to_id[finding.key] = finding_id
        primary_coa_id = (
            finding.primary_coa_id if finding.primary_coa_id in coa else None
        )
        rendered_text = _render(replace(finding, periods={
            key: replace(value, period_label=short_labels.get(key, value.period_label))
            for key, value in finding.periods.items()
        }), coa, source_ref_displays)
        # When two distinct treatments apply to different periods on the same
        # account, color alone cannot tell the reader which treatment is which.
        if finding.review_input_ids and finding.periods and any(
            other.key != finding.key and other.category == finding.category
            and other.explanation != finding.explanation and other.periods
            and set(other.affected_coa_ids) & set(finding.affected_coa_ids)
            and not set(other.periods) & set(finding.periods)
            for other in builders.values()
        ):
            rendered_text = _join_phrases([short_labels.get(p, labels.get(p, p))
                                         for p in finding.periods]) + ": " + rendered_text
        final_findings.append(
            CanonicalFeedbackFinding(
                finding_id=finding_id,
                category=finding.category,
                severity=finding.severity,
                action_required=finding.action_required,
                destination=(
                    "internal_only" if finding.key in internal_keys
                    else f"coa:{primary_coa_id}" if primary_coa_id else "run_notes"
                ),
                primary_coa_id=primary_coa_id,
                affected_coa_ids=finding.affected_coa_ids,
                explanation=_clean_message(
                    finding.explanation,
                    coa,
                    finding.source_refs,
                    quantified=any(
                        item.variance is not None
                        for item in finding.periods.values()
                    ),
                    source_ref_displays=source_ref_displays,
                ),
                source_refs=finding.source_refs,
                periods=list(finding.periods.values()),
                consequences=finding.consequences,
                source_input_ids=list(dict.fromkeys(finding.source_input_ids)),
                rendered_text=rendered_text,
            )
        )

    input_dispositions = [
        FeedbackInputDisposition(
            input_id=source.input_id,
            input_type=source.input_type,
            finding_id=key_to_id[key],
            status=status,
        )
        for source in all_inputs
        for key, status in [dispositions[source.input_id]]
    ]
    unmatched = [
        source
        for source in all_inputs
        if source.input_id not in dispositions
    ]
    bundle = FeedbackBundle(
        findings=final_findings,
        inputs=input_dispositions,
        rendered_count=sum(item.destination != "internal_only" for item in final_findings),
        unmatched_count=len(unmatched),
    )
    assert_feedback_invariants(bundle)
    return bundle


def compose_result_feedback(result: Any) -> FeedbackBundle:
    """Compose a live `NormalizationResult` without importing pipeline types."""
    period_labels = dict(_field(result, "period_labels", {}) or {})
    if not period_labels:
        period_labels = {"selected": str(_field(result, "period_label", "Selected period"))}
    checks_by_period = dict(_field(result, "checks_by_period", {}) or {})
    if not checks_by_period:
        checks_by_period = {"selected": list(_field(result, "checks", []) or [])}
    return compose_feedback(
        checks_by_period=checks_by_period,
        review_items=_field(result, "review_items", []) or [],
        exceptions=_field(result, "exceptions", []) or [],
        execution_issues=_field(result, "execution_issues", []) or [],
        execution_issues_by_period=(
            _field(result, "execution_issues_by_period", {}) or {}
        ),
        period_labels=period_labels,
        coa=dict(_field(result, "coa", {}) or {}),
        evidence_rows=_field(result, "evidence", []) or [],
        values_by_period=_field(result, "period_values", {}) or {},
        source_ref_displays=evidence_display_map(
            [
                row
                for row in (_field(result, "evidence", []) or [])
                if _field(row, "row_key")
            ]
        ),
    )


def assert_feedback_invariants(bundle: FeedbackBundle) -> None:
    """Fail closed if feedback could be lost or rendered twice."""
    finding_ids = [item.finding_id for item in bundle.findings]
    input_ids = [item.input_id for item in bundle.inputs]
    if len(finding_ids) != len(set(finding_ids)):
        raise FeedbackCompositionError("canonical finding IDs are not unique")
    if len(input_ids) != len(set(input_ids)):
        raise FeedbackCompositionError("feedback input IDs are not unique")
    if bundle.rendered_count != sum(item.destination != "internal_only" for item in bundle.findings):
        raise FeedbackCompositionError(
            "rendered feedback count does not equal canonical finding count"
        )
    if bundle.unmatched_count:
        raise FeedbackCompositionError(
            f"{bundle.unmatched_count} feedback inputs were not accounted for"
        )

    findings = {item.finding_id: item for item in bundle.findings}
    dispositions = {item.input_id: item for item in bundle.inputs}
    for finding in bundle.findings:
        expected_destination = (
            f"coa:{finding.primary_coa_id}"
            if finding.primary_coa_id
            else "run_notes"
        )
        if finding.destination not in {expected_destination, "internal_only"}:
            raise FeedbackCompositionError(
                f"{finding.finding_id} does not have exactly one destination"
            )
        if not finding.rendered_text.strip():
            raise FeedbackCompositionError(
                f"{finding.finding_id} has no user-facing text"
            )
        for input_id in finding.source_input_ids:
            disposition = dispositions.get(input_id)
            if disposition is None or disposition.finding_id != finding.finding_id:
                raise FeedbackCompositionError(
                    f"{input_id} is not assigned to its rendered finding"
                )
    for disposition in bundle.inputs:
        finding = findings.get(disposition.finding_id)
        if finding is None or disposition.input_id not in finding.source_input_ids:
            raise FeedbackCompositionError(
                f"{disposition.input_id} points to a missing canonical finding"
            )
        if (finding.destination == "internal_only") != (disposition.status == "internal_only"):
            raise FeedbackCompositionError("internal feedback must have an internal-only disposition")


def load_canonical_coa() -> dict[str, dict]:
    """Load just enough canonical metadata for saved-run shadow composition."""

    return load_coa()


def compose_run_log_feedback(
    run_log: dict[str, Any],
    *,
    coa: dict[str, dict] | None = None,
) -> FeedbackBundle:
    """Compose feedback from an existing `run_log.json` with no model calls."""
    source = run_log.get("source") or {}
    labels = {
        str(item.get("period_id")): str(item.get("label"))
        for item in source.get("periods") or []
        if item.get("period_id")
    }
    if not labels:
        labels = {"selected": str(source.get("period") or "Selected period")}
    typed_by_period = run_log.get("findings_by_period")
    if not typed_by_period and run_log.get("findings"):
        typed_by_period = {}
        for finding in run_log["findings"]:
            period_id = str(_field(finding, "period_id", "selected") or "selected")
            typed_by_period.setdefault(period_id, []).append(finding)
    return compose_feedback(
        checks_by_period=typed_by_period
        or run_log.get("checks_by_period")
        or {"selected": run_log.get("checks") or []},
        review_items=run_log.get("review_items") or [],
        exceptions=run_log.get("exceptions") or [],
        execution_issues=run_log.get("execution_issues") or [],
        execution_issues_by_period=run_log.get("execution_issues_by_period") or {},
        period_labels=labels,
        coa=coa or load_canonical_coa(),
        evidence_rows=run_log.get("evidence_rows") or [],
        values_by_period=run_log.get("values_by_period") or {},
        source_ref_displays=evidence_display_map(
            [
                row
                for row in (run_log.get("evidence_rows") or [])
                if _field(row, "row_key")
            ]
        ),
    )
