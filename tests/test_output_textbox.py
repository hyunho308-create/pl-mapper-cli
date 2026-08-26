import re
import zipfile

from openpyxl import load_workbook

from hotel_pl_normalizer.output import TEMPLATE, _restore_template_textbox


def test_restored_textbox_uses_template_drawing_relationship(tmp_path):
    path = tmp_path / "output.xlsx"
    load_workbook(TEMPLATE).save(path)

    _restore_template_textbox(path)

    with zipfile.ZipFile(path) as archive:
        sheet_xml = archive.read("xl/worksheets/sheet3.xml")
        rels_xml = archive.read("xl/worksheets/_rels/sheet3.xml.rels")
        drawing_xml = archive.read("xl/drawings/drawing1.xml")

    sheet_drawing_id = re.search(
        rb'drawing\b[^>]*r:id="([^"]+)"', sheet_xml
    )
    relationship_id = re.search(
        rb'<Relationship\b(?=[^>]*\bType="[^"]*/drawing")'
        rb'[^>]*\bId="([^"]+)"',
        rels_xml,
    )
    assert sheet_drawing_id is not None
    assert relationship_id is not None
    assert sheet_drawing_id.group(1) == relationship_id.group(1)
    assert b"TextBox 1" in drawing_xml
