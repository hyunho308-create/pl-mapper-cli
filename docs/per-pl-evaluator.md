# Quick P&L output review

The evaluator prepares a review in the current Codex task. It reads a saved v5
`run_log.json`, verifies the final workbook against it, and prints compact evidence
for Codex to review. It does not launch another session, call a model/API, or
create an intermediate parsed file. It never changes the source or mapped output.

## Use in Codex

Ask Codex to run the command below and complete the review printed in its output:

```powershell
.\.venv\Scripts\python.exe -m hotel_pl_normalizer.evaluation `
  "C:\path\to\source.xlsx" `
  --run-log "C:\path\to\run\run_log.json" `
  --mapped-workbook "C:\path\to\run\source_mapped.xlsx"
```

The evidence includes its own short review instructions. Read it in the active
task and return only issues, source references, amounts, and review coverage.
Keep the terminal output complete; if a tool clips it, read the remaining evidence
before claiming completion. The final END OF REVIEW EVIDENCE marker helps detect
clipping, but the reviewer must also check the stated row/account/finding counts.

Add `--output-dir "C:\path\to\review"` to also save the same text as `EVAL.md`.
Saving is optional; there is no new `eval.json` or `eval_input.json`. Existing
historical reports are left untouched. The saved Markdown remains a preparation
report with semantic review pending; the Codex response is the actual review.

Add repeated `--expected-period PERIOD_ID` options when the original requested
period IDs are available. Otherwise the check uses recorded periods plus
`dropped_periods`. It cannot infer a request that the log never recorded.

## What it checks

- Missing/dropped requested periods and empty output periods.
- Blank child values despite nonzero source amounts, and same-label source rows
  on the same sheet/page that might supply a missing period.
- Parent values without populated children; rows used only in parents or nowhere;
  children without direct source rows; and residual allocations.
- Every active child mapping, including unflagged mappings and populated children
  with no source rows, against its siblings and source labels/amounts.
- Feedback warnings/errors, raw validator findings/checks, execution errors, and
  unresolved human decisions. Referenced source rows are included even when no
  child account uses them. Repeated issues are grouped across periods and saved
  feedback/check copies. Each group retains its highest severity, affected
  periods/accounts, source references, distinct explanations and amounts, and
  original occurrence count. Feedback and raw checks join only when account,
  period, cited scope and quantified comparison agree unambiguously; distinct
  source conflicts stay separate.
- Source/output readability, evidence integrity, and workbook-to-log agreement.

Python identifies gap candidates. Codex judges accounting meaning. An unused
subtotal, valid subtraction, or source that supplies only a parent is not proof
of missing detail. Review source presentation notes as claims, not as proof.
Zero stays distinct from missing. Do not sum all source rows: totals overlap.

Tables use short A IDs for COA accounts, P IDs for periods, R IDs for source rows,
and D IDs for original sheets/pages. Start with the complete child overview, with
direct source labels, locations, columns and context inline, and expand the referenced supporting tables for
context, adjustments, residuals and gap candidates. COA definitions include all
synonyms; repeated wording is printed once under an N ID. Identical source
labels/period values/headings are printed once while retaining every original
R ID, location, column and use. This compression does not assert that two rows
have the same accounting scope. A direct source whose values equal its child's
values appears once in that child's overview row; the source table supplies all
remaining rows. All active child mappings and all nonzero
numeric evidence rows are retained; no row cap is applied.
Amounts come from the saved log; workbook disagreements are listed separately.

## Status and timing

The local command reports `ready_for_review` (exit 0), `issues_found` (exit 2), or
`incomplete` (exit 3). Zero means evidence is ready, not that the mapping passed a
semantic review. Missing artifacts/references prevent a complete preparation.
Local warnings and candidate gaps remain for review, even if the mapper accepted
its result. Rejected runs still expose their available warnings and evidence.

Codex separately returns `READY` or `REVIEW`, plus counts of periods, children,
grouped findings and recorded occurrences actually reviewed. `READY` means no
important issue requiring the analyst's attention was found in the completed
review. `REVIEW` includes important mapping defects, material source conflicts,
missing requested periods, rejected runs and incomplete reviews. Minor rounding,
valid unsplit parents and harmless presentation notes remain background notes.
Importance is a model judgment grounded in source evidence and affected amounts;
there is no extra rule engine or fixed dollar cutoff. A skipped finding or
child means incomplete and therefore `REVIEW`. This is a quick output check, not a source ingestion audit:
data that never entered the saved evidence cannot be recovered by this evaluator.

For unattended batches and one final analyst queue, see [the batch review
workflow](comps-review-workflow.md).

The command prints elapsed local processing time. Benchmark the complete Codex
review separately; preparation speed is not a promise about model response time.
The old `--codex-model`/`--reasoning-effort` flags and judge-client API are removed.
No Codex login or API key is needed to run the Python command itself.
