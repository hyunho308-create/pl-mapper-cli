"""Print local review evidence for the current Codex task; never launch a judge."""

from __future__ import annotations

import argparse
from pathlib import Path

from hotel_pl_normalizer.evaluation.report import (
    render_evaluation_markdown,
    write_evaluation_report,
)
from hotel_pl_normalizer.evaluation.runner import evaluate_run


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Quick local P&L checks and evidence for review in the active Codex task."
    )
    parser.add_argument("source", type=Path, help="Original source workbook or PDF")
    parser.add_argument(
        "--run-log", type=Path, required=True, help="Saved run_log.json"
    )
    parser.add_argument(
        "--mapped-workbook",
        type=Path,
        help="Final mapped workbook; required for accepted runs",
    )
    parser.add_argument(
        "--expected-period",
        action="append",
        default=[],
        metavar="PERIOD_ID",
        help="Original requested period ID; repeat for each expected period",
    )
    parser.add_argument(
        "--output-dir", type=Path, help="Also save EVAL.md here (optional)"
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.output_dir:
        destination = (args.output_dir / "EVAL.md").resolve()
        if destination in {
            p.resolve() for p in (args.source, args.run_log, args.mapped_workbook) if p
        }:
            parser.error("The review output must not overwrite an input artifact.")
    result = evaluate_run(
        args.source,
        args.run_log,
        args.mapped_workbook,
        expected_period_ids=args.expected_period,
    )
    print(render_evaluation_markdown(result), end="")
    if args.output_dir:
        write_evaluation_report(result, args.output_dir)
    # Zero means ready to review, never semantically approved.
    return {"ready_for_review": 0, "issues_found": 2, "incomplete": 3}[result.status]
