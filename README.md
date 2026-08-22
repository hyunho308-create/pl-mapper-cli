# Hotel P&L Normalizer CLI

Maps one hotel P&L workbook to the bundled 269-account Standard COA. It creates:

- a separate `[MAPPED].xlsx` workbook;
- `summary.json` with the run result; and
- `run_log.json` with the source-row audit trail.

The source workbook is never overwritten. Models select source rows; Python reads the values, performs the arithmetic, and validates the result before writing the authored output template.

## Requirements

- Windows, macOS, or Linux
- Python 3.11 or newer
- An OpenAI API key with access to the configured model
- A complete hotel P&L in `.xlsx`, `.xlsm`, or `.xls` format

Runs make paid OpenAI API calls and commonly take 10–20 minutes. Workbooks and run logs contain confidential financial data; keep them out of Git.

## Install

Clone this repository, open a terminal in it, and create an isolated environment.

### Windows PowerShell

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install .
$env:OPENAI_API_KEY = "paste-your-key-here"
.\.venv\Scripts\hotel-pl-normalizer.exe --doctor
```

The environment-variable command sets the key only for the current PowerShell window. Enter it again in a new window. Do not save the key in this repository.

### macOS or Linux

```bash
python3.11 -m venv .venv
./.venv/bin/python -m pip install --upgrade pip
./.venv/bin/python -m pip install .
export OPENAI_API_KEY="paste-your-key-here"
./.venv/bin/hotel-pl-normalizer --doctor
```

`--doctor` reports whether Python, dependencies, bundled assets, and the API-key setting are ready. It never displays the key.

## Run

Map the recommended validated period:

```powershell
.\.venv\Scripts\hotel-pl-normalizer.exe "C:\P&Ls\Hotel.xlsx" "C:\P&Ls\Hotel-normalized"
```

Map current and prior annual Actual periods when both are available:

```powershell
.\.venv\Scripts\hotel-pl-normalizer.exe "C:\P&Ls\Hotel.xlsx" "C:\P&Ls\Hotel-normalized" --actual-and-prior
```

Map the first two validated full-year, YTD, or TTM periods:

```powershell
.\.venv\Scripts\hotel-pl-normalizer.exe "C:\P&Ls\Hotel.xlsx" "C:\P&Ls\Hotel-normalized" --annual-periods 2
```

The output directory will contain the deliverables plus a `work/` directory of diagnostic artifacts. A successful run has `"accepted": true` in `summary.json`.

## Update

```powershell
git pull
.\.venv\Scripts\python.exe -m pip install --upgrade .
```

## Troubleshooting

- `OPENAI_API_KEY is not configured`: set it in the same terminal used to run the CLI.
- `Python 3.11+ is required`: install a current Python from [python.org](https://www.python.org/downloads/).
- A period is missing: review `summary.json` for `dropped_periods`; the CLI does not silently substitute Budget, Forecast, or a partial period.
- A run stops or fails validation: keep the source workbook, `summary.json`, `run_log.json`, and `work/` directory together for diagnosis.
