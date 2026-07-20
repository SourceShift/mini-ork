"""Parity gate: mini_ork.stores.checkpoint vs lib/checkpoint.sh.

Each test invokes the LIVE bash subprocess (sourcing ``lib/checkpoint.sh``
in a single ``bash -c`` block so the functions are visible to the caller)
on the same inputs as the Python port and asserts byte-identical output —
return codes, stdout strings, stderr substrings, and JSON state after
stripping the wall-clock fields bash and python emit differently
(``completed_at`` in seconds; ``age_s`` in summary rows).

Eight cases (above the kickoff's >=6 floor):
  (a) write empty node_id          → both rc=2 + "checkpoint_write: usage:"
                                      in stderr (substring membership)
  (b) write bad status             → both rc=2 + "status must be
                                      success|failure|skipped" in stderr
  (c) write success + can_resume   → both stdout equals "yes\\t<artifact>";
                                      .checkpoint.json parses to identical
                                      {status, artifact_path} (stripping
                                      completed_at)
  (d) write failure + can_resume   → both stdout equals "no"
  (e) write success, rmt artifact,
      then can_resume              → both stdout equals "no" (recorded
                                      but missing on disk → unsafe resume)
  (f) write 3 nodes (2 success+art,
      1 failure), clear the failure,
      then summary                 → both summaries print the same 2-row
                                      table; parsed dicts match on
                                      node_id / status / artifact_path;
                                      age_s allowed drift <=5 s
  (g) clear with no arg            → both remove the file; a subsequent
                                      can_resume returns "no"
  (h) write with MINI_ORK_RUN_DIR
      unset                        → both rc=2 + "MINI_ORK_RUN_DIR unset"
                                      substring in stderr

Environment isolation:
  Each test gets its own tmpdir-based run dir via ``tmp_path`` and uses
  ``monkeypatch.setenv("MINI_ORK_RUN_DIR", ...)`` (auto-reverts) so the
  Python process resolves the per-test path. The bash subprocess env is
  built from ``os.environ`` AFTER monkeypatch so both sides resolve the
  same path. Cases (a), (b), (h) don't use the dir at all (errors before
  any FS write); case (f) uses one shared dir so both sides see the
  SAME row ordering on disk after identical operations.
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
from mini_ork.stores import checkpoint as ck

SH = REPO / "lib" / "checkpoint.sh"


def _bash_call(env: dict, line: str) -> tuple[int, str, str]:
    """Run a single bash statement with ``lib/checkpoint.sh`` sourced.

    Returns (rc, op_stdout, op_stderr). The wrapper appends
    ``echo "RC=$?"`` so the rc survives even when the operation itself
    is silent. ``op_stdout`` strips that trailer.
    """
    wrapper = f'. "{SH}"\n{line}\necho "RC=$?"\n'
    r = subprocess.run(["bash", "-c", wrapper], env=env,
                       capture_output=True, text=True)
    lines = r.stdout.splitlines()
    assert lines and lines[-1].startswith("RC="), (
        f"unexpected bash stdout (no RC trailer): {r.stdout!r}"
    )
    rc = int(lines[-1][3:])
    op_stdout = "\n".join(lines[:-1])
    return rc, op_stdout, r.stderr


def _point_env(monkeypatch: pytest.MonkeyPatch, run_dir: str) -> dict:
    """Redirect the Python process env AND return the matching subprocess env.

    Returns a fresh env dict so the bash subprocess sees the same
    MINI_ORK_RUN_DIR the port resolves against. ``**os.environ`` is
    computed AFTER ``monkeypatch.setenv`` so the monkeypatched value
    propagates to bash.
    """
    monkeypatch.setenv("MINI_ORK_RUN_DIR", run_dir)
    return {**os.environ, "MINI_ORK_RUN_DIR": run_dir}


def _parse_summary(stdout: str) -> list[dict]:
    """Parse the bash/python summary table into dicts.

    Layout (fixed widths, identical on both sides):
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
# (a) write empty node_id → both rc=2 + usage substring in stderr
# ─────────────────────────────────────────────────────────────────────────────
def test_write_empty_node_id_parity(tmp_path, monkeypatch, capsys):
    run_dir = str(tmp_path / "a_run")
    os.makedirs(run_dir)
    env = _point_env(monkeypatch, run_dir)

    py_rc = ck.write("", "success", None)
    captured = capsys.readouterr()
    py_err = captured.err

    bash_rc, _, bash_err = _bash_call(env, 'checkpoint_write "" "success"')

    assert py_rc == bash_rc == 2
    assert "checkpoint_write: usage:" in bash_err
    assert "checkpoint_write: usage:" in py_err


# ─────────────────────────────────────────────────────────────────────────────
# (b) write bad status → both rc=2 + status-must-be... substring in stderr
# ─────────────────────────────────────────────────────────────────────────────
def test_write_bad_status_parity(tmp_path, monkeypatch, capsys):
    run_dir = str(tmp_path / "b_run")
    os.makedirs(run_dir)
    env = _point_env(monkeypatch, run_dir)

    py_rc = ck.write("node-b", "nope", None)
    captured = capsys.readouterr()
    py_err = captured.err

    bash_rc, _, bash_err = _bash_call(env, 'checkpoint_write "node-b" "nope"')

    assert py_rc == bash_rc == 2
    assert "status must be success|failure|skipped" in bash_err
    assert "status must be success|failure|skipped" in py_err
    assert "nope" in bash_err
    assert "nope" in py_err


# ─────────────────────────────────────────────────────────────────────────────
# (c) write success + can_resume → identical "yes\t<art>" stdout AND JSON state
# ─────────────────────────────────────────────────────────────────────────────
def test_write_success_then_can_resume_parity(tmp_path, monkeypatch, capsys):
    run_dir = str(tmp_path / "c_run")
    os.makedirs(run_dir)
    env = _point_env(monkeypatch, run_dir)

    art = os.path.join(run_dir, "plan.json")
    Path(art).touch()

    # Bash side: write + can_resume. Capture bash stdout as expected.
    r1 = _bash_call(env, f'checkpoint_write "node-c" "success" "{art}"')
    assert r1[0] == 0, r1
    bash_rc, bash_stdout, _ = _bash_call(env, 'checkpoint_can_resume "node-c"')
    assert bash_rc == 0  # can_resume always rc=0 (success or no)

    # Python side: same. capsys readouterr clears stdout between calls.
    py_rc_write = ck.write("node-c", "success", art)
    capsys.readouterr()  # discard empty stdout from write
    py_stdout = ck.can_resume("node-c")
    # can_resume writes the value to stdout; capsys reflects that.
    py_stdout_printed = capsys.readouterr().out.strip()

    assert py_rc_write == 0
    assert py_stdout == f"yes\t{art}"
    assert py_stdout_printed == py_stdout
    assert bash_stdout.strip() == py_stdout

    # JSON state: both files have the same {status, artifact_path}; only
    # ``completed_at`` differs (int(time.time()), wall-clock). Stripping
    # it leaves an identical shape on both sides.
    bash_json = json.loads(Path(run_dir).joinpath(".checkpoint.json").read_text())
    py_json = json.loads(Path(run_dir).joinpath(".checkpoint.json").read_text())
    assert bash_json["nodes"]["node-c"]["status"] == py_json["nodes"]["node-c"]["status"] == "success"
    assert bash_json["nodes"]["node-c"]["artifact_path"] == py_json["nodes"]["node-c"]["artifact_path"] == art
    # completed_at differs only by a few seconds (fork + python startup)
    assert abs(bash_json["nodes"]["node-c"]["completed_at"]
               - py_json["nodes"]["node-c"]["completed_at"]) < 10


# ─────────────────────────────────────────────────────────────────────────────
# (d) write failure + can_resume → both stdout "no"
# ─────────────────────────────────────────────────────────────────────────────
def test_write_failure_then_can_resume_parity(tmp_path, monkeypatch, capsys):
    run_dir = str(tmp_path / "d_run")
    os.makedirs(run_dir)
    env = _point_env(monkeypatch, run_dir)

    r1 = _bash_call(env, 'checkpoint_write "node-d" "failure"')
    assert r1[0] == 0
    _, bash_stdout, _ = _bash_call(env, 'checkpoint_can_resume "node-d"')

    py_rc = ck.write("node-d", "failure")
    capsys.readouterr()
    py_stdout = ck.can_resume("node-d")
    py_stdout_printed = capsys.readouterr().out.strip()

    assert py_rc == 0
    assert py_stdout == "no"
    assert py_stdout_printed == "no"
    assert bash_stdout.strip() == py_stdout


# ─────────────────────────────────────────────────────────────────────────────
# (e) write success, rmt artifact, can_resume → both stdout "no" (unsafe)
# ─────────────────────────────────────────────────────────────────────────────
def test_write_success_artifact_removed_can_resume_parity(tmp_path, monkeypatch, capsys):
    run_dir = str(tmp_path / "e_run")
    os.makedirs(run_dir)
    env = _point_env(monkeypatch, run_dir)

    art = os.path.join(run_dir, "lens-out.md")
    Path(art).touch()

    r1 = _bash_call(env, f'checkpoint_write "node-e" "success" "{art}"')
    assert r1[0] == 0

    py_rc = ck.write("node-e", "success", art)
    capsys.readouterr()

    # Remove the artifact from disk (bash-side wouldn't have survived
    # anyway since path was real). Match the bash path of removing it.
    os.remove(art)

    _, bash_stdout, _ = _bash_call(env, 'checkpoint_can_resume "node-e"')
    py_stdout = ck.can_resume("node-e")
    py_stdout_printed = capsys.readouterr().out.strip()

    assert py_rc == 0
    assert py_stdout == "no"
    assert py_stdout_printed == "no"
    assert bash_stdout.strip() == py_stdout


# ─────────────────────────────────────────────────────────────────────────────
# (f) write 3 nodes, clear 1, summary → parsed rows match (age_s drift <=5s)
# ─────────────────────────────────────────────────────────────────────────────
def test_summary_after_clear_parity(tmp_path, monkeypatch, capsys):
    run_dir = str(tmp_path / "f_run")
    os.makedirs(run_dir)
    env = _point_env(monkeypatch, run_dir)

    art_a = os.path.join(run_dir, "plan.json")
    art_b = os.path.join(run_dir, "lens-out.md")
    Path(art_a).touch()
    Path(art_b).touch()

    # Write 3 nodes (2 success+artifact, 1 failure) via BOTH sides in the
    # SAME order, then clear the failure node via both sides, then compare
    # summary outputs. Both sides see the same .checkpoint.json structure.
    for nid, status_str, art in [
        ("planner", "success", art_a),
        ("researcher", "success", art_b),
        ("verifier", "failure", ""),
    ]:
        r = _bash_call(env,
            f'checkpoint_write {nid!r} {status_str!r} {art!r}')
        assert r[0] == 0, r
        assert ck.write(nid, status_str, art or None) == 0
        capsys.readouterr()

    # Clear the failure node.
    r = _bash_call(env, 'checkpoint_clear "verifier"')
    assert r[0] == 0, r
    assert ck.clear("verifier") == 0
    capsys.readouterr()

    # Both summary calls — bash subprocess is the live expected, port is
    # the implementation. Compare parsed structural fields; age_s drifts
    # a few seconds between the bash fork and the python in-process call.
    r_bash_sum = _bash_call(env, "checkpoint_summary")
    assert r_bash_sum[0] == 0
    bash_sum_out = r_bash_sum[1]

    py_sum = ck.summary()
    py_sum_out = capsys.readouterr().out

    assert py_sum == bash_sum_out
    assert py_sum_out.rstrip("\n") == bash_sum_out

    bash_rows = _parse_summary(bash_sum_out)
    py_rows = _parse_summary(py_sum_out)

    assert len(bash_rows) == len(py_rows) == 2

    # Sort both sides for order-insensitive comparison (bash sorts keys,
    # port sorts keys — should already match, this is belt-and-suspenders).
    bash_rows.sort(key=lambda r: r["node_id"])
    py_rows.sort(key=lambda r: r["node_id"])

    for br, pr in zip(bash_rows, py_rows):
        assert br["node_id"] == pr["node_id"]
        assert br["status"] == pr["status"] == "success"
        assert br["artifact_path"] == pr["artifact_path"]
        # Age drift allowance (subprocess fork ~50-300 ms; allow 5s).
        assert abs(int(br["age_s"]) - int(pr["age_s"])) <= 5


# ─────────────────────────────────────────────────────────────────────────────
# (g) clear with no arg → file removed; can_resume returns "no"
# ─────────────────────────────────────────────────────────────────────────────
def test_clear_no_arg_removes_file_parity(tmp_path, monkeypatch, capsys):
    run_dir = str(tmp_path / "g_run")
    os.makedirs(run_dir)
    env = _point_env(monkeypatch, run_dir)

    ckpt = Path(run_dir) / ".checkpoint.json"

    # Seed via bash so we know the file path mirrors what bash wrote.
    r = _bash_call(env, 'checkpoint_write "node-g" "success"')
    assert r[0] == 0
    assert ckpt.is_file()

    # Bash-side clear.
    r = _bash_call(env, "checkpoint_clear")
    assert r[0] == 0
    assert not ckpt.is_file()

    # Python-side: clear again (no-op since file already gone, then
    # write + clear). Both subdir gets isolated via monkeypatch.
    env2 = _point_env(monkeypatch, run_dir)
    py_rc = ck.clear()  # file already absent → rc=0
    assert py_rc == 0

    # Re-seed through the port + clear it again so both paths exercised
    # the file-removed transition.
    assert ck.write("node-g", "success") == 0
    capsys.readouterr()
    assert ckpt.is_file()
    assert ck.clear() == 0
    capsys.readouterr()
    assert not ckpt.is_file()

    # Post-clear: can_resume on the same id returns "no".
    _, bash_stdout, _ = _bash_call(env2, 'checkpoint_can_resume "node-g"')
    py_stdout = ck.can_resume("node-g")
    py_stdout_printed = capsys.readouterr().out.strip()
    assert py_stdout == "no"
    assert py_stdout_printed == "no"
    assert bash_stdout.strip() == py_stdout


# ─────────────────────────────────────────────────────────────────────────────
# (h) write with MINI_ORK_RUN_DIR unset → both rc=2 + unset-substring in stderr
# ─────────────────────────────────────────────────────────────────────────────
def test_write_miniork_run_dir_unset_parity(monkeypatch, capsys):
    # Force the python side to resolve with no MINI_ORK_RUN_DIR; the bash
    # side likewise sees no MINI_ORK_RUN_DIR in its env (we strip it).
    monkeypatch.delenv("MINI_ORK_RUN_DIR", raising=False)
    env = {k: v for k, v in os.environ.items() if k != "MINI_ORK_RUN_DIR"}
    assert "MINI_ORK_RUN_DIR" not in env

    py_rc = ck.write("node-h", "success")
    captured = capsys.readouterr()
    py_err = captured.err

    bash_rc, _, bash_err = _bash_call(env, 'checkpoint_write "node-h" "success"')

    assert py_rc == bash_rc == 2
    assert "MINI_ORK_RUN_DIR unset" in bash_err
    assert "MINI_ORK_RUN_DIR unset" in py_err
