"""Unit tests: mini_ork.stores.checkpoint (bash parity halves removed; formerly vs lib/checkpoint.sh).

Each test drives the Python port and asserts return codes, stdout strings,
stderr substrings, and the JSON state (ignoring the wall-clock
``completed_at`` / ``age_s`` fields beyond sanity bounds).

Eight cases:
  (a) write empty node_id          → rc=2 + "checkpoint_write: usage:"
                                      in stderr (substring membership)
  (b) write bad status             → rc=2 + "status must be
                                      success|failure|skipped" in stderr
  (c) write success + can_resume   → stdout equals "yes\\t<artifact>";
                                      .checkpoint.json parses to
                                      {status, artifact_path}
  (d) write failure + can_resume   → stdout equals "no"
  (e) write success, rmt artifact,
      then can_resume              → stdout equals "no" (recorded
                                      but missing on disk → unsafe resume)
  (f) write 3 nodes (2 success+art,
      1 failure), clear the failure,
      then summary                 → summary prints a 2-row table; parsed
                                      rows match node_id / status /
                                      artifact_path; age_s sane
  (g) clear with no arg            → removes the file; a subsequent
                                      can_resume returns "no"
  (h) write with MINI_ORK_RUN_DIR
      unset                        → rc=2 + "MINI_ORK_RUN_DIR unset"
                                      substring in stderr

Environment isolation:
  Each test gets its own tmpdir-based run dir via ``tmp_path`` and uses
  ``monkeypatch.setenv("MINI_ORK_RUN_DIR", ...)`` (auto-reverts) so the
  Python process resolves the per-test path.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
from mini_ork.stores import checkpoint as ck


def _point_env(monkeypatch: pytest.MonkeyPatch, run_dir: str) -> None:
    """Redirect the Python process env at the per-test run dir."""
    monkeypatch.setenv("MINI_ORK_RUN_DIR", run_dir)


def _parse_summary(stdout: str) -> list[dict]:
    """Parse the summary table into dicts.

    Layout (fixed widths):
      cols 0-23   node_id   (left-padded to 24)
      col  24     ' '
      cols 25-34  status    (left-padded to 10)
      col  35     ' '
      cols 36-43  age_s     (left-padded to 8, integer)
      col  44     ' '
      cols 45+    artifact  (as-is; may be empty → row ends with a space)
    """
    lines = [ln for ln in stdout.splitlines() if ln]
    rows: list[dict] = []
    for ln in lines[2:]:  # skip header + 80-char separator
        if len(ln) < 45:
            ln = ln + " " * (45 - len(ln))
        rows.append({
            "node_id": ln[0:24].rstrip(),
            "status": ln[25:35].rstrip(),
            "age_s": ln[36:44].strip(),
            "artifact_path": ln[45:],
        })
    return rows


# ─────────────────────────────────────────────────────────────────────────────
# (a) write empty node_id → rc=2 + usage substring in stderr
# ─────────────────────────────────────────────────────────────────────────────
def test_write_empty_node_id(tmp_path, monkeypatch, capsys):
    run_dir = str(tmp_path / "a_run")
    os.makedirs(run_dir)
    _point_env(monkeypatch, run_dir)

    py_rc = ck.write("", "success", None)
    captured = capsys.readouterr()
    py_err = captured.err

    assert py_rc == 2
    assert "checkpoint_write: usage:" in py_err


# ─────────────────────────────────────────────────────────────────────────────
# (b) write bad status → rc=2 + status-must-be... substring in stderr
# ─────────────────────────────────────────────────────────────────────────────
def test_write_bad_status(tmp_path, monkeypatch, capsys):
    run_dir = str(tmp_path / "b_run")
    os.makedirs(run_dir)
    _point_env(monkeypatch, run_dir)

    py_rc = ck.write("node-b", "nope", None)
    captured = capsys.readouterr()
    py_err = captured.err

    assert py_rc == 2
    assert "status must be success|failure|skipped" in py_err
    assert "nope" in py_err


# ─────────────────────────────────────────────────────────────────────────────
# (c) write success + can_resume → "yes\t<art>" stdout AND JSON state
# ─────────────────────────────────────────────────────────────────────────────
def test_write_success_then_can_resume(tmp_path, monkeypatch, capsys):
    run_dir = str(tmp_path / "c_run")
    os.makedirs(run_dir)
    _point_env(monkeypatch, run_dir)

    art = os.path.join(run_dir, "plan.json")
    Path(art).touch()

    py_rc_write = ck.write("node-c", "success", art)
    capsys.readouterr()  # discard empty stdout from write
    py_stdout = ck.can_resume("node-c")
    # can_resume writes the value to stdout; capsys reflects that.
    py_stdout_printed = capsys.readouterr().out.strip()

    assert py_rc_write == 0
    assert py_stdout == f"yes\t{art}"
    assert py_stdout_printed == py_stdout

    # JSON state: {status, artifact_path}; ``completed_at`` is wall-clock.
    py_json = json.loads(Path(run_dir).joinpath(".checkpoint.json").read_text())
    assert py_json["nodes"]["node-c"]["status"] == "success"
    assert py_json["nodes"]["node-c"]["artifact_path"] == art


# ─────────────────────────────────────────────────────────────────────────────
# (d) write failure + can_resume → stdout "no"
# ─────────────────────────────────────────────────────────────────────────────
def test_write_failure_then_can_resume(tmp_path, monkeypatch, capsys):
    run_dir = str(tmp_path / "d_run")
    os.makedirs(run_dir)
    _point_env(monkeypatch, run_dir)

    py_rc = ck.write("node-d", "failure")
    capsys.readouterr()
    py_stdout = ck.can_resume("node-d")
    py_stdout_printed = capsys.readouterr().out.strip()

    assert py_rc == 0
    assert py_stdout == "no"
    assert py_stdout_printed == "no"


# ─────────────────────────────────────────────────────────────────────────────
# (e) write success, rmt artifact, can_resume → stdout "no" (unsafe)
# ─────────────────────────────────────────────────────────────────────────────
def test_write_success_artifact_removed_can_resume(tmp_path, monkeypatch, capsys):
    run_dir = str(tmp_path / "e_run")
    os.makedirs(run_dir)
    _point_env(monkeypatch, run_dir)

    art = os.path.join(run_dir, "lens-out.md")
    Path(art).touch()

    py_rc = ck.write("node-e", "success", art)
    capsys.readouterr()

    # Remove the artifact from disk → recorded but missing → unsafe resume.
    os.remove(art)

    py_stdout = ck.can_resume("node-e")
    py_stdout_printed = capsys.readouterr().out.strip()

    assert py_rc == 0
    assert py_stdout == "no"
    assert py_stdout_printed == "no"


# ─────────────────────────────────────────────────────────────────────────────
# (f) write 3 nodes, clear 1, summary → parsed rows match
# ─────────────────────────────────────────────────────────────────────────────
def test_summary_after_clear(tmp_path, monkeypatch, capsys):
    run_dir = str(tmp_path / "f_run")
    os.makedirs(run_dir)
    _point_env(monkeypatch, run_dir)

    art_a = os.path.join(run_dir, "plan.json")
    art_b = os.path.join(run_dir, "lens-out.md")
    Path(art_a).touch()
    Path(art_b).touch()

    # Write 3 nodes (2 success+artifact, 1 failure), then clear the failure.
    for nid, status_str, art in [
        ("planner", "success", art_a),
        ("researcher", "success", art_b),
        ("verifier", "failure", ""),
    ]:
        assert ck.write(nid, status_str, art or None) == 0
        capsys.readouterr()

    assert ck.clear("verifier") == 0
    capsys.readouterr()

    py_sum = ck.summary()
    py_sum_out = capsys.readouterr().out

    assert py_sum_out.rstrip("\n") == py_sum or py_sum in py_sum_out

    py_rows = _parse_summary(py_sum_out)

    assert len(py_rows) == 2

    py_rows.sort(key=lambda r: r["node_id"])

    assert py_rows[0]["node_id"] == "planner"
    assert py_rows[1]["node_id"] == "researcher"
    for pr in py_rows:
        assert pr["status"] == "success"
        # Age is wall-clock seconds since write; small and non-negative.
        assert 0 <= int(pr["age_s"]) <= 60
    assert py_rows[0]["artifact_path"] == art_a
    assert py_rows[1]["artifact_path"] == art_b


# ─────────────────────────────────────────────────────────────────────────────
# (g) clear with no arg → file removed; can_resume returns "no"
# ─────────────────────────────────────────────────────────────────────────────
def test_clear_no_arg_removes_file(tmp_path, monkeypatch, capsys):
    run_dir = str(tmp_path / "g_run")
    os.makedirs(run_dir)
    _point_env(monkeypatch, run_dir)

    ckpt = Path(run_dir) / ".checkpoint.json"

    assert ck.write("node-g", "success") == 0
    capsys.readouterr()
    assert ckpt.is_file()

    assert ck.clear() == 0
    capsys.readouterr()
    assert not ckpt.is_file()

    # Clear again — file already absent → rc=0 (idempotent).
    assert ck.clear() == 0
    capsys.readouterr()

    # Post-clear: can_resume on the same id returns "no".
    py_stdout = ck.can_resume("node-g")
    py_stdout_printed = capsys.readouterr().out.strip()
    assert py_stdout == "no"
    assert py_stdout_printed == "no"


# ─────────────────────────────────────────────────────────────────────────────
# (h) write with MINI_ORK_RUN_DIR unset → rc=2 + unset-substring in stderr
# ─────────────────────────────────────────────────────────────────────────────
def test_write_miniork_run_dir_unset(monkeypatch, capsys):
    # Force the python side to resolve with no MINI_ORK_RUN_DIR.
    monkeypatch.delenv("MINI_ORK_RUN_DIR", raising=False)

    py_rc = ck.write("node-h", "success")
    captured = capsys.readouterr()
    py_err = captured.err

    assert py_rc == 2
    assert "MINI_ORK_RUN_DIR unset" in py_err
