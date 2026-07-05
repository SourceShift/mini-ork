"""Parity gate: ``mini_ork.ported.mini_ork_bugs`` vs ``bin/mini-ork-bugs``.

Each test invokes the LIVE bash dispatcher against a temp DB seeded by
``db/init.sh``, then invokes the Python port against the SAME temp DB
(after restoring DB state to the pre-bash snapshot), and asserts the
stdout / stderr / exit-code / file-content match byte-for-byte.

No mocks, no hardcoded expected outputs — the expected output is always
the live bash control invocation. This is the strangler-fig invariant:
the bash script stays in place; the Python port must match its
observable behavior exactly so the migration can proceed module-by-module
without breaking operator workflows.

Schema bootstrap: bash's ``bug_report_sweep`` / ``prioritize`` /
``list`` / ``show`` / ``promote`` all query ``bug_reports`` and (for
promote) ``epics``. ``bug_report_promote`` additionally writes to
``kickoffs/auto/`` under ``$MINI_ORK_ROOT``. The promote test points
``MINI_ORK_ROOT`` at a per-test sandbox (with ``lib/`` symlinked to the
real repo so bash can source the library) so neither run pollutes the
real ``kickoffs/auto/``.

Cases (7, above the kickoff's >=6 floor):
  (1) help parity — bash `help` stdout == Python `_USAGE_BLOCK` byte-for-byte.
  (2) unknown subcommand — stderr `Unknown subcommand: <x>` + usage + exit 2.
  (3) sweep dedupes — bash and py both insert 1 row from 3 jsonl rows (2 dupes + 1 unique).
  (4) sweep --all walks all run dirs — bash and py produce identical DB state.
  (5) list + show stdout format parity (sqlite3 -line for show, pipe-separator for list).
  (6) prioritize --top 3 stdout row format parity (printf column shapes).
  (7) promote --top 2 creates epic rows + kickoff files + flips status, byte-equal.

Tolerance notes:
  * confidence floats compared at 1e-6 (kickoff tolerance).
  * first_seen_at / last_seen_at / updated_at allowed within 1-second window
    per the kickoff's epoch-drift policy.
  * sweep/promote tests wipe DB state between bash and py runs so each
    starts from the same pre-state.
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
from mini_ork.ported import mini_ork_bugs as py  # noqa: E402

BASH = REPO / "bin" / "mini-ork-bugs"
INIT_SH = REPO / "db" / "init.sh"


# ─────────────────────────────────────────────────────────────────────────────
# Fixtures / helpers
# ─────────────────────────────────────────────────────────────────────────────
def _which_tools() -> None:
    for tool in ("bash", "sqlite3", "python3"):
        if not shutil.which(tool):
            pytest.skip(f"{tool} not on PATH")
    if not BASH.exists():
        pytest.skip(f"missing bin/mini-ork-bugs at {BASH}")
    if not INIT_SH.exists():
        pytest.skip(f"missing db/init.sh at {INIT_SH}")


@pytest.fixture
def temp_db(tmp_path, monkeypatch):
    """Spin up a real mini-ork SQLite DB via ``db/init.sh``.

    The bash dispatcher resolves ``MINI_ORK_DB`` / ``MINI_ORK_HOME`` from
    env, and the Python port's ``_ensure_env()`` does the same. The test
    subprocesses pass both env vars so bash and py land on the same DB.
    """
    _which_tools()
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


def _run_bash(args: list[str], *, env_extra: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    """Run ``bin/mini-ork-bugs <args>`` with ``MINI_ORK_HOME``/``MINI_ORK_DB``
    inherited from the caller (set by the test)."""
    return subprocess.run(
        ["bash", str(BASH), *args],
        env={**os.environ, **(env_extra or {})},
        capture_output=True, text=True,
    )


def _run_py(args: list[str], *, env_extra: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    """Run ``python3 -m mini_ork.ported.mini_ork_bugs <args>`` with the
    caller's env. Python inherits MINI_ORK_HOME/MINI_ORK_DB via os.environ
    from the parent pytest process."""
    return subprocess.run(
        [sys.executable, "-m", "mini_ork.ported.mini_ork_bugs", *args],
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


def _wipe_db(db: str) -> None:
    """Reset bug_reports + epics to empty (used between bash and py runs
    so each starts from the same pre-state)."""
    con = sqlite3.connect(db)
    try:
        con.execute("DELETE FROM bug_reports")
        con.execute("DELETE FROM epics")
        con.commit()
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


def _reset_bug_status(db: str) -> None:
    """Flip every bug_reports row back to ``status='open'`` and clear
    ``promoted_to_epic_id`` — used between bash and py promote runs so
    py starts from the same pre-state as bash."""
    con = sqlite3.connect(db)
    try:
        con.execute(
            "UPDATE bug_reports SET status='open', promoted_to_epic_id=NULL, "
            "updated_at=strftime('%s','now') WHERE status='queued_as_epic'"
        )
        con.commit()
    finally:
        con.close()


def _fingerprint_matches(a: dict, b: dict, *, epoch_window_s: int = 1) -> bool:
    """Two bug_reports rows are "the same bug" if all columns except the
    auto-incremented ``id`` and volatile timestamps match. Float: 1e-6.
    Epoch: 1-second window.
    """
    skip = {"id"}
    if set(a.keys()) != set(b.keys()):
        return False
    for k in a.keys():
        if k in skip:
            continue
        va, vb = a[k], b[k]
        if k in ("first_seen_at", "last_seen_at", "updated_at"):
            if abs(int(va or 0) - int(vb or 0)) > epoch_window_s:
                return False
            continue
        if k == "confidence":
            if abs(float(va or 0) - float(vb or 0)) > 1e-6:
                return False
            continue
        if va != vb:
            return False
    return True


# ─────────────────────────────────────────────────────────────────────────────
# (1) help parity — bash `help` stdout == Python `_USAGE_BLOCK` byte-for-byte
# ─────────────────────────────────────────────────────────────────────────────
def test_help_parity(temp_db):
    """The hand-mirrored ``_USAGE_BLOCK`` must match what bash emits via
    ``sed -n '2,16p' "$0" | sed 's/^# \\{0,1\\}//'`` byte-for-byte. Drift
    in the bash docblock (e.g. someone edits bin/mini-ork-bugs lines 2-16)
    breaks this test, which is the desired behavior per the risk_notes.
    """
    bash_r = _run_bash(["help"])
    py_r = _run_py(["help"])
    assert bash_r.returncode == 0
    assert py_r.returncode == 0
    assert bash_r.stdout == py_r.stdout, (
        f"help drift: bash={bash_r.stdout!r} py={py_r.stdout!r}"
    )
    # Also confirm it equals the literal at import time (catches a third
    # party who somehow changes the env between calls).
    assert py_r.stdout.encode() == py._USAGE_BLOCK.encode()


# ─────────────────────────────────────────────────────────────────────────────
# (2) unknown subcommand → stderr msg + usage + exit 2
# ─────────────────────────────────────────────────────────────────────────────
def test_unknown_subcommand_exits_2(temp_db):
    """The bash case `*` branch hard-codes exit 2. The Python port must
    match: stderr `Unknown subcommand: <x>`, stdout usage, exit code 2.
    """
    bash_r = _run_bash(["frobnicate"])
    py_r = _run_py(["frobnicate"])
    assert bash_r.returncode == 2
    assert py_r.returncode == 2
    assert py_r.stderr == "Unknown subcommand: frobnicate\n"
    assert bash_r.stderr == py_r.stderr, (
        f"stderr drift: bash={bash_r.stderr!r} py={py_r.stderr!r}"
    )
    assert bash_r.stdout == py_r.stdout, (
        f"stdout drift: bash={bash_r.stdout!r} py={py_r.stdout!r}"
    )


# ─────────────────────────────────────────────────────────────────────────────
# (3) sweep dedupes — both bash and py insert 2 rows from 2 dupes + 1 unique
# ─────────────────────────────────────────────────────────────────────────────
def test_sweep_inserts_and_dedupes(temp_db):
    """Seed 3 jsonl rows (2 dupes of each other + 1 unique) in one run dir.
    Bash sweep inserts 2 rows: the dupe-pair fingerprint gets 1 INSERT
    (row 1 inserts) + 1 UPDATE (row 2 hits it, bumps frequency); the
    unique fingerprint gets 1 INSERT. Py sweep against the same starting
    state produces identical stdout and DB rows.
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

    # Bash run
    bash_r = _run_bash(["sweep"], env_extra=env_extra)
    assert bash_r.returncode == 0, f"bash sweep failed: {bash_r.stderr}"
    assert bash_r.stdout == "2\n", f"bash sweep stdout: {bash_r.stdout!r}"
    bash_rows = _row_dicts(temp_db["db"], "bug_reports")
    assert len(bash_rows) == 2, f"bash inserted {len(bash_rows)} rows, expected 2"

    # Wipe DB so py starts from the same pre-state.
    _wipe_db(temp_db["db"])

    # Py run
    py_r = _run_py(["sweep"], env_extra=env_extra)
    assert py_r.returncode == 0, f"py sweep failed: {py_r.stderr}"
    assert py_r.stdout == bash_r.stdout, (
        f"stdout drift: bash={bash_r.stdout!r} py={py_r.stdout!r}"
    )
    py_rows = _row_dicts(temp_db["db"], "bug_reports")
    assert len(py_rows) == len(bash_rows)

    # Match bash_post vs py_post row-by-row (sorted by fingerprint).
    bash_sorted = sorted(bash_rows, key=lambda r: r["fingerprint"])
    py_sorted = sorted(py_rows, key=lambda r: r["fingerprint"])
    for b, p in zip(bash_sorted, py_sorted):
        assert _fingerprint_matches(b, p), f"row diff:\nbash={b}\npy={p}"

    # The dup row's frequency: row 1 INSERT (freq=1) + row 2 UPDATE
    # (freq=2). Bash and py must agree on frequency.
    dup_row = next(r for r in bash_sorted if r["title"] == "dup bug")
    dup_py = next(r for r in py_sorted if r["title"] == "dup bug")
    assert dup_row["frequency"] == 2, dup_row
    assert dup_py["frequency"] == dup_row["frequency"]


# ─────────────────────────────────────────────────────────────────────────────
# (4) sweep --all walks all run dirs regardless of mtime
# ─────────────────────────────────────────────────────────────────────────────
def test_sweep_since_and_all_flags(temp_db):
    """Seed 2 run dirs (different mtimes), each with 2 unique jsonl rows.
    Bash ``sweep --since <epoch> --all`` picks ALL sinks regardless of
    mtime (--all overrides --since filter); py does the same. stdout +
    DB rows must match.
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

    # Bash run
    between = 1704067200 + 1  # one second after run-old's mtime
    bash_r = _run_bash(["sweep", "--since", str(between), "--all"], env_extra=env_extra)
    assert bash_r.returncode == 0, f"bash failed: {bash_r.stderr}"
    bash_rows = _row_dicts(temp_db["db"], "bug_reports")
    # Both run dirs picked (--all wins) → 4 unique rows
    assert bash_r.stdout == "4\n", f"bash stdout: {bash_r.stdout!r}"
    assert len(bash_rows) == 4

    # Wipe
    _wipe_db(temp_db["db"])

    # Py run
    py_r = _run_py(["sweep", "--since", str(between), "--all"], env_extra=env_extra)
    assert py_r.returncode == 0, f"py failed: {py_r.stderr}"
    py_rows = _row_dicts(temp_db["db"], "bug_reports")

    assert py_r.stdout == bash_r.stdout, (
        f"stdout drift: bash={bash_r.stdout!r} py={py_r.stdout!r}"
    )
    assert len(py_rows) == len(bash_rows) == 4

    bash_sorted = sorted(bash_rows, key=lambda r: r["fingerprint"])
    py_sorted = sorted(py_rows, key=lambda r: r["fingerprint"])
    for b, p in zip(bash_sorted, py_sorted):
        assert _fingerprint_matches(b, p), f"row diff:\nbash={b}\npy={p}"


# ─────────────────────────────────────────────────────────────────────────────
# (5) list + show stdout format parity
# ─────────────────────────────────────────────────────────────────────────────
def test_list_and_show_format_parity(temp_db):
    """Seed 3 bug_reports rows. Bash and py ``list`` + ``show`` stdout
    must match byte-for-byte. Floats compared at 1e-6 inline in the
    sqlite3 -line output (e.g. ``confidence = 0.88``).
    """
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

    bash_list = _run_bash(["list"], env_extra=env_extra)
    py_list = _run_py(["list"], env_extra=env_extra)
    assert bash_list.returncode == 0
    assert py_list.returncode == 0
    assert bash_list.stdout == py_list.stdout, (
        f"list drift: bash={bash_list.stdout!r} py={py_list.stdout!r}"
    )

    for bid in seeded_ids:
        bash_show = _run_bash(["show", str(bid)], env_extra=env_extra)
        py_show = _run_py(["show", str(bid)], env_extra=env_extra)
        assert bash_show.returncode == 0, f"bash show {bid} failed: {bash_show.stderr}"
        assert py_show.returncode == 0, f"py show {bid} failed: {py_show.stderr}"
        assert bash_show.stdout == py_show.stdout, (
            f"show {bid} drift: bash={bash_show.stdout!r} py={py_show.stdout!r}"
        )


# ─────────────────────────────────────────────────────────────────────────────
# (6) prioritize --top 3 stdout row format parity
# ─────────────────────────────────────────────────────────────────────────────
def test_prioritize_top_parity(temp_db):
    """Seed 5 bug_reports with varied severity/frequency/confidence. Bash
    and py ``prioritize --top 3`` stdout must match byte-for-byte (3
    rows of printf column shapes joined by `` | ``).
    """
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

    bash_r = _run_bash(["prioritize", "--top", "3"], env_extra=env_extra)
    py_r = _run_py(["prioritize", "--top", "3"], env_extra=env_extra)
    assert bash_r.returncode == 0
    assert py_r.returncode == 0
    assert bash_r.stdout == py_r.stdout, (
        f"prioritize drift:\nbash={bash_r.stdout!r}\npy={py_r.stdout!r}"
    )

    # Bash `prioritize` (no flag) defaults to --top 10. Confirm bash+py
    # also agree on the default.
    bash_def = _run_bash(["prioritize"], env_extra=env_extra)
    py_def = _run_py(["prioritize"], env_extra=env_extra)
    assert bash_def.returncode == 0
    assert py_def.returncode == 0
    assert bash_def.stdout == py_def.stdout, (
        f"prioritize default drift:\nbash={bash_def.stdout!r}\npy={py_def.stdout!r}"
    )


# ─────────────────────────────────────────────────────────────────────────────
# (7) promote --top 2 creates epic rows + kickoff files + flips status
# ─────────────────────────────────────────────────────────────────────────────
def test_promote_creates_epic_and_kickoff(temp_db, tmp_path):
    """Bash and py ``promote --top 2`` produce identical stdout + DB state
    + kickoff file contents. To avoid polluting ``kickoffs/auto/`` under
    the real REPO, both runs are pointed at a per-test sandbox via
    ``MINI_ORK_ROOT``. The sandbox has ``lib/`` symlinked to the real
    REPO's lib so bash can source the library.
    """
    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    # Bash sources "$MINI_ORK_ROOT/lib/bug_report.sh"; symlink so bash
    # finds the library without polluting the real repo.
    os.symlink(str(REPO / "lib"), str(sandbox / "lib"), target_is_directory=True)

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

    # ── Bash run ────────────────────────────────────────────────────────
    bash_r = _run_bash(["promote", "--top", "2"], env_extra=env_extra)
    assert bash_r.returncode == 0, f"bash promote failed: {bash_r.stderr}"
    assert bash_r.stdout == "2\n", f"bash stdout: {bash_r.stdout!r}"

    bash_kickoffs = sorted((sandbox / "kickoffs" / "auto").glob("*.md"))
    bash_brs = _row_dicts(temp_db["db"], "bug_reports")
    bash_epics = _row_dicts(temp_db["db"], "epics")
    assert len(bash_kickoffs) == 2
    assert all(r["status"] == "queued_as_epic" for r in bash_brs)
    assert len(bash_epics) == 2
    assert all(e["status"] == "not started" for e in bash_epics)

    # Snapshot bash kickoff file bytes for content comparison after py.
    bash_kickoff_bytes = {f.name: f.read_bytes() for f in bash_kickoffs}

    # ── Wipe back to pre-bash state ──────────────────────────────────────
    _reset_bug_status(temp_db["db"])
    con = sqlite3.connect(temp_db["db"])
    try:
        con.execute("DELETE FROM epics")
        con.commit()
    finally:
        con.close()
    for f in bash_kickoffs:
        f.unlink()

    # ── Py run ──────────────────────────────────────────────────────────
    py_r = _run_py(["promote", "--top", "2"], env_extra=env_extra)
    assert py_r.returncode == 0, f"py promote failed: {py_r.stderr}"

    py_kickoffs = sorted((sandbox / "kickoffs" / "auto").glob("*.md"))
    py_brs = _row_dicts(temp_db["db"], "bug_reports")
    py_epics = _row_dicts(temp_db["db"], "epics")

    # stdout byte-equal
    assert py_r.stdout == bash_r.stdout, (
        f"promote stdout drift: bash={bash_r.stdout!r} py={py_r.stdout!r}"
    )

    # Same file count + same filenames
    assert len(py_kickoffs) == len(bash_kickoffs) == 2
    assert [f.name for f in py_kickoffs] == sorted(bash_kickoff_bytes.keys()), (
        f"kickoff filenames drift: "
        f"bash={sorted(bash_kickoff_bytes.keys())} py={[f.name for f in py_kickoffs]}"
    )

    # Each kickoff file byte-equal between bash and py
    for pf in py_kickoffs:
        bf_bytes = bash_kickoff_bytes[pf.name]
        pf_bytes = pf.read_bytes()
        assert pf_bytes == bf_bytes, (
            f"kickoff content drift {pf.name}:\n"
            f"bash ({len(bf_bytes)} bytes) vs py ({len(pf_bytes)} bytes)"
        )

    # bug_reports.status flipped in py too
    assert all(r["status"] == "queued_as_epic" for r in py_brs)
    assert len(py_epics) == 2
    assert all(e["status"] == "not started" for e in py_epics)

    # ── Idempotence: re-running py promote produces 0 new epics ──────────
    py_rerun = _run_py(["promote", "--top", "2"], env_extra=env_extra)
    assert py_rerun.returncode == 0
    assert py_rerun.stdout == "0\n", f"py rerun stdout: {py_rerun.stdout!r}"
    assert len(_row_dicts(temp_db["db"], "epics")) == 2  # no new epics