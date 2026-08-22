"""Turn a validated catalog into the selections the mapping stage consumes.

Also holds the local, model-free backend, which is what makes the period stage
testable and runnable without an API key.
"""

from __future__ import annotations

import re
from collections import defaultdict

from hotel_pl_normalizer.models.common import ModelInfo
from hotel_pl_normalizer.models.period_selection import (
    PeriodCatalog,
    PeriodCatalogRepair,
    PeriodColumnBinding,
    PeriodColumnPacket,
    PeriodColumnSelection,
    PeriodColumnSelectionMap,
    PeriodOption,
    PeriodScenario,
    PeriodSheetAssessment,
    PeriodSheetRole,
    PeriodType,
)

from .signals import (
    _candidate_looks_like_rejected_metric_column,
    _local_period_identity,
    _packet_location_id,
)
from .validation import (
    validate_period_catalog,
    validate_period_column_selection,
)


class LocalPeriodCatalogBackend:
    """Deterministic test fallback that catalogs clearly labelled columns."""

    def run(self, packet: PeriodColumnPacket, prompt: str) -> PeriodCatalog:
        del prompt
        grouped: dict[
            tuple[str, PeriodScenario, PeriodType],
            list[tuple[str, int, str, list[str]]],
        ] = defaultdict(list)
        for sheet in packet.sheets:
            location_id = _packet_location_id(sheet)
            for candidate in sheet.candidate_columns:
                if _candidate_looks_like_rejected_metric_column(candidate):
                    continue
                label, scenario, period_type = _local_period_identity(candidate)
                key = (label, scenario, period_type)
                grouped[key].append(
                    (
                        location_id,
                        candidate.column,
                        candidate.excel_column,
                        candidate.header_context,
                    )
                )

        options = []
        used_ids: set[str] = set()
        for (label, scenario, period_type), locations in grouped.items():
            bindings_by_column: dict[
                tuple[int, str], list[tuple[str, list[str]]]
            ] = defaultdict(list)
            for location_id, column, excel_column, evidence in locations:
                bindings_by_column[(column, excel_column)].append(
                    (location_id, evidence)
                )
            period_id = _unique_period_id(_slug(label), used_ids)
            options.append(
                PeriodOption(
                    period_id=period_id,
                    label=label,
                    scenario=scenario,
                    period_type=period_type,
                    bindings=[
                        PeriodColumnBinding(
                            location_ids=[item[0] for item in location_evidence],
                            value_column=column,
                            excel_column=excel_column,
                            evidence=[
                                f"{location_id}: {header}"
                                for location_id, headers in location_evidence
                                for header in headers
                            ],
                        )
                        for (column, excel_column), location_evidence in sorted(
                            bindings_by_column.items()
                        )
                    ],
                )
            )
        options.sort(key=lambda item: (-_recommended_score(item), item.label))
        catalog = PeriodCatalog(
            catalog_id=f"{packet.workbook_id}:period_catalog",
            workbook_id=packet.workbook_id,
            model=ModelInfo(
                provider="local",
                model_name="period_catalog_heuristic",
                prompt_version="period_discovery_v1",
            ),
            sheet_assessments=[
                PeriodSheetAssessment(
                    location_id=_packet_location_id(sheet),
                    role=PeriodSheetRole.PRIMARY_CORE,
                    reason="Local deterministic discovery treats candidate-bearing sheets as core.",
                    evidence=[sheet.sheet_name],
                )
                for sheet in packet.sheets
            ],
            options=options,
            recommended_period_id=options[0].period_id if options else None,
        )
        return validate_period_catalog(packet, catalog)

    def repair(
        self, packet: PeriodColumnPacket, prompt: str
    ) -> PeriodCatalogRepair:
        del packet, prompt
        return PeriodCatalogRepair()


def prepare_selected_period_catalog(
    discovery_catalog: PeriodCatalog,
    response: PeriodCatalog,
    selected_period_ids: list[str],
) -> PeriodCatalog:
    """Keep discovery concepts authoritative and attach only model-selected columns."""
    response_by_id = {option.period_id: option for option in response.options}
    discovery_by_id = {option.period_id: option for option in discovery_catalog.options}
    options = []
    for period_id in dict.fromkeys(selected_period_ids):
        concept = discovery_by_id.get(period_id)
        if concept is None:
            raise ValueError(f"Selected period is missing from discovery: {period_id}")
        returned = response_by_id.get(period_id)
        options.append(
            concept.model_copy(
                update={
                    "bindings": (
                        _explode_period_bindings(returned.bindings)
                        if returned is not None
                        else []
                    ),
                    "unavailable_locations": (
                        returned.unavailable_locations if returned is not None else []
                    ),
                }
            )
        )
    return response.model_copy(
        update={
            "options": options,
            "recommended_period_id": options[0].period_id if options else None,
            "notes": [
                *response.notes,
                "binding_contract=sheet_period_v1",
            ],
        }
    )


def _explode_period_bindings(
    bindings: list[PeriodColumnBinding],
) -> list[PeriodColumnBinding]:
    """Normalize harmless model grouping without inventing any column decision."""
    return [
        binding.model_copy(update={"location_ids": [location_id]})
        for binding in bindings
        for location_id in binding.location_ids
    ]


def catalog_to_selection_map(
    packet: PeriodColumnPacket,
    catalog: PeriodCatalog,
    *,
    requested_period: str,
) -> PeriodColumnSelectionMap:
    option = next(
        (
            item
            for item in catalog.options
            if item.period_id == catalog.recommended_period_id
        ),
        None,
    )
    if option is None:
        return validate_period_column_selection(
            packet,
            PeriodColumnSelectionMap(
                selection_map_id=f"{packet.workbook_id}:period_columns",
                workbook_id=packet.workbook_id,
                requested_period=requested_period,
                model=catalog.model,
            ),
        )
    packet_by_location = {
        _packet_location_id(sheet): sheet for sheet in packet.sheets
    }
    candidate_lookup = {
        (location_id, candidate.column): candidate
        for location_id, sheet in packet_by_location.items()
        for candidate in sheet.candidate_columns
    }
    selections = []
    for binding in option.bindings:
        for location_id in binding.location_ids:
            sheet = packet_by_location.get(location_id)
            candidate = candidate_lookup.get((location_id, binding.value_column))
            if (
                sheet is None
                or candidate is None
                or candidate.excel_column != binding.excel_column
                or _candidate_looks_like_rejected_metric_column(candidate)
            ):
                continue
            selections.append(
                PeriodColumnSelection(
                    sheet_name=sheet.sheet_name,
                    department=sheet.department,
                    value_column=binding.value_column,
                    excel_column=binding.excel_column,
                    period_label=option.label,
                    evidence=["Derived from the validated recommended period catalog option."],
                    warnings=option.warnings,
                )
            )
    result = PeriodColumnSelectionMap(
        selection_map_id=f"{packet.workbook_id}:period_columns",
        workbook_id=packet.workbook_id,
        requested_period=requested_period,
        model=catalog.model,
        default_selection=_default_selection(selections),
        sheet_selections=selections,
    )
    return validate_period_column_selection(packet, result)


def selection_for_sheet(selection_map: PeriodColumnSelectionMap, sheet_name: str) -> PeriodColumnSelection | None:
    for selection in selection_map.sheet_selections:
        if selection.sheet_name == sheet_name:
            return selection
    if selection_map.default_selection is not None:
        return selection_map.default_selection
    return None


def _default_selection(selections: list[PeriodColumnSelection]) -> PeriodColumnSelection | None:
    if not selections:
        return None
    counts: dict[int, int] = defaultdict(int)
    for selection in selections:
        counts[selection.value_column] += 1
    default_column = max(counts, key=counts.get)
    for selection in selections:
        if selection.value_column == default_column:
            return selection.model_copy(update={"sheet_name": None, "department": None})
    return None


def _recommended_score(option: PeriodOption) -> int:
    score = {
        PeriodScenario.ACTUAL: 30,
        PeriodScenario.PRIOR_ACTUAL: 10,
        PeriodScenario.BUDGET: 5,
        PeriodScenario.FORECAST: 4,
        PeriodScenario.BLENDED: 3,
        PeriodScenario.UNKNOWN: 0,
    }[option.scenario]
    score += {
        PeriodType.YTD: 20,
        PeriodType.FULL_YEAR: 18,
        PeriodType.TTM: 16,
        PeriodType.MONTH: 8,
        PeriodType.CURRENT_PERIOD: 6,
        PeriodType.UNKNOWN: 0,
    }[option.period_type]
    score += len({location for binding in option.bindings for location in binding.location_ids})
    return score


def _slug(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_")
    return slug or "period"


def _unique_period_id(base: str, used: set[str]) -> str:
    candidate = base
    suffix = 2
    while candidate in used:
        candidate = f"{base}_{suffix}"
        suffix += 1
    used.add(candidate)
    return candidate
