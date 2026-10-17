"""LF-R12: harvested findings remember their target repo.

Covers the repo identity function, harvest stamping, the ``target_repo``
migration, the backfill CLI, and the repo-scoped selection in ``context_v2``
and the "Your code" tab. Hermetic: every repo is a temp git repo, every DB a
temp file — the live DB under ``.mini-ork/`` is never opened.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import time
from pathlib import Path

import mini_ork
from mini_ork import context_v2
from mini_ork.ide_pages.learn import code as learn_code
from mini_ork.learning import code_findings
from mini_ork.stores import migrate

_ALPHA = "defect marker alpha in repo A calc.py"
_BETA = "defect marker beta in repo B calc.py"
_GAMMA = "defect marker gamma has no recorded repo"


# ── helpers ────────────────────────────────────────────────────────────────

def _git(repo, *args):
    subprocess.run(
        ["git", "-c", "user.email=t@example.com", "-c", "user.name=Test",
         "-c", "init.defaultBranch=main", *args],
        cwd=str(repo), check=True, capture_output=True, text=True,
    )


def _make_repo(parent, name, files=("calc.py",)):
    repo = Path(parent) / name
    repo.mkdir()
    _git(repo, "init", "-q")
    for f in files:
        (repo / f).write_text("# fixture\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "fixture")
    return repo


_DDL = """
CREATE TABLE code_findings (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    fingerprint  TEXT    NOT NULL UNIQUE,
    run_id       TEXT    NOT NULL,
    source       TEXT    NOT NULL,
    file         TEXT,
    line         INTEGER,
    severity     TEXT    NOT NULL,
    category     TEXT    NOT NULL,
    issue        TEXT    NOT NULL,
    snippet      TEXT,
    verdict      TEXT,
    ts           INTEGER NOT NULL,
    target_repo  TEXT
);
CREATE TABLE task_runs (
    id           TEXT PRIMARY KEY,
    status       TEXT,
    cost_usd     REAL,
    created_at   INTEGER,
    kickoff_path TEXT
);
"""

_OLD_DDL = _DDL.replace("    ts           INTEGER NOT NULL,\n    target_repo  TEXT\n",
                        "    ts           INTEGER NOT NULL\n")


def _db(tmp_path, rows, *, runs=(), name="state.db"):
    """``rows``: (run_id, file, issue, target_repo); ``runs``:
    (run_id, status, created_at, kickoff_path)."""
    path = Path(tmp_path) / name
    con = sqlite3.connect(str(path))
    con.executescript(_DDL)
    now = int(time.time())
    for run_id, file, issue, target_repo in rows:
        fp = hashlib.sha1(f"{run_id}|{file}|{issue}".encode()).hexdigest()
        con.execute(
            "INSERT INTO code_findings (fingerprint, run_id, source, file, line,"
            " severity, category, issue, snippet, verdict, ts, target_repo)"
            " VALUES (?, ?, 'review:impl', ?, 1, 'high', 'wrong behaviour', ?,"
            " NULL, 'needs_revision', ?, ?)",
            (fp, run_id, file, issue, now, target_repo),
        )
    for run_id, status, created_at, kickoff_path in runs:
        con.execute("INSERT INTO task_runs VALUES (?, ?, 0.1, ?, ?)",
                    (run_id, status, created_at, kickoff_path))
    con.commit()
    con.close()
    return str(path)


def _engine_identity(monkeypatch):
    monkeypatch.delenv("MINI_ORK_ROOT", raising=False)
    parent = os.path.dirname(os.path.dirname(os.path.abspath(mini_ork.__file__)))
    return code_findings.repo_identity(parent)


def _kickoff(tmp_path):
    p = Path(tmp_path) / "kick-target-repo.md"
    p.write_text("# Target repo\n\n## Files in scope\n\n- `calc.py`\n\n"
                 "## Verification\n\n- `true`\n")
    return str(p)


def _issues(findings):
    return {f["issue"] for f in findings}


# ── repo_identity ──────────────────────────────────────────────────────────

def test_repo_and_worktree_share_one_identity(tmp_path):
    repo = _make_repo(tmp_path, "repoA")
    wt = Path(tmp_path) / "wt-a"
    _git(repo, "worktree", "add", str(wt))
    assert code_findings.repo_identity(str(repo)) == \
        code_findings.repo_identity(str(wt))
    # The worktree's own realpath is NOT the identity.
    assert code_findings.repo_identity(str(wt)) != os.path.realpath(str(wt))


def test_second_repo_has_a_different_identity(tmp_path):
    a = _make_repo(tmp_path, "repoA2")
    b = _make_repo(tmp_path, "repoB2")
    assert code_findings.repo_identity(str(a)) != \
        code_findings.repo_identity(str(b))


def test_plain_repo_identity_is_its_realpath(tmp_path):
    repo = _make_repo(tmp_path, "repoPlain")
    assert code_findings.repo_identity(str(repo)) == os.path.realpath(str(repo))


def test_missing_and_odd_inputs_never_raise(tmp_path):
    assert code_findings.repo_identity(str(tmp_path / "nope-xyz")) is None
    for odd in (None, "", str(tmp_path) + "/ no such dir"):
        code_findings.repo_identity(odd)


def test_non_git_existing_dir_falls_back_to_realpath(tmp_path):
    plain = Path(tmp_path) / "plain"
    plain.mkdir()
    assert code_findings.repo_identity(str(plain)) == os.path.realpath(str(plain))


# ── harvest stamping ───────────────────────────────────────────────────────

def _home_with_run(tmp_path, run_name, review, roots=None):
    run_dir = Path(tmp_path) / "home" / "runs" / run_name
    run_dir.mkdir(parents=True)
    if roots is not None:
        (run_dir / "run_profile.json").write_text(json.dumps({"roots": roots}))
    (run_dir / "review-impl.json").write_text(json.dumps(review))
    return Path(tmp_path) / "home"


_REVIEW = {
    "verdict": "needs_revision",
    "findings": [
        {"file": "calc.py", "line": 3, "severity": "high",
         "issue": "calc.py drops negative inputs"},
        {"file": "calc.py", "line": 9, "severity": "medium",
         "issue": "calc.py misses the carry"},
    ],
}


def _target_repos(db):
    con = sqlite3.connect(db)
    try:
        return [r[0] for r in con.execute("SELECT target_repo FROM code_findings")]
    finally:
        con.close()


def test_harvest_stamps_repo_identity_from_run_profile(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path, "repoHarvest")
    ident = code_findings.repo_identity(str(repo))
    monkeypatch.setenv("MINI_ORK_ROOT", str(repo))
    monkeypatch.delenv("MINI_ORK_HOME", raising=False)
    home = _home_with_run(tmp_path, "run-h", _REVIEW, {"target": str(repo)})
    db = Path(tmp_path) / "h.db"
    sqlite3.connect(str(db)).close()
    stats = code_findings.harvest(str(home), db=str(db))
    assert stats["reason"] == "ok"
    assert len(_target_repos(db)) >= 2
    assert set(_target_repos(db)) == {ident}


def test_harvest_without_profile_stamps_null(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path, "repoNoProfile")
    monkeypatch.setenv("MINI_ORK_ROOT", str(repo))
    monkeypatch.delenv("MINI_ORK_HOME", raising=False)
    home = _home_with_run(tmp_path, "run-np", _REVIEW, None)
    db = Path(tmp_path) / "np.db"
    sqlite3.connect(str(db)).close()
    code_findings.harvest(str(home), db=str(db))
    rows = _target_repos(db)
    assert rows and all(r is None for r in rows)


# ── backfill ───────────────────────────────────────────────────────────────

def _backfill_fixture(tmp_path):
    repo = _make_repo(tmp_path, "repoBackfill")
    home = Path(tmp_path) / "home"
    profiled = home / "runs" / "run-profiled"
    profiled.mkdir(parents=True)
    (profiled / "run_profile.json").write_text(
        json.dumps({"roots": {"target": str(repo)}}))
    (home / "runs" / "run-orphan").mkdir(parents=True)
    db = Path(tmp_path) / "bf.db"
    con = sqlite3.connect(str(db))
    con.executescript(_DDL)
    now = int(time.time())
    for run_id, issue in (("run-profiled", "marker alpha needs backfill"),
                          ("run-orphan", "marker delta has no profile")):
        con.execute(
            "INSERT INTO code_findings (fingerprint, run_id, source, file, line,"
            " severity, category, issue, snippet, verdict, ts, target_repo)"
            " VALUES (?, ?, 'review:impl', 'calc.py', 1, 'high', 'wrong behaviour',"
            " ?, NULL, 'needs_revision', ?, NULL)",
            (f"fp-{run_id}", run_id, issue, now),
        )
    con.commit()
    con.close()
    return repo, home, db


def test_backfill_fills_from_profiles_and_is_idempotent(tmp_path, monkeypatch):
    monkeypatch.delenv("MINI_ORK_DB", raising=False)
    monkeypatch.delenv("MINI_ORK_HOME", raising=False)
    repo, home, db = _backfill_fixture(tmp_path)
    ident = code_findings.repo_identity(str(repo))
    assert code_findings.backfill_target_repo(str(home), db=str(db)) == 1
    con = sqlite3.connect(str(db))
    try:
        got = dict(con.execute("SELECT run_id, target_repo FROM code_findings"))
    finally:
        con.close()
    assert got["run-profiled"] == ident
    assert got["run-orphan"] is None
    # The NULL orphan stays; nothing left to fill.
    assert code_findings.backfill_target_repo(str(home), db=str(db)) == 0


def test_backfill_cold_safe(tmp_path):
    assert code_findings.backfill_target_repo(
        str(tmp_path / "no-home"), db=str(tmp_path / "no.db")) == 0


def test_backfill_cli_surface(tmp_path, monkeypatch):
    monkeypatch.delenv("MINI_ORK_DB", raising=False)
    monkeypatch.delenv("MINI_ORK_HOME", raising=False)
    repo, home, db = _backfill_fixture(tmp_path)
    ident = code_findings.repo_identity(str(repo))
    rc = code_findings.main(
        ["backfill-target-repo", "--home", str(home), "--db", str(db)])
    assert rc == 0
    con = sqlite3.connect(str(db))
    try:
        stamped = con.execute(
            "SELECT target_repo FROM code_findings WHERE issue LIKE '%alpha%'"
        ).fetchone()
    finally:
        con.close()
    assert stamped == (ident,)


# ── ensure_schema + migration ──────────────────────────────────────────────

def _columns(db):
    con = sqlite3.connect(str(db))
    try:
        return [r[1] for r in con.execute("PRAGMA table_info(code_findings)")]
    finally:
        con.close()


def test_ensure_schema_adds_target_repo_idempotently(tmp_path):
    db = Path(tmp_path) / "ensure.db"
    con = sqlite3.connect(str(db))
    con.executescript(_OLD_DDL)
    con.commit()
    con.close()
    code_findings.ensure_schema(str(db))
    assert _columns(db).count("target_repo") == 1
    code_findings.ensure_schema(str(db))
    assert _columns(db).count("target_repo") == 1


def _migration_file() -> Path:
    root = Path(os.path.dirname(os.path.dirname(os.path.abspath(mini_ork.__file__))))
    hits = sorted((root / "db" / "migrations").glob("*code_findings_target_repo.sql"))
    assert len(hits) == 1, [h.name for h in hits]
    return hits[0]


def _migrate(tmp_path, name):
    mig = _migration_file()
    mdir = Path(tmp_path) / "migrations"
    mdir.mkdir()
    shutil.copy(str(mig), str(mdir / mig.name))
    db = Path(tmp_path) / name
    rc, out = migrate.migrate_apply(str(mdir), db=str(db))
    assert rc == 0, out
    return db, mig


def test_migration_applies_on_fresh_db(tmp_path):
    db = Path(tmp_path) / "fresh.db"
    con = sqlite3.connect(str(db))
    con.executescript(_OLD_DDL)
    con.commit()
    con.close()
    db, mig = _migrate(tmp_path, "fresh.db")
    assert _columns(db).count("target_repo") == 1
    con = sqlite3.connect(str(db))
    try:
        names = [r[1] for r in con.execute("PRAGMA index_list(code_findings)")]
        covered = {n for n in names
                   for r in con.execute(f"PRAGMA index_info({n})")
                   if r[2] == "target_repo"}
        recorded = con.execute(
            "SELECT COUNT(*) FROM schema_migrations WHERE filename = ?",
            (mig.name,)).fetchone()[0]
    finally:
        con.close()
    assert covered
    assert recorded >= 1


def test_migration_noop_when_ensure_schema_ran_first(tmp_path):
    db = Path(tmp_path) / "precolumned.db"
    sqlite3.connect(str(db)).close()
    code_findings.ensure_schema(str(db))
    assert _columns(db).count("target_repo") == 1
    db, mig = _migrate(tmp_path, "precolumned.db")
    assert _columns(db).count("target_repo") == 1
    con = sqlite3.connect(str(db))
    try:
        assert con.execute(
            "SELECT COUNT(*) FROM schema_migrations WHERE filename = ?",
            (mig.name,)).fetchone()[0] >= 1
    finally:
        con.close()


# ── context_v2 selection ───────────────────────────────────────────────────

def test_scope_findings_keeps_only_the_current_repo(tmp_path):
    a = code_findings.repo_identity(str(_make_repo(tmp_path, "repoA-sc")))
    b = code_findings.repo_identity(str(_make_repo(tmp_path, "repoB-sc")))
    db = _db(tmp_path, [("ra", "calc.py", _ALPHA, a),
                        ("rb", "calc.py", _BETA, b)])
    assert _issues(context_v2.scope_findings(["calc.py"], db=db, target_repo=b)) \
        == {_BETA}
    assert _issues(context_v2.scope_findings(["calc.py"], db=db, target_repo=a)) \
        == {_ALPHA}


def test_parent_fallback_is_repo_scoped_too(tmp_path):
    a = code_findings.repo_identity(str(_make_repo(tmp_path, "repoA-pf")))
    b = code_findings.repo_identity(str(_make_repo(tmp_path, "repoB-pf")))
    deep = "pkg/src/deep/calc.py"
    db = _db(tmp_path, [("ra", deep, _ALPHA, a), ("rb", deep, _BETA, b)])
    got = context_v2.scope_findings(["pkg/src/deep/new.py"], db=db, target_repo=a)
    assert _issues(got) == {_ALPHA}
    got = context_v2.scope_findings(["pkg/src/deep/new.py"], db=db, target_repo=b)
    assert _issues(got) == {_BETA}


def test_null_target_repo_is_engine_local_only(tmp_path, monkeypatch):
    engine = _engine_identity(monkeypatch)
    a = code_findings.repo_identity(str(_make_repo(tmp_path, "repoA-null")))
    assert a != engine
    db = _db(tmp_path, [("rn", "nulls.py", _GAMMA, None)])
    assert _issues(context_v2.scope_findings(
        ["nulls.py"], db=db, target_repo=engine)) == {_GAMMA}
    assert context_v2.scope_findings(["nulls.py"], db=db, target_repo=a) == []


def test_prior_attempts_filters_by_repo(tmp_path, monkeypatch):
    monkeypatch.delenv("MINI_ORK_ROOT", raising=False)
    a = code_findings.repo_identity(str(_make_repo(tmp_path, "repoA-pa")))
    b = code_findings.repo_identity(str(_make_repo(tmp_path, "repoB-pa")))
    kp = _kickoff(tmp_path)
    now = int(time.time())
    db = _db(tmp_path, [("p1", "calc.py", _ALPHA, a), ("p2", "calc.py", _BETA, b)],
             runs=[("p1", "failed", now, kp), ("p2", "failed", now, kp)],
             name="prior.db")
    got = context_v2.prior_attempts(kp, db=db, target_repo=a)
    assert _ALPHA in {f["issue"] for p in got for f in p["findings"]}
    assert _BETA not in {f["issue"] for p in got for f in p["findings"]}


def test_build_derives_identity_from_mo_target_cwd(tmp_path, monkeypatch):
    for var in ("MINI_ORK_ROOT", "MO_CONTEXT_V2_MIN_SEVERITY"):
        monkeypatch.delenv(var, raising=False)
    a = _make_repo(tmp_path, "repoA-build")
    b = _make_repo(tmp_path, "repoB-build")
    ia = code_findings.repo_identity(str(a))
    ib = code_findings.repo_identity(str(b))
    monkeypatch.setenv("MO_TARGET_CWD", str(a))
    kp = _kickoff(tmp_path)
    db = _db(tmp_path, [("ra", "calc.py", _ALPHA, ia),
                        ("rb", "calc.py", _BETA, ib),
                        ("rn", "calc.py", _GAMMA, None)])
    pack = context_v2.build(kp, db=db)
    assert pack["n_findings_scanned"] == 1
    reps = [c["representative"] for c in pack["file_findings"]]
    assert any(_ALPHA in r for r in reps)
    assert not any(_BETA in r or _GAMMA in r for r in reps)


def test_build_without_env_uses_engine_and_keeps_null_rows(tmp_path, monkeypatch):
    for var in ("MO_TARGET_CWD", "MINI_ORK_ROOT", "MO_CONTEXT_V2_MIN_SEVERITY"):
        monkeypatch.delenv(var, raising=False)
    a = _make_repo(tmp_path, "repoA-eng")
    b = _make_repo(tmp_path, "repoB-eng")
    kp = _kickoff(tmp_path)
    db = _db(tmp_path, [
        ("ra", "calc.py", _ALPHA, code_findings.repo_identity(str(a))),
        ("rb", "calc.py", _BETA, code_findings.repo_identity(str(b))),
        ("rn", "calc.py", _GAMMA, None),
    ])
    pack = context_v2.build(kp, db=db)
    assert pack["n_findings_scanned"] == 1
    assert any(_GAMMA in c["representative"] for c in pack["file_findings"])
    assert not any(_ALPHA in c["representative"] or _BETA in c["representative"]
                   for c in pack["file_findings"])


def test_pack_for_run_uses_the_profile_target(tmp_path, monkeypatch):
    for var in ("MO_TARGET_CWD", "MINI_ORK_ROOT", "MINI_ORK_DB", "MINI_ORK_HOME",
                "MO_CONTEXT_V2_MIN_SEVERITY"):
        monkeypatch.delenv(var, raising=False)
    a = _make_repo(tmp_path, "repoA-pack")
    b = _make_repo(tmp_path, "repoB-pack")
    kp = _kickoff(tmp_path)
    run_dir = Path(tmp_path) / "run-pack"
    run_dir.mkdir()
    (run_dir / "run_profile.json").write_text(
        json.dumps({"kickoff_path": kp, "roots": {"target": str(a)}}))
    db = _db(tmp_path, [
        ("ra", "calc.py", _ALPHA, code_findings.repo_identity(str(a))),
        ("rb", "calc.py", _BETA, code_findings.repo_identity(str(b))),
        ("rn", "calc.py", _GAMMA, None),
    ])
    pack = context_v2.pack_for_run(str(run_dir), db=db)
    assert pack["n_findings_scanned"] == 1
    assert any(_ALPHA in c["representative"] for c in pack["file_findings"])
    assert not any(_BETA in c["representative"] or _GAMMA in c["representative"]
                   for c in pack["file_findings"])


# ── IDE window ─────────────────────────────────────────────────────────────

_TAB_FILE = "mini_ork/context_v2.py"


def _window_db(tmp_path, rows):
    db = Path(tmp_path) / "window.db"
    con = sqlite3.connect(str(db))
    con.executescript(_DDL)
    now = int(time.time())
    for run_id, target_repo in rows:
        con.execute(
            "INSERT INTO code_findings (fingerprint, run_id, source, file, line,"
            " severity, category, issue, snippet, verdict, ts, target_repo)"
            " VALUES (?, ?, 'review:impl', ?, 1, 'high', 'wrong behaviour', ?,"
            " NULL, 'needs_revision', ?, ?)",
            (f"fp-{run_id}", run_id, _TAB_FILE, f"window marker {run_id}",
             now, target_repo),
        )
    con.commit()
    con.close()
    return str(db)


def test_window_findings_shows_only_the_given_repo(tmp_path, monkeypatch):
    monkeypatch.delenv("MINI_ORK_ROOT", raising=False)
    a = code_findings.repo_identity(str(_make_repo(tmp_path, "repoA-win")))
    b = code_findings.repo_identity(str(_make_repo(tmp_path, "repoB-win")))
    db = _window_db(tmp_path, [("ra", a), ("rb", b)])
    assert {f["run_id"] for f in learn_code._window_findings(
        db, since_days=30, target_repo=b)} == {"rb"}
    assert {f["run_id"] for f in learn_code._window_findings(
        db, since_days=30, target_repo=a)} == {"ra"}


def test_window_findings_keeps_null_rows_only_for_engine(tmp_path, monkeypatch):
    engine = _engine_identity(monkeypatch)
    a = code_findings.repo_identity(str(_make_repo(tmp_path, "repoA-wn")))
    assert a != engine
    db = _window_db(tmp_path, [("rn", None)])
    assert {f["run_id"] for f in learn_code._window_findings(
        db, since_days=30, target_repo=engine)} == {"rn"}
    assert learn_code._window_findings(db, since_days=30, target_repo=a) == []
