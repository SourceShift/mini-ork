"""``mini-ork board steer`` answers with a receipt the IDE can show.

Kickoff ``rp-e11b-steer-receipt``: the ``steer`` success path prints, as its
LAST stdout line, ``Sent to <node> at HH:MM — the agent reads it at its next
turn`` (or the queued wording when no node is running), and ``--json`` carries
the same string under a ``receipt`` key. Errors keep today's exit codes.

Each test builds a hermetic home (real schema via the repo's migration runner,
synthetic ``task_runs`` / ``run_events`` rows) and drives ``board_cmd.main`` —
the same surface ``bin/mini-ork board steer`` shells out to.
"""
from __future__ import annotations

import io
import json
import os
import re
import sqlite3
import sys
import time
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

import pytest

from mini_ork.cli import board_cmd
from mini_ork.stores import migrate as mig

REPO = Path(__file__).resolve().parents[2]


def _defuse_double_listed_path_crash() -> None:
    """Keep pytest alive when this file is passed to it twice.

    The kickoff's verification command names this file explicitly *and* globs
    ``tests/unit/test_board*.py``, which the shell expands to this same path
    again. pytest collects a path once per argv entry (no dedupe), and
    pytest-asyncio 0.23.4's ``pytest_collectstart`` re-patches a collector it
    has already patched: the second pass stores the *patched* wrapper as
    ``__original_collect`` and then calls it → ``RecursionError`` at
    collection (repro: ``pytest <f>.py <f>.py`` for any ``<f>``, even an empty
    file; the same command with ``-p no:asyncio`` collects fine).

    This module therefore disables that hook's per-collector event-loop
    fixture injection *only* when it can see its own path double-listed. The
    repo has no ``pytest.mark.asyncio`` items, so the injection is inert here,
    and in every other run (CI, the blind spec suite, a single-file run) this
    function returns before touching anything. Delete it once the kickoff
    command lists this file once, or the plugin is upgraded.
    """
    mine = os.path.basename(__file__)
    listed = [os.path.basename(a) for a in sys.argv if a.endswith(".py")]
    if listed.count(mine) < 2:
        return
    try:
        import pytest_asyncio.plugin as _plugin

        _plugin._fixture_scope_by_collector_type.clear()
    except Exception:  # noqa: BLE001 — plugin absent / private API moved: nothing to defuse
        pass


_defuse_double_listed_path_crash()

SENT_RE = re.compile(
    r"^Sent to (?P<node>.+?) at (?P<clock>\d{2}:\d{2}) — the agent reads it at its next turn$"
)
QUEUED_RE = re.compile(
    r"^Queued for the next node at (?P<clock>\d{2}:\d{2}) — no node is running now$"
)


@pytest.fixture
def home(tmp_path: Path) -> Path:
    h = tmp_path / "proj" / ".mini-ork"
    h.mkdir(parents=True)
    rc, out, err = mig.init_db(db=str(h / "state.db"), root=str(REPO))
    assert rc == 0, err or out
    return h


def _add_run(home: Path, run_id: str, status: str) -> None:
    now = int(time.time())
    con = sqlite3.connect(home / "state.db")
    try:
        con.execute(
            "INSERT INTO task_runs (id, recipe, status, cost_usd, created_at,"
            " updated_at, task_class, kickoff_path, workflow_version)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (run_id, "code-fix", status, 0.1, now - 600, now, "code_fix", "", "latest"),
        )
        con.commit()
    finally:
        con.close()
    (home / "runs" / run_id).mkdir(parents=True, exist_ok=True)


def _add_node_event(home: Path, run_id: str, event_id: str, kind: str,
                    node_id: str, ts: int, extra: dict | None = None) -> None:
    payload: dict = {"node_id": node_id, "node_type": node_id}
    if extra:
        payload.update(extra)
    con = sqlite3.connect(home / "state.db")
    try:
        con.execute(
            "INSERT INTO run_events (event_id, run_id, event_type, payload_json,"
            " created_at) VALUES (?, ?, ?, ?, ?)",
            (event_id, run_id, kind, json.dumps(payload), ts),
        )
        con.commit()
    finally:
        con.close()


def _steering_rows(home: Path, run_id: str) -> list[tuple]:
    con = sqlite3.connect(home / "state.db")
    try:
        return con.execute(
            "SELECT message, role_target, severity FROM operator_steering WHERE run_id = ?",
            (run_id,),
        ).fetchall()
    finally:
        con.close()


def _run_main(*argv: str) -> tuple[int, str, str]:
    """Drive ``board_cmd.main`` and capture its stdout/stderr."""
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        rc = board_cmd.main(list(argv), "")
    return rc, out.getvalue(), err.getvalue()


def _last_line(stdout: str) -> str:
    lines = [ln for ln in stdout.splitlines() if ln.strip()]
    return lines[-1] if lines else ""


def _assert_local_clock(clock: str) -> None:
    lt = time.localtime()
    hh, mm = (int(p) for p in clock.split(":"))
    got = (hh * 60 + mm) % 1440
    now = lt.tm_hour * 60 + lt.tm_min
    assert min((got - now) % 1440, (now - got) % 1440) <= 2, (
        f"receipt clock {clock!r} is not local time (now {lt.tm_hour:02d}:{lt.tm_min:02d})"
    )


def test_steer_running_run_prints_receipt_with_node(home: Path) -> None:
    """A live run with an open node_start prints the receipt, naming that node."""
    run_id = "run-live-1"
    _add_run(home, run_id, status="executing")
    now = int(time.time())
    _add_node_event(home, run_id, "e1", "node_start", "planner", now - 120)
    _add_node_event(home, run_id, "e2", "node_end", "planner", now - 60,
                    {"finish_reason": "done"})
    _add_node_event(home, run_id, "e3", "node_start", "implementer", now - 30)

    rc, out, err = _run_main("steer", run_id, "--text", "focus the verifier",
                             "--home", str(home))

    assert rc == 0, f"steer failed rc={rc} err={err!r}"
    line = _last_line(out)
    m = SENT_RE.match(line)
    assert m, f"last stdout line is not the Sent receipt: {line!r} (stdout {out!r})"
    assert m.group("node") == "implementer", f"named {m.group('node')!r}, not implementer"
    _assert_local_clock(m.group("clock"))
    assert _steering_rows(home, run_id), "receipt claims Sent but no row was written"


def test_steer_no_running_node_prints_queued_wording(home: Path) -> None:
    """A live run between nodes (planner started and ended) prints the queued line."""
    run_id = "run-between-1"
    _add_run(home, run_id, status="executing")
    now = int(time.time())
    _add_node_event(home, run_id, "e1", "node_start", "planner", now - 120)
    _add_node_event(home, run_id, "e2", "node_end", "planner", now - 60,
                    {"finish_reason": "done"})

    rc, out, err = _run_main("steer", run_id, "--text", "hold before publish",
                             "--home", str(home))

    assert rc == 0, f"steer failed rc={rc} err={err!r}"
    line = _last_line(out)
    m = QUEUED_RE.match(line)
    assert m, f"last stdout line is not the queued wording: {line!r} (stdout {out!r})"
    _assert_local_clock(m.group("clock"))
    assert not SENT_RE.match(line), "queued case must not print the Sent wording"
    assert _steering_rows(home, run_id), "queued note was not written"


def test_steer_finished_run_is_refused_with_nonzero_exit(home: Path) -> None:
    """A published run is terminal: refused, nonzero exit, nothing written."""
    run_id = "run-done-1"
    _add_run(home, run_id, status="published")

    rc, out, err = _run_main("steer", run_id, "--text", "too late", "--home", str(home))

    assert rc == 1, f"steering a finished run must exit 1, got {rc} (stdout {out!r})"
    assert "Sent to" not in out and "Queued for the next node" not in out
    payload = json.loads(out.strip())
    assert payload["ok"] is False and "terminal" in payload["error"]
    assert _steering_rows(home, run_id) == [], "a refused steer must not write a row"


def test_steer_json_adds_receipt_key_and_prints_nothing_else(home: Path) -> None:
    """``--json``: one JSON document carrying ``receipt``, and no extra line."""
    run_id = "run-json-1"
    _add_run(home, run_id, status="executing")
    now = int(time.time())
    _add_node_event(home, run_id, "e1", "node_start", "reviewer", now - 5)

    rc, out, err = _run_main("steer", run_id, "--text", "json please",
                             "--home", str(home), "--json")

    assert rc == 0, f"steer failed rc={rc} err={err!r}"
    payload = json.loads(out.strip())  # raises if anything else is printed
    assert payload["ok"] is True
    m = SENT_RE.match(payload["receipt"])
    assert m, f"payload['receipt'] is not the Sent line: {payload['receipt']!r}"
    assert m.group("node") == "reviewer"


def test_steer_missing_text_keeps_usage_error(home: Path) -> None:
    """Errors unchanged: a steer without --text stays a usage error (exit 2)."""
    run_id = "run-usage-1"
    _add_run(home, run_id, status="executing")

    rc, out, err = _run_main("steer", run_id, "--home", str(home))

    assert rc == 2, f"missing --text must keep exit 2, got {rc}"
    assert "mini-ork board steer: --text is required" in err
    assert _steering_rows(home, run_id) == []
