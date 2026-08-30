"""The structure and mapping stages read one workbook between them.

Both used to read the file separately -- a full parse each, 26s on the largest
reviewed workbook -- so `SharedWorkbook` hands one read to both. The record is the
expensive object, 580 MB on a 1.4 MB workbook, so who holds it and for how long is
the point of the type, and that is what these cover.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hotel_pl_normalizer import pipeline

SENTINEL = object()


def _counting_reader(calls: list[Path]):
    def read(path, **_):
        calls.append(Path(path))
        return SENTINEL

    return read


def test_shared_workbook_defers_the_read_until_a_stage_asks(monkeypatch):
    calls: list[Path] = []
    monkeypatch.setattr(pipeline, "read_excel_workbook", _counting_reader(calls))

    pipeline.shared_workbook(Path("never-opened.xlsx"))

    # Constructing the handle must not touch the file: a caller hands it to both
    # stages before either has decided it needs one.
    assert calls == []


def test_both_stages_share_a_single_read(monkeypatch):
    calls: list[Path] = []
    monkeypatch.setattr(pipeline, "read_excel_workbook", _counting_reader(calls))
    handle = pipeline.shared_workbook(Path("book.xlsx"))

    structure_stage = handle.require()
    mapping_stage = handle.require()

    assert structure_stage is SENTINEL
    assert mapping_stage is structure_stage
    assert len(calls) == 1


def test_releasing_drops_the_record_so_the_mapping_session_can_free_it(monkeypatch):
    calls: list[Path] = []
    monkeypatch.setattr(pipeline, "read_excel_workbook", _counting_reader(calls))
    handle = pipeline.shared_workbook(Path("book.xlsx"))
    handle.require()

    handle.release()

    # The whole reason for the handle: after release nothing here still refers to
    # the record, so the mapping stage's own `del` is the last reference.
    assert handle.record is None


def test_using_a_released_workbook_fails_loudly(monkeypatch):
    calls: list[Path] = []
    monkeypatch.setattr(pipeline, "read_excel_workbook", _counting_reader(calls))
    handle = pipeline.shared_workbook(Path("book.xlsx"))
    handle.require()
    handle.release()

    # Silently re-reading here would reintroduce the second parse this removed,
    # and do it invisibly.
    with pytest.raises(ValueError, match="already released"):
        handle.require()
    assert len(calls) == 1
