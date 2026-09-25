#!/usr/bin/env python3
"""PASS predicate for the mini-ork-self goal loop.

Contract (recipes/goal-loop/verifiers/goal_check.py and
recipes/goal-loop/lib/transforms.py::_await_terminal): invoked as
``python3 unit_predicate.py <unit_id>`` — argv, never a shell string — inside
MO_GOAL_TARGET_CWD. Exit 0 == PASS, 1 == fail, 2 == the predicate could not
decide (a non-zero non-1 code, so it can never be mistaken for a pass). The
FIRST stdout line is the reason.

WHY EVERY CHECK IS TWO PROBES. Each unit below has an obvious way to make its
check go green without fixing anything: neuter the migrate guard, delete the
migration that drops the columns, or widen the CHECK until the probe insert
succeeds for an unrelated reason. So each check runs a probe that MUST go green
and a second that MUST STAY RED. A child that takes the shortcut fails the
second probe, and the unit stays open. The instruments themselves — this file
and its lister — are frozen for the loop via MO_GOAL_PROTECTED_PATHS, so the
bar cannot be lowered from inside the tree being fixed.

The migrate guard is exercised through a subprocess against
``mini_ork.stores.migrate.migrate_apply`` because the guard's BEHAVIOUR is the
thing under test; the anti-gaming probes are what keep that from being a
self-report. Everything else is read straight out of sqlite.
"""
from __future__ import annotations

import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

# The migration whose rebuild drops the two live columns. Named here rather than
# globbed so the probe cannot be satisfied by renaming the file.
_M54 = "0054_execution_traces_status_blocked.sql"
# The migration whose comment-only reword started the drift. Any applied file
# would do; this one is the historical cause and is guaranteed applied.
_DRIFTED = "0038_gradient_records.sql"
_KEEP_COLUMNS = ("route_margin", "predicted_error")


def _repo_root() -> Path:
    """The tree under test. MO_GOAL_TARGET_CWD is the authoritative answer — the
    predicate scores the worktree the fix child edits, not the engine checkout
    that happens to be running this file."""
    cwd = os.environ.get("MO_GOAL_TARGET_CWD", "").strip()
    if cwd and os.path.isdir(os.path.join(cwd, "db", "migrations")):
        return Path(cwd)
    return Path(__file__).resolve().parents[4]


def _migrate(repo: Path, db: str, migrations_dir: Path, *,
             dry_run: bool, allow_drift: bool = False) -> tuple[int, str]:
    """Run the real guard over `migrations_dir` against `db`.

    allow_drift is forced explicitly per call rather than inherited: U1 probes
    drift DETECTION, so a stray MO_MIGRATE_ALLOW_DRIFT=1 in the environment would
    silently turn its "must stay red" probe green."""
    snippet = (
        "import sys\n"
        "from mini_ork.stores.migrate import migrate_apply\n"
        "err = []\n"
        "rc, _ = migrate_apply(%r, %r, %r, %r, err)\n"
        "sys.stdout.write('\\n'.join(err))\n"
        "sys.exit(rc)\n"
    ) % (str(migrations_dir), bool(dry_run), db, str(repo))
    env = {**os.environ, "PYTHONPATH": str(repo),
           "MO_MIGRATE_ALLOW_DRIFT": "1" if allow_drift else "0"}
    proc = subprocess.run([sys.executable, "-c", snippet], cwd=str(repo),
                          capture_output=True, text=True, env=env)
    return proc.returncode, (proc.stdout + proc.stderr).strip()


def _scratch_migrations(repo: Path, mutate: str):
    """A copy of the whole `db/` subtree, with one migration appended to.

    The whole subtree, not just `*.sql`: a fix may record what each migration
    MEANS somewhere under `db/` (a manifest, a sidecar), and a probe that
    copied only the .sql files would fail that fix for its file layout rather
    than its behaviour. Returns (handle, migrations_dir); the caller owns the
    handle and must clean it up.
    """
    td = tempfile.TemporaryDirectory(prefix="mo-self-mig-")
    dst = Path(td.name) / "db"
    shutil.copytree(repo / "db", dst)
    target = dst / "migrations" / _DRIFTED
    target.write_text(target.read_text(encoding="utf-8") + mutate, encoding="utf-8")
    return td, dst / "migrations"


def check_migrate_drift_comment_only(repo: Path, db: str) -> tuple[int, str]:
    """U1 — `mini-ork update` must survive a comment-only edit to an applied
    migration, must still refuse a real one, and must come up clean on the live
    db once the pending migrations have run.

    Root cause, measured 2026-09-25: commit d6bc90a3 reworded a comment in
    0038_gradient_records.sql. That bumped the file's sha256, so every db that
    had applied the pre-edit version now reads as `checksum drift`,
    migrate_apply returns 1, and `mini-ork update` dies on the FIRST drifted file
    — which is why 0054/0055/0056/0058 have sat unapplied ever since. The guard
    exists to catch a SEMANTIC edit to a shipped migration; a comment reword is
    not one, and a guard that cannot tell them apart is a guard people turn off
    wholesale (MO_MIGRATE_ALLOW_DRIFT=1), which loses the signal entirely.

    Three probes, all read-only against the live db:
      B  a comment-only difference must be ACCEPTED
      A  a real SQL difference must still be REFUSED (so the guard cannot be
         neutered into passing B)
      E  an upgrade-path run that has applied the pending migrations must be
         drift-clean WITHOUT MO_MIGRATE_ALLOW_DRIFT, i.e. the fix carries the
         one-time repair for the already-drifted row rather than relying on the
         escape hatch forever
    """
    mig = repo / "db" / "migrations"

    tmpdb = tempfile.mktemp(suffix=".db")
    try:
        rc, msg = _migrate(repo, tmpdb, mig, dry_run=False)
        if rc != 0:
            return 2, f"scratch apply failed, cannot evaluate: {msg[-200:]}"

        td, sdir = _scratch_migrations(repo, "\n-- drift probe: comment only\n")
        try:
            rc, msg = _migrate(repo, tmpdb, sdir, dry_run=True)
        finally:
            td.cleanup()
        if rc != 0:
            return 1, ("a comment-only difference is still read as drift, so "
                       "`mini-ork update` stays broken on every db that applied "
                       "the pre-edit 0038")

        td, sdir = _scratch_migrations(
            repo, "\nCREATE TABLE IF NOT EXISTS __drift_probe(x INTEGER);\n")
        try:
            rc, _ = _migrate(repo, tmpdb, sdir, dry_run=True)
        finally:
            td.cleanup()
        if rc == 0:
            return 1, ("a real SQL edit to an applied migration is no longer "
                       "detected — the guard was neutered, not fixed")

        if not os.path.isfile(db):
            return 2, f"live db not found: {db}"
        updb = tempfile.mktemp(suffix=".db")
        try:
            shutil.copy(db, updb)
            rc, msg = _migrate(repo, updb, mig, dry_run=False, allow_drift=True)
            if rc != 0:
                return 2, f"upgrade path could not apply: {msg[-200:]}"
            rc, msg = _migrate(repo, updb, mig, dry_run=True)
            if rc != 0:
                tail = msg.splitlines()[0] if msg else f"rc={rc}"
                return 1, ("still needs MO_MIGRATE_ALLOW_DRIFT after the pending "
                           f"migrations ran — the drifted row was not repaired: {tail}")
        finally:
            try:
                os.unlink(updb)
            except OSError:
                pass
        return 0, ("comment-only differences accepted; real edits still refused; "
                   "upgrade path drift-clean without the escape hatch")
    finally:
        try:
            os.unlink(tmpdb)
        except OSError:
            pass


def check_migration_0054_keeps_columns(repo: Path, db: str) -> tuple[int, str]:
    """U2 — applying the pending migrations must not destroy live data.

    Measured on a scratch copy of the live db 2026-09-25: 0054 rebuilds
    execution_traces via the create-copy-drop-rename dance, but its column list
    omits exactly two live columns — route_margin and predicted_error — and adds
    nothing. On the live db both already exist (0057/0059 were applied by hand
    precisely because update was blocked), so 0054 is destructive ON THE UPGRADE
    PATH: it would take route_margin with it, and route_margin is the entire
    training set the UCCI calibration map fits from.

    A fresh-DB run does NOT show this — 0054 runs before 0057/0059 there, so the
    columns are re-added afterwards. That is why the probe copies the LIVE db and
    re-creates the pending condition instead of building a schema from scratch.

    TWO probes, because the copy-the-live-db one alone is satisfiable by a
    rebuild that names the two columns unconditionally — which then dies on a
    FRESH install, where 0054 runs before anything has created them. So the
    fresh path is probed too, and the fix has to hold on both:

      L  on the live path, the two columns and every row survive, status
         'blocked' is accepted, and nothing is silently re-created
      F  a fresh db still installs: every migration applies clean, from empty
    """
    mig = repo / "db" / "migrations"
    if not os.path.isfile(db):
        return 2, f"live db not found: {db}"

    freshdb = tempfile.mktemp(suffix=".db")
    try:
        rc, msg = _migrate(repo, freshdb, mig, dry_run=False, allow_drift=True)
        if rc != 0:
            tail = msg.splitlines()[0] if msg else f"rc={rc}"
            return 1, (f"a fresh install no longer applies cleanly — the fix "
                       f"only holds where the columns already exist: {tail}")
    finally:
        try:
            os.unlink(freshdb)
        except OSError:
            pass

    tmpdb = tempfile.mktemp(suffix=".db")
    try:
        shutil.copy(db, tmpdb)
        con = sqlite3.connect(tmpdb)
        try:
            before_rows = con.execute("SELECT COUNT(*) FROM execution_traces").fetchone()[0]
            applied = con.execute(
                "SELECT COUNT(*) FROM schema_migrations WHERE filename=?",
                (_M54,)).fetchone()[0]
            if applied:
                con.execute("DELETE FROM schema_migrations WHERE filename=?", (_M54,))
                con.commit()
            have_before = {r[1] for r in con.execute("PRAGMA table_info(execution_traces)")}
        finally:
            con.close()

        if "route_margin" not in have_before and "predicted_error" not in have_before:
            return 2, ("neither watched column exists before the apply — the "
                       "probe cannot tell preservation from re-creation")

        rc, msg = _migrate(repo, tmpdb, mig, dry_run=False, allow_drift=True)
        if rc != 0:
            return 2, f"could not apply the pending migrations: {msg[-200:]}"

        con = sqlite3.connect(tmpdb)
        try:
            have_after = {r[1] for r in con.execute("PRAGMA table_info(execution_traces)")}
            after_rows = con.execute("SELECT COUNT(*) FROM execution_traces").fetchone()[0]
            blocked_ok, blocked_err = True, ""
            try:
                con.execute(
                    "INSERT INTO execution_traces (trace_id, task_class, status) "
                    "VALUES ('__mo_self_probe', 'probe', 'blocked')")
                con.rollback()
            except sqlite3.Error as exc:
                blocked_ok, blocked_err = False, str(exc)
        finally:
            con.close()

        dropped = [c for c in _KEEP_COLUMNS if c in have_before and c not in have_after]
        if dropped:
            return 1, (f"0054 still drops {', '.join(dropped)} on the upgrade path "
                       f"({before_rows} rows at risk)")
        if after_rows != before_rows:
            return 1, f"row count changed {before_rows} -> {after_rows} across the apply"
        if not blocked_ok:
            return 1, ("status='blocked' is still rejected by the CHECK "
                       f"constraint, so 0054's own purpose is unmet: {blocked_err}")
        return 0, (f"upgrade path keeps route_margin + predicted_error across "
                   f"{before_rows} rows and accepts status='blocked'")
    finally:
        try:
            os.unlink(tmpdb)
        except OSError:
            pass


_CHECKS = {
    "migrate-drift-comment-only": check_migrate_drift_comment_only,
    "migration-0054-drops-columns": check_migration_0054_keeps_columns,
}


def main(argv: list[str]) -> int:
    if not argv:
        print("usage: unit_predicate.py <unit_id>", file=sys.stderr)
        return 2
    unit = argv[0].strip()
    check = _CHECKS.get(unit)
    if check is None:
        print(f"unknown unit: {unit!r}", file=sys.stderr)
        return 2

    db = os.environ.get("MINI_ORK_DB", "").strip()
    if not db:
        print("MINI_ORK_DB unset — cannot evaluate", file=sys.stderr)
        return 2

    repo = _repo_root()
    try:
        rc, reason = check(repo, db)
    except Exception as exc:  # a broken probe is never a pass
        print(f"{unit} predicate error (not pass): {type(exc).__name__}: {exc}")
        return 2
    print(f"{unit} {'PASS' if rc == 0 else 'FAIL'}: {reason}")
    return rc


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
