"""Answer structural questions about a workbook without parsing all of it."""

from .agent import ExplorationOutput, exploration_summary, explore_workbook
from .reader import Cell, LazyWorkbook, SheetSummary
from .toolset import WorkbookExplorationToolset

__all__ = [
    "Cell",
    "ExplorationOutput",
    "LazyWorkbook",
    "SheetSummary",
    "WorkbookExplorationToolset",
    "explore_workbook",
    "exploration_summary",
]
