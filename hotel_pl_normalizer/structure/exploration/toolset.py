"""Workbook readers and ordered submissions for exploration.

The model must submit routing before it receives the period-detection
instructions. Mechanical checks enforce real sheet names and minimum financial
sheet coverage; workbook interpretation remains the model's responsibility.
"""

from __future__ import annotations

from collections import Counter
from functools import lru_cache
from importlib import resources
from typing import Any

from hotel_pl_normalizer.models.exploration import (
    DiscoveredPeriod,
    WorkbookExploration,
    WorkbookPeriods,
    WorkbookRouting,
)
from hotel_pl_normalizer.providers.base import tool_parameter_schema

from .reader import MAX_ROWS_PER_READ, LazyWorkbook


@lru_cache(maxsize=1)
def _period_detection_instructions() -> str:
    """Phase two's skill, delivered as the result of an accepted routing call.

    Held back rather than included in the opening prompt: a session told about
    both jobs at once optimises for the second and lets the first fall out of it.
    """
    return (
        resources.files("hotel_pl_normalizer.prompts")
        .joinpath("period_detection.md")
        .read_text(encoding="utf-8")
    )


class WorkbookExplorationToolset:
    """Reader tools plus the two phase submissions, over one open workbook."""

    # The session's opening prompt is the same for a given workbook, but the
    # tool results are file reads: caching a whole session on the prompt would
    # serve stale structure if the file were replaced under the same name.
    cacheable = False

    # Periods must be confirmed against the detail, so phase two has to open
    # several of the sheets routing marked as financial -- not just the summary.
    # This is a floor, not a target: a workbook with fewer financial sheets than
    # this needs only the ones it has.
    MIN_FINANCIAL_SHEETS_READ = 5

    def __init__(self, workbook: LazyWorkbook, *, max_reads: int = 40) -> None:
        self.workbook = workbook
        self.max_reads = max_reads
        self.reads = 0
        self.routing: WorkbookRouting | None = None
        self.submission: WorkbookExploration | None = None
        # Which sheets have actually been opened. Asking for five sheets in the
        # prompt did not produce five: across 29 workbooks, 24 sessions read
        # exactly one before submitting. Text alone does not carry this.
        self.read_sheets: set[str] = set()
        self.rejections: list[str] = []
        self._sheet_names = [sheet.sheet_name for sheet in workbook.sheets()]

    def signature(self) -> str:
        return f"exploration:{self.workbook.path.name}"

    # -- declarations -----------------------------------------------------

    def declarations(self) -> list[dict[str, Any]]:
        sheet_name = {
            "type": "string",
            "description": "Exact sheet name, as returned by list_sheets.",
            "enum": self._sheet_names,
        }
        return [
            {
                "name": "list_sheets",
                "description": (
                    "Every sheet in the workbook with its position, visibility, "
                    "declared size, and a preview of the first words of text on "
                    "it. Declared sizes are often wrong and are only a hint; the "
                    "preview is real sheet content. Start here."
                ),
                "parameters": {"type": "object", "properties": {}},
            },
            {
                "name": "read_rows",
                "description": (
                    "Populated cells in a row range of one sheet, with their "
                    f"coordinates. At most {MAX_ROWS_PER_READ} rows per call. "
                    "Header blocks are usually in the first 40 rows but not "
                    "always: read further if the period labels are not there."
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
                "name": "find_text",
                "description": (
                    "Where a string appears, case-insensitive, with coordinates. "
                    "Use it to locate a header block you could not find by "
                    "reading, for example 'YTD', 'Actual', 'Budget' or a month."
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
                "name": "submit_routing",
                "description": (
                    "Phase one result: the workbook layout and a decision for "
                    f"every one of the {len(self._sheet_names)} sheets. Every "
                    "sheet must appear. Returns the period-detection "
                    "instructions for phase two."
                ),
                # Derived from the model rather than hand-written, so the two
                # cannot drift. `model_json_schema()` alone is not usable here:
                # it describes nested models with `$ref`/`$defs`. Keep tool
                # schemas within the portable JSON Schema subset.
                "parameters": tool_parameter_schema(WorkbookRouting),
            },
            {
                "name": "submit_periods",
                "description": (
                    "Phase two result: the periods the workbook offers. Only "
                    "available after submit_routing has been accepted. This ends "
                    "the session."
                ),
                "parameters": tool_parameter_schema(WorkbookPeriods),
            },
        ]

    # -- dispatch ---------------------------------------------------------

    def dispatch(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if name == "list_sheets":
            return self._list_sheets()
        if name == "read_rows":
            return self._read_rows(arguments)
        if name == "find_text":
            return self._find_text(arguments)
        if name == "submit_routing":
            return self._submit_routing(arguments)
        if name == "submit_periods":
            return self._submit_periods(arguments)
        return {
            "ok": False,
            "error": f"Unknown tool {name!r}.",
            "instruction": (
                "Call list_sheets, read_rows, find_text, submit_routing or "
                "submit_periods."
            ),
        }

    def terminal_result(self, name: str, result: dict[str, Any]):
        """Ends the session only once phase two is accepted."""
        if name == "submit_periods" and result.get("accepted"):
            return result.get("structure")
        return None

    # -- tools ------------------------------------------------------------

    PREVIEW_LINES = 6

    def _list_sheets(self) -> dict[str, Any]:
        """Every sheet, with a few words that identify it.

        The preview is not a convenience. The stage this replaces routed sheets
        from a full parse, so it saw header text on every sheet for free; a
        session that only opens the handful of sheets it chooses does not, and a
        coded tab name like `0400` carries no signal at all. Without the preview
        it skipped 23 real departmental schedules on one workbook.

        The top of an exported sheet is mostly boilerplate, though -- report id,
        layout, run date, company name -- and on Ritz-Carlton the first dozen
        text cells are identical across the departmental tabs, which identifies
        nothing. Rather than filter for the particular shapes one vendor emits,
        each sheet keeps the lines that appear on the *fewest* other sheets:
        boilerplate is, by definition, what does not vary. What survives is what
        distinguishes the sheet -- on that workbook, `73R61 ~ RC Half Moon Bay /
        0400 - ...`.

        Ranking rather than thresholding is deliberate. A cut-off has to be
        picked, and the right one differs per workbook: Ritz's boilerplate covers
        only the 23 coded tabs, so any threshold high enough to be safe on a
        small workbook leaves it in place. Rarest-first needs no such number.

        Reading twelve rows of every sheet costs about half a second on the
        largest workbook in the corpus, so this is folded into `list_sheets`
        rather than offered as a separate tool the model has to remember.
        """
        sheets = self.workbook.sheets()
        previews = {sheet.sheet_name: self.workbook.peek(sheet.sheet_name) for sheet in sheets}
        across = Counter(line for lines in previews.values() for line in lines)

        def distinctive(lines: list[str]) -> list[str]:
            if len(lines) <= self.PREVIEW_LINES:
                return lines
            kept = set(sorted(lines, key=lambda line: across[line])[: self.PREVIEW_LINES])
            # Reading order is restored afterwards: the ranking decides *which*
            # lines to show, not how they read.
            return [line for line in lines if line in kept]

        return {
            "ok": True,
            "sheets": [
                {
                    "sheet_name": sheet.sheet_name,
                    "index": sheet.index,
                    "visible": sheet.visible,
                    "approx_rows": sheet.approx_rows,
                    "approx_columns": sheet.approx_columns,
                    "preview": distinctive(previews[sheet.sheet_name]),
                }
                for sheet in sheets
            ],
        }

    def _budget_exceeded(self) -> dict[str, Any] | None:
        if self.reads < self.max_reads:
            return None
        return {
            "ok": False,
            "error": f"Read budget of {self.max_reads} calls is spent.",
            "instruction": "Submit what you have and move on.",
        }

    def _read_rows(self, arguments: dict[str, Any]) -> dict[str, Any]:
        if (spent := self._budget_exceeded()) is not None:
            return spent
        sheet_name = str(arguments.get("sheet_name") or "")
        if sheet_name not in self._sheet_names:
            return {
                "ok": False,
                "error": f"No sheet named {sheet_name!r}.",
                "instruction": "Use a name from list_sheets.",
            }
        start = int(arguments.get("start_row") or 1)
        end = arguments.get("end_row")
        self.reads += 1
        self.read_sheets.add(sheet_name)
        rows = self.workbook.read_rows(
            sheet_name, start, int(end) if end is not None else None
        )
        return {
            "ok": True,
            "sheet_name": sheet_name,
            "merged_ranges": self.workbook.merged_ranges(sheet_name)[:40],
            "rows": [
                {"row": start + offset, "cells": [
                    {"at": cell.coordinate, "value": cell.value} for cell in row
                ]}
                for offset, row in enumerate(rows)
                if row
            ],
        }

    def _find_text(self, arguments: dict[str, Any]) -> dict[str, Any]:
        if (spent := self._budget_exceeded()) is not None:
            return spent
        query = str(arguments.get("query") or "")
        sheet_name = arguments.get("sheet_name")
        if sheet_name and sheet_name not in self._sheet_names:
            return {
                "ok": False,
                "error": f"No sheet named {sheet_name!r}.",
                "instruction": "Use a name from list_sheets, or omit it to search all.",
            }
        self.reads += 1
        hits = self.workbook.find_text(query, sheet_name)
        self.read_sheets.update(name for name, _ in hits)
        return {
            "ok": True,
            "query": query,
            # Every hit carries its sheet: a workbook-wide search returns cells
            # from many sheets, and a bare coordinate would not say which.
            "hits": [
                {"sheet_name": name, "at": hit.coordinate, "value": hit.value}
                for name, hit in hits
            ],
        }

    # -- phase one: routing -----------------------------------------------

    def _submit_routing(self, arguments: dict[str, Any]) -> dict[str, Any]:
        """Accept the routing decision, and hand back the period instructions.

        The only check beyond the schema is one the old sheet routing needed too:
        a sheet name that is not in the workbook cannot be routed to. What counts
        as a good routing decision is the model's call, exactly as it was before.

        The period instructions are withheld until now. When both jobs were
        described up front the model went straight for the periods and treated
        routing as whatever fell out of them, marking sixty-five real schedules
        `skip` with the reason "not opened this session". Ordering the phases
        fixes that without anything having to inspect the decisions.
        """
        try:
            routing = WorkbookRouting.model_validate(arguments)
        except Exception as exc:  # noqa: BLE001 - the message goes back to the model
            self.rejections.append(str(exc))
            return {
                "ok": True,
                "accepted": False,
                "error": f"That did not match the schema: {exc}",
                "instruction": "Correct it and call submit_routing again.",
            }

        unknown = sorted(
            {sheet.sheet_name for sheet in routing.sheets} - set(self._sheet_names)
        )
        if unknown:
            self.rejections.append(f"unknown sheets: {', '.join(unknown)}")
            return {
                "ok": True,
                "accepted": False,
                "error": f"These sheet names are not in the workbook: {', '.join(unknown)}.",
                "instruction": "Use names from list_sheets and call submit_routing again.",
            }

        self.routing = routing
        required = self._required_reads()
        return {
            "ok": True,
            "accepted": True,
            "routed_sheets": len(routing.sheets),
            # Named explicitly rather than left to the model to re-derive: these
            # are the sheets periods must be confirmed against, and the count is
            # the one `submit_periods` will hold it to.
            "financial_sheets": routing.financial_sheet_names,
            "must_read_at_least": required,
            "already_read": sorted(self.read_sheets & set(routing.financial_sheet_names)),
            "next_phase": _period_detection_instructions(),
        }

    def _required_reads(self) -> int:
        """How many financial sheets phase two owes, given what routing found."""
        available = len(self.routing.financial_sheet_names) if self.routing else 0
        return min(self.MIN_FINANCIAL_SHEETS_READ, available)

    # -- phase two: periods -----------------------------------------------

    def _submit_periods(self, arguments: dict[str, Any]) -> dict[str, Any]:
        """Accept the periods and end the session.

        Two checks, neither about which periods are right.

        The first is coverage: periods have to be confirmed against the detail,
        so enough of the sheets routing called financial must actually have been
        opened. The prompt asked for five and did not get them -- across 29
        workbooks, 24 sessions read exactly one sheet and submitted -- so the
        count is held here instead. Which sheets, and what they show, stay the
        model's call; only the number is enforced, and it drops to whatever the
        workbook has when that is fewer than five.

        The second is the schema. A workbook the model believes has no periods is
        reported as it stands rather than argued with -- that is a finding, and
        the run log carries it.
        """
        if self.routing is None:
            return {
                "ok": False,
                "error": "Routing has not been submitted yet.",
                "instruction": (
                    "Decide the sheets and call submit_routing first. The period "
                    "instructions come back in its result."
                ),
            }
        financial = self.routing.financial_sheet_names
        required = self._required_reads()
        seen = [name for name in financial if name in self.read_sheets]
        if len(seen) < required:
            outstanding = [name for name in financial if name not in self.read_sheets]
            message = (
                f"Only {len(seen)} of the {len(financial)} financial sheets have "
                f"been opened; periods must be confirmed against at least "
                f"{required}. Read the header rows of some of these first: "
                f"{', '.join(outstanding[:10])}."
            )
            self.rejections.append(message)
            return {
                "ok": True,
                "accepted": False,
                "error": message,
                "read_so_far": seen,
                "instruction": (
                    "Call read_rows on those sheets, then submit_periods again."
                ),
            }

        try:
            found = WorkbookPeriods.model_validate(arguments)
        except Exception as exc:  # noqa: BLE001 - the message goes back to the model
            self.rejections.append(str(exc))
            return {
                "ok": True,
                "accepted": False,
                "error": f"That did not match the schema: {exc}",
                "instruction": "Correct it and call submit_periods again.",
            }

        periods, corrected = self._with_true_evidence(found.periods)
        notes = [*self.routing.notes, *found.notes]
        if corrected:
            notes.append(
                "sheets_present was corrected on "
                f"{len(corrected)} period(s): named sheets this session never "
                f"opened ({', '.join(sorted(corrected)[:8])}). The list now holds "
                "only sheets that were actually read."
            )

        self.submission = WorkbookExploration(
            workbook_layout=self.routing.workbook_layout,
            layout_evidence=self.routing.layout_evidence,
            sheets=self.routing.sheets,
            periods=periods,
            recommended_period_id=found.recommended_period_id,
            notes=notes,
        )
        return {
            "ok": True,
            "accepted": True,
            "structure": self.submission.model_dump(mode="json"),
        }

    def _with_true_evidence(
        self, periods: list[DiscoveredPeriod]
    ) -> tuple[list[DiscoveredPeriod], set[str]]:
        """Cut `sheets_present` back to sheets this session actually opened.

        `sheets_present` is the evidence of observed per-sheet coverage, but the
        model writes both the evidence and the conclusion, and it pads: one run
        listed 26 sheets having opened 7. A padded list falsely reports coverage
        that the session never checked.

        This does not judge which periods are right. It replaces a claim about
        what was read with the toolset's own record of what was read, so a period
        resting on one sheet out of eight now says so. A reader, and anything
        downstream, can see the difference; before, they could not.

        Returns the periods and the set of names that were removed.
        """
        corrected: set[str] = set()
        result: list[DiscoveredPeriod] = []
        for period in periods:
            true_sheets = [n for n in period.sheets_present if n in self.read_sheets]
            invented = set(period.sheets_present) - set(true_sheets)
            if invented:
                corrected |= invented
                period = period.model_copy(update={"sheets_present": true_sheets})
            result.append(period)
        return result, corrected
