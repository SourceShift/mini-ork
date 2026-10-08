"""``retry_hint`` case 1.6 — a node that started and never ended is an interruption.

The kickoff (``kickoffs/auto/retry-hint-interrupted.md``): a dispatcher that dies
mid-node leaves a ``node_start`` with no matching ``node_end`` and a ``failed``
task row. Case 1.6 must classify that as ``strategy: resume`` from the dangling
node — not fall through to case 5 ("Failed at ?; the cause was not classified").

Fixtures mirror ``tests/unit/test_retry_hint.py`` (temp home + ``mig.init_db`` +
``sqlite3`` inserts). The first half inserts events with a controlled rising
``created_at`` so no stray ``node_end`` can close the dangling start.

The second half (``same-second`` tests) inserts every event at the SAME
``created_at`` with production-shaped ``event_id``s. ``created_at`` has
one-second resolution and the id is ``evt-<event_type>-<node>-<ns>-<pid>``, so
an ``ORDER BY created_at, event_id`` read hands back every ``node_end`` before
every ``node_start``. The walk must not depend on that order.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from mini_ork.recovery import retry_hint
from mini_ork.stores import migrate as mig

REPO = Path(__file__).resolve().parents[2]
RUN = "run-rhi-1791458537-intr01"


WORKFLOW = """\
version: 1
task_class: framework_edit
nodes:
  - {name: planner, type: planner, model_lane: planner, prompt_ref: prompts/planner.md}
  - {name: implementer, type: implementer, model_lane: worker, prompt_ref: prompts/implementer.md}
  - {name: live_smoke, type: verifier, verifier_ref: lib/live_smoke.py}
  - {name: static_check, type: verifier, verifier_ref: verifiers/static-check.py}
  - {name: reviewer, type: reviewer, model_lane: reviewer, prompt_ref: prompts/reviewer.md}
  - {name: publisher, type: publisher}
edges:
  - {from: planner, to: implementer, edge_type: depends_on}
  - {from: implementer, to: live_smoke, edge_type: depends_on}
  - {from: live_smoke, to: static_check, edge_type: depends_on}
  - {from: static_check, to: reviewer, edge_type: depends_on}
  - {from: reviewer, to: publisher, edge_type: depends_on}
"""


# ── fixtures ───────────────────────────────────────────────────────────────


@pytest.fixture
def home(tmp_path: Path) -> Path:
    h = tmp_path / "proj" / ".mini-ork"
    h.mkdir(parents=True)
    rc, _, err = mig.init_db(db=str(h / "state.db"), root=str(REPO))
    assert rc == 0, err
    recipe = h / "recipes" / "framework-edit"
    recipe.mkdir(parents=True)
    (recipe / "workflow.yaml").write_text(WORKFLOW)
    (recipe / "task_class.yaml").write_text("name: framework_edit\ndescription: rh\n")
    return h


def _seed_run(home: Path, run_id: str, *, status: str = "failed",
              now: int = 1_791_458_537) -> Path:
    run_dir = home / "runs" / run_id
    run_dir.mkdir(parents=True)
    kickoff = home / "kickoffs" / "demo.md"
    kickoff.parent.mkdir(parents=True, exist_ok=True)
    kickoff.write_text("# interrupted test\n")
    con = sqlite3.connect(home / "state.db")
    con.execute(
        "INSERT INTO task_runs (id, recipe, status, cost_usd, created_at, updated_at, "
        "ended_at, task_class, kickoff_path, workflow_version) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        (run_id, "framework-edit", status, 0.5, now, now + 100, now + 80,
         "framework_edit", str(kickoff), "latest"))
    con.commit()
    con.close()
    (run_dir / "plan.json").write_text('{"objective": "x"}')
    return run_dir


def _insert_events(home: Path, run_id: str,
                   events: list[tuple[str, dict]], *,
                   start: int = 1_791_458_600) -> None:
    """Insert ``(event_type, payload)`` rows in order via a rising ``created_at``.

    A rising timestamp guarantees the read order (``created_at ASC, event_id
    ASC``) matches the list order, so the walk sees exactly the sequence given.
    """
    con = sqlite3.connect(home / "state.db")
    for i, (et, payload) in enumerate(events):
        con.execute(
            "INSERT INTO run_events (event_id, run_id, event_type, payload_json, "
            "created_at) VALUES (?,?,?,?,?)",
            (f"ev-{run_id}-{i:02d}", run_id, et, json.dumps(payload), start + i))
    con.commit()
    con.close()


def _start(node: str) -> tuple[str, dict]:
    return ("node_start", {"node_id": node})


def _end(node: str, reason: str) -> tuple[str, dict]:
    return ("node_end", {"node_id": node, "finish_reason": reason})


def _production_event_id(event_type: str, node: str, seq: int,
                         *, pid: int = 4242) -> str:
    """``evt-<event_type>-<node>-<ns>-<pid>`` — the id
    ``observability.node_events`` actually writes.

    The ``event_type`` segment sits before the node and the ns suffix, so within
    one second every ``evt-node_end-…`` sorts before every ``evt-node_start-…``.
    That is the ordering pathology the same-second tests below exercise.
    """
    return f"evt-{event_type}-{node}-{1_791_458_600_000_000_000 + seq}-{pid}"


def _insert_events_same_second(home: Path, run_id: str,
                               events: list[tuple[str, dict]], *,
                               at: int = 1_791_458_600) -> None:
    """Insert every event at the SAME ``created_at`` with production-shaped ids.

    Rows are inserted in emission order, so ``rowid`` preserves the true
    sequence while an ``ORDER BY created_at, event_id`` read does not — the
    exact shape that broke the ordered walk.
    """
    con = sqlite3.connect(home / "state.db")
    for i, (et, payload) in enumerate(events):
        con.execute(
            "INSERT INTO run_events (event_id, run_id, event_type, payload_json, "
            "created_at) VALUES (?,?,?,?,?)",
            (_production_event_id(et, str(payload.get("node_id") or ""), i),
             run_id, et, json.dumps(payload), at))
    con.commit()
    con.close()


# ── case 1.6 ───────────────────────────────────────────────────────────────


def test_hint_version_is_3() -> None:
    assert retry_hint.HINT_VERSION == 3


def test_dangling_start_is_interrupted(home: Path) -> None:
    _seed_run(home, RUN)
    _insert_events(home, RUN, [
        _start("planner"), _end("planner", "done"),
        _start("implementer"), _end("implementer", "done"),
        _start("reviewer"), _end("reviewer", "verdict_revise"),
        _start("implementer"),  # no matching node_end — the dispatcher died here
    ])

    direct = retry_hint._case_interrupted(home, RUN)
    assert direct is not None
    assert direct["failed_node"] == "implementer"
    assert direct["from_node"] == "implementer"
    assert direct["retryable"] is True
    assert direct["strategy"] == "resume"
    assert direct["version"] == retry_hint.HINT_VERSION
    assert direct["needs_change"]["kind"] == "interrupted"
    assert direct["needs_change"]["evidence"] == "node_start without node_end"
    assert "implementer" in direct["needs_change"]["summary"]
    assert direct["notes"] == []

    # The compute path picks it up BEFORE the reviewer case — the older
    # reviewer verdict_revise must not shadow the interruption.
    hint = retry_hint.compute(home, RUN)
    assert hint is not None
    assert hint["failed_node"] == "implementer"
    assert hint["retryable"] is True
    assert hint["strategy"] == "resume"
    assert hint["needs_change"]["kind"] == "interrupted"
    assert "--strategy resume" in hint["command"]


def test_ended_node_with_rollback_is_not_interrupted(home: Path) -> None:
    _seed_run(home, RUN)
    _insert_events(home, RUN, [
        _start("planner"), _end("planner", "done"),
        _start("implementer"), _end("implementer", "done"),
        _start("reviewer"), _end("reviewer", "verdict_revise"),
        _start("implementer"), _end("implementer", "error"),
        _start("rollback"), _end("rollback", "done"),
    ])

    # Every started node also ended: this is a genuine failure, not an
    # interruption. The case declines and ``compute`` falls through to the
    # older rules.
    assert retry_hint._case_interrupted(home, RUN) is None
    hint = retry_hint.compute(home, RUN)
    if hint is not None:
        assert (hint.get("needs_change") or {}).get("kind") != "interrupted"


def test_two_open_nodes_takes_the_last_started(home: Path) -> None:
    _seed_run(home, RUN)
    _insert_events(home, RUN, [
        _start("planner"), _end("planner", "done"),
        _start("code_impact_lens"),   # parallel lens: started, never ended
        _start("prior_art_lens"),     # the later start wins
    ])

    hint = retry_hint._case_interrupted(home, RUN)
    assert hint is not None
    assert hint["failed_node"] == "prior_art_lens"
    assert hint["from_node"] == "prior_art_lens"


def test_parallel_sibling_failure_declines(home: Path) -> None:
    """A parallel batch that fails on one node (a 429 dead lane, a REFUTED
    sibling) leaves its in-flight sibling with a dangling ``node_start``.

    That is a genuine failure, not an interruption: the dead node's own case
    (lane / code) must classify it, and resuming the dangling sibling would
    re-hit the same dead lane. The failing ``node_end`` comes AFTER the
    sibling's start, so the case declines and ``compute`` never labels the run
    ``interrupted``.
    """
    _seed_run(home, RUN)
    _insert_events(home, RUN, [
        _start("code_impact_lens"),
        _start("prior_art_lens"),      # same parallel batch, still in flight
        _end("prior_art_lens", "error"),  # …but its sibling died
    ])

    assert retry_hint._case_interrupted(home, RUN) is None
    hint = retry_hint.compute(home, RUN)
    if hint is not None:
        assert (hint.get("needs_change") or {}).get("kind") != "interrupted"


def test_failure_before_restart_still_interrupted(home: Path) -> None:
    """The mirror of the sibling case: a failure BEFORE the dangling node's
    *start* does not veto it. A reviewer verdict from an earlier revise round,
    followed by a re-entered node that never ended, is still an interruption."""
    _seed_run(home, RUN)
    _insert_events(home, RUN, [
        _start("planner"), _end("planner", "done"),
        _start("implementer"), _end("implementer", "done"),
        _start("reviewer"), _end("reviewer", "verdict_revise"),
        _start("implementer"),  # re-entered after the verdict, never ended
    ])

    hint = retry_hint._case_interrupted(home, RUN)
    assert hint is not None
    assert hint["failed_node"] == "implementer"


def test_bare_node_end_without_start_declines(home: Path) -> None:
    """A ``node_end`` with no open ``node_start`` is a no-op.

    Several existing fixtures seed bare ``node_end`` rows; the walk must not
    invent an open node (or a closed one) from them.
    """
    _seed_run(home, RUN)
    _insert_events(home, RUN, [
        _end("implementer", "done"),
    ])
    assert retry_hint._case_interrupted(home, RUN) is None


def test_missing_run_events_table_declines(home: Path) -> None:
    _seed_run(home, RUN)
    con = sqlite3.connect(home / "state.db")
    con.execute("DROP TABLE run_events")
    con.commit()
    con.close()

    assert retry_hint._case_interrupted(home, RUN) is None
    # ``compute`` must not raise on an unreadable lifecycle.
    hint = retry_hint.compute(home, RUN)
    if hint is not None:
        assert (hint.get("needs_change") or {}).get("kind") != "interrupted"


# ── case 1.6 under real (one-second) timestamps ─────────────────────────────
#
# ``created_at`` is ``int(time.time())`` and the event id is
# ``evt-<event_type>-<node>-<ns>-<pid>``, so every same-second ``node_end``
# sorts before every ``node_start`` in the stored order. These tests pin the
# behaviour under that ordering — the shape that made a genuine failure read as
# "interrupted".


def test_same_second_read_order_is_pathological(home: Path) -> None:
    """Guard the precondition: with equal ``created_at`` and production-shaped
    ids, the shared read really does hand back the ``node_end`` first. If this
    ever stops holding, the regressions below lose their teeth."""
    _seed_run(home, RUN)
    _insert_events_same_second(home, RUN, [
        _start("implementer"), _end("implementer", "done"),
    ])

    events = retry_hint._run_node_events(home, RUN)
    assert events is not None
    assert [e["event_type"] for e in events] == ["node_end", "node_start"]


def test_same_second_rollback_after_verifier_error_declines(home: Path) -> None:
    """A node that starts and ends in the SAME second must not read as open.

    The reported HIGH: the verifier ends ``error`` and the rollback node starts
    and ends within one second, so the ordered read returned rollback's end
    before its start and the node stayed open forever — turning a genuine
    verifier failure into "Interrupted during rollback". Every started node also
    ended, so the case must decline.
    """
    _seed_run(home, RUN)
    _insert_events_same_second(home, RUN, [
        _start("implementer"), _end("implementer", "done"),
        _start("static_check_verifier"), _end("static_check_verifier", "error"),
        _start("rollback"), _end("rollback", "done"),
    ])

    assert retry_hint._case_interrupted(home, RUN) is None
    hint = retry_hint.compute(home, RUN)
    if hint is not None:
        assert (hint.get("needs_change") or {}).get("kind") != "interrupted"


def test_same_second_parallel_sibling_failure_declines(home: Path) -> None:
    """The round-1 regression under real timestamps.

    Two lens nodes start and one ends ``error`` in the SAME second (a fast 429 /
    dead-lane fail). In emission order the survivor's ``node_start`` precedes the
    sibling's failing ``node_end``, so the survivor is dangling — but a real
    failure owns the run and the lane/code case must classify it. The case
    declines; it must not name ``prior_art_lens`` (or the survivor) interrupted.
    """
    _seed_run(home, RUN)
    _insert_events_same_second(home, RUN, [
        _start("code_impact_lens"),
        _start("prior_art_lens"),        # same parallel batch, still in flight
        _end("prior_art_lens", "error"),  # …but its sibling died
    ])

    assert retry_hint._case_interrupted(home, RUN) is None
    hint = retry_hint.compute(home, RUN)
    if hint is not None:
        assert (hint.get("needs_change") or {}).get("kind") != "interrupted"


def test_same_second_dangling_implementer_still_fires(home: Path) -> None:
    """The genuine interruption survives equal timestamps.

    Every node — and the earlier failing ``verdict_revise`` — shares one second,
    and the dispatcher died after re-entering the implementer. The failing end
    was emitted BEFORE the re-start, so it is an earlier round's verdict, not a
    sibling's death: the hint still fires, from the implementer.
    """
    _seed_run(home, RUN)
    _insert_events_same_second(home, RUN, [
        _start("planner"), _end("planner", "done"),
        _start("implementer"), _end("implementer", "done"),
        _start("reviewer"), _end("reviewer", "verdict_revise"),
        _start("implementer"),  # re-entered after the verdict, never ended
    ])

    hint = retry_hint._case_interrupted(home, RUN)
    assert hint is not None
    assert hint["failed_node"] == "implementer"
    assert hint["from_node"] == "implementer"
    assert hint["strategy"] == "resume"
    assert (hint["needs_change"] or {}).get("kind") == "interrupted"


# ── review follow-ups ──────────────────────────────────────────────────────


def test_synthetic_crash_close_keeps_the_node_interrupted(home: Path) -> None:
    # ``board kill`` / the reaper / ``recover`` close a dead attempt with a
    # synthetic node_end {verdict: CRASH, interrupted: true}. That records the
    # interruption; it must not turn the run back into "unclassified".
    _seed_run(home, RUN)
    _insert_events(home, RUN, [
        _start("planner"), _end("planner", "done"),
        _start("implementer"),
        ("node_end", {"node_id": "implementer", "verdict": "CRASH",
                      "interrupted": True, "duration_ms": 0}),
    ])
    hint = retry_hint._case_interrupted(home, RUN)
    assert hint is not None
    assert hint["failed_node"] == "implementer"
    assert hint["strategy"] == "resume"


def test_a_skipped_sibling_after_the_start_does_not_veto(home: Path) -> None:
    # skipped / abstain / levels_unverified end a node without failing it
    # (task_state._TERMINAL_OK_FINISH), so they must not veto the case.
    _seed_run(home, RUN)
    _insert_events(home, RUN, [
        _start("implementer"),
        _start("lens"), _end("lens", "skipped"),
    ])
    hint = retry_hint._case_interrupted(home, RUN)
    assert hint is not None
    assert hint["failed_node"] == "implementer"
