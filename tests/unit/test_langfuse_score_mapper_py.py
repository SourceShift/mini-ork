"""Standalone unit tests for ``mini_ork.ported.langfuse_score_mapper``.

Replaces the bash-parity gate (which drove ``bash -c 'source
lib/langfuse_score_mapper.sh; ...'`` in a subprocess) as part of the
bash→Python migration: the Python port is now the sole implementation, so
its coverage no longer shells out to ``lib/langfuse_score_mapper.sh`` — it
asserts the port's behaviour directly. These pin the deterministic
contract the mapper must keep (the score table, the four decision/verdict
maps, the emit order for ``langfuse_score_for_verdict``, and its input
resolution rules) independent of any bash oracle.
"""

from __future__ import annotations

from typing import Dict, Tuple

import pytest

from mini_ork.ported.langfuse_score_mapper import (
    ORACLE_MAP,
    PROMOTION_MAP,
    REVIEWER_MAP,
    SCORE_TABLE,
    VERIFIER_MAP,
    langfuse_score_for_verdict,
    langfuse_score_table,
)

# Verbatim copy of the bash ``langfuse_score_table`` heredoc body (lib/
# langfuse_score_mapper.sh lines 70-88), which the port's docstring
# states it mirrors exactly. Pinned here as a static literal so the test
# suite doesn't need to invoke bash to catch drift.
_EXPECTED_TABLE_TEXT = (
    "event                          score   rationale\n"
    "─────                          ─────   ─────────\n"
    "reviewer APPROVE               +1.0    explicit approval, full credit\n"
    "reviewer REQUEST_CHANGES       -0.5    review found defects; not a pass\n"
    "reviewer ESCALATE               0.0    operator decides; do not pre-bias\n"
    "verifier pass                  +0.5    deterministic pass, half-credit\n"
    "                                       (full credit reserved for reviewer)\n"
    "verifier fail                  -0.5    deterministic fail\n"
    "verifier vacuous               -0.25   nothing checked != pass\n"
    "panel COALITION_ABORT          -0.75   structural panel failure (ρ + family)\n"
    "panel ALPHA_ESCALATE           -0.5    α<0.4, panel divergence too high\n"
    "panel CITATION_UNDERCOVERED    -0.5    citations failed to resolve\n"
    "panel REFUTE_FAILED            -0.75   validator hallucinated fabrications\n"
    "panel CI_TOO_WIDE              -0.25   per-finding CIs honest-uncertain\n"
    "rollback fired                 -1.0    publish was reverted post-hoc\n"
    "promoted                       +1.0    candidate passed promotion gate\n"
    "quarantined                    -1.0    candidate failed promotion gate\n"
    "pending_human_approval          0.0    awaiting decision\n"
)

# Verbatim copy of the bash ``_LANGFUSE_SCORES_*`` constants (lib/
# langfuse_score_mapper.sh lines 52-66) paired with the JSON-emit
# rationale strings (lines 136-150) — NOT the longer operator-facing
# table text above. This is the same shape as the port's SCORE_TABLE.
_EXPECTED_SCORE_TABLE: Dict[str, Tuple[float, str]] = {
    "reviewer_approve": (1.0, "explicit approval"),
    "reviewer_request_changes": (-0.5, "review found defects"),
    "reviewer_escalate": (0.0, "operator decides"),
    "verifier_pass": (0.5, "deterministic pass"),
    "verifier_fail": (-0.5, "deterministic fail"),
    "verifier_vacuous": (-0.25, "nothing checked"),
    "coalition_abort": (-0.75, "rho + family failure"),
    "alpha_escalate": (-0.5, "alpha below threshold"),
    "citation_undercovered": (-0.5, "citations missing"),
    "refute_failed": (-0.75, "fabrications survived"),
    "ci_too_wide": (-0.25, "per-finding CIs too wide"),
    "rollback_fired": (-1.0, "publish reverted"),
    "promoted": (1.0, "candidate promoted"),
    "quarantined": (-1.0, "candidate quarantined"),
    "pending_human_approval": (0.0, "awaiting decision"),
}


def _assert_score(
    emitted: Dict[str, object], name: str, value: float, comment: str
) -> None:
    """Field-by-field equality for one emitted score dict."""
    assert emitted["name"] == name
    assert emitted["data_type"] == "NUMERIC"
    assert emitted["comment"] == comment
    assert isinstance(emitted["value"], float)
    assert emitted["value"] == pytest.approx(value)


class TestScoreTable:
    def test_all_fifteen_events_present(self):
        assert set(SCORE_TABLE) == set(_EXPECTED_SCORE_TABLE)

    def test_values_and_comments_match_bash_source(self):
        assert SCORE_TABLE == _EXPECTED_SCORE_TABLE

    def test_langfuse_score_table_matches_bash_heredoc_verbatim(self):
        assert langfuse_score_table() == _EXPECTED_TABLE_TEXT


class TestMaps:
    def test_reviewer_map_entries(self):
        assert REVIEWER_MAP == {
            "APPROVE": "reviewer_approve",
            "REQUEST_CHANGES": "reviewer_request_changes",
            "ESCALATE": "reviewer_escalate",
        }

    def test_verifier_map_entries(self):
        assert VERIFIER_MAP == {
            "pass": "verifier_pass",
            "fail": "verifier_fail",
            "vacuous": "verifier_vacuous",
        }

    def test_oracle_map_entries(self):
        assert ORACLE_MAP == {
            "COALITION_ABORT": "coalition_abort",
            "ALPHA_ESCALATE": "alpha_escalate",
            "CITATION_UNDERCOVERED": "citation_undercovered",
            "REFUTE_FAILED": "refute_failed",
            "CI_TOO_WIDE": "ci_too_wide",
        }

    def test_promotion_map_entries(self):
        assert PROMOTION_MAP == {
            "promoted": "promoted",
            "quarantined": "quarantined",
            "pending_human_approval": "pending_human_approval",
        }

    def test_all_map_values_exist_in_score_table(self):
        for mapping in (REVIEWER_MAP, VERIFIER_MAP, ORACLE_MAP, PROMOTION_MAP):
            for event_name in mapping.values():
                assert event_name in SCORE_TABLE

    def test_reviewer_and_promotion_keys_are_disjoint(self):
        # A single "decision" value must not double-fire both maps.
        assert set(REVIEWER_MAP) & set(PROMOTION_MAP) == set()

    def test_verifier_and_oracle_keys_are_disjoint(self):
        # A single "verdict" value must not double-fire both maps.
        assert set(VERIFIER_MAP) & set(ORACLE_MAP) == set()


class TestLangfuseScoreForVerdictSingleMatch:
    """One emit per case, mirroring the six bash self-test fixtures."""

    def test_reviewer_approve_via_stdin_text(self):
        scores = langfuse_score_for_verdict(stdin_text='{"decision":"APPROVE"}')
        assert len(scores) == 1
        _assert_score(scores[0], "reviewer_approve", 1.0, "explicit approval")

    def test_verifier_fail_via_input_json_inline(self):
        scores = langfuse_score_for_verdict('{"verdict":"fail"}')
        assert len(scores) == 1
        _assert_score(scores[0], "verifier_fail", -0.5, "deterministic fail")

    def test_oracle_citation_undercovered(self):
        scores = langfuse_score_for_verdict('{"verdict":"CITATION_UNDERCOVERED"}')
        assert len(scores) == 1
        # Note: the JSON-emit rationale is 'citations missing' (not the
        # longer operator-facing table text 'citations failed to resolve').
        _assert_score(scores[0], "citation_undercovered", -0.5, "citations missing")

    def test_rollback_event(self):
        scores = langfuse_score_for_verdict('{"event":"rollback_fired"}')
        assert len(scores) == 1
        _assert_score(scores[0], "rollback_fired", -1.0, "publish reverted")

    def test_promotion_promoted(self):
        # 'promoted' is in PROMOTION_MAP but NOT in REVIEWER_MAP, so only
        # the promotion emit fires.
        scores = langfuse_score_for_verdict('{"decision":"promoted"}')
        assert len(scores) == 1
        _assert_score(scores[0], "promoted", 1.0, "candidate promoted")

    def test_quarantined(self):
        scores = langfuse_score_for_verdict('{"decision":"quarantined"}')
        assert len(scores) == 1
        _assert_score(scores[0], "quarantined", -1.0, "candidate quarantined")

    def test_pending_human_approval(self):
        scores = langfuse_score_for_verdict('{"decision":"pending_human_approval"}')
        assert len(scores) == 1
        _assert_score(
            scores[0], "pending_human_approval", 0.0, "awaiting decision"
        )


class TestLangfuseScoreForVerdictCombined:
    """Multiple rules can fire from one payload; order must match the
    bash emit order: reviewer_map(decision), promotion_map(decision),
    verifier_map(verdict), oracle_map(verdict), rollback event."""

    def test_reviewer_and_verifier_order(self):
        scores = langfuse_score_for_verdict(
            '{"decision":"APPROVE","verdict":"pass"}'
        )
        assert [s["name"] for s in scores] == ["reviewer_approve", "verifier_pass"]

    def test_oracle_and_rollback_order(self):
        scores = langfuse_score_for_verdict(
            '{"verdict":"COALITION_ABORT","event":"rollback_fired"}'
        )
        assert [s["name"] for s in scores] == ["coalition_abort", "rollback_fired"]

    def test_reviewer_verifier_rollback_full_order(self):
        scores = langfuse_score_for_verdict(
            '{"decision":"APPROVE","verdict":"fail","event":"rollback_fired"}'
        )
        assert [s["name"] for s in scores] == [
            "reviewer_approve",
            "verifier_fail",
            "rollback_fired",
        ]


class TestLangfuseScoreForVerdictNoMatch:
    def test_empty_match_dict_returns_empty_list(self):
        assert langfuse_score_for_verdict('{"foo":"bar"}') == []

    def test_unmatched_decision_and_verdict_values_ignored(self):
        assert (
            langfuse_score_for_verdict('{"decision":"UNKNOWN","verdict":"whatever"}')
            == []
        )

    def test_non_dict_list_input_returns_empty_list(self):
        assert langfuse_score_for_verdict("[1, 2, 3]") == []

    def test_non_dict_scalar_string_input_returns_empty_list(self):
        assert langfuse_score_for_verdict('"just a string"') == []

    def test_non_dict_scalar_number_input_returns_empty_list(self):
        assert langfuse_score_for_verdict("42") == []


class TestInputResolution:
    def test_usage_error_raises_value_error_when_no_input(self):
        with pytest.raises(ValueError) as exc:
            langfuse_score_for_verdict()
        msg = str(exc.value)
        assert "usage" in msg.lower()
        assert "langfuse_score_for_verdict" in msg

    def test_parse_error_raises_value_error_for_input_json(self):
        with pytest.raises(ValueError) as exc:
            langfuse_score_for_verdict("{not valid json")
        assert "parse error" in str(exc.value).lower()

    def test_parse_error_raises_value_error_for_stdin_text(self):
        with pytest.raises(ValueError) as exc:
            langfuse_score_for_verdict(stdin_text="{not valid json")
        assert "parse error" in str(exc.value).lower()

    def test_parse_error_raises_value_error_for_file_path_mode(self, tmp_path):
        bad_file = tmp_path / "bad.json"
        bad_file.write_text("{not valid json")
        with pytest.raises(ValueError) as exc:
            langfuse_score_for_verdict(str(bad_file))
        assert "parse error" in str(exc.value).lower()

    def test_input_json_file_path_is_read(self, tmp_path):
        payload_file = tmp_path / "verdict.json"
        payload_file.write_text('{"verdict":"pass"}')
        scores = langfuse_score_for_verdict(str(payload_file))
        assert len(scores) == 1
        _assert_score(scores[0], "verifier_pass", 0.5, "deterministic pass")

    def test_input_json_takes_precedence_over_stdin_text(self):
        # Mirrors bash lines 109-112 / port's _load_input: when both are
        # given, input_json wins.
        scores = langfuse_score_for_verdict(
            '{"decision":"APPROVE"}', stdin_text='{"verdict":"pass"}'
        )
        assert len(scores) == 1
        assert scores[0]["name"] == "reviewer_approve"

    def test_nonexistent_path_like_string_falls_back_to_inline_and_fails_parse(self):
        # Looks like a path but doesn't exist on disk -> treated as
        # literal JSON text -> not valid JSON -> parse error.
        with pytest.raises(ValueError) as exc:
            langfuse_score_for_verdict("/nonexistent/path/to/verdict.json")
        assert "parse error" in str(exc.value).lower()

    def test_directory_path_falls_back_to_literal_string_and_fails_parse(
        self, tmp_path
    ):
        # os.path.exists() is True for a directory but os.path.isfile()
        # is False, so _load_input returns the path string itself
        # (not its contents) -> json.loads on the path text fails.
        with pytest.raises(ValueError) as exc:
            langfuse_score_for_verdict(str(tmp_path))
        assert "parse error" in str(exc.value).lower()
