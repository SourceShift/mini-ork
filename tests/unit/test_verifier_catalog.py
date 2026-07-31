"""P3: behavioral verifier catalog (load, rank, score, malformed-card)."""
from __future__ import annotations

import textwrap

import pytest

from mini_ork.verify.catalog import (
    VerifierCard,
    VerifierStats,
    card_score,
    load_cards,
    rank_verifiers,
)


def _stats(discrimination=0.8, consistency=0.9, fuzz_penalty=0.05, n=10):
    return VerifierStats(
        discrimination=discrimination,
        consistency=consistency,
        fuzz_penalty=fuzz_penalty,
        n_observations=n,
    )


def _card(name, *, kind="behavioral", surface="api", cost=0.0, recipe="", stats=None):
    return VerifierCard(
        name=name,
        kind=kind,
        surface=surface,
        cost=cost,
        recipe=recipe,
        stats=stats or _stats(),
    )


# ─── card_score ─────────────────────────────────────────────────────────── #
def test_card_score_uses_irt_formula():
    s = _stats(discrimination=0.8, consistency=0.5, fuzz_penalty=0.05)
    c = _card("x", cost=1.0, stats=s)
    # 0.8 * 0.5 * (1/(1+1.0)) - 0.05 = 0.4 * 0.5 - 0.05 = 0.15
    assert card_score(c) == pytest.approx(0.15)


def test_card_score_higher_cost_lowers_score():
    s = _stats(discrimination=0.8, consistency=0.8, fuzz_penalty=0.0)
    lo = _card("lo", cost=0.0, stats=s)
    hi = _card("hi", cost=10.0, stats=s)
    assert card_score(hi) < card_score(lo)


def test_card_score_fuzz_subtracts_directly():
    s_no_fuzz = _stats(discrimination=0.6, consistency=0.6, fuzz_penalty=0.0)
    s_with_fuzz = _stats(discrimination=0.6, consistency=0.6, fuzz_penalty=0.1)
    no_fuzz = _card("a", cost=0.0, stats=s_no_fuzz)
    with_fuzz = _card("b", cost=0.0, stats=s_with_fuzz)
    assert card_score(with_fuzz) == pytest.approx(card_score(no_fuzz) - 0.1)


# ─── rank_verifiers ──────────────────────────────────────────────────────── #
def test_rank_verifiers_desc_by_score_then_asc_by_cost():
    high_score_high_cost = _card("hs_hc", cost=2.0, stats=_stats(discrimination=0.9, consistency=0.9))
    high_score_low_cost = _card("hs_lc", cost=0.1, stats=_stats(discrimination=0.9, consistency=0.9))
    low_score = _card("low", cost=0.0, stats=_stats(discrimination=0.3, consistency=0.3))

    ranked = rank_verifiers([low_score, high_score_high_cost, high_score_low_cost])
    assert [c.name for c in ranked] == ["hs_lc", "hs_hc", "low"]


def test_rank_verifiers_stable_on_tie():
    a = _card("alpha", cost=0.0)
    b = _card("beta", cost=0.0)
    c = _card("gamma", cost=0.0)
    # identical score, identical cost → input order preserved (stable sort)
    assert [x.name for x in rank_verifiers([a, b, c])] == ["alpha", "beta", "gamma"]


def test_rank_verifiers_handles_empty():
    assert rank_verifiers([]) == []


# ─── dataclass validation ───────────────────────────────────────────────── #
def test_verifier_card_rejects_unknown_kind():
    with pytest.raises(ValueError, match="kind must be one of"):
        _card("bad", kind="unknown_kind")


def test_verifier_card_rejects_unknown_surface():
    with pytest.raises(ValueError, match="surface must be one of"):
        _card("bad", surface="graphql")


def test_verifier_stats_rejects_negative_discrimination():
    with pytest.raises(ValueError, match="discrimination"):
        VerifierStats(discrimination=-0.1, consistency=0.5, fuzz_penalty=0.0)


def test_verifier_stats_rejects_negative_n_observations():
    with pytest.raises(ValueError, match="n_observations"):
        VerifierStats(discrimination=0.5, consistency=0.5, fuzz_penalty=0.0, n_observations=-1)


# ─── load_cards ─────────────────────────────────────────────────────────── #
def test_load_cards_parses_well_formed_card(tmp_path):
    (tmp_path / "api_contract.card.yaml").write_text(
        textwrap.dedent(
            """\
            name: api_contract
            kind: behavioral
            surface: api
            cost: 0.10
            stats:
              discrimination: 0.78
              consistency:   0.92
              fuzz_penalty:  0.04
              n_observations: 214
            """
        )
    )
    cards = load_cards(tmp_path)
    assert [c.name for c in cards] == ["api_contract"]
    c = cards[0]
    assert c.surface == "api"
    assert c.cost == pytest.approx(0.10)
    assert c.stats.discrimination == pytest.approx(0.78)


def test_load_cards_is_sorted_by_filename(tmp_path):
    for n in ("zeta", "alpha", "mu"):
        (tmp_path / f"{n}.card.yaml").write_text(
            textwrap.dedent(
                f"""\
                name: {n}
                kind: behavioral
                surface: api
                cost: 0.0
                stats:
                  discrimination: 0.5
                  consistency: 0.5
                  fuzz_penalty: 0.0
                """
            )
        )
    cards = load_cards(tmp_path)
    assert [c.name for c in cards] == ["alpha", "mu", "zeta"]


def test_load_cards_raises_on_malformed_card_with_path_context(tmp_path):
    (tmp_path / "broken.card.yaml").write_text(
        textwrap.dedent(
            """\
            name: broken
            kind: not_a_real_kind
            cost: 0.0
            stats:
              discrimination: 0.5
              consistency: 0.5
              fuzz_penalty: 0.0
            """
        )
    )
    with pytest.raises(ValueError, match=r"broken\.card\.yaml"):
        load_cards(tmp_path)


def test_load_cards_raises_on_missing_required_name(tmp_path):
    (tmp_path / "noname.card.yaml").write_text(
        textwrap.dedent(
            """\
            kind: behavioral
            cost: 0.0
            stats:
              discrimination: 0.5
              consistency: 0.5
              fuzz_penalty: 0.0
            """
        )
    )
    # YAML parses fine; missing 'name' surfaces in the dataclass ctor.
    with pytest.raises(ValueError):
        load_cards(tmp_path)


def test_load_cards_returns_empty_for_empty_dir(tmp_path):
    assert load_cards(tmp_path) == []