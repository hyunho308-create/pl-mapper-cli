from __future__ import annotations

import unittest

from hotel_pl_normalizer.models.workbook import CellRecord, WorkbookRow
from hotel_pl_normalizer.structure.representation import (
    infer_label_layout,
    is_technical_label,
    select_row_label,
)


def _cell(row: int, column: int, value: str | float) -> CellRecord:
    return CellRecord(
        row=row,
        column=column,
        address=f"R{row}C{column}",
        raw_value=value,
        display_value=str(value),
    )


def _row(index: int, *cells: CellRecord) -> WorkbookRow:
    return WorkbookRow(row_index=index, cells=list(cells))


class LabelLayoutTests(unittest.TestCase):
    def test_primary_column_accepts_short_uppercase_business_captions(self) -> None:
        rows = [
            _row(
                1, _cell(1, 1, "%,V361021"), _cell(1, 14, "AAA WD"), _cell(1, 15, 100.0)
            ),
            _row(
                2, _cell(2, 1, "%,V361521"), _cell(2, 14, "AAA WE"), _cell(2, 15, 200.0)
            ),
            _row(3, _cell(3, 1, "%,V361999"), _cell(3, 14, "FEES"), _cell(3, 15, 30.0)),
        ]

        layout = infer_label_layout(rows, value_columns={15})

        self.assertEqual(layout.primary_column, 14)
        self.assertEqual(select_row_label(rows[0], layout).cell.raw_value, "AAA WD")
        self.assertEqual(select_row_label(rows[2], layout).cell.raw_value, "FEES")

    def test_label_column_can_be_between_ptd_and_ytd_values(self) -> None:
        rows = [
            _row(
                1, _cell(1, 2, 10.0), _cell(1, 5, "Rooms Revenue"), _cell(1, 9, 100.0)
            ),
            _row(2, _cell(2, 2, 20.0), _cell(2, 5, "Food Revenue"), _cell(2, 9, 200.0)),
        ]

        layout = infer_label_layout(rows, value_columns={2, 9})

        self.assertEqual(layout.primary_column, 5)
        self.assertEqual(
            select_row_label(rows[1], layout).cell.raw_value, "Food Revenue"
        )

    def test_adjacent_column_is_treated_as_indentation(self) -> None:
        rows = [
            _row(1, _cell(1, 5, "ROOMS"), _cell(1, 9, 100.0)),
            _row(2, _cell(2, 6, "Transient"), _cell(2, 9, 60.0)),
            _row(3, _cell(3, 5, "TOTAL ROOMS"), _cell(3, 9, 160.0)),
        ]
        layout = infer_label_layout(rows, value_columns={9})

        selection = select_row_label(rows[1], layout)

        self.assertEqual(layout.primary_column, 5)
        self.assertEqual(selection.cell.raw_value, "Transient")
        self.assertEqual(selection.rule, "adjacent_indent")

    def test_two_row_local_override_is_allowed_but_one_off_is_not(self) -> None:
        rows = [
            _row(1, _cell(1, 5, "Rooms Revenue"), _cell(1, 9, 100.0)),
            _row(2, _cell(2, 5, "Food Revenue"), _cell(2, 9, 80.0)),
            _row(3, _cell(3, 12, "Electricity"), _cell(3, 9, 20.0)),
            _row(4, _cell(4, 12, "Natural Gas"), _cell(4, 9, 10.0)),
            _row(6, _cell(6, 15, "One-off note"), _cell(6, 9, 5.0)),
        ]
        layout = infer_label_layout(rows, value_columns={9})

        self.assertEqual(len(layout.overrides), 1)
        self.assertEqual(
            select_row_label(rows[2], layout).cell.raw_value, "Electricity"
        )
        self.assertEqual(select_row_label(rows[3], layout).rule, "local_override")
        self.assertIsNone(select_row_label(rows[4], layout).cell)

    def test_technical_syntax_is_narrowly_rejected(self) -> None:
        for value in (
            "%,R,FACCOUNT,TFULLSRV_ACCOUNT",
            "V361021",
            "ACCOUNT_STYLE",
            "[schedule].[Income Statement]",
            "<<Member[Account]>>",
            "#DIV/0!",
        ):
            with self.subTest(value=value):
                self.assertTrue(is_technical_label(value))
        for value in ("SPA", "FEES", "AAA WD", "A&G", "TOTAL EXPENSES"):
            with self.subTest(value=value):
                self.assertFalse(is_technical_label(value))


if __name__ == "__main__":
    unittest.main()
