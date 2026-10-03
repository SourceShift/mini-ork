"""Hermetic tests for ``mini_ork.acp.plan.plan_entries``.

Pure-function tests — no async, no agent, no DB. The five-node docs shape
(``planner → doc_editor → grep_assert, link_verifier → publisher``) is
the canonical decomposition shape the kickoff names.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from mini_ork.acp.plan import plan_entries  # noqa: E402

DOCS_DECOMPOSITION: list[dict] = [
    {
        "id": "planner",
        "description": "plan the doc edit",
        "node_type": "planner",
        "depends_on": [],
    },
    {
        "id": "doc_editor",
        "description": "edit the doc",
        "node_type": "implementer",
        "depends_on": ["planner"],
    },
    {
        "id": "grep_assert",
        "description": "grep assert",
        "node_type": "implementer",
        "depends_on": ["doc_editor"],
    },
    {
        "id": "link_verifier",
        "description": "verify links",
        "node_type": "verifier",
        "depends_on": ["planner"],
    },
    {
        "id": "publisher",
        "description": "publish",
        "node_type": "publisher",
        "depends_on": ["grep_assert", "link_verifier"],
    },
]


def _write_plan(run_dir: Path, decomposition: list[dict]) -> None:
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "plan.json").write_text(
        json.dumps({"decomposition": decomposition}),
        encoding="utf-8",
    )


def _docs_events() -> list[dict]:
    """Lifecycle events: planner emits nothing, doc_editor starts."""
    return [
        {
            "event_type": "node_start",
            "payload_json": {"node_id": "doc_editor", "node_type": "implementer"},
        },
    ]


def test_five_node_docs_shape_returns_entries_in_decomposition_order(tmp_path: Path) -> None:
    run_dir = tmp_path / "runs" / "run-docs-1"
    _write_plan(run_dir, DOCS_DECOMPOSITION)
    entries = plan_entries(run_dir, [])
    assert [e["content"] for e in entries] == [
        "planner (planner)",
        "doc_editor (implementer)",
        "grep_assert (implementer)",
        "link_verifier (verifier)",
        "publisher (publisher)",
    ]
    # Implementer/reviewer get high; everyone else (planner/verifier note:
    # verifier is NOT in the high set per the kickoff) gets medium.
    priorities = [e["priority"] for e in entries]
    assert priorities[0] == "medium"  # planner
    assert priorities[1] == "high"    # doc_editor (implementer)
    assert priorities[2] == "high"    # grep_assert (implementer)
    assert priorities[3] == "medium"  # link_verifier (verifier)
    assert priorities[4] == "medium"  # publisher


def test_reviewer_node_type_gets_high_priority(tmp_path: Path) -> None:
    run_dir = tmp_path / "runs" / "run-rev"
    _write_plan(
        run_dir,
        [
            {"id": "review", "description": "review", "node_type": "reviewer", "depends_on": []},
            {"id": "publish", "description": "publish", "node_type": "publisher", "depends_on": ["review"]},
        ],
    )
    entries = plan_entries(run_dir, [])
    assert [e["priority"] for e in entries] == ["high", "medium"]


def test_planner_marked_completed_when_dependent_starts(tmp_path: Path) -> None:
    """Skipped planner: it never emits, but its dependent doc_editor did.

    The kickoff's example: ``planner → doc_editor`` where the planner emits
    no events under a static plan. Once ``doc_editor`` has a ``node_start``
    the planner reads ``completed`` (skipped).
    """
    run_dir = tmp_path / "runs" / "run-skip-1"
    _write_plan(run_dir, DOCS_DECOMPOSITION)
    before = plan_entries(run_dir, [])
    planner_status_before = next(e for e in before if e["content"].startswith("planner"))["status"]
    assert planner_status_before == "pending"

    during = plan_entries(run_dir, _docs_events())
    planner_after = next(e for e in during if e["content"].startswith("planner"))
    assert planner_after["status"] == "completed"
    doc_editor = next(e for e in during if e["content"].startswith("doc_editor"))
    assert doc_editor["status"] == "in_progress"


def test_node_end_marks_completed_even_with_no_start(tmp_path: Path) -> None:
    """A ``node_end`` seen with no ``node_start`` still reads completed."""
    run_dir = tmp_path / "runs" / "run-endonly"
    _write_plan(
        run_dir,
        [
            {"id": "n1", "description": "", "node_type": "verifier", "depends_on": []},
        ],
    )
    entries = plan_entries(
        run_dir,
        [
            {"event_type": "node_end", "payload_json": {"node_id": "n1"}},
        ],
    )
    assert entries[0]["status"] == "completed"


def test_dict_payload_json_parsed(tmp_path: Path) -> None:
    run_dir = tmp_path / "runs" / "run-dict"
    _write_plan(
        run_dir,
        [
            {"id": "n1", "description": "", "node_type": "implementer", "depends_on": []},
        ],
    )
    entries = plan_entries(
        run_dir,
        [
            {"event_type": "node_start", "payload_json": {"node_id": "n1"}},
            {"event_type": "node_end", "payload_json": {"node_id": "n1"}},
        ],
    )
    assert entries[0]["status"] == "completed"


def test_json_string_payload_json_parsed(tmp_path: Path) -> None:
    run_dir = tmp_path / "runs" / "run-str"
    _write_plan(
        run_dir,
        [
            {"id": "n1", "description": "", "node_type": "implementer", "depends_on": []},
        ],
    )
    entries = plan_entries(
        run_dir,
        [
            {"event_type": "node_start", "payload_json": json.dumps({"node_id": "n1"})},
        ],
    )
    assert entries[0]["status"] == "in_progress"


def test_undecodable_string_payload_falls_back_to_no_match(tmp_path: Path) -> None:
    run_dir = tmp_path / "runs" / "run-bad"
    _write_plan(
        run_dir,
        [
            {"id": "n1", "description": "", "node_type": "implementer", "depends_on": []},
            {"id": "n2", "description": "", "node_type": "verifier", "depends_on": ["n1"]},
        ],
    )
    entries = plan_entries(
        run_dir,
        [
            {"event_type": "node_start", "payload_json": "not-json{"},
        ],
    )
    # No event matched any node → n1 stays pending; n2 still depends on n1,
    # so the "dependent started" rule does not flip it.
    assert [e["status"] for e in entries] == ["pending", "pending"]


def test_missing_plan_json_returns_empty(tmp_path: Path) -> None:
    run_dir = tmp_path / "runs" / "run-missing"
    run_dir.mkdir(parents=True)
    assert plan_entries(run_dir, []) == []


def test_unparseable_plan_json_returns_empty(tmp_path: Path) -> None:
    run_dir = tmp_path / "runs" / "run-bad-plan"
    run_dir.mkdir(parents=True)
    (run_dir / "plan.json").write_text("not-json{", encoding="utf-8")
    assert plan_entries(run_dir, []) == []


def test_plan_json_without_decomposition_returns_empty(tmp_path: Path) -> None:
    run_dir = tmp_path / "runs" / "run-no-dec"
    run_dir.mkdir(parents=True)
    (run_dir / "plan.json").write_text(json.dumps({"objective": "x"}), encoding="utf-8")
    assert plan_entries(run_dir, []) == []


def test_plan_json_with_non_list_decomposition_returns_empty(tmp_path: Path) -> None:
    run_dir = tmp_path / "runs" / "run-bad-dec"
    run_dir.mkdir(parents=True)
    (run_dir / "plan.json").write_text(
        json.dumps({"decomposition": {"id": "n1"}}), encoding="utf-8"
    )
    assert plan_entries(run_dir, []) == []


def test_plan_json_root_not_dict_returns_empty(tmp_path: Path) -> None:
    run_dir = tmp_path / "runs" / "run-root-list"
    run_dir.mkdir(parents=True)
    (run_dir / "plan.json").write_text(json.dumps([{"id": "n1"}]), encoding="utf-8")
    assert plan_entries(run_dir, []) == []


def test_node_without_id_is_skipped(tmp_path: Path) -> None:
    run_dir = tmp_path / "runs" / "run-noid"
    _write_plan(
        run_dir,
        [
            {"description": "no id", "node_type": "implementer", "depends_on": []},
            {"id": "n1", "description": "", "node_type": "verifier", "depends_on": []},
        ],
    )
    entries = plan_entries(run_dir, [])
    assert [e["content"] for e in entries] == ["n1 (verifier)"]