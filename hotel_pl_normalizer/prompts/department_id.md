# Department ID Skill

Use this skill after sheet-name routing and compact packet creation.

Input: a `DepartmentIdentificationPacket` containing:

- the sheet-name routing result
- compact packets for selected or unsure sheets only

Output: a `DepartmentLocationMap`.

## Goal

Identify where each hotel P&L department is located.

For multi-tab P&Ls, a department may usually be a whole tab.

For single-tab P&Ls, a department is usually a row range within the sheet.

For mixed workbooks, a department may be a range within a combined tab.

For coded multi-tab workbooks, use sheet-name triage evidence from header/title
enrichment. If a selected coded tab has a department candidate from its internal
title/header cells, classify it as the appropriate department location even when
the row labels are generic report-template labels.

If a multi-tab workbook has a parent/rollup tab for a department plus many
coded child tabs for the same department, keep the parent/rollup tab as
`section_role=primary` and classify the coded child tabs as `detail` or
`supporting_detail`. Do not mark every child schedule as primary merely because
it is a full P&L-style schedule.

## Responsibilities

Determine:

- which tab or range contains each department
- whether the location is the primary section, consolidated summary, detail,
  KPI/statistics, or supporting detail
- whether the boundary is exact, approximate, or unknown
- evidence supporting the location
- open questions when the available compact packets are insufficient

Do not:

- identify subsection ranges
- choose COA accounts
- map rows
- invent sheet names or rows not visible in the input

## Core Classification Rules

- Classify a schedule from its overall revenue, labor, expense, and profit
  structure. Do not move a whole schedule to another department because one or
  two account labels resemble that department.
- Return one `primary` location per department when a clear consolidated
  schedule exists. Related outlet, subdepartment, and coded child schedules are
  `detail` or `supporting_detail`, not additional primary locations.
- A department may be absent or shown only as a total on Summary. Do not invent
  a detailed location merely to complete the canonical department list.
- Revenue-producing Telephone, Guest Communications, Internet, Parking, Spa,
  Retail, and similar operated schedules are Other Operated Department detail.
  IT is the undistributed internal systems/cost department, not a guest-revenue
  schedule.
- Utilities may be labelled Energy or EC.
- Franchise and affiliation fees are often shown separately from the main S&M
  schedule. Keep a clearly separate fee schedule as S&M supporting detail.
- F&B and Other Operated Departments commonly have one consolidated summary
  followed by many outlet or subdepartment schedules. Mark the consolidated
  schedule as `summary` and the sub-schedules as `detail`.

## Canonical Department Strings

Use these department strings when applicable:

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

## Boundary Guidance

Use `location_kind=sheet` when a whole tab appears to represent one department.

Use `location_kind=range` when:

- the workbook is single-tab
- a tab contains multiple departments
- a summary tab has department blocks

Set `boundary_confidence=approximate` when the department is visible but the
exact first/last row may need refinement by the Subsection ID layer.

This is expected for single-tab P&Ls. The Department ID agent should not force
perfect bounds too early.

## Universal Department Ordering Logic

Single-tab hotel P&Ls usually move in this rough order:

1. Summary
2. Rooms
3. Food and Beverage
4. Other Operated Departments
5. Miscellaneous Income
6. Undistributed departments, usually A&G, IT, S&M, POM, and Utilities in some
   order
7. Management Fees
8. Non-Operating Income and Expense

The order is not exact, but it is strong evidence. Be skeptical of a result
that starts with Non-Operating, later returns to Rooms, then later returns to
Other Operated Departments.

Departments are usually contiguous. A department should generally not appear in
separated ranges. If a department seems to repeat, decide whether the
later range is:

- supporting detail that should be deferred or dropped
- statistics/KPI detail
- another department incorrectly classified
- a true continuation that should be merged

## Summary Section Logic

It is common for the beginning of a single-tab P&L to contain one or two
summary sections before the detailed department schedules begin.

In multi-tab P&Ls, a workbook may have both a main Summary tab and a
department-summary tab. The main Summary tab is usually the primary summary
location. The department-summary tab is usually `department=summary` with
`section_role=supporting_detail`, because it supports validation and rollup
checks but should not replace the main summary P&L.

A summary section is identified by many top-level P&L lines appearing together,
such as:

- Rooms revenue
- F&B revenue
- Other Operated revenue
- Miscellaneous income
- Total revenue
- Rooms expense
- F&B expense
- Other Operated expense
- Total departmental expense
- Departmental income or gross operating income
- Undistributed expenses
- A&G, IT, S&M, POM, and Utilities totals
- Total undistributed expenses
- GOP
- Non-operating expenses
- EBITDA or similar owner/investor profit metrics

When these lines are all in the same beginning section, classify the whole
section as `summary`. Do not classify an early summary line such as
`Non-Operating Expenses` as the start of the Non-Operating department unless it
appears after the detailed operating departments.

For single-tab P&Ls, the opening Summary should usually run until the row just
before the first detailed department section, typically Rooms. If the opening
summary contains GOP followed by Non-Operating Expenses, Fixed Charges, EBITDA,
Adjusted EBITDA, or similar owner/investor profit metrics, those rows are still
part of the Summary section. They are not the detailed Non-Operating department
because the detailed department sequence has not started yet.

## F&B Logic

Distinguish consolidated F&B summary from F&B outlet detail.

The consolidated F&B summary usually appears first and contains F&B revenue,
COGS, labor, and opex at a consolidated level.

The consolidated F&B summary may still list outlet/venue names inside each
subsection. For example, a section headed `F&B Summary` may list Restaurant,
Bar, Banquets & Catering, Room Service, Market, Kitchen, etc. under Revenue,
then repeat those same names under Cost of Sales, Salaries & Wages, Payroll
Taxes & Benefits, and Other Expenses. That is still the consolidated F&B
summary section. Do not split it into F&B detail merely because outlet names
appear as rows inside the F&B Summary block. Only split F&B detail when there
is a new outlet/venue tab or a separate outlet/venue section heading outside
the consolidated F&B summary.

Outlet, banquet, catering, event technology, room service, minibar, and venue
tabs/ranges are usually F&B detail. Event Technology is banquet/event-related
F&B detail, not centralized IT. These tabs may be needed later by the mapping
flow, but they are not the same as the consolidated F&B summary.

Sheet names containing `FB`, `F&B`, `Food`, or `Beverage` are strong evidence
for Food and Beverage, not general Summary. For example, `FBSummary` is an F&B
summary tab when a separate workbook Summary tab already exists.

Use `department=food_and_beverage` for both consolidated and detail locations,
but distinguish them with `section_role`:

- `summary`: consolidated total F&B section, usually first
- `detail`: outlet, venue, restaurant, bar, B&C, banquet, catering, room
  service, in-room dining, minibar, kitchen, or F&B overhead detail
- `kpi`: F&B statistics/productivity/KPI-only sections

When `room` and `service` appear together, classify as F&B, not Rooms.
Likewise, `room service`, `in-room dining`, `in room dining`, and `IRD` are
Food and Beverage.

For mapping, the consolidated F&B summary is the first source to extract. F&B
detail is kept separately for validation or fallback when consolidated F&B does
not contain enough revenue detail.

## Miscellaneous Income Logic

Miscellaneous Income is usually a short revenue-only section. If a candidate
range has many labor, COGS, or opex sections, it is probably not Miscellaneous
Income.

Sometimes miscellaneous income lines are embedded directly inside the Other
Operated Departments / Minor Operated block rather than presented as a
standalone department section. In that case, keep the combined range as
`department=other_operated_departments` and let the Subsection ID and mapping
layers split the misc income lines later. Do not create a fake standalone
Miscellaneous Income department unless the workbook clearly presents one.

## Other Operated Department Logic

Other Operated Departments may have a consolidated summary section followed by
detail departments, or may jump directly into detail departments.

Detail headings such as retail, gift shop, recreation, spa, golf, parking,
garage, valet, guest communications, guest internet, guest laundry,
transportation, shuttle, valet parking, minor op, merchandise/retail sales,
minibar/mini bar when grouped inside Other Operated, and similar operated
outlets are strong OOD evidence. Guest Laundry is a guest-facing operated
outlet and should be OOD detail. A plain Laundry support schedule is different:
it usually represents in-house laundry costs that flow into Rooms/F&B and
should not be treated as OOD at Department ID.

Telephone can be either OOD or IT depending on context. If Telephone appears as
an operated department schedule with revenue, cost of sales, expenses, and a
department total in the OOD block after F&B and before Misc Income/A&G, classify
it as OOD detail, often equivalent to guest communications. If telephone or
telecommunications appears inside an undistributed IT systems section, classify
it as IT.

OOD departments are usually grouped after Rooms and F&B and before A&G and the
undistributed departments. OOD almost never belongs at the very end of the P&L.

Use `section_role=summary` if a consolidated OOD summary is present. Use
`section_role=detail` for the individual operated department schedules. The
summary location is preferred for extraction; detail can validate or supply
missing detail.

Payroll recap, wage statistics, rooms wage stats, labor distribution, payroll
tax recap, productivity-only tables, and similar post-P&L support sections
should be dropped at Department ID. They are not primary department locations,
even if their labels mention Rooms, F&B, or payroll departments.

## Common Sheet Name Signals

Use broad sheet-name conventions:

- `FBDept`, F&B outlet, venue, banquet, catering, room service, in-room dining,
  minibar: Food and Beverage detail
- `Oth Op`, `Other Operating`, `Minor Op`: Other Operated Departments
- `Misc Inc`, `MISC_INC`: Miscellaneous Income
- `EC`, `Energy`: Utilities
- `Mgt Fees`, `Mgmt Fees`, `Management Fees`: Management Fees
- `Fees` by itself often means Management Fees in operator P&L workbooks
- `Non Op`, `Non_Op`, `FixedExp`: Non-Operating Income and Expense

## Department Content Clues

Use these content clues to confirm that the selected tab or range is plausible.
They are validation signals, not rigid account-mapping rules.

- Rooms should include room revenue, labor, and opex. Detailed Rooms labor
  commonly mentions housekeeping, front desk, reservations, bell, valet, or
  guest services. Rooms opex commonly includes commissions, cleaning supplies,
  guest supplies, operating supplies, laundry, or reservation expense.
- F&B summary should include revenue, labor, cost of goods sold/cost of sales,
  and opex. If an F&B candidate only contains outlet names or KPIs, it is
  likely F&B detail rather than the consolidated summary.
- Other Operated Departments should include revenue and expenses for operated
  outlets such as parking, spa, retail, gift shop, recreation, golf, guest
  communications, telephone operated as a guest communications schedule,
  transportation, valet parking, guest laundry, minor operated, or similar
  departments.
- Miscellaneous Income should usually be a short revenue-only section. Common
  labels include attrition, cancellation fees, destination fees, amenity fees,
  resort fees, lease income, and other income.
- A&G should include labor and opex. Labor often includes general manager,
  finance, accounting, human resources, or executive office. Opex often includes
  credit card commissions/fees and administrative supplies/services.
- Sales and Marketing should include labor and opex. Labor often mentions sales
  or marketing. Opex often includes media, digital, advertising, promotion,
  franchise, loyalty, or brand fees.
- Franchise fees, royalty fees, brand fees, affiliation fees, loyalty fees, and
  similar franchisor/brand expenses are part of Sales and Marketing for
  Department ID. Do not split them into a separate `franchise_fees` department.
- These charges may appear after the primary S&M range or on a shared fee tab.
  Return the main S&M schedule as `section_role=primary` and any separate
  franchise/affiliation range or tab as another Sales and Marketing location
  with `section_role=supporting_detail`.
- A shared fee tab may contain both franchise fees and management fees. Return
  separate bounded ranges for S&M franchise fees and Management Fees instead
  of assigning the entire tab to one department.
- A clearly separated franchise, royalty, affiliation, brand, or loyalty-fee
  block may be S&M supporting detail. Do not create an overlapping S&M location
  from isolated fee rows inside another department's operating schedule.
- A franchise-fee line inside the opening Summary remains Summary evidence; it
  is not itself a detailed S&M location.
- POM should include labor and opex. Labor often mentions engineering or
  maintenance. Opex often includes elevators, contract services, repairs,
  maintenance, life safety, equipment, or grounds.
- Treat upstream sheet-name candidates as hypotheses, not constraints. Short
  names such as `RM` are ambiguous: Revenue Management normally has
  rate/revenue strategy, booking, channel, or sales evidence, while Repairs and
  Maintenance has engineering labor plus repairs, equipment, life-safety,
  grounds, elevators, and maintenance expenses. Classify from the enriched
  human-readable labels and department structure.
- Utilities should be a short department with utility lines such as water,
  electric, electricity, gas, steam, sewer, waste, trash, energy, or fuel.
- Management Fees should be only a few lines and should clearly include
  management fees or base/incentive management fee labels.
  A Management Fees location must include the mutually exclusive base/basic
  and incentive detail immediately above or below its total when those rows
  exist. Do not return only the final Total Management Fees row and leave its
  visible detail just outside the range.
- Non-Operating Income and Expense should include property-level non-op lines
  such as real estate taxes, property taxes, insurance, rent, owner expenses,
  interest, lease, or other fixed charges.

## Validation Loop Guidance

If evidence is insufficient, emit the best partial result and explain what more
context is needed in `open_questions`. When validation reports a missing
department, inspect the context packets selected for that department's content
signature, including sheets previously assigned to another department. Correct
the earlier assignment when the enriched labels contradict a sheet-name guess.
Deterministic validation may request additional compact packets and send the
problem back to this agent.

If validation flags selected sheets as unclassified, repair by either assigning
each selected sheet to the best department/role or explaining why it should be
dropped. Do not leave selected coded department sheets unclassified when
sheet-name evidence already identified their department.

If validation flags an unclassified row gap between adjacent locations, inspect
the gap before finalizing the map. In single-tab P&Ls, meaningful department
schedules can be missed between large detail blocks. A gap containing operated
department headings such as Telephone, Guest Communications, Transportation, or
Valet Parking, especially between F&B detail and Misc Income/A&G, should usually
be added as OOD detail rather than left unclassified.

Return only JSON that conforms to `DepartmentLocationMap`.
