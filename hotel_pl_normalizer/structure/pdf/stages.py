"""Stateful routing/discovery and binding tools over direct PDF inspection."""

from __future__ import annotations

from typing import Any

from hotel_pl_normalizer.models.pdf import PdfDocumentRecord
from hotel_pl_normalizer.models.pdf_structure import (
    PdfBindings,
    PdfExploration,
    PdfPageRange,
    PdfPageRole,
    PdfPeriods,
    PdfRouting,
)
from hotel_pl_normalizer.providers.base import tool_parameter_schema

from .toolset import PdfInspectionToolset

MIN_FINANCIAL_PAGES_READ = 5


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
        must_read = min(MIN_FINANCIAL_PAGES_READ, len(financial))
        return {
            "ok": True,
            "accepted": True,
            "financial_page_ranges": [
                item.model_dump(mode="json")
                for item in _ranges_for_pages(financial)
            ],
            "must_read_at_least": must_read,
            "next_phase": (
                "Discover reporting periods from header lines on representative financial pages. "
                "Enumerate one period per amount anchor; Actual, Budget, Prior and Forecast are "
                "distinct. Exclude percentages and variances. Confirm the intersection across at "
                f"least {must_read} financial page(s), then call submit_periods."
            ),
        }
    def _submit_periods(self, arguments: dict[str, Any]) -> dict[str, Any]:
        if self.routing is None:
            return self._reject("Call submit_routing and receive its instructions first.")
        financial = self._financial_pages()
        required = min(MIN_FINANCIAL_PAGES_READ, len(financial))
        read_financial = self.period_read_pages & financial
        if len(read_financial) < required:
            return self._reject(
                f"Read period headers on at least {required} financial pages; read so far: "
                f"{sorted(read_financial)}."
            )
        try:
            periods = PdfPeriods.model_validate(arguments)
        except ValueError as exc:
            return self._reject(f"Periods did not match the schema: {exc}")
        ids = [period.period_id for period in periods.periods]
        if len(ids) != len(set(ids)):
            return self._reject("Every period_id must be unique.")
        if periods.recommended_period_id and periods.recommended_period_id not in set(ids):
            return self._reject("recommended_period_id must name one submitted period.")
        for period in periods.periods:
            claimed = _pages_in_ranges(period.pages_present)
            if not read_financial <= claimed:
                return self._reject(
                    f"{period.period_id} does not claim every financial page inspected in phase "
                    f"two. Missing: {sorted(read_financial - claimed)}. Drop it if not universal."
                )
        self.submission = PdfExploration(
            layout=self.routing.layout,
            layout_evidence=self.routing.layout_evidence,
            page_ranges=self.routing.page_ranges,
            periods=periods.periods,
            recommended_period_id=periods.recommended_period_id,
            notes=[*self.routing.notes, *periods.notes],
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
            if item.role in {
                PdfPageRole.FINANCIAL_STATEMENT,
                PdfPageRole.SUPPORTING_SCHEDULE,
            }
            for page in range(item.start_page, item.end_page + 1)
        }

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
        required = min(MIN_FINANCIAL_PAGES_READ, len(self.financial_pages))
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
