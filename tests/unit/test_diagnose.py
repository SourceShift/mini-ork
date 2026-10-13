"""``mini_ork.recovery.diagnose`` — the typed, evidence-cited run diagnosis.

Hermetic: every case builds a temp home + temp sqlite so the assertions pin the
*shape* of the live failure (the ``run-1791914481-48337`` abstain run, a
reviewer ``needs_revision``, a ``timeout``) rather than reading any real run
directory. Fixture scaffolding mirrors ``tests/unit/test_retry_hint.py``.
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
          recipe: str = "framework-edit") -> Path:
    run_dir = home / "runs" / run_id
    run_dir.mkdir(parents=True)
    (run_dir / "run_profile.json").write_text(json.dumps({"recipe": recipe}))
    con = sqlite3.connect(home / "state.db")
    con.execute(
        "INSERT OR REPLACE INTO task_runs (id, recipe, status, cost_usd, created_at, "
        "updated_at, ended_at, task_class, kickoff_path, workflow_version) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        (run_id, recipe, status, 0.5, T0, T0 + 100, T0 + 80, "framework_edit",
         "", "latest"),
    )
    con.commit()
    con.close()
    return run_dir


def _event(home: Path, run_id: str, seq: int, event_type: str, payload: dict[str, Any]) -> None:
    con = sqlite3.connect(home / "state.db")
    con.execute(
        "INSERT INTO run_events (event_id, run_id, event_type, payload_json, created_at) "
        "VALUES (?,?,?,?,?)",
        (f"evt-{event_type}-{seq}", run_id, event_type, json.dumps(payload), T0 + seq),
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


# ── R1 — the run-1791914481-48337 shape ────────────────────────────────────


def _seed_abstain(home: Path, run_id: str = RUN) -> Path:
    """The live shape: every node done, the publisher withheld the publish."""
    run_dir = _seed(home, run_id=run_id)
    (run_dir / "publish-abstain.json").write_text(json.dumps({
        "reason": "authored patch did not apply",
        "detail": "the implementer's patch no longer applies to the target tree",
    }))
    (run_dir / "verdict.json").write_text(json.dumps({
        "levels_decision": "abstain",
        "levels": {"applies": "PROVEN", "executes": "n/a", "target": "UNVERIFIED",
                   "preserve": "n/a", "contract": "n/a"},
        "levels_reasons": {"target": "no test exercises the change"},
    }))
    _event(home, run_id, 1, "node_start", {"node_id": "implementer"})
    _event(home, run_id, 2, "node_end",
           {"node_id": "implementer", "finish_reason": "done"})
    _event(home, run_id, 3, "node_start", {"node_id": "publisher", "node_type": "publisher"})
    _event(home, run_id, 4, "node_end",
           {"node_id": "publisher", "node_type": "publisher",
            "finish_reason": "levels_unverified"})
    return run_dir


def test_r1_publish_abstain_is_engine_deliver_publisher(home: Path) -> None:
    _seed_abstain(home)

    result = dx.diagnose(home, RUN, write=False)

    assert result["rule_id"] == "R1"
    assert (result["layer"], result["stage"], result["node"]) == ("engine", "deliver", "publisher")
    assert result["routes_to"] == "reverify"
    assert result["confidence"] > 0.5
    refs = [e["ref"] for e in result["evidence"]]
    assert "publish-abstain.json" in refs
    assert any(e["kind"] == "event" for e in result["evidence"])
    _assert_done_invariant(home, RUN, result)


def test_r1_declines_when_the_publisher_later_finished_done(home: Path) -> None:
    """A retried publisher that PUBLISHED is not a withheld run — read the latest end."""
    run_dir = _seed_abstain(home)
    (run_dir / "publish-abstain.json").unlink()
    _event(home, RUN, 5, "node_start", {"node_id": "publisher", "node_type": "publisher"})
    _event(home, RUN, 6, "node_end",
           {"node_id": "publisher", "node_type": "publisher", "finish_reason": "done"})

    result = dx.diagnose(home, RUN, write=False)

    assert result["rule_id"] != "R1"
    assert result["node"] != "publisher"
    _assert_done_invariant(home, RUN, result)


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
    assert any("finish_reason=timeout" in e["detail"] for e in result["evidence"])
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


# ── R6 — a node that started and never ended ───────────────────────────────


def test_r6_dangling_node_start_is_engine_resume(home: Path) -> None:
    _seed(home)
    _event(home, RUN, 1, "node_start", {"node_id": "implementer", "node_type": "implementer"})

    result = dx.diagnose(home, RUN, write=False)

    assert result["rule_id"] == "R6"
    assert (result["layer"], result["stage"], result["node"]) == ("engine", "execute", "implementer")
    assert result["routes_to"] == "resume"


# ── the unclassified fall-through ──────────────────────────────────────────


def test_no_evidence_run_is_unclassified_at_zero_confidence(home: Path) -> None:
    _seed(home)

    result = dx.diagnose(home, RUN, write=False)

    assert result["rule_id"] == dx.RULE_UNCLASSIFIED
    assert result["layer"] == "unclassified"
    assert result["confidence"] == 0.0
    assert result["evidence"] == []
    assert result["node"] is None


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
    """R1-R7 each cite at least one evidence item; only the fall-through may not."""
    _seed_abstain(home)
    seeded = [
        ("R1", dx.diagnose(home, RUN, write=False)),
    ]
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
