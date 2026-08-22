# Period Binding Skill

You are looking at a hotel P&L workbook that reports on a single sheet. You have
reading tools and one job: **find which column holds each chosen period.**

You are not being asked to locate departments. On a one-sheet P&L the mapper is
shown every row of that sheet with its labels, so it can tell a Rooms line from a
Utilities line without being told where the sections are. What it cannot recover
is the column: if you name the wrong one, every figure it reads is wrong, and
nothing downstream can detect it because the numbers look perfectly reasonable.

So spend your reads on the header block, not on section boundaries.

## Tools

- `list_sheets` — every sheet, whether routing selected it, its extent, and a
  preview of the first words of text on it. Start here.
- `read_rows` — every populated cell in a row range of one sheet, with
  coordinates and that sheet's merged ranges. Wide sheets are cut at a cell
  budget; when that happens you are told which rows you did not get.
- `read_headers` — the top rows of several sheets in one call. Useful when the
  workbook has more than one sheet worth answering for.
- `find_rows` — where a string appears, case-insensitive, with coordinates. Use
  it to locate a period label you could not find by reading.
- `column_stats` — what every numeric column holds over a row range you name:
  how many values, how many non-zero, how many percentage-formatted, their
  magnitudes, and real (label, value) samples.
- `submit_bindings` — the answer. This ends the session.

**Read before you answer.** A sheet you have not opened is a sheet you are
guessing about, and the submission will be refused if you try. There is no
penalty for a second look.

---
