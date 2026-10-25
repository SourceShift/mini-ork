"""Unit tests: mini_ork.rsi.candidates.

The point of these tests is to freeze two contracts the whole cycle depends on:

* severity is integer arithmetic with pinned outputs, so tuning cannot silently
  reorder the queue;
* identity is built from the *shape* of the evidence, so a table that grows or a
  harvest that runs a minute later cannot mint a new candidate and defeat the
  "already fixed" / "already failed" lookups.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from mini_ork.rsi import candidates as C  # noqa: E402

NOW = 1_760_000_000


def _build(**overrides):
    kwargs = dict(
        domain="learning",
        source="db:L1",
        kind="producer_missing",
        title="learning_pass_stats is never written",
        evidence_key="table:learning_pass_stats:empty",
        files=("mini_ork/learning/pass_stats.py",),
        now=NOW,
    )
    kwargs.update(overrides)
    return C.Candidate.build(**kwargs)


# ── severity ────────────────────────────────────────────────────────────────


def test_severity_base_plus_domain_bonus_is_pinned():
    # producer_missing 70 + reach 0 + age 0 + learning 4 - blast 0
    assert C.severity("producer_missing", domain="learning") == 74
    # coverage_gap 30 + routing 3
    assert C.severity("coverage_gap", domain="routing") == 33
    # silent_swallow 62 + verification 5
    assert C.severity("silent_swallow", domain="verification") == 67


def test_reach_is_log_scaled_and_capped():
    assert C.severity("silent_swallow", reach=1, domain="verification") == 67
    assert C.severity("silent_swallow", reach=101, domain="verification") == 77
    # isqrt(9999) = 99 -> capped at 15; growth past the cap changes nothing.
    assert C.severity("silent_swallow", reach=10_000, domain="verification") == 82
    assert C.severity("silent_swallow", reach=1_000_000, domain="verification") == 82


def test_persistence_adds_two_per_week_and_caps_at_ten():
    assert C.severity("stale_signal", age_days=6, domain="learning") == 42
    assert C.severity("stale_signal", age_days=7, domain="learning") == 44
    assert C.severity("stale_signal", age_days=40, domain="learning") == 52
    assert C.severity("stale_signal", age_days=70, domain="learning") == 52


def test_blast_radius_is_a_cost_capped_at_eight():
    assert C.severity("producer_missing", blast_radius=1, domain="learning") == 74
    assert C.severity("producer_missing", blast_radius=4, domain="learning") == 68
    assert C.severity("producer_missing", blast_radius=6, domain="learning") == 66
    assert C.severity("producer_missing", blast_radius=20, domain="learning") == 66


def test_severity_clamps_to_100():
    assert C.severity("producer_missing", reach=10 ** 6, age_days=365,
                      domain="verification") == 100


def test_unknown_kind_and_domain_fail_loudly():
    with pytest.raises(KeyError):
        C.severity("not_a_kind")
    with pytest.raises(KeyError):
        C.severity("producer_missing", domain="not_a_domain")
    with pytest.raises(ValueError):
        _build(domain="nope")
    with pytest.raises(ValueError):
        _build(kind="nope")


# ── identity ────────────────────────────────────────────────────────────────


def test_evidence_volume_does_not_change_identity():
    a = _build(evidence={"rows": 0, "sample": []})
    b = _build(evidence={"rows": 14_947, "sample": ["x", "y"]})
    assert a.signature == b.signature
    assert a.candidate_id == b.candidate_id


def test_now_does_not_change_identity():
    assert _build(now=NOW).signature == _build(now=NOW + 86_400).signature


def test_changing_the_fix_scope_changes_identity():
    a = _build()
    b = _build(files=("mini_ork/learning/pass_stats.py", "mini_ork/learning/ledger.py"))
    assert a.signature != b.signature


def test_changing_the_evidence_key_changes_identity():
    a = _build()
    b = _build(evidence_key="table:reflection_log:empty")
    assert a.signature != b.signature


def test_files_are_normalized_and_deduplicated():
    a = _build(files=("./mini_ork/learning/pass_stats.py",))
    b = _build(files=("mini_ork/learning/pass_stats.py", "mini_ork/learning/pass_stats.py"))
    assert a.files == ("mini_ork/learning/pass_stats.py",)
    assert a.signature == b.signature


def test_absolute_paths_are_rejected():
    with pytest.raises(ValueError):
        C.normalize_path("/etc/passwd")
    with pytest.raises(ValueError):
        _build(files=("/etc/passwd",))


def test_candidate_id_is_prefixed_signature():
    cand = _build()
    assert cand.candidate_id == "rsi-" + cand.signature
    assert len(cand.signature) == 16


def test_blast_radius_derives_from_files():
    assert _build().blast_radius == 1
    assert _build(files=("a.py", "b.py", "c.py")).blast_radius == 3


# ── record behaviour ────────────────────────────────────────────────────────


def test_build_derives_severity_from_inputs():
    cand = _build(reach=101, age_days=7, files=("mini_ork/learning/pass_stats.py",))
    # producer_missing 70 + isqrt(100)=10 + 2 + 4 - 0
    assert cand.severity == 86
    assert cand.first_seen_at == NOW


def test_seen_increments_without_moving_first_seen():
    cand = _build()
    twice = cand.seen().seen()
    assert twice.times_seen == 3
    assert twice.first_seen_at == cand.first_seen_at
    assert twice.signature == cand.signature


def test_json_round_trip_preserves_the_record():
    cand = _build(evidence={"table": "learning_pass_stats"}, fix_hint="wire the writer")
    again = C.Candidate.from_json(cand.to_json())
    assert again == cand
    assert again.evidence == {"table": "learning_pass_stats"}


def test_dedupe_keeps_max_severity_and_unions_evidence():
    # Same signature (reach is not part of it), different severity and evidence.
    weak = _build(reach=1, evidence={"rows": 0})
    strong = _build(reach=500, evidence={"writers": 0})
    assert weak.signature == strong.signature
    merged = C.dedupe([weak, strong])
    assert len(merged) == 1
    assert merged[0].severity == max(weak.severity, strong.severity)
    assert merged[0].evidence == {"rows": 0, "writers": 0}


def test_dedupe_keeps_distinct_signatures():
    a = _build()
    b = _build(evidence_key="table:reflection_log:empty")
    assert len(C.dedupe([a, b])) == 2
