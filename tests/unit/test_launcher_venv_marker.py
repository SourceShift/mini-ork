"""The launcher must not trust a stale ``MINI_ORK_VENV_ACTIVE`` marker.

Regression: the scheduler spawns ``bin/mini-ork`` (``mini_ork/scheduler.py``,
``dispatch_epic``) through the launcher's ``#!/usr/bin/env python3`` shebang, so
``env`` re-resolves ``python3`` from ``PATH``. That child also inherits
``MINI_ORK_VENV_ACTIVE=1`` from the parent's re-exec, so it skipped its own
re-exec, stayed on the PATH interpreter, and died in
``_require_supported_python`` with rc=2. Every scheduler dispatch failed this
way while interactive ``bin/mini-ork`` calls worked — the marker was true of
the parent and false of the child.
"""
from __future__ import annotations

import importlib.util
import os
import sys
from importlib.machinery import SourceFileLoader
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_LAUNCHER = _ROOT / "bin" / "mini-ork"


def _load_launcher():
    # The launcher calls ``os.execve`` at module scope. Disable the venv
    # re-exec before import or loading it would replace the pytest process.
    #
    # Loading it ALSO mutates process-global state at module scope: its
    # ``_configure_paths`` writes MINI_ORK_HOME / MINI_ORK_PROJECT_HOME /
    # MINI_ORK_ROOT / MINI_ORK_TARGET_REPO / MINI_ORK_ENGINE_ROOT into
    # ``os.environ`` and ``sys.path.insert(0, ENGINE_ROOT)``. This load runs at
    # MODULE scope, so anything left behind leaks into every later test in the
    # shard — a later test's ``bin/mini-ork`` subprocess inherits the leaked
    # MINI_ORK_PROJECT_HOME, which the launcher prefers over the test's own
    # MINI_ORK_HOME, so that child resolves the wrong ``.mini-ork`` (wrong
    # state.db, spurious mo-home upload). Snapshot and restore so the load is
    # hermetic.
    env_snapshot = dict(os.environ)
    path_snapshot = list(sys.path)
    os.environ["MINI_ORK_USE_VENV"] = "0"
    try:
        loader = SourceFileLoader("mini_ork_launcher_under_test", str(_LAUNCHER))
        spec = importlib.util.spec_from_loader(loader.name, loader)
        assert spec is not None
        module = importlib.util.module_from_spec(spec)
        loader.exec_module(module)
        return module
    finally:
        os.environ.clear()
        os.environ.update(env_snapshot)
        sys.path[:] = path_snapshot


launcher = _load_launcher()


def test_marker_is_ignored_on_an_unsupported_interpreter() -> None:
    """The exact scheduler-dispatch failure: marker set, interpreter too old."""
    assert launcher._should_reexec({"MINI_ORK_VENV_ACTIVE": "1"}, (3, 9, 23)) is True


def test_marker_is_honoured_on_a_supported_interpreter() -> None:
    """A genuine venv-active parent must not be made to re-exec in a loop."""
    assert launcher._should_reexec({"MINI_ORK_VENV_ACTIVE": "1"}, (3, 13, 11)) is False


def test_explicit_opt_out_wins_over_everything() -> None:
    assert launcher._should_reexec({"MINI_ORK_USE_VENV": "0"}, (3, 9, 23)) is False
    assert launcher._should_reexec(
        {"MINI_ORK_USE_VENV": "0", "MINI_ORK_VENV_ACTIVE": "1"}, (3, 13, 11)
    ) is False


def test_a_fresh_process_reexecs_when_a_venv_is_available() -> None:
    assert launcher._should_reexec({}, (3, 9, 23)) is True
    assert launcher._should_reexec({}, (3, 13, 11)) is True
