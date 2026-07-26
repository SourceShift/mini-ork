"""Contract tests for the cross-platform public command installer."""
from __future__ import annotations

import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from mini_ork.cli import install_command


def _root(tmp_path: Path) -> Path:
    root = tmp_path / "engine"
    launcher = root / "bin" / "mini-ork"
    launcher.parent.mkdir(parents=True)
    launcher.write_text("#!/usr/bin/env python3\n", encoding="utf-8")
    return root


def _env(tmp_path: Path, *, shell: str = "/bin/zsh", path: str = "/usr/bin") -> dict[str, str]:
    home = tmp_path / "home"
    return {"HOME": str(home), "SHELL": shell, "PATH": path}


def test_posix_install_writes_managed_launcher_and_profile(tmp_path):
    root = _root(tmp_path)
    env = _env(tmp_path)
    target_dir = tmp_path / "bin"

    result = install_command.install(
        root=root,
        bin_dir=target_dir,
        environment=env,
        platform_name="posix",
    )

    target = target_dir / "mini-ork"
    assert result.target == target
    assert result.launcher_changed is True
    assert result.path_changed is True
    assert target.read_text(encoding="utf-8").startswith("#!/bin/sh\n")
    assert install_command._MANAGED_MARKER in target.read_text(encoding="utf-8")
    assert str(root / "bin" / "mini-ork") in target.read_text(encoding="utf-8")
    assert stat.S_IMODE(target.stat().st_mode) == 0o755
    profile = Path(env["HOME"]) / ".zshrc"
    assert profile.read_text(encoding="utf-8").count(install_command._PROFILE_BEGIN) == 1


def test_posix_install_repairs_stale_symlink_and_is_idempotent(tmp_path):
    root = _root(tmp_path)
    env = _env(tmp_path)
    target_dir = tmp_path / "bin"
    target_dir.mkdir()
    target = target_dir / "mini-ork"
    legacy_launcher = tmp_path / "old-checkout" / "bin" / "mini-ork"
    legacy_launcher.parent.mkdir(parents=True)
    legacy_launcher.write_text("#!/usr/bin/env bash\nexport MINI_ORK_ROOT=/old-checkout\n", encoding="utf-8")
    target.symlink_to(legacy_launcher)

    first = install_command.install(root=root, bin_dir=target_dir, environment=env, platform_name="posix")
    second = install_command.install(root=root, bin_dir=target_dir, environment=env, platform_name="posix")

    assert first.launcher_changed is True
    assert target.is_symlink() is False
    assert second.launcher_changed is False
    assert second.path_changed is False


def test_non_managed_target_requires_force(tmp_path):
    root = _root(tmp_path)
    target_dir = tmp_path / "bin"
    target_dir.mkdir()
    target = target_dir / "mini-ork"
    target.write_text("another program\n", encoding="utf-8")

    with pytest.raises(install_command.InstallError, match="--force"):
        install_command.install(root=root, bin_dir=target_dir, environment=_env(tmp_path), platform_name="posix")

    install_command.install(
        root=root,
        bin_dir=target_dir,
        force=True,
        environment=_env(tmp_path),
        platform_name="posix",
    )
    assert install_command._MANAGED_MARKER in target.read_text(encoding="utf-8")


def test_unrecognized_symlink_requires_force(tmp_path):
    root = _root(tmp_path)
    target_dir = tmp_path / "bin"
    target_dir.mkdir()
    other_program = tmp_path / "other-program"
    other_program.write_text("#!/bin/sh\necho another tool\n", encoding="utf-8")
    target = target_dir / "mini-ork"
    target.symlink_to(other_program)

    with pytest.raises(install_command.InstallError, match="--force"):
        install_command.install(root=root, bin_dir=target_dir, environment=_env(tmp_path), platform_name="posix")

    target.unlink()
    target.symlink_to(tmp_path / "missing" / "bin" / "mini-ork")
    with pytest.raises(install_command.InstallError, match="--force"):
        install_command.install(root=root, bin_dir=target_dir, environment=_env(tmp_path), platform_name="posix")


def test_no_path_and_dry_run_do_not_modify_user_files(tmp_path):
    root = _root(tmp_path)
    target_dir = tmp_path / "bin"
    result = install_command.install(
        root=root,
        bin_dir=target_dir,
        update_path=False,
        dry_run=True,
        environment=_env(tmp_path),
        platform_name="posix",
    )

    assert result.launcher_changed is True
    assert result.path_changed is False
    assert target_dir.exists() is False
    assert "PATH management skipped (--no-path)." in result.notices


def test_windows_launcher_and_path_merge_contract(tmp_path):
    root = _root(tmp_path)
    result = install_command.install(
        root=root,
        bin_dir=tmp_path / "windows-bin",
        update_path=False,
        environment={"USERPROFILE": str(tmp_path / "home"), "PATH": r"C:\\Python"},
        platform_name="windows",
    )

    content = result.target.read_text(encoding="utf-8")
    assert result.target.name == "mini-ork.cmd"
    assert "py -3" in content
    assert "python " in content
    merged, changed = install_command.merge_windows_path(r"C:\\Python", tmp_path / "windows-bin")
    assert changed is True
    assert merged.endswith(str(tmp_path / "windows-bin"))
    duplicate, duplicate_changed = install_command.merge_windows_path(merged, tmp_path / "windows-bin")
    assert duplicate == merged
    assert duplicate_changed is False


def test_cli_reports_install_errors_and_routes_from_registry(tmp_path, capsys):
    root = _root(tmp_path)
    code = install_command.main(["--unknown"], root=root)
    assert code == 2
    assert "unknown install option" in capsys.readouterr().err

    from mini_ork.cli import main as cli

    target_dir = tmp_path / "bin"
    assert cli.main(["install", "--bin-dir", str(target_dir), "--no-path"], root=str(root)) == 0
    assert target_dir.joinpath("mini-ork").is_file()


def test_public_launcher_bootstraps_install_without_full_runtime_imports(tmp_path):
    target_dir = tmp_path / "bin"
    env = {**os.environ, "HOME": str(tmp_path / "home"), "SHELL": "/bin/zsh", "PATH": "/usr/bin"}

    run = subprocess.run(
        [str(REPO / "bin" / "mini-ork"), "install", "--bin-dir", str(target_dir), "--no-path"],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )

    assert run.returncode == 0, run.stderr
    assert target_dir.joinpath("mini-ork").is_file()
