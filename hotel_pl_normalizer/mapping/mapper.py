"""Map a complete workbook in one model-and-validator session.

``map_workbook`` is the production boundary. Models choose cited source rows;
Python reads their values, performs the arithmetic, and validates the result.
"""

from __future__ import annotations

import csv
import hashlib
import json
import re
from dataclasses import dataclass
from enum import Enum
from functools import lru_cache
from importlib import resources
from itertools import combinations
from typing import Any, Literal

from pydantic import Field, model_validator

from hotel_pl_normalizer.models.common import StrictModel
from hotel_pl_normalizer.providers.base import (
    ModelToolError,
    ProviderResponseTruncated,
    ProviderRunCancelled,
    ProviderToolLoopError,
)


class SourceOperation(str, Enum):
    DIRECT = "direct"
    SUM = "sum"
    ADJUSTED_SUBTOTAL = "adjusted_subtotal"
    NEGATE = "negate"
    RATIO = "ratio"
    PRODUCT = "product"
    SCALE = "scale"
    COA_ROLLUP = "coa_rollup"
    NO_VALUE = "no_value"


class ChildCoverage(str, Enum):
    COMPLETE = "complete"
    PARTIAL = "partial"
    NOT_PRESENT = "not_present"
    NOT_APPLICABLE = "not_applicable"


class OodMiscSummaryMode(str, Enum):
    SEPARATE = "separate"
    COMBINED_IN_OOD = "combined_in_ood"
    COMBINED_IN_MISC = "combined_in_misc"
    UNKNOWN = "unknown"


class SummaryMode(str, Enum):
    REPORTED = "reported"
    DERIVED = "derived"


class MappingOutcome(str, Enum):
    """The operator-facing disposition of a completed mapping attempt."""

    CLEAN = "clean"
    SOURCE_EXCEPTION = "source_exception"
    COVERAGE_GAP = "coverage_gap"
    SCOPE_EXCEPTION = "scope_exception"
    REJECTED = "rejected"


class StructuralScenario(str, Enum):
    EXTRA_DEPARTMENT = "extra_department"
    SUMMARY_ONLY_DEPARTMENT = "summary_only_department"
    DEPARTMENT_OFFSET = "department_offset"


class StructuralScenarioEvidence(StrictModel):
    scenario: StructuralScenario
    reason: str
    source_rows: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_evidence(self):
        if not self.reason.strip():
            raise ValueError("structural scenario reason cannot be blank")
        if not self.source_rows:
            raise ValueError("structural scenario must cite at least one source row")
        return self


GENERIC_VENUE_SLOTS = tuple(
    (
        f"S2.venue{index}_food_revenue",
        f"S2.venue{index}_beverage_revenue",
    )
    for index in range(1, 11)
)
GENERIC_VENUE_IDS = tuple(
    coa_id for slot in GENERIC_VENUE_SLOTS for coa_id in slot
)
RESIDUAL_AUTO_ACCEPT_RATIO = 0.05
UNSUPPORTED_REMAINDER_ABSOLUTE_THRESHOLD = 10_000.0
DETERMINISTIC_SUMMARY_CALCULATIONS = {
    "S12.ffe_reserve": {
        "formula": "0.04 * S12.total_revenue",
        "mapped_label": "Calculated: 4% of Total Revenue.",
        "dependencies": ("S12.total_revenue",),
    },
    "S12.noi": {
        "formula": "S12.ebitda - S12.ffe_reserve",
        "mapped_label": "Calculated: EBITDA less FF&E Reserve.",
        "dependencies": ("S12.ebitda", "S12.ffe_reserve"),
    },
}
DETERMINISTIC_SUMMARY_ACCOUNTS = frozenset(
    DETERMINISTIC_SUMMARY_CALCULATIONS
)


class SourceLayerOperation(str, Enum):
    DIRECT = "direct"
    SUM = "sum"
    ADJUSTED_SUBTOTAL = "adjusted_subtotal"
    NEGATE = "negate"


class AccountSourceDecision(StrictModel):
    coa_id: str
    operation: SourceOperation
    source_rows: list[str] = Field(default_factory=list)
    excluded_rows: list[str] = Field(default_factory=list)
    venue_name: str | None = None
    scale_factor: float | None = None
    child_coverage: ChildCoverage = ChildCoverage.NOT_APPLICABLE
    rationale: str | None = None

    @model_validator(mode="after")
    def validate_shape(self):
        if self.operation in {
            SourceOperation.NO_VALUE,
            SourceOperation.COA_ROLLUP,
        }:
            if self.source_rows or self.excluded_rows:
                raise ValueError(f"{self.operation.value} cannot cite source rows")
        elif not self.source_rows:
            raise ValueError("non-zero operations require source rows")
        if self.operation == SourceOperation.ADJUSTED_SUBTOTAL and not self.excluded_rows:
            raise ValueError(
                "adjusted_subtotal calculates sum(source_rows) - "
                "sum(excluded_rows) and requires at least one excluded row"
            )
        if self.operation == SourceOperation.SCALE and self.scale_factor is None:
            raise ValueError("scale requires scale_factor")
        return self


class WorkbookStrategy(StrictModel):
    reporting_layout: str
    summary_source: str
    department_source_strategy: list[str] = Field(default_factory=list)
    duplicate_or_supporting_schedules: list[str] = Field(default_factory=list)
    operator_to_coa_hierarchy_conflicts: list[str] = Field(default_factory=list)
    non_contiguous_sources: list[str] = Field(default_factory=list)
    source_sign_conventions: list[str] = Field(default_factory=list)
    calculated_parent_accounts: list[str] = Field(default_factory=list)
    source_detail_incomplete: list[str] = Field(default_factory=list)
    summary_mode: SummaryMode = SummaryMode.REPORTED
    derived_summary_source_rows: list[str] = Field(default_factory=list)
    structural_scenarios: list[StructuralScenarioEvidence] = Field(
        default_factory=list
    )
    ood_misc_summary_mode: OodMiscSummaryMode = OodMiscSummaryMode.UNKNOWN
    planned_validation_checks: list[str] = Field(default_factory=list)


class MappingReviewItem(StrictModel):
    kind: Literal[
        "ambiguity",
        "unusual_convention",
        "source_discrepancy",
        "scope_exception",
    ]
    message: str
    coa_ids: list[str] = Field(default_factory=list)
    source_rows: list[str] = Field(default_factory=list)
    selected_source_rows: list[str] = Field(default_factory=list)
    alternate_source_rows: list[str] = Field(default_factory=list)
    selected_excluded_rows: list[str] = Field(default_factory=list)
    alternate_excluded_rows: list[str] = Field(default_factory=list)
    selected_source_operation: SourceLayerOperation | None = None
    alternate_source_operation: SourceLayerOperation | None = None
    requires_human_decision: bool = False

    @model_validator(mode="after")
    def validate_context(self):
        if not self.message.strip():
            raise ValueError("review item message cannot be blank")
        if not self.coa_ids and not self.source_rows:
            raise ValueError("review item must cite a COA id or source row")
        if bool(self.selected_source_rows) != bool(self.alternate_source_rows):
            raise ValueError(
                "selected_source_rows and alternate_source_rows must be supplied together"
            )
        if self.selected_source_rows or self.alternate_source_rows:
            if self.kind != "source_discrepancy":
                raise ValueError(
                    "source-layer row sets are only valid for source_discrepancy"
                )
            if not self.selected_source_operation or not self.alternate_source_operation:
                raise ValueError(
                    "typed source-layer comparisons require selected and alternate operations"
                )
            cited = set(self.source_rows)
            selected = set(self.selected_source_rows) | set(
                self.selected_excluded_rows
            )
            alternate = set(self.alternate_source_rows) | set(
                self.alternate_excluded_rows
            )
            structured = selected | alternate
            if not structured <= cited:
                raise ValueError(
                    "source-layer row sets must also appear in source_rows"
                )
            if selected & alternate:
                raise ValueError("selected and alternate source layers must be disjoint")
            for side, operation, rows, excluded in (
                (
                    "selected",
                    self.selected_source_operation,
                    self.selected_source_rows,
                    self.selected_excluded_rows,
                ),
                (
                    "alternate",
                    self.alternate_source_operation,
                    self.alternate_source_rows,
                    self.alternate_excluded_rows,
                ),
            ):
                if operation == SourceLayerOperation.DIRECT and (
                    len(rows) != 1 or excluded
                ):
                    raise ValueError(f"{side} direct layer requires exactly one row")
                if operation == SourceLayerOperation.ADJUSTED_SUBTOTAL and not excluded:
                    raise ValueError(
                        f"{side} adjusted_subtotal layer requires excluded rows"
                    )
                if operation != SourceLayerOperation.ADJUSTED_SUBTOTAL and excluded:
                    raise ValueError(
                        f"{side} excluded rows require adjusted_subtotal"
                    )
        elif any(
            (
                self.selected_excluded_rows,
                self.alternate_excluded_rows,
                self.selected_source_operation,
                self.alternate_source_operation,
            )
        ):
            raise ValueError("source-layer operations require both source-row sets")
        if self.requires_human_decision and self.kind != "scope_exception":
            raise ValueError(
                "requires_human_decision is only valid for a scope_exception"
            )
        return self


class WorkbookSourcePlan(StrictModel):
    plan_id: str
    workbook_id: str
    strategy: WorkbookStrategy
    decisions: list[AccountSourceDecision]
    review_items: list[MappingReviewItem] = Field(default_factory=list)


class WorkbookSourcePatch(StrictModel):
    patch_id: str
    workbook_id: str
    replacements: list[AccountSourceDecision]
    repair_hypothesis: str
    expected_fix: str
    summary_mode: SummaryMode | None = None
    derived_summary_source_rows: list[str] | None = None
    structural_scenarios: list[StructuralScenarioEvidence] | None = None
    ood_misc_summary_mode: OodMiscSummaryMode | None = None
    department_source_strategy: list[str] | None = None
    duplicate_or_supporting_schedules: list[str] | None = None
    operator_to_coa_hierarchy_conflicts: list[str] | None = None
    source_detail_incomplete: list[str] | None = None
    review_items: list[MappingReviewItem] | None = None

    @model_validator(mode="after")
    def validate_repair_tracking(self):
        if not self.repair_hypothesis.strip():
            raise ValueError("repair_hypothesis cannot be blank")
        if not self.expected_fix.strip():
            raise ValueError("expected_fix cannot be blank")
        return self


class WorkbookMappingCompletion(StrictModel):
    workbook_id: str
    status: Literal["accepted", "rejected"]
    outcome: MappingOutcome
    validation_attempt: int


@dataclass(slots=True)
class MappingResult:
    """Authoritative result of one complete-workbook mapping session."""

    coa: dict[str, dict]
    values: dict[str, float | None]
    values_by_period: dict[str, dict[str, float | None]]
    decisions: list[AccountSourceDecision]
    checks: list[dict]
    checks_by_period: dict[str, list[str]]
    residual_plugs_by_period: dict[str, dict[str, float]]
    execution_issues: list[str]
    execution_issues_by_period: dict[str, list[str]]
    review_items: list[MappingReviewItem]
    accepted: bool
    outcome: MappingOutcome
    exceptions: list[dict[str, Any]]
    stopped_reason: str | None
    session_calls: int
    session_call_ms: list[int]
    session_tool_calls: int
    session_exhausted: bool
    model_calls: list[dict]
    tool_trace: list[dict]
    mapping_selection: dict[str, Any]


class WorkbookMappingValidator:
    """Deterministic validation tool used inside one stateful model session."""

    cacheable = False

    def __init__(
        self,
        workbook_id,
        evidence,
        coa,
        period_labels=None,
        sheet_routing_context=None,
    ):
        self.workbook_id = workbook_id
        self.evidence = evidence
        self.coa = coa
        self.period_labels = period_labels or {"selected": "Selected period"}
        self.sheet_routing_context = list(sheet_routing_context or [])
        self.preserve_blanks = len(self.period_labels) > 1
        self.history: list[dict[str, Any]] = []
        self.submissions: list[dict[str, Any]] = []
        self.actions: list[dict[str, Any]] = []
        self._previous_decisions: dict[str, str] = {}
        self.current_plan: WorkbookSourcePlan | None = None
        self.last_accepted_digest: str | None = None
        self.last_accepted_plan: WorkbookSourcePlan | None = None
        self.detail_enrichment_completed = False
        self.best_plan: WorkbookSourcePlan | None = None
        self.best_result: dict[str, Any] | None = None
        self.best_score: tuple[int, ...] | None = None
        self.best_validation_attempt: int | None = None
        self.checkpoints: list[dict[str, Any]] = []
        self.derived_summary_activated = False
        self.derived_summary_source_rows: list[str] = []
        self.structural_scenarios: dict[StructuralScenario, list[str]] = {}
        self.summary_only_pushdown_rows: set[str] = set()
        self.warning_cleanup_pending = False
        self.warning_cleanup_attempted = False
        self.warning_cleanup_outcome: str | None = None
        self.warning_cleanup_checkpoint_plan: WorkbookSourcePlan | None = None
        self.warning_cleanup_checkpoint_result: dict[str, Any] | None = None
        self.warning_cleanup_checkpoint_score: tuple[int, ...] | None = None
        self.warning_cleanup_checkpoint_attempt: int | None = None
        self.stop_repair = False
        self.stopped_reason: str | None = None
        self.stopped_validation_attempt: int | None = None
        self.repeated_findings: list[dict[str, Any]] = []

    def dispatch(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if name == "validate_mapping":
            if self.current_plan is not None:
                raise ModelToolError(
                    "validate_mapping is only for the initial complete plan. "
                    "Use patch_mapping for repairs."
                )
            arguments = _prevalidate_plan_arguments(arguments)
            try:
                plan = WorkbookSourcePlan.model_validate(arguments)
            except ValueError as exc:
                raise ModelToolError(f"Draft mapping is not valid: {exc}") from exc
            action = {"tool": name, "submitted_decision_count": len(plan.decisions)}
        elif name == "patch_mapping":
            plan, action = self._apply_patch(arguments)
        else:
            raise ModelToolError(f"Unknown tool {name!r}.")
        self._apply_conditional_strategy(plan)
        plan = _enrich_derived_summary_review_item(
            plan, self.derived_summary_source_rows
        )
        plan = _enrich_adjustment_review_items(plan, self.coa)
        result = self.evaluate(plan)
        result["outcome"] = _outcome_from_result(
            result, plan.review_items
        ).value
        current = {
            item.coa_id: _substantive_decision_digest(item)
            for item in plan.decisions
        }
        changed = sorted(
            coa_id
            for coa_id, decision in current.items()
            if self._previous_decisions.get(coa_id) != decision
        )
        self._previous_decisions = current
        if self.history and changed:
            result["changed_coa_ids_since_prior_validation"] = changed
        result["validation_attempt"] = len(self.history) + 1
        result.update(action)
        previous = self.history[-1] if self.history else None
        if (
            name == "patch_mapping"
            and previous
            and previous.get("accepted")
            and _finding_targets(previous, "source_detail_incomplete")
        ):
            self.detail_enrichment_completed = True
        result["detail_enrichment_completed"] = self.detail_enrichment_completed
        result.update(
            _compact_validation_feedback(
                previous,
                result,
                plan,
                self.evidence,
                self.coa,
                action,
            )
        )
        if (
            name == "patch_mapping"
            and previous
            and not previous.get("accepted")
            and result.get("accepted")
            and _needs_coverage_review(result.get("warnings", []))
        ):
            # A repair that moves a blocked mapping to warning-only has already
            # performed the focused coverage review. Keep the extra review turn
            # only for a first-pass plan that was accepted outright.
            self.warning_cleanup_attempted = True
            self.warning_cleanup_outcome = "completed_during_repair"
            result["coverage_review_completed"] = True
        self._update_stop_state(previous, result, plan)
        self.current_plan = plan
        self.submissions.append(plan.model_dump(mode="json"))
        self.actions.append(action)
        score = _validation_score(result)
        result["validation_score"] = list(score)
        self.history.append(result)
        self.checkpoints.append(
            {
                "validation_attempt": result["validation_attempt"],
                "plan": plan,
                "result": result,
                "score": score,
            }
        )
        if self.best_score is None or score <= self.best_score:
            self.best_plan = plan
            self.best_result = result
            self.best_score = score
            self.best_validation_attempt = result["validation_attempt"]
        if result["accepted"]:
            self.last_accepted_digest = self.digest(plan)
            self.last_accepted_plan = plan
        return result

    def _update_stop_state(self, previous, result, plan) -> None:
        review_kinds = {item.kind for item in plan.review_items}
        if any(
            item.kind == "scope_exception" and item.requires_human_decision
            for item in plan.review_items
        ):
            self.stop_repair = True
            self.stopped_reason = "scope_decision_required"
        elif "ambiguity" in review_kinds:
            self.stop_repair = True
            self.stopped_reason = "unresolved_semantic_ambiguity"
        elif (
            previous
            and not result.get("accepted")
            and _same_blocking_conflicts(previous, result)
        ):
            self.stop_repair = True
            self.stopped_reason = "repeated_quantified_conflict"
            self.repeated_findings = list(result.get("findings") or [])
        if self.stop_repair:
            self.stopped_validation_attempt = result["validation_attempt"]

    def _apply_conditional_strategy(self, plan: WorkbookSourcePlan) -> None:
        known_rows = {item["row_key"] for item in self.evidence}
        derived_rows = list(dict.fromkeys(plan.strategy.derived_summary_source_rows))
        if plan.strategy.summary_mode == SummaryMode.DERIVED and not derived_rows:
            raise ModelToolError(
                "summary_mode=derived requires derived_summary_source_rows showing "
                "the integrated statement or whole-P&L controls"
            )
        if plan.strategy.summary_mode != SummaryMode.DERIVED and derived_rows:
            raise ModelToolError(
                "derived_summary_source_rows require summary_mode=derived"
            )
        unknown = sorted(set(derived_rows) - known_rows)
        scenario_rows = {
            row
            for item in plan.strategy.structural_scenarios
            for row in item.source_rows
        }
        unknown.extend(sorted(scenario_rows - known_rows))
        if unknown:
            raise ModelToolError(
                "unknown conditional-strategy source rows: "
                + ", ".join(sorted(set(unknown)))
            )

        scenarios: dict[StructuralScenario, list[str]] = {}
        for item in plan.strategy.structural_scenarios:
            rows = scenarios.setdefault(item.scenario, [])
            rows.extend(row for row in item.source_rows if row not in rows)

        self.derived_summary_activated = (
            plan.strategy.summary_mode == SummaryMode.DERIVED
        )
        self.derived_summary_source_rows = derived_rows
        self.structural_scenarios = scenarios
        self.summary_only_pushdown_rows = set(
            scenarios.get(StructuralScenario.SUMMARY_ONLY_DEPARTMENT, [])
        )

    def _apply_patch(self, arguments):
        if self.current_plan is None:
            raise ModelToolError(
                "patch_mapping requires an initial validate_mapping submission."
            )
        arguments = _prevalidate_patch_arguments(arguments)
        try:
            patch = WorkbookSourcePatch.model_validate(arguments)
        except ValueError as exc:
            raise ModelToolError(f"Mapping patch is not valid: {exc}") from exc
        if patch.workbook_id != self.workbook_id:
            raise ModelToolError(
                f"workbook_id must be {self.workbook_id!r}, got {patch.workbook_id!r}"
            )
        replacement_ids = [item.coa_id for item in patch.replacements]
        if len(replacement_ids) != len(set(replacement_ids)):
            raise ModelToolError("patch_mapping contains duplicate replacement COA ids.")
        unknown = sorted(
            set(replacement_ids)
            - (set(self.coa) - DETERMINISTIC_SUMMARY_ACCOUNTS)
        )
        if unknown:
            raise ModelToolError("unknown replacement COA ids: " + ", ".join(unknown))
        existing = {item.coa_id: item for item in self.current_plan.decisions}
        changes_decisions = any(
            coa_id not in existing
            or _substantive_decision_digest(existing[coa_id])
            != _substantive_decision_digest(replacement)
            for coa_id, replacement in (
                (item.coa_id, item) for item in patch.replacements
            )
        )
        changes_strategy = (
            (
                patch.ood_misc_summary_mode is not None
                and patch.ood_misc_summary_mode
                != self.current_plan.strategy.ood_misc_summary_mode
            )
            or (
                patch.summary_mode is not None
                and patch.summary_mode != self.current_plan.strategy.summary_mode
            )
            or (
                patch.derived_summary_source_rows is not None
                and patch.derived_summary_source_rows
                != self.current_plan.strategy.derived_summary_source_rows
            )
            or (
                patch.structural_scenarios is not None
                and patch.structural_scenarios
                != self.current_plan.strategy.structural_scenarios
            )
            or (
                patch.department_source_strategy is not None
                and patch.department_source_strategy
                != self.current_plan.strategy.department_source_strategy
            )
            or (
                patch.duplicate_or_supporting_schedules is not None
                and patch.duplicate_or_supporting_schedules
                != self.current_plan.strategy.duplicate_or_supporting_schedules
            )
            or (
                patch.operator_to_coa_hierarchy_conflicts is not None
                and patch.operator_to_coa_hierarchy_conflicts
                != self.current_plan.strategy.operator_to_coa_hierarchy_conflicts
            )
            or (
                patch.source_detail_incomplete is not None
                and patch.source_detail_incomplete
                != self.current_plan.strategy.source_detail_incomplete
            )
        )
        changes_review = (
            patch.review_items is not None
            and patch.review_items != self.current_plan.review_items
        )
        if (
            not changes_decisions
            and not changes_strategy
            and not changes_review
        ):
            if self.warning_cleanup_pending:
                return self.current_plan, {
                    "tool": "patch_mapping",
                    "submitted_decision_count": 0,
                    "submitted_coa_ids": [],
                    "repair_hypothesis": patch.repair_hypothesis,
                    "expected_fix": patch.expected_fix,
                    "coverage_review_completed": True,
                }
            raise ModelToolError(
                "patch_mapping must change at least one mapping decision, strategy "
                "field, or review item."
            )
        replacements = {item.coa_id: item for item in patch.replacements}
        existing_ids = {item.coa_id for item in self.current_plan.decisions}
        decisions = [
            replacements.get(item.coa_id, item)
            for item in self.current_plan.decisions
        ]
        decisions.extend(
            replacements[coa_id]
            for coa_id in replacement_ids
            if coa_id not in existing_ids
        )
        strategy = self.current_plan.strategy
        strategy_updates = {}
        if patch.summary_mode is not None:
            strategy_updates["summary_mode"] = patch.summary_mode
        if patch.derived_summary_source_rows is not None:
            strategy_updates["derived_summary_source_rows"] = (
                patch.derived_summary_source_rows
            )
        if patch.structural_scenarios is not None:
            strategy_updates["structural_scenarios"] = patch.structural_scenarios
        if patch.ood_misc_summary_mode is not None:
            strategy_updates["ood_misc_summary_mode"] = patch.ood_misc_summary_mode
        if patch.department_source_strategy is not None:
            strategy_updates["department_source_strategy"] = (
                patch.department_source_strategy
            )
        if patch.duplicate_or_supporting_schedules is not None:
            strategy_updates["duplicate_or_supporting_schedules"] = (
                patch.duplicate_or_supporting_schedules
            )
        if patch.operator_to_coa_hierarchy_conflicts is not None:
            strategy_updates["operator_to_coa_hierarchy_conflicts"] = (
                patch.operator_to_coa_hierarchy_conflicts
            )
        if patch.source_detail_incomplete is not None:
            strategy_updates["source_detail_incomplete"] = (
                patch.source_detail_incomplete
            )
        if strategy_updates:
            strategy = strategy.model_copy(update=strategy_updates)
        plan = self.current_plan.model_copy(
            update={
                "plan_id": patch.patch_id,
                "strategy": strategy,
                "decisions": decisions,
                "review_items": (
                    patch.review_items
                    if patch.review_items is not None
                    else self.current_plan.review_items
                ),
            }
        )
        return plan, {
            "tool": "patch_mapping",
            "submitted_decision_count": len(patch.replacements),
            "submitted_coa_ids": replacement_ids,
            "repair_hypothesis": patch.repair_hypothesis,
            "expected_fix": patch.expected_fix,
        }

    def evaluate(self, plan: WorkbookSourcePlan) -> dict[str, Any]:
        execution_issues = []
        if plan.workbook_id != self.workbook_id:
            execution_issues.append(
                f"workbook_id must be {self.workbook_id!r}, got {plan.workbook_id!r}"
            )
        submitted = {item.coa_id for item in plan.decisions}
        required_decisions = set(self.coa) - DETERMINISTIC_SUMMARY_ACCOUNTS
        deterministic_submissions = sorted(
            submitted & DETERMINISTIC_SUMMARY_ACCOUNTS
        )
        if deterministic_submissions:
            execution_issues.append(
                "deterministic accounts must not be submitted by the model: "
                + ", ".join(deterministic_submissions)
            )
        missing = sorted(required_decisions - submitted)
        if missing:
            execution_issues.append(
                "missing COA decisions: " + ", ".join(missing)
            )
        rollup_targets = set(DERIVED_SUMMARY_LINKS) | set(SUMMARY_EQUATIONS)
        for decision in plan.decisions:
            if decision.operation != SourceOperation.COA_ROLLUP:
                continue
            if plan.strategy.summary_mode != SummaryMode.DERIVED:
                execution_issues.append(
                    f"{decision.coa_id}: coa_rollup requires summary_mode=derived"
                )
            elif decision.coa_id not in rollup_targets:
                execution_issues.append(
                    f"{decision.coa_id}: coa_rollup is only allowed for a linked "
                    "S12 account or Summary equation"
                )
        unknown_review_ids = sorted({
            coa_id
            for item in plan.review_items
            for coa_id in item.coa_ids
            if coa_id not in self.coa
        })
        if unknown_review_ids:
            execution_issues.append(
                "review items cite unknown COA ids: " + ", ".join(unknown_review_ids)
            )
        evidence_rows = {item["row_key"] for item in self.evidence}
        unknown_review_rows = sorted({
            row_key
            for item in plan.review_items
            for row_key in item.source_rows
            if row_key not in evidence_rows
        })
        if unknown_review_rows:
            execution_issues.append(
                "review items cite unknown source rows: "
                + ", ".join(unknown_review_rows)
            )
        errors = list(execution_issues)
        errors.extend(_review_item_blockers(plan.review_items))
        errors.extend(
            _period_completeness_issues(
                plan,
                self.evidence,
                self.period_labels,
            )
        )
        collapse_issue = _detail_collapse_issue(plan, self.evidence)
        if collapse_issue:
            errors.append(collapse_issue)
        errors.extend(
            _unused_financial_schedule_issues(
                plan,
                self.evidence,
                self.sheet_routing_context,
            )
        )
        missing_venue_names = sorted(
            decision.coa_id
            for decision in plan.decisions
            if decision.coa_id in GENERIC_VENUE_IDS
            and decision.operation != SourceOperation.NO_VALUE
            and not str(decision.venue_name or "").strip()
        )
        if missing_venue_names:
            errors.append(
                "mapped generic venues require venue_name: "
                + ", ".join(missing_venue_names)
            )
        warning_periods: dict[str, list[str]] = {}
        residual_plugs_by_period: dict[str, dict[str, float]] = {}
        for period_id, label in self.period_labels.items():
            values, calculation_issues = _execute(
                plan.decisions,
                self.evidence,
                self.coa,
                period_id=period_id,
                preserve_blanks=self.preserve_blanks,
            )
            residual_plugs_by_period[period_id] = _apply_residual_plugs(
                values,
                self.coa,
                plan.decisions,
                max_ratio=RESIDUAL_AUTO_ACCEPT_RATIO,
            )
            checks = _validate(
                _validation_values(values),
                self.coa,
                plan.decisions,
                plan.strategy,
                plan.review_items,
                self.summary_only_pushdown_rows,
            )
            checks.extend(
                _source_layer_conflict_warnings(
                    plan, self.evidence, values, period_id
                )
            )
            checks.extend(_source_layer_comparison_issues(
                plan, self.evidence, values, period_id
            ))
            checks = _qualify_source_discrepancies(
                checks,
                plan,
                self.evidence,
                self.coa,
                self.history,
                label,
                calculation_issues,
            )
            checks.extend(
                _unsupported_residual_remainder_warnings(
                    values,
                    self.coa,
                    plan.decisions,
                )
            )
            checks.extend(_review_item_warnings(plan.review_items))
            errors.extend(
                f"{label}: {item}"
                for item in calculation_issues
            )
            errors.extend(
                f"{label}: {item}"
                for item in checks
                if item.startswith("error|")
            )
            for item in checks:
                if item.startswith("warning|"):
                    warning_periods.setdefault(item, []).append(label)
        warnings = [
            f"{', '.join(labels)}: {item}"
            for item, labels in warning_periods.items()
        ]
        accepted = not errors
        needs_detail_enrichment = _needs_coverage_review(warnings)
        return {
            "ok": True,
            "accepted": accepted,
            "submitted_decision_count": len(plan.decisions),
            "missing_decision_count": len(missing),
            "error_count": len(errors),
            "warning_count": len(warnings),
            "errors": errors,
            "warnings": warnings,
            "residual_plugs_by_period": residual_plugs_by_period,
            "review_item_count": len(plan.review_items),
            "review_items": [
                item.model_dump(mode="json") for item in plan.review_items
            ],
            "instruction": (
                "Keep the reconciled parent fixed and continue mapping every "
                "positively identifiable child for each source_detail_incomplete "
                "parent. Do not clear supported children merely because coverage "
                "is incomplete. Use not_present only when the source contains no "
                "usable evidence for that child hierarchy. One final warning-cleanup "
                "response is available: make one evidence-supported patch only if "
                "it reduces warnings without disturbing accepted structure; "
                "otherwise return completion unchanged."
                if accepted and needs_detail_enrichment
                else "The mapping has no blocking errors. Return the compact "
                "completion object unchanged."
                if accepted
                else "Call patch_mapping with only implicated replacement "
                "decisions, not the complete plan."
            ),
        }

    def final_is_accepted(self, plan: WorkbookSourcePlan) -> bool:
        return (
            self.last_accepted_digest is not None
            and self.digest(plan) == self.last_accepted_digest
            and self.evaluate(plan)["accepted"]
        )

    def final_response_error(self, completion: WorkbookMappingCompletion):
        if self.current_plan is None:
            return (
                "You have not submitted a mapping. Call validate_mapping with the "
                f"complete {len(self.coa)}-decision plan before returning a "
                "completion object."
            )
        if completion.workbook_id != self.workbook_id:
            return f"The completion workbook_id must be {self.workbook_id!r}."
        if completion.validation_attempt != len(self.history):
            return (
                "validation_attempt must equal the latest attempt number, "
                f"{len(self.history)}."
            )
        if completion.status == "rejected":
            if not self.stop_repair:
                return (
                    "The validator has not classified this mapping as terminally "
                    "rejected. Continue with the requested repair."
                )
            if completion.outcome != _outcome_from_result(
                self.history[-1], self.current_plan.review_items
            ):
                return "Completion outcome must match the validator outcome."
            return None
        if self.last_accepted_plan is None:
            return (
                "The current mapping has not been accepted. Read the latest validator "
                "errors and call patch_mapping with only the omitted or implicated "
                "decisions before returning a completion object."
            )
        expected_outcome = _outcome_from_result(
            self.history[-1], self.last_accepted_plan.review_items
        )
        if completion.outcome != expected_outcome:
            return "Completion outcome must match the validator outcome."
        if self.warning_cleanup_pending:
            self.warning_cleanup_pending = False
            self.warning_cleanup_attempted = True
            self.warning_cleanup_outcome = "kept_accepted_plan"
        return None

    def terminal_result(self, name: str, result: dict[str, Any]):
        """Return a completion payload when another model turn cannot add value."""
        if self.warning_cleanup_pending and name == "patch_mapping":
            return self._finish_warning_cleanup(result)
        if self.stop_repair:
            return self._completion_payload(result, accepted=False)
        if not result.get("accepted"):
            return None
        if (
            _needs_coverage_review(result.get("warnings", []))
            and not self.warning_cleanup_attempted
        ):
            self.warning_cleanup_pending = True
            self.warning_cleanup_outcome = "offered"
            self.warning_cleanup_checkpoint_plan = self.current_plan
            self.warning_cleanup_checkpoint_result = result
            self.warning_cleanup_checkpoint_score = _validation_score(result)
            self.warning_cleanup_checkpoint_attempt = result["validation_attempt"]
            return None
        return self._completion_payload(result)

    def _completion_payload(
        self, result: dict[str, Any], *, accepted: bool = True
    ) -> dict[str, Any]:
        return {
            "workbook_id": self.workbook_id,
            "status": "accepted" if accepted else "rejected",
            "outcome": result.get("outcome")
            or _outcome_from_result(result, result.get("review_items", [])).value,
            "validation_attempt": result["validation_attempt"],
        }

    def _finish_warning_cleanup(self, result: dict[str, Any]) -> dict[str, Any]:
        checkpoint_plan = self.warning_cleanup_checkpoint_plan
        checkpoint_result = self.warning_cleanup_checkpoint_result
        checkpoint_score = self.warning_cleanup_checkpoint_score
        checkpoint_attempt = self.warning_cleanup_checkpoint_attempt
        if (
            checkpoint_plan is None
            or checkpoint_result is None
            or checkpoint_score is None
            or checkpoint_attempt is None
        ):
            raise RuntimeError("Warning cleanup has no accepted checkpoint.")

        self.warning_cleanup_pending = False
        self.warning_cleanup_attempted = True
        improved = (
            result.get("accepted")
            and int(result.get("warning_count") or 0)
            < int(checkpoint_result.get("warning_count") or 0)
        )
        if improved:
            self.warning_cleanup_outcome = "improved"
            return self._completion_payload(result)

        self.warning_cleanup_outcome = "rolled_back"
        self.current_plan = checkpoint_plan
        self.last_accepted_plan = checkpoint_plan
        self.last_accepted_digest = self.digest(checkpoint_plan)
        self.best_plan = checkpoint_plan
        self.best_result = checkpoint_result
        self.best_score = checkpoint_score
        self.best_validation_attempt = checkpoint_attempt
        self.stop_repair = False
        self.stopped_reason = None
        self.stopped_validation_attempt = None
        self.repeated_findings = []
        return self._completion_payload(checkpoint_result)

    @staticmethod
    def digest(plan: WorkbookSourcePlan) -> str:
        payload = json.dumps(
            {
                "workbook_id": plan.workbook_id,
                "strategy": plan.strategy.model_dump(mode="json"),
                "decisions": [
                    _substantive_decision_digest(item)
                    for item in plan.decisions
                ],
            },
            sort_keys=True,
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def declarations(self) -> list[dict[str, Any]]:
        string_list = {"type": "array", "items": {"type": "string"}}
        model_coa_ids = [
            coa_id for coa_id in self.coa
            if coa_id not in DETERMINISTIC_SUMMARY_ACCOUNTS
        ]
        coa_id_list = {
            "type": "array",
            "items": {"type": "string", "enum": list(self.coa)},
        }
        review_item = {
            "type": "object",
            "properties": {
                "kind": {
                    "type": "string",
                    "enum": [
                        "ambiguity",
                        "unusual_convention",
                        "source_discrepancy",
                        "scope_exception",
                    ],
                },
                "message": {"type": "string"},
                "coa_ids": coa_id_list,
                "source_rows": string_list,
                "selected_source_rows": string_list,
                "alternate_source_rows": string_list,
                "selected_excluded_rows": string_list,
                "alternate_excluded_rows": string_list,
                "selected_source_operation": {
                    "anyOf": [
                        {
                            "type": "string",
                            "enum": [item.value for item in SourceLayerOperation],
                        },
                        {"type": "null"},
                    ],
                },
                "alternate_source_operation": {
                    "anyOf": [
                        {
                            "type": "string",
                            "enum": [item.value for item in SourceLayerOperation],
                        },
                        {"type": "null"},
                    ],
                },
                "requires_human_decision": {"type": "boolean"},
            },
            "required": [
                "kind",
                "message",
                "coa_ids",
                "source_rows",
                "selected_source_rows",
                "alternate_source_rows",
                "selected_excluded_rows",
                "alternate_excluded_rows",
                "selected_source_operation",
                "alternate_source_operation",
                "requires_human_decision",
            ],
        }
        review_items = {"type": "array", "items": review_item}
        decision = {
            "type": "object",
            "properties": {
                "coa_id": {"type": "string", "enum": model_coa_ids},
                "operation": {
                    "type": "string",
                    "enum": [item.value for item in SourceOperation],
                },
                "source_rows": string_list,
                "excluded_rows": string_list,
                "venue_name": {
                    "description": (
                        "For a mapped generic venue, the concise operator venue "
                        "name shown in the P&L, such as Restaurant 1, La Loba, or "
                        "Lobby Bar. Otherwise null."
                    ),
                    "anyOf": [{"type": "string"}, {"type": "null"}],
                },
                "scale_factor": {"type": "number"},
                "child_coverage": {
                    "type": "string",
                    "enum": [item.value for item in ChildCoverage],
                },
                "rationale": {
                    "anyOf": [{"type": "string"}, {"type": "null"}],
                },
            },
            "required": [
                "coa_id", "operation", "source_rows", "excluded_rows",
                "venue_name", "child_coverage",
            ],
        }
        strategy = {
            "type": "object",
            "properties": {
                "reporting_layout": {"type": "string"},
                "summary_source": {"type": "string"},
                "department_source_strategy": string_list,
                "duplicate_or_supporting_schedules": string_list,
                "operator_to_coa_hierarchy_conflicts": string_list,
                "non_contiguous_sources": string_list,
                "source_sign_conventions": string_list,
                "calculated_parent_accounts": string_list,
                "source_detail_incomplete": string_list,
                "summary_mode": {
                    "type": "string",
                    "enum": [item.value for item in SummaryMode],
                },
                "derived_summary_source_rows": string_list,
                "structural_scenarios": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "scenario": {
                                "type": "string",
                                "enum": [
                                    item.value for item in StructuralScenario
                                ],
                            },
                            "reason": {"type": "string"},
                            "source_rows": string_list,
                        },
                        "required": ["scenario", "reason", "source_rows"],
                    },
                },
                "ood_misc_summary_mode": {
                    "type": "string",
                    "enum": [item.value for item in OodMiscSummaryMode],
                },
                "planned_validation_checks": string_list,
            },
            "required": [
                "reporting_layout",
                "summary_source",
                "summary_mode",
                "derived_summary_source_rows",
                "structural_scenarios",
                "ood_misc_summary_mode",
            ],
        }
        return [{
            "name": "validate_mapping",
            "description": (
                f"Submit the initial complete mapping with all {len(model_coa_ids)} "
                "model-mapped COA "
                "source decisions for deterministic validation. Call this exactly "
                "once."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "plan_id": {"type": "string"},
                    "workbook_id": {"type": "string"},
                    "strategy": strategy,
                    "decisions": {"type": "array", "items": decision},
                    "review_items": review_items,
                },
                "required": [
                    "plan_id", "workbook_id", "strategy", "decisions",
                    "review_items",
                ],
            },
        }, {
            "name": "patch_mapping",
            "description": (
                "Add an initially omitted COA decision or replace only decisions "
                "implicated by validator feedback, retain all other decisions, "
                "and rerun deterministic validation."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "patch_id": {"type": "string"},
                    "workbook_id": {"type": "string"},
                    "replacements": {"type": "array", "items": decision},
                    "repair_hypothesis": {
                        "type": "string",
                        "description": "One concise sentence explaining the likely cause of the current findings.",
                    },
                    "expected_fix": {
                        "type": "string",
                        "description": "One concise sentence describing what this patch should resolve.",
                    },
                    "summary_mode": {
                        "type": "string",
                        "enum": [item.value for item in SummaryMode],
                    },
                    "derived_summary_source_rows": {
                        "anyOf": [string_list, {"type": "null"}],
                    },
                    "structural_scenarios": {
                        "anyOf": [
                            strategy["properties"]["structural_scenarios"],
                            {"type": "null"},
                        ],
                    },
                    "ood_misc_summary_mode": {
                        "type": "string",
                        "enum": [item.value for item in OodMiscSummaryMode],
                    },
                    "department_source_strategy": {
                        "anyOf": [string_list, {"type": "null"}],
                    },
                    "duplicate_or_supporting_schedules": {
                        "anyOf": [string_list, {"type": "null"}],
                    },
                    "operator_to_coa_hierarchy_conflicts": {
                        "anyOf": [string_list, {"type": "null"}],
                    },
                    "source_detail_incomplete": {
                        "anyOf": [string_list, {"type": "null"}],
                    },
                    "review_items": review_items,
                },
                "required": [
                    "patch_id", "workbook_id", "replacements",
                    "repair_hypothesis", "expected_fix",
                ],
            },
        }]

    def signature(self) -> str:
        payload = json.dumps(self.declarations(), sort_keys=True)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _substantive_decision_digest(decision: AccountSourceDecision) -> str:
    """Serialize only fields that can change mapping execution."""
    return json.dumps(
        {
            "coa_id": decision.coa_id,
            "operation": decision.operation.value,
            "source_rows": decision.source_rows,
            "excluded_rows": decision.excluded_rows,
            "venue_name": decision.venue_name,
            "scale_factor": decision.scale_factor,
            "child_coverage": decision.child_coverage.value,
        },
        sort_keys=True,
    )


def _deduplicate_identical_decisions(values: Any) -> Any:
    """Remove only exact repeats; conflicting decisions remain visible errors."""
    if not isinstance(values, list):
        return values
    deduped = []
    seen: dict[str, str] = {}
    for value in values:
        if not isinstance(value, dict):
            deduped.append(value)
            continue
        coa_id = value.get("coa_id")
        signature = json.dumps(value, sort_keys=True, default=str)
        if coa_id and seen.get(str(coa_id)) == signature:
            continue
        if coa_id:
            seen[str(coa_id)] = signature
        deduped.append(value)
    return deduped


def _prevalidate_plan_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
    """Normalize initial-payload defects that cannot change mapping meaning."""
    normalized = dict(arguments)
    normalized["decisions"] = _deduplicate_identical_decisions(
        normalized.get("decisions")
    )
    return normalized


def _prevalidate_patch_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
    """Normalize only payload defects that cannot change mapping meaning."""
    normalized = dict(arguments)
    normalized.setdefault(
        "repair_hypothesis",
        "Review the current deterministic validation findings.",
    )
    normalized.setdefault(
        "expected_fix",
        "Resolve the cited findings without changing supported mappings.",
    )
    normalized["replacements"] = _deduplicate_identical_decisions(
        normalized.get("replacements")
    )
    normalized["review_items"] = _normalize_patch_review_items(
        normalized.get("review_items")
    )
    return normalized


def _normalize_patch_review_items(values: Any) -> Any:
    """Keep a repair note while dropping only malformed comparison metadata.

    The narrative, cited COA ids, and cited source rows still reach feedback.
    We clear the optional typed comparison fields only when they cannot form a
    valid, disjoint source-layer comparison and would otherwise reject the
    entire repair payload before its mapping changes can be evaluated.
    """
    if not isinstance(values, list):
        return values
    allowed_operations = {item.value for item in SourceLayerOperation}
    normalized_items = []
    for value in values:
        if not isinstance(value, dict):
            normalized_items.append(value)
            continue
        item = dict(value)
        selected = item.get("selected_source_rows") or []
        alternate = item.get("alternate_source_rows") or []
        selected_excluded = item.get("selected_excluded_rows") or []
        alternate_excluded = item.get("alternate_excluded_rows") or []
        selected_operation = item.get("selected_source_operation")
        alternate_operation = item.get("alternate_source_operation")
        cited = item.get("source_rows") or []
        comparison_present = any(
            (
                selected,
                alternate,
                selected_excluded,
                alternate_excluded,
                selected_operation,
                alternate_operation,
            )
        )
        malformed = False
        if comparison_present:
            selected_set = set(selected) | set(selected_excluded)
            alternate_set = set(alternate) | set(alternate_excluded)
            malformed = (
                item.get("kind") != "source_discrepancy"
                or not selected
                or not alternate
                or selected_operation not in allowed_operations
                or alternate_operation not in allowed_operations
                or not (selected_set | alternate_set) <= set(cited)
                or bool(selected_set & alternate_set)
                or (
                    selected_operation == SourceLayerOperation.DIRECT.value
                    and (len(selected) != 1 or bool(selected_excluded))
                )
                or (
                    alternate_operation == SourceLayerOperation.DIRECT.value
                    and (len(alternate) != 1 or bool(alternate_excluded))
                )
                or (
                    selected_operation
                    == SourceLayerOperation.ADJUSTED_SUBTOTAL.value
                    and not selected_excluded
                )
                or (
                    alternate_operation
                    == SourceLayerOperation.ADJUSTED_SUBTOTAL.value
                    and not alternate_excluded
                )
                or (
                    selected_operation
                    != SourceLayerOperation.ADJUSTED_SUBTOTAL.value
                    and bool(selected_excluded)
                )
                or (
                    alternate_operation
                    != SourceLayerOperation.ADJUSTED_SUBTOTAL.value
                    and bool(alternate_excluded)
                )
            )
        if malformed:
            item.update(
                {
                    "selected_source_rows": [],
                    "alternate_source_rows": [],
                    "selected_excluded_rows": [],
                    "alternate_excluded_rows": [],
                    "selected_source_operation": None,
                    "alternate_source_operation": None,
                }
            )
        normalized_items.append(item)
    return normalized_items


def _finding_texts(result: dict | None) -> list[str]:
    if not result:
        return []
    return [
        str(item)
        for item in [*result.get("errors", []), *result.get("warnings", [])]
    ]


def _finding_rule(finding: str) -> str:
    for marker in ("error|", "warning|"):
        if marker in finding:
            return finding.split(marker, 1)[1].split("|", 1)[0]
    return "execution"


def _finding_target(finding: str) -> str:
    for marker in ("error|", "warning|"):
        if marker in finding:
            parts = finding.split(marker, 1)[1].split("|", 2)
            return parts[1] if len(parts) > 1 else ""
    return ""


def _finding_targets(result: dict | None, rule: str) -> set[str]:
    return {
        target
        for finding in _finding_texts(result)
        if _finding_rule(finding) == rule
        for target in [_finding_target(finding)]
        if target
    }


SOURCE_EXCEPTION_RULES = {
    "source_discrepancy",
    "source_layer_conflict",
    "source_presentation_exception",
    "small_source_reconciliation_difference",
    "scope_exclusion",
}

COVERAGE_GAP_RULES = {
    "source_detail_incomplete",
    "coverage_unspecified",
    "large_residual_plug",
    "unsupported_residual_remainder",
    "unresolved_negative_residual",
}

QUANTIFIED_DETAIL_KEYS = {
    "actual",
    "expected",
    "variance",
    "parent",
    "children",
    "plug",
    "ratio",
}


def _review_kind(item) -> str:
    return str(
        item.kind if isinstance(item, MappingReviewItem) else item.get("kind")
    )


def _review_requires_human_decision(item) -> bool:
    return bool(
        item.requires_human_decision
        if isinstance(item, MappingReviewItem)
        else item.get("requires_human_decision", False)
    )


def _review_item_blockers(review_items) -> list[str]:
    blockers = []
    for item in review_items:
        kind = _review_kind(item)
        if kind not in {"ambiguity", "scope_exception"}:
            continue
        if kind == "scope_exception" and not _review_requires_human_decision(item):
            continue
        coa_ids = (
            item.coa_ids
            if isinstance(item, MappingReviewItem)
            else item.get("coa_ids", [])
        )
        message = (
            item.message
            if isinstance(item, MappingReviewItem)
            else item.get("message", "")
        )
        target = coa_ids[0] if coa_ids else "mapping"
        rule = "unresolved_ambiguity" if kind == "ambiguity" else "scope_exception"
        blockers.append(
            f"error|{rule}|{target}|message={str(message).replace('|', '/')}"
        )
    return blockers


def _review_item_warnings(review_items) -> list[str]:
    warnings = []
    for item in review_items:
        if _review_kind(item) != "scope_exception":
            continue
        if _review_requires_human_decision(item):
            continue
        coa_ids = (
            item.coa_ids
            if isinstance(item, MappingReviewItem)
            else item.get("coa_ids", [])
        )
        message = (
            item.message
            if isinstance(item, MappingReviewItem)
            else item.get("message", "")
        )
        target = coa_ids[0] if coa_ids else "mapping"
        warnings.append(
            f"warning|scope_exclusion|{target}|"
            f"message={str(message).replace('|', '/')}"
        )
    return warnings


def _needs_coverage_review(findings) -> bool:
    return any(_finding_rule(str(item)) in COVERAGE_GAP_RULES for item in findings)


def _outcome_from_result(result: dict[str, Any], review_items) -> MappingOutcome:
    if any(
        _review_kind(item) == "scope_exception"
        and _review_requires_human_decision(item)
        for item in review_items
    ):
        return MappingOutcome.SCOPE_EXCEPTION
    if result.get("errors") or result.get("accepted") is False:
        return MappingOutcome.REJECTED
    warning_rules = {_finding_rule(item) for item in result.get("warnings", [])}
    if warning_rules & COVERAGE_GAP_RULES:
        return MappingOutcome.COVERAGE_GAP
    if warning_rules & SOURCE_EXCEPTION_RULES:
        return MappingOutcome.SOURCE_EXCEPTION
    return MappingOutcome.CLEAN


def _blocking_fingerprint(result: dict[str, Any]) -> tuple[tuple, ...]:
    contexts = result.get("findings") or []
    fingerprint = []
    for finding in result.get("errors", []):
        rule = _finding_rule(finding)
        target = _finding_target(finding)
        rows = sorted({
            row
            for context in contexts
            if context.get("rule") == rule
            and (not target or target in context.get("coa_ids", []))
            for row in context.get("source_rows", [])
        })
        details = tuple(sorted(_finding_details(finding).items()))
        raw = str(finding) if rule == "execution" else ""
        fingerprint.append(
            (_finding_period(finding), rule, target, details, tuple(rows), raw)
        )
    return tuple(sorted(fingerprint, key=repr))


def _same_blocking_conflicts(previous, current) -> bool:
    previous_fingerprint = _blocking_fingerprint(previous)
    current_fingerprint = _blocking_fingerprint(current)
    if not previous_fingerprint or previous_fingerprint != current_fingerprint:
        return False
    return any(
        QUANTIFIED_DETAIL_KEYS & {key for key, _value in signature[3]}
        for signature in current_fingerprint
    )


def _numeric_detail(details, key):
    try:
        return float(details[key])
    except (KeyError, TypeError, ValueError):
        return None


def _structured_exceptions(checks_by_period, period_labels, review_items):
    """Join deterministic exception math to the model's cited treatment."""
    records = []
    for period_id, checks in checks_by_period.items():
        for finding in checks:
            rule = _finding_rule(finding)
            if rule not in SOURCE_EXCEPTION_RULES | COVERAGE_GAP_RULES:
                continue
            target = _finding_target(finding)
            details = _finding_details(finding)
            matching_review = next(
                (
                    item
                    for item in review_items
                    if _review_kind(item) == (
                        "scope_exception"
                        if rule == "scope_exclusion"
                        else "source_discrepancy"
                    )
                    and target in (
                        item.coa_ids
                        if isinstance(item, MappingReviewItem)
                        else item.get("coa_ids", [])
                    )
                    and (
                        rule != "source_layer_conflict"
                        or (
                            (
                                item.selected_source_rows
                                if isinstance(item, MappingReviewItem)
                                else item.get("selected_source_rows", [])
                            )
                            and (
                                item.coa_ids[0]
                                if isinstance(item, MappingReviewItem)
                                else (item.get("coa_ids") or [None])[0]
                            )
                            == target
                        )
                    )
                ),
                None,
            )
            if isinstance(matching_review, MappingReviewItem):
                treatment = matching_review.message
                source_rows = list(matching_review.source_rows)
            elif matching_review:
                treatment = str(matching_review.get("message") or "")
                source_rows = list(matching_review.get("source_rows") or [])
            else:
                treatment = (
                    "Retain the supported scope exclusion and disclose it for review."
                    if rule == "scope_exclusion"
                    else
                    "Preserve both independently reported source values for review."
                    if rule in SOURCE_EXCEPTION_RULES
                    else "Preserve supported source detail and flag the incomplete coverage."
                )
                source_rows = []
            records.append(
                {
                    "type": (
                        MappingOutcome.SOURCE_EXCEPTION.value
                        if rule in SOURCE_EXCEPTION_RULES
                        else MappingOutcome.COVERAGE_GAP.value
                    ),
                    "rule": rule,
                    "period_id": period_id,
                    "period_label": period_labels[period_id],
                    "target": target,
                    "reported_value": _numeric_detail(details, "actual")
                    if "actual" in details
                    else _numeric_detail(details, "parent"),
                    "comparison_value": _numeric_detail(details, "expected")
                    if "expected" in details
                    else _numeric_detail(details, "children"),
                    "variance": _numeric_detail(details, "variance"),
                    "equation": details.get("equation"),
                    "treatment": treatment,
                    "source_rows": source_rows,
                }
            )
    return records


def _source_layer_conflict_warnings(plan, evidence, values, period_id):
    """Calculate two typed, disjoint source equations for one COA target."""
    rows = {item["row_key"]: item for item in evidence}
    decisions = {item.coa_id: item for item in plan.decisions}
    warnings = []
    for review in plan.review_items:
        if review.kind != "source_discrepancy":
            continue
        if not review.selected_source_rows or not review.alternate_source_rows:
            continue
        cited_decisions = [
            decisions[coa_id]
            for coa_id in review.coa_ids
            if coa_id in decisions
        ]
        selected_rows = {
            row
            for decision in cited_decisions
            for row in [*decision.source_rows, *decision.excluded_rows]
        }
        if not set(review.selected_source_rows) <= selected_rows:
            continue
        target = next(
            (
                coa_id
                for coa_id in review.coa_ids
                if coa_id in decisions and values.get(coa_id) is not None
            ),
            None,
        )
        if target is None:
            continue
        try:
            selected = _source_layer_value(
                rows,
                review.selected_source_rows,
                review.selected_excluded_rows,
                review.selected_source_operation,
                period_id,
            )
            alternate = _source_layer_value(
                rows,
                review.alternate_source_rows,
                review.alternate_excluded_rows,
                review.alternate_source_operation,
                period_id,
            )
        except (KeyError, TypeError, ValueError):
            continue
        mapped = values.get(target)
        if selected is None or alternate is None or mapped is None:
            continue
        if abs(float(mapped) - selected) > _tolerance(float(mapped)):
            continue
        variance = selected - alternate
        if abs(variance) <= _tolerance(selected):
            continue
        warnings.append(
            f"warning|source_layer_conflict|{target}|"
            f"actual={selected:.4f}|expected={alternate:.4f}|"
            f"variance={variance:.4f}|equation=selected mapped layer versus "
            "typed alternate cited layer"
        )
    return warnings


def _source_layer_comparison_issues(plan, evidence, values, period_id):
    rows = {item["row_key"]: item for item in evidence}
    decisions = {item.coa_id: item for item in plan.decisions}
    issues = []
    for review in plan.review_items:
        if (
            review.kind != "source_discrepancy"
            or not review.selected_source_rows
            or not review.alternate_source_rows
        ):
            continue
        target = review.coa_ids[0] if review.coa_ids else None
        if target not in decisions or values.get(target) is None:
            continue
        selected_rows = set(review.selected_source_rows) | set(
            review.selected_excluded_rows
        )
        decision_rows = set(decisions[target].source_rows) | set(
            decisions[target].excluded_rows
        )
        if not selected_rows <= decision_rows:
            issues.append(
                f"error|invalid_source_layer_comparison|{target}|"
                "selected layer is not contained in the mapped target decision"
            )
            continue
        try:
            selected = _source_layer_value(
                rows,
                review.selected_source_rows,
                review.selected_excluded_rows,
                review.selected_source_operation,
                period_id,
            )
        except (KeyError, TypeError, ValueError) as exc:
            issues.append(
                f"error|invalid_source_layer_comparison|{target}|"
                f"selected layer cannot be calculated: {exc}"
            )
            continue
        mapped = float(values[target])
        if selected is not None and abs(mapped - selected) > _tolerance(mapped):
            issues.append(
                f"error|invalid_source_layer_comparison|{target}|"
                f"mapped={mapped:.4f}|selected_equation={selected:.4f}|"
                "typed selected layer does not equal the mapped target"
            )
    return issues


def _source_layer_value(
    rows,
    source_rows,
    excluded_rows,
    operation,
    period_id,
):
    if operation is None:
        raise ValueError("missing source-layer operation")
    included = [_row_value(rows, row, period_id=period_id) for row in source_rows]
    excluded = [_row_value(rows, row, period_id=period_id) for row in excluded_rows]
    if any(value is None for value in [*included, *excluded]):
        return None
    if operation == SourceLayerOperation.DIRECT:
        if len(included) != 1:
            raise ValueError("direct requires one row")
        return float(included[0])
    if operation == SourceLayerOperation.SUM:
        return sum(float(value) for value in included)
    if operation == SourceLayerOperation.ADJUSTED_SUBTOTAL:
        return sum(float(value) for value in included) - sum(
            float(value) for value in excluded
        )
    if operation == SourceLayerOperation.NEGATE:
        return -sum(float(value) for value in included)
    raise ValueError(f"unsupported source-layer operation {operation}")


def _validation_score(result: dict[str, Any]) -> tuple[int, ...]:
    """Rank executable plans by the accounting priorities used by the mapper."""
    errors = [
        _finding_rule(item)
        for item in result.get("errors", [])
    ]
    warnings = result.get("warnings", [])
    summary_math = sum(rule == "summary_math" for rule in errors)
    summary_department = sum(
        rule in {
            "summary_department",
            "summary_combined_ood_misc",
            "summary_combined_ood_misc_inactive_bucket",
            "ood_misc_summary_mode_unknown",
        }
        for rule in errors
    )
    source_or_execution = sum(
        rule == "execution" or rule.startswith("source_row_")
        for rule in errors
    )
    hierarchy = sum(
        rule in {
            "coverage",
            "duplicate_decision",
            "hierarchy_complete",
            "hierarchy_partial_with_residual",
            "coverage_inconsistent",
            "coverage_unspecified",
            "parent_no_value_with_children",
            "unused_financial_schedule",
        }
        for rule in errors
    )
    return (
        int(result.get("missing_decision_count") or 0),
        summary_math,
        summary_department,
        source_or_execution,
        hierarchy,
        len(errors),
        len(warnings),
    )


@lru_cache(maxsize=1)
def _validation_rule_guidance() -> dict[str, dict[str, str]]:
    text = resources.files("hotel_pl_normalizer.prompts").joinpath(
        "validation_feedback.md"
    ).read_text(encoding="utf-8")
    guidance = {}
    pattern = re.compile(
        r"### `([^`]+)`\s+Description:\s*(.*?)\s+Resolution:\s*(.*?)(?=\n### `|\Z)",
        re.DOTALL,
    )
    for rule, description, resolution in pattern.findall(text):
        guidance[rule] = {
            "description": " ".join(description.split()),
            "resolution": " ".join(resolution.split()),
        }
    return guidance


def _finding_context(finding, plan, evidence_rows, coa):
    rule = _finding_rule(finding)
    target = _finding_target(finding)
    coa_ids = [coa_id for coa_id in coa if coa_id in finding]
    if target in coa and target not in coa_ids:
        coa_ids.insert(0, target)
    if rule in {
        "hierarchy_complete",
        "hierarchy_partial_with_residual",
        "coverage_inconsistent",
        "coverage_unspecified",
        "parent_no_value_with_children",
        "source_detail_incomplete",
    } and target in coa:
        coa_ids.extend(
            item["coa_id"]
            for item in coa.values()
            if item.get("parent_coa_id") == target
        )
        coa_ids = list(dict.fromkeys(coa_ids))
    known_rows = {str(item.get("row_key")) for item in evidence_rows}
    explicit_rows = [row for row in known_rows if row in finding]
    decisions = {item.coa_id: item for item in plan.decisions}
    cited_rows = []
    for coa_id in coa_ids:
        decision = decisions.get(coa_id)
        if decision is not None:
            cited_rows.extend([*decision.source_rows, *decision.excluded_rows])
    source_rows = [
        row
        for row in dict.fromkeys([*explicit_rows, *cited_rows])
        if row in known_rows
    ]
    details = {
        key.strip(): value.strip()
        for part in str(finding).split("|")[3:]
        if "=" in part
        for key, value in [part.split("=", 1)]
    }
    context = {
        "rule": rule,
        "coa_ids": coa_ids,
        "source_rows": source_rows,
    }
    if details:
        context["details"] = details
    if rule in {
        "hierarchy_complete",
        "source_detail_incomplete",
        "hierarchy_partial_with_residual",
    } and {
        "parent",
        "children",
    } <= details.keys():
        try:
            parent = float(details["parent"])
            children = float(details["children"])
            difference = children - parent
            direction = "greater than" if difference > 0 else "less than"
            context["validation_math"] = (
                f"children are {abs(difference):,.2f} {direction} parent"
            )
        except ValueError:
            pass
    elif rule in {"summary_department", "summary_math"} and {
        "actual",
        "expected",
    } <= details.keys():
        try:
            actual = float(details["actual"])
            expected = float(details["expected"])
            difference = actual - expected
            direction = "greater than" if difference > 0 else "less than"
            subject = "summary" if rule == "summary_department" else "summary result"
            context["validation_math"] = (
                f"{subject} is {abs(difference):,.2f} {direction} expected equation"
                if rule == "summary_math"
                else f"{subject} is {abs(difference):,.2f} {direction} department"
            )
        except ValueError:
            pass
    if rule == "summary_department":
        candidates = _offset_candidates(finding, plan, evidence_rows, coa)
        if candidates:
            context["offset_candidates"] = candidates
    return context


OFFSET_LABEL_TERMS = (
    "offset",
    "allocation",
    "allocated",
    "credit",
    "reimbursement",
    "reimbursable",
    "transfer",
    "adjustment",
    "cross charge",
    "cross-charge",
    "inventory factor",
    "inv factor",
)


def _offset_candidates(finding, plan, evidence_rows, coa, limit=4):
    """Return a few unused, value-matched rows after a department mismatch."""
    details = {
        key.strip(): value.strip()
        for part in str(finding).split("|")[3:]
        if "=" in part
        for key, value in [part.split("=", 1)]
    }
    try:
        variance = float(details["variance"])
    except (KeyError, ValueError):
        return []
    if abs(variance) <= 0.005:
        return []

    target = _finding_target(finding)
    detail_ids = []
    if target in SUMMARY_LINKS:
        detail_ids.append(SUMMARY_LINKS[target])
    expression = details.get("equation", "")
    detail_ids.extend(
        coa_id for coa_id in coa if coa_id in expression and coa_id not in detail_ids
    )
    by_id = {decision.coa_id: decision for decision in plan.decisions}
    relevant_rows = [
        row
        for coa_id in detail_ids
        for row in (
            [*by_id[coa_id].source_rows, *by_id[coa_id].excluded_rows]
            if coa_id in by_id
            else []
        )
    ]
    relevant_sheets = {row.rsplit("!", 1)[0] for row in relevant_rows if "!" in row}
    used_rows = {
        row
        for decision in plan.decisions
        for row in [*decision.source_rows, *decision.excluded_rows]
    }
    tolerance = max(5.0, abs(variance) * 0.005)
    matches = []
    for evidence in evidence_rows:
        row_key = str(evidence.get("row_key") or "")
        if not row_key or row_key in used_rows:
            continue
        if relevant_sheets and row_key.rsplit("!", 1)[0] not in relevant_sheets:
            continue
        raw_values = evidence.get("selected_values") or {
            "selected": evidence.get("selected_value")
        }
        numeric_values = {
            period_id: float(value)
            for period_id, value in raw_values.items()
            if isinstance(value, (int, float))
        }
        if not numeric_values or not any(
            abs(abs(value) - abs(variance)) <= tolerance
            for value in numeric_values.values()
        ):
            continue
        label = str(evidence.get("label") or "")
        normalized = label.lower()
        term_match = next(
            (term for term in OFFSET_LABEL_TERMS if term in normalized), None
        )
        matches.append((
            0 if term_match else 1,
            row_key,
            {
                "row_key": row_key,
                "label": label,
                "values": numeric_values,
                "reason": (
                    f"label suggests {term_match} and value is near the variance"
                    if term_match
                    else "unused row value is near the variance"
                ),
            },
        ))
    matches.sort(key=lambda item: (item[0], item[1]))
    return [item[2] for item in matches[:limit]]


SOURCE_DISCREPANCY_BLOCKING_RULES = {
    "coverage",
    "duplicate_decision",
    "hierarchy_complete",
    "hierarchy_partial_with_residual",
    "coverage_inconsistent",
    "parent_no_value_with_children",
}


def _finding_details(finding):
    return {
        key.strip(): value.strip()
        for part in str(finding).split("|")[3:]
        if "=" in part
        for key, value in [part.split("=", 1)]
    }


def _qualify_source_discrepancies(
    checks, plan, evidence, coa, history, period_label, calculation_issues
):
    """Downgrade only a repeated, independently supported source mismatch."""
    if calculation_issues or any(
        item.startswith("error|summary_math|")
        or item.startswith("error|source_row_")
        or item.startswith("error|invalid_source_layer_comparison|")
        for item in checks
    ):
        return checks

    checks = _qualify_combined_ood_misc_presentation(
        checks, plan, coa, history, period_label
    )

    qualified = {}
    unqualified_expense_targets = set()
    output = []
    for finding in checks:
        target = _finding_target(finding)
        if not (
            finding.startswith("error|summary_department|")
            and target != "S12.total_departmental_expenses"
        ):
            output.append(finding)
            continue
        warning = _qualified_source_discrepancy_warning(
            finding, plan, evidence, coa, history, period_label, checks
        )
        if warning is None:
            output.append(finding)
            if target in DEPARTMENTAL_EXPENSE_SUMMARY_TARGETS:
                unqualified_expense_targets.add(target)
            continue
        qualified[target] = float(_finding_details(finding)["variance"])
        output.append(warning)

    aggregate = next(
        (
            item
            for item in output
            if item.startswith(
                "error|summary_department|S12.total_departmental_expenses|"
            )
        ),
        None,
    )
    if aggregate and not unqualified_expense_targets:
        details = _finding_details(aggregate)
        try:
            aggregate_variance = float(details["variance"])
        except (KeyError, ValueError):
            aggregate_variance = None
        component_variance = sum(
            qualified.get(target, 0.0)
            for target in DEPARTMENTAL_EXPENSE_SUMMARY_TARGETS
        )
        if (
            aggregate_variance is not None
            and qualified.keys() & DEPARTMENTAL_EXPENSE_SUMMARY_TARGETS
            and abs(aggregate_variance - component_variance)
            <= _tolerance(aggregate_variance)
        ):
            output.remove(aggregate)
            output.append(
                aggregate.replace(
                    "error|summary_department|",
                    "warning|source_discrepancy|",
                    1,
                )
                + "|component source discrepancies explain the aggregate difference"
            )
    return _collapse_qualified_source_warnings(output)


def _collapse_qualified_source_warnings(checks):
    """Keep one root warning when a typed comparison proves the same mismatch."""
    qualified = [
        item
        for item in checks
        if _finding_rule(item) in {
            "source_discrepancy",
            "source_presentation_exception",
        }
    ]
    output = []
    for finding in checks:
        if _finding_rule(finding) != "source_layer_conflict":
            output.append(finding)
            continue
        target = _finding_target(finding)
        variance = _numeric_detail(_finding_details(finding), "variance")
        duplicate = any(
            _finding_target(root) == target
            and variance is not None
            and (
                root_variance := _numeric_detail(
                    _finding_details(root), "variance"
                )
            ) is not None
            and abs(root_variance - variance) <= _tolerance(variance)
            for root in qualified
        )
        if not duplicate:
            output.append(finding)
    return output


OOD_MISC_SUMMARY_LINKS = {
    "S12.total_other_operated_departments_revenue": (
        "S3.total_other_operated_departments_revenue"
    ),
    "S12.total_miscellaneous_income": "S4.total_miscellaneous_income",
}


def _qualify_combined_ood_misc_presentation(
    checks, plan, coa, history, period_label
):
    """Flag, but do not silently accept, a proven combined source Summary."""
    mode = plan.strategy.ood_misc_summary_mode
    if mode not in {
        OodMiscSummaryMode.COMBINED_IN_OOD,
        OodMiscSummaryMode.COMBINED_IN_MISC,
    }:
        return checks
    if any(
        item.startswith("error|summary_combined_ood_misc|")
        or item.startswith("error|summary_combined_ood_misc_inactive_bucket|")
        for item in checks
    ):
        return checks

    findings = {
        _finding_target(item): item
        for item in checks
        if item.startswith("error|summary_department|")
        and _finding_target(item) in OOD_MISC_SUMMARY_LINKS
    }
    if set(findings) != set(OOD_MISC_SUMMARY_LINKS):
        return checks

    details = {target: _finding_details(item) for target, item in findings.items()}
    try:
        summary_values = {
            target: float(item["actual"]) for target, item in details.items()
        }
        detail_values = {
            target: float(item["expected"]) for target, item in details.items()
        }
        variances = {
            target: float(item["variance"]) for target, item in details.items()
        }
    except (KeyError, ValueError):
        return checks

    active_target = (
        "S12.total_other_operated_departments_revenue"
        if mode == OodMiscSummaryMode.COMBINED_IN_OOD
        else "S12.total_miscellaneous_income"
    )
    inactive_target = next(
        target for target in OOD_MISC_SUMMARY_LINKS if target != active_target
    )
    detail_total = sum(detail_values.values())
    if (
        abs(summary_values[active_target] - detail_total)
        > _tolerance(summary_values[active_target])
        or abs(summary_values[inactive_target])
        > _tolerance(summary_values[inactive_target])
        or abs(sum(variances.values())) > _tolerance(detail_total)
    ):
        return checks
    if not all(
        _same_discrepancy_was_previously_blocking(
            history, period_label, target, variances[target]
        )
        for target in OOD_MISC_SUMMARY_LINKS
    ):
        return checks

    by_id = {decision.coa_id: decision for decision in plan.decisions}
    active_summary = by_id.get(active_target)
    detail_decisions = [
        by_id.get(detail_id) for detail_id in OOD_MISC_SUMMARY_LINKS.values()
    ]
    if active_summary is None or any(item is None for item in detail_decisions):
        return checks
    summary_rows = _decision_source_set(active_summary)
    detail_row_sets = [_decision_source_set(item) for item in detail_decisions]
    summary_detail_overlap = summary_rows & set().union(*detail_row_sets)
    if (
        not summary_rows
        or any(not rows for rows in detail_row_sets)
        or (
            summary_detail_overlap
            and not _summary_only_overlap_is_authorized(
                plan, summary_detail_overlap
            )
        )
        or detail_row_sets[0] & detail_row_sets[1]
    ):
        return checks
    cited_rows = summary_rows | set().union(*detail_row_sets)
    cited_ids = set(OOD_MISC_SUMMARY_LINKS) | set(OOD_MISC_SUMMARY_LINKS.values())
    review = next(
        (
            item
            for item in plan.review_items
            if item.kind == "source_discrepancy"
            and cited_ids <= set(item.coa_ids)
            and (
                not item.source_rows
                or cited_rows <= set(item.source_rows)
            )
        ),
        None,
    )
    if review is None:
        return checks
    if _related_hierarchy_is_blocking(
        checks, list(OOD_MISC_SUMMARY_LINKS.values()), coa
    ):
        return checks

    return [
        (
            item.replace(
                "error|summary_department|",
                "warning|source_presentation_exception|",
                1,
            )
            + "|operator Summary combines OOD and Misc while detailed schedules "
            "report them separately"
            if item in findings.values()
            else item
        )
        for item in checks
    ]


def _summary_only_overlap_is_authorized(plan, overlap_rows):
    """Allow only source rows explicitly declared as summary-only evidence."""
    authorized_rows = {
        row
        for scenario in plan.strategy.structural_scenarios
        if scenario.scenario == StructuralScenario.SUMMARY_ONLY_DEPARTMENT
        for row in scenario.source_rows
    }
    return overlap_rows <= authorized_rows


DEPARTMENTAL_EXPENSE_SUMMARY_TARGETS = {
    "S12.total_rooms_expenses",
    "S12.total_food_and_beverage_expenses",
    "S12.total_other_operated_departments_expenses",
}


def _qualified_source_discrepancy_warning(
    finding, plan, evidence, coa, history, period_label, checks
):
    target = _finding_target(finding)
    details = _finding_details(finding)
    try:
        variance = float(details["variance"])
    except (KeyError, ValueError):
        return None
    typed_current_match = any(
        _finding_rule(item) == "source_layer_conflict"
        and _finding_target(item) == target
        and (
            typed_variance := _numeric_detail(
                _finding_details(item), "variance"
            )
        ) is not None
        and abs(typed_variance - variance) <= _tolerance(variance)
        for item in checks
    )
    if not typed_current_match and not _same_discrepancy_was_previously_blocking(
        history, period_label, target, variance
    ):
        return None

    detail_ids = []
    if target in DERIVED_SUMMARY_LINKS:
        detail_ids.append(DERIVED_SUMMARY_LINKS[target])
    expression = details.get("equation", "")
    detail_ids.extend(
        coa_id for coa_id in coa if coa_id in expression and coa_id not in detail_ids
    )
    if not detail_ids:
        return None
    by_id = {decision.coa_id: decision for decision in plan.decisions}
    summary_decision = by_id.get(target)
    detail_decisions = [by_id.get(coa_id) for coa_id in detail_ids]
    if summary_decision is None or any(item is None for item in detail_decisions):
        return None
    summary_rows = _decision_source_set(summary_decision)
    detail_rows = set().union(
        *(_decision_source_set(item) for item in detail_decisions)
    )
    if not summary_rows or not detail_rows or summary_rows & detail_rows:
        return None

    review = next(
        (
            item
            for item in plan.review_items
            if item.kind == "source_discrepancy"
            and {target, *detail_ids} <= set(item.coa_ids)
            and summary_rows | detail_rows <= set(item.source_rows)
        ),
        None,
    )
    if review is None:
        return None
    if _related_hierarchy_is_blocking(checks, detail_ids, coa):
        return None
    return (
        finding.replace(
            "error|summary_department|", "warning|source_discrepancy|", 1
        )
        + "|independently reported source layers do not reconcile"
    )


def _decision_source_set(decision):
    if decision.operation in {SourceOperation.NO_VALUE, SourceOperation.COA_ROLLUP}:
        return set()
    return set(decision.source_rows) | set(decision.excluded_rows)


def _same_discrepancy_was_previously_blocking(
    history, period_label, target, variance
):
    for result in history:
        for finding in result.get("errors", []):
            if (
                _finding_rule(finding) != "summary_department"
                or _finding_target(finding) != target
                or _finding_period(finding) != period_label
            ):
                continue
            try:
                prior_variance = float(_finding_details(finding)["variance"])
            except (KeyError, ValueError):
                continue
            if abs(prior_variance - variance) <= _tolerance(variance):
                return True
    return False


def _related_hierarchy_is_blocking(checks, detail_ids, coa):
    for finding in checks:
        if not finding.startswith("error|"):
            continue
        if _finding_rule(finding) not in SOURCE_DISCREPANCY_BLOCKING_RULES:
            continue
        target = _finding_target(finding)
        if any(
            target == detail_id
            or _accounts_share_lineage(target, detail_id, coa)
            for detail_id in detail_ids
        ):
            return True
    return False


def _compact_validation_feedback(previous, current, plan, evidence, coa, action):
    findings = _finding_texts(current)
    rules = list(dict.fromkeys(_finding_rule(item) for item in findings))
    guidance = _validation_rule_guidance()
    output = {
        "findings": _compact_finding_contexts(findings, plan, evidence, coa),
        "rule_guidance": [
            {"rule": rule, **guidance[rule]}
            for rule in rules
            if rule in guidance
        ],
    }
    hypothesis = action.get("repair_hypothesis")
    expected_fix = action.get("expected_fix")
    if hypothesis and expected_fix:
        previous_rules = {_finding_rule(item) for item in _finding_texts(previous)}
        current_rules = set(rules)
        output["repair_tracking"] = {
            "repair_hypothesis": hypothesis,
            "expected_fix": expected_fix,
            "resolved_rules": sorted(previous_rules - current_rules),
            "remaining_rules": sorted(previous_rules & current_rules),
            "new_rules": sorted(current_rules - previous_rules),
        }
    return output


def _compact_finding_contexts(findings, plan, evidence, coa):
    """Combine identical findings repeated across selected periods."""
    compact = []
    positions = {}
    for finding in findings:
        context = _finding_context(finding, plan, evidence, coa)
        period = _finding_period(finding)
        key = json.dumps(context, sort_keys=True)
        if key not in positions:
            positions[key] = len(compact)
            if period:
                context["_periods"] = [period]
            compact.append(context)
        elif period:
            periods = compact[positions[key]].setdefault("_periods", [])
            if period not in periods:
                periods.append(period)
    for context in compact:
        periods = context.pop("_periods", [])
        if len(periods) > 1:
            context["periods"] = periods
    return compact


def _finding_period(finding):
    positions = [
        position
        for marker in ("error|", "warning|")
        if (position := str(finding).find(marker)) >= 0
    ]
    if not positions:
        return None
    prefix = str(finding)[: min(positions)].strip()
    return prefix[:-1].strip() if prefix.endswith(":") else None


SUMMARY_LINKS = {
    "S12.total_rooms_revenue": "S1.total_rooms_revenue",
    "S12.total_rooms_expenses": "S1.total_rooms_expenses",
    "S12.total_food_and_beverage_revenue": "S2.total_food_and_beverage_revenue",
    "S12.total_food_and_beverage_expenses": "S2.total_food_and_beverage_expenses",
    "S12.total_other_operated_departments_expenses": "S3.total_other_operated_departments_expenses",
    "S12.total_administrative_and_general_expenses": "S5.total_administrative_and_general_expenses",
    "S12.total_information_and_telecommunications_systems_expenses": "S6.total_information_and_telecommunications_systems_expenses",
    "S12.total_sales_and_marketing_expenses": "S7.total_sales_and_marketing_expenses",
    "S12.total_property_operation_and_maintenance_expenses": "S8.total_property_operation_and_maintenance_expenses",
    "S12.total_utilities_expenses": "S9.total_utilities_expenses",
    "S12.total_management_fees": "S10.total_management_fees",
    "S12.total_non_operating_income_and_expenses": "S11.total_non_operating_income_and_expenses",
}


DERIVED_SUMMARY_LINKS = {
    **SUMMARY_LINKS,
    "S12.total_other_operated_departments_revenue": "S3.total_other_operated_departments_revenue",
    "S12.total_miscellaneous_income": "S4.total_miscellaneous_income",
    "S12.non_operating_income": "S11.non_operating_income",
    "S12.rent": "S11.rent",
    "S12.property_and_other_taxes": "S11.property_and_other_taxes",
    "S12.insurance": "S11.insurance",
    "S12.other": "S11.other",
}


SUMMARY_EQUATIONS = {
    "S12.total_revenue": [
        (1, "S12.total_rooms_revenue"),
        (1, "S12.total_food_and_beverage_revenue"),
        (1, "S12.total_other_operated_departments_revenue"),
        (1, "S12.total_miscellaneous_income"),
    ],
    "S12.total_departmental_expenses": [
        (1, "S12.total_rooms_expenses"),
        (1, "S12.total_food_and_beverage_expenses"),
        (1, "S12.total_other_operated_departments_expenses"),
    ],
    "S12.departmental_profit": [
        (1, "S12.total_revenue"),
        (-1, "S12.total_departmental_expenses"),
    ],
    "S12.total_undistributed_expenses": [
        (1, "S12.total_administrative_and_general_expenses"),
        (1, "S12.total_information_and_telecommunications_systems_expenses"),
        (1, "S12.total_sales_and_marketing_expenses"),
        (1, "S12.total_property_operation_and_maintenance_expenses"),
        (1, "S12.total_utilities_expenses"),
    ],
    "S12.gop": [
        (1, "S12.departmental_profit"),
        (-1, "S12.total_undistributed_expenses"),
    ],
    "S12.income_after_management_fees": [
        (1, "S12.gop"),
        (-1, "S12.total_management_fees"),
    ],
    "S12.total_non_operating_income_and_expenses": [
        (1, "S12.non_operating_income"),
        (1, "S12.rent"),
        (1, "S12.property_and_other_taxes"),
        (1, "S12.insurance"),
        (1, "S12.other"),
    ],
    "S12.ebitda": [
        (1, "S12.income_after_management_fees"),
        (-1, "S12.total_non_operating_income_and_expenses"),
    ],
}


DEPARTMENT_ROOT_IDS = {
    "S1.total_rooms_revenue",
    "S1.total_rooms_expenses",
    "S2.total_food_and_beverage_revenue",
    "S2.total_food_and_beverage_expenses",
    "S3.total_other_operated_departments_revenue",
    "S3.total_other_operated_departments_expenses",
    "S4.total_miscellaneous_income",
    "S5.total_administrative_and_general_expenses",
    "S6.total_information_and_telecommunications_systems_expenses",
    "S7.total_sales_and_marketing_expenses",
    "S8.total_property_operation_and_maintenance_expenses",
    "S9.total_utilities_expenses",
    "S10.total_management_fees",
    "S11.total_non_operating_income_and_expenses",
}

SUMMARY_DERIVED_ROW_REUSE = {
    frozenset({
        "S12.total_rooms_revenue",
        "S12.total_rooms_expenses",
    }),
    frozenset({
        "S12.total_food_and_beverage_revenue",
        "S12.total_food_and_beverage_expenses",
    }),
    frozenset({
        "S12.total_other_operated_departments_revenue",
        "S12.total_other_operated_departments_expenses",
    }),
    frozenset({
        "S12.total_revenue",
        "S12.total_departmental_expenses",
    }),
    frozenset({
        "S12.total_rooms_revenue",
        "S12.total_departmental_expenses",
    }),
    frozenset({
        "S12.total_food_and_beverage_revenue",
        "S12.total_departmental_expenses",
    }),
    frozenset({
        "S12.total_other_operated_departments_revenue",
        "S12.total_departmental_expenses",
    }),
    frozenset({
        "S1.total_rooms_revenue",
        "S1.total_rooms_expenses",
    }),
    frozenset({
        "S2.total_food_and_beverage_revenue",
        "S2.total_food_and_beverage_expenses",
    }),
    frozenset({
        "S3.total_other_operated_departments_revenue",
        "S3.total_other_operated_departments_expenses",
    }),
}


WORKBOOK_MAPPING_PROMPT = resources.files("hotel_pl_normalizer.prompts").joinpath(
    "workbook_mapping.md"
).read_text(encoding="utf-8")


def map_workbook(
    *,
    workbook_id: str,
    requested_period: str,
    periods,
    period_labels: dict[str, str] | None = None,
    evidence: list[dict],
    excluded_sheets: list[str],
    client,
    sheet_routing_context: list[dict] | None = None,
    on_activity=None,
    cancel=None,
) -> MappingResult:
    """Map one complete workbook using model decisions and Python validation."""
    initial_usage_count = len(client.usage_history)
    coa = _load_coa()
    period_labels = period_labels or {"selected": requested_period}
    prompt = _primary_prompt(
        workbook_id,
        requested_period,
        periods,
        period_labels,
        evidence,
        coa,
        excluded_sheets,
        sheet_routing_context,
    )
    validator = WorkbookMappingValidator(
        workbook_id,
        evidence,
        coa,
        period_labels=period_labels,
        sheet_routing_context=sheet_routing_context,
    )
    exhausted = False
    repair_truncated = False
    tool_trace: list[dict] = []
    repair_iteration_limit = 10 if len(period_labels) > 1 else 8
    iteration_limit = repair_iteration_limit + 1
    try:
        client.generate_json_model_with_tools(
            prompt,
            WorkbookMappingCompletion,
            toolset=validator,
            max_iterations=iteration_limit,
            trace=tool_trace,
            on_activity=on_activity,
            cancel=cancel,
        )
    except ProviderToolLoopError:
        exhausted = True
    except ProviderResponseTruncated:
        if not validator.submissions:
            raise
        exhausted = True
        repair_truncated = True
    except ProviderRunCancelled as stop:
        if not validator.submissions:
            raise
        validator.stopped_reason = str(stop)

    if validator.best_plan is not None:
        plan = validator.best_plan
    elif validator.current_plan is not None:
        plan = validator.current_plan
    elif validator.submissions:
        plan = WorkbookSourcePlan.model_validate(validator.submissions[-1])
    else:
        raise RuntimeError("The model never submitted a mapping to validate.")

    preserve_blanks = len(period_labels) > 1
    base_decisions = list(plan.decisions)
    provisional_by_period = {
        period_id: _execute(
            base_decisions,
            evidence,
            coa,
            period_id=period_id,
            preserve_blanks=preserve_blanks,
        )[0]
        for period_id in period_labels
    }
    combined_values = {
        coa_id: sum(
            float(values.get(coa_id) or 0.0)
            for values in provisional_by_period.values()
        )
        for coa_id in coa
    }
    decisions = _rank_generic_venue_decisions(base_decisions, combined_values)
    values_by_period = {}
    checks_by_period = {}
    execution_issues_by_period = {}
    residual_plugs_by_period = {}
    for period_id in period_labels:
        values, execution_issues = _execute(
            decisions,
            evidence,
            coa,
            period_id=period_id,
            preserve_blanks=preserve_blanks,
        )
        residual_plugs_by_period[period_id] = _apply_residual_plugs(
            values,
            coa,
            decisions,
            max_ratio=None,
            allow_large_negative=False,
        )
        unresolved_negative = _unresolved_negative_residuals(
            values, coa, decisions
        )
        values_by_period[period_id] = values
        execution_issues_by_period[period_id] = execution_issues
        checks_by_period[period_id] = _validate(
            _validation_values(values),
            coa,
            decisions,
            plan.strategy,
            plan.review_items,
            validator.summary_only_pushdown_rows,
        )
        checks_by_period[period_id].extend(
            _source_layer_conflict_warnings(
                plan, evidence, values, period_id
            )
        )
        checks_by_period[period_id].extend(
            _source_layer_comparison_issues(
                plan, evidence, values, period_id
            )
        )
        checks_by_period[period_id] = _qualify_source_discrepancies(
            checks_by_period[period_id],
            plan,
            evidence,
            coa,
            validator.history,
            period_labels[period_id],
            execution_issues,
        )
        checks_by_period[period_id] = _replace_unresolved_negative_errors(
            checks_by_period[period_id], unresolved_negative
        )
        checks_by_period[period_id].extend(
            _large_residual_plug_warnings(
                values,
                coa,
                residual_plugs_by_period[period_id],
            )
        )
        checks_by_period[period_id].extend(
            _unsupported_residual_remainder_warnings(
                values,
                coa,
                decisions,
            )
        )
    primary_period_id = next(iter(period_labels))
    checks_by_period[primary_period_id].extend(
        _review_item_blockers(plan.review_items)
    )
    checks_by_period[primary_period_id].extend(
        _review_item_warnings(plan.review_items)
    )
    checks_by_period[primary_period_id].extend(
        _period_completeness_issues(plan, evidence, period_labels)
    )
    checks_by_period[primary_period_id].extend(
        _unused_financial_schedule_issues(
            plan,
            evidence,
            sheet_routing_context,
        )
    )
    has_coverage_gap = any(
        _needs_coverage_review(period_checks)
        for period_checks in checks_by_period.values()
    )
    if has_coverage_gap and not validator.warning_cleanup_attempted:
        checks_by_period[primary_period_id].append(
            "error|coverage_review_not_completed|mapping_session|"
            "the required focused coverage review did not complete"
        )
        if validator.stopped_reason is None:
            validator.stopped_reason = "coverage_review_not_completed"
    values = values_by_period[primary_period_id]
    execution_issues = [
        f"{period_labels[period_id]}: {issue}"
        for period_id, issues in execution_issues_by_period.items()
        for issue in issues
    ]
    checks = [
        f"{check}|period={period_labels[period_id]}"
        for period_id, period_checks in checks_by_period.items()
        for check in period_checks
    ]
    if repair_truncated:
        checks.append(
            "warning|mapping_repair_truncated|mapping_session|"
            "A repair response reached its output-token limit; returned the "
            "best completed mapping from an earlier validation attempt."
        )
    accepted = (
        not execution_issues
        and not any(
            check.startswith("error|")
            for period_checks in checks_by_period.values()
            for check in period_checks
        )
    )
    final_result = {
        "accepted": accepted,
        "errors": [
            check
            for period_checks in checks_by_period.values()
            for check in period_checks
            if check.startswith("error|")
        ] + list(execution_issues),
        "warnings": [
            check
            for period_checks in checks_by_period.values()
            for check in period_checks
            if check.startswith("warning|")
        ],
    }
    outcome = _outcome_from_result(final_result, plan.review_items)
    exceptions = _structured_exceptions(
        checks_by_period, period_labels, plan.review_items
    )
    model_calls = list(client.usage_history[initial_usage_count:])
    session_calls = [call for call in model_calls if call.get("tool_loop")]

    return MappingResult(
        coa=coa,
        values=values,
        values_by_period=values_by_period,
        decisions=list(decisions),
        checks=list(checks),
        checks_by_period=checks_by_period,
        residual_plugs_by_period=residual_plugs_by_period,
        execution_issues=list(execution_issues),
        execution_issues_by_period=execution_issues_by_period,
        review_items=list(plan.review_items),
        accepted=accepted,
        outcome=outcome,
        exceptions=exceptions,
        stopped_reason=validator.stopped_reason,
        session_calls=len(session_calls),
        session_call_ms=[
            int(call.get("duration_ms") or 0) for call in session_calls
        ],
        session_tool_calls=sum(
            int(call.get("tool_calls") or 0) for call in session_calls
        ),
        session_exhausted=exhausted,
        model_calls=model_calls,
        tool_trace=tool_trace,
        mapping_selection={
            "selected_validation_attempt": validator.best_validation_attempt,
            "best_validation_attempt": validator.best_validation_attempt,
            "last_validation_attempt": len(validator.history) or None,
            "best_validation_score": list(validator.best_score or ()),
            "used_best_instead_of_last": (
                validator.best_validation_attempt is not None
                and validator.best_validation_attempt != len(validator.history)
            ),
            "selected_plan_digest": validator.digest(plan),
            "best_plan": _validation_checkpoint_summary(
                validator.best_validation_attempt,
                validator.best_result,
                validator.best_plan,
                validator,
            ),
            "last_plan": _validation_checkpoint_summary(
                len(validator.history) or None,
                validator.history[-1] if validator.history else None,
                validator.current_plan,
                validator,
            ),
            "conditional_guidance": {
                "derived_summary": validator.derived_summary_activated,
                "structural_scenarios": [
                    {
                        "scenario": scenario.value,
                        "source_rows": list(source_rows),
                    }
                    for scenario, source_rows in validator.structural_scenarios.items()
                ],
                "validation_rules": sorted({
                    item["rule"]
                    for attempt in validator.history
                    for item in attempt.get("rule_guidance", [])
                    if item.get("rule")
                }),
            },
            "confirmed_source_discrepancy": any(
                _finding_rule(check) in SOURCE_EXCEPTION_RULES
                for period_checks in checks_by_period.values()
                for check in period_checks
            ),
            "outcome": outcome.value,
            "exceptions": exceptions,
            "stopped_reason": validator.stopped_reason,
            "stopped_validation_attempt": validator.stopped_validation_attempt,
            "repeated_findings": list(validator.repeated_findings),
            "repair_response_truncated": repair_truncated,
            "warning_cleanup": {
                "attempted": validator.warning_cleanup_attempted,
                "outcome": validator.warning_cleanup_outcome,
                "checkpoint_validation_attempt": (
                    validator.warning_cleanup_checkpoint_attempt
                ),
                "selected_validation_attempt": validator.best_validation_attempt,
            },
        },
    )


def _validation_checkpoint_summary(attempt, result, plan, validator):
    if attempt is None or result is None or plan is None:
        return None
    return {
        "validation_attempt": attempt,
        "accepted": bool(result.get("accepted")),
        "submitted_decision_count": int(
            result.get("submitted_decision_count") or len(plan.decisions)
        ),
        "missing_decision_count": int(result.get("missing_decision_count") or 0),
        "error_count": int(result.get("error_count") or 0),
        "warning_count": int(result.get("warning_count") or 0),
        "score": list(result.get("validation_score") or _validation_score(result)),
        "plan_digest": validator.digest(plan),
    }


def _primary_prompt(
    workbook_id,
    requested_period,
    periods,
    period_labels,
    evidence,
    coa,
    excluded_sheets,
    sheet_routing_context=None,
) -> str:
    rows = _group_rows(evidence, period_labels)
    model_coa = {
        coa_id: metadata
        for coa_id, metadata in coa.items()
        if coa_id not in DETERMINISTIC_SUMMARY_ACCOUNTS
    }
    coa_lines = _coa_lines(model_coa)
    period_lines = []
    period_maps = periods if isinstance(periods, dict) else {"selected": periods}
    multi_period = len(period_maps) > 1
    for period_id, period_map in period_maps.items():
        for selection in [
            *period_map.sheet_selections,
            *([period_map.default_selection] if period_map.default_selection else []),
        ]:
            prefix = f"{period_id}|" if multi_period else ""
            period_lines.append(
                f"{prefix}{selection.sheet_name or '*'}|"
                f"column={selection.value_column}|{period_labels[period_id]}"
            )
    payload = {
        "workbook_id": workbook_id,
        "requested_period": requested_period,
        "period_columns": period_lines,
        "coa": coa_lines,
        "coa_hierarchy_equations": _hierarchy_equations(model_coa),
        "summary_equations": _summary_equation_lines(),
        "sheets_excluded_as_nonfinancial": excluded_sheets,
        "sheet_routing_context": list(sheet_routing_context or []),
        "workbook_rows": rows,
    }
    if multi_period:
        payload["selected_periods"] = period_labels
    return "\n\n".join([
        WORKBOOK_MAPPING_PROMPT.strip(),
        "## Workbook Data",
        json.dumps(payload, separators=(",", ":")),
    ])


def _execute(
    decisions,
    evidence,
    coa,
    *,
    period_id: str | None = None,
    preserve_blanks: bool = False,
):
    rows = {item["row_key"]: item for item in evidence}
    values = dict.fromkeys(coa, None if preserve_blanks else 0.0)
    issues = []
    seen = set()
    resolved = set()
    rollups = []
    for decision in decisions:
        if decision.coa_id not in coa:
            issues.append(f"unknown COA id {decision.coa_id}")
            continue
        if decision.coa_id in seen:
            issues.append(f"duplicate decision {decision.coa_id}")
        seen.add(decision.coa_id)
        if decision.operation == SourceOperation.COA_ROLLUP:
            rollups.append(decision)
            continue
        try:
            included = [
                _row_value(rows, key, period_id=period_id)
                for key in decision.source_rows
            ]
            excluded = [
                _row_value(rows, key, period_id=period_id)
                for key in decision.excluded_rows
            ]
            included_numbers = [value for value in included if value is not None]
            excluded_numbers = [value for value in excluded if value is not None]
            op = decision.operation
            if op == SourceOperation.NO_VALUE:
                value = None if preserve_blanks else 0.0
            elif op == SourceOperation.DIRECT:
                if len(included) != 1:
                    raise ValueError("direct requires one row")
                value = included[0]
            elif op == SourceOperation.SUM:
                value = (
                    sum(included_numbers)
                    if included_numbers
                    else (None if preserve_blanks else 0.0)
                )
            elif op == SourceOperation.ADJUSTED_SUBTOTAL:
                value = (
                    sum(included_numbers) - sum(excluded_numbers)
                    if included_numbers or excluded_numbers
                    else (None if preserve_blanks else 0.0)
                )
            elif op == SourceOperation.NEGATE:
                value = (
                    -sum(included_numbers)
                    if included_numbers
                    else (None if preserve_blanks else 0.0)
                )
            elif op == SourceOperation.RATIO:
                if len(included) != 2:
                    raise ValueError("ratio requires two rows")
                if any(value is None for value in included):
                    value = None if preserve_blanks else 0.0
                elif included[1] == 0:
                    raise ValueError("ratio requires two rows and nonzero denominator")
                else:
                    value = included[0] / included[1]
            elif op == SourceOperation.PRODUCT:
                if any(item is None for item in included):
                    value = None if preserve_blanks else 0.0
                else:
                    value = 1.0
                    for item in included:
                        value *= item
            elif op == SourceOperation.SCALE:
                value = (
                    sum(included_numbers) * float(decision.scale_factor)
                    if included_numbers
                    else (None if preserve_blanks else 0.0)
                )
            values[decision.coa_id] = (
                None if value is None else float(value)
            )
            if (
                decision.coa_id == "S12.occupancy"
                and values[decision.coa_id] is not None
                and 1.0 < values[decision.coa_id] <= 100.0
                and decision.operation == SourceOperation.DIRECT
                and not _source_value_is_percentage_formatted(
                    rows,
                    decision.source_rows[0],
                    period_id=period_id,
                )
            ):
                values[decision.coa_id] /= 100.0
            resolved.add(decision.coa_id)
        except (KeyError, TypeError, ValueError) as exc:
            issues.append(f"{decision.coa_id}: {exc}")
    pending = {decision.coa_id: decision for decision in rollups}
    while pending:
        progressed = False
        for coa_id in list(pending):
            dependencies = _coa_rollup_dependencies(coa_id)
            if dependencies is None:
                issues.append(f"{coa_id}: unsupported coa_rollup target")
                del pending[coa_id]
                progressed = True
                continue
            if not all(source in resolved for _, source in dependencies):
                continue
            available = [
                (sign, values.get(source)) for sign, source in dependencies
            ]
            numeric = [
                (sign, float(value))
                for sign, value in available
                if value is not None
            ]
            values[coa_id] = (
                sum(sign * value for sign, value in numeric)
                if numeric
                else (None if preserve_blanks else 0.0)
            )
            resolved.add(coa_id)
            del pending[coa_id]
            progressed = True
        if not progressed:
            for coa_id in pending:
                dependencies = _coa_rollup_dependencies(coa_id) or []
                missing_dependencies = [
                    source for _, source in dependencies if source not in resolved
                ]
                issues.append(
                    f"{coa_id}: unresolved coa_rollup dependencies "
                    + ",".join(missing_dependencies)
                )
            break
    _apply_deterministic_summary_calculations(values, preserve_blanks)
    missing = sorted(
        (set(coa) - DETERMINISTIC_SUMMARY_ACCOUNTS) - seen
    )
    if missing:
        issues.append(f"missing decisions: {','.join(missing)}")
    return values, issues


def _apply_deterministic_summary_calculations(values, preserve_blanks):
    if not DETERMINISTIC_SUMMARY_ACCOUNTS.intersection(values):
        return
    total_revenue = values.get("S12.total_revenue")
    ebitda = values.get("S12.ebitda")
    if total_revenue is None:
        reserve = None if preserve_blanks else 0.0
    else:
        reserve = 0.04 * float(total_revenue)
    if "S12.ffe_reserve" in values:
        values["S12.ffe_reserve"] = reserve
    if ebitda is None or reserve is None:
        noi = None if preserve_blanks else 0.0
    else:
        noi = float(ebitda) - float(reserve)
    if "S12.noi" in values:
        values["S12.noi"] = noi


def _coa_rollup_dependencies(coa_id):
    if coa_id in DERIVED_SUMMARY_LINKS:
        return [(1, DERIVED_SUMMARY_LINKS[coa_id])]
    return SUMMARY_EQUATIONS.get(coa_id)


def _rank_generic_venue_decisions(decisions, values):
    """Rank venue pairs by their combined calculated food and beverage revenue."""
    by_id = {decision.coa_id: decision for decision in decisions}
    if any(coa_id not in by_id for coa_id in GENERIC_VENUE_IDS):
        return list(decisions)

    ranked = sorted(
        enumerate(GENERIC_VENUE_SLOTS),
        key=lambda item: (
            all(
                by_id[coa_id].operation == SourceOperation.NO_VALUE
                for coa_id in item[1]
            ),
            -sum(float(values.get(coa_id, 0.0)) for coa_id in item[1]),
            item[0],
        ),
    )
    target_by_source = {}
    venue_name_by_source = {}
    for target_index, (_, source_slot) in enumerate(ranked):
        venue_name = next(
            (
                by_id[coa_id].venue_name
                for coa_id in source_slot
                if by_id[coa_id].venue_name
            ),
            None,
        )
        for source_id, target_id in zip(
            source_slot, GENERIC_VENUE_SLOTS[target_index]
        ):
            target_by_source[source_id] = target_id
            venue_name_by_source[source_id] = venue_name

    return [
        decision.model_copy(
            update={
                "coa_id": target_by_source[decision.coa_id],
                "venue_name": venue_name_by_source[decision.coa_id],
            }
        )
        if decision.coa_id in target_by_source
        else decision
        for decision in decisions
    ]


def _row_value(rows, row_key, *, period_id: str | None = None):
    if row_key not in rows:
        raise KeyError(f"unknown row {row_key}")
    row = rows[row_key]
    values = row.get("selected_values") or {}
    value = (
        values.get(period_id)
        if period_id is not None and period_id in values
        else row.get("selected_value")
    )
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError(f"row {row_key} has no selected numeric value")
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().replace(",", "").replace("$", "")
    if text.startswith("(") and text.endswith(")"):
        text = f"-{text[1:-1]}"
    return float(text)


def _source_value_is_percentage_formatted(
    rows, row_key, *, period_id: str | None = None
) -> bool:
    """Return whether an Excel percentage is already stored as a ratio."""
    row = rows.get(row_key) or {}
    formats = row.get("selected_value_formats") or {}
    number_format = (
        formats.get(period_id)
        if period_id is not None and period_id in formats
        else row.get("selected_value_format")
    )
    return isinstance(number_format, str) and "%" in number_format


def _validation_values(values):
    """Existing checks treat a blank as absent/zero without changing the output."""
    return {
        coa_id: (0.0 if value is None else float(value))
        for coa_id, value in values.items()
    }


def _normalized_evidence_label(value) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(value or "").casefold()).strip()


def _evidence_period_value(row, period_id):
    values = row.get("selected_values") or {}
    value = values.get(period_id)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _period_completeness_issues(plan, evidence, period_labels):
    """Block only when a demonstrably equivalent row fills a selected-period gap.

    A same-label candidate must contain the missing period and agree in every
    overlapping selected period. This avoids treating a legitimate zero or a
    differently scoped schedule as omitted detail.
    """
    if len(period_labels) < 2:
        return []
    rows = {str(row.get("row_key") or ""): row for row in evidence}
    by_label: dict[str, list[dict]] = {}
    for row in evidence:
        label = _normalized_evidence_label(row.get("label"))
        if label:
            by_label.setdefault(label, []).append(row)
    all_cited = {
        row_key
        for decision in plan.decisions
        for row_key in [*decision.source_rows, *decision.excluded_rows]
    }
    issues = []
    seen = set()
    for decision in plan.decisions:
        if decision.operation in {SourceOperation.NO_VALUE, SourceOperation.COA_ROLLUP}:
            continue
        for selected_key in [*decision.source_rows, *decision.excluded_rows]:
            selected = rows.get(selected_key)
            if not selected:
                continue
            label = _normalized_evidence_label(selected.get("label"))
            if not label:
                continue
            selected_values = {
                period_id: _evidence_period_value(selected, period_id)
                for period_id in period_labels
            }
            populated = {
                period_id: value
                for period_id, value in selected_values.items()
                if value is not None
            }
            missing_periods = [
                period_id
                for period_id, value in selected_values.items()
                if value is None
            ]
            if not populated or not missing_periods:
                continue
            for candidate in by_label.get(label, []):
                candidate_key = str(candidate.get("row_key") or "")
                if not candidate_key or candidate_key in all_cited:
                    continue
                candidate_values = {
                    period_id: _evidence_period_value(candidate, period_id)
                    for period_id in period_labels
                }
                fills = [
                    period_id
                    for period_id in missing_periods
                    if candidate_values[period_id] is not None
                    and abs(candidate_values[period_id]) > 0.005
                ]
                if not fills:
                    continue
                overlaps = [
                    period_id
                    for period_id in populated
                    if candidate_values[period_id] is not None
                ]
                if not overlaps or any(
                    abs(candidate_values[period_id] - populated[period_id])
                    > _tolerance(populated[period_id])
                    for period_id in overlaps
                ):
                    continue
                fingerprint = (decision.coa_id, selected_key, candidate_key)
                if fingerprint in seen:
                    continue
                seen.add(fingerprint)
                labels = ", ".join(period_labels[period_id] for period_id in fills)
                issues.append(
                    f"error|period_detail_available|{decision.coa_id}|"
                    f"selected_row={selected_key}|candidate_row={candidate_key}|"
                    f"missing_periods={labels}|candidate matches the selected row "
                    "where both are populated and supplies missing selected-period detail"
                )
    return issues


def _detail_collapse_issue(plan, evidence):
    """Reject a suspicious parent-only plan for a substantively rich workbook.

    This coarse safeguard uses only the rows already routed to mapping. It does
    not need to know which department owns a sheet or where a section begins.
    """
    rich_rows = [
        row
        for row in evidence
        if str(row.get("label") or "").strip()
        and any(
            isinstance(value, (int, float))
            and not isinstance(value, bool)
            and abs(float(value)) > 0.005
            for value in (row.get("selected_values") or {}).values()
        )
    ]
    rich_sheets = {row["row_key"].rsplit("!", 1)[0] for row in rich_rows}
    if len(rich_rows) < 120 and len(rich_sheets) < 8:
        return None

    active_detail = {
        decision.coa_id
        for decision in plan.decisions
        if decision.operation != SourceOperation.NO_VALUE
        and not decision.coa_id.startswith("S12.")
        and decision.coa_id not in DEPARTMENT_ROOT_IDS
        and any(
            row_key.rsplit("!", 1)[0] in rich_sheets
            for row_key in decision.source_rows
        )
    }

    minimum = max(12, 2 * len(rich_sheets))
    if len(active_detail) >= minimum or len(active_detail) >= len(rich_rows) * 0.12:
        return None
    return (
        "detail_mapping_collapsed: routing supplied "
        f"{len(rich_sheets)} substantive sheet(s) with "
        f"{len(rich_rows)} non-zero labelled rows, but the plan cites only "
        f"{len(active_detail)} detailed COA account(s) from them; "
        "map identifiable child accounts instead of collapsing the workbook "
        "mostly to parents/no_value"
    )


def _unused_financial_schedule_issues(plan, evidence, sheet_routing_context):
    """Require every routed department P&L to be used or explained.

    This is intentionally structural. Routing supplies the sheet role, evidence
    supplies whether the selected periods contain financial amounts, and the
    mapping supplies either a citation or an explicit duplicate/supporting
    explanation. No account-label semantics are inferred here.
    """
    active_rows_by_sheet: dict[str, list[str]] = {}
    for row in evidence:
        values = row.get("selected_values") or {
            "selected": row.get("selected_value")
        }
        if not any(
            isinstance(value, (int, float))
            and not isinstance(value, bool)
            and abs(float(value)) > 0.005
            for value in values.values()
        ):
            continue
        row_key = str(row.get("row_key") or "")
        if "!" not in row_key:
            continue
        sheet_name = row_key.rsplit("!", 1)[0]
        active_rows_by_sheet.setdefault(sheet_name, []).append(row_key)

    cited_rows = {
        row_key
        for decision in plan.decisions
        for row_key in [*decision.source_rows, *decision.excluded_rows]
    }
    supporting_notes = plan.strategy.duplicate_or_supporting_schedules
    routed_sheet_names = [
        str(item.get("sheet_name") or "").strip()
        for item in sheet_routing_context or []
        if str(item.get("sheet_name") or "").strip()
    ]
    issues = []
    for routed in sheet_routing_context or []:
        role = getattr(routed.get("role"), "value", routed.get("role"))
        included = bool(routed.get("include_as_financial_evidence"))
        sheet_name = str(routed.get("sheet_name") or "").strip()
        active_rows = active_rows_by_sheet.get(sheet_name, [])
        if (
            role != "department_p_and_l"
            or not included
            or not sheet_name
            or not active_rows
        ):
            continue
        if any(row_key in cited_rows for row_key in active_rows):
            continue
        scope_review = next(
            (
                item
                for item in getattr(plan, "review_items", [])
                if _review_kind(item) == "scope_exception"
                and any(
                    row_key.rsplit("!", 1)[0].casefold() == sheet_name.casefold()
                    for row_key in (
                        item.source_rows
                        if isinstance(item, MappingReviewItem)
                        else item.get("source_rows", [])
                    )
                    if "!" in row_key
                )
            ),
            None,
        )
        if scope_review is not None:
            continue
        if _strategy_documents_schedule(
            supporting_notes,
            sheet_name,
            routed_sheet_names,
        ):
            continue
        context_rows = ",".join(active_rows[:12])
        issues.append(
            f"error|unused_financial_schedule|{sheet_name}|"
            "routing classified this selected-period financial sheet as "
            "department_p_and_l, but no mapping decision cites it and "
            "duplicate_or_supporting_schedules does not explain which schedule "
            f"supersedes it|source_rows={context_rows}"
        )
    return issues


def _strategy_documents_schedule(entries, sheet_name, routed_sheet_names):
    """Whether a note names the unused sheet and its superseding schedule."""
    target_pattern = _sheet_name_pattern(sheet_name)
    superseding_patterns = [
        _sheet_name_pattern(candidate)
        for candidate in routed_sheet_names
        if candidate.casefold() != sheet_name.casefold()
    ]
    for entry in entries or []:
        text = str(entry).strip()
        if target_pattern.search(text) and any(
            pattern.search(text) for pattern in superseding_patterns
        ):
            return True
    return False


def _sheet_name_pattern(sheet_name):
    return re.compile(
        rf"(?<![\w]){re.escape(sheet_name)}(?![\w])",
        re.IGNORECASE,
    )


def _validate(
    values,
    coa,
    decisions,
    strategy,
    review_items=None,
    summary_only_pushdown_rows=None,
):
    issues = _source_row_reuse_issues(
        decisions,
        coa,
        review_items or [],
        summary_only_pushdown_rows or set(),
    )
    by_id = {item.coa_id: item for item in decisions}
    children = _children_by_parent(coa)
    for parent, child_ids in children.items():
        decision = by_id.get(parent)
        if decision is None:
            issues.append(f"error|coverage|{parent}|missing parent decision")
            continue
        child_total = sum(values.get(child, 0.0) for child in child_ids)
        variance = values.get(parent, 0.0) - child_total
        tolerance = _tolerance(values.get(parent, 0.0))
        residual_ids = [
            child for child in child_ids if _is_residual(coa.get(child, {}))
        ]
        active_children = [
            child
            for child in child_ids
            if by_id.get(child)
            and by_id[child].operation != SourceOperation.NO_VALUE
        ]
        coverage = decision.child_coverage
        if coverage == ChildCoverage.COMPLETE:
            if abs(variance) > tolerance:
                if _is_small_source_supported_conflict(
                    values.get(parent, 0.0),
                    variance,
                    by_id,
                    [parent, *active_children],
                ):
                    issues.append(
                        f"warning|small_source_reconciliation_difference|{parent}|"
                        f"rule=hierarchy_complete|parent={values.get(parent,0.0):.2f}|"
                        f"children={child_total:.2f}|variance={variance:.2f}|"
                        "reported parent and cited children differ; preserve both and review"
                    )
                else:
                    issues.append(
                        f"error|hierarchy_complete|{parent}|parent={values.get(parent,0.0):.2f}|"
                        f"children={child_total:.2f}|variance={variance:.2f}|"
                        f"child_ids={','.join(child_ids)}"
                    )
        elif coverage == ChildCoverage.PARTIAL:
            if residual_ids:
                if abs(variance) > 0.005:
                    issues.append(
                        f"error|hierarchy_partial_with_residual|{parent}|"
                        f"parent={values.get(parent,0.0):.2f}|children={child_total:.2f}|"
                        f"variance={variance:.2f}|residual_ids={','.join(residual_ids)}"
                    )
            elif abs(variance) > tolerance:
                issues.append(
                    f"warning|source_detail_incomplete|{parent}|partial children "
                    "and no legitimate residual child|"
                    f"parent={values.get(parent,0.0):.2f}|children={child_total:.2f}|"
                    f"variance={variance:.2f}|preserve parent and review"
                )
        elif coverage == ChildCoverage.NOT_PRESENT:
            if active_children:
                issues.append(
                    f"error|coverage_inconsistent|{parent}|marked not_present but "
                    f"active children={','.join(active_children)}"
                )
        else:
            issues.append(
                f"warning|coverage_unspecified|{parent}|parent must declare "
                "complete, partial, or not_present child coverage"
            )
        if (
            decision.operation == SourceOperation.NO_VALUE
            and any(abs(values.get(child, 0.0)) > tolerance for child in child_ids)
        ):
            issues.append(
                f"error|parent_no_value_with_children|{parent}|"
                f"active_children={','.join(active_children)}"
            )

    for summary_id, department_id in SUMMARY_LINKS.items():
        summary_decision = by_id.get(summary_id)
        if (
            summary_decision
            and summary_decision.operation == SourceOperation.COA_ROLLUP
        ):
            continue
        department_decision = by_id.get(department_id)
        if (
            department_decision
            and department_decision.operation == SourceOperation.NO_VALUE
            and department_decision.child_coverage == ChildCoverage.NOT_PRESENT
        ):
            continue
        _append_equation_issue(
            issues,
            "summary_department",
            summary_id,
            values.get(summary_id, 0.0),
            values.get(department_id, 0.0),
            department_id,
            source_supported=_all_source_supported(
                by_id, [summary_id, department_id]
            ),
        )
    departmental_expense_links = (
        ("S12.total_rooms_expenses", "S1.total_rooms_expenses"),
        (
            "S12.total_food_and_beverage_expenses",
            "S2.total_food_and_beverage_expenses",
        ),
        (
            "S12.total_other_operated_departments_expenses",
            "S3.total_other_operated_departments_expenses",
        ),
    )
    total_departmental_decision = by_id.get("S12.total_departmental_expenses")
    if (
        not (
            total_departmental_decision
            and total_departmental_decision.operation
            == SourceOperation.COA_ROLLUP
        )
        and all(
            (decision := by_id.get(department_id)) is not None
            and not (
                decision.operation == SourceOperation.NO_VALUE
                and decision.child_coverage == ChildCoverage.NOT_PRESENT
            )
            for _, department_id in departmental_expense_links
        )
    ):
        detail_ids = [department_id for _, department_id in departmental_expense_links]
        _append_equation_issue(
            issues,
            "summary_department",
            "S12.total_departmental_expenses",
            values.get("S12.total_departmental_expenses", 0.0),
            sum(values.get(department_id, 0.0) for department_id in detail_ids),
            " + ".join(detail_ids),
            source_supported=_all_source_supported(
                by_id,
                ["S12.total_departmental_expenses", *detail_ids],
            ),
        )
    if strategy.summary_mode != SummaryMode.DERIVED:
        _validate_ood_misc_summary(values, strategy, issues, by_id)
    for target, terms in SUMMARY_EQUATIONS.items():
        target_decision = by_id.get(target)
        if (
            target_decision
            and target_decision.operation == SourceOperation.COA_ROLLUP
        ):
            continue
        expected = sum(sign * values.get(source, 0.0) for sign, source in terms)
        _append_equation_issue(
            issues,
            "summary_math",
            target,
            values.get(target, 0.0),
            expected,
            _format_equation_terms(terms),
            source_supported=_all_source_supported(
                by_id, [target, *(source for _, source in terms)]
            ),
        )
    _append_equation_issue(
        issues,
        "kpi_math",
        "S12.occupancy",
        values.get("S12.occupancy", 0.0),
        _safe_ratio(values.get("S12.rooms_sold", 0.0), values.get("S12.rooms_available", 0.0)),
        "rooms_sold / rooms_available",
        tolerance=0.001,
    )
    _append_equation_issue(
        issues,
        "kpi_math",
        "S12.adr",
        values.get("S12.adr", 0.0),
        _safe_ratio(values.get("S12.total_rooms_revenue", 0.0), values.get("S12.rooms_sold", 0.0)),
        "rooms_revenue / rooms_sold",
        tolerance=0.05,
    )
    _append_equation_issue(
        issues,
        "kpi_math",
        "S12.revpar",
        values.get("S12.revpar", 0.0),
        _safe_ratio(values.get("S12.total_rooms_revenue", 0.0), values.get("S12.rooms_available", 0.0)),
        "rooms_revenue / rooms_available",
        tolerance=0.05,
    )
    if values.get("S11.non_operating_income", 0.0) > 1.0:
        issues.append(
            "error|non_operating_sign|S11.non_operating_income|normalized "
            "non-operating income must be zero or negative"
        )
    return issues


def _source_row_reuse_issues(
    decisions, coa, review_items, summary_only_pushdown_rows=frozenset()
):
    issues = []
    uses = {}
    all_uses = {}
    for decision in decisions:
        included = set(decision.source_rows)
        excluded = set(decision.excluded_rows)
        for row in sorted(
            row for row in included if decision.source_rows.count(row) > 1
        ):
            issues.append(
                f"error|source_row_repeated|{decision.coa_id}|row={row}|"
                "the same source row appears more than once in one calculation"
            )
        for row in sorted(included & excluded):
            issues.append(
                f"error|source_row_included_and_excluded|{decision.coa_id}|row={row}"
            )
        for row in included:
            uses.setdefault(row, []).append(decision)
        for row in included | excluded:
            all_uses.setdefault(row, []).append(decision)

    for row, row_decisions in uses.items():
        reviewed_adjustment = _reviewed_adjustment_allows(
            row,
            all_uses.get(row, row_decisions),
            review_items,
            coa,
        )
        reviewed_pushdown = _reviewed_summary_pushdown_allows(
            row,
            row_decisions,
            review_items,
            coa,
            summary_only_pushdown_rows,
        )
        invalid_pairs = [
            (left, right)
            for left, right in combinations(row_decisions, 2)
            if not _source_row_reuse_allowed(left, right, coa)
            and not reviewed_adjustment
            and not reviewed_pushdown
        ]
        if invalid_pairs:
            coa_ids = sorted({
                decision.coa_id
                for pair in invalid_pairs
                for decision in pair
            })
            issues.append(
                f"error|source_row_double_count|{row}|"
                f"coa_ids={','.join(coa_ids)}|"
                "same included source row is assigned to unrelated accounts"
            )
    return issues


def _source_row_reuse_allowed(left, right, coa):
    derived_operations = {
        SourceOperation.RATIO,
        SourceOperation.PRODUCT,
        SourceOperation.SCALE,
    }
    if left.operation in derived_operations or right.operation in derived_operations:
        return True
    if (
        frozenset({left.coa_id, right.coa_id}) in SUMMARY_DERIVED_ROW_REUSE
        and SourceOperation.ADJUSTED_SUBTOTAL
        in {left.operation, right.operation}
    ):
        return True
    if _same_department_revenue_expense_derivation(left, right, coa):
        return True
    if _accounts_share_lineage(left.coa_id, right.coa_id, coa):
        return True
    if _accounts_share_summary_department_lineage(
        left.coa_id, right.coa_id, coa
    ):
        return True
    if _accounts_share_dependency_path(left.coa_id, right.coa_id, coa):
        return True
    if any(
        {left.coa_id, right.coa_id} == {target, source}
        for target, terms in SUMMARY_EQUATIONS.items()
        for _, source in terms
    ):
        return True
    return False


def _same_department_revenue_expense_derivation(left, right, coa):
    """Allow one department's revenue row in a revenue-minus-profit expense."""
    if SourceOperation.ADJUSTED_SUBTOTAL not in {left.operation, right.operation}:
        return False
    left_department = str(coa.get(left.coa_id, {}).get("department") or "")
    right_department = str(coa.get(right.coa_id, {}).get("department") or "")
    if not left_department or left_department != right_department:
        return False

    def side(decision):
        item = coa.get(decision.coa_id, {})
        text = " ".join(
            str(value or "")
            for value in (
                decision.coa_id,
                item.get("account_name"),
                item.get("full_hierarchy_path"),
                item.get("hierarchy_path"),
            )
        ).lower()
        if "revenue" in text:
            return "revenue"
        if "expense" in text:
            return "expense"
        return None

    return {side(left), side(right)} == {"revenue", "expense"}


def _adjustment_accounts_directly_related(left, right, coa):
    if _accounts_share_lineage(left, right, coa):
        return True
    if _accounts_share_summary_department_lineage(left, right, coa):
        return True
    return any(
        {left, right} == {target, source}
        for target, terms in SUMMARY_EQUATIONS.items()
        for _, source in terms
    )


def _adjustment_accounts_connected(coa_ids, coa):
    remaining = set(coa_ids)
    if not remaining:
        return False
    reached = {remaining.pop()}
    while remaining:
        newly_reached = {
            candidate
            for candidate in remaining
            if any(
                _adjustment_accounts_directly_related(candidate, known, coa)
                for known in reached
            )
        }
        if not newly_reached:
            return False
        reached.update(newly_reached)
        remaining.difference_update(newly_reached)
    return True


def _reviewed_adjustment_allows(row, row_decisions, review_items, coa):
    """Allow rare connected reuse when decisions and human review prove it."""
    coa_ids = {decision.coa_id for decision in row_decisions}
    has_adjustment_operation = any(
        row in decision.excluded_rows
        or (
            decision.operation == SourceOperation.ADJUSTED_SUBTOTAL
            and row in decision.source_rows
        )
        for decision in row_decisions
    )
    if not has_adjustment_operation or not _adjustment_accounts_connected(coa_ids, coa):
        return False
    return any(
        item.kind == "unusual_convention"
        and row in item.source_rows
        and coa_ids <= set(item.coa_ids)
        for item in review_items
    )


def _reviewed_summary_pushdown_allows(
    row, row_decisions, review_items, coa, summary_only_pushdown_rows
):
    """Allow one cited Summary-only row down its linked department path."""
    if row not in summary_only_pushdown_rows:
        return False
    coa_ids = {decision.coa_id for decision in row_decisions}
    if not coa_ids or not any(coa_id.startswith("S12.") for coa_id in coa_ids):
        return False
    if not any(not coa_id.startswith("S12.") for coa_id in coa_ids):
        return False
    if not _adjustment_accounts_connected(coa_ids, coa):
        return False
    return any(
        item.kind == "unusual_convention"
        and row in item.source_rows
        and coa_ids <= set(item.coa_ids)
        for item in review_items
    )


def _enrich_derived_summary_review_item(plan, source_rows):
    if plan.strategy.summary_mode != SummaryMode.DERIVED:
        return plan
    message = (
        "No conventional Summary section was found; Summary accounts were "
        "derived from mapped department totals and checked against available "
        "whole-P&L totals."
    )
    if any(item.message == message for item in plan.review_items):
        return plan
    review = MappingReviewItem(
        kind="unusual_convention",
        message=message,
        coa_ids=["S12.total_revenue"],
        source_rows=list(source_rows),
    )
    return plan.model_copy(update={"review_items": [*plan.review_items, review]})


def _enrich_adjustment_review_items(plan, coa):
    """Add mechanically known affected accounts to matching adjustment reviews."""
    enriched = []
    for item in plan.review_items:
        coa_ids = list(item.coa_ids)
        if item.kind == "unusual_convention":
            for row in item.source_rows:
                row_decisions = [
                    decision
                    for decision in plan.decisions
                    if row in decision.source_rows or row in decision.excluded_rows
                ]
                actual_ids = {decision.coa_id for decision in row_decisions}
                has_adjustment_operation = any(
                    row in decision.excluded_rows
                    or (
                        decision.operation == SourceOperation.ADJUSTED_SUBTOTAL
                        and row in decision.source_rows
                    )
                    for decision in row_decisions
                )
                if (
                    has_adjustment_operation
                    and actual_ids.intersection(coa_ids)
                    and _adjustment_accounts_connected(actual_ids, coa)
                ):
                    coa_ids.extend(
                        decision.coa_id
                        for decision in row_decisions
                        if decision.coa_id not in coa_ids
                    )
        enriched.append(item.model_copy(update={"coa_ids": coa_ids}))
    return plan.model_copy(update={"review_items": enriched})


def _accounts_share_summary_department_lineage(left, right, coa):
    for summary_id, department_root in DERIVED_SUMMARY_LINKS.items():
        left_is_summary = left == summary_id or _accounts_share_lineage(
            summary_id, left, coa
        )
        right_is_summary = right == summary_id or _accounts_share_lineage(
            summary_id, right, coa
        )
        left_is_department = left == department_root or _accounts_share_lineage(
            department_root, left, coa
        )
        right_is_department = right == department_root or _accounts_share_lineage(
            department_root, right, coa
        )
        linked_department = str(
            coa.get(department_root, {}).get("department") or ""
        )
        left_is_linked_department = bool(linked_department) and str(
            coa.get(left, {}).get("department") or ""
        ) == linked_department
        right_is_linked_department = bool(linked_department) and str(
            coa.get(right, {}).get("department") or ""
        ) == linked_department
        if left_is_summary and (
            right_is_department or right_is_linked_department
        ):
            return True
        if right_is_summary and (
            left_is_department or left_is_linked_department
        ):
            return True
    return False


def _accounts_share_dependency_path(left, right, coa):
    """Whether one account feeds the other through known rollup equations.

    Edges point only from a child/component to its parent/total. Two siblings
    may therefore share a downstream total without becoming mutually reusable.
    """
    edges = {}
    for coa_id, metadata in coa.items():
        parent = str(metadata.get("parent_coa_id") or "")
        if parent:
            edges.setdefault(coa_id, set()).add(parent)
    for summary_id, department_id in DERIVED_SUMMARY_LINKS.items():
        edges.setdefault(department_id, set()).add(summary_id)
    for target, terms in SUMMARY_EQUATIONS.items():
        for _sign, source in terms:
            edges.setdefault(source, set()).add(target)

    def reaches(source, target):
        pending = [source]
        visited = {source}
        while pending:
            current = pending.pop()
            for candidate in edges.get(current, ()):
                if candidate == target:
                    return True
                if candidate not in visited:
                    visited.add(candidate)
                    pending.append(candidate)
        return False

    return reaches(left, right) or reaches(right, left)


def _accounts_share_lineage(left, right, coa):
    def ancestors(coa_id):
        output = set()
        parent = coa.get(coa_id, {}).get("parent_coa_id")
        while parent and parent not in output:
            output.add(parent)
            parent = coa.get(parent, {}).get("parent_coa_id")
        return output

    return left in ancestors(right) or right in ancestors(left)


def _all_source_supported(by_id, coa_ids):
    return bool(coa_ids) and all(
        (decision := by_id.get(coa_id)) is not None
        and decision.operation != SourceOperation.NO_VALUE
        and bool(decision.source_rows)
        for coa_id in coa_ids
    )


def _is_small_source_supported_conflict(actual, variance, by_id, coa_ids):
    return (
        _all_source_supported(by_id, coa_ids)
        and abs(variance) <= max(_tolerance(actual), abs(actual) * 0.0001)
    )


def _children_by_parent(coa):
    children = {}
    for item in coa.values():
        if item.get("parent_coa_id"):
            children.setdefault(item["parent_coa_id"], []).append(item["coa_id"])
    return children


def _validate_ood_misc_summary(values, strategy, issues, by_id):
    summary_ood = values.get("S12.total_other_operated_departments_revenue", 0.0)
    summary_misc = values.get("S12.total_miscellaneous_income", 0.0)
    detail_ood = values.get("S3.total_other_operated_departments_revenue", 0.0)
    detail_misc = values.get("S4.total_miscellaneous_income", 0.0)
    mode = strategy.ood_misc_summary_mode
    # Standard COA categories must remain comparable even when the operator's
    # Summary combines them. A combined presentation may later qualify as a
    # structured source-presentation exception, but the strategy flag alone
    # must never waive these same-category checks.
    _append_equation_issue(
        issues,
        "summary_department",
        "S12.total_other_operated_departments_revenue",
        summary_ood,
        detail_ood,
        "S3.total_other_operated_departments_revenue",
        source_supported=_all_source_supported(
            by_id,
            [
                "S12.total_other_operated_departments_revenue",
                "S3.total_other_operated_departments_revenue",
            ],
        ),
    )
    _append_equation_issue(
        issues,
        "summary_department",
        "S12.total_miscellaneous_income",
        summary_misc,
        detail_misc,
        "S4.total_miscellaneous_income",
        source_supported=_all_source_supported(
            by_id,
            [
                "S12.total_miscellaneous_income",
                "S4.total_miscellaneous_income",
            ],
        ),
    )
    if mode == OodMiscSummaryMode.COMBINED_IN_OOD:
        _append_equation_issue(issues, "summary_combined_ood_misc", "S12.total_other_operated_departments_revenue", summary_ood, detail_ood + detail_misc, "S3 OOD + S4 Misc", source_supported=_all_source_supported(by_id, ["S12.total_other_operated_departments_revenue", "S3.total_other_operated_departments_revenue", "S4.total_miscellaneous_income"]))
        _append_equation_issue(
            issues,
            "summary_combined_ood_misc_inactive_bucket",
            "S12.total_miscellaneous_income",
            summary_misc,
            0.0,
            "0 when OOD contains combined OOD + Misc",
        )
    elif mode == OodMiscSummaryMode.COMBINED_IN_MISC:
        _append_equation_issue(issues, "summary_combined_ood_misc", "S12.total_miscellaneous_income", summary_misc, detail_ood + detail_misc, "S3 OOD + S4 Misc", source_supported=_all_source_supported(by_id, ["S12.total_miscellaneous_income", "S3.total_other_operated_departments_revenue", "S4.total_miscellaneous_income"]))
        _append_equation_issue(
            issues,
            "summary_combined_ood_misc_inactive_bucket",
            "S12.total_other_operated_departments_revenue",
            summary_ood,
            0.0,
            "0 when Misc contains combined OOD + Misc",
        )
    elif mode == OodMiscSummaryMode.UNKNOWN:
        severity = (
            "error"
            if any(
                abs(value) > 0.005
                for value in (summary_ood, summary_misc, detail_ood, detail_misc)
            )
            else "warning"
        )
        issues.append(
            f"{severity}|ood_misc_summary_mode_unknown|S12|model must determine "
            "whether Summary presents OOD and Misc separately or combined"
        )


def _append_equation_issue(
    issues,
    rule,
    target,
    actual,
    expected,
    expression,
    tolerance=None,
    *,
    source_supported=False,
):
    allowed = _tolerance(actual) if tolerance is None else tolerance
    variance = actual - expected
    if abs(variance) > allowed:
        if source_supported and abs(variance) <= max(allowed, abs(actual) * 0.0001):
            issues.append(
                f"warning|small_source_reconciliation_difference|{target}|rule={rule}|"
                f"actual={actual:.4f}|expected={expected:.4f}|"
                f"variance={variance:.4f}|equation={expression}|"
                "reported values differ slightly; preserve them and review"
            )
        else:
            issues.append(
                f"error|{rule}|{target}|actual={actual:.4f}|expected={expected:.4f}|"
                f"variance={variance:.4f}|equation={expression}"
            )


def _safe_ratio(numerator, denominator):
    return 0.0 if not denominator else numerator / denominator


def _tolerance(value):
    return max(5.0, abs(value) * 0.00001)


def _format_equation_terms(terms):
    return " ".join(
        ("+ " if index and sign > 0 else "- " if sign < 0 else "") + coa_id
        for index, (sign, coa_id) in enumerate(terms)
    )


def _is_residual(item):
    return str(item.get("is_residual") or "").strip().lower() == "true"


def _residual_plug_ratio(plug: float, parent: float) -> float:
    if abs(parent) <= 0.005:
        return 0.0 if abs(plug) <= 0.005 else float("inf")
    return abs(plug) / abs(parent)


def _apply_residual_plugs(
    values, coa, decisions, *, max_ratio, allow_large_negative=True
):
    """Put a remainder into one residual, optionally limited by parent ratio."""
    plugs: dict[str, float] = {}
    by_id = {item.coa_id: item for item in decisions}
    for parent, child_ids in _children_by_parent(coa).items():
        decision = by_id.get(parent)
        if not decision or decision.child_coverage != ChildCoverage.PARTIAL:
            continue
        residual_ids = [
            child for child in child_ids if _is_residual(coa.get(child, {}))
        ]
        if len(residual_ids) != 1 or values.get(parent) is None:
            continue
        residual_id = residual_ids[0]
        sibling_total = sum(
            float(values.get(child) or 0.0)
            for child in child_ids
            if child != residual_id
        )
        identified_residual = float(values.get(residual_id) or 0.0)
        parent_value = float(values.get(parent) or 0.0)
        plug = parent_value - sibling_total - identified_residual
        ratio = _residual_plug_ratio(plug, parent_value)
        if (
            abs(plug) > 0.005
            and (
                max_ratio is None
                or ratio < max_ratio
            )
            and not (
                plug < 0
                and ratio >= RESIDUAL_AUTO_ACCEPT_RATIO
                and not allow_large_negative
            )
        ):
            values[residual_id] = identified_residual + plug
            plugs[residual_id] = plug
    return plugs


def _unresolved_negative_residuals(values, coa, decisions):
    unresolved = {}
    by_id = {item.coa_id: item for item in decisions}
    for parent, child_ids in _children_by_parent(coa).items():
        decision = by_id.get(parent)
        if not decision or decision.child_coverage != ChildCoverage.PARTIAL:
            continue
        residual_ids = [
            child for child in child_ids if _is_residual(coa.get(child, {}))
        ]
        if len(residual_ids) != 1 or values.get(parent) is None:
            continue
        residual_id = residual_ids[0]
        child_total = sum(float(values.get(child) or 0.0) for child in child_ids)
        plug = float(values.get(parent) or 0.0) - child_total
        ratio = _residual_plug_ratio(plug, float(values.get(parent) or 0.0))
        if plug < -0.005 and ratio >= RESIDUAL_AUTO_ACCEPT_RATIO:
            unresolved[parent] = (residual_id, plug, ratio)
    return unresolved


def _replace_unresolved_negative_errors(checks, unresolved):
    if not unresolved:
        return checks
    output = [
        item
        for item in checks
        if not (
            item.startswith("error|hierarchy_partial_with_residual|")
            and _finding_target(item) in unresolved
        )
    ]
    output.extend(
        f"warning|unresolved_negative_residual|{parent}|"
        f"residual_id={residual_id}|plug={plug:.2f}|ratio={ratio:.4f}|"
        "negative remainder was not forced into the residual before presentation"
        for parent, (residual_id, plug, ratio) in unresolved.items()
    )
    return output


def _large_residual_plug_warnings(values, coa, plugs):
    """Explain final residual plugs that were too large for automatic acceptance."""
    warnings = []
    children = _children_by_parent(coa)
    parent_by_child = {
        child: parent for parent, child_ids in children.items() for child in child_ids
    }
    for residual_id, plug in plugs.items():
        parent = parent_by_child.get(residual_id)
        if not parent:
            continue
        parent_value = float(values.get(parent) or 0.0)
        ratio = _residual_plug_ratio(plug, parent_value)
        if ratio < RESIDUAL_AUTO_ACCEPT_RATIO:
            continue
        warnings.append(
            f"warning|large_residual_plug|{parent}|"
            f"residual_id={residual_id}|plug={plug:.2f}|ratio={ratio:.4f}|"
            "remaining difference assigned to the residual before presentation"
        )
    return warnings


def _unsupported_residual_remainder_warnings(values, coa, decisions):
    """Surface material residuals calculated as a remainder, not direct detail."""
    children = _children_by_parent(coa)
    parent_by_child = {
        child: parent for parent, child_ids in children.items() for child in child_ids
    }
    warnings = []
    for decision in decisions:
        if decision.operation != SourceOperation.ADJUSTED_SUBTOTAL:
            continue
        if not _is_residual(coa.get(decision.coa_id, {})):
            continue
        parent = parent_by_child.get(decision.coa_id)
        if not parent or values.get(decision.coa_id) is None:
            continue
        remainder = float(values[decision.coa_id])
        parent_value = float(values.get(parent) or 0.0)
        ratio = _residual_plug_ratio(remainder, parent_value)
        if (
            abs(remainder) < UNSUPPORTED_REMAINDER_ABSOLUTE_THRESHOLD
            and ratio < RESIDUAL_AUTO_ACCEPT_RATIO
        ):
            continue
        warnings.append(
            f"warning|unsupported_residual_remainder|{parent}|"
            f"residual_id={decision.coa_id}|remainder={remainder:.2f}|"
            f"ratio={ratio:.4f}|residual value is calculated from a subtotal "
            "remainder rather than directly identified source detail"
        )
    return warnings


def _load_coa():
    source = resources.files("hotel_pl_normalizer.data").joinpath(
        "coa_v2.csv"
    )
    rows = {}
    with source.open("r", encoding="utf-8-sig", newline="") as handle:
        for order, raw in enumerate(csv.DictReader(handle)):
            coa_id = raw["coa_id"]
            rows[coa_id] = {
                **raw,
                "_order": order,
                "is_residual": str(raw.get("is_residual") or "false").strip().lower(),
            }
    parent_ids = {
        item["parent_coa_id"] for item in rows.values() if item["parent_coa_id"]
    }
    for coa_id, item in rows.items():
        item["is_total_line"] = str(coa_id in parent_ids).lower()
        item["indent_level"] = str(_coa_depth(coa_id, rows))
    return rows


def _coa_depth(coa_id, coa):
    depth = 0
    parent_id = coa[coa_id].get("parent_coa_id")
    visited = {coa_id}
    while parent_id and parent_id in coa and parent_id not in visited:
        visited.add(parent_id)
        depth += 1
        parent_id = coa[parent_id].get("parent_coa_id")
    return depth


def _coa_lines(coa):
    synonyms = _compact_synonyms(coa)
    return [
        _coa_line(item, synonyms.get(item["coa_id"], []), coa)
        for item in coa.values()
    ]


def _coa_line(item, synonyms, coa):
    fields = [
        item.get("coa_id", ""), item.get("account_name", ""),
        item.get("department", ""),
        f"hierarchy={_full_hierarchy_path(item, coa)}",
        f"residual={item.get('is_residual') or 'false'}",
    ]
    note = (item.get("mapping_note") or "").strip()
    if note:
        fields.append(f"note={note}")
    if synonyms:
        fields.append(f"synonyms={' ; '.join(synonyms)}")
    return "|".join(fields)


def _full_hierarchy_path(item, coa):
    path = [item.get("account_name") or item["coa_id"]]
    parent_id = item.get("parent_coa_id")
    visited = {item["coa_id"]}
    while parent_id and parent_id in coa and parent_id not in visited:
        visited.add(parent_id)
        parent = coa[parent_id]
        path.append(parent.get("account_name") or parent_id)
        parent_id = parent.get("parent_coa_id")
    return " > ".join(reversed(path))


def _compact_synonyms(coa, limit=12):
    output = {}
    for coa_id, item in coa.items():
        terms = []
        seen = set()
        for term in (item.get("synonyms") or "").split("|"):
            term = term.strip()
            normalized = _normalize_term(term)
            if len(normalized) < 2 or normalized in seen:
                continue
            seen.add(normalized)
            terms.append(term)
        output[coa_id] = terms[:limit]
    return output


def _normalize_term(value):
    return " ".join(
        "".join(character if character.isalnum() else " " for character in value.lower()).split()
    )


def _hierarchy_equations(coa):
    children = _children_by_parent(coa)
    return [
        f"{parent} = {' + '.join(child_ids)}"
        for parent, child_ids in sorted(children.items())
    ]


def _summary_equation_lines():
    return [
        f"{target} = {_format_equation_terms(terms)}"
        for target, terms in SUMMARY_EQUATIONS.items()
    ]


def _group_rows(evidence, period_labels=None):
    period_labels = period_labels or {"selected": "Selected period"}
    multi_period = len(period_labels) > 1
    grouped = {}
    for item in evidence:
        sheet, row = item["row_key"].rsplit("!", 1)
        flags = []
        if item.get("bold"):
            flags.append("b")
        if item.get("indent") not in {None, 0}:
            flags.append(f"i{item['indent']:g}")
        fields = [row]
        if flags:
            fields.append(",".join(flags))
        fields.append(item.get("label") or "")
        selected_values = item.get("selected_values") or {}
        fields.extend(
            _prompt_value(
                selected_values.get(period_id, item.get("selected_value"))
            )
            for period_id in period_labels
        )
        if multi_period:
            group_header = {
                "periods": [
                    f"{period_id}={period_labels[period_id]}"
                    for period_id in period_labels
                ],
                "value_columns": item.get("selected_value_columns")
                or {"selected": item.get("selected_value_column")},
                "rows": [],
            }
        else:
            group_header = {
                "value_column": item.get("selected_value_column"),
                "rows": [],
            }
        grouped.setdefault(sheet, group_header)["rows"].append("|".join(fields))
    return [{"sheet": key, **value} for key, value in grouped.items()]


def _prompt_value(value):
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    if isinstance(value, float):
        precision = 6 if abs(value) < 1 else 2
        return f"{value:.{precision}f}".rstrip("0").rstrip(".")
    return str(value)
