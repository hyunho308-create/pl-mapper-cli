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
    shared_workbook,
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

    args.output_dir.mkdir(parents=True, exist_ok=True)
    work_dir = args.output_dir / "work"
    source_path = args.workbook
    is_pdf = source_path.suffix.lower() == ".pdf"
    if is_pdf:
        discovery = discover_pdf_periods(
            source_path,
            output_dir=work_dir / "discovery",
            progress=lambda message: print(message, flush=True),
        )
        catalog = {
            "options": [
                period.model_dump(mode="json")
                for period in discovery.exploration.periods
            ]
        }
        valid_ids = {item["period_id"] for item in catalog["options"]}
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
        )
    else:
        # Discovery, structure analysis and evidence extraction all borrow this
        # same parsed record. Mapping releases it once compact evidence exists.
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
    write_normalized_workbook(
        result, args.output_dir / mapped_output_name(source_path.name)
    )
    write_run_log(result, args.output_dir / "run_log.json")
    summary = {
        "accepted": result.accepted,
        "outcome": result.outcome,
        "stopped_reason": result.stopped_reason,
        "exceptions": result.exceptions,
        "accounts_mapped": result.mapped_account_count,
        "cost_usd": result.cost_usd,
        "duration_ms": result.duration_ms,
        "mapping_model": result.mapping_model,
        "mapping_provider": result.mapping_provider,
        # What was asked for, and what came back. These diverge now that a
        # period binding cannot refuse is dropped rather than failing the run:
        # reporting the request as though it were the outcome told one rerun it
        # had mapped a Budget period whose column is empty.
        "requested_period_ids": selected_ids,
        "mapped_period_ids": sorted(result.period_labels),
        "selected_period_labels": result.period_labels,
        "dropped_periods": result.dropped_periods,
        "session_calls": result.session_calls,
        "session_exhausted": result.session_exhausted,
        "feedback_findings": int(
            result.feedback_manifest.get("rendered_count", 0)
        ),
    }
    if is_pdf:
        summary["source_format"] = "pdf"
        summary["pdf_structure_dir"] = str(work_dir / "pdf_structure")
    (args.output_dir / "summary.json").write_text(
        json.dumps(summary, indent=2), encoding="utf-8"
    )
    print(json.dumps(summary), flush=True)


if __name__ == "__main__":
    main()
