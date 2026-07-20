"""Parity gate: ``mini_ork.cli.resume`` vs ``bin/mini-ork-resume``.

Each test seeds the SAME run-dir state (missing / no sentinel / sentinel
present) and runs the scenario through both the LIVE bash dispatcher and
the Python port, asserting rc / stdout / stderr match byte-for-byte. For
the sentinel-present case, the audit jsonl row is parsed back through
``json.loads`` and compared structurally (modulo the live ``resumed_at``
timestamp, which is non-deterministic on the bash side; we substitute a
placeholder to confirm byte-equality of the printf format + sentinel
embedding + trailing newline, and verify the timestamp format
separately).

No mocks, no hardcoded expected outputs — the expected output is always
the live bash control invocation. This is the strangler-fig invariant:
the bash script stays in place; the Python port must match its
observable behavior exactly so the migration can proceed module-by-module
without breaking operator workflows.

The Python port is invoked via subprocess (``python -m mini_ork.cli.resume``)
so stdout/stderr match the real CLI surface, mirroring the bash path.

Cases (6, at the kickoff's >=6 floor):
  (1) no args         — rc=2 + usage on stdout.
  (2) --help          — rc=0 + usage on stdout.
  (3) -h              — rc=0 + usage on stdout.
  (4) missing RUN_DIR — rc=1 + stderr '... run dir not found: ...'.
  (5) no sentinel     — rc=0 + stderr '... no cost-pause sentinel ...'.
  (6) sentinel present — rc=0 + sentinel removed + jsonl row appended
                          (resumed_at UTC ISO Z; approver honors USER /
                          'unknown'; sentinel_payload round-trips; row
                          byte-equivalent to bash modulo timestamp).
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

BASH = REPO / "bin" / "mini-ork-resume"
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
def _which_tools() -> None:
    for tool in ("bash", "python3"):
        if not shutil.which(tool):
            pytest.skip(f"{tool} not on PATH")
    if not BASH.exists():
        pytest.skip(f"missing bin/mini-ork-resume at {BASH}")


def _bash_resume(args, tmp_run_dir, env_overrides=None, user=None):
    """Run bin/mini-ork-resume via subprocess; return (rc, stdout, stderr,
    jsonl_lines, sentinel_exists)."""
    env = dict(os.environ)
    if env_overrides:
        env.update(env_overrides)
    env["MINI_ORK_HOME"] = str(tmp_run_dir)
    if user is _UNSET:
        env.pop("USER", None)
    elif user is not None:
        env["USER"] = user
    cmd = ["bash", str(BASH)] + list(args)
    r = subprocess.run(cmd, env=env, capture_output=True, text=True,
                       cwd=str(REPO))
    approvals = tmp_run_dir / "runs" / (args[0] if args else "unused") \
        / ".cost-pause-approvals.jsonl"
    jsonl_lines = []
    if approvals.is_file():
        jsonl_lines = approvals.read_text().splitlines()
    sentinel = tmp_run_dir / "runs" / (args[0] if args else "unused") \
        / ".cost-pause"
    return (r.returncode, r.stdout, r.stderr, jsonl_lines, sentinel.exists())


def _py_resume(args, tmp_run_dir, env_overrides=None, user=None):
    """Run mini_ork.cli.resume via subprocess; return same shape."""
    env = dict(os.environ)
    if env_overrides:
        env.update(env_overrides)
    env["MINI_ORK_HOME"] = str(tmp_run_dir)
    if user is _UNSET:
        env.pop("USER", None)
    elif user is not None:
        env["USER"] = user
    cmd = ["python3", "-m", PY_MOD] + list(args)
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


def _normalize_timestamp_in_row(row: str, ts_subst: str = "TS") -> str:
    """Replace the resumed_at value with a placeholder so two rows can be
    compared modulo their non-deterministic timestamp."""
    return re.sub(
        r'"resumed_at":"[^"]+"',
        f'"resumed_at":"{ts_subst}"',
        row,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Cases
# ─────────────────────────────────────────────────────────────────────────────
def test_no_args_exits_2_with_usage(tmp_path):
    _which_tools()
    bash_rc, bash_out, bash_err, _, _ = _bash_resume([], tmp_path)
    py_rc, py_out, py_err, _, _ = _py_resume([], tmp_path)
    assert bash_rc == 2 == py_rc
    assert bash_out == py_out
    assert "Usage: mini-ork resume" in py_out
    assert bash_err == py_err == ""


def test_help_long_flag(tmp_path):
    _which_tools()
    bash_rc, bash_out, bash_err, _, _ = _bash_resume(["--help"], tmp_path)
    py_rc, py_out, py_err, _, _ = _py_resume(["--help"], tmp_path)
    assert bash_rc == 0 == py_rc
    assert bash_out == py_out
    assert "Usage: mini-ork resume" in py_out
    assert bash_err == py_err == ""


def test_help_short_flag(tmp_path):
    _which_tools()
    bash_rc, bash_out, bash_err, _, _ = _bash_resume(["-h"], tmp_path)
    py_rc, py_out, py_err, _, _ = _py_resume(["-h"], tmp_path)
    assert bash_rc == 0 == py_rc
    assert bash_out == py_out
    assert "Usage: mini-ork resume" in py_out
    assert bash_err == py_err == ""


def test_missing_run_dir(tmp_path):
    _which_tools()
    bash_rc, bash_out, bash_err, _, _ = _bash_resume(
        ["run-missing"], tmp_path)
    py_rc, py_out, py_err, _, _ = _py_resume(
        ["run-missing"], tmp_path)
    assert bash_rc == 1 == py_rc
    assert bash_out == py_out == ""
    assert bash_err == py_err
    assert "[mini-ork-resume] run dir not found:" in py_err
    assert "run-missing" in py_err


def test_no_sentinel(tmp_path):
    _which_tools()
    _seed_run_dir(tmp_path, "run-empty", with_sentinel=False)
    bash_rc, bash_out, bash_err, bash_lines, bash_sent = _bash_resume(
        ["run-empty"], tmp_path)
    py_rc, py_out, py_err, py_lines, py_sent = _py_resume(
        ["run-empty"], tmp_path)
    assert bash_rc == 0 == py_rc
    assert bash_out == py_out == ""
    assert bash_err == py_err
    assert "[mini-ork-resume] no cost-pause sentinel for run-empty" in py_err
    assert bash_lines == py_lines == []
    assert bash_sent is False and py_sent is False


def test_sentinel_present_with_user(tmp_path):
    _which_tools()
    bash_dir = tmp_path / "bash"
    py_dir = tmp_path / "py"
    _seed_run_dir(bash_dir, "run-x", with_sentinel=True)
    _seed_run_dir(py_dir, "run-x", with_sentinel=True)
    bash_rc, bash_out, bash_err, bash_lines, bash_sent = _bash_resume(
        ["run-x"], bash_dir, user="alice")
    py_rc, py_out, py_err, py_lines, py_sent = _py_resume(
        ["run-x"], py_dir, user="alice")

    # stdout contains the audit path; bash and py point at different
    # tmp dirs, so substitute the path back to a placeholder for parity.
    def _norm_path(s: str) -> str:
        return re.sub(r"/[^ \n]*\.cost-pause-approvals\.jsonl",
                      "<APPROVALS>", s)
    assert bash_rc == 0 == py_rc
    assert _norm_path(bash_out) == _norm_path(py_out)
    assert "[mini-ork-resume] resumed run-x (approver=alice" in py_out
    assert bash_err == py_err == ""
    assert bash_sent is False and py_sent is False
    assert len(bash_lines) == 1 and len(py_lines) == 1

    bash_row = bash_lines[0]
    py_row = py_lines[0]

    # Both rows parse as valid JSON.
    bash_parsed = json.loads(bash_row)
    py_parsed = json.loads(py_row)

    for parsed in (bash_parsed, py_parsed):
        assert set(parsed.keys()) == {"resumed_at", "approver", "run_id",
                                       "sentinel_payload"}
        assert parsed["approver"] == "alice"
        assert parsed["run_id"] == "run-x"
        assert parsed["sentinel_payload"] == json.loads(SENTINEL_BODY)
        assert TS_PATTERN.match(parsed["resumed_at"]), \
            f"bad timestamp {parsed['resumed_at']!r}"

    # Modulo the timestamp, the rows must be byte-identical (printf format,
    # sentinel embedding, trailing newline).
    assert _normalize_timestamp_in_row(bash_row) == \
        _normalize_timestamp_in_row(py_row)
    # splitlines() proves the file ends with a newline (otherwise the
    # last partial token wouldn't be a complete line); also verify the
    # raw file ends with exactly one newline (no double-newline).
    bash_approvals = (tmp_path / "bash" / "runs" / "run-x" /
                      ".cost-pause-approvals.jsonl").read_bytes()
    py_approvals = (tmp_path / "py" / "runs" / "run-x" /
                    ".cost-pause-approvals.jsonl").read_bytes()
    assert bash_approvals.endswith(b"\n") and not bash_approvals.endswith(
        b"\n\n")
    assert py_approvals.endswith(b"\n") and not py_approvals.endswith(
        b"\n\n")
    # And the timestamp-bearing forms differ only in the resumed_at value.
    assert bash_parsed["resumed_at"] != py_parsed["resumed_at"] or \
        abs(
            (datetime.strptime(bash_parsed["resumed_at"], "%Y-%m-%dT%H:%M:%SZ")
             - datetime.strptime(py_parsed["resumed_at"],
                                 "%Y-%m-%dT%H:%M:%SZ")).total_seconds()
        ) < 2