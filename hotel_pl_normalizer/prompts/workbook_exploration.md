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

Return one entry for **every** sheet `list_sheets` gave you, using the decisions
and role hints described in the Sheet Name Triage Skill appended below. That
skill is the authority on which sheets matter.

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

Also set `workbook_layout`:

- `single_tab_p_and_l` — the whole statement is on one sheet. Other sheets, if
  any, are support or analysis.
- `multi_tab_department_p_and_l` — a summary plus per-department schedules.
- `mixed_or_unknown` — anything else, or not yet clear.

Put what convinced you in `layout_evidence`.

Then call `submit_routing`.

---
