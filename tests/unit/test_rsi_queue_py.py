"""Unit tests: the ranked queue is pure, deterministic and never starves.

The three properties under test, each of which has a concrete failure it
prevents:

* **One ranked queue** — the first element is the worst gap anywhere, not the
  worst gap of the domain that happens to be scanned first.
* **No starvation** — a domain with a single modest candidate still gets a
  seeded slot when another domain floods the harvest.
* **No double spend** — a merged, failed or claimed signature is dropped, and
  the drop has a named code so the cycle can write ``selection.json``.
"""
from __future__ import annotations

from dataclasses import replace

import pytest

from mini_ork.rsi.candidates import Candidate
from mini_ork.rsi.queue import (
    CREDIT_MIN_N,
    LedgerView,
    QueueConfig,
    Rejection,
    build_queue,
    credit_adjustment,
    credit_for_source,
    effective_severity,
)

NOW = 1_760_000_000
DAY = 86400


def _cand(
    domain: str = "learning",
    *,
    kind: str = "data_path_dead",
    key: str = "",
    file: str = "mini_ork/learning/ledger.py",
    severity: int | None = None,
    source: str | None = None,
) -> Candidate:
    key = key or f"{domain}:{file}:{kind}"
    cand = Candidate.build(
        domain=domain,
        source=source or f"db:{key}",
        kind=kind,
        title=f"{domain} gap {key}",
        evidence_key=key,
        files=(file,),
        now=NOW,
    )
    return cand if severity is None else replace(cand, severity=severity)


def _sig(cand: Candidate) -> str:
    return cand.signature


# ── ordering ────────────────────────────────────────────────────────────────


def test_first_selected_is_the_global_maximum_regardless_of_domain():
    low = _cand("learning", key="l", severity=50)
    high = _cand("routing", key="r", severity=90)
    mid = _cand("verification", key="v", severity=70)
    plan = build_queue([low, high, mid], now=NOW)
    assert plan.selected[0].candidate.candidate_id == high.candidate_id
    assert plan.selected[0].seed == "global-max"


def test_round_robin_seeds_every_domain_before_the_backlog():
    # DOMAINS order is learning, routing, verification, so the *seeding* pass
    # puts learning ahead of a more severe verification candidate. With
    # max_candidates=3 every domain gets a slot, which is the whole point.
    learning = _cand("learning", key="l", severity=41)
    routing = _cand("routing", key="r", severity=99)
    verification = _cand("verification", key="v", severity=80)
    plan = build_queue([learning, routing, verification], now=NOW)
    assert [i.candidate.domain for i in plan.selected] == ["routing", "learning", "verification"]
    assert [i.seed for i in plan.selected] == ["global-max", "seed:learning", "seed:verification"]


def test_the_global_max_does_not_take_a_second_slot_from_its_own_domain():
    """With `max_candidates` equal to the number of domains, the first slot's
    domain must not also take the seed slot — otherwise one domain is silently
    absent from a queue that claims to be seeded."""
    l1 = _cand("learning", key="l1", severity=99)
    l2 = _cand("learning", key="l2", severity=98)
    routing = _cand("routing", key="r", severity=50)
    verification = _cand("verification", key="v", severity=45)
    plan = build_queue([l1, l2, routing, verification], now=NOW,
                       cfg=QueueConfig(max_candidates=3))
    assert [i.candidate.domain for i in plan.selected] == [
        "learning", "routing", "verification"
    ]
    assert plan.backlog[0].candidate.candidate_id == l2.candidate_id


def test_a_flooding_domain_cannot_starve_a_quiet_one():
    """The failure this prevents: verification produces most candidates, so a
    pure severity ranking would fix verification forever and learning never."""
    noisy = [_cand("verification", key=f"v{i}", severity=90 - i, file=f"mini_ork/v{i}.py")
             for i in range(5)]
    quiet = _cand("learning", key="quiet", severity=41, file="mini_ork/learning/q.py")
    plan = build_queue([*noisy, quiet], now=NOW, cfg=QueueConfig(max_candidates=3))
    assert "learning" in {i.candidate.domain for i in plan.selected}
    assert plan.selected[0].candidate.severity == 90


def test_ordering_is_deterministic_and_input_order_independent():
    cands = [
        _cand("learning", key="a", severity=60),
        _cand("learning", key="b", severity=60),
        _cand("routing", key="c", severity=60),
        _cand("verification", key="d", severity=60),
    ]
    forward = build_queue(cands, now=NOW, cfg=QueueConfig(max_candidates=4))
    reverse = build_queue(list(reversed(cands)), now=NOW, cfg=QueueConfig(max_candidates=4))
    assert [i.candidate.candidate_id for i in forward.selected] == [
        i.candidate.candidate_id for i in reverse.selected
    ]
    # Same-severity, same-blast ties fall to candidate_id — a total order with
    # no randomness, which is why the "global max" slot is reproducible too.
    assert forward.selected[0].candidate.candidate_id == min(
        c.candidate_id for c in cands
    )


def test_max_candidates_cuts_the_backlog_but_keeps_it_ordered():
    cands = [_cand("learning", key=f"x{i}", severity=90 - i) for i in range(5)]
    plan = build_queue(cands, now=NOW, cfg=QueueConfig(max_candidates=2))
    assert len(plan.selected) == 2
    assert len(plan.backlog) == 3
    assert plan.selected[-1].effective_severity >= plan.backlog[0].effective_severity
    assert plan.examined == 5


def test_dedupe_merges_one_gap_seen_twice():
    cand = _cand("routing", key="r1", severity=70)
    plan = build_queue([cand, cand], now=NOW)
    assert plan.examined == 1
    assert len(plan.selected) == 1


# ── avoidance ───────────────────────────────────────────────────────────────


def test_merged_ancestor_is_dropped():
    cand = _cand("learning", key="m", severity=80)
    view = LedgerView(merged_ancestor_signatures=frozenset({_sig(cand)}))
    plan = build_queue([cand], now=NOW, view=view)
    assert plan.selected == ()
    assert [r.code for r in plan.rejected] == ["already_merged"]


def test_merged_but_not_in_origin_main_returns_to_the_queue():
    """The caller only puts *ancestor* signatures in the view. A merged gap that
    is not in origin/main is a regression and must be re-queued."""
    cand = _cand("learning", key="m2", severity=80)
    view = LedgerView(by_signature={_sig(cand): {"status": "merged", "updated_at": NOW}})
    plan = build_queue([cand], now=NOW, view=view)
    assert plan.top is not None


def test_claimed_candidate_is_dropped():
    cand = _cand("routing", key="c", severity=80)
    view = LedgerView(claimed_ids=frozenset({cand.candidate_id}))
    plan = build_queue([cand], now=NOW, view=view)
    assert [r.code for r in plan.rejected] == ["already_claimed"]


def test_failed_signature_is_dropped_inside_the_ttl():
    cand = _cand("verification", key="f", severity=80)
    view = LedgerView(by_signature={
        _sig(cand): {"status": "quarantined", "severity": 75, "updated_at": NOW - 5 * DAY}
    })
    plan = build_queue([cand], now=NOW, view=view)
    assert [r.code for r in plan.rejected] == ["failed_signature"]
    assert "quarantined" in plan.rejected[0].detail


def test_failed_signature_is_allowed_again_after_the_ttl():
    cand = _cand("verification", key="f2", severity=80)
    view = LedgerView(by_signature={
        _sig(cand): {"status": "rejected", "severity": 75, "updated_at": NOW - 40 * DAY}
    })
    assert build_queue([cand], now=NOW, view=view).top is not None


def test_a_much_worse_failure_is_retried_immediately():
    cand = _cand("verification", key="f3", severity=90)
    view = LedgerView(by_signature={
        _sig(cand): {"status": "rejected", "severity": 70, "updated_at": NOW - DAY}
    })
    assert build_queue([cand], now=NOW, view=view).top is not None


def test_severity_rise_below_the_threshold_still_waits():
    cand = _cand("verification", key="f4", severity=84)
    view = LedgerView(by_signature={
        _sig(cand): {"status": "rejected", "severity": 70, "updated_at": NOW - DAY}
    })
    assert [r.code for r in build_queue([cand], now=NOW, view=view).rejected] == [
        "failed_signature"
    ]


# ── eligibility ─────────────────────────────────────────────────────────────


def test_low_severity_is_rejected_with_its_number():
    cand = _cand("learning", key="low", severity=30)
    plan = build_queue([cand], now=NOW, cfg=QueueConfig(min_severity=40))
    assert [r.code for r in plan.rejected] == ["low_severity"]
    assert "30" in plan.rejected[0].detail


def test_per_domain_cap_keeps_the_worst_and_names_the_rest():
    cands = [_cand("learning", key=f"cap{i}", severity=90 - i) for i in range(7)]
    plan = build_queue(cands, now=NOW, cfg=QueueConfig(per_domain_max=2, max_candidates=5))
    assert len(plan.rejections_for("domain_cap")) == 5
    assert len(plan.selected) == 2
    assert plan.selected[0].candidate.severity == 90


def test_domain_filter_drops_others_and_reports_the_hole():
    learning = _cand("learning", key="dl", severity=90)
    routing = _cand("routing", key="dr", severity=50)
    plan = build_queue([learning, routing], now=NOW, domains=["routing"])
    assert [r.code for r in plan.rejected] == ["domain_filtered"]
    assert plan.selected[0].candidate.domain == "routing"
    assert plan.holes == ("learning",)


def test_unknown_domain_filter_is_a_caller_error():
    with pytest.raises(ValueError, match="unknown domain filter"):
        build_queue([], now=NOW, domains=["nonsense"])


# ── credit ──────────────────────────────────────────────────────────────────


def test_credit_is_neutral_before_the_minimum_sample():
    assert credit_adjustment({("db:L1", "a"): {"n": CREDIT_MIN_N - 1, "merged": 2}}, "db:L1") == 0
    assert credit_adjustment(None, "db:L1") == 0


def test_credit_bonus_and_penalty_are_bounded():
    assert credit_adjustment({("db:L1", "a"): {"n": 3, "merged": 3}}, "db:L1") == 4
    assert credit_adjustment({("db:L1", "a"): {"n": 4, "merged": 0}}, "db:L1") == -4
    assert credit_adjustment({("db:L1", "a"): {"n": 4, "merged": 2}}, "db:L1") == 0


def test_credit_aggregates_across_lenses():
    stats = {("db:L1", "a"): {"n": 2, "merged": 2}, ("db:L1", "b"): {"n": 2, "merged": 0}}
    agg = credit_for_source(stats, "db:L1")
    assert (agg["n"], agg["merged"]) == (4, 2)
    assert agg["rate"] == 0.5


def test_credit_never_leaves_the_severity_range():
    high = _cand("learning", key="hi", severity=99, source="db:HOT")
    low = _cand("learning", key="lo", severity=1, source="db:COLD")
    credits = {("db:HOT", "a"): {"n": 3, "merged": 3},
               ("db:COLD", "a"): {"n": 3, "merged": 0}}
    assert effective_severity(high, credits) == 100
    assert effective_severity(low, credits) == 0


def test_credit_can_reorder_two_equal_candidates():
    good = _cand("learning", key="good", severity=60, source="db:GOOD")
    bad = _cand("learning", key="bad", severity=60, source="db:BAD")
    credits = {("db:GOOD", "a"): {"n": 3, "merged": 3},
               ("db:BAD", "a"): {"n": 3, "merged": 0}}
    plan = build_queue([bad, good], now=NOW, credits=credits, cfg=QueueConfig(max_candidates=2))
    assert [i.candidate.source for i in plan.selected] == ["db:GOOD", "db:BAD"]


# ── config + serialisation ──────────────────────────────────────────────────


def test_queue_config_rejects_nonsense():
    with pytest.raises(ValueError, match="max_candidates"):
        QueueConfig(max_candidates=0)
    with pytest.raises(ValueError, match="per_domain_max"):
        QueueConfig(per_domain_max=0)
    with pytest.raises(ValueError, match="min_severity"):
        QueueConfig(min_severity=101)
    with pytest.raises(ValueError, match="ttl"):
        QueueConfig(failed_signature_ttl_days=-1)


def test_queue_config_from_mapping_ignores_keys_it_does_not_own():
    cfg = QueueConfig.from_mapping({"max_candidates": 4, "min_severity": 55,
                                    "unrelated": "ignored"})
    assert (cfg.max_candidates, cfg.min_severity) == (4, 55)


def test_plan_dict_is_json_safe_and_names_every_drop():
    cand = _cand("learning", key="j", severity=70)
    plan = build_queue([cand], now=NOW)
    payload = plan.to_dict()
    assert payload["selected"][0]["candidate_id"] == cand.candidate_id
    assert payload["examined"] == 1
    assert set(payload) == {"examined", "selected", "backlog", "rejected", "holes"}


def test_rejection_rejects_an_unknown_code():
    with pytest.raises(ValueError, match="unknown reject code"):
        Rejection("c", "s", "because-i-said-so")
