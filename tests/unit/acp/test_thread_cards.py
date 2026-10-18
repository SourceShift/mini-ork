"""Unit tests for the pure thread-card builders (``mini_ork.acp.thread_cards``).

Every builder is deterministic and no-raise-by-contract: malformed payloads
yield a minimal card, never an exception, so the live projection and the
replay projection reconstruct the same payload from run data.
"""
from __future__ import annotations

import asyncio
import copy
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from acp.schema import ToolCallStart  # noqa: E402

from mini_ork.acp.agent import MiniOrkAcpAgent  # noqa: E402
from mini_ork.acp.thread_cards import (  # noqa: E402
    CARD_KEY,
    CARD_VERSION,
    build_card,
    failure_card,
    kickoff_card,
    node_card,
    review_card,
    run_card,
    with_meta,
)

RUN = "run-cards-1"

_SNAPSHOT = {
    "status": "failed",
    "events": [
        {
            "event_type": "node_start",
            "payload_json": {
                "node_id": "impl-1",
                "node_type": "implementer",
                "model_lane": "fast",
            },
        },
        {
            "event_type": "node_end",
            "payload_json": json.dumps(
                {
                    "node_id": "impl-1",
                    "node_type": "implementer",
                    "verdict": "refused",
                    "finish_reason": "error",
                }
            ),
        },
    ],
    "llm_calls": [],
}

_HINT = {
    "version": 1,
    "run_id": RUN,
    "failed_node": "implementer-1",
    "retryable": True,
    "stderr": "STDERR_MARKER_BOOM: exploded",
    "needs_change": {"kind": "code", "summary": "check failed"},
}

_KICKOFF = """# TASK
Fix the thread card launcher.

## IN SCOPE
- `mini_ork/acp/definitely_missing_zz.py`

## PROVES SUCCESS
- [ ] pytest passes
- [x] py_compile clean
"""


def _card(update) -> dict | None:
    meta = update.model_dump(mode="json", by_alias=True, exclude_none=True).get("_meta")
    return meta.get(CARD_KEY) if isinstance(meta, dict) else None


# ── envelope ────────────────────────────────────────────────────────────────


def test_build_card_defaults_and_caps():
    card = build_card(kind="notice", card_id="c-1", revision=0)
    assert card["v"] == CARD_VERSION == 1
    assert card["parent"] is None
    assert card["header"] == {
        "mark": "none", "title": "", "meta": "", "detail": "", "tag": "",
    }
    assert card["blocks"] == [] and card["actions"] == []
    assert card["suppress_stock"] is True
    assert card["fallback_text"] == ""

    clamped = build_card(
        kind="run",
        card_id="c",
        revision=2,
        header={"title": "t" * 350, "zzz": "ignored"},
        blocks=[{"type": "text", "text": "b%d" % i} for i in range(20)],
        actions=[{"weird": 1}],
    )
    assert len(clamped["header"]["title"]) == 200
    assert "zzz" not in clamped["header"]
    assert len(clamped["blocks"]) == 12
    assert clamped["actions"] == [{"weird": 1}]


def test_build_card_size_cap_keeps_fallback_text():
    card = build_card(
        kind="run",
        card_id="c",
        revision=0,
        header={"title": "t"},
        blocks=[{"type": "text", "text": "y" * 7000} for _ in range(12)],
        fallback_text="fb",
    )
    assert len(json.dumps(card)) <= 64 * 1024
    assert card["fallback_text"] == "fb"


def test_build_card_fallback_title_and_unknown_kind():
    card = build_card(kind="mystery", card_id="c", revision=0, header={"title": "Hi"})
    assert card["kind"] == "mystery"
    assert card["fallback_text"] == "Hi"


# ── with_meta ───────────────────────────────────────────────────────────────


def test_with_meta_round_trips_without_mutation():
    card = build_card(kind="node", card_id="n1", revision=0)
    start = ToolCallStart(
        session_update="tool_call", tool_call_id="n1", title="node", status="in_progress"
    )
    out = with_meta(start, card)
    assert out is not start
    assert _card(out) == card
    assert start.field_meta is None
    assert _card(start) is None


def test_with_meta_returns_non_supporting_update_unchanged():
    card = build_card(kind="run", card_id="c", revision=0)
    plain = {"tool_call_id": "n1"}
    assert with_meta(plain, card) == {"tool_call_id": "n1"}
    assert plain == {"tool_call_id": "n1"}


# ── run / node ──────────────────────────────────────────────────────────────


def test_run_card_steps_omit_invented_cost_and_model():
    snap = copy.deepcopy(_SNAPSHOT)
    snap["llm_calls"] = [{"cost_usd": 0.5, "total_tokens": 1000}]
    card = run_card(snap, run_id=RUN, recipe="code-fix", worktree={"branch": "wt/x"})
    assert card["id"] == f"{RUN}:parent"
    assert card["header"]["detail"] == "failed"
    assert card["header"]["tag"] == "code-fix"
    rows = []
    for block in card["blocks"]:
        if block.get("type") == "steps":
            rows.extend(block.get("steps") or [])
        if block.get("type") == "text":
            raise AssertionError("no summary ⇒ no text block")
    row = next(r for r in rows if r["id"] == "impl-1")
    assert row["label"] == "implementer"
    assert row["lane"] == "fast"
    assert row["status"] == "refused"
    assert "cost" not in row and "model" not in row
    assert "wt/x" in json.dumps(card)


def test_run_card_tolerates_empty_snapshot():
    card = run_card({}, run_id=RUN, recipe="docs")
    assert card["kind"] == "run"
    assert card["header"]["title"] == f"run {RUN}"


def test_node_card_dict_and_json_string_payloads():
    start = node_card(_SNAPSHOT["events"][0], run_id=RUN, recipe="code-fix")
    assert start["id"] == "impl-1"
    assert start["parent"] == f"{RUN}:parent"
    assert start["status"] == "in_progress"
    assert start["suppress_stock"] is True

    end = node_card(_SNAPSHOT["events"][1], run_id=RUN, recipe="code-fix", revision=4)
    assert end["status"] == "refused"
    assert end["revision"] == 4

    no_verdict = node_card(
        {"event_type": "node_end", "payload_json": {"node_id": "n"}},
        run_id=RUN,
        recipe="code-fix",
    )
    assert no_verdict["status"] == "completed"


def test_node_card_malformed_payloads_never_raise():
    for bad in (
        {"event_type": "node_start", "payload_json": "{not json"},
        {"event_type": "node_start"},
        {"event_type": "node_end", "payload_json": {"node_id": ["not", "a", "str"]}},
        {},
    ):
        assert node_card(bad, run_id=RUN, recipe="code-fix")["kind"] == "node"


# ── kickoff / failure / review ──────────────────────────────────────────────


def test_kickoff_card_parses_task_scope_and_checklist():
    card = kickoff_card("/tmp/kickoff-drafts/d-1.md", _KICKOFF, recipe="code-fix")
    assert card["kind"] == "kickoff" and card["status"] == "pending"
    assert card["id"] == "d-1"
    blob = json.dumps(card)
    assert "Fix the thread card launcher" in blob
    assert "definitely_missing_zz" in blob
    assert "pytest passes" in blob and "py_compile clean" in blob
    assert {a["option_id"] for a in card["actions"]} == {
        "run", "save", "change", "discard"
    }


def test_kickoff_card_minimal_and_empty_documents_are_fine():
    minimal = kickoff_card("/tmp/d.md", "# TASK\nOnly a task body.\n", recipe="code-fix")
    assert minimal["kind"] == "kickoff"
    assert kickoff_card("/tmp/d.md", "", recipe="code-fix")["kind"] == "kickoff"


def test_failure_card_actions_stderr_and_conditional_link(tmp_path):
    card = failure_card(RUN, copy.deepcopy(_SNAPSHOT), dict(_HINT), diagnosis_path=None)
    assert card["id"] == f"{RUN}:failure"
    assert card["parent"] == f"{RUN}:parent"
    assert card["status"] == "failed"
    assert {a["option_id"] for a in card["actions"] if "option_id" in a} == {
        "recover", "resume:implementer-1", "forensics", "leave"
    }
    assert any(
        "STDERR_MARKER_BOOM" in json.dumps(b)
        for b in card["blocks"]
        if b.get("type") == "code"
    )
    assert "absent-diagnosis.json" not in json.dumps(card)

    diag = tmp_path / "diagnosis.json"
    diag.write_text("{}", encoding="utf-8")
    with_link = failure_card(
        RUN, copy.deepcopy(_SNAPSHOT), dict(_HINT), diagnosis_path=str(diag)
    )
    assert str(diag) in json.dumps(with_link)
    assert any(a.get("kind") == "link" for a in with_link["actions"])


def test_failure_card_malformed_hint_is_minimal():
    for hint in (None, {}, {"failed_node": {"nested": "object"}}, "junk", 17):
        card = failure_card(RUN, {"status": "failed"}, hint, diagnosis_path=None)
        assert card["kind"] == "failure"
        assert card["id"] == f"{RUN}:failure"
        assert card["status"] == "failed"


def test_review_card_mirrors_permission_options():
    options = [
        {"option_id": "merge", "label": "Merge"},
        {"option_id": "discard", "label": "Discard changes"},
    ]
    card = review_card(RUN, {"status": "reviewing"}, options)
    assert card["kind"] == "review"
    assert card["id"] == f"{RUN}:parent"
    assert any(b.get("type") == "items" for b in card["blocks"])
    assert [a["option_id"] for a in card["actions"]] == ["merge", "discard"]
    assert [a["label"] for a in card["actions"]] == ["Merge", "Discard changes"]


# ── live / replay parity ────────────────────────────────────────────────────


class _Conn:
    def __init__(self) -> None:
        self.updates: list = []

    async def session_update(self, session_id, update):
        self.updates.append((session_id, update))


async def _noop(*args, **kwargs):
    return None


def _quiet(agent):
    agent._drain_live_for = _noop
    agent._replay_live_for = _noop
    agent._emit_diff_update_for_run = _noop
    agent._emit_diff_update_for_loaded_run = _noop
    agent._emit_plan_update = _noop
    agent._emit_title_state_update = _noop
    return agent


def test_live_and_replay_produce_equal_node_cards(tmp_path):
    snap = copy.deepcopy(_SNAPSHOT)

    live = _quiet(MiniOrkAcpAgent(home=str(tmp_path), recipe="code-fix"))
    live_conn = _Conn()
    live._conn = live_conn
    asyncio.run(live._project_snapshot(RUN, copy.deepcopy(snap), live=True))
    live_cards = [
        _card(u) for _, u in live_conn.updates if _card(u) is not None
    ]

    replay = _quiet(
        MiniOrkAcpAgent(
            home=str(tmp_path), recipe="code-fix", reader=lambda rid: copy.deepcopy(snap)
        )
    )
    replay_conn = _Conn()
    replay._conn = replay_conn
    asyncio.run(replay._replay_run_in_thread("th-1", RUN, cwd=str(tmp_path)))
    replay_cards = [
        _card(u) for _, u in replay_conn.updates if _card(u) is not None
    ]

    assert live_cards
    assert live_cards == replay_cards
