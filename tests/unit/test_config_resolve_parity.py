"""Unit tests: ``mini_ork.dispatch.config_resolve`` (bash parity halves removed; formerly vs ``lib/config_resolve.sh``).

For each fixture we seed a self-contained home/root/run-dir tree under
``tmp_path`` and call the Python port with a controlled env via
in-process capture, asserting raw stdout and the snapshotted file body.

Why raw stdout (no ``.strip()``): the contract is ``printf '%s\\n'`` —
exactly ``"path\\n"``. A ``strip()``-comparing harness would silently
mask a regression that drops the trailing ``\\n``.

Env isolation: each fixture pops ``MINI_ORK_RUN_DIR`` / ``MINI_ORK_HOME`` /
``MINI_ORK_ROOT`` before applying its overrides so pytest's arbitrary
collection order cannot leak a prior fixture's env into the next.
"""

from __future__ import annotations

import contextlib
import io
import os
from pathlib import Path

import pytest

from mini_ork.dispatch.config_resolve import (
    resolve_agents_yaml,
    snapshot_run_config,
)

REPO_ROOT = Path(__file__).resolve().parent.parent.parent

_THREE_VARS = ("MINI_ORK_RUN_DIR", "MINI_ORK_HOME", "MINI_ORK_ROOT")

# Repo root contains real ``.mini-ork/config/agents.yaml`` and
# ``config/agents.yaml``, both of which would satisfy the HOME/ROOT
# fall-through. Every fixture that doesn't intentionally exercise the
# defaults must override the irrelevant vars to this sentinel.
_NONEXISTENT = "/nonexistent/__mo_migrate_config_resolve_fixture__"


# ── In-process helpers ──────────────────────────────────────────────────────


def _with_env(overrides: dict):
    """Save the three vars, apply overrides; pair with _restore_env."""
    saved = {k: os.environ.pop(k, None) for k in _THREE_VARS}
    for k, v in overrides.items():
        os.environ[k] = v
    return saved


def _restore_env(saved: dict) -> None:
    for k in _THREE_VARS:
        os.environ.pop(k, None)
    for k, v in saved.items():
        if v is not None:
            os.environ[k] = v


def _capture_py_resolve(env: dict) -> str:
    saved = _with_env(env)
    try:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            resolve_agents_yaml()
        return buf.getvalue()
    finally:
        _restore_env(saved)


def _capture_py_snapshot(run_dir: Path, env: dict) -> str:
    saved = _with_env(env)
    try:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            snapshot_run_config(str(run_dir))
        return buf.getvalue()
    finally:
        _restore_env(saved)


def _seed(tree: Path, body: str) -> None:
    (tree / "config").mkdir(parents=True, exist_ok=True)
    (tree / "config" / "agents.yaml").write_text(body, encoding="utf-8")


# ── Resolve fixtures ────────────────────────────────────────────────────────
# Each returns (id, env_overrides, expected_relative_or_absolute_path).


def _f01_run_dir_wins(tmp_path):
    run = tmp_path / "run"
    home = tmp_path / "home"
    _seed(run, "lanes: [from-run]\n")
    _seed(home, "lanes: [from-home]\n")
    return (
        "01_run_dir_wins_over_home",
        {
            "MINI_ORK_RUN_DIR": str(run),
            "MINI_ORK_HOME": str(home),
            "MINI_ORK_ROOT": _NONEXISTENT,
        },
        str(run / "config" / "agents.yaml"),
    )


def _f02_home_wins(tmp_path):
    home = tmp_path / "home"
    root = tmp_path / "root"
    _seed(home, "lanes: [from-home]\n")
    _seed(root, "lanes: [from-root]\n")
    return (
        "02_home_wins_over_root",
        {
            "MINI_ORK_RUN_DIR": _NONEXISTENT,
            "MINI_ORK_HOME": str(home),
            "MINI_ORK_ROOT": str(root),
        },
        str(home / "config" / "agents.yaml"),
    )


def _f03_root_only(tmp_path):
    root = tmp_path / "root"
    _seed(root, "lanes: [from-root]\n")
    return (
        "03_root_only",
        {
            "MINI_ORK_RUN_DIR": _NONEXISTENT,
            "MINI_ORK_HOME": _NONEXISTENT,
            "MINI_ORK_ROOT": str(root),
        },
        str(root / "config" / "agents.yaml"),
    )


def _f04_all_missing_returns_overridden_root(_tmp_path):
    # HOME and ROOT both overridden to known-missing absolute paths so
    # the fall-through is reproducible regardless of accidental fixture
    # files at the repo root. The literal ROOT value is echoed.
    fake_root = _NONEXISTENT + "_root"
    return (
        "04_all_missing_returns_overridden_root",
        {
            "MINI_ORK_RUN_DIR": _NONEXISTENT,
            "MINI_ORK_HOME": _NONEXISTENT + "_home",
            "MINI_ORK_ROOT": fake_root,
        },
        fake_root + "/config/agents.yaml",
    )


RESOLVE_BUILDERS = (
    _f01_run_dir_wins,
    _f02_home_wins,
    _f03_root_only,
    _f04_all_missing_returns_overridden_root,
)


@pytest.mark.parametrize(
    "builder", RESOLVE_BUILDERS, ids=lambda b: b.__name__,
)
def test_resolve(tmp_path, builder):
    fixture_id, env_overrides, expected = builder(tmp_path)
    expected_nl = expected + "\n"

    py_out = _capture_py_resolve(env_overrides)
    assert py_out == expected_nl, (
        f"resolve stdout drift [{fixture_id}]: py={py_out!r}"
    )


# ── Snapshot fixtures ───────────────────────────────────────────────────────
# Each returns (id, run_dir, env_overrides, expected_dest_body_or_None).
# Idempotent: a pre-existing dest must keep its launch-time body.


def _f05_snapshot_idempotent(tmp_path):
    run = tmp_path / "run"
    home = tmp_path / "home"
    launch_body = "lanes: [launch-time]\n"
    competing = "lanes: [would-overwrite-if-not-idempotent]\n"
    _seed(run, launch_body)
    _seed(home, competing)
    return (
        "05_snapshot_is_idempotent_noop",
        run,
        {
            "MINI_ORK_RUN_DIR": str(run),
            "MINI_ORK_HOME": str(home),
            "MINI_ORK_ROOT": _NONEXISTENT,
        },
        launch_body,
    )


def _f06_snapshot_from_home(tmp_path):
    run = tmp_path / "run"
    home = tmp_path / "home"
    body = "lanes: [from-home]\n"
    _seed(home, body)
    return (
        "06_snapshot_fresh_from_home",
        run,
        {
            "MINI_ORK_RUN_DIR": str(run),
            "MINI_ORK_HOME": str(home),
            "MINI_ORK_ROOT": _NONEXISTENT,
        },
        body,
    )


def _f07_snapshot_from_root(tmp_path):
    run = tmp_path / "run"
    root = tmp_path / "root"
    body = "lanes: [from-root]\n"
    _seed(root, body)
    return (
        "07_snapshot_fresh_from_root_no_home",
        run,
        {
            "MINI_ORK_RUN_DIR": str(run),
            "MINI_ORK_HOME": _NONEXISTENT,
            "MINI_ORK_ROOT": str(root),
        },
        body,
    )


def _f08_snapshot_no_source(tmp_path):
    run = tmp_path / "run"
    return (
        "08_snapshot_no_source_anywhere",
        run,
        {
            "MINI_ORK_RUN_DIR": str(run),
            "MINI_ORK_HOME": _NONEXISTENT,
            "MINI_ORK_ROOT": _NONEXISTENT,
        },
        None,
    )


SNAPSHOT_BUILDERS = (
    _f05_snapshot_idempotent,
    _f06_snapshot_from_home,
    _f07_snapshot_from_root,
    _f08_snapshot_no_source,
)


@pytest.mark.parametrize(
    "builder", SNAPSHOT_BUILDERS, ids=lambda b: b.__name__,
)
def test_snapshot(tmp_path, builder):
    fixture_id, run_dir, env_overrides, expected_body = builder(tmp_path)
    dest = run_dir / "config" / "agents.yaml"

    _capture_py_snapshot(run_dir, env_overrides)

    if expected_body is None:
        assert not dest.exists(), (
            f"[{fixture_id}] expected NO dest (no source anywhere) "
            f"but found {dest}"
        )
    else:
        assert dest.exists(), (
            f"[{fixture_id}] expected dest at {dest} but it is missing"
        )
        actual = dest.read_text(encoding="utf-8")
        assert actual == expected_body, (
            f"[{fixture_id}] dest content drift: "
            f"expected={expected_body!r} actual={actual!r}"
        )


def test_smoke_import_no_io():
    """Module imports cleanly; public API is callable with no I/O."""
    import mini_ork.dispatch.config_resolve as mod
    assert mod.resolve_agents_yaml.__name__ == "resolve_agents_yaml"
    assert mod.snapshot_run_config.__name__ == "snapshot_run_config"
    assert callable(mod.resolve_agents_yaml)
    assert callable(mod.snapshot_run_config)
