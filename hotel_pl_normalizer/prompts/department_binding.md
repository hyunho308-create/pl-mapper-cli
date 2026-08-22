# Department and Period Binding Skill

You are looking at a hotel P&L workbook. Upstream routing has already decided
which sheets hold P&L content, and a person has already chosen which reporting
periods they want. You have reading tools and you decide where to look.

This session has **two phases, in order**:

1. **Locate every department.** Which sheet, and which rows within it.
2. **Bind each chosen period to a column.** You will be given instructions for
   this after phase one.

Do phase one completely before thinking about periods. The binding instructions
arrive in the result of `submit_departments`.

## What this is for, and which mistakes matter

Everything you return feeds one thing: the mapper, which is shown the workbook's
rows and decides which account each one belongs to. Your job is to tell it
**which column holds the figures** and **roughly where each department sits**.

Those two are not equally important.

- **The column matters most.** If you name the wrong column, every figure the
  mapper reads is wrong, and nothing downstream can detect it — the numbers look
  perfectly reasonable. This is the one thing worth spending reads on.
- **Department boundaries are a hint.** The mapper sees the row labels itself and
  can tell a Rooms line from a Utilities line. A span that starts five rows late,
  or a department you did not find at all, costs it a hint it can re-derive. It
  is not worth a long hunt, and it is never worth leaving a period unbound.

So: be careful about columns, be reasonable about boundaries, and always return
something. A partial answer is useful. A refusal is not.

## Tools

- `list_sheets` — every sheet, whether routing selected it, and a preview of the
  first lines of text on it.
- `read_rows` — every populated cell in a row range of one sheet, with
  coordinates and that sheet's merged ranges. All columns come back, not a
  window. Call it as often as you like, on any sheet, at any row range.
- `find_rows` — where a string appears, case-insensitive, with coordinates and
  the sheet each hit is on.
- `column_stats` — for a row range you name, what every numeric column in it
  actually holds: how many values, how many non-zero, how many percent-formatted,
  their magnitudes, and a few real (label, value) samples. Use it to check a
  column carries money before you bind it.
- `submit_departments` — the phase one answer.
- `submit_bindings` — the phase two answer. Ends the session.

**Read before you answer.** A sheet you have not opened is a sheet you are
guessing about. There is no penalty for a second look.

---

# Phase one — locate every department

Return one entry per department you can find. Use these strings exactly:

`summary`, `rooms`, `food_and_beverage`, `other_operated_departments`,
`miscellaneous_income`, `administrative_and_general`,
`information_and_telecommunications_systems`, `sales_and_marketing`,
`property_operations_and_maintenance`, `utilities`, `management_fees`,
`non_operating_income_and_expense`

**Where a department sits, and how to say it:**

- **A whole sheet is this department.** Omit `start_row` and `end_row`. Ordinary
  in a multi-tab workbook, where each tab is one department's schedule.
- **A row range within a sheet.** Give both `start_row` and `end_row`. This is
  the answer whenever one sheet carries more than one department — always in a
  single-tab P&L, and on any summary tab that has department blocks in it.

**Two departments cannot both be the whole of the same sheet.** If a sheet holds
several departments, every one of them needs rows. On a single-tab P&L that
means every department gets a range: saying "the whole sheet" ten times says
nothing, and the row numbers are the entire value of the answer.

If you are unsure exactly where a section ends, give your best row anyway. An
approximate boundary is useful; no boundary is not. You may also give
`start_row` alone and leave `end_row` out, and the section will be taken to run
to the next department below it.

A department that is genuinely absent should be absent. A limited-service
property has no F&B; a leased restaurant reports none of it. Do not invent a
location to complete the list.

## How a hotel P&L is laid out

Read the boundaries first — they are what tells you where one department stops
and the next begins.

### The opening Summary

Most P&Ls open with a summary section: many top-level lines together, one per
department, running from total revenue down to a bottom line. Rooms revenue, F&B
revenue, Other Operated revenue, total revenue, the departmental expenses, total
departmental expense, the undistributed departments as single lines, GOP.

**Where it ends** is the part that goes wrong. It ends at a bottom line, and the
bottom line is called EBITDA, EBDA, adjusted EBITDA, net income, income before
something, non-operating, fixed charges, or owner something. A Summary span that
continues past one of those has swallowed the detail underneath it.

In a single-tab P&L the opening Summary **ends at the row before the first
detailed department begins**, which is usually Rooms. It is the first block on
the sheet, not the whole sheet — a Summary running to the last row has swallowed
every department under it.

Two boundary rules that are easy to get backwards:

- **Non-op lines inside the opening summary belong to Summary.** In a single-tab
  P&L, a summary block containing GOP followed by Non-Operating Expenses, Fixed
  Charges and EBITDA is still all Summary. Those rows do not start the detailed
  Non-Operating department — the detailed department sequence has not begun yet.
  Non-Operating as a department appears *after* the operating departments.
- **Summary starts at the visible Summary heading.** Include the heading and the
  KPI or statistics rows at the top of the block. Exclude report titles, dates,
  and export metadata above it.

Where two opening summary blocks appear before the detailed departments, prefer
the later and fuller one — the one carrying more of the bottom-of-P&L lines.

In a multi-tab workbook with both a main Summary tab and a department-summary
tab, the main Summary tab is `summary` with `section_role=primary`, and the
department-summary tab is `summary` with `section_role=supporting_detail`.

### The order of the rest

After Summary, a P&L usually runs: Rooms, Food and Beverage, Other Operated
Departments, Miscellaneous Income, then the undistributed departments (A&G, IT,
S&M, POM, Utilities, in some order), then Management Fees, then Non-Operating.

The order is not exact but it is strong evidence. Be sceptical of a result that
starts with Non-Operating, later returns to Rooms, then returns to Other
Operated.

Departments are usually contiguous. **A heading naming another department inside
your span usually means the span swallowed its neighbour** — split it at the
heading. The exception is a short franchise, royalty, affiliation, brand or
loyalty fee block sitting inside another department's schedule: add that as an
overlapping S&M supporting range without truncating the department around it.

### When the sheet is grouped by revenue and expense instead

Some exports — accounting-package P&Ls especially — group **all** the revenue
together at the top and **all** the expense together below, so the sheet reads
Total Income, then Total Expense, rather than department by department.

Here the departments genuinely interleave: Rooms revenue sits beside F&B revenue
near the top, and Rooms expense sits beside F&B expense far below. Give each
department a span running from its first revenue row to its last expense row and
let them overlap. That is the correct description of this layout, not a mistake
to be tidied into contiguous blocks.

You can recognise it from the top of the sheet: an `Income` or `Sales` heading
covering several departments, and a matching `Expense` heading much further
down.

### Short departments are normal

**Management Fees is often three lines.** Utilities, Miscellaneous Income, Other
Operated and Non-Operating can all be very few lines too. A section is not
suspect for being short — that is the norm for these five, and a span that has
grown to cover many rows is the more likely error.

## What each department usually contains

These are guides for confirming a span is what you think it is, not rules. They
are deliberately generous, and a section missing one of them is worth a second
look rather than a rejection.

- **Rooms** — room revenue; labour, which commonly mentions housekeeping, front
  desk, reservations, bell, valet or guest services; and opex such as
  commissions, cleaning supplies, guest supplies, operating supplies, laundry or
  reservation expense. A span claiming to be Rooms with no labour line anywhere
  is usually a span that started below the revenue block or stopped above
  payroll.
- **Food and Beverage** — revenue, labour, **cost of goods sold**, opex. F&B is
  the department most often split per outlet, so distinguish the consolidated
  summary from outlet detail with `section_role`: `summary` for the consolidated
  block, `detail` for restaurant, bar, banquet, catering, room service, in-room
  dining, minibar, kitchen and F&B overhead. Both use
  `department=food_and_beverage`. **Find the consolidated summary if there is
  one** — mapping the outlets without it loses the department total quietly,
  because the outlets still add up to something plausible. A consolidated F&B
  summary may list outlet names as rows inside its own revenue and cost
  subsections; that is still the summary, not detail. Only split when there is a
  separate outlet section heading outside it. Room service, in-room dining and
  IRD are F&B, not Rooms.
- **Other Operated Departments** — revenue and expense for operated outlets:
  parking, garage, valet parking, spa, retail, merchandise, gift shop,
  recreation, golf, guest communications, guest internet, guest laundry,
  transportation, shuttle, minibar, minor operated. These rarely carry a heading
  naming the USALI department, so identify them by the outlet. OOD sits after
  Rooms and F&B and before A&G; it almost never belongs at the very end.
  **Guest Laundry is an operated outlet and belongs here. A plain Laundry
  support schedule is different** — that is in-house laundry cost flowing into
  Rooms and F&B, and is not OOD.
- **Miscellaneous Income** — mostly revenue: attrition, cancellation,
  destination fee, amenity fee, resort fee, lease income, other income. **It
  sometimes carries expenses.** That is not right under USALI but it is what
  operators do, so expense lines are not evidence against it.
- **Administrative and General** — labour, commonly general manager, finance,
  accounting, human resources, executive office; and opex such as credit card
  commissions, professional fees, office and administrative supplies.
- **Sales and Marketing** — labour mentioning sales or marketing; opex such as
  media, digital, advertising, promotion, loyalty or brand. **Franchise fees are
  only present at branded hotels**, so their absence says nothing at all.
- **Property Operations and Maintenance** — labour mentioning engineering or
  maintenance; opex such as elevators, contract services, repairs, life safety,
  equipment, grounds. May be labelled Repairs and Maintenance. A short sheet
  named `RM` is ambiguous: Repairs and Maintenance has engineering labour and
  repairs, while Revenue Management has rate, booking or channel content.
- **Utilities** — water, electric, gas, steam, sewer, waste, trash, energy,
  fuel. Often labelled Energy, or a coded tab such as `EC`.
- **Management Fees** — management fee, base fee, incentive fee. Include the
  base and incentive detail immediately above or below the total when those rows
  exist; do not return only the Total row and leave its detail outside the span.
- **Non-Operating Income and Expense** — real estate or property taxes,
  insurance, rent, owner expenses, interest, lease, other fixed charges.

## Three cases that are genuinely tricky

**Telephone can be either OOD or IT.** Where Telephone appears as an operated
department schedule — with revenue, cost of sales, expenses and a department
total, sitting in the operated block before the undistributed departments — it
is Other Operated detail, usually equivalent to guest communications. Where
telephone or telecommunications appears inside an undistributed IT systems
section, it is IT. IT is the internal systems cost department; it is not a
guest-revenue schedule.

**Fees are often grouped together even though they are different departments.**
Franchise, affiliation and royalty fees belong to **Sales and Marketing**,
always — both the summary S&M line and the departmental S&M schedule. Management
fees belong to **Management Fees**, their own department. What makes this tricky
is where they physically sit: the three commonly share one block or one fee tab.
A shared fee tab should come back as **two bounded ranges** — S&M for the
franchise and affiliation rows, Management Fees for the management fee rows —
never as one tab assigned to one department. A franchise fee line inside the
opening Summary is Summary evidence; it does not make a separate S&M location.

**Other Operated and Miscellaneous Income often share a span.** Where misc
income lines sit inside the Other Operated or Minor Operated block rather than
as a standalone section, keep the combined range as
`other_operated_departments` and let the mapper split the misc income lines. Do
not invent a standalone Miscellaneous Income department the workbook does not
present.

## Section roles

- `primary` — the department's main schedule. Most departments have exactly one.
- `summary` — a consolidated block above its own detail, mainly F&B and OOD.
- `detail` — an outlet, venue or sub-schedule under a parent department.
- `supporting_detail` — a schedule supporting a department reported elsewhere,
  such as a department-summary tab or a separate fee range.
- `kpi` — statistics, productivity or per-occupied-room tables.

Where a department has a parent or rollup tab plus several coded child tabs,
keep the parent primary and mark the children `detail`. Do not mark every child
primary just because each is a full P&L-style schedule.

Payroll recaps, wage statistics, labour distribution, payroll tax recaps and
productivity-only tables are not department locations. Leave them out.

## Coded tabs

Coded names such as `0400`, `EC`, `OTH_OP`, `F89_RC_ROOMS` are common and are
usually real departmental schedules. The real title is inside the sheet, like
`0232 - Group Banquet`, and the preview is where you will see it. Judge these by
content, never by the code. Treat any upstream sheet-name guess as a hypothesis,
not a constraint.

Then call `submit_departments`.
