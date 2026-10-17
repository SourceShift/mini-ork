"""One kickoff-scope parser (LF-R8): ``mini_ork.kickoff_scope``.

Three readers used to disagree about the same kickoff text. These tests pin the
one parser and the three callers rewired onto it:

- ``cli.main.gen_profile``'s ``scope_allow`` / ``scope_deny`` (``heading_kind``),
- ``memory.preferences.paths_in_text`` / ``scope_paths`` (``paths_in_line``),
- ``context_v2.parse_contract``'s ``files_in_scope`` / ``do_not``
  (``kickoff_scope`` / ``do_not_sentences``).

Fixtures are inline and hermetic: temp kickoffs and temp profiles only, no repo
reads beyond the module imports, no live run dir and no environment dependence.

Kickoff: ``kickoffs/auto/lf-r8-kickoff-scope.md``.
"""
from __future__ import annotations

import pytest

from mini_ork.cli import main as cli
from mini_ork.context_v2 import parse_contract
from mini_ork.kickoff_scope import (
    do_not_sentences,
    heading_kind,
    kickoff_scope,
    paths_in_line,
)
from mini_ork.memory.preferences import paths_in_text, scope_paths

FOUR_BARE_BULLETS = """# LF-R8 fixture

## Files in scope

- mini_ork/kickoff_scope.py
- mini_ork/cli/main.py
- mini_ork/memory/preferences.py
- mini_ork/context_v2.py

## Rules

Edit files directly.
"""

FOUR_PATHS = [
    "mini_ork/kickoff_scope.py",
    "mini_ork/cli/main.py",
    "mini_ork/memory/preferences.py",
    "mini_ork/context_v2.py",
]

DENY_ONLY = """# LF-R8 fixture

## Explicitly out of scope — do NOT touch

- `mini_ork/escalation_policy.py`
- `mini_ork/cli/execute.py`
- `recipes/**`
"""

FENCED_DECOY = """# LF-R8 fixture

## Files in scope

- mini_ork/kickoff_scope.py

```
## Files in scope

- fake.py
```
"""

WRAPPED_NEVER = """# LF-R8 fixture

## Function contract

- `scope_paths` must return paths and
    never raises into the run.

Edit files directly; do not emit unified diffs. Do not touch
escalation_policy.py, execute.py, or any recipe.
"""


def _gen_profile(tmp_path, text: str) -> dict:
    kickoff = tmp_path / "kickoff.md"
    kickoff.write_text(text, encoding="utf-8")
    (tmp_path / "agents.yaml").write_text("lanes:\n  implementer: codex\n",
                                          encoding="utf-8")
    return cli.gen_profile(
        kickoff_path=str(kickoff),
        root=str(tmp_path),
        recipe="code_fix",
        task_class="code_fix",
        profile_path=str(tmp_path / "run_profile.json"),
        agents_path=str(tmp_path / "agents.yaml"),
    )


# ─── heading_kind ───────────────────────────────────────────────────────────

class TestHeadingKind:
    @pytest.mark.parametrize("title", [
        "Out of scope",
        "Explicitly out of scope — do NOT touch",
        "Out-of-scope files",
        "Not in scope",
        "Outside scope",
        "Non-goals",
        "Scope deny",
        "Forbidden surfaces",
        "Do not touch",
        "OUT OF SCOPE",
    ])
    def test_deny_phrases(self, title):
        assert heading_kind(title) == "deny"

    @pytest.mark.parametrize("title", [
        "Files in scope",
        "Scope allow",
        "In scope",
        "Scope and goals",
        "FILES IN SCOPE",
    ])
    def test_allow_phrases(self, title):
        assert heading_kind(title) == "allow"

    @pytest.mark.parametrize("title", [
        "Goal", "Mechanism (exact)", "Verification commands", "Rules", "Tests",
    ])
    def test_unrelated_titles_are_none(self, title):
        assert heading_kind(title) is None

    def test_deny_wins_over_the_allow_trigger(self):
        # Each of these also contains an allow trigger ("scope"/"in scope") yet
        # a scope the kickoff rules OUT must never read as an allow section.
        for title in ("Out of scope", "Not in scope",
                      "Explicitly out of scope — do NOT touch"):
            assert heading_kind(title) == "deny", title


# ─── paths_in_line ──────────────────────────────────────────────────────────

class TestPathsInLine:
    def test_backticked_tokens(self):
        assert paths_in_line("- touch `mini_ork/kickoff_scope.py` only") == [
            "mini_ork/kickoff_scope.py"]
        assert paths_in_line("`mini_ork/a.py` and `mini_ork/b.py`") == [
            "mini_ork/a.py", "mini_ork/b.py"]

    def test_glob_targets_ending_double_star_are_excluded(self):
        # A glob `**` is a rule TARGET, never a file the run edits.
        assert paths_in_line("- `recipes/**` is off limits") == []
        assert paths_in_line("`mini_ork/**`") == []

    def test_bare_bullet_head(self):
        assert paths_in_line("- mini_ork/kickoff_scope.py") == [
            "mini_ork/kickoff_scope.py"]
        assert paths_in_line("- docs/plans/2026-10-15-learning-loop-fixes.md") == [
            "docs/plans/2026-10-15-learning-loop-fixes.md"]
        # No slash, but ends in a .ext.
        assert paths_in_line("- pyproject.toml") == ["pyproject.toml"]

    def test_head_cut_delimiters(self):
        assert paths_in_line("- mini_ork/cli/main.py — the CLI entrypoint") == [
            "mini_ork/cli/main.py"]
        assert paths_in_line("- tests/unit/test_kickoff_scope.py (new)") == [
            "tests/unit/test_kickoff_scope.py"]
        assert paths_in_line("- mini_ork/foo.py: keep pure") == ["mini_ork/foo.py"]

    def test_a_dot_slash_prefix_is_normalized(self):
        assert paths_in_line("- ./mini_ork/cli/main.py") == ["mini_ork/cli/main.py"]

    def test_a_backticked_head_is_not_counted_twice(self):
        assert paths_in_line("- `mini_ork/a.py`, `mini_ork/b.py` and friends") == [
            "mini_ork/a.py", "mini_ork/b.py"]

    def test_prose_words_are_never_paths(self):
        assert paths_in_line("- Keep the change small") == []
        assert paths_in_line("Keep the change small.") == []
        # A flush-left prose line is not a list item.
        assert paths_in_line("see mini_ork/foo.py in prose") == []

    def test_a_scope_token_holds_no_whitespace(self):
        # A backticked shell command has a "/" but is not a file the run
        # edits; nor is a stray bullet marker inside backticks.
        assert paths_in_line("- see `python -m pytest tests/unit/test_x.py`") == []
        assert paths_in_line(
            "- a fenced block containing `## Files in scope` and `- fake.py`") == []

    def test_trailing_commentary_is_stripped(self):
        assert paths_in_line("`mini_ork/cli/main.py — the CLI entrypoint`") == [
            "mini_ork/cli/main.py"]


# ─── kickoff_scope ──────────────────────────────────────────────────────────

class TestKickoffScope:
    def test_four_bare_bullets_yield_four_paths(self):
        assert kickoff_scope(FOUR_BARE_BULLETS) == {
            "allow": FOUR_PATHS, "deny": []}

    def test_deny_only_paths_never_reach_allow(self):
        result = kickoff_scope(DENY_ONLY)
        assert result["allow"] == []
        assert result["deny"] == [
            "mini_ork/escalation_policy.py", "mini_ork/cli/execute.py"]
        assert not any(p.endswith("**") for p in result["deny"])

    def test_a_fenced_block_contributes_nothing(self):
        result = kickoff_scope(FENCED_DECOY)
        assert result["allow"] == ["mini_ork/kickoff_scope.py"]
        assert result["deny"] == []

    def test_normalized_deduped_order_kept(self):
        text = (
            "# F\n\n## Files in scope\n\n- ./mini_ork/kickoff_scope.py\n"
            "- mini_ork/kickoff_scope.py\n- ./mini_ork/cli/main.py\n\n"
            "## Out of scope\n\n- mini_ork/other.py\n"
        )
        assert kickoff_scope(text) == {
            "allow": ["mini_ork/kickoff_scope.py", "mini_ork/cli/main.py"],
            "deny": ["mini_ork/other.py"],
        }

    def test_a_path_in_both_lists_stays_only_in_deny(self):
        text = (
            "# F\n\n## Files in scope\n\n- mini_ork/shared.py\n\n"
            "## Out of scope\n\n- mini_ork/shared.py\n- mini_ork/other.py\n"
        )
        result = kickoff_scope(text)
        assert result["allow"] == []
        assert result["deny"] == ["mini_ork/shared.py", "mini_ork/other.py"]

    def test_h2_through_h6_only(self):
        text = (
            "# Files in scope\n\n- mini_ork/h1_decoy.py\n\n"
            "## Files in scope\n\n- mini_ork/real.py\n\n"
            "###### Files in scope\n\n- mini_ork/deep.py\n"
        )
        assert kickoff_scope(text)["allow"] == [
            "mini_ork/real.py", "mini_ork/deep.py"]


# ─── do_not_sentences ───────────────────────────────────────────────────────

class TestDoNotSentences:
    def test_wrapped_continuation_yields_nothing_and_midline_rules_survive(self):
        # "- `scope_paths` must return paths and /     never raises into the
        # run." is ONE logical item starting with "scope_paths", so the wrapped
        # fragment is not a rule; the mid-line "do not …" sentences are.
        assert do_not_sentences(WRAPPED_NEVER) == [
            "do not emit unified diffs.",
            "Do not touch escalation_policy.py, execute.py, or any recipe.",
        ]

    def test_triggers_are_case_insensitive_only_at_the_start(self):
        result = do_not_sentences(
            "Do not write to the live DB. Don't rebase the worktree. "
            "never merge main. MUST NOT exceed the budget. "
            "We never rebase. It must not block the gate.\n"
        )
        for kept in ("Do not write to the live DB.", "Don't rebase the worktree.",
                     "never merge main.", "MUST NOT exceed the budget."):
            assert kept in result
        assert not any(s.startswith("We never rebase") for s in result)
        assert not any(s.startswith("It must not") for s in result)

    def test_markup_before_the_trigger_is_stripped(self):
        result = do_not_sentences("- **Never** write to the live DB.\n"
                                  "- __Do not__ emit diffs.\n")
        assert "Never write to the live DB." in result
        assert "Do not emit diffs." in result

    def test_capped_at_200_chars(self):
        result = do_not_sentences("Do not " + "x" * 300 + ".\n")
        assert result and len(result[0]) == 200
        assert result[0].startswith("Do not")

    def test_deduped_in_order(self):
        assert do_not_sentences(
            "Never rebase the worktree. Do not emit diffs. "
            "Never rebase the worktree. Do not emit diffs.\n"
        ) == ["Never rebase the worktree.", "Do not emit diffs."]

    def test_fenced_text_is_ignored(self):
        result = do_not_sentences(
            "Never rebase the worktree.\n\n```\nNever trust fenced rules.\n```\n")
        assert result == ["Never rebase the worktree."]

    def test_paragraph_lines_are_joined(self):
        assert do_not_sentences("Do not touch recipes\nunder any circumstances.\n") == [
            "Do not touch recipes under any circumstances."]

    def test_a_list_item_rule_is_found(self):
        assert do_not_sentences("## Rules\n\n- Never write to the live DB.\n") == [
            "Never write to the live DB."]

    def test_the_real_rules_section_is_captured(self):
        """The kickoff's own Rules: every rule is mid-line under a ``## Rules``
        heading whose kind is None, and all three used to be missed."""
        result = do_not_sentences(
            "## Rules\n\n"
            "Edit files directly; do not emit unified diffs. Never rebase, merge or reset\n"
            "the worktree during the run. Do not change `context_v2.build`, the pack schema,\n"
            "`prefs_for`'s glob semantics, or anything under `recipes/`, and do not write to\n"
            "the live DB.\n"
        )
        assert result[0] == "do not emit unified diffs."
        assert any(s.startswith("Never rebase, merge or reset") for s in result)
        assert any(s.startswith("Do not change") for s in result)
        assert not any(s.startswith("Edit files directly") for s in result)


# ─── the three callers agree ────────────────────────────────────────────────

def test_three_callers_agree_on_four_bare_bullets(tmp_path):
    """``gen_profile``, ``parse_contract`` and ``scope_paths`` all read the one
    parser: the four bare bullets reach all three identically."""
    profile = _gen_profile(tmp_path, FOUR_BARE_BULLETS)

    assert profile["scope_allow"] == FOUR_PATHS
    assert kickoff_scope(FOUR_BARE_BULLETS)["allow"] == FOUR_PATHS
    assert parse_contract(FOUR_BARE_BULLETS)["files_in_scope"] == FOUR_PATHS
    assert scope_paths(str(tmp_path)) == FOUR_PATHS


def test_out_of_scope_never_reaches_scope_allow(tmp_path):
    """A deny heading contains the word "scope" but must feed only ``deny``."""
    profile = _gen_profile(tmp_path, DENY_ONLY)

    assert profile["scope_allow"] == []
    assert profile["scope_deny"] == [
        "`mini_ork/escalation_policy.py`",
        "`mini_ork/cli/execute.py`",
        "`recipes/**`",
    ]
    assert parse_contract(DENY_ONLY)["files_in_scope"] == []
    assert scope_paths(str(tmp_path)) == []


def test_gen_profile_keeps_raw_bullet_entries(tmp_path):
    """The entry format is unchanged: a raw bullet line, so existing profile
    consumers keep working."""
    profile = _gen_profile(tmp_path, """# F

## Files in scope

- `mini_ork/a.py`, `mini_ork/b.py` and friends

## Out of scope

- `mini_ork/escalation_policy.py`
""")
    assert profile["scope_allow"] == ["`mini_ork/a.py`, `mini_ork/b.py` and friends"]
    assert profile["scope_deny"] == ["`mini_ork/escalation_policy.py`"]


def test_prose_bullets_are_not_paths_in_the_profile(tmp_path):
    profile = _gen_profile(tmp_path, """# F

## Files in scope

- mini_ork/foo.py
- Keep the change small
""")
    assert profile["scope_allow"] == ["mini_ork/foo.py", "Keep the change small"]
    # …but only the path reaches the path-rule matcher.
    assert scope_paths(str(tmp_path)) == ["mini_ork/foo.py"]


def test_paths_in_text_accepts_bare_bullets_and_skips_prose():
    text = ("## Files in scope\n\n- mini_ork/foo.py\n- `mini_ork/bar.py`\n"
            "- Keep the change small\n")
    assert paths_in_text(text) == ["mini_ork/foo.py", "mini_ork/bar.py"]
    assert paths_in_text("- `recipes/**`\n- prose only here\n") == []
