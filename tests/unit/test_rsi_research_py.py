"""Unit tests: mini_ork.rsi.research — a technique review becomes candidates.

The contracts frozen here are the ones a later wiring (RRL-4) will depend on:

* identity is the *technique*, not the review's row id, so a re-review that
  renumbers a technique does not mint a new candidate;
* severity is integer arithmetic with a pinned output, so the queue cannot be
  silently reordered by a change here;
* a missing review is "nothing to do" and a broken review is an error — the
  two are never confused.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from mini_ork.rsi import candidates as C  # noqa: E402
from mini_ork.rsi import research  # noqa: E402

NOW = 1_760_000_000


def _row(i, **kw):
    """A valid ranked row: everything the review writes for a good technique."""
    row = {
        "id": f"G0{i}-T01",
        "group_id": f"G0{i}",
        "technique": f"Technique number {i}",
        "mechanism": '"Does a thing."',
        "evidence": ["arxiv:2601.00001"],
        "evidence_strength": "strong",
        "miniork_surface": "verifier",
        "adoption_sketch": '"Add it."',
        "priority": 10 - i,
        "mean_impact": 4,
        "verdict": "implement-now",
        "single_rater": False,
        "disagreement": False,
        "rank": i,
    }
    row.update(kw)
    return row


def _review(dirpath, rows):
    """Write a review payload the way the technique-review recipe writes one."""
    dirpath = Path(dirpath)
    dirpath.mkdir(parents=True, exist_ok=True)
    payload = {
        "reviewers": ["a", "b"],
        "technique_count": len(rows),
        "unscored": [],
        "disagreement_count": 0,
        "groups": [],
        "ranked": rows,
    }
    path = dirpath / "merged-review.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


# ── the new candidate kind ──────────────────────────────────────────────────


def test_kind_base_has_the_new_kind_at_44():
    assert C.KIND_BASE["technique_unadopted"] == 44
    # 44 base + 0 reach + 0 age + 5 verification criticality - 0 blast
    assert C.severity("technique_unadopted", domain="verification") == 49


def test_the_new_kind_sits_between_its_neighbours():
    keys = list(C.KIND_BASE)
    assert keys.index("unused_capability") < keys.index("technique_unadopted")
    assert keys.index("technique_unadopted") < keys.index("stale_signal")


# ── finding the newest review ───────────────────────────────────────────────


def test_latest_review_takes_the_newest_file(tmp_path):
    older = _review(tmp_path / "runs" / "verify-a", [_row(1)])
    newer = _review(tmp_path / "runs" / "verify-b", [_row(2)])
    os.utime(older, (1000, 1000))
    os.utime(newer, (2000, 2000))
    assert research.latest_review(tmp_path, "runs/verify-*/merged-review.json") == newer


def test_latest_review_breaks_an_mtime_tie_by_path(tmp_path):
    first = _review(tmp_path / "runs" / "verify-a", [_row(1)])
    second = _review(tmp_path / "runs" / "verify-b", [_row(2)])
    os.utime(first, (1000, 1000))
    os.utime(second, (1000, 1000))
    assert research.latest_review(tmp_path, "runs/verify-*/merged-review.json") == second


def test_latest_review_is_none_without_a_match(tmp_path):
    assert research.latest_review(tmp_path, "runs/verify-*/merged-review.json") is None
    _review(tmp_path / "runs" / "other", [_row(1)])
    assert research.latest_review(tmp_path, "runs/verify-*/merged-review.json") is None


def test_latest_review_ignores_a_directory_named_like_a_review(tmp_path):
    impostor = tmp_path / "runs" / "verify-c" / "merged-review.json"
    impostor.mkdir(parents=True)
    os.utime(impostor, (5000, 5000))
    real = _review(tmp_path / "runs" / "verify-b", [_row(2)])
    os.utime(real, (1000, 1000))
    assert research.latest_review(tmp_path, "runs/verify-*/merged-review.json") == real


# ── reading a review ────────────────────────────────────────────────────────


def test_load_ranked_rejects_a_payload_that_is_not_an_object(tmp_path):
    path = tmp_path / "list.json"
    path.write_text("[1, 2, 3]", encoding="utf-8")
    try:
        research.load_ranked(path)
    except ValueError:
        return
    raise AssertionError("a non-object payload must raise ValueError")


def test_load_ranked_rejects_a_ranked_field_that_is_not_a_list(tmp_path):
    path = tmp_path / "str.json"
    path.write_text('{"ranked": "x"}', encoding="utf-8")
    try:
        research.load_ranked(path)
    except ValueError:
        return
    raise AssertionError("a non-list ranked field must raise ValueError")


def test_load_ranked_drops_rows_that_are_not_objects(tmp_path):
    path = tmp_path / "mixed.json"
    path.write_text(
        json.dumps({"ranked": [{"id": "G01-T01"}, "junk", 3, {"id": "G02-T01"}]}),
        encoding="utf-8",
    )
    assert research.load_ranked(path) == [{"id": "G01-T01"}, {"id": "G02-T01"}]


# ── slugging ────────────────────────────────────────────────────────────────


def test_slug_collapses_punctuation_into_single_dashes():
    assert research.slug("Oracle-integrity auditing, with  invariants!") == (
        "oracle-integrity-auditing-with-invariants"
    )


def test_slug_of_nothing_is_the_generic_technique():
    assert research.slug("!!!") == "technique"
    assert research.slug("") == "technique"


def test_slug_is_bounded_and_never_ends_in_a_dash():
    out = research.slug("a-" * 100)
    assert len(out) <= 64
    assert not out.endswith("-")


# ── eligibility ─────────────────────────────────────────────────────────────


def test_eligible_accepts_a_well_reviewed_row():
    assert research.eligible(_row(1)) is True
    assert research.eligible(_row(1, verdict="next")) is True


def test_eligible_rejects_a_parked_verdict():
    assert research.eligible(_row(1, verdict="watch")) is False


def test_eligible_rejects_disagreement_unless_asked_to_keep_it():
    assert research.eligible(_row(1, disagreement=True)) is False
    assert research.eligible(_row(1, disagreement=True), skip_disagreement=False) is True


def test_eligible_rejects_a_single_rater_unless_asked_to_keep_it():
    assert research.eligible(_row(1, single_rater=True)) is False
    assert research.eligible(_row(1, single_rater=True), skip_single_rater=False) is True


def test_eligible_rejects_weak_evidence():
    assert research.eligible(_row(1, evidence_strength="Weak")) is False


def test_eligible_rejects_a_row_without_a_technique_or_an_id():
    assert research.eligible(_row(1, technique="")) is False
    assert research.eligible(_row(1, id="")) is False


# ── candidates ──────────────────────────────────────────────────────────────


def test_candidates_are_the_top_max_n_in_priority_order():
    spec = research.DEFAULT_SPECS["verification"]
    rows = [_row(3), _row(1), _row(5), _row(2), _row(4)]
    cands = research.technique_candidates(rows, spec=spec, review_id="verify-b", now=NOW, max_n=3)
    assert len(cands) == 3
    assert cands[0].title == "adopt G01-T01: Technique number 1"
    assert [c.title.split(":")[0] for c in cands] == [
        "adopt G01-T01",
        "adopt G02-T01",
        "adopt G03-T01",
    ]


def test_every_candidate_carries_its_domain_and_benchmark():
    spec = research.DEFAULT_SPECS["verification"]
    cands = research.technique_candidates([_row(1), _row(2)], spec=spec, review_id="verify-b", now=NOW)
    for cand in cands:
        assert cand.kind == "technique_unadopted"
        assert cand.domain == "verification"
        assert cand.success_command == "bench:verifier-replay"
        assert cand.success_metric == "verifier-replay: sealed paired gain (G12)"
        assert tuple(cand.files) == ("recipes/code-fix/verifiers/test.py",)
    assert cands[0].source == "research:G01"


def test_the_fix_hint_quotes_the_mechanism_the_benchmark_and_the_surface():
    spec = research.DEFAULT_SPECS["verification"]
    hint = research.fix_hint(_row(1), spec)
    assert "Mechanism: Does a thing." in hint
    assert "verifier-replay" in hint
    for entry in spec.surface:
        assert entry in hint


def test_severity_follows_the_priority_arithmetic():
    spec = research.DEFAULT_SPECS["verification"]
    cand = research.technique_candidates([_row(1)], spec=spec, review_id="verify-b", now=NOW)[0]
    # priority 9 -> reach 1 + 9**2 = 82 -> isqrt(81) = 9 -> 44 + 9 + 5
    assert cand.severity == 58


def test_identity_is_the_technique_not_the_row_id():
    spec = research.DEFAULT_SPECS["verification"]
    row = _row(1)
    first = research.technique_candidates([row], spec=spec, review_id="verify-b", now=NOW)[0]
    again = research.technique_candidates([dict(row)], spec=spec, review_id="verify-b", now=NOW)[0]
    renamed = research.technique_candidates(
        [_row(1, id="G07-T03")], spec=spec, review_id="verify-b", now=NOW
    )[0]
    assert first.candidate_id == again.candidate_id
    assert first.candidate_id == renamed.candidate_id


def test_an_unreadable_priority_counts_as_zero():
    spec = research.DEFAULT_SPECS["verification"]
    rows = [_row(2, priority="n/a"), _row(1)]
    cands = research.technique_candidates(rows, spec=spec, review_id="verify-b", now=NOW)
    assert len(cands) == 2
    assert cands[0].title.startswith("adopt G01-T01")
    assert cands[1].evidence["priority"] == 0.0
    # priority 0 -> reach 1 -> isqrt(0) = 0 -> 44 + 0 + 5
    assert cands[1].severity == 49


def test_max_n_below_one_emits_nothing():
    spec = research.DEFAULT_SPECS["verification"]
    assert (
        research.technique_candidates([_row(1)], spec=spec, review_id="verify-b", now=NOW, max_n=0)
        == []
    )


# ── harvesting a domain ─────────────────────────────────────────────────────


def test_a_domain_with_no_review_harvests_nothing(tmp_path):
    spec = research.DEFAULT_SPECS["verification"]
    harvest = research.harvest_domain(tmp_path, spec, now=NOW)
    assert harvest.review is None
    assert harvest.review_id is None
    assert harvest.candidates == ()
    assert harvest.error is None
    assert "runs/verify-*/merged-review.json" in "".join(harvest.notes)


def test_a_broken_review_is_an_error_not_an_empty_harvest(tmp_path):
    spec = research.DEFAULT_SPECS["verification"]
    review = _review(tmp_path / "runs" / "verify-b", [_row(1)])
    review.write_text("not json", encoding="utf-8")
    harvest = research.harvest_domain(tmp_path, spec, now=NOW)
    assert harvest.error is not None
    assert harvest.error.startswith("unreadable review")
    assert harvest.candidates == ()


def test_a_valid_review_harvests_its_eligible_rows(tmp_path):
    spec = research.DEFAULT_SPECS["verification"]
    rows = [_row(1), _row(2), _row(3, verdict="watch"), _row(4)]
    review = _review(tmp_path / "runs" / "verify-b", rows)
    mtime = NOW - 3 * 86400 - 10
    os.utime(review, (mtime, mtime))
    harvest = research.harvest_domain(tmp_path, spec, now=NOW)
    assert harvest.error is None
    assert harvest.review_id == "verify-b"
    assert harvest.age_days == 3
    assert len(harvest.candidates) == 3
    assert "3 eligible of 4 ranked; emitted 3" in harvest.notes


def test_the_harvest_summary_names_its_candidates(tmp_path):
    spec = research.DEFAULT_SPECS["verification"]
    _review(tmp_path / "runs" / "verify-b", [_row(1), _row(2, verdict="watch")])
    harvest = research.harvest_domain(tmp_path, spec, now=NOW)
    summary = harvest.to_dict()
    assert summary["domain"] == "verification"
    assert summary["review_id"] == "verify-b"
    assert len(summary["candidates"]) == 1
    entry = summary["candidates"][0]
    assert set(entry) == {"candidate_id", "title", "source", "severity"}
    assert entry["title"] == harvest.candidates[0].title


def test_harvest_all_visits_every_domain_and_can_narrow_the_set(tmp_path):
    assert research.harvest_all(tmp_path, now=NOW, domains=["routing"]) == []
    harvests = research.harvest_all(tmp_path, now=NOW)
    assert [h.domain for h in harvests] == ["verification"]
