# Hotel P&L Mapper CLI

Map an operator hotel P&L workbook or born-digital PDF into a bundled 271-account Standard COA while preserving a source-row audit trail.

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
- A complete hotel P&L workbook (`.xlsx`, `.xlsm`, or `.xls`) or a born-digital `.pdf` with a usable text layer
- Dependencies: `openai>=1.0`, `openpyxl>=3.1`, `pdfplumber>=0.11`, `pypdf>=6.0`, `pydantic>=2.0`, `xlrd>=2.0`

PDFs are inspected directly from positioned text and numeric anchors; no intermediate Excel workbook is created. PDF routing, period discovery, and amount-column binding write auditable artifacts under `work/pdf_structure`, then hand compact source rows to the same mapper used by Excel. Scanned/image-only PDFs fail closed instead of being guessed or silently OCRed. The Excel ingestion and structure workflow is unchanged.
