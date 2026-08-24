# Getting Started with Codex

## What you are setting up

The Hotel P&L Normalizer turns an operator P&L into a separate Standard COA workbook. You will talk to Codex in ordinary language. Codex handles the setup commands, starts the CLI, relays the available periods, waits for your choice, and shows you the completed files.

The original workbook is never overwritten.

## 1. Download the project and install the basic tools

You need:

- The [Codex desktop app for Windows](https://learn.chatgpt.com/docs/windows/windows-app), installed and signed in.
- [Python 3.11 or newer](https://www.python.org/downloads/). In the Windows installer, leave the option to add Python to your path enabled.
- An OpenAI API account with billing enabled and access to `gpt-5.6-luna`.

To download the normalizer without using Git:

1. Open the [Hotel P&L Normalizer repository](https://github.com/hyunho308-create/pl-mapper-cli).
2. Select the green **Code** button, then **Download ZIP**.
3. Open the downloaded ZIP and extract the project into a permanent local folder, for example:

```text
Desktop\Hotel P&L Normalizer
```

Do not run the project from inside the ZIP file or leave it in the Downloads folder. A local Desktop or Documents folder is preferable to a network drive or an online-only folder.

## 2. Add your OpenAI API key safely

Create a key at the [OpenAI API key page](https://platform.openai.com/api-keys). The API key pays for the model calls made by this normalizer; it is separate from signing in to Codex.

Do not paste the key into a Codex chat and do not save it in this project folder. On Windows:

1. Open the Start menu and search for **Edit environment variables for your account**.
2. Under **User variables**, select **New**.
3. Enter `OPENAI_API_KEY` as the variable name.
4. Paste the API key as the variable value and select **OK** on each open window.
5. Completely close Codex and reopen it so the new setting is available.

This follows the [official OpenAI API setup](https://developers.openai.com/api/docs/quickstart), which uses `OPENAI_API_KEY` as a system environment variable. The normalizer checks that the variable exists but never displays its value.

## 3. Open the project in Codex

In Codex, choose **Add project** and select the unzipped `Hotel P&L Normalizer` folder.

Start a new task in that project and paste:

```text
Read GETTING_STARTED.md and set up this Hotel P&L Normalizer for me. Confirm Python 3.11 or newer is available, create a local .venv, install this package and its dependencies, and run the CLI doctor check. Do not ask me to paste an API key into chat and do not write any key into this project. Explain any problem in simple terms and finish all safe setup steps you can.
```

Codex may ask permission to install the listed Python packages. Approve those setup actions. You normally need to do this only once per computer or unzipped copy.

The setup is ready when the doctor check shows `[OK]` for every item. The doctor check does not make a paid model call.

## 4. Add a P&L workbook

Copy the operator workbook into the project folder. You may create a folder such as `P&Ls` to keep inputs organized. Supported files are `.xlsx`, `.xlsm`, and `.xls`.

Keep the original workbook unchanged. The CLI writes results to a separate output folder that Codex can create for you.

## 5. Ask Codex to normalize it

Start with this prompt and replace the file name:

```text
Normalize the hotel P&L workbook named Hotel.xlsx using the CLI in this project. Run it interactively in a persistent session. When the CLI discovers the available periods, show them to me in plain English and wait for my choice. Do not choose or restart the run while waiting. After I respond, continue the same run and save the results in a new output folder next to the workbook. Do not change the source workbook.
```

Codex will perform workbook exploration and then return with a numbered list similar to:

```text
1. 2025 Actual — Full Year (recommended)
2. 2024 Actual — Full Year
3. 2026 Budget — Full Year
```

Reply naturally, for example:

```text
Map 1 and 2.
```

Codex will pass that selection to the waiting CLI and continue the same run. A typical run takes 10–20 minutes and makes paid OpenAI API calls.

## 6. Review the result

When the run finishes, ask:

```text
Review the normalizer output. Tell me whether the run was accepted, which periods were actually mapped or dropped, what warnings need human review, and link me to the mapped workbook, summary.json, and run_log.json. Do not change the mapping.
```

The output folder contains:

- `[source name] [MAPPED].xlsx`: the Standard COA workbook.
- `summary.json`: the overall outcome, periods, cost, duration, and model calls.
- `run_log.json`: the source-row audit trail.
- `work/`: detailed diagnostic and model artifacts.

Keep these files together if you need help reviewing a run.

## Important practical rules

- Workbook-derived financial data is sent to the OpenAI API during analysis. Use the normalizer only for files you are authorized to process this way.
- Use one output folder per source workbook or run so results are easy to audit.
- Do not delete `summary.json`, `run_log.json`, or `work/` before a run has been reviewed.
- A warning or human-review flag does not automatically mean the source values should be changed. The operator P&L may contain a real discrepancy that should remain visible.
- The CLI will not silently replace a missing Actual period with Budget, Forecast, or a partial period.

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

## Getting a newer version

Return to the [GitHub repository](https://github.com/hyunho308-create/pl-mapper-cli), download a fresh ZIP from the green **Code** menu, and unzip it into a new folder. Open that folder as a Codex project and repeat the setup prompt in step 3.

Keep any source workbooks and completed output folders that you still need. Do not delete an older project copy until its runs have been reviewed and the files you need have been moved to the new copy.
