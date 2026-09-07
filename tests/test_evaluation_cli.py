from pathlib import Path

from hotel_pl_normalizer.evaluation.cli import build_parser, main
from hotel_pl_normalizer.evaluation.models import EvaluationResult


def test_cli_defaults_to_stdout_and_has_no_judge_settings():
    args = build_parser().parse_args(["source.xlsx", "--run-log", "run_log.json"])
    assert args.output_dir is None
    assert not hasattr(args, "codex_model")
    assert not hasattr(args, "reasoning_effort")


def test_cli_prints_complete_evidence_without_creating_files(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        "hotel_pl_normalizer.evaluation.cli.evaluate_run",
        lambda *args, **kwargs: EvaluationResult(source_name="source.xlsx"),
    )
    assert main(["source.xlsx", "--run-log", "run_log.json"]) == 0
    assert "Semantic review: **pending**" in capsys.readouterr().out
    assert list(tmp_path.iterdir()) == []


def test_cli_optionally_saves_only_markdown(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(
        "hotel_pl_normalizer.evaluation.cli.evaluate_run",
        lambda *args, **kwargs: EvaluationResult(source_name="source.xlsx"),
    )
    assert (
        main(
            ["source.xlsx", "--run-log", "run_log.json", "--output-dir", str(tmp_path)]
        )
        == 0
    )
    assert [p.name for p in tmp_path.iterdir()] == ["EVAL.md"]
    assert (tmp_path / "EVAL.md").read_text(encoding="utf-8") == capsys.readouterr().out


def test_cli_missing_artifacts_are_incomplete(capsys):
    assert main(["missing.xlsx", "--run-log", str(Path("missing-log.json"))]) == 3
    assert "incomplete" in capsys.readouterr().out
