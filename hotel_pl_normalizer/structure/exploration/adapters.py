"""Translate exploration into the catalog and routing artifacts downstream uses."""

from __future__ import annotations

from hotel_pl_normalizer.models.exploration import WorkbookExploration
from hotel_pl_normalizer.models.period_selection import (
    PeriodCatalog,
    PeriodOption,
)
from hotel_pl_normalizer.models.sheet_selection import (
    SheetNameSelection,
    SheetNameSelectionResult,
)


def exploration_to_period_catalog(
    exploration: WorkbookExploration,
    *,
    workbook_id: str,
    catalog_id: str | None = None,
) -> PeriodCatalog:
    """The periods exploration found, ready for interactive selection."""
    options = [
        PeriodOption(
            period_id=period.period_id,
            label=period.label,
            scenario=period.scenario,
            start_month=period.start_month,
            end_month=period.end_month,
            actual_months=period.actual_months,
        )
        for period in exploration.periods
    ]
    return PeriodCatalog(
        catalog_id=catalog_id or f"{workbook_id}:exploration",
        workbook_id=workbook_id,
        controlling_summary_sheet=exploration.controlling_summary_sheet,
        options=options,
        notes=list(exploration.notes),
    )


def exploration_to_sheet_selection(
    exploration: WorkbookExploration, *, workbook_id: str
) -> SheetNameSelectionResult:
    """The routing decisions exploration made, in sheet routing's shape."""
    selections = [
        SheetNameSelection(
            sheet_name=sheet.sheet_name,
            include_as_financial_evidence=sheet.include_as_financial_evidence,
            role=sheet.role,
            confidence=sheet.confidence,
            evidence=list(sheet.evidence),
        )
        for sheet in exploration.sheets
    ]
    return SheetNameSelectionResult(
        workbook_id=workbook_id,
        workbook_layout=exploration.workbook_layout,
        layout_evidence=list(exploration.layout_evidence),
        selections=selections,
        notes=list(exploration.notes),
    )
