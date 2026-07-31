"""P3: committee vote (REFUTED-outrank, decorrelation, weighting) +
reward mapping + JSONL append.
"""
from __future__ import annotations

import json

import pytest

from mini_ork.verify.behavioral import (
    PROVEN,
    REFUTED,
    UNVERIFIED,
    BehavioralVerdict,
    Check,
)
from mini_ork.verify.committee import committee_vote, pairwise_agreement
from mini_ork.verify.reward import record_reward, verdict_reward


def _verdict(status, *, surface="api", target="/health"):
    return BehavioralVerdict(
        status=status,
        surface=surface,
        target=target,
        checks=[Check(name="status", ok=(status == PROVEN), detail="")],
    )


# ─── pairwise_agreement ─────────────────────────────────────────────────── #
def test_pairwise_agreement_one_verdict_is_one():
    assert pairwise_agreement([_verdict(PROVEN)]) == 1.0


def test_pairwise_agreement_empty_is_one():
    assert pairwise_agreement([]) == 1.0


def test_pairwise_agreement_unanimous_is_one():
    vs = [_verdict(PROVEN, surface="api"), _verdict(PROVEN, surface="ui")]
    assert pairwise_agreement(vs) == 1.0


def test_pairwise_agreement_full_disagreement_is_zero():
    vs = [
        _verdict(PROVEN),
        _verdict(REFUTED),
    ]
    assert pairwise_agreement(vs) == 0.0


def test_pairwise_agreement_partial():
    vs = [
        _verdict(PROVEN),
        _verdict(PROVEN),
        _verdict(REFUTED),
    ]
    # 3 pairs: (P,P)=eq, (P,R)=diff, (P,R)=diff → 1/3
    assert pairwise_agreement(vs) == pytest.approx(1 / 3)


# ─── committee_vote: empty / unknown ────────────────────────────────────── #
def test_committee_vote_empty_is_unverified():
    assert committee_vote([]) == UNVERIFIED


def test_committee_vote_rejects_unknown_status():
    bad = BehavioralVerdict(status="MAYBE", surface="api", target="/x")
    with pytest.raises(ValueError, match="unknown status"):
        committee_vote([bad])


# ─── committee_vote: REFUTED outranks ───────────────────────────────────── #
def test_committee_vote_refuted_outranks_anything():
    vs = [
        _verdict(PROVEN, surface="api"),
        _verdict(REFUTED, surface="ui"),
        _verdict(PROVEN, surface="journey"),
    ]
    assert committee_vote(vs) == REFUTED


def test_committee_vote_refuted_outranks_despite_heavy_proven_weights():
    vs = [
        _verdict(PROVEN, surface="api"),
        _verdict(REFUTED, surface="ui"),
    ]
    assert committee_vote(vs, weights=[100.0, 1.0]) == REFUTED


# ─── committee_vote: decorrelation guard ────────────────────────────────── #
def test_committee_vote_requires_distinct_surfaces_for_proven():
    # Two PROVEN votes from the SAME surface (api) → correlated, UNVERIFIED.
    vs = [
        _verdict(PROVEN, surface="api"),
        _verdict(PROVEN, surface="api"),
        _verdict(UNVERIFIED, surface="ui"),
    ]
    assert committee_vote(vs) == UNVERIFIED


def test_committee_vote_proven_two_distinct_surfaces_passes_decorrelation():
    vs = [
        _verdict(PROVEN, surface="api"),
        _verdict(PROVEN, surface="ui"),
    ]
    assert committee_vote(vs) == PROVEN


def test_committee_vote_proven_with_three_distinct_surfaces_passes():
    vs = [
        _verdict(PROVEN, surface="api"),
        _verdict(PROVEN, surface="ui"),
        _verdict(PROVEN, surface="journey"),
    ]
    assert committee_vote(vs) == PROVEN


def test_committee_vote_empty_surface_does_not_satisfy_decorrelation():
    # PROVEN from two verdicts but both surface="" → collapsed to one bucket.
    vs = [
        _verdict(PROVEN, surface=""),
        _verdict(PROVEN, surface=""),
    ]
    assert committee_vote(vs) == UNVERIFIED


# ─── committee_vote: weighting / strict majority ────────────────────────── #
def test_committee_vote_unverified_outweighs_single_proven():
    vs = [
        _verdict(PROVEN, surface="api"),
        _verdict(UNVERIFIED, surface="ui"),
        _verdict(UNVERIFIED, surface="journey"),
    ]
    assert committee_vote(vs, weights=[1.0, 5.0, 5.0]) == UNVERIFIED


def test_committee_vote_proven_strict_majority_wins():
    vs = [
        _verdict(PROVEN, surface="api"),
        _verdict(PROVEN, surface="ui"),
        _verdict(UNVERIFIED, surface="journey"),
    ]
    assert committee_vote(vs, weights=[1.0, 1.0, 0.5]) == PROVEN


def test_committee_vote_treated_tie_is_unverified():
    # PROVEN weights == UNVERIFIED weights → abstain (strict majority rule).
    vs = [
        _verdict(PROVEN, surface="api"),
        _verdict(PROVEN, surface="ui"),
        _verdict(UNVERIFIED, surface="journey"),
    ]
    assert committee_vote(vs, weights=[1.0, 1.0, 2.0]) == UNVERIFIED


# ─── committee_vote: weight shape errors ────────────────────────────────── #
def test_committee_vote_rejects_wrong_length_weights():
    vs = [_verdict(PROVEN, surface="api"), _verdict(PROVEN, surface="ui")]
    with pytest.raises(ValueError, match="weights length"):
        committee_vote(vs, weights=[1.0])


def test_committee_vote_rejects_negative_weight():
    vs = [_verdict(PROVEN, surface="api"), _verdict(PROVEN, surface="ui")]
    with pytest.raises(ValueError, match="non-negative"):
        committee_vote(vs, weights=[1.0, -0.1])


# ─── verdict_reward mapping ─────────────────────────────────────────────── #
def test_verdict_reward_proven_is_one():
    assert verdict_reward(PROVEN) == 1.0


def test_verdict_reward_refuted_is_zero():
    assert verdict_reward(REFUTED) == 0.0


def test_verdict_reward_unverified_is_none():
    assert verdict_reward(UNVERIFIED) is None


def test_verdict_reward_rejects_unknown():
    with pytest.raises(ValueError, match="unknown verdict status"):
        verdict_reward("MAYBE")


# ─── record_reward: JSONL append ───────────────────────────────────────── #
def test_record_reward_appends_one_jsonl_row(tmp_path):
    sink = tmp_path / "rewards.jsonl"
    out = record_reward(
        run_id="run-1",
        surface="api",
        target="/health",
        status=PROVEN,
        ts="2026-07-31T00:00:00Z",
        path=sink,
    )
    assert out == 1.0
    lines = sink.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    row = json.loads(lines[0])
    assert row == {
        "run_id": "run-1",
        "surface": "api",
        "target": "/health",
        "status": PROVEN,
        "reward": 1.0,
        "ts": "2026-07-31T00:00:00Z",
    }


def test_record_reward_unverified_uses_null_reward(tmp_path):
    sink = tmp_path / "rewards.jsonl"
    out = record_reward(
        run_id="run-2",
        surface="ui",
        target="/signup",
        status=UNVERIFIED,
        ts="2026-07-31T00:00:00Z",
        path=sink,
    )
    assert out is None
    row = json.loads(sink.read_text(encoding="utf-8").splitlines()[0])
    assert row["reward"] is None


def test_record_reward_creates_parent_dir(tmp_path):
    sink = tmp_path / "nested" / "deep" / "rewards.jsonl"
    record_reward(
        run_id="run-3",
        surface="journey",
        target="/flow",
        status=REFUTED,
        ts="2026-07-31T00:00:00Z",
        path=sink,
    )
    assert sink.exists()


def test_record_reward_default_path_resolves_home(monkeypatch, tmp_path):
    monkeypatch.setenv("MINI_ORK_HOME", str(tmp_path))
    record_reward(
        run_id="run-4",
        surface="api",
        target="/health",
        status=PROVEN,
        ts="2026-07-31T00:00:00Z",
    )
    expected = tmp_path / "verify_rewards.jsonl"
    assert expected.exists()
    rows = [json.loads(line) for line in expected.read_text(encoding="utf-8").splitlines()]
    assert rows[-1]["run_id"] == "run-4"


def test_record_reward_rejects_empty_run_id(tmp_path):
    with pytest.raises(ValueError, match="run_id"):
        record_reward(
            run_id="",
            surface="api",
            target="/health",
            status=PROVEN,
            ts="2026-07-31T00:00:00Z",
            path=tmp_path / "x.jsonl",
        )


def test_record_reward_rejects_unknown_status(tmp_path):
    with pytest.raises(ValueError, match="unknown verdict status"):
        record_reward(
            run_id="run-5",
            surface="api",
            target="/health",
            status="MAYBE",
            ts="2026-07-31T00:00:00Z",
            path=tmp_path / "x.jsonl",
        )