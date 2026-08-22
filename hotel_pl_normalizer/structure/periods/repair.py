"""Fold targeted repair patches back into a catalog.

A validator rejection names what failed, and the repair stages return only the
pieces that change. Merging is deliberately additive and explicit -- omission
always means unchanged -- so a repair can never quietly drop a period nobody asked
it to touch.
"""

from __future__ import annotations

import re
from collections import defaultdict

from hotel_pl_normalizer.models.common import Severity
from hotel_pl_normalizer.models.period_selection import (
    PeriodCatalog,
    PeriodCatalogRepair,
    PeriodColumnBinding,
    PeriodColumnCandidate,
    PeriodColumnPacket,
    PeriodOption,
    PeriodSheetPacket,
    PeriodSheetRole,
    PeriodType,
    UnavailablePeriodLocation,
)

from .catalog import (
    _explode_period_bindings,
)
from .signals import (
    _candidate_looks_like_rejected_metric_column,
    _candidate_option_mismatch,
    _contains_mtd,
    _contains_ytd,
    _coverage_scenario_matches,
    _direct_header_values,
    _discovery_representatives,
    _local_period_identity,
    _nearest_explicit_month,
    _packet_location_id,
)


def merge_period_catalog_repair(
    catalog: PeriodCatalog, repair: PeriodCatalogRepair
) -> PeriodCatalog:
    """Merge explicit repairs while preserving every omitted option and binding."""
    failed_period_ids = {
        issue.period_id
        for issue in catalog.validation.issues
        if issue.severity == Severity.ERROR and issue.period_id is not None
    }
    failed_location_ids = {
        issue.location_id
        for issue in catalog.validation.issues
        if issue.severity == Severity.ERROR and issue.location_id is not None
    }
    options = {option.period_id: option for option in catalog.options}

    assessments = {
        item.location_id: item for item in catalog.sheet_assessments
    }
    for patch in repair.sheet_assessment_patches:
        if patch.location_id in failed_location_ids and patch.reason.strip():
            assessments[patch.location_id] = patch

    for removal in repair.removal_patches:
        if (
            removal.period_id in failed_period_ids
            and removal.period_id in options
            and removal.reason.strip()
            and removal.evidence
        ):
            del options[removal.period_id]

    for patch in repair.concept_patches:
        option = options.get(patch.period_id)
        if option is None or patch.period_id not in failed_period_ids:
            continue
        options[patch.period_id] = option.model_copy(
            update={
                "label": patch.label,
                "scenario": patch.scenario,
                "period_type": patch.period_type,
                "start_period": patch.start_period,
                "end_period": patch.end_period,
            }
        )

    for patch in repair.binding_patches:
        option = options.get(patch.period_id)
        if option is None or patch.period_id not in failed_period_ids:
            continue
        bindings = []
        for binding in option.bindings:
            remaining = [
                location_id
                for location_id in binding.location_ids
                if location_id != patch.location_id
            ]
            if remaining:
                bindings.append(binding.model_copy(update={"location_ids": remaining}))
        bindings.append(
            PeriodColumnBinding(
                location_ids=[patch.location_id],
                value_column=patch.value_column,
                excel_column=patch.excel_column,
                evidence=patch.evidence,
            )
        )
        unavailable = [
            item
            for item in option.unavailable_locations
            if item.location_id != patch.location_id
        ]
        options[patch.period_id] = option.model_copy(
            update={"bindings": bindings, "unavailable_locations": unavailable}
        )

    for patch in repair.unavailable_patches:
        option = options.get(patch.period_id)
        if (
            option is None
            or patch.period_id not in failed_period_ids
            or not patch.reason.strip()
        ):
            continue
        bindings = []
        for binding in option.bindings:
            remaining = [
                location_id
                for location_id in binding.location_ids
                if location_id != patch.location_id
            ]
            if remaining:
                bindings.append(binding.model_copy(update={"location_ids": remaining}))
        unavailable = [
            item
            for item in option.unavailable_locations
            if item.location_id != patch.location_id
        ]
        unavailable.append(
            UnavailablePeriodLocation(
                location_id=patch.location_id,
                reason=patch.reason,
                evidence=patch.evidence,
            )
        )
        options[patch.period_id] = option.model_copy(
            update={"bindings": bindings, "unavailable_locations": unavailable}
        )

    ordered = [
        options[option.period_id]
        for option in catalog.options
        if option.period_id in options
    ]
    recommended = catalog.recommended_period_id
    if recommended not in options:
        recommended = ordered[0].period_id if ordered else None
    notes = list(dict.fromkeys([*catalog.notes, *repair.notes]))
    return catalog.model_copy(
        update={
            "sheet_assessments": list(assessments.values()),
            "options": ordered,
            "recommended_period_id": recommended,
            "notes": notes,
        }
    )


def merge_selected_period_binding_repair(
    catalog: PeriodCatalog,
    repair: PeriodCatalog,
) -> PeriodCatalog:
    """Merge only validator-targeted period/location corrections."""
    failed_pairs = {
        (issue.period_id, issue.location_id)
        for issue in catalog.validation.issues
        if issue.severity == Severity.ERROR
        and issue.period_id is not None
        and issue.location_id is not None
    }
    repair_options = {option.period_id: option for option in repair.options}
    merged_options = []
    for option in catalog.options:
        failed_locations = {
            location_id
            for period_id, location_id in failed_pairs
            if period_id == option.period_id
        }
        patch = repair_options.get(option.period_id)
        if not failed_locations or patch is None:
            merged_options.append(option)
            continue
        kept_bindings = [
            binding
            for binding in option.bindings
            if not set(binding.location_ids).intersection(failed_locations)
        ]
        patch_bindings = [
            binding
            for binding in _explode_period_bindings(patch.bindings)
            if binding.location_ids[0] in failed_locations
        ]
        kept_unavailable = [
            item
            for item in option.unavailable_locations
            if item.location_id not in failed_locations
        ]
        patch_unavailable = [
            item
            for item in patch.unavailable_locations
            if item.location_id in failed_locations
        ]
        merged_options.append(
            option.model_copy(
                update={
                    "bindings": kept_bindings + patch_bindings,
                    "unavailable_locations": kept_unavailable + patch_unavailable,
                }
            )
        )
    return catalog.model_copy(update={"options": merged_options})


def align_unambiguous_period_bindings(
    packet: PeriodColumnPacket, catalog: PeriodCatalog
) -> PeriodCatalog:
    """Correct clear per-tab shifts while leaving ambiguous headers model-owned."""
    sheets = packet.sheets
    if packet.packet_id.endswith(":period_discovery"):
        sheets = [members[0] for members in _discovery_representatives(packet)]
    updated_options = []
    for option in catalog.options:
        resolved: dict[str, PeriodColumnCandidate] = {}
        for sheet in sheets:
            matches = []
            scenario_matches = []
            for candidate in sheet.candidate_columns:
                _, scenario, period_type = _local_period_identity(candidate)
                if (
                    period_type != PeriodType.UNKNOWN
                    and not _candidate_looks_like_rejected_metric_column(candidate)
                    and _candidate_option_mismatch(candidate, option) is None
                ):
                    matches.append(candidate)
                    if _coverage_scenario_matches(candidate, option):
                        scenario_matches.append(candidate)
            specific_matches = [
                candidate
                for candidate in scenario_matches
                if _candidate_has_specific_period_evidence(candidate, option)
            ]
            if len(specific_matches) == 1:
                choice = specific_matches
            else:
                choice = scenario_matches if len(scenario_matches) == 1 else matches
            if len(choice) == 1:
                resolved[_packet_location_id(sheet)] = choice[0]

        unresolved = {
            location_id
            for binding in option.bindings
            for location_id in binding.location_ids
            if location_id not in resolved
        }
        grouped: dict[tuple[int, str], list[str]] = defaultdict(list)
        for binding in option.bindings:
            for location_id in binding.location_ids:
                if location_id in unresolved:
                    grouped[(binding.value_column, binding.excel_column)].append(
                        location_id
                    )
        for location_id, candidate in resolved.items():
            grouped[(candidate.column, candidate.excel_column)].append(location_id)
        bindings = [
            PeriodColumnBinding(
                location_ids=location_ids,
                value_column=column,
                excel_column=excel_column,
            )
            for (column, excel_column), location_ids in grouped.items()
        ]
        updated_options.append(
            option.model_copy(
                update={
                    "bindings": bindings,
                    "unavailable_locations": [
                        item
                        for item in option.unavailable_locations
                        if item.location_id not in resolved
                    ],
                }
            )
        )
    return catalog.model_copy(update={"options": updated_options})


def _candidate_has_specific_period_evidence(
    candidate: PeriodColumnCandidate, option: PeriodOption
) -> bool:
    direct = " ".join(_direct_header_values(candidate)).lower()
    if option.period_type == PeriodType.MONTH:
        return _nearest_explicit_month(candidate) is not None
    if option.period_type == PeriodType.FULL_YEAR:
        return "total" in direct or bool(
            re.search(r"january\s*[-\u2013\u2014]\s*december", direct)
        )
    if option.period_type == PeriodType.YTD:
        return _contains_ytd(direct)
    if option.period_type == PeriodType.CURRENT_PERIOD:
        return _contains_mtd(direct)
    return False


def _sheet_role_hints(sheet: PeriodSheetPacket, *, limit: int = 4) -> list[str]:
    """Expose only title cells that materially help distinguish report purpose."""
    hints = []
    for cell in sheet.header_cells:
        value = re.sub(r"\s+", " ", cell.value).strip()
        if re.search(
            r"\b(?:statistical|statistics|kpi|critique|pace|trend|detail|summary|operating statement)\b",
            value,
            re.IGNORECASE,
        ):
            hints.append(f"{cell.coordinate}: {value}")
    for candidate in sheet.candidate_columns:
        for header in candidate.header_context:
            if re.search(
                r"\b(?:statistical|statistics|kpi|critique|pace|trend|detail|summary|operating statement)\b",
                header,
                re.IGNORECASE,
            ):
                hints.append(header)
    return list(dict.fromkeys(hints))[:limit]


def normalize_obvious_supporting_assessments(
    packet: PeriodColumnPacket, catalog: PeriodCatalog
) -> PeriodCatalog:
    """Guard against model drift when a sampled sheet explicitly names its role."""
    sampled = {
        _packet_location_id(members[0]): members[0]
        for members in _discovery_representatives(packet)
    }
    updated = []
    for item in catalog.sheet_assessments:
        sheet = sampled.get(item.location_id)
        role_text = " ".join(_sheet_role_hints(sheet)).lower() if sheet else ""
        if item.role == PeriodSheetRole.PRIMARY_CORE and re.search(
            r"\b(?:statistical summary|statistics|kpi|critique)\b", role_text
        ):
            item = item.model_copy(
                update={
                    "role": PeriodSheetRole.SUPPORTING,
                    "reason": "Explicit workbook title identifies a statistical, KPI, or critique schedule.",
                    "evidence": _sheet_role_hints(sheet),
                }
            )
        updated.append(item)
    return catalog.model_copy(update={"sheet_assessments": updated})
