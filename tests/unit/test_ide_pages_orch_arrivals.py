"""The intake tab's ledger and its per-source sync (FR-032).

Two things the intake tab could not do before: say *which run* an arrival
started, and poll one source without polling them all. Both are rendered from a
home that may predate the column, so the tests below build real ledgers — one
with the column, one without — and read the page the way the renderer does.

``intake once --source`` is the same verb the tab's own "Poll now" runs, narrowed
to one adapter; the seam test at the end feeds the emitted argv back through the
CLI rather than trusting the shape.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from mini_ork.ide_pages import orch
from mini_ork.stores import migrate as mig
from mini_ork.threads import store

REPO = Path(__file__).resolve().parents[2]


@pytest.fixture()
def home(tmp_path: Path) -> Path:
    h = tmp_path / "proj" / ".mini-ork"
    h.mkdir(parents=True)
    rc, _out, err = mig.init_db(db=str(h / "state.db"), root=str(REPO))
    assert rc == 0, err
    return h


def _record(home: Path, delivery_id: str, *, action: str = "launched",
            issue_uid: str = "github:o/r#1", thread_id: str = "th-1",
            run_id: str | None = None) -> None:
    store.record_intake_event("github", delivery_id, f"hash-{delivery_id}",
                              issue_uid=issue_uid, thread_id=thread_id, action=action,
                              run_id=run_id, db=str(home / "state.db"))


def _sections(home: Path) -> list[dict]:
    return orch.build(home, "intake", {})["sections"]


def _titled(home: Path, title: str) -> dict:
    return next(s for s in _sections(home) if s.get("title") == title)


def _rows(home: Path, title: str) -> list[dict]:
    return _titled(home, title)["rows"]


# ── the arrivals ledger ────────────────────────────────────────────────────

def test_an_arrival_that_started_a_run_links_to_it(home: Path) -> None:
    _record(home, "d1", run_id="run-abc")
    row = _rows(home, "Recent arrivals")[0]
    assert row["cells"][0]["t"] == "github"
    assert row["cells"][4]["t"] == "run-abc"
    assert row["acts"][0]["label"] == "Open run"
    assert row["acts"][0]["do"]["run"] == "run-abc"


def test_an_arrival_that_started_no_run_says_so_and_offers_nothing(home: Path) -> None:
    """An em dash, not a blank cell and not a button that opens nothing.

    A replay that folded into a live run started no run of its own, and neither
    did any row written before the column existed. The two are the same fact, so
    both render the same way — the page is a report of the ledger, and the ledger
    records nothing here.
    """
    _record(home, "d1", action="folded")
    row = _rows(home, "Recent arrivals")[0]
    assert row["cells"][4]["t"] == "—"
    assert row["acts"] == []


def test_the_run_column_is_the_one_the_header_promises(home: Path) -> None:
    """Six cells, six columns — the renderer pads from the head, not the row."""
    _record(home, "d1", run_id="run-abc")
    table = _titled(home, "Recent arrivals")
    assert table["head"] == ["source", "action", "issue", "thread", "run", "when"]
    assert len(table["rows"][0]["cells"]) == len(table["head"])


def test_a_ledger_written_before_the_column_existed_still_renders(home: Path) -> None:
    """The page can be the first thing to open a home an older release wrote.

    Without the probe this is an ``OperationalError`` swallowed by ``_rows`` and
    an empty table — the ledger silently loses every row it has. With it, the
    rows draw and every run reads as the em dash those rows in fact recorded.
    """
    con = sqlite3.connect(home / "state.db")
    con.execute("ALTER TABLE intake_events DROP COLUMN run_id")
    con.execute("INSERT INTO intake_events (provider, delivery_id, payload_hash, "
                "issue_uid, thread_id, action, created_at) VALUES (?,?,?,?,?,?,?)",
                ("github", "legacy", "hash-legacy", "github:o/r#9", "th-old", "launched",
                 "2026-01-01T00:00:00.000Z"))
    con.commit()
    con.close()

    sections = _sections(home)
    # No error card: the page rendered the ledger it has.
    assert not any("Recent arrivals" in str(s.get("title", "")) and "error" in str(s)
                   for s in sections if isinstance(s, dict))
    row = _rows(home, "Recent arrivals")[0]
    assert row["cells"][4]["t"] == "—"
    assert row["acts"] == []


def test_the_newest_arrival_is_first_and_the_ledger_is_capped(home: Path) -> None:
    for index in range(30):
        _record(home, f"d{index:02d}", issue_uid=f"github:o/r#{index}")
    rows = _rows(home, "Recent arrivals")
    assert len(rows) == 25
    assert rows[0]["cells"][2]["t"] == "github:o/r#29"


# ── the per-source sync ────────────────────────────────────────────────────

def _registered(home: Path) -> list[str]:
    from mini_ork.intake.poll import source_statuses
    from mini_ork.web.db import resolve_db

    return [s.source for s in source_statuses(resolve_db(home)) if s.registered]


def test_the_sync_button_polls_exactly_one_source(home: Path) -> None:
    from mini_ork.intake.poll import source_statuses
    from mini_ork.web.db import resolve_db

    statuses = source_statuses(resolve_db(home))
    rows = {row["cells"][0]["t"]: row for row in _rows(home, "Sources")}
    for status in statuses:
        acts = rows[status.source]["acts"]
        if not status.registered:
            # No adapter to call, so no button to press — not a button that
            # fails on stderr the moment it is clicked.
            assert acts == [], status.source
            continue
        assert [a["label"] for a in acts] == ["Sync"], status.source
        assert acts[0]["do"]["cli"] == ["intake", "once", "--source", status.source]
        assert status.source in acts[0]["do"]["confirm"]
        assert "starts a run" in acts[0]["do"]["confirm"]


def test_at_least_one_source_is_registered_so_that_test_is_not_vacuous(home: Path) -> None:
    assert _registered(home)


# ── the seam ───────────────────────────────────────────────────────────────

def test_the_emitted_sync_argv_is_one_the_cli_accepts(home: Path, capsys) -> None:
    """The page's button and the verb must agree — so run the button.

    ``--dry-run`` is appended rather than running the real poll: it proves the
    argv parses, the ``--source`` reaches the right subparser, and the pass
    selects without starting anything. The page must not poll a source on a
    click it cannot honour.
    """
    from mini_ork.cli import intake_cmd

    source = _registered(home)[0]
    argv = next(row for row in _rows(home, "Sources")
                if row["cells"][0]["t"] == source)["acts"][0]["do"]["cli"]
    assert argv == ["intake", "once", "--source", source]

    rc = intake_cmd.main([*argv[1:], "--dry-run", "--home", str(home)])
    assert rc == 0
    assert "nothing to start" in capsys.readouterr().out

    con = sqlite3.connect(home / "state.db")
    started = con.execute("SELECT COUNT(*) FROM task_runs").fetchone()[0]
    con.close()
    assert started == 0
