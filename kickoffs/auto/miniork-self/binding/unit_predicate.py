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
migration that drops the columns, drop the reflect step instead of bounding it,
or switch reflection off. So each check runs a probe that MUST go green and a
second that MUST STAY RED. A child that takes the shortcut fails the second
probe, and the unit stays open. The instruments themselves — this file and its
lister — are frozen for the loop via MO_GOAL_PROTECTED_PATHS, so the bar cannot
be lowered from inside the tree being fixed.

TWO KINDS OF CHECK. `migrate-drift-comment-only` and
`migration-0054-drops-columns` exercise the migration guard through a
subprocess against ``mini_ork.stores.migrate.migrate_apply``, because the
guard's BEHAVIOUR is the thing under test; the anti-gaming probes are what keep
that from being a self-report. `migration-order-is-reported` does the same and
reads the guard's err channel, not its stdout, because the stdout channel
already names every pending file and would satisfy the probe for free. The two
code-shape units read the tree instead: `reflect-step-is-bounded` and
`module-child-engine-pin` are properties of how a child process is SPAWNED, and
the honest probe for a spawn is a live decoy — a second ``mini_ork/`` package
in the child's working directory that a pinned spawn must never import.
"""
from __future__ import annotations

import ast
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
# The module child that wedges a run when it hangs, and the module prefix every
# engine-pinned child is spawned with.
_REFLECT_MODULE = "mini_ork.cli.reflect"
_MODULE_PREFIX = "mini_ork.cli."
_SUBPROCESS_FUNCS = ("run", "Popen", "call", "check_call", "check_output")


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

    Returns (rc, guard's err channel). ``migrate_apply``'s stdout is DISCARDED
    here and only its ``err_out`` list is returned, which is load-bearing for
    ``migration-order-is-reported``: that unit asserts a warning appears on the
    err channel, and if stdout were folded in, the routine ``  [apply] <file>``
    line would satisfy it on an unfixed tree.

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
    migration, must still refuse a real one, must not silently absorb an edit
    INSIDE a string literal, and must come up clean on the live db once the
    pending migrations have run.

    Root cause, measured 2026-09-25: commit d6bc90a3 reworded a comment in
    0038_gradient_records.sql. That bumped the file's sha256, so every db that
    had applied the pre-edit version now reads as `checksum drift`,
    migrate_apply returns 1, and `mini-ork update` dies on the FIRST drifted file
    — which is why 0054/0055/0056/0058 have sat unapplied ever since. The guard
    exists to catch a SEMANTIC edit to a shipped migration; a comment reword is
    not one, and a guard that cannot tell them apart is a guard people turn off
    wholesale (MO_MIGRATE_ALLOW_DRIFT=1), which loses the signal entirely.

    Four probes, all read-only against the live db:
      B  a comment-only difference must be ACCEPTED
      A  a real SQL difference must still be REFUSED (so the guard cannot be
         neutered into passing B)
      W  a whitespace-only difference INSIDE a string literal must still be
         REFUSED — the canonical form is allowed to collapse whitespace that is
         SQL formatting, but a literal's bytes are DATA, and collapsing them
         makes `VALUES ('a  b')` and `VALUES ('a b')` checksum equal
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

        # W: same statement, one byte less inside the literal. Built through a
        # controlled probe migration rather than by rewriting an existing one,
        # because the drifted file above carries no literal worth rewriting.
        ws_probe = (
            "CREATE TABLE IF NOT EXISTS __ws_probe(v TEXT);\n"
            "INSERT INTO __ws_probe(v) VALUES ('a  b');\n"
        )
        td = tempfile.TemporaryDirectory(prefix="mo-self-mig-")
        try:
            dst = Path(td.name) / "db"
            shutil.copytree(repo / "db", dst)
            probe = dst / "migrations" / "0099_ws_literal_probe.sql"
            probe.write_text(ws_probe, encoding="utf-8")
            wsdb = tempfile.mktemp(suffix=".db")
            try:
                rc, msg = _migrate(repo, wsdb, dst / "migrations",
                                   dry_run=False, allow_drift=True)
                if rc != 0:
                    return 2, (f"whitespace probe could not be applied, cannot "
                               f"evaluate: {msg[-200:]}")
                probe.write_text(ws_probe.replace("'a  b'", "'a b'"),
                                 encoding="utf-8")
                rc, _ = _migrate(repo, wsdb, dst / "migrations", dry_run=True)
            finally:
                try:
                    os.unlink(wsdb)
                except OSError:
                    pass
        finally:
            td.cleanup()
        if rc == 0:
            return 1, ("a whitespace-only change INSIDE a string literal is not "
                       "read as drift — the canonical form collapses literal "
                       "contents, so a real data edit is silently re-baselined")

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
                   "literal contents still hashed; upgrade path drift-clean "
                   "without the escape hatch")
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


def check_migration_order_is_reported(repo: Path, db: str) -> tuple[int, str]:
    """U5 — the runner must SAY SO when it is about to apply a migration that
    sorts before one already applied.

    Measured on the live ledger 2026-09-25: 0054 carries an applied_at of
    13:08:19, LATER than 0057 (2026-09-19T05:10:20) and 0059 (2026-09-25T10:54:12).
    It was applied last, by hand, and nobody was told that a lower-numbered
    migration had just run after two higher ones. `migrate_apply` walks
    ``sorted(glob("*.sql"))`` and treats a file absent from schema_migrations as
    simply "pending", so an out-of-order apply is indistinguishable from a
    normal one until it has already rebuilt the table.

    This unit does NOT ask the runner to refuse — U2 already forces the apply to
    become non-destructive, and a refusal here would make U2's live-path probe
    unsatisfiable. It asks for the warning that was missing: the same run stays
    rc 0, and the err channel names the file whose position is out of order.

      O  with 0054 pending while 0057/0059 are applied, the apply still succeeds
         AND the err channel names 0054
      N  a fresh install, where the pending set is every file in sorted order,
         succeeds and names NOTHING — so the warning cannot be emitted
         unconditionally, and routing the routine ``  [apply]`` line onto the
         err channel to satisfy O fails here
    """
    mig = repo / "db" / "migrations"
    if not os.path.isfile(db):
        return 2, f"live db not found: {db}"

    # N — the control. Every file pending, in sorted order: no one is out of
    # place, so a warning here would be noise.
    ndb = tempfile.mktemp(suffix=".db")
    try:
        rc, msg = _migrate(repo, ndb, mig, dry_run=False, allow_drift=True)
        if rc != 0:
            return 2, (f"a fresh install no longer applies cleanly, cannot "
                       f"evaluate: {msg[-200:]}")
        if _M54 in msg:
            return 1, ("the runner reports an order problem on a fresh install, "
                       "where every migration applies in sorted order — the "
                       "warning is unconditional, so it carries no signal")
    finally:
        try:
            os.unlink(ndb)
        except OSError:
            pass

    # O — the real thing. 0054 pending while the later migrations stay applied.
    odb = tempfile.mktemp(suffix=".db")
    try:
        shutil.copy(db, odb)
        con = sqlite3.connect(odb)
        try:
            con.execute("DELETE FROM schema_migrations WHERE filename=?", (_M54,))
            con.commit()
        finally:
            con.close()
        rc, msg = _migrate(repo, odb, mig, dry_run=False)
        if rc != 0:
            return 2, (f"the out-of-order apply no longer succeeds, so this "
                       f"probe cannot separate 'warned' from 'refused': "
                       f"{msg.splitlines()[0] if msg else rc}")
        if _M54 not in msg:
            return 1, ("applying a migration that sorts BEFORE already-applied "
                       "ones (0054 after 0057/0059, as happened on the live db) "
                       "is reported nowhere — the drift stays silent until the "
                       "table is already rebuilt")
        return 0, ("an out-of-order apply succeeds and names the file it is "
                   "applying out of order")
    finally:
        try:
            os.unlink(odb)
        except OSError:
            pass


def _spawn_calls(tree: ast.AST) -> list[ast.Call]:
    """Every subprocess-style spawn call in a module."""
    found: list[ast.Call] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = getattr(func, "attr", None) or getattr(func, "id", None)
        if name in _SUBPROCESS_FUNCS:
            found.append(node)
    return found


def _string_constants(call: ast.Call) -> list[str]:
    """String literals anywhere in a call — enough to recognise the target
    module without depending on the argv being a literal list."""
    return [n.value for n in ast.walk(call)
            if isinstance(n, ast.Constant) and isinstance(n.value, str)]


def _reflect_spawns(repo: Path):
    """(relpath, lineno, call) for every spawn of the reflect CLI in the tree."""
    hits = []
    for rel in ("mini_ork/cli/main.py", "mini_ork/cli/execute_handlers.py"):
        p = repo / rel
        if not p.is_file():
            continue
        tree = ast.parse(p.read_text(encoding="utf-8"))
        for call in _spawn_calls(tree):
            if _REFLECT_MODULE in _string_constants(call):
                hits.append((rel, call.lineno, call))
    return hits


def _bound_is_real(kw: ast.keyword | None) -> tuple[bool, str]:
    """A timeout keyword bounds the wait only if it is a positive number.

    ``timeout=None`` is the unfixed behaviour spelled out, and a negative value
    is rejected outright. A named/computed bound is accepted: the unit is that a
    bound EXISTS and is readable at the call site, not that it is written as a
    literal."""
    if kw is None:
        return False, "no timeout="
    value = kw.value
    if isinstance(value, ast.Constant):
        if value.value is None:
            return False, "timeout=None"
        if isinstance(value.value, bool) or not isinstance(value.value, (int, float)):
            return False, f"timeout={value.value!r}"
        if value.value <= 0:
            return False, f"timeout={value.value!r}"
        return True, ""
    src = ast.unparse(value)
    if "None" in src or src.lstrip().startswith("-"):
        return False, f"timeout={src}"
    return True, ""


def _auto_reflect_is_on(tree: ast.AST) -> bool:
    """True when ``MO_AUTO_REFLECT`` is read with an ON default."""
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (isinstance(func, ast.Attribute) and func.attr == "get"):
            continue
        args = list(node.args)
        if not args:
            continue
        key = args[0]
        if not (isinstance(key, ast.Constant) and key.value == "MO_AUTO_REFLECT"):
            continue
        default = args[1] if len(args) > 1 else None
        if default is None:
            kw = next((k for k in node.keywords if k.arg == "default"), None)
            default = kw.value if kw else None
        if default is None:
            return False  # `get` returns None → falsy → reflect never runs
        if isinstance(default, ast.Constant):
            return default.value not in (None, "0", 0, False)
        return True
    return False


def check_reflect_step_is_bounded(repo: Path, db: str) -> tuple[int, str]:
    """U3 — the reflect step must be bounded, because nothing else ends it.

    Measured 2026-09-25: ``mini_ork/cli/main.py`` runs the reflect child with
    NO ``timeout=``. Reflect is a seconds-long, best-effort step that runs AFTER
    the verdict is already final, so a lane that never returns holds the whole
    `mini-ork run` open forever with nothing left to decide. The campaign
    launcher has carried a watchdog that kills reflect processes past six
    minutes purely to work around this, which is a fix living in a shell script
    instead of in the code that spawns the child. Same missing bound in
    ``execute_handlers._handle_reflector_early``.

    TWO probes:
      T  every spawn of ``mini_ork.cli.reflect`` in main.py and
         execute_handlers.py carries a positive ``timeout=``
      S  reflection is still ON by default — deleting the step, or flipping
         ``MO_AUTO_REFLECT``'s default to "0", bounds nothing and would satisfy
         T by removing the thing being bounded
    """
    hits = _reflect_spawns(repo)
    if not hits:
        return 1, (f"no spawn of {_REFLECT_MODULE} found in mini_ork/cli — the "
                   "reflect step was removed rather than bounded, and reflection "
                   "is a required lifecycle step")

    unbounded = []
    for rel, lineno, call in hits:
        kw = next((k for k in call.keywords if k.arg == "timeout"), None)
        ok, why = _bound_is_real(kw)
        if not ok:
            unbounded.append(f"{rel}:{lineno} ({why})")
    if unbounded:
        return 1, ("the reflect child is spawned without a bound at "
                   f"{', '.join(unbounded)} — a wedged reflect holds the run "
                   "open after its verdict is already final")

    main_py = repo / "mini_ork" / "cli" / "main.py"
    if main_py.is_file():
        tree = ast.parse(main_py.read_text(encoding="utf-8"))
        if not _auto_reflect_is_on(tree):
            return 1, ("MO_AUTO_REFLECT no longer defaults to on — reflection "
                       "was switched off rather than bounded, so the missing "
                       "timeout is hidden instead of fixed")
    return 0, (f"{len(hits)} reflect spawn(s) carry a positive timeout and "
               "reflection is still enabled by default")


def _function_sets_pin(node: ast.AST) -> bool:
    try:
        return "PYTHONSAFEPATH" in ast.unparse(node)
    except Exception:  # pragma: no cover - unparse is total on parsed trees
        return False


def _pin_helper_names(repo: Path) -> set[str]:
    """Names of functions defined anywhere under the tree that set the pin.

    The fix is expected to be 'one policy, applied everywhere', so a spawn that
    calls a shared env builder has to be recognised as pinned. Resolving the
    callee by scanning for a definition that mentions PYTHONSAFEPATH is what
    lets the helper be introduced rather than inlined."""
    names: set[str] = set()
    root = repo / "mini_ork"
    if not root.is_dir():
        return names
    for p in root.rglob("*.py"):
        try:
            src = p.read_text(encoding="utf-8")
        except OSError:
            continue
        if "PYTHONSAFEPATH" not in src:
            continue
        try:
            tree = ast.parse(src)
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and _function_sets_pin(node):
                names.add(node.name)
    return names


def _expr_is_pinned(expr: ast.AST | None, tree: ast.AST, helpers: set[str],
                    seen: set[str]) -> bool:
    """Whether an ``env=`` expression carries the engine pin."""
    if expr is None:
        return False
    if isinstance(expr, ast.Dict):
        for key, value in zip(expr.keys, expr.values):
            if key is None:
                # ``{**base}`` / ``{**base, "K": v}`` — the pin rides in on the
                # unpacked mapping. Reading only literal keys would reject the
                # very "one policy, applied everywhere" shape this probe's
                # docstring asks for: a shared env builder spread into a dict.
                if _expr_is_pinned(value, tree, helpers, seen):
                    return True
            elif isinstance(key, ast.Constant) and key.value == "PYTHONSAFEPATH":
                return True
        return False
    if isinstance(expr, ast.Call):
        func = expr.func
        name = getattr(func, "attr", None) or getattr(func, "id", None)
        return name in helpers
    if isinstance(expr, ast.Name) and expr.id not in seen:
        seen.add(expr.id)
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                targets, value = node.targets, node.value
            elif isinstance(node, ast.AnnAssign):
                targets, value = [node.target], node.value
            else:
                continue
            for t in targets:
                # env["PYTHONSAFEPATH"] = "1" — an item assignment on the name.
                if isinstance(t, ast.Subscript):
                    base = t.value
                    if (isinstance(base, ast.Name) and base.id == expr.id
                            and isinstance(t.slice, ast.Constant)
                            and t.slice.value == "PYTHONSAFEPATH"):
                        return True
                # env = <something pinned>
                if (isinstance(t, ast.Name) and t.id == expr.id
                        and value is not None
                        and _expr_is_pinned(value, tree, helpers, seen)):
                    return True
        return False
    return False


def _decoy_tree(base: Path) -> Path:
    """A second ``mini_ork`` package that reports whether it was imported.

    Written to the DECOY's own directory rather than printed, because the
    handler under test captures its child's output and throws it away."""
    root = base / "decoy"
    (root / "mini_ork" / "cli").mkdir(parents=True)
    (root / "mini_ork" / "__init__.py").write_text("", encoding="utf-8")
    (root / "mini_ork" / "cli" / "__init__.py").write_text("", encoding="utf-8")
    (root / "mini_ork" / "cli" / "reflect.py").write_text(
        "from pathlib import Path\n"
        "Path(__file__).with_name('DECOY_RAN').write_text('x', encoding='utf-8')\n"
        "raise SystemExit(0)\n",
        encoding="utf-8")
    return root


def _decoy_sentinel(root: Path) -> Path:
    return root / "mini_ork" / "cli" / "DECOY_RAN"


def _unpinned_reflect_loads_decoy(repo: Path, decoy: Path) -> bool:
    """The control: with the pin absent, the decoy MUST win.

    Built from this process's own environment minus PYTHONSAFEPATH, so the
    control proves the decoy is reachable at all. Without it, a
    'the decoy did not load' result could mean the decoy was never reachable
    and the unit would pass on a tree that fixed nothing."""
    env = {**os.environ, "PYTHONPATH": str(repo)}
    env.pop("PYTHONSAFEPATH", None)
    subprocess.run([sys.executable, "-m", _REFLECT_MODULE], cwd=str(decoy),
                   capture_output=True, text=True, env=env)
    return _decoy_sentinel(decoy).exists()


def _decoy_was_imported(repo: Path, decoy: Path, sentinel: Path) -> bool:
    """Run the real reflector handler with the cwd inside the decoy."""
    sys.path.insert(0, str(repo))
    import mini_ork.cli.execute_handlers as execute_handlers  # noqa: PLC0415

    os.environ.pop("PYTHONSAFEPATH", None)  # the pin must come from the CODE
    sentinel.unlink(missing_ok=True)
    prev = os.getcwd()
    os.chdir(str(decoy))
    try:
        execute_handlers._handle_reflector_early(str(repo))
    finally:
        os.chdir(prev)
    return sentinel.exists()


def check_module_child_engine_pin(repo: Path, db: str) -> tuple[int, str]:
    """U4 — every ``python -m mini_ork.cli.*`` child must import THIS engine.

    Measured 2026-09-25: for ``-m`` and ``-c`` the interpreter puts the working
    directory at ``sys.path[0]``, AHEAD of ``PYTHONPATH``. The goal loop spawns
    its children with the cwd set to the repository under repair, so a repo that
    contains a ``mini_ork/`` tree silently shadows the engine: the child runs the
    target's stale copy. On this tree seven spawns under ``mini_ork/cli/`` build
    their child env without the pin — ``main.py``'s shared ``_module_env`` (four
    call sites), its hand-built ``plan_env``, ``execute_handlers``' two, and
    ``validate``'s one. ``PYTHONSAFEPATH=1`` drops the implicit cwd entry so the
    declared ``PYTHONPATH`` is the one that resolves.

    TWO probes:
      C  the control — with the pin absent, a decoy ``mini_ork`` package in the
         child's cwd IS imported (so the unit can detect what it claims to)
      T  with the pin in place, the REAL reflector handler, run with its cwd
         inside that decoy, does not import it — behaviourally, at the call
         site, not by reading a diff
      V  and no spawn under ``mini_ork/cli/`` is left unpinned, so fixing the
         one path the behavioural probe exercises is not enough
    """
    cli_dir = repo / "mini_ork" / "cli"
    if not cli_dir.is_dir():
        return 2, f"no mini_ork/cli in the tree under test: {repo}"

    helpers = _pin_helper_names(repo)
    unpinned: list[str] = []
    total = 0
    for p in sorted(cli_dir.rglob("*.py")):
        try:
            tree = ast.parse(p.read_text(encoding="utf-8"))
        except (OSError, SyntaxError):
            continue
        for call in _spawn_calls(tree):
            if not any(s.startswith(_MODULE_PREFIX) for s in _string_constants(call)):
                continue
            total += 1
            env_kw = next((k for k in call.keywords if k.arg == "env"), None)
            if not _expr_is_pinned(env_kw.value if env_kw else None, tree, helpers, set()):
                unpinned.append(f"{p.relative_to(repo)}:{call.lineno}")
    if total == 0:
        return 2, "no `python -m mini_ork.cli.*` spawn found — cannot evaluate"

    with tempfile.TemporaryDirectory(prefix="mo-self-decoy-") as td:
        decoy = _decoy_tree(Path(td))
        if not _unpinned_reflect_loads_decoy(repo, decoy):
            return 2, ("the decoy package was not reachable even without the pin "
                       "— this probe cannot detect engine shadowing")
        _decoy_sentinel(decoy).unlink(missing_ok=True)
        if _decoy_was_imported(repo, decoy, _decoy_sentinel(decoy)):
            return 1, ("the reflect child imported a `mini_ork` package from its "
                       "working directory instead of the engine — a repo under "
                       "repair shadows the engine it is being repaired by")

    if unpinned:
        return 1, ("module children are spawned with an env that does not pin "
                   f"the engine at {', '.join(unpinned)}")
    return 0, (f"all {total} `python -m mini_ork.cli.*` spawns pin the engine, "
               "and a decoy package in the child's cwd is not imported")


_CHECKS = {
    "migrate-drift-comment-only": check_migrate_drift_comment_only,
    "migration-0054-drops-columns": check_migration_0054_keeps_columns,
    "migration-order-is-reported": check_migration_order_is_reported,
    "reflect-step-is-bounded": check_reflect_step_is_bounded,
    "module-child-engine-pin": check_module_child_engine_pin,
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
