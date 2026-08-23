# Department Binding Simplification Plan

## Goal

Remove department row-boundary discovery from the normal workflow. Preserve its
useful information by classifying each important sheet during period binding,
when that sheet is already open. Keep exploration and sheet routing byte-for-byte
compatible with the baseline. The mapper will infer departments within mixed
sheets as it already does for single-sheet P&Ls.

## Implementation

1. Keep exploration and sheet routing unchanged. Do not request department
   hints, change routing decisions, or add routing-time reads.
2. Extend the period-binding result for each routed sheet with:
   - `department_hints`, each containing a canonical department, the existing
     department-binding section role (`primary`, `summary`, `detail`,
     `supporting_detail`, or `kpi`), and evidence;
   - zero hints for an unknown sheet and multiple hints for an ambiguous or mixed
     sheet. Do not make `mixed` or `unknown` into invented departments.
3. Require exactly one classification for each routed sheet, backed by the same
   read that establishes its selected-period column. Never request row ranges.
4. Carry the sheet classifications from binding into the mapping prompt.
   Preserve the current inclusion rule: only `skip` sheets are withheld; selected,
   unsure, and deferred sheets remain available to the mapper. An ambiguous sheet
   keeps all supported candidates, and an unknown sheet keeps none.
5. Replace the two binding branches with one period-binding stage that binds every
   selected period to a column on every routed financial sheet.
6. Add a deterministic failure when many unambiguous department sheets contain
   rich nonzero detail but the submitted mapping collapses mostly to parents and
   `no_value` decisions.
7. Remove department-span models, adapters, tools, prompt text, and artifacts
   after the controlled comparison succeeds.
8. Keep sheet routing, user period selection, per-sheet period binding, evidence
   compaction, mapping validation/repair, audit logs, and atomic output unchanged.

## Testing

1. Build a fixed regression set containing:
   - conventional multi-tab P&Ls;
   - opaque or coded department tabs;
   - single-tab P&Ls with several departments;
   - Summary and detail on the same sheet;
   - mixed/unknown sheets and multiple outlets;
   - 2025/2024 YTD and full-year comparisons.
2. Run discovery and routing once per workbook, then give that exact saved result
   to both workflows. Use the same periods, model, and reasoning settings and
   preserve both outputs and run logs.
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
