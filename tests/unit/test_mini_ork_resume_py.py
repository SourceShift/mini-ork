"""Unit tests: ``mini_ork.cli.resume`` (bash parity halves removed; formerly vs ``bin/mini-ork-resume``).

Each test seeds run-dir state (missing / no sentinel / sentinel present)
and runs the scenario through the Python CLI, asserting rc / stdout /
stderr. For the sentinel-present case, the audit jsonl row is parsed back
through ``json.loads`` and checked structurally (keys, approver, run_id,
sentinel_payload round-trip, resumed_at UTC ISO Z format, single trailing
newline).

The Python port is invoked via subprocess (``python -m mini_ork.cli.resume``)
so stdout/stderr match the real CLI surface.

Cases (6):
  (1) no args         — rc=2 + usage on stdout.
  (2) --help          — rc=0 + usage on stdout.
  (3) -h              — rc=0 + usage on stdout.
  (4) missing RUN_DIR — rc=1 + stderr '... run dir not found: ...'.
  (5) no sentinel     — rc=0 + stderr '... no cost-pause sentinel ...'.
  (6) sentinel present — rc=0 + sentinel removed + jsonl row appended.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

PY_MOD = "mini_ork.cli.resume"

# Sentinel body shape — cost_pause writes JSON with a trailing newline.
SENTINEL_BODY = (
    '{"threshold_usd":25.0,"spent_usd":51.5,'
    '"created_at":"2026-07-05T18:00:00Z","run_id":"RUN_X"}\n'
)
TS_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────
def _py_resume(args, tmp_run_dir, env_overrides=None, user=None):
    """Run mini_ork.cli.resume via subprocess; return (rc, stdout, stderr,
    jsonl_lines, sentinel_exists)."""
    env = dict(os.environ)
    if env_overrides:
        env.update(env_overrides)
    env["MINI_ORK_HOME"] = str(tmp_run_dir)
    if user is _UNSET:
        env.pop("USER", None)
    elif user is not None:
        env["USER"] = user
    # ``sys.executable``, not a bare ``python3``: the project floor is 3.11 and
    # a shell ``python3`` can be older (3.9 here), which fails on dataclass
    # ``slots=`` before the case under test even runs.
    cmd = [sys.executable, "-m", PY_MOD] + list(args)
    r = subprocess.run(cmd, env=env, capture_output=True, text=True,
                       cwd=str(REPO))
    run_id = args[0] if args else "unused"
    approvals = tmp_run_dir / "runs" / run_id / ".cost-pause-approvals.jsonl"
    jsonl_lines = []
    if approvals.is_file():
        jsonl_lines = approvals.read_text().splitlines()
    sentinel = tmp_run_dir / "runs" / run_id / ".cost-pause"
    return (r.returncode, r.stdout, r.stderr, jsonl_lines, sentinel.exists())


_UNSET = object()  # sentinel for "remove USER from env"


def _seed_run_dir(tmp_path, run_id, *, with_sentinel):
    """Create a run_dir seeded with a sentinel (or empty)."""
    run_dir = tmp_path / "runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    if with_sentinel:
        (run_dir / ".cost-pause").write_text(SENTINEL_BODY)


# ─────────────────────────────────────────────────────────────────────────────
# Cases
# ─────────────────────────────────────────────────────────────────────────────
def test_no_args_exits_2_with_usage(tmp_path):
    py_rc, py_out, py_err, _, _ = _py_resume([], tmp_path)
    assert py_rc == 2
    assert "Usage: mini-ork resume" in py_out
    assert py_err == ""


def test_help_long_flag(tmp_path):
    py_rc, py_out, py_err, _, _ = _py_resume(["--help"], tmp_path)
    assert py_rc == 0
    assert "Usage: mini-ork resume" in py_out
    assert py_err == ""


def test_help_short_flag(tmp_path):
    py_rc, py_out, py_err, _, _ = _py_resume(["-h"], tmp_path)
    assert py_rc == 0
    assert "Usage: mini-ork resume" in py_out
    assert py_err == ""


def test_missing_run_dir(tmp_path):
    py_rc, py_out, py_err, _, _ = _py_resume(["run-missing"], tmp_path)
    assert py_rc == 1
    assert py_out == ""
    assert "[mini-ork-resume] run dir not found:" in py_err
    assert "run-missing" in py_err


def test_no_sentinel(tmp_path):
    _seed_run_dir(tmp_path, "run-empty", with_sentinel=False)
    py_rc, py_out, py_err, py_lines, py_sent = _py_resume(
        ["run-empty"], tmp_path)
    assert py_rc == 0
    assert py_out == ""
    assert "[mini-ork-resume] no cost-pause sentinel for run-empty" in py_err
    assert py_lines == []
    assert py_sent is False


def test_sentinel_present_with_user(tmp_path):
    py_dir = tmp_path / "py"
    _seed_run_dir(py_dir, "run-x", with_sentinel=True)
    py_rc, py_out, py_err, py_lines, py_sent = _py_resume(
        ["run-x"], py_dir, user="alice")

    assert py_rc == 0
    assert "[mini-ork-resume] resumed run-x (approver=alice" in py_out
    assert ".cost-pause-approvals.jsonl" in py_out
    assert py_err == ""
    assert py_sent is False
    assert len(py_lines) == 1

    py_row = py_lines[0]
    py_parsed = json.loads(py_row)

    assert set(py_parsed.keys()) == {"resumed_at", "approver", "run_id",
                                     "sentinel_payload"}
    assert py_parsed["approver"] == "alice"
    assert py_parsed["run_id"] == "run-x"
    assert py_parsed["sentinel_payload"] == json.loads(SENTINEL_BODY)
    assert TS_PATTERN.match(py_parsed["resumed_at"]), \
        f"bad timestamp {py_parsed['resumed_at']!r}"

    # The file ends with exactly one newline (no double-newline).
    py_approvals = (py_dir / "runs" / "run-x" /
                    ".cost-pause-approvals.jsonl").read_bytes()
    assert py_approvals.endswith(b"\n") and not py_approvals.endswith(b"\n\n")


# ─────────────────────────────────────────────────────────────────────────────
# Per-run budget parks (``.budget-exhausted``)
#
# The dispatch gate refuses the next node and prints ``mini-ork resume <run>``
# as the unblock, so these pin the contract that command makes: resume parks
# nothing, it re-asks the budget policy and clears the marker only when the run
# fits — and a run never becomes unlimited because the ceiling was removed.
# ─────────────────────────────────────────────────────────────────────────────
import sqlite3  # noqa: E402 — grouped with the cases that need a ledger


def _seed_budget_park(tmp_path, run_id, *, spent, budget=None):
    """A run dir parked by its ceiling + the ledger those dollars came from."""
    run_dir = tmp_path / "runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    marker = {"spent_usd": spent, "budget_usd": budget,
              "detail": f"spent ${spent:.2f} of a ${budget or 0:.2f} budget",
              "recorded_at": 1790000000}
    (run_dir / ".budget-exhausted").write_text(json.dumps(marker) + "\n")
    db = tmp_path / "state.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE IF NOT EXISTS llm_calls "
                "(run_id TEXT, cost_usd REAL)")
    con.execute("INSERT INTO llm_calls VALUES (?, ?)", (run_id, spent))
    con.commit()
    con.close()
    return run_dir


def _budget_env(ceiling):
    return {"MINI_ORK_DB": "", "MO_BUDGET_POLICY": "",
            "MO_RUN_BUDGET_USD": ceiling}


def test_budget_park_resumes_once_the_ceiling_is_raised(tmp_path):
    run_dir = _seed_budget_park(tmp_path, "run-b", spent=0.84, budget=0.5)
    rc, out, err, lines, _sent = _py_resume(
        ["run-b", "--no-restart"], tmp_path,
        env_overrides=_budget_env("12"), user="alice")

    assert rc == 0, err
    assert "resumed run-b (approver=alice" in out
    assert "$0.84 of a $12.00 budget" in out
    assert err == ""
    assert not (run_dir / ".budget-exhausted").exists()
    assert len(lines) == 1
    row = json.loads(lines[0])
    assert row["sentinel_payload"]["marker"] == ".budget-exhausted"
    assert row["sentinel_payload"]["payload"]["spent_usd"] == 0.84


def test_budget_park_refuses_while_still_over_the_ceiling(tmp_path):
    run_dir = _seed_budget_park(tmp_path, "run-c", spent=0.84, budget=0.5)
    rc, out, err, lines, _sent = _py_resume(
        ["run-c", "--no-restart"], tmp_path,
        env_overrides=_budget_env("0.5"))

    assert rc == 1
    assert out == ""
    assert "still parked" in err and "$0.50" in err
    assert lines == []
    assert (run_dir / ".budget-exhausted").is_file()


def test_budget_park_refuses_when_the_ceiling_was_removed(tmp_path):
    """Unsetting the knob is not a way to un-park a run."""
    run_dir = _seed_budget_park(tmp_path, "run-d", spent=0.84, budget=0.5)
    rc, _out, err, _lines, _sent = _py_resume(
        ["run-d", "--no-restart"], tmp_path,
        env_overrides=_budget_env(""))

    assert rc == 1
    assert "no per-run budget is declared" in err
    assert "does not make a parked run unlimited" in err
    assert (run_dir / ".budget-exhausted").is_file()


def test_a_run_with_neither_sentinel_nor_marker_still_warns(tmp_path):
    """The budget branch must not swallow the pre-existing no-sentinel path."""
    _seed_run_dir(tmp_path, "run-plain", with_sentinel=False)
    rc, out, err, _lines, _sent = _py_resume(
        ["run-plain", "--no-restart"], tmp_path,
        env_overrides=_budget_env("12"))

    assert rc == 0
    assert out == ""
    assert "no cost-pause sentinel for run-plain" in err
