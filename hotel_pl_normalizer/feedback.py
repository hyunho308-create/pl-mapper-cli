"""Canonical, deterministic feedback composition.

The mapper deliberately keeps two kinds of truth:

* model-authored review items explain source meaning and the chosen treatment;
* deterministic checks supply arithmetic, severity, periods, and blocking state.

Rendering either stream independently loses context or repeats the same root
cause.  This module joins them before prose is produced and enforces that every
input reaches exactly one workbook destination.
"""

from __future__ import annotations

import csv
import hashlib
import json
import re
from collections import deque
from dataclasses import asdict, dataclass, field
from importlib import resources
from typing import Any, Iterable, Literal

from hotel_pl_normalizer.mapping.mapper import (
    DERIVED_SUMMARY_LINKS,
    SUMMARY_EQUATIONS,
)

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

GENERIC_COVERAGE_EXPLANATIONS = {
    "Preserve supported source detail and flag the incomplete coverage.",
    "The source supports the parent total but does not identify all expected child detail.",
}

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

USER_FEEDBACK_CATEGORIES = (
    SOURCE_PRESENTATION,
    MAPPING_TREATMENT,
    COVERAGE_GAP,
    SCOPE_EXCLUSION,
    RECONCILIATION_DIFFERENCE,
)

SOURCE_RULES = {
    "source_discrepancy",
    "source_layer_conflict",
    "source_presentation_exception",
}
RECONCILIATION_RULES = {"small_source_reconciliation_difference"}
COVERAGE_RULES = {
    "source_detail_incomplete",
    "coverage_unspecified",
    "large_residual_plug",
    "unsupported_residual_remainder",
    "unresolved_negative_residual",
}
SCOPE_RULES = {"scope_exclusion"}


@dataclass(frozen=True, slots=True)
class PeriodComparison:
    period_id: str
    period_label: str
    selected_value: float | None = None
    comparison_value: float | None = None
    variance: float | None = None
    amount: float | None = None
    ratio: float | None = None


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
    details: dict[str, str]
    raw: str


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
            raw_values.append(str(check))
            raw_periods.append(period_id)
    inputs = _inputs("check", raw_values, periods=raw_periods)
    records = []
    for source in inputs:
        raw = str(source.payload)
        parts = [part for part in raw.split("|") if part != ""]
        severity = parts[0].strip().lower() if parts else "error"
        rule = parts[1].strip() if len(parts) > 1 else "execution"
        target = parts[2].strip() if len(parts) > 2 else "mapping"
        period_id = source.period_id or "selected"
        records.append(
            _Check(
                source=source,
                period_id=period_id,
                period_label=period_labels.get(period_id, period_id),
                severity=severity if severity in {"error", "warning"} else "error",
                rule=rule,
                target=target,
                details=_parse_details(parts[3:]),
                raw=raw,
            )
        )
    return records, inputs


def _parse_reviews(values: Iterable[Any]) -> tuple[list[_Review], list[_Input]]:
    materialized = list(values or [])
    inputs = _inputs("review_item", materialized)
    records = []
    for source in inputs:
        value = source.payload
        records.append(
            _Review(
                source=source,
                kind=str(_field(value, "kind", "") or ""),
                message=str(_field(value, "message", "") or "").strip(),
                coa_ids=list(_field(value, "coa_ids", []) or []),
                source_rows=list(_field(value, "source_rows", []) or []),
                selected_source_rows=list(
                    _field(value, "selected_source_rows", []) or []
                ),
                alternate_source_rows=list(
                    _field(value, "alternate_source_rows", []) or []
                ),
                requires_human_decision=bool(
                    _field(value, "requires_human_decision", False)
                ),
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
            )
        )
    return records, inputs


def _category_for_rule(rule: str, severity: str) -> str:
    if severity == "error":
        return VALIDATION_ERROR
    if rule in SOURCE_RULES:
        return SOURCE_PRESENTATION
    if rule in RECONCILIATION_RULES:
        return RECONCILIATION_DIFFERENCE
    if rule in COVERAGE_RULES:
        return COVERAGE_GAP
    if rule in SCOPE_RULES:
        return SCOPE_EXCLUSION
    return VALIDATION_WARNING


def _category_for_exception(rule: str) -> str:
    if rule in SOURCE_RULES:
        return SOURCE_PRESENTATION
    if rule in RECONCILIATION_RULES:
        return RECONCILIATION_DIFFERENCE
    if rule in COVERAGE_RULES:
        return COVERAGE_GAP
    if rule in SCOPE_RULES:
        return SCOPE_EXCLUSION
    return UNCLASSIFIED_REVIEW


def _expected_review_kind(rule: str) -> str | None:
    if rule == "scope_exclusion":
        return "scope_exception"
    if rule in SOURCE_RULES:
        return "source_discrepancy"
    return None


def _match_review(exception: _Exception, reviews: list[_Review]) -> _Review | None:
    expected = _expected_review_kind(exception.rule)
    if expected is None:
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
    )


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
        return "The source supports the parent total but does not identify all expected child detail."
    if category == SCOPE_EXCLUSION:
        return "A materially populated item was excluded from the Standard COA based on the workbook's supported scope."
    return rule.replace("_", " ").strip().capitalize() or "A finding requires review."


def _check_explanation(check: _Check, coa: dict[str, dict]) -> str:
    message = str(check.details.get("message") or "").strip()
    if message:
        return message
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
        output = output.replace(source_ref, f"{sheet} row {row}")
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


def _period_sentence(
    category: str,
    periods: list[PeriodComparison],
    *,
    rules: set[str] | None = None,
    explanation: str = "",
) -> str:
    rules = rules or set()
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
    if not quantified:
        if periods and category in {
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
            phrases = [
                (
                    f"{_rounded_number(item.variance)} above the independently reported department amount in {item.period_label}"
                    if item.variance > 0
                    else f"{_rounded_number(item.variance)} below the independently reported department amount in {item.period_label}"
                    if item.variance < 0
                    else f"equal to the independently reported department amount in {item.period_label}"
                )
                for item in quantified
            ]
            return f"The Summary amount is {_join_phrases(phrases)}."
        if "summary_math" in rules:
            phrases = [
                (
                    f"{_rounded_number(item.variance)} above the required equation in {item.period_label}"
                    if item.variance > 0
                    else f"{_rounded_number(item.variance)} below the required equation in {item.period_label}"
                    if item.variance < 0
                    else f"equal to the required equation in {item.period_label}"
                )
                for item in quantified
            ]
            return f"The reported Summary amount is {_join_phrases(phrases)}."
        if rules & {
            "hierarchy_complete",
            "hierarchy_partial_with_residual",
        }:
            phrases = [
                (
                    f"{_rounded_number(item.variance)} below the parent in {item.period_label}"
                    if item.variance > 0
                    else f"{_rounded_number(item.variance)} above the parent in {item.period_label}"
                    if item.variance < 0
                    else f"equal to the parent in {item.period_label}"
                )
                for item in quantified
            ]
            return f"Child accounts are {_join_phrases(phrases)}."
    if category == SOURCE_PRESENTATION:
        phrases = [
            (
                f"{_rounded_number(item.variance)} higher in {item.period_label}"
                if item.variance > 0
                else f"{_rounded_number(item.variance)} lower in {item.period_label}"
                if item.variance < 0
                else f"equal in {item.period_label}"
            )
            for item in quantified
        ]
        return (
            "Compared with the alternate source, the selected amount is "
            f"{_join_phrases(phrases)}."
        )
    if category == COVERAGE_GAP:
        phrases = [
            (
                f"{_rounded_number(item.variance)} below the parent in {item.period_label}"
                if item.variance > 0
                else f"{_rounded_number(item.variance)} above the parent in {item.period_label}"
                if item.variance < 0
                else f"equal to the parent in {item.period_label}"
            )
            for item in quantified
        ]
        return f"Identified children are {_join_phrases(phrases)}."
    if category == RECONCILIATION_DIFFERENCE:
        phrases = [
            (
                f"{_rounded_number(item.variance)} higher in {item.period_label}"
                if item.variance > 0
                else f"{_rounded_number(item.variance)} lower in {item.period_label}"
                if item.variance < 0
                else f"equal in {item.period_label}"
            )
            for item in quantified
        ]
        retained = (
            ""
            if "retain" in explanation.casefold()
            else "; the reported values were retained"
        )
        return f"The reported total is {_join_phrases(phrases)}{retained}."
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


def _render(builder: _FindingBuilder, coa: dict[str, dict]) -> str:
    quantified = any(item.variance is not None for item in builder.periods.values())
    explanation = _clean_message(
        builder.explanation,
        coa,
        builder.source_refs,
        quantified=quantified,
    )
    prefix = "Needs review" if builder.severity == "error" else builder.category
    first = f"{prefix}: {explanation}" if explanation else f"{prefix}."
    sentences = [first]
    period_sentence = _period_sentence(
        builder.category,
        list(builder.periods.values()),
        rules=builder.rules,
        explanation=explanation,
    )
    if (
        builder.category == COVERAGE_GAP
        and period_sentence
        and explanation in GENERIC_COVERAGE_EXPLANATIONS
    ):
        return f"{prefix}: {period_sentence}"
    if (
        period_sentence
        and builder.rules & {"summary_department", "hierarchy_complete"}
        and explanation
        in {
            "The independently reported Summary and department amounts do not reconcile.",
            "The reported parent and its mapped child accounts do not reconcile.",
        }
    ):
        return f"{prefix}: {period_sentence}"
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


def _primary_for_review(review: _Review, coa: dict[str, dict]) -> str | None:
    candidates = [coa_id for coa_id in review.coa_ids if coa_id in coa]
    if not candidates:
        return None
    if review.kind == "unusual_convention":
        summary = [item for item in candidates if item.startswith("S12.")]
        if summary and "summary" in review.message.casefold():
            return summary[0]
        non_summary = [item for item in candidates if not item.startswith("S12.")]
        if non_summary:
            return max(
                non_summary,
                key=lambda item: _coa_depth(item, coa),
            )
    return candidates[0]


def _coa_depth(coa_id: str, coa: dict[str, dict]) -> int:
    depth = 0
    seen = {coa_id}
    parent = coa.get(coa_id, {}).get("parent_coa_id")
    while parent and parent in coa and parent not in seen:
        seen.add(parent)
        depth += 1
        parent = coa[parent].get("parent_coa_id")
    return depth


def _details_shape(details: dict[str, str]) -> tuple[tuple[str, str], ...]:
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
    tolerance = max(0.01, abs(left) * 0.00001, abs(right) * 0.00001)
    return abs(left - right) <= tolerance


def _match_check_exception(
    check: _Check,
    exceptions: list[_Exception],
) -> _Exception | None:
    matches = []
    for exception in exceptions:
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


def _dependency_edges(coa: dict[str, dict]) -> dict[str, list[tuple[str, int]]]:
    edges: dict[str, list[tuple[str, int]]] = {}
    for coa_id, metadata in coa.items():
        parent = str(metadata.get("parent_coa_id") or "")
        if parent:
            edges.setdefault(coa_id, []).append((parent, 1))
    for summary, detail in DERIVED_SUMMARY_LINKS.items():
        edges.setdefault(detail, []).append((summary, 1))
    for target, terms in SUMMARY_EQUATIONS.items():
        for sign, source in terms:
            edges.setdefault(source, []).append((target, int(sign)))
    return edges


def _dependency_coefficient(
    source: str | None,
    target: str | None,
    coa: dict[str, dict],
) -> int | None:
    if not source or not target:
        return None
    if source == target:
        return 1
    queue = deque([(source, 1, 0)])
    seen = {(source, 1)}
    edges = _dependency_edges(coa)
    while queue:
        current, coefficient, depth = queue.popleft()
        if depth >= 10:
            continue
        for next_target, sign in edges.get(current, []):
            next_coefficient = coefficient * sign
            if next_target == target:
                return next_coefficient
            state = (next_target, next_coefficient)
            if state not in seen:
                seen.add(state)
                queue.append((next_target, next_coefficient, depth + 1))
    return None


def _is_downstream_consequence(
    upstream: _FindingBuilder,
    downstream: _FindingBuilder,
    coa: dict[str, dict],
) -> bool:
    if upstream.category != SOURCE_PRESENTATION or not upstream.review_input_ids:
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
        if source is None or source.variance is None or comparison.variance is None:
            return False
        if not _same_number(source.variance * coefficient, comparison.variance):
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


def compose_feedback(
    *,
    checks_by_period: dict[str, list[Any]] | None,
    review_items: Iterable[Any] | None,
    exceptions: Iterable[Any] | None,
    execution_issues: Iterable[str] | None,
    execution_issues_by_period: dict[str, list[str]] | None,
    period_labels: dict[str, str] | None,
    coa: dict[str, dict],
) -> FeedbackBundle:
    """Join every final feedback input into one non-lossy finding bundle."""
    labels = dict(period_labels or {})
    checks, check_inputs = _parse_checks(checks_by_period or {}, labels)
    reviews, review_inputs = _parse_reviews(review_items or [])
    exception_records, exception_inputs = _parse_exceptions(exceptions or [])
    derived_summary_supersessions = _derived_summary_supersessions(reviews)

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
                _primary_for_review(matched_review, coa)
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

    # Every model review survives, even if it did not produce a structured
    # exception.  This is the non-loss fallback missing from the old renderer.
    for review in reviews:
        if review.source.input_id in dispositions:
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
            primary=_primary_for_review(review, coa),
            explanation=review.message or "A model review item requires attention.",
        )
        finding.add_input(review.source, review=True)
        finding.add_accounts(review.coa_ids)
        finding.add_refs(review.source_rows)
        dispositions[review.source.input_id] = (key, "rendered")

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
            finding.add_input(check.source)
            finding.rules.add(check.rule)
            comparison = _comparison_from_check(check)
            if check.period_id in finding.periods:
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
                if item.kind == expected_kind and check.target in item.coa_ids
            ),
            None,
        )
        if matching_review is not None:
            key, _status = dispositions[matching_review.source.input_id]
            finding = builders[key]
            finding.add_input(check.source)
            finding.rules.add(check.rule)
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
        final_findings.append(
            CanonicalFeedbackFinding(
                finding_id=finding_id,
                category=finding.category,
                severity=finding.severity,
                action_required=finding.action_required,
                destination=(
                    f"coa:{primary_coa_id}"
                    if primary_coa_id
                    else "run_notes"
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
                ),
                source_refs=finding.source_refs,
                periods=list(finding.periods.values()),
                consequences=finding.consequences,
                source_input_ids=list(dict.fromkeys(finding.source_input_ids)),
                rendered_text=_render(finding, coa),
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
        rendered_count=len(final_findings),
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
    )


def assert_feedback_invariants(bundle: FeedbackBundle) -> None:
    """Fail closed if feedback could be lost or rendered twice."""
    finding_ids = [item.finding_id for item in bundle.findings]
    input_ids = [item.input_id for item in bundle.inputs]
    if len(finding_ids) != len(set(finding_ids)):
        raise FeedbackCompositionError("canonical finding IDs are not unique")
    if len(input_ids) != len(set(input_ids)):
        raise FeedbackCompositionError("feedback input IDs are not unique")
    if bundle.rendered_count != len(bundle.findings):
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
        if finding.destination != expected_destination:
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


def load_canonical_coa() -> dict[str, dict]:
    """Load just enough canonical metadata for saved-run shadow composition."""
    source = resources.files("hotel_pl_normalizer.data").joinpath("coa_v2.csv")
    with source.open("r", encoding="utf-8-sig", newline="") as handle:
        return {row["coa_id"]: row for row in csv.DictReader(handle)}


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
    return compose_feedback(
        checks_by_period=run_log.get("checks_by_period") or {
            "selected": run_log.get("checks") or []
        },
        review_items=run_log.get("review_items") or [],
        exceptions=run_log.get("exceptions") or [],
        execution_issues=run_log.get("execution_issues") or [],
        execution_issues_by_period=run_log.get("execution_issues_by_period") or {},
        period_labels=labels,
        coa=coa or load_canonical_coa(),
    )
