"""Locate departments and bind selected periods in one session."""

from .adapters import binding_to_location_map, binding_to_selection_maps
from .agent import bind_departments

__all__ = [
    "bind_departments",
    "binding_to_location_map",
    "binding_to_selection_maps",
]
