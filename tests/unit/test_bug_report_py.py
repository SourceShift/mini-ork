"""Unit tests: mini_ork.observability.bug_report (bash parity halves removed; formerly vs lib/bug_report.sh).

Each test drives the Python port against a temp DB seeded by
``db/init.sh`` and asserts the resulting ``bug_reports`` rows +
``noticed_bugs.jsonl`` sinks + stdout strings semantically. No mocks.

Schema bootstrap: emit/sweep/prioritize/list/show/promote all query
``bug_reports`` and ``epics``. ``bug_report_promote`` additionally inserts
into ``epics``. The bootstrap path is ``db/init.sh`` which applies
0001_core + 0029_bug_reports + every other migration.

Cases:
  (a) bug_report_emit happy path — JSONL row shape
  (b) bug_report_emit invalid severity normalized to "medium"
  (c) bug_report_emit invalid confidence falls back to 0.5
  (d) bug_report_emit missing agent_role / title raises
  (e) bug_report_sweep dedupes on fingerprint, increments frequency, keeps max severity/conf
  (f) bug_report_sweep --since filters by sink mtime
  (g) bug_report_prioritize stdout row format
  (h) bug_report_list + bug_report_show stdout format
  (i) bug_report_promote creates epic row + kickoff file + flips status
  (j) bug_report_promote is idempotent (re-run → 0 new)

Tolerance notes:
  * confidence floats compared at 1e-6.
  * JSONL row: dict shape with separators=(",", ":"), UTF-8.
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
from mini_ork.observability import bug_report as py

INIT_SH = REPO / "db" / "init.sh"


# ─────────────────────────────────────────────────────────────────────────────
# Fixtures / helpers
# ─────────────────────────────────────────────────────────────────────────────
@pytest.fixture
def temp_db(tmp_path, monkeypatch):
    """Spin up a real mini-ork SQLite DB via db/init.sh.

    The Python port's ``_resolve_db()`` reads ``MINI_ORK_DB`` /
    ``MINI_ORK_HOME`` from the env; monkeypatch both so the port lands on
    the temp DB.
    """
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
    monkeypatch.setenv("MINI_ORK_DB", dbp)
    monkeypatch.setenv("MINI_ORK_HOME", str(home))
    return {"home": str(home), "db": dbp}


def _row_dicts(db: str, table: str) -> list[dict]:
    """Dump all rows of ``table`` as dicts. Ordered by the table's rowid."""
    con = sqlite3.connect(db)
    try:
        cols = [d[0] for d in con.execute(f"SELECT * FROM {table} LIMIT 0").description]
        rows = con.execute(f"SELECT {', '.join(cols)} FROM {table}").fetchall()
        return [dict(zip(cols, r)) for r in rows]
    finally:
        con.close()


def _read_jsonl(path: Path) -> list[dict]:
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        out.append(json.loads(line))
    return out


def _write_sink(run_dir: Path, rows: list[dict]) -> Path:
    """Write a noticed_bugs.jsonl sink directly (compact JSON per line)."""
    run_dir.mkdir(parents=True, exist_ok=True)
    sink = run_dir / "noticed_bugs.jsonl"
    sink.write_text(
        "".join(json.dumps(r, separators=(",", ":")) + "\n" for r in rows),
        encoding="utf-8",
    )
    return sink


def _seed_bug_row(db: str, fp: str, run_id: str, role: str, title: str,
                  sev: str, conf: float, freq: int,
                  task_class: str | None = None, observed_in: str = "lib/x",
                  status: str = "open") -> None:
    con = sqlite3.connect(db)
    try:
        now = int(time.time())
        con.execute(
            """INSERT INTO bug_reports
               (fingerprint, run_id, agent_role, task_class, observed_in,
                title, description, suggested_fix, severity, confidence,
                frequency, status, first_seen_at, last_seen_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (fp, run_id, role, task_class, observed_in,
             title, "d", "f", sev, conf, freq, status,
             now, now, now),
        )
        con.commit()
    finally:
        con.close()


# ─────────────────────────────────────────────────────────────────────────────
# (a) bug_report_emit happy path — JSONL row shape
# ─────────────────────────────────────────────────────────────────────────────
def test_emit_happy_path(tmp_path):
    """The port appends a JSONL row to ``noticed_bugs.jsonl`` with the
    emitted field values."""
    py_run_dir = tmp_path / "py_run"
    py_run_dir.mkdir()

    py.bug_report_emit(
        "reviewer", "high",
        "Pass-true with FAIL items",
        "rubric score=6",
        "short-circuit on critical FAIL",
        "lib/verifier-rubric.sh",
        0.88,
        run_dir=str(py_run_dir),
    )

    py_rows = _read_jsonl(py_run_dir / "noticed_bugs.jsonl")
    assert len(py_rows) == 1, py_rows
    row = py_rows[0]
    assert row["agent_role"] == "reviewer"
    assert row["severity"] == "high"
    assert row["title"] == "Pass-true with FAIL items"
    assert row["description"] == "rubric score=6"
    assert row["suggested_fix"] == "short-circuit on critical FAIL"
    assert row["observed_in"] == "lib/verifier-rubric.sh"
    # confidence must be float, not string.
    assert isinstance(row["confidence"], float)
    assert abs(row["confidence"] - 0.88) <= 1e-6


# ─────────────────────────────────────────────────────────────────────────────
# (b) bug_report_emit invalid severity normalized to "medium"
# ─────────────────────────────────────────────────────────────────────────────
def test_emit_invalid_severity_normalized(tmp_path):
    """Any severity outside {low,medium,high,critical} falls back to
    "medium"."""
    py_run_dir = tmp_path / "py_run"
    py_run_dir.mkdir()

    py.bug_report_emit(
        "implementer", "BOGUS", "some title", "d", "f", "o", 0.5,
        run_dir=str(py_run_dir),
    )

    py_rows = _read_jsonl(py_run_dir / "noticed_bugs.jsonl")
    assert len(py_rows) == 1
    assert py_rows[0]["severity"] == "medium"


# ─────────────────────────────────────────────────────────────────────────────
# (c) bug_report_emit invalid confidence falls back to 0.5
# ─────────────────────────────────────────────────────────────────────────────
def test_emit_invalid_confidence_fallback(tmp_path):
    """Non-numeric confidence coerces to 0.5."""
    py_run_dir = tmp_path / "py_run"
    py_run_dir.mkdir()

    py.bug_report_emit(
        "planner", "low", "title", "d", "f", "o", "not-a-number",
        run_dir=str(py_run_dir),
    )

    py_rows = _read_jsonl(py_run_dir / "noticed_bugs.jsonl")
    assert len(py_rows) == 1
    assert abs(float(py_rows[0]["confidence"]) - 0.5) <= 1e-6


# ─────────────────────────────────────────────────────────────────────────────
# (d) bug_report_emit missing agent_role / title raises
# ─────────────────────────────────────────────────────────────────────────────
def test_emit_missing_required_fields_raises(tmp_path):
    """Empty agent_role / title raise ``ValueError`` with the
    parameter-required phrase."""
    with pytest.raises(ValueError, match="agent_role required"):
        py.bug_report_emit("", "low", "title", "d", "f", "o", 0.5,
                           run_dir=str(tmp_path))
    with pytest.raises(ValueError, match="title required"):
        py.bug_report_emit("role", "low", "", "d", "f", "o", 0.5,
                           run_dir=str(tmp_path))


# ─────────────────────────────────────────────────────────────────────────────
# (e) bug_report_sweep dedupes on fingerprint, increments frequency,
#     keeps max severity/conf
# ─────────────────────────────────────────────────────────────────────────────
def test_sweep_dedupes_and_keeps_max_severity(temp_db):
    """Two JSONL rows with the same title (after whitespace+lowercase+strip
    normalization) produce a single ``bug_reports`` row whose ``frequency``
    is 2 and ``severity`` is the max of the two."""
    # Stage: two rows into the SAME sink file with the same title.
    # The first has low/conf=0.5; the second has high/conf=0.9.
    run_id_dir = Path(temp_db["home"]) / "runs" / "run-dedupe"
    _write_sink(run_id_dir, [
        {"agent_role": "reviewer", "severity": "low",
         "title": "Same title here", "description": "d1",
         "suggested_fix": "f", "observed_in": "o", "confidence": 0.5},
        {"agent_role": "reviewer", "severity": "high",
         "title": "Same title here", "description": "d2",
         "suggested_fix": "f", "observed_in": "o", "confidence": 0.9},
    ])

    py_swept = py.bug_report_sweep("--all", home=temp_db["home"])
    assert py_swept == 1, f"py sweep count: {py_swept}"
    py_rows = _row_dicts(temp_db["db"], "bug_reports")
    assert len(py_rows) == 1, py_rows
    # Row 1 inserts (frequency=1, sev=low, conf=0.5). Row 2 updates
    # (frequency=2, sev=high, conf=0.9). Final: frequency=2, sev=high,
    # conf=0.9 (max-rank / max-float semantics).
    row = py_rows[0]
    assert row["frequency"] == 2, row
    assert row["severity"] == "high"
    assert abs(float(row["confidence"]) - 0.9) <= 1e-6
    assert row["agent_role"] == "reviewer"
    assert row["title"] == "Same title here"
    assert row["status"] == "open"


# ─────────────────────────────────────────────────────────────────────────────
# (f) bug_report_sweep --since filters by sink mtime
# ─────────────────────────────────────────────────────────────────────────────
def test_sweep_since_filters_by_mtime(temp_db):
    """Without ``--all`` and with ``--since=<future-epoch>``, no sinks
    qualify; sweep returns 0. With ``--all``, all sinks qualify regardless
    of mtime; a re-sweep inserts 0 new rows (dedupe)."""
    # Stage one bug
    run_id_dir = Path(temp_db["home"]) / "runs" / "run-since"
    _write_sink(run_id_dir, [
        {"agent_role": "reviewer", "severity": "low",
         "title": "since test title", "description": "d",
         "suggested_fix": "f", "observed_in": "o", "confidence": 0.5},
    ])
    future = int(time.time()) + 3600
    py_r = py.bug_report_sweep("--since", str(future), home=temp_db["home"])
    assert py_r == 0

    # --all: 1 new row
    py_r = py.bug_report_sweep("--all", home=temp_db["home"])
    assert py_r == 1
    # already inserted; re-sweep increments frequency, count=0 new.
    py_r = py.bug_report_sweep("--all", home=temp_db["home"])
    assert py_r == 0


# ─────────────────────────────────────────────────────────────────────────────
# (g) bug_report_prioritize stdout row format
# ─────────────────────────────────────────────────────────────────────────────
def test_prioritize_stdout_format(temp_db):
    """``bug_report_prioritize --top N`` prints rows of shape
    ``<id> | <sev> | <freq> | <conf> | <role> | <title-truncated-to-80>``
    ordered by severity rank."""
    _seed_bug_row(temp_db["db"], "fp-a", "run-1", "reviewer",
                  "Alpha bug", "critical", 0.95, 3)
    _seed_bug_row(temp_db["db"], "fp-b", "run-2", "implementer",
                  "Beta bug " + "x" * 100, "high", 0.50, 1)

    py_out = py.bug_report_prioritize(top=5)
    rows = [r.split(" | ") for r in py_out.split("\n") if r]
    assert len(rows) == 2, f"row count {len(rows)}: {rows}"

    def _normalize(row: list[str]) -> tuple:
        assert len(row) >= 6, row
        cols = [c.strip() for c in row[:5]]
        title = " | ".join(row[5:])
        return tuple(cols), title

    cols0, title0 = _normalize(rows[0])
    cols1, title1 = _normalize(rows[1])
    # severity critical sorts first (higher rank)
    assert cols0[1] == "critical"
    assert cols0[2] == "3"           # frequency
    assert abs(float(cols0[3]) - 0.95) <= 1e-6
    assert cols0[4] == "reviewer"
    assert title0 == "Alpha bug"
    assert cols1[1] == "high"
    assert cols1[4] == "implementer"
    # title truncated to 80 chars
    assert len(title1) == 80
    assert title1.startswith("Beta bug ")


# ─────────────────────────────────────────────────────────────────────────────
# (h) bug_report_list + bug_report_show stdout format
# ─────────────────────────────────────────────────────────────────────────────
def test_list_and_show_format(temp_db):
    """``bug_report_list`` returns pipe-separated id|status|sev|role|title
    rows. ``bug_report_show <id>`` returns ``key = value\\n`` lines."""
    _seed_bug_row(temp_db["db"], "fp-list", "run-list", "reviewer",
                  "List row", "medium", 0.5, 1, observed_in="lib/l")

    list_out = py.bug_report_list()
    lines = [ln for ln in list_out.splitlines() if ln.strip()]
    assert len(lines) == 1
    fields = [f.strip() for f in lines[0].split("|")]
    assert fields[1:] == ["open", "medium", "reviewer", "List row"]

    # show(id=1) — "key = value" lines with the row's content
    py_show = py.bug_report_show(1)
    show = {}
    for ln in py_show.splitlines():
        if " = " in ln:
            k, v = ln.split(" = ", 1)
            show[k.strip()] = v
    assert show["title"] == "List row"
    assert show["fingerprint"] == "fp-list"
    assert show["severity"] == "medium"
    assert abs(float(show["confidence"]) - 0.5) <= 1e-6

    # show(missing id) raises
    with pytest.raises(ValueError, match="no row for id="):
        py.bug_report_show(99999)


# ─────────────────────────────────────────────────────────────────────────────
# (i) bug_report_promote creates epic row + kickoff file + flips status
# ─────────────────────────────────────────────────────────────────────────────
def test_promote_happy_path(temp_db, tmp_path):
    """Promote takes the top-N open bugs, writes a kickoff file under
    ``kickoffs/auto/{epic_id}.md``, INSERTs an ``epics`` row, and flips
    ``bug_reports.status`` to ``'queued_as_epic'``."""
    _seed_bug_row(temp_db["db"], "fp-promote", "run-promote", "reviewer",
                  "Promote me", "critical", 0.99, 5,
                  task_class="code_fix", observed_in="lib/p")

    # Override repo_root to a sandbox dir so we don't write to the real repo.
    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()

    py_promoted = py.bug_report_promote("--top", "3", repo_root=sandbox)
    assert py_promoted == 1, f"py promote count: {py_promoted}"

    # Status flipped to 'queued_as_epic'.
    rows = _row_dicts(temp_db["db"], "bug_reports")
    assert len(rows) == 1
    assert rows[0]["status"] == "queued_as_epic"
    assert rows[0]["promoted_to_epic_id"] is not None

    # Epic row exists.
    eid = rows[0]["promoted_to_epic_id"]
    epics = _row_dicts(temp_db["db"], "epics")
    matching = [e for e in epics if e["id"] == eid]
    assert len(matching) == 1, epics
    assert matching[0]["status"] == "not started"
    assert matching[0]["kickoff_path"] == f"kickoffs/auto/{eid}.md"
    assert matching[0]["title"].startswith("BUG-")

    # The kickoff file was written at sandbox/kickoffs/auto/{eid}.md.
    kickoff = sandbox / "kickoffs" / "auto" / f"{eid}.md"
    assert kickoff.exists(), f"missing kickoff: {kickoff}"
    text = kickoff.read_text(encoding="utf-8")
    assert "# Bug-promoted epic: Promote me" in text
    assert "Severity: **critical**" in text
    assert "verification_state" not in text  # sanity: not from another table
    assert "## Verification commands" in text
    assert "## Done When" in text

    # Re-promote is idempotent: the epic already exists → 0.
    assert py.bug_report_promote("--top", "3", repo_root=sandbox) == 0


# ─────────────────────────────────────────────────────────────────────────────
# (j) bug_report_promote is idempotent
# ─────────────────────────────────────────────────────────────────────────────
def test_promote_idempotent(temp_db, tmp_path):
    """Re-running promote when the epic row already exists must return 0
    and not create duplicate epics or duplicate kickoff files."""
    _seed_bug_row(temp_db["db"], "fp-idem", "run-idem", "planner",
                  "Idempotent test", "high", 0.8, 1, observed_in="lib/i")

    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()

    n1 = py.bug_report_promote("--top", "3", repo_root=sandbox)
    n2 = py.bug_report_promote("--top", "3", repo_root=sandbox)
    assert n1 == 1
    assert n2 == 0, f"second promote should be 0: {n2}"

    epics = _row_dicts(temp_db["db"], "epics")
    assert len(epics) == 1, f"expected 1 epic, got {len(epics)}"
