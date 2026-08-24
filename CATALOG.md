# Hotel P&L Normalizer Catalog

This is a short human-readable guide to the main parts of the repository. The code and bundled COA remain authoritative.

## Workflow

| Stage | What happens |
| --- | --- |
| Workbook ingestion | The source workbook is parsed once and held in memory during period selection. |
| Exploration and period discovery | The CLI identifies relevant sheets, workbook structure, and validated reporting periods. |
| User period selection | The CLI pauses and waits for one or more periods; it does not guess past this checkpoint. |
| Period binding | Selected periods are tied to their exact source sheets and columns. |
| COA mapping | The model assigns supported source rows to Standard COA accounts. |
| Validation | Python checks values, rollups, coverage, reconciliation, and mapping consistency. |
| Output | A new mapped workbook, summary, audit log, and diagnostic artifacts are written atomically. |

## Main repository areas

| Area | Purpose |
| --- | --- |
| `hotel_pl_normalizer/cli.py` | The command-line entry point, period-selection pause, and final output handoff. |
| `hotel_pl_normalizer/pipeline.py` | Coordinates the single end-to-end workflow. |
| `hotel_pl_normalizer/structure/` | Reads and explores workbooks, builds the compact representation, and binds periods. |
| `hotel_pl_normalizer/mapping/` | Runs mapping, evaluates evidence, validates results, and determines the final outcome. |
| `hotel_pl_normalizer/models/` | Defines the structured data passed between workflow stages. |
| `hotel_pl_normalizer/prompts/` | Instructions used for exploration, period detection and binding, mapping, and validation feedback. |
| `hotel_pl_normalizer/providers/` | Supplier-neutral model contract and the current OpenAI adapter. |
| `hotel_pl_normalizer/data/` | The Standard COA and authored output workbook template. |
| `tests/` | Fast regression tests for important workflow, COA, representation, and mapping behavior. |

## Bundled business assets

| Asset | Purpose |
| --- | --- |
| `coa_v2.csv` | The 271-account Standard COA, including mapping notes and synonyms. |
| `output_template.xlsx` | The controlled workbook template used for mapped output. |
| Prompt files | General hotel-accounting and workflow guidance for each model stage. |

## User-facing period choices

| Choice | Use |
| --- | --- |
| Interactive selection | Default. Codex relays the available periods and waits for the user's response. |
| Recommended | Maps the validated recommended period without pausing. |
| Actual and prior | Selects a validated annual Actual and matching Prior Actual when available. |
| Annual periods | Selects the first requested number of validated full-year, YTD, or TTM periods. |
| Exact period ID | Maps explicitly named validated periods for repeatable or automated runs. |

## Run outputs

| Output | Review it for |
| --- | --- |
| `[MAPPED].xlsx` | Final mapped values, account labels, comments, and review flags. |
| `summary.json` | Acceptance, outcome, selected and dropped periods, cost, duration, and model calls. |
| `run_log.json` | Source-row lineage and mapping audit. |
| `work/` | Period discovery, workbook structure, binding, model traces, and validator details. |
