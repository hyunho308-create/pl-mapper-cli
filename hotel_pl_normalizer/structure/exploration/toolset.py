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
from hotel_pl_normalizer.models.workbook import WorkbookRecord
from hotel_pl_normalizer.providers.base import tool_parameter_schema
from hotel_pl_normalizer.structure.monthly_spread import (
    MONTHLY_SPREAD_THRESHOLD,
    explicit_month_years,
)
from hotel_pl_normalizer.structure.period_headers import (
    latest_header_month,
    period_column_problem,
    period_layout_kind,
)

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

    # One controlling summary establishes the catalog. Department sheets only
    # confirm its periods; auxiliary T12/monthly summaries cannot expand it.
    MAX_DEPARTMENT_SHEETS_TO_CONFIRM = 4
    CONTROLLING_HEADER_ROWS = 40

    def __init__(
        self,
        workbook: LazyWorkbook,
        *,
        workbook_record: WorkbookRecord | None = None,
        max_reads: int = 40,
    ) -> None:
        self.workbook = workbook
        self.workbook_record = workbook_record
        self._record_sheets = {
            sheet.sheet_name: sheet for sheet in (workbook_record.sheets if workbook_record else [])
        }
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
        self._header_value_cache: dict[str, list[str]] = {}

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
            "ingestion_warnings": list(
                getattr(self.workbook, "compatibility_warnings", [])
            ),
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
        """Accept verified classifications and hand back period instructions.

        The schema deterministically rejects any inclusion/role contradiction.
        This layer additionally rejects sheet names that are not in the workbook.

        The period instructions are withheld until now. When both jobs were
        described up front the model went straight for the periods and treated
        routing as whatever fell out of them. Ordering the phases keeps sheet
        classification grounded in inspected content.
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
        summaries = self._routed_sheets("summary_p_and_l")
        departments = self._routed_sheets("department_p_and_l")
        return {
            "ok": True,
            "accepted": True,
            "routed_sheets": len(routing.sheets),
            # Named explicitly rather than left to the model to re-derive: these
            # are the sheets periods must be confirmed against, and the count is
            # the one `submit_periods` will hold it to.
            "financial_sheets": routing.financial_sheet_names,
            "summary_candidates": summaries,
            "department_sheets": departments,
            "required_department_reads": min(
                self.MAX_DEPARTMENT_SHEETS_TO_CONFIRM,
                len(departments),
            ),
            "must_read_at_least": required,
            "already_read": sorted(self.read_sheets & set(routing.financial_sheet_names)),
            "next_phase": _period_detection_instructions(),
        }

    def _required_reads(self) -> int:
        """One summary plus up to four normal department schedules."""
        if self.routing is None:
            return 0
        return 1 + min(
            self.MAX_DEPARTMENT_SHEETS_TO_CONFIRM,
            len(self._routed_sheets("department_p_and_l")),
        )

    def _routed_sheets(self, role: str) -> list[str]:
        if self.routing is None:
            return []
        return [
            sheet.sheet_name
            for sheet in self.routing.sheets
            if sheet.include_as_financial_evidence and sheet.role.value == role
        ]

    # -- phase two: periods -----------------------------------------------

    def _submit_periods(self, arguments: dict[str, Any]) -> dict[str, Any]:
        """Accept the periods and end the session.

        The schema is checked first. Then the chosen controlling summary must be
        an included summary P&L that was opened, up to four included department
        P&Ls must have been opened, and every submitted period must cite the
        controlling summary in `sheets_present`. Narrow deterministic checks
        also reject an auxiliary monthly controller over a PTD/YTD family and
        verify one exact current department amount column per period.
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

        summaries = self._routed_sheets("summary_p_and_l")
        anchor = found.controlling_summary_sheet
        if anchor not in summaries:
            routed = next(
                (
                    item
                    for item in self.routing.sheets
                    if item.sheet_name == anchor
                    and item.include_as_financial_evidence
                ),
                None,
            )
            received_role = routed.role.value if routed is not None else "not included"
            message = (
                f"controlling_summary_sheet must name an included summary_p_and_l; "
                f"received {anchor!r} with role {received_role!r}. Candidates: "
                f"{', '.join(summaries) or 'none'}."
            )
            self.rejections.append(message)
            return {
                "ok": True,
                "accepted": False,
                "error": message,
                "instruction": (
                    "Choose the controlling core summary and resubmit. If this is "
                    "a hotel-wide statement that routing misclassified, correct its "
                    "role with submit_routing first; do not call a hotel-wide monthly "
                    "snapshot a department merely to change the controller."
                ),
            }
        if anchor not in self.read_sheets:
            message = (
                f"Open the header rows of controlling summary {anchor!r} before "
                "submitting periods."
            )
            self.rejections.append(message)
            return {
                "ok": True,
                "accepted": False,
                "error": message,
                "instruction": "Read that sheet's header block and resubmit.",
            }

        departments = self._routed_sheets("department_p_and_l")
        required_departments = min(
            self.MAX_DEPARTMENT_SHEETS_TO_CONFIRM,
            len(departments),
        )
        seen_departments = [
            name for name in departments if name in self.read_sheets
        ]
        if len(seen_departments) < required_departments:
            outstanding = [
                name for name in departments if name not in self.read_sheets
            ]
            message = (
                f"Only {len(seen_departments)} normal department P&L sheet(s) "
                f"have been opened; confirm the controlling periods against "
                f"{required_departments}. Read header rows from: "
                f"{', '.join(outstanding[:10])}."
            )
            self.rejections.append(message)
            return {
                "ok": True,
                "accepted": False,
                "error": message,
                "read_so_far": seen_departments,
                "instruction": "Read those department headers and resubmit.",
            }

        controller_problem = self._controller_layout_problem(
            anchor, seen_departments
        )
        if controller_problem is not None:
            self.rejections.append(controller_problem["error"])
            return controller_problem

        explicit_months = explicit_month_years(self._header_values(anchor))
        if len(explicit_months) >= MONTHLY_SPREAD_THRESHOLD:
            submitted_months = {
                period.start_month
                for period in found.periods
                if period.start_month == period.end_month
            }
            missing_months = sorted(explicit_months - submitted_months)
            if missing_months:
                message = (
                    "The controlling summary has an explicit monthly spread, but the "
                    "period catalog omits displayed months. Return every monthly amount "
                    "period and keep any displayed annual/TTM Total as an additional "
                    f"aggregate. Missing month headers: {missing_months}."
                )
                self.rejections.append(message)
                return {
                    "ok": True,
                    "accepted": False,
                    "error": message,
                    "instruction": "Add the controlling summary's displayed months and resubmit.",
                }

        periods, corrected = self._with_true_evidence(found.periods)
        unanchored = [
            period.period_id
            for period in periods
            if anchor not in period.sheets_present
        ]
        if unanchored:
            message = (
                "Every selectable period must appear on the controlling summary. "
                "Remove periods introduced only by auxiliary T12, monthly, trend, "
                f"or supporting tabs: {', '.join(unanchored)}."
            )
            self.rejections.append(message)
            return {
                "ok": True,
                "accepted": False,
                "error": message,
                "instruction": "Resubmit only periods anchored on the controlling summary.",
            }

        unsupported = self._unsupported_core_periods(
            periods,
            departments,
            controller_as_of=latest_header_month(self._header_values(anchor)),
        )
        if unsupported:
            details = "; ".join(
                f"{period_id}: {reason}" for period_id, reason in unsupported.items()
            )
            message = (
                "These proposed periods were not confirmed on a normal, populated "
                f"department P&L with matching scenario and coverage: {details}"
            )
            self.rejections.append(message)
            return {
                "ok": True,
                "accepted": False,
                "error": message,
                "instruction": (
                    "If the controlling summary belongs to a recurring statement "
                    "series, read its latest/current member and a department member "
                    "with the same reporting end date. Resubmit only periods confirmed "
                    "there. Empty optional template tabs do not need to confirm a period."
                ),
            }
        notes = [
            *getattr(self.workbook, "compatibility_warnings", []),
            *self.routing.notes,
            *found.notes,
        ]
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
            controlling_summary_sheet=anchor,
            periods=periods,
            notes=notes,
        )
        return {
            "ok": True,
            "accepted": True,
            "structure": self.submission.model_dump(mode="json"),
        }

    def _header_values(self, sheet_name: str) -> list[str]:
        """Header text used only by deterministic layout checks."""

        if sheet_name not in self._header_value_cache:
            self._header_value_cache[sheet_name] = [
                cell.value
                for row in self.workbook.read_rows(
                    sheet_name, 1, self.CONTROLLING_HEADER_ROWS
                )
                for cell in row
            ]
        return self._header_value_cache[sheet_name]

    def _controller_layout_problem(
        self, anchor: str, seen_departments: list[str]
    ) -> dict[str, Any] | None:
        """Reject a one-off monthly spread over a recurring PTD/YTD family.

        This is deliberately narrower than generic layout matching. Summary and
        department schedules often differ cosmetically. The unsafe case is the
        one observed on CMI and Pontchartrain: a wide Jan-Dec/T12-style summary
        is chosen even though the operating statement family is PTD/YTD.
        """

        if (
            self.routing is None
            or self.routing.workbook_layout.value != "multi_tab_department_p_and_l"
        ):
            return None
        anchor_kind = period_layout_kind(self._header_values(anchor))
        department_kinds = Counter(
            period_layout_kind(self._header_values(name))
            for name in seen_departments
        )
        if (
            anchor_kind != "monthly_spread"
            or department_kinds["ptd_ytd"] <= department_kinds["monthly_spread"]
        ):
            return None

        compatible = [
            name
            for name in self._routed_sheets("summary_p_and_l")
            if period_layout_kind(self._header_values(name)) == "ptd_ytd"
        ]
        dated = [
            (latest_header_month(self._header_values(name)), index, name)
            for index, name in enumerate(compatible)
        ]
        latest = max(
            (item for item in dated if item[0] is not None),
            default=None,
        )
        latest_hint = latest[2] if latest is not None else None
        message = (
            f"{anchor!r} is a wide monthly/annual summary, while the normal "
            "department schedules inspected use a PTD/YTD layout. It is an "
            "auxiliary presentation for period discovery and cannot control the "
            "catalog in this workbook."
        )
        if latest_hint:
            message += f" The latest compatible PTD/YTD summary appears to be {latest_hint!r}."
        elif compatible:
            message += " Compatible PTD/YTD summaries include: " + ", ".join(compatible[:8]) + "."
        return {
            "ok": True,
            "accepted": False,
            "error": message,
            "instruction": (
                "Read the latest/current PTD/YTD summary and a normal department "
                "schedule with the same ending date, then derive the selectable "
                "PTD/YTD Actual, Budget, Forecast, and Prior/Last-Year periods from "
                "that core layout."
            ),
        }

    def _unsupported_core_periods(
        self,
        periods: list[DiscoveredPeriod],
        departments: list[str],
        *,
        controller_as_of: tuple[int, int] | None,
    ) -> dict[str, str]:
        """Require per-period confirmation without intersecting every tab.

        Repeating monthly snapshots are one schedule family, so a period need
        not appear on every historical member. One current, normal department
        statement with the same period is sufficient. A fully empty outlet or
        optional template contributes neither support nor a veto.
        """

        if (
            self.routing is None
            or self.routing.workbook_layout.value != "multi_tab_department_p_and_l"
            or not departments
            or not periods
        ):
            return {}

        latest_year = max(int(period.end_month[:4]) for period in periods)
        unsupported: dict[str, str] = {}
        department_set = set(departments)
        for period in periods:
            confirmation = period.department_confirmation
            if confirmation is None:
                unsupported[period.period_id] = (
                    "no exact department_confirmation was supplied"
                )
                continue
            name = confirmation.sheet_name
            if name not in department_set:
                unsupported[period.period_id] = (
                    f"{name!r} is not a routed department_p_and_l"
                )
                continue
            if name not in self.read_sheets or name not in period.sheets_present:
                unsupported[period.period_id] = (
                    f"{name!r} must be opened and included in sheets_present"
                )
                continue
            if not self._record_sheets:
                continue
            sheet = self._record_sheets.get(name)
            if sheet is None:
                unsupported[period.period_id] = f"{name!r} is absent from ingestion"
                continue
            problem = period_column_problem(
                sheet,
                period,
                confirmation.excel_column,
                latest_period_year=latest_year,
                controller_as_of=controller_as_of,
            )
            if problem:
                unsupported[period.period_id] = (
                    f"{name!r} {confirmation.excel_column.upper()}: {problem}"
                )
        return unsupported

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
