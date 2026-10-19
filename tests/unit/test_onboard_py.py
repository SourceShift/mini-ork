"""Contracts for ``mini-ork onboard`` — the first-run wizard.

The wizard's value is that it is *safe*: it must never spend money, never
downgrade the secure tier, and never claim a component was installed when its
toolchain was missing. These tests pin those properties without a terminal, a
network, or a Rust toolchain.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from mini_ork.cli import onboard


def _hooks(**kwargs) -> onboard.Hooks:
    """Hooks that never shell out; record nothing unless a test asks."""
    defaults = {
        "which": lambda name: f"/usr/bin/{name}",
        "run": lambda cmd, cwd=None: 0,
        "prompt": lambda question, default: default,
    }
    defaults.update(kwargs)
    return onboard.Hooks(**defaults)


# --------------------------------------------------------------------------
# Plan resolution
# --------------------------------------------------------------------------


def test_defaults_offer_components_only_when_their_toolchain_exists(monkeypatch, tmp_path):
    monkeypatch.setattr(onboard.shutil, "which", lambda name: None)
    plan = onboard.resolve_plan([], cwd=tmp_path)
    assert plan.components == ()

    monkeypatch.setattr(
        onboard.shutil, "which", lambda name: "/usr/bin/cargo" if name == "cargo" else None
    )
    plan = onboard.resolve_plan([], cwd=tmp_path)
    assert plan.components == ("contextnest",)


def test_explicit_with_installs_even_without_the_toolchain(monkeypatch, tmp_path):
    monkeypatch.setattr(onboard.shutil, "which", lambda name: None)
    plan = onboard.resolve_plan(["--with", "contextnest,zed"], cwd=tmp_path)
    assert plan.components == ("contextnest", "zed")


def test_contradictory_flags_are_refused(tmp_path):
    with pytest.raises(ValueError, match="conflicts"):
        onboard.resolve_plan(["--with", "contextnest", "--no-contextnest"], cwd=tmp_path)


def test_unknown_component_is_refused(tmp_path):
    with pytest.raises(ValueError, match="unknown component"):
        onboard.resolve_plan(["--with", "kubernetes"], cwd=tmp_path)


def test_workspace_defaults_to_cwd_and_expands_home(tmp_path):
    assert onboard.resolve_plan([], cwd=tmp_path).workspace == tmp_path
    expanded = onboard.resolve_plan(["--workspace", "~/demo"]).workspace
    assert expanded == Path.home() / "demo"


# --------------------------------------------------------------------------
# Steps
# --------------------------------------------------------------------------


def test_prerequisites_fail_loudly_when_git_is_missing(monkeypatch):
    monkeypatch.setattr(onboard.sys, "version_info", (3, 11, 0, "final", 0))
    result = onboard.check_prereqs(_hooks(which=lambda name: None))
    assert result.status == "failed"
    assert "git" in result.detail


def test_workspace_dry_run_writes_nothing(tmp_path):
    plan = onboard.resolve_plan(["--dry-run", "--no-contextnest", "--no-zed"], cwd=tmp_path)
    result = onboard.init_workspace(plan, _hooks())
    assert result.status == "ok"
    assert "dry run" in result.detail
    assert not (tmp_path / ".mini-ork").exists()


def test_workspace_init_is_idempotent(tmp_path):
    (tmp_path / ".mini-ork").mkdir()
    plan = onboard.resolve_plan(["--no-contextnest", "--no-zed"], cwd=tmp_path)
    result = onboard.init_workspace(plan, _hooks())
    assert result.status == "skipped"
    assert "already exists" in result.detail


def test_contextnest_without_cargo_fails_closed_with_a_fix(tmp_path):
    plan = onboard.resolve_plan(["--with", "contextnest"], cwd=tmp_path)
    result = onboard.install_contextnest(plan, _hooks(which=lambda name: None))
    assert result.status == "failed"
    assert "rustup" in result.detail


def test_contextnest_dry_run_clones_nothing(tmp_path, monkeypatch):
    monkeypatch.setenv("CONTEXTNEST_INSTALL_DIR", str(tmp_path / "cn"))
    plan = onboard.resolve_plan(["--with", "contextnest", "--dry-run"], cwd=tmp_path)
    result = onboard.install_contextnest(plan, _hooks())
    assert result.status == "ok"
    assert "would clone" in result.detail
    assert not (tmp_path / "cn").exists()


def test_contextnest_build_failure_is_reported_not_hidden(tmp_path, monkeypatch):
    target = tmp_path / "cn"
    target.mkdir()
    (target / "Cargo.toml").write_text("[package]\n", encoding="utf-8")
    monkeypatch.setenv("CONTEXTNEST_INSTALL_DIR", str(target))

    def run(cmd, cwd=None):
        return 1 if cmd[:2] == ["cargo", "build"] else 0

    plan = onboard.resolve_plan(["--with", "contextnest", "--yes"], cwd=tmp_path)
    result = onboard.install_contextnest(plan, _hooks(run=run))
    assert result.status == "failed"
    assert "cargo build failed" in result.detail


def test_contextnest_config_is_seeded_in_the_simple_tier(tmp_path, monkeypatch):
    home = tmp_path / ".mini-ork"
    monkeypatch.setenv("MINI_ORK_HOME", str(home))
    plan = onboard.resolve_plan(["--with", "contextnest", "--yes"], cwd=tmp_path)
    note = onboard._write_contextnest_config(plan)
    dest = home / "config" / "contextnest.yaml"
    assert dest.is_file()
    assert "config seeded" in note
    assert "base_url: http://127.0.0.1:28080" in dest.read_text(encoding="utf-8")


def test_contextnest_config_is_not_written_in_the_secure_tier(tmp_path, monkeypatch):
    home = tmp_path / ".mini-ork"
    monkeypatch.setenv("MINI_ORK_HOME", str(home))
    plan = onboard.resolve_plan(["--with", "contextnest", "--secure", "--yes"], cwd=tmp_path)
    note = onboard._write_contextnest_config(plan)
    assert not (home / "config" / "contextnest.yaml").exists()
    assert "CN_TOKEN" in note


def test_zed_is_skipped_with_the_exact_next_command(monkeypatch, tmp_path):
    plan = onboard.resolve_plan(["--with", "zed", "--yes"], cwd=tmp_path)
    result = onboard.setup_zed(plan, _hooks(which=lambda name: None))
    assert result.status == "skipped"
    assert "mini-ork zed setup" in result.detail


def test_zed_setup_runs_only_when_zed_exists(tmp_path, monkeypatch):
    calls: list[list[str]] = []
    monkeypatch.setattr(
        "mini_ork.cli.zed_cmd.main", lambda rest, root: calls.append(rest) or 0
    )
    plan = onboard.resolve_plan(["--with", "zed", "--yes"], cwd=tmp_path)
    result = onboard.setup_zed(plan, _hooks())
    assert result.status == "ok"
    assert calls == [["setup"]]


# --------------------------------------------------------------------------
# Report and exit codes
# --------------------------------------------------------------------------


def test_render_prints_next_steps_only_on_a_clean_run():
    ok = [onboard.StepResult("prerequisites", "ok", "python 3.11+, git")]
    assert "Next steps" in onboard._render(ok)
    failed = ok + [onboard.StepResult("contextnest", "failed", "no cargo")]
    assert "Next steps" not in onboard._render(failed)
    assert "[FAILED]" in onboard._render(failed)


def test_strict_turns_a_failed_component_into_a_nonzero_exit(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(onboard, "run", lambda plan, hooks: [
        onboard.StepResult("prerequisites", "ok", "python 3.11+, git"),
        onboard.StepResult("contextnest", "failed", "no cargo"),
    ])
    rc = onboard.main(["--with", "contextnest", "--strict"], root=str(tmp_path))
    assert rc == 1
    assert "[FAILED]" in capsys.readouterr().out


def test_non_strict_run_reports_the_failure_but_exits_zero(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(onboard, "run", lambda plan, hooks: [
        onboard.StepResult("prerequisites", "ok", "python 3.11+, git"),
        onboard.StepResult("contextnest", "failed", "no cargo"),
    ])
    assert onboard.main(["--with", "contextnest"], root=str(tmp_path)) == 0


def test_json_mode_is_machine_readable(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(onboard, "run", lambda plan, hooks: [
        onboard.StepResult("prerequisites", "ok", "python 3.11+, git"),
    ])
    rc = onboard.main(["--json"], root=str(tmp_path))
    payload = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert payload["steps"][0] == {
        "name": "prerequisites",
        "status": "ok",
        "detail": "python 3.11+, git",
    }


def test_bad_arguments_exit_two(tmp_path):
    assert onboard.main(["--with", "kubernetes"], root=str(tmp_path)) == 2
