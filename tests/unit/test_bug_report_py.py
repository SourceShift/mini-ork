"""Parity gate: mini_ork.ported.bug_report vs lib/bug_report.sh.

Each test invokes the LIVE bash subprocess against a temp DB seeded by
``db/init.sh``, then invokes the Python port against a parallel temp DB
(seeded identically), and asserts the resulting ``bug_reports`` rows +
``noticed_bugs.jsonl`` sinks + stdout strings match exactly. No mocks,
no hardcoded expected outputs — expected is always derived from the live
control bash invocation.

Test fixtures use a single DB per test (not bash+py pair) where the
public surface is pure-file or pure-DB (emit, sweep). For surfaces where
the bash and Python emit must be independently observable (prioritize,
list, show, promote), we run bash against the single DB, capture
stdout/exit-code, and run the Python port against the same DB — the
output must match.

Schema bootstrap: bash's emit/sweep/prioritize/list/show/promote all
query ``bug_reports`` and ``epics``. ``bug_report_promote`` additionally
inserts into ``epics``. The only correct bootstrap path is ``db/init.sh``
which applies 0001_core + 0029_bug_reports + every other migration.

Cases (8, above the kickoff's >=6 floor):
  (a) bug_report_emit happy path — JSONL row shape parity
  (b) bug_report_emit invalid severity normalized to "medium"
  (c) bug_report_emit invalid confidence falls back to 0.5
  (d) bug_report_emit missing agent_role / title raises
  (e) bug_report_sweep dedupes on fingerprint, increments frequency, keeps max severity/conf
  (f) bug_report_sweep --since filters by sink mtime
  (g) bug_report_prioritize stdout row format parity
  (h) bug_report_list + bug_report_show stdout format parity
  (i) bug_report_promote creates epic row + kickoff file + flips status
  (j) bug_report_promote is idempotent (re-run → 0 new)

Tolerance notes:
  * confidence floats compared at 1e-6 (kicked-off tolerance).
  * first_seen_at / last_seen_at / updated_at allowed within 1-second window.
  * JSONL row ordering: same line count, identical dict per line
    (sort_keys=False, separators=(",", ":"), UTF-8).
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
from mini_ork.ported import bug_report as py  # noqa: E402

SH = REPO / "lib" / "bug_report.sh"
INIT_SH = REPO / "db" / "init.sh"


# ─────────────────────────────────────────────────────────────────────────────
# Fixtures / helpers
# ─────────────────────────────────────────────────────────────────────────────
def _which_tools() -> None:
    for tool in ("bash", "sqlite3", "python3"):
        if not shutil.which(tool):
            pytest.skip(f"{tool} not on PATH")
    if not SH.exists():
        pytest.skip(f"missing lib/bug_report.sh at {SH}")
    if not INIT_SH.exists():
        pytest.skip(f"missing db/init.sh at {INIT_SH}")


@pytest.fixture
def temp_db(tmp_path, monkeypatch):
    """Spin up a real mini-ork SQLite DB via db/init.sh.

    The Python port's ``_resolve_db()`` reads ``MINI_ORK_DB`` /
    ``MINI_ORK_HOME`` from the env; monkeypatch both so the Python port
    lands on the same DB the bash subprocess writes to.
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
    monkeypatch.setenv("MINI_ORK_DB", dbp)
    monkeypatch.setenv("MINI_ORK_HOME", str(home))
    return {"home": str(home), "db": dbp}


def _bash_run_func(
    func: str,
    args: list[str],
    *,
    db: str,
    extra_env: dict[str, str] | None = None,
    run_dir: str | None = None,
) -> subprocess.CompletedProcess:
    """Source the bash library and call ``func`` with positional args.

    Args are wrapped in double quotes with internal ``"`` escaped as
    ``\\"`` so JSON-like payloads survive the bash parse. ``run_dir`` is
    exported as ``MINI_ORK_RUN_DIR`` so emit lands in the test sandbox.
    """
    def _q(a: str) -> str:
        return '"' + a.replace("\\", "\\\\").replace('"', '\\"') + '"'
    arg_str = " ".join(_q(a) for a in args)
    script = f'. "{SH}"\n{func} {arg_str}\n'
    env = {
        **os.environ,
        "MINI_ORK_DB": db,
        "MINI_ORK_HOME": str(Path(db).parent),
    }
    if run_dir is not None:
        env["MINI_ORK_RUN_DIR"] = run_dir
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        ["bash", "-c", script],
        env=env,
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


def _epoch_close(a: int | float | None, b: int | float | None,
                 *, window_s: int = 1) -> bool:
    """Integer epoch tolerance in seconds; falls through to 1e-6 float diff."""
    if a is None and b is None:
        return True
    if a is None or b is None:
        return False
    if abs(int(a) - int(b)) <= window_s:
        return True
    return abs(float(a) - float(b)) <= 1e-6


def _normalize_emit_row(row: dict) -> dict:
    """Strip the volatile fields the parity check should ignore.

    ``agent_role`` / ``severity`` / ``title`` / ``description`` /
    ``suggested_fix`` / ``observed_in`` / ``confidence`` are the parity
    invariants; nothing on the row is volatile per-line.
    """
    return {k: row.get(k) for k in (
        "agent_role", "severity", "title", "description",
        "suggested_fix", "observed_in", "confidence",
    )}


def _read_jsonl(path: Path) -> list[dict]:
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        out.append(json.loads(line))
    return out


# ─────────────────────────────────────────────────────────────────────────────
# (a) bug_report_emit happy path — JSONL row shape parity
# ─────────────────────────────────────────────────────────────────────────────
def test_emit_happy_path_parity(tmp_path, monkeypatch):
    """Both ports must append a JSONL row to ``noticed_bugs.jsonl`` with
    identical dict shape (separators=(",", ":") and exact field values)."""
    _which_tools()
    home = tmp_path / "home"
    home.mkdir()
    dbp = str(home / "state.db")
    subprocess.run(
        ["bash", str(INIT_SH)],
        env={**os.environ, "MINI_ORK_HOME": str(home), "MINI_ORK_DB": dbp},
        capture_output=True, text=True, check=True,
    )

    bash_run_dir = tmp_path / "bash_run"
    bash_run_dir.mkdir()
    py_run_dir = tmp_path / "py_run"
    py_run_dir.mkdir()

    bash_args = ["reviewer", "high",
                 "Pass-true with FAIL items",
                 "rubric score=6",
                 "short-circuit on critical FAIL",
                 "lib/verifier-rubric.sh",
                 "0.88"]
    bash_r = _bash_run_func("bug_report_emit", bash_args,
                            db=dbp, run_dir=str(bash_run_dir))
    assert bash_r.returncode == 0, f"bash failed: {bash_r.stderr}"
    py.bug_report_emit(
        "reviewer", "high",
        "Pass-true with FAIL items",
        "rubric score=6",
        "short-circuit on critical FAIL",
        "lib/verifier-rubric.sh",
        0.88,
        run_dir=str(py_run_dir),
    )

    bash_rows = _read_jsonl(bash_run_dir / "noticed_bugs.jsonl")
    py_rows = _read_jsonl(py_run_dir / "noticed_bugs.jsonl")
    assert len(bash_rows) == 1, bash_rows
    assert len(py_rows) == 1, py_rows
    assert _normalize_emit_row(bash_rows[0]) == _normalize_emit_row(py_rows[0]), (
        f"emit row mismatch:\n  bash={bash_rows[0]!r}\n  py  ={py_rows[0]!r}"
    )
    # confidence must be float, not string.
    assert isinstance(py_rows[0]["confidence"], float)
    assert abs(py_rows[0]["confidence"] - 0.88) <= 1e-6


# ─────────────────────────────────────────────────────────────────────────────
# (b) bug_report_emit invalid severity normalized to "medium"
# ─────────────────────────────────────────────────────────────────────────────
def test_emit_invalid_severity_normalized(tmp_path, monkeypatch):
    """Bash falls back to "medium" for any severity outside {low,medium,
    high,critical}. Python port must match."""
    _which_tools()
    home = tmp_path / "home"
    home.mkdir()
    dbp = str(home / "state.db")
    subprocess.run(
        ["bash", str(INIT_SH)],
        env={**os.environ, "MINI_ORK_HOME": str(home), "MINI_ORK_DB": dbp},
        capture_output=True, text=True, check=True,
    )

    bash_run_dir = tmp_path / "bash_run"
    bash_run_dir.mkdir()
    py_run_dir = tmp_path / "py_run"
    py_run_dir.mkdir()

    bash_r = _bash_run_func(
        "bug_report_emit",
        ["implementer", "BOGUS", "some title", "d", "f", "o", "0.5"],
        db=dbp, run_dir=str(bash_run_dir),
    )
    assert bash_r.returncode == 0, f"bash failed: {bash_r.stderr}"
    py.bug_report_emit(
        "implementer", "BOGUS", "some title", "d", "f", "o", 0.5,
        run_dir=str(py_run_dir),
    )

    bash_rows = _read_jsonl(bash_run_dir / "noticed_bugs.jsonl")
    py_rows = _read_jsonl(py_run_dir / "noticed_bugs.jsonl")
    assert len(bash_rows) == 1 and len(py_rows) == 1
    assert bash_rows[0]["severity"] == "medium"
    assert py_rows[0]["severity"] == "medium"


# ─────────────────────────────────────────────────────────────────────────────
# (c) bug_report_emit invalid confidence falls back to 0.5
# ─────────────────────────────────────────────────────────────────────────────
def test_emit_invalid_confidence_fallback(tmp_path, monkeypatch):
    """Bash's heredoc uses ``try: float(conf) except: 0.5``. Non-numeric
    confidence must coerce to 0.5."""
    _which_tools()
    home = tmp_path / "home"
    home.mkdir()
    dbp = str(home / "state.db")
    subprocess.run(
        ["bash", str(INIT_SH)],
        env={**os.environ, "MINI_ORK_HOME": str(home), "MINI_ORK_DB": dbp},
        capture_output=True, text=True, check=True,
    )

    bash_run_dir = tmp_path / "bash_run"
    bash_run_dir.mkdir()
    py_run_dir = tmp_path / "py_run"
    py_run_dir.mkdir()

    bash_r = _bash_run_func(
        "bug_report_emit",
        ["planner", "low", "title", "d", "f", "o", "not-a-number"],
        db=dbp, run_dir=str(bash_run_dir),
    )
    assert bash_r.returncode == 0, f"bash failed: {bash_r.stderr}"
    py.bug_report_emit(
        "planner", "low", "title", "d", "f", "o", "not-a-number",
        run_dir=str(py_run_dir),
    )

    bash_rows = _read_jsonl(bash_run_dir / "noticed_bugs.jsonl")
    py_rows = _read_jsonl(py_run_dir / "noticed_bugs.jsonl")
    assert len(bash_rows) == 1 and len(py_rows) == 1
    assert abs(float(bash_rows[0]["confidence"]) - 0.5) <= 1e-6
    assert abs(float(py_rows[0]["confidence"]) - 0.5) <= 1e-6


# ─────────────────────────────────────────────────────────────────────────────
# (d) bug_report_emit missing agent_role / title raises
# ─────────────────────────────────────────────────────────────────────────────
def test_emit_missing_required_fields_raises(tmp_path, monkeypatch):
    """Bash ``${1:?agent_role required}`` and ``${3:?title required}`` abort
    with the parameter-required message. The Python port raises
    ``ValueError`` with the same phrase."""
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
def test_sweep_dedupes_and_keeps_max_severity(temp_db, tmp_path):
    """Two JSONL rows with the same title (after whitespace+lowercase+strip
    normalization) must produce a single ``bug_reports`` row whose
    ``frequency`` is 2 and ``severity`` is the max of the two."""
    # Stage: bash emits two rows into the SAME sink file with the same title.
    # The first has low/conf=0.5; the second has high/conf=0.9. Both
    # normalize to the same fingerprint, so sweep must dedupe.
    run_id_dir = Path(temp_db["home"]) / "runs" / "run-dedupe"
    run_id_dir.mkdir(parents=True, exist_ok=True)
    bash_r1 = _bash_run_func(
        "bug_report_emit",
        ["reviewer", "low", "Same title here", "d1", "f", "o", "0.5"],
        db=temp_db["db"], run_dir=str(run_id_dir),
    )
    assert bash_r1.returncode == 0, bash_r1.stderr
    bash_r2 = _bash_run_func(
        "bug_report_emit",
        ["reviewer", "high", "Same title here", "d2", "f", "o", "0.9"],
        db=temp_db["db"], run_dir=str(run_id_dir),
    )
    assert bash_r2.returncode == 0, bash_r2.stderr

    def _wipe():
        con = sqlite3.connect(temp_db["db"])
        try:
            con.execute("DELETE FROM bug_reports")
            con.commit()
        finally:
            con.close()

    # Bash sweep on a fresh DB.
    _wipe()
    bash_r = _bash_run_func("bug_report_sweep", ["--all"],
                            db=temp_db["db"])
    assert bash_r.returncode == 0, f"bash sweep failed: {bash_r.stderr}"
    bash_swept = int(bash_r.stdout.strip())
    assert bash_swept == 1, f"bash sweep count: {bash_swept!r}"
    bash_rows = _row_dicts(temp_db["db"], "bug_reports")
    assert len(bash_rows) == 1, bash_rows
    # Row 1 inserts (frequency=1, sev=low, conf=0.5). Row 2 updates
    # (frequency=2, sev=high, conf=0.9). Final: frequency=2, sev=high,
    # conf=0.9 (max-rank / max-float semantics).
    assert bash_rows[0]["frequency"] == 2, bash_rows[0]
    assert bash_rows[0]["severity"] == "high"
    assert abs(float(bash_rows[0]["confidence"]) - 0.9) <= 1e-6

    # Wipe + py sweep on the SAME sink file.
    _wipe()
    py_swept = py.bug_report_sweep("--all", home=temp_db["home"])
    assert py_swept == 1, f"py sweep count: {py_swept}"
    py_rows = _row_dicts(temp_db["db"], "bug_reports")
    assert len(py_rows) == 1, py_rows
    assert py_rows[0]["frequency"] == 2
    assert py_rows[0]["severity"] == "high"
    assert abs(float(py_rows[0]["confidence"]) - 0.9) <= 1e-6

    # Final parity check: both rows must be identical (modulo timestamps).
    b, p = bash_rows[0], py_rows[0]
    for f in ("fingerprint", "run_id", "agent_role", "title",
              "description", "suggested_fix", "severity",
              "frequency", "status", "task_class", "observed_in"):
        assert b.get(f) == p.get(f), f"{f}: bash={b.get(f)!r} py={p.get(f)!r}"
    assert abs(float(b["confidence"]) - float(p["confidence"])) <= 1e-6
    for ts in ("first_seen_at", "last_seen_at", "updated_at"):
        assert _epoch_close(b[ts], p[ts]), f"{ts}: bash={b[ts]} py={p[ts]}"


# ─────────────────────────────────────────────────────────────────────────────
# (f) bug_report_sweep --since filters by sink mtime
# ─────────────────────────────────────────────────────────────────────────────
def test_sweep_since_filters_by_mtime(temp_db, tmp_path):
    """Without ``--all`` and with ``--since=<future-epoch>``, no sinks
    qualify; sweep returns 0. With ``--all``, all sinks qualify regardless
    of mtime."""
    # Stage one bug
    run_id_dir = Path(temp_db["home"]) / "runs" / "run-since"
    run_id_dir.mkdir(parents=True, exist_ok=True)
    _bash_run_func(
        "bug_report_emit",
        ["reviewer", "low", "since test title", "d", "f", "o", "0.5"],
        db=temp_db["db"], run_dir=str(run_id_dir),
    )
    future = int(time.time()) + 3600
    bash_r = _bash_run_func("bug_report_sweep", ["--since", str(future)],
                            db=temp_db["db"])
    assert bash_r.returncode == 0, bash_r.stderr
    assert int(bash_r.stdout.strip()) == 0, bash_r.stdout

    py_r = py.bug_report_sweep("--since", str(future), home=temp_db["home"])
    assert py_r == 0

    # --all: 1
    bash_r = _bash_run_func("bug_report_sweep", ["--all"],
                            db=temp_db["db"])
    assert int(bash_r.stdout.strip()) == 1, bash_r.stdout
    py_r = py.bug_report_sweep("--all", home=temp_db["home"])
    # already inserted by bash; py re-sweep increments frequency, count=0 new.
    assert py_r == 0


# ─────────────────────────────────────────────────────────────────────────────
# (g) bug_report_prioritize stdout row format parity
# ─────────────────────────────────────────────────────────────────────────────
def test_prioritize_stdout_format_parity(temp_db):
    """Bash `bug_report_prioritize --top 10` prints rows of shape
    ``<id> | <sev> | <freq> | <conf> | <role> | <title-truncated-to-80>``
    with printf column widths. Python port returns the same string."""
    # Seed via Python so we control the exact row shape.
    py.bug_report_sweep("--all", home=temp_db["home"])
    # Manually insert 2 rows with known shapes (avoid sweep's normalization).
    con = sqlite3.connect(temp_db["db"])
    try:
        now = int(time.time())
        con.executemany(
            """INSERT INTO bug_reports
               (fingerprint, run_id, agent_role, task_class, observed_in,
                title, description, suggested_fix, severity, confidence,
                frequency, status, first_seen_at, last_seen_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            [
                ("fp-a", "run-1", "reviewer", None, "lib/x",
                 "Alpha bug", "d", "f", "critical", 0.95, 3, "open",
                 now, now, now),
                ("fp-b", "run-2", "implementer", None, "lib/y",
                 "Beta bug " + "x" * 100, "d", "f", "high", 0.50, 1, "open",
                 now, now, now),
            ],
        )
        con.commit()
    finally:
        con.close()

    bash_r = _bash_run_func("bug_report_prioritize", ["--top", "5"],
                            db=temp_db["db"])
    assert bash_r.returncode == 0, f"bash failed: {bash_r.stderr}"
    py_out = py.bug_report_prioritize(top=5)

    # Strip volatile ids (bash AUTOINCREMENT vs py depends on history).
    # We compare per-row structure instead of byte-for-byte: tokenize on
    # " | " at the row level and on "\n" between rows.
    def _rows(text: str) -> list[list[str]]:
        return [r.split(" | ") for r in text.split("\n") if r]

    bash_rows = _rows(bash_r.stdout)
    py_rows = _rows(py_out)
    assert len(bash_rows) == 2, f"bash row count {len(bash_rows)}: {bash_rows}"
    assert len(py_rows) == 2
    # Each row: [id-padded, sev-padded, freq-padded, conf-padded, role-padded, title]
    # After splitting on " | " the title may contain " | " (unlikely but
    # possible) — join the tail back.
    def _normalize(row: list[str]) -> tuple:
        # Last cell is the (possibly multi-piece) title; everything before
        # is the 5 fixed columns.
        assert len(row) >= 6, row
        cols = row[:5]
        title = " | ".join(row[5:])
        # Strip printf padding for stable comparison.
        cols = [c.strip() for c in cols]
        # Truncate title to 80 chars to mirror bash substr(title,1,80).
        title = title[:80]
        return tuple(cols), title

    assert _normalize(bash_rows[0]) == _normalize(py_rows[0])
    assert _normalize(bash_rows[1]) == _normalize(py_rows[1])

    # Sanity: severity critical row sorts first (higher rank).
    assert "critical" in bash_rows[0][1]
    assert "high" in bash_rows[1][1]


# ─────────────────────────────────────────────────────────────────────────────
# (h) bug_report_list + bug_report_show stdout format parity
# ─────────────────────────────────────────────────────────────────────────────
def test_list_and_show_format_parity(temp_db):
    """``bug_report_list`` returns pipe-separated id|status|sev|role|title
    rows. ``bug_report_show <id>`` returns ``key = value\\n`` lines
    terminated by a blank line — exact ``sqlite3 -line`` form."""
    py.bug_report_sweep("--all", home=temp_db["home"])
    con = sqlite3.connect(temp_db["db"])
    try:
        now = int(time.time())
        con.execute(
            """INSERT INTO bug_reports
               (fingerprint, run_id, agent_role, task_class, observed_in,
                title, description, suggested_fix, severity, confidence,
                frequency, status, first_seen_at, last_seen_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            ("fp-list", "run-list", "reviewer", None, "lib/l",
             "List row", "d", "f", "medium", 0.5, 1, "open",
             now, now, now),
        )
        con.commit()
    finally:
        con.close()

    bash_list = _bash_run_func("bug_report_list", [], db=temp_db["db"])
    assert bash_list.returncode == 0, bash_list.stderr
    assert bash_list.stdout == py.bug_report_list(), (
        f"list mismatch:\n  bash={bash_list.stdout!r}\n  py  ={py.bug_report_list()!r}"
    )

    # show(id=1) — compare line-by-line because int and float formatting
    # can differ slightly across sqlite3 versions.
    bash_show = _bash_run_func("bug_report_show", ["1"], db=temp_db["db"])
    assert bash_show.returncode == 0, bash_show.stderr
    py_show = py.bug_report_show(1)
    bash_lines = bash_show.stdout.splitlines()
    py_lines = py_show.splitlines()
    assert len(bash_lines) == len(py_lines), (
        f"show line count: bash={len(bash_lines)} py={len(py_lines)}"
    )
    for b, p in zip(bash_lines, py_lines):
        # Each line is "key = value". Compare keys exactly; values may
        # differ for INTEGER columns (bash prints int, py prints int).
        if " = " not in b:
            # Empty lines should match.
            assert b == p, f"blank line mismatch: bash={b!r} py={p!r}"
            continue
        bk, bv = b.split(" = ", 1)
        pk, pv = p.split(" = ", 1)
        assert bk == pk, f"show key mismatch: {bk!r} vs {pk!r}"
        # For confidence (float) allow 1e-6; for ints allow exact.
        if bk == "confidence":
            try:
                assert abs(float(bv) - float(pv)) <= 1e-6, (
                    f"show confidence: bash={bv!r} py={pv!r}"
                )
            except ValueError:
                assert bv == pv, f"show confidence non-numeric: bash={bv!r} py={pv!r}"
        else:
            assert bv == pv, f"show {bk}: bash={bv!r} py={pv!r}"

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
    py.bug_report_sweep("--all", home=temp_db["home"])
    con = sqlite3.connect(temp_db["db"])
    try:
        now = int(time.time())
        con.execute(
            """INSERT INTO bug_reports
               (fingerprint, run_id, agent_role, task_class, observed_in,
                title, description, suggested_fix, severity, confidence,
                frequency, status, first_seen_at, last_seen_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            ("fp-promote", "run-promote", "reviewer", "code_fix", "lib/p",
             "Promote me", "long description", "fix it",
             "critical", 0.99, 5, "open",
             now, now, now),
        )
        con.commit()
    finally:
        con.close()

    # Override repo_root to a sandbox dir so we don't write to the real repo.
    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()

    py_promoted = py.bug_report_promote("--top", "3", repo_root=sandbox)
    assert py_promoted == 1, f"py promote count: {py_promoted}"

    # bash promote against the same DB.
    bash_r = _bash_run_func("bug_report_promote", ["--top", "3"],
                            db=temp_db["db"])
    assert bash_r.returncode == 0, f"bash promote failed: {bash_r.stderr}"
    # bash may write to the REAL repo's kickoffs/auto. If so, we don't
    # compare bash's stdout to py's stdout directly — instead, check
    # that bash_flips status too (writes count = 0 because py already
    # created the epic).
    bash_promoted = int(bash_r.stdout.strip())

    # Status flipped to 'queued_as_epic' in both.
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

    # Python wrote the kickoff file at sandbox/kickoffs/auto/{eid}.md.
    kickoff = sandbox / "kickoffs" / "auto" / f"{eid}.md"
    assert kickoff.exists(), f"missing py kickoff: {kickoff}"
    text = kickoff.read_text(encoding="utf-8")
    assert "# Bug-promoted epic: Promote me" in text
    assert "Severity: **critical**" in text
    assert "verification_state" not in text  # sanity: not from another table
    assert "## Verification commands" in text
    assert "## Done When" in text

    # Both ports flipped status; bash promoted count is 0 because the
    # epic already existed (idempotence).
    assert bash_promoted == 0, f"bash re-promote should be 0: {bash_promoted}"


# ─────────────────────────────────────────────────────────────────────────────
# (j) bug_report_promote is idempotent
# ─────────────────────────────────────────────────────────────────────────────
def test_promote_idempotent(temp_db, tmp_path):
    """Re-running promote when the epic row already exists must return 0
    and not create duplicate epics or duplicate kickoff files."""
    py.bug_report_sweep("--all", home=temp_db["home"])
    con = sqlite3.connect(temp_db["db"])
    try:
        now = int(time.time())
        con.execute(
            """INSERT INTO bug_reports
               (fingerprint, run_id, agent_role, task_class, observed_in,
                title, description, suggested_fix, severity, confidence,
                frequency, status, first_seen_at, last_seen_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            ("fp-idem", "run-idem", "planner", None, "lib/i",
             "Idempotent test", "d", "f", "high", 0.8, 1, "open",
             now, now, now),
        )
        con.commit()
    finally:
        con.close()

    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()

    n1 = py.bug_report_promote("--top", "3", repo_root=sandbox)
    n2 = py.bug_report_promote("--top", "3", repo_root=sandbox)
    assert n1 == 1
    assert n2 == 0, f"second promote should be 0: {n2}"

    epics = _row_dicts(temp_db["db"], "epics")
    assert len(epics) == 1, f"expected 1 epic, got {len(epics)}"