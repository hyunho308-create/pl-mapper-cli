"""Shared period signals: what a header, a column or a sheet looks like.

These are the predicates the other period modules agree on -- whether a header
names a period, whether a column is really a percentage or per-occupied-room
metric, whether a sheet is a primary statement or a supporting schedule. They are
here because packet building, prompt rendering, validation and repair all have to
answer the same questions the same way; a second opinion about what "YTD" looks
like would show up as two stages disagreeing about the same workbook.
"""

from __future__ import annotations

import re

from hotel_pl_normalizer.models.period_selection import (
    PeriodColumnCandidate,
    PeriodColumnPacket,
    PeriodOption,
    PeriodScenario,
    PeriodSheetPacket,
    PeriodType,
    humanize_period_label,
)


def _candidate_option_mismatch(
    candidate: PeriodColumnCandidate, option: PeriodOption
) -> str | None:
    """Explain a strong header conflict; weak/opaque headers remain model-owned."""
    context = " ".join(candidate.header_context).lower()
    if not context.strip():
        return None
    _, candidate_scenario, candidate_type = _local_period_identity(candidate)
    if (
        candidate_scenario != PeriodScenario.UNKNOWN
        and option.scenario != PeriodScenario.UNKNOWN
        and candidate_scenario != option.scenario
        and not (
            option.scenario == PeriodScenario.PRIOR_ACTUAL
            and candidate_scenario == PeriodScenario.ACTUAL
        )
    ):
        return f"header scenario is {candidate_scenario.value}"
    compatible_period_types = {
        candidate_type,
        option.period_type,
    } == {PeriodType.MONTH, PeriodType.CURRENT_PERIOD}
    if (
        candidate_type != PeriodType.UNKNOWN
        and option.period_type != PeriodType.UNKNOWN
        and candidate_type != option.period_type
        and not compatible_period_types
    ):
        return f"header period type is {candidate_type.value}"
    expected_period = option.start_period
    if expected_period and re.fullmatch(r"20\d{2}-\d{2}", expected_period):
        year, month = expected_period.split("-")
        month_names = {
            "01": ("jan", "january"),
            "02": ("feb", "february"),
            "03": ("mar", "march"),
            "04": ("apr", "april"),
            "05": ("may",),
            "06": ("jun", "june"),
            "07": ("jul", "july"),
            "08": ("aug", "august"),
            "09": ("sep", "september"),
            "10": ("oct", "october"),
            "11": ("nov", "november"),
            "12": ("dec", "december"),
        }[month]
        nearest_year = _nearest_explicit_year(candidate)
        header_years = set(re.findall(r"\b20\d{2}\b", context))
        if nearest_year and nearest_year != year:
            return f"header identifies {nearest_year}, not year {year}"
        if not nearest_year and header_years and year not in header_years:
            return f"header identifies {sorted(header_years)}, not year {year}"
        if option.period_type == PeriodType.MONTH:
            all_month_names = (
                "jan", "january", "feb", "february", "mar", "march",
                "apr", "april", "may", "jun", "june", "jul", "july",
                "aug", "august", "sep", "september", "oct", "october",
                "nov", "november", "dec", "december",
            )
            explicit_month = _nearest_explicit_month(candidate)
            expected_month_names = {name[:3] for name in month_names}
            if explicit_month and explicit_month[:3] not in expected_month_names:
                return f"header identifies a different month than {expected_period}"
            header_has_month = any(
                re.search(rf"\b{re.escape(name)}\b", context)
                for name in all_month_names
            )
            expected_month_present = any(
                re.search(rf"\b{re.escape(name)}\b", context)
                for name in month_names
            ) or f"{year}{month}" in context
            if header_has_month and not expected_month_present:
                return f"header identifies a different month than {expected_period}"
    return None


def _period_representative_groups(
    packet: PeriodColumnPacket,
) -> list[list[PeriodSheetPacket]]:
    """Group structurally equivalent tabs for header sampling, never for binding."""
    grouped: dict[tuple, list[PeriodSheetPacket]] = {}
    for sheet in packet.sheets:
        top_merged_geometry = tuple(
            merged.range.upper()
            for merged in sheet.merged_headers
            if int(re.search(r"\d+", merged.range).group()) <= 30
        )
        signature = (
            _looks_like_primary_statement_sheet(sheet.sheet_name),
            _looks_like_supporting_schedule_sheet(sheet.sheet_name),
            tuple(candidate.column for candidate in sheet.candidate_columns),
            top_merged_geometry,
        )
        grouped.setdefault(signature, []).append(sheet)
    return list(grouped.values())


def _discovery_representatives(
    packet: PeriodColumnPacket, *, limit: int = 5
) -> list[list[PeriodSheetPacket]]:
    """Choose a small, diverse sample, preferring core dollar P&L layouts."""
    if packet.packet_id.endswith(":period_discovery") and all(
        not sheet.candidate_columns for sheet in packet.sheets
    ):
        return [[sheet] for sheet in packet.sheets[:limit]]
    groups = _period_representative_groups(packet)
    groups.sort(
        key=lambda members: (
            _looks_like_primary_statement_sheet(members[0].sheet_name),
            not _looks_like_supporting_schedule_sheet(members[0].sheet_name),
            len(members),
            len(members[0].candidate_columns),
        ),
        reverse=True,
    )
    return groups[:limit]


def _coverage_scenario_matches(
    candidate: PeriodColumnCandidate, option: PeriodOption
) -> bool:
    scenario = _local_period_identity(candidate)[1]
    if scenario == option.scenario:
        return True
    if option.scenario == PeriodScenario.ACTUAL and scenario == PeriodScenario.UNKNOWN:
        return True
    expected = option.end_period or option.start_period
    expected_year = expected[:4] if expected and re.match(r"20\d{2}", expected) else None
    return bool(
        option.scenario == PeriodScenario.PRIOR_ACTUAL
        and scenario == PeriodScenario.ACTUAL
        and expected_year
        and _nearest_explicit_year(candidate) == expected_year
    )


def _excel_column_number(column: str) -> int:
    number = 0
    for character in column:
        number = number * 26 + ord(character) - ord("A") + 1
    return number


def _looks_like_primary_statement_sheet(sheet_name: str) -> bool:
    normalized = re.sub(r"[^a-z0-9]+", " ", sheet_name.lower()).strip()
    return any(
        term in normalized
        for term in (
            "summary",
            "operating statement",
            "total hotel",
            "profit loss",
        )
    )


def _looks_like_period_header_text(text: str) -> bool:
    lowered = re.sub(r"\s+", " ", text).strip().lower()
    return bool(
        _contains_ytd(lowered)
        or _contains_mtd(lowered)
        or "trailing 12" in lowered
        or "ttm" in lowered
        or re.search(
            r"\b(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|"
            r"jun(?:e)?|jul(?:y)?|aug(?:ust)?|sep(?:tember)?|oct(?:ober)?|"
            r"nov(?:ember)?|dec(?:ember)?)\b(?:\s+|[-/.])*20\d{2}\b",
            lowered,
        )
    )


def _looks_like_rejected_metric_column(context: str) -> bool:
    # Opaque report tokens such as ``%,C`` describe query output, not the
    # column's financial meaning.
    context = re.sub(r"%,[^\s|]*", "", context)
    if (
        "variance" in context
        or re.search(r"\bvar\b", context)
        or re.search(r"\bvs\.?\s+(?:actual|budget|forecast|prior)", context)
        or "rev / cost" in context
        or re.search(r"\bpor\b", context)
        or re.search(r"\brpor\b", context)
        or "margin" in context
        or re.search(r"%\s+of\s+(?:income|revenue|sales|total)", context)
    ):
        return True
    if "%" not in context:
        return False
    return not (
        "actual" in context
        and (
            "ytd" in context
            or "year to date" in context
            or "year-to-date" in context
            or "mtd" in context
            or "period" in context
        )
    )


def _candidate_looks_like_rejected_metric_column(
    candidate: PeriodColumnCandidate,
) -> bool:
    """Ignore stale metric controls when a later row explicitly names a period."""
    entries: list[tuple[int | None, str, str]] = []
    explicit_rows = []
    for header in candidate.header_context:
        match = re.match(r"^[A-Z]+(\d+):\s*(.*)$", header, re.IGNORECASE)
        row = int(match.group(1)) if match else None
        value = match.group(2) if match else header
        entries.append((row, value, header))
        if row is not None and (
            _looks_like_period_header_text(value)
            or re.fullmatch(
                r"\s*(?:actual|budget|forecast|prior actual|last year)\s*",
                value,
                re.IGNORECASE,
            )
        ):
            explicit_rows.append(row)
    if explicit_rows:
        last_explicit_row = max(explicit_rows)
        retained = []
        for row, value, header in entries:
            normalized = re.sub(r"\s+", " ", value).strip().lower()
            if (
                row is not None
                and row < last_explicit_row
                and normalized in {"%", "% or por", "por"}
            ):
                continue
            retained.append(header)
        context = " ".join(retained).lower()
    else:
        context = " ".join(candidate.header_context).lower()
    return _looks_like_rejected_metric_column(context)


def _packet_location_id(sheet: PeriodSheetPacket) -> str:
    return sheet.location_id or f"{sheet.sheet_name}:{sheet.department or 'unknown'}"


def _looks_like_supporting_schedule_sheet(sheet_name: str) -> bool:
    normalized = re.sub(r"[^a-z0-9]+", " ", sheet_name.lower()).strip()
    return any(
        re.search(rf"\b{term}\b", normalized)
        for term in ("stats", "statistics", "kpi", "payroll")
    )


def _local_period_identity(
    candidate: PeriodColumnCandidate,
) -> tuple[str, PeriodScenario, PeriodType, float]:
    context = " ".join(candidate.header_context)
    normalized = re.sub(r"\s+", " ", context).strip()
    lowered = normalized.lower()
    year = _nearest_explicit_year(candidate)
    if year is None:
        year_match = re.search(r"\b(20\d{2})\b", lowered)
        year = year_match.group(1) if year_match else None

    attached_context = " ".join(_attached_header_values(candidate)).lower()
    scenario_context = (
        attached_context
        if any(
            term in attached_context
            for term in ("actual", "budget", "forecast", "last year", "prior year")
        )
        else lowered
    )
    if "last year" in scenario_context or "prior year" in scenario_context:
        scenario = PeriodScenario.PRIOR_ACTUAL
        scenario_label = "Prior Actual"
    elif "budget" in scenario_context:
        scenario = PeriodScenario.BUDGET
        scenario_label = "Budget"
    elif "forecast" in scenario_context:
        scenario = PeriodScenario.FORECAST
        scenario_label = "Forecast"
    elif "actual" in scenario_context or "actual_con" in scenario_context:
        scenario = PeriodScenario.ACTUAL
        scenario_label = "Actual"
    else:
        scenario = PeriodScenario.UNKNOWN
        scenario_label = "Unknown"

    month_pattern = (
        r"\b(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|"
        r"jul(?:y)?|aug(?:ust)?|sep(?:tember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\b"
    )
    explicit_month_name = _nearest_explicit_month(candidate)
    month_match = re.search(month_pattern, explicit_month_name or lowered)
    direct_context = " ".join(_direct_header_values(candidate)).lower()
    trailing = "trailing 12" in lowered or "ttm" in lowered
    if trailing and explicit_month_name:
        period_type = PeriodType.MONTH
        period_label = explicit_month_name.title()[:3]
    elif trailing:
        period_type = PeriodType.TTM
        period_label = "TTM"
    elif _contains_ytd(attached_context):
        period_type = PeriodType.YTD
        period_label = "YTD"
    elif explicit_month_name:
        # A workbook title such as "Trailing 12" may share a column with an
        # explicit month header. The closest period label is authoritative.
        period_type = PeriodType.MONTH
        period_label = explicit_month_name.title()[:3]
    elif (
        _contains_mtd(attached_context)
        or re.search(r"\bperiod\b", attached_context)
    ):
        period_type = PeriodType.CURRENT_PERIOD
        period_label = "Current Period"
    elif _contains_ytd(direct_context):
        period_type = PeriodType.YTD
        period_label = "YTD"
    elif _contains_ytd(lowered):
        period_type = PeriodType.YTD
        period_label = "YTD"
    elif "total" in lowered or re.search(
        r"january\s*[-\u2013\u2014]\s*december", lowered
    ):
        period_type = PeriodType.FULL_YEAR
        period_label = "Full Year"
    elif year and scenario != PeriodScenario.UNKNOWN:
        period_type = PeriodType.FULL_YEAR
        period_label = "Full Year"
    elif month_match:
        period_type = PeriodType.MONTH
        period_label = month_match.group(1).title()[:3]
    elif (
        _contains_mtd(direct_context)
        or _contains_mtd(lowered)
        or re.search(r"\bperiod\b", direct_context)
    ):
        period_type = PeriodType.CURRENT_PERIOD
        period_label = "Current Period"
    else:
        period_type = PeriodType.UNKNOWN
        period_label = candidate.excel_column

    parts = [part for part in (year, period_label, scenario_label) if part]
    return humanize_period_label(" ".join(parts)), scenario, period_type


def _contains_ytd(text: str) -> bool:
    return bool(
        "ytd" in text
        or "year to date" in text
        or "year-to-date" in text
        or re.search(r"\by\s*[-. ]\s*t\s*[-. ]\s*d\b", text)
    )


def _contains_mtd(text: str) -> bool:
    return bool(
        "mtd" in text
        or re.search(r"\bptd\b", text)
        or "month to date" in text
        or "month-to-date" in text
        or "period to date" in text
        or "period-to-date" in text
        or "current period" in text
        or "periodic" in text
        or "m-t-d" in text
        or re.search(r"\bp\s*[-. ]\s*t\s*[-. ]\s*d\b", text)
    )


def _direct_header_values(candidate: PeriodColumnCandidate) -> list[str]:
    return [
        header.split(":", 1)[1].strip() if ":" in header else header
        for header in candidate.header_context
        if " merged:" not in header.lower()
    ]


def _attached_header_values(candidate: PeriodColumnCandidate) -> list[str]:
    """Headers attached to this column, excluding explicitly nearby context."""
    return [
        header.rsplit(": ", 1)[-1].strip()
        for header in candidate.header_context
        if " nearby:" not in header.lower()
    ]


def _nearest_explicit_month(candidate: PeriodColumnCandidate) -> str | None:
    month_pattern = (
        r"\b(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|"
        r"jul(?:y)?|aug(?:ust)?|sep(?:tember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\b"
    )
    attached = _attached_header_values(candidate)
    if _contains_ytd(" ".join(attached).lower()):
        return None
    for header in reversed(attached):
        lowered = header.lower()
        matches = list(re.finditer(month_pattern, lowered))
        if (
            not _contains_ytd(lowered)
            and not re.search(r"january\s*[-\u2013\u2014]\s*december", lowered)
            and len(matches) == 1
        ):
            return matches[0].group(1).lower()
    for header in reversed(_direct_header_values(candidate)):
        lowered = header.lower()
        matches = list(re.finditer(month_pattern, lowered))
        if (
            not _contains_ytd(lowered)
            and not re.search(r"january\s*[-\u2013\u2014]\s*december", lowered)
            and len(matches) == 1
        ):
            return matches[0].group(1).lower()
    return None


def _nearest_explicit_year(candidate: PeriodColumnCandidate) -> str | None:
    direct = _direct_header_values(candidate)
    for header in reversed(direct or candidate.header_context):
        years = re.findall(r"\b(20\d{2})\b", header)
        if len(set(years)) == 1:
            return years[0]
        fiscal_years = re.findall(
            r"\bfy\s*[-']?\s*(\d{2})\b", header, re.IGNORECASE
        )
        if len(set(fiscal_years)) == 1:
            return f"20{fiscal_years[0]}"
    return None
