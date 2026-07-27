"""Unit tests: ``mini_ork.observability.bug_collector`` (bash parity halves removed; formerly vs ``bin/mini-ork-bug-collector``).

Each test invokes the Python port via ``python -m`` against a sandbox
run-dir and asserts the resulting ``noticed_bugs.jsonl`` content + stderr
+ exit-code. No mocks.

Cases (8):
  (a) --mode off — writes no JSONL, exits 0, no stderr.
  (b) --help / -h — stdout equals the module `_USAGE_BLOCK`; -h matches --help.
  (c) heuristic with no --output-file → 0 rows, exits 0, no stderr.
  (d) heuristic with TODO marker → 1 low-severity row, confidence 0.55.
  (e) heuristic ONLY_ON_FAILURE gate: broken-invariant fires on
      status=failure, suppressed on status=success (stderr still emits).
  (f) heuristic dedupes by (scope, title.lower()) within one scan.
  (g) heuristic caps emissions at 5 per node (10-input → 5-output).
  (h) --mode llm writes exact stderr `  [bug-collector] llm mode not yet
      implemented; use --mode heuristic` and exits 0.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
from mini_ork.observability import bug_collector as py


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────
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
    py_run = tmp_path / "py_run"
    py_run.mkdir()

    args = ["--mode", "off", "--node-id", "n1", "--output-file",
            str(tmp_path / "out.md")]
    py_r = _run_py(args, env_extra={"MINI_ORK_RUN_DIR": str(py_run)})

    assert py_r.returncode == 0, f"py failed: {py_r.stderr}"
    assert py_r.stdout == ""
    assert py_r.stderr == ""
    assert not (py_run / "noticed_bugs.jsonl").exists()


# ─────────────────────────────────────────────────────────────────────────────
# (b) --help / -h — stdout equals the module _USAGE_BLOCK
# ─────────────────────────────────────────────────────────────────────────────
def test_help():
    py_r = _run_py(["--help"])
    assert py_r.returncode == 0, py_r.stderr
    assert py_r.stdout.encode() == py._USAGE_BLOCK.encode()
    # Sanity: literal starts with the docblock headline.
    assert py._USAGE_BLOCK.startswith(
        "mini-ork bug-collector — auto-dispatched after each node completes by\n"
    )

    # `-h` shortcut matches `--help` byte-for-byte.
    py_h = _run_py(["-h"])
    assert py_h.returncode == 0
    assert py_h.stdout == py_r.stdout


# ─────────────────────────────────────────────────────────────────────────────
# (c) heuristic with no --output-file → 0 rows, exits 0, no stderr
# ─────────────────────────────────────────────────────────────────────────────
def test_heuristic_no_output_file(tmp_path):
    """targets=[] → return 0 without calling heuristic_scan; no stderr."""
    py_run = tmp_path / "py_run"
    py_run.mkdir()

    args = ["--mode", "heuristic", "--node-id", "n1"]
    py_r = _run_py(args, env_extra={"MINI_ORK_RUN_DIR": str(py_run)})

    assert py_r.returncode == 0
    assert py_r.stdout == ""
    assert py_r.stderr == ""
    assert not (py_run / "noticed_bugs.jsonl").exists()


# ─────────────────────────────────────────────────────────────────────────────
# (d) heuristic with TODO marker → 1 low-severity row, confidence 0.55
# ─────────────────────────────────────────────────────────────────────────────
def test_heuristic_todo_marker(tmp_path):
    """A single ``TODO:`` line must emit exactly one row with scope
    'todo-marker', severity 'low', confidence 0.55."""
    out_md = tmp_path / "agent_output.md"
    out_md.write_text(
        "Just one line of output.\nTODO: fix this thing.\nMore text.\n",
        encoding="utf-8",
    )

    py_run = tmp_path / "py_run"
    py_run.mkdir()

    args = ["--mode", "heuristic", "--node-id", "node-1",
            "--node-type", "implementer", "--output-file", str(out_md),
            "--status", "success", "--task-class", "code_fix"]

    py_r = _run_py(args, env_extra={"MINI_ORK_RUN_DIR": str(py_run)})

    assert py_r.returncode == 0, f"py failed: {py_r.stderr}"

    py_rows = _read_jsonl(py_run / "noticed_bugs.jsonl")
    assert len(py_rows) == 1, py_rows

    assert py_r.stderr == "1\n"
    assert py_r.stdout == ""

    # confidence is a float, 0.55 ± 1e-6.
    assert isinstance(py_rows[0]["confidence"], float)
    assert abs(py_rows[0]["confidence"] - 0.55) <= 1e-6
    assert py_rows[0]["severity"] == "low"
    assert "todo-marker" in py_rows[0]["description"]


# ─────────────────────────────────────────────────────────────────────────────
# (e) ONLY_ON_FAILURE gate: broken-invariant suppressed on success
# ─────────────────────────────────────────────────────────────────────────────
def test_heuristic_only_on_failure_gate(tmp_path):
    """status=success → 0 rows, stderr ``0\\n``; status=failure → 1
    high-severity row, stderr ``1\\n``."""
    out_md = tmp_path / "agent_output.md"
    out_md.write_text(
        "The assertion fails on line 42 of foo.py.\n",
        encoding="utf-8",
    )

    # Run 1: status=success → broken-invariant suppressed.
    py_run1 = tmp_path / "py_run1"
    py_run1.mkdir()
    args_ok = ["--mode", "heuristic", "--node-id", "n1",
               "--node-type", "verifier", "--output-file", str(out_md),
               "--status", "success", "--task-class", "code_fix"]
    py_r1 = _run_py(args_ok, env_extra={"MINI_ORK_RUN_DIR": str(py_run1)})
    assert py_r1.returncode == 0
    # Scan DID run (targets non-empty), so stderr fires with `0\n`.
    assert py_r1.stderr == "0\n"
    assert py_r1.stdout == ""
    assert _read_jsonl(py_run1 / "noticed_bugs.jsonl") == []

    # Run 2: status=failure → broken-invariant fires once.
    py_run2 = tmp_path / "py_run2"
    py_run2.mkdir()
    args_fail = ["--mode", "heuristic", "--node-id", "n1",
                 "--node-type", "verifier", "--output-file", str(out_md),
                 "--status", "failure", "--task-class", "code_fix"]
    py_r2 = _run_py(args_fail, env_extra={"MINI_ORK_RUN_DIR": str(py_run2)})
    assert py_r2.returncode == 0

    py_rows = _read_jsonl(py_run2 / "noticed_bugs.jsonl")
    assert len(py_rows) == 1
    assert py_rows[0]["severity"] == "high"
    assert abs(py_rows[0]["confidence"] - 0.78) <= 1e-6
    assert "broken-invariant" in py_rows[0]["description"]
    assert py_r2.stderr == "1\n"


# ─────────────────────────────────────────────────────────────────────────────
# (f) dedupe by (scope, title.lower())
# ─────────────────────────────────────────────────────────────────────────────
def test_heuristic_dedupes_within_scan(tmp_path):
    """Three identical TODO lines must emit exactly one row."""
    out_md = tmp_path / "agent_output.md"
    out_md.write_text(
        "TODO: fix the bug\n"
        "TODO: fix the bug\n"
        "TODO: fix the bug\n",
        encoding="utf-8",
    )

    py_run = tmp_path / "py_run"
    py_run.mkdir()
    args = ["--mode", "heuristic", "--node-id", "n1",
            "--node-type", "implementer", "--output-file", str(out_md),
            "--status", "success", "--task-class", "code_fix"]
    py_r = _run_py(args, env_extra={"MINI_ORK_RUN_DIR": str(py_run)})
    assert py_r.returncode == 0

    py_rows = _read_jsonl(py_run / "noticed_bugs.jsonl")
    assert len(py_rows) == 1, py_rows
    assert py_r.stderr == "1\n"


# ─────────────────────────────────────────────────────────────────────────────
# (g) cap emissions at 5 per node
# ─────────────────────────────────────────────────────────────────────────────
def test_heuristic_caps_at_5(tmp_path):
    """10 distinct TODO lines → exactly 5 rows; stderr ``5\\n``."""
    out_md = tmp_path / "agent_output.md"
    out_md.write_text(
        "\n".join(f"TODO: fix item {i}" for i in range(10)) + "\n",
        encoding="utf-8",
    )

    py_run = tmp_path / "py_run"
    py_run.mkdir()
    args = ["--mode", "heuristic", "--node-id", "n1",
            "--node-type", "implementer", "--output-file", str(out_md),
            "--status", "success", "--task-class", "code_fix"]
    py_r = _run_py(args, env_extra={"MINI_ORK_RUN_DIR": str(py_run)})
    assert py_r.returncode == 0

    py_rows = _read_jsonl(py_run / "noticed_bugs.jsonl")
    assert len(py_rows) == 5, py_rows
    assert py_r.stderr == "5\n"


# ─────────────────────────────────────────────────────────────────────────────
# (h) --mode llm stub
# ─────────────────────────────────────────────────────────────────────────────
def test_mode_llm_stub(tmp_path):
    """Writes the exact stderr message and exits 0."""
    expected_stderr = (
        "  [bug-collector] llm mode not yet implemented; "
        "use --mode heuristic\n"
    )
    args = ["--mode", "llm", "--node-id", "n1",
            "--output-file", str(tmp_path / "out.md")]
    py_r = _run_py(args)
    assert py_r.returncode == 0, f"py failed: {py_r.stderr}"
    assert py_r.stdout == ""
    assert py_r.stderr == expected_stderr
