from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone

import pytest

from hotel_pl_normalizer.models.pdf import (
    PdfDocumentRecord,
    PdfPage,
    PdfSource,
    PdfTextLine,
    PdfWord,
)
from hotel_pl_normalizer.models.pdf_structure import PdfExploration
from hotel_pl_normalizer.structure.pdf import (
    PdfBindingToolset,
    PdfExplorationToolset,
    bind_pdf_periods,
    explore_pdf,
)


def _document(page_count: int = 6) -> PdfDocumentRecord:
    pages = []
    for number in range(1, page_count + 1):
        word = PdfWord(
            word_id=f"p{number}:w1",
            text="100",
            x0=60,
            top=10,
            x1=80,
            bottom=20,
            numeric_value=100,
        )
        line = PdfTextLine(
            line_id=f"p{number}:l1",
            line_number=1,
            text="100",
            x0=60,
            top=10,
            x1=80,
            bottom=20,
            word_ids=(word.word_id,),
        )
        pages.append(
            PdfPage(
                page_number=number,
                width=100,
                height=100,
                words=[word],
                text_lines=[line],
            )
        )
    return PdfDocumentRecord(
        document_id="pdf_test",
        source=PdfSource(
            source_id="source",
            original_filename="test.pdf",
            local_path="test.pdf",
            file_hash="abc",
            ingested_at=datetime(2026, 8, 25, tzinfo=timezone.utc),
        ),
        pages=pages,
    )


def _routing():
    return {
        "layout": "consistent_multi_page_statement",
        "layout_evidence": ["same header"],
        "page_ranges": [
            {
                "start_page": 1,
                "end_page": 6,
                "include_as_financial_evidence": True,
                "role": "summary_p_and_l",
                "confidence": "high",
                "evidence": ["pages 1-6"],
            }
        ],
    }


def _periods():
    return {
        "controlling_summary_pages": {"start_page": 1, "end_page": 6},
        "periods": [
            {
                "period_id": "2025-01_2025-12_actual",
                "label": "2025 Actual",
                "scenario": "actual",
                "start_month": "2025-01",
                "end_month": "2025-12",
                "pages_present": [{"start_page": 1, "end_page": 6}],
                "evidence": ["p1:l1"],
            }
        ],
    }


def test_pdf_exploration_requires_exact_routing_and_an_open_summary_anchor():
    toolset = PdfExplorationToolset(_document())
    incomplete = _routing()
    incomplete["page_ranges"][0]["end_page"] = 5
    assert toolset.dispatch("submit_routing", incomplete)["accepted"] is False

    accepted = toolset.dispatch("submit_routing", _routing())
    assert accepted["accepted"] is True
    assert accepted["must_read_at_least"] == 1
    assert toolset.dispatch("submit_periods", _periods())["accepted"] is False

    toolset.dispatch("read_page_lines", {"page_number": 1})
    result = toolset.dispatch("submit_periods", _periods())
    assert result["accepted"] is True
    structure = PdfExploration.model_validate(result["structure"])
    assert structure.financial_pages == [1, 2, 3, 4, 5, 6]
    assert toolset.terminal_result("submit_periods", result) == result["structure"]


def test_pdf_periods_are_anchored_to_summary_and_departments_only_confirm():
    toolset = PdfExplorationToolset(_document(6))
    routing = {
        "layout": "mixed_page_layouts",
        "page_ranges": [
            {
                "start_page": 1,
                "end_page": 1,
                "include_as_financial_evidence": True,
                "role": "summary_p_and_l",
                "confidence": "high",
                "evidence": ["core summary"],
            },
            *[
                {
                    "start_page": page,
                    "end_page": page,
                    "include_as_financial_evidence": True,
                    "role": "department_p_and_l",
                    "confidence": "high",
                    "evidence": [f"normal department {page}"],
                }
                for page in range(2, 6)
            ],
            {
                "start_page": 6,
                "end_page": 6,
                "include_as_financial_evidence": True,
                "role": "financial_supporting_schedule",
                "confidence": "high",
                "evidence": ["one-off T12"],
            },
        ],
    }
    accepted = toolset.dispatch("submit_routing", routing)
    assert accepted["required_department_reads"] == 4
    for page in range(1, 7):
        toolset.dispatch("read_page_lines", {"page_number": page})

    annual = _periods()["periods"][0]
    annual["pages_present"] = [{"start_page": 1, "end_page": 5}]
    monthly = {
        "period_id": "2025-05_2025-05_actual",
        "label": "May 2025 Actual",
        "scenario": "actual",
        "start_month": "2025-05",
        "end_month": "2025-05",
        "pages_present": [{"start_page": 6, "end_page": 6}],
        "evidence": ["p6:l1"],
    }
    rejected = toolset.dispatch(
        "submit_periods",
        {
            "controlling_summary_pages": {"start_page": 1, "end_page": 1},
            "periods": [annual, monthly],
        },
    )
    assert rejected["accepted"] is False
    assert monthly["period_id"] in rejected["error"]

    result = toolset.dispatch(
        "submit_periods",
        {
            "controlling_summary_pages": {"start_page": 1, "end_page": 1},
            "periods": [annual],
        },
    )
    assert result["accepted"] is True
    assert result["structure"]["controlling_summary_pages"] == {
        "start_page": 1,
        "end_page": 1,
    }


def test_pdf_monthly_controlling_summary_cannot_collapse_to_ttm_only():
    document = _document(5)
    month_headers = (
        "Oct 2024 Nov 2024 Dec 2024 Jan 2025 Feb 2025 Mar 2025 "
        "Apr 2025 May 2025 Jun 2025 Jul 2025 Aug 2025 Sep 2025"
    )
    first_page = document.pages[0]
    header_line = replace(first_page.text_lines[0], text=month_headers)
    document = replace(
        document,
        pages=[
            replace(first_page, text_lines=[header_line, *first_page.text_lines[1:]]),
            *document.pages[1:],
        ],
    )
    toolset = PdfExplorationToolset(document)
    routing = {
        "layout": "consistent_multi_page_statement",
        "page_ranges": [
            {
                "start_page": 1,
                "end_page": 1,
                "include_as_financial_evidence": True,
                "role": "summary_p_and_l",
                "confidence": "high",
                "evidence": ["monthly controlling summary"],
            },
            *[
                {
                    "start_page": page,
                    "end_page": page,
                    "include_as_financial_evidence": True,
                    "role": "department_p_and_l",
                    "confidence": "high",
                    "evidence": [f"normal department {page}"],
                }
                for page in range(2, 6)
            ],
        ],
    }
    assert toolset.dispatch("submit_routing", routing)["accepted"] is True
    for page in range(1, 6):
        toolset.dispatch("read_page_lines", {"page_number": page})

    ttm = {
        "scenario": "actual",
        "start_month": "2024-10",
        "end_month": "2025-09",
        "pages_present": [{"start_page": 1, "end_page": 5}],
        "evidence": ["p1:l1"],
    }
    collapsed = toolset.dispatch(
        "submit_periods",
        {
            "controlling_summary_pages": {"start_page": 1, "end_page": 1},
            "periods": [ttm],
        },
    )
    assert collapsed["accepted"] is False
    assert "2024-10" in collapsed["error"]
    assert "2025-09" in collapsed["error"]

    monthly_periods = []
    for year, month in [(2024, month) for month in range(10, 13)] + [
        (2025, month) for month in range(1, 10)
    ]:
        value = f"{year:04d}-{month:02d}"
        monthly_periods.append(
            {
                "scenario": "actual",
                "start_month": value,
                "end_month": value,
                "pages_present": [{"start_page": 1, "end_page": 5}],
                "evidence": ["p1:l1"],
            }
        )
    complete = toolset.dispatch(
        "submit_periods",
        {
            "controlling_summary_pages": {"start_page": 1, "end_page": 1},
            "periods": [*monthly_periods, ttm],
        },
    )
    assert complete["accepted"] is True
    assert len(complete["structure"]["periods"]) == 13


def test_pdf_routing_rejects_inclusion_role_contradictions():
    toolset = PdfExplorationToolset(_document())
    contradictory = _routing()
    contradictory["page_ranges"][0]["role"] = "balance_sheet"

    result = toolset.dispatch("submit_routing", contradictory)

    assert result["accepted"] is False
    assert "incompatible" in result["error"]


def test_pdf_binding_requires_sample_reads_and_exact_financial_coverage():
    exploration = PdfExploration.model_validate(
        {**_routing(), **_periods(), "notes": []}
    )
    toolset = PdfBindingToolset(
        _document(), exploration, ["2025-01_2025-12_actual"]
    )
    for page in range(1, 6):
        toolset.dispatch("read_page_lines", {"page_number": page})

    incomplete = {
        "bindings": [
            {
                "period_id": "2025-01_2025-12_actual",
                "start_page": 1,
                "end_page": 5,
                "right_edge": 80,
                "header_text": "Actual",
                "evidence": ["p1:l1"],
            }
        ]
    }
    assert toolset.dispatch("submit_bindings", incomplete)["accepted"] is False

    complete = {
        "bindings": [
            {
                "period_id": "2025-01_2025-12_actual",
                "start_page": 1,
                "end_page": 6,
                "right_edge": 80,
                "header_text": "Actual",
                "evidence": ["p1:l1", "p5:l1"],
            }
        ]
    }
    result = toolset.dispatch("submit_bindings", complete)
    assert result["accepted"] is True
    assert result["structure"]["bindings"][0]["right_edge"] == 80


def test_pdf_stage_declarations_have_no_duplicate_tool_names():
    exploration = PdfExploration.model_validate({**_routing(), **_periods()})
    for toolset in (
        PdfExplorationToolset(_document()),
        PdfBindingToolset(
            _document(), exploration, ["2025-01_2025-12_actual"]
        ),
    ):
        names = [tool["name"] for tool in toolset.declarations()]
        assert len(names) == len(set(names))


class _BareFinalClient:
    def generate_json_model_with_tools(self, prompt, response_model, **kwargs):
        return response_model()


def test_pdf_agents_refuse_bare_final_json_that_bypasses_terminal_tools():
    document = _document()
    exploration = PdfExploration.model_validate({**_routing(), **_periods()})
    with pytest.raises(RuntimeError, match="accepted submit_periods"):
        explore_pdf(document, client=_BareFinalClient())
    with pytest.raises(RuntimeError, match="accepted submit_bindings"):
        bind_pdf_periods(
            document,
            exploration,
            client=_BareFinalClient(),
            period_ids=["2025-01_2025-12_actual"],
        )
