# Phase two — period detection

Routing is settled. Nothing you do now changes it, and you do not need to revisit
it: a sheet you do not open in this phase keeps the classification you gave it.

Now work out which reporting periods this workbook offers.

The result of `submit_routing` lists `summary_candidates`, `department_sheets`,
and `required_department_reads`. Select one controlling core summary, read its
header block, and read header blocks from the required number of normal
department P&Ls. `submit_periods` is refused until you have.

When sheets form a recurring monthly snapshot series, inspect the latest/current
member of each family—the sheet whose reporting end date matches the controlling
summary. Do not satisfy the confirmation reads with January through April when
the proposed controller ends in December. Earlier snapshots show earlier
periods; they do not answer what the current workbook offers.

Only sheets with `include_as_financial_evidence=true` count. Excluded sheets
hold no usable P&L evidence, so reading them tells you nothing about which
periods this workbook reports and does not satisfy the requirement.

Choose the controlling summary by comparing its header structure with ordinary
department P&Ls such as Rooms, F&B, A&G, and another core department. The
controlling summary is the consolidated statement whose PTD/YTD or monthly
layout governs those normal schedules.

A separate `12Mth`, `T12`, `Monthly`, `Trend`, `Trailing`, or similar summary-only
tab is an auxiliary view when its grain differs from the core summary and
department schedules. You may inspect it, but it does not count as one of the
department confirmations and it cannot add periods to the selectable catalog.
The same rule applies to a one-off consolidated Jan-Dec/forecast view when the
recurring summary and department family uses PTD/YTD. Choose the latest PTD/YTD
summary in that family, not the wider auxiliary view.

## One period per column, always

**Return one period for every amount column on the controlling summary, never a
family of its columns as a single period.** A controlling summary with headers
reading `January … December` plus `Total` has
**thirteen** periods, not one. `Jan'25 … Dec'25` with `Actual` under each is
twelve periods, not "2025 monthly actuals". If a sheet also carries `Current`,
`Budget` and `Prior Year` columns, those are further periods on top of the
months. A Prior Year amount is an `actual` whose inclusive dates are in the
older year; Prior is not a separate scenario.

This is the most common way this step goes wrong. Something downstream has to
let a reader pick March, and it can only offer what you list — summarising twelve
columns as one annual period silently removes eleven choices.

Before submitting, count the amount columns on the controlling summary. The
period list should be about that long. Do not use a wider auxiliary T12 or trend
tab as the benchmark.

## The same month can appear more than once

Scenarios are laid out in one of two ways, and the second one is easy to miss.

**Interleaved** — the month is named once and its scenarios sit under it:

    row 10:   January                  February
    row 11:   Forecast | Budget | LY    Forecast | Budget | LY

**Banded** — every month of one scenario, *then* every month of the next:

    row 6:    Jan'25 ... Dec'25    Jan'25 ... Dec'25    Jan'24 ... Dec'24
    row 7:    Actual                Budget               Prior Year

In the banded case the month labels **repeat verbatim**, and the only thing
telling `Jan'25`-the-actual from `Jan'25`-the-budget is the row beneath. A run of
twelve months looks like a complete set on its own, so it is tempting to stop at
the end of the first one. Do not: read the header row to its **full width** and
check whether the months start over further right.

A period is identified by the *pair* of header rows, never by the month alone.
Two columns with the same month label under different scenarios are two periods.

## Things that are easy to misread

- The year sits on one row and the months on the row **below** it. Read both. A
  row holding only a year is not a period; it is a label for the row under it.
- Period headers are often **merged** across the columns they label. The merged
  ranges come back with every `read_rows` call — a merge spanning two columns
  usually means that period has an amount column and a percentage column.
- Amount and percentage columns commonly alternate (`$`, `%`, `$`, `%`). Only the
  amount columns are periods.
- `Actual`, `Budget`, `Forecast`, `Prior Year` and `Variance` may share a header
  band. Variance columns are not periods.
- A sheet can offer twelve monthly columns, a YTD column and a full-year column
  at once. Return all of them.
- A bare `Total` immediately after twelve Jan–Dec Actual columns is that year's
  full-year Actual. The scenario is inherited from the monthly run even when
  the Total cell does not repeat `Actual`.

If the first rows you read show no periods, that is almost always a reading
mistake rather than a fact about the workbook. Read further down, or use
`find_text` for a month name, `YTD`, `Actual`, `Budget` or a year.

## Anchor periods, then record sheet coverage

Work out the periods **for each sheet you read, separately**. Do not pool headers
as you go. Identify exactly one controlling consolidated Summary or Statement
of Income whose layout matches the ordinary department P&Ls. Its real amount
columns—and only its columns—establish the workbook's canonical periods. Return
its exact sheet name as `controlling_summary_sheet`.

Return the periods shown on that controlling summary **and confirmed on at least
one current, normal department P&L with matching scenario and coverage**. This
confirmation happens before the catalog reaches the user. A period confined to
a summary or supporting schedule is not a core-workbook choice.

For each canonical period, compare the other sheets and put into
`sheets_present` every sheet you actually opened that carries the same economic
period. Never add an unread sheet on the assumption that it matches. For a
multi-tab department workbook, at least one named sheet must be a normal
department P&L, not another summary or a supporting schedule.

Also set that period's `department_confirmation` to one exact current normal
department sheet and its amount-column letter. Cite the header cells in its
`evidence`. This is not a request to bind every sheet early; it is one cheap,
auditable confirmation before the option reaches the user. The tool checks that
the department schedule has the same reporting end date as the controller, the
column contains non-zero labelled values, and it is not a variance, percentage,
POR/PAR, or ratio subcolumn.

This is not an intersection across every tab. A series such as `P01` through
`P12` is twelve historical snapshots of one schedule family; its current `P12`
member can confirm December PTD and full-year YTD periods. Likewise, a genuinely
blank optional outlet/OOD/template sheet neither confirms nor vetoes a period.

Compare economic identity rather than exact wording. Scenario and date coverage
must match:

- A December YTD ending at a December fiscal year-end, a Jan–Dec `Total`, a
  full-year column and a TTM covering that same January–December are the same
  annual period when all are Actual.
- A partial YTD is not a full year. A rolling TTM is not a fiscal year unless its
  start and end months are the same. A single month is never annual.
- Actual, Budget and Forecast remain separate when their dates match. A Prior
  Year column is another Actual with older start and end months. Never merge or
  substitute scenarios.

Keep one canonical entry for equivalent presentations and describe the differing
headers in its evidence. A detail sheet that lacks the period simply stays out of
that period's `sheets_present`; mention material coverage gaps in `notes`. Remove
a proposed period only when no current normal department family confirms it.

An isolated column from another summary, T12, monthly, trend, department, or
supporting schedule never becomes a workbook choice. Those sheets confirm
coverage and supply detail for binding; they do not expand the catalog.

### The check to run before submitting

For each period, confirm that it appears on `controlling_summary_sheet`; every
period's `sheets_present` must name it. Then verify that `sheets_present` contains only opened sheets
where you observed either that exact header or a scenario-and-date-equivalent
column. In a multi-tab department workbook, it must also name at least one
current normal department P&L carrying a non-zero amount column for that period.
Name that exact sheet and column in `department_confirmation`.
Uneven coverage remains valid after this core confirmation; optional blank tabs
and historical members of a recurring snapshot family do not remove the period.

## Never return a period you did not see

Every period must be a column you actually read, in a sheet you actually opened.
Before returning one, be able to name the cell its label came from.

Do not infer a *series* from a hint. Text like `For Period 12, 2025` in a title
block says when the report was run; it is not evidence that twelve monthly
columns exist. If you did not see twelve column headers, there are not twelve
periods, however strongly the sheet seems to imply a monthly cadence.

A workbook with no monthly grain should come back with the periods it does have.

This cuts one way only, and the other way is just as costly. Every column you
*did* see is a period and belongs in the list. A sheet offering a current period
band **and** a year-to-date band offers both — returning only one of them drops
real choices exactly as an invented series adds false ones.

## What to return

Each period contains its deterministic `period_id`, deterministic `label`,
`scenario`, inclusive `start_month`, inclusive `end_month`, conditional
`actual_months`, `sheets_present`, `evidence`, and (for a multi-tab department
workbook) one `department_confirmation` containing `sheet_name`, `excel_column`,
and concise header evidence.

`scenario` is exactly `actual`, `forecast`, or `budget`. Classify Prior Year and
Last Year columns as `actual` with their older dates. Never use an unknown,
blended, prior, YTD, TTM, or monthly scenario.

Use `YYYY-MM` for both months. Dates are inclusive: July 2024 is
`start_month=2024-07`, `end_month=2024-07`; calendar 2025 is `2025-01` through
`2025-12`.

`actual_months` is conditional. Omit it for Actual, Budget, monthly Forecast,
YTD Forecast, TTM Forecast, and other non-calendar-year Forecast periods. For a
full-calendar-year Forecast, omit it for a pure forecast and use integers 1
through 11 for reforecasts. The number means actual months already completed;
the forecast portion is `12 - actual_months`. Thus a 2025 reforecast containing
January through March Actual plus April through December Forecast has
`actual_months=3`.

The schema verifies and builds identity deterministically. Use these exact
rules when supplying IDs and labels:

- ID: `{start_month}_{end_month}_{scenario}`, with `_3p9` appended for a 3+9
  reforecast (and the analogous suffix for other reforecasts).
- Calendar year: `2025 Actual`, `2025 Budget`, or `2025 Forecast`; append the
  split for a reforecast, such as `2025 Forecast (3+9)`.
- One month: `June 2025 Actual`, `June 2025 Budget`, or `June 2025 Forecast`.
- January-starting partial year: `June 2025 YTD Actual` and analogous scenarios.
- Non-calendar twelve months: `June 2025 TTM Actual` and analogous scenarios.
- Any other range: `Jul 2024–Sep 2024 Actual` and analogous scenarios.

Do not independently invent another label or ID format. A mismatched label or
ID is rejected by the schema.

Then call `submit_periods`. That ends the session.
