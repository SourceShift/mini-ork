"""``mini_ork.ide_pages.outcome`` — one run's outcome and its next action.

The fixture pattern mirrors ``test_ide_pages_run.py``: a temp mini-ork home with
an initialised DB and a ``task_runs`` row, then ``run_page._load`` builds the
``Run`` the page modules read.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from mini_ork.ide_pages import outcome
from mini_ork.ide_pages import run as run_page
from mini_ork.stores import migrate as mig

REPO = Path(__file__).resolve().parents[2]
RUN = "run-1791000000-abc123"
T0 = 1_791_000_000


@pytest.fixture
def home(tmp_path: Path) -> Path:
    h = tmp_path / "proj" / ".mini-ork"
    h.mkdir(parents=True)
    rc, _out, err = mig.init_db(db=str(h / "state.db"), root=str(REPO))
    assert rc == 0, err
    return h


def _seed(home: Path, *, status: str = "failed") -> Path:
    run_dir = home / "runs" / RUN
    run_dir.mkdir(parents=True, exist_ok=True)
    kickoff = home / "kickoffs" / "demo.md"
    kickoff.parent.mkdir(parents=True, exist_ok=True)
    kickoff.write_text("# Make the demo pass\n\nDetails.\n")
    con = sqlite3.connect(home / "state.db")
    con.execute(
        "INSERT INTO task_runs (id, recipe, status, cost_usd, created_at, updated_at, ended_at, "
        "task_class, kickoff_path, workflow_version, trace_id) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (RUN, "demo-recipe", status, 0.5, T0, T0 + 120, T0 + 100, "demo", str(kickoff),
         "latest", "tr-demo-1"))
    con.commit()
    con.close()
    return run_dir


def _run(home: Path):
    run = run_page._load(home, RUN)
    assert run is not None
    return run


def _snapshot(root: Path) -> dict[str, int]:
    """Every path under ``root`` with its mtime — the read-only witness."""
    return {str(p.relative_to(root)): p.stat().st_mtime_ns for p in root.rglob("*")}


def test_db_status_beats_verdict_json(home: Path) -> None:
    """A failed row is never "done", even when verdict.json says pass:true."""
    run_dir = _seed(home, status="failed")
    (run_dir / "verdict.json").write_text('{"verdict": "pass"}')
    out = outcome.resolve(_run(home))
    assert out["state"] == "failed"
    assert out["tone"] == "red"
    assert "verified" not in out["text"]


def test_lane_hint_offers_a_lane_switch(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A lane change names the switch, its target, and the reason in the confirm."""
    _seed(home, status="failed")
    hint = {
        "run_id": RUN,
        "failed_node": "codex_lens",
        "retryable": True,
        "strategy": "resume",
        "from_node": "codex_lens",
        "needs_change": {
            "kind": "lane",
            "summary": "minimax is out of quota",
            "detail": "",
            "lane": "minimax",
            "alias": "codex_lens",
            "provider": "minimax",
            "error_kind": "quota",
            "nodes": ["codex_lens"],
            "suggestions": [{"lane": "deepseek", "reason": "19 ok"}],
            "code": False,
        },
        "notes": [],
        "command": "",
        "computed_at": "2026-01-01T00:00:00Z",
    }
    import mini_ork.recovery.retry_hint as retry_hint

    monkeypatch.setattr(retry_hint, "load_or_compute", lambda *a, **k: hint)
    out = outcome.resolve(_run(home))
    assert out["state"] == "failed"
    labels = [a["label"] for a in out["actions"]]
    assert "Switch codex_lens → deepseek" in labels
    assert "Retry on the same lane" in labels
    switch = next(a for a in out["actions"] if a["label"] == "Switch codex_lens → deepseek")
    assert switch["do"]["cli"] == ["board", "retry", RUN, "--lane", "codex_lens=deepseek"]
    assert "19 ok" in switch["do"]["confirm"]
    assert switch["kind"] == "primary"
    same = next(a for a in out["actions"] if a["label"] == "Retry on the same lane")
    assert same["do"]["cli"] == ["board", "retry", RUN]


def test_pending_gate_is_needs_you_with_a_decision_callout(home: Path) -> None:
    """A pending retry gate: Approve/Reject carrying the inbox id, plus the fix steps."""
    run_dir = _seed(home, status="failed")
    from mini_ork.gates import oversight_inbox

    iid = oversight_inbox.enqueue(
        gate_id="retry_precondition", feature=RUN, phase="retry",
        context={
            "hint": {"run_id": RUN,
                     "needs_change": {"kind": "code", "summary": "the reviewer rejected it",
                                      "detail": "the change is wrong"}},
            "steps": ["start a revision run"],
        },
        db_path=str(home / "state.db"))
    (run_dir / "retry-gate.json").write_text(
        json.dumps({"inbox_id": iid, "blocks_dispatch_for": ""}))

    out = outcome.resolve(_run(home))
    assert out["state"] == "needs_you"
    assert out["tone"] == "orange" and out["icon"] == "?"
    assert out["text"] == "the reviewer rejected it"
    assert len(out["callouts"]) == 1
    callout = out["callouts"][0]
    assert callout["type"] == "callout"
    assert callout["title"] == "This run needs your decision"
    assert callout["tone"] == "orange"
    assert "1. " in callout["text_md"]  # the numbered fix steps
    assert [a["label"] for a in callout["actions"]] == ["Approve retry", "Leave it"]
    assert callout["actions"][0]["do"]["cli"] == ["board", "gate", "approve", str(iid)]
    assert callout["actions"][1]["do"]["cli"] == ["board", "gate", "reject", str(iid)]
    assert callout["actions"][0]["kind"] == "primary"


def test_published_levels_add_badges_and_verified(home: Path) -> None:
    """Level badges come from run-verdict.json; " · verified" only when it passes."""
    run_dir = _seed(home, status="published")
    (run_dir / "run-verdict.json").write_text(json.dumps({
        "verdict": "pass",
        "levels": {"applies": "PROVEN", "executes": "UNVERIFIED", "target": "PROVEN"},
        "levels_decision": "publish",
    }))
    (run_dir / "verdict.json").write_text('{"verdict": "pass"}')
    out = outcome.resolve(_run(home))
    assert out["state"] == "done" and out["tone"] == "green" and out["icon"] == "✓"
    assert out["text"] == "Published · verified"
    counts = {c["t"]: c["c"] for c in out["counts"]}
    assert counts["applies PROVEN"] == "green"
    assert counts["executes UNVERIFIED"] == "yellow"
    assert counts["target PROVEN"] == "green"

    # Nothing passes → no " · verified", but the badges still show.
    (run_dir / "run-verdict.json").write_text(json.dumps({
        "levels": {"applies": "UNVERIFIED"}, "levels_decision": "abstain"}))
    (run_dir / "verdict.json").write_text('{"verdict": "fail"}')
    out2 = outcome.resolve(_run(home))
    assert out2["text"] == "Published"
    assert "applies UNVERIFIED" in {c["t"] for c in out2["counts"]}


def test_running_offers_stop_and_kill(home: Path) -> None:
    _seed(home, status="executing")
    out = outcome.resolve(_run(home))
    assert out["state"] == "running" and out["icon"] == "●"
    assert out["text"].startswith("Running")
    assert [a["label"] for a in out["actions"]] == ["Stop", "Kill"]
    assert out["actions"][0]["do"]["cli"] == ["board", "stop", RUN]
    assert out["actions"][0]["do"]["confirm"]
    assert out["actions"][1]["do"]["cli"] == ["board", "kill", RUN]


def test_resolve_is_read_only(home: Path) -> None:
    """Nothing under the run dir changes — no hint cache, no diffstat."""
    run_dir = _seed(home, status="failed")
    (run_dir / "verifier_test.json").write_text(
        '{"verifier": "test", "pass": false, "error_summary": "boom"}')
    run = _run(home)
    before = _snapshot(run_dir)
    outcome.resolve(run)
    assert _snapshot(run_dir) == before


def test_bare_failed_detail_never_guesses_a_node(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """task_state's bare "Failed" fallback names no node, and the hint's own node
    is a best-effort guess — the card must not render "Failed at <guess>": the
    text stays "Failed" and the hint's summary carries the detail."""
    _seed(home, status="failed")
    import mini_ork.recovery.retry_hint as retry_hint

    monkeypatch.setattr(retry_hint, "load_or_compute",
                        lambda *a, **k: {"retryable": False, "from_node": "reviewer",
                                         "needs_change": {"kind": "code", "summary": "rejected"}})
    run = _run(home)
    run.card["detail"] = "Failed"
    out = outcome.resolve(run)
    assert out["text"] == "Failed"
    assert "rejected. The change itself must be revised." in out["detail"]


def test_explicit_level_decision_beats_a_recipe_verdict(home: Path) -> None:
    """An abstain/refute level decision is never "verified", whatever verdict.json says."""
    run_dir = _seed(home, status="published")
    (run_dir / "run-verdict.json").write_text(json.dumps({
        "levels": {"applies": "UNVERIFIED"}, "levels_decision": "abstain"}))
    (run_dir / "verdict.json").write_text('{"verdict": "pass"}')
    assert outcome.resolve(_run(home))["text"] == "Published"


def test_levels_stamped_into_verdict_json_still_show(home: Path) -> None:
    """execute stamps levels into verdict.json when the recipe does not own it."""
    run_dir = _seed(home, status="published")
    (run_dir / "verdict.json").write_text(json.dumps({
        "verdict": "pass", "levels": {"applies": "PROVEN"}, "levels_decision": "publish"}))
    out = outcome.resolve(_run(home))
    assert out["text"] == "Published · verified"
    assert "applies PROVEN" in {c["t"] for c in out["counts"]}
