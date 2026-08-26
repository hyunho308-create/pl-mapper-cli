"""Bounded tools that let a model inspect a PDF without an Excel conversion."""

from __future__ import annotations

import hashlib
import json
import re
from statistics import median
from typing import Any

from hotel_pl_normalizer.models.pdf import (
    PdfDocumentRecord,
    PdfPage,
    PdfTextLine,
    PdfWord,
)

MAX_LINES_PER_READ = 60
MAX_REGION_WORDS = 800
MAX_SEARCH_HITS = 50
MAX_COLUMN_SAMPLES = 4


class PdfToolError(ValueError):
    """An actionable error safe to return to a tool-using model."""


class PdfInspectionToolset:
    """Read-only PDF tools over positioned source objects.

    These tools expose page geometry and displayed text. They deliberately do
    not create worksheets, cells, merged ranges, or inferred table columns.
    """

    cacheable = False

    def __init__(self, document: PdfDocumentRecord) -> None:
        self.document = document
        self._words = {
            word.word_id: word
            for page in document.pages
            for word in page.words
        }

    def inspect_document(self) -> dict[str, Any]:
        pages_with_text = sum(bool(page.words) for page in self.document.pages)
        return {
            "ok": True,
            "filename": self.document.source.original_filename,
            "document_id": self.document.document_id,
            "page_count": len(self.document.pages),
            "pages_with_text": pages_with_text,
            "extractable_word_count": sum(len(page.words) for page in self.document.pages),
            "numeric_token_count": sum(
                word.numeric_value is not None
                for page in self.document.pages
                for word in page.words
            ),
            "decorative_word_count": sum(
                word.decorative for page in self.document.pages for word in page.words
            ),
            "warnings": [warning.message for warning in self.document.warnings],
            "coordinate_system": (
                "PDF points from the top-left: x increases rightward and y increases downward."
            ),
        }

    def list_pages(self) -> dict[str, Any]:
        return {
            "ok": True,
            "pages": [
                {
                    "page_number": page.page_number,
                    "width": page.width,
                    "height": page.height,
                    "rotation": page.rotation,
                    "lines": len(page.text_lines),
                    "words": len(page.words),
                    "numeric_tokens": sum(
                        word.numeric_value is not None for word in page.words
                    ),
                    "drawn_rules": len(page.rules),
                    "decorative_words": sum(word.decorative for word in page.words),
                    "preview": [
                        line.text for line in page.text_lines[:4] if line.text
                    ],
                }
                for page in self.document.pages
            ],
        }

    def read_page_lines(
        self,
        page_number: int,
        start_line: int = 1,
        end_line: int | None = None,
    ) -> dict[str, Any]:
        page = self._page(page_number)
        if start_line < 1:
            raise PdfToolError("start_line must be at least 1.")
        if end_line is None:
            end_line = start_line + MAX_LINES_PER_READ - 1
        if end_line < start_line:
            raise PdfToolError("start_line must be less than or equal to end_line.")
        if end_line - start_line + 1 > MAX_LINES_PER_READ:
            raise PdfToolError(
                f"One read is limited to {MAX_LINES_PER_READ} lines; request a smaller range."
            )
        selected = [
            line
            for line in page.text_lines
            if start_line <= line.line_number <= end_line
        ]
        word_count = sum(len(line.word_ids) for line in selected)
        if word_count > MAX_REGION_WORDS:
            raise PdfToolError(
                f"Line range contains {word_count} words, above the {MAX_REGION_WORDS}-word "
                "limit. Request fewer lines."
            )
        return {
            "ok": True,
            "page_number": page_number,
            "page_width": page.width,
            "page_height": page.height,
            "lines": [self._line_payload(line) for line in selected],
        }

    def read_region(
        self,
        page_number: int,
        x0: float,
        top: float,
        x1: float,
        bottom: float,
    ) -> dict[str, Any]:
        page = self._page(page_number)
        if not (0 <= x0 < x1 <= page.width and 0 <= top < bottom <= page.height):
            raise PdfToolError(
                f"Region must satisfy 0 <= x0 < x1 <= {page.width} and "
                f"0 <= top < bottom <= {page.height}."
            )
        selected = [
            word
            for word in page.words
            if x0 <= (word.x0 + word.x1) / 2 <= x1
            and top <= (word.top + word.bottom) / 2 <= bottom
        ]
        if len(selected) > MAX_REGION_WORDS:
            raise PdfToolError(
                f"Region contains {len(selected)} words, above the {MAX_REGION_WORDS}-word "
                "limit. Request a smaller region."
            )
        return {
            "ok": True,
            "page_number": page_number,
            "region": {"x0": x0, "top": top, "x1": x1, "bottom": bottom},
            "words": [self._word_payload(word) for word in selected],
        }

    def find_text(
        self,
        query: str,
        page_number: int | None = None,
        *,
        regex: bool = False,
    ) -> dict[str, Any]:
        if not query.strip():
            raise PdfToolError("query must not be blank.")
        pages = [self._page(page_number)] if page_number is not None else self.document.pages
        try:
            pattern = re.compile(query, re.IGNORECASE) if regex else None
        except re.error as exc:
            raise PdfToolError(f"Invalid regex: {exc}") from exc
        needle = query.casefold()
        hits: list[dict[str, Any]] = []
        for page in pages:
            for line in page.text_lines:
                matched = bool(pattern.search(line.text)) if pattern else needle in line.text.casefold()
                if not matched:
                    continue
                hits.append(
                    {
                        "page_number": page.page_number,
                        "line": line.line_number,
                        "at": line.line_id,
                        "top": line.top,
                        "text": line.text,
                    }
                )
                if len(hits) >= MAX_SEARCH_HITS:
                    return {"ok": True, "query": query, "truncated": True, "hits": hits}
        return {"ok": True, "query": query, "truncated": False, "hits": hits}

    def numeric_anchors(
        self,
        page_number: int,
        start_line: int = 1,
        end_line: int | None = None,
        tolerance: float | None = None,
    ) -> dict[str, Any]:
        """Describe repeated right edges of numeric tokens without naming columns."""
        page = self._page(page_number)
        if end_line is None:
            end_line = len(page.text_lines)
        if start_line < 1 or end_line < start_line:
            raise PdfToolError("Use a valid inclusive line range starting at 1.")
        tolerance = float(tolerance) if tolerance is not None else max(4.0, page.width * 0.006)
        if not 0.5 <= tolerance <= 30:
            raise PdfToolError("tolerance must be between 0.5 and 30 PDF points.")

        line_by_word = {
            word_id: line.line_number
            for line in page.text_lines
            if start_line <= line.line_number <= end_line
            for word_id in line.word_ids
        }
        figures = [
            word
            for word in page.words
            if word.word_id in line_by_word and word.numeric_value is not None
        ]
        clusters: list[list[PdfWord]] = []
        for word in sorted(figures, key=lambda item: item.x1):
            choices = [
                (abs(word.x1 - median(item.x1 for item in cluster)), index)
                for index, cluster in enumerate(clusters)
            ]
            distance, index = min(choices, default=(float("inf"), -1))
            if distance <= tolerance:
                clusters[index].append(word)
            else:
                clusters.append([word])

        payloads = []
        for cluster in sorted(clusters, key=lambda items: median(item.x1 for item in items)):
            anchor = round(float(median(item.x1 for item in cluster)), 3)
            samples = sorted(cluster, key=lambda item: (line_by_word[item.word_id], item.x0))[
                :MAX_COLUMN_SAMPLES
            ]
            payloads.append(
                {
                    "right_edge": anchor,
                    "count": len(cluster),
                    "nonzero": sum(bool(item.numeric_value) for item in cluster),
                    "percent_tokens": sum(item.is_percent for item in cluster),
                    "samples": [
                        {
                            "line": line_by_word[item.word_id],
                            "at": item.word_id,
                            "text": item.text,
                            "value": item.numeric_value,
                        }
                        for item in samples
                    ],
                }
            )
        return {
            "ok": True,
            "page_number": page_number,
            "start_line": start_line,
            "end_line": end_line,
            "alignment_tolerance": round(tolerance, 3),
            "numeric_tokens": len(figures),
            "anchors": payloads,
            "note": (
                "Anchors are repeated numeric right edges, not inferred spreadsheet columns."
            ),
        }

    def dispatch(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        methods = {
            "inspect_document": self.inspect_document,
            "list_pages": self.list_pages,
            "read_page_lines": self.read_page_lines,
            "read_region": self.read_region,
            "find_text": self.find_text,
            "numeric_anchors": self.numeric_anchors,
        }
        method = methods.get(name)
        if method is None:
            return {
                "ok": False,
                "error": f"Unknown tool {name!r}. Available tools: {', '.join(methods)}.",
                "instruction": "Call one of the declared read-only PDF tools.",
            }
        try:
            return method(**arguments)
        except PdfToolError as exc:
            return {
                "ok": False,
                "error": str(exc),
                "instruction": "Correct the arguments and call the tool again.",
            }
        except TypeError as exc:
            return {
                "ok": False,
                "error": f"Invalid arguments for {name}: {exc}",
                "instruction": "Use the declared argument schema and call again.",
            }

    @staticmethod
    def declarations() -> list[dict[str, Any]]:
        page_number = {"type": "integer", "minimum": 1}
        line_number = {"type": "integer", "minimum": 1}
        coordinate = {"type": "number", "minimum": 0}
        return [
            {
                "name": "inspect_document",
                "description": "PDF metadata, extractable-text coverage, and ingestion warnings. Start here.",
                "parameters": {"type": "object", "properties": {}},
            },
            {
                "name": "list_pages",
                "description": "Every page with dimensions, object counts, and a short text preview.",
                "parameters": {"type": "object", "properties": {}},
            },
            {
                "name": "read_page_lines",
                "description": (
                    f"Read up to {MAX_LINES_PER_READ} visual text lines with positioned source tokens."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "page_number": page_number,
                        "start_line": line_number,
                        "end_line": line_number,
                    },
                    "required": ["page_number"],
                },
            },
            {
                "name": "read_region",
                "description": "Read source words whose centers fall inside one page-space rectangle.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "page_number": page_number,
                        "x0": coordinate,
                        "top": coordinate,
                        "x1": coordinate,
                        "bottom": coordinate,
                    },
                    "required": ["page_number", "x0", "top", "x1", "bottom"],
                },
            },
            {
                "name": "find_text",
                "description": "Find literal text or a regex in reconstructed visual lines across the PDF.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string"},
                        "page_number": page_number,
                        "regex": {"type": "boolean"},
                    },
                    "required": ["query"],
                },
            },
            {
                "name": "numeric_anchors",
                "description": (
                    "Cluster displayed numeric tokens by repeated right edge over a line range. "
                    "This describes alignment but does not infer spreadsheet columns."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "page_number": page_number,
                        "start_line": line_number,
                        "end_line": line_number,
                        "tolerance": {"type": "number", "minimum": 0.5, "maximum": 30},
                    },
                    "required": ["page_number"],
                },
            },
        ]

    def signature(self) -> str:
        payload = {
            "document": self.document.document_id,
            "tools": self.declarations(),
        }
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:16]

    def _page(self, page_number: int) -> PdfPage:
        try:
            return self.document.page(int(page_number))
        except (IndexError, TypeError, ValueError) as exc:
            raise PdfToolError(str(exc)) from exc

    def _line_payload(self, line: PdfTextLine) -> dict[str, Any]:
        return {
            "line": line.line_number,
            "at": line.line_id,
            "top": line.top,
            "text": line.text,
            "tokens": [self._word_payload(self._words[word_id]) for word_id in line.word_ids],
        }

    @staticmethod
    def _word_payload(word: PdfWord) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "at": word.word_id,
            "text": word.text,
            "x0": word.x0,
            "x1": word.x1,
        }
        if word.numeric_value is not None:
            payload["number"] = word.numeric_value
        if word.is_percent:
            payload["percent"] = True
        if word.bold:
            payload["bold"] = True
        if word.decorative:
            payload["decorative"] = True
        return payload
