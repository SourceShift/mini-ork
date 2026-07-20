"""Standalone unit tests for the canonical ``mini_ork.similarity`` ranker.

Replaces the bash-parity gate (``test_similarity_parity.py``) as part of the
bash→Python migration: the Python port is now the sole implementation, so its
coverage no longer runs ``lib/similarity.sh`` in a subprocess — it asserts the
port's behaviour directly. These pin the deterministic contract the ranker
must keep (tokenisation rules, TF, cosine, the table/column whitelist, and the
top-k ranking pipeline) independent of any bash oracle.
"""

from __future__ import annotations

import math

import pytest

from mini_ork.similarity import (
    ALLOWED,
    allowed_table_col,
    cos,
    rank,
    rank_raw,
    tf,
    tok,
)


class TestTok:
    def test_lowercases_and_keeps_word_class_chars(self):
        # hyphen, dot, slash, underscore are in the kept class → stay in-token.
        assert tok("Auth-Bug in the MIDDLEWARE.py") == ["auth-bug", "the", "middleware.py"]

    def test_drops_tokens_shorter_than_three(self):
        assert tok("a bb ccc dddd") == ["ccc", "dddd"]

    def test_splits_on_punctuation_runs(self):
        assert tok("foo, bar;;;baz") == ["foo", "bar", "baz"]

    def test_empty_and_none_safe(self):
        assert tok("") == []
        assert tok(None) == []  # type: ignore[arg-type]


class TestTf:
    def test_term_frequency_normalises_by_total(self):
        assert tf(["aaa", "aaa", "bbb"]) == {"aaa": 2 / 3, "bbb": 1 / 3}

    def test_empty_input_is_empty_dict(self):
        assert tf([]) == {}


class TestCos:
    def test_identical_vectors_is_one(self):
        v = {"aaa": 1.0, "bbb": 2.0}
        assert cos(v, v) == pytest.approx(1.0)

    def test_disjoint_vectors_is_zero(self):
        assert cos({"aaa": 1.0}, {"bbb": 1.0}) == 0.0

    def test_empty_side_is_zero(self):
        assert cos({}, {"aaa": 1.0}) == 0.0

    def test_known_value(self):
        # a=(1,0), b=(1,1) → 1/√2
        assert cos({"x": 1.0}, {"x": 1.0, "y": 1.0}) == pytest.approx(1 / math.sqrt(2))


class TestAllowedTableCol:
    def test_whitelisted_pair_true(self):
        assert allowed_table_col("bug_reports", "title") is True

    def test_column_not_in_table_false(self):
        assert allowed_table_col("bug_reports", "not_a_col") is False

    def test_unknown_table_false(self):
        assert allowed_table_col("nope", "title") is False

    def test_allowed_map_shape_is_stable(self):
        # Guards the whitelist against silent drift (mirrors lib/similarity.sh::ALLOWED).
        assert ALLOWED["gradient_records"] == {"signal", "suggested_change", "target"}


class TestRank:
    DOCS = ["auth bug in middleware", "totally unrelated content", "auth auth bug fix"]

    def test_excludes_zero_score_docs_and_ranks_relevant(self):
        out = rank("auth bug", self.DOCS, limit=5)
        idx = [i for _, i in out]
        assert set(idx) == {0, 2}          # doc 1 shares no terms → score 0 → dropped
        assert 1 not in idx

    def test_sorted_descending_by_score(self):
        out = rank("auth bug", self.DOCS, limit=5)
        scores = [s for s, _ in out]
        assert scores == sorted(scores, reverse=True)

    def test_limit_truncates(self):
        assert len(rank("auth bug", self.DOCS, limit=1)) == 1

    def test_no_match_returns_empty(self):
        assert rank("zzz", ["nothing here", "still nothing"]) == []

    def test_scores_are_rounded_to_ndigits(self):
        out = rank("auth bug", self.DOCS, limit=5, round_ndigits=4)
        for s, _ in out:
            assert round(s, 4) == s

    def test_raw_scores_preserve_precision_and_define_reported_order(self):
        raw = rank_raw("auth bug", self.DOCS)
        rounded = rank("auth bug", self.DOCS, limit=5)
        assert any(score != round(score, 4) for score, _ in raw)
        assert [index for _, index in rounded] == [index for _, index in raw]

    def test_raw_limit_is_optional(self):
        assert len(rank_raw("auth bug", self.DOCS, limit=1)) == 1
        assert len(rank_raw("auth bug", self.DOCS)) == 2
