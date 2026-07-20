"""Parity gate: ``mini_ork.observability.bug_collector`` vs ``bin/mini-ork-bug-collector``.

Each test invokes the LIVE bash subprocess against a sandbox run-dir,
then invokes the Python port via ``python -m`` against a parallel
sandbox run-dir, and asserts the resulting ``noticed_bugs.jsonl``
content + stderr + exit-code match exactly. No mocks, no hardcoded
expected outputs — expected is always the live bash control invocation.

The bash script's source-of-truth is ``bin/mini-ork-bug-collector``
(strangler-fig KEEP invariant per the migration kickoff); this test
exercises every branch of that script and the Python port must mirror
each one byte-for-byte.

Cases (8, above the kickoff's >=6 floor):
  (a) --mode off — writes no JSONL, exits 0, no stderr.
  (b) --help / -h — bash `sed -n '2,32p' ...` stdout == Python `_usage()`
      byte-for-byte; -h matches --help.
  (c) heuristic with no --output-file → 0 rows, exits 0, no stderr
      (heredoc never invoked when targets empty).
  (d) heuristic with TODO marker → 1 low-severity row, confidence 0.55.
  (e) heuristic ONLY_ON_FAILURE gate: broken-invariant fires on
      status=failure, suppressed on status=success (stderr still emits).
  (f) heuristic dedupes by (scope, title.lower()) within one scan.
  (g) heuristic caps emissions at 5 per node (10-input → 5-output).
  (h) --mode llm writes exact stderr `  [bug-collector] llm mode not yet
      implemented; use --mode heuristic` and exits 0.

Tolerance notes:
  * confidence floats compared at 1e-6 (kickoff tolerance).
  * JSONL row comparison: full dict equality (all 9 fields are parity
    invariants — no field stripping needed).
  * stderr must match byte-for-byte (the bash ``print(emitted, ...,
    file=sys.stderr)`` is part of the parity contract).
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
from mini_ork.observability import bug_collector as py

BASH = REPO / "bin" / "mini-ork-bug-collector"


# ─────────────────────────────────────────────────────────────────────────────
# Fixtures / helpers
# ─────────────────────────────────────────────────────────────────────────────
def _which_tools() -> None:
    for tool in ("bash", "python3"):
        if not shutil.which(tool):
            pytest.skip(f"{tool} not on PATH")
    if not BASH.exists():
        pytest.skip(f"missing bin/mini-ork-bug-collector at {BASH}")


def _run_bash(args: list[str], *, env_extra: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    """Run ``bash bin/mini-ork-bug-collector <args>`` with caller env."""
    return subprocess.run(
        ["bash", str(BASH), *args],
        env={**os.environ, **(env_extra or {})},
        capture_output=True, text=True,
    )


def _run_py(args: list[str], *, env_extra: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    """Run ``python -m mini_ork.observability.bug_collector <args>``."""
    return subprocess.run(
        [sys.executable, "-m", "mini_ork.observability.bug_collector", *args],
        env={**os.environ, **(env_extra or {})},
        capture_output=True, text=True,
    )


def _read_jsonl(path: Path) -> list[dict]:
    """Parse a JSONL file into a list of dicts. Missing file → []."""
    if not path.exists():
        return []
    out: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        out.append(json.loads(line))
    return out


# ─────────────────────────────────────────────────────────────────────────────
# (a) --mode off writes no JSONL, exits 0, no stderr
# ─────────────────────────────────────────────────────────────────────────────
def test_mode_off_exits_zero_no_output(tmp_path):
    """Bash line 50: ``[ "$mode" = "off" ] && exit 0``. Both ports exit
    0 with empty stdout and empty stderr; no JSONL written."""
    _which_tools()
    bash_run = tmp_path / "bash_run"
    bash_run.mkdir()
    py_run = tmp_path / "py_run"
    py_run.mkdir()

    args = ["--mode", "off", "--node-id", "n1", "--output-file",
            str(tmp_path / "out.md")]
    bash_r = _run_bash(args, env_extra={"MINI_ORK_RUN_DIR": str(bash_run)})
    py_r = _run_py(args, env_extra={"MINI_ORK_RUN_DIR": str(py_run)})

    assert bash_r.returncode == 0, f"bash failed: {bash_r.stderr}"
    assert py_r.returncode == 0, f"py failed: {py_r.stderr}"
    assert bash_r.stdout == ""
    assert py_r.stdout == ""
    assert bash_r.stderr == py_r.stderr == ""
    assert not (bash_run / "noticed_bugs.jsonl").exists()
    assert not (py_run / "noticed_bugs.jsonl").exists()


# ─────────────────────────────────────────────────────────────────────────────
# (b) --help / -h parity — bash sed output == Python _USAGE_BLOCK
# ─────────────────────────────────────────────────────────────────────────────
def test_help_parity():
    """Bash ``sed -n '2,32p'`` then stripping leading hash-space or
    bare-hash emits the same text as Python ``_usage()`` byte-for-byte.
    ``-h`` matches ``--help``."""
    _which_tools()
    bash_r = _run_bash(["--help"])
    assert bash_r.returncode == 0, bash_r.stderr
    py_r = _run_py(["--help"])
    assert py_r.returncode == 0, py_r.stderr

    assert bash_r.stdout == py_r.stdout, (
        f"help drift:\nbash={bash_r.stdout!r}\npy  ={py_r.stdout!r}"
    )
    # Drift-detection: the Python literal must equal the live bash output.
    assert py_r.stdout.encode() == py._USAGE_BLOCK.encode()
    # Sanity: literal starts with the docblock headline.
    assert py._USAGE_BLOCK.startswith(
        "mini-ork bug-collector — auto-dispatched after each node completes by\n"
    )

    # `-h` shortcut matches `--help` byte-for-byte.
    bash_h = _run_bash(["-h"])
    py_h = _run_py(["-h"])
    assert bash_h.returncode == 0
    assert py_h.returncode == 0
    assert bash_h.stdout == py_h.stdout == bash_r.stdout
    assert bash_h.stdout == py_r.stdout


# ─────────────────────────────────────────────────────────────────────────────
# (c) heuristic with no --output-file → 0 rows, exits 0, no stderr
# ─────────────────────────────────────────────────────────────────────────────
def test_heuristic_no_output_file(tmp_path):
    """Bash line 77: ``[ -z "$targets" ] && exit 0`` — heredoc never
    invoked, no stderr emitted. Python port mirrors: targets=[] →
    return 0 without calling heuristic_scan."""
    _which_tools()
    bash_run = tmp_path / "bash_run"
    bash_run.mkdir()
    py_run = tmp_path / "py_run"
    py_run.mkdir()

    args = ["--mode", "heuristic", "--node-id", "n1"]
    bash_r = _run_bash(args, env_extra={"MINI_ORK_RUN_DIR": str(bash_run)})
    py_r = _run_py(args, env_extra={"MINI_ORK_RUN_DIR": str(py_run)})

    assert bash_r.returncode == 0
    assert py_r.returncode == 0
    assert bash_r.stdout == ""
    assert py_r.stdout == ""
    assert bash_r.stderr == py_r.stderr == ""
    assert not (bash_run / "noticed_bugs.jsonl").exists()
    assert not (py_run / "noticed_bugs.jsonl").exists()


# ─────────────────────────────────────────────────────────────────────────────
# (d) heuristic with TODO marker → 1 low-severity row, confidence 0.55
# ─────────────────────────────────────────────────────────────────────────────
def test_heuristic_todo_marker(tmp_path):
    """A single ``TODO:`` line must emit exactly one row with scope
    'todo-marker', severity 'low', confidence 0.55. Both ports must
    write byte-identical JSONL and stderr ``1\\n``."""
    _which_tools()
    out_md = tmp_path / "agent_output.md"
    out_md.write_text(
        "Just one line of output.\nTODO: fix this thing.\nMore text.\n",
        encoding="utf-8",
    )

    bash_run = tmp_path / "bash_run"
    bash_run.mkdir()
    py_run = tmp_path / "py_run"
    py_run.mkdir()

    args = ["--mode", "heuristic", "--node-id", "node-1",
            "--node-type", "implementer", "--output-file", str(out_md),
            "--status", "success", "--task-class", "code_fix"]

    bash_r = _run_bash(args, env_extra={"MINI_ORK_RUN_DIR": str(bash_run)})
    py_r = _run_py(args, env_extra={"MINI_ORK_RUN_DIR": str(py_run)})

    assert bash_r.returncode == 0, f"bash failed: {bash_r.stderr}"
    assert py_r.returncode == 0, f"py failed: {py_r.stderr}"

    bash_rows = _read_jsonl(bash_run / "noticed_bugs.jsonl")
    py_rows = _read_jsonl(py_run / "noticed_bugs.jsonl")
    assert len(bash_rows) == 1, bash_rows
    assert len(py_rows) == 1, py_rows

    # byte-for-byte equality of the JSON dict (all 9 fields).
    assert bash_rows[0] == py_rows[0], (
        f"row drift:\nbash={bash_rows[0]}\npy  ={py_rows[0]}"
    )

    assert bash_r.stderr == py_r.stderr == "1\n"
    assert bash_r.stdout == py_r.stdout == ""

    # Sanity: confidence is a float, 0.55 ± 1e-6.
    assert isinstance(py_rows[0]["confidence"], float)
    assert abs(py_rows[0]["confidence"] - 0.55) <= 1e-6
    assert py_rows[0]["severity"] == "low"
    assert "todo-marker" in py_rows[0]["description"]


# ─────────────────────────────────────────────────────────────────────────────
# (e) ONLY_ON_FAILURE gate: broken-invariant suppressed on success
# ─────────────────────────────────────────────────────────────────────────────
def test_heuristic_only_on_failure_gate(tmp_path):
    """Bash lines 108-109 + 126-127: scope 'broken-invariant' is gated by
    ``ONLY_ON_FAILURE``. status=success → 0 rows, stderr ``0\\n``;
    status=failure → 1 high-severity row, stderr ``1\\n``."""
    _which_tools()
    out_md = tmp_path / "agent_output.md"
    out_md.write_text(
        "The assertion fails on line 42 of foo.py.\n",
        encoding="utf-8",
    )

    # Run 1: status=success → broken-invariant suppressed.
    bash_run1 = tmp_path / "bash_run1"
    bash_run1.mkdir()
    py_run1 = tmp_path / "py_run1"
    py_run1.mkdir()
    args_ok = ["--mode", "heuristic", "--node-id", "n1",
               "--node-type", "verifier", "--output-file", str(out_md),
               "--status", "success", "--task-class", "code_fix"]
    bash_r1 = _run_bash(args_ok, env_extra={"MINI_ORK_RUN_DIR": str(bash_run1)})
    py_r1 = _run_py(args_ok, env_extra={"MINI_ORK_RUN_DIR": str(py_run1)})
    assert bash_r1.returncode == 0
    assert py_r1.returncode == 0
    # Heredoc DID run (targets non-empty), so stderr fires with `0\n`.
    assert bash_r1.stderr == py_r1.stderr == "0\n"
    assert bash_r1.stdout == py_r1.stdout == ""
    assert _read_jsonl(bash_run1 / "noticed_bugs.jsonl") == []
    assert _read_jsonl(py_run1 / "noticed_bugs.jsonl") == []

    # Run 2: status=failure → broken-invariant fires once.
    bash_run2 = tmp_path / "bash_run2"
    bash_run2.mkdir()
    py_run2 = tmp_path / "py_run2"
    py_run2.mkdir()
    args_fail = ["--mode", "heuristic", "--node-id", "n1",
                 "--node-type", "verifier", "--output-file", str(out_md),
                 "--status", "failure", "--task-class", "code_fix"]
    bash_r2 = _run_bash(args_fail, env_extra={"MINI_ORK_RUN_DIR": str(bash_run2)})
    py_r2 = _run_py(args_fail, env_extra={"MINI_ORK_RUN_DIR": str(py_run2)})
    assert bash_r2.returncode == 0
    assert py_r2.returncode == 0

    bash_rows = _read_jsonl(bash_run2 / "noticed_bugs.jsonl")
    py_rows = _read_jsonl(py_run2 / "noticed_bugs.jsonl")
    assert len(bash_rows) == 1
    assert len(py_rows) == 1
    assert bash_rows[0] == py_rows[0]
    assert bash_rows[0]["severity"] == "high"
    assert abs(bash_rows[0]["confidence"] - 0.78) <= 1e-6
    assert "broken-invariant" in bash_rows[0]["description"]
    assert bash_r2.stderr == py_r2.stderr == "1\n"


# ─────────────────────────────────────────────────────────────────────────────
# (f) dedupe by (scope, title.lower())
# ─────────────────────────────────────────────────────────────────────────────
def test_heuristic_dedupes_within_scan(tmp_path):
    """Bash lines 131-134: ``seen = {(scope, title.lower())}`` blocks
    repeats. Three identical TODO lines must emit exactly one row."""
    _which_tools()
    out_md = tmp_path / "agent_output.md"
    out_md.write_text(
        "TODO: fix the bug\n"
        "TODO: fix the bug\n"
        "TODO: fix the bug\n",
        encoding="utf-8",
    )

    bash_run = tmp_path / "bash_run"
    bash_run.mkdir()
    py_run = tmp_path / "py_run"
    py_run.mkdir()
    args = ["--mode", "heuristic", "--node-id", "n1",
            "--node-type", "implementer", "--output-file", str(out_md),
            "--status", "success", "--task-class", "code_fix"]
    bash_r = _run_bash(args, env_extra={"MINI_ORK_RUN_DIR": str(bash_run)})
    py_r = _run_py(args, env_extra={"MINI_ORK_RUN_DIR": str(py_run)})
    assert bash_r.returncode == 0
    assert py_r.returncode == 0

    bash_rows = _read_jsonl(bash_run / "noticed_bugs.jsonl")
    py_rows = _read_jsonl(py_run / "noticed_bugs.jsonl")
    assert len(bash_rows) == 1, bash_rows
    assert len(py_rows) == 1, py_rows
    assert bash_rows[0] == py_rows[0]
    assert bash_r.stderr == py_r.stderr == "1\n"


# ─────────────────────────────────────────────────────────────────────────────
# (g) cap emissions at 5 per node
# ─────────────────────────────────────────────────────────────────────────────
def test_heuristic_caps_at_5(tmp_path):
    """Bash lines 147-153: after emitted >= 5, all three nested loops
    break. 10 distinct TODO lines → exactly 5 rows; stderr ``5\\n``."""
    _which_tools()
    out_md = tmp_path / "agent_output.md"
    out_md.write_text(
        "\n".join(f"TODO: fix item {i}" for i in range(10)) + "\n",
        encoding="utf-8",
    )

    bash_run = tmp_path / "bash_run"
    bash_run.mkdir()
    py_run = tmp_path / "py_run"
    py_run.mkdir()
    args = ["--mode", "heuristic", "--node-id", "n1",
            "--node-type", "implementer", "--output-file", str(out_md),
            "--status", "success", "--task-class", "code_fix"]
    bash_r = _run_bash(args, env_extra={"MINI_ORK_RUN_DIR": str(bash_run)})
    py_r = _run_py(args, env_extra={"MINI_ORK_RUN_DIR": str(py_run)})
    assert bash_r.returncode == 0
    assert py_r.returncode == 0

    bash_rows = _read_jsonl(bash_run / "noticed_bugs.jsonl")
    py_rows = _read_jsonl(py_run / "noticed_bugs.jsonl")
    assert len(bash_rows) == 5, bash_rows
    assert len(py_rows) == 5, py_rows
    assert bash_rows == py_rows
    assert bash_r.stderr == py_r.stderr == "5\n"


# ─────────────────────────────────────────────────────────────────────────────
# (h) --mode llm stub
# ─────────────────────────────────────────────────────────────────────────────
def test_mode_llm_stub(tmp_path):
    """Bash lines 162-164: writes the exact stderr message
    ``"  [bug-collector] llm mode not yet implemented; use --mode heuristic"``
    and exits 0. Python port must match byte-for-byte."""
    _which_tools()
    expected_stderr = (
        "  [bug-collector] llm mode not yet implemented; "
        "use --mode heuristic\n"
    )
    args = ["--mode", "llm", "--node-id", "n1",
            "--output-file", str(tmp_path / "out.md")]
    bash_r = _run_bash(args)
    py_r = _run_py(args)
    assert bash_r.returncode == 0, f"bash failed: {bash_r.stderr}"
    assert py_r.returncode == 0, f"py failed: {py_r.stderr}"
    assert bash_r.stdout == py_r.stdout == ""
    assert bash_r.stderr == py_r.stderr == expected_stderr