# Workbook Exploration Skill

You are looking at a hotel P&L workbook that has **not** been read yet. Nothing
has been extracted for you. You have reading tools and you decide where to look.

This session has **two phases, in order**:

1. **Route every sheet.** Decide what each sheet in the workbook is.
2. **Find the periods.** You will be given instructions for this after phase one.

Do phase one completely before thinking about periods. The period instructions
arrive in the result of `submit_routing`.

## Tools

- `list_sheets` — every sheet, its position, whether it is visible, its declared
  size, and a `preview`: the first lines of text found on that sheet. Declared
  sizes are frequently wrong; treat them as a hint about which sheets are
  substantial, never as a boundary. The `preview` is real sheet content.
- `read_rows` — every populated cell in a row range of one sheet, with
  coordinates and that sheet's merged ranges. All columns come back, not a
  window. Call it as often as you like, on any sheet, at any row range.
- `find_text` — where a string appears, case-insensitive, with coordinates and
  the sheet each hit is on.
- `submit_routing` — the phase one answer.
- `submit_periods` — the phase two answer. Ends the session.

## Phase one — route every sheet

Return one entry for **every** sheet `list_sheets` gave you. For each sheet,
return only its exact `sheet_name`, a `decision`, a `role_hint`, and concise
`evidence` for the decision. Do not identify departments, subsections, row
ranges, periods, or COA mappings in this phase.

### Ask for more whenever you need it

Earlier versions of this job had to judge each sheet from a fixed extract and
live with whatever it contained. You do not. **If you are unsure about a sheet,
open it.**

- The `preview` is only the first lines of text. It is a starting point, not the
  sheet.
- `read_rows` returns every populated cell in the rows you ask for, across all
  columns — so if a sheet is wider than you expected, you are already seeing it.
  If the interesting content is further down, ask for a later range: rows 40-100
  are as available as rows 1-20.
- Call `read_rows` again with a different range as many times as you need. There
  is no penalty for a second look, and no reason to settle for a guess.
- `find_text` will locate a word anywhere in the workbook and tell you which
  sheet it was on. Use it when you do not know where to look.

An `unsure` decision should mean you looked and it was genuinely ambiguous — not
that you did not look.

### Judge coded tabs by content

Coded tab names such as `0400` or `F89_RC_ROOMS` are common and are usually
departmental schedules holding real P&L detail. The real title is inside the
sheet, like `0232 - Group Banquet`, and the preview is where you will see it. If
the preview is still not enough, read the sheet. Judge these by their content,
never by the code.

### Sheets that usually hold mapping evidence

Keep the core statement and substantive schedules that may supply Standard COA
values:

- A summary operating statement or statement of income.
- Department P&L schedules such as Rooms, Food and Beverage, Other Operated
  Departments, Miscellaneous Income, Administrative and General, Information
  and Telecommunications Systems, Sales and Marketing, Property Operations and
  Maintenance, Utilities, Management Fees, Fixed Charges, and Non-Operating
  Income and Expense.
- F&B outlet, venue, restaurant, bar, banquet/catering, event technology, room
  service, in-room dining/IRD, and minibar schedules. Prefer a consolidated F&B
  summary for totals, but keep real outlet detail available for mapping and
  validation.
- Other Operated detail such as parking, gift shop, retail, spa, recreation,
  golf, guest communications, and Guest Laundry. Guest Laundry is a
  guest-facing outlet; it is not the same as an in-house Laundry schedule.

If a combined Banquet/Catering schedule exists, standalone Banquet and Catering
tabs may be supporting detail. Use the workbook's content and relationships,
not a fixed name rule, to distinguish them.

### Sheets that are usually supporting-only

Normally skip sheets that do not supply P&L statement values:

- Balance sheets; check, audit, validation, formula, or reconciliation tabs.
- Data cubes, raw downloads, exports, parameters, and calculation tabs.
- Statistics-only, payroll-detail, labor-productivity, overtime, and employee
  tabs when a core P&L schedule is available.
- Reservations statistics, market mix, staff/employee dining, and similar
  support schedules when the core Rooms or F&B schedule is available.
- Plain in-house Laundry schedules. Do not apply this rule to Guest Laundry.
- Duplicate or shorter summary tabs when a fuller summary reaches further down
  the P&L.

A monthly, TTM, budget, forecast, or trend tab is not automatically skippable.
Keep it when it contains P&L amount columns or represents a distinct reporting
grain needed for period discovery; skip it only when it is analysis or support
and the financial statements provide the usable values.

### Routing decisions

- `triage`: likely to contain mapped P&L rows or substantive detail that may be
  needed for mapping or validation.
- `defer`: a conditional supporting schedule that may become useful if a core
  statement or summary cannot supply a required value.
- `skip`: clearly non-P&L, calculation, analysis, duplicate, or supporting-only.
- `unsure`: genuinely ambiguous after examining the available title/header
  evidence.

When uncertain between `skip` and `unsure`, choose `unsure`. When uncertain
between `triage` and `defer`, use `defer` only for conditional support; obvious
core department and outlet P&Ls belong in `triage`.

Use the role hints exactly as defined by the submission schema:
`summary_p_and_l`, `department_p_and_l`, `supporting_schedule`,
`check_or_analysis`, `balance_sheet`, or `unknown`.

Short acronyms are weak evidence. For example, `RM` may mean Revenue Management,
Repairs and Maintenance, or something operator-specific. Open the sheet and use
its title or labels; if it remains ambiguous, use `unsure` and `unknown` rather
than inventing a meaning.

Also set `workbook_layout`:

- `single_tab_p_and_l` — the whole statement is on one sheet. Other sheets, if
  any, are support or analysis.
- `multi_tab_department_p_and_l` — a summary plus per-department schedules.
- `mixed_or_unknown` — anything else, or not yet clear.

Put what convinced you in `layout_evidence`.

Then call `submit_routing`.

---
