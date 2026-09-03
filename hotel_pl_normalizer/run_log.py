"""A complete, durable record of one normalization run.

What this is for
----------------
The standardized workbook says what the answer was. It does not say how the system
got there, and that is the part you need when a client asks why a line is what it
is, when you are choosing where to spend the next improvement, or when you want to
know what a run actually cost.

So this records the whole chain, end to end:

    raw rows in  ->  what the model was told  ->  which rows it picked per account
                 ->  what Python computed     ->  what the checks said
                 ->  every model call, its tokens and its latency

Each account's decision is joined back to the *labels and values of the rows it
cites*, so a reviewer reads "cost_of_food = sum of these three lines, which were
called these three things and held these three numbers" without opening the source
workbook at all. That join is the difference between a log you can audit and a log
you can only grep.

The log contains extracted line items, labels, and figures. Treat it as client
financial data and store or delete it under the same policy as the source workbook.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from hotel_pl_normalizer.feedback import compose_result_feedback
from hotel_pl_normalizer.mapping import DETERMINISTIC_SUMMARY_CALCULATIONS
from hotel_pl_normalizer.pipeline import NormalizationResult

LOG_VERSION = 4


def _plain(value: Any) -> Any:
    """Coerce pydantic models and enums into something json.dump accepts."""
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if hasattr(value, "value") and not isinstance(value, (str, int, float, bool)):
        return value.value
    if isinstance(value, dict):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def _token_totals(calls: list[dict]) -> dict[str, int]:
    """Sum the token counters the provider actually returned.

    Only counters present in the responses are summed. Reporting a zero for a
    field the provider never sent would read as "no thinking tokens" rather than
    "not measured", and those are different claims.
    """
    fields = (
        "prompt_token_count",
        "candidates_token_count",
        "total_token_count",
        "cached_content_token_count",
        "cache_write_token_count",
        "thoughts_token_count",
    )
    totals: dict[str, int] = {}
    for field in fields:
        present = [int(call[field]) for call in calls if call.get(field) is not None]
        if present:
            totals[field] = sum(present)
    return totals


def build_run_log(result: NormalizationResult) -> dict[str, Any]:
    """Assemble the full record for one run."""
    feedback_manifest = dict(result.feedback_manifest or {})
    if not feedback_manifest:
        feedback_manifest = compose_result_feedback(result).to_dict()
        result.feedback_manifest = feedback_manifest
    period_values = result.period_values or {"selected": result.values}
    period_labels = result.period_labels or {"selected": result.period_label}
    evidence_by_key = {
        str(row.get("row_key")): row for row in (result.evidence or [])
    }

    accounts = []
    for decision in result.decisions:
        payload = _plain(decision)
        coa_id = str(payload.get("coa_id", ""))
        meta = result.coa.get(coa_id, {})

        # The join that makes this auditable: every cited row_key resolved back to
        # the label and figure the model was looking at when it chose it.
        def resolve(keys: Any) -> list[dict[str, Any]]:
            resolved = []
            for key in keys or []:
                row = evidence_by_key.get(str(key), {})
                item = {
                    "row_key": key,
                    "label": row.get("label"),
                    "value": row.get("selected_value"),
                    "indent": row.get("indent"),
                    "bold": row.get("bold"),
                    "found": bool(row),
                }
                if len(period_values) > 1:
                    item["values"] = row.get("selected_values") or {}
                resolved.append(item)
            return resolved

        accounts.append(
            {
                "coa_id": coa_id,
                "account_name": meta.get("account_name"),
                "department": meta.get("department"),
                "computed_value": result.values.get(coa_id),
                "computed_values": {
                    period_id: values.get(coa_id)
                    for period_id, values in period_values.items()
                },
                "operation": payload.get("operation"),
                "scale_factor": payload.get("scale_factor"),
                "child_coverage": payload.get("child_coverage"),
                "venue_name": payload.get("venue_name"),
                "rationale": payload.get("rationale"),
                "residual_plugs": {
                    period_id: plugs[coa_id]
                    for period_id, plugs in result.residual_plugs_by_period.items()
                    if coa_id in plugs
                },
                "source_rows": resolve(payload.get("source_rows")),
                "excluded_rows": resolve(payload.get("excluded_rows")),
            }
        )

    calculated = {
        coa_id: calculation
        for coa_id, calculation in DETERMINISTIC_SUMMARY_CALCULATIONS.items()
        if coa_id in result.coa
    }
    for coa_id, calculation in calculated.items():
        meta = result.coa[coa_id]
        accounts.append(
            {
                "coa_id": coa_id,
                "account_name": meta.get("account_name"),
                "department": meta.get("department"),
                "computed_value": result.values.get(coa_id),
                "computed_values": {
                    period_id: values.get(coa_id)
                    for period_id, values in period_values.items()
                },
                "operation": "calculated",
                "formula": calculation["formula"],
                "dependencies": list(calculation["dependencies"]),
                "scale_factor": None,
                "child_coverage": "not_applicable",
                "venue_name": None,
                "rationale": calculation["mapped_label"],
                "residual_plugs": {},
                "source_rows": [],
                "excluded_rows": [],
            }
        )

    mapped = {
        str(getattr(decision, "coa_id", "")) for decision in result.decisions
    } | set(calculated)
    unmapped = [
        {"coa_id": coa_id, "account_name": meta.get("account_name")}
        for coa_id, meta in result.coa.items()
        if coa_id not in mapped
    ]

    return {
        "log_version": LOG_VERSION,
        "source": {
            "name": result.source_name,
            "workbook_id": result.workbook_id,
            "period": result.period_label,
            "periods": [
                {"period_id": period_id, "label": period_labels[period_id]}
                for period_id in period_values
            ],
        },
        "outcome": {
            "accepted": result.accepted,
            "classification": result.outcome,
            "stopped_reason": result.stopped_reason,
            "accounts_in_chart": len(result.coa),
            "accounts_populated": result.mapped_account_count,
            "accounts_with_a_decision": len(result.decisions),
            "accounts_calculated_deterministically": len(calculated),
            "accounts_without_a_decision": len(unmapped),
            "checks_raised": len(result.checks),
            "execution_issues": len(result.execution_issues),
            "review_items": len(result.review_items),
            "feedback_findings": int(
                feedback_manifest.get("rendered_count", 0)
            ),
        },
        "latency": {
            "total_ms": result.duration_ms,
            "mapping_session_ms": result.session_ms,
            "mapping_rounds": result.session_calls,
            "mapping_round_ms": list(result.session_call_ms),
            "tool_calls": result.session_tool_calls,
            "hit_round_ceiling": result.session_exhausted,
            "structure_stages": list(result.structure_stages),
        },
        "tokens": _token_totals(result.model_calls),
        "cost": {
            "usd": result.cost_usd,
            "mapping_provider": result.mapping_provider,
            "mapping_model": result.mapping_model,
            **result.cost_details,
        },
        "model_calls": result.model_calls,
        "tool_trace": list(result.tool_trace),
        "mapping_selection": dict(result.mapping_selection),
        # The rows the model was shown, verbatim. This is "what came in raw" as far
        # as the model is concerned -- the workbook has more, but nothing else
        # reached the prompt, so nothing else could have influenced the answer.
        "evidence_rows": list(result.evidence or []),
        "accounts": accounts,
        "accounts_without_a_decision": unmapped,
        "values": dict(result.values),
        "values_by_period": period_values,
        "residual_plugs_by_period": result.residual_plugs_by_period,
        "checks": [_plain(check) for check in result.checks],
        "checks_by_period": result.checks_by_period,
        "execution_issues": list(result.execution_issues),
        "execution_issues_by_period": result.execution_issues_by_period,
        # A run that mapped some of the periods asked for is not the same as one
        # that mapped all of them, and the log is where that difference has to
        # be visible: the values below simply would not mention the missing one.
        "dropped_periods": dict(result.dropped_periods),
        "review_items": [_plain(item) for item in result.review_items],
        "exceptions": list(result.exceptions),
        "feedback_manifest": feedback_manifest,
    }


def write_run_log(result: NormalizationResult, path: Path) -> Path:
    """Write the record as pretty JSON, so it is greppable and diffable."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(build_run_log(result), indent=2, default=str), encoding="utf-8"
    )
    return path
