from __future__ import annotations

import unittest

from hotel_pl_normalizer.models.workbook import CellRecord
from hotel_pl_normalizer.structure.representation import label_cell


def _text_cell(column: int, value: str) -> CellRecord:
    return CellRecord(
        row=1,
        column=column,
        address=f"R1C{column}",
        raw_value=value,
        display_value=value,
    )


class LabelCellTests(unittest.TestCase):
    def test_rescues_uppercase_business_caption_in_preferred_column(self) -> None:
        for caption in ("SPA", "FEES", "AAA WD", "AAA WE"):
            with self.subTest(caption=caption):
                cell = _text_cell(14, caption)
                self.assertIs(label_cell([cell], preferred_column=14), cell)

    def test_does_not_rescue_technical_member_codes(self) -> None:
        for caption in (
            "V361021",
            "ACCOUNT_STYLE",
            "[schedule].[Income Statement]",
            "%,R,FACCOUNT,TFULLSRV_ACCOUNT",
        ):
            with self.subTest(caption=caption):
                cell = _text_cell(14, caption)
                self.assertIsNone(label_cell([cell], preferred_column=14))

    def test_falls_back_to_strict_caption_outside_preferred_column(self) -> None:
        metadata = _text_cell(9, "V361021")
        caption = _text_cell(21, "Water Expense - Municipal Water")

        self.assertIs(
            label_cell([metadata, caption], preferred_column=9),
            caption,
        )

    def test_strict_preferred_caption_still_wins(self) -> None:
        preferred = _text_cell(9, "Rooms Revenue")
        alternate = _text_cell(21, "Water Expense - Municipal Water")

        self.assertIs(
            label_cell([preferred, alternate], preferred_column=9),
            preferred,
        )


if __name__ == "__main__":
    unittest.main()
