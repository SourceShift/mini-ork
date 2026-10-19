"""Unit tests for the verifier mutation-discrimination suite.

Covers the nine kickoff scenarios: the six named mutations, kill-rate
scoring (including UNVERIFIED abstentions and an unproven baseline),
admission thresholds, catalog gating, certificate digest stability, and
sidecar persistence.
"""
from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

import pytest

from mini_ork.certify import certificate
from mini_ork.gates import verifier_mutation
from mini_ork.verify import catalog

PROVEN = "PROVEN"
REFUTED = "REFUTED"
UNVERIFIED = "UNVERIFIED"

RICH = {
    "surface": "api",
    "target": "http://example.test/api",
    "expect_status": [200],
    "expect_body": {"name": "alpha", "count": 3, "ok": True, "tags": ["a", "b"]},
}


@dataclass
class _Verdict:
    status: str


def _verdict(status: str) -> _Verdict:
    return _Verdict(status)


def _changed_top_level_keys(original: dict, mutant: dict) -> set:
    keys = set(original) | set(mutant)
    return {k for k in keys if original.get(k) != mutant.get(k)}


# --------------------------------------------------------------------------- #
# 1. mutations: exactly one defect, deep copy, ValueError when N/A
# --------------------------------------------------------------------------- #


def test_each_mutation_changes_exactly_one_field_and_deep_copies():
    snapshot = copy.deepcopy(RICH)
    for name in verifier_mutation.MUTATIONS:
        mutant = verifier_mutation.mutate(RICH, name)
        assert isinstance(mutant, dict), name
        assert mutant != RICH, name
        assert mutant is not RICH, name
        changed = _changed_top_level_keys(RICH, mutant)
        assert len(changed) == 1, (name, changed)
        assert changed <= {"expect_body", "expect_status"}, (name, changed)
        # deep copy: the mutated container is a fresh object
        (key,) = changed
        assert mutant[key] is not RICH[key], name
        # nested mutable containers never alias the original
        assert mutant["expect_body"] is not RICH["expect_body"], name
        assert mutant["expect_body"]["tags"] is not RICH["expect_body"]["tags"], name
        assert RICH == snapshot, name


def test_mutation_shapes_match_the_kickoff():
    assert verifier_mutation.mutate(RICH, "value_flip")["expect_body"]["name"] != "alpha"
    assert verifier_mutation.mutate(RICH, "field_drop")["expect_body"] == {
        "count": 3,
        "ok": True,
        "tags": ["a", "b"],
    }
    assert verifier_mutation.mutate(RICH, "type_swap")["expect_body"]["name"] == 0
    assert verifier_mutation.mutate(RICH, "list_reorder")["expect_body"]["tags"] == ["b", "a"]
    assert verifier_mutation.mutate(RICH, "truncate_string")["expect_body"]["name"] == "alph"
    assert verifier_mutation.mutate(RICH, "status_swap")["expect_status"] == [500]


def test_value_flip_flips_bools_numbers_and_falls_back_to_visible():
    assert verifier_mutation.mutate({"expect_body": {"ok": True}}, "value_flip")["expect_body"][
        "ok"
    ] is False
    assert verifier_mutation.mutate({"expect_body": {"n": 3}}, "value_flip")["expect_body"]["n"] == -3
    visible = verifier_mutation.mutate(
        {"expect_body": {}, "expect_visible": ["Save", "Cancel"]}, "value_flip"
    )["expect_visible"]
    assert visible[0] != "Save" and visible[1] == "Cancel"


def test_type_swap_covers_each_declared_case():
    cases = [
        ({"s": "x"}, lambda v: isinstance(v, int) and not isinstance(v, bool)),
        ({"n": 3}, lambda v: isinstance(v, str)),
        ({"b": True}, lambda v: isinstance(v, str) and v.lower() == "false"),
        ({"l": [1, 2]}, lambda v: isinstance(v, dict)),
    ]
    for original, check in cases:
        (key,) = original
        mutant = verifier_mutation.mutate({"expect_body": dict(original)}, "type_swap")
        assert check(mutant["expect_body"][key]), (key, mutant)


def test_field_drop_falls_back_to_required_schema_field():
    mutant = verifier_mutation.mutate(
        {"expect_body": {}, "expect_json_schema": {"required": ["a", "b"]}}, "field_drop"
    )
    assert mutant["expect_json_schema"]["required"] == ["b"]


@pytest.mark.parametrize(
    "observable, mutation",
    [
        ({"expect_body": {"tags": ["a"]}}, "value_flip"),
        ({"expect_body": {}}, "value_flip"),
        ({"expect_body": {}}, "field_drop"),
        ({"expect_body": {}}, "type_swap"),
        ({"expect_body": {"n": 3}}, "list_reorder"),
        ({"expect_body": {"n": 3}}, "truncate_string"),
        ({"expect_body": {"s": ""}}, "truncate_string"),
        ({"expect_body": {"n": 3}}, "status_swap"),
        (RICH, "no_such_mutation"),
    ],
)
def test_inapplicable_mutation_raises_value_error_naming_it(observable, mutation):
    with pytest.raises(ValueError, match=mutation):
        verifier_mutation.mutate(observable, mutation)


# --------------------------------------------------------------------------- #
# 2-3. kill rate: lenient runner 0.0, strict runner 1.0
# --------------------------------------------------------------------------- #


def test_lenient_runner_scores_zero():
    result = verifier_mutation.score(RICH, runner=lambda obs: _verdict(PROVEN))
    assert result["measured"] is True
    assert result["kill_score"] == 0.0
    assert result["killed"] == 0
    assert result["survived"] == 6
    assert result["abstained"] == 0
    assert {entry["name"] for entry in result["mutations"]} == set(verifier_mutation.MUTATIONS)


def test_strict_runner_scores_one():
    def strict_runner(observable):
        return _verdict(PROVEN if observable == RICH else REFUTED)

    result = verifier_mutation.score(RICH, runner=strict_runner)
    assert result["measured"] is True
    assert result["kill_score"] == 1.0
    assert result["killed"] == 6
    assert result["survived"] == 0
    assert result["abstained"] == 0


# --------------------------------------------------------------------------- #
# 4. UNVERIFIED is an abstention, never a kill
# --------------------------------------------------------------------------- #


def _mixed_runner(observable):
    if observable == RICH:
        return _verdict(PROVEN)
    if observable.get("expect_status") != RICH["expect_status"]:
        return _verdict(REFUTED)
    body = observable.get("expect_body") or {}
    if "name" not in body:
        return _verdict(REFUTED)
    name = body["name"]
    if name == "alpha":
        return _verdict(PROVEN)
    if isinstance(name, bool):
        return _verdict(PROVEN)
    if isinstance(name, int):
        return _verdict(PROVEN)
    if name == "alph":
        return _verdict(UNVERIFIED)
    return _verdict(UNVERIFIED)


def test_unverified_is_abstained_and_excluded_from_denominator():
    result = verifier_mutation.score(RICH, runner=_mixed_runner)
    assert result["measured"] is True
    assert result["killed"] == 2
    assert result["survived"] == 2
    assert result["abstained"] == 2
    assert result["kill_score"] == 0.5
    assert result["kill_score"] == result["killed"] / (result["killed"] + result["survived"])


def test_all_mutations_abstaining_does_not_crash():
    result = verifier_mutation.score(
        RICH, runner=lambda obs: _verdict(PROVEN if obs == RICH else UNVERIFIED)
    )
    assert result["killed"] == 0
    assert result["survived"] == 0
    assert result["abstained"] == 6
    assert result["kill_score"] is None


# --------------------------------------------------------------------------- #
# 5. baseline not proven -> unmeasured
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("status", [REFUTED, UNVERIFIED])
def test_unproven_baseline_is_unmeasured(status):
    result = verifier_mutation.score(RICH, runner=lambda obs: _verdict(status))
    assert result["measured"] is False
    assert result["kill_score"] is None
    assert "baseline" in result["reason"].lower()


# --------------------------------------------------------------------------- #
# 6. admit()
# --------------------------------------------------------------------------- #


def test_admit_none_true_and_default_floor(monkeypatch):
    monkeypatch.delenv("MO_VERIFIER_MUTATION_MIN", raising=False)
    assert verifier_mutation.admit(None) is True
    assert verifier_mutation.admit(0.79) is False
    assert verifier_mutation.admit(0.8) is True


def test_admit_env_threshold(monkeypatch):
    monkeypatch.setenv("MO_VERIFIER_MUTATION_MIN", "0.5")
    assert verifier_mutation.admit(0.6) is True
    assert verifier_mutation.admit(0.3) is False


def test_admit_explicit_min_kill_wins(monkeypatch):
    monkeypatch.delenv("MO_VERIFIER_MUTATION_MIN", raising=False)
    assert verifier_mutation.admit(0.85, min_kill=0.9) is False
    assert verifier_mutation.admit(0.9, min_kill=0.9) is True


# --------------------------------------------------------------------------- #
# 7. rank_verifiers admission gate
# --------------------------------------------------------------------------- #


def _cards(tmp_path):
    low = catalog.VerifierCard(
        name="low",
        kind="behavioral",
        surface="api",
        cost=1.0,
        stats=catalog.VerifierStats(0.5, 0.5, 0.0, 5),
        source=str(tmp_path / "low.card.yaml"),
    )
    unscored = catalog.VerifierCard(
        name="unscored",
        kind="behavioral",
        surface="api",
        cost=1.0,
        stats=catalog.VerifierStats(0.9, 0.9, 0.0, 5),
        source=str(tmp_path / "unscored.card.yaml"),
    )
    return low, unscored


_LOW_RESULT = {"kill_score": 0.2, "killed": 1, "survived": 4, "abstained": 0, "measured": True}


def _names(cards):
    return [card.name for card in cards]


def test_rank_verifiers_drops_low_score_keeps_unscored(tmp_path, monkeypatch):
    monkeypatch.setenv("MO_VERIFIER_MUTATION", "1")
    monkeypatch.delenv("MO_VERIFIER_MUTATION_MIN", raising=False)
    low, unscored = _cards(tmp_path)
    verifier_mutation.write_score(low.source, _LOW_RESULT)
    assert _names(catalog.rank_verifiers([low, unscored])) == ["unscored"]


def test_rank_verifiers_knob_off_keeps_both(tmp_path, monkeypatch):
    monkeypatch.delenv("MO_VERIFIER_MUTATION_MIN", raising=False)
    low, unscored = _cards(tmp_path)
    verifier_mutation.write_score(low.source, _LOW_RESULT)
    for off in ("0", "off"):
        monkeypatch.setenv("MO_VERIFIER_MUTATION", off)
        assert _names(catalog.rank_verifiers([low, unscored])) == ["unscored", "low"]


def test_rank_verifiers_supplied_scores_beat_sidecar(tmp_path, monkeypatch):
    monkeypatch.setenv("MO_VERIFIER_MUTATION", "1")
    monkeypatch.delenv("MO_VERIFIER_MUTATION_MIN", raising=False)
    low, unscored = _cards(tmp_path)
    verifier_mutation.write_score(low.source, _LOW_RESULT)

    assert _names(catalog.rank_verifiers([low, unscored], mutation_scores={"low": 0.9})) == [
        "unscored",
        "low",
    ]
    assert _names(catalog.rank_verifiers([low, unscored], mutation_scores={"low": 0.2})) == [
        "unscored"
    ]
    # an empty mapping was still supplied: no sidecar reads
    assert _names(catalog.rank_verifiers([low, unscored], mutation_scores={})) == [
        "unscored",
        "low",
    ]
    assert _names(catalog.rank_verifiers([low, unscored], mutation_scores={"low": None})) == [
        "unscored",
        "low",
    ]


# --------------------------------------------------------------------------- #
# 8. certificate: byte-identical without the score, sealed with it
# --------------------------------------------------------------------------- #

CERT_KWARGS = dict(
    verdict="PROVEN",
    reason="every declared check held",
    repo_path="/tmp/repo",
    remote=None,
    base_sha="0f" * 20,
    head_sha="1e" * 20,
    patch_text="--- a/foo.py\n+++ b/foo.py\n@@ -1 +1 @@\n-old\n+new\n",
    issue_text="The verifier never rejects a subtly wrong artifact.",
    oracle_method="metamorphic",
    mr_n=3,
    proven_bar=0.8,
    model="oracle-m",
    image="img:tag",
    poc_plus=None,
    mr_pass_rate=0.9,
    invariants=["inv-1"],
    cost_usd=1.5,
    llm_calls=7,
    duration_s=12.5,
)


def _digest_of(base: dict) -> str:
    canonical = json.dumps(base, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def test_certificate_without_score_is_byte_identical_to_prechange_shape():
    cert = certificate.build_certificate(**CERT_KWARGS)
    assert "mutation_kill_score" not in json.dumps(cert)
    base = {key: value for key, value in cert.items() if key != "digest"}
    assert cert["digest"] == _digest_of(base)

    cert_none = certificate.build_certificate(**CERT_KWARGS, mutation_kill_score=None)
    assert "mutation_kill_score" not in json.dumps(cert_none)


def test_certificate_with_kill_score_adds_key_and_changes_digest():
    plain = certificate.build_certificate(**CERT_KWARGS)
    scored = certificate.build_certificate(**CERT_KWARGS, mutation_kill_score=0.9)
    assert scored["method"]["mutation_kill_score"] == 0.9
    base = {key: value for key, value in scored.items() if key != "digest"}
    assert scored["digest"] == _digest_of(base)

    # same inputs apart from id/issued_at: the score is the only difference
    plain_base = {key: value for key, value in plain.items() if key != "digest"}
    same_inputs = {**plain_base, "id": scored["id"], "issued_at": scored["issued_at"]}
    with_key = copy.deepcopy(same_inputs)
    with_key["method"]["mutation_kill_score"] = 0.9
    assert scored["digest"] != _digest_of(same_inputs)
    assert scored["digest"] == _digest_of(with_key)


# --------------------------------------------------------------------------- #
# 9. sidecar round-trip and corrupt input
# --------------------------------------------------------------------------- #


def test_sidecar_round_trip(tmp_path):
    card = str(tmp_path / "verifier.card.yaml")
    result = verifier_mutation.score(RICH, runner=lambda obs: _verdict(PROVEN))
    sidecar = verifier_mutation.write_score(card, result)
    assert sidecar == card + ".mutation.json"
    data = json.loads(Path(sidecar).read_text(encoding="utf-8"))
    for key in ("kill_score", "killed", "survived", "abstained", "measured_at"):
        assert key in data, key
    assert verifier_mutation.read_score(card) == 0.0


def test_sidecar_latest_write_wins(tmp_path):
    card = str(tmp_path / "c.card.yaml")
    verifier_mutation.write_score(card, {"kill_score": 0.75, "killed": 3, "survived": 1})
    verifier_mutation.write_score(card, {"kill_score": 0.4, "killed": 2, "survived": 3})
    assert verifier_mutation.read_score(card) == 0.4


def test_sidecar_missing_or_corrupt_returns_none(tmp_path):
    card = str(tmp_path / "missing.card.yaml")
    assert verifier_mutation.read_score(card) is None
    Path(card + ".mutation.json").write_text("{not json at all", encoding="utf-8")
    assert verifier_mutation.read_score(card) is None


def test_sidecar_unmeasured_stays_none(tmp_path):
    card = str(tmp_path / "u.card.yaml")
    verifier_mutation.write_score(
        card,
        {"kill_score": None, "killed": 0, "survived": 0, "abstained": 0, "measured": False},
    )
    assert verifier_mutation.read_score(card) is None
