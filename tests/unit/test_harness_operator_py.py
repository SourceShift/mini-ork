"""Unit tests for the typed harness-edit operator (Harness-R1 measurement half).

Hermetic: no DB, no lane, no network, no real run. The editable install
resolves ``mini_ork`` to main, so the repo root is inserted on ``sys.path``
before importing. The twelve assertions are the kickoff acceptance contract;
they are implemented verbatim and must not be reworded, weakened, or dropped.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

import pytest

from mini_ork.cli import harness_edit
from mini_ork.learning import harness_contrast, harness_operator


def test_1_failure_signature_separates_classes():
    assert (
        harness_operator.failure_signature({"node": "w3", "exit_code": 137})
        == "infra_interrupt:w3"
    )
    assert (
        harness_operator.failure_signature({"node": "w3", "max_turns_hit": True})
        == "provider_limit:w3"
    )
    assert (
        harness_operator.failure_signature({"node": "w3", "reason": "fatal: unrecoverable"})
        == "terminal:w3"
    )
    # A receipt with no node keys to "-".
    assert harness_operator.failure_signature({"exit_code": 137}) == "infra_interrupt:-"


def test_2_group_failures_counts_and_deduplicates():
    receipts = [
        {"node": "w3", "exit_code": 137, "run_id": "r2"},
        {"node": "w3", "exit_code": 137, "run_id": "r1"},
        {"node": "w3", "exit_code": 137, "run_id": "r2"},
    ]
    groups = harness_operator.group_failures(receipts)
    assert len(groups) == 1
    assert groups[0]["n"] == 3
    assert groups[0]["runs"] == ["r1", "r2"]


def test_3_empty_batch_is_absence_of_evidence_not_a_crash():
    assert harness_operator.group_failures([]) == []
    assert harness_operator.propose([]) == []


def test_4_singleton_group_is_omitted():
    receipts = [
        {"node": "w3", "exit_code": 137, "run_id": "r1"},
        {"node": "w3", "exit_code": 137, "run_id": "r2"},
        {"node": "w4", "reason": "fatal", "run_id": "r3"},
    ]
    proposals = harness_operator.propose(receipts)
    assert len(proposals) == 1
    assert proposals[0]["signature"] == "infra_interrupt:w3"
    assert "terminal:w4" not in json.dumps(proposals)


def test_5_support_equals_n_and_sorted_by_support_then_signature():
    receipts = []
    receipts += [{"node": "a", "exit_code": 137} for _ in range(2)]       # infra_interrupt:a
    receipts += [{"node": "b", "max_turns_hit": True} for _ in range(5)]  # provider_limit:b
    receipts += [{"node": "c", "reason": "fatal"} for _ in range(3)]      # terminal:c
    proposals = harness_operator.propose(receipts)
    assert [p["signature"] for p in proposals] == [
        "provider_limit:b",
        "terminal:c",
        "infra_interrupt:a",
    ]
    assert [p["support"] for p in proposals] == [5, 3, 2]


def test_6_typed_mapping_holds_exactly():
    receipts = [
        {"node": "w", "exit_code": 137},          # infra_interrupt
        {"node": "w", "max_turns_hit": True},     # provider_limit
        {"node": "w", "reason": "invalid json"},  # output_invalid
        {"node": "w", "reason": "needs_answers"}, # input_required
        {"node": "w", "reason": "fatal"},         # terminal
    ]
    proposals = harness_operator.propose(receipts, min_support=1)
    by_signature = {p["signature"]: (p["target"], p["kind"]) for p in proposals}
    assert by_signature["infra_interrupt:w"] == ("recovery", "retry_policy")
    assert by_signature["provider_limit:w"] == ("recovery", "retry_policy")
    assert by_signature["output_invalid:w"] == ("prompt", "prompt_edit")
    assert by_signature["input_required:w"] == ("stage_order", "reorder")
    assert by_signature["terminal:w"] == ("verifier", "gate_edit")


def test_7_score_agrees_with_primitive():
    rows = [
        {"probe": "p1", "lane": "glm", "a": False, "b": True},
        {"probe": "p2", "lane": "glm", "a": False, "b": False},
    ]
    proposal = {"signature": "x", "expected_direction": "improve"}
    scored = harness_operator.score(proposal, rows)
    attributed = harness_contrast.attribute(rows)
    assert scored["delta"] == attributed["delta"]
    assert scored["n"] == attributed["n"]


def test_8_score_over_empty_rows_is_none_never_zero():
    proposal = {"signature": "x", "expected_direction": "improve"}
    scored = harness_operator.score(proposal, [])
    assert scored["delta"] is None
    assert scored["agrees"] is None


def test_9_agrees_rule():
    improve_rows = [{"probe": "p1", "lane": "glm", "a": False, "b": True}]  # delta 1.0
    degrade_rows = [{"probe": "p1", "lane": "glm", "a": True, "b": False}]  # delta -1.0

    no_claim = harness_operator.score({"signature": "x"}, improve_rows)
    assert no_claim["agrees"] is None

    improve = harness_operator.score(
        {"signature": "x", "expected_direction": "improve"}, improve_rows
    )
    assert improve["agrees"] is True

    disagree = harness_operator.score(
        {"signature": "x", "expected_direction": "improve"}, degrade_rows
    )
    assert disagree["agrees"] is False

    none_claim = harness_operator.score(
        {"signature": "x", "expected_direction": "none"}, improve_rows
    )
    assert none_claim["agrees"] is None


def test_10_score_raises_on_mixed_lanes():
    rows = [
        {"probe": "p1", "lane": "glm", "a": False, "b": True},
        {"probe": "p2", "lane": "kimi", "a": False, "b": True},
    ]
    with pytest.raises(ValueError):
        harness_operator.score({"signature": "x", "expected_direction": "improve"}, rows)


def test_11_summarize_over_empty_receipts():
    report = harness_operator.summarize([])
    assert report["n_receipts"] == 0
    assert report["n_proposals"] == 0
    assert report["proposals"] == []
    assert report["scores"] == []


def test_12_cli_runs_and_reports(tmp_path, capsys):
    fixture = tmp_path / "receipts.json"
    fixture.write_text(
        json.dumps(
            [
                {"node": "w3", "exit_code": 137, "run_id": "r1"},
                {"node": "w3", "exit_code": 137, "run_id": "r2"},
            ]
        )
    )
    assert harness_edit.main([str(fixture), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert "proposals" in payload

    assert harness_edit.main([str(tmp_path / "missing.json")]) == 0
    assert capsys.readouterr().out == ""


def test_13_summarize_scores_every_proposal_from_one_shot_rows():
    """A caller's iterable must not leave later proposals silently unmeasured."""
    receipts = [{"node": "a", "exit_code": 137}] * 2 + [{"node": "b", "reason": "fatal"}] * 2
    rows = [{"probe": "p1", "lane": "glm", "a": False, "b": True}]

    from_list = harness_operator.summarize(receipts, rows=list(rows))
    from_iter = harness_operator.summarize(receipts, rows=iter(rows))

    assert len(from_iter["scores"]) == 2
    assert [s["delta"] for s in from_iter["scores"]] == [
        s["delta"] for s in from_list["scores"]
    ]
    assert all(s["delta"] is not None for s in from_iter["scores"])
