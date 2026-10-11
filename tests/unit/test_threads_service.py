"""Tests for ``mini_ork.threads.service`` — the thread state machine.

``launch_run`` and ``steer_run`` are monkeypatched (the house pattern from
``tests/test_web_smoke.py``), so nothing spawns. The state.db is a real migrated
one, because the continuation path reads ``task_runs`` and ``execution_traces``
as well as the thread tables. What is pinned:

* a first message launches attempt 1 under the thread id
* a message while a run is live *steers* — it never spawns a second run
* a follow-up after the run ended *continues* the same thread: attempt 2, a new
  run id, and a kickoff carrying the transcript and the prior verdict — the
  409's replacement
* a redelivered external message is a no-op
* a run task_runs still shows going is steered even when the thread has lost
  track of it, so one thread never runs two
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from mini_ork.stores import migrate as mig  # noqa: E402
from mini_ork.threads import service, store  # noqa: E402
from mini_ork.threads.model import RunStatus, ThreadState  # noqa: E402


@pytest.fixture
def home(tmp_path: Path) -> Path:
    h = tmp_path / "proj" / ".mini-ork"
    h.mkdir(parents=True)
    rc, _, err = mig.init_db(db=str(h / "state.db"), root=str(REPO))
    assert rc == 0, err
    return h


@pytest.fixture
def launches(monkeypatch) -> list[dict]:
    import mini_ork.web.control as control

    calls: list[dict] = []

    def fake(home, recipe, kickoff, run_id=None):
        calls.append({"home": home, "recipe": recipe, "kickoff": kickoff, "run_id": run_id})
        return {
            "ok": True,
            "run_id": run_id,
            "recipe": recipe,
            "pid": 4242,
            "kickoff_path": str(Path(home) / "runs-inbox" / f"{run_id}.md"),
            "log_path": str(Path(home) / "logs" / f"{run_id}.log"),
        }

    monkeypatch.setattr(control, "launch_run", fake)
    return calls


@pytest.fixture
def steers(monkeypatch) -> list[dict]:
    import mini_ork.web.control as control

    calls: list[dict] = []

    def fake(db, task_run_id, message, **kwargs):
        calls.append({"run_id": task_run_id, "message": message, **kwargs})
        return {"ok": True, "steering_id": 7}

    monkeypatch.setattr(control, "steer_run", fake)
    return calls


def _seed_trace(db: Path, run_id: str, verdict: str | None, artifact: str | None) -> None:
    con = sqlite3.connect(db)
    try:
        con.execute(
            "INSERT INTO execution_traces (trace_id, run_id, workflow_version_id, "
            "agent_version_id, task_class, prompt_version_hash, context_bundle_hash, "
            "status, reviewer_verdict, final_artifact_ref) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (f"tr-{run_id}", run_id, "wv1", "av1", "demo", "p1", "c1",
             "success", verdict, artifact),
        )
        con.commit()
    finally:
        con.close()


def test_a_first_message_launches_attempt_one(home: Path, launches: list[dict]) -> None:
    out = service.deliver(home, "t1", "Implement the retry budget.", title="Retry budget")

    assert out.action == "launched"
    assert out.run_id == "t1"           # attempt 1 reuses the thread id
    assert out.turn == 1
    assert launches[0]["run_id"] == "t1"
    assert launches[0]["recipe"] == service.DEFAULT_RECIPE
    assert "# Retry budget" in launches[0]["kickoff"]
    assert "Implement the retry budget." in launches[0]["kickoff"]

    runs = store.runs_for("t1", db=str(home / "state.db"))
    assert [r.attempt for r in runs] == [1]
    assert store.get_thread("t1", db=str(home / "state.db")).status is ThreadState.RUNNING


def test_a_message_while_a_run_is_live_steers_not_spawns(
    home: Path, launches: list[dict], steers: list[dict]
) -> None:
    service.deliver(home, "t1", "first")
    out = service.deliver(home, "t1", "also check the migrations")

    assert out.action == "steered"
    assert out.run_id == "t1"
    assert len(launches) == 1                    # no second run
    assert len(steers) == 1
    assert steers[0]["run_id"] == "t1"
    assert steers[0]["message"] == "also check the migrations"


def test_a_follow_up_after_the_run_ended_continues_the_same_thread(
    home: Path, launches: list[dict]
) -> None:
    service.deliver(home, "t1", "Implement the retry budget.", title="Retry budget")
    service.on_run_terminal(home, "t1", RunStatus.FINISHED, text="Added backoff on 429.")
    _seed_trace(home / "state.db", "t1", "tests passed", "src/retry.py")

    out = service.deliver(home, "t1", "Also cover the 503 case.")

    assert out.action == "continued"
    assert out.thread_id == "t1"                 # the SAME thread, not a new one
    assert out.run_id == "t1-t2"                 # attempt 2, new run id
    assert len(launches) == 2

    # The new run is told what came before and how the last one ended — the
    # whole reason a continuation differs from starting over.
    kickoff = launches[1]["kickoff"]
    assert "Also cover the 503 case." in kickoff
    assert "## Thread so far" in kickoff
    assert "**Agent:** Added backoff on 429." in kickoff
    assert "## Prior run" in kickoff
    assert "- Outcome: finished" in kickoff
    assert "- Verdict: tests passed" in kickoff
    assert "`src/retry.py`" in kickoff

    runs = store.runs_for("t1", db=str(home / "state.db"))
    assert [r.attempt for r in runs] == [1, 2]
    assert store.get_thread("t1", db=str(home / "state.db")).status is ThreadState.RUNNING


def test_a_redelivered_external_message_is_a_no_op(
    home: Path, launches: list[dict]
) -> None:
    service.deliver(home, "t1", "hi", external_id="slack:C1/123.45")
    before = len(store.messages("t1", db=str(home / "state.db")))

    out = service.deliver(home, "t1", "hi", external_id="slack:C1/123.45")

    assert out.action == "duplicate"
    assert len(launches) == 1
    assert len(store.messages("t1", db=str(home / "state.db"))) == before


def test_a_run_task_runs_still_shows_going_is_steered_not_spawned(
    home: Path, launches: list[dict], steers: list[dict]
) -> None:
    """The crash window between launch_run and link_run: the thread has lost the
    live flag, but the runner is still going. A second launch would put two runs
    on one thread, so the follow-up steers instead."""
    db = str(home / "state.db")
    store.open_thread("t1", db=db)
    store.link_run("t1", "t1", attempt=1, db=db)
    # The thread's own row says the run is over …
    store.settle_run("t1", RunStatus.FINISHED, db=db)
    # … but task_runs says it is still going.
    con = sqlite3.connect(db)
    try:
        con.execute(
            "INSERT INTO task_runs (id, recipe, status, cost_usd, created_at, updated_at, "
            "ended_at, task_class, kickoff_path, workflow_version, trace_id) "
            "VALUES ('t1','code-fix','executing',0.0,0,0,NULL,'demo','k.md','latest','tr-t1')"
        )
        con.commit()
    finally:
        con.close()

    out = service.deliver(home, "t1", "one more thing")

    assert out.action == "steered"
    assert out.run_id == "t1"
    assert len(launches) == 0


def test_a_failed_launch_reports_error_and_records_no_run(home: Path, monkeypatch) -> None:
    import mini_ork.web.control as control

    monkeypatch.setattr(
        control, "launch_run", lambda *a, **k: {"ok": False, "error": "recipe not found"}
    )
    out = service.deliver(home, "t1", "go")

    assert out.action == "error"
    assert out.ok is False
    assert out.run_id is None
    assert "recipe not found" in (out.detail or "")
    assert store.runs_for("t1", db=str(home / "state.db")) == []


def test_a_launch_links_the_issue_it_works(home: Path, launches: list[dict]) -> None:
    from mini_ork.pm import store as pm_store
    from mini_ork.pm.model import LinkKind

    service.deliver(home, "github-o-r-1", "go", issue_uid="github:o/r#1")

    links = pm_store.links_for("github:o/r#1", db=str(home / "state.db"))
    assert [link.kind for link in links] == [LinkKind.RUN]
    assert links[0].to_uid == "run:github-o-r-1"
