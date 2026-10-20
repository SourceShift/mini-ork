"""RBF-27: tests never resolve or write the operator's live state.db.

Three isolation holes this pins shut:

1. ``node_events._resolve_db`` used ``$(pwd)/.mini-ork/state.db`` as its last
   fallback, so a pytest run started from the operator's checkout emitted into
   the LIVE DB (~1,900 ``remote.*`` events for 90 orphan run_ids).
2. ``web.control.launch_run`` set ``MINI_ORK_HOME`` but not ``MINI_ORK_DB``,
   and every resolver prefers the latter — so the child could still fall
   through to its cwd.
3. The suite-wide conftest pops ``MINI_ORK_DB``/``MINI_ORK_HOME`` for
   isolation, which is what made (1) reachable.

Hermetic: every database is a tmp_path file seeded below, and the spawn case
monkeypatches ``subprocess.Popen`` so no child is ever started.
"""
from __future__ import annotations

import os
import sqlite3
import subprocess
from pathlib import Path

from mini_ork.observability import node_events
from mini_ork.web import control

_DB_ENV = ("MINI_ORK_DB", "MINI_ORK_HOME", "MINI_ORK_PROJECT_HOME")


def _seed_db(root: Path) -> Path:
    """Create ``<root>/.mini-ork/state.db`` with the minimal ``run_events``
    table so a leaked insert is observable as a row count > 0."""
    dot = root / ".mini-ork"
    dot.mkdir(parents=True, exist_ok=True)
    db = dot / "state.db"
    con = sqlite3.connect(db)
    try:
        con.execute(
            "CREATE TABLE run_events ("
            "event_id TEXT PRIMARY KEY, run_id TEXT, event_type TEXT, "
            "payload_json TEXT, created_at INTEGER)"
        )
        con.commit()
    finally:
        con.close()
    return db


def _row_count(db: Path) -> int:
    con = sqlite3.connect(db)
    try:
        return int(con.execute("SELECT count(*) FROM run_events").fetchone()[0])
    finally:
        con.close()


def _scrub_db_env(monkeypatch) -> None:
    for var in _DB_ENV:
        monkeypatch.delenv(var, raising=False)


def test_no_cwd_db_flag_resolves_none_and_emit_is_noop(tmp_path, monkeypatch):
    """With cwd = a tmp dir holding `.mini-ork/state.db`, no DB/home env, and
    ``MINI_ORK_NO_CWD_DB=1``: ``_resolve_db()`` is None and an emit writes
    nothing."""
    db = _seed_db(tmp_path)
    monkeypatch.chdir(tmp_path)
    _scrub_db_env(monkeypatch)
    monkeypatch.setenv("MINI_ORK_NO_CWD_DB", "1")

    assert node_events._resolve_db() is None

    rc = node_events.mo_node_emit(
        "rbf27-run", "rbf27-node", "researcher", "node_start"
    )
    assert rc == 0
    assert _row_count(db) == 0, "emit wrote through the cwd fallback despite the flag"


def test_cwd_fallback_still_used_without_flag(tmp_path, monkeypatch):
    """Regression: with the flag absent (or not exactly ``"1"``) the cwd
    fallback still resolves and still writes."""
    db = _seed_db(tmp_path)
    monkeypatch.chdir(tmp_path)
    _scrub_db_env(monkeypatch)
    expected = str(tmp_path.resolve() / ".mini-ork" / "state.db")

    monkeypatch.delenv("MINI_ORK_NO_CWD_DB", raising=False)
    assert node_events._resolve_db() == expected

    rc = node_events.mo_node_emit(
        "rbf27-run", "rbf27-node", "researcher", "node_start"
    )
    assert rc == 0
    assert _row_count(db) == 1, "the ungated cwd fallback must still emit"


def test_launch_run_pins_child_db_to_home(tmp_path, monkeypatch):
    """``launch_run`` sets ``MINI_ORK_DB == <home>/state.db`` so the child never
    falls back to its cwd."""
    home = tmp_path / "web-home"
    fake_root = tmp_path / "root"
    (fake_root / "bin").mkdir(parents=True)
    (fake_root / "bin" / "mini-ork").write_text(
        "#!/usr/bin/env python3\n", encoding="utf-8"
    )
    monkeypatch.setenv("MINI_ORK_ROOT", str(fake_root))
    # Scrub first: the child env starts as dict(os.environ), so an inherited
    # value must not be able to satisfy the assertion below.
    monkeypatch.delenv("MINI_ORK_DB", raising=False)
    monkeypatch.delenv("MINI_ORK_HOME", raising=False)

    captured: dict[str, dict[str, str]] = {}

    class _FakePopen:
        def __init__(self, argv, **kwargs):
            captured["env"] = kwargs["env"]
            self.pid = 424242

    monkeypatch.setattr(subprocess, "Popen", _FakePopen)

    out = control.launch_run(
        home=home,
        recipe="code-fix",
        kickoff_markdown="# rbf-27 kickoff\n",
        run_id="rbf27-run",
    )
    assert out["ok"] is True, out

    env = captured["env"]
    assert env["MINI_ORK_DB"] == str(home / "state.db")
    assert env["MINI_ORK_HOME"] == str(home)


def test_conftest_sets_no_cwd_db_flag():
    """The suite-wide isolation fixture (or its import-time preamble) sets the
    flag, so every test inherits the no-cwd-DB policy."""
    assert os.environ["MINI_ORK_NO_CWD_DB"] == "1"
