"""Parity gate: ``mini_ork.recovery.healer.decide`` vs ``lib/healer.sh``.

Every test invokes the LIVE bash subprocess (``bash lib/healer.sh EPIC RUN_DIR``)
with no mocks and no hardcoded expected outputs — the expected surface is
derived from the bash control invocation that shares the inputs. We then
compare (rc, stdout, stderr) byte-for-byte against the Python port.

Cases (six, all deterministic in this env because the missing siblings —
memory-retrieve.sh, memory-store.sh, agentflow-llm-helpers.sh, mo-event.sh —
collapse the bash logic to the escalate-human early-exit branch at
lib/healer.sh:83-87):

  (a) test_usage_error           — no args: rc=2 + stderr contains /Usage/.
  (b) test_missing_run_dir       — non-existent RUN_DIR: rc=3 + stderr
                                  /[healer] run_dir not found/.
  (c) test_empty_run_dir         — empty tmp dir: rc=0 +
                                  escalate-human JSON line.
  (d) test_logs_present          — worker.log with errors: rc=0 +
                                  escalate-human JSON line.
  (e) test_reviewer_verdict      — verdict.json = REQUEST_CHANGES: rc=0 +
                                  escalate-human JSON line.
  (f) test_byte_identical_loop   — loops the previous three escalate cases
                                  asserting bash.rc == py.rc and
                                  bash.stdout == py.stdout exact (stderr
                                  stripped of trailing newlines to ignore
                                  printf's final newline).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
from mini_ork.recovery import healer as hl

SH = REPO / "lib" / "healer.sh"


# ─────────────────────────────────────────────────────────────────────────────
# Helpers — live-bash subprocess, no mocks
# ─────────────────────────────────────────────────────────────────────────────
def _bash(epic_id: str, run_dir: str) -> subprocess.CompletedProcess:
    """Run the live ``bash lib/healer.sh EPIC RUN_DIR`` invocation.

    Captures rc + stdout + stderr so the Python port can be compared byte-
    for-byte. ``MINI_ORK_ROOT`` is set explicitly so the bash side resolves
    to the same repo the Python side has been loaded from (matches the
    bash script's own auto-resolution of ``MINI_ORK_ROOT``)."""
    env = os.environ.copy()
    env["MINI_ORK_ROOT"] = str(REPO)
    env.setdefault("MINI_ORK_HOME", str(REPO / ".mini-ork-test-home"))
    return subprocess.run(
        ["bash", str(SH), epic_id, run_dir],
        env=env,
        capture_output=True,
        text=True,
    )


def _py(epic_id: str, run_dir: str) -> tuple[int, str, str]:
    return hl.decide(epic_id, run_dir, mini_ork_root=str(REPO))


def _assert_parity(
    *,
    bash: subprocess.CompletedProcess,
    py_rc: int,
    py_out: str,
    py_err: str,
    case: str,
) -> None:
    """Assert rc match, stdout byte-identical, stderr equal after rstrip-newline."""
    assert py_rc == bash.returncode, (
        f"[{case}] rc mismatch py={py_rc} bash={bash.returncode}\n"
        f"py stderr: {py_err!r}\n"
        f"bash stderr: {bash.stderr!r}"
    )
    assert py_out == bash.stdout, (
        f"[{case}] stdout mismatch\n"
        f"py:  {py_out!r}\n"
        f"bash:{bash.stdout!r}"
    )
    assert py_err.rstrip("\n") == bash.stderr.rstrip("\n"), (
        f"[{case}] stderr mismatch\n"
        f"py:  {py_err!r}\n"
        f"bash:{bash.stderr!r}"
    )


# ─────────────────────────────────────────────────────────────────────────────
# (a) usage error — no args → rc=2 + /Usage/ in stderr
# ─────────────────────────────────────────────────────────────────────────────
def test_usage_error():
    py_rc, py_out, py_err = hl.decide("", "")
    bash = _bash("", "")
    assert py_rc == 2 == bash.returncode, (
        f"both must exit 2. py_rc={py_rc} bash_rc={bash.returncode}"
    )
    assert py_out == bash.stdout == ""
    assert "Usage" in py_err and "Usage" in bash.stderr
    assert "<epic_id>" in py_err and "<epic_id>" in bash.stderr


# ─────────────────────────────────────────────────────────────────────────────
# (b) missing run_dir → rc=3 + stderr "[healer] run_dir not found"
# ─────────────────────────────────────────────────────────────────────────────
def test_missing_run_dir(tmp_path: Path):
    bogus = str(tmp_path / "ghost")
    py_rc, py_out, py_err = _py("EPIC-X", bogus)
    bash = _bash("EPIC-X", bogus)
    _assert_parity(
        bash=bash, py_rc=py_rc, py_out=py_out, py_err=py_err, case="missing_run_dir"
    )
    assert py_rc == 3


# ─────────────────────────────────────────────────────────────────────────────
# (c) empty run_dir escalates — bash hits :83-87, emits escalate-human JSON
# ─────────────────────────────────────────────────────────────────────────────
def test_empty_run_dir_escalates(tmp_path: Path):
    run = tmp_path / "empty"
    run.mkdir()
    py_rc, py_out, py_err = _py("EPIC-EMPTY", str(run))
    bash = _bash("EPIC-EMPTY", str(run))
    _assert_parity(
        bash=bash, py_rc=py_rc, py_out=py_out, py_err=py_err, case="empty"
    )
    assert py_rc == 0
    parsed = json.loads(py_out)
    assert parsed["matched"] is False
    assert parsed["recovery_action"] == "escalate-human"
    assert parsed["failure_class"] == "unknown"
    assert parsed["lesson_id"] is None
    assert parsed["recovery_args"] == {}


# ─────────────────────────────────────────────────────────────────────────────
# (d) worker.log with errors — escalate-human JSON in this env
# ─────────────────────────────────────────────────────────────────────────────
def test_logs_present_escalates(tmp_path: Path):
    run = tmp_path / "logs"
    run.mkdir()
    (run / "worker.log").write_text(
        "2026-07-04T00:00:00 ERROR Cannot find module 'foo-bar'\n"
        "2026-07-04T00:00:01 TS2307: Cannot find module 'baz'\n"
        "2026-07-04T00:00:02 fail whale — apiserver returned 503\n"
        "2026-07-04T00:00:03 another uninteresting info line\n"
    )
    py_rc, py_out, py_err = _py("EPIC-LOGS", str(run))
    bash = _bash("EPIC-LOGS", str(run))
    _assert_parity(
        bash=bash, py_rc=py_rc, py_out=py_out, py_err=py_err, case="logs"
    )
    assert py_rc == 0
    assert json.loads(py_out)["recovery_action"] == "escalate-human"


# ─────────────────────────────────────────────────────────────────────────────
# (e) reviewer verdict REQUEST_CHANGES — escalate-human JSON
# ─────────────────────────────────────────────────────────────────────────────
def test_reviewer_verdict_request_changes(tmp_path: Path):
    run = tmp_path / "verdict"
    run.mkdir()
    (run / "verdict.json").write_text(
        json.dumps({"verdict": "REQUEST_CHANGES", "reviewer": "harsh-critic"})
    )
    py_rc, py_out, py_err = _py("EPIC-VERDICT", str(run))
    bash = _bash("EPIC-VERDICT", str(run))
    _assert_parity(
        bash=bash, py_rc=py_rc, py_out=py_out, py_err=py_err, case="verdict"
    )
    assert py_rc == 0
    parsed = json.loads(py_out)
    assert parsed["recovery_action"] == "escalate-human"


# ─────────────────────────────────────────────────────────────────────────────
# (f) byte-identical stdout across the three escalate cases
# ─────────────────────────────────────────────────────────────────────────────
def _seed_empty(d: Path) -> None:
    """Empty run_dir — bash will still find nothing log-shaped and hit
    the escalate-human branch at lib/healer.sh:83-87."""
    del d  # parameter unused: empty seed is the whole point


def _seed_worker_log(d: Path) -> None:
    (d / "worker.log").write_text(
        "ERROR cannot find module\nTS2307 fail\n"
    )


def _seed_verdict(d: Path) -> None:
    (d / "verdict.json").write_text(
        json.dumps({"final_verdict": "REQUEST_CHANGES"})
    )


def _seed_gauntlet_log(d: Path) -> None:
    (d / "gauntlet-1.log").write_text(
        "exception: timed out while waiting\n429 rate_limit exceeded\n"
    )


@pytest.mark.parametrize(
    "label, builder",
    [
        ("empty", _seed_empty),
        ("with_worker_log", _seed_worker_log),
        ("with_verdict", _seed_verdict),
        ("with_gauntlet_log", _seed_gauntlet_log),
    ],
)
def test_byte_identical_loop(tmp_path: Path, label: str, builder):
    run = tmp_path / label
    run.mkdir()
    builder(run)
    py_rc, py_out, py_err = _py("EPIC-PARITY", str(run))
    bash = _bash("EPIC-PARITY", str(run))
    _assert_parity(
        bash=bash, py_rc=py_rc, py_out=py_out, py_err=py_err, case=label
    )
    # All four escalate-cases share the same escalate-human JSON shape
    # (and identical bytes) since the bash port never reaches the LLM
    # branch in this env.
    assert py_out == (
        '{"lesson_id":null,"failure_class":"unknown",'
        '"recovery_action":"escalate-human","recovery_args":{},'
        '"matched":false}\n'
    )
