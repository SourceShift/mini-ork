"""Regression coverage for provider-free lifecycle dry runs."""

from mini_ork.cli import main as cli_main


def test_rubric_prescreen_is_disabled_for_dry_runs(monkeypatch, tmp_path):
    monkeypatch.setenv("MO_RUBRIC", "1")
    monkeypatch.setenv("MINI_ORK_DRY_RUN", "1")

    assert cli_main._should_run_rubric(str(tmp_path)) is False


def test_rubric_prescreen_remains_enabled_for_real_runs(monkeypatch, tmp_path):
    monkeypatch.setenv("MO_RUBRIC", "1")
    monkeypatch.setenv("MINI_ORK_DRY_RUN", "0")

    assert cli_main._should_run_rubric(str(tmp_path)) is True
