"""Lossless presentation grouping, without a model or fuzzy text matching."""

from __future__ import annotations

import json
import math
from collections import defaultdict


def group_findings(items: list[dict]) -> list[dict]:
    """Group repeated issues, retaining every distinct explanation and amount.

    Feedback can absorb a raw check only when its primary account, period,
    quantified comparison and cited source scope agree unambiguously.
    """
    feedback = [item for item in items if item["origin"] == "feedback"]
    groups: dict[tuple, dict] = {}
    for item in items:
        key = item.get("_identity") or (
            item["code"], tuple(sorted(item["targets"])),
            tuple(sorted(item["refs"])), item["message"],
        )
        if item["origin"] in {"validator", "check"}:
            matches = [candidate for candidate in feedback if _matches(item, candidate)]
            identities = {candidate["_identity"] for candidate in matches}
            if len(identities) == 1:
                key = identities.pop()
        group = groups.setdefault(
            key,
            {**{k: v for k, v in item.items() if not k.startswith("_")},
             "periods": [], "targets": [], "refs": [], "origins": [],
             "codes": [], "occurrences": 0, "variants": []},
        )
        ranks = {"info": 0, "warning": 1, "error": 2}
        if ranks.get(item["severity"], 0) > ranks.get(group["severity"], 0):
            group["severity"] = item["severity"]
        for field in ("periods", "targets", "refs"):
            group[field] = list(dict.fromkeys([*group[field], *item[field]]))
        for field, value in (("origins", item["origin"]), ("codes", item["code"])):
            if value not in group[field]:
                group[field].append(value)
        group["occurrences"] += 1
        variant = {k: item[k] for k in ("severity", "origin", "code", "message", "targets", "periods", "detail", "refs")}
        if variant not in group["variants"]:
            group["variants"].append(variant)
    return list(groups.values())


def _matches(raw: dict, feedback: dict) -> bool:
    if not raw.get("_primary") or raw["_primary"] != feedback.get("_primary"):
        return False
    # Different cited alternatives can have equal amounts. Keep these separate.
    if not set(raw.get("_scope", ())) <= set(feedback.get("_scope", ())):
        return False
    comparisons = feedback.get("_comparisons", [])
    raw_detail = raw["detail"]
    if not isinstance(raw_detail, dict) or len(raw["periods"]) != 1:
        return False
    for comparison in comparisons:
        if comparison.get("period_id") != raw["periods"][0]:
            continue
        room_fields = {
            "occupancy_above_capacity": ("occupancy", "rooms_sold", "rooms_available"),
            "invalid_rooms_available": ("rooms_sold", "rooms_available"),
        }.get(raw["code"], ())
        if room_fields and all(
            _same_amount(raw_detail.get(field), comparison.get(field))
            for field in room_fields
        ):
            return True
        pairs = [
            (raw_detail.get("actual", raw_detail.get("parent")), comparison.get("selected_value")),
            (raw_detail.get("expected", raw_detail.get("children")), comparison.get("comparison_value")),
        ]
        if all(_same_amount(a, b) for a, b in pairs):
            return True
        if (
            _same_amount(raw_detail.get("remainder"), comparison.get("amount"))
            and _same_amount(raw_detail.get("ratio"), comparison.get("ratio"))
        ):
            return True
    return False


def _same_amount(left, right) -> bool:
    return (
        isinstance(left, (int, float)) and isinstance(right, (int, float))
        and math.isclose(left, right, rel_tol=0, abs_tol=0.0001)
    )


def source_groups(rows: list[dict]) -> list[list[dict]]:
    """Store repeated source content once, retaining each original location.

    This is text compression only, never a claim that equal rows share scope or
    may be omitted from accounting. Every location keeps its columns and usage;
    different headings or period values stay apart.
    """
    groups: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        signature = json.dumps(
            {k: row[k] for k in ("label", "values", "context")},
            sort_keys=True,
        )
        groups[signature].append(row)
    return list(groups.values())
