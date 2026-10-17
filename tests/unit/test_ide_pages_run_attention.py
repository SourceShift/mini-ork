"""The run page's needs-you lane (C5) and a gate callout that explains itself (C4).

Every fixture is a throwaway mini-ork home under ``tmp_path``: a sqlite
``state.db`` with one ``task_runs`` row (plus inbox rows where a scenario needs
them) and a ``runs/<id>/`` directory with the scenario's files. Nothing here
writes outside ``tmp_path``.

The page entry is ``mini_ork.ide_pages.run.build(home, tab, {"run": id})`` with
``MINI_ORK_IDE_SPEC=2`` — the same surface ``board page run`` renders.
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
from pathlib import Path

import pytest

from mini_ork.ide_pages import run as run_page

_TASK_RUNS_DDL = """
CREATE TABLE IF NOT EXISTS task_runs (
  id TEXT PRIMARY KEY,
  status TEXT,
  recipe TEXT,
  created_at TEXT,
  updated_at TEXT,
  trace_id TEXT,
  ended_at TEXT,
  kickoff_path TEXT,
  plan_path TEXT,
  notes TEXT
)
"""


def _iso(epoch: int) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch))


@pytest.fixture(autouse=True)
def _page_env(monkeypatch):
    monkeypatch.setenv("MINI_ORK_IDE_SPEC", "2")
    monkeypatch.setenv("MO_SERVE_PORT", "1")  # nothing listens here: no serve url
    monkeypatch.delenv("MINI_ORK_HOME", raising=False)
    monkeypatch.delenv("MINI_ORK_DB", raising=False)
    monkeypatch.delenv("MO_REVIEW_BEFORE_MERGE", raising=False)
    monkeypatch.delenv("MO_REVIEW_AFTER_NODES", raising=False)
    monkeypatch.delenv("MO_REVIEW_TIER", raising=False)


def _make_home(tmp_path: Path, run_id: str, *, status: str) -> tuple[Path, Path]:
    home = tmp_path / "home"
    run_dir = home / "runs" / run_id
    run_dir.mkdir(parents=True)
    now = int(time.time())
    con = sqlite3.connect(home / "state.db")
    con.execute(_TASK_RUNS_DDL)
    con.execute(
        "INSERT INTO task_runs (id, status, recipe, created_at, updated_at)"
        " VALUES (?, ?, ?, ?, ?)",
        (run_id, status, "", _iso(now - 600), _iso(now)),
    )
    con.commit()
    con.close()
    return home, run_dir


def _enqueue_inbox(home: Path, *, gate_id: str, feature: str, context: dict) -> int:
    from mini_ork.gates import oversight_inbox

    return oversight_inbox.enqueue(
        gate_id, feature, "retry", context, db_path=str(home / "state.db")
    )


def _open_retry_gate(home: Path, run_dir: Path, *, hint: dict) -> int:
    iid = _enqueue_inbox(
        home, gate_id="retry_precondition", feature="retry.fix",
        context={"hint": hint},
    )
    (run_dir / "retry-gate.json").write_text(
        json.dumps({"inbox_id": iid}), encoding="utf-8"
    )
    return iid


def _ask(run_dir: Path, ask_id: str, question: str) -> None:
    asks = run_dir / "asks"
    asks.mkdir(exist_ok=True)
    (asks / f"{ask_id}.json").write_text(
        json.dumps({"ask_id": ask_id, "question": question}), encoding="utf-8"
    )


def _persist_hint(run_dir: Path, hint: dict) -> Path:
    from mini_ork.recovery.retry_hint import HINT_VERSION

    path = run_dir / "retry-hint.json"
    path.write_text(json.dumps({"version": HINT_VERSION, **hint}), encoding="utf-8")
    # Fresh only while strictly newer than every dependency file.
    stamp = time.time() + 60
    os.utime(path, (stamp, stamp))
    return path


def _build(home: Path, run_id: str, tab: str = "story") -> dict:
    page = run_page.build(Path(home), tab, {"run": run_id})
    assert page.get("ok") is True, f"page build failed: {page.get('error')}"
    return page


def _lane(page: dict) -> dict:
    """The single needs-you lane: a section carrying attention-shaped items."""
    found = []
    for section in page.get("sections") or []:
        items = section.get("items")
        if not isinstance(items, list) or not items:
            continue
        if all(isinstance(i, dict) for i in items) and any(
            str(i.get("id") or "").strip() and str(i.get("t") or "").strip()
            and isinstance(i.get("acts"), list)
            for i in items
        ):
            found.append(section)
    assert len(found) == 1, f"expected one attention lane, found {len(found)}"
    return found[0]


def _items(page: dict) -> list[dict]:
    return _lane(page)["items"]


def _blob(item: dict) -> str:
    return f"{item.get('t') or ''}\n{item.get('sub') or ''}"


def _cli_count(page: dict, prefix: tuple[str, ...]) -> int:
    want = list(prefix)

    def walk(obj):
        if isinstance(obj, dict):
            cli = obj.get("cli")
            if isinstance(cli, list):
                yield [str(a) for a in cli]
            for value in obj.values():
                yield from walk(value)
        elif isinstance(obj, list):
            for value in obj:
                yield from walk(value)

    return sum(1 for cli in walk(page) if cli[: len(want)] == want)


# ── C5: one needs-you lane ──────────────────────────────────────────────────

def test_parked_run_yields_one_lane_with_the_review_actions(tmp_path):
    run_id = "run-1799050001-parked"
    home, run_dir = _make_home(tmp_path, run_id, status="reviewing")
    (run_dir / ".review-pause").write_text(
        json.dumps({"waiting_node": "review-1"}), encoding="utf-8")
    _enqueue_inbox(
        home, gate_id=f"node:{run_id}:review-1", feature="review.after_node",
        context={"node_id": "review-1"})

    page = _build(home, run_id)
    lane = _lane(page)
    assert page["sections"][0] is lane, "the lane is the top of the tab"
    items = _items(page)
    assert len(items) == 1, "one decision (the parked review), one block"
    item = items[0]
    assert str(item.get("id") or "").strip()
    assert str(item.get("t") or "").strip()
    assert any(
        (b.get("do") or {}).get("cli", [])[:2] == ["approvals", "approve"]
        for b in item["acts"]
    ), "the review decision's Approve control rides the item"
    # One decision, one block: the control appears once and the old parked
    # callout is gone from the tab.
    assert _cli_count(page, ("approvals", "approve")) == 1
    titles = [str(s.get("title") or "") for s in page["sections"]]
    assert "Waiting for your review" not in titles
    assert "This run needs your decision" not in titles


def test_lane_tops_every_v2_tab_with_stable_ids(tmp_path):
    run_id = "run-1799050002-tops"
    home, run_dir = _make_home(tmp_path, run_id, status="reviewing")
    (run_dir / ".review-pause").write_text(
        json.dumps({"waiting_node": "review-1"}), encoding="utf-8")
    _enqueue_inbox(
        home, gate_id=f"node:{run_id}:review-1", feature="review.after_node",
        context={"node_id": "review-1"})

    for tab in ("story", "graph", "questions"):
        page = _build(home, run_id, tab=tab)
        lane = _lane(page)
        assert page["sections"][0] is lane, f"lane is not the top of {tab}"
        for item in _items(page):
            assert str(item.get("id") or "").strip()
            assert str(item.get("t") or "").strip()
            assert isinstance(item.get("acts"), list)
    first = [str(i.get("id")) for i in _items(_build(home, run_id))]
    second = [str(i.get("id")) for i in _items(_build(home, run_id))]
    assert first == second and all(s.strip() for s in first)


def test_open_gate_and_unanswered_ask_are_two_items(tmp_path):
    run_id = "run-1799050003-gateask"
    home, run_dir = _make_home(tmp_path, run_id, status="failed")
    _open_retry_gate(home, run_dir, hint={"run_id": run_id})
    _ask(run_dir, "ask-1", "Should we pin the widget SDK to v2 for this run?")

    items = _items(_build(home, run_id))
    assert len(items) == 2, "the open gate and the ask are two items"
    for item in items:
        assert str(item.get("id") or "").strip()
        assert str(item.get("t") or "").strip()
        assert item["acts"], "each item carries its own buttons"
    ask_idx = next(i for i, item in enumerate(items) if "widget SDK" in _blob(item))
    gate_idx = 1 - ask_idx
    assert gate_idx < ask_idx, "the open gate precedes the open ask (fixed order)"


def test_healthy_running_run_has_no_lane(tmp_path):
    run_id = "run-1799050004-healthy"
    home, _run_dir = _make_home(tmp_path, run_id, status="executing")
    page = _build(home, run_id)
    titles = [str(s.get("title") or "") for s in page["sections"]]
    assert "Needs you" not in titles, "a healthy run renders no needs-you lane"
    assert page["sections"][0]["type"] == "triage"


def test_cost_pause_is_an_attention_item(tmp_path):
    run_id = "run-1799050005-cost"
    home, run_dir = _make_home(tmp_path, run_id, status="executing")
    (run_dir / ".cost-pause").write_text("", encoding="utf-8")
    items = _items(_build(home, run_id))
    assert any("cost" in _blob(i).lower() or "budget" in _blob(i).lower()
               for i in items)
    assert all(str(i.get("id") or "").strip() for i in items)


def test_merge_approval_is_an_attention_item(tmp_path, monkeypatch):
    run_id = "run-1799050006-merge"
    home, _run_dir = _make_home(tmp_path, run_id, status="published")
    monkeypatch.setenv("MO_REVIEW_BEFORE_MERGE", "required")
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    (home / "worktrees").mkdir()
    (home / "worktrees" / f"{run_id}.json").write_text(
        json.dumps({
            "run_id": run_id, "path": str(worktree), "branch": "wt/spec",
            "base_branch": "main", "base_sha": "deadbeefdeadbeef",
            "project": str(tmp_path), "adopted": False, "clean_at_start": True,
            "home": str(home),
        }),
        encoding="utf-8",
    )
    items = _items(_build(home, run_id))
    merge_items = [i for i in items if "merge" in _blob(i).lower()]
    assert merge_items
    assert merge_items[0]["acts"]


# ── C4: every gate/retry callout explains itself ────────────────────────────

def test_gate_item_names_strategy_node_command_and_cost(tmp_path):
    run_id = "run-1799050007-gate"
    home, run_dir = _make_home(tmp_path, run_id, status="failed")
    command = f"mini-ork recover {run_id} --strategy verify"
    _open_retry_gate(home, run_dir, hint={
        "run_id": run_id,
        "failed_node": "check-node-4",
        "from_node": "check-node-4",
        "retryable": True,
        "strategy": "verify",
        "needs_change": {
            "kind": "environment",
            "summary": "The check-node-4 check could not reach something it needs",
            "detail": "MINI_ORK_TOKEN is not set",
            "evidence": "verifier_check-node-4.json",
        },
        "notes": [],
        "command": command,
        "estimated_cost_usd": 0.42,
    })
    items = _items(_build(home, run_id))
    assert len(items) == 1
    blob = _blob(items[0])
    assert blob.strip(), "no gate callout ships with empty text"
    assert "check-node-4" in blob, "the text names the from-node"
    assert "verif" in blob.lower(), "the text names the strategy (Re-verify)"
    assert command in blob, "the text states the exact command it will run"
    assert "Estimated cost" in blob, "a known estimate is stated"
    assert items[0]["acts"], "the gate item keeps its decision button"
    # One decision, one block: the retry control appears once on the tab.
    assert _cli_count(_build(home, run_id), ("board", "gate")) == 1


def test_gate_falls_back_to_retry_hint_when_fix_steps_empty(tmp_path):
    run_id = "run-1799050008-fallback"
    home, run_dir = _make_home(tmp_path, run_id, status="failed")
    command = f"mini-ork recover {run_id} --strategy verify"
    # The gate's own hint has no needs_change, so fix_steps renders empty…
    _open_retry_gate(home, run_dir, hint={"run_id": run_id, "strategy": "verify"})
    # …and the persisted retry-hint.json is the fallback source.
    _persist_hint(run_dir, {
        "run_id": run_id,
        "status": "failed",
        "failed_node": "impl-node-7",
        "from_node": "impl-node-7",
        "retryable": True,
        "strategy": "verify",
        "notes": ["Bump the widget budget to 40 dollars"],
        "command": command,
    })
    (run_dir / "run-verdict.json").write_text(
        json.dumps({"levels": {
            "applies": "PROVEN", "executes": "PROVEN", "target": "UNVERIFIED",
            "preserve": "PROVEN", "contract": "PROVEN",
        }}),
        encoding="utf-8",
    )
    items = _items(_build(home, run_id))
    assert len(items) == 1
    blob = _blob(items[0])
    assert "impl-node-7" in blob, "the fallback names the hint's failed_node"
    assert "Bump the widget budget" in blob, "the fallback surfaces notes[0]"
    assert command in blob, "the fallback states the retry command"
    assert "target" in blob, "the verification gate names the unverified level"
    assert "PROVEN" in blob, "it says publishing requires every level PROVEN"


def test_broken_hint_still_yields_non_empty_gate_text(tmp_path):
    run_id = "run-1799050009-broken"
    home, run_dir = _make_home(tmp_path, run_id, status="failed")
    _open_retry_gate(home, run_dir, hint={"run_id": run_id})
    (run_dir / "retry-hint.json").write_text("{not valid json", encoding="utf-8")
    _ask(run_dir, "ask-1", "Should we pin the widget SDK to v2 for this run?")

    page = _build(home, run_id)
    assert page.get("ok") is True, "a broken part must cost that part, never the page"
    gate_items = [i for i in _items(page) if "widget SDK" not in _blob(i)]
    assert gate_items, "the gate item survives the broken hint"
    assert str(gate_items[0].get("t") or "").strip()
    assert gate_items[0]["acts"]


def test_v2_tabs_and_outcome_tab_name_are_untouched():
    # The tab bar is main's: the needs-you lane adds a section to whichever
    # tab is open, never a tab of its own.
    assert [key for key, _ in run_page.V2_TABS] == [
        "dag", "outcome", "checks", "kickoff", "agents",
        "learnings", "artifacts",
    ]
    assert callable(run_page._story_tab)
    assert callable(run_page._outcome_tab)
