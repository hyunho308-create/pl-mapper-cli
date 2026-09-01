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
from hotel_pl_normalizer.structure.pdf.agent import PdfStageFailure


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


def _dual_block_document() -> PdfDocumentRecord:
    words = [
        PdfWord("p1:w1", "PERIODIC", 180, 10, 240, 20, bold=True),
        PdfWord("p1:w2", "YEAR", 580, 10, 620, 20, bold=True),
        PdfWord("p1:w3", "TO", 625, 10, 645, 20, bold=True),
        PdfWord("p1:w4", "DATE", 650, 10, 690, 20, bold=True),
        PdfWord("p1:w5", "100", 220, 30, 250, 40, numeric_value=100),
        PdfWord("p1:w6", "1200", 610, 30, 650, 40, numeric_value=1200),
    ]
    lines = [
        PdfTextLine(
            "p1:l1",
            1,
            "PERIODIC YEAR TO DATE",
            180,
            10,
            690,
            20,
            tuple(word.word_id for word in words[:4]),
        ),
        PdfTextLine(
            "p1:l2",
            2,
            "100 1200",
            220,
            30,
            650,
            40,
            tuple(word.word_id for word in words[4:]),
        ),
    ]
    return PdfDocumentRecord(
        document_id="pdf_dual_block",
        source=PdfSource(
            source_id="source",
            original_filename="dual.pdf",
            local_path="dual.pdf",
            file_hash="dual",
            ingested_at=datetime(2026, 8, 25, tzinfo=timezone.utc),
        ),
        pages=[
            PdfPage(
                page_number=1,
                width=800,
                height=600,
                words=words,
                text_lines=lines,
            )
        ],
    )


def _amount_percent_document() -> PdfDocumentRecord:
    words = [
        PdfWord("p1:w1", "Actual", 80, 5, 130, 15, bold=True),
        PdfWord("p1:w2", "Budget", 160, 5, 210, 15, bold=True),
        PdfWord("p1:w3", "AMT", 80, 20, 100, 30, bold=True),
        PdfWord("p1:w4", "%REV", 110, 20, 130, 30, bold=True),
        PdfWord("p1:w5", "AMT", 160, 20, 180, 30, bold=True),
        PdfWord("p1:w6", "%REV", 190, 20, 210, 30, bold=True),
        PdfWord("p1:w7", "1,000", 80, 40, 100, 50, numeric_value=1000),
        PdfWord("p1:w8", "55.5", 110, 40, 130, 50, numeric_value=55.5),
        PdfWord("p1:w9", "900", 160, 40, 180, 50, numeric_value=900),
        PdfWord("p1:w10", "50.0", 190, 40, 210, 50, numeric_value=50),
    ]
    return PdfDocumentRecord(
        document_id="pdf_amount_percent",
        source=PdfSource(
            source_id="source",
            original_filename="amount_percent.pdf",
            local_path="amount_percent.pdf",
            file_hash="amount_percent",
            ingested_at=datetime(2026, 8, 25, tzinfo=timezone.utc),
        ),
        pages=[
            PdfPage(
                page_number=1,
                width=300,
                height=200,
                words=words,
                text_lines=[
                    PdfTextLine(
                        "p1:l1",
                        1,
                        "Actual Budget",
                        80,
                        5,
                        210,
                        15,
                        ("p1:w1", "p1:w2"),
                    ),
                    PdfTextLine(
                        "p1:l2",
                        2,
                        "AMT %REV AMT %REV",
                        80,
                        20,
                        210,
                        30,
                        ("p1:w3", "p1:w4", "p1:w5", "p1:w6"),
                    ),
                    PdfTextLine(
                        "p1:l3",
                        3,
                        "1,000 55.5 900 50.0",
                        80,
                        40,
                        210,
                        50,
                        ("p1:w7", "p1:w8", "p1:w9", "p1:w10"),
                    ),
                ],
            )
        ],
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


def test_pdf_period_submission_ignores_model_authored_identity():
    toolset = PdfExplorationToolset(_document())
    assert toolset.dispatch("submit_routing", _routing())["accepted"] is True
    toolset.dispatch("read_page_lines", {"page_number": 1})
    periods = _periods()
    periods["periods"][0]["period_id"] = "actual_2025"
    periods["periods"][0]["label"] = "FY25 Actual"

    result = toolset.dispatch("submit_periods", periods)

    assert result["accepted"] is True
    canonical = result["structure"]["periods"][0]
    assert canonical["period_id"] == "2025-01_2025-12_actual"
    assert canonical["label"] == "2025 Actual"


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


def test_pdf_annual_period_rejects_the_ptd_side_of_an_explicit_dual_block():
    exploration = PdfExploration.model_validate(
        {
            "layout": "single_page_statement",
            "page_ranges": [
                {
                    "start_page": 1,
                    "end_page": 1,
                    "include_as_financial_evidence": True,
                    "role": "summary_p_and_l",
                    "confidence": "high",
                    "evidence": ["dual block"],
                }
            ],
            "controlling_summary_pages": {"start_page": 1, "end_page": 1},
            "periods": [
                {
                    "scenario": "actual",
                    "start_month": "2025-01",
                    "end_month": "2025-12",
                }
            ],
        }
    )
    period_id = "2025-01_2025-12_actual"
    toolset = PdfBindingToolset(_dual_block_document(), exploration, [period_id])
    toolset.dispatch("read_page_lines", {"page_number": 1})

    wrong = toolset.dispatch(
        "submit_bindings",
        {
            "bindings": [
                {
                    "period_id": period_id,
                    "start_page": 1,
                    "end_page": 1,
                    "right_edge": 250,
                    "header_text": "PERIODIC Actual",
                }
            ]
        },
    )
    assert wrong["accepted"] is False
    assert "YEAR TO DATE/YTD" in wrong["error"]

    correct = toolset.dispatch(
        "submit_bindings",
        {
            "bindings": [
                {
                    "period_id": period_id,
                    "start_page": 1,
                    "end_page": 1,
                    "right_edge": 650,
                    "header_text": "YEAR TO DATE Actual",
                }
            ]
        },
    )
    assert correct["accepted"] is True


def test_pdf_binding_splits_blank_pages_without_discarding_valid_range():
    document = _document()
    document.pages[2] = replace(document.pages[2], words=[], text_lines=[])
    exploration = PdfExploration.model_validate(
        {**_routing(), **_periods(), "notes": []}
    )
    toolset = PdfBindingToolset(
        document, exploration, ["2025-01_2025-12_actual"]
    )
    for page in range(1, 6):
        toolset.dispatch("read_page_lines", {"page_number": page})

    result = toolset.dispatch(
        "submit_bindings",
        {
            "bindings": [
                {
                    "period_id": "2025-01_2025-12_actual",
                    "start_page": 1,
                    "end_page": 6,
                    "right_edge": 80,
                    "header_text": "Actual",
                }
            ],
            "unavailable": [
                {
                    "period_id": "2025-01_2025-12_actual",
                    "start_page": 3,
                    "end_page": 3,
                    "reason": "Blank continuation page.",
                }
            ],
        },
    )

    assert result["accepted"] is True
    assert [
        (item["start_page"], item["end_page"])
        for item in result["structure"]["bindings"]
    ] == [(1, 2), (4, 6)]
    assert "pages without that displayed amount anchor" in result["structure"][
        "notes"
    ][0]


def test_pdf_binding_repair_can_supply_only_missing_page_outcomes():
    document = _document()
    document.pages[2] = replace(document.pages[2], words=[], text_lines=[])
    exploration = PdfExploration.model_validate(
        {**_routing(), **_periods(), "notes": []}
    )
    toolset = PdfBindingToolset(
        document, exploration, ["2025-01_2025-12_actual"]
    )
    for page in range(1, 6):
        toolset.dispatch("read_page_lines", {"page_number": page})

    initial = toolset.dispatch(
        "submit_bindings",
        {
            "bindings": [
                {
                    "period_id": "2025-01_2025-12_actual",
                    "start_page": 1,
                    "end_page": 6,
                    "right_edge": 80,
                    "header_text": "Actual",
                }
            ]
        },
    )
    assert initial["accepted"] is False
    assert "were retained" in initial["error"]

    repaired = toolset.dispatch(
        "submit_bindings",
        {
            "unavailable": [
                {
                    "period_id": "2025-01_2025-12_actual",
                    "start_page": 3,
                    "end_page": 3,
                    "reason": "Blank continuation page.",
                }
            ]
        },
    )

    assert repaired["accepted"] is True
    assert [
        (item["start_page"], item["end_page"])
        for item in repaired["structure"]["bindings"]
    ] == [(1, 2), (4, 6)]


def test_pdf_binding_lists_compact_financial_layout_groups():
    exploration = PdfExploration.model_validate(
        {**_routing(), **_periods(), "notes": []}
    )
    toolset = PdfBindingToolset(
        _document(), exploration, ["2025-01_2025-12_actual"]
    )

    result = toolset.dispatch("list_financial_layouts", {})

    assert result["ok"] is True
    assert result["financial_page_count"] == 6
    assert len(result["layout_groups"]) == 1
    assert result["layout_groups"][0]["page_ranges"] == [
        {"start_page": 1, "end_page": 6}
    ]
    assert result["layout_groups"][0]["common_non_percent_anchors"] == [
        {
            "right_edge": 80.0,
            "pages_present": 6,
            "page_ranges": [{"start_page": 1, "end_page": 6}],
            "scenario_hints": [],
        }
    ]


def test_pdf_layout_binding_expands_to_page_level_bindings():
    exploration = PdfExploration.model_validate(
        {**_routing(), **_periods(), "notes": []}
    )
    period_id = "2025-01_2025-12_actual"
    toolset = PdfBindingToolset(_document(), exploration, [period_id])
    for page in range(1, 6):
        toolset.dispatch("read_page_lines", {"page_number": page})
    layouts = toolset.dispatch("list_financial_layouts", {})["layout_groups"]

    result = toolset.dispatch(
        "submit_layout_bindings",
        {
            "layout_bindings": [
                {
                    "layout_id": layouts[0]["layout_id"],
                    "period_id": period_id,
                    "right_edge": 80,
                    "header_text": "Actual",
                }
            ],
            "layout_unavailable": [],
        },
    )

    assert result["accepted"] is True
    assert result["structure"]["bindings"] == [
        {
            "start_page": 1,
            "end_page": 6,
            "period_id": period_id,
            "right_edge": 80.0,
            "header_text": "Actual",
            "evidence": [],
        }
    ]


def test_pdf_layout_binding_excludes_explicit_ratio_subcolumns():
    period_id = "2025-01_2025-12_actual"
    exploration = PdfExploration.model_validate(
        {
            "layout": "single_page_statement",
            "page_ranges": [
                {
                    "start_page": 1,
                    "end_page": 1,
                    "include_as_financial_evidence": True,
                    "role": "summary_p_and_l",
                    "confidence": "high",
                    "evidence": ["statement"],
                }
            ],
            "controlling_summary_pages": {"start_page": 1, "end_page": 1},
            "periods": [
                {
                    "scenario": "actual",
                    "start_month": "2025-01",
                    "end_month": "2025-12",
                }
            ],
        }
    )
    toolset = PdfBindingToolset(
        _amount_percent_document(), exploration, [period_id]
    )
    toolset.dispatch("read_page_lines", {"page_number": 1})
    layout = toolset.dispatch("list_financial_layouts", {})["layout_groups"][0]
    assert [
        item["right_edge"] for item in layout["common_non_percent_anchors"]
    ] == [100.0, 180.0]
    assert [
        item["scenario_hints"] for item in layout["common_non_percent_anchors"]
    ] == [["actual"], ["budget"]]

    wrong = toolset.dispatch(
        "submit_layout_bindings",
        {
            "layout_bindings": [
                {
                    "layout_id": layout["layout_id"],
                    "period_id": period_id,
                    "right_edge": 130,
                    "header_text": "%REV",
                }
            ],
            "layout_unavailable": [],
        },
    )
    assert wrong["accepted"] is False
    assert "no listed numeric anchor" in wrong["error"]

    wrong_scenario = toolset.dispatch(
        "submit_layout_bindings",
        {
            "layout_bindings": [
                {
                    "layout_id": layout["layout_id"],
                    "period_id": period_id,
                    "right_edge": 180,
                    "header_text": "Budget AMT",
                }
            ],
            "layout_unavailable": [],
        },
    )
    assert wrong_scenario["accepted"] is False
    assert "incompatible" in wrong_scenario["error"]

    correct = toolset.dispatch(
        "submit_layout_bindings",
        {
            "layout_bindings": [
                {
                    "layout_id": layout["layout_id"],
                    "period_id": period_id,
                    "right_edge": 100,
                    "header_text": "AMT",
                }
            ],
            "layout_unavailable": [],
        },
    )
    assert correct["accepted"] is True


def test_pdf_layout_binding_requires_one_outcome_per_layout_period():
    document = _document()
    document.pages[2] = replace(document.pages[2], words=[], text_lines=[])
    exploration = PdfExploration.model_validate(
        {**_routing(), **_periods(), "notes": []}
    )
    period_id = "2025-01_2025-12_actual"
    toolset = PdfBindingToolset(document, exploration, [period_id])
    for page in range(1, 6):
        toolset.dispatch("read_page_lines", {"page_number": page})
    layouts = toolset.dispatch("list_financial_layouts", {})["layout_groups"]
    assert len(layouts) == 2

    incomplete = toolset.dispatch(
        "submit_layout_bindings",
        {
            "layout_bindings": [
                {
                    "layout_id": layouts[0]["layout_id"],
                    "period_id": period_id,
                    "right_edge": 80,
                    "header_text": "Actual",
                }
            ],
            "layout_unavailable": [],
        },
    )
    assert incomplete["accepted"] is False
    assert layouts[1]["layout_id"] in incomplete["error"]

    complete = toolset.dispatch(
        "submit_layout_bindings",
        {
            "layout_bindings": [
                {
                    "layout_id": layouts[0]["layout_id"],
                    "period_id": period_id,
                    "right_edge": 80,
                    "header_text": "Actual",
                }
            ],
            "layout_unavailable": [
                {
                    "layout_id": layouts[1]["layout_id"],
                    "period_id": period_id,
                    "reason": "Blank routed page.",
                }
            ],
        },
    )
    assert complete["accepted"] is True
    assert complete["structure"]["unavailable"] == [
        {
            "start_page": 3,
            "end_page": 3,
            "period_id": period_id,
            "reason": "Blank routed page.",
        }
    ]


def test_pdf_layout_binding_stops_after_two_repairs():
    exploration = PdfExploration.model_validate(
        {**_routing(), **_periods(), "notes": []}
    )
    toolset = PdfBindingToolset(
        _document(), exploration, ["2025-01_2025-12_actual"]
    )
    invalid = {"layout_bindings": [], "layout_unavailable": []}

    for _ in range(3):
        assert toolset.dispatch("submit_layout_bindings", invalid)["accepted"] is False
    with pytest.raises(RuntimeError, match="two repairs"):
        toolset.dispatch("submit_layout_bindings", invalid)


def test_pdf_binding_cannot_mark_a_discovered_period_unavailable_everywhere():
    exploration = PdfExploration.model_validate(
        {**_routing(), **_periods(), "notes": []}
    )
    toolset = PdfBindingToolset(
        _document(), exploration, ["2025-01_2025-12_actual"]
    )
    for page in range(1, 6):
        toolset.dispatch("read_page_lines", {"page_number": page})

    result = toolset.dispatch(
        "submit_bindings",
        {
            "unavailable": [
                {
                    "period_id": "2025-01_2025-12_actual",
                    "start_page": 1,
                    "end_page": 6,
                    "reason": "No binding attempted.",
                }
            ]
        },
    )

    assert result["accepted"] is False
    assert "controlling summary" in result["error"]


def _summary_and_department_exploration() -> PdfExploration:
    return PdfExploration.model_validate(
        {
            "layout": "consistent_multi_page_statement",
            "page_ranges": [
                {
                    "start_page": 1,
                    "end_page": 1,
                    "include_as_financial_evidence": True,
                    "role": "summary_p_and_l",
                    "confidence": "high",
                    "evidence": ["summary"],
                },
                *[
                    {
                        "start_page": page,
                        "end_page": page,
                        "include_as_financial_evidence": True,
                        "role": "department_p_and_l",
                        "confidence": "high",
                        "evidence": ["department"],
                    }
                    for page in range(2, 7)
                ],
            ],
            "controlling_summary_pages": {"start_page": 1, "end_page": 1},
            "periods": _periods()["periods"],
        }
    )


def test_pdf_unavailable_range_cannot_cross_routed_statement_boundaries():
    toolset = PdfBindingToolset(
        _document(),
        _summary_and_department_exploration(),
        ["2025-01_2025-12_actual"],
    )
    for page in range(1, 6):
        toolset.dispatch("read_page_lines", {"page_number": page})

    result = toolset.dispatch(
        "submit_bindings",
        {
            "bindings": [
                {
                    "period_id": "2025-01_2025-12_actual",
                    "start_page": 1,
                    "end_page": 1,
                    "right_edge": 80,
                    "header_text": "Actual",
                }
            ],
            "unavailable": [
                {
                    "period_id": "2025-01_2025-12_actual",
                    "start_page": 2,
                    "end_page": 6,
                    "reason": "Broad fallback.",
                }
            ],
        },
    )

    assert result["accepted"] is False
    assert "crosses routed statement boundaries" in result["error"]


def test_pdf_core_period_requires_a_department_binding():
    toolset = PdfBindingToolset(
        _document(),
        _summary_and_department_exploration(),
        ["2025-01_2025-12_actual"],
    )
    for page in range(1, 6):
        toolset.dispatch("read_page_lines", {"page_number": page})

    result = toolset.dispatch(
        "submit_bindings",
        {
            "bindings": [
                {
                    "period_id": "2025-01_2025-12_actual",
                    "start_page": 1,
                    "end_page": 1,
                    "right_edge": 80,
                    "header_text": "Actual",
                }
            ],
            "unavailable": [
                {
                    "period_id": "2025-01_2025-12_actual",
                    "start_page": page,
                    "end_page": page,
                    "reason": "Not established.",
                }
                for page in range(2, 7)
            ],
        },
    )

    assert result["accepted"] is False
    assert "no usable binding on any routed department" in result["error"]


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
    binding_names = [
        tool["name"]
        for tool in PdfBindingToolset(
            _document(), exploration, ["2025-01_2025-12_actual"]
        ).declarations()
    ]
    assert "submit_layout_bindings" in binding_names
    assert "submit_bindings" not in binding_names


class _BareFinalClient:
    usage_history = []

    def generate_json_model_with_tools(self, prompt, response_model, **kwargs):
        return response_model()


def test_pdf_agents_refuse_bare_final_json_that_bypasses_terminal_tools():
    document = _document()
    exploration = PdfExploration.model_validate({**_routing(), **_periods()})
    with pytest.raises(RuntimeError, match="accepted submit_periods"):
        explore_pdf(document, client=_BareFinalClient())
    with pytest.raises(RuntimeError, match="accepted submit_layout_bindings"):
        bind_pdf_periods(
            document,
            exploration,
            client=_BareFinalClient(),
            period_ids=["2025-01_2025-12_actual"],
        )


class _FailedPdfClient:
    usage_history = [{"status": "pass", "prompt_token_count": 10}]

    def generate_json_model_with_tools(self, prompt, response_model, **kwargs):
        kwargs["trace"].append({"tool": "list_pages", "ok": True})
        raise RuntimeError("tool loop exhausted")


def test_pdf_stage_failure_retains_paid_trace_and_usage():
    with pytest.raises(PdfStageFailure) as caught:
        explore_pdf(_document(), client=_FailedPdfClient())

    assert caught.value.diagnostics["stage"] == "pdf_period_discovery"
    assert caught.value.diagnostics["model_calls"][0]["prompt_token_count"] == 10
    assert caught.value.diagnostics["tool_trace"] == [
        {"tool": "list_pages", "ok": True}
    ]
