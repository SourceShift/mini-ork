"""The detached post-run reflect enforces its own deadline (RBF-20b).

Inline reflect is bounded by the parent's ``subprocess.run(timeout=...)``; the
detached post-run child has no such parent, so ``reflect.main`` arms
``SIGALRM`` from ``MO_REFLECT_DEADLINE_S`` and the lifecycle passes the computed
bound into the child's env.

Hermetic: the subprocess tests get a fresh env pointed at a tmp home with no
mini-ork run identity (audit RB-04), and the spawn test drives the lifecycle in
dry-run mode with ``subprocess.Popen`` stubbed, so no real DB is touched.
"""
from __future__ import annotations

import io
import os
import re
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path

import mini_ork

REPO_ROOT = Path(mini_ork.__file__).resolve().parents[1]
DEADLINE_LINE_RE = re.compile(
    r"^reflect: deadline (?P<n>[0-9]+)s reached; stopping$", re.MULTILINE
)

_KICKOFF = """# Spec-test kickoff

## Goal

Exercise the post-run reflect tail of a dry run.

## Success criteria

- The run completes.
"""

_REFLECT_MODULE = "mini_ork.cli.reflect"


# ── child-process harness ────────────────────────────────────────────────────
def _child_env(tmp_path: Path, extra: dict[str, str] | None = None) -> dict[str, str]:
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "PYTHONPATH": str(REPO_ROOT),
        "PYTHONDONTWRITEBYTECODE": "1",
        "HOME": str(tmp_path),
        "TMPDIR": str(tmp_path),
        "MINI_ORK_HOME": str(tmp_path / "home"),
        "MINI_ORK_DB": str(tmp_path / "home" / "state.db"),
        "MINI_ORK_DRY_RUN": "1",
    }
    if extra:
        env.update(extra)
    return env


def _run_child(
    tmp_path: Path,
    script: str,
    extra_env: dict[str, str] | None = None,
    timeout: float = 25.0,
) -> tuple[subprocess.CompletedProcess, float]:
    started = time.monotonic()
    proc = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        env=_child_env(tmp_path, extra_env),
        cwd=str(tmp_path),
        timeout=timeout,
    )
    return proc, time.monotonic() - started


# ── 1. the alarm fires, exits 124, and prints the deadline line ──────────────
def test_deadline_fires_with_line_and_exit_124(tmp_path):
    script = """
import time
import mini_ork.cli.reflect as reflect
reflect._arm_reflect_deadline()
time.sleep(30)
print("SLEPT")
"""
    proc, elapsed = _run_child(
        tmp_path, script, extra_env={"MO_REFLECT_DEADLINE_S": "1"}
    )
    assert proc.returncode == 124, (
        f"expected rc 124, got {proc.returncode}\n"
        f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    )
    matches = list(DEADLINE_LINE_RE.finditer(proc.stderr))
    assert len(matches) == 1, (
        f"expected one deadline line, got "
        f"{[m.group(0) for m in matches]}\nstderr:\n{proc.stderr}"
    )
    assert matches[0].group("n") == "1"
    assert "SLEPT" not in proc.stdout, "the 30 s sleep completed — alarm never fired"
    assert elapsed < 8.0, f"deadline took {elapsed:.1f}s to fire"


# ── 2. no MO_REFLECT_DEADLINE_S → no alarm (regression) ─────────────────────
def test_no_deadline_env_installs_no_alarm(tmp_path):
    script = """
import signal
import mini_ork.cli.reflect as reflect
armed = reflect._arm_reflect_deadline()
print("ARMED", armed)
print("REMAINING", signal.alarm(0))
"""
    proc, _ = _run_child(tmp_path, script)
    assert proc.returncode == 0, proc.stderr
    assert "ARMED None" in proc.stdout, proc.stdout
    assert "REMAINING 0" in proc.stdout, (
        f"an alarm was installed with no MO_REFLECT_DEADLINE_S: {proc.stdout}"
    )
    assert not DEADLINE_LINE_RE.search(proc.stderr), proc.stderr


# ── 3. the post-run spawn carries the computed bound ────────────────────────
@contextmanager
def _scrubbed_env(**overrides):
    """Drop every mini-ork key so a leaked run identity cannot be adopted."""
    saved = dict(os.environ)
    for key in list(os.environ):
        if key.startswith(("MINI_ORK_", "MO_")):
            del os.environ[key]
    os.environ.update(overrides)
    try:
        yield
    finally:
        os.environ.clear()
        os.environ.update(saved)


class _FakeChild:
    def __init__(self, argv, kwargs):
        self.argv = [str(a) for a in argv]
        self.kwargs = dict(kwargs)
        self.pid = os.getpid()
        self.stdout = io.StringIO("")
        self.stderr = io.StringIO("")

    def poll(self, *_a, **_k):
        return 0

    def wait(self, *_a, **_k):
        return 0

    def communicate(self, *_a, **_k):
        return ("", "")

    def kill(self, *_a, **_k):
        pass

    terminate = kill


@contextmanager
def _spy_popen():
    fakes: list[_FakeChild] = []
    original = subprocess.Popen

    def spy(args=None, *rest, **kwargs):
        if isinstance(args, (list, tuple)) and any(
                _REFLECT_MODULE in str(part) for part in args):
            child = _FakeChild(args, kwargs)
            fakes.append(child)
            return child
        return original(args, *rest, **kwargs)

    subprocess.Popen = spy
    try:
        yield fakes
    finally:
        subprocess.Popen = original


def test_post_run_spawn_env_carries_the_bound(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    kickoff = tmp_path / "kickoff.md"
    kickoff.write_text(_KICKOFF, encoding="utf-8")

    from mini_ork.cli import main as mo_main
    from mini_ork.cli.main import _reflect_timeout

    db = str(home / "state.db")
    with _scrubbed_env(
        MINI_ORK_DRY_RUN="1",
        MINI_ORK_HOME=str(home),
        MINI_ORK_DB=db,
        MO_AUTO_REPAIR="0",
        MO_RUBRIC="0",
    ):
        expected_bound, _why = _reflect_timeout(db)
        with _spy_popen() as fakes:
            rc = mo_main.main(["run", "code-fix", str(kickoff)])

    assert rc == 0, f"dry-run lifecycle failed (rc={rc})"
    assert fakes, "the post-run reflect child was not spawned through Popen"
    child_env = fakes[0].kwargs.get("env") or {}
    assert child_env.get("MO_REFLECT_POST_RUN") == "1", (
        f"spawned a child that is not the post-run reflect: {child_env}"
    )
    assert child_env.get("MO_REFLECT_DEADLINE_S") == str(expected_bound), (
        "the detached child receives no deadline equal to the computed bound "
        f"(expected {expected_bound!r}, got "
        f"{child_env.get('MO_REFLECT_DEADLINE_S')!r})"
    )
