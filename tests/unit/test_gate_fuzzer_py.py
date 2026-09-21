"""Hermetic unit tests for the gate fuzzer.

Covers ``mini_ork.gates.gate_fuzzer``: the report's rate math, the ``None``-on-
zero-denominator rule, defer semantics, input validation, corpus well-formedness,
and — end to end against the *real* shipped ``artifact_contract`` gate — that the
blind spot is real and that the fuzzer discriminates rather than calling every
case a blind spot.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from mini_ork.gates import gate_fuzzer as gf  # noqa: E402


# ─────────────────────────────────────────────────────────────────────────────
# (1) rate math is exact
# ─────────────────────────────────────────────────────────────────────────────
def test_rate_math_is_exact():
    """A hand-built case list with a known mix yields the exact counts/rates.

    2 expected-fail (one accepted → blind spot), 3 expected-pass (one rejected
    → over block, one defer). The defer is an expected-pass case so the
    denominators stay 2 and 3 and both rates come out exact.
    """
    cases = [
        {"id": "f1", "expect": "fail"},   # got pass → blind spot
        {"id": "f2", "expect": "fail"},   # got fail → ok
        {"id": "p1", "expect": "pass"},   # got pass → ok
        {"id": "p2", "expect": "pass"},   # got fail → over block
        {"id": "d1", "expect": "pass"},   # got defer → unmeasured
    ]
    got = {"f1": "pass", "f2": "fail", "p1": "pass", "p2": "fail", "d1": "defer"}
    rep = gf.fuzz_gate(lambda c: got[c["id"]], cases)
    assert rep["blind_spots"] == 1
    assert rep["over_blocks"] == 1
    assert rep["defers"] == 1
    assert rep["blind_spot_rate"] == 0.5
    assert rep["over_block_rate"] == pytest.approx(1 / 3)
    assert [r["id"] for r in rep["results"]] == ["f1", "f2", "p1", "p2", "d1"]


# ─────────────────────────────────────────────────────────────────────────────
# (2) 0/0 is None, never 0.0
# ─────────────────────────────────────────────────────────────────────────────
def test_zero_denominator_rates_are_none():
    """No expected-fail case → blind_spot_rate is None (not 0.0)."""
    rep = gf.fuzz_gate(lambda c: "pass", [{"id": "p1", "expect": "pass"}])
    assert rep["n_expect_fail"] == 0
    assert rep["blind_spot_rate"] is None

    rep2 = gf.fuzz_gate(lambda c: "fail", [{"id": "f1", "expect": "fail"}])
    assert rep2["n_expect_pass"] == 0
    assert rep2["over_block_rate"] is None


# ─────────────────────────────────────────────────────────────────────────────
# (3) a defer is neither ok nor a blind spot
# ─────────────────────────────────────────────────────────────────────────────
def test_defer_is_neither_ok_nor_a_blind_spot():
    """expect fail + got defer → blind_spots stays 0, defers increments."""
    rep = gf.fuzz_gate(lambda c: "defer", [{"id": "d1", "expect": "fail"}])
    assert rep["blind_spots"] == 0
    assert rep["defers"] == 1
    assert rep["results"][0]["ok"] is False
    assert rep["results"][0]["got"] == "defer"


# ─────────────────────────────────────────────────────────────────────────────
# (4) bad input raises
# ─────────────────────────────────────────────────────────────────────────────
def test_unknown_expect_raises():
    with pytest.raises(ValueError):
        gf.fuzz_gate(lambda c: "pass", [{"id": "x", "expect": "maybe"}])


def test_bad_evaluator_return_raises():
    with pytest.raises(ValueError):
        gf.fuzz_gate(lambda c: "maybe", [{"id": "x", "expect": "pass"}])


def test_load_corpus_missing_file_raises(tmp_path):
    with pytest.raises(ValueError):
        gf.load_corpus(str(tmp_path / "absent.json"))


def test_load_corpus_non_array_raises(tmp_path):
    p = tmp_path / "obj.json"
    p.write_text('{"id": "x"}', encoding="utf-8")
    with pytest.raises(ValueError):
        gf.load_corpus(str(p))


def test_load_corpus_duplicate_id_raises(tmp_path):
    p = tmp_path / "dup.json"
    p.write_text(json.dumps([
        {"id": "a", "expect": "pass"},
        {"id": "a", "expect": "fail"},
    ]), encoding="utf-8")
    with pytest.raises(ValueError):
        gf.load_corpus(str(p))


@pytest.mark.parametrize("content", [
    '["not an object"]',
    '[{"expect": "pass"}]',
    '[{"id": "", "expect": "pass"}]',
    '[{"id": 7, "expect": "pass"}]',
    '[{"id": "a", "expect": "maybe"}]',
])
def test_load_corpus_other_bad_shapes_raise(tmp_path, content):
    p = tmp_path / "bad.json"
    p.write_text(content, encoding="utf-8")
    with pytest.raises(ValueError):
        gf.load_corpus(str(p))


# ─────────────────────────────────────────────────────────────────────────────
# (5) the shipped corpus is well-formed
# ─────────────────────────────────────────────────────────────────────────────
def test_shipped_corpus_is_well_formed():
    corpus = gf.load_corpus(gf.DEFAULT_CORPUS)
    assert len(corpus) >= 8
    ids = [c["id"] for c in corpus]
    assert len(ids) == len(set(ids))
    for c in corpus:
        assert {"id", "expect", "artifact", "contract"} <= set(c)
        assert isinstance(c["id"], str) and c["id"]
        assert c["expect"] in ("pass", "fail")
        assert {"name", "content"} <= set(c["artifact"])
        assert isinstance(c["artifact"]["name"], str)
        assert isinstance(c["contract"], dict)
    assert any(c["expect"] == "pass" for c in corpus)
    assert any(c["expect"] == "fail" for c in corpus)


# ─────────────────────────────────────────────────────────────────────────────
# (6) the blind spot is real, end to end
# ─────────────────────────────────────────────────────────────────────────────
def test_blind_spot_is_real_end_to_end(tmp_path):
    corpus = gf.load_corpus(gf.DEFAULT_CORPUS)
    rep = gf.fuzz_gate(gf.artifact_contract_evaluator(str(tmp_path)), corpus)
    assert rep["n"] == len(corpus)
    assert rep["blind_spots"] >= 1


# ─────────────────────────────────────────────────────────────────────────────
# (7) the fuzzer discriminates
# ─────────────────────────────────────────────────────────────────────────────
def test_fuzzer_discriminates(tmp_path):
    corpus = gf.load_corpus(gf.DEFAULT_CORPUS)
    rep = gf.fuzz_gate(gf.artifact_contract_evaluator(str(tmp_path)), corpus)
    by_id = {r["id"]: r for r in rep["results"]}
    assert by_id["strong-verifier-rejects-wrong-content"]["ok"] is True
    assert by_id["strong-verifier-rejects-wrong-content"]["got"] == "fail"
    assert by_id["strong-verifier-admits-good-content"]["ok"] is True
    assert by_id["strong-verifier-admits-good-content"]["got"] == "pass"


# ─────────────────────────────────────────────────────────────────────────────
# (8) the quoting over-block is observed
# ─────────────────────────────────────────────────────────────────────────────
def test_quoting_over_block_observed(tmp_path):
    corpus = gf.load_corpus(gf.DEFAULT_CORPUS)
    rep = gf.fuzz_gate(gf.artifact_contract_evaluator(str(tmp_path)), corpus)
    by_id = {r["id"]: r for r in rep["results"]}
    assert by_id["apostrophe-path-overblock"]["got"] == "fail"
    assert by_id["apostrophe-path-overblock"]["ok"] is False


# ─────────────────────────────────────────────────────────────────────────────
# (9) summarize renders None as `-`
# ─────────────────────────────────────────────────────────────────────────────
def test_summarize_renders_none_rate_as_dash():
    rep = gf.fuzz_gate(lambda c: "pass", [{"id": "p1", "expect": "pass"}])
    assert rep["blind_spot_rate"] is None
    assert "0/0 (-)" in gf.summarize(rep)
