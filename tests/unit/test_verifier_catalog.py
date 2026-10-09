"""P3: behavioral verifier catalog (load, rank, score, malformed-card)."""
from __future__ import annotations

import importlib.util
import json
import os
import textwrap
from pathlib import Path

import pytest
import yaml

from mini_ork.verify.catalog import (
    VerifierCard,
    VerifierStats,
    card_score,
    load_cards,
    rank_verifiers,
)

REPO_ROOT = Path(__file__).resolve().parents[2]


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


# ─── wiring: the api_contract card is consumed by a recipe ───────────────── #
def _load_recipe_dispatcher():
    """Import the recipe-local api_contract dispatcher by path (its directory
    is hyphenated, so it is not importable by name)."""
    path = REPO_ROOT / "recipes" / "post-mvp-delivery" / "verifiers" / "api_contract.py"
    spec = importlib.util.spec_from_file_location("mo_pmd_api_contract", path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_repo_api_contract_card_is_wired_to_its_consuming_recipe():
    """Guards the "producer built, never consumed" shape: the shipped card must
    name the recipe that actually lists the verifier in its success_verifiers."""
    cards = {c.name: c for c in load_cards(REPO_ROOT / "verifiers", "*.card.yaml")}
    assert "api_contract" in cards
    card = cards["api_contract"]
    # A non-empty recipe pointer is what makes the card consumed rather than orphaned.
    assert card.recipe == "recipes/post-mvp-delivery"

    recipe_dir = REPO_ROOT / card.recipe
    assert recipe_dir.is_dir(), f"card recipe {card.recipe!r} is not a recipe dir"
    contract = yaml.safe_load((recipe_dir / "artifact_contract.yaml").read_text(encoding="utf-8"))
    assert "verifiers/api_contract.py" in (contract.get("success_verifiers") or [])
    # The recipe-local dispatcher and the observable it defaults to must exist,
    # or the wiring resolves to the (observable-less) top-level seed and abstains.
    assert (recipe_dir / "verifiers" / "api_contract.py").is_file()
    assert (recipe_dir / "verifiers" / "api_contract.observable.yaml").is_file()


def test_api_contract_dispatcher_bridges_unverified_to_vacuous_envelope():
    """The bridge turns the behavioral engine's UNVERIFIED (exit 1, multi-line
    verdict) into the dispatcher's single-line vacuous envelope at exit 0, so
    `mini_ork/cli/verify.py` records it as unmeasured — never a pass, never a
    hard fail. PROVEN/REFUTED pass through unchanged."""
    mod = _load_recipe_dispatcher()

    unverified = json.dumps({"status": "UNVERIFIED", "evidence": "no staging_url"})
    out, code = mod._bridge(unverified + "\n", 1)
    assert code == 0
    # The engine's own verdict is preserved, then the vacuous envelope is last
    # (verify.py scans backwards for the last single-line JSON object).
    assert out.startswith(unverified)
    envelope = json.loads(out.splitlines()[-1])
    assert envelope["verdict"] == "vacuous"
    assert envelope["pass"] is None

    # A verdict with no trailing newline still gets the envelope on its own line.
    out2, code2 = mod._bridge(unverified, 1)
    assert code2 == 0
    assert json.loads(out2.splitlines()[-1])["pass"] is None

    proven = json.dumps({"status": "PROVEN", "pass": True})
    assert mod._bridge(proven, 0) == (proven, 0)

    refuted = json.dumps({"status": "REFUTED", "pass": False})
    assert mod._bridge(refuted, 1) == (refuted, 1)


def test_api_contract_dispatcher_defaults_observable_env(monkeypatch, capsys):
    """`main()` exports MO_OBSERVABLE_SPEC pointing at the recipe-local descriptor
    when the operator has not supplied one, then abstains (vacuous, exit 0) on
    the unprobeable default surface."""
    mod = _load_recipe_dispatcher()
    monkeypatch.delenv("MO_OBSERVABLE_SPEC", raising=False)
    monkeypatch.delenv("MO_BEHAV_SURFACE", raising=False)
    monkeypatch.setenv("MO_STAGING_URL", "")  # empty base → no protocol → unreachable
    try:
        rc = mod.main()
        assert rc == 0
        spec_env = os.environ.get("MO_OBSERVABLE_SPEC", "")
        assert os.path.basename(spec_env) == "api_contract.observable.yaml"
        assert os.path.isfile(spec_env)
        out = capsys.readouterr().out
    finally:
        # main() setdefaults the var; remove it so the addition cannot leak.
        os.environ.pop("MO_OBSERVABLE_SPEC", None)
    envelope = [ln for ln in out.splitlines() if ln.startswith("{")]
    assert envelope and json.loads(envelope[-1])["pass"] is None
