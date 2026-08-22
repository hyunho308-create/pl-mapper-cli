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

If the first rows you read show no periods, that is almost always a reading
mistake rather than a fact about the workbook. Read further down, or use
`find_text` for a month name, `YTD`, `Actual`, `Budget` or a year.

## Confirm every period against the detail sheets

Work out the periods **for each sheet you read, separately**. Do not pool them as
you go — one list per sheet, then compare.

**Return the intersection: only the periods that every financial sheet you read
offers.** Put those sheets into that period's `sheets_present`, so the list is
the evidence that the period really is available throughout.

`sheets_present` lists **only sheets you actually opened in this phase**. Do not
add the rest of the financial sheets on the assumption they match — the point of
the list is to record what was checked, and padding it with unread names destroys
exactly that.

Summaries routinely advertise more than the schedules behind them. A summary may
show twelve months while every departmental schedule carries only December and a
year to date; those ten extra months are not usable, because nothing downstream
can produce departmental detail for a period the departments do not report. The
usable periods are the ones present in the full detail.

So a period on the summary but missing from the schedules is dropped. A period on
one schedule but missing from the others is dropped too — the test is the same
for every sheet, and the summary gets no special standing. List what you dropped
and why in `notes`, so a reader can see what the workbook claimed to offer.

This applies only when the workbook actually has departmental schedules. On a
single-tab P&L there is nothing to confirm against, so **keep every column that
sheet shows** — all of them, enumerated one per column as above.

Dropping means removing a period from the list. It never means merging several
into one. If twelve monthly columns survive, return twelve periods; if they are
dropped, return none of them. `2025_monthly_actual` standing in for twelve
separate months is not a period, and is never the right answer.

**Intersect the columns, not the bands.** A scenario is part of a period's
identity: `Dec 2025 Actual` and `Dec 2025 Budget` are two different periods that
happen to share a month. Comparing sheets band-by-band and returning one period
per band collapses that away.

Worked example. Every schedule shows a `Dec 2025` band and a `YTD` band, each
with `Actual`, `Budget`, `Forecast` and `Prior Year` columns. All eight columns
appear on all five sheets, so **all eight survive**:

    dec_2025_actual, dec_2025_budget, dec_2025_forecast, dec_2025_prior
    ytd_2025_actual, ytd_2025_budget, ytd_2025_forecast, ytd_2025_prior

Returning two — one for `Dec 2025` and one for `YTD` — is wrong. Nothing was
dropped here; the intersection was complete. If your period count is far smaller
than the number of amount columns each sheet carries, you have merged rather
than intersected.

### The check to run before submitting

For each period, compare its `sheets_present` against the list of sheets you
read in this phase. **If `sheets_present` does not contain every one of them,
drop that period.** No exceptions, and it does not matter which sheet is the
odd one out.

That single test is what "intersection" means here. A period found on the trend
tab alone, with `sheets_present` naming one sheet out of the eight you read, is
not available in the detail and does not belong in the answer — however many
columns it spans, and however real those columns are.

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
`full_year`; a cumulative column ending part way through is `ytd`.

Set `recommended_period_id` to the one a reader would most likely want — normally
the current year-to-date or full-year actual.

Then call `submit_periods`. That ends the session.
