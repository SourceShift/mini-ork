"""Suite-wide test isolation.

Many ported ``main()`` functions mirror the bash entrypoints by exporting
process env (``os.environ["MINI_ORK_HOME"]``, ``MINI_ORK_DB``, ``MINI_ORK_RUN_ID``,
``MINI_ORK_ROOT``, …). That is correct for the real CLI (process-scoped) but, when
a test calls ``main()`` in-process, the mutation persists into the shared
``os.environ`` and leaks into later tests. A downstream bash-parity test then
spawns bash with ``{**os.environ, ...}`` and inherits a stale (often deleted)
``MINI_ORK_HOME``/``MINI_ORK_DB`` from the earlier test — so bash and the port
diverge and the parity assertion fails. Each such test passes in isolation but
fails in the full suite (the CI-only, single-process failure mode).

This autouse fixture snapshots and restores ``os.environ`` and the working
directory around every test, isolating that leakage suite-wide. It also closes
and drops ``mini_ork.web.db``'s cached ``StateDB`` connections, which outlive a
test's ``tmp_path`` and would otherwise read a reused path's old database, and
restores ``mini_ork.features.store``'s memory of the defaults it applied, which
has the same out-of-test lifetime as the environment it wrote.
"""
from __future__ import annotations

import getpass
import os
import sys
import tempfile
from pathlib import Path

import pytest

from scratch_prune import prune_stale_basetemps

# Basetemps untouched this long belong to no live session (see scratch_prune).
_STALE_BASETEMP_AGE_S = 3 * 60 * 60


def pytest_sessionstart(session):
    # Controller only: xdist workers share the controller's basetemp root.
    if hasattr(session.config, "workerinput"):
        return
    try:
        root = Path(tempfile.gettempdir()) / f"pytest-of-{getpass.getuser()}"
        prune_stale_basetemps(root, max_age_s=_STALE_BASETEMP_AGE_S)
    except Exception:  # noqa: BLE001 — scratch hygiene must never fail a test session
        pass


@pytest.fixture(autouse=True)
def _isolate_process_state():
    env_snapshot = dict(os.environ)
    # Never let a test resolve the real credential store. secret_store_path()
    # prefers MINI_ORK_SECRETS over MINI_ORK_HOME, so a test that points
    # MINI_ORK_HOME at tmp_path and then writes secrets would overwrite the
    # caller's real file whenever the suite runs under a mini-ork run that
    # exported MINI_ORK_SECRETS (held-out tasks, framework-edit verifiers).
    os.environ.pop("MINI_ORK_SECRETS", None)
    # Same hazard for the state database. A run exports MINI_ORK_DB/MINI_ORK_HOME
    # for its nodes, so a verifier's pytest inherits the live home, and
    # mini_ork.web.db.resolve_db prefers MINI_ORK_DB over the --home a test
    # passes: on 2026-10-13 test_intake_poll, test_threads_cli and
    # test_web_intake_route wrote their acme fixtures (tickets, threads stuck
    # "running", intake events) into the operator's real state.db, and they
    # showed up on the Board. Unset both, which is the environment CI runs in;
    # a test that needs either sets it itself.
    os.environ.pop("MINI_ORK_DB", None)
    os.environ.pop("MINI_ORK_HOME", None)
    # Never let a test spawn a real, detached auto-repair `recover`: the run and
    # recover flows call auto_repair.maybe_repair on every failed run, and it is
    # on unless MO_AUTO_REPAIR is "0". Tests of the loop opt back in explicitly.
    os.environ["MO_AUTO_REPAIR"] = "0"
    # Same hazard for the per-run watcher: `mini-ork run` spawns it detached,
    # and an in-process lifecycle test would leave a real process behind for
    # every run it executes. Tests of the spawn opt back in explicitly.
    os.environ["MO_ORCH_WATCH"] = "0"
    # Every in-process reader of state.db goes through mini_ork.web.db.db_for,
    # which caches a StateDB (holding a read-only sqlite connection) in the
    # process-wide ``_dbs`` dict, keyed by the home path. pytest reuses a
    # tmp_path string across tests whose names share their first 30 chars
    # (``tmp_path_retention_policy = "failed"`` deletes a passing test's dir, so
    # the numbered slot is handed to the next same-prefix test). The reused
    # test then builds a fresh state.db at the same path, but db_for returns the
    # cached StateDB whose connection still points at the deleted inode (and
    # whose has_table cache is stale), so it reads the PREVIOUS test's rows and
    # fleet/run-page/retry-hint/board reads go wrong silently. Close and drop
    # the cache per test so a fresh connection observes the new file. Guarded
    # by sys.modules: never import the module just to reset it.
    db_mod = sys.modules.get("mini_ork.web.db")
    if db_mod is not None:
        for cached in list(db_mod._dbs.values()):
            close = getattr(cached, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:  # noqa: BLE001 — a reset must never fail a test
                    pass
        db_mod._dbs.clear()
    # ``features.store.apply_defaults`` writes ``os.environ`` and remembers the
    # names it wrote so a later read for a *different* home may replace them —
    # that memory is the only thing distinguishing our value from the operator's.
    # The environment is restored below; the memory is module state and must be
    # too, or one test's applied home would make the next test overwrite a
    # variable it monkeypatched itself. Guarded by sys.modules, as above.
    store_mod = sys.modules.get("mini_ork.features.store")
    applied_snapshot = set(store_mod._applied) if store_mod is not None else None
    try:
        cwd_snapshot = os.getcwd()
    except OSError:
        cwd_snapshot = None
    try:
        yield
    finally:
        if applied_snapshot is not None and store_mod is not None:
            store_mod._applied.clear()
            store_mod._applied.update(applied_snapshot)
        os.environ.clear()
        os.environ.update(env_snapshot)
        if cwd_snapshot is not None:
            try:
                os.chdir(cwd_snapshot)
            except OSError:
                pass
