from __future__ import annotations

import re
from collections import Counter

from hotel_pl_normalizer.models.common import Severity, ValidationStatus
from hotel_pl_normalizer.models.sheet_selection import (
    DepartmentCandidate,
    SheetNameDecision,
    SheetNameRoleHint,
    SheetNameSelection,
    SheetNameSelectionResult,
    SheetNameSelectionValidation,
    SheetNameSelectionValidationIssue,
    SheetNameTriagePacket,
    WorkbookSheetLayout,
)

_FNB_DETAIL_SHEET_TERMS = (
    "outlet",
    "venue",
    "restaurant",
    "bar",
    "banquet",
    "catering",
    "room service",
    "roomservice",
    "in room dining",
    "in-room dining",
    "ird",
    "minibar",
    "mini bar",
)

_FNB_DETAIL_SKIP_TERMS = (
    "kpi",
    "stat",
    "stats",
    "statistics",
    "productivity",
)

_ROOMS_SUPPORT_TERMS = (
    "mma",
    "market mix",
    "marketmix",
    "reservation",
    "reservations",
    "resv",
)

_FNB_SUPPORT_TERMS = (
    "staff dining",
    "employee dining",
    "dining",
    "fb retail",
    "f&b retail",
    "food beverage retail",
    "food and beverage retail",
    "retail shop",
)

_OOD_DETAIL_SHEET_TERMS = (
    "guest laundry",
)


def normalize_sheet_name_selection_result(
    packet: SheetNameTriagePacket,
    result: SheetNameSelectionResult,
) -> SheetNameSelectionResult:
    """Repair a sheet-name selection result so downstream code has safe buckets."""
    input_names = [sheet.sheet_name for sheet in packet.sheets]
    input_name_set = set(input_names)
    notes = list(result.notes)

    selections_by_name: dict[str, SheetNameSelection] = {}
    duplicate_names = {
        name for name, count in Counter(selection.sheet_name for selection in result.selections).items() if count > 1
    }
    for selection in result.selections:
        if selection.sheet_name not in input_name_set:
            notes.append(f"Dropped invented sheet selection: {selection.sheet_name}.")
            continue
        if selection.sheet_name in selections_by_name:
            notes.append(f"Dropped duplicate sheet selection for: {selection.sheet_name}.")
            continue
        selections_by_name[selection.sheet_name] = selection

    for sheet_name in input_names:
        if sheet_name not in selections_by_name:
            selections_by_name[sheet_name] = SheetNameSelection(
                sheet_name=sheet_name,
                decision=SheetNameDecision.UNSURE,
                role_hint=SheetNameRoleHint.UNKNOWN,
                evidence=["No selection was provided; defaulted to unsure."],
            )
            notes.append(f"Defaulted missing sheet selection to unsure: {sheet_name}.")

    selections = [selections_by_name[name] for name in input_names]
    selections = _promote_explicit_summary_tabs(selections, notes)

    if len(selections) == 1 and selections[0].decision == SheetNameDecision.SKIP:
        selection = selections[0]
        selections[0] = selection.model_copy(
            update={
                "decision": SheetNameDecision.UNSURE,
                "role_hint": SheetNameRoleHint.UNKNOWN,
                "evidence": selection.evidence
                + ["The only sheet in a workbook cannot be skipped."],
            }
        )
        notes.append("Changed single-sheet skip decision to unsure.")

    normalized_selections: list[SheetNameSelection] = []
    for selection in selections:
        if _is_event_technology_selection(selection):
            normalized_selections.append(
                selection.model_copy(
                    update={
                        "decision": SheetNameDecision.TRIAGE,
                        "role_hint": SheetNameRoleHint.SUPPORTING_SCHEDULE,
                        "department_candidates": [
                            DepartmentCandidate(
                                department="food_and_beverage",
                                evidence=["Event Technology is treated as F&B/banquet detail, not centralized IT."],
                            )
                        ],
                        "needs_sheet_enrichment": True,
                        "evidence": selection.evidence
                        + ["Event Technology was normalized to Food and Beverage detail."],
                        "defer_reason": None,
                    }
                )
            )
            notes.append(f"Normalized Event Technology sheet to F&B detail: {selection.sheet_name}.")
            continue
        if selection.decision in {SheetNameDecision.SKIP, SheetNameDecision.DEFER, SheetNameDecision.UNSURE} and _is_obvious_ood_detail_sheet(selection.sheet_name):
            existing_candidates = list(selection.department_candidates)
            if not any(candidate.department == "other_operated_departments" for candidate in existing_candidates):
                existing_candidates.insert(
                    0,
                    DepartmentCandidate(
                        department="other_operated_departments",
                        evidence=["Sheet name is an explicit OOD detail signal."],
                    ),
                )
            normalized_selections.append(
                selection.model_copy(
                    update={
                        "decision": SheetNameDecision.TRIAGE,
                        "role_hint": SheetNameRoleHint.SUPPORTING_SCHEDULE,
                        "department_candidates": existing_candidates,
                        "needs_sheet_enrichment": True,
                        "evidence": selection.evidence
                        + [
                            "Explicit OOD detail sheets such as Guest Laundry are kept for triage, not skipped, because they are guest-facing operated outlets."
                        ],
                        "defer_reason": None,
                    }
                )
            )
            notes.append(f"Promoted explicit OOD detail sheet from skip/defer to triage: {selection.sheet_name}.")
            continue
        if selection.decision == SheetNameDecision.DEFER and _is_obvious_fnb_detail_sheet(selection.sheet_name):
            existing_candidates = list(selection.department_candidates)
            if not any(candidate.department == "food_and_beverage" for candidate in existing_candidates):
                existing_candidates.insert(
                    0,
                    DepartmentCandidate(
                        department="food_and_beverage",
                        evidence=["Sheet name is an obvious F&B detail signal."],
                    ),
                )
            normalized_selections.append(
                selection.model_copy(
                    update={
                        "decision": SheetNameDecision.TRIAGE,
                        "role_hint": SheetNameRoleHint.SUPPORTING_SCHEDULE,
                        "department_candidates": existing_candidates,
                        "needs_sheet_enrichment": True,
                        "evidence": selection.evidence
                        + [
                            "Obvious F&B detail sheets are kept for triage, not deferred, because they may be needed for revenue fallback or validation."
                        ],
                        "defer_reason": None,
                    }
                )
            )
            notes.append(f"Promoted obvious F&B detail sheet from defer to triage: {selection.sheet_name}.")
            continue
        normalized_selections.append(selection)
    selections = normalized_selections

    if selections and all(selection.decision in (SheetNameDecision.SKIP, SheetNameDecision.DEFER) for selection in selections):
        first = selections[0]
        selections[0] = first.model_copy(
            update={
                "decision": SheetNameDecision.UNSURE,
                "role_hint": SheetNameRoleHint.UNKNOWN,
                "evidence": first.evidence
                + ["All sheets were skipped or deferred; forced one sheet to unsure for review."],
            }
        )
        notes.append("Changed one skipped/deferred sheet to unsure because no sheet was selected for immediate review.")

    if duplicate_names:
        notes.append(f"Duplicate selections were present for: {', '.join(sorted(duplicate_names))}.")

    selections = _skip_contextual_support_sheets(selections, notes)

    return SheetNameSelectionResult(
        workbook_id=packet.workbook_id,
        model=result.model,
        workbook_layout=(
            WorkbookSheetLayout.SINGLE_TAB_P_AND_L
            if len(input_names) == 1
            else result.workbook_layout
        ),
        layout_evidence=result.layout_evidence,
        selections=selections,
        selected_sheet_names=[item.sheet_name for item in selections if item.decision == SheetNameDecision.TRIAGE],
        deferred_sheet_names=[item.sheet_name for item in selections if item.decision == SheetNameDecision.DEFER],
        skipped_sheet_names=[item.sheet_name for item in selections if item.decision == SheetNameDecision.SKIP],
        unsure_sheet_names=[item.sheet_name for item in selections if item.decision == SheetNameDecision.UNSURE],
        notes=notes,
    )


def validate_sheet_name_selection_result(
    packet: SheetNameTriagePacket,
    result: SheetNameSelectionResult,
) -> SheetNameSelectionValidation:
    input_names = [sheet.sheet_name for sheet in packet.sheets]
    input_name_set = set(input_names)
    selection_names = [selection.sheet_name for selection in result.selections]
    selection_name_set = set(selection_names)
    issues: list[SheetNameSelectionValidationIssue] = []

    if result.workbook_id != packet.workbook_id:
        issues.append(
            SheetNameSelectionValidationIssue(
                severity=Severity.ERROR,
                message="Selection result workbook_id does not match the input packet.",
            )
        )

    invented = sorted(selection_name_set - input_name_set)
    if invented:
        issues.append(
            SheetNameSelectionValidationIssue(
                severity=Severity.ERROR,
                message="Selection result contains invented sheet names.",
                sheet_names=invented,
            )
        )

    missing = [name for name in input_names if name not in selection_name_set]
    if missing:
        issues.append(
            SheetNameSelectionValidationIssue(
                severity=Severity.ERROR,
                message="Selection result is missing input sheet names.",
                sheet_names=missing,
            )
        )

    duplicates = sorted(name for name, count in Counter(selection_names).items() if count > 1)
    if duplicates:
        issues.append(
            SheetNameSelectionValidationIssue(
                severity=Severity.ERROR,
                message="Selection result contains duplicate sheet decisions.",
                sheet_names=duplicates,
            )
        )

    bucket_names = (
        result.selected_sheet_names
        + result.deferred_sheet_names
        + result.skipped_sheet_names
        + result.unsure_sheet_names
    )
    if set(bucket_names) != input_name_set or len(bucket_names) != len(input_names):
        issues.append(
            SheetNameSelectionValidationIssue(
                severity=Severity.ERROR,
                message="Selected, deferred, skipped, and unsure buckets do not exactly cover input sheets.",
            )
        )

    if len(input_names) == 1:
        only = result.selections[0] if result.selections else None
        if (
            only is not None
            and only.decision == SheetNameDecision.SKIP
        ):
            issues.append(
                SheetNameSelectionValidationIssue(
                    severity=Severity.ERROR,
                    message="The only sheet in a workbook cannot be skipped.",
                    sheet_names=[only.sheet_name],
                )
            )

    if input_names and len(result.skipped_sheet_names) + len(result.deferred_sheet_names) == len(input_names):
        issues.append(
            SheetNameSelectionValidationIssue(
                severity=Severity.ERROR,
                message="All sheets were skipped or deferred; at least one sheet must remain selected or unsure.",
                sheet_names=input_names,
            )
        )

    if len(input_names) == 1 and result.workbook_layout == WorkbookSheetLayout.MULTI_TAB_DEPARTMENT_P_AND_L:
        issues.append(
            SheetNameSelectionValidationIssue(
                severity=Severity.ERROR,
                message="Single-sheet workbook cannot be classified as a multi-tab department P&L.",
                sheet_names=input_names,
            )
        )

    status = ValidationStatus.PASS
    if any(issue.severity == Severity.ERROR for issue in issues):
        status = ValidationStatus.FAIL
    elif issues:
        status = ValidationStatus.WARNING
    return SheetNameSelectionValidation(status=status, issues=issues)


def _is_obvious_fnb_detail_sheet(sheet_name: str) -> bool:
    normalized = re.sub(r"\s+", " ", sheet_name.lower().replace("_", " ")).strip()
    compact = re.sub(r"[^a-z0-9]+", "", normalized)
    if any(term in normalized or term.replace(" ", "") in compact for term in _FNB_DETAIL_SKIP_TERMS):
        return False
    return any(term in normalized or term.replace(" ", "") in compact for term in _FNB_DETAIL_SHEET_TERMS)


def _is_obvious_ood_detail_sheet(sheet_name: str) -> bool:
    normalized = re.sub(r"\s+", " ", sheet_name.lower().replace("_", " ")).strip()
    compact = re.sub(r"[^a-z0-9]+", "", normalized)
    return any(term in normalized or term.replace(" ", "") in compact for term in _OOD_DETAIL_SHEET_TERMS)


def _is_event_technology_selection(selection: SheetNameSelection) -> bool:
    text = " ".join([selection.sheet_name, *selection.evidence]).lower()
    normalized = re.sub(r"[^a-z0-9]+", " ", text)
    return "event technology" in normalized or "event tech" in normalized


def _skip_contextual_support_sheets(
    selections: list[SheetNameSelection],
    notes: list[str],
) -> list[SheetNameSelection]:
    has_core_rooms = any(
        selection.decision != SheetNameDecision.SKIP
        and _has_department_candidate(selection, "rooms")
        and _normalized_sheet_name(selection.sheet_name) in {"rooms", "room"}
        for selection in selections
    )
    has_core_fnb = any(
        selection.decision != SheetNameDecision.SKIP
        and _has_department_candidate(selection, "food_and_beverage")
        and any(term in _normalized_sheet_name(selection.sheet_name) for term in ("fb cons", "fb summary", "fbsummary", "food beverage", "food and beverage"))
        for selection in selections
    )
    normalized_selections: list[SheetNameSelection] = []
    for selection in selections:
        normalized = _normalized_sheet_name(selection.sheet_name)
        compact = re.sub(r"[^a-z0-9]+", "", normalized)
        should_skip_rooms_support = has_core_rooms and _contains_any_term(normalized, compact, _ROOMS_SUPPORT_TERMS)
        should_skip_fnb_support = has_core_fnb and _contains_any_term(normalized, compact, _FNB_SUPPORT_TERMS)
        if selection.decision != SheetNameDecision.SKIP and (should_skip_rooms_support or should_skip_fnb_support):
            normalized_selections.append(
                selection.model_copy(
                    update={
                        "decision": SheetNameDecision.SKIP,
                        "role_hint": SheetNameRoleHint.SUPPORTING_SCHEDULE,
                        "department_candidates": [],
                        "needs_sheet_enrichment": False,
                        "evidence": selection.evidence
                        + [
                            "Skipped as a contextual support/analysis sheet because the core department tab exists."
                        ],
                        "defer_reason": None,
                    }
                )
            )
            notes.append(f"Skipped contextual support sheet because core department tab exists: {selection.sheet_name}.")
            continue
        normalized_selections.append(selection)
    return normalized_selections


def _promote_explicit_summary_tabs(
    selections: list[SheetNameSelection],
    notes: list[str],
) -> list[SheetNameSelection]:
    normalized_selections: list[SheetNameSelection] = []
    for selection in selections:
        normalized = _normalized_sheet_name(selection.sheet_name)
        if selection.decision == SheetNameDecision.SKIP and normalized in {"summary", "summary operating statement"}:
            normalized_selections.append(
                selection.model_copy(
                    update={
                        "decision": SheetNameDecision.TRIAGE,
                        "role_hint": SheetNameRoleHint.SUMMARY_P_AND_L,
                        "department_candidates": [
                            DepartmentCandidate(
                                department="summary",
                                evidence=["Sheet name is an explicit main Summary tab."],
                            )
                        ],
                        "needs_sheet_enrichment": True,
                        "evidence": selection.evidence
                        + ["Explicit main Summary tabs should remain selected for Department ID."],
                        "defer_reason": None,
                    }
                )
            )
            notes.append(f"Promoted explicit main Summary tab from skip to triage: {selection.sheet_name}.")
            continue
        normalized_selections.append(selection)
    return normalized_selections


def _has_department_candidate(selection: SheetNameSelection, department: str) -> bool:
    return any(candidate.department == department for candidate in selection.department_candidates)


def _normalized_sheet_name(sheet_name: str) -> str:
    return re.sub(r"\s+", " ", sheet_name.lower().replace("_", " ")).strip()


def _contains_any_term(normalized: str, compact: str, terms: tuple[str, ...]) -> bool:
    for term in terms:
        compact_term = term.replace(" ", "")
        if len(compact_term) <= 4:
            if normalized == term or compact == compact_term:
                return True
            if bool(re.search(rf"(^|[^a-z0-9]){re.escape(term)}([^a-z0-9]|$)", normalized)):
                return True
            continue
        if term in normalized or compact_term in compact:
            return True
    return False
