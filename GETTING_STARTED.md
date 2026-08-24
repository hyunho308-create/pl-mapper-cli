# Getting Started with Codex

## 1. Download the project and install the basic tools

You need:

- The [Codex desktop app for Windows](https://learn.chatgpt.com/docs/windows/windows-app), installed and signed in.
- [Python 3.11 or newer](https://www.python.org/downloads/).
- An OpenAI API account with billing enabled and access to `gpt-5.6-luna`.

To download the mapper:

1. Open the [Hotel P&L Mapper repository](https://github.com/hyunho308-create/pl-mapper-cli).
2. Select the green **Code** button, then **Download ZIP**.
3. Open the downloaded ZIP and extract the project into a permanent local folder, for example:

```text
Desktop\Hotel P&L Mapper
```

Do not run the project from inside the ZIP file or leave it in the Downloads folder. A local Desktop or Documents folder is preferable to a network drive or an online-only folder.

During setup, Codex installs these Python packages automatically:

- `openai`: connects the mapper to the OpenAI API and Luna model.
- `openpyxl`: reads `.xlsx` and `.xlsm` workbooks and writes the mapped workbook.
- `pydantic`: checks the structured information passed between workflow stages.
- `xlrd`: reads older `.xls` workbooks.

You do not need to find or install these packages separately. They are listed in the project configuration and are installed together when Codex installs the mapper.

## 2. Add your OpenAI API key safely

Create a key at the [OpenAI API key page](https://platform.openai.com/api-keys). The API key pays for the model calls made by this mapper; it is separate from signing in to Codex.

> **Note:** The free tier and Tier 1 are subject to 60K and 200K tokens per minute limits, respectively, for the Luna API. Full mapping runs with prior calls appended for context will often exceed this cap. Tier 2 allows up to 2MM tokens per minute and can be activating by loading >$50 into the API to start. Mapping typically costs around $0.10/P&L so $50 should be plenty to start.

Do not paste the key into a Codex chat and do not save it in this project folder. On Windows:

1. Open the Start menu and search for **Edit environment variables for your account**.
2. Under **User variables**, select **New**.
3. Enter `OPENAI_API_KEY` as the variable name.
4. Paste the API key as the variable value and select **OK** on each open window.
5. Completely close Codex and reopen it so the new setting is available.

This follows the [official OpenAI API setup](https://developers.openai.com/api/docs/quickstart), which uses `OPENAI_API_KEY` as a system environment variable. The mapper checks that the variable exists but never displays its value.

## 3. Open the project in Codex

In Codex, choose **Add project** and select the unzipped `Hotel P&L Mapper` folder as a local project.

Start a new task in that project and paste:

```text
Read GETTING_STARTED.md and set up this Hotel P&L Mapper for me. Confirm Python 3.11 or newer is available, create a local .venv, install this package and its dependencies, and run the CLI doctor check. Do not ask me to paste an API key into chat and do not write any key into this project. Explain any problem in simple terms and finish all safe setup steps you can.
```

Codex may ask permission to install the listed Python packages. Approve those setup actions. You normally need to do this only once per computer.

The setup is ready when the doctor check shows `[OK]` for every item. The doctor check does not make a paid model call.

## 4. Add a P&L workbook

Copy the operator workbook into the project folder or attach directly into a codex chat. You may create a folder such as `P&Ls` to keep inputs organized in the project folder. Supported files are `.xlsx`, `.xlsm`, and `.xls`.

Keep the original workbook unchanged. The CLI writes results to a separate output folder that Codex can create for you.

## 5. Ask Codex to normalize it

Start with this prompt and replace the file name:

```text
Normalize the hotel P&L workbook named Hotel.xlsx using the CLI in this project. Run it interactively in a persistent session. When the CLI discovers the available periods, show them to me in plain English and wait for my choice. Do not choose or restart the run while waiting. After I respond, continue the same run and save the results in a new output folder next to the workbook. Do not change the source workbook.
```

Codex will perform workbook exploration and then return with a numbered list similar to:

```text
1. 2025 Actual — Full Year
2. 2024 Actual — Full Year
3. 2026 Budget — Full Year
```

Reply naturally, for example:

```text
Map 1 and 2 or map 2026 Budget.
```

Codex will pass that selection to the waiting CLI and continue the same run. A typical run takes 10–20 minutes and makes paid OpenAI API calls.

## 6. Review the result

When the run finishes, review the output excel `[source name] [MAPPED].xlsx`. You can also ask codex something like:

```text
Review the mapper output. Tell me whether the run was accepted, which periods were actually mapped or dropped, what warnings need human review, and link me to the mapped workbook, summary.json, and run_log.json. Do not change the mapping.
```

The output folder contains:

- `[source name] [MAPPED].xlsx`: the Standard COA workbook.
- `summary.json`: the overall outcome, periods, cost, duration, and model calls.
- `run_log.json`: the source-row audit trail.
- `work/`: detailed diagnostic and model artifacts.

## Important practical rules

- Do not delete `summary.json`, `run_log.json`, or `work/` before a run has been reviewed.
- A warning or human-review flag does not automatically mean the source values should be changed. The operator P&L may contain a real discrepancy that should remain visible.

## If something goes wrong

You can stay in Codex and ask it to diagnose the issue in plain language. Useful prompts include:

```text
Run the doctor check and fix only setup problems. Do not make a paid API call.
```

```text
Inspect summary.json and the work folder from the failed run. Explain the failure in simple terms and tell me whether it is safe to retry. Do not change the code.
```

```text
The period I expected is missing. Review the discovered and dropped periods and explain why. Do not substitute another period.
```
