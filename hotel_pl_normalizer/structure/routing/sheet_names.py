from __future__ import annotations

import re

from hotel_pl_normalizer.models.sheet_selection import (
    DepartmentCandidate,
    SheetNameCandidate,
    SheetNameDecision,
    SheetNameRoleHint,
    SheetNameSelection,
    SheetNameSelectionResult,
    SheetNameTriagePacket,
    WorkbookSheetLayout,
)
from hotel_pl_normalizer.models.workbook import WorkbookRecord

_SUMMARY_NAMES = ("summary", "deptsummary", "department summary", "statement", "p&l", "pl")
_DEPARTMENT_NAMES = (
    "rooms",
    "room",
    "fb",
    "f&b",
    "fbdept",
    "food",
    "beverage",
    "banquet",
    "catering",
    "outlet",
    "venue",
    "restaurant",
    "bar",
    "roomservice",
    "room service",
    "in room dining",
    "in-room dining",
    "ird",
    "minibar",
    "mini bar",
    "miscincome",
    "misc income",
    "minorop",
    "oth op",
    "oth_op",
    "other operating",
    "minor op",
    "retail",
    "guest communications",
    "guest communication",
    "guest laundry",
    "recreation",
    "spa",
    "golf",
    "admin",
    "a&g",
    "it",
    "sm",
    "s&m",
    "rm",
    "engineering",
    "property opera",
    "pom",
    "utilities",
    "energy",
    "ec",
    "management",
    "mgmt",
    "mgt",
    "fees",
    "fixedexp",
    "fixed exp",
    "nonop",
    "non op",
)
_CONDITIONAL_DETAIL_NAMES = (
    "outlet",
    "venue",
    "banquet",
    "catering",
    "restaurant",
    "bar",
    "roomservice",
    "room service",
    "in room dining",
    "in-room dining",
    "ird",
    "minibar",
    "mini bar",
    "giftshop",
    "gift shop",
    "parking",
    "laundry",
)
_LIKELY_SKIP_NAMES = (
    "balance",
    "balancesheet",
    "check",
    "data",
    "cube",
    "stats",
    "statistics",
    "kpi",
    "payroll",
    "pteb",
    "efte",
    "laundry",
    "ttm",
    "12mth",
    "analysis",
    "productivity",
    "productivit",
    "setup",
    "set up",
    "staff dining",
    "cafeteria",
    "benefits",
    "overtime",
)


def build_sheet_name_triage_packet(
    workbook: WorkbookRecord,
    *,
    enrich_sheet_names: set[str] | None = None,
) -> SheetNameTriagePacket:
    enrich_sheet_names = enrich_sheet_names or set()
    return SheetNameTriagePacket(
        workbook_id=workbook.workbook_id,
        source_filename=workbook.source.original_filename,
        sheet_count=workbook.workbook_metadata.sheet_count,
        sheets=[
            SheetNameCandidate(
                sheet_name=sheet.sheet_name,
                sheet_index=idx,
                visible=sheet.visible,
                header_cells=_header_cells(sheet) if sheet.sheet_name in enrich_sheet_names else [],
            )
            for idx, sheet in enumerate(workbook.sheets, start=1)
        ],
    )


def _header_cells(sheet, *, max_rows: int = 12, max_cells: int = 16) -> list[str]:
    cells: list[str] = []
    for row in sheet.rows[:max_rows]:
        for cell in row.cells:
            value = cell.display_value
            if value is None or not str(value).strip():
                continue
            if _is_header_noise(str(value)):
                continue
            cells.append(f"{cell.address}: {str(value).strip()}")
            if len(cells) >= max_cells:
                return cells
    return cells


def _is_header_noise(value: str) -> bool:
    normalized = value.strip().lower()
    if normalized.startswith("%,") or normalized in {"%,c", "%var", "actual", "budget", "% "}:
        return True
    return normalized.startswith(
        (
            "report id:",
            "layout:",
            "scope:",
            "currency code:",
            "for period ",
            "period",
        )
    )


def build_local_sheet_name_selection(packet: SheetNameTriagePacket) -> SheetNameSelectionResult:
    """Local inspection helper until the sheet-name LLM call is wired.

    This is not intended to be authoritative. It gives us a cheap way to test
    the packet shape and keep obvious analysis/check tabs out of rich triage
    during local development.
    """
    selections = _apply_workbook_context([_local_selection(sheet) for sheet in packet.sheets])
    workbook_layout = (
        WorkbookSheetLayout.SINGLE_TAB_P_AND_L
        if len(packet.sheets) == 1
        else WorkbookSheetLayout.MIXED_OR_UNKNOWN
    )
    return SheetNameSelectionResult(
        workbook_id=packet.workbook_id,
        workbook_layout=workbook_layout,
        layout_evidence=[
            "Local development helper only; production layout should come from the sheet-name triage agent."
        ],
        selections=selections,
        selected_sheet_names=[item.sheet_name for item in selections if item.decision == SheetNameDecision.TRIAGE],
        deferred_sheet_names=[item.sheet_name for item in selections if item.decision == SheetNameDecision.DEFER],
        skipped_sheet_names=[item.sheet_name for item in selections if item.decision == SheetNameDecision.SKIP],
        unsure_sheet_names=[item.sheet_name for item in selections if item.decision == SheetNameDecision.UNSURE],
        notes=[
            "Local sheet-name selection is a development helper only; the production decision should come from the sheet-name triage agent."
        ],
    )


def _local_selection(sheet: SheetNameCandidate) -> SheetNameSelection:
    normalized = _normalize_sheet_name(sheet.sheet_name)
    evidence_text = _normalize_sheet_name(" ".join([sheet.sheet_name, *sheet.header_cells]))
    if not sheet.visible:
        return SheetNameSelection(
            sheet_name=sheet.sheet_name,
            decision=SheetNameDecision.SKIP,
            role_hint=SheetNameRoleHint.UNKNOWN,
            evidence=["Sheet is hidden."],
        )
    if _is_guest_laundry_sheet(normalized):
        return SheetNameSelection(
            sheet_name=sheet.sheet_name,
            decision=SheetNameDecision.TRIAGE,
            role_hint=SheetNameRoleHint.SUPPORTING_SCHEDULE,
            department_candidates=[
                DepartmentCandidate(
                    department="other_operated_departments",
                    evidence=["Sheet name explicitly indicates Guest Laundry, an OOD operated outlet."],
                )
            ],
            needs_sheet_enrichment=True,
            evidence=[
                "Guest Laundry is a guest-facing Other Operated detail schedule, not a plain in-house laundry support tab."
            ],
        )
    if any(term in normalized for term in _LIKELY_SKIP_NAMES):
        return SheetNameSelection(
            sheet_name=sheet.sheet_name,
            decision=SheetNameDecision.SKIP,
            role_hint=_skip_role_hint(normalized),
            evidence=["Sheet name resembles a calculation, analysis, check, statistics, payroll, or balance sheet tab."],
        )
    department_candidates = _department_candidates(evidence_text)
    if any(term in evidence_text for term in _CONDITIONAL_DETAIL_NAMES):
        return SheetNameSelection(
            sheet_name=sheet.sheet_name,
            decision=SheetNameDecision.TRIAGE,
            role_hint=SheetNameRoleHint.SUPPORTING_SCHEDULE,
            department_candidates=department_candidates,
            needs_sheet_enrichment=True,
            evidence=[
                "Sheet name resembles an F&B or OOD detail schedule that may be needed for department-level validation or fallback extraction."
            ],
        )
    non_summary_candidates = [candidate for candidate in department_candidates if candidate.department != "summary"]
    if non_summary_candidates and any(term in evidence_text for term in _DEPARTMENT_NAMES):
        return SheetNameSelection(
            sheet_name=sheet.sheet_name,
            decision=SheetNameDecision.TRIAGE,
            role_hint=SheetNameRoleHint.DEPARTMENT_P_AND_L,
            department_candidates=department_candidates,
            needs_sheet_enrichment=True,
            evidence=["Sheet name contains a department-specific signal."],
        )
    if any(term in evidence_text for term in _SUMMARY_NAMES):
        return SheetNameSelection(
            sheet_name=sheet.sheet_name,
            decision=SheetNameDecision.TRIAGE,
            role_hint=SheetNameRoleHint.SUMMARY_P_AND_L,
            department_candidates=department_candidates,
            needs_sheet_enrichment=True,
            evidence=["Sheet name resembles a summary P&L tab."],
        )
    if any(term in evidence_text for term in _DEPARTMENT_NAMES):
        return SheetNameSelection(
            sheet_name=sheet.sheet_name,
            decision=SheetNameDecision.TRIAGE,
            role_hint=SheetNameRoleHint.DEPARTMENT_P_AND_L,
            department_candidates=department_candidates,
            needs_sheet_enrichment=True,
            evidence=["Sheet name resembles a P&L department or schedule tab."],
        )
    return SheetNameSelection(
        sheet_name=sheet.sheet_name,
        decision=SheetNameDecision.UNSURE,
        role_hint=SheetNameRoleHint.UNKNOWN,
        evidence=["Sheet name is not descriptive enough for name-only routing."],
    )


def _apply_workbook_context(selections: list[SheetNameSelection]) -> list[SheetNameSelection]:
    normalized_by_name = {selection.sheet_name: _normalize_sheet_name(selection.sheet_name) for selection in selections}
    compact_names = {name: normalized.replace(" ", "") for name, normalized in normalized_by_name.items()}
    has_main_summary = any(
        normalized == "summary"
        or "summary operating statement" in normalized
        or "operating statement" in normalized
        or "statement of income" in normalized
        for normalized in normalized_by_name.values()
    )
    has_combined_banquet_catering = any(
        "banquetcatering" in compact or "banquetandcatering" in compact or "banquet&catering" in normalized
        for normalized, compact in ((normalized_by_name[name], compact) for name, compact in compact_names.items())
    )
    has_explicit_fnb_summary = any(
        compact in {"fbsummary", "fandbsummary", "foodbeveragesummary", "foodandbeveragesummary"}
        or ("summary" in normalized and ("fb" in normalized or "f&b" in normalized or "food" in normalized or "beverage" in normalized))
        for name, normalized in normalized_by_name.items()
        for compact in [compact_names[name]]
    )
    has_explicit_fnb_detail = any(
        any(term in normalized for term in ("outlet", "restaurant", "bar", "room service", "roomservice", "in room dining", "banquet", "catering"))
        for normalized in normalized_by_name.values()
    )

    adjusted: list[SheetNameSelection] = []
    for selection in selections:
        normalized = normalized_by_name[selection.sheet_name]
        compact = compact_names[selection.sheet_name]
        if has_main_summary and normalized in {"deptsummary", "department summary", "dept summary"}:
            adjusted.append(
                selection.model_copy(
                    update={
                        "decision": SheetNameDecision.SKIP,
                        "role_hint": SheetNameRoleHint.SUPPORTING_SCHEDULE,
                        "department_candidates": [],
                        "needs_sheet_enrichment": False,
                        "evidence": selection.evidence
                        + [
                            "Workbook also has a main Summary tab; department-summary tabs are duplicate/supporting rollups and should be skipped unless the main Summary lacks required lines."
                        ],
                    }
                )
            )
            continue
        if has_explicit_fnb_summary and has_explicit_fnb_detail and re.fullmatch(r"fbdept\d+", compact):
            adjusted.append(
                selection.model_copy(
                    update={
                        "decision": SheetNameDecision.SKIP,
                        "role_hint": SheetNameRoleHint.SUPPORTING_SCHEDULE,
                        "department_candidates": [],
                        "needs_sheet_enrichment": False,
                        "evidence": selection.evidence
                        + [
                            "Workbook has explicit F&B summary and outlet/detail tabs; generic coded FBDept tabs are treated as duplicate/supporting schedules."
                        ],
                    }
                )
            )
            continue
        if has_combined_banquet_catering and normalized in {"banquet", "catering"}:
            adjusted.append(
                selection.model_copy(
                    update={
                        "decision": SheetNameDecision.SKIP,
                        "role_hint": SheetNameRoleHint.SUPPORTING_SCHEDULE,
                        "department_candidates": [],
                        "needs_sheet_enrichment": False,
                        "evidence": selection.evidence
                        + [
                            "Workbook has a combined Banquet/Catering tab; standalone Banquet and Catering tabs are treated as subsections/supporting detail."
                        ],
                    }
                )
            )
            continue
        adjusted.append(selection)
    return adjusted


def _skip_role_hint(normalized: str) -> SheetNameRoleHint:
    if "balance" in normalized:
        return SheetNameRoleHint.BALANCE_SHEET
    if "check" in normalized or "analysis" in normalized:
        return SheetNameRoleHint.CHECK_OR_ANALYSIS
    return SheetNameRoleHint.SUPPORTING_SCHEDULE


def _department_candidates(normalized: str) -> list[DepartmentCandidate]:
    patterns = [
        ("summary", ("summary", "statement")),
        ("food_and_beverage", ("fb", "fbsummary", "fbdept", "f&b", "food", "beverage", "banquet", "catering", "event technology", "event tech", "outlet", "venue", "restaurant", "bar", "room service", "roomservice", "in room dining", "in-room dining", "ird", "minibar", "mini bar")),
        ("rooms", ("rooms", "guestroom")),
        ("other_operated_departments", ("other operated", "other operating", "oth op", "oth_op", "minor op", "minorop", "parking", "retail", "guest communications", "guest communication", "guest laundry", "recreation", "spa", "golf", "gift shop", "giftshop")),
        ("miscellaneous_income", ("misc income", "miscincome", "miscellaneous")),
        ("administrative_and_general", ("admin", "a&g")),
        ("information_and_telecommunications_systems", ("it", "information", "telecom", "telephone")),
        ("sales_and_marketing", ("sm", "s&m", "sales", "marketing")),
        ("property_operations_and_maintenance", ("rm", "engineering", "property opera", "pom", "maintenance")),
        ("utilities", ("utilities", "energy", "ec", "water", "waste")),
        ("management_fees", ("management", "mgmt", "mgt", "fees")),
        ("franchise_fees", ("franchise", "royalty")),
        ("non_operating_income_and_expense", ("nonop", "non op", "fixedexp", "fixed exp", "rent")),
    ]
    candidates: list[DepartmentCandidate] = []
    compact = normalized.replace(" ", "")
    for department, terms in patterns:
        matched_terms = [term for term in terms if _contains_sheet_term(normalized, compact, term)]
        if matched_terms:
            candidates.append(
                DepartmentCandidate(
                    department=department,
                    evidence=[f"Sheet name contains: {', '.join(matched_terms)}."],
                )
            )
    return candidates


def _normalize_sheet_name(sheet_name: str) -> str:
    return re.sub(r"\s+", " ", sheet_name.lower().replace("_", " ")).strip()


def _is_guest_laundry_sheet(normalized: str) -> bool:
    compact = normalized.replace(" ", "")
    return "guest laundry" in normalized or "guestlaundry" in compact


def _contains_sheet_term(normalized: str, compact: str, term: str) -> bool:
    term_compact = term.replace(" ", "")
    if len(term_compact) <= 2:
        return bool(re.search(rf"(^|[^a-z0-9]){re.escape(term_compact)}([^a-z0-9]|$)", normalized))
    return term in normalized or term_compact in compact
