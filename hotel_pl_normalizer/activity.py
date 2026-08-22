"""Turn the mapper's tool calls into lines a person can follow.

Why this exists
---------------
Mapping can take 10-20 minutes, and for all of it the page could say only
"Mapping the workbook" over a bar that creeps. A long silent wait reads as a
hang, and worse, a run going wrong looks exactly like a run going well until the
workbook arrives.

The mapper already records every tool call it makes. Rendering those is the
cheapest honest progress there is: it reports what the model actually did, not
an estimate of how far along it is. There is no token stream to show instead --
the mapping session is a non-streaming loop of rounds -- and the trace is the
better signal anyway, because it says what the model looked at.

Lines are written for the person who uploaded the workbook, so they name sheets
and rows rather than tools and arguments.
"""

from __future__ import annotations

from typing import Any

# Kept short: this is a feed read at a glance in a 190px box, not a log.
MAX_LINE = 120


def _sheet(arguments: dict[str, Any]) -> str:
    name = str(arguments.get("sheet_name") or "").strip()
    return f'"{name}"' if name else "the workbook"


def _rows(arguments: dict[str, Any]) -> str:
    start, end = arguments.get("start_row"), arguments.get("end_row")
    if start and end:
        return f"rows {start}-{end} of "
    if start:
        return f"rows from {start} of "
    return ""


def describe_tool_call(name: str, arguments: dict[str, Any], ok: bool = True) -> str:
    """One readable sentence for one tool call."""
    arguments = arguments if isinstance(arguments, dict) else {}

    if name == "inspect_workbook":
        line = "Looked over the workbook's sheets"
    elif name == "read_range":
        line = f"Read {_rows(arguments)}{_sheet(arguments)}"
    elif name == "read_nonzero_rows":
        line = f"Read the populated rows of {_sheet(arguments)}"
    elif name == "read_sparse_ranges":
        count = len(arguments.get("ranges") or [])
        line = f"Read {count} section{'' if count == 1 else 's'} of {_sheet(arguments)}"
    elif name == "find_rows":
        query = str(arguments.get("query") or "").strip()
        line = f'Searched for "{query}"' if query else "Searched the workbook"
        if arguments.get("sheet_name"):
            line += f" in {_sheet(arguments)}"
    elif name == "validate_mapping":
        count = len(arguments.get("decisions") or [])
        line = f"Proposed a mapping for {count} accounts" if count else "Proposed a mapping"
    elif name == "patch_mapping":
        count = len(arguments.get("replacements") or [])
        line = (
            f"Revised {count} account{'' if count == 1 else 's'}"
            if count
            else "Revised the mapping"
        )
    else:
        line = name.replace("_", " ").capitalize()

    # Failures matter more than successes here: a run that is going wrong shows
    # up as repeated corrections long before the workbook lands.
    if not ok:
        line += " — did not work, trying again"
    return line[:MAX_LINE]


def describe_round(number: int, limit: int, tool_calls: int) -> str:
    """The header line for one model round."""
    calls = (
        f" · {tool_calls} step{'' if tool_calls == 1 else 's'}" if tool_calls else ""
    )
    return f"Round {number} of {limit}{calls}"


def _clock(seconds: float) -> str:
    """Elapsed time as a person reads it: 45s, 2m 05s, 11m 30s."""
    total = int(seconds)
    if total < 60:
        return f"{total}s"
    return f"{total // 60}m {total % 60:02d}s"


def describe_progress(
    elapsed_seconds: float, output_tokens: int, *, thinking: bool
) -> str:
    """A heartbeat for a round that is still running.

    Rounds reach ten minutes, and until the reply lands there is nothing to
    describe -- the feed used to go silent for that whole time, which is exactly
    when a person starts wondering whether the run has died. A streamed reply
    arrives continuously, so this reports what is happening while it happens.

    `thinking` separates the two halves that feel identical from outside but are
    not: the model reasoning before it commits to anything, and the model writing
    the answer. Only the second produces tokens worth counting, so the count is
    omitted from the first rather than shown as a stalled zero.
    """
    if thinking:
        return f"Thinking · {_clock(elapsed_seconds)}"
    written = f" · {output_tokens:,} tokens" if output_tokens else ""
    return f"Working through the workbook · {_clock(elapsed_seconds)}{written}"


def describe_tool_call_started(name: str) -> str:
    """One line for a tool the model has named but not finished asking for.

    The name arrives well before its arguments -- often a minute before, on a
    long round -- and saying which sheet is about to be read is more useful than
    saying nothing until the whole request has been assembled.
    """
    readable = {
        "inspect_workbook": "Looking over the workbook's sheets",
        "read_range": "Reading part of the workbook",
        "read_nonzero_rows": "Reading the populated rows",
        "read_sparse_ranges": "Reading several sections",
        "find_rows": "Searching the workbook",
        "validate_mapping": "Checking the mapping",
        "patch_mapping": "Revising the mapping",
    }.get(name)
    if readable is None:
        readable = name.replace("_", " ").capitalize()
    return f"{readable}…"[:MAX_LINE]
