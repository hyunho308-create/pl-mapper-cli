"""Identify the account-label cell used by mapping evidence."""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Iterable

from hotel_pl_normalizer.models.workbook import CellRecord, WorkbookRow

_CONTROL_LABELS = {
    "account",
    "app",
    "apply to",
    "businessunit",
    "category",
    "criteria",
    "datasrc",
    "department",
    "dimension",
    "format",
    "insert",
    "measures",
    "memberset",
    "option",
    "parameter",
    "parameters",
    "querytype",
    "range",
    "rptcurrency",
    "sqlonly",
    "suppress",
    "time",
    "top",
    "use",
}
_BUSINESS_LABEL_TERMS = (
    "allowance",
    "benefit",
    "cost",
    "department",
    "ebitda",
    "expense",
    "fees",
    "income",
    "labor",
    "payroll",
    "profit",
    "revenue",
    "sales",
    "salaries",
    "total",
    "wages",
)
_SHORT_BUSINESS_LABELS = {
    "a&g",
    "adr",
    "ebitda",
    "f&b",
    "gop",
    "hvac",
    "it",
    "linen",
    "noi",
    "ood",
    "pom",
    "revpar",
    "sewer",
    "s&m",
    "smerf",
    "water",
}


def label_cell(
    text_cells: list[CellRecord],
    *,
    preferred_column: int | None = None,
) -> CellRecord | None:
    candidates = [
        (score, cell)
        for cell in text_cells
        if (score := _row_label_score(_clean_text(cell.display_value or ""))) > 0
    ]
    if not candidates:
        return None
    if preferred_column is not None:
        return next(
            (cell for _, cell in candidates if cell.column == preferred_column),
            None,
        )
    return max(candidates, key=lambda item: (item[0], item[1].column))[1]


def dominant_label_column(rows: Iterable[WorkbookRow]) -> int | None:
    """Infer the stable account-label column from rows containing figures."""
    counts: Counter[int] = Counter()
    scores: Counter[int] = Counter()
    for row in rows:
        if not any(_is_numeric_cell(cell) for cell in row.cells):
            continue
        for cell in row.cells:
            if not _is_text_cell(cell):
                continue
            score = _row_label_score(_clean_text(cell.display_value or ""))
            if score > 0:
                counts[cell.column] += 1
                scores[cell.column] += score
    if not counts:
        return None
    return max(counts, key=lambda column: (counts[column], scores[column], -column))


def _row_label_score(text: str) -> int:
    if not text or not re.search(r"[A-Za-z]", text):
        return 0
    if re.fullmatch(r"[-+]?\d+(?:\.\d+)?%?", text):
        return 0
    normalized = _normalize_label(text)
    if not normalized or normalized in _CONTROL_LABELS:
        return 0
    if _looks_like_control_or_member_code(text, normalized):
        return 0

    words = re.findall(r"[A-Za-z][A-Za-z&'-]*", text)
    score = 1
    if len(words) >= 2:
        score += 4
    if any(character.islower() for character in text):
        score += 2
    if any(term in normalized for term in _BUSINESS_LABEL_TERMS):
        score += 3
    if text[:1].isupper() and not text.isupper():
        score += 1
    if any(character in text for character in "_|!$"):
        score -= 4
    return max(score, 0)


def _looks_like_control_or_member_code(text: str, normalized: str) -> bool:
    if _looks_like_technical_metadata_text(text):
        return True
    if text.strip().lower() in _SHORT_BUSINESS_LABELS or "&" in text:
        return False
    compact = re.sub(r"[^A-Za-z0-9]+", "", text)
    return bool(
        re.fullmatch(r"[A-Z]{1,6}\d{0,4}", compact)
        or re.fullmatch(r"[A-Z]+(?:_[A-Z0-9]+)+", text)
        or re.fullmatch(r"[A-Z]\d", text)
        or normalized.startswith("account style")
        or "dep(" in text.lower()
    )


def _looks_like_technical_metadata_text(text: str) -> bool:
    stripped = text.strip()
    normalized = re.sub(r"[^a-z0-9]+", " ", stripped.lower()).strip()
    return bool(
        "fontbold" in normalized
        or "indentlevel" in normalized
        or re.search(r"\[[^\]]+\]\s*\.\s*\[[^\]]+\]", stripped)
        or (stripped.startswith("<<") and "[" in stripped and "]" in stripped)
        or ("!" in stripped and "$" in stripped)
        or "dep(" in stripped.lower()
    )


def _is_text_cell(cell: CellRecord) -> bool:
    value = _clean_text(cell.display_value or "")
    return bool(
        isinstance(cell.raw_value, str)
        and value
        and not _looks_like_technical_metadata_text(value)
    )


def _is_numeric_cell(cell: CellRecord) -> bool:
    return isinstance(cell.raw_value, int | float) and not isinstance(
        cell.raw_value, bool
    )


def _clean_text(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _normalize_label(label: str) -> str:
    return re.sub(r"[^a-z0-9%]+", " ", _clean_text(label).lower()).strip()
