"""Build mapper evidence directly from positioned PDF words.

The upstream PDF binding stage identifies each selected amount column by its
repeated numeric right edge. Label selection is deliberately independent of
those amount positions: every page gets a dominant text lane, adjacent
indentation is allowed, and a stable run of alternate labels can form a local
override. This supports labels left of monthly/annual amounts and labels in the
middle of common PTD/YTD layouts with the same structural rules.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from statistics import median
from typing import Any

from hotel_pl_normalizer.models.pdf import (
    PdfDocumentRecord,
    PdfPage,
    PdfTextLine,
    PdfWord,
)
from hotel_pl_normalizer.models.pdf_structure import PdfBindings
from hotel_pl_normalizer.structure.representation import is_technical_label


@dataclass(frozen=True)
class PdfLabelRun:
    """One contiguous run of nonnumeric words on a visual line."""

    words: tuple[PdfWord, ...]
    text: str
    x0: float
    x1: float
    bold: bool


@dataclass(frozen=True)
class PdfLabelRegion:
    """A contiguous visual-line range using an alternate label lane."""

    start_line: int
    end_line: int
    x0: float


@dataclass(frozen=True)
class PdfLabelLayout:
    """One page's primary label lane and rare local exceptions."""

    primary_x0: float | None
    primary_tolerance: float
    adjacent_tolerance: float
    overrides: tuple[PdfLabelRegion, ...] = ()


@dataclass(frozen=True)
class PdfLabelSelection:
    run: PdfLabelRun | None
    context: tuple[PdfLabelRun, ...] = ()
    status: str = "blank"
    rule: str = "none"


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
        lines = [
            (line, [words_by_id[word_id] for word_id in line.word_ids])
            for line in page.text_lines
        ]
        runs_by_line = {
            line.line_number: _label_runs(words, page) for line, words in lines
        }
        label_layout = _infer_page_label_layout(page, lines, runs_by_line)
        anchor_tolerance = _anchor_tolerance(page)

        for line, words in lines:
            selected_words = {
                period_id: _word_at_anchor(words, anchor, anchor_tolerance)
                for period_id, anchor in page_anchors.items()
            }
            selected_values = {
                period_id: word.numeric_value if word is not None else None
                for period_id, word in selected_words.items()
            }
            label_selection = _select_line_label(
                line,
                runs_by_line[line.line_number],
                label_layout,
            )
            label_run = label_selection.run
            label = label_run.text if label_run is not None else ""
            bold = label_run.bold if label_run is not None else False

            if not label and all(value is None for value in selected_values.values()):
                continue
            if label and all(value is None for value in selected_values.values()):
                # Keep concise visual section headings, but discard ordinary
                # prose and repeated report furniture that only inflate context.
                if not bold or len(label) > 100:
                    continue
            if _all_zero(selected_values) and not bold:
                continue

            first_period = period_ids[0]
            selected_columns = {
                period_id: None if anchor is None else f"x={anchor:.3f}"
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
                    "indent": (
                        round(label_run.x0 - label_layout.primary_x0, 3)
                        if label_run is not None and label_layout.primary_x0 is not None
                        else None
                    ),
                    "bold": bold,
                    "label_x0": round(label_run.x0, 3) if label_run else None,
                    "label_rule": label_selection.rule,
                    "label_status": label_selection.status,
                    "label_context": [run.text for run in label_selection.context],
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


def _infer_page_label_layout(
    page: PdfPage,
    lines: list[tuple[PdfTextLine, list[PdfWord]]],
    runs_by_line: dict[int, list[PdfLabelRun]],
) -> PdfLabelLayout:
    cluster_tolerance = _primary_lane_tolerance(page)
    adjacent_tolerance = _adjacent_lane_tolerance(page)
    financial_line_runs = [
        runs_by_line[line.line_number]
        for line, words in lines
        if any(_is_amount(word) for word in words)
    ]
    financial_runs = [run for runs in financial_line_runs for run in runs]
    if not financial_runs:
        return PdfLabelLayout(None, cluster_tolerance, adjacent_tolerance)

    clusters: list[list[PdfLabelRun]] = []
    for run in sorted(financial_runs, key=lambda item: item.x0):
        choices = [
            (abs(run.x0 - median(item.x0 for item in cluster)), index)
            for index, cluster in enumerate(clusters)
        ]
        distance, index = min(choices, default=(float("inf"), -1))
        if distance <= cluster_tolerance:
            clusters[index].append(run)
        else:
            clusters.append([run])

    def cluster_rank(cluster: list[PdfLabelRun]) -> tuple[int, int, int, float]:
        anchor = float(median(run.x0 for run in cluster))
        coverage = sum(
            any(abs(run.x0 - anchor) <= adjacent_tolerance for run in runs)
            for runs in financial_line_runs
        )
        return (
            coverage,
            len(cluster),
            sum(len(run.text) for run in cluster),
            -anchor,
        )

    primary_cluster = max(clusters, key=cluster_rank)
    primary_x0 = float(median(run.x0 for run in primary_cluster))
    overrides = _stable_pdf_overrides(
        page,
        lines,
        runs_by_line,
        primary_x0,
        adjacent_tolerance,
    )
    return PdfLabelLayout(
        primary_x0=primary_x0,
        primary_tolerance=cluster_tolerance,
        adjacent_tolerance=adjacent_tolerance,
        overrides=tuple(overrides),
    )


def _select_line_label(
    line: PdfTextLine,
    runs: list[PdfLabelRun],
    layout: PdfLabelLayout,
) -> PdfLabelSelection:
    if not runs or layout.primary_x0 is None:
        return PdfLabelSelection(run=None)
    override = next(
        (
            region
            for region in layout.overrides
            if region.start_line <= line.line_number <= region.end_line
        ),
        None,
    )
    anchor = override.x0 if override is not None else layout.primary_x0
    candidates = [
        run for run in runs if abs(run.x0 - anchor) <= layout.adjacent_tolerance
    ]
    if not candidates:
        return PdfLabelSelection(run=None)
    selected = min(candidates, key=lambda run: (abs(run.x0 - anchor), -run.x0))
    context = tuple(run for run in candidates if run is not selected)
    if override is not None:
        rule = "local_override"
    elif abs(selected.x0 - layout.primary_x0) <= layout.primary_tolerance:
        rule = "primary"
    else:
        rule = "adjacent_indent"
    return PdfLabelSelection(
        run=selected,
        context=context,
        status="selected" if len(candidates) == 1 else "selected_with_context",
        rule=rule,
    )


def _stable_pdf_overrides(
    page: PdfPage,
    lines: list[tuple[PdfTextLine, list[PdfWord]]],
    runs_by_line: dict[int, list[PdfLabelRun]],
    primary_x0: float,
    adjacent_tolerance: float,
) -> list[PdfLabelRegion]:
    missing: list[tuple[int, float]] = []
    for line, words in lines:
        if not any(_is_amount(word) for word in words):
            continue
        runs = runs_by_line[line.line_number]
        if any(abs(run.x0 - primary_x0) <= adjacent_tolerance for run in runs):
            continue
        if len(runs) == 1:
            missing.append((line.line_number, runs[0].x0))

    regions: list[PdfLabelRegion] = []
    run: list[tuple[int, float]] = []
    lane_tolerance = _primary_lane_tolerance(page)
    for item in missing:
        if run and (
            item[0] > run[-1][0] + 1
            or abs(item[1] - median(x0 for _, x0 in run)) > lane_tolerance
        ):
            _append_pdf_override(regions, run)
            run = []
        run.append(item)
    _append_pdf_override(regions, run)
    return regions


def _append_pdf_override(
    regions: list[PdfLabelRegion], run: list[tuple[int, float]]
) -> None:
    if len(run) >= 2:
        regions.append(
            PdfLabelRegion(
                start_line=run[0][0],
                end_line=run[-1][0],
                x0=float(median(x0 for _, x0 in run)),
            )
        )


def _label_runs(words: list[PdfWord], page: PdfPage) -> list[PdfLabelRun]:
    candidates = [
        word
        for word in sorted(words, key=lambda item: item.x0)
        if not word.decorative
        and word.numeric_value is None
        and not word.is_percent
        and (re.search(r"[A-Za-z]", word.text) or word.text.strip() in {"&", "/", "+"})
        and not is_technical_label(word.text)
    ]
    if not candidates:
        return []
    gap_tolerance = max(8.0, page.width * 0.014)
    groups: list[list[PdfWord]] = []
    for word in candidates:
        if groups and word.x0 - groups[-1][-1].x1 <= gap_tolerance:
            groups[-1].append(word)
        else:
            groups.append([word])
    runs = []
    for group in groups:
        text = re.sub(r"\s+", " ", " ".join(word.text for word in group)).strip(" |")
        if text and not is_technical_label(text):
            runs.append(
                PdfLabelRun(
                    words=tuple(group),
                    text=text,
                    x0=min(word.x0 for word in group),
                    x1=max(word.x1 for word in group),
                    bold=any(word.bold for word in group),
                )
            )
    return runs


def _word_at_anchor(
    words: list[PdfWord],
    anchor: float | None,
    tolerance: float,
) -> PdfWord | None:
    if anchor is None:
        return None
    candidates = sorted(
        (
            (abs(word.x1 - anchor), word)
            for word in words
            if _is_amount(word) and abs(word.x1 - anchor) <= tolerance
        ),
        key=lambda item: item[0],
    )
    if not candidates:
        return None
    # Do not force a choice between two nearly equidistant displayed amounts.
    if len(candidates) > 1 and candidates[1][0] - candidates[0][0] <= 0.25:
        return None
    return candidates[0][1]


def _anchor_tolerance(page: PdfPage) -> float:
    """Use the same tolerance as anchor discovery and binding validation."""
    return max(4.0, page.width * 0.006)


def _primary_lane_tolerance(page: PdfPage) -> float:
    return max(4.0, page.width * 0.006)


def _adjacent_lane_tolerance(page: PdfPage) -> float:
    return max(18.0, page.width * 0.03)


def _is_amount(word: PdfWord) -> bool:
    return word.numeric_value is not None and not word.is_percent


def _all_zero(values: dict[str, Any]) -> bool:
    numeric = [
        value
        for value in values.values()
        if isinstance(value, (int, float)) and not isinstance(value, bool)
    ]
    return bool(numeric) and all(value == 0 for value in numeric)
