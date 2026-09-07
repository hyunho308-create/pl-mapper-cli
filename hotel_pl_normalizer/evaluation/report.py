"""Render the complete review directly as compact Markdown tables."""

from __future__ import annotations

import json
from importlib import resources
from pathlib import Path

from hotel_pl_normalizer.evaluation.models import EvaluationResult, MechanicalStatus


def render_evaluation_markdown(result: EvaluationResult) -> str:
    row_ids = {row["key"]: f"R{i}" for i, row in enumerate(result.rows, 1)}
    period_ids = {p["period_id"]: f"P{i}" for i, p in enumerate(result.periods, 1)}
    account_ids = {
        item["coa_id"]: f"A{i}" for i, item in enumerate(result.definitions, 1)
    }
    scopes = dict.fromkeys(row["key"].rpartition("!")[0] for row in result.rows)
    scope_ids = {scope: f"D{i}" for i, scope in enumerate(scopes, 1)}

    def account(cid):
        return account_ids.get(cid, cid)

    def refs(keys):
        return ",".join(row_ids.get(key, f"MISSING:{key}") for key in keys)

    def amounts(values):
        return " / ".join(_cell(value) for value in values)

    lines = [
        f"# Quick P&L review: {_cell(result.source_name)}",
        "",
        f"Preparation: **{result.status}**. Semantic review: **pending**.",
        f"Local processing: {result.elapsed_seconds:.2f}s. "
        f"Coverage: {len(result.periods)} periods; {len(result.children)} active children; "
        f"{len(result.rows)} source rows; {len(result.findings)} findings/candidates.",
        "",
        resources.files("hotel_pl_normalizer.prompts")
        .joinpath("quick_output_review.md")
        .read_text(encoding="utf-8")
        .strip(),
        "",
        "## Periods",
        "",
        *[
            f"- P{i}: {_cell(p['label'])} ({_cell(p['period_id'])})"
            for i, p in enumerate(result.periods, 1)
        ],
        "",
        "Amounts and source anchors follow this period order; '-' means missing, 0 means zero.",
        "Amounts below come from the saved log; any workbook disagreements appear in mechanical checks.",
        "",
        "## Mechanical checks",
        "",
    ]
    for check in result.mechanical_checks:
        lines.append(
            f"- {check.status.value}: {_cell(check.code)} — {_cell(check.message)}"
        )
        if check.status != MechanicalStatus.PASS:
            lines.extend(f"  - {_cell(value)}" for value in check.evidence)
    _table(
        lines,
        "Period and parent coverage",
        ["Parent", "Period", "Value", "Nonzero children", "Declared coverage"],
        [
            [
                account(item["parent"]),
                period_ids.get(item["period"], item["period"]),
                item["value"],
                f"{item['populated']}/{item['total']}",
                item["declared"],
            ]
            for item in result.coverage
        ],
    )
    _table(
        lines,
        "Findings and gap candidates",
        [
            "ID",
            "Severity / origin / code",
            "Accounts / periods",
            "Evidence",
            "Question or problem",
        ],
        [
            [
                f"F{i}",
                f"{item['severity']} / {item['origin']} / {item['code']}",
                ",".join(
                    [
                        *[account(cid) for cid in item["targets"]],
                        *[period_ids.get(p, p) for p in item["periods"]],
                    ]
                ),
                refs(item["refs"]),
                item["message"]
                + (f" Details: {_detail(item['detail'])}" if item["detail"] else ""),
            ]
            for i, item in enumerate(result.findings, 1)
        ],
    )
    _table(
        lines,
        "Active child mappings (all)",
        [
            "Account",
            "Parent",
            "Values",
            "Operation / scale / venue",
            "Source",
            "Subtract",
        ],
        [
            [
                account(item["coa_id"]),
                account(item["parent"]),
                amounts(item["values"]),
                " / ".join(
                    str(v)
                    for v in (item["operation"], item["scale"], item["venue"])
                    if v is not None
                ),
                refs(item["refs"]),
                refs(item["excluded"]),
            ]
            for item in sorted(
                result.children, key=lambda item: (item["parent"], item["coa_id"])
            )
        ],
    )
    _table(
        lines,
        "Account and sibling definitions",
        ["ID", "Account", "Name", "Parent", "Mapping rule"],
        [
            [
                account(item["coa_id"]),
                item["coa_id"],
                item["name"],
                account(item["parent"]),
                item["note"],
            ]
            for item in result.definitions
        ],
    )
    _table(
        lines,
        "Source sheets/pages",
        ["ID", "Sheet or page (original name)"],
        [[scope_ids[scope], scope] for scope in scopes],
    )
    lines.extend(
        [
            "",
            "Locations D1!5 mean row/line 5 in D1 above. '*' marks a detail candidate, not a proven omission.",
            "A blank heading carries forward the previous heading within that same sheet/page.",
        ]
    )
    source_rows, headings = [], {}
    for item in result.rows:
        scope, _, number = item["key"].rpartition("!")
        heading = item["context"] if headings.get(scope) != item["context"] else ""
        headings[scope] = item["context"]
        source_rows.append(
            [
                row_ids[item["key"]],
                f"{scope_ids[scope]}!{number}",
                item["label"],
                amounts(item["values"]),
                amounts(item["anchors"]),
                item["usage"] + (" *" if item["candidate"] else ""),
                heading,
            ]
        )
    _table(
        lines,
        "Source rows",
        ["ID", "Location", "Label", "Values", "Columns", "Use", "Nearby heading"],
        source_rows,
    )
    _table(
        lines,
        "Recorded source context (claims to assess)",
        ["Evidence", "Note"],
        [[refs(item["refs"]), item["message"]] for item in result.context_notes],
    )
    lines.extend(
        [
            "",
            "END OF REVIEW EVIDENCE. All listed tables are complete; no row limit was applied.",
            "",
        ]
    )
    return "\n".join(lines)


def write_evaluation_report(result: EvaluationResult, output_dir: str | Path) -> Path:
    """Optionally save the same review; never write an intermediate JSON file."""
    root = Path(output_dir)
    root.mkdir(parents=True, exist_ok=True)
    path = root / "EVAL.md"
    path.write_text(render_evaluation_markdown(result), encoding="utf-8")
    return path


def _cell(value) -> str:
    if value is None:
        return "-"
    if isinstance(value, float):
        value = format(value, ".12g")
    return (
        " ".join(str(value).split())
        .replace("|", "&#124;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def _detail(value) -> str:
    if isinstance(value, dict):
        value = {k: v for k, v in value.items() if v is not None}
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _table(lines: list[str], title: str, headers: list[str], rows: list[list]) -> None:
    lines.extend(
        [
            "",
            f"## {title}",
            "",
            "|" + "|".join(headers) + "|",
            "|" + "|".join("---" for _ in headers) + "|",
        ]
    )
    lines.extend("|" + "|".join(_cell(value) for value in row) + "|" for row in rows)
