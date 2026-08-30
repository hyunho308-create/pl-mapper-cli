from datetime import datetime, timezone

from hotel_pl_normalizer.mapping.pdf_evidence import compact_pdf_evidence
from hotel_pl_normalizer.models.pdf import (
    PdfDocumentRecord,
    PdfPage,
    PdfSource,
    PdfTextLine,
    PdfWord,
)
from hotel_pl_normalizer.models.pdf_structure import PdfBindings, PdfPeriodAnchorBinding


def _word(
    word_id,
    text,
    x0,
    x1,
    *,
    value=None,
    percent=False,
    bold=False,
    top=20,
):
    return PdfWord(
        word_id=word_id,
        text=text,
        x0=x0,
        top=top,
        x1=x1,
        bottom=top + 10,
        numeric_value=value,
        is_percent=percent,
        bold=bold,
    )


def _page(*line_words: list[PdfWord], width=612) -> PdfPage:
    words = [word for items in line_words for word in items]
    lines = [
        PdfTextLine(
            line_id=f"p1:l{number}",
            line_number=number,
            text=" ".join(word.text for word in items),
            x0=min(word.x0 for word in items),
            top=min(word.top for word in items),
            x1=max(word.x1 for word in items),
            bottom=max(word.bottom for word in items),
            word_ids=tuple(word.word_id for word in items),
        )
        for number, items in enumerate(line_words, start=1)
    ]
    return PdfPage(
        page_number=1,
        width=width,
        height=792,
        words=words,
        text_lines=lines,
    )


def _document(page: PdfPage) -> PdfDocumentRecord:
    return PdfDocumentRecord(
        document_id="pdf_test",
        source=PdfSource(
            source_id="test",
            original_filename="test.pdf",
            local_path="test.pdf",
            file_hash="abc",
            ingested_at=datetime.now(timezone.utc),
        ),
        pages=[page],
    )


def _bindings(**period_anchors: float) -> PdfBindings:
    return PdfBindings(
        bindings=[
            PdfPeriodAnchorBinding(
                period_id=period_id,
                start_page=1,
                end_page=1,
                right_edge=anchor,
                header_text=period_id,
            )
            for period_id, anchor in period_anchors.items()
        ]
    )


def test_pdf_evidence_uses_bound_amount_anchor_without_creating_cells():
    page = _page(
        [
            _word("w1", "Rooms", 20, 55, bold=True),
            _word("w2", "Revenue", 60, 105, bold=True),
            _word("w3", "1,250", 200, 240, value=1250.0),
            _word("w4", "75.0%", 250, 290, value=75.0, percent=True),
        ]
    )

    evidence = compact_pdf_evidence(
        _document(page), _bindings(fy2025=240), period_ids=["fy2025"]
    )

    assert evidence == [
        {
            "row_key": "Page 001!1",
            "label": "Rooms Revenue",
            "selected_value_columns": {"fy2025": "x=240.000"},
            "selected_values": {"fy2025": 1250.0},
            "selected_value_column": "x=240.000",
            "selected_value": 1250.0,
            "indent": 0.0,
            "bold": True,
            "label_x0": 20,
            "label_rule": "primary",
            "label_status": "selected",
            "label_context": [],
            "pdf_source": {"page": 1, "line_id": "p1:l1", "top": 20},
        }
    ]


def test_center_label_is_used_for_ptd_values_to_its_left_and_ytd_to_its_right():
    page = _page(
        [
            _word("p1", "100", 70, 100, value=100.0),
            _word("l1", "Rooms", 220, 255),
            _word("l2", "Revenue", 260, 305),
            _word("y1", "1,200", 450, 490, value=1200.0),
        ],
        [
            _word("p2", "40", 75, 100, value=40.0, top=40),
            _word("l3", "Food", 220, 250, top=40),
            _word("l4", "Revenue", 255, 300, top=40),
            _word("y2", "500", 455, 490, value=500.0, top=40),
        ],
    )

    evidence = compact_pdf_evidence(
        _document(page),
        _bindings(ptd=100, ytd=490),
        period_ids=["ptd", "ytd"],
    )

    assert [item["label"] for item in evidence] == ["Rooms Revenue", "Food Revenue"]
    assert evidence[0]["selected_values"] == {"ptd": 100.0, "ytd": 1200.0}
    assert evidence[1]["selected_values"] == {"ptd": 40.0, "ytd": 500.0}


def test_far_right_total_uses_left_label_across_monthly_values():
    monthly = [
        _word(
            f"m{index}",
            str(index),
            80 + index * 30,
            100 + index * 30,
            value=float(index),
        )
        for index in range(1, 13)
    ]
    total = _word("total", "78", 550, 580, value=78.0)
    page = _page([_word("label", "Revenue", 20, 65), *monthly, total])

    evidence = compact_pdf_evidence(
        _document(page), _bindings(total=580), period_ids=["total"]
    )

    assert evidence[0]["label"] == "Revenue"
    assert evidence[0]["selected_value"] == 78.0


def test_evidence_uses_binding_tolerance_instead_of_the_old_point_75_cutoff():
    page = _page(
        [
            _word("label", "Revenue", 20, 65),
            _word("value", "1,000", 200, 242.5, value=1000.0),
        ]
    )

    evidence = compact_pdf_evidence(
        _document(page), _bindings(actual=240), period_ids=["actual"]
    )

    assert evidence[0]["selected_value"] == 1000.0


def test_nearly_equidistant_amounts_are_left_unmatched():
    page = _page(
        [
            _word("label", "Revenue", 20, 65),
            _word("left", "100", 180, 238, value=100.0),
            _word("right", "200", 205, 242, value=200.0),
        ]
    )

    evidence = compact_pdf_evidence(
        _document(page), _bindings(actual=240), period_ids=["actual"]
    )

    assert evidence == []


def test_adjacent_indentation_and_stable_local_override_are_supported():
    page = _page(
        [_word("l1", "Revenue", 20, 65), _word("v1", "100", 210, 240, value=100.0)],
        [
            _word("l2", "Rooms", 35, 70, top=40),
            _word("v2", "60", 215, 240, value=60.0, top=40),
        ],
        [
            _word("l3", "Expenses", 20, 70, top=60),
            _word("v3", "50", 215, 240, value=50.0, top=60),
        ],
        [
            _word("l4", "Electricity", 100, 160, top=80),
            _word("v4", "20", 215, 240, value=20.0, top=80),
        ],
        [
            _word("l5", "Water", 100, 135, top=100),
            _word("v5", "10", 215, 240, value=10.0, top=100),
        ],
    )

    evidence = compact_pdf_evidence(
        _document(page), _bindings(actual=240), period_ids=["actual"]
    )

    assert [item["label"] for item in evidence] == [
        "Revenue",
        "Rooms",
        "Expenses",
        "Electricity",
        "Water",
    ]
    assert evidence[1]["label_rule"] == "adjacent_indent"
    assert evidence[3]["label_rule"] == "local_override"
    assert evidence[4]["label_rule"] == "local_override"
