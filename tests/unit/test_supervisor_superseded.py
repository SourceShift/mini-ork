"""SDE-4b: ``superseded`` runs and the supervisor's R4/R5 rules.

Hermetic: a temp home whose ``state.db`` is built by applying the repo's real
migrations (so the widened ``task_runs.status`` CHECK is the one under test),
plus a throwaway git repo whose ``refs/remotes/origin/main`` is the landing ref.
Nothing reaches the operator's real trees.
"""
from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest

from mini_ork import run_status
from mini_ork.orchestration import run_reaper
from mini_ork.recovery import supervisor

REPO_ROOT = Path(__file__).resolve().parents[2]
T0 = 1_790_000_000


# ── fixtures ────────────────────────────────────────────────────────────────


def _build_db(db_path: Path) -> str:
    from mini_ork.scheduler import ensure_retry_schema
    from mini_ork.stores.migrate import migrate_apply

    rc, out = migrate_apply(str(REPO_ROOT / "db" / "migrations"),
                            db=str(db_path), root=str(REPO_ROOT))
    assert rc == 0, f"migrations failed: {out[-5:]}"
    ensure_retry_schema(str(db_path))
    return str(db_path)


@pytest.fixture()
def env(tmp_path):
    home = tmp_path / "home"
    (home / "runs").mkdir(parents=True)
    db = _build_db(home / "state.db")
    engine = tmp_path / "engine"
    engine.mkdir()
    _git(engine, "init", "-q")
    _git(engine, "config", "user.email", "t@example.invalid")
    _git(engine, "config", "user.name", "T")
    (engine / "seed.txt").write_text("seed\n", encoding="utf-8")
    _git(engine, "add", "-A")
    _git(engine, "-c", "commit.gpgsign=false", "commit", "-q", "-m", "seed")
    _git(engine, "update-ref", "refs/remotes/origin/main", "HEAD")
    return home, db, engine


def _git(repo, *args):
    proc = subprocess.run(["git", "-C", str(repo), "-c", "core.hooksPath=/dev/null",
                           *args], capture_output=True, text=True)
    assert proc.returncode == 0, f"git {args}: {proc.stderr.strip()}"
    return proc.stdout


def _commit(engine, name, text, message) -> str:
    (Path(engine) / name).write_text(text, encoding="utf-8")
    _git(engine, "add", "-A")
    _git(engine, "-c", "commit.gpgsign=false", "commit", "-q", "-m", message)
    sha = _git(engine, "rev-parse", "HEAD").strip()
    _git(engine, "update-ref", "refs/remotes/origin/main", sha)
    return sha


def _exec(db, sql, args=()):
    con = sqlite3.connect(str(db))
    try:
        con.execute(sql, args)
        con.commit()
    finally:
        con.close()


def _rows(db, sql, args=()):
    con = sqlite3.connect(str(db))
    try:
        return con.execute(sql, args).fetchall()
    finally:
        con.close()


def _add_run(db, run_id, status, *, kickoff="kick/k.md", created_at=T0,
             notes=None):
    _exec(db,
          "INSERT INTO task_runs (id, task_class, kickoff_path, status,"
          " created_at, updated_at, notes) VALUES (?,?,?,?,?,?,?)",
          (run_id, "code_fix", kickoff, status, created_at, created_at, notes))


def _add_epic(db, epic_id, *, status="done", last_run_id=None, notes=None):
    _exec(db, "INSERT INTO epics (id, title, status, last_run_id, notes)"
              " VALUES (?,?,?,?,?)",
          (epic_id, f"{epic_id} title", status, last_run_id, notes))


def _add_attempt(db, epic_id, attempt, run_id):
    _exec(db, "INSERT INTO epic_attempts (epic_id, attempt, run_id, started_at)"
              " VALUES (?,?,?,?)",
          (epic_id, attempt, run_id, f"2026-01-{attempt:02d}T00:00:00Z"))


def _sweep(home, db, engine, *, dry_run=False):
    return supervisor.sweep(str(home), str(db), engine_root=str(engine),
                            now=T0 + 10_000, dry_run=dry_run)


def _run_row(db, run_id):
    rows = _rows(db, "SELECT status, notes FROM task_runs WHERE id=?", (run_id,))
    return rows[0] if rows else None


def _epic_row(db, epic_id):
    rows = _rows(db, "SELECT status, notes FROM epics WHERE id=?", (epic_id,))
    return rows[0] if rows else None


def _supersede(actions):
    return [a for a in actions if a.get("action") == "supersede"]


def _backlink(actions):
    return [a for a in actions if a.get("action") == "backlink"]


def _dead_pid() -> int:
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait(timeout=60)
    return proc.pid


# ── clause 1: the migration ─────────────────────────────────────────────────


def test_migration_accepts_superseded_and_rejects_unknown(env):
    _, db, _ = env
    _add_run(db, "run-sup", "superseded")
    assert _run_row(db, "run-sup")[0] == "superseded"
    with pytest.raises(sqlite3.IntegrityError):
        _add_run(db, "run-bogus", "no_such_status")


def test_migration_keeps_every_prior_status(env):
    _, db, _ = env
    for i, status in enumerate(["classified", "planned", "executing",
                                "verifying", "reviewing", "published",
                                "rolled_back", "failed", "not_needed"]):
        _add_run(db, f"run-{i}", status, created_at=T0 + i)
    for i in range(9):
        assert _run_row(db, f"run-{i}") is not None


# ── clause 2: run_status ────────────────────────────────────────────────────


def test_superseded_is_terminal_but_not_unsuccessful():
    assert "superseded" in run_status.TERMINAL
    assert "superseded" not in run_status.UNSUCCESSFUL
    assert "superseded" in run_status.STATUSES
    assert "superseded" not in run_status.LIVE


def test_superseded_bucket_and_label():
    assert run_status.DEFAULT_BUCKET["superseded"] == "not_needed"
    assert run_status.LABELS["superseded"] == "Superseded"
    assert run_status.label("superseded") == "Superseded"
    assert run_status.BUCKETS == ("working", "needs_you", "done", "failed",
                                  "not_needed")


def test_sql_fragments_carry_superseded():
    assert "'superseded'" in run_status.SQL_TERMINAL
    assert "'superseded'" not in run_status.SQL_UNSUCCESSFUL


# ── clause 4a: a later published run of the same kickoff ────────────────────


def test_r4a_later_published_run_supersedes(env):
    home, db, engine = env
    _add_run(db, "run-fail", "failed", kickoff="kick/a.md", created_at=T0)
    _add_run(db, "run-pub", "published", kickoff="kick/a.md",
             created_at=T0 + 100)

    actions = _sweep(home, db, engine)
    hits = [a for a in _supersede(actions) if a["run_id"] == "run-fail"]
    assert len(hits) == 1
    assert hits[0]["reason"] == "later run run-pub published"
    status, notes = _run_row(db, "run-fail")
    assert status == "superseded"
    assert notes.endswith("superseded: later run run-pub published")

    # Compare-and-set: a second sweep must not append the note again.
    again = _sweep(home, db, engine)
    assert not [a for a in _supersede(again) if a["run_id"] == "run-fail"]
    assert _run_row(db, "run-fail") == (status, notes)


def test_r4a_earlier_published_run_does_not_count(env):
    home, db, engine = env
    _add_run(db, "run-pub-early", "published", kickoff="kick/a.md",
             created_at=T0)
    _add_run(db, "run-fail-late", "failed", kickoff="kick/a.md",
             created_at=T0 + 100)

    actions = _sweep(home, db, engine)
    assert not [a for a in _supersede(actions)
                if a["run_id"] == "run-fail-late"]
    assert _run_row(db, "run-fail-late") == ("failed", None)


# ── clause 4b: the diff landed on origin/main ───────────────────────────────


def test_r4b_landed_diff_supersedes(env):
    home, db, engine = env
    sha = _commit(engine, "landed.txt", "landed change\n", "land it")
    run_dir = home / "runs" / "run-fail-landed"
    run_dir.mkdir(parents=True)
    (run_dir / "framework-edit.diff").write_text(
        _git(engine, "show", "--format=", sha), encoding="utf-8")
    _add_run(db, "run-fail-landed", "failed")

    actions = _sweep(home, db, engine)
    hits = [a for a in _supersede(actions) if a["run_id"] == "run-fail-landed"]
    assert len(hits) == 1
    assert hits[0]["reason"] == "diff landed on origin/main"
    status, notes = _run_row(db, "run-fail-landed")
    assert status == "superseded"
    assert notes.endswith("superseded: diff landed on origin/main")


def test_r4b_landed_patch_ids_computed_once_per_sweep(env, monkeypatch):
    home, db, engine = env
    sha = _commit(engine, "landed.txt", "landed change\n", "land it")
    patch = _git(engine, "show", "--format=", sha)
    for run_id in ("run-a", "run-b"):
        run_dir = home / "runs" / run_id
        run_dir.mkdir(parents=True)
        (run_dir / "framework-edit.diff").write_text(patch, encoding="utf-8")
        _add_run(db, run_id, "failed", kickoff=f"kick/{run_id}.md")

    calls = []
    real = supervisor.landed_patch_ids

    def counting(*args, **kwargs):
        calls.append(1)
        return real(*args, **kwargs)

    monkeypatch.setattr(supervisor, "landed_patch_ids", counting)
    actions = _sweep(home, db, engine)
    assert {"run-a", "run-b"} <= {a["run_id"] for a in _supersede(actions)}
    assert len(calls) == 1


# ── clause 4c: a done epic's failed attempt ─────────────────────────────────


def test_r4c_done_epic_attempt_supersedes(env):
    home, db, engine = env
    _add_run(db, "run-fail-epic", "failed")
    _add_epic(db, "epic-done", last_run_id="run-fail-epic")
    _add_attempt(db, "epic-done", 1, "run-fail-epic")

    actions = _sweep(home, db, engine)
    hits = [a for a in _supersede(actions) if a["run_id"] == "run-fail-epic"]
    assert len(hits) == 1
    assert hits[0]["reason"] == "epic epic-done done"
    status, notes = _run_row(db, "run-fail-epic")
    assert status == "superseded"
    assert notes.endswith("superseded: epic epic-done done")


def test_r4c_epic_not_done_does_not_supersede(env):
    home, db, engine = env
    _add_run(db, "run-fail-blocked", "failed")
    _add_epic(db, "epic-blocked", status="blocked",
              last_run_id="run-fail-blocked")
    _add_attempt(db, "epic-blocked", 1, "run-fail-blocked")

    actions = _sweep(home, db, engine)
    assert not [a for a in _supersede(actions)
                if a["run_id"] == "run-fail-blocked"]
    assert _run_row(db, "run-fail-blocked") == ("failed", None)


# ── R4 guards ───────────────────────────────────────────────────────────────


@pytest.mark.parametrize("state", ["waiting_on_fix", "reviving", "repairing"])
def test_r4_skips_runs_owned_by_a_repair_loop(env, state):
    home, db, engine = env
    run_id = f"run-owned-{state}"
    _add_run(db, run_id, "failed", kickoff="kick/owned.md", created_at=T0)
    _add_run(db, "run-pub", "published", kickoff="kick/owned.md",
             created_at=T0 + 100)
    run_dir = home / "runs" / run_id
    run_dir.mkdir(parents=True)
    (run_dir / "repair.json").write_text(json.dumps({"state": state}),
                                         encoding="utf-8")

    actions = _sweep(home, db, engine)
    assert not [a for a in _supersede(actions) if a["run_id"] == run_id]
    assert _run_row(db, run_id) == ("failed", None)


def test_r4_never_touches_published_or_executing_runs(env):
    home, db, engine = env
    _add_run(db, "run-exec", "executing", kickoff="kick/e.md", created_at=T0)
    _add_run(db, "run-pub-e", "published", kickoff="kick/e.md",
             created_at=T0 + 100)
    _add_run(db, "run-pub-old", "published", kickoff="kick/p.md",
             created_at=T0)
    _add_run(db, "run-pub-new", "published", kickoff="kick/p.md",
             created_at=T0 + 100)

    actions = _sweep(home, db, engine)
    assert _supersede(actions) == []
    assert _run_row(db, "run-exec") == ("executing", None)
    assert _run_row(db, "run-pub-old")[0] == "published"
    assert _run_row(db, "run-pub-new")[0] == "published"


# ── clause 4 R5: back-link delivered epics ──────────────────────────────────


def test_r5_published_attempt_backlinks_latest_and_is_idempotent(env):
    home, db, engine = env
    _add_run(db, "run-failed", "failed", kickoff="kick/r5.md", created_at=T0)
    _add_run(db, "run-pub-early", "published", kickoff="kick/r5.md",
             created_at=T0 + 10)
    _add_run(db, "run-pub-late", "published", kickoff="kick/r5.md",
             created_at=T0 + 20)
    _add_epic(db, "epic-r5", last_run_id="run-failed")
    _add_attempt(db, "epic-r5", 1, "run-failed")
    _add_attempt(db, "epic-r5", 2, "run-pub-early")
    _add_attempt(db, "epic-r5", 3, "run-pub-late")

    actions = _sweep(home, db, engine)
    hits = [a for a in _backlink(actions) if a["epic_id"] == "epic-r5"]
    assert len(hits) == 1 and hits[0]["reason"]
    _, notes = _epic_row(db, "epic-r5")
    assert notes.endswith("delivered_by=run-pub-late")

    again = _sweep(home, db, engine)
    assert not [a for a in _backlink(again) if a["epic_id"] == "epic-r5"]
    assert _epic_row(db, "epic-r5")[1] == notes
    assert notes.count("delivered_by=") == 1


def test_r5_origin_main_commit_naming_the_epic(env):
    home, db, engine = env
    sha = _commit(engine, "handoff.txt", "delivered\n",
                  "epic EPIC-4B-COMMIT delivered by hand")
    _add_run(db, "run-r5c-failed", "failed", kickoff="kick/r5c.md")
    _add_epic(db, "EPIC-4B-COMMIT", last_run_id="run-r5c-failed")

    actions = _sweep(home, db, engine)
    hits = [a for a in _backlink(actions) if a["epic_id"] == "EPIC-4B-COMMIT"]
    assert len(hits) == 1
    assert _epic_row(db, "EPIC-4B-COMMIT")[1].endswith(
        f"delivered_by=commit:{sha}")


def test_r5_no_evidence_no_action(env):
    home, db, engine = env
    _commit(engine, "other.txt", "unrelated\n", "an unrelated landing")
    _add_run(db, "run-r5n-failed", "failed", kickoff="kick/r5n.md")
    _add_epic(db, "epic-r5-none", last_run_id="run-r5n-failed")
    _add_attempt(db, "epic-r5-none", 1, "run-r5n-failed")

    actions = _sweep(home, db, engine)
    assert not [a for a in _backlink(actions) if a["epic_id"] == "epic-r5-none"]
    assert _epic_row(db, "epic-r5-none") == ("done", None)


def test_r5_skips_not_done_self_published_and_already_marked(env):
    home, db, engine = env
    _add_run(db, "run-failed", "failed", kickoff="kick/x.md")
    _add_run(db, "run-pub", "published", kickoff="kick/y.md")
    _add_epic(db, "epic-blocked", status="blocked", last_run_id="run-failed")
    _add_attempt(db, "epic-blocked", 1, "run-failed")
    _add_attempt(db, "epic-blocked", 2, "run-pub")
    _add_epic(db, "epic-selfpub", last_run_id="run-pub")
    _add_attempt(db, "epic-selfpub", 1, "run-pub")
    _add_epic(db, "epic-marked", last_run_id="run-failed",
              notes="delivered_by=run-999 already")
    _add_attempt(db, "epic-marked", 1, "run-pub")

    actions = _sweep(home, db, engine)
    assert _backlink(actions) == []
    assert _epic_row(db, "epic-blocked")[1] is None
    assert _epic_row(db, "epic-selfpub")[1] is None
    assert _epic_row(db, "epic-marked")[1] == "delivered_by=run-999 already"


# ── dry run ─────────────────────────────────────────────────────────────────


def test_dry_run_computes_actions_without_writing(env):
    home, db, engine = env
    _add_run(db, "run-dry-failed", "failed", kickoff="kick/dry.md",
             created_at=T0)
    _add_run(db, "run-dry-pub", "published", kickoff="kick/dry.md",
             created_at=T0 + 100)
    _add_epic(db, "epic-dry", last_run_id="run-dry-failed")
    _add_attempt(db, "epic-dry", 1, "run-dry-failed")
    _add_attempt(db, "epic-dry", 2, "run-dry-pub")

    dry = _sweep(home, db, engine, dry_run=True)
    dry_sup = [a for a in _supersede(dry) if a["run_id"] == "run-dry-failed"]
    dry_btl = [a for a in _backlink(dry) if a["epic_id"] == "epic-dry"]
    assert len(dry_sup) == 1 and len(dry_btl) == 1
    assert _run_row(db, "run-dry-failed") == ("failed", None)
    assert _epic_row(db, "epic-dry") == ("done", None)

    wet = _sweep(home, db, engine)
    wet_sup = [a for a in _supersede(wet) if a["run_id"] == "run-dry-failed"]
    wet_btl = [a for a in _backlink(wet) if a["epic_id"] == "epic-dry"]
    assert len(wet_sup) == 1 and len(wet_btl) == 1
    for key in ("run_id", "action", "reason"):
        assert dry_sup[0][key] == wet_sup[0][key]
    for key in ("epic_id", "action", "reason"):
        assert dry_btl[0][key] == wet_btl[0][key]
    assert _run_row(db, "run-dry-failed")[0] == "superseded"
    assert _epic_row(db, "epic-dry")[1].endswith("delivered_by=run-dry-pub")


# ── clause 3: the reaper treats superseded as terminal ──────────────────────


def test_reaper_leaves_superseded_and_not_needed_rows_unchanged(env):
    home, db, engine = env
    _add_run(db, "run-sup", "superseded")
    _add_run(db, "run-nn", "not_needed")
    _add_run(db, "run-live", "executing")
    dead = _dead_pid()
    for run_id in ("run-sup", "run-nn", "run-live"):
        run_dir = home / "runs" / run_id
        run_dir.mkdir(parents=True)
        (run_dir / ".pid").write_text(f"{dead}\n", encoding="utf-8")
    time.sleep(0.05)

    reaped = run_reaper.reap(home)
    assert [e["run_id"] for e in reaped] == ["run-live"]
    assert _run_row(db, "run-sup") == ("superseded", None)
    assert _run_row(db, "run-nn") == ("not_needed", None)
    assert _run_row(db, "run-live")[0] == "failed"


def test_finalize_passed_does_not_touch_superseded(env):
    _, db, _ = env
    _add_run(db, "run-sup-fp", "superseded")
    _add_run(db, "run-live-fp", "executing")
    assert run_reaper.finalize_passed(db, "run-sup-fp", "note") is False
    assert _run_row(db, "run-sup-fp") == ("superseded", None)
    assert run_reaper.finalize_passed(db, "run-live-fp", "note") is True
    assert _run_row(db, "run-live-fp")[0] == "published"
