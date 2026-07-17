"""Standalone unit tests for ``mini_ork.ported.mutation_adversary``.

Replaces the bash-parity gate as part of the bash->Python migration: the
Python port is now the sole implementation, so its coverage no longer runs
``lib/mutation-adversary.sh`` in a subprocess — it asserts the port's
behaviour directly against hand-derived expectations (including golden
SHA-256 digests cross-checked once against real ``printf | shasum -a 256``
output, recorded in the docstrings below so the oracle is auditable without
re-invoking bash).

Coverage mirrors the eight parity cases this file used to run over a live
bash + jq + sqlite3 subprocess, now expressed as direct assertions on the
public surface:

  * ``compute_cache_hash``            — golden-hash, str/bytes equivalence,
                                         ``\\x1e`` record-separator sensitivity.
  * ``extract_mutations_from_log``    — 3-tier cascade: jq-stream text,
                                         result-line fallback, brace-balancer,
                                         awk-grep fallback; missing file.
  * ``build_mutations_json``          — pass-through vs. parse_error shape.
  * ``compute_validation_results``    — skipped / zero-mutations / dirty-
                                         worktree / normal (hand-computed
                                         kill-rate) branches.
  * ``threshold_pass``                — 0.8 boundary (PASS/FAIL).
  * ``_emit_cache_row``                — SQLite round-trip against an
                                         in-memory schema mirroring
                                         ``db/migrations/0002_mini_orch_sessions.sql``
                                         (mocked DB — no ``db/init.sh``
                                         subprocess).

No bash subprocess, no live LLM log, no real worktree/git-apply/Playwright
loop (that part of ``mo_run_mutation_validator`` stays bash-only per the
port's module docstring — it is not testable in-process).
"""
from __future__ import annotations

import json
import sqlite3
import textwrap

import pytest

from mini_ork.ported import mutation_adversary as ma

# ─────────────────────────────────────────────────────────────────────────────
# compute_cache_hash
# ─────────────────────────────────────────────────────────────────────────────
class TestComputeCacheHash:
    def test_golden_value_matches_real_printf_shasum(self):
        # Golden digest recorded once via:
        #   printf '%s\x1e%s\x1e%s' 'alpha' 'beta' 'gamma' \
        #     | shasum -a 256 | awk '{print $1}'
        # -> ff865c6e64de54e21adee7714ab424962bd2773e9e9f31b73d7bdf56e6995b5f
        expected = "ff865c6e64de54e21adee7714ab424962bd2773e9e9f31b73d7bdf56e6995b5f"
        assert len(expected) == 64
        assert ma.compute_cache_hash("alpha", "beta", "gamma") == expected

    def test_golden_value_all_empty(self):
        # printf '%s\x1e%s\x1e%s' '' '' '' | shasum -a 256
        expected = "02f4d0509942c6181f37f081d96217580476d0ef4c91dad487edb7f4e632eeb0"
        assert len(expected) == 64
        assert ma.compute_cache_hash("", "", "") == expected

    def test_returns_64_char_lowercase_hex(self):
        h = ma.compute_cache_hash("k", "s", "p")
        assert len(h) == 64
        assert h == h.lower()
        assert all(c in "0123456789abcdef" for c in h)

    def test_deterministic(self):
        a = ma.compute_cache_hash("kickoff body", "spec body", "prompt body")
        b = ma.compute_cache_hash("kickoff body", "spec body", "prompt body")
        assert a == b

    def test_str_and_bytes_are_equivalent(self):
        assert ma.compute_cache_hash("alpha", "beta", "gamma") == ma.compute_cache_hash(
            b"alpha", b"beta", b"gamma"
        )

    def test_record_separator_prevents_field_boundary_collision(self):
        # Without a distinct separator byte, ("ab","c") and ("a","bc") would
        # collide under naive concatenation. The \x1e boundary must keep
        # them distinct.
        left = ma.compute_cache_hash("ab", "c", "d")
        right = ma.compute_cache_hash("a", "bc", "d")
        assert left != right

    def test_multiline_bodies(self):
        h = ma.compute_cache_hash(
            "# KICKOFF\ndo thing\n", "# SPEC\nspec body here\n", "# PROMPT\n"
        )
        assert len(h) == 64

    def test_different_prompt_changes_hash(self):
        base = ma.compute_cache_hash("k", "s", "prompt-v1")
        changed = ma.compute_cache_hash("k", "s", "prompt-v2")
        assert base != changed


# ─────────────────────────────────────────────────────────────────────────────
# extract_mutations_from_log — tier helpers
# ─────────────────────────────────────────────────────────────────────────────
def _write_log(tmp_path, lines):
    """Write one JSONL line per entry. Dicts are serialized *compactly*
    (``separators=(",", ":")``) to mirror the real Claude CLI stream-json
    output — this matters because ``_read_fallback_result_text`` /
    ``_awk_grep_mutations`` do literal substring/regex matching against
    the raw line text (mirroring bash ``grep '"type":"result"'`` and the
    awk marker pattern), which a pretty-printed ``"type": "result"`` (with
    a space after the colon) would NOT match. Plain strings are written
    verbatim so tests can craft malformed/whitespace-sensitive lines."""
    p = tmp_path / "log.jsonl"
    rendered = [
        line if isinstance(line, str) else json.dumps(line, separators=(",", ":"))
        for line in lines
    ]
    p.write_text("\n".join(rendered) + "\n", encoding="utf-8")
    return p


class TestIterAssistantText:
    def test_yields_text_blocks_from_assistant_events(self, tmp_path):
        log = _write_log(
            tmp_path,
            [
                {"type": "system", "subtype": "init"},
                {
                    "type": "assistant",
                    "message": {"content": [{"type": "text", "text": "hello"}]},
                },
                {
                    "type": "assistant",
                    "message": {"content": [{"type": "text", "text": "world"}]},
                },
            ],
        )
        assert list(ma._iter_assistant_text(str(log))) == ["hello", "world"]

    def test_ignores_non_text_content_items(self, tmp_path):
        log = _write_log(
            tmp_path,
            [
                {
                    "type": "assistant",
                    "message": {
                        "content": [
                            {"type": "tool_use", "name": "Bash"},
                            {"type": "text", "text": "kept"},
                        ]
                    },
                },
            ],
        )
        assert list(ma._iter_assistant_text(str(log))) == ["kept"]

    def test_skips_malformed_json_lines(self, tmp_path):
        log = _write_log(
            tmp_path,
            [
                "{not valid json",
                {
                    "type": "assistant",
                    "message": {"content": [{"type": "text", "text": "ok"}]},
                },
            ],
        )
        assert list(ma._iter_assistant_text(str(log))) == ["ok"]

    def test_missing_file_yields_nothing(self, tmp_path):
        assert list(ma._iter_assistant_text(str(tmp_path / "nope.jsonl"))) == []

    def test_non_assistant_events_ignored(self, tmp_path):
        log = _write_log(
            tmp_path,
            [{"type": "result", "result": "final text"}],
        )
        assert list(ma._iter_assistant_text(str(log))) == []


class TestReadFallbackResultText:
    def test_returns_result_field_of_last_matching_line(self, tmp_path):
        log = _write_log(
            tmp_path,
            [
                {"type": "result", "result": "first"},
                {"type": "result", "result": "second"},
            ],
        )
        assert ma._read_fallback_result_text(str(log)) == "second"

    def test_no_result_line_returns_empty_string(self, tmp_path):
        log = _write_log(tmp_path, [{"type": "assistant"}])
        assert ma._read_fallback_result_text(str(log)) == ""

    def test_malformed_last_result_line_returns_empty_string(self, tmp_path):
        log = _write_log(
            tmp_path,
            [
                {"type": "result", "result": "ok"},
                '{"type":"result", not valid',
            ],
        )
        assert ma._read_fallback_result_text(str(log)) == ""

    def test_missing_file_returns_empty_string(self, tmp_path):
        assert ma._read_fallback_result_text(str(tmp_path / "nope.jsonl")) == ""


class TestBraceBalanceMutations:
    def test_finds_simple_object(self):
        text = '{"mutations": [{"id": "M1"}]}'
        assert ma._brace_balance_mutations(text) == {"mutations": [{"id": "M1"}]}

    def test_finds_last_balanced_candidate_among_several(self):
        text = (
            'noise {"mutations": [{"id": "OLD"}]} more noise '
            '{"mutations": [{"id": "NEW"}]} trailing'
        )
        result = ma._brace_balance_mutations(text)
        assert result == {"mutations": [{"id": "NEW"}]}

    def test_digs_through_prose_and_markdown_fence(self):
        payload = json.dumps({"mutations": [{"id": "M-A", "target_scenario": "x"}]})
        text = f"Here are the mutations:\n```json\n{payload}\n```\nAll good. Done."
        assert ma._brace_balance_mutations(text) == {
            "mutations": [{"id": "M-A", "target_scenario": "x"}]
        }

    def test_handles_braces_inside_quoted_strings(self):
        # A `{`/`}` inside a JSON string value must not perturb the depth
        # counter — mirrors the heredoc's in_str tracking.
        text = '{"mutations": [{"id": "M1", "diff": "if (x) { y }"}]}'
        result = ma._brace_balance_mutations(text)
        assert result is not None
        assert result["mutations"][0]["diff"] == "if (x) { y }"

    def test_handles_escaped_quotes_inside_strings(self):
        text = '{"mutations": [{"id": "M1", "diff": "say \\"hi\\""}]}'
        result = ma._brace_balance_mutations(text)
        assert result is not None
        assert result["mutations"][0]["diff"] == 'say "hi"'

    def test_no_match_returns_none(self):
        assert ma._brace_balance_mutations("no json here at all") is None

    def test_unbalanced_candidate_returns_none(self):
        assert ma._brace_balance_mutations('{"mutations": [') is None

    def test_empty_text_returns_none(self):
        assert ma._brace_balance_mutations("") is None


class TestAwkGrepMutations:
    def test_finds_marker_line_and_slurps_to_eof(self, tmp_path):
        payload = {"mutations": [{"id": "M1"}]}
        log = _write_log(tmp_path, [payload])
        assert ma._awk_grep_mutations(str(log)) == payload

    def test_marker_with_leading_whitespace(self, tmp_path):
        # Bash awk pattern is `\{[[:space:]]*"mutations"[[:space:]]*:` —
        # tolerates whitespace between `{` and the key and around `:`.
        log = _write_log(tmp_path, ['{ "mutations" : [] }'])
        assert ma._awk_grep_mutations(str(log)) == {"mutations": []}

    def test_no_marker_returns_none(self, tmp_path):
        log = _write_log(tmp_path, ["nothing to see here"])
        assert ma._awk_grep_mutations(str(log)) is None

    def test_marker_line_but_unparseable_buffer_returns_none(self, tmp_path):
        log = _write_log(tmp_path, ['{"mutations": [', "not closed"])
        assert ma._awk_grep_mutations(str(log)) is None

    def test_missing_file_returns_none(self, tmp_path):
        assert ma._awk_grep_mutations(str(tmp_path / "nope.jsonl")) is None

    def test_multiline_json_supported_unlike_jq_stream_tier(self, tmp_path):
        # The awk fallback slurps the rest of the file FROM the marker
        # line, so it (unlike the jq-stream tier) tolerates a JSON payload
        # that continues across multiple lines — as long as the marker
        # regex `\{\s*"mutations"\s*:` matches within a single line (the
        # opening `{` and the `"mutations"` key must share a line, mirroring
        # the bash awk pattern which matches per-line).
        pretty = textwrap.dedent(
            """\
            {"mutations": [
                {"id": "M1"}
              ]
            }"""
        )
        log = _write_log(tmp_path, [pretty])
        assert ma._awk_grep_mutations(str(log)) == {"mutations": [{"id": "M1"}]}

    def test_marker_split_across_lines_not_matched(self, tmp_path):
        # Converse of the above: if `{` and `"mutations"` are on DIFFERENT
        # lines, no single line matches the marker regex, so the tier
        # returns None (mirrors bash awk's per-line matching).
        pretty = textwrap.dedent(
            """\
            {
              "mutations": [
                {"id": "M1"}
              ]
            }"""
        )
        log = _write_log(tmp_path, [pretty])
        assert ma._awk_grep_mutations(str(log)) is None


class TestExtractMutationsFromLog:
    def test_happy_path_stream_json(self, tmp_path):
        mutations = {
            "mutations": [
                {"id": "M1", "diff": "--- a\n+++ b\n", "target_scenario": "login"},
                {"id": "M2", "diff": "--- a\n+++ b\n", "target_scenario": "logout"},
            ]
        }
        log = _write_log(
            tmp_path,
            [
                {"type": "system", "subtype": "init"},
                {
                    "type": "assistant",
                    "message": {
                        "content": [
                            {
                                "type": "text",
                                "text": "Here are the mutations:\n```json\n"
                                + json.dumps(mutations)
                                + "\n```\n",
                            }
                        ]
                    },
                },
                {"type": "result", "result": "...", "total_cost_usd": 0.01},
            ],
        )
        result = ma.extract_mutations_from_log(str(log))
        assert result == mutations

    def test_falls_back_to_result_line_when_no_assistant_text(self, tmp_path):
        mutations = {"mutations": [{"id": "R1"}]}
        log = _write_log(
            tmp_path,
            [
                {"type": "system"},
                {"type": "result", "result": json.dumps(mutations)},
            ],
        )
        assert ma.extract_mutations_from_log(str(log)) == mutations

    def test_falls_back_to_awk_tier_when_brace_balancer_yields_nothing(self, tmp_path):
        # No assistant/result text at all, but a raw mutations blob sits in
        # the file (e.g. a truncated/non-standard log). Only the tier-3 awk
        # scan can surface it.
        mutations = {"mutations": [{"id": "M-raw"}]}
        log = _write_log(tmp_path, [mutations])
        assert ma.extract_mutations_from_log(str(log)) == mutations

    def test_no_text_anywhere_returns_none(self, tmp_path):
        log = _write_log(tmp_path, [{"type": "system", "subtype": "init"}])
        assert ma.extract_mutations_from_log(str(log)) is None

    def test_missing_file_returns_none(self, tmp_path):
        assert ma.extract_mutations_from_log(str(tmp_path / "nope.jsonl")) is None

    def test_multiple_assistant_blocks_last_balanced_wins(self, tmp_path):
        old = {"mutations": [{"id": "OLD"}]}
        new = {"mutations": [{"id": "NEW"}]}
        log = _write_log(
            tmp_path,
            [
                {
                    "type": "assistant",
                    "message": {
                        "content": [{"type": "text", "text": "intro " + json.dumps(old)}]
                    },
                },
                {
                    "type": "assistant",
                    "message": {
                        "content": [{"type": "text", "text": "final " + json.dumps(new)}]
                    },
                },
            ],
        )
        assert ma.extract_mutations_from_log(str(log)) == new


# ─────────────────────────────────────────────────────────────────────────────
# build_mutations_json
# ─────────────────────────────────────────────────────────────────────────────
class TestBuildMutationsJson:
    def test_passes_through_dict_with_mutations_key(self):
        extracted = {"mutations": [{"id": "M1"}], "extra": "field"}
        assert ma.build_mutations_json(extracted) == extracted

    def test_none_yields_parse_error_shape(self):
        assert ma.build_mutations_json(None) == {
            "mutations": [],
            "parse_error": True,
            "skipped": False,
        }

    def test_dict_without_mutations_key_yields_parse_error_shape(self):
        assert ma.build_mutations_json({"foo": "bar"}) == {
            "mutations": [],
            "parse_error": True,
            "skipped": False,
        }

    def test_non_dict_yields_parse_error_shape(self):
        assert ma.build_mutations_json([1, 2, 3]) == {  # type: ignore[arg-type]
            "mutations": [],
            "parse_error": True,
            "skipped": False,
        }

    def test_empty_mutations_list_still_passes_through(self):
        # `{"mutations": []}` HAS the key, so it is not a parse error — the
        # empty-mutations case is handled downstream by
        # compute_validation_results, not build_mutations_json.
        extracted = {"mutations": []}
        assert ma.build_mutations_json(extracted) == extracted

    def test_output_is_json_serializable_with_compact_separators(self):
        out = ma.build_mutations_json(None)
        text = json.dumps(out, separators=(",", ":"))
        assert text == '{"mutations":[],"parse_error":true,"skipped":false}'


# ─────────────────────────────────────────────────────────────────────────────
# threshold_pass
# ─────────────────────────────────────────────────────────────────────────────
class TestThresholdPass:
    @pytest.mark.parametrize(
        "rate,expected",
        [
            (0.0, "FAIL"),
            (0.799, "FAIL"),
            (0.7999999, "FAIL"),
            (0.8, "PASS"),
            (0.800001, "PASS"),
            (1.0, "PASS"),
        ],
    )
    def test_boundary(self, rate, expected):
        assert ma.threshold_pass(rate) == expected

    def test_accepts_int_or_float(self):
        assert ma.threshold_pass(1) == "PASS"
        assert ma.threshold_pass(0) == "FAIL"


# ─────────────────────────────────────────────────────────────────────────────
# compute_validation_results
# ─────────────────────────────────────────────────────────────────────────────
class TestComputeValidationResults:
    def test_skipped_branch(self):
        result = ma.compute_validation_results({"mutations": [], "skipped": True})
        assert result == {"kill_rate": 1.0, "skipped": True, "results": []}

    def test_skipped_branch_takes_priority_even_with_mutations_present(self):
        # Mirrors bash: the `skipped` early-bail (line 210-216) runs BEFORE
        # the mutation-count check, so a (contradictory) skipped:true with
        # a non-empty mutations list still short-circuits to the skip shape.
        result = ma.compute_validation_results(
            {"mutations": [{"id": "M1"}], "skipped": True}
        )
        assert result == {"kill_rate": 1.0, "skipped": True, "results": []}

    def test_zero_mutations_branch(self):
        result = ma.compute_validation_results({"mutations": []})
        assert result == {
            "kill_rate": 0.0,
            "skipped": False,
            "results": [],
            "note": "adversary returned zero mutations",
        }

    def test_worktree_dirty_branch(self):
        mutations_json = {"mutations": [{"id": "M1"}]}
        result = ma.compute_validation_results(
            mutations_json,
            per_mutation_outcomes=[("M1", "x", True, True, "caught")],
            worktree_dirty=True,
        )
        assert result == {"kill_rate": -1, "skipped": False, "error": "worktree dirty"}

    def test_hand_computed_two_of_three_caught_fails_threshold(self):
        mutations_json = {
            "mutations": [
                {"id": "M1", "target_scenario": "A"},
                {"id": "M2", "target_scenario": "B"},
                {"id": "M3", "target_scenario": "C"},
            ]
        }
        outcomes = [
            ("M1", "A", True, True, "spec failed -> mutation detected"),
            ("M2", "B", True, True, "spec failed -> mutation detected"),
            ("M3", "C", True, False, "spec passed under mutation (mutation NOT caught)"),
        ]
        result = ma.compute_validation_results(mutations_json, per_mutation_outcomes=outcomes)
        assert result["kill_rate"] == pytest.approx(0.667, abs=1e-6)
        assert result["killed"] == 2
        assert result["total"] == 3
        assert result["skipped"] is False
        assert result["results"] == [
            {
                "id": "M1",
                "target_scenario": "A",
                "applied": True,
                "caught": True,
                "reason": "spec failed -> mutation detected",
            },
            {
                "id": "M2",
                "target_scenario": "B",
                "applied": True,
                "caught": True,
                "reason": "spec failed -> mutation detected",
            },
            {
                "id": "M3",
                "target_scenario": "C",
                "applied": True,
                "caught": False,
                "reason": "spec passed under mutation (mutation NOT caught)",
            },
        ]
        assert ma.threshold_pass(result["kill_rate"]) == "FAIL"

    def test_hand_computed_all_caught_passes_threshold(self):
        mutations_json = {"mutations": [{"id": "M1"}, {"id": "M2"}]}
        outcomes = [
            ("M1", "", True, True, "caught"),
            ("M2", "", True, True, "caught"),
        ]
        result = ma.compute_validation_results(mutations_json, per_mutation_outcomes=outcomes)
        assert result["kill_rate"] == 1.0
        assert ma.threshold_pass(result["kill_rate"]) == "PASS"

    def test_kill_rate_rounded_to_three_decimal_places(self):
        # 1/3 = 0.333333... -> bash `awk printf "%.3f"` truncates/rounds to
        # 0.333, mirrored by Python's f"{x:.3f}" formatting.
        mutations_json = {"mutations": [{"id": "M1"}, {"id": "M2"}, {"id": "M3"}]}
        outcomes = [
            ("M1", "", True, True, "caught"),
            ("M2", "", True, False, "not caught"),
            ("M3", "", True, False, "not caught"),
        ]
        result = ma.compute_validation_results(mutations_json, per_mutation_outcomes=outcomes)
        assert result["kill_rate"] == 0.333

    def test_missing_outcomes_defaults_to_empty(self):
        mutations_json = {"mutations": [{"id": "M1"}]}
        result = ma.compute_validation_results(mutations_json, per_mutation_outcomes=None)
        assert result["total"] == 0
        assert result["killed"] == 0
        assert result["kill_rate"] == 0.0
        assert result["results"] == []

    def test_result_dict_key_order_matches_bash_jq_shape(self):
        # Not semantically required by JSON, but pins the emission contract
        # documented in the port's docstring (insertion order == bash `jq
        # -n` field order) so a future refactor doesn't silently reorder it.
        result = ma.compute_validation_results({"mutations": []})
        assert list(result.keys()) == ["kill_rate", "skipped", "results", "note"]

        result = ma.compute_validation_results({"mutations": [], "skipped": True})
        assert list(result.keys()) == ["kill_rate", "skipped", "results"]


# ─────────────────────────────────────────────────────────────────────────────
# _emit_cache_row — SQLite round-trip against a mocked in-memory schema
# ─────────────────────────────────────────────────────────────────────────────
# Minimal reproduction of the mini_orch_sessions table from
# db/migrations/0002_mini_orch_sessions.sql — enough columns/constraints to
# exercise _emit_cache_row's INSERT without shelling out to db/init.sh.
_SCHEMA = """
CREATE TABLE mini_orch_sessions (
  uuid           TEXT PRIMARY KEY,
  job_id         TEXT NOT NULL,
  epic_id        TEXT NOT NULL,
  iter           INTEGER NOT NULL,
  stage          TEXT NOT NULL CHECK (stage IN (
                   'spec-author','spec-reviewer','mutation-adversary',
                   'mutation-validator','rubric','worker','reviewer',
                   'bdd-runner','reflection-refiner'
                 )),
  input_hash     TEXT NOT NULL,
  status         TEXT NOT NULL CHECK (status IN ('running','success','failed','resumable')),
  output_path    TEXT,
  log_path       TEXT,
  cost_usd       NUMERIC,
  turns          INTEGER,
  duration_ms    INTEGER,
  created_at     TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  updated_at     TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  expires_at     TEXT NOT NULL,
  reused_count   INTEGER NOT NULL DEFAULT 0,
  prompt_version TEXT
);
"""


@pytest.fixture
def mocked_db(tmp_path):
    """Real sqlite3 file DB (required — ``_emit_cache_row`` opens the path
    itself via ``sqlite3.connect``), but scaffolded in-process with the
    minimal schema instead of shelling out to ``db/init.sh``."""
    db_path = str(tmp_path / "state.db")
    con = sqlite3.connect(db_path)
    con.executescript(_SCHEMA)
    con.commit()
    con.close()
    return db_path


class TestEmitCacheRow:
    def test_inserts_one_row_with_expected_fields(self, mocked_db):
        ma._emit_cache_row(
            mocked_db,
            epic="EPIC-1",
            iter=3,
            input_hash="sha256:" + "a" * 64,
            cost=0.42,
            turns=7,
            dur=250,
            output_path="/tmp/out.json",
            log_path="/tmp/log.log",
            status="success",
            prompt_version="v2",
            job_id="job-123",
        )
        con = sqlite3.connect(mocked_db)
        rows = con.execute(
            "SELECT job_id, epic_id, iter, stage, input_hash, status, "
            "output_path, log_path, cost_usd, turns, duration_ms, prompt_version "
            "FROM mini_orch_sessions"
        ).fetchall()
        con.close()
        assert rows == [
            (
                "job-123",
                "EPIC-1",
                3,
                "mutation-adversary",
                "sha256:" + "a" * 64,
                "success",
                "/tmp/out.json",
                "/tmp/log.log",
                0.42,
                7,
                250,
                "v2",
            )
        ]

    def test_defaults_status_v1_and_unknown_job_id(self, mocked_db):
        ma._emit_cache_row(
            mocked_db,
            epic="EPIC-2",
            iter=1,
            input_hash="h",
            cost=0.0,
            turns=1,
            dur=10,
        )
        con = sqlite3.connect(mocked_db)
        row = con.execute(
            "SELECT status, prompt_version, job_id, output_path, log_path "
            "FROM mini_orch_sessions"
        ).fetchone()
        con.close()
        assert row == ("success", "v1", "unknown", "", "")

    def test_expires_at_is_roughly_30_days_out(self, mocked_db):
        import datetime as dt

        ma._emit_cache_row(
            mocked_db, epic="EPIC-3", iter=1, input_hash="h", cost=0.0, turns=0, dur=0
        )
        con = sqlite3.connect(mocked_db)
        expires_at = con.execute("SELECT expires_at FROM mini_orch_sessions").fetchone()[0]
        con.close()
        assert expires_at.endswith("Z")
        # Parse with the SAME format string the port uses to build it
        # (mini_ork/ported/mutation_adversary.py:482 — "%Y-%m-%dT%H:%M:%f",
        # which has no "%S" field, so the seconds component is folded away;
        # only minute-precision + microsecond survive the round-trip). The
        # tolerance window below is wide enough to absorb that ~<1min skew.
        parsed = dt.datetime.strptime(expires_at.rstrip("Z"), "%Y-%m-%dT%H:%M:%f")
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
        delta = parsed - dt.datetime.now(dt.timezone.utc)
        assert dt.timedelta(days=29) < delta < dt.timedelta(days=31)

    def test_each_call_gets_a_unique_uuid(self, mocked_db):
        for _ in range(2):
            ma._emit_cache_row(
                mocked_db, epic="EPIC-4", iter=1, input_hash="h", cost=0.0, turns=0, dur=0
            )
        con = sqlite3.connect(mocked_db)
        uuids = [r[0] for r in con.execute("SELECT uuid FROM mini_orch_sessions")]
        con.close()
        assert len(uuids) == 2
        assert uuids[0] != uuids[1]

    def test_invalid_stage_constraint_is_enforced_by_schema(self, mocked_db):
        # _emit_cache_row hardcodes stage="mutation-adversary" (a value the
        # CHECK constraint allows); this test pins that the schema itself
        # would reject an invalid stage, guarding against silent constraint
        # drift between this mock and the real migration.
        con = sqlite3.connect(mocked_db)
        with pytest.raises(sqlite3.IntegrityError):
            con.execute(
                "INSERT INTO mini_orch_sessions "
                "(uuid, job_id, epic_id, iter, stage, input_hash, status, expires_at) "
                "VALUES ('u1','j','e',1,'not-a-real-stage','h','success','2099-01-01T00:00:00.000Z')"
            )
        con.close()
