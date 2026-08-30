from __future__ import annotations

import csv
from pathlib import Path

COA_PATH = (
    Path(__file__).resolve().parents[1]
    / "hotel_pl_normalizer"
    / "data"
    / "coa_v2.csv"
)


def test_rooms_other_segment_accounts_are_explicit_non_residual_children() -> None:
    with COA_PATH.open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))

    by_id = {row["coa_id"]: row for row in rows}
    ids = [row["coa_id"] for row in rows]
    expected = {
        "S1.other_transient_rooms_revenue": "S1.transient_rooms_revenue",
        "S1.other_group_rooms_revenue": "S1.group_rooms_revenue",
    }

    assert len(ids) == 271
    assert len(ids) == len(set(ids))
    for coa_id, parent_id in expected.items():
        assert by_id[coa_id]["parent_coa_id"] == parent_id
        assert by_id[coa_id]["is_residual"].casefold() == "false"
        assert "never use as a plug" in by_id[coa_id]["mapping_note"].casefold()
        assert ids.index(coa_id) + 1 == ids.index(parent_id)
