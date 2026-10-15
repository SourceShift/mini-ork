"""Hermetic tests for run-scoped review dedupe and ``prune_duplicates``.

One structured finding was landing twice per run: once as the reviewer's own
``review:*`` source and once as the ``review:verdict`` file echoing it (the
kickoff's 38 already-doubled rows). ``_harvest_run`` now dedupes within a run on
``(normalized file, line, issue.strip())`` across ``review:*`` sources only,
processing ``review:verdict`` last so the reviewer's source wins, and
``prune_duplicates`` cleans rows written before that rule.

Everything is hermetic: a temp "home" holding hand-written ``review-*.json`` /
``verifier-*.json`` files, a temp SQLite DB (touched first — ``_open`` refuses a
path missing on disk), and no network, credentials, or run-local state.
"""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from mini_ork.learning import code_findings  # noqa: E402

# The finding every scenario echoes between files. The file value carries a "/"
# so path normalization is the identity on it (no bare-name resolution).
FINDING = {
    "file": "mini_ork/leak.py",
    "line": 24,
    "issue": "handle leaked on the error path",
    "severity": "high",
}


def _home(tmp_path: Path, runs=("run-alpha",)):
    runs_root = tmp_path / "home" / "runs"
    runs_root.mkdir(parents=True)
    for name in runs:
        (runs_root / name).mkdir()
    db = tmp_path / "state.db"
    db.touch()
    return tmp_path / "home", str(db)


def _dbs(tmp_path: Path) -> str:
    db = tmp_path / "state.db"
    db.touch()
    code_findings.ensure_schema(str(db))
    return str(db)


def _review(run_dir: Path, role: str, findings, verdict: str = "fail") -> None:
    (run_dir / f"review-{role}.json").write_text(
        json.dumps({"verdict": verdict, "findings": findings}), encoding="utf-8"
    )


def _verifier(run_dir: Path, fname: str, payload) -> None:
    (run_dir / fname).write_text(json.dumps(payload), encoding="utf-8")


def _rows(db: str):
    con = sqlite3.connect(db)
    try:
        return con.execute(
            "SELECT run_id, source, file, line, issue FROM code_findings ORDER BY id"
        ).fetchall()
    finally:
        con.close()


def _insert(db: str, fp, run, source, file, line, issue) -> None:
    con = sqlite3.connect(db)
    try:
        con.execute(
            "INSERT INTO code_findings "
            "(fingerprint, run_id, source, file, line, severity, category, issue, "
            " snippet, verdict, ts) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)",
            (fp, run, source, file, line, "high", "bug", issue, None, "fail"),
        )
        con.commit()
    finally:
        con.close()


# ── harvest: dedupe within one run, reviewer's source wins ─────────────────────


def test_reviewer_twin_deduped_reviewer_wins(tmp_path, monkeypatch) -> None:
    """'dedupe review:verdict echoes of other review:* findings ... keeping the
    non-verdict review source': the twin inserts once, as the reviewer."""
    monkeypatch.setattr(code_findings, "_git_ls_files", lambda *_: None)
    home, db = _home(tmp_path)
    run_dir = home / "runs" / "run-alpha"
    _review(run_dir, "reviewer", [dict(FINDING)])
    # Same file/line/issue, different severity: severity is not part of the key.
    _review(run_dir, "verdict", [dict(FINDING, severity="low")])

    stats = code_findings.harvest(str(home), db=db)

    assert stats["reason"] == "ok", stats
    # Deduped *before* insert, so the echo is never counted as a finding.
    assert stats["findings"] == 1, stats
    assert _rows(db) == [
        ("run-alpha", "review:reviewer", "mini_ork/leak.py", 24,
         "handle leaked on the error path")
    ]


def test_verdict_only_finding_still_inserts(tmp_path, monkeypatch) -> None:
    """'while verdict-only findings still insert'."""
    monkeypatch.setattr(code_findings, "_git_ls_files", lambda *_: None)
    home, db = _home(tmp_path)
    _review(home / "runs" / "run-alpha", "verdict", [dict(FINDING)])

    stats = code_findings.harvest(str(home), db=db)

    assert stats["findings"] == 1, stats
    assert _rows(db) == [
        ("run-alpha", "review:verdict", "mini_ork/leak.py", 24,
         "handle leaked on the error path")
    ]


def test_reviewer_wins_regardless_of_file_order(tmp_path, monkeypatch) -> None:
    """'ordering review:verdict after every other review source': a naive
    first-row-wins dedupe would keep the echo, since ``review-verdict.json``
    sorts before ``review-zzreport.json``. The padded issue also exercises the
    ``issue.strip()`` part of the key."""
    monkeypatch.setattr(code_findings, "_git_ls_files", lambda *_: None)
    home, db = _home(tmp_path)
    run_dir = home / "runs" / "run-alpha"
    _review(run_dir, "verdict", [dict(FINDING, issue="  handle leaked on the error path  ")])
    _review(run_dir, "zzreport", [dict(FINDING)])

    stats = code_findings.harvest(str(home), db=db)

    assert stats["findings"] == 1, stats
    assert _rows(db) == [
        ("run-alpha", "review:zzreport", "mini_ork/leak.py", 24,
         "handle leaked on the error path")
    ]


def test_dedupe_scoped_within_one_run(tmp_path, monkeypatch) -> None:
    """'dedupe within one run': one row per run, never one per home."""
    monkeypatch.setattr(code_findings, "_git_ls_files", lambda *_: None)
    home, db = _home(tmp_path, runs=("run-one", "run-two"))
    _review(home / "runs" / "run-one", "reporter", [dict(FINDING)])
    _review(home / "runs" / "run-two", "verdict", [dict(FINDING)])

    stats = code_findings.harvest(str(home), db=db)

    assert stats["findings"] == 2, stats
    assert _rows(db) == [
        ("run-one", "review:reporter", "mini_ork/leak.py", 24,
         "handle leaked on the error path"),
        ("run-two", "review:verdict", "mini_ork/leak.py", 24,
         "handle leaked on the error path"),
    ]


def test_reharvest_with_run_ids_is_noop(tmp_path, monkeypatch) -> None:
    """An explicit re-harvest of an old run stays a no-op (fingerprint intact)."""
    monkeypatch.setattr(code_findings, "_git_ls_files", lambda *_: None)
    home, db = _home(tmp_path)
    run_dir = home / "runs" / "run-alpha"
    _review(run_dir, "reviewer", [dict(FINDING)])
    _review(run_dir, "verdict", [dict(FINDING)])
    assert code_findings.harvest(str(home), db=db)["findings"] == 1

    again = code_findings.harvest(str(home), run_ids=["run-alpha"], db=db)

    assert again["findings"] == 0, again
    assert len(_rows(db)) == 1


def test_verifier_rows_never_deduped(tmp_path, monkeypatch) -> None:
    """Two verifier files failing for the same reason keep both rows; a review
    row on the same file neither shadows nor is shadowed by them."""
    monkeypatch.setattr(code_findings, "_git_ls_files", lambda *_: None)
    home, db = _home(tmp_path)
    run_dir = home / "runs" / "run-alpha"
    reason = {"pass": False, "reason": "gate failed for mini_ork/leak.py: timeout"}
    # Both naming conventions the harvest reads, one shared reason.
    _verifier(run_dir, "verifier-static.json", reason)
    _verifier(run_dir, "verifier_pytest.json", reason)
    _review(run_dir, "reporter", [dict(FINDING)])

    stats = code_findings.harvest(str(home), db=db)

    assert stats["findings"] == 3, stats
    rows = _rows(db)
    assert len(rows) == 3, rows
    sources = {r[1] for r in rows}
    assert {"verifier:static", "verifier:pytest", "review:reporter"} <= sources, rows


# ── prune_duplicates ───────────────────────────────────────────────────────────


def _seed_twin_and_verdict_only(db: str) -> None:
    _insert(db, "fp-a", "r1", "review:reviewer", "mini_ork/a.py", 3, "off by one")
    # Same (run_id, file, line, issue) — severity/snippet differ on purpose.
    _insert(db, "fp-b", "r1", "review:verdict", "mini_ork/a.py", 3, "off by one")
    _insert(db, "fp-c", "r2", "review:verdict", "mini_ork/b.py", None, "missing test")


def test_prune_deletes_twin_keeps_verdict_only_and_is_idempotent(tmp_path) -> None:
    db = _dbs(tmp_path)
    _seed_twin_and_verdict_only(db)

    assert code_findings.prune_duplicates(db) == 1
    assert _rows(db) == [
        ("r1", "review:reviewer", "mini_ork/a.py", 3, "off by one"),
        ("r2", "review:verdict", "mini_ork/b.py", None, "missing test"),
    ]
    # Idempotent: the echo is gone, nothing left to prune.
    assert code_findings.prune_duplicates(db) == 0
    assert len(_rows(db)) == 2


def test_prune_matches_null_file_and_line(tmp_path) -> None:
    """The shadow key uses IFNULL(file,'') / IFNULL(line,-1), so NULL-bearing
    rows match the harvest key."""
    db = _dbs(tmp_path)
    _insert(db, "fp-d", "r3", "review:reviewer", None, None, "no location")
    _insert(db, "fp-e", "r3", "review:verdict", None, None, "no location")

    assert code_findings.prune_duplicates(db) == 1
    assert _rows(db) == [("r3", "review:reviewer", None, None, "no location")]


def test_prune_never_crosses_runs(tmp_path) -> None:
    """The run_id in the key is load-bearing: once per run, never once ever."""
    db = _dbs(tmp_path)
    _insert(db, "fp-f", "r1", "review:reviewer", "mini_ork/a.py", 3, "off by one")
    _insert(db, "fp-g", "r2", "review:verdict", "mini_ork/a.py", 3, "off by one")

    assert code_findings.prune_duplicates(db) == 0
    assert len(_rows(db)) == 2


def test_prune_never_touches_verifier_rows(tmp_path) -> None:
    """Verifier rows are outside the dedupe domain: even a verdict row whose
    only twin is a verifier row survives."""
    db = _dbs(tmp_path)
    _insert(db, "fp-h", "r4", "verifier:test", "mini_ork/a.py", 3, "off by one")
    _insert(db, "fp-i", "r4", "review:verdict", "mini_ork/a.py", 3, "off by one")

    assert code_findings.prune_duplicates(db) == 0
    assert len(_rows(db)) == 2


def test_prune_missing_db_returns_zero_without_raising(tmp_path) -> None:
    assert code_findings.prune_duplicates(str(tmp_path / "no-such.db")) == 0


# ── CLI ────────────────────────────────────────────────────────────────────────


def test_module_cli_exposes_prune_duplicates(tmp_path, capsys) -> None:
    db = _dbs(tmp_path)
    _seed_twin_and_verdict_only(db)

    rc = code_findings.main(["prune-duplicates", "--db", db, "--json"])

    assert rc == 0
    assert json.loads(capsys.readouterr().out) == 1
    assert _rows(db) == [
        ("r1", "review:reviewer", "mini_ork/a.py", 3, "off by one"),
        ("r2", "review:verdict", "mini_ork/b.py", None, "missing test"),
    ]


def test_module_cli_prune_duplicates_as_subprocess(tmp_path) -> None:
    db = _dbs(tmp_path)
    _seed_twin_and_verdict_only(db)

    # The subprocess must resolve mini_ork from this checkout regardless of the
    # session's cwd, and must not pick up the caller's live DB/home.
    env = os.environ.copy()
    env.pop("MINI_ORK_DB", None)
    env.pop("MINI_ORK_HOME", None)
    env["PYTHONPATH"] = str(REPO) + os.pathsep + env.get("PYTHONPATH", "")
    proc = subprocess.run(
        [sys.executable, "-m", "mini_ork.learning.code_findings",
         "prune-duplicates", "--db", db, "--json"],
        capture_output=True, text=True, timeout=120, env=env,
    )

    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout) == 1, proc.stdout
