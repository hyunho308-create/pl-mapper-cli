"""A rename that Windows transiently denies must not fail the run.

This is the bug these cover: full-suite runs failed about one time in two, on a
different innocent test each time, with

    PermissionError: [WinError 5] Access is denied: '...tmp....xlsx' -> '...o.xlsx'

raised from the rename in `write_normalized_workbook`. Nothing here holds either
file -- every handle is closed first -- so the handle belongs to another process
on the machine, and a freshly written `.xlsx` is what a scanner opens first. It is
a race, which is why it only appeared under load.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from hotel_pl_normalizer import atomic


def _write(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


def test_a_clean_replace_still_happens_once(tmp_path, monkeypatch):
    calls = []
    real = os.replace

    def counting(src, dst):
        calls.append((src, dst))
        return real(src, dst)

    monkeypatch.setattr(atomic.os, "replace", counting)
    source = _write(tmp_path / "new.txt", "new")
    target = _write(tmp_path / "live.txt", "old")

    atomic.replace_atomically(source, target)

    assert target.read_text(encoding="utf-8") == "new"
    assert not source.exists()
    assert len(calls) == 1, "the common path must not pay for the retry"


def test_a_transient_denial_is_retried_rather_than_raised(tmp_path, monkeypatch):
    """The scanner lets go after a few milliseconds; the write should still land."""
    real = os.replace
    attempts = {"n": 0}

    def denied_twice(src, dst):
        attempts["n"] += 1
        if attempts["n"] <= 2:
            raise PermissionError(5, "Access is denied")
        return real(src, dst)

    monkeypatch.setattr(atomic.os, "replace", denied_twice)
    monkeypatch.setattr(atomic.time, "sleep", lambda _: None)
    source = _write(tmp_path / "new.txt", "new")
    target = _write(tmp_path / "live.txt", "old")

    atomic.replace_atomically(source, target)

    assert attempts["n"] == 3
    assert target.read_text(encoding="utf-8") == "new"


def test_a_permission_error_that_persists_is_still_raised(tmp_path, monkeypatch):
    """Retrying for ever would turn a real permission problem into a hang."""
    def always_denied(src, dst):
        raise PermissionError(5, "Access is denied")

    monkeypatch.setattr(atomic.os, "replace", always_denied)
    monkeypatch.setattr(atomic.time, "sleep", lambda _: None)
    source = _write(tmp_path / "new.txt", "new")
    target = _write(tmp_path / "live.txt", "old")

    with pytest.raises(PermissionError):
        atomic.replace_atomically(source, target)

    assert target.read_text(encoding="utf-8") == "old"


def test_other_os_errors_are_not_retried(tmp_path, monkeypatch):
    """A missing source will not fix itself, and retrying only delays the report."""
    attempts = {"n": 0}

    def missing(src, dst):
        attempts["n"] += 1
        raise FileNotFoundError(2, "No such file")

    monkeypatch.setattr(atomic.os, "replace", missing)
    monkeypatch.setattr(atomic.time, "sleep", lambda _: None)

    with pytest.raises(FileNotFoundError):
        atomic.replace_atomically(tmp_path / "absent.txt", tmp_path / "live.txt")

    assert attempts["n"] == 1
