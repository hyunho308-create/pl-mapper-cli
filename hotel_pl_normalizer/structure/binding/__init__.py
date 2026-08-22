"""Locating departments and binding periods in one session.

This is the production path after period selection. Set
`FRESH_START_DEPARTMENT_BINDING=0` only to fall back to the older separate
department-ID and period-binding stages.

See `DEPARTMENT_BINDING_PLAN.md` for the design and for the verdict on each rule
in the stages this will eventually replace.
"""

from .adapters import binding_to_location_map, binding_to_selection_maps
from .agent import (
    BindingOutput,
    bind_departments,
    binding_summary,
    render_binding_prompt,
)
from .checks import CheckResult, check_bindings, check_departments
from .column_stats import (
    MIN_NONZERO_TO_CHARACTERISE,
    MIN_NUMERIC_TO_CHARACTERISE,
    ColumnFigures,
    SpanFigures,
    ValueSample,
    column_stats,
)
from .toolset import DepartmentBindingToolset

__all__ = [
    "MIN_NONZERO_TO_CHARACTERISE",
    "MIN_NUMERIC_TO_CHARACTERISE",
    "BindingOutput",
    "CheckResult",
    "ColumnFigures",
    "DepartmentBindingToolset",
    "SpanFigures",
    "ValueSample",
    "bind_departments",
    "binding_summary",
    "binding_to_location_map",
    "binding_to_selection_maps",
    "check_bindings",
    "check_departments",
    "column_stats",
    "render_binding_prompt",
]
