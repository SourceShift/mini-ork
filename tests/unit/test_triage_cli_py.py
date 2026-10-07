"""Unit tests for the ``mini-ork triage`` CLI handler."""
from __future__ import annotations

import json

import mini_ork.cli.triage as cli
from mini_ork.triage.failures import Evidence, TriageResult


def _stub(monkeypatch, blame: str):
    def _run(run_id, **kwargs):
        return TriageResult(
            run_id=run_id,
            recipe="acq-wave5-rsi",
            blame=blame,
            evidence=[Evidence("traceback", "boom at mini_ork/cli/execute.py")],
        )

    monkeypatch.setattr(cli, "triage_run", _run)


def test_exit_zero_on_mini_ork(monkeypatch, capsys):
    _stub(monkeypatch, "mini_ork")
    rc = cli.main(["--run", "run-1", "--dry-run"], root="/repo")
    assert rc == 0
    assert "mini_ork" in capsys.readouterr().out


def test_exit_three_on_consumer(monkeypatch, capsys):
    _stub(monkeypatch, "consumer")
    rc = cli.main(["--run", "run-1"], root="/repo")
    assert rc == 3
    assert "consumer" in capsys.readouterr().out


def test_latest_resolves_the_most_recent_failed_run(monkeypatch):
    seen: dict[str, str] = {}

    def _run(run_id, **kwargs):
        seen["run_id"] = run_id
        return TriageResult(run_id=run_id, blame="mini_ork")

    monkeypatch.setattr(cli, "latest_failed_run", lambda db: "run-latest")
    monkeypatch.setattr(cli, "resolve_db", lambda db: ":memory:")
    monkeypatch.setattr(cli, "triage_run", _run)
    cli.main(["--latest", "--dry-run"], root=".")
    assert seen["run_id"] == "run-latest"


def test_missing_run_is_usage_error(monkeypatch, capsys):
    monkeypatch.setattr(cli, "latest_failed_run", lambda db: None)
    rc = cli.main(["--latest"], root=".")
    assert rc == 2
    assert "no run given" in capsys.readouterr().err


def test_json_output(monkeypatch, capsys):
    _stub(monkeypatch, "mini_ork")
    cli.main(["--run", "run-1", "--dry-run", "--json"], root=".")
    payload = json.loads(capsys.readouterr().out)
    assert payload["blame"] == "mini_ork"
    assert payload["run_id"] == "run-1"
