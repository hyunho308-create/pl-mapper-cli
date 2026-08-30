"""Small deterministic guard for explicit monthly-spread headers."""

from __future__ import annotations

import re
from collections.abc import Iterable

MONTHLY_SPREAD_THRESHOLD = 6

_MONTH_NUMBERS = {
    "jan": 1,
    "feb": 2,
    "mar": 3,
    "apr": 4,
    "may": 5,
    "jun": 6,
    "jul": 7,
    "aug": 8,
    "sep": 9,
    "oct": 10,
    "nov": 11,
    "dec": 12,
}
_EXPLICIT_MONTH_YEAR = re.compile(
    r"\b(" + "|".join(_MONTH_NUMBERS) + r")[a-z]*[ '\-/]*(20\d{2}|\d{2})\b",
    re.IGNORECASE,
)
_ISO_DATE = re.compile(r"\b(20\d{2})-(0?[1-9]|1[0-2])(?:-\d{1,2})?\b")
_SLASH_DATE = re.compile(r"\b(0?[1-9]|1[0-2])/\d{1,2}/(20\d{2})\b")


def explicit_month_years(values: Iterable[str]) -> set[str]:
    """Return canonical months explicitly named in header-like text."""
    months: set[str] = set()
    for value in values:
        text = str(value)
        for match in _EXPLICIT_MONTH_YEAR.finditer(text):
            year = int(match.group(2))
            if year < 100:
                year += 2000
            month = _MONTH_NUMBERS[match.group(1)[:3].lower()]
            months.add(f"{year:04d}-{month:02d}")
        for match in _ISO_DATE.finditer(text):
            months.add(f"{int(match.group(1)):04d}-{int(match.group(2)):02d}")
        for match in _SLASH_DATE.finditer(text):
            months.add(f"{int(match.group(2)):04d}-{int(match.group(1)):02d}")
    return months
