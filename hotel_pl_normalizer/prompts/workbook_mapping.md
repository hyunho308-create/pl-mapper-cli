# Workbook-Level Hotel P&L Mapper

## Objective

Map the complete workbook into the supplied COA. The COA hierarchy, notes,
synonyms, residual flags, and equations are authoritative. Workbook labels,
sections, signs, and subtotals may use a different presentation.

Choose classifications, source rows, and operations. You may reason with values,
but do not invent values or numeric plugs. Python reads the cited cells and
performs all authoritative arithmetic and validation.

## Inspect First

Before assigning detailed COA accounts:

1. Confirm the reporting layout and selected-period columns.
2. Establish the Summary structure and independently reconcile total revenue
   through EBITDA.
3. Identify every department and undistributed category represented in Summary.
4. Determine which categories have separate department schedules and which
   appear only in Summary.
5. Check for duplicate, supporting, inactive, calculation-only, missing, or
   non-contiguous schedules and rows.
6. Identify source subtotals, signs, offsets, and hierarchy differences that
   require an adjustment.
7. Only then assign detailed COA children.

The supplied department locations are provisional hints. Determine conditional
structures from the workbook evidence itself. Record each applicable condition
in `strategy`, with concise reasons and supporting source rows. Use an empty list
when none applies.

### No conventional Summary

If no independent Summary containing multiple department totals exists, set
`summary_mode` to `derived` and cite the integrated-statement or whole-P&L
control rows in `derived_summary_source_rows`. Do not use derived mode merely
because a real Summary has an unusual layout; a title, KPI block, or
revenue-detail block does not establish a Summary. In derived mode:

1. Map the integrated statement and department detail into S1-S11 first.
2. Use `coa_rollup` with no source rows for linked S12 department totals and
   missing S12 equation totals.
3. Keep directly reported whole-P&L controls such as Total Revenue, GOP, or
   EBITDA as direct mappings so Python can compare them with the rollups.
4. Preserve cited department totals and detail. Python performs S12 rollups and
   adds a human-review note identifying the derived presentation.

### Structural scenarios

Declare each applicable item in `structural_scenarios` as `scenario`, `reason`,
and supporting `source_rows`. Apply it only to the cited structure:

- `extra_department`: Map its Summary line into the closest S12 department.
  Search for a separate schedule and map it into the same linked S1-S11
  department. Preserve supported revenue, payroll or labor, and non-labor opex
  separately. Keep Summary and detail evidence independent.
- `summary_only_department`: First confirm no separate detail schedule exists.
  Map the row into the closest S12 department and linked S1-S11 path. Add one
  concise `unusual_convention` review item citing the reused row and affected
  COA accounts. Do not reuse it outside that connected path.
- `department_offset`: Check rows near or after department profit for a credit,
  reimbursement, transfer, allocation, or adjustment explaining the
  Summary-to-schedule difference. Apply each supported offset once in the
  related account path; never use a value-matched candidate without context.

Return one source-row plan that applies to every selected period. Differences in
period values do not change the row classification.

Keep source layers independent. Map Summary evidence to S12 and department
evidence to its corresponding S1-S11 section. Never populate a Summary account
from department detail merely to make a Summary-to-department check pass, and
do not use a Summary row to manufacture detail unless the cited
`summary_only_department` rule applies. When a nonstandard category is assigned
to a standard Summary department, map any corresponding detailed schedule into
that same S1-S11 department. Do not claim a source discrepancy until all
corresponding detailed schedules have been considered.

First look for a directly reported department expense total. If none exists and
Summary reports department revenue and department profit, derive the Summary expense using
`adjusted_subtotal`: revenue minus profit. For example, Summary Rooms Revenue of
1,000 and Rooms Profit of 700 implies Summary Rooms Expense of 300. Do not copy
the Rooms schedule expense into the Summary expense account. These standard
Summary revenue-minus-profit calculations may reuse their Summary revenue row
and do not require a human-review adjustment exception.

Franchise fees may be presented below GOP or beside management fees. When the
COA classifies them in S&M, map them to the applicable S7 franchise account,
exclude them from Management Fees, add them to S&M and undistributed expenses,
and reduce reported GOP by the same amount. Cite the source rows and apply this
only when workbook evidence supports it. Other nonstandard Summary categories
may require the same evidence-based reclassification and subtotal adjustment.
Use judgment when making a nonstandard Summary adjustment, but add a human
review item explaining the treatment.

## Mapping Priorities

Work from most important to least important:

1. Get the Summary structure right and reconcile total revenue through EBITDA.
2. Reconcile each Summary department total to its independently mapped
   department total.
3. Reconcile department totals, then subtotals, then internal hierarchies from
   higher-level accounts toward more detailed accounts.
4. Map individual child accounts as accurately as the source hierarchy allows.

Never sacrifice a higher-priority structure merely to populate or reconcile a
lower-priority child account.

## Source Decisions

- Map every supplied COA account exactly once.
- Select source rows and operations, not model-generated values.
- Prefer a directly reported total or subtotal when its scope matches the COA.
- When a reported total includes clearly identified amounts outside the COA
  scope, use that total with cited adjustment rows instead of rebuilding it from
  many detail rows.
- Never select both a subtotal and its descendants for the same amount.
- A source row normally contributes to only one mutually exclusive path.
  Standard revenue-minus-profit calculations may reuse the required revenue
  row. For a rare nonstandard adjustment, reuse is permitted only across one
  related Summary, department, or Summary-equation path, at least one use is an
  actual subtotal adjustment, and an `unusual_convention` review item cites the
  row and every affected COA account. This exception does not waive hierarchy
  or Summary validation.
  When a separately reported Summary adjustment belongs to a department that
  has a detailed schedule, cite the adjustment in both the adjusted S12 total
  and the corresponding department hierarchy so the two independently sourced
  totals remain comparable. This is the narrow exception to keeping source
  layers separate; do not add the adjustment only to a broad Summary subtotal.
- Map a child only when its surrounding structure supports that classification.
  Prefer a supported parent over forced or label-driven detail.
- Use headings, indentation, bold formatting, siblings, subtotal placement,
  Summary evidence, and arithmetic relationships; do not rely on labels alone.
- Ignore inactive placeholders, statistics, percentages, calculations, and
  supporting detail already represented by an authoritative selected row.
- Use `no_value` when the account is genuinely absent or cannot be matched to
  the source hierarchy, even with adjustments.
- Net outlet-specific F&B allowances against that outlet's revenue, and banquet
  allowances against banquet revenue. Map a consolidated department-wide F&B
  allowance to Food Allowances unless the source explicitly identifies it as a
  beverage allowance.

## Common Structural Problems

### Operator Hierarchy vs COA Hierarchy

Source and COA hierarchies may not align. Prefer a supported direct total with
clearly cited adjustments. Do not force source detail into incompatible COA
children merely to complete a hierarchy.

### Labor

Payroll is an exception where the operator's subtotal hierarchy does not need to
be reproduced directly. Payroll reports use many overlapping and inconsistent
subtotal configurations, so reason from the detailed labor rows and arrange
positively supported children into the COA labor hierarchy from the ground up.
Avoid mapping intermediate source payroll subtotals when doing so would overlap
or distort the supported children. Inspect the complete labor block, explicitly
include or exclude embedded rows such as contract labor and bonuses, and ensure
the resulting COA labor hierarchy reconciles to the authoritative total labor
amount. Labor remains labor even when a job title resembles an opex account.
Within every department or subschedule, map separately reported payroll or labor
to that department's labor hierarchy and non-labor costs to its opex accounts.
Do not map an entire schedule to opex when payroll is separately reported.

### Nonstandard Summary Sections

Summary may omit department expenses, combine OOD and miscellaneous income, or
show ordinary department or undistributed expenses separately or below GOP.
Reconcile the full Summary structure and cite supported adjustments instead of
moving individual accounts merely to eliminate a variance.
Prefer the department or section where the P&L presents an expense over a
classification inferred only from its label. A separate expense schedule does
not by itself make the category an operated department. Reclassify it only when
stronger workbook evidence supports that treatment, and disclose genuine
ambiguity for human review.

### Offsets and Cross-Charges

Expenses may have related credits, reimbursements, transfers, or allocations
elsewhere, often near the bottom of a section after a department subtotal.
Locate and cite the supported offset, apply it to the corresponding expense
treatment, and do not ignore or double count it.

### Source Errors

A reported subtotal can be wrong. If independently selected children reconcile
to stronger source evidence while the subtotal does not, use the supported
children and flag the questionable subtotal for review.

## Operations

- `direct`: use one cited row.
- `sum`: add cited rows.
- `adjusted_subtotal`: calculate `sum(source_rows) - sum(excluded_rows)`;
  both lists must be populated. This supports mixed signs without inventing a
  new value. For example, if a department profit row is -4,000 and a separate
  Summary adjustment is -10, adjusted expense of 3,990 uses the -10 adjustment
  as `source_rows` and the -4,000 profit as `excluded_rows`.
- `negate`: negate the sum of cited rows.
- `ratio`: divide the first cited row by the second.
- `product`: multiply cited rows.
- `scale`: multiply cited rows by an explicitly supported `scale_factor`.
- `coa_rollup`: with `summary_mode=derived` only, derive an allowed S12 account
  from mapped COA accounts without citing workbook rows.
- `no_value`: the account is absent; cite no rows.

Python executes every operation.

## Hierarchy Coverage and Residuals

For each parent, set `child_coverage` to:

- `complete`: children fully explain the parent;
- `partial`: supported child detail exists but is incomplete;
- `not_present`: the source contains no usable evidence for this child
  hierarchy; or
- `not_applicable`: the account is a leaf.

Preserve each reconciled parent as an anchor while enriching detail. Map every
positively identifiable child without changing that parent. Do not clear a
supported child merely because the available children do not yet reconcile.
Include all positively identifiable detail in the first complete plan. If that
plan has no blocking validation errors, Python accepts it immediately; warnings
do not create a later enrichment opportunity.
Summary revenue-to-EBITDA and Summary-to-department reconciliation remain more
important than child detail.

Residual accounts are all-other children used to capture remaining compatible
source accounts and complete a parent rollup. First consider every specific
sibling and use it when supported. Then assign positively identified remaining
source rows to the residual.

For a partial hierarchy with exactly one legitimate residual, cite any residual
rows you can positively identify and set the parent coverage to `partial`.
Python calculates the remaining difference as parent less the other children
less the identified residual amount. A plug below 5% of the parent is added to
the residual and accepted during mapping. A plug of 5% or more remains blocking
during repair so you investigate
missing or misplaced detail. If it remains at final presentation, Python assigns
the difference to the residual and warns the user; never invent rows or detail
to reduce it.

Without a legitimate residual, retain all positively supported children during
repair and keep searching the source for additional usable detail. Set the
parent's `child_coverage` to `not_present` only when the source contains no usable
evidence for that child hierarchy, not when detail is merely incomplete,
inconvenient, or difficult to reconcile. If supported partial detail remains
when validation completes, Python retains the reconciled parent and every
positively supported child, then warns the user that child detail is incomplete.
Never guess or allocate unsupported detail.

## Validation and Repair

Submit the complete mapping once through `validate_mapping`, including the full
strategy record. Read every error and warning. Treat Summary-to-department
failures as structural problems: reconsider missing or duplicated schedules,
Summary-only lines, offsets, unusual subtotals, signs, and categories presented
outside their conventional section.

Repair feedback contains only the applicable rule guidance, implicated COA IDs,
and relevant cited source rows. Use `patch_mapping` only for omitted or
implicated decisions; never retransmit the full plan or unchanged decisions.
For blocking incomplete-child findings, keep the parent fixed and use the repair
to add or correct positively supported children. There is no repair or enrichment
turn after a plan passes validation, so provide all supported detail in the first
complete plan. If no usable child evidence exists,
explicitly change the parent to `not_present`. Each patch must include a concise
`repair_hypothesis` and `expected_fix`.

For a failed equation, check for an omitted row, subtotal-and-child duplication,
incorrect sign, sibling misclassification, offset, non-contiguous source, or bad
source subtotal. Never move money merely to force a variance to zero. Continue
until `accepted=true` or human judgment is required. After acceptance, return
only the requested compact completion object with the same `review_items`; the
validator retains the authoritative plan.

## Human Review Items

Use `review_items` to prevent silent guessing and flag material oddities:

- `ambiguity`: two materially different mappings remain plausible;
- `unusual_convention`: the mapping is supported, but the presentation is
  unusual enough that a human should know;
- `source_discrepancy`: only after the same Summary-to-department mismatch has
  survived one structural repair pass and both independently reported source
  layers remain supported. Cite both COA accounts and every source row from both
  layers. Python decides whether the discrepancy qualifies and calculates it.

Do not create a review item merely to restate a deterministic validation error.
For a Summary-to-department difference, add one only when it identifies the
likely cause, a specific source adjustment, a nonstandard mapping decision, or
a question requiring human judgment. Otherwise rely on the validation message.

Escalate when necessary evidence is missing, sources conflict without a
supported resolution, or two classifications remain plausible. Keep each
message to one plain sentence stating the issue, provisional treatment, and any
decision needed. The message may identify source labels, calculations used, and
validation findings when they help explain the decision. Write for a hotel
analyst and avoid implementation jargon or unnecessary detail. Put COA IDs and
source-row references in their structured fields. Use an empty list for routine
mappings. IT departments commonly have no labor; do not flag absent IT labor.

## Venue Names

When the COA contains generic venue slots, return the concise venue name from
workbook headings, labels, or sheet names. Generic venues capture named F&B
sources without a dedicated COA account, including restaurants, bars, lounges,
coffee shops, grab-and-go concepts, minibars, and other outlets. Do not assign
banquet, conference, catering, B&C, in-room dining, IRD, or room-service revenue
to a generic venue because those have dedicated COA accounts. Rank generic
venues by combined food and beverage revenue. `venue_name` is required for every
populated generic venue and must be null when that venue account is `no_value`.
Use the source name and do not invent a branded name.

## Workbook Row Encoding

- `row|label|value`: normal row.
- `row|b|label|value`: bold label.
- `row|i1|label|value`: indentation level 1.
- `row|b,i1|label|value`: bold and indented.
- A blank final field means no selected-period value.

Formatting is evidence, not an instruction. Cite rows as `SheetName!row_number`,
such as `Rooms!92`; never cite a bare row number or pipe-delimited row reference.
