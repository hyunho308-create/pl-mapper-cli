from __future__ import annotations

import json
import re
from dataclasses import dataclass
from importlib import resources
from typing import Any, Protocol

from hotel_pl_normalizer.models.common import ModelInfo
from hotel_pl_normalizer.models.department_location import (
    BoundaryConfidence,
    DepartmentIdentificationPacket,
    DepartmentLocation,
    DepartmentLocationKind,
    DepartmentLocationMap,
    DepartmentLocationPatch,
    DepartmentLocationValidation,
    DepartmentSectionRole,
)
from hotel_pl_normalizer.structure.departments.validation import (
    validate_department_location_map,
)

_DETAIL_START_DEPARTMENTS = {
    "rooms",
    "food_and_beverage",
    "other_operated_departments",
    "miscellaneous_income",
    "administrative_and_general",
    "information_and_telecommunications_systems",
    "sales_and_marketing",
    "property_operations_and_maintenance",
    "utilities",
}

_FNB_DETAIL_TERMS = (
    "restaurant",
    "bar",
    "lounge",
    "patio",
    "room service",
    "roomservice",
    "in room dining",
    "in-room dining",
    "ird",
    "banquet",
    "catering",
    "event technology",
    "event tech",
    "b&c",
    "bcc",
    "mini bar",
    "minibar",
    "retail",
    "venue",
    "outlet",
    "kitchen",
    "overhead",
    "food admin",
    "fb admin",
    "f&b admin",
    "f b admin",
    "food management",
    "fb management",
    "f&b management",
    "coffee",
    "cafe",
)

_OOD_DETAIL_TERMS = (
    "retail",
    "gift shop",
    "giftshop",
    "recreation",
    "spa",
    "golf",
    "parking",
    "telephone",
    "guest communication",
    "guest communications",
    "guest laundry",
    "transportation",
    "shuttle",
    "valet parking",
    "other operating",
    "minor op",
    "minor operated",
)

_SUPPORT_SHEET_TERMS = (
    "setup",
    "set up",
    "staff dining",
    "cafeteria",
    "benefits",
)

_PROMPT_MAX_LABELS_PER_PACKET = 36
_PROMPT_SINGLE_TAB_MAX_ROWS = 800
_DEPARTMENT_PROMPT_TERMS = (
    "summary",
    "revenue",
    "revenues",
    "allowance",
    "allowances",
    "cost",
    "costs",
    "sales",
    "labor",
    "salaries",
    "wages",
    "payroll",
    "expense",
    "expenses",
    "profit",
    "loss",
    "income",
    "rooms",
    "food",
    "beverage",
    "banquet",
    "catering",
    "room service",
    "in room dining",
    "other operated",
    "misc",
    "administrative",
    "general",
    "information",
    "telecommunications",
    "sales",
    "marketing",
    "property operations",
    "maintenance",
    "utilities",
    "energy",
    "management fees",
    "non operating",
    "ebitda",
)

_LOW_SIGNAL_LABELS = {
    "actual",
    "apply to",
    "autofitcol",
    "blankrow",
    "budget",
    "current year",
    "fontbold frame",
    "fontbold indentlevel",
    "for the period ended",
    "group",
    "local currency",
    "prior year actual",
    "value",
    "variance",
    "year to date",
}


class DepartmentIdBackend(Protocol):
    def run(
        self,
        packet: DepartmentIdentificationPacket,
        prompt: str,
        *,
        toolset: Any | None = None,
        trace: list[dict] | None = None,
    ) -> DepartmentLocationMap:
        """Return a model-shaped DepartmentLocationMap.

        `toolset`, when given, lets the backend read the workbook before
        answering. Backends that cannot are free to ignore it.
        """

    def run_patch(
        self,
        packet: DepartmentIdentificationPacket,
        prompt: str,
        target_map_id: str,
        *,
        toolset: Any | None = None,
        trace: list[dict] | None = None,
    ) -> DepartmentLocationPatch:
        """Return only the DepartmentLocation changes required by validation.

        `toolset`, when given, lets the repair read the workbook. Repair is where
        that matters most: the first answer was wrong, so re-reading the same
        prompt is the least promising move available.
        """


class LocalDepartmentIdBackend:
    def run(
        self,
        packet: DepartmentIdentificationPacket,
        prompt: str,
        *,
        toolset: Any | None = None,
        trace: list[dict] | None = None,
    ) -> DepartmentLocationMap:
        if _should_use_single_tab_range_detection(packet):
            locations = _single_tab_locations(packet, packet.compact_packets[0])
        else:
            locations = [_location_for_packet(packet, compact_packet) for compact_packet in packet.compact_packets]
            locations = [location for location in locations if location is not None]
        result = DepartmentLocationMap(
            department_location_map_id=f"{packet.workbook_id}:department_locations",
            workbook_id=packet.workbook_id,
            model=ModelInfo(
                provider="local",
                model_name="department_id_heuristic",
                prompt_version="department_id_v1",
            ),
            workbook_layout=packet.sheet_name_selection.workbook_layout.value,
            locations=locations,
            validation=DepartmentLocationValidation(status="skipped"),
            notes=[
                "Local Department ID is a development helper only; production should use the Department ID model call."
            ],
        )
        return result

    def run_patch(
        self,
        packet: DepartmentIdentificationPacket,
        prompt: str,
        target_map_id: str,
        *,
        toolset: Any | None = None,
        trace: list[dict] | None = None,
    ) -> DepartmentLocationPatch:
        return DepartmentLocationPatch(
            patch_id=f"{target_map_id}:patch",
            target_map_id=target_map_id,
            workbook_id=packet.workbook_id,
            model=ModelInfo(
                provider="local",
                model_name="department_id_heuristic",
                prompt_version="department_id_patch_v1",
            ),
            rationale="Local development helper does not modify model-produced locations.",
        )


@dataclass(frozen=True)
class DepartmentIdAgentOutput:
    location_map: DepartmentLocationMap
    validation: DepartmentLocationValidation
    prompt: str
    repair_improved: bool | None = None


class DepartmentIdAgent:
    def __init__(
        self,
        backend: DepartmentIdBackend | None = None,
        *,
        workbook_sheet_names: set[str] | None = None,
        toolset: Any | None = None,
        repair_toolset: Any | None = None,
    ) -> None:
        self.backend = backend or LocalDepartmentIdBackend()
        # Lets validation tell "this sheet does not exist" apart from "routing
        # did not select it". The second is a warning; treating it as an error
        # abandons the entire property.
        self.workbook_sheet_names = workbook_sheet_names
        # Tools are split by pass on purpose. Measured on GRY, offering them on the
        # first pass changed nothing except cost: the model either declined them
        # entirely, or read the right sheet and reached the same answer. Repair is
        # the pass where a check has already failed, so evidence beats re-reading.
        self.toolset = toolset
        self.repair_toolset = repair_toolset
        self.tool_trace: list[dict] = []

    def run(self, packet: DepartmentIdentificationPacket) -> DepartmentIdAgentOutput:
        prompt = render_department_id_prompt(
            packet, tools_available=self.toolset is not None
        )
        if self.toolset is not None:
            raw_location_map = self.backend.run(
                packet, prompt, toolset=self.toolset, trace=self.tool_trace
            )
        else:
            raw_location_map = self.backend.run(packet, prompt)
        validation = validate_department_location_map(
            packet,
            raw_location_map,
            workbook_sheet_names=self.workbook_sheet_names,
        )
        location_map = raw_location_map.model_copy(update={"validation": validation})
        return DepartmentIdAgentOutput(location_map=location_map, validation=validation, prompt=prompt)

    def _request_patch(
        self,
        packet: DepartmentIdentificationPacket,
        prompt: str,
        prior_location_map: DepartmentLocationMap,
    ) -> DepartmentLocationPatch:
        if self.repair_toolset is not None:
            return self.backend.run_patch(
                packet,
                prompt,
                prior_location_map.department_location_map_id,
                toolset=self.repair_toolset,
                trace=self.tool_trace,
            )
        return self.backend.run_patch(
            packet,
            prompt,
            prior_location_map.department_location_map_id,
        )

    def repair(
        self,
        packet: DepartmentIdentificationPacket,
        prior_location_map: DepartmentLocationMap,
        prior_validation: DepartmentLocationValidation,
    ) -> DepartmentIdAgentOutput:
        prompt = render_department_id_repair_prompt(
            packet,
            prior_location_map,
            prior_validation,
            tools_available=self.repair_toolset is not None,
        )
        try:
            patch = self._request_patch(packet, prompt, prior_location_map)
        except Exception:
            # Repair is best effort by definition: the prior map is already
            # validated and usable, so a repair that cannot produce a patch --
            # a tool session that runs out of turns, a transport failure -- must
            # leave it standing rather than fail the property. GRY lost all 156
            # accounts to a repair hitting the tool-loop cap.
            return DepartmentIdAgentOutput(
                location_map=prior_location_map,
                validation=prior_validation,
                prompt=prompt,
                repair_improved=False,
            )
        try:
            patched_location_map = apply_department_location_patch(prior_location_map, patch)
        except ValueError:
            # A patch naming a location that does not exist is the extreme case of
            # a patch that improves nothing: the model misread the map it was
            # asked to correct. Treat it the same way -- keep the prior map and
            # let its validation issues stand -- rather than failing the whole
            # property. One hallucinated id previously cost all 156 accounts.
            return DepartmentIdAgentOutput(
                location_map=prior_location_map,
                validation=prior_validation,
                prompt=prompt,
                repair_improved=False,
            )
        validation = validate_department_location_map(
            packet,
            patched_location_map,
            workbook_sheet_names=self.workbook_sheet_names,
        )
        if not _department_patch_improves_validation(prior_validation, validation):
            return DepartmentIdAgentOutput(
                location_map=prior_location_map,
                validation=prior_validation,
                prompt=prompt,
                repair_improved=False,
            )
        location_map = patched_location_map.model_copy(update={"validation": validation})
        return DepartmentIdAgentOutput(
            location_map=location_map,
            validation=validation,
            prompt=prompt,
            repair_improved=True,
        )


TOOL_GUIDANCE = """## Tools

You can read the workbook before you answer. The packet below is a *summary* of
the sheets that upstream routing selected, and routing is not always right: it
sometimes omits a real schedule, and the summary omits rows. Both are fixable by
looking.

- `inspect_workbook` -- every sheet in the workbook, including ones missing from
  the packet. Call this first if a department seems to have no home.
- `find_rows` -- locate a label without guessing a row number. Use it to place a
  section boundary, or to confirm a schedule is what its sheet name suggests.
- `read_range` -- read a contiguous band of rows.
- `read_nonzero_rows` -- read only rows carrying a nonzero number; usually the
  cheapest way to see a whole schedule.
- `read_sparse_ranges` -- read several disjoint bands in one call.

These are available whenever they would help. Cases where they typically do:

- **A department has no obvious location.** `inspect_workbook` may show its sheet
  exists but was never routed in. A location on a real sheet the packet lacks is
  correct and accepted -- its data is fetched on demand -- so a department is worth
  a look before being reported as absent.
- **A boundary is uncertain.** `start_row` and `end_row` that cut off a schedule
  silently lose every account below the cut, so reading around the edge is
  cheaper than estimating it.
- **A sheet name is ambiguous** (`EC`, `OTH_OP`, `MISC_INC`). A few rows will say
  what the labels actually are.

Read whatever you need, then answer. Where the packet already settles a
department, answering directly is correct -- there is no need to spend a call
confirming what you can already see."""


def render_department_id_prompt(
    packet: DepartmentIdentificationPacket,
    *,
    include_skill: bool = True,
    tools_available: bool = False,
) -> str:
    """Render the Department ID prompt.

    `tools_available` adds the tool guidance. It is off by default and added only
    when tools are genuinely offered, which keeps the tools-off arm's prompts
    byte-identical -- so it stays cache-warm and directly comparable, and the
    measured difference is the tools rather than a reworded prompt.

    The guidance is needed because offering tool declarations is not enough:
    3.6-flash on GRY reported `tool_loop=True, tool_calls=0`, answering straight
    from the packet while being told to "return only JSON".
    """
    sections: list[str] = []
    if include_skill:
        sections.append(_load_department_id_skill())
    if tools_available:
        sections.append(TOOL_GUIDANCE)
    sections.append("## Input DepartmentIdentificationPacket")
    sections.append("```json")
    sections.append(json.dumps(department_id_prompt_view(packet), separators=(",", ": ")))
    sections.append("```")
    if tools_available:
        sections.append(
            "Use the tools if anything above is uncertain, then return only JSON "
            "that conforms to DepartmentLocationMap."
        )
    else:
        sections.append("Return only JSON that conforms to DepartmentLocationMap.")
    return "\n\n".join(sections)


def render_department_id_repair_prompt(
    packet: DepartmentIdentificationPacket,
    prior_location_map: DepartmentLocationMap,
    prior_validation: DepartmentLocationValidation,
    *,
    include_skill: bool = True,
    tools_available: bool = False,
) -> str:
    """Render the repair prompt, optionally telling the model it can look.

    Off by default so the tools-off arm's prompts stay byte-identical.
    """
    sections: list[str] = []
    if include_skill:
        sections.append(_load_department_id_skill())
    if tools_available:
        sections.append(TOOL_GUIDANCE)
        sections.append(
            "A check below has already failed, so the packet alone did not settle "
            "it. Reading the relevant rows is usually more informative than "
            "re-reading this prompt."
        )
    sections.append("## Repair Task")
    sections.append(
        "Repair only the locations implicated by the validation issues. Return a DepartmentLocationPatch. "
        "Use `replacements` with a target_location_id when refining, reclassifying, or narrowing an existing location. "
        "Use `additions` only for a genuinely new location not represented in the prior map. "
        "Use `remove_location_ids` only when validation proves an existing location must be deleted. "
        "Omitting a location preserves it."
    )
    sections.append(
        "If a selected packet is duplicate, validation-only, KPI/statistics, supporting detail, or should not be a primary extraction source, "
        "do not omit it silently. Include it as a location with the best department and an appropriate section_role such as "
        "`supporting_detail`, `kpi`, or `detail`, and explain that role in evidence/open_questions. "
        "Exception: in a single-tab P&L, trailing payroll recap, wage statistics, department distribution, productivity-only, "
        "and similar post-P&L support ranges should be dropped/omitted rather than returned as department locations. "
        "For example, in a multi-tab workbook, a Department Summary tab beside a main Summary tab is usually "
        "`department=summary` and `section_role=supporting_detail`, not a second primary summary. "
        "For an oversized department sheet where only the top block is the actual department and later rows are metadata or checks, "
        "use `location_kind=range` with start_row/end_row for the actual department block. "
        "For short departments such as Miscellaneous Income, Utilities, Management Fees, and Non-Op, trim report headers, "
        "calendar metadata, validation rows, GL period tables, and other non-P&L support rows from the location bounds. "
        "When validation says an existing range contains a distinct department heading at a specific row, split the prior range at that "
        "heading and start the new department at the visible heading row. Exception: a short franchise, royalty, affiliation, brand, or "
        "loyalty-fee block embedded inside another department tab is overlapping S&M supporting detail; add that supporting range without "
        "truncating the surrounding primary department, especially when the primary schedule resumes below the fee block. Preserve "
        "unaffected locations from the prior map. "
        "For multi-tab workbooks, use the sheet-name selection evidence and department_candidates as strong context. "
        "A selected coded sheet with a candidate department should not remain unclassified merely because its row labels are generic. "
        "If it appears to be a child schedule under a parent department, include it with `section_role=detail` or "
        "`section_role=supporting_detail` instead of omitting it. "
        "When a department has a parent/rollup tab and several coded child tabs, keep the parent/rollup tab primary and "
        "classify the coded child tabs as detail/supporting_detail rather than additional primary department locations. "
        "For a single-tab P&L, use `section_role=primary` for ordinary department ranges such as Rooms, A&G, IT, S&M, POM, "
        "Utilities, Management Fees, Misc Income, and Non-Op. Franchise fees, royalty fees, brand fees, affiliation fees, and "
        "loyalty fees are part of S&M at Department ID and should not be split into a separate department. Use `section_role=detail` mainly for F&B outlet "
        "detail or OOD outlet detail. If Misc Income or other income lines are embedded inside the Other Operated / Minor Operated "
        "block rather than presented as a clear standalone department, keep that combined range as `department=other_operated_departments` "
        "and leave the misc-income split to the Subsection ID layer. "
        "The opening Summary range should start at the visible Summary heading, not at report "
        "title/date/header rows above it. If an F&B Summary range is missing labor, COGS, or opex because those rows were "
        "split into an adjacent F&B detail range on the same sheet, merge those adjacent rows back into the F&B Summary unless "
        "there is a separate outlet/venue section heading outside the consolidated F&B summary. If validation flags an "
        "unclassified gap with operated-department clues such as Telephone, Guest Communications, Transportation, Shuttle, "
        "Valet Parking, Spa, Retail, Gift Shop, Recreation, Golf, Guest Laundry, or Minor Op, add the relevant ranges as "
        "`department=other_operated_departments` and `section_role=detail` when the gap sits in the operated-departments block. "
        "Do not leave a validation-flagged operated department gap unclassified."
    )
    sections.append("## Prior DepartmentLocationMap")
    sections.append("```json")
    sections.append(json.dumps(_location_map_prompt_view(prior_location_map), separators=(",", ": ")))
    sections.append("```")
    sections.append("## Validation Issues To Repair")
    sections.append("```json")
    sections.append(json.dumps(_validation_prompt_view(prior_validation), separators=(",", ": ")))
    sections.append("```")
    sections.append("## Complete Relevant Label Evidence")
    sections.append("```json")
    sections.append(json.dumps(department_id_repair_prompt_view(packet), separators=(",", ": ")))
    sections.append("```")
    if tools_available:
        sections.append(
            "Use the tools if the failed check is unclear, then return only JSON "
            "that conforms to DepartmentLocationPatch."
        )
    else:
        sections.append("Return only JSON that conforms to DepartmentLocationPatch.")
    return "\n\n".join(sections)


def apply_department_location_patch(
    location_map: DepartmentLocationMap,
    patch: DepartmentLocationPatch,
) -> DepartmentLocationMap:
    if patch.target_map_id != location_map.department_location_map_id:
        raise ValueError("DepartmentLocationPatch target_map_id does not match the prior map.")
    if patch.workbook_id != location_map.workbook_id:
        raise ValueError("DepartmentLocationPatch workbook_id does not match the prior map.")

    remove_ids = set(patch.remove_location_ids)
    replacement_by_target = {
        replacement.target_location_id: replacement.location
        for replacement in patch.replacements
    }
    existing_ids = {location.location_id for location in location_map.locations}
    unknown_targets = set(replacement_by_target) - existing_ids
    if unknown_targets:
        raise ValueError(
            "DepartmentLocationPatch replacement targets do not exist: "
            + ", ".join(sorted(unknown_targets))
        )
    addition_ids = [location.location_id for location in patch.additions]
    if len(addition_ids) != len(set(addition_ids)):
        raise ValueError("DepartmentLocationPatch additions contain duplicate location IDs.")
    conflicting_additions = set(addition_ids) & (existing_ids - remove_ids)
    if conflicting_additions:
        raise ValueError(
            "DepartmentLocationPatch additions reuse existing location IDs: "
            + ", ".join(sorted(conflicting_additions))
        )
    locations = []
    for location in location_map.locations:
        if location.location_id in remove_ids:
            continue
        locations.append(replacement_by_target.get(location.location_id, location))
    locations.extend(patch.additions)
    notes = list(location_map.notes)
    if patch.rationale:
        notes.append(f"Department ID repair: {patch.rationale}")
    return location_map.model_copy(
        update={
            "locations": locations,
            "model": patch.model,
            "notes": notes,
        }
    )


def _department_patch_improves_validation(
    prior: DepartmentLocationValidation,
    candidate: DepartmentLocationValidation,
) -> bool:
    prior_keys = {_department_issue_key(issue) for issue in prior.issues}
    candidate_keys = {_department_issue_key(issue) for issue in candidate.issues}
    if not candidate_keys:
        return True
    if len(candidate_keys) < len(prior_keys):
        return True
    return candidate_keys < prior_keys


def _department_issue_key(issue) -> tuple:
    return (
        issue.severity,
        issue.issue_type,
        issue.department,
        issue.sheet_name,
        issue.message,
    )


def department_id_prompt_view(packet: DepartmentIdentificationPacket) -> dict:
    is_single_tab = packet.sheet_name_selection.workbook_layout.value == "single_tab_p_and_l"
    return {
        "source_filename": packet.source_filename,
        "sheet_name_selection": _sheet_name_selection_prompt_view(packet),
        "compact_packets": [
            _compact_packet_prompt_view(
                compact_packet,
                include_row_indexes=is_single_tab,
                max_items=_PROMPT_SINGLE_TAB_MAX_ROWS if is_single_tab else _PROMPT_MAX_LABELS_PER_PACKET,
                preserve_sheet_coverage=is_single_tab,
            )
            for compact_packet in packet.compact_packets
        ],
    }


def _location_map_prompt_view(location_map: DepartmentLocationMap) -> dict:
    """Only the editable location state belongs in a repair prompt."""
    return {
        "department_location_map_id": location_map.department_location_map_id,
        "locations": [
            {
                "location_id": item.location_id,
                "department": item.department,
                "sheet_name": item.sheet_name,
                "location_kind": item.location_kind.value,
                "start_row": item.start_row,
                "end_row": item.end_row,
                "section_role": item.section_role.value,
                "boundary_confidence": item.boundary_confidence.value,
            }
            for item in location_map.locations
        ],
    }


def department_id_repair_prompt_view(
    packet: DepartmentIdentificationPacket,
) -> dict:
    """All relevant labels, independent of validator-selected context windows."""
    return {
        "source_filename": packet.source_filename,
        "sheet_name_selection": _sheet_name_selection_prompt_view(packet),
        "compact_packets": [
            _compact_packet_prompt_view(
                compact_packet,
                include_row_indexes=True,
                max_items=len(compact_packet.rows),
                preserve_sheet_coverage=False,
            )
            for compact_packet in packet.compact_packets
        ],
    }


def _validation_prompt_view(validation: DepartmentLocationValidation) -> dict:
    return {
        "status": validation.status.value,
        "issues": [
            {
                "severity": issue.severity.value,
                "issue_type": issue.issue_type.value,
                "department": issue.department,
                "sheet_name": issue.sheet_name,
                "message": issue.message,
            }
            for issue in validation.issues
        ],
    }


def _sheet_name_selection_prompt_view(packet: DepartmentIdentificationPacket) -> dict:
    return {
        "workbook_layout": packet.sheet_name_selection.workbook_layout.value,
        "selections": [
            {
                "sheet_name": selection.sheet_name,
                "decision": selection.decision.value,
                "role_hint": selection.role_hint.value,
                "department_candidates": [
                    {"department": candidate.department}
                    for candidate in selection.department_candidates
                ],
            }
            for selection in packet.sheet_name_selection.selections
        ],
    }


def _compact_packet_prompt_view(
    compact_packet,
    *,
    windows: list[tuple[int, int]] | None = None,
    include_row_indexes: bool,
    max_items: int,
    preserve_sheet_coverage: bool,
) -> dict:
    rows = _prompt_rows(
        compact_packet,
        windows=windows,
        max_rows=max_items,
        preserve_sheet_coverage=preserve_sheet_coverage,
    )
    view = {
        "sheet_name": compact_packet.sheet_name,
        "start_row": compact_packet.start_row,
        "end_row": compact_packet.end_row,
        "row_count": len(compact_packet.rows),
        "included_label_count": len(rows),
    }
    if include_row_indexes:
        view["row_labels"] = [
            {
                "row_index": row.row_index,
                "label": row.label.raw,
            }
            for row in rows
        ]
    else:
        view["label_names"] = [row.label.raw for row in rows]
    return view


def _prompt_rows(
    compact_packet,
    *,
    windows: list[tuple[int, int]] | None,
    max_rows: int,
    preserve_sheet_coverage: bool,
):
    labeled_rows = [row for row in compact_packet.rows if row.label.raw and _is_relevant_prompt_label(row.label.raw)]
    if windows:
        window_rows = [
            row
            for row in labeled_rows
            if any(start_row <= row.row_index <= end_row for start_row, end_row in windows)
        ]
        if window_rows:
            return _dedupe_rows(window_rows[:max_rows])

    if preserve_sheet_coverage:
        return _single_tab_prompt_rows(labeled_rows, max_rows=max_rows)

    scored = [(_prompt_row_score(row), row) for row in labeled_rows]
    must_keep = [row for score, row in scored if score >= 8]
    if len(must_keep) >= max_rows:
        return _dedupe_rows(must_keep[:max_rows])

    remaining = [row for score, row in sorted(scored, key=lambda item: (-item[0], item[1].row_index)) if row not in must_keep]
    selected = must_keep + remaining[: max_rows - len(must_keep)]
    return _dedupe_rows(sorted(selected, key=lambda row: row.row_index))


def _single_tab_prompt_rows(labeled_rows, *, max_rows: int):
    selected = [
        row
        for row in labeled_rows
        if row.heuristic_hints.possible_section_boundary
        or _single_tab_key_total_label(row.label.normalized or row.label.raw or "")
    ]
    selected = _dedupe_rows(sorted(selected, key=lambda row: row.row_index))
    if len(selected) <= max_rows:
        return selected

    must_keep = [
        row
        for row in selected
        if row.heuristic_hints.possible_section_boundary
        and any(term in (row.label.normalized or row.label.raw or "").lower() for term in _DEPARTMENT_PROMPT_TERMS)
    ]
    remaining = [row for row in selected if row not in must_keep]
    if len(must_keep) >= max_rows:
        return _evenly_sample_rows(must_keep, max_rows=max_rows)
    return _dedupe_rows(sorted(must_keep + _evenly_sample_rows(remaining, max_rows=max_rows - len(must_keep)), key=lambda row: row.row_index))


def _single_tab_key_total_label(label: str) -> bool:
    normalized = re.sub(r"[^a-z0-9]+", " ", label.lower()).strip()
    if not normalized:
        return False
    if normalized in {"gross profit", "department profit loss", "gross operating profit", "ebitda", "net income"}:
        return True
    return normalized.startswith("total ") and any(
        term in normalized
        for term in (
            "revenue",
            "expenses",
            "expense",
            "labor",
            "payroll",
            "cost",
            "department",
            "income",
            "fees",
            "taxes",
        )
    )


def _evenly_sample_rows(rows, *, max_rows: int):
    if max_rows <= 0:
        return []
    if len(rows) <= max_rows:
        return rows
    if max_rows == 1:
        return [rows[0]]
    step = (len(rows) - 1) / (max_rows - 1)
    indexes = [round(idx * step) for idx in range(max_rows)]
    return _dedupe_rows([rows[index] for index in indexes])


def _prompt_row_score(row) -> int:
    label = (row.label.normalized or row.label.raw or "").lower()
    score = 0
    if row.heuristic_hints.possible_section_boundary:
        score += 5
    if row.heuristic_hints.possible_total_line:
        score += 5
    if row.layout.bold:
        score += 2
    if row.layout.blank_above:
        score += 1
    if row.layout.numeric_cell_count:
        score += 1
    if any(term in label for term in _DEPARTMENT_PROMPT_TERMS):
        score += 4
    if label in _LOW_SIGNAL_LABELS:
        score -= 4
    return score


def _is_relevant_prompt_label(label: str) -> bool:
    normalized = re.sub(r"[^a-z0-9]+", " ", label.lower()).strip()
    if not normalized or normalized in _LOW_SIGNAL_LABELS:
        return False
    if _is_report_header_noise(label):
        return False
    if normalized.startswith("fontbold "):
        return False
    if re.fullmatch(r"[a-z]{1,4}\d?", normalized):
        return False
    return True


def _dedupe_rows(rows):
    seen: set[int] = set()
    deduped = []
    for row in rows:
        if row.row_index in seen:
            continue
        deduped.append(row)
        seen.add(row.row_index)
    return deduped


def _load_department_id_skill() -> str:
    return resources.files("hotel_pl_normalizer.prompts").joinpath("department_id.md").read_text(encoding="utf-8")


def _location_for_packet(
    packet: DepartmentIdentificationPacket,
    compact_packet,
) -> DepartmentLocation | None:
    routing_selection = next(
        (
            selection
            for selection in packet.sheet_name_selection.selections
            if selection.sheet_name == compact_packet.sheet_name
        ),
        None,
    )
    candidates = list(routing_selection.department_candidates) if routing_selection is not None else []
    preferred_candidate = _preferred_department_candidate(candidates)
    if _looks_like_support_sheet(compact_packet.sheet_name):
        return None
    department = preferred_candidate.department if preferred_candidate is not None else (
        _infer_department(compact_packet.sheet_name, "") or _infer_department(compact_packet.sheet_name, _packet_label_text(compact_packet))
    )
    if department is None:
        return None

    start_row = compact_packet.start_row
    end_row = compact_packet.end_row
    location_kind = DepartmentLocationKind.RANGE if start_row is not None or end_row is not None else DepartmentLocationKind.SHEET
    boundary_confidence = BoundaryConfidence.EXACT if location_kind == DepartmentLocationKind.SHEET else BoundaryConfidence.APPROXIMATE
    evidence = [f"Compact packet sheet name: {compact_packet.sheet_name}."]
    if preferred_candidate is not None:
        evidence.extend(preferred_candidate.evidence)
    else:
        evidence.append("Local helper inferred department from sheet name and visible labels.")
    return DepartmentLocation(
        location_id=_location_id(packet.workbook_id, department, compact_packet.sheet_name, start_row, end_row),
        department=department,
        section_role=_section_role_for_sheet(department, compact_packet.sheet_name),
        location_kind=location_kind,
        sheet_name=compact_packet.sheet_name,
        start_row=start_row,
        end_row=end_row,
        boundary_confidence=boundary_confidence,
        evidence=evidence,
    )


def _section_role_for_sheet(department: str, sheet_name: str) -> DepartmentSectionRole:
    normalized = re.sub(r"\s+", " ", sheet_name.lower().replace("_", " ")).strip()
    compact = re.sub(r"[^a-z0-9]+", "", normalized)
    if department == "food_and_beverage":
        if "kpi" in normalized or "productivity" in normalized or "stat" in normalized:
            return DepartmentSectionRole.KPI
        if "detail" in normalized or "dept" in normalized:
            return DepartmentSectionRole.DETAIL
        if any(_contains_department_term(normalized, compact, term) for term in _FNB_DETAIL_TERMS):
            return DepartmentSectionRole.DETAIL
        if "summary" in normalized or "cons" in normalized or "consolidated" in normalized:
            return DepartmentSectionRole.SUMMARY
        return DepartmentSectionRole.SUMMARY
    if department == "other_operated_departments":
        if "summary" in normalized or "parent" in normalized or "cons" in normalized:
            return DepartmentSectionRole.SUMMARY
        if any(_contains_department_term(normalized, compact, term) for term in _OOD_DETAIL_TERMS):
            return DepartmentSectionRole.DETAIL
    return DepartmentSectionRole.PRIMARY


def _preferred_department_candidate(candidates):
    non_summary = [candidate for candidate in candidates if candidate.department != "summary"]
    if non_summary:
        return non_summary[0]
    return candidates[0] if candidates else None


def _should_use_single_tab_range_detection(packet: DepartmentIdentificationPacket) -> bool:
    return len(packet.compact_packets) == 1 and packet.sheet_name_selection.workbook_layout.value == "single_tab_p_and_l"


def _single_tab_locations(packet: DepartmentIdentificationPacket, compact_packet) -> list[DepartmentLocation]:
    heading_hits: list[tuple[int, str, str]] = []
    for row in compact_packet.rows:
        if row.layout.numeric_cell_count > 0:
            continue
        label = row.label.raw or ""
        if _is_report_header_noise(label):
            continue
        department = _department_from_heading(label)
        if department is None:
            continue
        heading_hits.append((row.row_index, department, label))

    if not heading_hits:
        fallback = _location_for_packet(packet, compact_packet)
        return [fallback] if fallback is not None else []

    heading_hits = _coerce_early_summary_headings(heading_hits)
    locations: list[DepartmentLocation] = []
    max_row = max((row.row_index for row in compact_packet.rows), default=None)
    for idx, (start_row, department, label) in enumerate(heading_hits):
        next_start = heading_hits[idx + 1][0] if idx + 1 < len(heading_hits) else None
        end_row = (next_start - 1) if next_start is not None else max_row
        if end_row is None:
            continue
        locations.append(
            DepartmentLocation(
                location_id=_location_id(packet.workbook_id, department, compact_packet.sheet_name, start_row, end_row),
                department=department,
                section_role=_section_role_for_single_tab_location(department, label),
                location_kind=DepartmentLocationKind.RANGE,
                sheet_name=compact_packet.sheet_name,
                start_row=start_row,
                end_row=end_row,
                boundary_confidence=BoundaryConfidence.APPROXIMATE,
                evidence=[
                    f"Single-tab local helper found department heading '{label}' at row {start_row}.",
                    "End row is inferred from the next department heading and should be refined by Subsection ID validation.",
                ],
            )
        )
    locations = _split_single_tab_fnb_locations(locations, compact_packet)
    return _merge_and_filter_single_tab_locations(locations)


def _coerce_early_summary_headings(heading_hits: list[tuple[int, str, str]]) -> list[tuple[int, str, str]]:
    first_detail_idx = next(
        (
            idx
            for idx, (_, department, _) in enumerate(heading_hits)
            if department in _DETAIL_START_DEPARTMENTS
        ),
        None,
    )
    if first_detail_idx is None:
        return heading_hits
    return [
        (row_index, "summary", label) if idx < first_detail_idx else (row_index, department, label)
        for idx, (row_index, department, label) in enumerate(heading_hits)
    ]


def _split_single_tab_fnb_locations(locations: list[DepartmentLocation], compact_packet) -> list[DepartmentLocation]:
    split_locations: list[DepartmentLocation] = []
    for location in locations:
        if location.department != "food_and_beverage" or location.start_row is None or location.end_row is None:
            split_locations.append(location)
            continue
        detail_start = _find_fnb_detail_start(compact_packet, location.start_row, location.end_row)
        if detail_start is None or detail_start <= location.start_row:
            split_locations.append(location)
            continue
        summary = location.model_copy(
            update={
                "end_row": detail_start - 1,
                "section_role": DepartmentSectionRole.SUMMARY,
                "evidence": location.evidence + ["Split as consolidated F&B summary before first F&B detail heading."],
            }
        )
        detail = location.model_copy(
            update={
                "location_id": f"{location.location_id}:detail:{detail_start}-{location.end_row}",
                "start_row": detail_start,
                "section_role": DepartmentSectionRole.DETAIL,
                "evidence": location.evidence + ["Split as F&B detail beginning at first outlet/detail heading."],
            }
        )
        split_locations.extend([summary, detail])
    return split_locations


def _find_fnb_detail_start(compact_packet, start_row: int, end_row: int) -> int | None:
    for row in compact_packet.rows:
        if row.row_index <= start_row or row.row_index > end_row:
            continue
        if row.layout.numeric_cell_count > 0:
            continue
        normalized = row.label.normalized or ""
        compact = re.sub(r"[^a-z0-9]+", "", normalized)
        if _looks_like_fnb_detail_heading(normalized, compact):
            return row.row_index
    return None


def _section_role_for_single_tab_location(department: str, label: str) -> DepartmentSectionRole:
    normalized = re.sub(r"\s+", " ", label.lower().replace("_", " ")).strip()
    compact = re.sub(r"[^a-z0-9]+", "", normalized)
    if department == "food_and_beverage":
        if "summary" in normalized or "department" in normalized:
            return DepartmentSectionRole.SUMMARY
        if _looks_like_fnb_detail_heading(normalized, compact):
            return DepartmentSectionRole.DETAIL
    if department == "other_operated_departments":
        if "summary" in normalized or "parent" in normalized:
            return DepartmentSectionRole.SUMMARY
        if _looks_like_ood_detail_heading(normalized, compact):
            return DepartmentSectionRole.DETAIL
    return DepartmentSectionRole.PRIMARY


def _looks_like_fnb_detail_heading(normalized: str, compact: str) -> bool:
    if normalized in {
        "outlet revenue",
        "other f&b revenue",
        "other food and beverage revenue",
        "other food and beverage expenses",
        "f&b cost of goods sold",
        "cost of other f&b revenue",
    }:
        return False
    return any(_contains_department_term(normalized, compact, term) for term in _FNB_DETAIL_TERMS)


def _looks_like_ood_detail_heading(normalized: str, compact: str) -> bool:
    return any(_contains_department_term(normalized, compact, term) for term in _OOD_DETAIL_TERMS)


def _merge_and_filter_single_tab_locations(locations: list[DepartmentLocation]) -> list[DepartmentLocation]:
    if not locations:
        return []

    merged: list[DepartmentLocation] = []
    for location in locations:
        if (
            merged
            and merged[-1].department == location.department
            and merged[-1].section_role == location.section_role
            and merged[-1].sheet_name == location.sheet_name
            and merged[-1].end_row is not None
            and location.start_row is not None
            and location.start_row <= merged[-1].end_row + 1
        ):
            merged[-1] = merged[-1].model_copy(
                update={
                    "end_row": location.end_row,
                    "evidence": merged[-1].evidence + location.evidence,
                }
            )
        else:
            merged.append(location)

    filtered: list[DepartmentLocation] = []
    seen_locations: set[tuple[str, DepartmentSectionRole]] = set()
    undistributed_started = False
    for location in merged:
        if location.department in {
            "administrative_and_general",
            "information_and_telecommunications_systems",
            "sales_and_marketing",
            "property_operations_and_maintenance",
            "utilities",
        }:
            undistributed_started = True
        if undistributed_started and location.department in {
            "rooms",
            "food_and_beverage",
            "other_operated_departments",
            "miscellaneous_income",
        }:
            continue
        location_key = (location.department, location.section_role)
        if location_key in seen_locations:
            continue
        filtered.append(location)
        seen_locations.add(location_key)
    return filtered


def _is_report_header_noise(label: str) -> bool:
    normalized = label.strip().lower()
    return (
        normalized.startswith("for property")
        or normalized.startswith("for properties")
        or normalized in {"income statement", "actual", "variance", "ptd", "mtd actual", "por"}
        or bool(re.fullmatch(r"as of \d{1,2}/\d{1,2}/\d{4}", normalized))
        or bool(re.fullmatch(r"\d{1,2}/\d{1,2}/\d{4} at .*", normalized))
        or normalized.startswith("page ")
    )


def _department_from_heading(label: str) -> str | None:
    normalized = re.sub(r"\s+", " ", label.lower().replace("_", " ")).strip()
    compact = re.sub(r"[^a-z0-9]+", "", normalized)
    if not normalized:
        return None
    if normalized == "summary" or "summary operating statement" in normalized:
        return "summary"
    if _looks_like_fnb_detail_heading(normalized, compact):
        return "food_and_beverage"
    if _looks_like_ood_detail_heading(normalized, compact):
        return "other_operated_departments"
    heading_patterns = [
        ("rooms", ("rooms department", "rooms")),
        ("food_and_beverage", ("f&b summary", "food and beverage summary", "food & beverage summary", "food and beverage department", "food & beverage department", "f b summary", "fb summary", "fbsummary")),
        ("other_operated_departments", ("other operated", "other operating", "oth op", "other income", "parking department", "gift shop department", "spa department", "telephone department", "transportation department", "minor operated departments")),
        ("miscellaneous_income", ("miscellaneous income", "misc income")),
        ("administrative_and_general", ("admin & general", "administrative and general", "a&g")),
        ("property_operations_and_maintenance", ("engineering", "property operations", "maintenance")),
        ("utilities", ("utilities", "energy")),
        ("sales_and_marketing", ("sales & marketing", "sales and marketing")),
        ("information_and_telecommunications_systems", ("it", "information technology", "information and telecommunications")),
        ("non_operating_income_and_expense", ("non operating", "non-operating")),
        ("management_fees", ("management fee",)),
    ]
    for department, terms in heading_patterns:
        for term in terms:
            if _contains_department_term(normalized, compact, term):
                return department
    return None


def _packet_label_text(compact_packet) -> str:
    labels = [row.label.normalized or row.label.raw or "" for row in compact_packet.rows if row.label.raw]
    return " ".join(labels[:80]).lower()


def _infer_department(sheet_name: str, label_text: str) -> str | None:
    text = f"{sheet_name} {label_text}".lower().replace("_", " ")
    compact = re.sub(r"[^a-z0-9]+", "", text)
    patterns = [
        ("property_operations_and_maintenance", ("property operations", "property opera", "maintenance", "engineering", "pom", "repairs")),
        ("management_fees", ("management fee", "management fe", "mgmt fee", "mgt fee", "mgt_fees", "fees")),
        ("information_and_telecommunications_systems", ("information technology", "information and telecommunications", "telecommunications", "telephone", "it")),
        ("administrative_and_general", ("administrative", "admin", "a&g", "general", "human resources")),
        ("food_and_beverage", ("fbsummary", "fbdept", "fb summary", "food", "beverage", "f&b", "banquet", "catering", "event technology", "event tech", "restaurant", "outlet", "venue", "ird", "mini bar")),
        ("sales_and_marketing", ("sales", "marketing", "s&m", "advertising", "franchise", "royalty", "loyalty", "brand fee")),
        ("non_operating_income_and_expense", ("non operating", "nonop", "fixed charges", "rent", "insurance")),
        ("miscellaneous_income", ("miscellaneous income", "misc income", "misc_inc", "resort fee", "destination fee")),
        ("other_operated_departments", ("other operated", "other operating", "oth op", "oth_op", "ood", "minor operated", "parking", "retail", "guest communications", "guest communication", "guest laundry", "telephone", "transportation", "shuttle", "valet parking", "spa", "golf", "recreation")),
        ("utilities", ("utilities", "energy", "ec", "water", "waste", "electric")),
        ("rooms", ("rooms", "room revenue", "guestroom")),
        ("summary", ("summary", "statement", "operating statement", "income statement")),
    ]
    for department, needles in patterns:
        for needle in needles:
            if _contains_department_term(text, compact, needle):
                return department
    return None


def _looks_like_support_sheet(sheet_name: str) -> bool:
    normalized = re.sub(r"\s+", " ", sheet_name.lower().replace("_", " ")).strip()
    return any(_contains_department_term(normalized, re.sub(r"[^a-z0-9]+", "", normalized), term) for term in _SUPPORT_SHEET_TERMS)


def _contains_department_term(text: str, compact: str, needle: str) -> bool:
    needle_compact = re.sub(r"[^a-z0-9]+", "", needle)
    if len(needle_compact) <= 2:
        return bool(re.search(rf"(^|[^a-z0-9]){re.escape(needle_compact)}([^a-z0-9]|$)", text))
    if re.fullmatch(r"[a-z0-9]+", needle):
        return bool(re.search(rf"(^|[^a-z0-9]){re.escape(needle_compact)}([^a-z0-9]|$)", text)) or compact.startswith(needle_compact)
    return needle in text or needle_compact in compact


def _location_id(
    workbook_id: str,
    department: str,
    sheet_name: str,
    start_row: int | None,
    end_row: int | None,
) -> str:
    safe_sheet = re.sub(r"[^a-zA-Z0-9]+", "_", sheet_name).strip("_").lower()
    if start_row is None and end_row is None:
        return f"{workbook_id}:{department}:{safe_sheet}"
    return f"{workbook_id}:{department}:{safe_sheet}:{start_row or ''}-{end_row or ''}"
