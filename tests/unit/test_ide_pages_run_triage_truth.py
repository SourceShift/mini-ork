"""The run page's triage names the node that actually failed or is blocking.

C3 of the run-page review (``.mini-ork/kickoffs/run-page/rp-c3-triage-truth.md``):
one truthful status. The triage names the first failed node in story order, else
the node awaiting a human, else the node of the open gate — never a node whose
state is ``done``/``passed`` — and every chip string is human display text.

Fixtures are stub :class:`mini_ork.ide_pages.run.Run` objects rendered through
the page's own v2 story-tab builder (:func:`mini_ork.ide_pages.run._build_v2`),
the same read-only path ``run.build`` takes at IDE level 2. No credentials, no
network: every file the triage reads is created under ``tmp_path``.
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any

import pytest

import mini_ork.ide_pages.outcome as O
import mini_ork.ide_pages.run as R

_SNAKE = re.compile(r"[a-z]+_[a-z]+")


# ── fixtures ────────────────────────────────────────────────────────────────

def _node(nid: str, ntype: str, state: str, finish: str = "") -> R.Node:
    ran = state != "pending"
    return R.Node(id=nid, type=ntype, state=state,
                  start=1730000001 if ran else None,
                  end=1730000002 if ran else None, finish=finish)


def _write_files(run_dir: Path, files: dict[str, Any] | None,
                 touch: tuple[str, ...] = ()) -> None:
    for name, payload in (files or {}).items():
        p = run_dir / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("" if payload is None else json.dumps(payload), encoding="utf-8")
    for name in touch:
        p = run_dir / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("", encoding="utf-8")


def _add_pending_review(home: Path, run_id: str, node_id: str, node_type: str) -> None:
    """One pending ``review.after_node`` inbox row — how a parked run waits."""
    from mini_ork.gates import node_review

    conn = node_review._connect(str(home / "state.db"))
    try:
        ts = int(time.time())
        conn.execute(
            "INSERT INTO mo_inbox_gates (gate_id, feature, phase, context_json,"
            " status, enqueued_at, blocks_dispatch_for) VALUES (?,?,?,?,?,?,?)",
            (f"node:{run_id}:{node_id}:{ts}", node_review.FEATURE, node_type,
             json.dumps({"run_id": run_id, "node_id": node_id,
                         "node_type": node_type, "created_at": ts}),
             "pending", ts, run_id),
        )
        conn.commit()
    finally:
        conn.close()


def _make_run(tmp_path: Path, *, rid: str, status: str, nodes: list[R.Node],
              files: dict[str, Any] | None = None, touch: tuple[str, ...] = (),
              task_state: dict[str, Any] | None = None,
              pending_review: tuple[str, str] | None = None) -> R.Run:
    home = tmp_path / "home"
    run_dir = home / "runs" / rid
    run_dir.mkdir(parents=True, exist_ok=True)
    _write_files(run_dir, files, touch)
    if pending_review is not None:
        _add_pending_review(home, rid, pending_review[0], pending_review[1])
    card: dict[str, Any] = {"status": status, "recipe": "code-fix",
                            "title": f"{rid} fixture", "cost_total": 0.0}
    if task_state is not None:
        card["task_state"] = task_state
    row: dict[str, Any] = {"id": rid, "recipe": "code-fix", "status": status,
                           "created_at": 1730000000, "cost_usd": 0.0}
    if status in ("failed", "published", "rolled_back", "not_needed"):
        row["ended_at"] = 1730000600
    return R.Run(id=rid, home=home, run_dir=run_dir, row=row, card=card,
                 nodes=list(nodes), cols=[[n.id for n in nodes]], calls=[],
                 recipe_dir=None, workspace=None, edges=[])


def _story_page(monkeypatch: Any, run: R.Run) -> dict[str, Any]:
    monkeypatch.setenv("MINI_ORK_IDE_SPEC", "2")
    page = R._build_v2(run, "story", {"run": run.id})
    assert page.get("ok") is True, f"the page must build; got error: {page.get('error')}"
    return page


def _triage(page: dict[str, Any]) -> dict[str, Any]:
    for sec in page.get("sections") or []:
        if sec.get("type") == "triage":
            return sec
    pytest.fail("the story page has no triage section")


def _chip_texts(page: dict[str, Any]) -> list[str]:
    """Every chip string the page shows: the header chip line + triage counts."""
    out = [str(c.get("t") or "").strip() for c in page.get("chips") or []]
    for sec in page.get("sections") or []:
        if sec.get("type") == "triage":
            out += [str(c.get("t") or "").strip() for c in sec.get("counts") or []]
    return [t for t in out if t]


# ── the triage names the node that actually failed ──────────────────────────

def test_failed_reviewer_named_not_later_done_nodes(tmp_path, monkeypatch):
    """A reviewer that failed is named even though later nodes ran and passed."""
    nodes = [_node("scout", "researcher", "done", "done"),
             _node("critic", "reviewer", "failed", "error"),
             _node("herald", "publisher", "done", "done"),
             _node("scribe", "verifier", "done", "done")]
    run = _make_run(
        tmp_path, rid="run-triage-failed-reviewer", status="failed", nodes=nodes,
        files={"review-critic.json": {
            "verdict": "needs_change",
            "reasons": ["The diff touches files outside the declared scope"],
            "findings": []}})
    tri = _triage(_story_page(monkeypatch, run))
    assert tri.get("node") == "critic"
    assert "Failed at critic" in tri["text"]
    assert tri.get("reason") == "The diff touches files outside the declared scope"


def test_first_failed_node_in_story_order_is_named(tmp_path, monkeypatch):
    """Two failed nodes: the earlier one in story order is the one named."""
    nodes = [_node("scout", "researcher", "done", "done"),
             _node("critic", "reviewer", "failed", "error"),
             _node("scribe", "verifier", "failed", "error")]
    run = _make_run(tmp_path, rid="run-triage-two-failed", status="failed",
                    nodes=nodes)
    tri = _triage(_story_page(monkeypatch, run))
    assert tri.get("node") == "critic"
    assert "scribe" not in str(tri.get("text") or "")


def test_failed_node_beats_a_node_awaiting_human(tmp_path, monkeypatch):
    """Rule 1 outranks rule 2: a failure exists, so the parked review is not named."""
    nodes = [_node("scout", "researcher", "done", "done"),
             _node("critic", "reviewer", "failed", "error")]
    run = _make_run(
        tmp_path, rid="run-triage-failed-and-gated", status="failed", nodes=nodes,
        pending_review=("scout", "researcher"))
    assert R._pending_node_ids(run) == ["scout"]
    tri = _triage(_story_page(monkeypatch, run))
    assert tri.get("node") == "critic"


def test_parked_run_names_the_pending_review_node(tmp_path, monkeypatch):
    """A parked run's triage names the node waiting for a human decision."""
    nodes = [_node("scout", "researcher", "done", "done"),
             _node("critic", "reviewer", "pending")]
    run = _make_run(
        tmp_path, rid="run-triage-parked", status="reviewing", nodes=nodes,
        touch=(".review-pause",),
        task_state={"state": "needs_you", "detail": ""},
        pending_review=("critic", "reviewer"),
        files={"review-critic.json": {"verdict": "awaiting_review",
                                      "reasons": [], "findings": []}})
    assert R._is_parked_run(run) and R._pending_node_ids(run) == ["critic"]
    tri = _triage(_story_page(monkeypatch, run))
    assert tri.get("node") == "critic"


def test_pending_review_on_a_done_node_is_never_named(tmp_path, monkeypatch):
    """c:0 is absolute: a parked review whose own node already finished is not named."""
    nodes = [_node("scout", "researcher", "done", "done")]
    run = _make_run(
        tmp_path, rid="run-triage-parked-done-node", status="reviewing", nodes=nodes,
        touch=(".review-pause",),
        task_state={"state": "needs_you", "detail": ""},
        pending_review=("scout", "researcher"))
    assert R._is_parked_run(run) and R._pending_node_ids(run) == ["scout"]
    tri = _triage(_story_page(monkeypatch, run))
    assert tri.get("node") != "scout"
    assert "scout" not in str(tri.get("text") or "")


# ── chip, triage node and reason agree on the same failure ─────────────────

def test_failed_db_status_and_retry_gate_agree(tmp_path, monkeypatch):
    """DB status failed + task_state needs_you: chip, node and reason all say failed."""
    nodes = [_node("scout", "researcher", "done", "done"),
             _node("critic", "reviewer", "failed", "error"),
             _node("herald", "publisher", "done", "done")]
    run = _make_run(
        tmp_path, rid="run-triage-agreement", status="failed", nodes=nodes,
        task_state={"state": "needs_you", "detail": ""},
        files={"review-critic.json": {"verdict": "needs_change",
                                      "reasons": ["The retry broke the parser"],
                                      "findings": []}})
    page = _story_page(monkeypatch, run)
    tri = _triage(page)
    chips = [str(c.get("t") or "") for c in page.get("chips") or []]
    assert "Failed" in chips                       # chip agrees with DB status
    assert tri.get("node") == "critic"             # never a later done node
    assert tri.get("reason") == "The retry broke the parser"
    assert tri["text"].startswith("Failed at critic")


# ── no guessing, no blanking ────────────────────────────────────────────────

def test_failed_run_without_retry_hint_still_has_triage_text(tmp_path, monkeypatch):
    """No retry-hint.json: the failed node is still named and the box says something."""
    nodes = [_node("scout", "researcher", "done", "done"),
             _node("critic", "reviewer", "failed", "error")]
    run = _make_run(tmp_path, rid="run-triage-no-hint", status="failed", nodes=nodes)
    assert not (run.run_dir / "retry-hint.json").exists()
    tri = _triage(_story_page(monkeypatch, run))
    assert str(tri.get("text") or "").strip()
    assert tri.get("node") == "critic"


def test_no_failed_or_blocked_node_omits_the_node_field(tmp_path, monkeypatch):
    """Every node is done: the triage omits the node rather than guessing one."""
    nodes = [_node("scout", "researcher", "done", "done"),
             _node("herald", "publisher", "done", "done")]
    run = _make_run(tmp_path, rid="run-triage-all-done", status="failed",
                    nodes=nodes)
    page = _story_page(monkeypatch, run)
    tri = _triage(page)
    assert not str(tri.get("node") or "").strip()
    assert "scout" not in str(tri.get("text") or "")
    assert "herald" not in str(tri.get("text") or "")
    # The DB status is still represented honestly on the chip line.
    assert any("fail" in t.lower() for t in
               [str(c.get("t") or "") for c in page.get("chips") or []])


# ── chips carry human words, never a database enum ──────────────────────────

@pytest.mark.parametrize("builder", ["failed", "parked", "published"])
def test_no_chip_text_is_snake_case(tmp_path, monkeypatch, builder):
    """Chip text (header line + triage counts) is display text, not an enum."""
    if builder == "failed":
        run = _make_run(
            tmp_path, rid="run-triage-chip-failed", status="failed",
            nodes=[_node("scout", "researcher", "done", "done"),
                   _node("critic", "reviewer", "failed", "error")],
            files={"review-critic.json": {"verdict": "needs_change",
                                          "reasons": ["fix incomplete"],
                                          "findings": []},
                   "retry-hint.json": {"failed_node": "critic",
                                       "notes": ["Reviewer returned needs_change"],
                                       "retryable": True}})
    elif builder == "parked":
        run = _make_run(
            tmp_path, rid="run-triage-chip-parked", status="reviewing",
            nodes=[_node("scout", "researcher", "done", "done"),
                   _node("critic", "reviewer", "pending")],
            touch=(".review-pause",),
            task_state={"state": "needs_you", "detail": ""})
    else:
        run = _make_run(
            tmp_path, rid="run-triage-chip-published", status="published",
            nodes=[_node("scout", "researcher", "done", "done")],
            files={"verdict.json": {"verdict": "pass"}})
    texts = _chip_texts(_story_page(monkeypatch, run))
    assert texts, "the page must show chips"
    leaked = [t for t in texts if _SNAKE.search(t)]
    assert not leaked, f"a database enum reached a chip: {leaked}"


def test_state_chip_is_human_words(tmp_path, monkeypatch):
    """``needs_you`` is drawn as "Needs you", not as the raw task-state enum."""
    run = _make_run(
        tmp_path, rid="run-triage-state-chip", status="reviewing",
        nodes=[_node("scout", "researcher", "done", "done"),
               _node("critic", "reviewer", "pending")],
        touch=(".review-pause",),
        task_state={"state": "needs_you", "detail": ""})
    page = _story_page(monkeypatch, run)
    state_chip = str(page["chips"][0].get("t") or "").strip()
    assert state_chip == O.display_word("needs_you") == "Needs you"
