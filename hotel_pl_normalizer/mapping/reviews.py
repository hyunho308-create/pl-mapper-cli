"""Permissive review-item views for typed plans and historical run logs."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping


@dataclass(slots=True)
class ReviewItemView:
    """One normalized, non-validating view of a review item.

    Historical logs may contain kinds unknown to today's model schema. This
    adapter deliberately preserves them instead of trying to revalidate old
    payloads as current model output.
    """

    kind: str
    message: str
    coa_ids: list[str]
    source_rows: list[str]
    selected_source_rows: list[str]
    alternate_source_rows: list[str]
    selected_excluded_rows: list[str]
    alternate_excluded_rows: list[str]
    selected_source_operation: Any
    alternate_source_operation: Any
    requires_human_decision: bool
    review_item_id: str | None
    mapping_treatment: str | None = None
    period_ids: list[str] = field(default_factory=list)


def _field(value: Any, name: str, default=None):
    if isinstance(value, Mapping):
        return value.get(name, default)
    return getattr(value, name, default)


def normalize_review_item(value: Any) -> ReviewItemView:
    """Normalize one current model or legacy dict without rejecting history."""

    return ReviewItemView(
        kind=str(_field(value, "kind", "") or ""),
        message=str(_field(value, "message", "") or ""),
        mapping_treatment=_field(value, "mapping_treatment"),
        period_ids=list(_field(value, "period_ids", []) or []),
        coa_ids=list(_field(value, "coa_ids", []) or []),
        source_rows=list(_field(value, "source_rows", []) or []),
        selected_source_rows=list(
            _field(value, "selected_source_rows", []) or []
        ),
        alternate_source_rows=list(
            _field(value, "alternate_source_rows", []) or []
        ),
        selected_excluded_rows=list(
            _field(value, "selected_excluded_rows", []) or []
        ),
        alternate_excluded_rows=list(
            _field(value, "alternate_excluded_rows", []) or []
        ),
        selected_source_operation=_field(
            value, "selected_source_operation"
        ),
        alternate_source_operation=_field(
            value, "alternate_source_operation"
        ),
        requires_human_decision=bool(
            _field(value, "requires_human_decision", False)
        ),
        review_item_id=(
            str(_field(value, "review_item_id", "") or "") or None
        ),
    )


def normalize_review_items(values: Iterable[Any] | None) -> list[ReviewItemView]:
    """Materialize normalized review views once at a consumer boundary."""

    return [normalize_review_item(value) for value in values or []]
