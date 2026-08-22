"""Translate exploration into the catalog and routing artifacts downstream uses."""

from __future__ import annotations

from hotel_pl_normalizer.models.exploration import WorkbookExploration
from hotel_pl_normalizer.models.period_selection import (
    PeriodCatalog,
    PeriodOption,
)
from hotel_pl_normalizer.models.sheet_selection import (
    SheetNameDecision,
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
            period_type=period.period_type,
            start_period=period.start_period,
            end_period=period.end_period,
        )
        for period in exploration.periods
    ]
    recommended = exploration.recommended_period_id
    known = {option.period_id for option in options}
    if recommended not in known:
        # A recommendation naming a period that is not in the list is worse than
        # none: every consumer of it would have to guard, and the first option is
        # not a safe substitute when the model meant something specific.
        recommended = None
    return PeriodCatalog(
        catalog_id=catalog_id or f"{workbook_id}:exploration",
        workbook_id=workbook_id,
        options=options,
        recommended_period_id=recommended,
        notes=list(exploration.notes),
    )


def exploration_to_sheet_selection(
    exploration: WorkbookExploration, *, workbook_id: str
) -> SheetNameSelectionResult:
    """The routing decisions exploration made, in sheet routing's shape."""
    selections = [
        SheetNameSelection(
            sheet_name=sheet.sheet_name,
            decision=sheet.decision,
            role_hint=sheet.role_hint,
            evidence=list(sheet.evidence),
        )
        for sheet in exploration.sheets
    ]

    def named(decision: SheetNameDecision) -> list[str]:
        return [s.sheet_name for s in exploration.sheets if s.decision == decision]

    return SheetNameSelectionResult(
        workbook_id=workbook_id,
        workbook_layout=exploration.workbook_layout,
        layout_evidence=list(exploration.layout_evidence),
        selections=selections,
        selected_sheet_names=named(SheetNameDecision.TRIAGE),
        deferred_sheet_names=named(SheetNameDecision.DEFER),
        skipped_sheet_names=named(SheetNameDecision.SKIP),
        unsure_sheet_names=named(SheetNameDecision.UNSURE),
        notes=list(exploration.notes),
    )
