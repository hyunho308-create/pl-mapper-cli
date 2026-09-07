"""Run one workbook through the production pipeline from the command line."""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
from collections.abc import Callable
from importlib.metadata import PackageNotFoundError, version
from importlib.resources import files
from pathlib import Path

from hotel_pl_normalizer.output import mapped_output_name, write_normalized_workbook
from hotel_pl_normalizer.pipeline import (
    analyze_workbook_structure,
    discover_pdf_periods,
    discover_workbook_periods,
    normalize_pdf,
    normalize_workbook,
    shared_pdf_document,
    shared_workbook,
    validated_pdf_period_ids,
    validated_period_ids,
)
from hotel_pl_normalizer.run_log import write_run_log


def _catalog(run) -> dict:
    stage = next(
        item for item in run.stages if item.stage_name == "period_discovery"
    )
    return json.loads(
        Path(stage.artifact_paths["catalog"]).read_text(encoding="utf-8")
    )


SUPPORTED_INPUT_SUFFIXES = {".xlsx", ".xlsm", ".xls", ".pdf"}


def _display_period_detail(value: object) -> str:
    text = str(value).replace("_", " ")
    return text.upper() if text.lower() in {"ytd", "ttm"} else text.title()


def _version() -> str:
    try:
        return version("hotel-pl-normalizer")
    except PackageNotFoundError:
        return "source"


def _doctor() -> int:
    """Check the local runtime without reading or displaying the API key."""
    checks = {
        "Python 3.11+": sys.version_info >= (3, 11),
        "OPENAI_API_KEY configured": bool(os.environ.get("OPENAI_API_KEY")),
        "openai installed": importlib.util.find_spec("openai") is not None,
        "openpyxl installed": importlib.util.find_spec("openpyxl") is not None,
        "pydantic installed": importlib.util.find_spec("pydantic") is not None,
        "xlrd installed": importlib.util.find_spec("xlrd") is not None,
        "pdfplumber installed": importlib.util.find_spec("pdfplumber") is not None,
        "pypdf installed": importlib.util.find_spec("pypdf") is not None,
        "Standard COA bundled": files("hotel_pl_normalizer.data").joinpath("coa_v2.csv").is_file(),
        "Output template bundled": files("hotel_pl_normalizer.data").joinpath("output_template.xlsx").is_file(),
    }
    print(f"Hotel P&L Mapper {_version()}")
    for label, passed in checks.items():
        print(f"[{'OK' if passed else 'MISSING'}] {label}")
    return 0 if all(checks.values()) else 1


def _validate_period_ids(
    catalog: dict,
    valid_ids: set[str],
    requested_ids: list[str],
) -> list[str]:
    """Validate explicit period IDs supplied in place of a human selection."""
    available = [item["period_id"] for item in catalog["options"]]
    unknown = [item for item in requested_ids if item not in available]
    invalid = [
        item for item in requested_ids if item in available and item not in valid_ids
    ]
    if unknown:
        raise ValueError("Unknown period id(s): " + ", ".join(unknown))
    if invalid:
        raise ValueError("Period id(s) failed validation: " + ", ".join(invalid))
    return list(dict.fromkeys(requested_ids))


def _prompt_for_period_ids(
    catalog: dict,
    valid_ids: set[str],
    *,
    read: Callable[[str], str] = input,
) -> list[str]:
    """Show validated periods and wait for a numbered user selection."""
    options = [
        item for item in catalog["options"] if item["period_id"] in valid_ids
    ]
    if not options:
        raise RuntimeError("No discovered period passed validation.")

    print("\nAvailable validated periods:", flush=True)
    for index, item in enumerate(options, start=1):
        details = " / ".join(
            _display_period_detail(value)
            for value in (
                item.get("scenario"),
                item.get("start_month"),
                item.get("end_month"),
            )
            if value
        )
        suffix = f" — {details}" if details else ""
        print(f"  {index}. {item['label']}{suffix}", flush=True)

    prompt = "Select period number(s), separated by commas [q to cancel]: "
    while True:
        try:
            response = read(prompt).strip()
        except EOFError as exc:
            raise SystemExit(
                "Period selection requires an interactive terminal. Re-run with "
                "one or more explicit --period-id values."
            ) from exc
        if not response:
            print("Choose at least one period, or enter q to cancel.")
            continue
        elif response.lower() in {"q", "quit", "cancel"}:
            raise SystemExit("Period selection cancelled.")
        else:
            try:
                indexes = [int(part.strip()) for part in response.split(",")]
            except ValueError:
                print("Enter one or more numbers from the list, such as 1 or 1,2.")
                continue
        if not indexes or any(
            index < 1 or index > len(options) for index in indexes
        ):
            print(f"Choose number(s) between 1 and {len(options)}.")
            continue
        return list(
            dict.fromkeys(options[index - 1]["period_id"] for index in indexes)
        )


def _result_summary(result, selected_ids: list[str]) -> dict:
    """Build the shared success/failure summary for a completed mapping."""
    return {
        "accepted": result.accepted,
        "outcome": result.outcome,
        "stopped_reason": result.stopped_reason,
        "exceptions": result.exceptions,
        "accounts_mapped": result.mapped_account_count,
        "cost_usd": result.cost_usd,
        "duration_ms": result.duration_ms,
        "mapping_model": result.mapping_model,
        "mapping_provider": result.mapping_provider,
        "requested_period_ids": selected_ids,
        "mapped_period_ids": sorted(result.period_labels),
        "selected_period_labels": result.period_labels,
        "dropped_periods": result.dropped_periods,
        "session_calls": result.session_calls,
        "session_exhausted": result.session_exhausted,
        "feedback_findings": int(
            result.feedback_manifest.get("rendered_count", 0)
        ),
        "feedback_mode": result.feedback_manifest.get("mode", "canonical"),
    }


def _write_summary(path: Path, summary: dict) -> None:
    path.write_text(json.dumps(summary, indent=2), encoding="utf-8")


def _write_result_artifacts(
    result,
    *,
    output_dir: Path,
    source_path: Path,
    selected_ids: list[str],
    is_pdf: bool,
    work_dir: Path,
) -> dict:
    """Checkpoint paid mapping state before rendering the workbook."""
    run_log_path = output_dir / "run_log.json"
    summary_path = output_dir / "summary.json"
    summary = _result_summary(result, selected_ids)
    if is_pdf:
        summary["source_format"] = "pdf"
        summary["pdf_structure_dir"] = str(work_dir / "pdf_structure")

    try:
        write_run_log(result, run_log_path)
    except Exception as exc:
        summary.update(
            {
                "accepted": False,
                "outcome": "artifact_failure",
                "mapping_outcome": result.outcome,
                "delivery_status": "failed",
                "failure_stage": "result_checkpoint",
                "failure": f"{type(exc).__name__}: {exc}",
                "mapping_result_saved": False,
            }
        )
        _write_summary(summary_path, summary)
        raise

    # Feedback fallback may have been established while building the run log.
    summary.update(_result_summary(result, selected_ids))
    try:
        write_normalized_workbook(
            result,
            output_dir / mapped_output_name(source_path.name),
        )
    except Exception as exc:
        summary.update(
            {
                "accepted": False,
                "outcome": "artifact_failure",
                "mapping_outcome": result.outcome,
                "delivery_status": "failed",
                "failure_stage": "workbook_output",
                "failure": f"{type(exc).__name__}: {exc}",
                "mapping_result_saved": True,
                "run_log_path": str(run_log_path),
            }
        )
        _write_summary(summary_path, summary)
        raise

    summary["delivery_status"] = "complete"
    summary["mapping_result_saved"] = True
    summary["run_log_path"] = str(run_log_path)
    _write_summary(summary_path, summary)
    return summary


def _write_unhandled_failure_summary(
    output_dir: Path,
    source_path: Path,
    exc: Exception,
) -> None:
    """Guarantee a user-facing failure summary for pipeline-stage exceptions."""
    diagnostic = f"{type(exc).__name__}: {exc}"
    summary_path = output_dir / "summary.json"
    existing: dict = {}
    if summary_path.is_file():
        try:
            candidate = json.loads(summary_path.read_text(encoding="utf-8"))
            if candidate.get("failure") == diagnostic:
                existing = candidate
        except (OSError, ValueError, TypeError):
            pass
    existing.update(
        {
            "accepted": False,
            "outcome": existing.get("outcome", "stage_failure"),
            "stopped_reason": diagnostic,
            "failure": diagnostic,
            "failure_stage": existing.get("failure_stage", "pipeline"),
            "delivery_status": "failed",
            "mapping_result_saved": bool(
                existing.get("mapping_result_saved", False)
            ),
            "source_name": source_path.name,
        }
    )
    failure_artifact = output_dir / "work" / "failure.json"
    if failure_artifact.is_file():
        existing["stage_failure_path"] = str(failure_artifact)
    _write_summary(summary_path, existing)


def _run_cli_workflow(args) -> dict:
    """Execute the paid workflow after argument validation."""
    args.output_dir.mkdir(parents=True, exist_ok=True)
    work_dir = args.output_dir / "work"
    source_path = args.workbook
    is_pdf = source_path.suffix.lower() == ".pdf"
    if is_pdf:
        parsed_pdf = shared_pdf_document(source_path)
        discovery = discover_pdf_periods(
            source_path,
            output_dir=work_dir / "discovery",
            progress=lambda message: print(message, flush=True),
            parsed=parsed_pdf,
        )
        catalog = {
            "options": [
                period.model_dump(mode="json")
                for period in discovery.exploration.periods
            ]
        }
        valid_ids = validated_pdf_period_ids(discovery)
        selected_ids = (
            _validate_period_ids(catalog, valid_ids, args.period_id)
            if args.period_id
            else _prompt_for_period_ids(catalog, valid_ids)
        )
        result = normalize_pdf(
            source_path,
            output_dir=work_dir,
            selected_period_ids=selected_ids,
            source_name=source_path.name,
            progress=lambda message: print(message, flush=True),
            on_activity=lambda message: print(f"  · {message}", flush=True),
            discovery=discovery,
            parsed=parsed_pdf,
        )
    else:
        parsed = shared_workbook(source_path)
        discovery = discover_workbook_periods(
            source_path,
            output_dir=work_dir / "discovery",
            progress=lambda message: print(message, flush=True),
            parsed=parsed,
        )
        catalog = _catalog(discovery)
        valid_ids = validated_period_ids(discovery)
        selected_ids = (
            _validate_period_ids(catalog, valid_ids, args.period_id)
            if args.period_id
            else _prompt_for_period_ids(catalog, valid_ids)
        )
        labels = {
            item["period_id"]: item["label"] for item in catalog["options"]
        }
        print(
            "Selected period(s): "
            + ", ".join(f"{labels[item]} [{item}]" for item in selected_ids),
            flush=True,
        )
        structure = analyze_workbook_structure(
            source_path,
            output_dir=work_dir / "upstream",
            discovery_run=discovery,
            selected_period_ids=selected_ids,
            progress=lambda message: print(message, flush=True),
            parsed=parsed,
        )
        result = normalize_workbook(
            source_path,
            output_dir=work_dir,
            prior_run=structure,
            selected_period_ids=selected_ids,
            source_name=source_path.name,
            progress=lambda message: print(message, flush=True),
            on_activity=lambda message: print(f"  · {message}", flush=True),
            parsed=parsed,
        )
    return _write_result_artifacts(
        result,
        output_dir=args.output_dir,
        source_path=source_path,
        selected_ids=selected_ids,
        is_pdf=is_pdf,
        work_dir=work_dir,
    )


def main() -> None:
    # The progress feed carries typographic characters, and a Windows console
    # defaults to cp1252, which turns them into replacement marks. Nothing is
    # wrong with the messages -- the stream just has to be told they are UTF-8.
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(
        prog="hotel-pl-normalizer",
        description="Map one hotel P&L workbook or PDF into the bundled Standard COA.",
    )
    parser.add_argument("workbook", type=Path, nargs="?")
    parser.add_argument("output_dir", type=Path, nargs="?")
    parser.add_argument("--doctor", action="store_true", help="Check setup and exit without making an API call.")
    parser.add_argument("--version", action="version", version=f"%(prog)s {_version()}")
    parser.add_argument(
        "--period-id",
        action="append",
        default=[],
        help=(
            "Period id to map; repeat to map multiple periods and skip the "
            "interactive prompt."
        ),
    )
    args = parser.parse_args()

    if args.doctor:
        raise SystemExit(_doctor())
    if args.workbook is None or args.output_dir is None:
        parser.error("workbook and output_dir are required unless --doctor is used")
    if not os.environ.get("OPENAI_API_KEY"):
        parser.error("OPENAI_API_KEY is not configured in this terminal")
    args.workbook = args.workbook.expanduser().resolve()
    args.output_dir = args.output_dir.expanduser().resolve()
    if not args.workbook.is_file():
        parser.error(f"workbook does not exist: {args.workbook}")
    if args.workbook.suffix.lower() not in SUPPORTED_INPUT_SUFFIXES:
        parser.error("input must be an .xlsx, .xlsm, .xls, or .pdf file")

    try:
        summary = _run_cli_workflow(args)
    except Exception as exc:
        try:
            _write_unhandled_failure_summary(args.output_dir, args.workbook, exc)
        except OSError:
            # Never replace the useful root exception with a secondary disk
            # error while trying to report it.
            pass
        raise
    print(json.dumps(summary), flush=True)


if __name__ == "__main__":
    main()
