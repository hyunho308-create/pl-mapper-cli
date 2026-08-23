"""Run and compare the fixed department-binding A/B regression set."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path


FORMAT_ROOT = Path(r"C:\Users\Hyun.Lee\dev\hotel_pl_normalizer-v2\P&L Formats")
BASELINE_ROOT = Path(
    r"C:\Users\Hyun.Lee\dev\hotel_pl_normalizer-v2\dist\pl-mapper-cli-ab-baseline"
)
STREAMLINED_ROOT = Path(__file__).resolve().parent
CASES = {
    "kabuki_single": "1.10 Hotel Kabuki - 2025 Detailed.xlsx",
    "wapc_mixed": "WAPC P&L 2025.xlsm",
    "cmi_conventional": "CMI 2025 Actuals YTD.xlsx",
    "marriott_coded": "Marriot Long Beach YE 2025 P&L.xlsm",
    "gry_multi_outlet": "GRY 2025 12M.xlsx",
}


def _load_api_key() -> bool:
    """Load only OPENAI_API_KEY from the parent repository's ignored .env."""
    if os.environ.get("OPENAI_API_KEY"):
        return True
    dotenv = STREAMLINED_ROOT.parents[1] / ".env"
    if not dotenv.is_file():
        return False
    for raw_line in dotenv.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        name, separator, raw_value = line.partition("=")
        if separator != "=" or name.strip() != "OPENAI_API_KEY":
            continue
        value = raw_value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        if value:
            os.environ["OPENAI_API_KEY"] = value
            return True
    return False


def _run_one(workflow: str, source_root: Path, case: str, output_root: Path) -> None:
    workbook = FORMAT_ROOT / CASES[case]
    output = output_root / workflow / case
    summary = output / "summary.json"
    if summary.is_file():
        print(f"[{workflow}/{case}] already complete; skipping", flush=True)
        return
    output.mkdir(parents=True, exist_ok=True)
    command = [
        sys.executable,
        "-m",
        "hotel_pl_normalizer.cli",
        str(workbook),
        str(output),
    ]
    command.extend(
        ["--recommended"]
        if case == "cmi_conventional"
        else ["--annual-periods", "1"]
    )
    print(f"[{workflow}/{case}] starting", flush=True)
    with (output / "console.log").open("w", encoding="utf-8") as log:
        process = subprocess.Popen(
            command,
            cwd=source_root,
            env=os.environ.copy(),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        assert process.stdout is not None
        for line in process.stdout:
            log.write(line)
            log.flush()
            print(f"[{workflow}/{case}] {line.rstrip()}", flush=True)
        code = process.wait()
    if code:
        raise RuntimeError(f"{workflow}/{case} exited with status {code}")


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _period_values(log: dict) -> tuple[str, dict]:
    labels = {
        item["period_id"]: item["label"] for item in log["source"].get("periods", [])
    }
    values = log.get("values_by_period") or {"selected": log.get("values", {})}
    period_id = next(iter(values))
    return labels.get(period_id, log["source"].get("period", period_id)), values[period_id]


def _period_signature(output_root: Path, workflow: str, case: str, summary: dict) -> dict:
    catalog_path = (
        output_root
        / workflow
        / case
        / "work"
        / "discovery"
        / "stages"
        / "period_discovery"
        / "catalog.json"
    )
    catalog = _load(catalog_path)
    mapped = set(summary.get("mapped_period_ids") or [])
    option = next(item for item in catalog["options"] if item["period_id"] in mapped)
    return {
        key: option.get(key)
        for key in ("scenario", "period_type", "start_period", "end_period")
    }


def _case_comparison(case: str, output_root: Path) -> dict:
    summaries = {
        name: _load(output_root / name / case / "summary.json")
        for name in ("baseline", "streamlined")
    }
    logs = {
        name: _load(output_root / name / case / "run_log.json")
        for name in ("baseline", "streamlined")
    }
    baseline_label, baseline_values = _period_values(logs["baseline"])
    streamlined_label, streamlined_values = _period_values(logs["streamlined"])
    signatures = {
        workflow: _period_signature(output_root, workflow, case, summaries[workflow])
        for workflow in ("baseline", "streamlined")
    }
    comparable = signatures["baseline"] == signatures["streamlined"]
    differences = []
    if comparable:
        for coa_id in sorted(set(baseline_values) | set(streamlined_values)):
            left = baseline_values.get(coa_id)
            right = streamlined_values.get(coa_id)
            if left is None and right is None:
                continue
            delta = float(right or 0) - float(left or 0)
            if abs(delta) > 0.01:
                differences.append({"coa_id": coa_id, "baseline": left, "streamlined": right, "delta": delta})
    citations = {}
    for workflow, log in logs.items():
        citations[workflow] = {
            item["coa_id"]: sorted(row["row_key"] for row in item.get("source_rows", []))
            for item in log.get("accounts", [])
        }
    changed_citations = sum(
        citations["baseline"].get(coa_id, [])
        != citations["streamlined"].get(coa_id, [])
        for coa_id in set(citations["baseline"]) | set(citations["streamlined"])
    )
    return {
        "case": case,
        "workbook": CASES[case],
        "baseline_period": baseline_label,
        "streamlined_period": streamlined_label,
        "baseline_period_signature": signatures["baseline"],
        "streamlined_period_signature": signatures["streamlined"],
        "periods_comparable": comparable,
        "baseline": summaries["baseline"],
        "streamlined": summaries["streamlined"],
        "value_difference_count": len(differences) if comparable else None,
        "absolute_value_difference": (
            round(sum(abs(item["delta"]) for item in differences), 2)
            if comparable
            else None
        ),
        "changed_source_citation_accounts": changed_citations,
        "largest_value_differences": sorted(
            differences, key=lambda item: abs(item["delta"]), reverse=True
        )[:20],
        "baseline_validation": logs["baseline"].get("outcome", {}),
        "streamlined_validation": logs["streamlined"].get("outcome", {}),
        "baseline_tokens": logs["baseline"].get("tokens", {}),
        "streamlined_tokens": logs["streamlined"].get("tokens", {}),
    }


def _write_report(output_root: Path) -> None:
    comparisons = [_case_comparison(case, output_root) for case in CASES]
    (output_root / "comparison.json").write_text(
        json.dumps(comparisons, indent=2), encoding="utf-8"
    )
    lines = [
        "# Department Binding A/B Results",
        "",
        "| Case | Period match | Accepted B/S | Accounts B/S | Cost B/S | Duration B/S | Value differences | Citation changes |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for item in comparisons:
        baseline = item["baseline"]
        streamlined = item["streamlined"]
        lines.append(
            "| {case} | {period} | {ba}/{sa} | {bm}/{sm} | ${bc:.4f}/${sc:.4f} | {bd:.1f}m/{sd:.1f}m | {diff} | {citations} |".format(
                case=item["case"],
                period="yes" if item["periods_comparable"] else "NO",
                ba=baseline.get("accepted"),
                sa=streamlined.get("accepted"),
                bm=baseline.get("accounts_mapped"),
                sm=streamlined.get("accounts_mapped"),
                bc=float(baseline.get("cost_usd") or 0),
                sc=float(streamlined.get("cost_usd") or 0),
                bd=float(baseline.get("duration_ms") or 0) / 60000,
                sd=float(streamlined.get("duration_ms") or 0) / 60000,
                diff=item["value_difference_count"],
                citations=item["changed_source_citation_accounts"],
            )
        )
    (output_root / "comparison.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"Wrote {output_root / 'comparison.md'}", flush=True)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-root", type=Path, default=STREAMLINED_ROOT / ".ab-results")
    parser.add_argument("--workflow", choices=("baseline", "streamlined", "both"), default="both")
    parser.add_argument("--case", action="append", choices=tuple(CASES))
    parser.add_argument("--compare-only", action="store_true")
    args = parser.parse_args()
    output_root = args.output_root.resolve()
    if not args.compare_only:
        if not _load_api_key():
            parser.error("OPENAI_API_KEY is not configured; no billable runs were started")
        workflows = (
            (("baseline", BASELINE_ROOT), ("streamlined", STREAMLINED_ROOT))
            if args.workflow == "both"
            else ((args.workflow, BASELINE_ROOT if args.workflow == "baseline" else STREAMLINED_ROOT),)
        )
        for case in args.case or CASES:
            for workflow, source_root in workflows:
                _run_one(workflow, source_root, case, output_root)
    if all(
        (output_root / workflow / case / "summary.json").is_file()
        for workflow in ("baseline", "streamlined")
        for case in CASES
    ):
        _write_report(output_root)
    else:
        print("Comparison waits until all ten summaries exist.", flush=True)


if __name__ == "__main__":
    main()
