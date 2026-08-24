# Phase two — period detection

Routing is settled. Nothing you do now changes it, and you do not need to revisit
it: a sheet you do not open in this phase keeps the decision you already gave it.

Now work out which reporting periods this workbook offers.

The result of `submit_routing` lists the workbook's `financial_sheets` and a
`must_read_at_least` count. Read the header rows of **at least that many of
them**. `submit_periods` is refused until you have.

Only sheets you routed to `triage` or `unsure` count. A sheet you marked `skip`
holds no P&L content, so reading it tells you nothing about which periods this
workbook reports — reading skipped tabs does not satisfy the requirement and is
usually a waste of a call.

Start with a summary or statement of income, then read **departmental
schedules** — Rooms, F&B, A&G and the rest. Reading only the summary is not
enough, and it is the most common way this step goes wrong.

**If — and only if — the workbook has a tab that looks like it carries a
different period grain, spend one of the five on it.** Tabs named or previewing
as `12Mth`, `Monthly`, `Trend` or `Trailing` sometimes do. On one workbook the
summary offers only December MTD and YTD across four scenarios while a `12Mth`
tab carries all twelve months, and reading only one of them loses the other.

Most workbooks have no such tab. That is normal and needs no hunting: plenty of
files legitimately offer a current period and a year to date, and nothing else.

## One period per column, always

**Return one period for every amount column, never a family of columns as a
single period.** A row of headers reading `January … December` plus `Total` is
**thirteen** periods, not one. `Jan'25 … Dec'25` with `Actual` under each is
twelve periods, not "2025 monthly actuals". If a sheet also carries `Current`,
`Budget` and `Prior Year` columns, those are further periods on top of the
months.

This is the most common way this step goes wrong. Something downstream has to
let a reader pick March, and it can only offer what you list — summarising twelve
columns as one annual period silently removes eleven choices.

Before submitting, count the amount columns you saw on your richest sheet. Your
period list should be about that long. If it is much shorter, you have rolled
columns up; expand them.

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
as you go. First identify the controlling consolidated Summary, Statement of
Income or annual statement sheet. Its real amount columns establish the
workbook's canonical periods. If a separate `12Mth`, `Trend`, `Monthly` or `TTM`
sheet genuinely carries an additional grain, it may anchor those additional
periods too. If no consolidated sheet exists, use the richest substantive P&L
sheet as the anchor.

Return the periods shown on those controlling sheets. **Do not delete a valid
controlling period merely because another financial sheet lacks it.** The next
stage binds every chosen period independently on every routed sheet and can mark
one sheet unavailable without losing the period for the whole workbook.

For each canonical period, compare the other sheets and put into
`sheets_present` every sheet you actually opened that carries the same economic
period. This is a coverage record, not an all-sheets admission test. Never add an
unread sheet on the assumption that it matches.

Compare economic identity rather than exact wording. Scenario and date coverage
must match:

- A December YTD ending at a December fiscal year-end, a Jan–Dec `Total`, a
  full-year column and a TTM covering that same January–December are the same
  annual period when all are Actual.
- A partial YTD is not a full year. A rolling TTM is not a fiscal year unless its
  start and end months are the same. A single month is never annual.
- Actual, Prior Actual, Budget and Forecast remain separate even when their dates
  match. Never merge or substitute scenarios.

Keep one canonical entry for equivalent presentations and describe the differing
headers in its evidence. A detail sheet that lacks the period simply stays out of
that period's `sheets_present`; mention material coverage gaps in `notes`.

This rule is not permission to promote an isolated, unusual column from an
arbitrary supporting schedule into a workbook choice. Periods come from the
controlling statement sheets; departmental schedules confirm coverage and supply
detail for the later binding stage.

### The check to run before submitting

For each period, confirm that it appears on at least one controlling statement
sheet you opened. Then verify that `sheets_present` contains only opened sheets
where you observed either that exact header or a scenario-and-date-equivalent
column. Uneven coverage is valid and should remain visible rather than causing
the period to disappear.

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

For each period: `period_id` (short and stable, such as `2025_ytd_actual`), a
human `label`, `period_type`, `scenario`, and `start_period` / `end_period` as
`YYYY-MM` where the dates are legible.

`period_type` is one of `month`, `current_period`, `ytd`, `full_year`, `ttm`,
`unknown`. A single calendar month is `month`. A column totalling a whole year is
`full_year`; this includes a YTD column ending at that workbook's fiscal
year-end. A cumulative column ending part way through the fiscal year is `ytd`.

Set `recommended_period_id` to the one a reader would most likely want — normally
the current year-to-date or full-year actual.

Then call `submit_periods`. That ends the session.
