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
    discover_workbook_periods,
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


# Full-year, year-to-date and trailing-twelve periods are the spans normally
# compared between properties; a single month is only useful in context.
ANNUAL_PERIOD_TYPES = {"full_year", "ytd", "ttm"}
SUPPORTED_WORKBOOK_SUFFIXES = {".xlsx", ".xlsm", ".xls"}


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
        "Standard COA bundled": files("hotel_pl_normalizer.data").joinpath("coa_v2.csv").is_file(),
        "Output template bundled": files("hotel_pl_normalizer.data").joinpath("output_template.xlsx").is_file(),
    }
    print(f"Hotel P&L Mapper {_version()}")
    for label, passed in checks.items():
        print(f"[{'OK' if passed else 'MISSING'}] {label}")
    return 0 if all(checks.values()) else 1


def _choose_period_ids(
    catalog: dict,
    valid_ids: set[str],
    requested_ids: list[str],
    annual_periods: int = 0,
    actual_and_prior: bool = False,
) -> list[str]:
    """Decide which discovered periods to map.

    Explicit `--period-id` wins and is checked rather than trusted -- naming a
    period that failed validation should say so, not quietly map something else.

    `--actual-and-prior` chooses a validated annual Actual and the matching
    Prior Actual from this discovery response. It uses semantic fields rather
    than model-generated period ids, which are intentionally not stable across
    separate discovery calls.

    `--annual-periods N` takes the first N validated annual periods in catalog
    order, with the recommendation first if it qualifies. Fewer than N is not an
    error: a workbook holding one annual column has one annual period, and
    refusing to map it would be unhelpful.

    With neither, the validated recommendation is mapped, falling back to the
    first validated period of any kind.
    """
    available = [item["period_id"] for item in catalog["options"]]
    if requested_ids:
        unknown = [item for item in requested_ids if item not in available]
        invalid = [item for item in requested_ids if item in available and item not in valid_ids]
        if unknown:
            raise ValueError("Unknown period id(s): " + ", ".join(unknown))
        if invalid:
            raise ValueError("Period id(s) failed validation: " + ", ".join(invalid))
        return list(dict.fromkeys(requested_ids))

    recommended = catalog.get("recommended_period_id")

    if actual_and_prior:
        annual_options = [
            item
            for item in catalog["options"]
            if item.get("period_type") in ANNUAL_PERIOD_TYPES
            and item["period_id"] in valid_ids
        ]
        actuals = [item for item in annual_options if item.get("scenario") == "actual"]
        prior_actuals = [
            item for item in annual_options if item.get("scenario") == "prior_actual"
        ]
        if not actuals and not prior_actuals:
            raise RuntimeError(
                "No discovered annual Actual or Prior Actual period passed "
                "validation. Available "
                "validated periods: " + (", ".join(sorted(valid_ids)) or "none")
            )
        candidates = actuals or prior_actuals
        primary = next(
            (item for item in candidates if item["period_id"] == recommended),
            candidates[0],
        )
        prior = next(
            (
                item
                for item in prior_actuals
                if item.get("scenario") == "prior_actual"
                and item.get("period_type") == primary.get("period_type")
                and item["period_id"] != primary["period_id"]
            ),
            None,
        )
        return [primary["period_id"]] + ([prior["period_id"]] if prior else [])

    if annual_periods > 0:
        annual = [
            item["period_id"]
            for item in catalog["options"]
            if item.get("period_type") in ANNUAL_PERIOD_TYPES
            and item["period_id"] in valid_ids
        ]
        if recommended in annual:
            annual = [recommended] + [pid for pid in annual if pid != recommended]
        if annual:
            return annual[:annual_periods]
        raise RuntimeError(
            "No discovered annual period passed validation. Available validated "
            "periods: " + (", ".join(sorted(valid_ids)) or "none")
        )

    if recommended in valid_ids:
        return [recommended]
    for period_id in available:
        if period_id in valid_ids:
            return [period_id]
    raise RuntimeError("No discovered period passed validation.")


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

    recommended = catalog.get("recommended_period_id")
    default_index = next(
        (
            index
            for index, item in enumerate(options, start=1)
            if item["period_id"] == recommended
        ),
        1,
    )
    print("\nAvailable validated periods:", flush=True)
    for index, item in enumerate(options, start=1):
        details = " / ".join(
            _display_period_detail(value)
            for value in (
                item.get("scenario"),
                item.get("period_type"),
                item.get("start_period"),
                item.get("end_period"),
            )
            if value
        )
        marker = " (recommended)" if index == default_index else ""
        suffix = f" — {details}" if details else ""
        print(f"  {index}. {item['label']}{suffix}{marker}", flush=True)

    prompt = (
        "Select period number(s), separated by commas "
        f"[default {default_index}; q to cancel]: "
    )
    while True:
        try:
            response = read(prompt).strip()
        except EOFError as exc:
            raise SystemExit(
                "Period selection requires an interactive terminal. Re-run with "
                "--actual-and-prior, --annual-periods N, or --period-id."
            ) from exc
        if not response:
            indexes = [default_index]
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
        description="Map one hotel P&L workbook into the bundled Standard COA.",
    )
    parser.add_argument("workbook", type=Path, nargs="?")
    parser.add_argument("output_dir", type=Path, nargs="?")
    parser.add_argument("--doctor", action="store_true", help="Check setup and exit without making an API call.")
    parser.add_argument("--version", action="version", version=f"%(prog)s {_version()}")
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument(
        "--period-id",
        action="append",
        default=[],
        help=(
            "Period id to map; repeat to map multiple periods and skip the "
            "interactive prompt."
        ),
    )
    selection.add_argument(
        "--annual-periods",
        type=int,
        default=0,
        metavar="N",
        help=(
            "Map the first N validated annual periods (full year, YTD or TTM) "
            "instead of the recommendation."
        ),
    )
    selection.add_argument(
        "--actual-and-prior",
        action="store_true",
        help=(
            "Map the first validated annual Actual and a matching Prior Actual "
            "when available."
        ),
    )
    selection.add_argument(
        "--recommended",
        action="store_true",
        help="Map the validated recommended period without prompting.",
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
    if args.workbook.suffix.lower() not in SUPPORTED_WORKBOOK_SUFFIXES:
        parser.error("workbook must be an .xlsx, .xlsm, or .xls file")

    args.output_dir.mkdir(parents=True, exist_ok=True)
    work_dir = args.output_dir / "work"
    # Discovery, structure analysis and evidence extraction all borrow this same
    # parsed record. Mapping releases it as soon as the compact evidence exists.
    parsed = shared_workbook(args.workbook)
    discovery = discover_workbook_periods(
        args.workbook,
        output_dir=work_dir / "discovery",
        progress=lambda message: print(message, flush=True),
        parsed=parsed,
    )
    catalog = _catalog(discovery)
    valid_ids = validated_period_ids(discovery)
    if args.period_id or args.annual_periods or args.actual_and_prior or args.recommended:
        selected_ids = _choose_period_ids(
            catalog,
            valid_ids,
            args.period_id,
            annual_periods=args.annual_periods,
            actual_and_prior=args.actual_and_prior,
        )
    else:
        selected_ids = _prompt_for_period_ids(catalog, valid_ids)
    labels = {
        item["period_id"]: item["label"] for item in catalog["options"]
    }
    print(
        "Selected period(s): "
        + ", ".join(f"{labels[item]} [{item}]" for item in selected_ids),
        flush=True,
    )
    structure = analyze_workbook_structure(
        args.workbook,
        output_dir=work_dir / "upstream",
        discovery_run=discovery,
        selected_period_ids=selected_ids,
        progress=lambda message: print(message, flush=True),
        parsed=parsed,
    )
    result = normalize_workbook(
        args.workbook,
        output_dir=work_dir,
        prior_run=structure,
        selected_period_ids=selected_ids,
        source_name=args.workbook.name,
        progress=lambda message: print(message, flush=True),
        # `progress` names the current stage; `on_activity` is the live feed of
        # what the mapper is doing during a long model session.
        on_activity=lambda message: print(f"  · {message}", flush=True),
        parsed=parsed,
    )
    write_normalized_workbook(
        result, args.output_dir / mapped_output_name(args.workbook.name)
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
    }
    (args.output_dir / "summary.json").write_text(
        json.dumps(summary, indent=2), encoding="utf-8"
    )
    print(json.dumps(summary), flush=True)


if __name__ == "__main__":
    main()
