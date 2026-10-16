"""The ``orchestrator.armed`` receipt: one run-level row proving the per-run
orchestrator was wired on *this* run's path.

``runner_orchestrator.enabled()`` reports that the layer is on globally. It
cannot report whether a given run's execution ever reached the
dependency-graph path that actually calls it — the other ``_parallel`` call
sites stay unwired. The run page needs that distinction (audit P1#9): an
enabled-but-unwired run must not render as "armed", because the word reads as
"the layer chose to stay quiet" rather than "the layer structurally could not
act here".

The receipt is written once, where the engine commits to the wired path. This
file pins the write and its fail-safe contract; the page-side predicate that
consumes it lives with the run page.
"""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from mini_ork.cli.execute import _emit_orchestrator_armed  # noqa: E402

INIT_SH = REPO / "db" / "init.sh"


@pytest.fixture
def home(tmp_path, monkeypatch):
    """A real mini-ork home with a seeded ``state.db`` (the `run_events` table
    the receipt lands in comes from db/init.sh, not a hand-rolled schema)."""
    h = tmp_path / "home"
    h.mkdir()
    dbp = h / "state.db"
    r = subprocess.run(
        ["bash", str(INIT_SH)],
        env={**os.environ, "MINI_ORK_HOME": str(h), "MINI_ORK_DB": str(dbp)},
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        pytest.skip(f"db/init.sh failed: rc={r.returncode}\n{r.stderr}")
    monkeypatch.setenv("MINI_ORK_HOME", str(h))
    monkeypatch.setenv("MINI_ORK_DB", str(dbp))
    return h


def _events(home: Path) -> list[dict]:
    con = sqlite3.connect(home / "state.db")
    con.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in con.execute(
            "SELECT run_id, event_type, payload_json FROM run_events "
            "ORDER BY created_at, rowid")]
    finally:
        con.close()


def test_the_receipt_lands_as_a_run_level_row(home):
    """The row names the run and carries no node — nothing about this event
    belongs to a node, which is exactly why ``mo_node_emit`` could not write it."""
    _emit_orchestrator_armed(str(home / "state.db"), "run-armed")

    rows = _events(home)
    assert len(rows) == 1, rows
    assert rows[0]["run_id"] == "run-armed"
    assert rows[0]["event_type"] == "orchestrator.armed"
    payload = json.loads(rows[0]["payload_json"])
    assert payload == {"reason": "wired on the dependency-graph path"}
    assert "node_id" not in payload


def test_a_missing_db_is_a_noop_and_not_an_error(home, capsys):
    """Fail-safe end to end: the caller must never have to guard this call, and
    observability must not create the database it could not find."""
    _emit_orchestrator_armed(str(home / "nope.db"), "run-armed")

    assert _events(home) == []
    assert not (home / "nope.db").exists()
    assert capsys.readouterr().err == ""
