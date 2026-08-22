"""Period/value-column detection layer.

Split by what each part is responsible for, in dependency order:

    signals     what a header, column or sheet looks like -- the predicates the
                rest of this package has to agree on
    packets     reduce a workbook to what a period stage can reason about
    prompts     what the model is asked, and the compact views embedded in it
    validation  could this answer be true of this workbook?
    catalog     a validated catalog turned into mapping-stage selections, plus
                the local model-free backend
    repair      fold targeted repair patches back into a catalog

This was one 3,693-line module. Nothing here changed in the split; the boundaries
follow the call graph that was already there.
"""

from .catalog import (
    LocalPeriodCatalogBackend,
    catalog_to_selection_map,
    prepare_selected_period_catalog,
    selection_for_sheet,
)
from .packets import (
    build_period_column_packet,
    build_workbook_period_discovery_packet,
)
from .prompts import (
    render_period_catalog_prompt,
    render_period_catalog_repair_prompt,
    render_selected_period_binding_prompt,
    render_selected_period_binding_repair_prompt,
)
from .repair import (
    align_unambiguous_period_bindings,
    merge_period_catalog_repair,
    merge_selected_period_binding_repair,
    normalize_obvious_supporting_assessments,
)
from .validation import (
    validate_discovery_period_catalog,
    validate_period_catalog,
    validate_period_column_selection,
    validate_selected_period_catalog,
)

__all__ = [
    "align_unambiguous_period_bindings",
    "LocalPeriodCatalogBackend",
    "build_period_column_packet",
    "build_workbook_period_discovery_packet",
    "catalog_to_selection_map",
    "render_period_catalog_prompt",
    "render_period_catalog_repair_prompt",
    "merge_period_catalog_repair",
    "normalize_obvious_supporting_assessments",
    "render_selected_period_binding_prompt",
    "render_selected_period_binding_repair_prompt",
    "merge_selected_period_binding_repair",
    "prepare_selected_period_catalog",
    "selection_for_sheet",
    "validate_period_catalog",
    "validate_discovery_period_catalog",
    "validate_period_column_selection",
    "validate_selected_period_catalog",
]
