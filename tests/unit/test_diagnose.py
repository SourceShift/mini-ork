"""``mini_ork.recovery.diagnose`` — the typed, evidence-cited run diagnosis.

Hermetic: every case builds a temp home + temp sqlite so the assertions pin the
*shape* of the live failure (the ``run-1791914481-48337`` abstain run, a
reviewer ``needs_revision``, a ``timeout``, a reaped lifecycle) rather than
reading any real run directory. Fixture scaffolding mirrors
``tests/unit/test_retry_hint.py``.
"""
from __future__ import annotations

import json
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from mini_ork.recovery import diagnose as dx
from mini_ork.recovery import retry_hint
from mini_ork.stores import migrate as mig

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "tests"))

RUN = "run-1791914481-48337"
T0 = 1_791_914_481


# ── fixtures ───────────────────────────────────────────────────────────────


@pytest.fixture
def home(tmp_path: Path) -> Path:
    h = tmp_path / "proj" / ".mini-ork"
    h.mkdir(parents=True)
    rc, _out, err = mig.init_db(db=str(h / "state.db"), root=str(REPO))
    assert rc == 0, err
    return h


def _seed(home: Path, *, run_id: str = RUN, status: str = "failed",
          recipe: str = "framework-edit", notes: str = "") -> Path:
    run_dir = home / "runs" / run_id
    run_dir.mkdir(parents=True)
    (run_dir / "run_profile.json").write_text(json.dumps({"recipe": recipe}))
    con = sqlite3.connect(home / "state.db")
    con.execute(
        "INSERT OR REPLACE INTO task_runs (id, recipe, status, cost_usd, created_at, "
        "updated_at, ended_at, task_class, kickoff_path, workflow_version, notes) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (run_id, recipe, status, 0.5, T0, T0 + 100, T0 + 80, "framework_edit",
         "", "latest", notes or None),
    )
    con.commit()
    con.close()
    return run_dir


def _set_notes(home: Path, run_id: str, notes: str) -> None:
    con = sqlite3.connect(home / "state.db")
    con.execute("UPDATE task_runs SET notes = ? WHERE id = ?", (notes, run_id))
    con.commit()
    con.close()


def _event(home: Path, run_id: str, seq: int, event_type: str, payload: dict[str, Any]) -> None:
    con = sqlite3.connect(home / "state.db")
    con.execute(
        "INSERT INTO run_events (event_id, run_id, event_type, payload_json, created_at) "
        "VALUES (?,?,?,?,?)",
        (f"evt-{run_id}-{event_type}-{seq}", run_id, event_type,
         json.dumps(payload), T0 + seq),
    )
    con.commit()
    con.close()


def _latest_ends(home: Path, run_id: str) -> dict[str, str]:
    """The test's own read of each node's latest ``node_end`` finish reason."""
    con = sqlite3.connect(home / "state.db")
    try:
        rows = con.execute(
            "SELECT payload_json FROM run_events WHERE run_id = ? AND event_type = 'node_end' "
            "ORDER BY created_at ASC, event_id ASC", (run_id,)).fetchall()
    finally:
        con.close()
    ends: dict[str, str] = {}
    for (raw,) in rows:
        payload = json.loads(raw or "{}")
        node = str(payload.get("node_id") or "")
        if node:
            ends[node] = str(payload.get("finish_reason") or "")
    return ends


def _assert_done_invariant(home: Path, run_id: str, result: dict[str, Any]) -> None:
    """``node`` never names a node whose latest ``node_end`` finished ``done``."""
    node = result.get("node")
    if not node:
        return
    assert _latest_ends(home, run_id).get(node) != "done", (
        f"diagnosis named {node!r} although it finished done: {result}"
    )


def _assert_contract(result: dict[str, Any]) -> None:
    """The wire contract every result — matched or fall-through — must obey."""
    assert result["layer"] in dx.LAYERS, result
    assert result["routes_to"] in dx.ROUTES, result
    for item in result["evidence"]:
        assert set(item) == {"kind", "ref", "excerpt"}, item
    if result["rule_id"] == dx.RULE_UNCLASSIFIED:
        assert result["source"] == ""
    else:
        assert result["source"] == "rule", result


# ── R1 — the run-1791914481-48337 shape (proven levels, withheld publish) ───


def _seed_abstain(home: Path, run_id: str = RUN, *, levels_ok: bool = True,
                  reason: str = "publish_abstain") -> Path:
    """The live shape: every node done, the publisher withheld the publish.

    ``levels_ok=True`` + ``reason="publish_abstain"`` is the ENGINE fault (R1);
    ``levels_ok=False`` / ``reason="levels_unverified"`` is the change's own
    unproven vector (R1b).
    """
    run_dir = _seed(home, run_id=run_id)
    (run_dir / "publish-abstain.json").write_text(json.dumps({
        "reason": "authored patch did not apply",
        "detail": "the implementer's patch no longer applies to the target tree",
    }))
    (run_dir / "verdict.json").write_text(json.dumps({
        "verdict": "pass",
        "source": "execute@run-level",
        "levels": {"applies": "PROVEN", "executes": "PROVEN", "target": "PROVEN",
                   "preserve": "PROVEN", "contract": "PROVEN"},
        "levels_required": ["applies", "target"],
        "levels_ok": levels_ok,
        "levels_decision": "publish" if levels_ok else "abstain",
    }))
    _event(home, run_id, 1, "node_start", {"node_id": "implementer"})
    _event(home, run_id, 2, "node_end",
           {"node_id": "implementer", "finish_reason": "done"})
    _event(home, run_id, 3, "node_start", {"node_id": "publisher", "node_type": "publisher"})
    _event(home, run_id, 4, "node_end",
           {"node_id": "publisher", "node_type": "publisher", "finish_reason": reason})
    return run_dir


def test_r1_publish_abstain_with_proven_levels_is_engine_fix_epic(home: Path) -> None:
    _seed_abstain(home)

    result = dx.diagnose(home, RUN, write=False)

    assert result["rule_id"] == "R1"
    assert (result["layer"], result["stage"], result["node"]) == ("engine", "deliver", "publisher")
    assert result["routes_to"] == "fix_epic"
    assert result["source"] == "rule"
    refs = [e["ref"] for e in result["evidence"]]
    assert "publish-abstain.json" in refs
    assert any(e["kind"] == "event" and "publish_abstain" in e["excerpt"]
               for e in result["evidence"])
    _assert_contract(result)
    _assert_done_invariant(home, RUN, result)


def test_r1_publish_abstain_declines_when_the_publisher_later_finished_done(home: Path) -> None:
    """A retried publisher that PUBLISHED is not a withheld run — read the latest end."""
    run_dir = _seed_abstain(home)
    (run_dir / "publish-abstain.json").unlink()
    _event(home, RUN, 5, "node_start", {"node_id": "publisher", "node_type": "publisher"})
    _event(home, RUN, 6, "node_end",
           {"node_id": "publisher", "node_type": "publisher", "finish_reason": "done"})

    result = dx.diagnose(home, RUN, write=False)

    assert result["rule_id"] != "R1"
    assert result["node"] != "publisher"
    _assert_contract(result)
    _assert_done_invariant(home, RUN, result)


def test_r1b_levels_unverified_is_change_verify_reverify(home: Path) -> None:
    """A withheld publish with an UNPROVEN level vector is the change's problem."""
    _seed_abstain(home, levels_ok=False, reason="levels_unverified")

    result = dx.diagnose(home, RUN, write=False)

    assert result["rule_id"] == "R1b"
    assert (result["layer"], result["stage"], result["node"]) == ("change", "verify", "publisher")
    assert result["routes_to"] == "reverify"
    assert any(e["kind"] == "event" and "levels_unverified" in e["excerpt"]
               for e in result["evidence"])
    _assert_contract(result)


def test_r1_publish_abstain_without_a_level_stamp_is_change_not_engine(home: Path) -> None:
    """``publish_abstain`` alone is not enough — no ``levels_ok`` stamp ⇒ R1b."""
    run_dir = _seed_abstain(home)
    (run_dir / "verdict.json").unlink()

    result = dx.diagnose(home, RUN, write=False)

    assert result["rule_id"] == "R1b"
    assert result["layer"] == "change"
    assert result["routes_to"] == "reverify"


def test_diagnose_writes_diagnosis_json_only_when_asked(home: Path) -> None:
    run_dir = _seed_abstain(home)

    dx.diagnose(home, RUN, write=False)
    assert not (run_dir / "diagnosis.json").exists()

    dx.diagnose(home, RUN, write=True)
    written = json.loads((run_dir / "diagnosis.json").read_text())
    assert written["version"] == dx.DIAGNOSIS_VERSION
    assert written["rule_id"] == "R1"


# ── R4 — a reviewer asked for a revision ───────────────────────────────────


def test_r4_reviewer_needs_revision_is_change_review(home: Path) -> None:
    run_dir = _seed(home)
    (run_dir / "review-reviewer.json").write_text(json.dumps({
        "verdict": "needs_revision",
        "reasons": ["the diff does not touch the parser it claims to fix"],
    }))
    _event(home, RUN, 1, "node_start", {"node_id": "reviewer", "node_type": "reviewer"})
    _event(home, RUN, 2, "node_end",
           {"node_id": "reviewer", "node_type": "reviewer", "verdict": "needs_revision"})

    result = dx.diagnose(home, RUN, write=False)

    assert result["rule_id"] == "R4"
    assert (result["layer"], result["stage"], result["node"]) == ("change", "review", "reviewer")
    assert result["routes_to"] == "revise"
    assert any(e["ref"] == "review-reviewer.json" for e in result["evidence"])
    _assert_contract(result)
    _assert_done_invariant(home, RUN, result)


# ── R3 — a timeout is provider trouble, not a change to revise ─────────────


def test_r3_timeout_is_provider_retry(home: Path) -> None:
    _seed(home)
    _event(home, RUN, 1, "node_start", {"node_id": "implementer", "node_type": "implementer"})
    _event(home, RUN, 2, "node_end",
           {"node_id": "implementer", "node_type": "implementer", "finish_reason": "timeout"})

    result = dx.diagnose(home, RUN, write=False)

    assert result["rule_id"] == "R3"
    assert (result["layer"], result["stage"], result["node"]) == ("provider", "execute", "implementer")
    assert result["routes_to"] == "retry"
    assert any("finish_reason=timeout" in e["excerpt"] for e in result["evidence"])
    _assert_contract(result)
    _assert_done_invariant(home, RUN, result)


# ── R5 — a verifier refuted the change ─────────────────────────────────────


def test_r5_failing_verifier_is_change_verify(home: Path) -> None:
    run_dir = _seed(home)
    (run_dir / "verifier_test.json").write_text(json.dumps({
        "status": "FAIL", "pass": False, "reason": "2 tests failed in test_parser.py",
    }))

    result = dx.diagnose(home, RUN, write=False)

    assert result["rule_id"] == "R5"
    assert (result["layer"], result["stage"]) == ("change", "verify")
    assert result["routes_to"] == "revise"
    assert any(e["ref"] == "verifier_test.json" for e in result["evidence"])
    _assert_contract(result)


# ── R6 — a node that started and never ended ───────────────────────────────


def test_r6_interrupted_without_a_note_is_provider_retry(home: Path) -> None:
    """A dangling node is transport trouble, never a framework bug."""
    _seed(home)
    _event(home, RUN, 1, "node_start", {"node_id": "implementer", "node_type": "implementer"})

    result = dx.diagnose(home, RUN, write=False)

    assert result["rule_id"] == "R6"
    assert result["layer"] == "provider"
    assert result["routes_to"] == "retry"
    assert result["node"] == "implementer"
    _assert_contract(result)


def test_r6_interrupted_with_killed_by_user_note_is_user_none(home: Path) -> None:
    """``board kill`` appends ``killed-by-user`` — a person stopped it: no action."""
    _seed(home, notes="killed-by-user")
    _event(home, RUN, 1, "node_start", {"node_id": "implementer", "node_type": "implementer"})

    result = dx.diagnose(home, RUN, write=False)

    assert result["rule_id"] == "R6"
    assert result["layer"] == "user"
    assert result["routes_to"] == "none"
    assert result["node"] == "implementer"
    assert any(e["kind"] == "note" for e in result["evidence"])
    _assert_contract(result)


def test_r6_interrupted_matches_the_note_per_segment_not_as_a_prefix(home: Path) -> None:
    """Notes are append-only: an earlier note must not hide a later ``killed-by-user``."""
    _seed(home, notes="reaped: dispatcher pid 4 gone; killed-by-user")
    _event(home, RUN, 1, "node_start", {"node_id": "implementer", "node_type": "implementer"})

    result = dx.diagnose(home, RUN, write=False)

    assert (result["layer"], result["routes_to"]) == ("user", "none")
    _assert_contract(result)


def test_r6_interrupted_is_never_engine(home: Path) -> None:
    """Neither with nor without the note may an interruption read as an engine fault."""
    for notes in ("", "killed-by-user"):
        run_id = f"run-int-{notes or 'plain'}"
        _seed(home, run_id=run_id, notes=notes)
        _event(home, run_id, 1, "node_start", {"node_id": "implementer"})

        result = dx.diagnose(home, run_id, write=False)

        assert result["layer"] != "engine", result
        assert result["routes_to"] in {"retry", "none"}


# ── RN — the ``task_runs.notes`` rules ─────────────────────────────────────


def test_note_reaped_is_provider_retry(home: Path) -> None:
    _seed(home, notes="reaped: dispatcher pid 4242 gone")
    _event(home, RUN, 1, "node_start", {"node_id": "implementer", "node_type": "implementer"})

    result = dx.diagnose(home, RUN, write=False)

    # R6 (the dangling node) fires first and is the same verdict — provider/retry.
    assert result["rule_id"] in {"R6", "RN"}
    assert result["layer"] == "provider"
    assert result["routes_to"] == "retry"
    _assert_contract(result)


def test_note_reaped_with_no_open_node_reports_an_unknown_stage(home: Path) -> None:
    """A reaped run whose nodes all ended: the note is the only cause left."""
    _seed(home, notes="reaped: no live dispatcher (pid 9 gone)")
    _event(home, RUN, 1, "node_start", {"node_id": "planner"})
    _event(home, RUN, 2, "node_end", {"node_id": "planner", "finish_reason": "done"})

    result = dx.diagnose(home, RUN, write=False)

    assert result["rule_id"] == "RN"
    assert (result["layer"], result["stage"]) == ("provider", "unknown")
    assert result["routes_to"] == "retry"
    assert any(e["kind"] == "note" and e["ref"] == "task_runs.notes" for e in result["evidence"])
    _assert_contract(result)


def test_note_planner_dispatch_rc_is_provider_plan_planner_retry(home: Path) -> None:
    _seed(home, notes="planner_dispatch_rc_43")

    result = dx.diagnose(home, RUN, write=False)

    assert result["rule_id"] == "RN"
    assert (result["layer"], result["stage"], result["node"]) == ("provider", "plan", "planner")
    assert result["routes_to"] == "retry"
    _assert_contract(result)


def test_note_lifecycle_exited_on_an_exception_with_a_traceback_is_engine(home: Path) -> None:
    """Only a traceback inside mini-ork's own source makes the crash an engine fault.

    No node failed and none is dangling: the lifecycle process itself died, and
    its own log is the only witness that the exception came from mini-ork.
    """
    run_dir = _seed(home, notes="lifecycle exited on an exception before a verdict")
    (run_dir / "execute.log").write_text(
        "Traceback (most recent call last):\n"
        '  File "/x/mini_ork/recovery/diagnose.py", line 12, in _diagnose\n'
        "    raise RuntimeError('boom')\n"
        "RuntimeError: boom\n"
    )
    _event(home, RUN, 1, "node_start", {"node_id": "planner", "node_type": "planner"})
    _event(home, RUN, 2, "node_end",
           {"node_id": "planner", "node_type": "planner", "finish_reason": "done"})

    result = dx.diagnose(home, RUN, write=False)

    assert result["rule_id"] == "RN"
    assert result["layer"] == "engine"
    assert result["routes_to"] == "fix_epic"
    assert any(e["kind"] == "note" for e in result["evidence"])
    assert any(e["kind"] == "log" for e in result["evidence"])
    _assert_contract(result)


def test_note_lifecycle_exited_on_an_exception_with_a_non_framework_traceback_stays_unclassified(
        home: Path) -> None:
    """A traceback in the consumer's code is not mini-ork's fault — do not label engine."""
    run_dir = _seed(home, notes="lifecycle exited on an exception before a verdict")
    (run_dir / "execute.log").write_text(
        "Traceback (most recent call last):\n"
        '  File "/tmp/target/app/parser.py", line 3, in parse\n'
        "ValueError: bad input\n"
    )

    result = dx.diagnose(home, RUN, write=False)

    assert result["layer"] == "unclassified"
    assert result["source"] == ""
    _assert_contract(result)


def test_note_lifecycle_exited_on_an_exception_without_a_traceback_stays_unclassified(
        home: Path) -> None:
    """No traceback localises it, so the honest answer is ``unclassified``."""
    _seed(home, notes="lifecycle exited on an exception before a verdict")

    result = dx.diagnose(home, RUN, write=False)

    assert result["rule_id"] == dx.RULE_UNCLASSIFIED
    assert result["layer"] == "unclassified"
    _assert_contract(result)


def test_note_lifecycle_exited_without_a_verdict_stays_unclassified(home: Path) -> None:
    """A model diagnoser owns this one in SDE-3 — the rule set must not guess."""
    _seed(home, notes="lifecycle exited without a verdict")

    result = dx.diagnose(home, RUN, write=False)

    assert result["rule_id"] == dx.RULE_UNCLASSIFIED
    assert result["layer"] == "unclassified"
    assert result["source"] == ""
    _assert_contract(result)


# ── R7 / R8 — the retry-hint rules ─────────────────────────────────────────


def test_r7_owner_only_fix_is_environment_owner(home: Path, monkeypatch) -> None:
    from mini_ork.recovery import retry_hint as rh

    _seed(home)
    monkeypatch.setattr(rh, "load_or_compute", lambda *a, **k: {
        "version": rh.HINT_VERSION, "run_id": RUN, "failed_node": "implementer",
        "retryable": True, "strategy": "resume", "from_node": "implementer",
        "needs_change": {"kind": "credentials", "summary": "the API key is missing",
                         "detail": "", "evidence": ""},
        "notes": [], "command": "", "computed_at": "2026-01-01T00:00:00Z",
    })

    result = dx.diagnose(home, RUN, write=False)

    assert result["rule_id"] == "R7"
    assert result["layer"] == "environment"
    assert result["routes_to"] == "owner"
    _assert_contract(result)


def test_r8_lane_hint_is_model_swap_lane(home: Path, monkeypatch) -> None:
    from mini_ork.recovery import retry_hint as rh

    _seed(home)
    monkeypatch.setattr(rh, "load_or_compute", lambda *a, **k: {
        "version": rh.HINT_VERSION, "run_id": RUN, "failed_node": "implementer",
        "retryable": True, "strategy": "lane", "from_node": "implementer",
        "needs_change": {"kind": "lane", "summary": "flash is unavailable",
                         "detail": "", "evidence": ""},
        "notes": [], "command": "", "computed_at": "2026-01-01T00:00:00Z",
    })

    result = dx.diagnose(home, RUN, write=False)

    assert result["rule_id"] == "R8"
    assert result["layer"] == "model"
    assert result["routes_to"] == "swap_lane"
    _assert_contract(result)


# ── the contract fields ────────────────────────────────────────────────────


def test_every_rule_result_carries_the_literal_rule_source(home: Path) -> None:
    """``source`` is ``"rule"`` on every rule result and ``""`` on the fall-through."""
    _seed_abstain(home)
    for run_id, seed in (
        ("run-r1", lambda: _seed_abstain(home, "run-r1")),
        ("run-r1b", lambda: _seed_abstain(home, "run-r1b", levels_ok=False,
                                          reason="levels_unverified")),
        ("run-plain", lambda: _seed(home, run_id="run-plain")),
    ):
        seed()
        result = dx.diagnose(home, run_id, write=False)
        assert result["source"] == ("rule" if run_id != "run-plain" else ""), result


def test_evidence_items_carry_excerpt_and_never_detail(home: Path) -> None:
    _seed_abstain(home)

    result = dx.diagnose(home, RUN, write=False)

    assert result["evidence"]
    for item in result["evidence"]:
        assert "excerpt" in item
        assert "detail" not in item


def test_layer_and_routes_values_are_inside_the_declared_enums(home: Path) -> None:
    """Every value the suite can produce lies inside the contract enums."""
    # R1
    _seed_abstain(home)
    # R1b
    _seed_abstain(home, "run-r1b", levels_ok=False, reason="levels_unverified")
    # R2
    r2 = _seed(home, run_id="run-r2")
    (r2 / "impl-implementer.log").write_text(
        'Traceback (most recent call last):\n'
        '  File "/x/mini_ork/foo.py", line 1, in f\n'
        "ValueError: x\n"
    )
    _event(home, "run-r2", 1, "node_start", {"node_id": "implementer"})
    _event(home, "run-r2", 2, "node_end",
           {"node_id": "implementer", "finish_reason": "error"})
    # R3
    _seed(home, run_id="run-r3")
    _event(home, "run-r3", 1, "node_end",
           {"node_id": "implementer", "finish_reason": "timeout"})
    # R4
    r4 = _seed(home, run_id="run-r4")
    (r4 / "review-reviewer.json").write_text(json.dumps({"verdict": "needs_revision"}))
    # R5
    r5 = _seed(home, run_id="run-r5")
    (r5 / "verifier_test.json").write_text(json.dumps({"status": "FAIL", "pass": False}))
    # R6 (provider + user)
    _seed(home, run_id="run-r6", notes="killed-by-user")
    _event(home, "run-r6", 1, "node_start", {"node_id": "implementer"})
    _seed(home, run_id="run-r6p")
    _event(home, "run-r6p", 1, "node_start", {"node_id": "implementer"})
    # RN
    _seed(home, run_id="run-rn", notes="planner_dispatch_rc_43")
    # fall-through
    _seed(home, run_id="run-none")

    run_ids = ["run-1791914481-48337", "run-r1b", "run-r2", "run-r3", "run-r4",
               "run-r5", "run-r6", "run-r6p", "run-rn", "run-none"]
    seen_layers: set[str] = set()
    seen_routes: set[str] = set()
    for run_id in run_ids:
        result = dx.diagnose(home, run_id, write=False)
        seen_layers.add(result["layer"])
        seen_routes.add(result["routes_to"])
        _assert_contract(result)

    assert seen_layers <= dx.LAYERS
    assert seen_routes <= dx.ROUTES
    for required in ("engine", "change", "provider", "user", "unclassified"):
        assert required in seen_layers, (required, seen_layers)
    for required in ("fix_epic", "reverify", "retry", "none", "revise", "human"):
        assert required in seen_routes, (required, seen_routes)


# ── the unclassified fall-through ──────────────────────────────────────────


def test_no_evidence_run_is_unclassified_at_zero_confidence(home: Path) -> None:
    _seed(home)

    result = dx.diagnose(home, RUN, write=False)

    assert result["rule_id"] == dx.RULE_UNCLASSIFIED
    assert result["layer"] == "unclassified"
    assert result["confidence"] == 0.0
    assert result["evidence"] == []
    assert result["node"] is None
    assert result["source"] == ""


def test_done_node_invariant_never_names_a_finished_node(home: Path) -> None:
    """A run whose only node finished ``done`` names nothing, not the finished node."""
    _seed(home)
    _event(home, RUN, 1, "node_start", {"node_id": "implementer"})
    _event(home, RUN, 2, "node_end", {"node_id": "implementer", "finish_reason": "done"})

    result = dx.diagnose(home, RUN, write=False)

    assert result["node"] is None
    _assert_done_invariant(home, RUN, result)


def test_diagnose_never_raises_on_a_missing_run(home: Path) -> None:
    result = dx.diagnose(home, "run-does-not-exist", write=True)

    assert result["layer"] == "unclassified"
    assert result["confidence"] == 0.0


def test_every_matching_rule_cites_evidence(home: Path) -> None:
    """Every matched rule cites at least one evidence item; only the fall-through may not."""
    _seed_abstain(home)
    seeded = [("R1", dx.diagnose(home, RUN, write=False))]
    r4 = _seed(home, run_id="run-r4")
    (r4 / "review-reviewer.json").write_text(json.dumps({"verdict": "needs_revision"}))
    seeded.append(("R4", dx.diagnose(home, "run-r4", write=False)))
    _seed(home, run_id="run-r3")
    _event(home, "run-r3", 1, "node_end",
           {"node_id": "implementer", "finish_reason": "timeout"})
    seeded.append(("R3", dx.diagnose(home, "run-r3", write=False)))

    for rule_id, result in seeded:
        assert result["rule_id"] == rule_id
        assert result["evidence"], f"{rule_id} matched with no evidence"


# ── retry_hint: the case-5 fallback obeys the same invariant ───────────────


def test_case5_names_the_publisher_not_a_finished_implementer(home: Path) -> None:
    """The abstain shape: the implementer finished ``done``, the publisher abstained."""
    run_dir = _seed_abstain(home)
    # The publisher's own abstain artefact is what case 1.5 declines on (a node
    # failed per ``_failing_node``), so case 5 is the one that labels the run.
    (run_dir / "impl-implementer.log").write_text("implementer finished normally\n")
    (run_dir / "publish-abstain.json").unlink()
    _event(home, RUN, 7, "node_start", {"node_id": "publisher", "node_type": "publisher"})
    _event(home, RUN, 8, "node_end",
           {"node_id": "publisher", "node_type": "publisher", "finish_reason": "publish_abstain"})

    hint = retry_hint.load_or_compute(home, RUN, write=False)

    assert hint is not None
    assert hint["failed_node"] == "publisher"
    assert "implementer" not in json.dumps(hint.get("needs_change") or {})


def test_case5_falls_back_to_a_node_that_still_has_a_dangling_start(home: Path) -> None:
    """A dangling node is not ``done``, so it stays the case-5 label."""
    run_dir = _seed(home, run_id="run-open")
    (run_dir / "impl-implementer.log").write_text("implementer started\n")
    _event(home, "run-open", 1, "node_start", {"node_id": "implementer"})

    hint = retry_hint.load_or_compute(home, "run-open", write=False)

    assert hint is not None
    assert hint["failed_node"] == "implementer"


# ── auto_repair wiring: same action, additive diagnosis key ────────────────


def test_decide_keeps_its_action_and_gains_a_diagnosis_key(home: Path, monkeypatch) -> None:
    import mini_ork.recovery.retry_hint as rh
    from mini_ork.recovery import auto_repair as ar

    _seed(home)
    monkeypatch.delenv("MO_AUTO_REPAIR", raising=False)
    monkeypatch.setattr(rh, "load_or_compute", lambda *a, **k: {
        "version": rh.HINT_VERSION, "run_id": RUN, "failed_node": "implementer",
        "retryable": True, "strategy": "resume", "from_node": "implementer",
        "needs_change": {"kind": "code", "summary": "the change needs a revision",
                         "detail": "", "evidence": ""},
        "notes": [], "command": "", "computed_at": "2026-01-01T00:00:00Z",
    })

    decision = ar.decide(home, RUN)

    # The action is unchanged (rule 6: a code revision goes back to the
    # implementer) — the diagnosis is additive only.
    assert decision["action"] == "revise"
    assert isinstance(decision["diagnosis"], dict)
    assert decision["diagnosis"]["layer"] == "unclassified"


def test_apply_copies_the_diagnosis_into_repair_json(home: Path) -> None:
    from mini_ork.recovery import auto_repair as ar

    run_dir = _seed(home)
    decision = ar.decide(home, RUN) | {"action": "human", "reason": "hand over"}

    ar.apply(home, RUN, decision, spawn=False)

    repair = json.loads((run_dir / "repair.json").read_text())
    attempt = repair["attempts"][-1]
    assert attempt["diagnosis"]["layer"] == "unclassified"
    assert attempt["diagnosis"]["rule_id"] == dx.RULE_UNCLASSIFIED
    # ``apply`` (the side-effecting half) is what persists the full record.
    assert json.loads((run_dir / "diagnosis.json").read_text())["run_id"] == RUN


# ── replay is read-only ────────────────────────────────────────────────────


def test_replay_reports_the_layer_aggregate_and_writes_nothing(home: Path) -> None:
    _seed_abstain(home)
    _seed(home, run_id="run-plain")

    before = sorted(p.name for p in (home / "runs" / RUN).iterdir())

    data = dx.replay(home, limit=10)

    assert data["version"] == dx.DIAGNOSIS_VERSION
    by_rule = data["counts"]["by_rule"]
    assert by_rule.get("R1") == 1
    assert data["unclassified_share"] == pytest.approx(0.5)
    assert sorted(p.name for p in (home / "runs" / RUN).iterdir()) == before
    assert not (home / "runs" / RUN / "diagnosis.json").exists()


def test_replay_rows_carry_source_and_evidence(home: Path) -> None:
    _seed_abstain(home)
    _seed(home, run_id="run-plain")

    data = dx.replay(home, limit=10)

    assert data["runs"]
    for row in data["runs"]:
        assert "source" in row
        assert "evidence" in row
        assert row["layer"] in dx.LAYERS
        assert row["routes_to"] in dx.ROUTES
        if row["rule_id"] != dx.RULE_UNCLASSIFIED:
            assert row["source"] == "rule"
            assert row["evidence"]


def test_replay_limit_and_empty_home(home: Path) -> None:
    for i in range(3):
        _seed(home, run_id=f"run-{i}")
    assert len(dx.replay(home, limit=2)["runs"]) == 2
    assert dx.replay(home / "nope", limit=2)["unclassified_share"] == 0.0


# ── CLI ────────────────────────────────────────────────────────────────────


def test_cli_prints_json_without_writing(home: Path, capsys) -> None:
    run_dir = _seed_abstain(home)

    rc = dx.main([RUN, "--home", str(home), "--json", "--no-write"])

    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["layer"] == "engine"
    assert not (run_dir / "diagnosis.json").exists()


def test_cli_out_writes_the_same_payload_as_json(home: Path, capsys, tmp_path: Path) -> None:
    _seed_abstain(home)
    out = tmp_path / "diagnosis-replay.json"

    rc = dx.main(["--home", str(home), "--replay", "--limit", "5", "--json",
                  "--no-write", "--out", str(out)])

    assert rc == 0
    printed = json.loads(capsys.readouterr().out)
    written = json.loads(out.read_text())
    assert written == printed
    assert written["runs"]
    assert all("source" in row and "evidence" in row for row in written["runs"])


def test_cli_requires_a_run_id_unless_replaying(home: Path, capsys) -> None:
    with pytest.raises(SystemExit):
        dx.main(["--home", str(home)])


def test_module_adds_no_cli_verb() -> None:
    """The diagnosis is an API + a module ``python -m`` entry point, not a new verb."""
    from mini_ork.cli import main as cli_main

    assert cli_main.SUBCOMMAND_REGISTRY.get("diagnose") is None
    # ...and the module still carries its own ``python3 -m`` entry point.
    source = (REPO / "mini_ork" / "recovery" / "diagnose.py").read_text(encoding="utf-8")
    assert 'if __name__ == "__main__":' in source


def test_diagnosis_is_not_a_retry_hint_cache_dependency(home: Path) -> None:
    """Writing ``diagnosis.json`` must not invalidate the retry-hint cache."""
    run_dir = _seed_abstain(home)
    deps = retry_hint._cache_dependencies(home, run_dir)
    dx.diagnose(home, RUN, write=True)
    assert not any("diagnosis.json" in key for key in deps)


def test_computed_at_is_iso_utc(home: Path) -> None:
    _seed_abstain(home)
    result = dx.diagnose(home, RUN, write=False)
    assert result["computed_at"].endswith("Z")
    assert int(time.strftime("%Y", time.strptime(result["computed_at"], "%Y-%m-%dT%H:%M:%SZ"))) > 2000
