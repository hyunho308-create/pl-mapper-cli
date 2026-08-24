# Hotel P&L Mapper CLI

Map an operator hotel P&L into a bundled 271-account Standard COA while preserving the source workbook and a source-row audit trail.

Follow the [getting started guide](./GETTING_STARTED.md) to begin.

![Hotel P&L Mapper workflow](./hotel-pl-normalizer-flow.svg)

## What a completed run creates

| Output | Purpose |
| --- | --- |
| `[source name] [MAPPED].xlsx` | The operator P&L mapped into the Standard COA template. |
| `summary.json` | Plain run status, periods mapped or dropped, cost, duration, and model usage. |
| `run_log.json` | Source-row mapping and audit details. |
| `work/` | Detailed intermediate artifacts for review and troubleshooting. |

A successful run normally has `"accepted": true` in `summary.json`. Some qualified source discrepancies may be retained and clearly flagged for human review rather than changed to force a tie.

## Requirements

- Windows, macOS, or Linux
- Python 3.11 or newer
- An OpenAI API key with access to `gpt-5.6-luna`
- A complete hotel P&L workbook
- Dependencies: `openai>=1.0`, `openpyxl>=3.1`, `pydantic>=2.0`, `xlrd>=2.0`

## Terminal use

The [getting started guide](./GETTING_STARTED.md) is the recommended path using Codex which bypasses the need to use the terminal directly.

For direct terminal setup on Windows:

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install .
$env:OPENAI_API_KEY = "paste-your-key-here"
.\.venv\Scripts\hotel-pl-normalizer.exe --doctor
```

Run interactively:

```powershell
.\.venv\Scripts\hotel-pl-normalizer.exe "C:\P&Ls\Hotel.xlsx" "C:\P&Ls\Hotel-mapped"
```

The CLI lists validated periods and waits for one number, multiple comma-separated numbers, or Enter to accept its recommendation. Unattended alternatives are available through `--actual-and-prior`, `--annual-periods N`, `--period-id`, and `--recommended`.

`--doctor` checks Python, dependencies, bundled assets, and the API-key setting without displaying the key or making an API call.
