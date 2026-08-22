# Department Binding Simplification Plan

## Goal

Remove department row-boundary discovery from the normal workflow. Preserve its
useful information by classifying each important sheet during exploration, then
bind the user-selected periods directly on those sheets. The mapper will infer
departments within mixed sheets as it already does for single-sheet P&Ls.

## Implementation

1. Extend the exploration result for each sheet with:
   - the existing `role_hint`, describing the broad function of the sheet;
   - `department_hints`, each containing a canonical department, the existing
     department-binding section role (`primary`, `summary`, `detail`,
     `supporting_detail`, or `kpi`), and evidence;
   - zero hints for an unknown sheet and multiple hints for an ambiguous or mixed
     sheet. Do not make `mixed` or `unknown` into invented departments.
2. Update `sheet_name_triage.md` and its tool schema so the department candidates
   already requested by the prompt are retained in the structured result. Use the
   exact canonical department vocabulary from `models/binding.py`, and cross-check
   the new result against every non-row field currently returned by department
   binding before removing that stage.
3. Carry the sheet classifications from exploration into the mapping prompt.
   Preserve the current inclusion rule: only `skip` sheets are withheld; selected,
   unsure, and deferred sheets remain available to the mapper. An ambiguous sheet
   keeps all supported candidates, and an unknown sheet keeps none.
4. Replace the two binding branches with one period-binding stage that binds every
   selected period to a column on every routed financial sheet.
5. Remove department-span models, adapters, tools, prompt text, artifacts, and the
   `routed_department_not_present` validation dependency after no callers remain.
6. Keep sheet routing, user period selection, per-sheet period binding, evidence
   compaction, mapping validation/repair, audit logs, and atomic output unchanged.

## Testing

1. Build a fixed regression set containing:
   - conventional multi-tab P&Ls;
   - opaque or coded department tabs;
   - single-tab P&Ls with several departments;
   - Summary and detail on the same sheet;
   - mixed/unknown sheets and multiple outlets;
   - 2025/2024 YTD and full-year comparisons.
2. Run the current and simplified workflows on the same workbooks, periods, model,
   and reasoning settings. Preserve both outputs and run logs.
3. Compare:
   - selected sheets and bound columns;
   - old department labels/section roles versus new sheet department hints;
   - inclusion of selected, unsure, deferred, and skipped sheets;
   - mapped COA values and populated-account counts;
   - validation errors, warnings, and human-review items;
   - source-row citations for material accounts;
   - token usage, model calls, elapsed time, and estimated cost.
4. Manually review every material value difference and every new validation error.
   Pay special attention to coded tabs and mixed sheets.
5. Run package checks: import every module, CLI `--help`/`--doctor`, period-selection
   modes, single-ingestion handoff, 269-account asset check, wheel build/install,
   and `git diff --check`.

## Acceptance Criteria

- No selected period is bound to a different column without documented evidence.
- No unexplained material COA-value regression is introduced.
- Coded tabs retain useful department hints; mixed sheets remain available to the
  mapper without forced classification.
- Validation and audit outputs remain complete.
- Model calls, tokens, elapsed time, or cost improve without reducing acceptance
  quality across the regression set.

Implement this on a separate branch and keep the current workflow available until
the regression comparison is reviewed.
