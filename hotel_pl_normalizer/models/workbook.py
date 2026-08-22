"""Parsed workbook structures.

Why these are dataclasses and not pydantic models
-------------------------------------------------
These are the only models in the system built hundreds of thousands of times: one
`CellRecord` and one `CellStyle` per cell. As pydantic models that cost about
1,719 bytes per cell, which turned a 1.36 MB workbook (GRY, 218,879 cells) into
791 MB of peak memory and an out-of-memory kill on a 512 MB instance.

The same fields as `slots` dataclasses cost 296 bytes -- 5.8x less, 65 MB for the
same workbook. Nothing was removed to get there. The saving is entirely the
per-instance overhead of a validating model, paid on every cell to re-validate
data that openpyxl and xlrd have already parsed.

`model_validate` and `model_dump` are kept as a compatibility surface so callers
and tests need no changes. `model_dump` reproduces pydantic's `mode="json"` output
key for key on purpose: `workbook_id` is a hash of that payload, so any drift
would silently invalidate every cached response and orphan every artifact keyed on
it.

Everything else in the system stays pydantic. This is a targeted exception for the
one hot structure, not a change of approach.
"""

from __future__ import annotations

from dataclasses import dataclass, field, fields, is_dataclass
from datetime import datetime
from enum import Enum
from typing import Any

from .common import Severity


class FileType(str, Enum):
    XLS = "xls"
    XLSX = "xlsx"
    XLSM = "xlsm"
    PDF = "pdf"


def _json_value(value: Any) -> Any:
    """Match pydantic's `mode="json"` encoding for the types used here."""
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, datetime):
        return value.isoformat()
    if is_dataclass(value) and not isinstance(value, type):
        return {f.name: _json_value(getattr(value, f.name)) for f in fields(value)}
    if isinstance(value, list):
        return [_json_value(item) for item in value]
    return value


class _Model:
    """The slice of the pydantic surface these structures are used through."""

    def model_dump(self, mode: str = "python", **_: Any) -> dict[str, Any]:
        if mode == "json":
            return {f.name: _json_value(getattr(self, f.name)) for f in fields(self)}
        return {f.name: getattr(self, f.name) for f in fields(self)}

    @classmethod
    def model_validate(cls, data: Any) -> Any:
        if isinstance(data, cls):
            return data
        if not isinstance(data, dict):
            raise TypeError(
                f"{cls.__name__} expects a mapping, got {type(data).__name__}"
            )

        known = {f.name: f for f in fields(cls)}
        unknown = set(data) - set(known)
        if unknown:
            # Matches the strict-model behaviour this replaced: a mistyped key
            # should fail loudly rather than vanish into a default.
            raise ValueError(
                f"{cls.__name__} got unexpected fields: {sorted(unknown)}"
            )
        return cls(
            **{
                name: _coerce(known[name].type, value)
                for name, value in data.items()
            }
        )


def _coerce(annotation: Any, value: Any) -> Any:
    """Rebuild nested structures handed to `model_validate` as plain dicts."""
    if value is None:
        return None
    text = (
        annotation
        if isinstance(annotation, str)
        else getattr(annotation, "__name__", str(annotation))
    )
    is_list = text.startswith("list[") or text.startswith("List[")
    for name, model in _NESTED.items():
        if name in text:
            if is_list:
                return [model.model_validate(item) for item in value]
            return model.model_validate(value)
    if "FileType" in text and not isinstance(value, FileType):
        return FileType(value)
    if "Severity" in text and not isinstance(value, Severity):
        return Severity(value)
    if "datetime" in text and isinstance(value, str):
        return datetime.fromisoformat(value)
    return value


# Field order is preserved from the pydantic definitions wherever a default does
# not force otherwise. `_parsed_content_hash` sorts keys, so ordering cannot move
# the workbook id, but callers constructing positionally would notice.
@dataclass(slots=True)
class WorkbookSource(_Model):
    source_id: str
    original_filename: str
    file_type: FileType
    ingested_at: datetime
    storage_uri: str | None = None
    local_path: str | None = None
    file_hash: str | None = None
    uploaded_by: str | None = None
    uploaded_at: datetime | None = None


@dataclass(slots=True)
class WorkbookMetadata(_Model):
    sheet_count: int = 0
    detected_property_code: str | None = None
    detected_operator: str | None = None
    detected_period_label: str | None = None


@dataclass(slots=True)
class MergedRange(_Model):
    range: str
    top_left_row: int
    top_left_column: int
    value: str | int | float | bool | None = None


# Frozen because ingestion hands one instance to every cell that shares a style.
# Nothing mutated a style before this, so freezing costs nothing and turns that
# sharing from an assumption into a guarantee.
@dataclass(slots=True, frozen=True)
class CellStyle(_Model):
    bold: bool = False
    italic: bool = False
    underline: bool = False
    font_size: float | None = None
    indent: float | None = None
    horizontal_alignment: str | None = None
    fill_color: str | None = None
    font_color: str | None = None
    border_bottom: str | None = None


@dataclass(slots=True)
class CellRecord(_Model):
    row: int
    column: int
    address: str
    raw_value: str | int | float | bool | None = None
    display_value: str | None = None
    number_format: str | None = None
    is_merged: bool = False
    merged_parent: str | None = None
    style: CellStyle = field(default_factory=CellStyle)


@dataclass(slots=True)
class WorkbookRow(_Model):
    row_index: int
    cells: list[CellRecord] = field(default_factory=list)
    height: float | None = None
    hidden: bool = False


@dataclass(slots=True)
class WorkbookSheet(_Model):
    sheet_id: str
    sheet_name: str
    max_row: int
    max_column: int
    rows: list[WorkbookRow] = field(default_factory=list)
    merged_ranges: list[MergedRange] = field(default_factory=list)
    visible: bool = True


@dataclass(slots=True)
class IngestionWarning(_Model):
    severity: Severity
    message: str
    sheet_name: str | None = None
    row: int | None = None
    column: int | None = None


@dataclass(slots=True)
class WorkbookRecord(_Model):
    workbook_id: str
    source: WorkbookSource
    sheets: list[WorkbookSheet] = field(default_factory=list)
    workbook_metadata: WorkbookMetadata = field(default_factory=WorkbookMetadata)
    ingestion_warnings: list[IngestionWarning] = field(default_factory=list)


# Resolved after definition so `_coerce` can rebuild nested payloads by type name.
_NESTED: dict[str, Any] = {
    "WorkbookSource": WorkbookSource,
    "WorkbookMetadata": WorkbookMetadata,
    "MergedRange": MergedRange,
    "CellStyle": CellStyle,
    "CellRecord": CellRecord,
    "WorkbookRow": WorkbookRow,
    "WorkbookSheet": WorkbookSheet,
    "IngestionWarning": IngestionWarning,
}
