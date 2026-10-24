"""The post-run reflect child must get a durable cwd, not the caller's.

The RSI runner launches ``mini-ork run`` from an iteration worktree and removes
that worktree ~2 s after the run returns. The reflect child inherited that cwd,
so every later ``os.getcwd()`` raised ENOENT and no learning artifact was ever
produced. The run dir (else the home) outlives the run.

Kickoff source: RSI Tier-0 #5 (loop defect D1).
"""
import os
import subprocess

from mini_ork.cli import main as mo_main

from test_reflect_run_scope import _KICKOFF, _scrubbed_env, _spy_popen


def _assert_durable_cwd(cwd, home, ephemeral):
    assert cwd is not None, "reflect spawn got no cwd kwarg"
    assert os.path.isdir(str(cwd)), f"reflect cwd is not a directory: {cwd!r}"
    assert os.path.realpath(str(cwd)) != os.path.realpath(str(ephemeral)), (
        "reflect cwd is the worktree cwd deleted under the child")
    home_real = os.path.realpath(str(home))
    assert os.path.realpath(str(cwd)).startswith(home_real + os.sep), (
        f"reflect cwd {cwd!r} is not under MINI_ORK_HOME {home_real!r}")


def _drive(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    kickoff = tmp_path / "kickoff.md"
    kickoff.write_text(_KICKOFF, encoding="utf-8")
    ephemeral = tmp_path / "ephemeral"
    ephemeral.mkdir()
    monkeypatch.chdir(ephemeral)
    return home, kickoff, ephemeral


def test_detached_reflect_spawn_has_durable_cwd(tmp_path, monkeypatch):
    home, kickoff, ephemeral = _drive(tmp_path, monkeypatch)
    with _scrubbed_env(
        MINI_ORK_DRY_RUN="1",
        MINI_ORK_HOME=str(home),
        MINI_ORK_DB=str(home / "state.db"),
        MO_AUTO_REPAIR="0",
        MO_RUBRIC="0",
    ), _spy_popen() as fakes:
        rc = mo_main.main(["run", "code-fix", str(kickoff)])

    assert rc == 0, f"dry-run lifecycle failed (rc={rc})"
    assert fakes, "the post-run reflect child was not spawned through Popen"
    _assert_durable_cwd(fakes[0].kwargs.get("cwd"), home, ephemeral)


def test_inline_reflect_run_has_durable_cwd(tmp_path, monkeypatch):
    home, kickoff, ephemeral = _drive(tmp_path, monkeypatch)
    seen = []
    real_run = subprocess.run

    def spy(args=None, *rest, **kwargs):
        if isinstance(args, (list, tuple)) and any(
                "mini_ork.cli.reflect" in str(part) for part in args):
            seen.append(kwargs)
            return subprocess.CompletedProcess(list(args), 0, stdout="", stderr="")
        return real_run(args, *rest, **kwargs)

    monkeypatch.setattr(subprocess, "run", spy)
    with _scrubbed_env(
        MINI_ORK_DRY_RUN="1",
        MINI_ORK_HOME=str(home),
        MINI_ORK_DB=str(home / "state.db"),
        MO_AUTO_REPAIR="0",
        MO_RUBRIC="0",
        MO_REFLECT_INLINE="1",
    ):
        rc = mo_main.main(["run", "code-fix", str(kickoff)])

    assert rc == 0, f"dry-run lifecycle failed (rc={rc})"
    assert seen, "no inline reflect subprocess.run was observed"
    _assert_durable_cwd(seen[0].get("cwd"), home, ephemeral)
