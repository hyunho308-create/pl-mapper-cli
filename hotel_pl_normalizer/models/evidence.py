"""Modality-neutral mapper evidence with explicit legacy adapters.

The model still sees the historical ``scope!position`` row keys and dictionaries.
Internally, locators and value anchors retain their real Excel or PDF meaning so
audit code never has to guess a modality from a display string.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, TypeAlias

from openpyxl.utils.cell import get_column_letter

EvidenceScalar: TypeAlias = str | int | float | bool | None


class EvidenceLocatorKind(str, Enum):
    EXCEL = "excel"
    PDF = "pdf"


@dataclass(frozen=True, slots=True)
class ExcelRowLocator:
    sheet_name: str
    row_index: int
    kind: EvidenceLocatorKind = field(
        default=EvidenceLocatorKind.EXCEL,
        init=False,
    )

    def __post_init__(self) -> None:
        if not self.sheet_name:
            raise ValueError("Excel evidence requires a sheet name")
        if self.row_index < 1:
            raise ValueError("Excel evidence row_index must be positive")

    @property
    def legacy_key(self) -> str:
        return f"{self.sheet_name}!{self.row_index}"

    @property
    def identity(self) -> str:
        return f"excel:{self.legacy_key}"

    @property
    def display(self) -> str:
        return f"{self.sheet_name} row {self.row_index}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind.value,
            "identity": self.identity,
            "display": self.display,
            "sheet_name": self.sheet_name,
            "row_index": self.row_index,
        }


@dataclass(frozen=True, slots=True)
class PdfLineLocator:
    page_number: int
    line_number: int
    line_id: str | None = None
    top: float | None = None
    kind: EvidenceLocatorKind = field(
        default=EvidenceLocatorKind.PDF,
        init=False,
    )

    def __post_init__(self) -> None:
        if self.page_number < 1 or self.line_number < 1:
            raise ValueError("PDF evidence page and line numbers must be positive")

    @property
    def legacy_key(self) -> str:
        return f"Page {self.page_number:03d}!{self.line_number}"

    @property
    def identity(self) -> str:
        return f"pdf:page={self.page_number}:line={self.line_number}"

    @property
    def display(self) -> str:
        return f"Page {self.page_number}, line {self.line_number}"

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "kind": self.kind.value,
            "identity": self.identity,
            "display": self.display,
            "page_number": self.page_number,
            "line_number": self.line_number,
        }
        if self.line_id is not None:
            payload["line_id"] = self.line_id
        if self.top is not None:
            payload["top"] = self.top
        return payload


EvidenceLocator: TypeAlias = ExcelRowLocator | PdfLineLocator


@dataclass(frozen=True, slots=True)
class ExcelValueAnchor:
    column_index: int
    excel_column: str | None = None
    kind: EvidenceLocatorKind = field(
        default=EvidenceLocatorKind.EXCEL,
        init=False,
    )

    def __post_init__(self) -> None:
        if self.column_index < 1:
            raise ValueError("Excel evidence column_index must be positive")
        if self.excel_column is None:
            object.__setattr__(self, "excel_column", get_column_letter(self.column_index))

    @property
    def display(self) -> str:
        return str(self.excel_column)

    @property
    def legacy_value(self) -> int:
        return self.column_index

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind.value,
            "column_index": self.column_index,
            "excel_column": self.excel_column,
            "display": self.display,
        }


@dataclass(frozen=True, slots=True)
class PdfValueAnchor:
    right_edge: float
    kind: EvidenceLocatorKind = field(
        default=EvidenceLocatorKind.PDF,
        init=False,
    )

    def __post_init__(self) -> None:
        if self.right_edge <= 0:
            raise ValueError("PDF evidence right_edge must be positive")

    @property
    def display(self) -> str:
        return f"x={self.right_edge:.3f}"

    @property
    def legacy_value(self) -> str:
        return self.display

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind.value,
            "right_edge": self.right_edge,
            "display": self.display,
        }


EvidenceAnchor: TypeAlias = ExcelValueAnchor | PdfValueAnchor


@dataclass(frozen=True, slots=True, eq=False)
class EvidenceRow(Mapping[str, Any]):
    """One source row with stable identity and per-period value anchors."""

    locator: EvidenceLocator
    label: str
    values_by_period: Mapping[str, EvidenceScalar]
    anchors_by_period: Mapping[str, EvidenceAnchor | None]
    primary_period_id: str
    value_formats_by_period: Mapping[str, str | None] = field(default_factory=dict)
    indent: float | None = None
    bold: bool = False
    label_position: int | float | None = None
    label_rule: str = "none"
    label_status: str = "blank"
    label_context: tuple[str, ...] = ()
    _legacy_payload: Mapping[str, Any] | None = field(
        default=None,
        repr=False,
        compare=False,
    )

    def __post_init__(self) -> None:
        if not self.primary_period_id:
            raise ValueError("EvidenceRow requires a primary_period_id")
        object.__setattr__(self, "values_by_period", dict(self.values_by_period))
        object.__setattr__(self, "anchors_by_period", dict(self.anchors_by_period))
        object.__setattr__(
            self,
            "value_formats_by_period",
            dict(self.value_formats_by_period),
        )
        object.__setattr__(self, "label_context", tuple(self.label_context))
        if self._legacy_payload is not None:
            object.__setattr__(self, "_legacy_payload", deepcopy(dict(self._legacy_payload)))

    @property
    def row_key(self) -> str:
        return self.locator.legacy_key

    @property
    def identity(self) -> str:
        return self.locator.identity

    @property
    def display(self) -> str:
        return self.locator.display

    @classmethod
    def from_legacy_dict(
        cls,
        payload: Mapping[str, Any],
        *,
        locator_kind: EvidenceLocatorKind | str | None = None,
        anchors_by_period: Mapping[str, EvidenceAnchor | None] | None = None,
        primary_period_id: str | None = None,
    ) -> EvidenceRow:
        """Parse an old evidence dictionary without changing its legacy shape."""

        source = deepcopy(dict(payload))
        row_key = str(source.get("row_key") or "")
        try:
            scope, raw_position = row_key.rsplit("!", 1)
            position = int(raw_position)
        except (ValueError, AttributeError) as exc:
            raise ValueError(f"invalid legacy evidence row_key {row_key!r}") from exc

        locator_payload = source.get("locator")
        explicit_kind = None
        if isinstance(locator_payload, Mapping):
            explicit_kind = locator_payload.get("kind")
        if locator_kind is not None:
            kind = EvidenceLocatorKind(locator_kind)
        elif explicit_kind is not None:
            kind = EvidenceLocatorKind(str(explicit_kind))
        elif isinstance(source.get("pdf_source"), Mapping):
            # Old PDF rows identify themselves through structured PDF metadata.
            # A scope merely named ``Page NNN`` remains a valid Excel sheet.
            kind = EvidenceLocatorKind.PDF
        else:
            kind = EvidenceLocatorKind.EXCEL

        if kind == EvidenceLocatorKind.PDF:
            pdf_source = source.get("pdf_source")
            pdf_source = pdf_source if isinstance(pdf_source, Mapping) else {}
            locator_values = (
                locator_payload if isinstance(locator_payload, Mapping) else {}
            )
            page_number = int(
                locator_values.get("page_number")
                or pdf_source.get("page")
                or _page_number_from_scope(scope)
            )
            locator: EvidenceLocator = PdfLineLocator(
                page_number=page_number,
                line_number=int(locator_values.get("line_number") or position),
                line_id=(
                    str(locator_values.get("line_id") or pdf_source.get("line_id"))
                    if locator_values.get("line_id") or pdf_source.get("line_id")
                    else None
                ),
                top=_optional_float(locator_values.get("top", pdf_source.get("top"))),
            )
        else:
            locator_values = (
                locator_payload if isinstance(locator_payload, Mapping) else {}
            )
            locator = ExcelRowLocator(
                sheet_name=str(locator_values.get("sheet_name") or scope),
                row_index=int(locator_values.get("row_index") or position),
            )

        values_payload = source.get("selected_values")
        if isinstance(values_payload, Mapping):
            values = dict(values_payload)
        else:
            inferred_primary = primary_period_id or "selected"
            values = {inferred_primary: source.get("selected_value")}
        primary = primary_period_id or next(iter(values), None) or "selected"

        if anchors_by_period is not None:
            anchors = dict(anchors_by_period)
        elif isinstance(source.get("anchors_by_period"), Mapping):
            anchors = {
                str(period_id): _anchor_from_dict(value)
                for period_id, value in source["anchors_by_period"].items()
            }
        else:
            selected_columns = source.get("selected_value_columns")
            if not isinstance(selected_columns, Mapping):
                selected_columns = {primary: source.get("selected_value_column")}
            anchors = {
                str(period_id): _anchor_from_legacy(value, kind)
                for period_id, value in selected_columns.items()
            }

        formats = source.get("selected_value_formats")
        if not isinstance(formats, Mapping):
            formats = (
                {primary: source.get("selected_value_format")}
                if "selected_value_format" in source
                else {}
            )
        legacy_payload = {
            key: value
            for key, value in source.items()
            if key not in {"locator", "anchors_by_period"}
        }
        label_position = (
            source.get("label_x0")
            if kind == EvidenceLocatorKind.PDF
            else source.get("label_column")
        )
        return cls(
            locator=locator,
            label=str(source.get("label") or ""),
            values_by_period=values,
            anchors_by_period=anchors,
            primary_period_id=primary,
            value_formats_by_period=dict(formats),
            indent=_optional_float(source.get("indent")),
            bold=bool(source.get("bold", False)),
            label_position=label_position,
            label_rule=str(source.get("label_rule") or "none"),
            label_status=str(source.get("label_status") or "blank"),
            label_context=tuple(str(item) for item in source.get("label_context") or []),
            _legacy_payload=legacy_payload,
        )

    def to_legacy_dict(self) -> dict[str, Any]:
        """Return the exact pre-v5 dictionary used by prompts and old readers."""

        if self._legacy_payload is not None:
            return deepcopy(dict(self._legacy_payload))
        anchor_values = {
            period_id: anchor.legacy_value if anchor is not None else None
            for period_id, anchor in self.anchors_by_period.items()
        }
        payload: dict[str, Any] = {
            "row_key": self.row_key,
            "label": self.label,
            "selected_value_columns": anchor_values,
            "selected_values": dict(self.values_by_period),
            "selected_value_column": anchor_values.get(self.primary_period_id),
            "selected_value": self.values_by_period.get(self.primary_period_id),
            "indent": self.indent,
            "bold": self.bold,
            "label_rule": self.label_rule,
            "label_status": self.label_status,
            "label_context": list(self.label_context),
        }
        if isinstance(self.locator, ExcelRowLocator):
            payload["selected_value_formats"] = dict(self.value_formats_by_period)
            payload["label_column"] = self.label_position
        else:
            payload["label_x0"] = self.label_position
            payload["pdf_source"] = {
                "page": self.locator.page_number,
                "line_id": self.locator.line_id,
                "top": self.locator.top,
            }
        return payload

    def to_audit_dict(self) -> dict[str, Any]:
        """Add typed v5 locator/anchor data without removing legacy fields."""

        payload = self.to_legacy_dict()
        payload["locator"] = self.locator.to_dict()
        payload["anchors_by_period"] = {
            period_id: anchor.to_dict() if anchor is not None else None
            for period_id, anchor in self.anchors_by_period.items()
        }
        return payload

    def __getitem__(self, key: str) -> Any:
        return self.to_legacy_dict()[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self.to_legacy_dict())

    def __len__(self) -> int:
        return len(self.to_legacy_dict())

    def __eq__(self, other: object) -> bool:
        if isinstance(other, EvidenceRow):
            return self.to_audit_dict() == other.to_audit_dict()
        if isinstance(other, Mapping):
            return self.to_legacy_dict() == dict(other)
        return NotImplemented


@dataclass(frozen=True, slots=True)
class PeriodEvidenceLocation:
    """One real source location used for a selected period."""

    period_id: str
    period_label: str
    scope: str
    anchor: EvidenceAnchor
    evidence: tuple[str, ...] = ()

    def prompt_line(self, *, multi_period: bool) -> str:
        prefix = f"{self.period_id}|" if multi_period else ""
        if isinstance(self.anchor, ExcelValueAnchor):
            anchor = f"column={self.anchor.column_index}"
        else:
            anchor = f"right_edge={self.anchor.right_edge!r}"
        return f"{prefix}{self.scope}|{anchor}|{self.period_label}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "period_id": self.period_id,
            "period_label": self.period_label,
            "scope": self.scope,
            "anchor": self.anchor.to_dict(),
            "evidence": list(self.evidence),
        }


@dataclass(frozen=True, slots=True)
class PeriodLocationSummary:
    """Ordered, modality-neutral period locations for the mapper prompt."""

    period_ids: tuple[str, ...]
    locations: tuple[PeriodEvidenceLocation, ...]

    @classmethod
    def from_excel(
        cls,
        period_maps: Any,
        period_labels: Mapping[str, str],
    ) -> PeriodLocationSummary:
        maps = (
            period_maps
            if isinstance(period_maps, Mapping)
            else {"selected": period_maps}
        )
        locations: list[PeriodEvidenceLocation] = []
        for period_id, selection_map in maps.items():
            selections = list(selection_map.sheet_selections)
            if selection_map.default_selection is not None:
                selections.append(selection_map.default_selection)
            for selection in selections:
                locations.append(
                    PeriodEvidenceLocation(
                        period_id=str(period_id),
                        period_label=str(period_labels[period_id]),
                        scope=selection.sheet_name or "*",
                        anchor=ExcelValueAnchor(
                            column_index=selection.value_column,
                            excel_column=selection.excel_column,
                        ),
                        evidence=tuple(selection.evidence),
                    )
                )
        return cls(period_ids=tuple(str(item) for item in maps), locations=tuple(locations))

    @classmethod
    def from_pdf(
        cls,
        bindings: Any,
        period_labels: Mapping[str, str],
        *,
        period_ids: Sequence[str] | None = None,
    ) -> PeriodLocationSummary:
        ordered = tuple(period_ids or period_labels)
        locations = [
            PeriodEvidenceLocation(
                period_id=binding.period_id,
                period_label=str(period_labels[binding.period_id]),
                scope=f"Pages {binding.start_page}-{binding.end_page}",
                anchor=PdfValueAnchor(right_edge=binding.right_edge),
                evidence=tuple(binding.evidence),
            )
            for binding in bindings.bindings
            if binding.period_id in ordered
        ]
        return cls(period_ids=ordered, locations=tuple(locations))

    def prompt_lines(self) -> list[str]:
        multi_period = len(self.period_ids) > 1
        return [
            location.prompt_line(multi_period=multi_period)
            for period_id in self.period_ids
            for location in self.locations
            if location.period_id == period_id
        ]

    def to_dict(self) -> dict[str, Any]:
        return {
            "period_ids": list(self.period_ids),
            "locations": [location.to_dict() for location in self.locations],
        }


def ensure_evidence_row(
    value: EvidenceRow | Mapping[str, Any],
    *,
    locator_kind: EvidenceLocatorKind | str | None = None,
) -> EvidenceRow:
    if isinstance(value, EvidenceRow):
        return value
    return EvidenceRow.from_legacy_dict(value, locator_kind=locator_kind)


def ensure_evidence_rows(
    values: Sequence[EvidenceRow | Mapping[str, Any]],
) -> list[EvidenceRow]:
    return [ensure_evidence_row(value) for value in values]


def legacy_evidence_dict(value: EvidenceRow | Mapping[str, Any]) -> dict[str, Any]:
    return ensure_evidence_row(value).to_legacy_dict()


def audit_evidence_dict(value: EvidenceRow | Mapping[str, Any]) -> dict[str, Any]:
    return ensure_evidence_row(value).to_audit_dict()


def evidence_display_map(
    values: Sequence[EvidenceRow | Mapping[str, Any]],
) -> dict[str, str]:
    rows = ensure_evidence_rows(values)
    return {row.row_key: row.display for row in rows}


def _anchor_from_dict(value: Any) -> EvidenceAnchor | None:
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise ValueError("typed evidence anchor must be an object or null")
    kind = EvidenceLocatorKind(str(value.get("kind") or ""))
    if kind == EvidenceLocatorKind.PDF:
        return PdfValueAnchor(right_edge=float(value["right_edge"]))
    return ExcelValueAnchor(
        column_index=int(value["column_index"]),
        excel_column=(str(value["excel_column"]) if value.get("excel_column") else None),
    )


def _anchor_from_legacy(
    value: Any,
    kind: EvidenceLocatorKind,
) -> EvidenceAnchor | None:
    if value is None:
        return None
    if kind == EvidenceLocatorKind.PDF:
        text = str(value)
        if not text.startswith("x="):
            raise ValueError(f"invalid legacy PDF anchor {value!r}")
        return PdfValueAnchor(right_edge=float(text[2:]))
    return ExcelValueAnchor(column_index=int(value))


def _page_number_from_scope(scope: str) -> int:
    prefix = "Page "
    if not scope.startswith(prefix):
        raise ValueError(f"legacy PDF row has invalid page scope {scope!r}")
    return int(scope[len(prefix) :])


def _optional_float(value: Any) -> float | None:
    return None if value is None else float(value)


__all__ = [
    "EvidenceAnchor",
    "EvidenceLocator",
    "EvidenceLocatorKind",
    "EvidenceRow",
    "ExcelRowLocator",
    "ExcelValueAnchor",
    "PdfLineLocator",
    "PdfValueAnchor",
    "PeriodEvidenceLocation",
    "PeriodLocationSummary",
    "audit_evidence_dict",
    "ensure_evidence_row",
    "ensure_evidence_rows",
    "evidence_display_map",
    "legacy_evidence_dict",
]
