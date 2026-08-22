"""Bounded, read-only workbook tools for a tool-calling model.

Why these exist
---------------
Every stage today gets one pre-built packet and one shot. When the packet is
wrong -- a department boundary off by ten rows, an outlet block the builder did
not know to include -- the stage has no recourse. It fails, and the department is
dropped or falls back to a Summary total. That is what a tool-enabled pass fixes:
the model can go and look, and correct its own boundary instead of dying.

The set is deliberately five tools. `hotel_pl_agent` has 26, but 21 of those are
per-department validate/repair pairs that this codebase already owns as Python
validators. What is genuinely missing here is the ability to *read*.

Built over `WorkbookRecord`, not the file
-----------------------------------------
`hotel_pl_agent` opens the workbook again with openpyxl. Doing that here would be
wrong twice over: it would create a second source of truth that can disagree with
what the pipeline actually ingested, and openpyxl cannot read the legacy `.xls`
properties at all -- those come through xlrd. Reading the ingested record means
the model sees exactly the cells the rest of the pipeline sees.

Determinism
-----------
Every tool is a pure function of an immutable record, and results are ordered, so
the same call always returns the same bytes. That is what lets a whole tool
session be cached on its opening prompt.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any

from hotel_pl_normalizer.models.workbook import WorkbookRecord, WorkbookSheet

# Bounds exist so one tool call cannot undo the context savings that made the
# staged pipeline cheap in the first place.
MAX_ROWS_PER_READ = 160
MAX_MATCHES = 60
MAX_RANGES_PER_CALL = 12


class WorkbookToolError(ValueError):
    """A tool was called with arguments that do not describe anything real.

    Raised rather than returned so the caller can decide: a tool-calling loop
    hands the message back to the model to retry, while a direct caller sees a
    normal exception.
    """


def _cell_text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


@dataclass(frozen=True)
class WorkbookToolset:
    """One workbook, exposed to a model as five read-only tools."""

    record: WorkbookRecord

    # ------------------------------------------------------------------ helpers

    def _sheet(self, sheet_name: str) -> WorkbookSheet:
        for sheet in self.record.sheets:
            if sheet.sheet_name == sheet_name:
                return sheet
        available = ", ".join(sorted(s.sheet_name for s in self.record.sheets))
        raise WorkbookToolError(
            f"No sheet named {sheet_name!r}. Available sheets: {available}"
        )

    def _row_payload(self, sheet: WorkbookSheet, row_index: int) -> dict[str, Any] | None:
        for row in sheet.rows:
            if row.row_index != row_index:
                continue
            label = ""
            indent = None
            bold = False
            values: dict[str, Any] = {}
            for cell in row.cells:
                text = _cell_text(cell.raw_value)
                if _is_number(cell.raw_value):
                    values[cell.address] = cell.raw_value
                elif text and not label:
                    label = text
                    indent = cell.style.indent
                    bold = cell.style.bold
            if not label and not values:
                return None
            return {
                "row": row_index,
                "label": label,
                "indent": indent,
                "bold": bold,
                "values": values,
            }
        return None

    # -------------------------------------------------------------------- tools

    def inspect_workbook(self) -> dict[str, Any]:
        """Sheet inventory: enough to choose where to look, and nothing more."""
        return {
            "ok": True,
            "filename": self.record.source.original_filename,
            "sheets": [
                {
                    "sheet_name": sheet.sheet_name,
                    "visible": sheet.visible,
                    "max_row": sheet.max_row,
                    "max_column": sheet.max_column,
                }
                for sheet in self.record.sheets
            ],
        }

    def read_range(
        self, sheet_name: str, start_row: int, end_row: int
    ) -> dict[str, Any]:
        """Read a contiguous row band, label plus numeric cells per row."""
        sheet = self._sheet(sheet_name)
        if start_row < 1 or end_row < start_row:
            raise WorkbookToolError(
                f"Invalid range {start_row}-{end_row}: need 1 <= start_row <= end_row."
            )
        span = end_row - start_row + 1
        if span > MAX_ROWS_PER_READ:
            raise WorkbookToolError(
                f"Range of {span} rows exceeds the {MAX_ROWS_PER_READ}-row limit. "
                "Narrow it, or use find_rows to locate the part you need."
            )
        rows = [
            payload
            for index in range(start_row, end_row + 1)
            if (payload := self._row_payload(sheet, index)) is not None
        ]
        return {
            "ok": True,
            "sheet_name": sheet_name,
            "requested": {"start_row": start_row, "end_row": end_row},
            "rows": rows,
        }

    def find_rows(
        self, query: str, sheet_name: str | None = None, regex: bool = False
    ) -> dict[str, Any]:
        """Locate rows whose label matches, to find a boundary without guessing."""
        if not str(query).strip():
            raise WorkbookToolError("find_rows needs a non-empty query.")
        if regex:
            try:
                pattern = re.compile(query, re.IGNORECASE)
            except re.error as exc:
                raise WorkbookToolError(f"Invalid regex {query!r}: {exc}") from exc
            matches_text = lambda text: bool(pattern.search(text))  # noqa: E731
        else:
            needle = str(query).strip().lower()
            matches_text = lambda text: needle in text.lower()  # noqa: E731

        sheets = [self._sheet(sheet_name)] if sheet_name else list(self.record.sheets)
        hits: list[dict[str, Any]] = []
        for sheet in sheets:
            for row in sheet.rows:
                payload = self._row_payload(sheet, row.row_index)
                if payload is None or not payload["label"]:
                    continue
                if matches_text(payload["label"]):
                    hits.append({"sheet_name": sheet.sheet_name, **payload})
                    if len(hits) >= MAX_MATCHES:
                        return {
                            "ok": True,
                            "matches": hits,
                            "truncated": True,
                            "note": (
                                f"Stopped at {MAX_MATCHES} matches. Narrow the query "
                                "or pass sheet_name."
                            ),
                        }
        return {"ok": True, "matches": hits, "truncated": False}

    def read_nonzero_rows(
        self, sheet_name: str, start_row: int = 1, end_row: int | None = None
    ) -> dict[str, Any]:
        """Rows carrying at least one nonzero number.

        The usual reason a band looks empty is that it is mostly zero-filled, and
        skipping those rows is what makes a whole schedule fit in one call.
        """
        sheet = self._sheet(sheet_name)
        last = sheet.max_row if end_row is None else end_row
        rows = []
        for index in range(max(1, start_row), last + 1):
            payload = self._row_payload(sheet, index)
            if payload is None:
                continue
            if any(value not in (0, 0.0) for value in payload["values"].values()):
                rows.append(payload)
        truncated = len(rows) > MAX_ROWS_PER_READ
        return {
            "ok": True,
            "sheet_name": sheet_name,
            "rows": rows[:MAX_ROWS_PER_READ],
            "truncated": truncated,
        }

    def read_sparse_ranges(
        self, sheet_name: str, ranges: list[Any]
    ) -> dict[str, Any]:
        """Read several disjoint bands in one call.

        Cheaper than one wide read when the rows of interest are scattered, which
        is the normal shape of an OOD schedule.
        """
        sheet = self._sheet(sheet_name)
        if not ranges:
            raise WorkbookToolError("read_sparse_ranges needs at least one range.")
        if len(ranges) > MAX_RANGES_PER_CALL:
            raise WorkbookToolError(
                f"{len(ranges)} ranges exceeds the {MAX_RANGES_PER_CALL} limit."
            )
        out = []
        total = 0
        for item in ranges:
            if isinstance(item, dict):
                start, end = item.get("start_row"), item.get("end_row")
            elif isinstance(item, (list, tuple)) and len(item) == 2:
                start, end = item
            else:
                raise WorkbookToolError(
                    "Each range must be {'start_row': int, 'end_row': int}."
                )
            try:
                start, end = int(start), int(end)
            except (TypeError, ValueError) as exc:
                raise WorkbookToolError(f"Non-integer range {item!r}.") from exc
            if start < 1 or end < start:
                raise WorkbookToolError(f"Invalid range {start}-{end}.")
            total += end - start + 1
            if total > MAX_ROWS_PER_READ:
                raise WorkbookToolError(
                    f"Ranges total more than {MAX_ROWS_PER_READ} rows."
                )
            out.append(
                {
                    "start_row": start,
                    "end_row": end,
                    "rows": [
                        payload
                        for index in range(start, end + 1)
                        if (payload := self._row_payload(sheet, index)) is not None
                    ],
                }
            )
        return {"ok": True, "sheet_name": sheet_name, "ranges": out}

    # ------------------------------------------------------- model-facing wiring

    def dispatch(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Route one model-requested tool call to its implementation."""
        handlers = {
            "inspect_workbook": lambda a: self.inspect_workbook(),
            "read_range": lambda a: self.read_range(
                str(a["sheet_name"]), int(a["start_row"]), int(a["end_row"])
            ),
            "find_rows": lambda a: self.find_rows(
                str(a["query"]),
                sheet_name=(
                    str(a["sheet_name"]) if a.get("sheet_name") else None
                ),
                regex=bool(a.get("regex", False)),
            ),
            "read_nonzero_rows": lambda a: self.read_nonzero_rows(
                str(a["sheet_name"]),
                start_row=int(a.get("start_row", 1)),
                end_row=int(a["end_row"]) if a.get("end_row") is not None else None,
            ),
            "read_sparse_ranges": lambda a: self.read_sparse_ranges(
                str(a["sheet_name"]), list(a.get("ranges") or [])
            ),
        }
        handler = handlers.get(name)
        if handler is None:
            raise WorkbookToolError(
                f"Unknown tool {name!r}. Available: {sorted(handlers)}"
            )
        try:
            return handler(arguments)
        except KeyError as exc:
            raise WorkbookToolError(
                f"{name} is missing required argument {exc.args[0]!r}."
            ) from exc
        except (TypeError, ValueError) as exc:
            if isinstance(exc, WorkbookToolError):
                raise
            raise WorkbookToolError(f"{name} got unusable arguments: {exc}") from exc

    @staticmethod
    def declarations() -> list[dict[str, Any]]:
        """Gemini function declarations for the five tools."""
        sheet = {"type": "string", "description": "Exact sheet name."}
        return [
            {
                "name": "inspect_workbook",
                "description": (
                    "List every sheet with its visibility and used extent. Call this "
                    "first when you do not already know the sheet names."
                ),
                "parameters": {"type": "object", "properties": {}},
            },
            {
                "name": "read_range",
                "description": (
                    "Read a contiguous band of rows, returning each row's label and "
                    f"numeric cells. At most {MAX_ROWS_PER_READ} rows per call."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "sheet_name": sheet,
                        "start_row": {"type": "integer", "description": "1-based."},
                        "end_row": {"type": "integer", "description": "Inclusive."},
                    },
                    "required": ["sheet_name", "start_row", "end_row"],
                },
            },
            {
                "name": "find_rows",
                "description": (
                    "Find rows whose label matches a query. Use this to locate a "
                    "section boundary rather than guessing at row numbers."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "Substring, or a regex if regex=true.",
                        },
                        "sheet_name": {
                            "type": "string",
                            "description": "Optional; omit to search every sheet.",
                        },
                        "regex": {"type": "boolean"},
                    },
                    "required": ["query"],
                },
            },
            {
                "name": "read_nonzero_rows",
                "description": (
                    "Read only rows carrying at least one nonzero number. Usually "
                    "the cheapest way to see a whole schedule at once."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "sheet_name": sheet,
                        "start_row": {"type": "integer"},
                        "end_row": {"type": "integer"},
                    },
                    "required": ["sheet_name"],
                },
            },
            {
                "name": "read_sparse_ranges",
                "description": (
                    "Read several disjoint row bands in one call. Cheaper than one "
                    "wide read when the rows you need are scattered."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "sheet_name": sheet,
                        "ranges": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "start_row": {"type": "integer"},
                                    "end_row": {"type": "integer"},
                                },
                                "required": ["start_row", "end_row"],
                            },
                        },
                    },
                    "required": ["sheet_name", "ranges"],
                },
            },
        ]

    def signature(self) -> str:
        """Stable digest of the tool contract, for the response cache key.

        A tool session's result depends on which tools were offered, so the cache
        key has to cover them. The workbook itself is already covered, because its
        content hash is in the packet id inside the prompt.
        """
        payload = json.dumps(self.declarations(), sort_keys=True)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]
