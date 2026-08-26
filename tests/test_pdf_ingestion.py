from __future__ import annotations

from pathlib import Path

import pytest
from reportlab.pdfgen import canvas

from hotel_pl_normalizer.structure.ingestion import read_pdf_document
from hotel_pl_normalizer.structure.ingestion.pdf import parse_displayed_number
from hotel_pl_normalizer.structure.pdf import PdfInspectionToolset, PdfToolError


def _pdf(path: Path) -> Path:
    pdf = canvas.Canvas(str(path), pagesize=(612, 792))
    pdf.setFont("Helvetica-Bold", 12)
    pdf.drawString(40, 750, "Hotel Profit and Loss")
    pdf.setFont("Helvetica", 9)
    pdf.drawString(40, 720, "Department")
    pdf.drawRightString(300, 720, "Jan Actual")
    pdf.drawRightString(400, 720, "Feb Budget")
    pdf.drawString(40, 690, "Rooms Revenue")
    pdf.drawRightString(300, 690, "1,234.50")
    pdf.drawRightString(400, 690, "(900.25)")
    pdf.drawString(40, 670, "Occupancy")
    pdf.drawRightString(300, 670, "72.5%")
    pdf.line(35, 660, 405, 660)
    pdf.showPage()
    pdf.save()
    return path


@pytest.mark.parametrize(
    ("text", "expected", "percent"),
    [
        ("1,234.50", 1234.5, False),
        ("(900.25)", -900.25, False),
        ("72.5%", 72.5, True),
        ("$42-", -42.0, False),
        ("2025-01", None, False),
        ("1/31/2025", None, False),
        ("(12", None, False),
        ("-", None, False),
    ],
)
def test_displayed_number_parser_is_conservative(text, expected, percent):
    assert parse_displayed_number(text) == (expected, percent)


def test_pdf_ingestion_preserves_source_geometry_and_line_coverage(tmp_path):
    path = _pdf(tmp_path / "sample.pdf")

    first = read_pdf_document(path, source_id="sample")
    second = read_pdf_document(path, source_id="sample-again")

    assert first.document_id == second.document_id
    assert len(first.pages) == 1
    page = first.pages[0]
    assert page.width == 612
    assert page.height == 792
    assert any(word.text == "Rooms" for word in page.words)
    assert any(word.numeric_value == -900.25 for word in page.words)
    assert any(word.is_percent for word in page.words)
    assert any(rule.orientation == "horizontal" for rule in page.rules)

    source_ids = [word.word_id for word in page.words if not word.decorative]
    grouped_ids = [word_id for line in page.text_lines for word_id in line.word_ids]
    assert sorted(grouped_ids) == sorted(source_ids)
    assert len(grouped_ids) == len(set(grouped_ids))
    assert [line.line_number for line in page.text_lines] == list(
        range(1, len(page.text_lines) + 1)
    )


def test_pdf_reader_rejects_non_pdf(tmp_path):
    path = tmp_path / "sample.xlsx"
    path.write_bytes(b"not a workbook")
    with pytest.raises(ValueError, match="only supports .pdf"):
        read_pdf_document(path)


def test_pdf_inspection_tools_are_bounded_and_coordinate_aware(tmp_path):
    toolset = PdfInspectionToolset(read_pdf_document(_pdf(tmp_path / "sample.pdf")))

    inspected = toolset.inspect_document()
    assert inspected["page_count"] == 1
    assert inspected["numeric_token_count"] == 3
    assert toolset.list_pages()["pages"][0]["preview"]

    rows = toolset.read_page_lines(1)["lines"]
    rooms = next(line for line in rows if "Rooms Revenue" in line["text"])
    assert [token["number"] for token in rooms["tokens"] if "number" in token] == [
        1234.5,
        -900.25,
    ]
    assert all(token["at"].startswith("p1:w") for token in rooms["tokens"])

    hits = toolset.find_text("rooms revenue")["hits"]
    assert hits[0]["page_number"] == 1
    assert hits[0]["at"].startswith("p1:l")

    anchors = toolset.numeric_anchors(1)["anchors"]
    assert sum(anchor["count"] for anchor in anchors) == 3
    assert any(anchor["percent_tokens"] == 1 for anchor in anchors)

    region = toolset.read_region(1, 0, 0, 612, 792)
    assert len(region["words"]) == inspected["extractable_word_count"]
    with pytest.raises(PdfToolError, match="outside this 1-page PDF"):
        toolset.read_page_lines(2)
    with pytest.raises(PdfToolError, match="limited to 60 lines"):
        toolset.read_page_lines(1, 1, 61)


def test_pdf_declared_tools_match_dispatch_and_signature(tmp_path):
    toolset = PdfInspectionToolset(read_pdf_document(_pdf(tmp_path / "sample.pdf")))
    declared = {item["name"] for item in toolset.declarations()}

    assert declared == {
        "inspect_document",
        "list_pages",
        "read_page_lines",
        "read_region",
        "find_text",
        "numeric_anchors",
    }
    assert toolset.dispatch("inspect_document", {})["ok"] is True
    assert toolset.dispatch("read_page_lines", {"page_number": 1})["ok"] is True
    assert toolset.signature() == toolset.signature()
    assert len(toolset.signature()) == 16
    refused = toolset.dispatch("make_spreadsheet", {})
    assert refused["ok"] is False
    assert "Unknown tool" in refused["error"]
