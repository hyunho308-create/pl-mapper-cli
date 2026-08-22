# Sheet Name Triage Skill

Use this skill to route workbook sheets before department identification.

Input: a workbook id, source filename, and every sheet name in order, with
compact `header_cells` from each sheet. Treat the headers as stronger evidence
than a coded or generic tab name. Make the routing decision in this one call.

Output: a `SheetNameSelectionResult`.

## Goal

Decide which sheets should receive richer row-level triage.

This step should also classify the workbook layout and provide department
candidates for each sheet when the name gives enough evidence. Do not classify
subsections, periods, row ranges, or COA mappings here.

If a tab name is a code such as `0400`, `0232`, or `F89_RC_ROOMS`, inspect any
provided header/title cells. Operator workbooks often put the true title inside
the sheet, such as `0232 - Group Banquet` or `0410 - Administrative and
General`. Use those title cells to classify the sheet.

## Typical Hotel P&L Workbook Structure

Useful sheets often include:

- Summary operating statement or statement of income.
- Department P&L schedules for Rooms, Food and Beverage, Other Operated
  Departments, Miscellaneous Income, Administrative and General, Information
  and Telecommunications Systems, Sales and Marketing, Property Operations and
  Maintenance, Utilities, Management Fees, Fixed Charges, and
  Non-Operating Income and Expense.
- F&B outlet, venue, restaurant, bar, combined banquet/catering, event
  technology, room service, in-room dining/IRD, and minibar tabs are F&B
  detail. Keep them for richer triage as `triage` because they may be needed
  for department-level validation or fallback extraction. The mapping flow
  should still prefer the consolidated F&B summary first.
- Staff dining, employee dining, generic `Dining` support tabs, and F&B
  retail-shop support tabs should not be treated as F&B outlet detail when a
  consolidated F&B summary and normal venue/outlet tabs exist. Skip these unless
  they are the only available source for a required F&B value.
- Rooms reservation support tabs, reservations statistics/cost tabs, and market
  mix analysis tabs are not Rooms department locations when a core Rooms tab
  exists. Skip these unless the core Rooms tab is missing or validation later
  explicitly asks for supporting detail.
- If a combined Banquet/Catering tab exists, standalone Banquet and Catering
  tabs are usually subsections/supporting tabs for that combined schedule, not
  separate department locations.
- Other Operated detail tabs such as parking, gift shop, retail, spa,
  recreation, golf, guest communications, guest laundry, and minor operated
  departments are OOD detail. Keep them when they may be needed for validation
  or fallback extraction. Guest Laundry means a guest-facing operated outlet
  and is different from an in-house Laundry support schedule.

Usually skippable sheets include:

- Balance sheet tabs.
- Check, audit, validation, formula, or reconciliation tabs.
- Data cube, raw data, download, export, or parameter tabs.
- Statistics-only tabs unless the workbook has no usable P&L tab names.
- Payroll detail, labor productivity, overtime, EFTE/PTEB, and employee detail
  tabs unless there is no department P&L tab for labor.
- Reservation support, reservations statistics, and market mix analysis tabs
  when a core Rooms department tab exists.
- Staff dining, employee dining, generic Dining support tabs, and F&B retail
  shop support tabs when consolidated F&B and normal F&B venue/outlet tabs
  exist.
- Plain Laundry tabs, which are usually in-house support/detail schedules
  allocated to Rooms and F&B rather than Standard COA department tabs in this
  MVP flow. Do not apply this skip rule to Guest Laundry tabs; Guest Laundry is
  OOD detail.
- Duplicate summary tabs. It is common to have both a full summary that goes to
  EBITDA and a department summary that stops at GOP. Prefer the fuller summary
  and skip the duplicate/supporting version when both exist.
- Generic coded F&B department tabs when the workbook also has an explicit F&B
  summary plus outlet/detail tabs. In that case, the generic coded F&B tab is
  often a duplicate/supporting schedule.
- TTM, 12-month, budget, forecast, and analysis tabs when the target task is
  actual YTD mapping and a current actual P&L tab exists.

## Decision Rules

- `triage`: sheet name is likely to contain mapped P&L statement rows or a
  department schedule that may be needed for mapping.
- `defer`: sheet may become useful later, but should not receive rich triage
  immediately. Use this only when the sheet is a conditional supporting schedule
  whose department cannot be trusted from the name alone. Do not defer obvious
  F&B detail sheets such as venue, outlet, bar, restaurant, event technology,
  room service, in-room dining/IRD, minibar, banquet, catering, or guest
  laundry tabs; route those to `triage`. Do not defer plain in-house Laundry;
  route plain Laundry to `skip`.
- `skip`: sheet name is clearly calculation, analysis, check, balance sheet,
  raw data, or supporting-only information.
- `unsure`: sheet name is too short, coded, generic, or ambiguous. Use this
  when richer sheet facts are needed before deciding.

When in doubt between `skip` and `unsure`, choose `unsure`.

When in doubt between `triage` and `defer`, choose `defer` for detail tabs that
are only needed if a summary sheet fails validation.

When in doubt between `triage` and `unsure`, choose `triage` if the sheet name
resembles a core hotel P&L department; otherwise choose `unsure`.

## Output Requirements

Return one selection for every input sheet name.

Populate workbook-level layout fields:

- `workbook_layout`: `single_tab_p_and_l`, `multi_tab_department_p_and_l`, or
  `mixed_or_unknown`
- `layout_evidence`

Populate:

- `sheet_name`
- `decision`
- `role_hint`
- `department_hints`
- `evidence`

Also populate:

- `selected_sheet_names`
- `deferred_sheet_names`
- `skipped_sheet_names`
- `unsure_sheet_names`

Do not invent sheet names.

For each `department_hints` item, return `department`, `section_role`, and
`evidence`. Use only these canonical department strings:

- `summary`
- `rooms`
- `food_and_beverage`
- `other_operated_departments`
- `miscellaneous_income`
- `administrative_and_general`
- `information_and_telecommunications_systems`
- `sales_and_marketing`
- `property_operations_and_maintenance`
- `utilities`
- `management_fees`
- `non_operating_income_and_expense`

Use these section roles exactly:

- `primary` — the department's main schedule;
- `summary` — a consolidated block or department rollup;
- `detail` — an outlet, venue, or child schedule;
- `supporting_detail` — a schedule supporting a department reported elsewhere;
- `kpi` — statistics or productivity rather than the financial schedule;
- `unknown` — the role cannot yet be established.

Use multiple department hints when a sheet is ambiguous or contains several
departments. Leave the list empty when the sheet should be skipped or its
department cannot be established. Do not invent `mixed` or `unknown` as
departments and do not invent row boundaries.

A generic Fees, Fee Departments, Shared Fees, or similar sheet is not safe to
defer solely because its name is broad. Triage it with both `management_fees`
and `sales_and_marketing` as candidates and read enough content to distinguish
them when possible. Such sheets
commonly contain management fees, franchise/royalty/brand fees, or bounded
ranges for both.

Short acronyms are weak evidence and may have different meanings across
operators. For example, `RM` can mean Revenue Management, Repairs and
Maintenance, or another operator-defined schedule. Do not resolve an ambiguous
acronym from the sheet name alone. Return every plausible department hint,
and use `unsure` or `triage` until sheet labels establish the department.
