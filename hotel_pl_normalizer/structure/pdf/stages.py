"""Stateful routing/discovery and binding tools over direct PDF inspection."""

from __future__ import annotations

from statistics import median
from typing import Any

from hotel_pl_normalizer.models.pdf import PdfDocumentRecord
from hotel_pl_normalizer.models.pdf_structure import (
    PdfBindings,
    PdfExploration,
    PdfPageRange,
    PdfPeriodAnchorBinding,
    PdfPeriods,
    PdfRouting,
    PdfUnavailablePeriod,
)
from hotel_pl_normalizer.models.period_selection import is_annual_summary_period
from hotel_pl_normalizer.providers.base import tool_parameter_schema
from hotel_pl_normalizer.structure.monthly_spread import (
    MONTHLY_SPREAD_THRESHOLD,
    explicit_month_years,
)

from .toolset import PdfInspectionToolset

MAX_DEPARTMENT_PAGES_TO_CONFIRM = 4
MAX_BINDING_SUBMISSIONS = 3

_HEADER_LINE_LIMIT = 25


def _explicit_header_months(document: PdfDocumentRecord, pages: set[int]) -> set[str]:
    """Return explicit month-year labels in the top header band of pages."""
    return explicit_month_years(
        line.text
        for page_number in pages
        for line in document.page(page_number).text_lines[:_HEADER_LINE_LIMIT]
    )


def _pages_in_ranges(ranges) -> set[int]:
    pages: set[int] = set()
    for item in ranges:
        pages.update(range(item.start_page, item.end_page + 1))
    return pages


def _ranges_for_pages(pages: set[int]) -> list[PdfPageRange]:
    if not pages:
        return []
    ordered = sorted(pages)
    ranges: list[PdfPageRange] = []
    start = previous = ordered[0]
    for page in ordered[1:]:
        if page != previous + 1:
            ranges.append(PdfPageRange(start_page=start, end_page=previous))
            start = page
        previous = page
    ranges.append(PdfPageRange(start_page=start, end_page=previous))
    return ranges


def _period_submission_schema() -> dict[str, Any]:
    """Tool input omits identity fields that Python can build exactly."""
    schema = tool_parameter_schema(PdfPeriods)
    period_items = (
        schema.get("properties", {})
        .get("periods", {})
        .get("items", {})
    )
    properties = period_items.get("properties", {})
    properties.pop("period_id", None)
    properties.pop("label", None)
    required = period_items.get("required")
    if isinstance(required, list):
        period_items["required"] = [
            item for item in required if item not in {"period_id", "label"}
        ]
    return schema


def _layout_binding_submission_schema() -> dict[str, Any]:
    """Compact model input; Python owns expansion to page-level bindings."""
    layout_binding = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "layout_id": {"type": "string"},
            "period_id": {"type": "string"},
            "right_edge": {"type": "number", "exclusiveMinimum": 0},
            "header_text": {"type": "string"},
            "evidence": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["layout_id", "period_id", "right_edge", "header_text"],
    }
    layout_unavailable = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "layout_id": {"type": "string"},
            "period_id": {"type": "string"},
            "reason": {"type": "string"},
        },
        "required": ["layout_id", "period_id", "reason"],
    }
    page_binding = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "start_page": {"type": "integer", "minimum": 1},
            "end_page": {"type": "integer", "minimum": 1},
            "period_id": {"type": "string"},
            "right_edge": {"type": "number", "exclusiveMinimum": 0},
            "header_text": {"type": "string"},
            "evidence": {"type": "array", "items": {"type": "string"}},
        },
        "required": [
            "start_page",
            "end_page",
            "period_id",
            "right_edge",
            "header_text",
        ],
    }
    page_unavailable = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "start_page": {"type": "integer", "minimum": 1},
            "end_page": {"type": "integer", "minimum": 1},
            "period_id": {"type": "string"},
            "reason": {"type": "string"},
        },
        "required": ["start_page", "end_page", "period_id", "reason"],
    }
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "layout_bindings": {"type": "array", "items": layout_binding},
            "layout_unavailable": {"type": "array", "items": layout_unavailable},
            "page_bindings": {"type": "array", "items": page_binding},
            "page_unavailable": {"type": "array", "items": page_unavailable},
            "notes": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["layout_bindings", "layout_unavailable"],
    }


def _without_period_identity(arguments: dict[str, Any]) -> dict[str, Any]:
    """Ignore model-authored IDs and labels; dates and scenario own identity."""
    normalized = dict(arguments)
    normalized["periods"] = []
    for item in arguments.get("periods") or []:
        if not isinstance(item, dict):
            normalized["periods"].append(item)
            continue
        period = dict(item)
        period.pop("period_id", None)
        period.pop("label", None)
        normalized["periods"].append(period)
    return normalized


def _period_ytd_boundary(document: PdfDocumentRecord, pages: set[int]):
    """Locate an explicit PTD/YTD split; return boundary and YTD side."""
    for page_number in sorted(pages):
        page = document.page(page_number)
        words = {word.word_id: word for word in page.words}
        for line in page.text_lines[:_HEADER_LINE_LIMIT]:
            tokens = [words[word_id] for word_id in line.word_ids]
            labels = [token.text.upper().replace("‐", "-") for token in tokens]
            ytd_indexes = [
                index
                for index, label in enumerate(labels)
                if label == "YTD"
                or (
                    label == "YEAR"
                    and index + 2 < len(labels)
                    and labels[index + 1] == "TO"
                    and labels[index + 2] == "DATE"
                )
            ]
            period_indexes = [
                index
                for index, label in enumerate(labels)
                if label == "PERIODIC"
                or (
                    label == "PERIOD"
                    and not (
                        index > 0 and labels[index - 1] in {"THE", "REPORTING"}
                    )
                )
            ]
            if not ytd_indexes or not period_indexes:
                continue
            period_x = median(
                (tokens[index].x0 + tokens[index].x1) / 2
                for index in period_indexes
            )
            ytd_x = median(
                (tokens[index].x0 + tokens[index].x1) / 2
                for index in ytd_indexes
            )
            if abs(period_x - ytd_x) < page.width * 0.1:
                continue
            return (period_x + ytd_x) / 2, ytd_x > period_x
    return None


def _amount_header_markers(document: PdfDocumentRecord, page_number: int):
    """Return explicit amount/ratio subcolumn markers from the header band."""
    page = document.page(page_number)
    words = {word.word_id: word for word in page.words}
    markers: list[tuple[float, str]] = []
    for line in page.text_lines[:_HEADER_LINE_LIMIT]:
        tokens = [words[word_id] for word_id in line.word_ids]
        labels = [token.text.upper().replace(" ", "") for token in tokens]
        has_amount = any(label in {"AMT", "AMOUNT"} for label in labels)
        has_ratio = any(
            label.startswith("%") or label in {"POR", "PAR"}
            for label in labels
        )
        if not has_amount or not has_ratio:
            continue
        markers.extend(
            (token.x1, "amount" if label in {"AMT", "AMOUNT"} else "ratio")
            for token, label in zip(tokens, labels, strict=True)
            if label in {"AMT", "AMOUNT", "POR", "PAR"} or label.startswith("%")
        )
    return markers


def _scenario_header_markers(document: PdfDocumentRecord, page_number: int):
    """Return explicit scenario labels positioned in the header band."""
    page = document.page(page_number)
    words = {word.word_id: word for word in page.words}
    markers: list[tuple[float, str]] = []
    for line in page.text_lines[:_HEADER_LINE_LIMIT]:
        tokens = [words[word_id] for word_id in line.word_ids]
        labels = [token.text.upper().strip(" :") for token in tokens]
        for index, (token, label) in enumerate(zip(tokens, labels, strict=True)):
            if label in {"ACTUAL", "ACTUALS"}:
                markers.append((token.x1, "actual"))
            elif label == "BUDGET":
                markers.append((token.x1, "budget"))
            elif label.startswith("FORECAST"):
                markers.append((token.x1, "forecast"))
            elif label == "YEAR" and index > 0 and labels[index - 1] == "LAST":
                markers.append((token.x1, "prior_actual"))
    return markers


class PdfExplorationToolset(PdfInspectionToolset):
    def __init__(self, document: PdfDocumentRecord) -> None:
        super().__init__(document)
        self.routing: PdfRouting | None = None
        self.submission: PdfExploration | None = None
        self.period_read_pages: set[int] = set()
        self.rejections: list[str] = []

    def declarations(self) -> list[dict[str, Any]]:
        return [
            *super().declarations(),
            {
                "name": "submit_routing",
                "description": (
                    "Phase one result. Route every PDF page exactly once using contiguous "
                    "page ranges. Returns the period-discovery instructions."
                ),
                "parameters": tool_parameter_schema(PdfRouting),
            },
            {
                "name": "submit_periods",
                "description": "Phase two result. Submit every distinct usable amount period; ends the session.",
                "parameters": _period_submission_schema(),
            },
        ]

    def dispatch(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if name == "submit_routing":
            return self._submit_routing(arguments)
        if name == "submit_periods":
            return self._submit_periods(arguments)
        result = super().dispatch(name, arguments)
        if (
            self.routing is not None
            and name in {"read_page_lines", "read_region", "numeric_anchors"}
            and result.get("ok")
            and arguments.get("page_number") is not None
        ):
            self.period_read_pages.add(int(arguments["page_number"]))
        return result

    def terminal_result(self, name: str, result: dict[str, Any]):
        if name == "submit_periods" and result.get("accepted"):
            return result.get("structure")
        return None

    def _submit_routing(self, arguments: dict[str, Any]) -> dict[str, Any]:
        try:
            routing = PdfRouting.model_validate(arguments)
        except ValueError as exc:
            return self._reject(f"Routing did not match the schema: {exc}")
        expected = set(range(1, len(self.document.pages) + 1))
        seen: list[int] = []
        for item in routing.page_ranges:
            if item.end_page < item.start_page:
                return self._reject("Every page range must have start_page <= end_page.")
            seen.extend(range(item.start_page, item.end_page + 1))
        if set(seen) != expected or len(seen) != len(expected):
            missing = sorted(expected - set(seen))
            duplicate_count = len(seen) - len(set(seen))
            return self._reject(
                f"Route every page exactly once. Missing pages: {missing[:20]}; "
                f"duplicate assignments: {duplicate_count}."
            )
        self.routing = routing
        self.period_read_pages.clear()
        financial = self._financial_pages()
        summaries = self._ranges_for_role("summary_p_and_l")
        departments = self._ranges_for_role("department_p_and_l")
        required_departments = min(MAX_DEPARTMENT_PAGES_TO_CONFIRM, len(departments))
        return {
            "ok": True,
            "accepted": True,
            "financial_page_ranges": [
                item.model_dump(mode="json")
                for item in _ranges_for_pages(financial)
            ],
            "summary_page_ranges": [
                item.model_dump(mode="json") for item in summaries
            ],
            "department_page_ranges": [
                item.model_dump(mode="json") for item in departments
            ],
            "required_department_reads": required_departments,
            "must_read_at_least": 1 + required_departments,
            "next_phase": (
                "Choose one controlling core summary statement from summary_page_ranges. "
                "Discover periods from that summary, then confirm its layout against "
                f"{required_departments} normal department page(s). Periods found only on "
                "auxiliary department, T12, monthly, trend, or supporting pages may confirm "
                "coverage but cannot add periods. If the controlling summary itself is a T12 "
                "or monthly spread, its displayed months are core periods: return every monthly "
                "amount column plus the displayed TTM/Total amount, never only the aggregate. "
                "Enumerate one period per amount anchor; Actual, Budget, Prior and Forecast are "
                "distinct. Exclude percentages and variances, then call submit_periods."
            ),
        }

    def _submit_periods(self, arguments: dict[str, Any]) -> dict[str, Any]:
        if self.routing is None:
            return self._reject("Call submit_routing and receive its instructions first.")
        try:
            periods = PdfPeriods.model_validate(_without_period_identity(arguments))
        except ValueError as exc:
            return self._reject(f"Periods did not match the schema: {exc}")
        ids = [period.period_id for period in periods.periods]
        if len(ids) != len(set(ids)):
            return self._reject("Every period_id must be unique.")
        anchor = set(
            range(
                periods.controlling_summary_pages.start_page,
                periods.controlling_summary_pages.end_page + 1,
            )
        )
        summary_ranges = self._ranges_for_role("summary_p_and_l")
        summary_candidates = [
            set(range(item.start_page, item.end_page + 1))
            for item in summary_ranges
        ]
        if not anchor or anchor not in summary_candidates:
            return self._reject(
                "controlling_summary_pages must exactly match one routed, included "
                "summary_p_and_l statement range."
            )
        if not anchor & self.period_read_pages:
            return self._reject(
                "Read the period header on at least one controlling summary page before "
                "submitting periods."
            )

        explicit_months = _explicit_header_months(self.document, anchor)
        if len(explicit_months) >= MONTHLY_SPREAD_THRESHOLD:
            submitted_months = {
                period.start_month
                for period in periods.periods
                if period.start_month == period.end_month
            }
            missing_months = sorted(explicit_months - submitted_months)
            if missing_months:
                return self._reject(
                    "The controlling summary has an explicit monthly spread, but the period "
                    "catalog omits displayed months. Return every monthly amount period and "
                    "keep any displayed TTM/Total as an additional aggregate. Missing month "
                    f"headers: {missing_months}."
                )

        department_ranges = self._ranges_for_role("department_p_and_l")
        required_departments = min(
            MAX_DEPARTMENT_PAGES_TO_CONFIRM, len(department_ranges)
        )
        read_department_ranges = [
            item
            for item in department_ranges
            if self.period_read_pages
            & set(range(item.start_page, item.end_page + 1))
        ]
        if len(read_department_ranges) < required_departments:
            return self._reject(
                "Read a period header from each of "
                f"{required_departments} normal department schedule range(s); "
                f"confirmed so far: {len(read_department_ranges)}."
            )

        corrected_periods = []
        corrected_pages: set[int] = set()
        for period in periods.periods:
            claimed = _pages_in_ranges(period.pages_present)
            observed = claimed & self.period_read_pages
            corrected_pages.update(claimed - observed)
            if not observed & anchor:
                return self._reject(
                    "Every selectable period must appear on the controlling summary. "
                    "Remove periods introduced only by auxiliary T12, monthly, trend, "
                    f"department, or supporting pages: {period.period_id}."
                )
            corrected_periods.append(
                period.model_copy(update={"pages_present": _ranges_for_pages(observed)})
            )

        notes = [*self.routing.notes, *periods.notes]
        if corrected_pages:
            notes.append(
                "pages_present was corrected to pages actually inspected during period "
                f"discovery; removed unobserved pages: {sorted(corrected_pages)}."
            )
        self.submission = PdfExploration(
            layout=self.routing.layout,
            layout_evidence=self.routing.layout_evidence,
            page_ranges=self.routing.page_ranges,
            controlling_summary_pages=periods.controlling_summary_pages,
            periods=corrected_periods,
            notes=notes,
        )
        return {
            "ok": True,
            "accepted": True,
            "structure": self.submission.model_dump(mode="json"),
        }
    def _financial_pages(self) -> set[int]:
        if self.routing is None:
            return set()
        return {
            page
            for item in self.routing.page_ranges
            if item.include_as_financial_evidence
            for page in range(item.start_page, item.end_page + 1)
        }

    def _ranges_for_role(self, role: str) -> list[PdfPageRange]:
        if self.routing is None:
            return []
        return [
            PdfPageRange(start_page=item.start_page, end_page=item.end_page)
            for item in self.routing.page_ranges
            if item.include_as_financial_evidence and item.role.value == role
        ]

    def _reject(self, message: str) -> dict[str, Any]:
        self.rejections.append(message)
        return {"ok": True, "accepted": False, "error": message}


class PdfBindingToolset(PdfInspectionToolset):
    def __init__(
        self,
        document: PdfDocumentRecord,
        exploration: PdfExploration,
        period_ids: list[str],
    ) -> None:
        super().__init__(document)
        self.exploration = exploration
        self.period_ids = list(dict.fromkeys(period_ids))
        self.financial_pages = set(exploration.financial_pages)
        self.financial_ranges = [
            set(range(item.start_page, item.end_page + 1))
            for item in exploration.page_ranges
            if item.include_as_financial_evidence
        ]
        self.department_pages = {
            page
            for item in exploration.page_ranges
            if item.include_as_financial_evidence
            and item.role.value == "department_p_and_l"
            for page in range(item.start_page, item.end_page + 1)
        }
        self.block_boundaries: dict[int, tuple[float, bool]] = {}
        for page_number in self.financial_pages:
            boundary = _period_ytd_boundary(document, {page_number})
            if boundary is not None:
                self.block_boundaries[page_number] = boundary
        self.periods = {
            period.period_id: period
            for period in exploration.periods
            if period.period_id in self.period_ids
        }
        self.read_pages: set[int] = set()
        self.submission: PdfBindings | None = None
        self.pending_submission: PdfBindings | None = None
        self.layout_groups: list[dict[str, Any]] | None = None
        self.binding_submission_count = 0
        self.rejections: list[str] = []

    def declarations(self) -> list[dict[str, Any]]:
        return [
            *super().declarations(),
            {
                "name": "list_financial_layouts",
                "description": (
                    "Group routed financial pages by repeated numeric geometry. "
                    "Returns representative pages, exact page ranges, and common "
                    "non-percentage anchors. This is a reading aid, not a period decision."
                ),
                "parameters": {"type": "object", "properties": {}},
            },
            {
                "name": "submit_layout_bindings",
                "description": (
                    "Bind each selected period once per deterministic layout group. Python "
                    "expands the chosen layout anchor to exact page ranges. Optional page "
                    "outcomes override exceptional pages. Ends the session."
                ),
                "parameters": _layout_binding_submission_schema(),
            },
        ]

    def dispatch(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if name == "list_financial_layouts":
            return self._list_financial_layouts()
        if name == "submit_layout_bindings":
            return self._submit_layout_bindings(arguments)
        # Kept as an internal compatibility path for focused validator tests;
        # it is deliberately absent from declarations so live model sessions
        # cannot emit the old page-expanded payload.
        if name == "submit_bindings":
            return self._submit_bindings(arguments)
        result = super().dispatch(name, arguments)
        if (
            name in {"read_page_lines", "read_region", "numeric_anchors"}
            and result.get("ok")
            and arguments.get("page_number") is not None
        ):
            self.read_pages.add(int(arguments["page_number"]))
        return result

    def terminal_result(self, name: str, result: dict[str, Any]):
        if name == "submit_layout_bindings" and result.get("accepted"):
            return result.get("structure")
        return None

    def _list_financial_layouts(self) -> dict[str, Any]:
        """Compact a long PDF into geometry groups without inferring semantics."""
        if self.layout_groups is None:
            self.layout_groups = self._build_financial_layouts()
        return {
            "ok": True,
            "financial_page_count": len(self.financial_pages),
            "layout_groups": self.layout_groups,
            "instruction": (
                "Read the full period header on representative pages, then provide one "
                "outcome per layout_id and selected period. Python expands each chosen "
                "anchor using its exact page_ranges."
            ),
        }

    def _build_financial_layouts(self) -> list[dict[str, Any]]:
        page_anchors: dict[int, list[float]] = {}
        page_anchor_hints: dict[int, dict[float, str]] = {}
        page_scenario_anchors: dict[int, dict[str, list[float]]] = {}
        for page_number in sorted(self.financial_pages):
            page = self.document.page(page_number)
            anchors = (
                super().numeric_anchors(page_number).get("anchors", [])
                if page.text_lines
                else []
            )
            candidates = [
                float(item["right_edge"])
                for item in anchors
                if int(item.get("count") or 0)
                > int(item.get("percent_tokens") or 0)
            ]
            markers = _amount_header_markers(self.document, page_number)
            scenario_markers = _scenario_header_markers(
                self.document, page_number
            )
            page_anchors[page_number] = []
            page_anchor_hints[page_number] = {}
            page_scenario_anchors[page_number] = {}
            for anchor in candidates:
                _distance, kind = min(
                    (
                        (abs(anchor - marker_edge), marker_kind)
                        for marker_edge, marker_kind in markers
                    ),
                    default=(float("inf"), "unknown"),
                )
                if markers and kind != "amount":
                    continue
                page_anchors[page_number].append(anchor)
                if markers and scenario_markers:
                    scenario_distance, scenario = min(
                        (
                            (abs(anchor - scenario_edge), scenario_name)
                            for scenario_edge, scenario_name in scenario_markers
                        ),
                        default=(float("inf"), ""),
                    )
                    if scenario_distance <= max(30.0, page.width * 0.05):
                        page_anchor_hints[page_number][anchor] = scenario
                        page_scenario_anchors[page_number].setdefault(
                            scenario, []
                        ).append(anchor)

        groups: list[dict[str, Any]] = []
        for page_number in sorted(self.financial_pages):
            page = self.document.page(page_number)
            anchors = page_anchors[page_number]
            matched = None
            for group in groups:
                tolerance = max(4.0, page.width * 0.006)
                representative = group["representative_anchors"]
                smaller, larger = sorted(
                    (anchors, representative), key=len
                )
                if not smaller:
                    compatible = not larger
                else:
                    hits = sum(
                        any(abs(anchor - other) <= tolerance for other in larger)
                        for anchor in smaller
                    )
                    compatible = hits / len(smaller) >= 0.75
                same_page = (
                    abs(page.width - group["width"]) <= 1
                    and abs(page.height - group["height"]) <= 1
                    and page.rotation == group["rotation"]
                )
                representative_scenarios = group["representative_scenario_anchors"]
                current_scenarios = page_scenario_anchors[page_number]
                if representative_scenarios or current_scenarios:
                    semantic_compatible = set(representative_scenarios) == set(
                        current_scenarios
                    ) and all(
                        len(representative_scenarios[scenario])
                        == len(current_scenarios[scenario])
                        and all(
                            abs(left - right) <= tolerance
                            for left, right in zip(
                                sorted(representative_scenarios[scenario]),
                                sorted(current_scenarios[scenario]),
                                strict=True,
                            )
                        )
                        for scenario in representative_scenarios
                    )
                else:
                    semantic_compatible = True
                if compatible and semantic_compatible and same_page:
                    matched = group
                    break
            if matched is None:
                matched = {
                    "width": page.width,
                    "height": page.height,
                    "rotation": page.rotation,
                    "pages": [],
                    "anchors_by_page": {},
                    "representative_anchors": anchors,
                    "representative_scenario_anchors": page_scenario_anchors[
                        page_number
                    ],
                }
                groups.append(matched)
            matched["pages"].append(page_number)
            matched["anchors_by_page"][page_number] = anchors
            if len(anchors) > len(matched["representative_anchors"]):
                matched["representative_anchors"] = anchors
                matched["representative_scenario_anchors"] = (
                    page_scenario_anchors[page_number]
                )

        payloads: list[dict[str, Any]] = []
        for index, group in enumerate(groups, start=1):
            pages = set(group["pages"])
            representative_pages = sorted(
                pages,
                key=lambda number: (-len(page_anchors[number]), number),
            )[:3]
            anchor_clusters: list[list[tuple[int, float]]] = []
            tolerance = max(4.0, float(group["width"]) * 0.006)
            for page_number in sorted(pages):
                for anchor in page_anchors[page_number]:
                    choices = [
                        (
                            abs(anchor - median(value for _, value in cluster)),
                            cluster,
                        )
                        for cluster in anchor_clusters
                    ]
                    distance, cluster = min(
                        choices,
                        key=lambda item: item[0],
                        default=(float("inf"), None),
                    )
                    if cluster is not None and distance <= tolerance:
                        cluster.append((page_number, anchor))
                    else:
                        anchor_clusters.append([(page_number, anchor)])
            common = []
            for cluster in anchor_clusters:
                anchor_pages = {page for page, _ in cluster}
                pages_present = len(anchor_pages)
                scenario_hints = sorted(
                    {
                        hint
                        for page, anchor in cluster
                        for hint in [page_anchor_hints[page].get(anchor)]
                        if hint
                    }
                )
                common.append(
                    {
                        "right_edge": round(
                            float(median(value for _, value in cluster)), 3
                        ),
                        "pages_present": pages_present,
                        "page_ranges": [
                            item.model_dump(mode="json")
                            for item in _ranges_for_pages(anchor_pages)
                        ],
                        "scenario_hints": scenario_hints,
                    }
                )
            common.sort(key=lambda item: item["right_edge"])
            preview_page = self.document.page(representative_pages[0])
            payloads.append(
                {
                    "layout_id": f"layout_{index}",
                    "page_count": len(pages),
                    "page_ranges": [
                        item.model_dump(mode="json")
                        for item in _ranges_for_pages(pages)
                    ],
                    "representative_pages": representative_pages,
                    "common_non_percent_anchors": common,
                    "header_preview": [
                        line.text for line in preview_page.text_lines[:8] if line.text
                    ],
                }
            )
        return payloads

    def _submit_layout_bindings(self, arguments: dict[str, Any]) -> dict[str, Any]:
        """Expand compact layout choices into the existing page-level contract."""
        self.binding_submission_count += 1
        if self.binding_submission_count > MAX_BINDING_SUBMISSIONS:
            raise RuntimeError(
                "PDF layout binding exceeded one initial submission and two repairs."
            )
        if self.layout_groups is None:
            self.layout_groups = self._build_financial_layouts()
        layouts = {item["layout_id"]: item for item in self.layout_groups}
        layout_bindings = arguments.get("layout_bindings") or []
        layout_unavailable = arguments.get("layout_unavailable") or []
        if not isinstance(layout_bindings, list) or not isinstance(
            layout_unavailable, list
        ):
            return self._reject(
                "layout_bindings and layout_unavailable must both be arrays."
            )

        chosen = set(self.period_ids)
        outcomes: dict[tuple[str, str], str] = {}
        for kind, items in (
            ("binding", layout_bindings),
            ("unavailable", layout_unavailable),
        ):
            for item in items:
                if not isinstance(item, dict):
                    return self._reject(f"Each layout {kind} must be an object.")
                layout_id = str(item.get("layout_id") or "")
                period_id = str(item.get("period_id") or "")
                if layout_id not in layouts:
                    return self._reject(f"Unknown layout_id {layout_id!r}.")
                if period_id not in chosen:
                    return self._reject(f"Unknown period_id {period_id!r}.")
                pair = (layout_id, period_id)
                if pair in outcomes:
                    return self._reject(
                        f"{layout_id} and {period_id} have more than one layout outcome."
                    )
                outcomes[pair] = kind

        expected = {
            (layout_id, period_id)
            for layout_id in layouts
            for period_id in self.period_ids
        }
        missing = sorted(expected - set(outcomes))
        if missing:
            preview = ", ".join(f"{layout}/{period}" for layout, period in missing[:12])
            return self._reject(
                "Every layout and selected period requires one compact outcome. Missing: "
                + preview
            )

        expanded_bindings: list[PdfPeriodAnchorBinding] = []
        expanded_unavailable: list[PdfUnavailablePeriod] = []
        notes = list(arguments.get("notes") or [])
        for item in layout_bindings:
            layout = layouts[item["layout_id"]]
            try:
                proposed_edge = float(item["right_edge"])
                header_text = str(item["header_text"])
            except (KeyError, TypeError, ValueError):
                return self._reject(
                    f"{item['layout_id']} binding requires numeric right_edge and header_text."
                )
            evidence = [str(value) for value in item.get("evidence") or []]
            layout_pages = _pages_in_ranges(
                [PdfPageRange.model_validate(value) for value in layout["page_ranges"]]
            )
            tolerance = max(
                4.0,
                max(self.document.page(page).width for page in layout_pages) * 0.006,
            )
            candidates = [
                anchor
                for anchor in layout["common_non_percent_anchors"]
                if abs(float(anchor["right_edge"]) - proposed_edge) <= tolerance
            ]
            if not candidates:
                return self._reject(
                    f"{item['layout_id']} has no listed numeric anchor near "
                    f"right_edge {proposed_edge}."
                )
            anchor = min(
                candidates,
                key=lambda value: abs(float(value["right_edge"]) - proposed_edge),
            )
            canonical_edge = float(anchor["right_edge"])
            scenario_hints = set(anchor.get("scenario_hints") or [])
            period = self.periods.get(item["period_id"])
            expected_scenario = period.scenario.value if period is not None else None
            recognized = scenario_hints & {
                "actual",
                "prior_actual",
                "budget",
                "forecast",
            }
            allowed_hints = {
                "actual": {"actual", "prior_actual"},
                "budget": {"budget"},
                "forecast": {"forecast"},
            }.get(expected_scenario, set())
            if recognized and not recognized & allowed_hints:
                return self._reject(
                    f"{item['layout_id']} anchor {canonical_edge} has explicit scenario "
                    f"hint(s) {sorted(recognized)}, incompatible with "
                    f"{item['period_id']}."
                )
            anchor_pages = _pages_in_ranges(
                [
                    PdfPageRange.model_validate(value)
                    for value in anchor["page_ranges"]
                ]
            )
            expanded_bindings.extend(
                PdfPeriodAnchorBinding(
                    period_id=item["period_id"],
                    start_page=page_range.start_page,
                    end_page=page_range.end_page,
                    right_edge=canonical_edge,
                    header_text=header_text,
                    evidence=evidence,
                )
                for page_range in _ranges_for_pages(anchor_pages)
            )
            missing_anchor_pages = layout_pages - anchor_pages
            expanded_unavailable.extend(
                PdfUnavailablePeriod(
                    period_id=item["period_id"],
                    start_page=page_range.start_page,
                    end_page=page_range.end_page,
                    reason=(
                        f"Layout {item['layout_id']} uses amount anchor "
                        f"{canonical_edge}, which is not displayed on this routed statement."
                    ),
                )
                for page_range in self._statement_ranges(missing_anchor_pages)
            )
            if missing_anchor_pages:
                notes.append(
                    f"{item['layout_id']} {item['period_id']}: selected anchor "
                    f"{canonical_edge} absent on pages {sorted(missing_anchor_pages)}."
                )

        for item in layout_unavailable:
            layout = layouts[item["layout_id"]]
            pages = _pages_in_ranges(
                [PdfPageRange.model_validate(value) for value in layout["page_ranges"]]
            )
            expanded_unavailable.extend(
                PdfUnavailablePeriod(
                    period_id=item["period_id"],
                    start_page=page_range.start_page,
                    end_page=page_range.end_page,
                    reason=str(item["reason"]),
                )
                for page_range in self._statement_ranges(pages)
            )

        try:
            page_outcomes = PdfBindings.model_validate(
                {
                    "bindings": arguments.get("page_bindings") or [],
                    "unavailable": arguments.get("page_unavailable") or [],
                    "notes": [],
                }
            )
        except ValueError as exc:
            return self._reject(f"Page overrides did not match the schema: {exc}")
        replacement_pages = {period_id: set() for period_id in self.period_ids}
        for item in [*page_outcomes.bindings, *page_outcomes.unavailable]:
            if item.period_id not in replacement_pages:
                return self._reject(f"Unknown period_id {item.period_id!r}.")
            replacement_pages[item.period_id].update(
                range(item.start_page, item.end_page + 1)
            )

        def retain_unreplaced(items):
            retained = []
            for item in items:
                pages = set(range(item.start_page, item.end_page + 1))
                pages -= replacement_pages[item.period_id]
                retained.extend(
                    item.model_copy(
                        update={
                            "start_page": page_range.start_page,
                            "end_page": page_range.end_page,
                        }
                    )
                    for page_range in _ranges_for_pages(pages)
                )
            return retained

        expanded = PdfBindings(
            bindings=[
                *retain_unreplaced(expanded_bindings),
                *page_outcomes.bindings,
            ],
            unavailable=[
                *retain_unreplaced(expanded_unavailable),
                *page_outcomes.unavailable,
            ],
            notes=notes,
        )
        return self._submit_bindings(expanded.model_dump(mode="json"))

    def _statement_ranges(self, pages: set[int]) -> list[PdfPageRange]:
        """Split unavailable pages at routed statement boundaries."""
        return [
            page_range
            for statement_pages in self.financial_ranges
            for page_range in _ranges_for_pages(pages & statement_pages)
        ]

    def _submit_bindings(self, arguments: dict[str, Any]) -> dict[str, Any]:
        required = min(5, len(self.financial_pages))
        if len(self.read_pages & self.financial_pages) < required:
            return self._reject(
                f"Read headers and numeric anchors on at least {required} financial pages first."
            )
        try:
            submission = PdfBindings.model_validate(arguments)
        except ValueError as exc:
            return self._reject(f"Bindings did not match the schema: {exc}")
        chosen = set(self.period_ids)
        normalized_bindings: list[PdfPeriodAnchorBinding] = []
        split_notes: list[str] = []
        for item in submission.bindings:
            if item.period_id not in chosen:
                return self._reject(f"Unknown period_id {item.period_id!r}.")
            pages = set(range(item.start_page, item.end_page + 1))
            if item.end_page < item.start_page or not pages <= self.financial_pages:
                return self._reject(
                    f"{item.period_id} range {item.start_page}-{item.end_page} is not wholly "
                    "within routed financial pages."
                )
            period = self.periods.get(item.period_id)
            if period is not None:
                expects_ytd = is_annual_summary_period(
                    period.start_month, period.end_month
                )
                for page_number in pages:
                    boundary = self.block_boundaries.get(page_number)
                    if boundary is None:
                        continue
                    split, ytd_on_right = boundary
                    anchor_is_ytd = (
                        item.right_edge > split
                        if ytd_on_right
                        else item.right_edge < split
                    )
                    if anchor_is_ytd != expects_ytd:
                        expected = "YEAR TO DATE/YTD" if expects_ytd else "PERIODIC/PTD"
                        return self._reject(
                            f"{item.period_id} anchor {item.right_edge} on page "
                            f"{page_number} is in the wrong header block. This period's "
                            f"inclusive dates require the {expected} side of the explicit "
                            "dual-block header."
                        )
            matched_pages: set[int] = set()
            for page_number in pages:
                page = self.document.page(page_number)
                if item.right_edge > page.width:
                    return self._reject(
                        f"right_edge {item.right_edge} is outside page {page_number}."
                    )
                tolerance = max(4.0, page.width * 0.006)
                if any(
                    word.numeric_value is not None
                    and not word.is_percent
                    and abs(word.x1 - item.right_edge) <= tolerance
                    for word in page.words
                ):
                    matched_pages.add(page_number)
            normalized_bindings.extend(
                item.model_copy(
                    update={
                        "start_page": page_range.start_page,
                        "end_page": page_range.end_page,
                    }
                )
                for page_range in _ranges_for_pages(matched_pages)
            )
            unmatched = pages - matched_pages
            if unmatched:
                split_notes.append(
                    f"Split {item.period_id} anchor {item.right_edge}; pages without "
                    f"that displayed amount anchor remain unresolved: {sorted(unmatched)}."
                )
        submission = submission.model_copy(
            update={
                "bindings": normalized_bindings,
                "notes": [*submission.notes, *split_notes],
            }
        )
        if self.pending_submission is not None:
            replacement_pages = {period_id: set() for period_id in self.period_ids}
            for item in [*submission.bindings, *submission.unavailable]:
                replacement_pages[item.period_id].update(
                    range(item.start_page, item.end_page + 1)
                )

            def retained_ranges(items):
                retained = []
                for item in items:
                    pages = set(range(item.start_page, item.end_page + 1))
                    pages -= replacement_pages[item.period_id]
                    retained.extend(
                        item.model_copy(
                            update={
                                "start_page": page_range.start_page,
                                "end_page": page_range.end_page,
                            }
                        )
                        for page_range in _ranges_for_pages(pages)
                    )
                return retained

            submission = submission.model_copy(
                update={
                    "bindings": [
                        *retained_ranges(self.pending_submission.bindings),
                        *submission.bindings,
                    ],
                    "unavailable": [
                        *retained_ranges(self.pending_submission.unavailable),
                        *submission.unavailable,
                    ],
                    "notes": [
                        *self.pending_submission.notes,
                        *submission.notes,
                    ],
                }
            )
        coverage = {period_id: set() for period_id in self.period_ids}
        for item in [*submission.bindings, *submission.unavailable]:
            if item.period_id not in chosen:
                return self._reject(f"Unknown period_id {item.period_id!r}.")
            pages = set(range(item.start_page, item.end_page + 1))
            if item.end_page < item.start_page or not pages <= self.financial_pages:
                return self._reject(
                    f"{item.period_id} range {item.start_page}-{item.end_page} is not wholly "
                    "within routed financial pages."
                )
            if coverage[item.period_id] & pages:
                return self._reject(
                    f"{item.period_id} assigns at least one financial page more than once."
                )
            if not hasattr(item, "right_edge") and not any(
                pages <= routed_range for routed_range in self.financial_ranges
            ):
                return self._reject(
                    f"{item.period_id} unavailable range {item.start_page}-{item.end_page} "
                    "crosses routed statement boundaries. Availability is a per-statement "
                    "claim; split it to the routed financial page ranges."
                )
            coverage[item.period_id].update(pages)
        for period_id, covered in coverage.items():
            missing = self.financial_pages - covered
            if missing:
                self.pending_submission = submission
                return self._reject(
                    f"{period_id} has neither a binding nor unavailable reason for pages: "
                    f"{sorted(missing)[:30]}. Valid normalized outcomes were retained; "
                    "submit only bindings or unavailable reasons for missing pages."
                )
            summary = self.exploration.controlling_summary_pages
            summary_pages = (
                set(range(summary.start_page, summary.end_page + 1))
                if summary is not None
                else set()
            )
            bound_pages = {
                page
                for item in submission.bindings
                if item.period_id == period_id
                for page in range(item.start_page, item.end_page + 1)
            }
            if summary_pages and not bound_pages & summary_pages:
                self.pending_submission = submission
                return self._reject(
                    f"{period_id} was discovered on controlling summary pages "
                    f"{sorted(summary_pages)}, so it must have a usable binding on at "
                    "least one of those pages. Do not mark it unavailable everywhere."
                )
            if self.department_pages and not bound_pages & self.department_pages:
                self.pending_submission = submission
                return self._reject(
                    f"{period_id} has no usable binding on any routed department P&L "
                    "page, even though period discovery used normal department schedules "
                    "to confirm the core period layout. Bind a representative department "
                    "range and split only genuinely unavailable schedules."
                )
        self.submission = submission
        self.pending_submission = None
        return {
            "ok": True,
            "accepted": True,
            "structure": submission.model_dump(mode="json"),
        }

    def _reject(self, message: str) -> dict[str, Any]:
        self.rejections.append(message)
        return {
            "ok": True,
            "accepted": False,
            "error": message,
            "instruction": (
                "Correct the compact layout choices and call submit_layout_bindings "
                "again. At most two repair submissions are allowed."
            ),
        }
