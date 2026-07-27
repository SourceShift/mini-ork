"""Unit tests: ``mini_ork.cli.bugs`` (bash parity halves removed; formerly vs ``bin/mini-ork-bugs``).

Each test invokes the Python CLI (``python3 -m mini_ork.cli.bugs``) against
a temp DB seeded by ``db/init.sh`` and asserts stdout / stderr / exit-code
/ DB-state / file-content semantically.

Schema bootstrap: ``sweep`` / ``prioritize`` / ``list`` / ``show`` /
``promote`` all query ``bug_reports`` and (for promote) ``epics``.
``promote`` additionally writes to ``kickoffs/auto/`` under
``$MINI_ORK_ROOT`` — the promote test points ``MINI_ORK_ROOT`` at a
per-test sandbox so the real ``kickoffs/auto/`` is not polluted.

Cases (7):
  (1) help — stdout equals the module ``_USAGE_BLOCK``.
  (2) unknown subcommand — stderr `Unknown subcommand: <x>` + usage + exit 2.
  (3) sweep dedupes — 2 DB rows from 3 jsonl rows (2 dupes + 1 unique).
  (4) sweep --all walks all run dirs regardless of mtime.
  (5) list + show stdout surfaces the seeded rows.
  (6) prioritize --top 3 → 3 rows, ordered by severity/confidence/frequency.
  (7) promote --top 2 creates epic rows + kickoff files + flips status.
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
from mini_ork.cli import bugs as py

INIT_SH = REPO / "db" / "init.sh"


# ─────────────────────────────────────────────────────────────────────────────
# Fixtures / helpers
# ─────────────────────────────────────────────────────────────────────────────
@pytest.fixture
def temp_db(tmp_path, monkeypatch):
    """Spin up a real mini-ork SQLite DB via ``db/init.sh``."""
    for tool in ("bash", "sqlite3"):
        if not shutil.which(tool):
            pytest.skip(f"{tool} not on PATH")
    if not INIT_SH.exists():
        pytest.skip(f"missing db/init.sh at {INIT_SH}")
    home = tmp_path / "home"
    home.mkdir()
    dbp = str(home / "state.db")
    r = subprocess.run(
        ["bash", str(INIT_SH)],
        env={**os.environ, "MINI_ORK_HOME": str(home), "MINI_ORK_DB": dbp},
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        pytest.skip(f"db/init.sh failed: rc={r.returncode}\nstderr={r.stderr}")
    return {"home": str(home), "db": dbp}


def _run_py(args: list[str], *, env_extra: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    """Run ``python3 -m mini_ork.cli.bugs <args>`` with the caller's env."""
    return subprocess.run(
        [sys.executable, "-m", "mini_ork.cli.bugs", *args],
        env={**os.environ, **(env_extra or {})},
        capture_output=True, text=True,
    )


def _row_dicts(db: str, table: str) -> list[dict]:
    """Dump all rows of ``table`` as dicts. Ordered by the table's rowid."""
    con = sqlite3.connect(db)
    try:
        cols = [d[0] for d in con.execute(f"SELECT * FROM {table} LIMIT 0").description]
        rows = con.execute(f"SELECT {', '.join(cols)} FROM {table}").fetchall()
        return [dict(zip(cols, r)) for r in rows]
    finally:
        con.close()


def _seed_bug_report(db: str, *, fingerprint: str, run_id: str, agent_role: str,
                     task_class: str | None, observed_in: str, title: str,
                     description: str, suggested_fix: str, severity: str,
                     confidence: float, frequency: int, status: str = "open",
                     now: int | None = None) -> int:
    """Insert one bug_reports row, returning its id."""
    if now is None:
        now = int(time.time())
    con = sqlite3.connect(db)
    try:
        cur = con.execute(
            """INSERT INTO bug_reports
               (fingerprint, run_id, agent_role, task_class, observed_in,
                title, description, suggested_fix, severity, confidence,
                frequency, status, first_seen_at, last_seen_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (fingerprint, run_id, agent_role, task_class, observed_in,
             title, description, suggested_fix, severity, confidence,
             frequency, status, now, now, now),
        )
        con.commit()
        last_id = cur.lastrowid
        assert last_id is not None
        return int(last_id)
    finally:
        con.close()


# ─────────────────────────────────────────────────────────────────────────────
# (1) help — stdout equals the module _USAGE_BLOCK
# ─────────────────────────────────────────────────────────────────────────────
def test_help(temp_db):
    py_r = _run_py(["help"])
    assert py_r.returncode == 0
    assert py_r.stdout.encode() == py._USAGE_BLOCK.encode()


# ─────────────────────────────────────────────────────────────────────────────
# (2) unknown subcommand → stderr msg + usage + exit 2
# ─────────────────────────────────────────────────────────────────────────────
def test_unknown_subcommand_exits_2(temp_db):
    py_r = _run_py(["frobnicate"])
    assert py_r.returncode == 2
    assert py_r.stderr == "Unknown subcommand: frobnicate\n"
    assert py_r.stdout.encode() == py._USAGE_BLOCK.encode()


# ─────────────────────────────────────────────────────────────────────────────
# (3) sweep dedupes — 2 rows from 2 dupes + 1 unique
# ─────────────────────────────────────────────────────────────────────────────
def test_sweep_inserts_and_dedupes(temp_db):
    """Seed 3 jsonl rows (2 dupes of each other + 1 unique) in one run dir.
    Sweep inserts 2 rows: the dupe-pair fingerprint gets 1 INSERT (freq 1)
    + 1 UPDATE (freq 2); the unique fingerprint gets 1 INSERT.
    """
    run_id_dir = Path(temp_db["home"]) / "runs" / "run-dedupe"
    run_id_dir.mkdir(parents=True)
    sink = run_id_dir / "noticed_bugs.jsonl"
    rows = [
        {"agent_role": "reviewer", "severity": "high", "title": "dup bug",
         "description": "d", "suggested_fix": "f",
         "observed_in": "lib/x", "confidence": 0.7},
        {"agent_role": "reviewer", "severity": "high", "title": "dup bug",
         "description": "d", "suggested_fix": "f",
         "observed_in": "lib/x", "confidence": 0.7},
        {"agent_role": "planner", "severity": "medium",
         "title": "unique bug", "description": "u", "suggested_fix": "g",
         "observed_in": "lib/y", "confidence": 0.5},
    ]
    sink.write_text("\n".join(json.dumps(r, separators=(",", ":")) for r in rows) + "\n")

    env_extra = {"MINI_ORK_HOME": temp_db["home"], "MINI_ORK_DB": temp_db["db"]}

    py_r = _run_py(["sweep"], env_extra=env_extra)
    assert py_r.returncode == 0, f"py sweep failed: {py_r.stderr}"
    assert py_r.stdout == "2\n", f"sweep stdout: {py_r.stdout!r}"
    py_rows = _row_dicts(temp_db["db"], "bug_reports")
    assert len(py_rows) == 2

    # The dup row's frequency: row 1 INSERT (freq=1) + row 2 UPDATE (freq=2).
    dup_row = next(r for r in py_rows if r["title"] == "dup bug")
    assert dup_row["frequency"] == 2, dup_row
    unique_row = next(r for r in py_rows if r["title"] == "unique bug")
    assert unique_row["frequency"] == 1


# ─────────────────────────────────────────────────────────────────────────────
# (4) sweep --all walks all run dirs regardless of mtime
# ─────────────────────────────────────────────────────────────────────────────
def test_sweep_since_and_all_flags(temp_db):
    """Seed 2 run dirs (different mtimes), each with 2 unique jsonl rows.
    ``sweep --since <epoch> --all`` picks ALL sinks regardless of mtime
    (--all overrides --since filter).
    """
    runs_dir = Path(temp_db["home"]) / "runs"
    runs_dir.mkdir(parents=True, exist_ok=True)
    for run_id, idx_offset in [("run-old", 0), ("run-new", 100)]:
        rd = runs_dir / run_id
        rd.mkdir()
        sink = rd / "noticed_bugs.jsonl"
        rows = [
            {"agent_role": "reviewer", "severity": "high",
             "title": f"bug-{idx_offset + i}",
             "description": "d", "suggested_fix": "f",
             "observed_in": "lib/x", "confidence": 0.8}
            for i in range(2)
        ]
        sink.write_text("\n".join(json.dumps(r, separators=(",", ":")) for r in rows) + "\n")
        # Set mtime: run-old = 2024-01-01, run-new = NOW
        if run_id == "run-old":
            os.utime(sink, (1704067200, 1704067200))
        else:
            os.utime(sink, (time.time(), time.time()))

    env_extra = {"MINI_ORK_HOME": temp_db["home"], "MINI_ORK_DB": temp_db["db"]}

    between = 1704067200 + 1  # one second after run-old's mtime
    py_r = _run_py(["sweep", "--since", str(between), "--all"], env_extra=env_extra)
    assert py_r.returncode == 0, f"py failed: {py_r.stderr}"
    py_rows = _row_dicts(temp_db["db"], "bug_reports")
    # Both run dirs picked (--all wins) → 4 unique rows
    assert py_r.stdout == "4\n", f"sweep stdout: {py_r.stdout!r}"
    assert len(py_rows) == 4


# ─────────────────────────────────────────────────────────────────────────────
# (5) list + show stdout surfaces the seeded rows
# ─────────────────────────────────────────────────────────────────────────────
def test_list_and_show(temp_db):
    now = int(time.time())
    seeded_ids = []
    for i, (sev, conf) in enumerate([("high", 0.88), ("medium", 0.5), ("low", 0.1)]):
        bid = _seed_bug_report(
            temp_db["db"], fingerprint=f"fp-list-{i}", run_id=f"run-list-{i}",
            agent_role="reviewer", task_class=None, observed_in="lib/x",
            title=f"List bug {i}", description="d", suggested_fix="f",
            severity=sev, confidence=conf, frequency=1, now=now,
        )
        seeded_ids.append(bid)

    env_extra = {"MINI_ORK_HOME": temp_db["home"], "MINI_ORK_DB": temp_db["db"]}

    py_list = _run_py(["list"], env_extra=env_extra)
    assert py_list.returncode == 0
    for i in range(3):
        assert f"List bug {i}" in py_list.stdout

    for i, bid in enumerate(seeded_ids):
        py_show = _run_py(["show", str(bid)], env_extra=env_extra)
        assert py_show.returncode == 0, f"py show {bid} failed: {py_show.stderr}"
        assert f"List bug {i}" in py_show.stdout
        assert f"fp-list-{i}" in py_show.stdout


# ─────────────────────────────────────────────────────────────────────────────
# (6) prioritize --top 3 → 3 rows ordered by severity/confidence/frequency
# ─────────────────────────────────────────────────────────────────────────────
def test_prioritize_top(temp_db):
    now = int(time.time())
    seed_specs = [
        ("critical", 0.99, 5),
        ("high",     0.88, 4),
        ("high",     0.50, 1),
        ("medium",   0.95, 2),
        ("low",      0.10, 10),
    ]
    for i, (sev, conf, freq) in enumerate(seed_specs):
        _seed_bug_report(
            temp_db["db"], fingerprint=f"fp-prio-{i}", run_id=f"run-p-{i}",
            agent_role="reviewer", task_class=None, observed_in="lib/x",
            title=f"Prio bug {i} " + ("x" * 50),  # long title to exercise substr(title,1,80)
            description="d", suggested_fix="f",
            severity=sev, confidence=conf, frequency=freq, now=now,
        )

    env_extra = {"MINI_ORK_HOME": temp_db["home"], "MINI_ORK_DB": temp_db["db"]}

    py_r = _run_py(["prioritize", "--top", "3"], env_extra=env_extra)
    assert py_r.returncode == 0
    lines = [ln for ln in py_r.stdout.splitlines() if ln.strip()]
    assert len(lines) == 3
    # critical first, then high 0.88 x4, then medium 0.95 x2 (score order)
    assert "Prio bug 0" in lines[0]
    assert "Prio bug 1" in lines[1]
    assert "Prio bug 3" in lines[2]

    # Default (no flag) is --top 10 → all 5 rows.
    py_def = _run_py(["prioritize"], env_extra=env_extra)
    assert py_def.returncode == 0
    def_lines = [ln for ln in py_def.stdout.splitlines() if ln.strip()]
    assert len(def_lines) == 5


# ─────────────────────────────────────────────────────────────────────────────
# (7) promote --top 2 creates epic rows + kickoff files + flips status
# ─────────────────────────────────────────────────────────────────────────────
def test_promote_creates_epic_and_kickoff(temp_db, tmp_path):
    """``promote --top 2`` produces DB state + kickoff files. To avoid
    polluting ``kickoffs/auto/`` under the real REPO, the run is pointed at
    a per-test sandbox via ``MINI_ORK_ROOT``.
    """
    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()

    now = int(time.time())
    for i, (sev, conf, freq) in enumerate([("high", 0.9, 5), ("high", 0.85, 3)]):
        _seed_bug_report(
            temp_db["db"], fingerprint=f"fp-promote-{i}", run_id=f"run-promote-{i}",
            agent_role="reviewer", task_class=None, observed_in="lib/p",
            title=f"Promote bug {i}", description=f"desc {i}",
            suggested_fix=f"fix {i}",
            severity=sev, confidence=conf, frequency=freq, now=now,
        )

    env_extra = {
        "MINI_ORK_HOME": temp_db["home"],
        "MINI_ORK_DB": temp_db["db"],
        "MINI_ORK_ROOT": str(sandbox),
    }

    py_r = _run_py(["promote", "--top", "2"], env_extra=env_extra)
    assert py_r.returncode == 0, f"py promote failed: {py_r.stderr}"
    assert py_r.stdout == "2\n", f"promote stdout: {py_r.stdout!r}"

    py_kickoffs = sorted((sandbox / "kickoffs" / "auto").glob("*.md"))
    py_brs = _row_dicts(temp_db["db"], "bug_reports")
    py_epics = _row_dicts(temp_db["db"], "epics")

    assert len(py_kickoffs) == 2
    # kickoff files carry the bug title + fix
    bodies = "\n".join(f.read_text() for f in py_kickoffs)
    assert "Promote bug 0" in bodies and "Promote bug 1" in bodies
    assert "fix 0" in bodies and "fix 1" in bodies

    # bug_reports.status flipped
    assert all(r["status"] == "queued_as_epic" for r in py_brs)
    assert len(py_epics) == 2
    assert all(e["status"] == "not started" for e in py_epics)

    # ── Idempotence: re-running promote produces 0 new epics ─────────────
    py_rerun = _run_py(["promote", "--top", "2"], env_extra=env_extra)
    assert py_rerun.returncode == 0
    assert py_rerun.stdout == "0\n", f"py rerun stdout: {py_rerun.stdout!r}"
    assert len(_row_dicts(temp_db["db"], "epics")) == 2  # no new epics
