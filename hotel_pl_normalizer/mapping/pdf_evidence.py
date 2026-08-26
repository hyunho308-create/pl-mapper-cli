"""Build mapper evidence directly from positioned PDF words.

This is deliberately not a PDF-to-Excel conversion.  The upstream PDF binding
stage identifies the right edge of each selected amount column; this module
then joins each visual account line to the displayed numeric token at that
anchor.  The output is the same small row-evidence contract consumed by the
existing mapper, so PDF support does not alter Excel ingestion or mapping.
"""

from __future__ import annotations

import re
from typing import Any

from hotel_pl_normalizer.models.pdf import PdfDocumentRecord, PdfWord
from hotel_pl_normalizer.models.pdf_structure import PdfBindings

ANCHOR_TOLERANCE = 0.75


def compact_pdf_evidence(
    document: PdfDocumentRecord,
    bindings: PdfBindings,
    *,
    period_ids: list[str],
) -> list[dict[str, Any]]:
    """Return one auditable mapper row for each useful visual PDF line."""
    anchors: dict[tuple[str, int], float] = {}
    for binding in bindings.bindings:
        if binding.period_id not in period_ids:
            continue
        for page_number in range(binding.start_page, binding.end_page + 1):
            anchors[(binding.period_id, page_number)] = binding.right_edge

    evidence: list[dict[str, Any]] = []
    for page in document.pages:
        page_anchors = {
            period_id: anchors.get((period_id, page.page_number))
            for period_id in period_ids
        }
        if all(anchor is None for anchor in page_anchors.values()):
            continue
        words_by_id = {word.word_id: word for word in page.words}
        for line in page.text_lines:
            words = [words_by_id[word_id] for word_id in line.word_ids]
            selected_words = {
                period_id: _word_at_anchor(words, anchor)
                for period_id, anchor in page_anchors.items()
            }
            selected_values = {
                period_id: word.numeric_value if word is not None else None
                for period_id, word in selected_words.items()
            }
            matched = [word for word in selected_words.values() if word is not None]
            cutoff = min((word.x0 for word in matched), default=float("inf"))
            label_words = [
                word
                for word in words
                if word.x1 < cutoff - 0.25
                and not word.decorative
                and word.numeric_value is None
                and not word.is_percent
            ]
            label = _label(label_words)
            bold = any(word.bold for word in label_words)

            if not label:
                continue
            if all(value is None for value in selected_values.values()):
                # Keep concise visual section headings, but discard ordinary
                # prose and repeated report furniture that only inflate context.
                if not bold or len(label) > 100:
                    continue
            if _all_zero(selected_values) and not bold:
                continue

            first_period = period_ids[0]
            selected_columns = {
                period_id: (
                    None if anchor is None else f"x={anchor:.3f}"
                )
                for period_id, anchor in page_anchors.items()
            }
            evidence.append(
                {
                    "row_key": f"Page {page.page_number:03d}!{line.line_number}",
                    "label": label,
                    "selected_value_columns": selected_columns,
                    "selected_values": selected_values,
                    "selected_value_column": selected_columns[first_period],
                    "selected_value": selected_values[first_period],
                    "indent": None,
                    "bold": bold,
                    "pdf_source": {
                        "page": page.page_number,
                        "line_id": line.line_id,
                        "top": round(line.top, 3),
                    },
                }
            )
    return evidence


def pdf_evidence_stats(evidence: list[dict[str, Any]]) -> dict[str, int]:
    pages = {str(item["row_key"]).split("!", 1)[0] for item in evidence}
    value_rows = sum(
        any(value is not None for value in item.get("selected_values", {}).values())
        for item in evidence
    )
    return {"pages": len(pages), "rows": len(evidence), "value_rows": value_rows}


def _word_at_anchor(words: list[PdfWord], anchor: float | None) -> PdfWord | None:
    if anchor is None:
        return None
    candidates = [
        word
        for word in words
        if word.numeric_value is not None
        and not word.is_percent
        and abs(word.x1 - anchor) <= ANCHOR_TOLERANCE
    ]
    if not candidates:
        return None
    return min(candidates, key=lambda word: abs(word.x1 - anchor))


def _label(words: list[PdfWord]) -> str:
    # Numeric columns to the left of the selected anchor are context, not part
    # of the account name. Some PDFs encode a minus glyph separately enough
    # that the ingestion parser cannot safely call it a number, so use the
    # stronger label invariant here: an account-label token contains a letter.
    label_tokens = [word.text for word in words if re.search(r"[A-Za-z]", word.text)]
    text = " ".join(label_tokens)
    return re.sub(r"\s+", " ", text).strip(" |")


def _all_zero(values: dict[str, Any]) -> bool:
    numeric = [
        value
        for value in values.values()
        if isinstance(value, (int, float)) and not isinstance(value, bool)
    ]
    return bool(numeric) and all(value == 0 for value in numeric)
