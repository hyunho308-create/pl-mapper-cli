"""Render the complete review directly as compact Markdown tables."""

from __future__ import annotations

import json
from collections import Counter
from importlib import resources
from pathlib import Path

from hotel_pl_normalizer.evaluation.grouping import source_groups
from hotel_pl_normalizer.evaluation.models import EvaluationResult, MechanicalStatus


def render_evaluation_markdown(result: EvaluationResult) -> str:
    row_ids = {row["key"]: f"R{i}" for i, row in enumerate(result.rows, 1)}
    period_ids = {p["period_id"]: f"P{i}" for i, p in enumerate(result.periods, 1)}
    account_ids = {
        item["coa_id"]: f"A{i}" for i, item in enumerate(result.definitions, 1)
    }
    scopes = dict.fromkeys(row["key"].rpartition("!")[0] for row in result.rows)
    scope_ids = {scope: f"D{i}" for i, scope in enumerate(scopes, 1)}
    rows_by_key = {row["key"]: row for row in result.rows}
    grouped_rows = source_groups(result.rows)
    wording_counts = Counter(
        str(item.get(field) or "") for item in result.definitions
        for field in ("note", "synonyms")
    )
    shared_wording = {
        value: f"N{i}" for i, value in enumerate(
            (value for value, count in wording_counts.items() if count > 1 and len(value) > 30), 1
        )
    }
    inline_keys = {
        item["refs"][0] for item in result.children
        if item["operation"] == "direct" and len(item["refs"]) == 1
        and not item["excluded"]
        and item["refs"][0] in rows_by_key
        and rows_by_key[item["refs"][0]]["values"] == item["values"]
    }
    inline_keys &= {group[0]["key"] for group in grouped_rows if len(group) == 1}
    shown_inline: set[str] = set()

    def account(cid):
        return account_ids.get(cid, cid)

    def refs(keys):
        return ",".join(row_ids.get(key, f"MISSING:{key}") for key in keys)

    def amounts(values):
        return " / ".join(_cell(value) for value in values)

    def source_labels(keys, child=None):
        displays = []
        for key in keys:
            if (
                key in inline_keys and key not in shown_inline
                and child is not None and child["operation"] == "direct"
                and child["refs"] == [key] and not child["excluded"]
                and child["values"] == rows_by_key[key]["values"]
            ):
                row = rows_by_key[key]
                scope, _, number = key.rpartition("!")
                displays.append(
                    f"{row_ids[key]}={scope_ids[scope]}!{number} "
                    f"[{amounts(row['anchors'])}; {row['usage']}] {row['label']}"
                    + (f" @ {row['context']}" if row["context"] else "")
                )
                shown_inline.add(key)
            else:
                displays.append(row_ids.get(key, f"MISSING:{key}"))
        return "; ".join(displays)

    lines = [
        f"# Quick P&L review: {_cell(result.source_name)}",
        "",
        f"Preparation: **{result.status}**. Semantic review: **pending**.",
        f"Local processing: {result.elapsed_seconds:.2f}s. "
        f"Coverage: {len(result.periods)} periods; {len(result.children)} active children; "
        f"{len(result.rows)} source rows ({len(grouped_rows)} distinct content rows); "
        f"{len(result.findings)} grouped findings/candidates "
        f"({sum(item.get('occurrences', 1) for item in result.findings)} recorded occurrences).",
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
    parent_coverage: dict[str, dict[str, dict]] = {}
    for item in result.coverage:
        parent_coverage.setdefault(item["parent"], {})[item["period"]] = item
    _table(
        lines,
        "Period and parent coverage",
        ["Parent", "Values", "Nonzero children", "Declared coverage"],
        [
            [
                account(parent),
                amounts([periods.get(pid, {}).get("value") for pid in period_ids]),
                " / ".join(f"{periods[pid]['populated']}/{periods[pid]['total']}" if pid in periods else "-" for pid in period_ids),
                " / ".join(str(periods[pid]["declared"]) if pid in periods else "-" for pid in period_ids),
            ]
            for parent, periods in parent_coverage.items()
        ],
    )
    lines.extend(["", "A source printed in a child row has exactly that child's period values; its columns, use and heading follow the location. Other sources are expanded below."])
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
                f"{item['severity']} / {','.join(item.get('origins', [item['origin']]))} / "
                f"{','.join(item.get('codes', [item['code']]))} (x{item.get('occurrences', 1)})",
                ",".join(
                    [
                        *[account(cid) for cid in item["targets"]],
                        *[period_ids.get(p, p) for p in item["periods"]],
                    ]
                ),
                refs(item["refs"]),
                item["message"],
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
            "Source and labels",
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
                source_labels(item["refs"], item),
                source_labels(item["excluded"]),
            ]
            for item in sorted(
                result.children, key=lambda item: (item["parent"], item["coa_id"])
            )
        ],
    )
    _table(
        lines,
        "Account and sibling definitions",
        ["ID", "Account", "Name", "Parent", "Mapping rule", "Synonyms"],
        [
            [
                account(item["coa_id"]),
                item["coa_id"],
                item["name"],
                account(item["parent"]),
                shared_wording.get(item["note"], item["note"]),
                shared_wording.get(item.get("synonyms", ""), item.get("synonyms", "")),
            ]
            for item in result.definitions
        ],
    )
    if shared_wording:
        _table(lines, "Shared COA wording", ["ID", "Full mapping rule or synonyms"],
               [[identifier, wording] for wording, identifier in shared_wording.items()])
    _table(
        lines,
        "Finding evidence and distinct explanations",
        ["Issue", "Severity / origin / code", "Accounts / periods", "Evidence", "Explanation / amounts"],
        [
            [f"F{i}", f"{variant['severity']} / {variant['origin']} / {variant['code']}",
             ",".join([*[account(cid) for cid in variant["targets"]],
                       *[period_ids.get(pid, pid) for pid in variant["periods"]]]),
             refs(variant["refs"]),
             (variant["message"] if variant["message"] != item["message"] else "")
             + (f" {_detail(variant['detail'])}" if variant["detail"] is not None else "")]
            for i, item in enumerate(result.findings, 1)
            for variant in _display_variants(item)
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
            "Identical content is printed once with EVERY original R ID/location. This does not prove shared accounting scope.",
            "For repeated rows, semicolon-separated columns and uses follow the listed location order.",
        ]
    )
    source_rows, headings = [], {}
    for group in grouped_rows:
        item = group[0]
        if len(group) == 1 and item["key"] in inline_keys:
            continue
        group_scopes = [row["key"].rpartition("!")[0] for row in group]
        heading = item["context"] if any(headings.get(scope) != item["context"] for scope in group_scopes) else ""
        for scope in group_scopes:
            headings[scope] = item["context"]
        locations = [
            f"{row_ids[row['key']]}={scope_ids[scope]}!{number}"
            for row in group
            for scope, _, number in [row["key"].rpartition("!")]
        ]
        source_rows.append(
            [
                ", ".join(locations),
                item["label"],
                amounts(item["values"]),
                "; ".join(amounts(row["anchors"]) for row in group),
                "; ".join(row["usage"] + (" *" if row["candidate"] else "") for row in group),
                heading,
            ]
        )
    _table(
        lines,
        "Source rows",
        ["ID = location (all)", "Label", "Values", "Columns", "Use", "Nearby heading"],
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


def _display_variants(item: dict) -> list[dict]:
    """Print duplicate raw/typed copies once while retaining their origins."""
    variants: dict[str, dict] = {}
    for variant in item.get("variants", [item]):
        identity = json.dumps(
            {key: variant[key] for key in ("severity", "code", "message", "targets", "periods", "detail", "refs")},
            sort_keys=True,
        )
        if identity in variants:
            previous = variants[identity]
            previous["origin"] = ",".join(dict.fromkeys([*previous["origin"].split(","), variant["origin"]]))
        else:
            variants[identity] = dict(variant)
    return list(variants.values())


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
