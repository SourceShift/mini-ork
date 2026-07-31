"""P1b: opt-in shared-drive cwd routing (``mini_ork.runtime.run_drive``).

Guards the load-bearing default: with the env unset, a run's cwd is unchanged
(today's host-tree behavior). Opting in redirects the cwd onto a per-run drive.
"""
from __future__ import annotations

import os

import pytest

from mini_ork.runtime.run_drive import (
    ENV_BACKEND,
    ENV_ROOT,
    resolve_run_drive_cwd,
    shared_drive_enabled,
)


def test_disabled_by_default_returns_default_cwd(tmp_path):
    default = str(tmp_path / "target")
    assert resolve_run_drive_cwd(default, env={}) == default
    assert shared_drive_enabled({}) is False


def test_empty_backend_is_disabled(tmp_path):
    default = str(tmp_path / "target")
    env = {ENV_BACKEND: "   "}
    assert resolve_run_drive_cwd(default, env=env) == default
    assert shared_drive_enabled(env) is False


def test_local_bind_returns_mount_and_creates_root(tmp_path):
    default = str(tmp_path / "target")
    drive_root = str(tmp_path / "drive")
    env = {ENV_BACKEND: "local-bind", ENV_ROOT: drive_root}
    got = resolve_run_drive_cwd(default, env=env)
    assert got == os.path.abspath(drive_root)
    assert os.path.isdir(drive_root)  # up() provisioned it
    assert shared_drive_enabled(env) is True


def test_root_defaults_to_cwd_when_unset(tmp_path):
    default = str(tmp_path / "target")
    env = {ENV_BACKEND: "local-bind"}
    got = resolve_run_drive_cwd(default, env=env)
    assert got == os.path.abspath(default)
    assert os.path.isdir(default)


def test_unknown_backend_raises_valueerror(tmp_path):
    env = {ENV_BACKEND: "nope"}
    with pytest.raises(ValueError):
        resolve_run_drive_cwd(str(tmp_path), env=env)


def test_reads_process_env_when_env_arg_omitted(tmp_path, monkeypatch):
    default = str(tmp_path / "target")
    drive_root = str(tmp_path / "drive")
    monkeypatch.setenv(ENV_BACKEND, "local-bind")
    monkeypatch.setenv(ENV_ROOT, drive_root)
    assert resolve_run_drive_cwd(default) == os.path.abspath(drive_root)
    assert shared_drive_enabled() is True
