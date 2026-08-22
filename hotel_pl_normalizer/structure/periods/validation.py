"""Check a period catalog against the packet it claims to describe.

Everything here answers one question: could this answer be true of this workbook?
Bindings must point at columns that exist and carry period evidence, an option
cannot bind a location twice, coverage has to reach the locations that matter, and
a period's identity has to match the headers behind it.

These run on model output before anything downstream trusts it, so a check that
passes something wrong is worse than one that is too strict: a rejected catalog
goes to repair, while a wrong one is bound to figures and shipped.
"""

from __future__ import annotations

import re
from collections import defaultdict

from hotel_pl_normalizer.models.common import (
    Severity,
    ValidationStatus,
)
from hotel_pl_normalizer.models.period_selection import (
    PeriodCatalog,
    PeriodCatalogIssue,
    PeriodCatalogValidation,
    PeriodColumnCandidate,
    PeriodColumnPacket,
    PeriodColumnSelection,
    PeriodColumnSelectionIssue,
    PeriodColumnSelectionMap,
    PeriodColumnSelectionValidation,
    PeriodOption,
    PeriodScenario,
    PeriodSheetPacket,
    PeriodSheetRole,
    PeriodType,
    UnavailablePeriodLocation,
    humanize_period_label,
)

from .signals import (
    _candidate_looks_like_rejected_metric_column,
    _candidate_option_mismatch,
    _contains_mtd,
    _contains_ytd,
    _coverage_scenario_matches,
    _direct_header_values,
    _discovery_representatives,
    _excel_column_number,
    _local_period_identity,
    _looks_like_primary_statement_sheet,
    _looks_like_supporting_schedule_sheet,
    _nearest_explicit_month,
    _nearest_explicit_year,
    _packet_location_id,
)


def validate_selected_period_catalog(
    packet: PeriodColumnPacket,
    catalog: PeriodCatalog,
    selected_period_ids: list[str],
) -> PeriodCatalog:
    """Validate sheet-level selected-period lookups using the original light contract."""
    issues: list[PeriodCatalogIssue] = []
    locations = {_packet_location_id(sheet): sheet for sheet in packet.sheets}
    candidates = {
        (location_id, candidate.column): candidate
        for location_id, sheet in locations.items()
        for candidate in sheet.candidate_columns
    }
    expected_ids = list(dict.fromkeys(selected_period_ids))
    option_ids = [option.period_id for option in catalog.options]
    if option_ids != expected_ids:
        issues.append(
            PeriodCatalogIssue(
                severity=Severity.ERROR,
                message=(
                    "Binding response must contain the selected period ids in order; "
                    f"expected={expected_ids}, returned={option_ids}."
                ),
            )
        )

    for option in catalog.options:
        dispositions: dict[str, int] = defaultdict(int)
        for binding in option.bindings:
            for location_id in binding.location_ids:
                dispositions[location_id] += 1
                sheet = locations.get(location_id)
                if sheet is None:
                    issues.append(
                        PeriodCatalogIssue(
                            severity=Severity.ERROR,
                            period_id=option.period_id,
                            location_id=location_id,
                            message="Binding references an unknown department location.",
                        )
                    )
                    continue
                candidate = candidates.get((location_id, binding.value_column))
                if candidate is None or candidate.excel_column != binding.excel_column:
                    issues.append(
                        PeriodCatalogIssue(
                            severity=Severity.ERROR,
                            period_id=option.period_id,
                            location_id=location_id,
                            message=(
                                f"Selected column {binding.excel_column} is not a candidate "
                                "for this location."
                            ),
                        )
                    )
                    continue
                if candidate.numeric_count == 0:
                    issues.append(
                        PeriodCatalogIssue(
                            severity=Severity.ERROR,
                            period_id=option.period_id,
                            location_id=location_id,
                            message=f"Column {binding.excel_column} has no numeric values.",
                        )
                    )
                elif _candidate_looks_like_rejected_metric_column(candidate):
                    issues.append(
                        PeriodCatalogIssue(
                            severity=Severity.ERROR,
                            period_id=option.period_id,
                            location_id=location_id,
                            message=(
                                f"Column {binding.excel_column} looks like a percentage, "
                                "variance, or KPI column."
                            ),
                        )
                    )
                elif _candidate_looks_like_ratio_values(
                    candidate, sheet.candidate_columns
                ):
                    issues.append(
                        PeriodCatalogIssue(
                            severity=Severity.ERROR,
                            period_id=option.period_id,
                            location_id=location_id,
                            message=(
                                f"Column {binding.excel_column} is dominated by "
                                "percentage-formatted or ratio-scale values rather "
                                "than financial amounts."
                            ),
                        )
                    )
                elif mismatch := _selected_binding_mismatch(
                    candidate, option, sheet
                ):
                    issues.append(
                        PeriodCatalogIssue(
                            severity=Severity.ERROR,
                            period_id=option.period_id,
                            location_id=location_id,
                            message=(
                                f"Column {binding.excel_column} does not match "
                                f"{option.label!r}: {mismatch}"
                            ),
                        )
                    )
        for unavailable in option.unavailable_locations:
            dispositions[unavailable.location_id] += 1
            sheet = locations.get(unavailable.location_id)
            if sheet is not None and not sheet.candidate_columns:
                # Header-only routing locations cannot contribute financial
                # evidence. Their explicit unavailable disposition completes
                # the contract without vetoing usable bindings elsewhere.
                continue
            issues.append(
                PeriodCatalogIssue(
                    severity=Severity.ERROR,
                    period_id=option.period_id,
                    location_id=unavailable.location_id,
                    message=(
                        "Selected period is unavailable for this mapping location: "
                        f"{unavailable.reason}."
                    ),
                )
            )
        for location_id in locations:
            count = dispositions.get(location_id, 0)
            if count != 1:
                issues.append(
                    PeriodCatalogIssue(
                        severity=Severity.ERROR,
                        period_id=option.period_id,
                        location_id=location_id,
                        message=(
                            "Return exactly one sheet-level binding or unavailable "
                            f"decision for this period; received {count}."
                        ),
                    )
                )

    status = (
        ValidationStatus.FAIL
        if any(issue.severity == Severity.ERROR for issue in issues)
        else ValidationStatus.PASS
    )
    return catalog.model_copy(
        update={
            "workbook_id": packet.workbook_id,
            "validation": PeriodCatalogValidation(status=status, issues=issues),
        }
    )


def _selected_binding_mismatch(
    candidate: PeriodColumnCandidate,
    option: PeriodOption,
    sheet: PeriodSheetPacket | None = None,
) -> str | None:
    """Conservative semantic checks from the original sheet selector."""
    context = " ".join(candidate.header_context).lower()
    has_budget = "budget" in context
    has_forecast = any(term in context for term in ("forecast", "outlook"))
    has_prior = any(
        term in context
        for term in ("prior year", "last year", "previous year", "-1yr", "1yr")
    )
    expected = option.end_period or option.start_period
    expected_year = (
        expected.split("-", 1)[0]
        if expected and re.fullmatch(r"20\d{2}(?:-\d{2})?", expected)
        else None
    )
    explicit_year = _nearest_explicit_year(candidate)
    explicit_prior_date = bool(
        expected_year and explicit_year and expected_year == explicit_year
    )
    sheet_scenario = _unambiguous_sheet_scenario(sheet)
    if (
        option.scenario == PeriodScenario.BUDGET
        and not has_budget
        and sheet_scenario != PeriodScenario.BUDGET
    ):
        return "header does not identify Budget"
    if (
        option.scenario == PeriodScenario.FORECAST
        and not has_forecast
        and sheet_scenario != PeriodScenario.FORECAST
    ):
        return "header does not identify Forecast"
    if (
        option.scenario == PeriodScenario.PRIOR_ACTUAL
        and not has_prior
        and not explicit_prior_date
        and sheet_scenario != PeriodScenario.PRIOR_ACTUAL
    ):
        return "header identifies current Actual rather than Prior/Last Year"
    if option.scenario == PeriodScenario.ACTUAL and (has_budget or has_forecast or has_prior):
        return "header identifies a different scenario"

    local_type = _local_period_identity(candidate)[2]
    if option.period_type == PeriodType.YTD and local_type in {
        PeriodType.MONTH,
        PeriodType.CURRENT_PERIOD,
        PeriodType.FULL_YEAR,
    }:
        return f"header period type is {local_type.value}"
    if (
        option.period_type == PeriodType.FULL_YEAR
        and not _candidate_has_full_year_evidence(candidate)
        and not (
            option.scenario
            in {
                PeriodScenario.BUDGET,
                PeriodScenario.FORECAST,
                PeriodScenario.PRIOR_ACTUAL,
            }
            and _sheet_has_full_year_evidence(
                sheet.candidate_columns if sheet is not None else []
            )
        )
    ):
        return "header and surrounding report block do not identify a full year"

    explicit_month = _nearest_explicit_month(candidate)
    if (
        option.period_type in {PeriodType.MONTH, PeriodType.CURRENT_PERIOD}
        and expected
        and explicit_month
        and re.fullmatch(r"20\d{2}-\d{2}", expected)
    ):
        expected_year, expected_month = expected.split("-")
        month_numbers = {
            name: f"{index:02d}"
            for index, names in enumerate(
                (
                    ("jan", "january"),
                    ("feb", "february"),
                    ("mar", "march"),
                    ("apr", "april"),
                    ("may",),
                    ("jun", "june"),
                    ("jul", "july"),
                    ("aug", "august"),
                    ("sep", "september"),
                    ("oct", "october"),
                    ("nov", "november"),
                    ("dec", "december"),
                ),
                start=1,
            )
            for name in names
        }
        explicit_month_number = month_numbers[explicit_month]
        explicit_year = _nearest_explicit_year(candidate)
        if explicit_month_number != expected_month:
            return f"header identifies {explicit_month}, not month {expected_month}"
        if (
            option.scenario != PeriodScenario.PRIOR_ACTUAL
            and explicit_year is not None
            and explicit_year != expected_year
        ):
            return f"header identifies {explicit_year}, not {expected_year}"
    return None


def validate_discovery_period_catalog(
    packet: PeriodColumnPacket, catalog: PeriodCatalog
) -> PeriodCatalog:
    """Validate concept coverage without pretending discovery bindings are values."""
    issues: list[PeriodCatalogIssue] = []
    location_ids = {_packet_location_id(sheet) for sheet in packet.sheets}
    option_ids = [option.period_id for option in catalog.options]
    if len(option_ids) != len(set(option_ids)):
        issues.append(
            PeriodCatalogIssue(
                severity=Severity.ERROR,
                message="Period option ids must be unique.",
            )
        )
    if catalog.recommended_period_id not in set(option_ids):
        issues.append(
            PeriodCatalogIssue(
                severity=Severity.ERROR,
                message="recommended_period_id must identify a catalog option.",
            )
        )

    issues.extend(_representative_sheet_assessment_issues(packet, catalog))
    roles = {item.location_id: item.role for item in catalog.sheet_assessments}
    primary_ids = {
        _packet_location_id(members[0])
        for members in _discovery_representatives(packet)
        if roles.get(_packet_location_id(members[0])) == PeriodSheetRole.PRIMARY_CORE
    }
    seen_concepts = set()
    for option in catalog.options:
        concept = (
            option.scenario,
            option.period_type,
            option.start_period,
            option.end_period,
        )
        if concept in seen_concepts:
            issues.append(
                PeriodCatalogIssue(
                    severity=Severity.ERROR,
                    period_id=option.period_id,
                    message="Duplicate period concept; return it only once.",
                )
            )
        seen_concepts.add(concept)
        covered = set()
        for binding in option.bindings:
            for location_id in binding.location_ids:
                if location_id in covered:
                    issues.append(
                        PeriodCatalogIssue(
                            severity=Severity.ERROR,
                            period_id=option.period_id,
                            location_id=location_id,
                            message="A period option cannot cite the same sheet twice.",
                        )
                    )
                covered.add(location_id)
                if location_id not in location_ids:
                    issues.append(
                        PeriodCatalogIssue(
                            severity=Severity.ERROR,
                            period_id=option.period_id,
                            location_id=location_id,
                            message="Discovery evidence references an unknown sampled sheet.",
                        )
                    )
        for location_id in sorted(primary_ids - covered):
            issues.append(
                PeriodCatalogIssue(
                    severity=Severity.ERROR,
                    period_id=option.period_id,
                    location_id=location_id,
                    message=(
                        "Period heading is not evidenced on every sampled primary_core "
                        "sheet. Add a binding_patch for this sheet, reclassify the sheet, "
                        "or remove the partial period."
                    ),
                )
            )

    actual_dates = {
        (option.period_type, option.start_period, option.end_period)
        for option in catalog.options
        if option.scenario == PeriodScenario.ACTUAL
        and option.start_period is not None
    }
    for option in catalog.options:
        if option.scenario == PeriodScenario.PRIOR_ACTUAL and (
            option.period_type,
            option.start_period,
            option.end_period,
        ) in actual_dates:
            issues.append(
                PeriodCatalogIssue(
                    severity=Severity.ERROR,
                    period_id=option.period_id,
                    message="Prior Actual must use the prior year's dates.",
                )
            )

    status = (
        ValidationStatus.FAIL
        if any(issue.severity == Severity.ERROR for issue in issues)
        else ValidationStatus.PASS
    )
    return catalog.model_copy(
        update={
            "workbook_id": packet.workbook_id,
            "options": _humanize_catalog_options(catalog.options),
            "validation": PeriodCatalogValidation(status=status, issues=issues),
        }
    )


def _catalog_identity_issues(catalog: PeriodCatalog) -> list[PeriodCatalogIssue]:
    """Checks about the catalog as a whole, before any binding is examined.

    Both are cheap and both are fatal downstream: duplicate ids make every later
    lookup ambiguous, and a recommendation naming no option leaves the CLI and the
    demo server with nothing to select by default.
    """
    issues: list[PeriodCatalogIssue] = []
    option_ids = [option.period_id for option in catalog.options]
    if len(option_ids) != len(set(option_ids)):
        issues.append(
            PeriodCatalogIssue(
                severity=Severity.ERROR,
                message="Period option ids must be unique.",
            )
        )
    if catalog.recommended_period_id not in set(option_ids):
        issues.append(
            PeriodCatalogIssue(
                severity=Severity.ERROR,
                message="recommended_period_id must identify a catalog option.",
            )
        )
    return issues


def _binding_rejection(
    sheet: PeriodSheetPacket,
    candidate: PeriodColumnCandidate,
    binding,
    option: PeriodOption,
    *,
    concept_only: bool,
) -> str | None:
    """Why this column cannot carry this period on this tab, or None if it can.

    Ordered cheapest and most conclusive first, and the order matters: a column
    that is a percentage is reported as a percentage rather than as a period
    mismatch, which is the more useful thing for a repair pass to be told.

    `concept_only` runs during discovery, when the question is whether the period
    *concept* is coherent rather than whether this exact column is the best source
    for it. Header-strength comparisons are therefore skipped -- discovery has not
    yet decided which column wins -- and the one arithmetic check that still
    applies is whether a column's month spread can add up to the claimed total.
    """
    if candidate.excel_column != binding.excel_column:
        return (
            f"Column number {binding.value_column} does not match "
            f"{binding.excel_column} for this location."
        )
    if _candidate_looks_like_rejected_metric_column(candidate):
        return (
            f"Column {binding.excel_column} looks like a percentage, variance, "
            "or KPI column."
        )
    if _looks_like_period_control_candidate(sheet, candidate):
        return (
            f"Column {binding.excel_column} looks like a workbook period-control "
            "or configuration column, not a P&L value column."
        )
    if concept_only:
        total_mismatch = _month_spread_total_mismatch(sheet, candidate, option)
        if total_mismatch:
            return (
                f"Column {binding.excel_column} does not support "
                f"{option.label!r}: {total_mismatch}"
            )
        return None
    if not _has_identified_period(candidate):
        return (
            f"Column {binding.excel_column} has no identifiable local period "
            "header evidence."
        )
    if mismatch := _candidate_option_mismatch(candidate, option):
        return (
            f"Column {binding.excel_column} does not match {option.label!r} "
            f"on this tab: {mismatch}"
        )
    if better := _more_specific_candidate_for_option(sheet, candidate, option):
        return (
            f"Column {binding.excel_column} is weaker evidence for "
            f"{option.label!r} than explicit header column {better.excel_column}."
        )
    return None


def _option_binding_issues(
    option: PeriodOption,
    locations: dict[str, PeriodSheetPacket],
    candidate_lookup: dict[tuple[str, int], PeriodColumnCandidate],
    column_uses: dict[tuple[str, int], list[PeriodOption]],
    *,
    concept_only: bool,
) -> tuple[list[PeriodCatalogIssue], set[str], int]:
    """Examine one option's bindings.

    Returns the issues raised, the locations this option claims to cover, and how
    many of its bindings survived. `column_uses` is filled in as a side effect --
    it is the input to the cross-option reuse check, which cannot run until every
    option has been examined.

    Only bindings that survive outside `concept_only` are recorded there. During
    discovery a column has not yet been committed to a period, so counting it as
    a use would report a conflict that the later, stricter pass is what decides.
    """
    issues: list[PeriodCatalogIssue] = []
    covered: set[str] = set()
    supported = 0

    def reject(location_id: str, message: str) -> None:
        issues.append(
            PeriodCatalogIssue(
                severity=Severity.ERROR,
                period_id=option.period_id,
                location_id=location_id,
                message=message,
            )
        )

    for binding in option.bindings:
        for location_id in binding.location_ids:
            if location_id in covered:
                reject(
                    location_id,
                    "A period option cannot bind the same location twice.",
                )
            covered.add(location_id)
            if location_id not in locations:
                reject(location_id, "Binding references an unknown department location.")
                continue
            candidate = candidate_lookup.get((location_id, binding.value_column))
            if candidate is None:
                reject(
                    location_id,
                    f"Column {binding.excel_column} is unavailable for this "
                    "location; inspect this tab separately.",
                )
                continue
            rejection = _binding_rejection(
                locations[location_id],
                candidate,
                binding,
                option,
                concept_only=concept_only,
            )
            if rejection is not None:
                reject(location_id, rejection)
                continue
            supported += 1
            if not concept_only:
                column_uses[(location_id, candidate.column)].append(option)
    return issues, covered, supported


def _omitted_tab_issues(
    option: PeriodOption,
    locations: dict[str, PeriodSheetPacket],
    covered: set[str],
) -> list[PeriodCatalogIssue]:
    """Tabs this option skipped that plainly offer a column matching it.

    A skipped department is not a loud failure -- the mapping simply comes back
    without those figures -- so this looks for the case the model has no excuse
    for: a column with direct header evidence, of the right scenario, that
    nothing refutes.
    """
    issues: list[PeriodCatalogIssue] = []
    for location_id, sheet in locations.items():
        if location_id in covered:
            continue
        matching = next(
            (
                candidate
                for candidate in sheet.candidate_columns
                if _has_identified_period(candidate)
                and _has_direct_period_header_evidence(candidate)
                and _coverage_scenario_matches(candidate, option)
                and not _candidate_looks_like_rejected_metric_column(candidate)
                and _candidate_option_mismatch(candidate, option) is None
            ),
            None,
        )
        if matching is not None:
            issues.append(
                PeriodCatalogIssue(
                    severity=Severity.ERROR,
                    period_id=option.period_id,
                    location_id=location_id,
                    message=(
                        f"Option omitted this tab even though column "
                        f"{matching.excel_column} matches {option.label!r}."
                    ),
                )
            )
    return issues


def _incompatible_column_reuse_issues(
    column_uses: dict[tuple[str, int], list[PeriodOption]],
    candidate_lookup: dict[tuple[str, int], PeriodColumnCandidate],
) -> list[PeriodCatalogIssue]:
    """One column claimed by periods that cannot both be true of it.

    This is the only check that needs every option at once. A column with a weak
    header -- "YTD Actual" with no year -- cannot be refuted for either period on
    its own; the conflict exists only in the comparison. A dated header is caught
    one binding at a time instead, by `_binding_rejection`.

    Reported once per distinct (column, set of options) pair rather than once per
    tab, because the same convention usually repeats across every department and
    one message describing it is more useful than twenty.
    """
    incompatible_reuse: dict[
        tuple[int, tuple[str, ...]], list[tuple[str, list[PeriodOption]]]
    ] = defaultdict(list)
    for (location_id, column), options in column_uses.items():
        identities = {
            (option.scenario, option.start_period, option.end_period)
            for option in options
        }
        if len(identities) <= 1:
            continue
        key = (column, tuple(sorted(option.period_id for option in options)))
        incompatible_reuse[key].append((location_id, options))

    issues: list[PeriodCatalogIssue] = []
    for (column, _), matches in incompatible_reuse.items():
        location_id, options = matches[0]
        candidate = candidate_lookup[(location_id, column)]
        labels = ", ".join(sorted({option.label for option in options}))
        issues.append(
            PeriodCatalogIssue(
                severity=Severity.ERROR,
                period_id=options[0].period_id,
                location_id=location_id,
                message=(
                    f"Column {candidate.excel_column} is reused for incompatible "
                    f"periods ({labels}) on {len(matches)} tab(s); one source column "
                    "cannot represent different scenarios or dates."
                ),
            )
        )
    return issues


def _prior_actual_date_collision_issues(
    catalog: PeriodCatalog,
) -> list[PeriodCatalogIssue]:
    """A prior period carrying the current period's dates.

    The usual cause is a model that recognised a second Actual column and labelled
    it prior without shifting the year. Left alone it produces two periods that
    claim the same dates, and the output workbook shows the same figures twice
    under different headings, which reads as agreement rather than as a bug.
    """
    actual_dates = {
        (option.period_type, option.start_period, option.end_period)
        for option in catalog.options
        if option.scenario == PeriodScenario.ACTUAL and option.start_period is not None
    }
    return [
        PeriodCatalogIssue(
            severity=Severity.ERROR,
            period_id=option.period_id,
            message=(
                "Prior Actual uses the same dates as Current Actual; shift the "
                "prior period back to the year identified by its headers."
            ),
        )
        for option in catalog.options
        if option.scenario == PeriodScenario.PRIOR_ACTUAL
        and (option.period_type, option.start_period, option.end_period) in actual_dates
    ]


def _report_date_alignment_issues(
    packet: PeriodColumnPacket, catalog: PeriodCatalog
) -> list[PeriodCatalogIssue]:
    """Dated periods must agree with the report date the workbook states.

    When the workbook says what it covers -- "December 2025" in a title row -- a
    month, current-period or YTD option is not free to claim other dates. Prior
    periods are expected one year back from the same month; annual and trailing
    types are exempt because their spans are not derivable from a report date.

    Only during discovery (`concept_only`), where dates are still being decided.
    """
    report_period = _discovery_report_period(packet)
    if report_period is None:
        return []
    report_year, report_month = report_period.split("-")
    dated_types = {PeriodType.MONTH, PeriodType.CURRENT_PERIOD, PeriodType.YTD}

    issues: list[PeriodCatalogIssue] = []
    for option in catalog.options:
        if option.period_type not in dated_types:
            continue
        option_year = (
            str(int(report_year) - 1)
            if option.scenario == PeriodScenario.PRIOR_ACTUAL
            else report_year
        )
        expected_end = f"{option_year}-{report_month}"
        expected_start = (
            f"{option_year}-01" if option.period_type == PeriodType.YTD else expected_end
        )
        if option.start_period != expected_start or option.end_period != expected_end:
            issues.append(
                PeriodCatalogIssue(
                    severity=Severity.ERROR,
                    period_id=option.period_id,
                    message=(
                        "Report-date headers require a concept_patch with "
                        f"start_period={expected_start} and "
                        f"end_period={expected_end}."
                    ),
                )
            )
    return issues


def validate_period_catalog(
    packet: PeriodColumnPacket,
    catalog: PeriodCatalog,
    *,
    require_complete_coverage: bool = True,
    require_representative_coverage: bool = False,
    concept_only: bool = False,
) -> PeriodCatalog:
    """Decide whether this catalog could be true of this workbook.

    The checks run in widening scope: the catalog's own identity, then each
    option's bindings one at a time, then the comparisons that need every option
    at once, then coverage across tabs. Each returns its issues rather than
    mutating shared state, so any one of them can be read, tested or changed
    without holding the rest in mind.

    Every issue is an ERROR and any error fails the catalog. That is deliberate:
    a failed catalog goes to a repair pass, while one that passes is bound to
    real figures and shipped, so being too strict costs a round trip and being
    too lax costs a wrong workbook.

    `concept_only` is discovery's mode -- is this period a coherent idea? --
    against the default, which asks the stricter question of whether these exact
    columns are the right sources.
    """
    # A catalog written against the selected-period contract is a different
    # document with different rules, and that validator owns it.
    if "binding_contract=sheet_period_v1" in catalog.notes and not concept_only:
        return validate_selected_period_catalog(
            packet,
            catalog,
            [option.period_id for option in catalog.options],
        )

    locations = {_packet_location_id(sheet): sheet for sheet in packet.sheets}
    candidate_lookup = {
        (location_id, candidate.column): candidate
        for location_id, sheet in locations.items()
        for candidate in sheet.candidate_columns
    }
    column_uses: dict[tuple[str, int], list[PeriodOption]] = defaultdict(list)

    issues = _catalog_identity_issues(catalog)

    for option in catalog.options:
        option_issues, covered, supported = _option_binding_issues(
            option,
            locations,
            candidate_lookup,
            column_uses,
            concept_only=concept_only,
        )
        issues.extend(option_issues)
        if require_complete_coverage:
            issues.extend(_omitted_tab_issues(option, locations, covered))
        if supported == 0:
            issues.append(
                PeriodCatalogIssue(
                    severity=Severity.ERROR,
                    period_id=option.period_id,
                    message="Period option has no supported source columns.",
                )
            )

    issues.extend(_incompatible_column_reuse_issues(column_uses, candidate_lookup))
    issues.extend(_prior_actual_date_collision_issues(catalog))
    if concept_only:
        issues.extend(_report_date_alignment_issues(packet, catalog))

    if (
        require_complete_coverage
        and not concept_only
        and packet.packet_id.endswith(":period_discovery")
    ):
        issues.extend(_missing_catalog_period_issues(packet, catalog))
    if require_representative_coverage:
        issues.extend(_representative_sheet_assessment_issues(packet, catalog))
        if concept_only:
            primary_packet = _primary_core_discovery_packet(packet, catalog)
            if primary_packet.sheets:
                issues.extend(
                    _missing_catalog_period_issues(
                        primary_packet,
                        catalog,
                        allow_single=True,
                        require_all_sheets=True,
                    )
                )
        issues.extend(_representative_coverage_issues(packet, catalog))

    status = ValidationStatus.PASS
    if any(issue.severity == Severity.ERROR for issue in issues):
        status = ValidationStatus.FAIL
    return catalog.model_copy(
        update={
            "workbook_id": packet.workbook_id,
            "options": _humanize_catalog_options(catalog.options),
            "validation": PeriodCatalogValidation(status=status, issues=issues),
        }
    )


def _humanize_catalog_options(options: list[PeriodOption]) -> list[PeriodOption]:
    labels = [_structured_period_label(option) for option in options]
    return [
        option.model_copy(
            update={
                "label": label
            }
        )
        for option, label in zip(options, labels)
    ]


def _structured_period_label(option: PeriodOption) -> str:
    fallback = humanize_period_label(
        f"{option.label} {option.scenario.value} {option.period_type.value}"
    )
    period = option.end_period or option.start_period
    if not period or not re.fullmatch(r"20\d{2}-\d{2}", period):
        return fallback
    year, month = period.split("-")
    month_name = (
        "Jan", "Feb", "Mar", "Apr", "May", "Jun",
        "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
    )[int(month) - 1]
    scenario = {
        PeriodScenario.BUDGET: "Budget",
        PeriodScenario.FORECAST: "Forecast",
        PeriodScenario.BLENDED: "Blended",
    }.get(option.scenario, "Actual")
    if option.period_type == PeriodType.MONTH:
        return f"{month_name} {year} {scenario}"
    if option.period_type == PeriodType.YTD:
        return f"{month_name} {year} YTD {scenario}"
    if option.period_type == PeriodType.CURRENT_PERIOD:
        return f"{month_name} {year} MTD {scenario}"
    if option.period_type == PeriodType.FULL_YEAR:
        return f"{year} {scenario}"
    if option.period_type == PeriodType.TTM:
        return f"{month_name} {year} TTM {scenario}"
    return fallback


def _more_specific_candidate_for_option(
    sheet: PeriodSheetPacket,
    selected: PeriodColumnCandidate,
    option: PeriodOption,
) -> PeriodColumnCandidate | None:
    selected_score = _candidate_option_specificity(selected, option)
    alternatives = [
        candidate
        for candidate in sheet.candidate_columns
        if candidate.column != selected.column
        and not _candidate_looks_like_rejected_metric_column(candidate)
        and not _looks_like_period_control_candidate(sheet, candidate)
        and (
            _coverage_scenario_matches(candidate, option)
            or (
                option.scenario == PeriodScenario.PRIOR_ACTUAL
                and _local_period_identity(candidate)[1] == PeriodScenario.ACTUAL
            )
        )
        and _candidate_option_mismatch(candidate, option) is None
        and _candidate_option_specificity(candidate, option) > selected_score
    ]
    if len(alternatives) == 1:
        return alternatives[0]
    return None


def _candidate_option_specificity(
    candidate: PeriodColumnCandidate, option: PeriodOption
) -> int:
    direct = " ".join(_direct_header_values(candidate)).lower()
    _, scenario, period_type = _local_period_identity(candidate)
    score = 0
    expected = option.start_period
    if expected and re.fullmatch(r"20\d{2}-\d{2}", expected):
        expected_year = expected[:4]
        if _nearest_explicit_year(candidate) == expected_year:
            score += 3
    if scenario == option.scenario or (
        option.scenario == PeriodScenario.PRIOR_ACTUAL
        and scenario == PeriodScenario.ACTUAL
    ):
        score += 2
    if period_type == option.period_type:
        score += 1
    if option.period_type == PeriodType.MONTH and _nearest_explicit_month(candidate):
        score += 2
    elif option.period_type == PeriodType.YTD and _contains_ytd(direct):
        score += 2
    elif option.period_type == PeriodType.CURRENT_PERIOD and _contains_mtd(direct):
        score += 2
    elif option.period_type == PeriodType.FULL_YEAR and "total" in direct:
        score += 1
    return score


def _has_identified_period(candidate: PeriodColumnCandidate) -> bool:
    """Return whether local headers identify a period for deterministic binding."""
    return _local_period_identity(candidate)[2] != PeriodType.UNKNOWN


def _report_date_hints(sheet: PeriodSheetPacket, *, limit: int = 3) -> list[str]:
    """Keep only compact report-level date cells, never the full header snapshot."""
    strong = []
    fallback = []
    for cell in sheet.header_cells:
        value = re.sub(r"\s+", " ", cell.value).strip()
        lowered = value.lower()
        rendered = f"{cell.coordinate}: {value}"
        if re.search(r"\b(?:as of|period end(?:ed|ing)?|for the period)\b", lowered):
            strong.append(rendered)
        elif (
            re.search(r"\b20\d{2}[. /-](?:0?[1-9]|1[0-2]|jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)\b", lowered)
            or re.search(r"\b(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\s+\d{1,2},?\s+20\d{2}\b", lowered)
        ):
            fallback.append(rendered)
    return list(dict.fromkeys([*strong, *fallback]))[:limit]


def _discovery_report_period(packet: PeriodColumnPacket) -> str | None:
    """Read an explicit report-ending month shared by discovery evidence."""
    texts = [
        hint.split(": ", 1)[-1]
        for members in _discovery_representatives(packet)
        for hint in _report_date_hints(members[0])
    ]
    texts.append(packet.source_filename)
    month_numbers = {
        name: f"{index:02d}"
        for index, name in enumerate(
            ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"),
            start=1,
        )
    }
    for text in texts:
        numeric = re.search(r"\b(0?[1-9]|1[0-2])[/.-]\d{1,2}[/.-](20\d{2})\b", text)
        if numeric:
            return f"{numeric.group(2)}-{int(numeric.group(1)):02d}"
        named = re.search(
            r"\b(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?|aug(?:ust)?|sep(?:tember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\b.*?\b(20\d{2})\b",
            text,
            re.IGNORECASE,
        )
        if named:
            return f"{named.group(2)}-{month_numbers[named.group(1).lower()[:3]]}"
        year_first = re.search(
            r"\b(20\d{2})[. /_-]+(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)\b",
            text,
            re.IGNORECASE,
        )
        if year_first:
            return f"{year_first.group(1)}-{month_numbers[year_first.group(2).lower()]}"
    return None


def _representative_coverage_issues(
    packet: PeriodColumnPacket, catalog: PeriodCatalog
) -> list[PeriodCatalogIssue]:
    sampled_groups = _discovery_representatives(packet)
    assessment_by_id = {
        item.location_id: item for item in catalog.sheet_assessments
    }
    role_aware = bool(assessment_by_id)
    if role_aware:
        core_groups = [
            members
            for members in sampled_groups
            if (
                assessment_by_id.get(_packet_location_id(members[0])) is not None
                and assessment_by_id[_packet_location_id(members[0])].role
                == PeriodSheetRole.PRIMARY_CORE
            )
        ]
    else:
        core_groups = [
            members
            for members in sampled_groups
            if not _looks_like_supporting_schedule_sheet(members[0].sheet_name)
        ]
    if not core_groups and not role_aware:
        core_groups = sampled_groups
    issues = []
    for option in catalog.options:
        bindings_by_location = {
            location_id: binding
            for binding in option.bindings
            for location_id in binding.location_ids
        }
        unavailable = {
            item.location_id: item for item in option.unavailable_locations
        }
        for members in core_groups:
            sheet = members[0]
            location_id = _packet_location_id(sheet)
            disposition = unavailable.get(location_id)
            matches = _matching_period_candidates(sheet, option)
            if matches:
                continue
            binding = bindings_by_location.get(location_id)
            if binding is not None and any(
                candidate.column == binding.value_column
                and candidate.excel_column == binding.excel_column
                for candidate in matches
            ):
                continue
            if (
                not role_aware
                and disposition
                and _unavailable_is_noisy_schedule(disposition)
            ):
                continue
            suggestion = ""
            if len(matches) == 1:
                suggestion = (
                    f" Add a binding_patch for {location_id} ({sheet.sheet_name}, "
                    f"column {matches[0].excel_column}); it represents {len(members)} "
                    "structurally matching tab(s)."
                )
            issues.append(
                PeriodCatalogIssue(
                    severity=Severity.ERROR,
                    period_id=option.period_id,
                    location_id=location_id,
                    message=(
                        f"Period is not resolved on sampled core P&L sheet "
                        f"{sheet.sheet_name}. Bind it when supported or remove the "
                        "period when it is partial/summary-only. If this is actually "
                        "a Stats/KPI/supporting schedule, mark it unavailable with "
                        "concrete evidence."
                        f"{suggestion}"
                    ),
                )
            )
    return issues


def _primary_core_discovery_packet(
    packet: PeriodColumnPacket, catalog: PeriodCatalog
) -> PeriodColumnPacket:
    roles = {item.location_id: item.role for item in catalog.sheet_assessments}
    sheets = [
        members[0]
        for members in _discovery_representatives(packet)
        if roles.get(_packet_location_id(members[0]))
        == PeriodSheetRole.PRIMARY_CORE
    ]
    return packet.model_copy(update={"sheets": sheets})


def _matching_period_candidates(
    sheet: PeriodSheetPacket, option: PeriodOption
) -> list[PeriodColumnCandidate]:
    return [
        candidate
        for candidate in sheet.candidate_columns
        if not _candidate_looks_like_rejected_metric_column(candidate)
        and _coverage_scenario_matches(candidate, option)
        and _candidate_option_mismatch(candidate, option) is None
    ]


def _representative_sheet_assessment_issues(
    packet: PeriodColumnPacket, catalog: PeriodCatalog
) -> list[PeriodCatalogIssue]:
    """Require one evidenced semantic role for every sampled discovery sheet."""
    sampled = [members[0] for members in _discovery_representatives(packet)]
    sampled_by_id = {_packet_location_id(sheet): sheet for sheet in sampled}
    counts: dict[str, int] = defaultdict(int)
    assessments = {}
    for item in catalog.sheet_assessments:
        counts[item.location_id] += 1
        assessments[item.location_id] = item

    issues = []
    for location_id, sheet in sampled_by_id.items():
        item = assessments.get(location_id)
        if item is None:
            issues.append(
                PeriodCatalogIssue(
                    severity=Severity.ERROR,
                    location_id=location_id,
                    message=(
                        f"Missing sheet_assessment for sampled sheet {sheet.sheet_name}; "
                        "return a sheet_assessment_patch with its semantic role."
                    ),
                )
            )
        elif counts[location_id] != 1:
            issues.append(
                PeriodCatalogIssue(
                    severity=Severity.ERROR,
                    location_id=location_id,
                    message=(
                        f"Sampled sheet {sheet.sheet_name} must have exactly one "
                        "sheet assessment."
                    ),
                )
            )
        elif not item.reason.strip() or not item.evidence:
            issues.append(
                PeriodCatalogIssue(
                    severity=Severity.ERROR,
                    location_id=location_id,
                    message=(
                        f"Sheet assessment for {sheet.sheet_name} needs a reason and "
                        "exact sheet/header evidence."
                    ),
                )
            )
    for location_id in assessments.keys() - sampled_by_id.keys():
        issues.append(
            PeriodCatalogIssue(
                severity=Severity.ERROR,
                location_id=location_id,
                message="Sheet assessment references a location outside the sample.",
            )
        )

    primary = [
        sampled_by_id[location_id]
        for location_id, item in assessments.items()
        if location_id in sampled_by_id and item.role == PeriodSheetRole.PRIMARY_CORE
    ]
    if not primary:
        target = _packet_location_id(sampled[0]) if sampled else None
        issues.append(
            PeriodCatalogIssue(
                severity=Severity.ERROR,
                location_id=target,
                message="At least one sampled sheet must be classified primary_core.",
            )
        )
    elif len(sampled) > 1 and any(
        not _looks_like_primary_statement_sheet(sheet.sheet_name)
        for sheet in sampled
    ) and not any(
        not _looks_like_primary_statement_sheet(sheet.sheet_name)
        for sheet in primary
    ):
        target = next(
            _packet_location_id(sheet)
            for sheet in sampled
            if not _looks_like_primary_statement_sheet(sheet.sheet_name)
        )
        issues.append(
            PeriodCatalogIssue(
                severity=Severity.ERROR,
                location_id=target,
                message=(
                    "A multi-sheet discovery cohort cannot be summary-only; classify "
                    "at least one mapping-ready non-summary P&L sheet primary_core."
                ),
            )
        )
    return issues


def _unavailable_is_noisy_schedule(item: UnavailablePeriodLocation) -> bool:
    text = " ".join([item.reason, *item.evidence]).lower()
    return bool(
        item.evidence
        and any(
            term in text
            for term in (
                "stats",
                "statistics",
                "kpi",
                "non-dollar",
                "non dollar",
                "payroll schedule",
                "supporting schedule",
            )
        )
    )


def _looks_like_period_control_candidate(
    sheet: PeriodSheetPacket, candidate: PeriodColumnCandidate
) -> bool:
    """Reject sparse query/configuration columns that merely contain period text."""
    context = " ".join(candidate.header_context).lower()
    control_evidence = any(
        token in context
        for token in (
            "[date].",
            "[version].",
            "prior year gl period",
            "hierarchy rest listview",
            "hiearchy rest listview",
            "<<001/",
        )
    )
    if not control_evidence:
        return False
    maximum_numeric_count = max(
        (item.numeric_count for item in sheet.candidate_columns), default=0
    )
    return (
        candidate.numeric_count <= 5
        and maximum_numeric_count >= 20
        and candidate.numeric_count * 10 < maximum_numeric_count
    )


def _month_spread_total_mismatch(
    sheet: PeriodSheetPacket,
    candidate: PeriodColumnCandidate,
    option: PeriodOption,
) -> str | None:
    """Validate a bare Total using the Jan-Dec block immediately to its left."""
    if option.period_type != PeriodType.FULL_YEAR:
        return None
    total_coordinates = []
    for header in candidate.header_context:
        match = re.match(r"([A-Z]+)(\d+):\s*total\b", header, re.IGNORECASE)
        if match:
            total_coordinates.append((match.group(1).upper(), int(match.group(2))))
    if not total_coordinates:
        return None
    total_column_name, total_row = total_coordinates[-1]
    total_column = _excel_column_number(total_column_name)
    nearby = []
    for cell in sheet.header_cells:
        match = re.match(r"([A-Z]+)(\d+)$", cell.coordinate, re.IGNORECASE)
        if not match:
            continue
        column = _excel_column_number(match.group(1).upper())
        row = int(match.group(2))
        if total_column - 12 <= column < total_column and row in {
            total_row,
            total_row + 1,
        }:
            nearby.append(cell.value)
    text = " ".join(nearby).lower()
    months = {
        match.group(1)[:3]
        for match in re.finditer(
            r"\b(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|"
            r"jun(?:e)?|jul(?:y)?|aug(?:ust)?|sep(?:tember)?|oct(?:ober)?|"
            r"nov(?:ember)?|dec(?:ember)?)\b",
            text,
        )
    }
    if len(months) < 10:
        return None
    years = set(re.findall(r"\b20\d{2}\b", text))
    expected_year = option.start_period[:4] if option.start_period else None
    if expected_year and len(years) == 1 and expected_year not in years:
        return f"the preceding monthly block is for {next(iter(years))}"
    scenario = (
        PeriodScenario.BUDGET
        if "budget" in text
        else PeriodScenario.FORECAST
        if "forecast" in text
        else PeriodScenario.ACTUAL
        if "actual" in text
        else PeriodScenario.UNKNOWN
    )
    if scenario != PeriodScenario.UNKNOWN and scenario != option.scenario:
        return f"the preceding monthly block is {scenario.value}"
    return None


def _missing_catalog_period_issues(
    packet: PeriodColumnPacket,
    catalog: PeriodCatalog,
    *,
    allow_single: bool = False,
    require_all_sheets: bool = False,
) -> list[PeriodCatalogIssue]:
    """Find clear repeated/header-native periods omitted from the whole catalog."""
    grouped: dict[
        tuple[str, PeriodScenario, PeriodType],
        list[tuple[str, PeriodColumnCandidate]],
    ] = defaultdict(list)
    for sheet in packet.sheets:
        if _looks_like_supporting_schedule_sheet(sheet.sheet_name):
            continue
        location_id = _packet_location_id(sheet)
        for candidate in sheet.candidate_columns:
            if (
                _candidate_looks_like_rejected_metric_column(candidate)
                or _looks_like_period_control_candidate(sheet, candidate)
                or not _has_direct_period_header_evidence(candidate)
                or not _has_identified_period(candidate)
            ):
                continue
            label, scenario, period_type = _local_period_identity(candidate)
            if scenario == PeriodScenario.UNKNOWN:
                continue
            matched = any(
                (
                    _coverage_scenario_matches(candidate, option)
                    or (
                        option.scenario == PeriodScenario.PRIOR_ACTUAL
                        and scenario == PeriodScenario.ACTUAL
                    )
                )
                and _candidate_option_mismatch(candidate, option) is None
                for option in catalog.options
            )
            if not matched:
                grouped[(label, scenario, period_type)].append(
                    (location_id, candidate)
                )

    issues = []
    for (label, scenario, period_type), matches in grouped.items():
        location_count = len({location_id for location_id, _ in matches})
        if require_all_sheets and location_count < len(packet.sheets):
            continue
        primary_statement = any(
            _looks_like_primary_statement_sheet(
                next(
                    sheet.sheet_name
                    for sheet in packet.sheets
                    if _packet_location_id(sheet) == location_id
                )
            )
            and _nearest_explicit_year(candidate) is not None
            for location_id, candidate in matches
        )
        if location_count < 2 and not primary_statement and not allow_single:
            continue
        location_id, candidate = matches[0]
        issues.append(
            PeriodCatalogIssue(
                severity=Severity.ERROR,
                location_id=location_id,
                message=(
                    f"Catalog omitted the source-supported {label} "
                    f"({scenario.value}, {period_type.value}); column "
                    f"{candidate.excel_column} and {location_count - 1} other tab(s) "
                    "show the same period concept."
                ),
            )
        )
    return issues


def validate_period_column_selection(
    packet: PeriodColumnPacket, result: PeriodColumnSelectionMap
) -> PeriodColumnSelectionMap:
    issues: list[PeriodColumnSelectionIssue] = []
    if packet.sheets and not result.sheet_selections:
        issues.append(
            PeriodColumnSelectionIssue(
                severity=Severity.ERROR,
                message="No period columns were selected.",
            )
        )
    candidate_lookup = {
        (sheet.sheet_name, candidate.column): candidate
        for sheet in packet.sheets
        for candidate in sheet.candidate_columns
    }
    for selection in result.sheet_selections:
        if selection.sheet_name is None:
            issues.append(
                PeriodColumnSelectionIssue(
                    severity=Severity.WARNING,
                    message="Sheet-level selection is missing sheet_name.",
                )
            )
            continue
        candidate = candidate_lookup.get((selection.sheet_name, selection.value_column))
        if candidate is None:
            if _can_inherit_default_selection(packet, result, selection):
                issues.append(
                    PeriodColumnSelectionIssue(
                        severity=Severity.WARNING,
                        sheet_name=selection.sheet_name,
                        message=(
                            f"Selected column {selection.excel_column} was inherited from the "
                            "validated workbook default because this sheet has no local candidates."
                        ),
                    )
                )
                continue
            issues.append(
                PeriodColumnSelectionIssue(
                    severity=Severity.ERROR,
                    sheet_name=selection.sheet_name,
                    message=f"Selected column {selection.excel_column} is not in the candidate packet.",
                )
            )
            continue
        if _candidate_looks_like_rejected_metric_column(candidate):
            issues.append(
                PeriodColumnSelectionIssue(
                    severity=Severity.ERROR,
                    sheet_name=selection.sheet_name,
                    message=f"Selected column {selection.excel_column} looks like a percentage, variance, or KPI column.",
                )
            )
        if candidate.numeric_count == 0:
            issues.append(
                PeriodColumnSelectionIssue(
                    severity=Severity.ERROR,
                    sheet_name=selection.sheet_name,
                    message=f"Selected column {selection.excel_column} has no numeric values in the relevant rows.",
                )
            )
        column = selection.value_column
        if (selection.sheet_name, column) not in candidate_lookup:
            issues.append(
                PeriodColumnSelectionIssue(
                    severity=Severity.ERROR,
                    sheet_name=selection.sheet_name,
                    message=f"Selected column {column} is not in the candidate packet.",
                )
            )
            continue
    status = ValidationStatus.PASS
    if any(issue.severity == Severity.ERROR for issue in issues):
        status = ValidationStatus.FAIL
    elif issues:
        status = ValidationStatus.WARNING
    return result.model_copy(
        update={
            "validation": PeriodColumnSelectionValidation(status=status, issues=issues),
        }
    )


def _can_inherit_default_selection(
    packet: PeriodColumnPacket,
    result: PeriodColumnSelectionMap,
    selection: PeriodColumnSelection,
) -> bool:
    sheet = next(
        (item for item in packet.sheets if item.sheet_name == selection.sheet_name),
        None,
    )
    default = result.default_selection
    return bool(
        sheet is not None
        and not sheet.candidate_columns
        and default is not None
        and selection.value_column == default.value_column
    )


def _candidate_looks_like_ratio_values(
    candidate: PeriodColumnCandidate,
    sheet_candidates: list[PeriodColumnCandidate] | None = None,
) -> bool:
    """Reject amount bindings that are statistically percentage/ratio columns."""
    if candidate.numeric_count <= 0:
        return False
    if candidate.percentage_format_count * 2 >= candidate.numeric_count:
        return True
    if (
        candidate.numeric_count >= 5
        and candidate.nonzero_count >= 3
        and candidate.subunit_nonzero_count * 5 >= candidate.nonzero_count * 4
        and candidate.material_value_count == 0
    ):
        return True
    if not (
        candidate.numeric_count >= 5
        and candidate.nonzero_count >= 3
        and candidate.percentage_scale_count * 5 >= candidate.nonzero_count * 4
        and candidate.large_amount_count == 0
    ):
        return False
    identity = _local_period_identity(candidate)[:3]
    if identity[1:] == (PeriodScenario.UNKNOWN, PeriodType.UNKNOWN):
        return False
    return any(
        abs(other.column - candidate.column) == 1
        and _local_period_identity(other)[:3] == identity
        and other.large_amount_count >= 3
        for other in (sheet_candidates or [])
    )


def _unambiguous_sheet_scenario(
    sheet: PeriodSheetPacket | None,
) -> PeriodScenario | None:
    """Use a report-wide scenario only when the sheet declares exactly one."""
    if sheet is None:
        return None
    text = " ".join(
        [
            *(cell.value for cell in sheet.header_cells),
            *(cell.value for cell in sheet.spatial_header_cells),
        ]
    ).lower()
    scenarios = set()
    if "actual" in text:
        scenarios.add(PeriodScenario.ACTUAL)
    if "budget" in text:
        scenarios.add(PeriodScenario.BUDGET)
    if any(term in text for term in ("forecast", "outlook")):
        scenarios.add(PeriodScenario.FORECAST)
    if any(
        term in text
        for term in ("prior year", "last year", "previous year", "-1yr")
    ):
        scenarios.add(PeriodScenario.PRIOR_ACTUAL)
    return next(iter(scenarios)) if len(scenarios) == 1 else None


def _candidate_has_full_year_evidence(candidate: PeriodColumnCandidate) -> bool:
    context = " ".join(candidate.header_context).lower()
    normalized = re.sub(r"[^a-z0-9]+", " ", context).strip()
    return bool(
        re.search(r"\btotal\b", normalized)
        or re.search(r"\bfull year\b", normalized)
        or re.search(r"\bannual\b", normalized)
        or re.search(r"\bfy\s*\d{2,4}\b", normalized)
        or re.search(r"\b12\s*months?\b", normalized)
        or re.search(
            r"\bjan(?:uary)?\s+(?:through|thru|to)?\s*dec(?:ember)?\b",
            normalized,
        )
    )


def _sheet_has_full_year_evidence(
    candidates: list[PeriodColumnCandidate],
) -> bool:
    return any(_candidate_has_full_year_evidence(candidate) for candidate in candidates)


def _has_direct_period_header_evidence(candidate: PeriodColumnCandidate) -> bool:
    text = " ".join(_direct_header_values(candidate)).lower()
    text = re.sub(r"%,[^\s|]*", "", text)
    return bool(
        re.search(r"\b20\d{2}\b", text)
        or re.search(
            r"\b(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|"
            r"jun(?:e)?|jul(?:y)?|aug(?:ust)?|sep(?:tember)?|oct(?:ober)?|"
            r"nov(?:ember)?|dec(?:ember)?)\b",
            text,
        )
        or _contains_ytd(text)
        or _contains_mtd(text)
        or any(term in text for term in ("actual", "budget", "forecast"))
    )
