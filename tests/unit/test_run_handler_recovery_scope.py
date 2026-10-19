"""A fresh ``mini-ork run`` must not inherit another run's recovery scope.

Incident (2026-10-19): ``run-1792151796-92942--pick-files-directories--D1``,
a 15-node ``recursive-validate-impl`` child, printed
``recovery: from_node=test_validity closure=1/15 nodes`` and dispatched only
``publisher``. ``test_validity`` is a spec-driven-delivery node: the parent
dispatcher spawns children with ``os.environ.copy()``
(``recipes/spec-driven-delivery/lib/per_spec_dispatcher.py:_launch_child``),
so the parent's ``MINI_ORK_RECOVERY_FROM``/``CLOSURE`` crossed the process
boundary. ``execute._apply_recovery_filter`` then pruned the child's DAG by
the parent's node names, the tier-4 panel never ran, and the publisher
blocked on ``missing panel verdict``.
"""
from __future__ import annotations

import os

import pytest

from mini_ork.cli import main as cli_main
from mini_ork.cli import run_resume


@pytest.fixture
def inherited_scope(monkeypatch):
    for key in cli_main._INHERITED_RECOVERY_SCOPE_VARS:
        monkeypatch.setenv(key, f"parent-value-for-{key}")
    return cli_main._INHERITED_RECOVERY_SCOPE_VARS


def test_a_fresh_run_starts_without_inherited_recovery_scope(
        monkeypatch, inherited_scope) -> None:
    monkeypatch.setattr(
        run_resume, "run_with_resume", lambda _argv: (run_resume.RC_OK, {}),
    )
    seen: dict[str, str | None] = {}

    def lifecycle(_rest, _root):
        seen.update({key: os.environ.get(key) for key in inherited_scope})
        return 0

    monkeypatch.setattr(cli_main, "_run_lifecycle", lifecycle)

    assert cli_main._run_handler([], "/engine") == 0
    assert seen == {key: None for key in inherited_scope}


def test_a_resume_hands_dispatch_a_clean_process_env(
        monkeypatch, inherited_scope) -> None:
    """``dispatch_resume`` publishes the resuming run's own closure; a stale
    parent's scope must not be left underneath it."""
    monkeypatch.setattr(
        run_resume, "run_with_resume",
        lambda _argv: (run_resume.RC_OK, {"resume_id": "run-123"}),
    )
    seen: dict[str, str | None] = {}

    def dispatch(_sink, *, root):
        del root
        seen.update({key: os.environ.get(key) for key in inherited_scope})
        return 0

    monkeypatch.setattr(run_resume, "dispatch_resume", dispatch)

    assert cli_main._run_handler(["--resume", "run-123"], "/engine") == 0
    assert seen == {key: None for key in inherited_scope}


def test_the_scrub_covers_every_scope_variable() -> None:
    """Guard against adding a recovery env var without scrubbing it here."""
    assert set(cli_main._INHERITED_RECOVERY_SCOPE_VARS) == {
        "MINI_ORK_RECOVERY_RUN_ID",
        "MINI_ORK_RECOVERY_SKU",
        "MINI_ORK_RECOVERY_CLOSURE",
        "MINI_ORK_RECOVERY_STRATEGY",
        "MINI_ORK_RECOVERY_FROM",
        "MINI_ORK_RECOVERY_ACTIVE",
    }
