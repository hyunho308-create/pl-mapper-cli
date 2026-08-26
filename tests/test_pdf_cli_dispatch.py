from types import SimpleNamespace

from hotel_pl_normalizer import cli


def test_pdf_cli_dispatches_directly_without_excel_conversion(tmp_path, monkeypatch):
    source = tmp_path / "statement.pdf"
    source.write_bytes(b"%PDF-1.4\n")
    output = tmp_path / "out"
    seen = {}

    def fake_normalize_pdf(path, **kwargs):
        seen["path"] = path
        seen["kwargs"] = kwargs
        return SimpleNamespace(
            accepted=True,
            outcome="accepted",
            stopped_reason=None,
            exceptions=[],
            mapped_account_count=1,
            cost_usd=0.01,
            duration_ms=100,
            mapping_model="test-model",
            mapping_provider="test-provider",
            period_labels={"fy": "FY Actual"},
            dropped_periods={},
            session_calls=1,
            session_exhausted=False,
        )

    monkeypatch.setenv("OPENAI_API_KEY", "configured-for-test")
    monkeypatch.setattr(cli, "normalize_pdf", fake_normalize_pdf)
    monkeypatch.setattr(cli, "write_normalized_workbook", lambda *args: None)
    monkeypatch.setattr(cli, "write_run_log", lambda *args: None)
    monkeypatch.setattr(
        cli,
        "shared_workbook",
        lambda *args: (_ for _ in ()).throw(AssertionError("Excel path used")),
    )
    monkeypatch.setattr(
        cli.sys,
        "argv",
        ["hotel-pl-normalizer", str(source), str(output), "--recommended"],
    )

    cli.main()

    assert seen["path"] == source.resolve()
    assert seen["kwargs"]["selected_period_ids"] == []
    assert seen["kwargs"]["select_periods"] is None
    assert (output / "summary.json").is_file()
