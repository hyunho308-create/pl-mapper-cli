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
    ProviderResponseTruncated,
    ProviderToolLoopError,
)
from hotel_pl_normalizer.providers.base import ModelToolError


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
    kind: Literal["ambiguity", "unusual_convention", "source_discrepancy"]
    message: str
    coa_ids: list[str] = Field(default_factory=list)
    source_rows: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_context(self):
        if not self.message.strip():
            raise ValueError("review item message cannot be blank")
        if not self.coa_ids and not self.source_rows:
            raise ValueError("review item must cite a COA id or source row")
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
    status: Literal["accepted"]
    accepted_validation_attempt: int
    review_items: list[MappingReviewItem] = Field(default_factory=list)


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
        routed_departments=None,
        sheet_classifications=None,
    ):
        self.workbook_id = workbook_id
        self.evidence = evidence
        self.coa = coa
        self.period_labels = period_labels or {"selected": "Selected period"}
        self.routed_departments = frozenset(routed_departments or ())
        self.sheet_classifications = tuple(sheet_classifications or ())
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

    def dispatch(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if name == "validate_mapping":
            if self.current_plan is not None:
                raise ModelToolError(
                    "validate_mapping is only for the initial complete plan. "
                    "Use patch_mapping for repairs."
                )
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
        unknown = sorted(set(replacement_ids) - set(self.coa))
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
        missing = sorted(set(self.coa) - submitted)
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
        collapse_issue = _department_collapse_issue(
            plan, self.evidence, self.sheet_classifications
        )
        if collapse_issue:
            errors.append(collapse_issue)
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
                self.routed_departments,
                self.summary_only_pushdown_rows,
            )
            checks = _qualify_source_discrepancies(
                checks,
                plan,
                self.evidence,
                self.coa,
                self.history,
                label,
                calculation_issues,
            )
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
        needs_detail_enrichment = any(
            "warning|source_detail_incomplete|" in item for item in warnings
        )
        return {
            "ok": True,
            "accepted": accepted,
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
                else "The mapping has no blocking errors. One final warning-cleanup "
                "response is available: make one evidence-supported patch only if "
                "it reduces warnings without disturbing accepted structure; "
                "otherwise return the compact completion object unchanged."
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
        if self.last_accepted_plan is None:
            return (
                "The current mapping has not been accepted. Read the latest validator "
                "errors and call patch_mapping with only the omitted or implicated "
                "decisions before returning a completion object."
            )
        if completion.workbook_id != self.workbook_id:
            return f"The completion workbook_id must be {self.workbook_id!r}."
        if completion.accepted_validation_attempt != len(self.history):
            return (
                "accepted_validation_attempt must equal the latest accepted attempt "
                f"number, {len(self.history)}."
            )
        if completion.review_items != self.last_accepted_plan.review_items:
            return "Completion review_items must match the accepted mapping plan."
        if self.warning_cleanup_pending:
            self.warning_cleanup_pending = False
            self.warning_cleanup_attempted = True
            self.warning_cleanup_outcome = "kept_accepted_plan"
        return None

    def terminal_result(self, name: str, result: dict[str, Any]):
        """Return a completion payload when another model turn cannot add value."""
        if self.warning_cleanup_pending and name == "patch_mapping":
            return self._finish_warning_cleanup(result)
        if not result.get("accepted"):
            return None
        if result.get("warning_count", 0) and not self.warning_cleanup_attempted:
            self.warning_cleanup_pending = True
            self.warning_cleanup_outcome = "offered"
            self.warning_cleanup_checkpoint_plan = self.current_plan
            self.warning_cleanup_checkpoint_result = result
            self.warning_cleanup_checkpoint_score = _validation_score(result)
            self.warning_cleanup_checkpoint_attempt = result["validation_attempt"]
            return None
        return self._completion_payload(result)

    def _completion_payload(self, result: dict[str, Any]) -> dict[str, Any]:
        return {
            "workbook_id": self.workbook_id,
            "status": "accepted",
            "accepted_validation_attempt": result["validation_attempt"],
            "review_items": result.get("review_items", []),
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
                    ],
                },
                "message": {"type": "string"},
                "coa_ids": coa_id_list,
                "source_rows": string_list,
            },
            "required": ["kind", "message", "coa_ids", "source_rows"],
        }
        review_items = {"type": "array", "items": review_item}
        decision = {
            "type": "object",
            "properties": {
                "coa_id": {"type": "string", "enum": list(self.coa)},
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
                f"Submit the initial complete mapping with all {len(self.coa)} COA "
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
            "routed_department_not_present",
        }
        for rule in errors
    )
    return (
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
    "routed_department_not_present",
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
        for item in checks
    ):
        return checks

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
    return output


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
    if not _same_discrepancy_was_previously_blocking(
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
    if _offset_candidates(finding, plan, evidence, coa):
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


DEPARTMENT_ROOT_ROUTES = {
    "S1.total_rooms_revenue": "rooms",
    "S1.total_rooms_expenses": "rooms",
    "S2.total_food_and_beverage_revenue": "food_and_beverage",
    "S2.total_food_and_beverage_expenses": "food_and_beverage",
    "S3.total_other_operated_departments_revenue": "other_operated_departments",
    "S3.total_other_operated_departments_expenses": "other_operated_departments",
    "S4.total_miscellaneous_income": "miscellaneous_income",
    "S5.total_administrative_and_general_expenses": "administrative_and_general",
    "S6.total_information_and_telecommunications_systems_expenses": "information_and_telecommunications_systems",
    "S7.total_sales_and_marketing_expenses": "sales_and_marketing",
    "S8.total_property_operation_and_maintenance_expenses": "property_operations_and_maintenance",
    "S9.total_utilities_expenses": "utilities",
    "S10.total_management_fees": "management_fees",
    "S11.total_non_operating_income_and_expenses": "non_operating_income_and_expense",
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
    sheet_classifications,
    periods,
    period_labels: dict[str, str] | None = None,
    evidence: list[dict],
    skipped_sheets: list[str],
    client,
    on_activity=None,
) -> MappingResult:
    """Map one complete workbook using model decisions and Python validation."""
    coa = _load_coa()
    period_labels = period_labels or {"selected": requested_period}
    prompt = _primary_prompt(
        workbook_id,
        requested_period,
        sheet_classifications,
        periods,
        period_labels,
        evidence,
        coa,
        skipped_sheets,
    )
    validator = WorkbookMappingValidator(
        workbook_id,
        evidence,
        coa,
        period_labels=period_labels,
        routed_departments={
            hint.department.value
            for item in sheet_classifications
            for hint in item.department_hints
            if hint.section_role.value not in {"kpi", "unknown"}
        },
        sheet_classifications=sheet_classifications,
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
        )
    except ProviderToolLoopError:
        exhausted = True
    except ProviderResponseTruncated:
        if not validator.submissions:
            raise
        exhausted = True
        repair_truncated = True

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
            validator.routed_departments,
            validator.summary_only_pushdown_rows,
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
    primary_period_id = next(iter(period_labels))
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
    model_calls = list(client.usage_history)
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
                check.startswith("warning|source_discrepancy|")
                for period_checks in checks_by_period.values()
                for check in period_checks
            ),
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
        "error_count": int(result.get("error_count") or 0),
        "warning_count": int(result.get("warning_count") or 0),
        "score": list(result.get("validation_score") or _validation_score(result)),
        "plan_digest": validator.digest(plan),
    }


def _primary_prompt(
    workbook_id,
    requested_period,
    sheet_classifications,
    periods,
    period_labels,
    evidence,
    coa,
    skipped_sheets,
) -> str:
    rows = _group_rows(evidence, period_labels)
    coa_lines = _coa_lines(coa)
    classifications = [
        {
            "sheet_name": item.sheet_name,
            "department_hints": [
                {
                    "department": hint.department.value,
                    "section_role": hint.section_role.value,
                    "evidence": list(hint.evidence),
                }
                for hint in item.department_hints
            ],
            "evidence": list(item.evidence),
        }
        for item in sheet_classifications
    ]
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
                f"department={selection.department or '-'}|"
                f"column={selection.value_column}|{period_labels[period_id]}"
            )
    payload = {
        "workbook_id": workbook_id,
        "requested_period": requested_period,
        "period_columns": period_lines,
        "period_binding_department_hints": classifications,
        "coa": coa_lines,
        "coa_hierarchy_equations": _hierarchy_equations(coa),
        "summary_equations": _summary_equation_lines(),
        "sheets_excluded_as_irrelevant": skipped_sheets,
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
    missing = sorted(set(coa) - seen)
    if missing:
        issues.append(f"missing decisions: {','.join(missing)}")
    return values, issues


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


def _validation_values(values):
    """Existing checks treat a blank as absent/zero without changing the output."""
    return {
        coa_id: (0.0 if value is None else float(value))
        for coa_id, value in values.items()
    }


def _department_collapse_issue(plan, evidence, sheet_classifications):
    """Reject a suspicious parent-only plan when binding found rich detail tabs.

    This is deliberately a coarse safety net, not a completeness quota. It only
    activates for a genuinely large body of unambiguous department-sheet
    evidence, then asks whether the plan cited a plausible number of detailed
    COA accounts from those same sheets.
    """
    department_prefixes: dict[str, set[str]] = {}
    for coa_id, department in DEPARTMENT_ROOT_ROUTES.items():
        department_prefixes.setdefault(department, set()).add(
            coa_id.split(".", 1)[0] + "."
        )
    confident: dict[str, str] = {}
    for item in sheet_classifications:
        useful = [
            hint
            for hint in item.department_hints
            if hint.department.value != "summary"
            and hint.section_role.value in {"primary", "summary", "detail"}
        ]
        if len(useful) == 1:
            confident[item.sheet_name] = useful[0].department.value
    if not confident:
        return None

    rich_rows = [
        row
        for row in evidence
        if row["row_key"].rsplit("!", 1)[0] in confident
        and str(row.get("label") or "").strip()
        and any(
            isinstance(value, (int, float))
            and not isinstance(value, bool)
            and abs(float(value)) > 0.005
            for value in (row.get("selected_values") or {}).values()
        )
    ]
    rich_sheets = {
        row["row_key"].rsplit("!", 1)[0] for row in rich_rows
    }
    if len(rich_rows) < 120 and len(rich_sheets) < 8:
        return None

    active_detail = set()
    for decision in plan.decisions:
        department = next(
            (
                confident[sheet]
                for row_key in decision.source_rows
                for sheet in [row_key.rsplit("!", 1)[0]]
                if sheet in rich_sheets
            ),
            None,
        )
        if department is None:
            continue
        if any(
            decision.coa_id.startswith(prefix)
            for prefix in department_prefixes.get(department, ())
        ) and decision.coa_id not in DEPARTMENT_ROOT_ROUTES:
            active_detail.add(decision.coa_id)

    minimum = max(12, 2 * len(rich_sheets))
    if len(active_detail) >= minimum or len(active_detail) >= len(rich_rows) * 0.12:
        return None
    return (
        "department_detail_collapsed: period binding identified "
        f"{len(rich_sheets)} unambiguous department sheet(s) with "
        f"{len(rich_rows)} non-zero labelled rows, but the plan cites only "
        f"{len(active_detail)} detailed department COA account(s) from them; "
        "map identifiable child accounts instead of collapsing the workbook "
        "mostly to parents/no_value"
    )


def _validate(
    values,
    coa,
    decisions,
    strategy,
    review_items=None,
    routed_departments=None,
    summary_only_pushdown_rows=None,
):
    issues = _source_row_reuse_issues(
        decisions,
        coa,
        review_items or [],
        summary_only_pushdown_rows or set(),
    )
    by_id = {item.coa_id: item for item in decisions}
    routed_departments = frozenset(routed_departments or ())
    for department_id, routed_department in DEPARTMENT_ROOT_ROUTES.items():
        decision = by_id.get(department_id)
        if (
            routed_department in routed_departments
            and decision is not None
            and decision.operation == SourceOperation.NO_VALUE
            and decision.child_coverage == ChildCoverage.NOT_PRESENT
        ):
            issues.append(
                f"error|routed_department_not_present|{department_id}|"
                f"department={routed_department}|a routed department cannot be "
                "marked no_value/not_present"
            )
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
    for summary_id, department_root in SUMMARY_LINKS.items():
        if left == summary_id and (
            right == department_root
            or _accounts_share_lineage(department_root, right, coa)
        ):
            return True
        if right == summary_id and (
            left == department_root
            or _accounts_share_lineage(department_root, left, coa)
        ):
            return True
    return False


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
    if mode == OodMiscSummaryMode.SEPARATE:
        _append_equation_issue(issues, "summary_department", "S12.total_other_operated_departments_revenue", summary_ood, detail_ood, "S3.total_other_operated_departments_revenue", source_supported=_all_source_supported(by_id, ["S12.total_other_operated_departments_revenue", "S3.total_other_operated_departments_revenue"]))
        _append_equation_issue(issues, "summary_department", "S12.total_miscellaneous_income", summary_misc, detail_misc, "S4.total_miscellaneous_income", source_supported=_all_source_supported(by_id, ["S12.total_miscellaneous_income", "S4.total_miscellaneous_income"]))
    elif mode == OodMiscSummaryMode.COMBINED_IN_OOD:
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
    else:
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
