"""Present one exploration result in the shapes the pipeline already speaks.

Exploration answers what period discovery and sheet routing answered between
them, so nothing downstream has to change: the catalog goes where discovery's
went, the routing goes where routing's went, and period selection, department
identification and mapping carry on unaware.

Two things make this a translation rather than a rewrite.

Discovery's options carry `bindings` -- a guess at which column holds each period
-- and exploration deliberately produces none, because committing a period to a
column on each tab needs the department locations that do not exist yet at that
point. That guess was never load-bearing: `prepare_selected_period_catalog`
keeps discovery's *concepts* and replaces the bindings wholesale with what the
binding stage returns. Empty is what the field is worth here, and saying so is
more honest than inventing a column.

`sheet_assessments` is the reverse: discovery reported a role for each of the
five sheets it sampled, and exploration has a decision for every sheet in the
workbook. The decisions map onto the roles directly, so the assessment list
covers the whole workbook instead of a sample.
"""

from __future__ import annotations

from hotel_pl_normalizer.models.exploration import WorkbookExploration
from hotel_pl_normalizer.models.period_selection import (
    PeriodCatalog,
    PeriodOption,
    PeriodSheetAssessment,
    PeriodSheetRole,
)
from hotel_pl_normalizer.models.sheet_selection import (
    SheetNameDecision,
    SheetNameSelection,
    SheetNameSelectionResult,
)

# What a routing decision says about a sheet's role in period discovery. `triage`
# is a sheet worth reading, which is what `primary_core` meant; `unsure` is the
# same claim held less firmly, so it is offered as an alternate rather than
# dropped. Everything else is support.
_DECISION_TO_ROLE = {
    SheetNameDecision.TRIAGE: PeriodSheetRole.PRIMARY_CORE,
    SheetNameDecision.UNSURE: PeriodSheetRole.ALTERNATE_CORE,
}


def exploration_to_period_catalog(
    exploration: WorkbookExploration,
    *,
    workbook_id: str,
    catalog_id: str | None = None,
) -> PeriodCatalog:
    """The periods exploration found, as the catalog discovery used to write."""
    options = [
        PeriodOption(
            period_id=period.period_id,
            label=period.label,
            scenario=period.scenario,
            period_type=period.period_type,
            start_period=period.start_period,
            end_period=period.end_period,
            # Left empty on purpose -- see the module docstring.
            bindings=[],
            warnings=[],
        )
        for period in exploration.periods
    ]
    assessments = [
        PeriodSheetAssessment(
            location_id=sheet.sheet_name,
            role=_DECISION_TO_ROLE.get(sheet.decision, PeriodSheetRole.SUPPORTING),
            reason=(sheet.evidence[0] if sheet.evidence else f"Routed {sheet.decision.value}."),
            evidence=list(sheet.evidence),
        )
        for sheet in exploration.sheets
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
        sheet_assessments=assessments,
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
