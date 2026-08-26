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


def _word(word_id, text, x0, x1, *, value=None, percent=False, bold=False):
    return PdfWord(
        word_id=word_id,
        text=text,
        x0=x0,
        top=20,
        x1=x1,
        bottom=30,
        numeric_value=value,
        is_percent=percent,
        bold=bold,
    )


def test_pdf_evidence_uses_bound_amount_anchor_without_creating_cells():
    words = [
        _word("w1", "Rooms", 20, 55, bold=True),
        _word("w2", "Revenue", 60, 105, bold=True),
        _word("w3", "1,250", 200, 240, value=1250.0),
        _word("w4", "75.0%", 250, 290, value=75.0, percent=True),
    ]
    page = PdfPage(
        page_number=1,
        width=612,
        height=792,
        words=words,
        text_lines=[
            PdfTextLine(
                line_id="p1:l1",
                line_number=1,
                text="Rooms Revenue 1,250 75.0%",
                x0=20,
                top=20,
                x1=290,
                bottom=30,
                word_ids=tuple(word.word_id for word in words),
            )
        ],
    )
    document = PdfDocumentRecord(
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
    bindings = PdfBindings(
        bindings=[
            PdfPeriodAnchorBinding(
                period_id="fy2025",
                start_page=1,
                end_page=1,
                right_edge=240,
                header_text="Year to Date Actual",
            )
        ]
    )

    evidence = compact_pdf_evidence(document, bindings, period_ids=["fy2025"])

    assert evidence == [
        {
            "row_key": "Page 001!1",
            "label": "Rooms Revenue",
            "selected_value_columns": {"fy2025": "x=240.000"},
            "selected_values": {"fy2025": 1250.0},
            "selected_value_column": "x=240.000",
            "selected_value": 1250.0,
            "indent": None,
            "bold": True,
            "pdf_source": {"page": 1, "line_id": "p1:l1", "top": 20},
        }
    ]
