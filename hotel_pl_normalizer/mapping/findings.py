"""Typed validation findings with a stable legacy-string boundary.

``Finding`` subclasses :class:`str` deliberately.  Existing model-tool responses,
saved run logs, and third-party callers therefore keep receiving the exact
``severity|rule|target|...`` representation, while live code can use structured
attributes without repeatedly parsing and rebuilding that text.
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping, Sequence
from typing import Any, Literal, TypeAlias

Severity: TypeAlias = Literal["info", "warning", "error"]
DetailValue: TypeAlias = str | int | float | bool | None
_UNCHANGED = object()
_NUMBER = re.compile(r"^[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?$")


def _typed_detail(value: str) -> DetailValue:
    text = value.strip()
    lowered = text.casefold()
    if lowered == "true":
        return True
    if lowered == "false":
        return False
    if lowered in {"inf", "+inf", "infinity", "+infinity"}:
        return math.inf
    if lowered in {"-inf", "-infinity"}:
        return -math.inf
    if _NUMBER.fullmatch(text):
        try:
            return int(text) if not any(mark in text for mark in ".eE") else float(text)
        except ValueError:
            pass
    return text


def _render_detail(value: DetailValue, format_spec: str | None) -> str:
    if format_spec and isinstance(value, (int, float)) and not isinstance(value, bool):
        return format(value, format_spec)
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return "null"
    return str(value)


class Finding(str):
    """One deterministic mapping finding plus its legacy representation."""

    severity: Severity
    rule: str
    target: str
    period_id: str | None
    details: Mapping[str, DetailValue]
    note: str | None
    review_item_id: str | None

    def __new__(
        cls,
        severity: Severity,
        rule: str,
        target: str,
        details: Mapping[str, DetailValue] | None = None,
        note: str | None = None,
        period_id: str | None = None,
        review_item_id: str | None = None,
        *,
        detail_formats: Mapping[str, str] | None = None,
        legacy_parts: Sequence[str] | None = None,
        legacy_prefix: str = "",
    ) -> Finding:
        if severity not in {"info", "warning", "error"}:
            raise ValueError(f"unsupported finding severity {severity!r}")
        if not str(rule).strip():
            raise ValueError("finding rule cannot be blank")
        if not str(target).strip():
            raise ValueError("finding target cannot be blank")

        typed_details = dict(details or {})
        formats = dict(detail_formats or {})
        if legacy_parts is None:
            parts = [
                f"{key}={_render_detail(value, formats.get(key))}"
                for key, value in typed_details.items()
            ]
            if note:
                parts.append(str(note))
        else:
            parts = [str(part) for part in legacy_parts if str(part) != ""]
        base = "|".join([str(severity), str(rule), str(target), *parts])
        value = f"{legacy_prefix}{base}"
        instance = str.__new__(cls, value)
        instance.severity = severity
        instance.rule = str(rule)
        instance.target = str(target)
        instance.period_id = period_id
        instance.details = typed_details
        instance.note = str(note) if note is not None else None
        instance.review_item_id = review_item_id
        instance._detail_formats = formats
        instance._legacy_parts = tuple(parts)
        instance._legacy_prefix = legacy_prefix
        return instance

    def __init__(
        self,
        severity: Severity,
        rule: str,
        target: str,
        details: Mapping[str, DetailValue] | None = None,
        note: str | None = None,
        period_id: str | None = None,
        review_item_id: str | None = None,
        *,
        detail_formats: Mapping[str, str] | None = None,
        legacy_parts: Sequence[str] | None = None,
        legacy_prefix: str = "",
    ) -> None:
        # All immutable string construction happens in __new__.
        del severity, rule, target, details, note, period_id, review_item_id
        del detail_formats, legacy_parts, legacy_prefix

    @classmethod
    def from_legacy(
        cls,
        value: str,
        *,
        period_id: str | None = None,
        review_item_id: str | None = None,
    ) -> Finding:
        """Parse old run-log text once at its compatibility boundary."""

        if isinstance(value, cls):
            if period_id is None and review_item_id is None:
                return value
            return value.with_updates(
                period_id=value.period_id if period_id is None else period_id,
                review_item_id=(
                    value.review_item_id
                    if review_item_id is None
                    else review_item_id
                ),
            )
        text = str(value)
        positions = [
            position
            for marker in ("error|", "warning|", "info|")
            if (position := text.find(marker)) >= 0
        ]
        if not positions:
            raise ValueError(f"not a legacy finding: {text!r}")
        position = min(positions)
        prefix = text[:position]
        parts = text[position:].split("|")
        if len(parts) < 3:
            raise ValueError(f"incomplete legacy finding: {text!r}")
        severity = parts[0].strip().lower()
        body = parts[3:]
        details: dict[str, DetailValue] = {}
        notes = []
        for part in body:
            if "=" in part:
                key, raw = part.split("=", 1)
                details[key.strip()] = _typed_detail(raw)
            elif part:
                notes.append(part)
        inferred_period = period_id
        if inferred_period is None and prefix.strip().endswith(":"):
            inferred_period = prefix.strip()[:-1].strip() or None
        return cls(
            severity,  # type: ignore[arg-type]
            parts[1].strip(),
            parts[2].strip(),
            details,
            "|".join(notes) or None,
            inferred_period,
            review_item_id,
            legacy_parts=body,
            legacy_prefix=prefix,
        )

    @classmethod
    def from_value(
        cls,
        value: Finding | str | Mapping[str, Any],
        *,
        period_id: str | None = None,
    ) -> Finding:
        if isinstance(value, cls):
            return value if period_id is None else value.with_updates(period_id=period_id)
        if isinstance(value, Mapping) and {"severity", "rule", "target"} <= value.keys():
            legacy = value.get("legacy")
            parsed = cls.from_legacy(str(legacy)) if legacy else None
            return cls(
                value["severity"],
                str(value["rule"]),
                str(value["target"]),
                value.get("details") or {},
                value.get("note"),
                period_id if period_id is not None else value.get("period_id"),
                value.get("review_item_id"),
                legacy_parts=parsed._legacy_parts if parsed is not None else None,
                legacy_prefix=parsed._legacy_prefix if parsed is not None else "",
            )
        return cls.from_legacy(str(value), period_id=period_id)

    def to_legacy_string(self) -> str:
        return str(self)

    def to_dict(self) -> dict[str, Any]:
        return {
            "severity": self.severity,
            "rule": self.rule,
            "target": self.target,
            "period_id": self.period_id,
            "details": dict(self.details),
            "note": self.note,
            "review_item_id": self.review_item_id,
            "legacy": self.to_legacy_string(),
        }

    def with_updates(
        self,
        *,
        severity: Severity | object = _UNCHANGED,
        rule: str | object = _UNCHANGED,
        target: str | object = _UNCHANGED,
        details: Mapping[str, DetailValue] | object = _UNCHANGED,
        note: str | None | object = _UNCHANGED,
        period_id: str | None | object = _UNCHANGED,
        review_item_id: str | None | object = _UNCHANGED,
        append_note: str | None = None,
        legacy_prefix: str | object = _UNCHANGED,
    ) -> Finding:
        """Return a validated replacement without string surgery."""

        new_details = self.details if details is _UNCHANGED else details
        new_note = self.note if note is _UNCHANGED else note
        body_unchanged = details is _UNCHANGED and note is _UNCHANGED
        parts = list(self._legacy_parts) if body_unchanged else None
        if append_note:
            parts = list(parts or [])
            parts.append(str(append_note))
            new_note = "|".join(filter(None, [str(new_note or ""), str(append_note)]))
        return Finding(
            self.severity if severity is _UNCHANGED else severity,  # type: ignore[arg-type]
            self.rule if rule is _UNCHANGED else str(rule),
            self.target if target is _UNCHANGED else str(target),
            new_details,  # type: ignore[arg-type]
            new_note,  # type: ignore[arg-type]
            self.period_id if period_id is _UNCHANGED else period_id,  # type: ignore[arg-type]
            (
                self.review_item_id
                if review_item_id is _UNCHANGED
                else review_item_id
            ),  # type: ignore[arg-type]
            detail_formats=self._detail_formats,
            legacy_parts=parts,
            legacy_prefix=(
                self._legacy_prefix
                if legacy_prefix is _UNCHANGED
                else str(legacy_prefix)
            ),
        )

    def downgrade(
        self,
        *,
        rule: str | None = None,
        note: str | None = None,
        review_item_id: str | None | object = _UNCHANGED,
    ) -> Finding:
        """Safely change a blocking finding into a warning."""

        if self.severity != "error":
            raise ValueError("only an error finding can be downgraded")
        return self.with_updates(
            severity="warning",
            rule=rule or self.rule,
            review_item_id=review_item_id,
            append_note=note,
        )

    def with_period_label(
        self,
        period_id: str,
        period_label: str,
        *,
        as_prefix: bool,
    ) -> Finding:
        """Attach a period while preserving the historical rendered shape."""

        if as_prefix:
            return self.with_updates(
                period_id=period_id,
                legacy_prefix=f"{period_label}: ",
            )
        details = dict(self.details)
        details["period"] = period_label
        parts = [*self._legacy_parts, f"period={period_label}"]
        return Finding(
            self.severity,
            self.rule,
            self.target,
            details,
            self.note,
            period_id,
            self.review_item_id,
            detail_formats=self._detail_formats,
            legacy_parts=parts,
        )


def ensure_finding(value: Finding | str, *, period_id: str | None = None) -> Finding:
    """Keep live findings typed and parse only old string inputs."""

    return Finding.from_value(value, period_id=period_id)
