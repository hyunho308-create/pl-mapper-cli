"""One mapping-check runner shared by repair-session and final validation."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from hotel_pl_normalizer.mapping.findings import Finding
from hotel_pl_normalizer.mapping.rules import assert_registered_rules
from hotel_pl_normalizer.mapping.source_controls import (
    check_source_controls,
    control_reference_issues,
)
from hotel_pl_normalizer.models.evidence import EvidenceRow, ensure_evidence_rows

CheckStage = Literal["session", "final"]


@dataclass(slots=True)
class CheckRun:
    values_by_period: dict[str, dict[str, float | None]]
    execution_issues: list[str]
    execution_issues_by_period: dict[str, list[str]]
    global_findings: list[Finding]
    findings_by_period: dict[str, list[Finding]]
    residual_plugs_by_period: dict[str, dict[str, float]]
    missing_decision_count: int

    def final_findings_by_period(self) -> dict[str, list[Finding]]:
        """Place workbook-wide findings once on the primary period."""

        output = {
            period_id: list(findings)
            for period_id, findings in self.findings_by_period.items()
        }
        primary = next(iter(output))
        output[primary].extend(self.global_findings)
        return output


def run_checks(
    *,
    plan,
    evidence: list[EvidenceRow | dict],
    coa: dict[str, dict],
    period_labels: dict[str, str],
    expected_workbook_id: str,
    stage: CheckStage,
    preserve_blanks: bool,
    history: list[dict[str, Any]] | None = None,
    sheet_routing_context: list[dict] | None = None,
    summary_only_pushdown_rows: set[str] | None = None,
) -> CheckRun:
    """Execute and validate a plan once under a documented lifecycle stage.

    All accounting, structural, scope, coverage, and review checks are common.
    The only stage difference is residual presentation: a repair session accepts
    only small automatic plugs, while final presentation fills positive
    remainders and reports material or unresolved-negative residuals.
    """

    # Lazy import avoids a module cycle while the data models still live in
    # mapper.py during this bounded extraction.
    from hotel_pl_normalizer.mapping import mapper as core

    plan = core._assign_review_item_ids(plan)
    evidence = ensure_evidence_rows(evidence)
    execution_issues: list[str] = []
    if plan.workbook_id != expected_workbook_id:
        execution_issues.append(
            f"workbook_id must be {expected_workbook_id!r}, got {plan.workbook_id!r}"
        )
    submitted = {item.coa_id for item in plan.decisions}
    required_decisions = set(coa) - core.DETERMINISTIC_SUMMARY_ACCOUNTS
    deterministic_submissions = sorted(
        submitted & core.DETERMINISTIC_SUMMARY_ACCOUNTS
    )
    if deterministic_submissions:
        execution_issues.append(
            "deterministic accounts must not be submitted by the model: "
            + ", ".join(deterministic_submissions)
        )
    missing = sorted(required_decisions - submitted)
    if missing:
        execution_issues.append("missing COA decisions: " + ", ".join(missing))

    rollup_targets = set(core.DERIVED_SUMMARY_LINKS) | set(core.SUMMARY_EQUATIONS)
    for decision in plan.decisions:
        if decision.operation != core.SourceOperation.COA_ROLLUP:
            continue
        if plan.strategy.summary_mode != core.SummaryMode.DERIVED:
            execution_issues.append(
                f"{decision.coa_id}: coa_rollup requires summary_mode=derived"
            )
        elif decision.coa_id not in rollup_targets:
            execution_issues.append(
                f"{decision.coa_id}: coa_rollup is only allowed for a linked "
                "S12 account or Summary equation"
            )

    unknown_review_ids = sorted(
        {
            coa_id
            for item in plan.review_items
            for coa_id in item.coa_ids
            if coa_id not in coa
        }
    )
    if unknown_review_ids:
        execution_issues.append(
            "review items cite unknown COA ids: " + ", ".join(unknown_review_ids)
        )
    unknown_review_periods = sorted({
        period for item in plan.review_items for period in item.period_ids
        if period not in period_labels
    })
    if unknown_review_periods:
        execution_issues.append(
            "review items cite unknown period ids: " + ", ".join(unknown_review_periods)
        )
    evidence_rows = {item["row_key"] for item in evidence}
    execution_issues.extend(control_reference_issues(plan.source_controls, evidence_rows, coa))
    unknown_review_rows = sorted(
        {
            row_key
            for item in plan.review_items
            for row_key in item.source_rows
            if row_key not in evidence_rows
        }
    )
    if unknown_review_rows:
        execution_issues.append(
            "review items cite unknown source rows: "
            + ", ".join(unknown_review_rows)
        )

    global_findings = [
        *core._review_item_blockers([item for item in plan.review_items if not item.period_ids]),
        *core._non_residual_plug_issues(plan, coa),
        *core._period_completeness_issues(plan, evidence, period_labels),
        *core._unused_financial_schedule_issues(
            plan, evidence, sheet_routing_context or []
        ),
        *core._review_item_warnings([item for item in plan.review_items if not item.period_ids]),
    ]
    collapse_issue = core._detail_collapse_issue(plan, evidence)
    if collapse_issue:
        global_findings.append(collapse_issue)
    missing_venue_names = sorted(
        decision.coa_id
        for decision in plan.decisions
        if decision.coa_id in core.GENERIC_VENUE_IDS
        and decision.operation != core.SourceOperation.NO_VALUE
        and not str(decision.venue_name or "").strip()
    )
    if missing_venue_names:
        execution_issues.append(
            "mapped generic venues require venue_name: "
            + ", ".join(missing_venue_names)
        )

    values_by_period: dict[str, dict[str, float | None]] = {}
    execution_issues_by_period: dict[str, list[str]] = {}
    findings_by_period: dict[str, list[Finding]] = {}
    residual_plugs_by_period: dict[str, dict[str, float]] = {}
    for period_id, period_label in period_labels.items():
        period_reviews = [
            item for item in plan.review_items
            if not item.period_ids or period_id in item.period_ids
        ]
        period_plan = plan.model_copy(update={"review_items": period_reviews})
        values, calculation_issues = core._execute(
            plan.decisions,
            evidence,
            coa,
            period_id=period_id,
            preserve_blanks=preserve_blanks,
        )
        if stage == "session":
            plugs = core._apply_residual_plugs(
                values,
                coa,
                plan.decisions,
                max_ratio=core.RESIDUAL_AUTO_ACCEPT_RATIO,
            )
            unresolved_negative = {}
        else:
            plugs = core._apply_residual_plugs(
                values,
                coa,
                plan.decisions,
                max_ratio=None,
                allow_large_negative=False,
            )
            unresolved_negative = core._unresolved_negative_residuals(
                values, coa, plan.decisions
            )
        checks = [
            item.with_updates(period_id=period_id)
            for item in core._validate(
                core._validation_values(values),
                coa,
                plan.decisions,
                plan.strategy,
                period_reviews,
                summary_only_pushdown_rows or set(),
            )
        ]
        scoped_reviews = [item for item in period_reviews if item.period_ids]
        checks.extend(core._review_item_blockers(scoped_reviews))
        checks.extend(core._review_item_warnings(scoped_reviews))
        checks.extend(
            core._source_layer_conflict_warnings(
                period_plan, evidence, values, period_id
            )
        )
        checks.extend(
            core._source_layer_comparison_issues(
                period_plan, evidence, values, period_id
            )
        )
        checks = core._qualify_source_discrepancies(
            checks,
            period_plan,
            evidence,
            coa,
            history or [],
            period_label,
            calculation_issues,
        )
        if stage == "final":
            checks.extend(check_source_controls(plan.source_controls, evidence, period_id))
            checks = core._replace_unresolved_negative_errors(
                checks, unresolved_negative
            )
            checks.extend(
                core._large_residual_plug_warnings(values, coa, plugs)
            )
        checks.extend(
            core._unsupported_residual_remainder_warnings(
                values, coa, plan.decisions
            )
        )
        values_by_period[period_id] = values
        execution_issues_by_period[period_id] = list(calculation_issues)
        findings_by_period[period_id] = [
            item
            if item.period_id == period_id
            else item.with_updates(period_id=period_id)
            for item in checks
        ]
        residual_plugs_by_period[period_id] = plugs

    typed_global = list(global_findings)
    assert_registered_rules(
        finding.rule
        for finding in [
            *typed_global,
            *(item for findings in findings_by_period.values() for item in findings),
        ]
    )
    return CheckRun(
        values_by_period=values_by_period,
        execution_issues=execution_issues,
        execution_issues_by_period=execution_issues_by_period,
        global_findings=typed_global,
        findings_by_period=findings_by_period,
        residual_plugs_by_period=residual_plugs_by_period,
        missing_decision_count=len(missing),
    )
