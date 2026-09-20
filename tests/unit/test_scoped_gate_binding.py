"""Hermetic contracts for the goal-loop's scoped-gate baseline.

``kickoffs/book-goal-loop/binding/scoped_gate.py`` scopes the fix child's
typecheck/test to the CHILD'S OWN diff. The base it subtracts is the whole
question: a long-lived branch ref (``origin/main``) makes the scope the feature
branch's entire divergence, so a child that edited nothing is still typechecked
against unrelated files and reddens on pre-existing diagnostics.

The regression this guards, observed live: a zero-change child reported
``scoped typecheck failed for 44 changed file(s)`` — 54 branch-divergence .ts
files minus test files — and the wave failed for a reason the child did not
cause. ``_scoped_base`` reads the child's ``pre-implementer-ref`` instead.

No git, no network, no DB: ``_scoped_base`` is pure env + one file read.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_BINDING = (
    Path(__file__).resolve().parents[2]
    / "kickoffs"
    / "book-goal-loop"
    / "binding"
    / "scoped_gate.py"
)


def _load():
    spec = importlib.util.spec_from_file_location("scoped_gate_binding", _BINDING)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def mod():
    return _load()


@pytest.fixture()
def child_run(tmp_path, monkeypatch):
    """A child run dir holding ``pre-implementer-ref``, wired via the env the
    verifier actually exports (MINI_ORK_HOME + MINI_ORK_RUN_ID)."""
    home = tmp_path / ".mini-ork"
    run_id = "child-1789911252-19277"
    (home / "runs" / run_id).mkdir(parents=True)
    monkeypatch.setenv("MINI_ORK_HOME", str(home))
    monkeypatch.setenv("MINI_ORK_RUN_ID", run_id)
    return home / "runs" / run_id


def test_prefers_child_baseline_over_branch_ref(mod, child_run, monkeypatch):
    """The child's own start point wins even when the launcher set a branch ref."""
    monkeypatch.setenv("MO_GOAL_SCOPED_BASE", "origin/main")
    (child_run / "pre-implementer-ref").write_text(
        "6022375b93e8591620491211cbe2b0f29f0b02e0\n", encoding="utf-8"
    )
    assert mod._scoped_base() == "6022375b93e8591620491211cbe2b0f29f0b02e0"


def test_short_sha_accepted(mod, child_run, monkeypatch):
    monkeypatch.setenv("MO_GOAL_SCOPED_BASE", "origin/main")
    (child_run / "pre-implementer-ref").write_text("6022375b\n", encoding="utf-8")
    assert mod._scoped_base() == "6022375b"


def test_missing_ref_falls_back_to_env(mod, child_run, monkeypatch):
    """No ref file (e.g. a resumed run) -> the configured base, unchanged."""
    monkeypatch.setenv("MO_GOAL_SCOPED_BASE", "origin/main")
    assert mod._scoped_base() == "origin/main"


def test_non_hex_ref_falls_back_to_env(mod, child_run, monkeypatch):
    """A garbage ref must never reach ``git diff`` as a revision argument."""
    monkeypatch.setenv("MO_GOAL_SCOPED_BASE", "origin/main")
    (child_run / "pre-implementer-ref").write_text("HEAD; rm -rf /\n", encoding="utf-8")
    assert mod._scoped_base() == "origin/main"


def test_unset_run_env_falls_back_to_env(mod, monkeypatch):
    """A bare ``scoped_gate.py`` invocation has no run dir at all."""
    monkeypatch.delenv("MINI_ORK_HOME", raising=False)
    monkeypatch.delenv("MINI_ORK_RUN_ID", raising=False)
    monkeypatch.setenv("MO_GOAL_SCOPED_BASE", "origin/main")
    assert mod._scoped_base() == "origin/main"


def test_main_scopes_through_the_child_baseline(mod, monkeypatch, capsys):
    """The wiring that matters: ``main`` must ask ``_scoped_base``, not the raw
    env — otherwise the fix is dead code and the phantom-red returns."""
    monkeypatch.setenv("MO_GOAL_SCOPED_BASE", "origin/main")
    monkeypatch.setattr(mod, "_scoped_base", lambda: "childbaseline0000")
    seen = {}

    def fake_changed(root, base):
        seen["base"] = base
        return []

    monkeypatch.setattr(mod, "_changed", fake_changed)
    rc = mod.main(["typecheck"])
    assert rc == 0
    assert seen["base"] == "childbaseline0000"  # not "origin/main"
    assert "no in-scope TypeScript changes" in capsys.readouterr().out
