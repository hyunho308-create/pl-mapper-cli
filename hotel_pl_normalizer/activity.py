"""Turn model rounds and tool calls into readable CLI progress lines."""

from __future__ import annotations

from typing import Any

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
