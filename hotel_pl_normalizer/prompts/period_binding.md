# Period Column Binding

Find, for **each sheet** and **each chosen period**, the one column that holds
that period's figures.

## These periods were already found in this workbook

An earlier pass read this workbook and reported which periods it offers. A person
then chose from that list. **The periods below exist here** — your job is to
locate the column each one sits in, not to re-decide whether the workbook has it.

If a period looks absent from a sheet, the usual causes, in order of likelihood:

1. You have not read far enough — the header block is deeper than you looked, or
   further right than the first screen of columns.
2. That sheet genuinely does not carry it. Supporting schedules often carry
   fewer periods than the main statement.

Only after checking the first should you conclude the second. And when you do,
mark that **sheet** unavailable for that period. Do not drop the period.

## Answer for every deterministic layout

Call `list_sheet_layouts` first. It inspects the header markers on every routed
sheet and groups sheets only when the same period, scenario, block and metric
markers occupy the same Excel columns. Sheets without enough evidence remain
singleton layouts.

Return exactly one binding or unavailable outcome for every layout and selected
period. Python expands the layout choice to every member sheet and then applies
the existing exact per-sheet verifier. If the chosen column is blank or all zero
on one member, Python records that member as unavailable instead of forcing a
false binding.

Use `sheet_bindings` or `sheet_unavailable` only for a verified member exception.
Do not enumerate ordinary member sheets. The compact submission permits one
initial answer and at most two repairs.

### Do it in one or two calls, not twenty

`list_sheet_layouts` returns representative header cells and candidate column
profiles for every layout in one call. Usually that is enough. When it is not:

- Use `read_rows` on the representative sheet when its header sits deeper than
  the returned semantic cells.
- Use `column_stats` where you need to distinguish amounts from ratios.
- Read a named member sheet only before creating a sheet-specific override.

Do not re-list every member sheet in the submission. Decide by layout and submit
the complete compact answer once.

## Bind per layout, verify per sheet

One binding per sheet per period, and nothing finer. A column convention belongs
to the sheet: every department on a tab reads the same columns as every other
department on it. You never need to say which department a column is for.

**Equivalent periods use different columns in different layouts.** A one-column
shift between the Summary tab and department tabs creates separate deterministic
layouts. Never transfer a column choice between layout IDs merely because the
columns are near each other.

The chosen-period list includes canonical scenario and inclusive month coverage from
discovery. Match the economic period, not the exact wording of its header. A
December YTD column ending at a December fiscal year-end, a Jan–Dec `Total`, an
FY column and a TTM ending in that same December are equivalent when they cover
the same twelve months and carry the same scenario. Bind each sheet's equivalent
column even when one calls it `YTD` and another calls it `Total`.

The equivalence is strict in the facts that matter: scenario and date coverage.
Never substitute Budget or Forecast for Actual, never substitute a single month
for an annual period, and never treat a partial YTD as a full year. When exact
coverage cannot be established from the header block, mark that sheet
unavailable instead of guessing.

## What is not a period column

- **Variance columns.** Anything comparing two things: `Variance`, `Var`,
  `Fav/(Unfav)`, `B/(W)`, `vs. Budget`, `vs. Prior`, `Better/Worse`, or a column
  of differences beside two amount columns.
- **Explicit ratio subcolumns.** Headers marked `Ratio`, `POR`, `RPOR`, `CPOR`,
  `PAR`, `% of Revenue`, `% of Income`, `% of Sales`, or `Rev / Cost`.
- **Any explicitly marked percentage column.** A stale `%` or `% or POR` export
  control above a later explicit period header does not control; the later
  period header wins.

Occupancy, ADR and RevPAR often appear as rows inside an otherwise valid
Actual/Budget amount column. Their presence as row labels does not disqualify
that period column.
- **Query metadata.** Tokens such as `%,C`, `[Date].`, `[Version].`,
  `Prior Year GL Period`, `<<001/` are Hyperion or SmartView export plumbing, not
  headers. A column carrying these and only a handful of numbers, on a sheet where
  the real columns carry dozens, is a configuration column.

Two header-reading rules worth having explicitly:

- **A later explicit row beats an earlier metric label.** Where a stale `%` or
  `% or POR` label sits *above* a row that names a period outright, the row that
  names the period wins.
- **A bare `Total` immediately after twelve Jan–Dec columns is that year's
  full-year total**, for the same scenario, even when the Total cell repeats
  neither the year nor the word Actual. Do not relabel it as a nearby Forecast or
  Budget.

## A period block and a period column are different things

The commonest way to get this wrong is to bind the right *kind* of column in the
wrong *block*. A schedule frequently carries two full sets of columns side by
side — one for the current month, one for the year to date — each with its own
Actual, Budget, Prior Year and variance columns underneath.

The block headings sit on their own row, above the column headings, and are often
spaced or merged across the columns they cover:

```
row 8    B = P E R I O D                    S = Y E A R - T O - D A T E
row 9    B=ACTUAL  D=BUDGET  G=LAST YEAR    S=ACTUAL  U=BUDGET  Y=LAST YEAR
```

Reading row 9 alone, `B=ACTUAL` looks like the answer for any Actual period. It
is the *month's* Actual. The block heading on row 8 is what distinguishes them,
so **find the block first, then the column within it.**

The marker is not always on its own row. Some layouts put it inline among the
scenario labels, and some put the current-month block to the *left* of the label
column with the year-to-date block to its right:

```
row 7    A=Actual  C=Budget  E=Last Year   [I=labels]   J=YTD  L=Budget  N=Last Year
```

Here `A` is again the month, and the bare `YTD` cell at `J` is the only thing
that says where the year-to-date block starts. A column headed plainly `Actual`
is the answer to "which Actual" only once you know which block it sits in — so
scan the whole header row, both sides of the labels, before choosing.

Two ways to confirm you are in the right block:

- **Compare magnitudes.** A year-to-date figure is a sum of months, so on any
  schedule past January it is visibly larger than the same account's current
  month. `column_stats` on a substantive row range shows you both. If your "YTD"
  column and the month column hold similar figures, you are probably in the month
  block.
- **Count the blocks.** If the sheet has two Actual columns, two Budget columns
  and two Prior Year columns, it has two blocks, and you owe an answer about
  which is which rather than taking the first.

## Check the column carries money

`column_stats` tells you what a column actually holds over a row range you name.
Use it on a substantive row range before binding — the header can say `Actual`
over a column of percentages.

Read its answer as evidence, not as a verdict:

- It reports how many values it saw. **A span with three numeric rows cannot tell
  you what scale a column is** — Management Fees is often exactly that, and the
  tool will say so. When the sample is thin, judge from the header instead.
- Percent-formatted counts, magnitudes and sample values are facts about the
  span. What they mean is your call.
- A column that is entirely zero across a substantive row range is worth a second
  look. Some budget columns genuinely are empty; if it is empty everywhere, say
  so and mark it unavailable rather than binding to something else.

## Answer for every sheet

For each financial sheet, either a binding per chosen period, or an entry in
`unavailable` naming the period, the sheet, and what you saw. Silence about a
sheet is the one answer that helps nobody.

An unavailable sheet is not a failure. The run continues, your reason is
recorded, and that sheet contributes no value for that period. **One sheet
lacking one period is never a reason to abandon the others.**

Then call `submit_layout_bindings`.
