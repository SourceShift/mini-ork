"""Unit tests: ``mini_ork.recovery.healer.decide`` (bash parity halves removed; formerly vs ``lib/healer.sh``).

Every test drives the Python port with no mocks. The expected surface is the
deterministic contract of the port: in a test env the missing siblings
(memory-retrieve, memory-store, llm helpers, event emitter) collapse the
decision logic to the escalate-human early-exit branch.

Cases:

  (a) test_usage_error           — no args: rc=2 + stderr contains /Usage/.
  (b) test_missing_run_dir       — non-existent RUN_DIR: rc=3.
  (c) test_empty_run_dir         — empty tmp dir: rc=0 + escalate-human JSON.
  (d) test_logs_present          — worker.log with errors: rc=0 +
                                  escalate-human JSON.
  (e) test_reviewer_verdict      — verdict.json = REQUEST_CHANGES: rc=0 +
                                  escalate-human JSON.
  (f) test_escalate_json_loop    — loops four escalate cases asserting the
                                  exact escalate-human JSON line.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
from mini_ork.recovery import healer as hl


def _py(epic_id: str, run_dir: str) -> tuple[int, str, str]:
    return hl.decide(epic_id, run_dir, mini_ork_root=str(REPO))


_ESCALATE_JSON = (
    '{"lesson_id":null,"failure_class":"unknown",'
    '"recovery_action":"escalate-human","recovery_args":{},'
    '"matched":false}\n'
)


# ─────────────────────────────────────────────────────────────────────────────
# (a) usage error — no args → rc=2 + /Usage/ in stderr
# ─────────────────────────────────────────────────────────────────────────────
def test_usage_error():
    py_rc, py_out, py_err = hl.decide("", "")
    assert py_rc == 2
    assert py_out == ""
    assert "Usage" in py_err
    assert "<epic_id>" in py_err


# ─────────────────────────────────────────────────────────────────────────────
# (b) missing run_dir → rc=3 + stderr "[healer] run_dir not found"
# ─────────────────────────────────────────────────────────────────────────────
def test_missing_run_dir(tmp_path: Path):
    bogus = str(tmp_path / "ghost")
    py_rc, py_out, py_err = _py("EPIC-X", bogus)
    assert py_rc == 3
    assert py_out == ""
    assert "run_dir not found" in py_err


# ─────────────────────────────────────────────────────────────────────────────
# (c) empty run_dir escalates — emits escalate-human JSON
# ─────────────────────────────────────────────────────────────────────────────
def test_empty_run_dir_escalates(tmp_path: Path):
    run = tmp_path / "empty"
    run.mkdir()
    py_rc, py_out, py_err = _py("EPIC-EMPTY", str(run))
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
    assert py_rc == 0
    parsed = json.loads(py_out)
    assert parsed["recovery_action"] == "escalate-human"


# ─────────────────────────────────────────────────────────────────────────────
# (f) exact escalate-human JSON across the four escalate cases
# ─────────────────────────────────────────────────────────────────────────────
def _seed_empty(d: Path) -> None:
    """Empty run_dir — nothing log-shaped; escalate-human branch."""
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
def test_escalate_json_loop(tmp_path: Path, label: str, builder):
    run = tmp_path / label
    run.mkdir()
    builder(run)
    py_rc, py_out, py_err = _py("EPIC-PARITY", str(run))
    assert py_rc == 0
    # All four escalate cases share the same escalate-human JSON shape
    # (and identical bytes) since the port never reaches the LLM branch
    # in this env.
    assert py_out == _ESCALATE_JSON
