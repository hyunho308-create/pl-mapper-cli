"""Reader and submission tools for one period-column binding session."""

from __future__ import annotations

from typing import Any

from hotel_pl_normalizer.models.binding import UnavailablePeriod, WorkbookBindings
from hotel_pl_normalizer.models.workbook import WorkbookRecord
from hotel_pl_normalizer.providers.base import tool_parameter_schema

from .checks import check_bindings
from .column_stats import column_stats

MAX_ROWS_PER_READ = 60
MAX_FIND_HITS = 40
MAX_PREVIEW_LINES = 6

# A row cap alone does not bound a read: hotel P&Ls get wide, and 57 rows of an
# 85-column sheet came back as 125 KB -- about 31,000 tokens. That lands in the
# conversation and is re-sent on every later turn, so one such call cost WAPC
# most of its 1.47 M input tokens. Cells are what the payload is actually made
# of, so cells are what is capped; the reader is told it was cut and can ask for
# a narrower range.
MAX_CELLS_PER_READ = 1_200

# `read_headers` reads the top of many sheets at once. Bounded on both axes
# because the payload is the product of the two: twenty sheets by twenty rows is
# already a large tool result, and the point is to make full coverage affordable
# rather than to move the whole workbook into the prompt.
MAX_SHEETS_PER_HEADER_READ = 20
MAX_HEADER_ROWS = 30
DEFAULT_HEADER_ROWS = 15


class PeriodBindingToolset:
    """Reader tools plus one period-binding submission."""

    # Tool results are reads of one specific record, and the prompt names the
    # workbook, so a cached session would be safe -- but the periods vary per
    # run and the prompt would have to carry them all. Not worth the subtlety.
    cacheable = False

    # How many times a submission may be refused before its next well-formed
    # submission is taken anyway. A check that keeps refusing is not teaching the
    # model anything, and a session that argues until it runs out of turns
    # returns *nothing* -- which is strictly worse than the imperfect answer it
    # was refusing. Hotel Kabuki burned all forty turns this way.
    MAX_REJECTIONS_PER_PHASE = 2

    # The coverage gate gets its own, larger allowance. Every other rejection
    # asks the model to reconsider a judgement, which it may reasonably keep;
    # this one asks it to go and read, which is unambiguous and always available.
    # Two attempts is the wrong budget for an instruction that cannot be misread.
    MAX_COVERAGE_REFUSALS = 5

    def __init__(
        self,
        workbook: WorkbookRecord,
        *,
        period_ids: list[str],
        financial_sheets: list[str] | None = None,
        max_reads: int = 120,
    ) -> None:
        self.workbook = workbook
        self.period_ids = list(period_ids)
        self.sheets = {sheet.sheet_name: sheet for sheet in workbook.sheets}
        # Routing defines the binding scope. Other sheets remain readable for
        # context, but they cannot contribute a binding outcome.
        self.financial_sheets = [
            name for name in (financial_sheets or list(self.sheets)) if name in self.sheets
        ]
        self.max_reads = max_reads
        self.reads = 0
        # Only `read_rows` and `column_stats` count as opening a sheet. A
        # `find_rows` hit does not: a workbook-wide search returns cells from
        # twenty tabs without the model having seen any of their header blocks,
        # and "which column is December" is a question only the header answers.
        self.opened_sheets: set[str] = set()
        self.submission: WorkbookBindings | None = None
        self.rejections: list[str] = []
        self.observations: list[str] = []
        self._refusals: dict[str, int] = {}

    def signature(self) -> str:
        return f"period_binding:{self.workbook.workbook_id}"

    # -- declarations -----------------------------------------------------

    def declarations(self) -> list[dict[str, Any]]:
        sheet_name = {
            "type": "string",
            "description": "Exact sheet name, as returned by list_sheets.",
            "enum": list(self.sheets),
        }
        return [
            {
                "name": "list_sheets",
                "description": (
                    "Every sheet in the workbook, whether routing selected it, "
                    "its extent, and a preview of the first words of text on it. "
                    "Start here."
                ),
                "parameters": {"type": "object", "properties": {}},
            },
            {
                "name": "read_rows",
                "description": (
                    "Populated cells in a row range of one sheet, with their "
                    f"coordinates. At most {MAX_ROWS_PER_READ} rows per call."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "sheet_name": sheet_name,
                        "start_row": {"type": "integer", "minimum": 1},
                        "end_row": {"type": "integer", "minimum": 1},
                    },
                    "required": ["sheet_name", "start_row"],
                },
            },
            {
                "name": "read_headers",
                "description": (
                    "The top rows of several sheets in ONE call, with "
                    "coordinates. This is the cheap way to answer for every "
                    f"sheet: name up to {MAX_SHEETS_PER_HEADER_READ} of them and "
                    "compare their header blocks side by side. Counts as having "
                    "opened each one."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "sheet_names": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": (
                                "Exact sheet names, as returned by list_sheets."
                            ),
                        },
                        "rows": {
                            "type": "integer",
                            "minimum": 1,
                            "maximum": MAX_HEADER_ROWS,
                            "description": (
                                f"How many rows from the top. Default "
                                f"{DEFAULT_HEADER_ROWS}; raise it when a header "
                                "block sits deeper."
                            ),
                        },
                    },
                    "required": ["sheet_names"],
                },
            },
            {
                "name": "find_rows",
                "description": (
                    "Where a string appears, case-insensitive, with coordinates "
                    "and the sheet each hit is on. Use it to locate a period "
                    "label without guessing a row number."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string"},
                        "sheet_name": sheet_name,
                    },
                    "required": ["query"],
                },
            },
            {
                "name": "column_stats",
                "description": (
                    "What every numeric column holds over a row range you name: "
                    "how many values, how many non-zero, how many are "
                    "percentage-formatted, their magnitudes, and real (label, "
                    "value) samples. Use it to check a column carries money "
                    "before binding it."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "sheet_name": sheet_name,
                        "start_row": {"type": "integer", "minimum": 1},
                        "end_row": {"type": "integer", "minimum": 1},
                    },
                    "required": ["sheet_name", "start_row", "end_row"],
                },
            },
            {
                "name": "submit_bindings",
                "description": (
                    "Which column holds each chosen period on each sheet. "
                    "This ends the session."
                ),
                "parameters": tool_parameter_schema(WorkbookBindings),
            },
        ]

    # -- dispatch ---------------------------------------------------------

    def dispatch(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        handlers = {
            "list_sheets": lambda: self._list_sheets(),
            "read_rows": lambda: self._read_rows(arguments),
            "read_headers": lambda: self._read_headers(arguments),
            "find_rows": lambda: self._find_rows(arguments),
            "column_stats": lambda: self._column_stats(arguments),
            "submit_bindings": lambda: self._submit_bindings(arguments),
        }
        handler = handlers.get(name)
        if handler is None:
            return {
                "ok": False,
                "error": f"Unknown tool {name!r}.",
                "instruction": f"Call one of: {', '.join(handlers)}.",
            }
        return handler()

    def terminal_result(self, name: str, result: dict[str, Any]):
        """End the session once the binding submission is accepted."""
        if name == "submit_bindings" and result.get("accepted"):
            return result.get("structure")
        return None

    # -- readers ----------------------------------------------------------

    def _list_sheets(self) -> dict[str, Any]:
        routed = set(self.financial_sheets)
        return {
            "ok": True,
            "filename": self.workbook.source.original_filename,
            "sheets": [
                {
                    "sheet_name": sheet.sheet_name,
                    "routed_as_financial": sheet.sheet_name in routed,
                    "max_row": sheet.max_row,
                    "max_column": sheet.max_column,
                    "preview": self._preview(sheet),
                }
                for sheet in self.workbook.sheets
            ],
        }

    def _preview(self, sheet) -> list[str]:
        """A few identifying words from the top of a sheet.

        A coded tab name carries no signal on its own -- the real title sits
        inside the sheet, like `0232 - Group Banquet`.
        """
        found: list[str] = []
        for row in sheet.rows[:12]:
            for cell in row.cells:
                text = str(cell.display_value or cell.raw_value or "").strip()
                if len(text) > 2 and any(c.isalpha() for c in text) and text not in found:
                    found.append(text)
                    if len(found) >= MAX_PREVIEW_LINES:
                        return found
        return found

    def _budget_spent(self) -> dict[str, Any] | None:
        if self.reads < self.max_reads:
            return None
        return {
            "ok": False,
            "error": f"Read budget of {self.max_reads} calls is spent.",
            "instruction": "Submit what you have. A partial answer is useful.",
        }

    def _sheet_or_error(self, sheet_name: str):
        sheet = self.sheets.get(sheet_name)
        if sheet is None:
            return None, {
                "ok": False,
                "error": f"No sheet named {sheet_name!r}.",
                "instruction": "Use a name from list_sheets.",
            }
        return sheet, None

    def _read_rows(self, arguments: dict[str, Any]) -> dict[str, Any]:
        if (spent := self._budget_spent()) is not None:
            return spent
        sheet, error = self._sheet_or_error(str(arguments.get("sheet_name") or ""))
        if error is not None:
            return error
        start = max(1, int(arguments.get("start_row") or 1))
        end = arguments.get("end_row")
        end = int(end) if end is not None else start + MAX_ROWS_PER_READ - 1
        end = min(end, start + MAX_ROWS_PER_READ - 1)
        self.reads += 1
        self.opened_sheets.add(sheet.sheet_name)

        rows: list[dict[str, Any]] = []
        cells_returned = 0
        truncated_at: int | None = None
        for row in sheet.rows:
            if not start <= row.row_index <= end:
                continue
            cells = [
                {"at": cell.address, "value": _text(cell)}
                for cell in row.cells
                if _text(cell) is not None
            ]
            if not cells:
                continue
            if cells_returned + len(cells) > MAX_CELLS_PER_READ and rows:
                truncated_at = row.row_index
                break
            rows.append({"row": row.row_index, "cells": cells})
            cells_returned += len(cells)

        result: dict[str, Any] = {
            "ok": True,
            "sheet_name": sheet.sheet_name,
            "merged_ranges": [merged.range for merged in sheet.merged_ranges[:40]],
            "rows": rows,
        }
        if truncated_at is not None:
            # Said plainly so a silently short read cannot hide a header row.
            result["truncated_before_row"] = truncated_at
            result["instruction"] = (
                f"This sheet is {sheet.max_column} columns wide, so the read was "
                f"cut at {MAX_CELLS_PER_READ} cells. Rows {truncated_at}-{end} "
                "were not returned. Ask for a narrower row range to see them."
            )
        return result

    def _read_headers(self, arguments: dict[str, Any]) -> dict[str, Any]:
        """The top of many sheets at once.

        Full coverage was costing one tool call per sheet, and a session with
        twenty-five routed tabs would rather submit a short answer than spend
        twenty-five turns earning a complete one -- which is exactly what one run
        did, growing its answer two bindings at a time until it was accepted.
        Making the reads cheap is a better fix than insisting harder.

        Folded into one call for the same reason `list_sheets` folds a preview of
        every sheet into itself: the model should not have to remember to do
        something the workbook is already open for.
        """
        if (spent := self._budget_spent()) is not None:
            return spent
        requested = arguments.get("sheet_names") or []
        if isinstance(requested, str):
            requested = [requested]
        known = [str(name) for name in requested if str(name) in self.sheets]
        unknown = [str(name) for name in requested if str(name) not in self.sheets]
        if not known:
            return {
                "ok": False,
                "error": f"None of those sheets are in this workbook: {', '.join(unknown[:8])}.",
                "instruction": "Use names from list_sheets.",
            }
        rows = int(arguments.get("rows") or DEFAULT_HEADER_ROWS)
        rows = max(1, min(rows, MAX_HEADER_ROWS))
        taken = known[:MAX_SHEETS_PER_HEADER_READ]
        self.reads += 1
        self.opened_sheets.update(taken)

        result: dict[str, Any] = {
            "ok": True,
            "rows_per_sheet": rows,
            "sheets": [
                {
                    "sheet_name": name,
                    "merged_ranges": [
                        merged.range for merged in self.sheets[name].merged_ranges[:12]
                    ],
                    "rows": [
                        {
                            "row": row.row_index,
                            "cells": [
                                {"at": cell.address, "value": _text(cell)}
                                for cell in row.cells
                                if _text(cell) is not None
                            ],
                        }
                        for row in self.sheets[name].rows
                        if row.row_index <= rows
                        and any(_text(cell) is not None for cell in row.cells)
                    ],
                }
                for name in taken
            ],
        }
        if unknown:
            result["ignored_unknown_sheets"] = unknown[:8]
        if len(known) > MAX_SHEETS_PER_HEADER_READ:
            result["not_read"] = known[MAX_SHEETS_PER_HEADER_READ:]
            result["instruction"] = (
                f"Only the first {MAX_SHEETS_PER_HEADER_READ} were read. Call "
                "read_headers again for the rest."
            )
        return result

    def _find_rows(self, arguments: dict[str, Any]) -> dict[str, Any]:
        if (spent := self._budget_spent()) is not None:
            return spent
        query = str(arguments.get("query") or "").strip().lower()
        if not query:
            return {"ok": False, "error": "find_rows needs a query."}
        requested = arguments.get("sheet_name")
        if requested and requested not in self.sheets:
            return {
                "ok": False,
                "error": f"No sheet named {requested!r}.",
                "instruction": "Use a name from list_sheets, or omit it to search all.",
            }
        names = [requested] if requested else list(self.sheets)
        self.reads += 1
        hits = []
        for name in names:
            for row in self.sheets[name].rows:
                for cell in row.cells:
                    text = _text(cell)
                    if text and query in text.lower():
                        hits.append(
                            {"sheet_name": name, "at": cell.address, "value": text}
                        )
                        if len(hits) >= MAX_FIND_HITS:
                            return {"ok": True, "query": query, "hits": hits}
        return {"ok": True, "query": query, "hits": hits}

    def _column_stats(self, arguments: dict[str, Any]) -> dict[str, Any]:
        if (spent := self._budget_spent()) is not None:
            return spent
        sheet, error = self._sheet_or_error(str(arguments.get("sheet_name") or ""))
        if error is not None:
            return error
        start = max(1, int(arguments.get("start_row") or 1))
        end = int(arguments.get("end_row") or start)
        self.reads += 1
        self.opened_sheets.add(sheet.sheet_name)
        return column_stats(sheet, start, end).as_dict()

    # -- submission -------------------------------------------------------

    def _submit_bindings(self, arguments: dict[str, Any]) -> dict[str, Any]:
        try:
            submission = WorkbookBindings.model_validate(arguments)
        except Exception as exc:  # noqa: BLE001 - the message goes back to the model
            return self._reject(f"That did not match the schema: {exc}", "submit_bindings")

        # Before anything about the columns: did the session actually open the
        # sheets it is making claims about? Asking in the prompt does not carry
        # this. Exploration measured it -- 24 of 29 sessions read exactly one
        # sheet after being asked in text for five -- and the corpus measured it
        # again here: the fast model bound columns on 28 tabs it never opened,
        # and got the period block wrong on every one of them.
        unread = self._unopened_claims(submission)
        unanswered = self._unanswered_pairs(submission)
        if unread or unanswered:
            self._refusals["coverage"] = self._refusals.get("coverage", 0) + 1
            if self._refusals["coverage"] <= self.MAX_COVERAGE_REFUSALS:
                return self._coverage_refusal(unread, unanswered)
            # Out of patience. Keep only claims grounded in opened sheets, then
            # fail closed for every unresolved sheet-period pair. Never infer a
            # missing column from another sheet's convention.
            if unread:
                submission = _drop_unopened(submission, self.opened_sheets)
                self.observations.append(
                    f"Dropped bindings for {len(unread)} sheet(s) the session never "
                    f"opened ({', '.join(sorted(unread)[:6])})."
                )
            unanswered = self._unanswered_pairs(submission)
            if unanswered:
                submission = _mark_unresolved_unavailable(submission, unanswered)
                self.observations.append(
                    f"Marked {len(unanswered)} unresolved sheet-period pair(s) "
                    "unavailable; no default column was inferred."
                )

        result = check_bindings(
            submission,
            self.sheets,
            period_ids=self.period_ids,
            financial_sheets=self.financial_sheets,
        )
        if not result.accepted and not self._out_of_patience("submit_bindings"):
            return self._reject(" ".join(result.rejections), "submit_bindings")

        if result.rejections:
            # Preserve only mechanically valid, unambiguous outcomes, then fail
            # closed for every pair the salvage removed.
            submission = _drop_rejected_bindings(
                submission,
                self.sheets,
                self.period_ids,
                self.financial_sheets,
            )
            unresolved = self._unanswered_pairs(submission)
            submission = _mark_unresolved_unavailable(submission, unresolved)
            self.observations.extend(
                f"Removed during deterministic salvage: {message}"
                for message in result.rejections
            )
            if unresolved:
                self.observations.append(
                    f"Marked {len(unresolved)} invalid or ambiguous sheet-period "
                    "pair(s) unavailable; no default column was inferred."
                )
        self.observations.extend(result.observations)
        self.submission = submission.model_copy(
            update={"observations": list(self.observations)}
        )
        return {
            "ok": True,
            "accepted": True,
            "structure": self.submission.model_dump(mode="json"),
        }

    def _unanswered_pairs(self, submission: WorkbookBindings) -> set[tuple[str, str]]:
        """Routed sheet-period pairs this submission says nothing about.

        The hole the first version of the read gate left. It asked only that
        claims be backed by reads, which a *small* answer satisfies trivially --
        and one run found that out, growing its submission two bindings at a time
        and being accepted at five sheets of twenty-five. Routing already decided
        these sheets hold P&L content, so silence about one is not an answer;
        `unavailable` is always available where a period genuinely is not there.
        """
        answered = {
            (binding.sheet_name, binding.period_id) for binding in submission.bindings
        } | {(item.sheet_name, item.period_id) for item in submission.unavailable}
        return {
            (name, period_id)
            for name in self.financial_sheets
            for period_id in self.period_ids
            if (name, period_id) not in answered
        }

    def _coverage_refusal(
        self,
        unread: set[str],
        unanswered: set[tuple[str, str]],
    ) -> dict[str, Any]:
        """One message covering the binding and read obligations."""
        parts: list[str] = []
        if unanswered:
            examples = ", ".join(
                f"{sheet} [{period}]"
                for sheet, period in sorted(unanswered)[:12]
            )
            parts.append(
                f"{len(unanswered)} routed sheet-period pair(s) have no binding "
                f"and no unavailable note: {examples}. Routing "
                "decided these hold P&L content, so each one needs an answer for "
                "every chosen period -- a column, or an unavailable note saying "
                "what you saw instead."
            )
        if unread:
            parts.append(
                f"{len(unread)} sheet(s) in the submission have not been opened: "
                f"{', '.join(sorted(unread)[:12])}. Which column holds a period is "
                "a question only that sheet's header rows answer."
            )
        parts.append(
            f"Use read_headers to open up to {MAX_SHEETS_PER_HEADER_READ} sheets "
            "in a single call and compare their header blocks side by side, then "
            "submit once with every sheet answered for. Do not shrink the "
            "submission to satisfy this: a sheet left out is excluded from "
            "mapping for that period rather than assigned another sheet's column."
        )
        message = " ".join(parts)
        self.rejections.append(message)
        return {
            "ok": True,
            "accepted": False,
            "error": message,
            "opened_so_far": sorted(self.opened_sheets),
            "sheets_to_answer_for": self.financial_sheets,
            "instruction": "Read what you are missing, then call submit_bindings again.",
        }

    def _unopened_claims(self, submission: WorkbookBindings) -> set[str]:
        """Sheets this submission makes a claim about without having opened.

        Both a binding and an `unavailable` note are claims: saying a period is
        not on a sheet is as much an assertion about its headers as saying which
        column holds it.
        """
        claimed = {binding.sheet_name for binding in submission.bindings} | {
            item.sheet_name for item in submission.unavailable
        }
        return {
            name
            for name in claimed
            if name in self.sheets and name not in self.opened_sheets
        }

    def _out_of_patience(self, tool: str) -> bool:
        """Has this submission been refused often enough to stop refusing it?"""
        return self._refusals.get(tool, 0) >= self.MAX_REJECTIONS_PER_PHASE

    def _reject(self, message: str, tool: str) -> dict[str, Any]:
        self.rejections.append(message)
        self._refusals[tool] = self._refusals.get(tool, 0) + 1
        remaining = self.MAX_REJECTIONS_PER_PHASE - self._refusals[tool]
        instruction = f"Correct it and call {tool} again."
        if remaining <= 0:
            instruction += (
                " This is the last time this will be refused -- the next "
                "well-formed answer is taken as it stands, so send your best "
                "one rather than stopping."
            )
        return {
            "ok": True,
            "accepted": False,
            "error": message,
            "instruction": instruction,
        }

    def best_effort(self) -> WorkbookBindings | None:
        """Whatever valid submission this session established."""
        if self.submission is not None:
            return self.submission
        return None


def _drop_unopened(
    submission: WorkbookBindings, opened: set[str]
) -> WorkbookBindings:
    """Remove claims about sheets the session never opened.

    Safer than keeping them. A dropped binding leaves the sheet on the workbook's
    most common column, which is right far more often than a column chosen
    without looking; a kept one puts a specific wrong figure in front of the
    mapper, which nothing downstream can detect.
    """
    return submission.model_copy(
        update={
            "bindings": [
                binding
                for binding in submission.bindings
                if binding.sheet_name in opened
            ],
            "unavailable": [
                item for item in submission.unavailable if item.sheet_name in opened
            ],
        }
    )


def _drop_rejected_bindings(
    submission: WorkbookBindings,
    sheets,
    period_ids: list[str],
    financial_sheets: list[str],
) -> WorkbookBindings:
    """Keep the bindings that stand on their own, drop the ones that cannot.

    Used only when patience has run out. A binding naming a sheet that is not
    here, or a column with no numbers, would put a wrong figure in front of the
    mapper -- which is the one outcome worse than no figure -- so those go. The
    rest are kept, because they were never the problem.
    """
    chosen = set(period_ids)
    financial = set(financial_sheets)
    unavailable = []
    unavailable_pairs: set[tuple[str, str]] = set()
    for item in submission.unavailable:
        pair = (item.sheet_name, item.period_id)
        if (
            pair in unavailable_pairs
            or item.period_id not in chosen
            or item.sheet_name not in sheets
            or item.sheet_name not in financial
            or not item.reason.strip()
        ):
            continue
        unavailable.append(item)
        unavailable_pairs.add(pair)

    candidates = []
    for binding in submission.bindings:
        sheet = sheets.get(binding.sheet_name)
        pair = (binding.sheet_name, binding.period_id)
        if (
            sheet is None
            or binding.sheet_name not in financial
            or binding.period_id not in chosen
            or pair in unavailable_pairs
        ):
            continue
        letters = (binding.excel_column or "").strip().upper()
        if not letters.isalpha():
            continue
        column = _excel_column_number(letters)
        if not _sheet_column_holds_numbers(sheet, column):
            continue
        candidates.append(binding)

    pair_counts: dict[tuple[str, str], int] = {}
    column_periods: dict[tuple[str, str], set[str]] = {}
    for binding in candidates:
        pair = (binding.sheet_name, binding.period_id)
        pair_counts[pair] = pair_counts.get(pair, 0) + 1
        column_periods.setdefault(
            (binding.sheet_name, binding.excel_column.strip().upper()), set()
        ).add(binding.period_id)
    kept = [
        binding
        for binding in candidates
        if pair_counts[(binding.sheet_name, binding.period_id)] == 1
        and len(
            column_periods[
                (binding.sheet_name, binding.excel_column.strip().upper())
            ]
        )
        == 1
    ]
    return submission.model_copy(
        update={"bindings": kept, "unavailable": unavailable}
    )


def _mark_unresolved_unavailable(
    submission: WorkbookBindings,
    unresolved: set[tuple[str, str]],
) -> WorkbookBindings:
    """Fail closed for pairs the model did not establish explicitly."""
    if not unresolved:
        return submission
    additions = [
        UnavailablePeriod(
            sheet_name=sheet_name,
            period_id=period_id,
            reason=(
                "Binding was not established before the model session ended; "
                "no column was inferred from another sheet."
            ),
        )
        for sheet_name, period_id in sorted(unresolved)
    ]
    return submission.model_copy(
        update={"unavailable": [*submission.unavailable, *additions]}
    )


def _excel_column_number(letters: str) -> int:
    number = 0
    for character in letters:
        number = number * 26 + ord(character) - ord("A") + 1
    return number


def _sheet_column_holds_numbers(sheet, column: int) -> bool:
    return any(
        isinstance(cell.raw_value, int | float)
        and not isinstance(cell.raw_value, bool)
        for row in sheet.rows
        for cell in row.cells
        if cell.column == column
    )


def _text(cell) -> str | None:
    """One cell as short display text, or None when it holds nothing."""
    value = cell.display_value if cell.display_value is not None else cell.raw_value
    if value is None:
        return None
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    text = " ".join(str(value).split())
    return text[:160] or None


__all__ = ["PeriodBindingToolset", "MAX_ROWS_PER_READ"]
