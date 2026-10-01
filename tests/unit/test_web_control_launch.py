"""Hermetic test for ``mini_ork.web.control.launch_run``'s spawn shape.

The bug we guard against is a real macOS footgun:

- ``bin/mini-ork`` ships a ``#!/usr/bin/env python3`` shebang, so without an
  explicit interpreter prefix the kernel execs whatever ``python3`` is first
  on PATH (3.9 on macOS).
- The parent re-exec'd into the venv and set ``MINI_ORK_VENV_ACTIVE=1``;
  the child inherited that flag, skipped its own venv re-exec at
  bin/mini-ork:214, then died at the version check at bin/mini-ork:193-209.

The fix is two-fold: (a) prepend ``sys.executable`` to the argv, (b) pop
``MINI_ORK_VENV_ACTIVE`` from the child env so a child started some other
way (e.g. by a CI runner that hasn't re-exec'd) still re-execs into the
venv on its own. These tests pin the spawn shape; they monkeypatch
``subprocess.Popen`` so no real child runs.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

import pytest  # noqa: E402

from mini_ork.web import control  # noqa: E402


class _FakePopen:
    """Capture argv/env without spawning; assign a fake pid for the return dict."""

    last: dict = {}

    def __init__(self, argv, **kw):
        type(self).last = {
            "argv": argv,
            "env": kw.get("env", {}),
            "cwd": kw.get("cwd"),
            "stdout": kw.get("stdout"),
            "stderr": kw.get("stderr"),
            "stdin": kw.get("stdin"),
            "start_new_session": kw.get("start_new_session"),
        }
        self.pid = 999999


@pytest.fixture()
def home(tmp_path: Path) -> Path:
    h = tmp_path / ".mini-ork"
    h.mkdir(parents=True)
    return h


@pytest.fixture()
def fake_root(tmp_path: Path) -> Path:
    """A repo root containing a ``bin/mini-ork`` file (we never run it because
    ``subprocess.Popen`` is monkeypatched, but ``launch_run`` checks the file
    exists before spawning).
    """
    (tmp_path / "bin").mkdir(parents=True)
    (tmp_path / "bin" / "mini-ork").write_text(
        "#!/usr/bin/env python3\nimport sys; sys.exit(0)\n", encoding="utf-8"
    )
    return tmp_path


@pytest.fixture(autouse=True)
def _patch_popen(monkeypatch):
    """Subprocess is re-imported inside ``launch_run``, but Python resolves the
    local ``subprocess`` to the module object — so a module-level patch is
    visible through the function-local import. Restore after each test.
    """
    monkeypatch.setattr(subprocess, "Popen", _FakePopen)
    yield
    _FakePopen.last = {}


def test_launch_run_uses_sys_executable_and_passes_kickoff_path(
    home: Path, fake_root: Path, monkeypatch
) -> None:
    monkeypatch.setenv("MINI_ORK_ROOT", str(fake_root))

    out = control.launch_run(home, "code-fix", "# kickoff\n\nGoal: x\n", run_id="run-launch-1")

    assert out["ok"] is True, out
    argv = _FakePopen.last["argv"]
    assert argv[0] == sys.executable, argv
    assert argv[1].endswith("bin/mini-ork"), argv
    assert argv[2] == "run", argv
    assert argv[3] == "code-fix", argv
    assert Path(argv[4]).name == "run-launch-1.md", argv


def test_launch_run_strips_mini_ork_venv_active_from_child_env(
    home: Path, fake_root: Path, monkeypatch
) -> None:
    monkeypatch.setenv("MINI_ORK_ROOT", str(fake_root))
    # Parent is already venv-active (the typical case once the server has
    # re-exec'd via bin/mini-ork). The child must NOT inherit it; otherwise
    # bin/mini-ork:214 would skip its own venv re-exec.
    monkeypatch.setenv("MINI_ORK_VENV_ACTIVE", "1")

    out = control.launch_run(home, "code-fix", "# kickoff\n", run_id="run-launch-2")

    assert out["ok"] is True, out
    env = _FakePopen.last["env"]
    assert "MINI_ORK_VENV_ACTIVE" not in env, env


def test_launch_run_sets_run_id_home_root_in_child_env(
    home: Path, fake_root: Path, monkeypatch
) -> None:
    monkeypatch.setenv("MINI_ORK_ROOT", str(fake_root))
    monkeypatch.delenv("MINI_ORK_VENV_ACTIVE", raising=False)

    out = control.launch_run(home, "code-fix", "# kickoff\n", run_id="run-launch-3")

    assert out["ok"] is True, out
    env = _FakePopen.last["env"]
    assert env["MINI_ORK_RUN_ID"] == "run-launch-3", env
    assert env["MINI_ORK_HOME"] == str(home), env
    assert env["MINI_ORK_ROOT"] == str(fake_root), env


def test_launch_run_keeps_extra_env_overrides(home: Path, fake_root: Path, monkeypatch) -> None:
    monkeypatch.setenv("MINI_ORK_ROOT", str(fake_root))

    out = control.launch_run(
        home,
        "code-fix",
        "# kickoff\n",
        run_id="run-launch-4",
        extra_env={"MO_TARGET_CWD": "/tmp/target", "MO_OVERRIDE": "yes"},
    )

    assert out["ok"] is True, out
    env = _FakePopen.last["env"]
    assert env["MO_TARGET_CWD"] == "/tmp/target", env
    assert env["MO_OVERRIDE"] == "yes", env


def test_launch_run_returns_pid_log_and_kickoff_paths(home: Path, fake_root: Path, monkeypatch) -> None:
    monkeypatch.setenv("MINI_ORK_ROOT", str(fake_root))

    out = control.launch_run(home, "code-fix", "# kickoff\n", run_id="run-launch-5")

    assert out["ok"] is True, out
    assert out["run_id"] == "run-launch-5"
    assert out["recipe"] == "code-fix"
    assert out["pid"] == 999999  # the fake popen
    assert Path(out["kickoff_path"]).name == "run-launch-5.md"
    assert out["kickoff_path"].endswith("/runs-inbox/run-launch-5.md")
    assert out["log_path"].endswith("/runs-inbox/run-launch-5.launch.log")