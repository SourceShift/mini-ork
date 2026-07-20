"""Standalone unit tests for ``mini_ork.learning.reflection_refiner``.

Replaces the bash-parity gate as part of the bash->Python migration: the
Python port is now the sole implementation, so its coverage no longer shells
out to `jq`/`awk`/`sed`/`sqlite3`/bash to diff against a live oracle — it
asserts the port's behaviour directly. These pin the deterministic contract
of each sub-pipeline (failure-summary projection, the awk-split/sed prompt
assembly, the awk range extraction, the fallback heredoc, the sqlite kickoff
lookup, the early-bail gate, and the feedback-append) independent of any bash
oracle.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from mini_ork.learning.reflection_refiner import (
    append_to_feedback,
    build_prompt,
    extract_reflection,
    format_failure_summary,
    read_kickoff_path,
    render_fallback,
    should_run,
)


class TestFormatFailureSummary:
    def test_populated_failures_are_rendered_and_joined(self):
        verdict = {
            "verdict": "FAIL",
            "scenarios_run": 3,
            "scenarios_failed": 2,
            "failures": [
                {"title": "Auth rejects valid token", "spec": "spec/auth.md",
                 "error": "expected 200\ngot 401"},
                {"title": "Rate limit fires too early", "spec": "spec/rate.md",
                 "error": "burst at 100 rps"},
            ],
        }
        assert format_failure_summary(verdict) == (
            "Total: 3 scenarios, 2 failed.\n\n"
            "- **Auth rejects valid token** (spec/auth.md): expected 200 // got 401\n"
            "- **Rate limit fires too early** (spec/rate.md): burst at 100 rps\n"
        )

    def test_empty_failures_list_leaves_trailing_blank_line(self):
        verdict = {"scenarios_run": 0, "scenarios_failed": 0, "failures": []}
        # header already ends "\n\n"; the empty join contributes '' then a
        # final "\n" is appended → three newlines total after "failed.".
        assert format_failure_summary(verdict) == "Total: 0 scenarios, 0 failed.\n\n\n"

    def test_missing_failures_key_defaults_to_empty(self):
        verdict = {"scenarios_run": 1, "scenarios_failed": 0}
        assert format_failure_summary(verdict) == "Total: 1 scenarios, 0 failed.\n\n\n"

    def test_none_failures_value_treated_as_empty(self):
        verdict = {"scenarios_run": 1, "scenarios_failed": 0, "failures": None}
        assert format_failure_summary(verdict) == "Total: 1 scenarios, 0 failed.\n\n\n"

    def test_missing_scenario_counts_default_to_zero(self):
        verdict = {"failures": []}
        assert format_failure_summary(verdict).startswith("Total: 0 scenarios, 0 failed.")

    def test_missing_title_spec_error_default_to_empty_string(self):
        verdict = {
            "scenarios_run": 1, "scenarios_failed": 1,
            "failures": [{}],
        }
        assert format_failure_summary(verdict) == (
            "Total: 1 scenarios, 1 failed.\n\n- **** (): \n"
        )

    def test_multiline_error_uses_double_slash_separator(self):
        verdict = {
            "scenarios_run": 1, "scenarios_failed": 1,
            "failures": [{"title": "t", "spec": "s", "error": "a\nb\nc"}],
        }
        assert "a // b // c" in format_failure_summary(verdict)


class TestShouldRun:
    def test_missing_bdd_verdict_file_bails(self, tmp_path: Path):
        ok, reason = should_run(tmp_path)
        assert ok is False
        assert reason == "[mini-ork] reflection-refiner: no bdd-verdict.json — skipping"

    def test_unparseable_json_bails_with_same_missing_message(self, tmp_path: Path):
        (tmp_path / "bdd-verdict.json").write_text("{not json", encoding="utf-8")
        ok, reason = should_run(tmp_path)
        assert ok is False
        assert "no bdd-verdict.json" in reason

    def test_verdict_pass_bails_with_nothing_to_refine(self, tmp_path: Path):
        (tmp_path / "bdd-verdict.json").write_text(
            json.dumps({"verdict": "PASS"}), encoding="utf-8",
        )
        ok, reason = should_run(tmp_path)
        assert ok is False
        assert reason == "[mini-ork] reflection-refiner: bdd verdict=PASS — nothing to refine"

    def test_verdict_missing_key_treated_as_empty_string(self, tmp_path: Path):
        (tmp_path / "bdd-verdict.json").write_text(
            json.dumps({"scenarios_run": 1}), encoding="utf-8",
        )
        ok, reason = should_run(tmp_path)
        assert ok is False
        assert reason == "[mini-ork] reflection-refiner: bdd verdict= — nothing to refine"

    def test_verdict_fail_proceeds(self, tmp_path: Path):
        (tmp_path / "bdd-verdict.json").write_text(
            json.dumps({"verdict": "FAIL", "scenarios_run": 1, "scenarios_failed": 1,
                        "failures": [{"title": "t", "spec": "s", "error": "e"}]}),
            encoding="utf-8",
        )
        ok, reason = should_run(tmp_path)
        assert ok is True
        assert reason == ""

    def test_accepts_pathlike_and_str(self, tmp_path: Path):
        (tmp_path / "bdd-verdict.json").write_text(
            json.dumps({"verdict": "FAIL"}), encoding="utf-8",
        )
        assert should_run(str(tmp_path)) == (True, "")


class TestReadKickoffPath:
    def _make_db(self, tmp_path: Path) -> str:
        dbp = str(tmp_path / "state.db")
        con = sqlite3.connect(dbp)
        try:
            con.execute(
                "CREATE TABLE epics (id TEXT PRIMARY KEY, kickoff_path TEXT)"
            )
            con.execute(
                "INSERT INTO epics(id, kickoff_path) VALUES (?, ?)",
                ("epic-present", "kickoffs/p.md"),
            )
            con.execute(
                "INSERT INTO epics(id, kickoff_path) VALUES (?, ?)",
                ("epic-empty", ""),
            )
            # Row whose id contains a quote — exercises the parameterized
            # query path (the bash original string-interpolates the id,
            # which is unsafe; the port must not).
            con.execute(
                "INSERT INTO epics(id, kickoff_path) VALUES (?, ?)",
                ("epic-o'brien", "kickoffs/q.md"),
            )
            con.commit()
        finally:
            con.close()
        return dbp

    def test_present_row_returns_value(self, tmp_path: Path):
        db = self._make_db(tmp_path)
        assert read_kickoff_path(db, "epic-present") == "kickoffs/p.md"

    def test_present_but_empty_returns_empty_string(self, tmp_path: Path):
        db = self._make_db(tmp_path)
        assert read_kickoff_path(db, "epic-empty") == ""

    def test_missing_row_returns_none(self, tmp_path: Path):
        db = self._make_db(tmp_path)
        assert read_kickoff_path(db, "epic-missing") is None

    def test_quote_in_epic_id_is_handled_safely(self, tmp_path: Path):
        db = self._make_db(tmp_path)
        assert read_kickoff_path(db, "epic-o'brien") == "kickoffs/q.md"

    def test_nonexistent_db_path_returns_none(self):
        assert read_kickoff_path("/nonexistent/path/db.sqlite", "epic") is None


class TestBuildPrompt:
    TEMPLATE = (
        "## Head\n"
        "intro paragraph {{KICKOFF_PATH}}\n"
        "{{KICKOFF_BODY}}\n"
        "## Diff\n"
        "files changed: {{KICKOFF_PATH}}\n"
        "{{DIFF_FILES}}\n"
        "## Failures\n"
        "see: {{KICKOFF_PATH}}\n"
        "{{FAILURE_SUMMARY}}\n"
        "## Tail\n"
        "end marker {{KICKOFF_PATH}}\n"
    )

    def test_full_pipeline_assembly(self):
        out = build_prompt(
            self.TEMPLATE,
            kickoff_body="BODY-LINE-1\nBODY-LINE-2\n",
            kickoff_path="kickoffs/foo|bar.md",
            diff_files=["src/a.py", "src/b.py", "tests/test_x.py"],
            failure_summary="Total: 2 scenarios, 1 failed.\n\n- **t1** (s1): boom // stack\n",
        )
        assert out == (
            "## Head\n"
            "intro paragraph kickoffs/foo|bar.md\n"
            "BODY-LINE-1\nBODY-LINE-2\n"
            "## Diff\n"
            "files changed: kickoffs/foo|bar.md\n"
            "src/a.py\nsrc/b.py\ntests/test_x.py\n"
            "## Failures\n"
            "see: kickoffs/foo|bar.md\n"
            "Total: 2 scenarios, 1 failed.\n\n- **t1** (s1): boom // stack\n\n"
            "## Tail\n"
            "end marker kickoffs/foo|bar.md\n"
        )

    def test_bare_marker_line_is_dropped_bsd_awk_quirk(self):
        # When a marker is the WHOLE line, the gsub leaves only the newline,
        # which awk's `length($0)` treats as empty → the line is not printed.
        template = "head\n{{KICKOFF_BODY}}\ntail {{DIFF_FILES}}\n{{FAILURE_SUMMARY}}\nend\n"
        out = build_prompt(
            template, kickoff_body="BODY\n", kickoff_path="p",
            diff_files=["d1"], failure_summary="fs",
        )
        assert out == "head\nBODY\ntail \nd1\nfs\nend\n"

    def test_empty_diff_files_list_still_emits_newline(self):
        # Mirrors `echo "$diff_files"` on an empty string → one bare newline.
        # Each marker sits alone on its own line (as a real template would
        # have it) so every awk split fires independently.
        out = build_prompt(
            "{{KICKOFF_BODY}}\n{{DIFF_FILES}}\n{{FAILURE_SUMMARY}}\n",
            kickoff_body="B", kickoff_path="p", diff_files=[], failure_summary="F",
        )
        assert out == "B\nF\n"

    def test_kickoff_path_substituted_in_every_segment(self):
        out = build_prompt(
            "a {{KICKOFF_PATH}}\n{{KICKOFF_BODY}}b {{KICKOFF_PATH}}\n{{DIFF_FILES}}"
            "c {{KICKOFF_PATH}}\n{{FAILURE_SUMMARY}}d {{KICKOFF_PATH}}\n",
            kickoff_body="BODY\n", kickoff_path="KP", diff_files=["x"],
            failure_summary="FS",
        )
        assert "a KP\n" in out
        assert "b KP\n" in out
        assert "c KP\n" in out
        assert "d KP\n" in out


class TestExtractReflection:
    def test_extracts_from_heading_to_end_inclusive(self):
        log = (
            "preamble noise\n"
            "## Reflection refiner\n"
            "## Hypotheses\n"
            "- hypothesis 1\n"
            "- hypothesis 2\n"
            "## Remediation\n"
            "fix foo\n"
        )
        assert extract_reflection(log) == (
            "## Reflection refiner\n"
            "## Hypotheses\n"
            "- hypothesis 1\n"
            "- hypothesis 2\n"
            "## Remediation\n"
            "fix foo\n"
        )

    def test_no_match_returns_empty_string(self):
        assert extract_reflection("no heading here\n") == ""

    def test_empty_input_returns_empty_string(self):
        assert extract_reflection("") == ""

    def test_heading_must_be_at_line_start(self):
        # Indented / mid-line occurrence does not count as the anchor.
        log = "  ## Reflection refiner\nnot extracted\n"
        assert extract_reflection(log) == ""

    def test_heading_without_trailing_newline_still_extracted(self):
        log = "noise\n## Reflection refiner"
        assert extract_reflection(log) == "## Reflection refiner"


class TestRenderFallback:
    def test_matches_expected_heredoc_shape(self):
        fs = "Total: 1 scenarios, 1 failed.\n\n- **t** (s): boom\n"
        out = render_fallback(fs, "3")
        # fs's own trailing "\n" + render_fallback's appended "\n" (after the
        # f-string slot) + the literal blank-line "\n" before the footer line
        # together produce a THREE-newline gap ahead of "See iter-3/...".
        assert out == (
            "## Reflection refiner — fallback (LLM output unparseable)\n"
            "\n"
            "The reflection refiner did not produce parseable output. "
            "Raw failure summary:\n"
            "\n"
            "Total: 1 scenarios, 1 failed.\n\n- **t** (s): boom\n"
            "\n"
            "\n"
            "See iter-3/reflection.log for the full transcript.\n"
        )

    def test_empty_failure_summary_does_not_raise(self):
        out = render_fallback("", "1")
        assert out.endswith("See iter-1/reflection.log for the full transcript.\n")
        # Empty fs still contributes its own "\n" slot, so the gap grows to
        # four consecutive newlines between the label and the footer line.
        assert "Raw failure summary:\n\n\n\nSee" in out


class TestAppendToFeedback:
    def test_appends_blank_line_then_reflection_content(self, tmp_path: Path):
        iter_dir = tmp_path / "run"
        (iter_dir / "iter-2").mkdir(parents=True)
        refl_content = "## Reflection refiner\n- fix foo\n"
        (iter_dir / "iter-2" / "reflection.md").write_text(refl_content, encoding="utf-8")

        feedback_path = tmp_path / "feedback.md"
        feedback_path.write_text("existing feedback line\n", encoding="utf-8")

        ok = append_to_feedback("epic-app", "2", feedback_path, iter_dir)

        assert ok is True
        assert feedback_path.read_text(encoding="utf-8") == (
            "existing feedback line\n\n" + refl_content
        )

    def test_missing_reflection_file_is_a_noop(self, tmp_path: Path):
        iter_dir = tmp_path / "missing"
        iter_dir.mkdir()
        feedback_path = tmp_path / "feedback.md"
        feedback_path.write_text("untouched\n", encoding="utf-8")

        ok = append_to_feedback("epic-x", "9", feedback_path, iter_dir)

        assert ok is False
        assert feedback_path.read_text(encoding="utf-8") == "untouched\n"

    def test_creates_feedback_file_if_absent(self, tmp_path: Path):
        iter_dir = tmp_path / "run"
        (iter_dir / "iter-1").mkdir(parents=True)
        (iter_dir / "iter-1" / "reflection.md").write_text("content\n", encoding="utf-8")
        feedback_path = tmp_path / "new-feedback.md"

        ok = append_to_feedback("epic-new", "1", feedback_path, iter_dir)

        assert ok is True
        assert feedback_path.read_text(encoding="utf-8") == "\ncontent\n"
