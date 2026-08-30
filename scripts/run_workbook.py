"""Run one workbook through the production pipeline from the command line."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from hotel_pl_normalizer.cli import _validate_period_ids
from hotel_pl_normalizer.models.period_selection import is_annual_summary_period
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


def _choose_period_ids(
    catalog: dict,
    valid_ids: set[str],
    requested_ids: list[str],
    annual_periods: int = 0,
) -> list[str]:
    """Batch-only policy that replaces the normal human selection boundary."""
    if requested_ids:
        return _validate_period_ids(catalog, valid_ids, requested_ids)
    annual = [
        item["period_id"]
        for item in catalog["options"]
        if item["period_id"] in valid_ids
        and is_annual_summary_period(item["start_month"], item["end_month"])
    ]
    if not annual:
        raise RuntimeError("No discovered annual period passed validation.")
    return annual[:annual_periods]


def main() -> None:
    # The progress feed carries typographic characters, and a Windows console
    # defaults to cp1252, which turns them into replacement marks. Nothing is
    # wrong with the messages -- the stream just has to be told they are UTF-8.
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser()
    parser.add_argument("workbook", type=Path)
    parser.add_argument("output_dir", type=Path)
    selection = parser.add_mutually_exclusive_group(required=True)
    selection.add_argument(
        "--period-id",
        action="append",
        default=[],
        help="Period id to map; repeat to map multiple periods.",
    )
    selection.add_argument(
        "--annual-periods",
        type=int,
        default=0,
        metavar="N",
        help=(
            "Map the first N validated annual periods (full year, YTD or TTM) "
            "in discovery order."
        ),
    )
    args = parser.parse_args()
    if not args.period_id and args.annual_periods < 1:
        parser.error("--annual-periods must be at least 1")

    args.output_dir.mkdir(parents=True, exist_ok=True)
    work_dir = args.output_dir / "work"
    if args.workbook.suffix.lower() == ".pdf":
        discovery = discover_pdf_periods(
            args.workbook,
            output_dir=work_dir / "discovery",
            progress=lambda message: print(message, flush=True),
        )
        catalog = {
            "options": [
                period.model_dump(mode="json")
                for period in discovery.exploration.periods
            ]
        }
        selected_ids = _choose_period_ids(
            catalog,
            {item["period_id"] for item in catalog["options"]},
            args.period_id,
            annual_periods=args.annual_periods,
        )
        result = normalize_pdf(
            args.workbook,
            output_dir=work_dir,
            selected_period_ids=selected_ids,
            source_name=args.workbook.name,
            progress=lambda message: print(message, flush=True),
            on_activity=lambda message: print(f"  · {message}", flush=True),
            discovery=discovery,
        )
    else:
        # The local CLI can afford to retain one parsed workbook across the
        # interactive boundary; the web worker deliberately does not.
        parsed = shared_workbook(args.workbook)
        discovery = discover_workbook_periods(
            args.workbook,
            output_dir=work_dir / "discovery",
            progress=lambda message: print(message, flush=True),
            parsed=parsed,
        )
        catalog = _catalog(discovery)
        selected_ids = _choose_period_ids(
            catalog,
            validated_period_ids(discovery),
            args.period_id,
            annual_periods=args.annual_periods,
        )
        labels = {
            item["period_id"]: item["label"] for item in catalog["options"]
        }
        print(
            "Selected period(s): "
            + ", ".join(f"{labels[item]} [{item}]" for item in selected_ids),
            flush=True,
        )
        # Read once for both stages; normalize_workbook releases it before mapping.
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
            # `progress` names the current stage; `on_activity` is the running feed of
            # what the mapper is actually doing. The demo server has always shown the
            # second one and the CLI never did, so a mapping session that runs for ten
            # minutes printed one line and then nothing.
            on_activity=lambda message: print(f"  · {message}", flush=True),
            parsed=parsed,
        )
    write_normalized_workbook(
        result, args.output_dir / mapped_output_name(args.workbook.name)
    )
    write_run_log(result, args.output_dir / "run_log.json")
    summary = {
        "accepted": result.accepted,
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
        "stopped_reason": result.stopped_reason,
    }
    (args.output_dir / "summary.json").write_text(
        json.dumps(summary, indent=2), encoding="utf-8"
    )
    print(json.dumps(summary), flush=True)


if __name__ == "__main__":
    main()
