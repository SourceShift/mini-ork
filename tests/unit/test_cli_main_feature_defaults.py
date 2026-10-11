"""Startup ordering: ``features.yaml`` is applied before the secret store.

Both writers use ``os.environ.setdefault``, so the *first* caller wins and the
order in ``main`` is the precedence: an IDE toggle (features.yaml) must beat a
stale hand-edit in ``secrets.local.sh``, while a real environment variable still
beats both. Precedence: env > features.yaml > secrets.local.sh > engine default.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from mini_ork.cli import main as cli_main


@pytest.fixture(autouse=True)
def _home(tmp_path: Path, monkeypatch) -> Path:
    home = tmp_path / ".mini-ork"
    (home / "config").mkdir(parents=True)
    monkeypatch.setenv("MINI_ORK_HOME", str(home))
    monkeypatch.delenv("MINI_ORK_FEATURES", raising=False)
    monkeypatch.delenv("MINI_ORK_SECRETS", raising=False)
    return home


def _write_features(home: Path, text: str) -> None:
    (home / "config" / "features.yaml").write_text(text, encoding="utf-8")


def _write_secrets(home: Path, lines: list[str]) -> Path:
    """A valid secrets.local.sh: 0700 parent, 0600 file, ``export`` lines."""
    parent = home / "config"
    os.chmod(parent, 0o700)
    path = parent / "secrets.local.sh"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.chmod(path, 0o600)
    return path


def test_feature_defaults_reach_the_environment(_home: Path) -> None:
    _write_features(_home, "features:\n  plan_qa_gate: '0'\n")
    cli_main._load_feature_defaults()
    assert os.environ["MINI_ORK_EXECUTE_GATE"] == "0"


def test_a_raw_env_section_is_applied_verbatim(_home: Path) -> None:
    _write_features(_home, "env:\n  MO_DAILY_BUDGET_USD: '20'\n")
    cli_main._load_feature_defaults()
    assert os.environ["MO_DAILY_BUDGET_USD"] == "20"


def test_a_real_environment_variable_wins(_home: Path, monkeypatch) -> None:
    monkeypatch.setenv("MINI_ORK_EXECUTE_GATE", "1")
    _write_features(_home, "features:\n  plan_qa_gate: '0'\n")
    cli_main._load_feature_defaults()
    assert os.environ["MINI_ORK_EXECUTE_GATE"] == "1"  # env beats the file


def test_features_beat_a_stale_secret_store(_home: Path) -> None:
    """The IDE toggle must not be silently beaten by a hand-edited secrets file."""
    _write_features(_home, "env:\n  MO_DAILY_BUDGET_USD: '20'\n")
    _write_secrets(_home, ["export MO_DAILY_BUDGET_USD=99"])
    cli_main._load_feature_defaults()  # ordered first, exactly as main() does
    cli_main._load_secret_store_env()
    assert os.environ["MO_DAILY_BUDGET_USD"] == "20"


def test_a_name_only_in_the_secret_store_still_loads(_home: Path) -> None:
    _write_secrets(_home, ["export MO_DAILY_BUDGET_USD=99"])
    cli_main._load_feature_defaults()
    cli_main._load_secret_store_env()
    assert os.environ["MO_DAILY_BUDGET_USD"] == "99"


def test_an_unreadable_store_warns_but_does_not_raise(_home: Path, monkeypatch, capsys) -> None:
    """A bad-permission secrets file is the documented fail-closed path."""
    # A group-readable store makes read_secret_exports raise SecretStoreError.
    path = _write_secrets(_home, ["export OPENAI_API_KEY=sk-x"])
    os.chmod(path, 0o644)
    cli_main._load_feature_defaults()
    cli_main._load_secret_store_env()  # must not raise
    assert "secret store not loaded" in capsys.readouterr().err


def test_a_store_read_failure_warns_and_main_keeps_going(_home: Path, monkeypatch, capsys) -> None:
    """``effective_env`` raising must degrade to a warning, never a crash."""
    def boom(*_args, **_kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr("mini_ork.features.store.effective_env", boom)
    cli_main._load_feature_defaults()  # default import is resolved at call time
    assert "feature defaults not loaded" in capsys.readouterr().err


def test_a_malformed_file_does_not_stop_an_ordinary_command(_home: Path) -> None:
    _write_features(_home, "features: [this is not a mapping\n")
    rc = cli_main.main(["help"])
    assert isinstance(rc, int)  # ``help`` survived a broken file


def test_main_applies_defaults_before_dispatch(_home: Path) -> None:
    _write_features(_home, "features:\n  oracle_gates: '0'\n")
    cli_main.main(["help"])
    assert os.environ["MO_ORACLE_GATES_AUTO"] == "0"
