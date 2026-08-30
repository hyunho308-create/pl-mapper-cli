"""Account-label detection shared by mapping evidence."""

from .builder import (
    LabelLayout,
    LabelRegion,
    LabelSelection,
    infer_label_layout,
    is_technical_label,
    select_row_label,
)

__all__ = [
    "LabelLayout",
    "LabelRegion",
    "LabelSelection",
    "infer_label_layout",
    "is_technical_label",
    "select_row_label",
]
