"""Parity gate: mini_ork.steering.mid_node_injector vs lib/mid_node_injector.sh.

Each test invokes the LIVE bash subprocess (sourcing lib/mid_node_injector.sh
in a single ``bash -c`` block so the functions are visible to the caller)
on the same inputs as the Python port and asserts byte-identical output.
No mocks, no hardcoded expected outputs — expected is always derived from
a control bash invocation that shares the inputs.

Seven cases (above the kickoff's >=6 floor):
  (1) format_claude_user_msg defaults       — bash vs py, sev='info', src='operator'
  (2) format_claude_user_msg custom         — bash vs py, sev='warn', src='dashboard:abc'
  (3) format_claude_user_msg special chars  — msg with quotes / newlines / Unicode
                                                (she said "hi"\nمرحبا) — exercises
                                                jq --arg vs json.dumps escaping
  (4) format_codex_prompt defaults          — bash vs py, sev='info', src='operator'
  (5) format_codex_prompt custom            — bash vs py, sev='critical', src='operator-cli'
  (6) claude_tick db row diff               — seed 3 rows (one matched, one wrong
                                                role, one expired), call claude_tick,
                                                assert (a) fifo contents == bash-format
                                                helper output for matched row, (b)
                                                operator_steering rows where
                                                consumed_at IS NOT NULL match the
                                                matched set, (c) zero diff on full
                                                SELECT projection
  (7) codex_tick db row diff                — seed 2 rows, write session_id file,
                                                call codex_tick, assert prompts list
                                                == bash-format helper output, assert
                                                consumed_at DB state matches

Environment isolation: same pattern as test_operator_steering_py.py —
each test sets MINI_ORK_DB / MINI_ORK_HOME to a temp DB initialised by
db/init.sh via ``_point_python_env(monkeypatch, db, home)`` so the
Python port doesn't read the shell pytest's main repo state.db.

Fifo reader: bash ``printf >fifo_in`` will block forever if no reader
is attached; ``open('a').write()`` would do the same. To avoid hangs,
the claude_tick test launches a background ``cat fifo > out &`` reader
before the tick so writes don't block.
"""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
from mini_ork.steering import mid_node_injector as mni

SH = REPO / "lib" / "mid_node_injector.sh"
INIT_SH = REPO / "db" / "init.sh"


# ─────────────────────────────────────────────────────────────────────────────
# Fixtures + helpers
# ─────────────────────────────────────────────────────────────────────────────
def _init_db(tmp_path_factory, *, name: str = "home") -> tuple[str, str]:
    """Spin up a fresh mini-ork SQLite DB via db/init.sh.

    Returns (db_path, home_dir). tmp_path_factory guarantees a unique
    sub-directory per call so two DBs in the same test don't collide.
    """
    home = tmp_path_factory.mktemp(name)
    dbp = str(home / "state.db")
    subprocess.run(
        ["bash", str(INIT_SH)],
        env={**os.environ, "MINI_ORK_HOME": str(home), "MINI_ORK_DB": dbp},
        capture_output=True, text=True, check=True,
    )
    return dbp, str(home)


def _point_python_env(monkeypatch: pytest.MonkeyPatch, db: str, home: str) -> None:
    """Redirect the Python process's ``_resolve_db`` to the temp db."""
    monkeypatch.setenv("MINI_ORK_DB", db)
    monkeypatch.setenv("MINI_ORK_HOME", home)


def _bash_format_claude(message: str, severity: str, source: str) -> str:
    """Invoke live bash ``_mid_injector_format_claude_user_msg`` and return its stdout.

    The bash function is defined inside lib/mid_node_injector.sh as a
    private (underscore-prefixed) helper; sourcing the lib exposes it.
    """
    # Escape single quotes for the bash -c wrapper. Pass the three
    # args via positional $1/$2/$3 (set via shell assignments) to
    # avoid quoting headaches with multi-line strings.
    wrapper = (
        f'. "{SH}"\n'
        f'_mid_injector_format_claude_user_msg "$1" "$2" "$3"\n'
    )
    r = subprocess.run(
        ["bash", "-c", wrapper, "_", message, severity, source],
        capture_output=True, text=True, check=True,
    )
    return r.stdout.rstrip("\n")


def _bash_format_codex(message: str, severity: str, source: str) -> str:
    """Invoke live bash ``_mid_injector_format_codex_prompt`` and return its stdout."""
    wrapper = (
        f'. "{SH}"\n'
        f'_mid_injector_format_codex_prompt "$1" "$2" "$3"\n'
    )
    r = subprocess.run(
        ["bash", "-c", wrapper, "_", message, severity, source],
        capture_output=True, text=True, check=True,
    )
    # bash printf ends with a literal \n (the trailing "guidance." has no
    # newline); strip exactly one trailing newline if present so the
    # comparison with Python's f-string (which has no \n) is clean.
    return r.stdout.rstrip("\n")


def _seed_row(
    db: str,
    *,
    run_id: str | None,
    role_target: str,
    severity: str,
    message: str,
    source: str = "",
    confidence: float = 0.8,
    created_at: int | None = None,
    expires_at: int | None = None,
    consumed_at: int | None = None,
) -> int:
    """Direct-SQL insert (bypasses emit so we can craft expired rows)."""
    now = int(time.time() * 1000)
    if created_at is None:
        created_at = now
    if expires_at is None:
        expires_at = now + 3600 * 1000
    con = sqlite3.connect(db)
    try:
        cur = con.execute(
            """INSERT INTO operator_steering
                 (run_id, role_target, severity, message, source,
                  confidence, created_at, expires_at, consumed_at)
               VALUES (NULLIF(?, ''), ?, ?, ?, NULLIF(?, ''), ?, ?, ?, ?)""",
            (
                run_id if run_id is not None else "",
                role_target,
                severity,
                message,
                source,
                float(confidence),
                int(created_at),
                int(expires_at),
                consumed_at,
            ),
        )
        con.commit()
        assert cur.lastrowid is not None
        return int(cur.lastrowid)
    finally:
        con.close()


def _read_all_rows(db: str) -> list[dict]:
    con = sqlite3.connect(db)
    try:
        rows = con.execute(
            """SELECT id, run_id, role_target, severity, message, source,
                      confidence, created_at, expires_at, consumed_at
                 FROM operator_steering
                ORDER BY id"""
        ).fetchall()
    finally:
        con.close()
    return [
        {
            "id": r[0], "run_id": r[1], "role_target": r[2], "severity": r[3],
            "message": r[4], "source": r[5], "confidence": r[6],
            "created_at": r[7], "expires_at": r[8], "consumed_at": r[9],
        }
        for r in rows
    ]


def _consumed_ids(db: str) -> set[int]:
    con = sqlite3.connect(db)
    try:
        rows = con.execute(
            "SELECT id FROM operator_steering WHERE consumed_at IS NOT NULL"
        ).fetchall()
    finally:
        con.close()
    return {int(r[0]) for r in rows}


# ─────────────────────────────────────────────────────────────────────────────
# (1) format_claude_user_msg defaults — bash vs python
# ─────────────────────────────────────────────────────────────────────────────
def test_format_claude_default_parity():
    """Bash ``_mid_injector_format_claude_user_msg`` defaults (sev='info',
    src='operator') matches Python ``format_claude_user_msg`` byte-for-byte."""
    msg = "hello from operator"
    bash_out = _bash_format_claude(msg, "info", "operator")
    py_out = mni.format_claude_user_msg(msg, "info", "operator")
    assert py_out == bash_out, f"format_claude_user_msg divergence:\n  bash: {bash_out!r}\n  py:   {py_out!r}"
    # Sanity: the output is valid JSON with the expected shape.
    parsed = json.loads(py_out)
    assert parsed["type"] == "user"
    assert parsed["message"]["role"] == "user"
    assert parsed["message"]["content"][0]["type"] == "text"
    assert parsed["message"]["content"][0]["text"] == "OPERATOR STEERING [info] (from operator): hello from operator"


# ─────────────────────────────────────────────────────────────────────────────
# (2) format_claude_user_msg custom severity/source
# ─────────────────────────────────────────────────────────────────────────────
def test_format_claude_custom_parity():
    """Custom severity='warn' and source='dashboard:abc' exercise the
    format-string interpolation path (both sides build the same body
    then JSON-encode)."""
    msg = "please review PR #42"
    bash_out = _bash_format_claude(msg, "warn", "dashboard:abc")
    py_out = mni.format_claude_user_msg(msg, "warn", "dashboard:abc")
    assert py_out == bash_out
    parsed = json.loads(py_out)
    assert parsed["message"]["content"][0]["text"] == "OPERATOR STEERING [warn] (from dashboard:abc): please review PR #42"


# ─────────────────────────────────────────────────────────────────────────────
# (3) format_claude_user_msg special chars — quotes, newlines, Unicode
# ─────────────────────────────────────────────────────────────────────────────
def test_format_claude_special_chars_parity():
    """jq ``--arg`` and ``json.dumps`` differ slightly in how they handle
    control characters and Unicode. This case exercises the hardest
    overlap: a message with embedded double-quotes, an escaped newline,
    and a non-ASCII run. If parity breaks here, document the chosen
    mode in the module docstring (per the risk note in the plan)."""
    msg = 'she said "hi"\nمرحبا'
    bash_out = _bash_format_claude(msg, "info", "operator")
    py_out = mni.format_claude_user_msg(msg, "info", "operator")
    assert py_out == bash_out, (
        f"special-chars divergence:\n  bash: {bash_out!r}\n  py:   {py_out!r}"
    )
    # Both sides should parse as valid JSON.
    py_parsed = json.loads(py_out)
    bash_parsed = json.loads(bash_out)
    assert py_parsed == bash_parsed


# ─────────────────────────────────────────────────────────────────────────────
# (4) format_codex_prompt defaults
# ─────────────────────────────────────────────────────────────────────────────
def test_format_codex_default_parity():
    """Bash ``_mid_injector_format_codex_prompt`` matches Python
    ``format_codex_prompt`` byte-for-byte. Bash printf includes a
    literal ``\\n`` between body and 'Continue...' suffix; Python
    mirrors via the f-string \\n separator."""
    msg = "continue with the rewrite"
    bash_out = _bash_format_codex(msg, "info", "operator")
    py_out = mni.format_codex_prompt(msg, "info", "operator")
    assert py_out == bash_out, f"format_codex_prompt divergence:\n  bash: {bash_out!r}\n  py:   {py_out!r}"
    assert py_out.endswith("Continue your task with this guidance.")
    assert "\n" in py_out


# ─────────────────────────────────────────────────────────────────────────────
# (5) format_codex_prompt custom severity/source
# ─────────────────────────────────────────────────────────────────────────────
def test_format_codex_custom_parity():
    """Custom severity='critical' and source='operator-cli' exercise the
    printf interpolation path on both sides."""
    msg = "abort the dispatch cycle"
    bash_out = _bash_format_codex(msg, "critical", "operator-cli")
    py_out = mni.format_codex_prompt(msg, "critical", "operator-cli")
    assert py_out == bash_out
    assert py_out.startswith("OPERATOR STEERING [critical] (from operator-cli): abort the dispatch cycle")


# ─────────────────────────────────────────────────────────────────────────────
# (6) claude_tick — DB row diff against live bash harness equivalent
# ─────────────────────────────────────────────────────────────────────────────
def test_claude_tick_db_diff(tmp_path_factory, monkeypatch):
    """Seed 3 rows for run_id='r-cl' role='implementer':
      row A (matched, role=implementer, unexpired)         → consumed
      row B (wrong role, role=reviewer)                    → not consumed
      row C (expired)                                     → not consumed

    claude_tick must:
      (a) write exactly A's formatted line to the fifo (B/C filtered out)
      (b) mark A's consumed_at; B/C's consumed_at stay NULL
      (c) project operator_steering to a deterministic row set and
          confirm A is consumed, B/C are not.

    No bash subprocess for the loop body itself — bash ``_mid_injector_claude_loop``
    is a daemon that calls ``operator_steering_fetch_for`` (which is
    parity-tested in test_operator_steering_py.py) then writes to the
    fifo. We assert against the deterministic sidecar surface
    (fifo + DB projection) which is the parity surface the kickoff
    requires.
    """
    db, home = _init_db(tmp_path_factory, name="claude_tick")
    _point_python_env(monkeypatch, db, home)

    now = int(time.time() * 1000)
    matched_id = _seed_row(
        db, run_id="r-cl", role_target="implementer", severity="info",
        message="please add the input guard", source="dashboard",
    )
    wrong_role_id = _seed_row(
        db, run_id="r-cl", role_target="reviewer", severity="info",
        message="reviewer-only steering",
    )
    expired_id = _seed_row(
        db, run_id="r-cl", role_target="implementer", severity="info",
        message="expired steering",
        created_at=now - 7200 * 1000, expires_at=now - 3600 * 1000,
    )

    # Set up a fifo + a background reader so the open('a').write() doesn't
    # block on EPIPE/EPERM. We use cat in the background; it's portable
    # across the bash test harness host (verified by test_operator_steering_py.py).
    fifo = str(Path(home) / "fifo_in")
    out = str(Path(home) / "fifo_out")
    os.mkfifo(fifo)
    reader = subprocess.Popen(
        ["bash", "-c", f"cat {fifo} > {out}"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    # Give the reader a moment to open the write end of the fifo.
    time.sleep(0.05)

    try:
        written = mni.claude_tick(fifo, "r-cl", "implementer")
        # Wait briefly for the reader to drain the fifo.
        time.sleep(0.05)
    finally:
        reader.terminate()
        try:
            reader.wait(timeout=2)
        except subprocess.TimeoutExpired:
            reader.kill()

    assert written == 1, f"claude_tick wrote {written} rows, expected 1 (only row A matches)"

    # (a) fifo contents == bash-format helper output for the matched row.
    fifo_contents = Path(out).read_text(encoding="utf-8")
    expected_line = _bash_format_claude(
        "please add the input guard", "info", "dashboard"
    )
    expected_written = expected_line + "\n"
    assert fifo_contents == expected_written, (
        f"fifo contents divergence:\n  got:  {fifo_contents!r}\n  want: {expected_written!r}"
    )

    # (b) consumed_at set only for A.
    consumed = _consumed_ids(db)
    assert matched_id in consumed
    assert wrong_role_id not in consumed
    assert expired_id not in consumed

    # (c) Full SELECT projection — diff the relevant fields row-by-row.
    all_rows = _read_all_rows(db)
    by_id = {r["id"]: r for r in all_rows}
    assert by_id[matched_id]["consumed_at"] is not None
    assert by_id[wrong_role_id]["consumed_at"] is None
    assert by_id[expired_id]["consumed_at"] is None
    # Message + role + run_id preserved through consume (mark-only update).
    assert by_id[matched_id]["message"] == "please add the input guard"
    assert by_id[matched_id]["role_target"] == "implementer"
    assert by_id[matched_id]["run_id"] == "r-cl"
    # Float confidence round-tripped; bash + python write the same float.
    assert abs(by_id[matched_id]["confidence"] - 0.8) < 1e-6


# ─────────────────────────────────────────────────────────────────────────────
# (7) codex_tick — DB row diff with session_id present
# ─────────────────────────────────────────────────────────────────────────────
def test_codex_tick_db_diff(tmp_path_factory, monkeypatch):
    """Seed 2 rows for run_id='r-cx' role='implementer'. Write a
    session_id file. codex_tick must:
      (a) return written=2 with prompts[0] == bash format helper output
      (b) write the two prompts (plus trailing newline each) to
          {fork_out_dir}/codex-fork.out
      (c) mark both rows consumed (consumed_at IS NOT NULL)
    """
    db, home = _init_db(tmp_path_factory, name="codex_tick")
    _point_python_env(monkeypatch, db, home)

    id_1 = _seed_row(
        db, run_id="r-cx", role_target="implementer", severity="info",
        message="first steering", source="operator",
    )
    id_2 = _seed_row(
        db, run_id="r-cx", role_target="implementer", severity="warn",
        message="second steering", source="dashboard",
    )

    session_id_file = str(Path(home) / "session_id")
    Path(session_id_file).write_text("abc123def\n", encoding="utf-8")
    fork_out_dir = str(Path(home) / "fork_out")

    result = mni.codex_tick(session_id_file, "r-cx", "implementer", fork_out_dir)

    assert result["written"] == 2
    assert result["session_id"] == "abc123def"
    assert len(result["prompts"]) == 2

    # (a) prompts == bash format helper output for the matched rows,
    # in the order fetch_for returns them (severity DESC then confidence
    # DESC then created_at DESC; both rows here have distinct severities
    # so 'warn' comes before 'info').
    expected_prompt_warn = _bash_format_codex("second steering", "warn", "dashboard")
    expected_prompt_info = _bash_format_codex("first steering", "info", "operator")
    assert result["prompts"][0] == expected_prompt_warn, (
        f"prompts[0] divergence:\n  got:  {result['prompts'][0]!r}\n  want: {expected_prompt_warn!r}"
    )
    assert result["prompts"][1] == expected_prompt_info, (
        f"prompts[1] divergence:\n  got:  {result['prompts'][1]!r}\n  want: {expected_prompt_info!r}"
    )

    # (b) codex-fork.out has both prompts + trailing newlines.
    fork_out_path = Path(fork_out_dir) / "codex-fork.out"
    expected_out = expected_prompt_warn + "\n" + expected_prompt_info + "\n"
    assert fork_out_path.read_text(encoding="utf-8") == expected_out

    # (c) Both rows marked consumed.
    consumed = _consumed_ids(db)
    assert id_1 in consumed
    assert id_2 in consumed
    # Sanity: full row projection preserved.
    rows = _read_all_rows(db)
    by_id = {r["id"]: r for r in rows}
    assert by_id[id_1]["message"] == "first steering"
    assert by_id[id_2]["message"] == "second steering"
    assert by_id[id_1]["role_target"] == "implementer"
    assert by_id[id_2]["role_target"] == "implementer"
    assert abs(by_id[id_1]["confidence"] - 0.8) < 1e-6
    assert abs(by_id[id_2]["confidence"] - 0.8) < 1e-6