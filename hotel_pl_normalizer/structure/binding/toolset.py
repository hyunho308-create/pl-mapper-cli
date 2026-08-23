"""The tools one department-and-binding session can call.

Four readers and two submissions, one per phase, over the parsed record the
mapper is about to be given. Reading the same record matters: two readers of one
workbook can disagree, and every figure downstream comes from this one.

The readers are thin. They hand back what is in the cells, with coordinates, and
leave the judgement to the model -- except `column_stats`, which counts and then
says plainly when the counts are too thin to mean anything. That is the one place
a rule survived, and `column_stats.py` explains why.

The two submissions are ordered, and the ordering is the only control here.
`submit_bindings` is refused until `submit_departments` has been accepted, and
phase two's instructions are returned *by* `submit_departments` rather than
stated up front. Exploration measured what happens otherwise: a session told
about both jobs at once optimises for the second and lets the first fall out of
it.
"""

from __future__ import annotations

from functools import lru_cache
from importlib import resources
from typing import Any

from hotel_pl_normalizer.models.binding import (
    DEPARTMENTS,
    DepartmentBinding,
    WorkbookBindings,
    WorkbookDepartments,
)
from hotel_pl_normalizer.models.workbook import WorkbookRecord
from hotel_pl_normalizer.providers.base import tool_parameter_schema

from .checks import check_bindings, check_departments, normalize_spans
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


@lru_cache(maxsize=1)
def _period_binding_instructions() -> str:
    """Phase two's skill, delivered as the result of an accepted phase one."""
    return (
        resources.files("hotel_pl_normalizer.prompts")
        .joinpath("period_binding.md")
        .read_text(encoding="utf-8")
    )


class DepartmentBindingToolset:
    """Reader tools plus the two phase submissions, over one parsed workbook."""

    # Tool results are reads of one specific record, and the prompt names the
    # workbook, so a cached session would be safe -- but the periods vary per
    # run and the prompt would have to carry them all. Not worth the subtlety.
    cacheable = False

    # How many times one phase may be refused before its next well-formed
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
        period_labels: dict[str, str] | None = None,
        financial_sheets: list[str] | None = None,
        max_reads: int = 120,
        locate_departments: bool = True,
    ) -> None:
        self.workbook = workbook
        self.period_ids = list(period_ids)
        self.period_labels = dict(period_labels or {})
        self.sheets = {sheet.sheet_name: sheet for sheet in workbook.sheets}
        # Routing's answer, kept as a preference rather than a boundary: a span
        # on a sheet routing skipped is accepted, because routing is sometimes
        # wrong and being right about it should not cost a property.
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
        # Locating departments is where this stage spends -- 208 of 317 tool
        # calls across the corpus happen before `submit_departments`, and on a
        # one-sheet P&L it is nearly all of them. With it off there is one job
        # and one submission, so `departments` starts settled and empty rather
        # than waiting to be filled.
        self.locate_departments = locate_departments
        self.departments: WorkbookDepartments | None = (
            None if locate_departments else WorkbookDepartments()
        )
        self.submission: DepartmentBinding | None = None
        self.rejections: list[str] = []
        self.observations: list[str] = []
        self._refusals: dict[str, int] = {}

    def signature(self) -> str:
        return f"department_binding:{self.workbook.workbook_id}"

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
                    "and the sheet each hit is on. Use it to locate a department "
                    "heading or a period label without guessing a row number."
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
            *(
                [
                    {
                        "name": "submit_departments",
                        "description": (
                            "Phase one result: where every department sits. "
                            "Returns the period-binding instructions for phase "
                            "two."
                        ),
                        "parameters": tool_parameter_schema(WorkbookDepartments),
                    }
                ]
                if self.locate_departments
                else []
            ),
            {
                "name": "submit_bindings",
                "description": (
                    "Which column holds each chosen period on each sheet. This "
                    "ends the session."
                    + (
                        " Only available after submit_departments has been "
                        "accepted."
                        if self.locate_departments
                        else ""
                    )
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
            "submit_departments": lambda: self._submit_departments(arguments),
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
        """Ends the session only once phase two is accepted."""
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
            # Said plainly, because a silently short read is how a department
            # boundary goes missing without anyone noticing.
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

    # -- phase one --------------------------------------------------------

    def _submit_departments(self, arguments: dict[str, Any]) -> dict[str, Any]:
        try:
            submission = WorkbookDepartments.model_validate(arguments)
        except Exception as exc:  # noqa: BLE001 - the message goes back to the model
            return self._reject(f"That did not match the schema: {exc}", "submit_departments")

        submission = normalize_spans(submission)
        result = check_departments(
            submission,
            self.sheets,
            financial_sheets=self.financial_sheets,
            read_sheets=self.opened_sheets,
        )
        if not result.accepted and not self._out_of_patience("submit_departments"):
            return self._reject(" ".join(result.rejections), "submit_departments")

        self.departments = submission
        self.observations.extend(result.observations)
        # A refusal that was overruled is still worth knowing about, so it
        # travels with the answer instead of disappearing with the argument.
        self.observations.extend(
            f"Accepted despite: {message}" for message in result.rejections
        )
        return {
            "ok": True,
            "accepted": True,
            "departments_located": len(submission.departments),
            "observations": result.observations,
            # Named explicitly so phase two does not have to re-derive them, and
            # labelled so the model can recognise them in a header.
            "periods_to_bind": [
                {"period_id": period_id, "label": self.period_labels.get(period_id, period_id)}
                for period_id in self.period_ids
            ],
            "sheets_to_answer_for": self.financial_sheets,
            "next_phase": _period_binding_instructions(),
        }

    # -- phase two --------------------------------------------------------

    def _submit_bindings(self, arguments: dict[str, Any]) -> dict[str, Any]:
        if self.departments is None:
            return {
                "ok": False,
                "error": "Departments have not been submitted yet.",
                "instruction": (
                    "Locate the departments and call submit_departments first. "
                    "The binding instructions come back in its result."
                ),
            }
        try:
            submission = WorkbookBindings.model_validate(arguments)
        except Exception as exc:  # noqa: BLE001 - the message goes back to the model
            return self._reject(f"That did not match the schema: {exc}", "submit_bindings")

        classification_names = [
            item.sheet_name for item in submission.sheet_classifications
        ]
        invalid_classifications = sorted(
            name for name in set(classification_names) if name not in self.financial_sheets
        )
        duplicate_classifications = sorted({
            name for name in classification_names if classification_names.count(name) > 1
        })
        classification_errors = []
        if invalid_classifications:
            classification_errors.append(
                "sheet classifications name non-routed sheets: "
                + ", ".join(invalid_classifications)
            )
        if duplicate_classifications:
            classification_errors.append(
                "sheet classifications must be unique: "
                + ", ".join(duplicate_classifications)
            )
        if classification_errors and not self._out_of_patience("submit_bindings"):
            return self._reject(" ".join(classification_errors), "submit_bindings")
        if classification_errors:
            submission = _clean_classifications(submission, set(self.financial_sheets))

        # Before anything about the columns: did the session actually open the
        # sheets it is making claims about? Asking in the prompt does not carry
        # this. Exploration measured it -- 24 of 29 sessions read exactly one
        # sheet after being asked in text for five -- and the corpus measured it
        # again here: the fast model bound columns on 28 tabs it never opened,
        # and got the period block wrong on every one of them.
        unread = self._unopened_claims(submission)
        unanswered = self._unanswered_sheets(submission)
        unclassified = self._unclassified_sheets(submission)
        if unread or unanswered or unclassified:
            self._refusals["coverage"] = self._refusals.get("coverage", 0) + 1
            if self._refusals["coverage"] <= self.MAX_COVERAGE_REFUSALS:
                return self._coverage_refusal(unread, unanswered, unclassified)
            # Out of patience. An unopened claim is the one case where taking the
            # answer is worse than dropping it -- the sheet falls back to the
            # workbook's usual column, which beats a column nobody looked at.
            # An unanswered sheet already falls back, so it just gets recorded.
            if unread:
                submission = _drop_unopened(submission, self.opened_sheets)
                self.observations.append(
                    f"Dropped bindings for {len(unread)} sheet(s) the session never "
                    f"opened ({', '.join(sorted(unread)[:6])}); they fall back to the "
                    "workbook default column."
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
            # Taking a flawed binding beats taking none: every sheet the model
            # did name still gets its column, and the rest fall back to the
            # workbook default rather than to nothing at all.
            submission = _drop_rejected_bindings(submission, self.sheets, self.period_ids)
            self.observations.extend(
                f"Accepted despite: {message}" for message in result.rejections
            )
        self.observations.extend(result.observations)
        self.submission = DepartmentBinding(
            departments=self.departments.departments,
            bindings=submission.bindings,
            unavailable=submission.unavailable,
            sheet_classifications=submission.sheet_classifications,
            notes=[*self.departments.notes, *submission.notes],
            observations=list(self.observations),
        )
        return {
            "ok": True,
            "accepted": True,
            "structure": self.submission.model_dump(mode="json"),
        }

    def _unanswered_sheets(self, submission: WorkbookBindings) -> set[str]:
        """Routed sheets this submission says nothing about, for some period.

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
            name
            for name in self.financial_sheets
            for period_id in self.period_ids
            if (name, period_id) not in answered
        }

    def _unclassified_sheets(self, submission: WorkbookBindings) -> set[str]:
        """Routed sheets without the sheet-level hint gathered during binding."""
        classified = {
            item.sheet_name for item in submission.sheet_classifications
            if item.sheet_name in self.sheets
        }
        return set(self.financial_sheets) - classified

    def _coverage_refusal(
        self,
        unread: set[str],
        unanswered: set[str],
        unclassified: set[str],
    ) -> dict[str, Any]:
        """One message covering both halves of the obligation."""
        parts: list[str] = []
        if unanswered:
            parts.append(
                f"{len(unanswered)} routed sheet(s) have no binding and no "
                f"unavailable note: {', '.join(sorted(unanswered)[:12])}. Routing "
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
        if unclassified:
            parts.append(
                f"{len(unclassified)} routed sheet(s) have no sheet classification: "
                f"{', '.join(sorted(unclassified)[:12])}. Return exactly one "
                "classification per routed sheet. Use an empty department_hints "
                "list when the sheet is mixed or unknown; do not invent boundaries."
            )
        parts.append(
            f"Use read_headers to open up to {MAX_SHEETS_PER_HEADER_READ} sheets "
            "in a single call and compare their header blocks side by side, then "
            "submit once with every sheet answered for. Do not shrink the "
            "submission to satisfy this: a sheet left out falls back to another "
            "sheet's column, which is the same guess, only harder to see."
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
        } | {item.sheet_name for item in submission.sheet_classifications}
        return {
            name
            for name in claimed
            if name in self.sheets and name not in self.opened_sheets
        }

    def _out_of_patience(self, tool: str) -> bool:
        """Has this phase been refused often enough to stop refusing it?"""
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

    def best_effort(self) -> DepartmentBinding | None:
        """Whatever this session established, for a loop that ran out of turns.

        Departments without bindings is a real answer: every sheet falls back to
        the workbook's usual column and the mapper still runs. Nothing at all is
        not.
        """
        if self.submission is not None:
            return self.submission
        if self.departments is None:
            return None
        return DepartmentBinding(
            departments=self.departments.departments,
            notes=list(self.departments.notes),
            observations=[
                *self.observations,
                "Session ran out of turns before binding any period; every sheet "
                "falls back to the workbook default column.",
            ],
        )


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
            "sheet_classifications": [
                item
                for item in submission.sheet_classifications
                if item.sheet_name in opened
            ],
        }
    )


def _drop_rejected_bindings(
    submission: WorkbookBindings, sheets, period_ids: list[str]
) -> WorkbookBindings:
    """Keep the bindings that stand on their own, drop the ones that cannot.

    Used only when patience has run out. A binding naming a sheet that is not
    here, or a column with no numbers, would put a wrong figure in front of the
    mapper -- which is the one outcome worse than no figure -- so those go. The
    rest are kept, because they were never the problem.
    """
    chosen = set(period_ids)
    kept = []
    for binding in submission.bindings:
        sheet = sheets.get(binding.sheet_name)
        if sheet is None or binding.period_id not in chosen:
            continue
        letters = (binding.excel_column or "").strip().upper()
        if not letters.isalpha():
            continue
        kept.append(binding)
    return submission.model_copy(update={"bindings": kept})


def _clean_classifications(
    submission: WorkbookBindings, allowed: set[str]
) -> WorkbookBindings:
    """Keep the first classification for each routed sheet."""
    seen: set[str] = set()
    kept = []
    for item in submission.sheet_classifications:
        if item.sheet_name not in allowed or item.sheet_name in seen:
            continue
        seen.add(item.sheet_name)
        kept.append(item)
    return submission.model_copy(update={"sheet_classifications": kept})


def _text(cell) -> str | None:
    """One cell as short display text, or None when it holds nothing."""
    value = cell.display_value if cell.display_value is not None else cell.raw_value
    if value is None:
        return None
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    text = " ".join(str(value).split())
    return text[:160] or None


__all__ = ["DEPARTMENTS", "DepartmentBindingToolset", "MAX_ROWS_PER_READ"]
