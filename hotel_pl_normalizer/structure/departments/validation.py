"""Check where the model said each department lives against what the sheets show.

What this is validating
-----------------------
The department-ID stage returns a `DepartmentLocationMap`: for each USALI
department, which sheet it is on and which rows it spans. Everything downstream
depends on those spans. Period binding asks "which column is YTD *for Rooms*",
the mapper is shown rows grouped by the department they fall in, and the output
workbook rolls figures up by department. A span that starts ten rows late drops
that department's revenue lines silently -- the run completes, the workbook
opens, and Rooms is simply short.

So this file exists to catch a plausible-looking map that is wrong, and every
check here is a way a hotel P&L has actually been misread.

Why the rules are lists of words
--------------------------------
USALI gives departments standard names and standard contents, but operators
write them however their accounting system does. "Rooms" is also "Guest Rooms"
and "Accommodation"; telephone sits under IT in the 11th edition and under its
own department in older ones; F&B may be one section or a section per outlet.
There is no schema to check against, so the checks look for the *content
signature* a department should have -- Rooms without any labour line is not a
Rooms section, whatever it is called -- and each table below is the vocabulary
for one of those signatures.

The tables are deliberately generous. A false accept costs a wrong figure; a
false reject costs a repair round trip, and the repair prompt tells the model
exactly which signature was missing. Missing content is a WARNING for that
reason, while a structural impossibility -- a location on a sheet that does not
exist, rows outside the packet -- is an ERROR.

One severity decision is worth knowing about, because it was learned the hard
way: a location on a sheet the workbook has but routing did not select is a
*warning*, not an error. It used to be an error, and a department-ID failure
abandons the whole property -- GRY lost all 156 accounts because routing skipped
its real utilities schedule and the model found it anyway. See the comment on
that branch.
"""

from __future__ import annotations

import re
from collections import Counter

from hotel_pl_normalizer.models.common import Severity, ValidationStatus
from hotel_pl_normalizer.models.department_location import (
    DepartmentContextRequest,
    DepartmentIdentificationPacket,
    DepartmentLocationIssueType,
    DepartmentLocationMap,
    DepartmentLocationMapIssue,
    DepartmentLocationValidation,
    DepartmentSectionRole,
)

# The USALI departments a full-service hotel P&L is expected to carry. Absence is
# a WARNING, never an ERROR: a limited-service property genuinely has no F&B, and
# `_missing_department_allowed_by_context` excuses the cases the sheets explain.
_EXPECTED_DEPARTMENTS = {
    "summary",
    "rooms",
    "food_and_beverage",
    "administrative_and_general",
    "information_and_telecommunications_systems",
    "sales_and_marketing",
    "property_operations_and_maintenance",
    "utilities",
    "miscellaneous_income",
    "management_fees",
    "non_operating_income_and_expense",
}

# The content signature of each department: the kinds of line it should contain,
# and the words that identify each kind. A span claiming to be Rooms that shows no
# labour line anywhere gets reported -- whatever those rows are, that is not a
# Rooms department, and the usual cause is a span starting below the revenue block
# or stopping above payroll.
#
# `description` is what the repair prompt is told. `groups` is a list of
# (group name, matching terms); a group is present when any of its terms appears
# in the location's labels.
_DEPARTMENT_CONTENT_RULES = {
    "rooms": {
        "description": "Rooms should include revenue, labor, and opex clues such as housekeeping/front desk and commissions/supplies/laundry.",
        "groups": [
            ("revenue", ("revenue", "transient", "group", "contract", "room revenue")),
            ("labor", ("labor", "salaries", "wages", "payroll", "housekeeping", "front desk", "reservations", "guest services", "bell", "valet")),
            ("opex", ("expense", "commissions", "commission", "cleaning", "guest supplies", "operating supplies", "laundry", "reservation")),
        ],
    },
    "other_operated_departments": {
        "description": "OOD should include revenue and expenses for operated outlets such as parking, garage/valet, spa, retail/merchandise, gift shop, recreation, golf, guest communications, guest internet, telephone guest communications, transportation, valet parking, shuttle, guest laundry, or minibar when grouped inside OOD.",
        "groups": [
            ("ood_outlet", ("parking", "garage", "valet", "spa", "retail", "merchandise", "gift shop", "recreation", "golf", "guest communication", "guest communications", "guest internet", "internet guest", "internet rev guest", "telephone", "local", "long distance", "guest laundry", "transportation", "shuttle", "valet parking", "mini bar", "minibar", "minor operated", "other operated", "other operating")),
            ("revenue", ("revenue", "sales", "income")),
            ("expense", ("expense", "expenses", "labor", "payroll", "cost", "costs", "operating")),
        ],
    },
    "miscellaneous_income": {
        "description": "Miscellaneous Income should be a short revenue-only section with labels such as attrition, cancellation, destination, amenity, resort fee, lease income, or other income.",
        "groups": [
            ("income", ("attrition", "cancellation", "destination", "amenity", "resort fee", "lease income", "other income", "miscellaneous income", "income")),
        ],
        "warn_if_many_rows": 80,
        "unexpected_terms": ("labor", "salaries", "wages", "payroll", "cogs", "cost of goods", "operating expenses"),
    },
    "administrative_and_general": {
        "description": "A&G should include labor and opex clues such as general manager, finance/accounting, HR, and credit card commissions/fees.",
        "groups": [
            ("labor", ("labor", "salaries", "wages", "payroll", "general manager", "finance", "accounting", "human resources", "hr", "executive office")),
            ("opex", ("expense", "credit card", "cc commission", "card commission", "office", "professional", "administrative", "supplies")),
        ],
    },
    "sales_and_marketing": {
        "description": "S&M should include labor and opex clues such as sales/marketing labor, media, digital, advertising, promotion, franchise, loyalty, or brand fees.",
        "groups": [
            ("labor", ("labor", "salaries", "wages", "payroll", "sales", "marketing")),
            ("opex", ("media", "digital", "advertising", "promotion", "franchise", "loyalty", "brand", "expense")),
        ],
    },
    "property_operations_and_maintenance": {
        "description": "POM should include labor and opex clues such as engineering/maintenance, elevators, contract services, repairs, life safety, equipment, or grounds.",
        "groups": [
            ("labor", ("labor", "salaries", "wages", "payroll", "engineering", "maintenance")),
            ("opex", ("elevator", "contract services", "contract", "repairs", "maintenance", "life safety", "equipment", "grounds", "expense")),
        ],
    },
    "utilities": {
        "description": "Utilities should be a short section with water, electric, gas, steam, sewer, waste, trash, energy, or fuel lines.",
        "groups": [
            ("utilities", ("water", "electric", "electricity", "gas", "steam", "sewer", "waste", "trash", "energy", "fuel", "utilities")),
        ],
        "warn_if_many_rows": 80,
    },
    "management_fees": {
        "description": "Management Fees should be only a few lines and clearly include management fee labels.",
        "groups": [
            ("management_fee", ("management fee", "management fees", "base fee", "incentive fee")),
        ],
        "warn_if_many_rows": 50,
    },
    "non_operating_income_and_expense": {
        "description": "Non-Op should include property-level lines such as real estate taxes, insurance, rent, owner expenses, interest, lease, or fixed charges.",
        "groups": [
            ("non_op", ("real estate tax", "real estate taxes", "property tax", "property taxes", "insurance", "rent", "owner", "interest", "lease", "fixed charges", "non operating")),
        ],
    },
}

# F&B is the department most often split across outlets -- restaurant, bar, room
# service, banquets -- each with its own block. The consolidated summary is a
# separate section from those outlets, and mapping the outlets without it loses
# the department total, so it is checked for separately.
_FNB_SUMMARY_RULE = {
    "description": "F&B summary should include revenue, labor, COGS/cost of sales, and opex.",
    "groups": [
        ("revenue", ("revenue", "sales", "food", "beverage", "banquet", "catering", "room service", "in room dining", "in-room dining")),
        ("labor", ("labor", "salaries", "wages", "payroll")),
        ("cogs", ("cogs", "cost of goods", "cost of sales", "cost of food", "cost of beverage", "food cost", "beverage cost")),
        ("opex", ("operating expenses", "other expenses", "opex", "expense")),
    ],
}

# Labels that end a Summary section. Summary runs from revenue down to a bottom
# line, and these are what that bottom line is called; a Summary span continuing
# past one of them has swallowed the department detail underneath.
_SUMMARY_ENDING_TERMS = (
    "ebitda",
    "ebda",
    "adjusted ebitda",
    "net income",
    "income before",
    "non operating",
    "non-operating",
    "fixed charges",
    "owner",
)

# Departments that must have exactly one primary location. Detail and supporting
# sections may legitimately repeat -- an operator can report Rooms payroll on its
# own schedule -- but two primaries for one department double-counts it.
_PRIMARY_ROLE_DEPARTMENTS = {
    "rooms",
    "miscellaneous_income",
    "administrative_and_general",
    "information_and_telecommunications_systems",
    "sales_and_marketing",
    "property_operations_and_maintenance",
    "utilities",
    "management_fees",
    "non_operating_income_and_expense",
}

# Other Operated Departments outlets, the ones most often left out of the map:
# parking, spa, golf, retail. They rarely carry a heading naming the USALI
# department, so an unclaimed gap holding these words is usually a missed outlet
# rather than page furniture.
_OOD_GAP_TERMS = (
    "parking",
    "garage",
    "valet",
    "spa",
    "retail",
    "merchandise",
    "gift shop",
    "giftshop",
    "recreation",
    "golf",
    "guest communication",
    "guest communications",
    "telephone",
    "transportation",
    "shuttle",
    "valet parking",
    "guest laundry",
    "mini bar",
    "minibar",
    "minor operated",
    "minor op",
)

# The opposite: words that make an unclaimed gap explicable. A run of rows holding
# only these is page furniture, and reporting it would teach the reader to ignore
# gap warnings generally.
_GAP_NOISE_TERMS = (
    "page",
    "for property",
    "for properties",
    "income statement",
    "actual",
    "variance",
    "as of",
)


def _packet_alignment_issues(
    packet: DepartmentIdentificationPacket, location_map: DepartmentLocationMap
) -> list[DepartmentLocationMapIssue]:
    """The map has to be about this workbook, and has to contain something.

    Both are ERRORs. A map for a different workbook cannot be repaired into a
    correct one, and a map with no locations means the stage produced nothing --
    continuing would map every row into no department at all.
    """
    issues: list[DepartmentLocationMapIssue] = []
    if location_map.workbook_id != packet.workbook_id:
        issues.append(
            DepartmentLocationMapIssue(
                severity=Severity.ERROR,
                issue_type=DepartmentLocationIssueType.NEEDS_MORE_CONTEXT,
                message="DepartmentLocationMap workbook_id does not match the input packet.",
            )
        )
    if not location_map.locations:
        issues.append(
            DepartmentLocationMapIssue(
                severity=Severity.ERROR,
                issue_type=DepartmentLocationIssueType.MISSING_LOCATION,
                message="No department locations were identified from the available compact packets.",
                requested_context=[
                    DepartmentContextRequest(
                        sheet_name=compact_packet.sheet_name,
                        reason="Need compact sheet rows because no department could be located.",
                    )
                    for compact_packet in packet.compact_packets
                ],
            )
        )
    return issues


def _missing_department_issues(
    packet: DepartmentIdentificationPacket, location_map: DepartmentLocationMap
) -> list[DepartmentLocationMapIssue]:
    """Expected USALI departments nothing was found for.

    A WARNING, because absence is often real -- a limited-service property has no
    F&B, a leased restaurant reports none of it -- and the repair prompt carries
    the sheets worth re-reading. `_missing_department_allowed_by_context` drops
    the cases the workbook itself explains.
    """
    located = {location.department for location in location_map.locations}
    return [
        DepartmentLocationMapIssue(
            severity=Severity.WARNING,
            issue_type=DepartmentLocationIssueType.MISSING_LOCATION,
            department=department,
            message=f"Expected department was not identified: {department}.",
            requested_context=_missing_department_context_requests(packet, department),
        )
        for department in sorted(_EXPECTED_DEPARTMENTS - located)
        if not _missing_department_allowed_by_context(packet, location_map, department)
    ]


def _duplicate_section_issues(
    location_map: DepartmentLocationMap,
) -> list[DepartmentLocationMapIssue]:
    """One department claiming the same role in more than one place.

    Detail, KPI and supporting-detail roles are exempt: an operator may report
    Rooms payroll on its own schedule, and that is a second Rooms *detail*
    section, not a second Rooms. Any other repeat is either split detail that was
    mis-roled or genuine double coverage, and only re-reading the rows can say
    which -- so this asks, rather than deciding.
    """
    issues: list[DepartmentLocationMapIssue] = []
    repeatable = {
        DepartmentSectionRole.DETAIL,
        DepartmentSectionRole.KPI,
        DepartmentSectionRole.SUPPORTING_DETAIL,
    }
    counts = Counter(
        (location.department, location.section_role)
        for location in location_map.locations
    )
    for (department, section_role), count in sorted(counts.items()):
        if section_role in repeatable or count <= 1:
            continue
        duplicates = [
            location
            for location in location_map.locations
            if location.department == department
            and location.section_role == section_role
        ]
        issues.append(
            DepartmentLocationMapIssue(
                severity=Severity.WARNING,
                issue_type=DepartmentLocationIssueType.DUPLICATE_DEPARTMENT,
                department=department,
                message=f"Department section appears in {count} locations for role {section_role.value}; later validation must confirm whether this is split detail or duplicate coverage.",
                requested_context=[
                    DepartmentContextRequest(
                        sheet_name=location.sheet_name,
                        start_row=location.start_row,
                        end_row=location.end_row,
                        reason=f"Need to determine whether this {department} location is primary, supporting detail, or duplicate coverage.",
                    )
                    for location in duplicates
                ],
            )
        )
    return issues


def _unroutable_sheet_issue(
    location, sheet_names: set[str], workbook_sheet_names: set[str] | None
) -> DepartmentLocationMapIssue:
    """A location pointing at a sheet that is not in the compact packets.

    Severity turns on whether the workbook has the sheet at all. If it does, this
    is a routing miss and the model was *right* to look there -- so it is a
    WARNING, and departments build that packet on demand.

    It used to be an ERROR, and that was wrong in an expensive way: a
    department-ID failure abandons the whole property, and GRY lost all 156 of its
    accounts because sheet routing skipped its real `EC` utilities schedule while
    the model found it anyway.

    A sheet the workbook does not have is still an ERROR: nothing can be built
    from it, and the span is a hallucination.
    """
    routed_but_missing = (
        workbook_sheet_names is not None and location.sheet_name in workbook_sheet_names
    )
    return DepartmentLocationMapIssue(
        severity=Severity.WARNING if routed_but_missing else Severity.ERROR,
        issue_type=DepartmentLocationIssueType.UNKNOWN_SHEET,
        department=location.department,
        sheet_name=location.sheet_name,
        message=(
            "Department location references a sheet that exists in the workbook "
            "but was not routed into the compact packets; its packet will be "
            "built on demand."
            if routed_but_missing
            else "Department location references a sheet that was not included in the compact packets."
        ),
        requested_context=[
            DepartmentContextRequest(
                sheet_name=location.sheet_name,
                reason="Need compact packet for referenced sheet.",
            )
        ],
    )


def _row_range_issues(location, min_row: int, max_row: int) -> list[DepartmentLocationMapIssue]:
    """Spans that cannot be true of the sheet they name.

    These are arithmetic, not judgement: a span starting before the packet begins,
    ending after it ends, or running backwards. Each is an ERROR because the rows
    it selects are not the rows it claims, and every figure taken from it is
    attributed to the wrong department.
    """
    issues: list[DepartmentLocationMapIssue] = []
    if location.start_row is not None and location.start_row < min_row:
        issues.append(
            _invalid_range_issue(
                location.department,
                location.sheet_name,
                "start_row is before compact packet bounds.",
            )
        )
    if location.end_row is not None and location.end_row > max_row:
        issues.append(
            _invalid_range_issue(
                location.department,
                location.sheet_name,
                "end_row is after compact packet bounds.",
            )
        )
    if (
        location.start_row is not None
        and location.end_row is not None
        and location.start_row > location.end_row
    ):
        issues.append(
            _invalid_range_issue(
                location.department,
                location.sheet_name,
                "start_row is after end_row.",
            )
        )
    return issues


def _location_issues(
    location,
    packet: DepartmentIdentificationPacket,
    location_map: DepartmentLocationMap,
    sheet_names: set[str],
    row_bounds: dict[str, tuple[int | None, int | None]],
    workbook_sheet_names: set[str] | None,
) -> list[DepartmentLocationMapIssue]:
    """Everything checkable about one location, cheapest first.

    Returns early when the location cannot be examined at all -- an unroutable
    sheet, or a packet with no rows -- because every later check reads those rows
    and would report confusing follow-on failures from one root cause.
    """
    if location.sheet_name not in sheet_names:
        return [_unroutable_sheet_issue(location, sheet_names, workbook_sheet_names)]

    min_row, max_row = row_bounds[location.sheet_name]
    if min_row is None or max_row is None:
        return [
            DepartmentLocationMapIssue(
                severity=Severity.WARNING,
                issue_type=DepartmentLocationIssueType.NEEDS_MORE_CONTEXT,
                department=location.department,
                sheet_name=location.sheet_name,
                message="Referenced compact packet contains no rows.",
                requested_context=[
                    DepartmentContextRequest(
                        sheet_name=location.sheet_name,
                        reason="Need non-empty compact sheet packet.",
                    )
                ],
            )
        ]

    issues = _row_range_issues(location, min_row, max_row)
    # Judgement checks: does this span hold what its department should hold, and
    # does it stop where that department stops. Each returns one issue or None.
    for check in (
        _content_signature_issue(packet, location),
        _summary_boundary_issue(packet, location),
        _summary_start_issue(packet, location),
        _summary_duplicate_issue(packet, location),
        _section_role_issue(location, location_map),
    ):
        if check is not None:
            issues.append(check)
    return issues


def _missing_fnb_summary_issues(
    location_map: DepartmentLocationMap,
) -> list[DepartmentLocationMapIssue]:
    """F&B outlets found, but no consolidated F&B summary among them.

    F&B is the department most often reported per outlet. Mapping the outlets
    without the summary loses the department total, and the loss is quiet: the
    outlets add up to something plausible.
    """
    fnb = [
        location
        for location in location_map.locations
        if location.department == "food_and_beverage"
    ]
    if not fnb:
        return []
    if any(location.section_role == DepartmentSectionRole.SUMMARY for location in fnb):
        return []
    return [
        DepartmentLocationMapIssue(
            severity=Severity.WARNING,
            issue_type=DepartmentLocationIssueType.NEEDS_MORE_CONTEXT,
            department="food_and_beverage",
            message="Food and Beverage was identified but no consolidated F&B summary location was returned.",
            requested_context=[
                DepartmentContextRequest(
                    sheet_name=location.sheet_name,
                    start_row=location.start_row,
                    end_row=location.end_row,
                    reason="Reinspect F&B locations and identify the consolidated F&B summary separately from outlet/detail sections.",
                )
                for location in fnb
            ],
        )
    ]


def _unassigned_packet_issues(
    packet: DepartmentIdentificationPacket, location_map: DepartmentLocationMap
) -> list[DepartmentLocationMapIssue]:
    """Sheets routing selected that the map then said nothing about.

    Routing already decided these were worth reading. Silence about one is not an
    answer -- either it holds a department, or the model should say why it does
    not -- so this asks for the classification rather than assuming it is empty.
    """
    assigned = {location.sheet_name for location in location_map.locations}
    return [
        DepartmentLocationMapIssue(
            severity=Severity.WARNING,
            issue_type=DepartmentLocationIssueType.NEEDS_MORE_CONTEXT,
            sheet_name=compact_packet.sheet_name,
            message="Selected or unsure compact packet was not assigned to a department location or explicitly classified as duplicate/supporting/drop.",
            requested_context=[
                DepartmentContextRequest(
                    sheet_name=compact_packet.sheet_name,
                    reason="Need the Department ID agent to classify this packet or explain why it should be dropped.",
                    start_row=compact_packet.start_row,
                    end_row=compact_packet.end_row,
                )
            ],
        )
        for compact_packet in packet.compact_packets
        if compact_packet.sheet_name not in assigned
    ]


def validate_department_location_map(
    packet: DepartmentIdentificationPacket,
    location_map: DepartmentLocationMap,
    *,
    workbook_sheet_names: set[str] | None = None,
) -> DepartmentLocationValidation:
    """Check a department location map against the sheets it claims to describe.

    Runs in widening scope: the map as a whole, then each location on its own,
    then the checks that need every location at once, then the cross-sheet sweeps
    for departments hiding in unclaimed rows.

    Severity carries the meaning. ERROR means the map cannot be true of this
    workbook and the stage fails; WARNING means it is suspect and goes to a repair
    pass with the specific rows to re-read attached. Getting that split right
    matters more than it looks: a department-ID failure abandons the property
    entirely, so anything recoverable is deliberately a warning.

    `workbook_sheet_names` is what the workbook actually contains, as opposed to
    what routing selected. Supplying it downgrades "sheet not in the packets" from
    an error to a warning when the sheet is real -- see `_unroutable_sheet_issue`.
    """
    sheet_names = {
        compact_packet.sheet_name for compact_packet in packet.compact_packets
    }
    row_bounds = {
        compact_packet.sheet_name: (
            min((row.row_index for row in compact_packet.rows), default=None),
            max((row.row_index for row in compact_packet.rows), default=None),
        )
        for compact_packet in packet.compact_packets
    }

    issues = _packet_alignment_issues(packet, location_map)
    issues.extend(_missing_department_issues(packet, location_map))
    issues.extend(_duplicate_section_issues(location_map))

    for location in location_map.locations:
        issues.extend(
            _location_issues(
                location,
                packet,
                location_map,
                sheet_names,
                row_bounds,
                workbook_sheet_names,
            )
        )

    issues.extend(_missing_fnb_summary_issues(location_map))
    issues.extend(_unassigned_packet_issues(packet, location_map))

    # Sweeps over rows no location claimed, looking for a department that was
    # missed rather than one that was described wrongly.
    issues.extend(_unclassified_gap_issues(packet, location_map))
    issues.extend(_misplaced_telephone_it_issues(packet, location_map))
    issues.extend(_embedded_department_heading_issues(packet, location_map))
    issues.extend(_uncovered_sales_marketing_fee_issues(packet, location_map))

    status = ValidationStatus.PASS
    if any(issue.severity == Severity.ERROR for issue in issues):
        status = ValidationStatus.FAIL
    elif issues:
        status = ValidationStatus.WARNING
    return DepartmentLocationValidation(status=status, issues=issues)


def _uncovered_sales_marketing_fee_issues(
    packet: DepartmentIdentificationPacket,
    location_map: DepartmentLocationMap,
) -> list[DepartmentLocationMapIssue]:
    issues = []
    for compact_packet in packet.compact_packets:
        uncovered = []
        for row in compact_packet.rows:
            label = (row.label.normalized or row.label.raw or "").lower()
            if not row.values or not any(
                term in label
                for term in (
                    "franchise",
                    "affiliation",
                    "royalty fee",
                    "royalties",
                )
            ):
                continue
            if any(
                _location_covers_row(
                    location,
                    compact_packet.sheet_name,
                    row.row_index,
                )
                and location.department in {"summary", "sales_and_marketing"}
                for location in location_map.locations
            ):
                continue
            uncovered.append(row)
        if not uncovered:
            continue
        issues.append(
            DepartmentLocationMapIssue(
                severity=Severity.WARNING,
                issue_type=DepartmentLocationIssueType.MISSING_LOCATION,
                department="sales_and_marketing",
                sheet_name=compact_packet.sheet_name,
                message=(
                    "Explicit franchise, affiliation, or royalty rows are outside "
                    "both Summary and the identified S&M locations."
                ),
                requested_context=[
                    DepartmentContextRequest(
                        sheet_name=compact_packet.sheet_name,
                        start_row=max(
                            1,
                            min(row.row_index for row in uncovered) - 2,
                        ),
                        end_row=max(row.row_index for row in uncovered) + 2,
                        reason=(
                            "Inspect the separate franchise/affiliation fee range "
                            "and distinguish it from management fees."
                        ),
                    )
                ],
            )
        )
    return issues


def _location_covers_row(location, sheet_name: str, row_index: int) -> bool:
    return (
        location.sheet_name == sheet_name
        and (location.start_row is None or row_index >= location.start_row)
        and (location.end_row is None or row_index <= location.end_row)
    )


def _invalid_range_issue(department: str, sheet_name: str, message: str) -> DepartmentLocationMapIssue:
    return DepartmentLocationMapIssue(
        severity=Severity.ERROR,
        issue_type=DepartmentLocationIssueType.INVALID_RANGE,
        department=department,
        sheet_name=sheet_name,
        message=message,
        requested_context=[
            DepartmentContextRequest(
                sheet_name=sheet_name,
                reason="Need compact packet rows to repair the invalid department range.",
            )
        ],
    )


def _unclassified_gap_issues(
    packet: DepartmentIdentificationPacket,
    location_map: DepartmentLocationMap,
) -> list[DepartmentLocationMapIssue]:
    compact_packets = {compact_packet.sheet_name: compact_packet for compact_packet in packet.compact_packets}
    issues: list[DepartmentLocationMapIssue] = []
    for sheet_name, compact_packet in compact_packets.items():
        ranged_locations = sorted(
            [
                location
                for location in location_map.locations
                if location.sheet_name == sheet_name
                and location.start_row is not None
                and location.end_row is not None
            ],
            key=lambda location: (location.start_row or 0, location.end_row or 0),
        )
        if len(ranged_locations) < 2:
            continue
        for previous, current in zip(ranged_locations, ranged_locations[1:]):
            gap_start = (previous.end_row or 0) + 1
            gap_end = (current.start_row or 0) - 1
            if gap_start > gap_end:
                continue
            labels = _meaningful_labels_for_gap(compact_packet, gap_start, gap_end)
            if not labels:
                continue
            gap_text = " ".join(labels)
            if not any(_contains_text_term(gap_text, term) for term in _OOD_GAP_TERMS):
                continue
            issues.append(
                DepartmentLocationMapIssue(
                    severity=Severity.WARNING,
                    issue_type=DepartmentLocationIssueType.NEEDS_MORE_CONTEXT,
                    department="other_operated_departments",
                    sheet_name=sheet_name,
                    message=(
                        "Unclassified gap between department locations contains OOD detail clues. "
                        "Operated schedules such as Telephone, Transportation, Guest Communications, "
                        "Valet Parking, Guest Laundry, Spa, Retail, Gift Shop, Recreation, Golf, or Minor Op "
                        "should usually be classified as OOD detail when they sit in the operated-departments block."
                    ),
                    requested_context=[
                        DepartmentContextRequest(
                            sheet_name=sheet_name,
                            start_row=gap_start,
                            end_row=gap_end,
                            reason="Reinspect this unclassified gap and add any operated department schedules as OOD detail.",
                        )
                    ],
                )
            )
    return issues


def _misplaced_telephone_it_issues(
    packet: DepartmentIdentificationPacket,
    location_map: DepartmentLocationMap,
) -> list[DepartmentLocationMapIssue]:
    compact_packets = {compact_packet.sheet_name: compact_packet for compact_packet in packet.compact_packets}
    issues: list[DepartmentLocationMapIssue] = []
    for location in location_map.locations:
        if location.department != "information_and_telecommunications_systems":
            continue
        if location.start_row is None or location.end_row is None:
            continue
        compact_packet = compact_packets.get(location.sheet_name)
        if compact_packet is None:
            continue
        labels = _labels_for_location(compact_packet, location.start_row, location.end_row)
        text = " ".join(labels)
        if not _contains_text_term(text, "telephone"):
            continue
        if not (
            any(_contains_text_term(text, term) for term in ("revenue", "income"))
            and any(_contains_text_term(text, term) for term in ("cost of sales", "expenses", "department total", "dept total"))
        ):
            continue
        if _has_prior_undistributed_department(location_map, location):
            continue
        next_primary_start = _next_primary_department_start(location_map, location)
        if next_primary_start is None or location.start_row > next_primary_start:
            continue
        issues.append(
            DepartmentLocationMapIssue(
                severity=Severity.WARNING,
                issue_type=DepartmentLocationIssueType.NEEDS_MORE_CONTEXT,
                department="information_and_telecommunications_systems",
                sheet_name=location.sheet_name,
                message=(
                    "Telephone was classified as IT, but the range looks like an operated department schedule "
                    "with revenue/cost/expense totals before the undistributed departments. In that context, "
                    "Telephone should usually be OOD detail rather than IT."
                ),
                requested_context=[
                    DepartmentContextRequest(
                        sheet_name=location.sheet_name,
                        start_row=location.start_row,
                        end_row=location.end_row,
                        reason="Reclassify this Telephone range as OOD detail unless surrounding rows show it is part of the undistributed IT section.",
                    )
                ],
            )
        )
    return issues


def _has_prior_undistributed_department(location_map: DepartmentLocationMap, location) -> bool:
    undistributed_departments = {
        "administrative_and_general",
        "information_and_telecommunications_systems",
        "sales_and_marketing",
        "property_operations_and_maintenance",
        "utilities",
    }
    return any(
        other.sheet_name == location.sheet_name
        and other.department in undistributed_departments
        and other.department != location.department
        and other.start_row is not None
        and other.start_row < (location.start_row or 0)
        for other in location_map.locations
    )


def _embedded_department_heading_issues(
    packet: DepartmentIdentificationPacket,
    location_map: DepartmentLocationMap,
) -> list[DepartmentLocationMapIssue]:
    compact_packets = {compact_packet.sheet_name: compact_packet for compact_packet in packet.compact_packets}
    issues: list[DepartmentLocationMapIssue] = []
    for location in location_map.locations:
        if location.start_row is None or location.end_row is None:
            continue
        compact_packet = compact_packets.get(location.sheet_name)
        if compact_packet is None:
            continue
        for row in compact_packet.rows:
            if row.row_index <= location.start_row or row.row_index > location.end_row:
                continue
            label = (row.label.normalized or row.label.raw or "").lower().strip()
            if not label:
                continue
            if not row.heuristic_hints.possible_section_boundary and not _is_explicit_department_heading(label):
                continue
            embedded_department = _department_heading_for_validation(label)
            if embedded_department is None or embedded_department == location.department:
                continue
            if _is_expected_child_or_detail_heading(location.department, embedded_department, label):
                continue
            issues.append(
                DepartmentLocationMapIssue(
                    severity=Severity.WARNING,
                    issue_type=DepartmentLocationIssueType.NEEDS_MORE_CONTEXT,
                    department=location.department,
                    sheet_name=location.sheet_name,
                    message=(
                        f"{location.department} range appears to contain a {embedded_department} heading at row {row.row_index}. "
                        "Single-tab department ranges are usually contiguous, so this may mean a neighboring department was swallowed "
                        "inside a broader range and should be split out."
                    ),
                    requested_context=[
                        DepartmentContextRequest(
                            sheet_name=location.sheet_name,
                            start_row=max((location.start_row or row.row_index), row.row_index - 5),
                            end_row=min((location.end_row or row.row_index), row.row_index + 25),
                            reason=f"Reinspect this boundary and split out {embedded_department} if it is a distinct department section.",
                        )
                    ],
                )
            )
            break
    return issues


def _is_explicit_department_heading(label: str) -> bool:
    stripped = re.sub(r"\s+", " ", label).strip()
    return stripped in {
        "summary",
        "summary operating statement",
        "rooms",
        "rooms department",
        "food and beverage",
        "f&b",
        "f&b summary",
        "other operated departments",
        "other operated department",
        "miscellaneous income",
        "misc income",
        "administrative and general",
        "admin and general",
        "admin & general",
        "a&g",
        "information technology",
        "information and telecommunications systems",
        "information and telecommunications",
        "it",
        "sales and marketing",
        "sales & marketing",
        "property operations and maintenance",
        "repairs and maintenance",
        "utilities",
        "management fees",
        "management fee",
        "non operating income and expense",
        "non-operating income and expense",
        "non operating income and expenses",
        "non-operating income and expenses",
    }


def _department_heading_for_validation(label: str) -> str | None:
    if _is_summary_heading(label):
        return "summary"
    if _is_rooms_heading(label):
        return "rooms"
    heading_patterns = [
        ("food_and_beverage", ("food and beverage", "f&b", "fb summary", "fb department")),
        ("other_operated_departments", ("other operated", "other operating", "minor operated", "minor op", "parking", "gift shop", "giftshop", "spa", "recreation", "retail", "golf")),
        ("miscellaneous_income", ("miscellaneous income", "misc income")),
        ("administrative_and_general", ("administrative and general", "admin and general", "admin & general", "a&g")),
        ("information_and_telecommunications_systems", ("information technology", "information and telecommunications", "telecommunications", "information systems", "it expenses", "it payroll", "total it")),
        ("sales_and_marketing", ("sales and marketing", "sales & marketing", "marketing", "sales department", "franchise fee", "franchise fees", "total franchise fees", "royalty fee", "royalty fees")),
        ("property_operations_and_maintenance", ("property operations and maintenance", "repairs and maintenance", "engineering", "maintenance")),
        ("utilities", ("utilities", "energy water and waste", "energy, water, and waste")),
        ("management_fees", ("management fees", "management fee", "total management fees")),
        ("non_operating_income_and_expense", ("non operating", "non-operating", "fixed charges")),
    ]
    for department, terms in heading_patterns:
        if any(_contains_text_term(label, term) for term in terms):
            return department
    return None


def _is_expected_child_or_detail_heading(parent_department: str, embedded_department: str, label: str) -> bool:
    if parent_department == "summary":
        return True
    if (
        embedded_department == "sales_and_marketing"
        and any(
            _contains_text_term(label, term)
            for term in (
                "franchise fee",
                "franchise fees",
                "royalty fee",
                "royalty fees",
                "affiliation fee",
                "affiliation fees",
                "brand fee",
                "brand fees",
                "loyalty fee",
                "loyalty fees",
            )
        )
    ):
        return True
    if parent_department == "food_and_beverage" and embedded_department == "rooms":
        return any(_contains_text_term(label, term) for term in ("room service", "in room dining", "in-room dining"))
    if parent_department == "other_operated_departments" and embedded_department == "miscellaneous_income":
        return True
    if parent_department == "other_operated_departments" and embedded_department in {
        "other_operated_departments",
        "information_and_telecommunications_systems",
    }:
        return _contains_text_term(label, "telephone") or _contains_text_term(label, "guest communications")
    return False


def _next_primary_department_start(
    location_map: DepartmentLocationMap,
    location,
) -> int | None:
    downstream_departments = {
        "miscellaneous_income",
        "administrative_and_general",
        "sales_and_marketing",
        "property_operations_and_maintenance",
        "utilities",
        "management_fees",
        "non_operating_income_and_expense",
    }
    starts = [
        other.start_row
        for other in location_map.locations
        if other.sheet_name == location.sheet_name
        and other.start_row is not None
        and other.start_row > (location.end_row or location.start_row)
        and other.department in downstream_departments
    ]
    return min(starts) if starts else None


def _meaningful_labels_for_gap(compact_packet, start_row: int, end_row: int) -> list[str]:
    labels: list[str] = []
    for row in compact_packet.rows:
        if row.row_index < start_row or row.row_index > end_row:
            continue
        label = (row.label.normalized or row.label.raw or "").lower().strip()
        if not label:
            continue
        if _is_gap_noise_label(label):
            continue
        labels.append(label)
    return labels


def _is_gap_noise_label(label: str) -> bool:
    if re.fullmatch(r"page \d+ of \d+", label):
        return True
    if re.fullmatch(r"as of \d{1,2}/\d{1,2}/\d{4}", label):
        return True
    return any(label == term or label.startswith(f"{term} ") for term in _GAP_NOISE_TERMS)


def _content_signature_issue(packet: DepartmentIdentificationPacket, location) -> DepartmentLocationMapIssue | None:
    if location.section_role not in {
        DepartmentSectionRole.PRIMARY,
        DepartmentSectionRole.SUMMARY,
    }:
        return None
    compact_packet = next(
        (item for item in packet.compact_packets if item.sheet_name == location.sheet_name),
        None,
    )
    if compact_packet is None:
        return None

    labels = _labels_for_location(compact_packet, location.start_row, location.end_row)
    if not labels:
        return None
    normalized_text = " ".join(labels)

    rule = None
    if location.department == "food_and_beverage" and location.section_role == DepartmentSectionRole.SUMMARY:
        rule = _FNB_SUMMARY_RULE
    elif location.department in _DEPARTMENT_CONTENT_RULES:
        rule = _DEPARTMENT_CONTENT_RULES[location.department]
    if rule is None:
        return None

    missing_groups = [
        group_name
        for group_name, terms in rule["groups"]
        if not any(_contains_text_term(normalized_text, term) for term in terms)
    ]
    unexpected_terms = [
        term
        for term in rule.get("unexpected_terms", ())
        if _contains_text_term(normalized_text, term)
    ]
    warn_if_many_rows = rule.get("warn_if_many_rows")
    too_many_rows = warn_if_many_rows is not None and len(labels) > warn_if_many_rows

    if not missing_groups and not unexpected_terms and not too_many_rows:
        return None

    problems: list[str] = []
    if missing_groups:
        problems.append(f"missing expected clue groups: {', '.join(missing_groups)}")
    if unexpected_terms:
        problems.append(f"contains unexpected terms for this department: {', '.join(unexpected_terms[:5])}")
    if too_many_rows:
        problems.append(f"has {len(labels)} label rows, which is unusually large for this department")

    return DepartmentLocationMapIssue(
        severity=Severity.WARNING,
        issue_type=DepartmentLocationIssueType.NEEDS_MORE_CONTEXT,
        department=location.department,
        sheet_name=location.sheet_name,
        message=f"Department content signature is weak for {location.department}: {'; '.join(problems)}. {rule['description']}",
        requested_context=[
            DepartmentContextRequest(
                sheet_name=location.sheet_name,
                reason="Reinspect this location with department-specific content clues before trusting the classification.",
                start_row=location.start_row,
                end_row=location.end_row,
            )
        ],
    )


def _labels_for_location(compact_packet, start_row: int | None, end_row: int | None) -> list[str]:
    labels: list[str] = []
    for row in compact_packet.rows:
        if start_row is not None and row.row_index < start_row:
            continue
        if end_row is not None and row.row_index > end_row:
            continue
        label = row.label.normalized or row.label.raw
        if label:
            labels.append(label.lower())
    return labels


def _missing_department_allowed_by_context(
    packet: DepartmentIdentificationPacket,
    location_map: DepartmentLocationMap,
    department: str,
) -> bool:
    if department == "miscellaneous_income":
        return _ood_location_contains_misc_income(packet, location_map)
    if department in {"management_fees", "non_operating_income_and_expense"}:
        return _summary_location_contains_department(packet, location_map, department)
    return False


def _ood_location_contains_misc_income(
    packet: DepartmentIdentificationPacket,
    location_map: DepartmentLocationMap,
) -> bool:
    return _location_text_contains_terms(
        packet,
        location_map,
        department="other_operated_departments",
        terms=(
            "miscellaneous income",
            "misc income",
            "destination fee",
            "amenity fee",
            "resort fee",
            "attrition",
            "cancellation",
            "total other income",
            "other income",
        ),
    )


def _summary_location_contains_department(
    packet: DepartmentIdentificationPacket,
    location_map: DepartmentLocationMap,
    department: str,
) -> bool:
    terms_by_department = {
        "management_fees": ("management fee", "management fees", "base fee", "incentive fee"),
        "non_operating_income_and_expense": (
            "non operating",
            "non-operating",
            "fixed charges",
            "property tax",
            "property taxes",
            "insurance",
            "rent",
            "owner expenses",
        ),
    }
    terms = terms_by_department.get(department)
    if not terms:
        return False
    return _location_text_contains_terms(packet, location_map, department="summary", terms=terms)


def _location_text_contains_terms(
    packet: DepartmentIdentificationPacket,
    location_map: DepartmentLocationMap,
    *,
    department: str,
    terms: tuple[str, ...],
) -> bool:
    compact_packets = {compact_packet.sheet_name: compact_packet for compact_packet in packet.compact_packets}
    for location in location_map.locations:
        if location.department != department:
            continue
        compact_packet = compact_packets.get(location.sheet_name)
        if compact_packet is None:
            continue
        text = " ".join(_labels_for_location(compact_packet, location.start_row, location.end_row))
        if any(_contains_text_term(text, term) for term in terms):
            return True
    return False


def _contains_text_term(text: str, term: str) -> bool:
    normalized_term = re.sub(r"\s+", " ", term.lower()).strip()
    compact_text = re.sub(r"[^a-z0-9]+", "", text)
    compact_term = re.sub(r"[^a-z0-9]+", "", normalized_term)
    if len(compact_term) <= 2:
        return bool(re.search(rf"(^|[^a-z0-9]){re.escape(compact_term)}([^a-z0-9]|$)", text))
    if re.fullmatch(r"[a-z0-9]+", normalized_term):
        return bool(re.search(rf"(^|[^a-z0-9]){re.escape(compact_term)}([^a-z0-9]|$)", text))
    return normalized_term in text or compact_term in compact_text


def _summary_boundary_issue(packet: DepartmentIdentificationPacket, location) -> DepartmentLocationMapIssue | None:
    if location.department != "summary" or location.start_row is None or location.end_row is None:
        return None
    compact_packet = next(
        (item for item in packet.compact_packets if item.sheet_name == location.sheet_name),
        None,
    )
    if compact_packet is None:
        return None

    current_labels = _labels_for_location(compact_packet, location.start_row, location.end_row)
    current_text = " ".join(current_labels)
    if not any(_contains_text_term(current_text, term) for term in ("total revenue", "gross operating profit", "gop")):
        return None
    if any(_contains_text_term(current_text, term) for term in _SUMMARY_ENDING_TERMS):
        return None

    rows_after = [
        row
        for row in compact_packet.rows
        if row.row_index > location.end_row and (row.label.normalized or row.label.raw)
    ][:60]
    after_text = " ".join((row.label.normalized or row.label.raw or "").lower() for row in rows_after)
    if not any(_contains_text_term(after_text, term) for term in _SUMMARY_ENDING_TERMS):
        return None

    rooms_start = next(
        (
            row.row_index
            for row in rows_after
            if _is_rooms_heading((row.label.normalized or row.label.raw or "").lower())
        ),
        None,
    )
    requested_end = rooms_start - 1 if rooms_start is not None else rows_after[-1].row_index
    return DepartmentLocationMapIssue(
        severity=Severity.WARNING,
        issue_type=DepartmentLocationIssueType.NEEDS_MORE_CONTEXT,
        department="summary",
        sheet_name=location.sheet_name,
        message=(
            "Early summary range appears truncated before non-op/EBITDA-style summary ending lines. "
            "In a single-tab P&L, non-op lines inside the opening summary block should remain part of Summary, "
            "not start the detailed Non-Op department."
        ),
        requested_context=[
            DepartmentContextRequest(
                sheet_name=location.sheet_name,
                reason="Reinspect the opening summary boundary through the rows immediately before the Rooms department starts.",
                start_row=location.start_row,
                end_row=requested_end,
            )
        ],
    )


def _summary_start_issue(packet: DepartmentIdentificationPacket, location) -> DepartmentLocationMapIssue | None:
    if location.department != "summary" or location.start_row is None:
        return None
    if location.section_role == DepartmentSectionRole.SUPPORTING_DETAIL:
        return None
    compact_packet = next(
        (item for item in packet.compact_packets if item.sheet_name == location.sheet_name),
        None,
    )
    if compact_packet is None:
        return None
    first_summary_row = next(
        (
            row.row_index
            for row in compact_packet.rows
            if row.row_index <= location.end_row
            and _contains_text_term((row.label.normalized or row.label.raw or "").lower(), "summary")
        ),
        None,
    )
    if first_summary_row is None or location.start_row == first_summary_row:
        return None
    if location.start_row > first_summary_row:
        return DepartmentLocationMapIssue(
            severity=Severity.WARNING,
            issue_type=DepartmentLocationIssueType.NEEDS_MORE_CONTEXT,
            department="summary",
            sheet_name=location.sheet_name,
            message=(
                "Summary range starts after the visible Summary heading. "
                "The Summary location should include the heading and KPI/stat rows at the top of the opening summary block."
            ),
            requested_context=[
                DepartmentContextRequest(
                    sheet_name=location.sheet_name,
                    reason="Move the Summary start_row back to the visible Summary heading unless those rows are a separate non-P&L preface.",
                    start_row=first_summary_row,
                    end_row=location.start_row,
                )
            ],
        )
    return DepartmentLocationMapIssue(
        severity=Severity.WARNING,
        issue_type=DepartmentLocationIssueType.NEEDS_MORE_CONTEXT,
        department="summary",
        sheet_name=location.sheet_name,
        message=(
            "Summary range starts before the visible Summary heading. "
            "Report titles, date headers, and other workbook metadata should not be included in the department location."
        ),
        requested_context=[
            DepartmentContextRequest(
                sheet_name=location.sheet_name,
                reason="Move the Summary start_row to the visible Summary heading or first true summary row.",
                start_row=location.start_row,
                end_row=first_summary_row,
            )
        ],
    )


def _summary_duplicate_issue(packet: DepartmentIdentificationPacket, location) -> DepartmentLocationMapIssue | None:
    if location.department != "summary" or location.start_row is None or location.end_row is None:
        return None
    compact_packet = next(
        (item for item in packet.compact_packets if item.sheet_name == location.sheet_name),
        None,
    )
    if compact_packet is None:
        return None

    rows = [row for row in compact_packet.rows if row.label.normalized or row.label.raw]
    rooms_start = _first_detailed_rooms_start(rows)
    if rooms_start is None:
        return None
    summary_headings = [
        row.row_index
        for row in rows
        if row.row_index < rooms_start and _is_summary_heading((row.label.normalized or row.label.raw or "").lower())
    ]
    if len(summary_headings) < 2:
        return None
    preferred_start = summary_headings[-1]
    preferred_end = rooms_start - 1
    preferred_labels = [
        (row.label.normalized or row.label.raw or "").lower()
        for row in rows
        if preferred_start <= row.row_index <= preferred_end
    ]
    preferred_text = " ".join(preferred_labels)
    if not any(_contains_text_term(preferred_text, term) for term in _SUMMARY_ENDING_TERMS):
        return None
    if location.start_row == preferred_start and location.end_row == preferred_end:
        return None
    return DepartmentLocationMapIssue(
        severity=Severity.WARNING,
        issue_type=DepartmentLocationIssueType.NEEDS_MORE_CONTEXT,
        department="summary",
        sheet_name=location.sheet_name,
        message=(
            "Multiple opening summary sections appear before the detailed Rooms section. "
            "Prefer the later/fuller Summary Operating Statement when it contains more bottom-of-P&L lines."
        ),
        requested_context=[
            DepartmentContextRequest(
                sheet_name=location.sheet_name,
                reason="Choose the fuller opening summary block and end it immediately before the detailed Rooms section.",
                start_row=preferred_start,
                end_row=preferred_end,
            )
        ],
    )


def _section_role_issue(location, location_map: DepartmentLocationMap) -> DepartmentLocationMapIssue | None:
    if location.department in _PRIMARY_ROLE_DEPARTMENTS and location.section_role != DepartmentSectionRole.PRIMARY:
        if location.section_role == DepartmentSectionRole.SUPPORTING_DETAIL and any(
            other.department == location.department
            and other.sheet_name != location.sheet_name
            and other.section_role == DepartmentSectionRole.PRIMARY
            for other in location_map.locations
        ):
            return None
        return DepartmentLocationMapIssue(
            severity=Severity.WARNING,
            issue_type=DepartmentLocationIssueType.NEEDS_MORE_CONTEXT,
            department=location.department,
            sheet_name=location.sheet_name,
            message=(
                f"{location.department} should normally be section_role=primary at Department ID. "
                f"Returned section_role={location.section_role.value}."
            ),
            requested_context=[
                DepartmentContextRequest(
                    sheet_name=location.sheet_name,
                    reason="Reinspect whether this is the primary department location or only a supporting/detail schedule.",
                    start_row=location.start_row,
                    end_row=location.end_row,
                )
            ],
        )
    return None


def _is_rooms_heading(text: str) -> bool:
    stripped = text.strip()
    return stripped in {"rooms", "rooms department"} or _contains_text_term(stripped, "rooms department")


def _is_summary_heading(text: str) -> bool:
    stripped = text.strip()
    return stripped in {"summary", "summary operating statement"} or _contains_text_term(stripped, "summary operating statement")


def _first_detailed_rooms_start(rows) -> int | None:
    for index, row in enumerate(rows):
        label = (row.label.normalized or row.label.raw or "").lower()
        if not _is_rooms_heading(label):
            continue
        next_labels = [
            (later.label.normalized or later.label.raw or "").lower()
            for later in rows[index + 1 : index + 6]
        ]
        if _looks_like_summary_rooms_rollup(next_labels):
            continue
        if any(_contains_text_term(next_label, "revenue") for next_label in next_labels):
            return row.row_index
    return None


def _looks_like_summary_rooms_rollup(next_labels: list[str]) -> bool:
    next_text = " ".join(next_labels)
    return (
        any(_contains_text_term(next_text, term) for term in ("food and beverage", "f&b"))
        and any(_contains_text_term(next_text, term) for term in ("other operated", "miscellaneous income", "misc income"))
    )


def _missing_department_context_requests(
    packet: DepartmentIdentificationPacket,
    department: str,
    *,
    limit: int = 5,
) -> list[DepartmentContextRequest]:
    """Rank repair context by department content rather than workbook order."""
    rule = _DEPARTMENT_CONTENT_RULES.get(department)
    if rule is None and department == "food_and_beverage":
        rule = _FNB_SUMMARY_RULE
    if rule is None:
        return [
            DepartmentContextRequest(
                sheet_name=compact_packet.sheet_name,
                reason=f"Reinspect for missing {department} department location.",
            )
            for compact_packet in packet.compact_packets[:limit]
        ]

    candidates_by_sheet = {
        selection.sheet_name: {
            candidate.department
            for candidate in selection.department_candidates
        }
        for selection in packet.sheet_name_selection.selections
    }
    ranked: list[tuple[int, str, tuple[str, ...], int | None, int | None]] = []
    for compact_packet in packet.compact_packets:
        labels = [
            row.label.normalized or ""
            for row in compact_packet.rows
            if row.label.normalized
        ]
        matched_groups = []
        matched_row_count = 0
        for group_name, terms in rule["groups"]:
            group_matches = sum(
                any(term in label for term in terms)
                for label in labels
            )
            if group_matches:
                matched_groups.append(group_name)
                matched_row_count += group_matches
        routing_bonus = (
            25
            if department
            in candidates_by_sheet.get(compact_packet.sheet_name, set())
            else 0
        )
        score = len(matched_groups) * 100 + min(matched_row_count, 50) + routing_bonus
        if score <= 0:
            continue
        ranked.append(
            (
                score,
                compact_packet.sheet_name,
                tuple(matched_groups),
                compact_packet.start_row,
                compact_packet.end_row,
            )
        )

    if not ranked:
        return [
            DepartmentContextRequest(
                sheet_name=compact_packet.sheet_name,
                reason=f"Reinspect for missing {department} department location.",
            )
            for compact_packet in packet.compact_packets[:limit]
        ]

    ranked.sort(key=lambda item: (-item[0], item[1].lower()))
    return [
        DepartmentContextRequest(
            sheet_name=sheet_name,
            start_row=start_row,
            end_row=end_row,
            reason=(
                f"Reinspect for missing {department}; this sheet has "
                f"content evidence for {', '.join(groups)}."
            ),
        )
        for _, sheet_name, groups, start_row, end_row in ranked[:limit]
    ]
