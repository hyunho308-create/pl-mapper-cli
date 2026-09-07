"""Read saved artifacts and prepare a compact review without model calls or writes.

Unassigned rows and missing child values are candidates, not proof of an error.
"""

from __future__ import annotations

import json
import math
from collections import defaultdict
from pathlib import Path
from time import perf_counter
from typing import Any

from hotel_pl_normalizer.evaluation.evidence import build_evidence_index
from hotel_pl_normalizer.evaluation.mechanical import run_mechanical_checks
from hotel_pl_normalizer.evaluation.models import (
    EvaluationResult,
    MechanicalCheck,
    MechanicalStatus,
)
from hotel_pl_normalizer.mapping.coa import children_by_parent, load_coa
from hotel_pl_normalizer.mapping.findings import ensure_finding


def evaluate_run(
    source_path: str | Path,
    run_log_path: str | Path,
    mapped_workbook_path: str | Path | None,
    *,
    expected_period_ids: list[str] | None = None,
) -> EvaluationResult:
    """Check a saved run. Optional expected IDs come from the original request."""
    started = perf_counter()
    source, log_path = Path(source_path), Path(run_log_path)
    result = EvaluationResult(source_name=source.name)
    try:
        log = json.loads(log_path.read_text(encoding="utf-8-sig"))
        index = build_evidence_index(log)
    except (OSError, UnicodeError, ValueError, TypeError) as exc:
        result.evidence_complete = False
        result.mechanical_checks.append(
            MechanicalCheck(
                code="run_log_integrity",
                status=MechanicalStatus.FAIL,
                message=f"Cannot prepare evidence: {exc}",
                evidence=[str(log_path)],
            )
        )
    else:
        result.mechanical_checks = run_mechanical_checks(
            source,
            log_path,
            mapped_workbook_path,
            run_log=log,
            evidence_index=index,
        )
        result.evidence_complete = all(
            check.status == MechanicalStatus.PASS
            for check in result.mechanical_checks
            if check.code != "output_log_correlation"
        )
        try:
            _collect_review(log, result, expected_period_ids or [])
        except (KeyError, TypeError, ValueError, AttributeError) as exc:
            result.evidence_complete = False
            result.mechanical_checks.append(
                MechanicalCheck(
                    code="review_evidence",
                    status=MechanicalStatus.FAIL,
                    message=f"Review evidence is incomplete: {exc}",
                )
            )
    result.elapsed_seconds = perf_counter() - started
    return result


def _collect_review(log: dict, result: EvaluationResult, expected: list[str]) -> None:
    coa = load_coa()
    children = children_by_parent(coa)
    accounts = {a["coa_id"]: a for a in log["accounts"]}
    rows = {r["row_key"]: r for r in log["evidence_rows"]}
    result.periods = log["source"]["periods"]
    periods = [p["period_id"] for p in result.periods]
    values = log["values_by_period"]
    leaves = {
        cid
        for cid, meta in coa.items()
        if meta["parent_coa_id"] and cid not in children
    }
    usage: dict[str, set[str]] = defaultdict(set)
    needed: set[str] = set()
    definitions: set[str] = set()

    def add(
        code: str,
        message: str,
        *,
        severity: str = "warning",
        targets=(),
        period_ids=(),
        refs=(),
        detail: Any = None,
        origin: str = "local",
    ) -> None:
        references = list(dict.fromkeys(refs))
        needed.update(references)
        result.findings.append(
            {
                "code": code,
                "severity": severity,
                "message": message,
                "targets": list(targets),
                "periods": list(period_ids),
                "refs": references,
                "detail": detail,
                "origin": origin,
            }
        )

    dropped = log.get("dropped_periods", {})
    for pid in dict.fromkeys([*expected, *dropped]):
        if pid in dropped or pid not in periods:
            add(
                "missing_period",
                str(dropped.get(pid) or "Requested period is absent."),
                severity="error",
                period_ids=[pid],
            )
    if log["outcome"].get("accepted") is False:
        add(
            "run_not_accepted",
            str(
                log["outcome"].get("stopped_reason")
                or "The normalization did not finish successfully."
            ),
            severity="error",
        )
    for pid in periods:
        if not any(_nonzero(v) for v in values.get(pid, {}).values()):
            add(
                "empty_period",
                "Output period has no nonzero values.",
                severity="error",
                period_ids=[pid],
            )

    # Same-label rows are candidates only; matching text does not prove scope.
    by_label: dict[tuple, list[dict]] = defaultdict(list)
    for row in rows.values():
        by_label[_label_key(row)].append(row)
    for cid, account in accounts.items():
        refs = _refs(account.get("source_rows", []))
        excluded = _refs(account.get("excluded_rows", []))
        operation = account.get("operation", "")
        active = operation != "no_value" and bool(refs)
        if active:
            needed.update([*refs, *excluded])
        for key in refs:
            usage[key].add(
                "child"
                if cid in leaves and active
                else "parent"
                if active
                else "no_value"
            )
        for key in excluded:
            usage[key].add(
                "child subtraction"
                if active and cid in leaves
                else "parent subtraction"
            )
        mapped = [values.get(pid, {}).get(cid) for pid in periods]
        if cid not in coa:
            add(
                "unknown_account",
                "Account is absent from the current COA.",
                severity="error",
                targets=[cid],
                refs=refs,
            )
        if cid not in leaves or not (active or any(_nonzero(v) for v in mapped)):
            continue
        needed.update([*refs, *excluded])
        parent = coa[cid]["parent_coa_id"]
        definitions.update([parent, *children[parent]])
        result.children.append(
            {
                "coa_id": cid,
                "parent": parent,
                "values": mapped,
                "operation": operation,
                "scale": account.get("scale_factor"),
                "venue": account.get("venue_name"),
                "refs": refs,
                "excluded": excluded,
            }
        )
        if not refs and any(_nonzero(v) for v in mapped):
            add(
                "child_without_rows",
                "Populated child has no direct source rows; inspect its operation and residuals.",
                targets=[cid],
            )
        for pid, value in zip(periods, mapped):
            nonzero_refs = [
                key
                for key in refs
                if key in rows and _nonzero(_row_value(rows[key], pid))
            ]
            if value is None and nonzero_refs:
                add(
                    "period_value_missing",
                    "Child is blank despite nonzero cited source amounts.",
                    targets=[cid],
                    period_ids=[pid],
                    refs=nonzero_refs,
                )
            elif (
                value == 0
                and operation == "direct"
                and len(refs) == 1
                and nonzero_refs
                and not excluded
            ):
                add(
                    "zero_from_nonzero_source",
                    "Direct child is zero despite a nonzero source amount.",
                    targets=[cid],
                    period_ids=[pid],
                    refs=nonzero_refs,
                )
            elif value is None and refs:
                alternatives = [
                    r["row_key"]
                    for key in refs
                    if key in rows
                    for r in by_label[_label_key(rows[key])]
                    if r["row_key"] not in refs and _nonzero(_row_value(r, pid))
                ]
                if alternatives:
                    add(
                        "period_detail_candidate",
                        "Same-label rows on this sheet/page contain the missing period; verify their scope.",
                        targets=[cid],
                        period_ids=[pid],
                        refs=alternatives,
                    )

    for parent, child_ids in children.items():
        for pid in periods:
            period_values = values.get(pid, {})
            amount = period_values.get(parent)
            count = sum(_nonzero(period_values.get(cid)) for cid in child_ids)
            if not _nonzero(amount) and not count:
                continue
            declaration = accounts.get(parent, {}).get("child_coverage")
            result.coverage.append(
                {
                    "parent": parent,
                    "period": pid,
                    "value": amount,
                    "populated": count,
                    "total": len(child_ids),
                    "declared": declaration,
                }
            )
            if _nonzero(amount) and not count:
                add(
                    "parent_without_detail",
                    "Parent has a value and no populated children; check whether the source provides detail.",
                    targets=[parent],
                    period_ids=[pid],
                    refs=_refs(accounts.get(parent, {}).get("source_rows", [])),
                )
    for pid, plugs in log.get("residual_plugs_by_period", {}).items():
        for cid, amount in plugs.items():
            if _nonzero(amount):
                add(
                    "residual_plug",
                    "Inspect the amount allocated to a residual.",
                    targets=[cid],
                    period_ids=[pid],
                    detail=amount,
                )

    _collect_findings(log, accounts, add)
    for note in log.get("review_items", []):
        refs = _refs(note.get("source_rows", []))
        # These claims explain exclusions but do not suppress gap candidates.
        result.context_notes.append({"message": note.get("message"), "refs": refs})
        needed.update(refs)
        if note.get("requires_human_decision"):
            add(
                "human_decision_required",
                note.get("message", "Unresolved review item."),
                severity="error",
                targets=note.get("coa_ids", []),
                refs=refs,
            )

    # One shared source table includes unused rows. R IDs avoid repeated long
    # sheet names. Preserve zero and missing separately; never truncate rows.
    context: dict[tuple, str] = {}
    for key, row in rows.items():
        row_values = [_row_value(row, pid) for pid in periods]
        scope = _label_key(row)[:2]
        label = str(row.get("label") or "")
        if not any(_number(v) for v in row_values) and label:
            context[scope] = label
        if key not in needed and not any(_nonzero(v) for v in row_values):
            continue
        row_usage = usage.get(key, set())
        candidate = any(_nonzero(v) for v in row_values) and not (
            row_usage & {"child", "child subtraction"}
        )
        result.rows.append(
            {
                "key": key,
                "label": label,
                "values": row_values,
                "context": context.get(scope, ""),
                "usage": ",".join(sorted(row_usage)) or "unused",
                "candidate": candidate,
                "anchors": [_anchor(row, pid) for pid in periods],
            }
        )
    missing = needed - rows.keys()
    if missing:
        result.evidence_complete = False
        add(
            "missing_source_references",
            "Some review references are absent from saved evidence.",
            severity="error",
            refs=sorted(missing),
        )
    for finding in result.findings:
        definitions.update(cid for cid in finding["targets"] if cid in coa)
    definitions.update(
        item["parent"] for item in result.coverage if item["parent"] in coa
    )
    result.definitions = [
        {
            "coa_id": cid,
            "name": meta["account_name"],
            "parent": meta["parent_coa_id"],
            "note": meta["mapping_note"],
        }
        for cid, meta in coa.items()
        if cid in definitions
    ]


def _collect_findings(log: dict, accounts: dict, add) -> None:
    """Keep explanations AND raw errors; remove exact duplicates."""
    seen = set()

    def emit(item: dict, origin: str) -> None:
        if item.get("severity") not in {"warning", "error"}:
            return
        signature = json.dumps(
            {k: v for k, v in item.items() if k != "legacy"}, sort_keys=True
        )
        if signature in seen:
            return
        seen.add(signature)
        targets = item.get("affected_coa_ids") or [
            item.get("target") or item.get("primary_coa_id")
        ]
        targets = [cid for cid in targets if cid]
        refs = _refs(item.get("source_refs", []))
        for cid in targets:
            refs.extend(_refs(accounts.get(cid, {}).get("source_rows", [])))
            refs.extend(_refs(accounts.get(cid, {}).get("excluded_rows", [])))
        periods = [
            p["period_id"] for p in item.get("periods", []) if p.get("period_id")
        ]
        if item.get("period_id"):
            periods.append(item["period_id"])
        add(
            item.get("finding_id") or item.get("rule") or "finding",
            item.get("explanation")
            or item.get("note")
            or item.get("rule", "Unexplained finding"),
            severity=item["severity"],
            targets=targets,
            refs=refs,
            period_ids=list(dict.fromkeys(periods)),
            detail=item.get("details", item.get("periods")),
            origin=origin,
        )

    for item in (log.get("feedback_manifest") or {}).get("findings", []):
        emit(item, "feedback")
    for item in log.get("findings", []):
        emit(item, "validator")
    for pid, items in log.get("findings_by_period", {}).items():
        for item in items:
            emit({**item, "period_id": item.get("period_id") or pid}, "validator")
    checks = [(None, item) for item in log.get("checks", [])]
    checks += [
        (pid, item)
        for pid, items in log.get("checks_by_period", {}).items()
        for item in items
    ]
    for pid, item in checks:
        try:
            emit(ensure_finding(item, period_id=pid).to_dict(), "validator")
        except (TypeError, ValueError):
            add("unparsed_check", str(item), origin="validator")
    errors = [(None, item) for item in log.get("execution_issues", [])]
    errors += [
        (pid, item)
        for pid, items in log.get("execution_issues_by_period", {}).items()
        for item in items
    ]
    for pid, item in dict.fromkeys(errors):
        add(
            "execution_error",
            str(item),
            severity="error",
            period_ids=[pid] if pid else [],
            origin="execution",
        )


def _refs(items) -> list[str]:
    return [
        str(item.get("row_key")) if isinstance(item, dict) else str(item)
        for item in items
        if item and (not isinstance(item, dict) or item.get("row_key"))
    ]


def _number(value) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
    )


def _nonzero(value) -> bool:
    return _number(value) and value != 0


def _row_value(row: dict, pid: str):
    return row.get("selected_values", {}).get(pid)


def _label_key(row: dict) -> tuple:
    locator = row.get("locator", {})
    return (
        locator.get("kind"),
        locator.get("sheet_name", locator.get("page_number")),
        " ".join(str(row.get("label") or "").casefold().split()),
    )


def _anchor(row: dict, pid: str) -> str:
    anchor = row.get("anchors_by_period", {}).get(pid) or {}
    return str(anchor.get("display") or anchor.get("excel_column") or "?")
