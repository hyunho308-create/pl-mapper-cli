# Batched P&L review in Codex

Use Luna through the existing production normalizer and Astra in the active
Codex task to review its saved output. Process five files at a time; increase to
ten only when a batch is completing reliably. Python calculates and validates
numbers. Astra judges accounting meaning and which issues need the analyst.

1. Inventory and hash the inputs. Reuse a completed output only when its source
   hash and requested periods match; retain its review findings.
2. Map full-calendar-year **2025 Actual** and **2024 Actual when available**.
   The existing discovery model identifies periods; select only validated
   options. Flag unavailable or ambiguous Actual periods and continue with the
   other files. Do not substitute Forecast, Budget, partial-year YTD or TTM unless the analyst
   explicitly designates a source period as Actual; retain that instruction and
   the original source labels in the audit evidence.
3. Run the existing local eval preparation and read its complete evidence in
   Codex. Check every active child, requested period and warning/error. Review
   source references when they can resolve an important uncertainty.
4. Assign **READY** or **REVIEW**. Rejections, missing periods/artifacts and
   incomplete reviews always require review. Otherwise Astra judges whether a
   likely defect or unresolved source decision materially affects the output.
   Small rounding, valid unsplit parents and harmless notes stay in the file's
   background notes. Acceptance alone does not establish READY.
5. After each batch, reflect on recurring mistakes. Update a short mapping-note
   section only for a broadly supported clarification, recording the evidence
   and prompt version with the batch. Replace redundant wording instead of
   accumulating rules. Check affected prior examples before retaining a change.
   This is prompt refinement, not model training. Do not add special-case code,
   change arithmetic or COA structure, or retry individual edge cases repeatedly.
6. Continue through the set without asking the analyst about each exception.
   Deliver one review queue with property, period, amount, source reference,
   reason and the decision needed. Link every mapped workbook and full review
   from the complete index. Keep failed/unmapped files visible in that queue.

Use existing source, summary, run-log and workbook artifacts plus one small
index and short Markdown reviews. Do not create a second evaluation service,
an additional model API client, intermediate parsed handoff files or a rule
framework. Preserve original sources and prior run attempts.

The eval reads saved evidence and cannot certify detail that ingestion never
captured. A ready result means no important issue was found within that scope.
