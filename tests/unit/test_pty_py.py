"""Unit tests for ``mini_ork.web.routes.pty`` cwd resolution.

``run_id`` reaches ``_resolve_cwd`` straight off a WebSocket query param, so it
is attacker-controlled. These tests pin the path-traversal guard: a hostile
``run_id`` must never move the orchestrator's cwd outside the project's
``runs/`` directory.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from mini_ork.web.routes import pty


@pytest.fixture()
def home(tmp_path: Path) -> Path:
    # Mimic a project: <root>/.mini-ork with a runs/ dir and one real run.
    h = tmp_path / "proj" / ".mini-ork"
    (h / "runs" / "run-123").mkdir(parents=True)
    return h


def test_resolve_cwd_returns_real_run_dir(home: Path) -> None:
    assert pty._resolve_cwd("run-123", home) == (home / "runs" / "run-123").resolve()


def test_resolve_cwd_missing_run_falls_back_to_project_root(home: Path) -> None:
    # Unknown but syntactically safe run_id -> project root, not an error.
    assert pty._resolve_cwd("does-not-exist", home) == home.parent


def test_resolve_cwd_no_run_id_is_project_root(home: Path) -> None:
    assert pty._resolve_cwd(None, home) == home.parent


@pytest.mark.parametrize(
    "hostile",
    [
        "../../../../etc",
        "../..",
        "..",
        ".",
        "run-123/../../..",
        "/etc",
        "/etc/passwd",
        "sub/dir",
        "back\\slash",
        "nul\x00byte",
    ],
)
def test_resolve_cwd_rejects_traversal(home: Path, hostile: str) -> None:
    # No hostile run_id may escape runs/: every one falls back to home.parent
    # (the project root) rather than resolving to somewhere outside the project.
    resolved = pty._resolve_cwd(hostile, home)
    assert resolved == home.parent


@pytest.mark.parametrize("bad", ["../../etc", "..", ".", "a/b", "c\\d", "x\x00y", ""])
def test_is_safe_run_id_rejects(bad: str) -> None:
    assert pty._is_safe_run_id(bad) is False


@pytest.mark.parametrize("ok", ["run-123", "20260804T101500Z", "abc_DEF.9"])
def test_is_safe_run_id_accepts_plain_segments(ok: str) -> None:
    assert pty._is_safe_run_id(ok) is True
