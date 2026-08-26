"""Read a PDF into positioned, auditable primitives without inventing cells."""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone
from pathlib import Path
from statistics import median
from uuid import uuid4

from hotel_pl_normalizer.models.common import Severity
from hotel_pl_normalizer.models.pdf import (
    PdfDocumentRecord,
    PdfIngestionWarning,
    PdfPage,
    PdfRule,
    PdfSource,
    PdfTextLine,
    PdfWord,
)

# A word whose full text matches this grammar is safe to count as a displayed
# figure. Dates, account codes and text containing letters deliberately do not.
_NUMBER = re.compile(
    r"^\s*(?P<open>\()?\s*[$€£¥]?\s*"
    r"(?P<number>(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?|\.\d+)"
    r"\s*(?P<percent>%)?\s*(?P<trailing>-)?\s*(?P<close>\))?\s*$"
)
_BOLD_MARKERS = ("bold", "black", "demi", "heavy", "semibold")


def read_pdf_document(
    path: str | Path,
    *,
    source_id: str | None = None,
) -> PdfDocumentRecord:
    """Ingest a born-digital PDF without imposing a spreadsheet grid.

    The only derived structure is visual text-line grouping. It depends on word
    geometry, not report names, date formats, table borders, or hotel templates.
    """
    source_path = Path(path)
    if source_path.suffix.lower() != ".pdf":
        raise ValueError(f"read_pdf_document only supports .pdf files: {source_path}")
    if not source_path.is_file():
        raise FileNotFoundError(source_path)

    import pdfplumber
    from pypdf import PdfReader

    warnings: list[PdfIngestionWarning] = []
    try:
        reader = PdfReader(source_path, strict=False)
        if reader.is_encrypted:
            try:
                unlocked = reader.decrypt("")
            except Exception:  # noqa: BLE001 - converted into a precise error
                unlocked = 0
            if not unlocked:
                raise ValueError(
                    f"PDF is encrypted and cannot be opened without a password: {source_path.name}"
                )
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError(f"Invalid or unreadable PDF {source_path.name}: {exc}") from exc

    pages: list[PdfPage] = []
    try:
        with pdfplumber.open(source_path) as pdf:
            for page_number, page in enumerate(pdf.pages, start=1):
                pages.append(_read_page(page, page_number))
    except Exception as exc:
        raise ValueError(f"Could not extract PDF {source_path.name}: {exc}") from exc

    blank_pages = [page.page_number for page in pages if not page.words]
    if blank_pages:
        preview = ", ".join(str(number) for number in blank_pages[:10])
        suffix = "..." if len(blank_pages) > 10 else ""
        warnings.append(
            PdfIngestionWarning(
                severity=Severity.WARNING,
                message=(
                    f"No extractable text on {len(blank_pages)} page(s): {preview}{suffix}. "
                    "They may be scanned images and require OCR."
                ),
            )
        )

    digest = _sha256(source_path)
    source = PdfSource(
        source_id=source_id or f"src_{uuid4().hex}",
        original_filename=source_path.name,
        local_path=str(source_path),
        file_hash=digest,
        ingested_at=datetime.now(timezone.utc),
    )
    return PdfDocumentRecord(
        document_id=f"pdf_{digest[:24]}",
        source=source,
        pages=pages,
        warnings=warnings,
    )


def parse_displayed_number(text: str) -> tuple[float | None, bool]:
    """Parse one complete accounting token while preserving its raw text."""
    match = _NUMBER.fullmatch(text)
    if match is None:
        return None, False
    # Reject mismatched accounting parentheses rather than guessing the sign.
    if bool(match.group("open")) != bool(match.group("close")):
        return None, False
    value = float(match.group("number").replace(",", ""))
    if match.group("open") or match.group("trailing"):
        value = -value
    return value, bool(match.group("percent"))


def _read_page(page, page_number: int) -> PdfPage:
    raw_words = page.extract_words(
        # Financial columns can be only a point apart. A tighter tolerance
        # prevents adjacent negative amount and percentage columns from being
        # fused into one token while ordinary inter-word spaces remain wider.
        x_tolerance=1,
        y_tolerance=3,
        keep_blank_chars=False,
        use_text_flow=False,
        extra_attrs=["fontname", "size"],
    ) or []
    ordered = sorted(
        raw_words,
        key=lambda word: (
            round(float(word["top"]), 3),
            round(float(word["x0"]), 3),
            str(word.get("text") or ""),
        ),
    )
    decorative_indexes = _decorative_word_indexes(ordered)
    words: list[PdfWord] = []
    seen: set[tuple[str, float, float, float, float]] = set()
    for index, raw in enumerate(ordered, start=1):
        text = " ".join(str(raw.get("text") or "").split())
        if not text:
            continue
        # Duplicate text layers are common in exported reports. Word-level
        # deduplication is linear and prevents double figures without the very
        # expensive all-character comparison performed by Page.dedupe_chars.
        identity = (
            text,
            round(float(raw["x0"]), 1),
            round(float(raw["top"]), 1),
            round(float(raw["x1"]), 1),
            round(float(raw["bottom"]), 1),
        )
        if identity in seen:
            continue
        seen.add(identity)
        number, is_percent = parse_displayed_number(text)
        font_name = str(raw.get("fontname") or "") or None
        font_size = _rounded(raw.get("size"))
        words.append(
            PdfWord(
                word_id=f"p{page_number}:w{index}",
                text=text,
                x0=_rounded(raw["x0"]),
                top=_rounded(raw["top"]),
                x1=_rounded(raw["x1"]),
                bottom=_rounded(raw["bottom"]),
                font_name=font_name,
                font_size=font_size,
                bold=bool(
                    font_name
                    and any(marker in font_name.lower() for marker in _BOLD_MARKERS)
                ),
                decorative=index in decorative_indexes,
                numeric_value=number,
                is_percent=is_percent,
            )
        )

    return PdfPage(
        page_number=page_number,
        width=_rounded(page.width),
        height=_rounded(page.height),
        rotation=int(getattr(page, "rotation", 0) or 0),
        words=words,
        text_lines=_group_text_lines(
            [word for word in words if not word.decorative], page_number
        ),
        rules=_extract_rules(page, page_number),
    )


def _decorative_word_indexes(raw_words: list[dict]) -> set[int]:
    """Find obvious diagonal watermark glyphs without filtering normal titles.

    Rotated watermarks are often exposed as dozens of isolated, very large
    alphabetic glyphs in one font. A large title, logo, or initial is retained:
    the same font must contribute at least eight such single-letter words.
    """
    sizes = [float(word["size"]) for word in raw_words if word.get("size")]
    if not sizes:
        return set()
    large = max(14.0, median(sizes) * 2.0)
    by_font: dict[str, list[int]] = {}
    for index, word in enumerate(raw_words, start=1):
        text = str(word.get("text") or "").strip()
        size = float(word.get("size") or 0)
        font = str(word.get("fontname") or "")
        if len(text) == 1 and text.isalpha() and size >= large and font:
            by_font.setdefault(font, []).append(index)
    return {
        index
        for indexes in by_font.values()
        if len(indexes) >= 8
        for index in indexes
    }


def _group_text_lines(words: list[PdfWord], page_number: int) -> list[PdfTextLine]:
    """Group words by stable vertical centers, independent of any table.

    A group's full top/bottom extent is deliberately not used as its anchor.
    Some PDFs contain rotated or malformed words spanning many visual rows; an
    expanding bounding box would then chain the rest of the page into one line.
    Medians keep those outliers local and make the failure mode extra small lines
    rather than one unusably large line.
    """
    groups: list[list[PdfWord]] = []
    for word in sorted(words, key=lambda item: (item.top, item.x0)):
        best_index: int | None = None
        best_distance = float("inf")
        word_mid = (word.top + word.bottom) / 2
        word_height = max(word.bottom - word.top, 1.0)
        for index in range(max(0, len(groups) - 8), len(groups)):
            centers = [(item.top + item.bottom) / 2 for item in groups[index]]
            heights = [max(item.bottom - item.top, 1.0) for item in groups[index]]
            line_mid = median(centers)
            distance = abs(word_mid - line_mid)
            tolerance = min(5.0, max(2.0, min(word_height, median(heights)) * 0.45))
            if distance <= tolerance and distance < best_distance:
                best_index = index
                best_distance = distance
        if best_index is None:
            groups.append([word])
        else:
            groups[best_index].append(word)

    sorted_groups = sorted(groups, key=lambda group: (min(w.top for w in group), min(w.x0 for w in group)))
    lines: list[PdfTextLine] = []
    for line_number, group in enumerate(sorted_groups, start=1):
        row = sorted(group, key=lambda word: (word.x0, word.top))
        lines.append(
            PdfTextLine(
                line_id=f"p{page_number}:l{line_number}",
                line_number=line_number,
                text=" ".join(word.text for word in row),
                x0=min(word.x0 for word in row),
                top=min(word.top for word in row),
                x1=max(word.x1 for word in row),
                bottom=max(word.bottom for word in row),
                word_ids=tuple(word.word_id for word in row),
            )
        )
    return lines


def _extract_rules(page, page_number: int) -> list[PdfRule]:
    candidates: list[tuple[str, float, float, float, float]] = []
    for item in page.lines:
        x0, x1 = sorted((float(item.get("x0", 0)), float(item.get("x1", 0))))
        top, bottom = sorted((float(item.get("top", 0)), float(item.get("bottom", 0))))
        orientation = "horizontal" if (x1 - x0) >= (bottom - top) else "vertical"
        candidates.append((orientation, x0, top, x1, bottom))
    for item in page.rects:
        x0, x1 = sorted((float(item.get("x0", 0)), float(item.get("x1", 0))))
        top, bottom = sorted((float(item.get("top", 0)), float(item.get("bottom", 0))))
        # Thin rectangles are commonly exported table rules. A large fill is a
        # visual background and says nothing useful about row/column structure.
        if min(x1 - x0, bottom - top) <= 2.0:
            orientation = "horizontal" if (x1 - x0) >= (bottom - top) else "vertical"
            candidates.append((orientation, x0, top, x1, bottom))

    rules: list[PdfRule] = []
    for index, (orientation, x0, top, x1, bottom) in enumerate(candidates, start=1):
        rules.append(
            PdfRule(
                rule_id=f"p{page_number}:r{index}",
                orientation=orientation,
                x0=_rounded(x0),
                top=_rounded(top),
                x1=_rounded(x1),
                bottom=_rounded(bottom),
            )
        )
    return rules


def _rounded(value) -> float:
    return round(float(value), 3)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
