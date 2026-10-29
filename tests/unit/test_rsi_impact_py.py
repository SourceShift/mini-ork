"""Tests for ``mini_ork.rsi.impact`` — the G12 impact statistics core.

Hermetic: no file, database, network or subprocess access. Every case is taken
from the rrl-1 kickoff's ``Tests`` section.
"""

from __future__ import annotations

import random

import pytest

from mini_ork.rsi.impact import (
    SPEND_Z,
    SPENDING_MODES,
    STRATA_DEFAULT,
    PairedTable,
    binom_tail,
    effective_families,
    family_collapse,
    guard_check,
    look_alpha,
    paired_counts,
    ratchet,
    sage_decision,
    split_items,
    stable_items,
    thompson_order,
)

# --------------------------------------------------------------- constants


def test_constants():
    assert SPEND_Z == 3.39
    assert SPENDING_MODES == ("horizon_free", "per_decision")
    assert STRATA_DEFAULT == ""


# ------------------------------------------------------------ paired_counts


def test_paired_counts_kickoff_example():
    base = {"a": 0, "b": 1, "c": 1, "d": 0}
    head = {"a": 1, "b": 0, "c": 1, "d": 0, "e": 1}
    table = paired_counts(base, head)
    assert isinstance(table, PairedTable)
    assert (table.wins, table.losses, table.both_pass, table.both_fail, table.missing) == (
        1,
        1,
        1,
        1,
        1,
    )
    assert table.n == 4
    assert table.base_passed == 2
    assert table.discordant == 2


def test_paired_counts_explicit_items():
    base = {"a": 0, "b": 1, "c": 1, "d": 0}
    head = {"a": 1, "b": 0, "c": 1, "d": 0, "e": 1}
    table = paired_counts(base, head, items=["a", "zz"])
    assert table.wins == 1
    assert table.missing == 1


def test_paired_counts_threshold_boundary():
    assert paired_counts({"x": 0.5}, {"x": 0.5}).both_pass == 1
    assert paired_counts({"x": 0.49}, {"x": 0.49}).both_fail == 1


def test_paired_table_to_dict():
    table = PairedTable(1, 2, 3, 4, 5)
    assert table.to_dict() == {
        "wins": 1,
        "losses": 2,
        "both_pass": 3,
        "both_fail": 4,
        "missing": 5,
        "n": 10,
        "base_passed": 5,
        "discordant": 3,
    }


# -------------------------------------------------------------- binom_tail


def test_binom_tail_all_successes():
    assert binom_tail(7, 7, 0.5) == 0.5**7


def test_binom_tail_one_below_all():
    assert abs(binom_tail(7, 8, 0.5) - 9 / 256) < 1e-12


def test_binom_tail_edges():
    assert binom_tail(0, 5, 0.3) == 1.0
    assert binom_tail(6, 5, 0.3) == 0.0


def test_binom_tail_invalid_parameters():
    with pytest.raises(ValueError):
        binom_tail(1, 5, 1.1)
    with pytest.raises(ValueError):
        binom_tail(1, -1, 0.5)


# -------------------------------------------------------------- look_alpha


def test_look_alpha_first_look():
    assert look_alpha(0.05, 1) == pytest.approx(0.05 / 3.39, rel=1e-12)


def test_look_alpha_per_decision():
    assert look_alpha(0.05, 1, spending="per_decision") == 0.05


def test_look_alpha_look_zero_raises():
    with pytest.raises(ValueError):
        look_alpha(0.05, 0)


def test_look_alpha_total_spend_below_alpha0():
    assert sum(look_alpha(0.05, k) for k in range(1, 10001)) < 0.05


# ---------------------------------------------------------- sage_decision

ALPHA = 0.0147


def _table(wins, losses, both_pass=0, both_fail=0, missing=0):
    return PairedTable(wins, losses, both_pass, both_fail, missing)


def test_sage_accepts_seven_wins():
    result = sage_decision(_table(7, 0), alpha=ALPHA)
    assert result["accept"] is True
    assert result["reason"] == "accepted"
    assert result["p_value"] == round(1 / 128, 6)


def test_sage_six_wins_not_significant():
    result = sage_decision(_table(6, 0), alpha=ALPHA)
    assert result["reason"] == "not_significant"


def test_sage_regression_budget_wins_by_default():
    result = sage_decision(_table(7, 1), alpha=ALPHA)
    assert result["reason"] == "regression_budget"


def test_sage_full_budget_falls_through_to_significance():
    result = sage_decision(_table(7, 1), alpha=ALPHA, max_regression_rate=1.0)
    assert result["reason"] == "not_significant"
    assert result["p_value"] == round(9 / 256, 6)


def test_sage_no_discordant_pairs():
    result = sage_decision(_table(0, 0, both_pass=4), alpha=ALPHA)
    assert result["reason"] == "no_discordant_pairs"
    assert result["p_value"] == 1.0


def test_sage_net_loss():
    result = sage_decision(_table(2, 3), alpha=ALPHA, max_regression_rate=1.0)
    assert result["reason"] == "net_loss"
    assert result["delta"] == -1.0


def test_sage_validation():
    with pytest.raises(ValueError):
        sage_decision(_table(7, 0), alpha=ALPHA, lam=0)
    with pytest.raises(ValueError):
        sage_decision(_table(7, 0), alpha=ALPHA, max_regression_rate=-0.1)


def test_sage_returns_exact_key_set():
    result = sage_decision(_table(7, 0), alpha=ALPHA)
    assert set(result) == {
        "accept",
        "reason",
        "wins",
        "losses",
        "delta",
        "p_value",
        "alpha",
        "lam",
        "q",
        "regression_rate",
        "n",
        "base_passed",
    }


# ----------------------------------------------------------- stable_items


def test_stable_items_kickoff_example():
    stable, flaky = stable_items(
        [{"a": 1, "b": 0, "c": 1}, {"a": 1, "b": 1, "c": 1, "d": 0}]
    )
    assert stable == frozenset({"a", "c"})
    assert flaky == frozenset({"b"})


def test_stable_items_empty():
    assert stable_items([]) == (frozenset(), frozenset())


# ------------------------------------------------------------ split_items

SECRET = b"unit-test-secret"
ITEMS40 = [f"case-{i:02d}" for i in range(40)]
STRATA_40 = {
    **{f"case-{i:02d}": "hard_negative" for i in range(17)},
    **{f"case-{i:02d}": "correct" for i in range(17, 40)},
}


def test_split_items_deterministic():
    first = split_items(ITEMS40, secret=SECRET, epoch=1, strata=STRATA_40)
    second = split_items(ITEMS40, secret=SECRET, epoch=1, strata=STRATA_40)
    assert first == second


def test_split_items_epoch_and_secret_redraw():
    epoch1 = split_items(ITEMS40, secret=SECRET, epoch=1, strata=STRATA_40)
    epoch2 = split_items(ITEMS40, secret=SECRET, epoch=2, strata=STRATA_40)
    other = split_items(ITEMS40, secret=b"other-secret", epoch=1, strata=STRATA_40)
    assert epoch1["dev"] != epoch2["dev"]
    assert epoch1["dev"] != other["dev"]


def test_split_items_stratified_counts():
    result = split_items(ITEMS40, secret=SECRET, epoch=1, strata=STRATA_40)
    dev_hard_negative = sum(1 for x in result["dev"] if STRATA_40[x] == "hard_negative")
    dev_correct = sum(1 for x in result["dev"] if STRATA_40[x] == "correct")
    assert dev_hard_negative == 8
    assert dev_correct == 12


def test_split_items_partition():
    result = split_items(ITEMS40, secret=SECRET, epoch=1, strata=STRATA_40)
    assert set(result["dev"]) | set(result["sealed"]) == set(ITEMS40)
    assert not (set(result["dev"]) & set(result["sealed"]))


def test_split_items_invalid_arguments():
    with pytest.raises(ValueError):
        split_items(ITEMS40, secret=b"", epoch=1)
    with pytest.raises(ValueError):
        split_items(ITEMS40, secret=SECRET, epoch=1, dev_fraction=1.0)


# ------------------------------------------------------------- guard_check


def test_guard_check_lower_is_better():
    ok = guard_check(
        {"false_accept": 5.0},
        {"false_accept": 5.0},
        tolerances={"false_accept": 0.0},
        lower_is_better=("false_accept",),
    )
    assert ok == {"ok": True, "violations": []}

    bad = guard_check(
        {"false_accept": 5.0},
        {"false_accept": 6.0},
        tolerances={"false_accept": 0.0},
        lower_is_better=("false_accept",),
    )
    assert bad["ok"] is False
    assert bad["violations"][0]["reason"] == "worse"


def test_guard_check_missing_metric():
    result = guard_check({"m": 1.0}, {}, tolerances={"m": 0.5})
    assert result["ok"] is False
    assert result["violations"] == [{"metric": "m", "reason": "missing"}]


def test_guard_check_higher_is_better_within_tolerance():
    result = guard_check(
        {"accuracy": 0.8}, {"accuracy": 0.79}, tolerances={"accuracy": 0.02}
    )
    assert result == {"ok": True, "violations": []}


# ------------------------------------------------------------------ ratchet


def test_ratchet_empty_history():
    assert ratchet([]) == {
        "peak": None,
        "peak_index": None,
        "declines": 0,
        "revert": False,
        "n": 0,
    }


def test_ratchet_three_trailing_declines_revert():
    result = ratchet([0.5, 0.6, 0.57, 0.57, 0.57])
    assert result["peak"] == 0.6
    assert result["declines"] == 3
    assert result["revert"] is True


def test_ratchet_stops_at_first_non_decline():
    result = ratchet([0.6, 0.59, 0.57, 0.57], drop=0.02)
    assert result["declines"] == 2
    assert result["revert"] is False


def test_ratchet_new_peak_at_end():
    result = ratchet([0.5, 0.6, 0.65])
    assert result["peak"] == 0.65
    assert result["declines"] == 0


def test_ratchet_consecutive_validation():
    with pytest.raises(ValueError):
        ratchet([0.5, 0.6], consecutive=0)


# --------------------------------------------------------- family diversity


def test_effective_families_empty_and_single():
    assert effective_families([]) == 0.0
    assert effective_families(["A"] * 6) == 1.0


def test_family_collapse_narrow_plateau():
    result = family_collapse(["A"] * 6, [False] * 6)
    assert result["collapse"] is True
    assert result["n_eff"] == 1.0


def test_family_collapse_diverse():
    result = family_collapse(["A", "B", "C", "A", "B", "C"], [False] * 6)
    assert result["collapse"] is False
    assert result["n_eff"] == pytest.approx(3.0, abs=1e-6)


def test_family_collapse_accept_breaks_plateau():
    result = family_collapse(["A"] * 6, [False] * 5 + [True])
    assert result["plateau"] is False


def test_family_collapse_short_history():
    result = family_collapse(["A"] * 5, [False] * 5)
    assert result["plateau"] is False


def test_family_collapse_length_mismatch():
    with pytest.raises(ValueError):
        family_collapse(["A", "B"], [False])


# --------------------------------------------------------- thompson_order


def test_thompson_deterministic_per_rng():
    options = [
        {"id": "t1", "family": "f1", "prior": 0.5},
        {"id": "t2", "family": "f1", "prior": 0.5},
        {"id": "t3", "family": "f2", "prior": 0.2},
    ]
    attempts = [
        {"family": "f1", "technique": "t1", "success": True},
        {"family": "f1", "technique": "t2", "success": False},
        {"family": "f2", "technique": "t3", "success": True},
    ]
    first = thompson_order(options, attempts, rng=random.Random(7))
    second = thompson_order(options, attempts, rng=random.Random(7))
    assert first == second


def test_thompson_successful_family_usually_first():
    options = [
        {"id": "g1", "family": "good", "prior": 0.5},
        {"id": "b1", "family": "bad", "prior": 0.5},
    ]
    attempts = (
        [{"family": "good", "technique": "g1", "success": True}] * 20
        + [{"family": "bad", "technique": "b1", "success": False}] * 20
    )
    first_place = sum(
        1
        for seed in range(200)
        if thompson_order(options, attempts, rng=random.Random(seed))[0] == "g1"
    )
    assert first_place >= 190


def test_thompson_prior_dominates_without_attempts():
    options = [
        {"id": "hi", "family": "f1", "prior": 1.0},
        {"id": "lo", "family": "f2", "prior": 0.0},
    ]
    first_place = sum(
        1
        for seed in range(200)
        if thompson_order(options, [], rng=random.Random(seed), prior_weight=10.0)[0]
        == "hi"
    )
    assert first_place >= 170


def test_thompson_validation_and_empty():
    options = [{"id": "t1", "family": "f1", "prior": 0.5}]
    with pytest.raises(ValueError):
        thompson_order(options, [], rng=random.Random(1), tau=0)
    assert thompson_order([], [], rng=random.Random(1)) == []
