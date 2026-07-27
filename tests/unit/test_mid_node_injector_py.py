"""Unit tests: mini_ork.steering.mid_node_injector (bash parity halves removed; formerly vs lib/mid_node_injector.sh).

Each test drives the Python port and asserts its documented output
contract: the formatter JSON shape + text body, and the tick functions'
fifo/fork-out writes + operator_steering consumption semantics.

Seven cases:
  (1) format_claude_user_msg defaults       — sev='info', src='operator'
  (2) format_claude_user_msg custom         — sev='warn', src='dashboard:abc'
  (3) format_claude_user_msg special chars  — msg with quotes / newlines / Unicode
  (4) format_codex_prompt defaults          — sev='info', src='operator'
  (5) format_codex_prompt custom            — sev='critical', src='operator-cli'
  (6) claude_tick db row diff               — seed 3 rows (one matched, one wrong
                                                role, one expired), call claude_tick,
                                                assert (a) fifo contents == formatted
                                                line for matched row, (b) only the
                                                matched row is consumed
  (7) codex_tick db row diff                — seed 2 rows, write session_id file,
                                                call codex_tick, assert prompts +
                                                fork.out contents + consumption

Environment isolation: each test sets MINI_ORK_DB / MINI_ORK_HOME to a
temp DB initialised by db/init.sh via ``_point_python_env(monkeypatch, db,
home)`` so the Python port doesn't read the shell pytest's main repo
state.db.

Fifo reader: ``open('a').write()`` on a fifo blocks forever if no reader
is attached. To avoid hangs, the claude_tick test launches a background
``cat fifo > out`` reader before the tick so writes don't block.
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
# (1) format_claude_user_msg defaults
# ─────────────────────────────────────────────────────────────────────────────
def test_format_claude_default():
    """``format_claude_user_msg`` defaults (sev='info', src='operator')
    produce the stream-json user-message shape with the steering body."""
    msg = "hello from operator"
    py_out = mni.format_claude_user_msg(msg, "info", "operator")
    parsed = json.loads(py_out)
    assert parsed["type"] == "user"
    assert parsed["message"]["role"] == "user"
    assert parsed["message"]["content"][0]["type"] == "text"
    assert parsed["message"]["content"][0]["text"] == "OPERATOR STEERING [info] (from operator): hello from operator"


# ─────────────────────────────────────────────────────────────────────────────
# (2) format_claude_user_msg custom severity/source
# ─────────────────────────────────────────────────────────────────────────────
def test_format_claude_custom():
    """Custom severity='warn' and source='dashboard:abc' exercise the
    format-string interpolation path."""
    msg = "please review PR #42"
    py_out = mni.format_claude_user_msg(msg, "warn", "dashboard:abc")
    parsed = json.loads(py_out)
    assert parsed["message"]["content"][0]["text"] == "OPERATOR STEERING [warn] (from dashboard:abc): please review PR #42"


# ─────────────────────────────────────────────────────────────────────────────
# (3) format_claude_user_msg special chars — quotes, newlines, Unicode
# ─────────────────────────────────────────────────────────────────────────────
def test_format_claude_special_chars():
    """A message with embedded double-quotes, an escaped newline, and a
    non-ASCII run must round-trip through the JSON encoding."""
    msg = 'she said "hi"\nمرحبا'
    py_out = mni.format_claude_user_msg(msg, "info", "operator")
    parsed = json.loads(py_out)
    assert parsed["message"]["content"][0]["text"] == (
        'OPERATOR STEERING [info] (from operator): she said "hi"\nمرحبا'
    )


# ─────────────────────────────────────────────────────────────────────────────
# (4) format_codex_prompt defaults
# ─────────────────────────────────────────────────────────────────────────────
def test_format_codex_default():
    """``format_codex_prompt``: steering body + newline + 'Continue...'
    suffix."""
    msg = "continue with the rewrite"
    py_out = mni.format_codex_prompt(msg, "info", "operator")
    assert py_out.endswith("Continue your task with this guidance.")
    assert "\n" in py_out
    assert py_out.startswith("OPERATOR STEERING [info] (from operator): continue with the rewrite")


# ─────────────────────────────────────────────────────────────────────────────
# (5) format_codex_prompt custom severity/source
# ─────────────────────────────────────────────────────────────────────────────
def test_format_codex_custom():
    """Custom severity='critical' and source='operator-cli' exercise the
    interpolation path."""
    msg = "abort the dispatch cycle"
    py_out = mni.format_codex_prompt(msg, "critical", "operator-cli")
    assert py_out.startswith("OPERATOR STEERING [critical] (from operator-cli): abort the dispatch cycle")
    assert py_out.endswith("Continue your task with this guidance.")


# ─────────────────────────────────────────────────────────────────────────────
# (6) claude_tick — fifo write + DB consumption
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
    # block on EPIPE/EPERM.
    fifo = str(Path(home) / "fifo_in")
    out = str(Path(home) / "fifo_out")
    os.mkfifo(fifo)
    with open(out, "w") as out_fh:
        reader = subprocess.Popen(
            ["cat", fifo],
            stdout=out_fh, stderr=subprocess.DEVNULL,
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

    # (a) fifo contents == the formatted line for the matched row.
    fifo_contents = Path(out).read_text(encoding="utf-8")
    expected_line = mni.format_claude_user_msg(
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

    # (c) Full SELECT projection — check the relevant fields row-by-row.
    all_rows = _read_all_rows(db)
    by_id = {r["id"]: r for r in all_rows}
    assert by_id[matched_id]["consumed_at"] is not None
    assert by_id[wrong_role_id]["consumed_at"] is None
    assert by_id[expired_id]["consumed_at"] is None
    # Message + role + run_id preserved through consume (mark-only update).
    assert by_id[matched_id]["message"] == "please add the input guard"
    assert by_id[matched_id]["role_target"] == "implementer"
    assert by_id[matched_id]["run_id"] == "r-cl"
    # Float confidence round-tripped.
    assert abs(by_id[matched_id]["confidence"] - 0.8) < 1e-6


# ─────────────────────────────────────────────────────────────────────────────
# (7) codex_tick — DB row diff with session_id present
# ─────────────────────────────────────────────────────────────────────────────
def test_codex_tick_db_diff(tmp_path_factory, monkeypatch):
    """Seed 2 rows for run_id='r-cx' role='implementer'. Write a
    session_id file. codex_tick must:
      (a) return written=2 with prompts in fetch order
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

    # (a) prompts in the order fetch_for returns them (severity DESC then
    # confidence DESC then created_at DESC; both rows here have distinct
    # severities so 'warn' comes before 'info').
    expected_prompt_warn = mni.format_codex_prompt("second steering", "warn", "dashboard")
    expected_prompt_info = mni.format_codex_prompt("first steering", "info", "operator")
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
