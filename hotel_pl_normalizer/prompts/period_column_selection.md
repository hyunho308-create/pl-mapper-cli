# Period Discovery

Identify every useful dollar period available in the hotel P&L. Do not choose
only one requested period. The human will choose from this catalog before
mapping begins.

## What Counts as a Period

Catalog source-supported Actual, Prior Actual, Budget, Forecast, and Blended
periods, including:

- individual months;
- current-period or MTD columns;
- YTD columns;
- full-year or annual totals; and
- trailing-twelve-month totals.

Do not catalog percentages, variances, margins, POR measures, occupancy, ADR,
RevPAR, or other KPI/statistical columns as dollar periods.

Do not create a workbook period solely from Stats, KPI, or other statistical
schedules. A period must have dollar P&L account-row evidence. It may appear
only on a Summary, P&L Trend, or dollar payroll schedule; those are valid when
the downstream P&L headers support the same period.

## Evidence

Use each location's coordinate-preserving headers and numeric candidate counts.
Headers may span several rows or appear as system metadata such as `TIME`,
`MONTH`, `CATEGORY`, `MEASURE`, `ActualFlag`, `PERIODIC`, or `YTD`.

Follow the operating mode in the task prompt:

- During representative discovery, identify canonical period concepts and use
  only the supplied representative locations as availability evidence. Do not
  bind every family member or expose a summary-only period.
- During selected-period binding, search every supplied department location and
  bind only the user-selected concepts to their exact numeric columns. Account
  for every selected period and every supplied location exactly once: return a
  binding with coordinate-preserving header evidence, or an explicit
  `unavailable_locations` disposition with the reason and inspected evidence.
  Never silently omit a location.

A Stats/KPI/supporting sheet may be omitted in either mode.

Before returning, audit every supplied header family's distinct period groups. For each MTD,
YTD, monthly, or annual group, pair every dollar scenario subheader such as
Actual, Budget, Forecast, and Prior Year with that group. Return each supported
combination as its own option; do not stop after finding one option per scenario.

Perform these two completeness checks before returning:

1. **Twelve-month spread:** If a representative dollar P&L header contains a
   sequence of monthly columns for one scenario and year, enumerate every visible
   month in that sequence. Seeing January is a cue to scan horizontally for
   February through December, not a reason to return only January. When at least
   ten distinct month names from the same annual sequence are visible, return all
   supported visible months (normally twelve) and inspect the following `Total`
   column for the corresponding full-year option.
2. **PTD/YTD comparison groups:** If headers contain a Current Period, Period to
   Date, PTD, or MTD group, inspect its scenario subcolumns and the adjacent YTD
   group. Conversely, finding a YTD group is a cue to look for the paired PTD/MTD
   group. Return every supported dollar scenario in each group—commonly Actual,
   Budget, and Prior/Last Year, with Forecast when present. A merged group label
   applies to all of its scenario subcolumns even when `MTD` or `YTD` is not
   repeated in each individual cell. A common layout repeats the same
   Actual/Budget/Prior-Year triplet twice while labeling only the right-hand
   block `YTD`; in that pattern, inspect the unlabeled left-hand triplet as the
   current-period/PTD group, using the report's as-of month. Do not apply this
   inference if another header explicitly gives the left-hand block a different
   meaning.

Silently compare the completed option list with these patterns. Do not return a
single month from a visible twelve-month sequence or only one side of a visible
PTD/YTD pair unless the other periods lack non-summary dollar P&L support.

Equivalent periods may use different columns on Summary and department tabs.
Check every location separately before grouping it. Confirm that the candidate's
own header context identifies the same month, scenario, and period; never assume
tabs share a column convention because their columns are nearby. A one-column shift between
Summary and department tabs is common. Group only locations whose exact candidate
column and header meaning match. Use only supplied `location_id` values and
candidate columns. If a period is absent from a location, omit that location.
An unlabeled or unidentified numeric candidate is not evidence that the period
exists on that location. Omit the location rather than inheriting or guessing
its column.

Use merged and multi-row header context to expand repeated local labels such as
`YTD`, `Budget`, or `LY`. Each human-facing label must include enough year,
scenario, and period context to distinguish it from every other option.

## Classification

- `scenario`: `actual`, `prior_actual`, `budget`, `forecast`, `blended`, or
  `unknown`.
- `period_type`: `month`, `current_period`, `ytd`, `full_year`, `ttm`, or
  `unknown`.
- `start_period` and `end_period`: use `YYYY-MM` when the workbook supports the
  dates; otherwise leave them null.

Each option must point to one source column per location. Never combine or sum
columns. A human may select several separate period options later.

Treat filename claims as supporting evidence, not authority. If the filename
and workbook headers conflict, follow the workbook headers and add a concise
warning.

Do not invent arbitrary month ranges such as Jan-Jun. Catalog the source-native
total and monthly periods that a human can actually select.

When a Jan-Dec monthly scenario block is immediately followed by a `Total`
column, treat that Total as the full-year period for the same scenario and year.
For example, twelve 2025 Actual month columns followed by `Total` means `2025
Actual`, even when the Total cell does not repeat the words `2025` or `Actual`.
Do not relabel that Total as a nearby Forecast, Budget, or prior-year period.

## Recommended Period

Recommend at most one option. Prefer a current Actual YTD or full-year period
with broad workbook coverage. A period does not need to exist in every location.

Keep labels and warnings short. Return only one `PeriodCatalog` matching the
output schema.
