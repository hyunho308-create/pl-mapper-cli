# Model notes and source comparisons

The COA Model Feedback column distinguishes mapping treatment, source
presentation, missing mapped detail, and validation findings. It is a review
aid, not an approval of accounting meaning.

## Note composition

- Combine related findings using explicit review IDs, source references and
  accounting dependencies. A shared amount alone is not enough to merge issues.
- Place a review on its responsible populated account or parent, rather than a
  blank child. Keep distinct issues even when they affect the same account.
- Code supplies measured comparisons, periods and KPI units. Missing mapped
  detail does not prove that the original source lacks detail.
- Keep `mapping_treatment` separate from discrepancy prose so a useful mapping
  explanation survives suppression of a rounding-only difference.
- Preserve raw findings and their rendered, superseded or internal-only
  dispositions in the run log. Suppressing a visible note does not erase audit
  evidence. Note composition requires no additional model call.

## Independent source-subtotal checks

The existing mapping call can return `source_controls` alongside its account
decisions. Each control names a reported subtotal, non-overlapping component
rows and a responsible COA account. Current controls are limited to additive
subtotals with components above the total on the same sheet. Subtractions,
profit/offset totals, opposite-sign comparisons, cross-sheet comparisons and
missing values remain audit-only, not visible source-error flags. Legacy
equations and their values remain in the audit; code does not normalize signs.
Code calculates each comparison independently for every selected period, even
if the reported subtotal was not used in the final financial mapping.

The checks never change mapped values. Unknown references are validated;
missing or invalid amounts remain unverified rather than becoming zero. Small
differences use the common reconciliation tolerance and remain audit-only.

## Important limitation

The arithmetic is deterministic; selection of the source relationship is still
a model judgment. Live verification found both previously missed differences
and incorrectly scoped proposals, including cumulative totals interpreted as
single blocks and intermediate payroll totals with ambiguous captions.

Treat proposed comparisons as review findings, not automatic rejection or
correction instructions. A missing warning does not establish complete source
coverage. Reviewed saved-run demonstrations are not proof of reliable automatic
discovery on a new P&L, and this feature is not a replacement for human sign-off.

Implementation: `mapping/source_controls.py` owns arithmetic; the mapping plan,
validator and run log carry the controls; `feedback.py` composes visible notes.
See [the quick output review](per-pl-evaluator.md) for a separate saved-run review.

## Workbook feedback presentation

Feedback is composed from the final saved checks, exceptions and model reviews;
it does not require a separate model call or change mapped amounts. Routine
informational room-KPI explanations stay in the audit, while actual KPI warnings
remain visible. Identical comments appear once per account, without repeated
period labels. Numeric comparisons use the year when it uniquely identifies the
period; differing treatments retain enough period context to be distinguished.

Summary-to-department comparisons appear beside department totals, using Summary
as the reference. This does not suppress Summary arithmetic errors. Every affected
highlighted COA account receives the concise issue, including partial children.
Run Notes uses the mapper's `run_summary` (up to 500 characters), saved with each
mapping version. Every repair refreshes that summary and the complete review
list; restoring an earlier mapping restores its comments too. Old logs without
a summary leave Notes blank unless there is a separate run-level issue.
Its deterministic status describes completion, review needs, unresolved
errors, a required scope decision, or a stopped run. Raw findings remain in the
run log even when merged or hidden from the workbook.

Narratives describe final decisions rather than repair history, using named
departments and plain explanations instead of internal mapping terminology.
Source-subtotal notes show the named subtotal and differences by period; exact
reported totals, component totals, and row references remain in the audit.
If the initial mapping cannot be parsed or fails input validation, no plan is
saved: the model must correct and resubmit the full `validate_mapping` call.
Only saved plans can be repaired with `patch_mapping`.

Issue highlighting is confined to COA, not KHP Model Accounts. Summary/detail
comments name the department and use Summary as the reference; duplicate proven
comparisons are merged. Routine descriptions of where totals are sourced do
not belong in model notes. A coverage review not reached because another error
stopped the run is retained as an informational audit event, not another flag.

Mapping preserves minibar's operator department: F&B minibar uses a generic
venue; only OOD minibar uses S3. Unsegmented room revenue stays at its supported
parent, and wages with unknown management status stay at Salaries and Wages.
Use partial coverage and a parent rationale for an unreliable split. That
explanation ends enrichment for this incomplete-detail warning, but preserves
the parent and supported children in the saved mapping. It never waives
Summary arithmetic, Summary/detail reconciliation or large residual errors.

### Final Excel detail cleanup

The shared writer applies an output-only mask per period to incomplete child
groups without a residual sibling. It preserves reconciled groups and rounding
differences within the existing tolerance. Department COGS, Salaries and Wages,
Labor, and Opex subtotals anchor independent branches: a higher discrepancy does
not erase those totals or their usable detail. Their own incomplete children
are still eligible for cleanup.

Omitted cells are blank, without issue highlighting or mapped-label text for
the displayed source period. Comments remain only when relevant to retained
periods. The parent reads: "Detailed breakdown omitted because it does not add
up to the total." Discrepancies between retained totals remain visible. Run Notes
wraps with a saved height based on text length and column width.

This changes neither mapping acceptance nor original values, decisions and
findings in the run log. KHP Model Accounts formulas and formatting are unchanged;
Excel recalculates their results from the cleaned COA on opening. CLI and web
exports both use this same writer; no additional model call is involved.

### Instruction audit

Aligned the main mapping prompt (priorities, labor, rooms segmentation, named
activities, coverage, repair, review notes and venues), live repair instructions,
validation definitions, rule registry, and COA notes. Removed the old forced
minibar-to-OOD, unsegmented-to-Transient and unsplit-wages-to-Nonmanagement defaults.
The upstream exploration prompt only discovers schedules and contains no
conflicting classification rule; it is unchanged. Historical analysis, archived
plans, saved run logs and prior workbooks are deliberately not rewritten.

No account IDs, hierarchy equations, baseline amounts, model settings, Summary
checks or source-row execution rules changed. The optional source-control
guardrails do not prove semantic correctness of every remaining comparison;
membership still requires model judgment. Prompt behavior needs a new live test;
saved-run replays verify presentation and arithmetic only.
