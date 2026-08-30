"""Stateful routing/discovery and binding tools over direct PDF inspection."""

from __future__ import annotations

from typing import Any

from hotel_pl_normalizer.models.pdf import PdfDocumentRecord
from hotel_pl_normalizer.models.pdf_structure import (
    PdfBindings,
    PdfExploration,
    PdfPageRange,
    PdfPeriods,
    PdfRouting,
)
from hotel_pl_normalizer.providers.base import tool_parameter_schema
from hotel_pl_normalizer.structure.monthly_spread import (
    MONTHLY_SPREAD_THRESHOLD,
    explicit_month_years,
)

from .toolset import PdfInspectionToolset

MAX_DEPARTMENT_PAGES_TO_CONFIRM = 4

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
                "parameters": tool_parameter_schema(PdfPeriods),
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
            periods = PdfPeriods.model_validate(arguments)
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
        self.read_pages: set[int] = set()
        self.submission: PdfBindings | None = None
        self.rejections: list[str] = []

    def declarations(self) -> list[dict[str, Any]]:
        return [
            *super().declarations(),
            {
                "name": "submit_bindings",
                "description": (
                    "Bind each selected period to numeric right-edge anchors over contiguous "
                    "financial page ranges, or mark a range unavailable. Ends the session."
                ),
                "parameters": tool_parameter_schema(PdfBindings),
            },
        ]

    def dispatch(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
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
        if name == "submit_bindings" and result.get("accepted"):
            return result.get("structure")
        return None

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
            coverage[item.period_id].update(pages)
            if hasattr(item, "right_edge"):
                unmatched_pages: list[int] = []
                for page_number in pages:
                    page = self.document.page(page_number)
                    if item.right_edge > page.width:
                        return self._reject(
                            f"right_edge {item.right_edge} is outside page {page_number}."
                        )
                    tolerance = max(4.0, page.width * 0.006)
                    if not any(
                        word.numeric_value is not None
                        and not word.is_percent
                        and abs(word.x1 - item.right_edge) <= tolerance
                        for word in page.words
                    ):
                        unmatched_pages.append(page_number)
                if unmatched_pages:
                    return self._reject(
                        f"{item.period_id} right_edge {item.right_edge} has no displayed, "
                        "non-percentage numeric token on pages: "
                        f"{unmatched_pages[:30]}. Split the range or inspect numeric_anchors again."
                    )
        for period_id, covered in coverage.items():
            missing = self.financial_pages - covered
            if missing:
                return self._reject(
                    f"{period_id} has neither a binding nor unavailable reason for pages: "
                    f"{sorted(missing)[:30]}."
                )
        self.submission = submission
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
                "Correct the issue and call submit_bindings again with the complete "
                "replacement bindings and unavailable lists. This is not a patch."
            ),
        }
