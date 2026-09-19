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
rows, explicit subtraction rows if necessary, and a responsible COA account.
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
a summary use a simple pointer to highlighted accounts, not reconstructed prose.
Its deterministic status describes completion, review needs, unresolved
errors, a required scope decision, or a stopped run. Raw findings remain in the
run log even when merged or hidden from the workbook.
