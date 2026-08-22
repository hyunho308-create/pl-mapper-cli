"""Compact sheet/range representation for routed workbook regions.

`dominant_label_column` and `label_cell` are exported because the mapping stage
needs the same answer this package gives: which column on a sheet holds the
account label, and which cell in a row is that label. Two implementations would
mean the evidence handed to the mapper could disagree with the packets the
structure stages reasoned about, for the same sheet.
"""

from .builder import (
    build_compact_sheet_packet,
    build_compact_sheet_packets,
    dominant_label_column,
    label_cell,
)

__all__ = [
    "build_compact_sheet_packet",
    "build_compact_sheet_packets",
    "dominant_label_column",
    "label_cell",
]
