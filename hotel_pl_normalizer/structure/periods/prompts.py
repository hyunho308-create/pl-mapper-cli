"""Render the period prompts, and the compact views they embed.

Kept apart from validation deliberately: what a model is asked and what its answer
must satisfy are different contracts, and they change for different reasons. The
views here exist to keep a prompt small enough to be answerable -- a full packet
is far larger than the decision needs.
"""

from __future__ import annotations

import json
import re
from importlib import resources

from openpyxl.utils import get_column_letter

from hotel_pl_normalizer.models.common import Severity
from hotel_pl_normalizer.models.period_selection import (
    PeriodCatalog,
    PeriodColumnPacket,
    PeriodSheetPacket,
    PeriodSheetRole,
)

from .signals import (
    _discovery_representatives,
    _excel_column_number,
    _looks_like_supporting_schedule_sheet,
    _packet_location_id,
    _period_representative_groups,
)


def render_period_catalog_prompt(
    packet: PeriodColumnPacket,
    *,
    include_skill: bool = True,
    representative: bool = False,
) -> str:
    sections = []
    if include_skill and not representative:
        sections.append(_load_period_selection_skill())
    if representative:
        sections.append(
            "## Fast period discovery\n\n"
            "Identify the periods consistently available on the sampled core hotel "
            "P&L sheets. Read each rectangular header_window spatially, as a human "
            "would read the workbook. Lower, column-specific scenario headers override "
            "broad report titles above them; merged_spans apply only to their stated "
            "row and column range. Metadata above the true header band is context, not "
            "a scenario label for every column beneath it. Bind each option to one "
            "representative header column on every sampled core P&L sheet that shows "
            "the heading. These discovery bindings are evidence anchors only and are "
            "discarded after period selection. Do not decide whether the cited column "
            "is the eventual numeric value, percentage, variance, or comparison column. "
            "Do not expose a period found only on a summary or supporting sheet.\n\n"
            "Recognize the two common layouts: (1) Jan-Dec Actual columns followed by "
            "Total Actual and optional full-year Budget, Forecast, or Last Year; and "
            "(2) PTD/MTD Actual, Budget, Forecast, or Last Year followed by the same "
            "YTD scenarios. Classify a Month-To-Date, Period-To-Date, MTD, PTD, or "
            "Periodic heading as period_type=current_period; reserve period_type=month for a "
            "standalone named month in a monthly spread. A bare Total immediately "
            "after Jan-Dec inherits that "
            "monthly block's year and scenario; do not relabel it from a comparison "
            "column farther to the right. Audit both PTD/MTD and YTD groups for every "
            "Actual, Budget, Forecast, and Last Year subcolumn, including MTD Last "
            "Year. Return every supported combination, but never invent one. "
            "Use the visible row/column hierarchy and merged spans in header_window. "
            "When the report title identifies the reporting month, set "
            "start_period and end_period in YYYY-MM form; shift Prior/Last Year back "
            "one year. A scenario heading still counts when adjacent subcolumns contain "
            "percentages, variances, POR, or other calculations; discovery is about "
            "whether the period heading exists. Before returning a 12-month catalog, "
            "perform a final right-edge audit: a Total immediately after Jan-Dec is the "
            "full-year Actual even if the cell below Total is blank, and a nearby year "
            "with Forecast 12 is a full-year Forecast. Include either concept when its "
            "heading is present on every primary_core sheet, even when its column shifts.\n\n"
            "Return one sheet_assessment for every sampled sheet. Use primary_core "
            "for the coherent, mapping-ready P&L family whose period availability "
            "should be compared; alternate_core for a legitimate but different "
            "report product such as a Trend, Detail, or YTD-only view; and supporting "
            "for Critique, KPI, statistics, payroll, or ancillary schedules. A "
            "departmental schedule containing dollar P&L accounts is primary_core "
            "when it shares the main report's period layout; never call it supporting "
            "merely because its tab name contains Schedule or a department name. When "
            "multiple departmental P&L sheets are sampled, the primary_core cohort "
            "must include a non-summary sheet. Alternate/supporting sheets neither "
            "veto nor introduce selectable periods. Include a short reason and exact "
            "sheet/header evidence for each role. Return only a PeriodCatalog."
        )
    sections.extend(
        [
            "## Input PeriodColumnPacket",
            (
                "Quickly discover canonical period concepts from the supplied spatial "
                "header windows. Bind each option to the representative location_id "
                "for every sampled core P&L sheet whose heading supports it. Omit summary-only "
                "or inconsistently available periods; a genuine Stats/KPI/supporting "
                "sheet may be ignored."
                if representative
                else "Discover every useful dollar period present in this compact candidate-column packet."
            ),
            "```json",
            json.dumps(
                _period_prompt_view(
                    packet,
                    include_requested_period=False,
                    representative=representative,
                ),
                separators=(",", ": "),
            ),
            "```",
            "Return only JSON matching the PeriodCatalog schema.",
        ]
    )
    return "\n\n".join(sections)


def render_period_catalog_repair_prompt(
    packet: PeriodColumnPacket,
    catalog: PeriodCatalog,
    *,
    representative: bool = False,
) -> str:
    """Ask for targeted corrections without replacing the discovered catalog."""
    errors = [
        issue.model_dump(mode="json")
        for issue in catalog.validation.issues
        if issue.severity == Severity.ERROR
    ]
    failed_period_ids = {
        issue.period_id
        for issue in catalog.validation.issues
        if issue.severity == Severity.ERROR and issue.period_id is not None
    }
    failed_options = [
        option.model_dump(mode="json")
        for option in catalog.options
        if option.period_id in failed_period_ids
    ]
    if representative:
        repair_input = _representative_repair_prompt_view(packet, catalog)
        return "\n\n".join(
            [
                "## Targeted period-discovery repair",
                "Inspect only the supplied spatial header windows and validation "
                "errors. Return patch operations, not a replacement catalog; omitted "
                "periods remain unchanged. Bind a supported period to a representative "
                "header column. If a sampled sheet is genuinely Stats, KPI, payroll, or "
                "another non-dollar supporting schedule, mark it unavailable with "
                "concrete header evidence and do not veto the period. Remove a period "
                "only when it is partial, summary-only, or inconsistent across core "
                "P&L sheets. Use sheet_assessment_patches when a validation error "
                "shows that a sampled sheet belongs to a different report product or "
                "is supporting rather than primary_core.",
                "Repair sheets:",
                "```json",
                json.dumps(repair_input, separators=(",", ": ")),
                "```",
                "Rejected period options:",
                "```json",
                json.dumps(failed_options, separators=(",", ":")),
                "```",
                "Validation errors:",
                "```json",
                json.dumps(errors, separators=(",", ":")),
                "```",
                "Return only JSON matching the PeriodCatalogRepair schema.",
            ]
        )
    return "\n\n".join(
        [
            render_period_catalog_prompt(packet),
            "## Repair only the validator-rejected catalog locations",
            "Return targeted patch operations, not a replacement PeriodCatalog. "
            "Omitted periods and locations remain unchanged. Use binding_patches to "
            "add a missing representative binding or replace a failed location with "
            "an exact candidate column. A representative-coverage error naming a "
            "location requires a binding_patch for that location when its headers "
            "support the period. Use "
            "concept_patches only to correct rejected scenario, type, or dates. Use "
            "unavailable_patches when the period is absent from that representative "
            "layout. Use removal_patches only when the complete period concept is "
            "partial, summary-only, or inconsistently available; every removal needs "
            "a concrete reason and exact header evidence. Never remove a period merely "
            "because one binding needs a different column.",
            (
                "Use only representative location ids and columns in the input."
                if representative
                else "Use only location ids and columns in the input."
            ),
            "Rejected period options:",
            "```json",
            json.dumps(failed_options, separators=(",", ":")),
            "```",
            "Validation errors:",
            "```json",
            json.dumps(errors, separators=(",", ":")),
            "```",
            "Return only JSON matching the PeriodCatalogRepair schema.",
        ]
    )


def render_selected_period_binding_repair_prompt(
    packet: PeriodColumnPacket,
    discovery_catalog: PeriodCatalog,
    catalog: PeriodCatalog,
) -> str:
    """Repair selected bindings without reopening period discovery."""
    errors = [
        issue.model_dump(mode="json")
        for issue in catalog.validation.issues
        if issue.severity == Severity.ERROR
    ]
    failed_pairs = {
        (issue.period_id, issue.location_id)
        for issue in catalog.validation.issues
        if issue.severity == Severity.ERROR
        and issue.period_id is not None
        and issue.location_id is not None
    }
    failed_period_ids = {period_id for period_id, _ in failed_pairs}
    failed_location_ids = {location_id for _, location_id in failed_pairs}
    selected_concepts = [
        option.model_copy(update={"bindings": [], "unavailable_locations": []})
        for option in discovery_catalog.options
        if option.period_id in failed_period_ids
    ]
    repair_packet = packet.model_copy(
        update={
            "sheets": [
                sheet
                for sheet in packet.sheets
                if _packet_location_id(sheet) in failed_location_ids
            ]
        }
    )
    return "\n\n".join(
        [
            "## Repair the previous selected-period bindings",
            _load_period_selection_skill(),
            "Return corrections only for the failed period/location pairs listed "
            "below. Inspect each supplied sheet independently. Each binding must "
            "contain one location_id. Do not repeat valid bindings and do not return "
            "unselected periods.",
            "Selected concepts needing repair:",
            "```json",
            json.dumps(
                [option.model_dump(mode="json") for option in selected_concepts],
                separators=(",", ": "),
            ),
            "```",
            "Failed sheet packets:",
            "```json",
            json.dumps(
                _compact_selected_period_binding_view(repair_packet),
                separators=(",", ": "),
            ),
            "```",
            "Validation errors:",
            "```json",
            json.dumps(errors, separators=(",", ":")),
            "```",
            "Return a PeriodCatalog containing only replacement decisions for the "
            "failed pairs. Other valid decisions will be retained automatically.",
        ]
    )


def render_selected_period_binding_prompt(
    packet: PeriodColumnPacket,
    discovery_catalog: PeriodCatalog,
    selected_period_ids: list[str],
) -> str:
    """Ask the period agent to bind only the concepts selected by the user."""
    wanted = set(selected_period_ids)
    selected = [
        option.model_copy(update={"bindings": [], "unavailable_locations": []})
        for option in discovery_catalog.options
        if option.period_id in wanted
    ]
    if len(selected) != len(wanted):
        raise ValueError("One or more selected periods are missing from discovery.")
    return "\n\n".join(
        [
            _load_period_selection_skill(),
            "## Bind only the user-selected periods",
            "Use the battle-tested sheet-level selection approach: inspect each sheet "
            "independently and select the exact source column for each user-selected "
            "period. Return exactly one decision for every selected period and every "
            "location_id. Each PeriodColumnBinding must contain exactly one location_id; "
            "do not group sheets even when they share a column. Use an "
            "unavailable_locations entry only when the period genuinely is not present. "
            "Do not discover unselected periods or infer an exceptional tab from the "
            "dominant workbook layout. Bind financial amount columns, not adjacent "
            "percentage, margin, ratio, variance, or KPI columns. Treat number-format "
            "and value-shape counts as stronger evidence than a merged header shared "
            "by an amount column and a percentage column.",
            "Selected concepts:",
            "```json",
            json.dumps(
                [option.model_dump(mode="json") for option in selected],
                separators=(",", ": "),
            ),
            "```",
            "Department period packet:",
            "```json",
            json.dumps(
                _compact_selected_period_binding_view(packet),
                separators=(",", ": "),
            ),
            "```",
            "Return only JSON matching the PeriodCatalog schema.",
        ]
    )


def _compact_selected_period_binding_view(packet: PeriodColumnPacket) -> dict:
    """Main-branch-style candidate packet used only after period selection."""
    return {
        "source_filename": packet.source_filename,
        "sheets": [
            {
                "location_id": _packet_location_id(sheet),
                "sheet_name": sheet.sheet_name,
                "department": sheet.department,
                "candidate_columns": [
                    {
                        "column": candidate.column,
                        "excel_column": candidate.excel_column,
                        "header_context": candidate.header_context,
                        "numeric_count": candidate.numeric_count,
                        "nonzero_count": candidate.nonzero_count,
                        "percentage_format_count": candidate.percentage_format_count,
                        "subunit_nonzero_count": candidate.subunit_nonzero_count,
                        "material_value_count": candidate.material_value_count,
                        "percentage_scale_count": candidate.percentage_scale_count,
                        "large_amount_count": candidate.large_amount_count,
                        "sample_values": [
                            {"label": sample.label, "value": sample.value}
                            for sample in candidate.sample_values[:3]
                        ],
                    }
                    for candidate in sheet.candidate_columns
                ],
            }
            for sheet in packet.sheets
        ],
    }


def _period_prompt_view(
    packet: PeriodColumnPacket,
    *,
    include_requested_period: bool = True,
    representative: bool = False,
) -> dict:
    if representative:
        representatives = _discovery_representatives(packet)
        view = {
            "source_filename": packet.source_filename,
            "sampled_sheets": [
                {
                    "represented_sheet_count": len(members),
                    **_period_discovery_sheet_prompt_record(members[0]),
                }
                for members in representatives
            ],
        }
        if include_requested_period:
            view["requested_period"] = packet.requested_period
        return view
    view = {
        "source_filename": packet.source_filename,
        "sheets": [_period_sheet_prompt_record(sheet) for sheet in packet.sheets],
    }
    if include_requested_period:
        view["requested_period"] = packet.requested_period
    return view


def _period_sheet_prompt_record(sheet: PeriodSheetPacket) -> dict:
    return {
        "location_id": _packet_location_id(sheet),
        "sheet_name": sheet.sheet_name,
        "department": sheet.department,
        "header_cells": [
            {"coordinate": cell.coordinate, "value": cell.value}
            for cell in sheet.header_cells
        ],
        "merged_headers": [
            {"range": merged.range, "value": merged.value}
            for merged in sheet.merged_headers
        ],
        "candidate_columns": [
            {
                "column": candidate.column,
                "excel_column": candidate.excel_column,
                "numeric_count": candidate.numeric_count,
                "nonzero_count": candidate.nonzero_count,
            }
            for candidate in sheet.candidate_columns
        ],
    }


def _period_discovery_sheet_prompt_record(sheet: PeriodSheetPacket) -> dict:
    """A spatial header window; exact value-column binding uses a later packet."""
    cell_lookup = {}
    for cell in sheet.spatial_header_cells:
        match = re.fullmatch(r"([A-Z]+)(\d+)", cell.coordinate)
        if match:
            cell_lookup[(int(match.group(2)), _excel_column_number(match.group(1)))] = cell.value
    end_column = sheet.spatial_header_end_column or 0
    rows = []
    if (
        sheet.spatial_header_start_row is not None
        and sheet.spatial_header_end_row is not None
    ):
        rows = [
            {
                "row": row,
                "cells": [
                    cell_lookup.get((row, column))
                    for column in range(1, end_column + 1)
                ],
            }
            for row in range(
                sheet.spatial_header_start_row,
                sheet.spatial_header_end_row + 1,
            )
        ]
    return {
        "location_id": _packet_location_id(sheet),
        "sheet_name": sheet.sheet_name,
        "header_window": {
            "columns": [
                get_column_letter(column) for column in range(1, end_column + 1)
            ],
            "rows": rows,
            "merged_spans": [
                {"range": merged.range, "value": merged.value}
                for merged in sheet.spatial_merged_headers
            ],
        },
    }


def _representative_repair_prompt_view(
    packet: PeriodColumnPacket, catalog: PeriodCatalog
) -> dict:
    """Send failed sampled sheets, then fill the five-sheet budget with new layouts."""
    groups = _period_representative_groups(packet)
    initial = _discovery_representatives(packet)
    failed_ids = {
        issue.location_id
        for issue in catalog.validation.issues
        if issue.severity == Severity.ERROR and issue.location_id
    }
    missing_concept = any(
        issue.severity == Severity.ERROR and issue.period_id is None
        for issue in catalog.validation.issues
    )
    if missing_concept:
        role_by_id = {
            item.location_id: item.role for item in catalog.sheet_assessments
        }
        selected = [
            members
            for members in initial
            if role_by_id.get(_packet_location_id(members[0]))
            == PeriodSheetRole.PRIMARY_CORE
        ]
    else:
        selected = [
            members
            for members in initial
            if _packet_location_id(members[0]) in failed_ids
        ]
    if not selected:
        selected = initial[:1]
    selected_ids = {_packet_location_id(members[0]) for members in selected}
    extras = sorted(
        (
            members
            for members in groups
            if _packet_location_id(members[0]) not in selected_ids
            and members not in initial
        ),
        key=lambda members: (
            not _looks_like_supporting_schedule_sheet(members[0].sheet_name),
            len(members),
            len(members[0].candidate_columns),
        ),
        reverse=True,
    )
    selected.extend(extras[: max(0, 5 - len(selected))])
    return {
        "source_filename": packet.source_filename,
        "sheets": [
            {
                "represented_sheet_count": len(members),
                **_period_discovery_sheet_prompt_record(members[0]),
            }
            for members in selected[:5]
        ],
    }


def _load_period_selection_skill() -> str:
    return resources.files("hotel_pl_normalizer.prompts").joinpath("period_column_selection.md").read_text(encoding="utf-8")
