"""RB-31: ``execute.log`` is written as the run goes, including recover attempts.

Hermetic unit coverage for ``mini_ork/cli/log_tee.py`` and the two call sites
the kickoff names (``cli/main.py``'s execute capture, ``recovery/planner.py``'s
in-process execute). No lifecycle, DB or subprocess run is started; the
SIGTERM test only exercises the tee's handler in a throwaway process.
"""

from __future__ import annotations

import contextlib
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from mini_ork.cli import log_tee

EXECUTE_LOG = "execute.log"
DRYRUN_LOG = "execute.dryrun.log"


# ── mechanism §1/§2: teed live, survives a raise ─────────────────────────────


def test_execute_prints_then_raises_keeps_the_line(tmp_path):
    """A dispatcher that dies mid-node still leaves its output in execute.log."""
    run_dir = tmp_path / "run"
    run_dir.mkdir()

    tee = log_tee.execute_tee(str(run_dir))
    with pytest.raises(RuntimeError):
        with tee:
            with contextlib.redirect_stdout(tee.stdout), \
                    contextlib.redirect_stderr(tee.stderr):
                print("node starting")
                print("failing node", file=sys.stderr)
                raise RuntimeError("dispatcher died")

    text = (run_dir / EXECUTE_LOG).read_text(encoding="utf-8")
    assert "node starting" in text
    assert "failing node" in text
    assert tee.closed is True


def test_log_is_opened_at_enter_and_line_flushed(tmp_path):
    """The file exists before the first write and a line lands without close."""
    run_dir = tmp_path / "run"
    run_dir.mkdir()

    with log_tee.execute_tee(str(run_dir)) as tee:
        path = run_dir / EXECUTE_LOG
        assert path.is_file(), "execute.log must exist when the tee is entered"
        tee.write("streamed-live-line\n")
        assert "streamed-live-line" in path.read_text(encoding="utf-8")


# ── the deadline early-return path ───────────────────────────────────────────


def test_deadline_early_return_still_leaves_execute_log(tmp_path):
    """The after-execute deadline check returns before any post-execute write."""
    run_dir = tmp_path / "run"
    run_dir.mkdir()

    with log_tee.execute_tee(str(run_dir)) as tee:
        tee.write("deadline_hit after execute\n")
        # The lifecycle returns here — no _write_execute_log() is reached.
        early_return_rc = 0
    assert early_return_rc == 0

    text = (run_dir / EXECUTE_LOG).read_text(encoding="utf-8")
    assert "deadline_hit after execute" in text


# ── dry-run sibling name is kept ─────────────────────────────────────────────


def test_dry_run_tees_to_the_dryrun_sibling(tmp_path, monkeypatch):
    monkeypatch.setenv("MINI_ORK_DRY_RUN", "1")
    run_dir = tmp_path / "run"
    run_dir.mkdir()

    with log_tee.execute_tee(str(run_dir)) as tee:
        tee.write("rehearsal-line\n")

    assert "rehearsal-line" in (run_dir / DRYRUN_LOG).read_text(encoding="utf-8")
    assert not (run_dir / EXECUTE_LOG).exists()


# ── SIGTERM flushes before the default behaviour ─────────────────────────────

_SIGTERM_CHILD = """
import sys, time
sys.path.insert(0, {repo!r})
from mini_ork.cli import log_tee
tee = log_tee.execute_tee({run_dir!r})
tee.__enter__()
tee.write("sigterm-partial-line")
time.sleep(60)
"""


def test_sigterm_flushes_the_log_before_death(tmp_path):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    repo = str(Path(log_tee.__file__).resolve().parents[2])
    script = _SIGTERM_CHILD.format(repo=repo, run_dir=str(run_dir))
    env = dict(os.environ)
    env.pop("MINI_ORK_DRY_RUN", None)
    env["PYTHONPATH"] = repo + os.pathsep + env.get("PYTHONPATH", "")

    proc = subprocess.Popen([sys.executable, "-c", script], env=env,
                            stderr=subprocess.PIPE, text=True)
    try:
        log = run_dir / EXECUTE_LOG
        deadline = time.time() + 15
        while time.time() < deadline:
            if log.is_file() or proc.poll() is not None:
                break
            time.sleep(0.05)
        assert log.is_file(), "child never opened execute.log"
        time.sleep(0.3)  # let the child install its handler and park
        proc.send_signal(signal.SIGTERM)
        rc = proc.wait(timeout=15)
        assert rc in (-signal.SIGTERM, 128 + signal.SIGTERM), (
            f"expected death by SIGTERM, got rc={rc}")
        assert "sigterm-partial-line" in log.read_text(encoding="utf-8")
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)
        if proc.stderr:
            proc.stderr.close()


# ── mechanism §3: recover attempt log + pointer line ─────────────────────────


def test_recover_execution_writes_attempt_log_and_pointer_line(tmp_path):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / EXECUTE_LOG).write_text("dead attempt output\n", encoding="utf-8")

    with log_tee.execute_tee(str(run_dir), recover=True) as tee:
        tee.write("recover attempt one\n")
    assert "recover attempt one" in (
        run_dir / "execute.recover-1.log").read_text(encoding="utf-8")
    live = (run_dir / EXECUTE_LOG).read_text(encoding="utf-8")
    assert live.startswith("dead attempt output")
    assert "recover attempt 1: see execute.recover-1.log" in live

    with log_tee.execute_tee(str(run_dir), recover=True) as tee:
        tee.write("recover attempt two\n")
    assert (run_dir / "execute.recover-2.log").is_file()
    live = (run_dir / EXECUTE_LOG).read_text(encoding="utf-8")
    assert "recover attempt 2: see execute.recover-2.log" in live


# ── the two declared call sites use the shared helper ────────────────────────


def test_lifecycle_execute_capture_uses_the_shared_tee():
    from mini_ork.cli import main as cli_main

    src = Path(cli_main.__file__).read_text(encoding="utf-8")
    assert "log_tee" in src
    assert "execute_stdout = io.StringIO()" not in src


def test_recover_path_uses_the_shared_tee():
    from mini_ork.recovery import planner

    src = Path(planner.__file__).read_text(encoding="utf-8")
    assert "log_tee" in src
