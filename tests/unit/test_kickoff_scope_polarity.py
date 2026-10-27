"""Scope polarity inside an allow section (RSI Tier-0 #7, loop defect D3).

A negative clause used to be ignored when it appeared under an allow-kind
heading, so ``Leave `.mini-ork/` (run state) untouched`` put ``.mini-ork/`` in
``allow`` — the inverse of the kickoff's intent. These tests pin the
clause-aware rule in ``mini_ork.kickoff_scope``.

Hermetic: inline text only, no I/O, no environment.
"""
from __future__ import annotations

from mini_ork.kickoff_scope import (
    _logical_lines,
    do_not_sentences,
    kickoff_scope,
)

# The kickoff's template, exactly as written — including the wrap on the third
# bullet line.
TEMPLATE = (
    "## Scope\n"
    "- Target repo: this mini-ork checkout at `/tmp/wt` — an isolated git worktree\n"
    "  on branch self-improve/iter-1-x.\n"
    "- Improvable surface: the `mini_ork/` Python package, `recipes/`, and\n"
    "  `schemas/`. Leave `.mini-ork/` (run state) untouched; tests are read-only\n"
    "  context unless the patch adds a regression test.\n"
    "- Exactly one patch this iteration; keep the diff minimal and reviewable.\n"
)


def _scoped(heading: str, *lines: str) -> str:
    return "\n".join([heading, *lines]) + "\n"


def test_template_denies_run_state():
    scope = kickoff_scope(TEMPLATE)
    assert ".mini-ork/" in scope["deny"]
    assert ".mini-ork/" not in scope["allow"]


def test_template_allows_declared_surface():
    scope = kickoff_scope(TEMPLATE)
    for path in ("mini_ork/", "recipes/", "schemas/"):
        assert path in scope["allow"]
        assert path not in scope["deny"]


def test_template_emits_the_leave_rule():
    assert "Leave .mini-ork/ (run state) untouched" in do_not_sentences(TEMPLATE)


def test_mixed_clauses_on_one_line():
    scope = kickoff_scope(
        _scoped("## Files in scope", "- Leave `a/` untouched; edit `b/foo.py`."))
    assert "a/" in scope["deny"] and "a/" not in scope["allow"]
    assert "b/foo.py" in scope["allow"] and "b/foo.py" not in scope["deny"]


def test_a_trigger_clause_denies():
    scope = kickoff_scope(
        _scoped("## Files in scope", "- Do not touch `x/y.py`."))
    assert "x/y.py" in scope["deny"]
    assert "x/y.py" not in scope["allow"]


def test_keep_unchanged_denies():
    scope = kickoff_scope(
        _scoped("## Scope", "Keep `config/agents.yaml` unchanged."))
    assert "config/agents.yaml" in scope["deny"]
    assert "config/agents.yaml" not in scope["allow"]


def test_keep_without_the_qualifier_stays_allowed():
    scope = kickoff_scope(
        _scoped("## Scope", "Keep the tests in `tests/unit/test_a.py` passing."))
    assert "tests/unit/test_a.py" in scope["allow"]
    assert "tests/unit/test_a.py" not in scope["deny"]


def test_bare_bullet_heads_still_parse():
    scope = kickoff_scope(
        _scoped("## Files in scope", "- mini_ork/foo.py — the parser"))
    assert "mini_ork/foo.py" in scope["allow"]
    assert "mini_ork/foo.py" not in scope["deny"]


def test_deny_sections_are_unchanged():
    scope = kickoff_scope(_scoped("## Out of scope", "- `z/` (keep it)"))
    assert "z/" in scope["deny"]
    assert "z/" not in scope["allow"]


# ─── wrapped "do not" sentences keep their polarity on every line ───────────

# Every fixture below sits under the allow-kind heading the kickoff names.
_SCOPE_HEADING = "## Files in scope (touch ONLY these)"
# `paths_in_line` admits only path-like tokens (a `/` or a trailing `.ext`),
# so the kickoff's shorthand `a`/`b` are spelled `a.py`/`b.py` here.
_WRAPPED_DENY = (
    _SCOPE_HEADING + "\n"
    "\n"
    "- `a/x.py`\n"
    "\n"
    "Do NOT modify `a/y.py`, `a/z.py`,\n"
    "`recipes/`, `docs/` (note), or any path in\n"
    "`config/kernel-frozen.yaml`. Every existing test must pass.\n"
)


def test_a_wrapped_deny_sentence_denies_every_wrapped_path():
    """Kickoff test 1: the Goal's wrapped sentence, with a bullet before it."""
    scope = kickoff_scope(_WRAPPED_DENY)
    assert scope["allow"] == ["a/x.py"]
    for denied in ("a/y.py", "a/z.py", "recipes/", "docs/",
                   "config/kernel-frozen.yaml"):
        assert denied in scope["deny"]
    assert "a/x.py" not in scope["deny"]


def test_a_new_sentence_after_a_terminated_deny_is_allowed():
    """Kickoff test 2: `Do not modify `a`.` then `Also edit `b`.`."""
    scope = kickoff_scope(
        _scoped(_SCOPE_HEADING,
                "Do not modify `a.py`.",
                "Also edit `b.py`."))
    assert scope["allow"] == ["b.py"]
    assert scope["deny"] == ["a.py"]


def test_a_bullet_with_a_wrapped_description_stays_allowed():
    """Kickoff test 3: a wrapped description is part of the same bullet."""
    scope = kickoff_scope(
        _scoped(_SCOPE_HEADING,
                "- `a.py` (new;",
                "  do not add tests here)"))
    assert scope["allow"] == ["a.py"]
    assert "a.py" not in scope["deny"]


def test_a_bare_bullet_head_still_parses():
    """Kickoff test 4: `- mini_ork/foo.py` (no backticks) is in scope."""
    scope = kickoff_scope(_scoped(_SCOPE_HEADING, "- mini_ork/foo.py"))
    assert scope["allow"] == ["mini_ork/foo.py"]
    assert "mini_ork/foo.py" not in scope["deny"]


def test_a_blank_line_ends_the_sentence():
    """Kickoff test 5: the paragraph after a blank line is a new sentence."""
    scope = kickoff_scope(
        _scoped(_SCOPE_HEADING,
                "Do not modify `a.py`,",
                "",
                "`b.py` is in scope."))
    assert scope["allow"] == ["b.py"]
    assert scope["deny"] == ["a.py"]


def test_a_semicolon_ends_the_clause():
    """Kickoff test 6: `Do not modify `a`; edit` / ``b` instead.`"""
    scope = kickoff_scope(
        _scoped(_SCOPE_HEADING,
                "Do not modify `a.py`; edit",
                "`b.py` instead."))
    assert scope["allow"] == ["b.py"]
    assert scope["deny"] == ["a.py"]


def test_logical_lines_default_strips_the_list_marker():
    """Kickoff test 7: `keep_marker=False` keeps the strip-marker behaviour."""
    text = "- a\n  b\n1. c\n"
    assert _logical_lines(text) == ["a b", "c"]
    assert _logical_lines(text, keep_marker=False) == ["a b", "c"]
    assert _logical_lines(text, keep_marker=True) == ["- a b", "1. c"]


# ── review 130 ───────────────────────────────────────────────────────────────

_SCOPE_H = "## Files in scope (touch ONLY these)\n\n"


def test_a_do_not_line_after_an_unterminated_line_still_denies():
    scope = kickoff_scope(
        _SCOPE_H + "Files you may touch: `a.py`\nDo not modify `docs/`.\n")
    assert scope["allow"] == ["a.py"]
    assert "docs/" in scope["deny"]


def test_a_wrapped_positive_line_does_not_flip_to_deny():
    scope = kickoff_scope(_SCOPE_H + "Edit `a.py` and\n`b.py` together.\n")
    assert scope["allow"] == ["a.py", "b.py"] and scope["deny"] == []


def test_numbered_items_keep_their_bare_head():
    scope = kickoff_scope(_SCOPE_H + "1. mini_ork/foo.py\n2. `mini_ork/bar.py`\n")
    assert scope["allow"] == ["mini_ork/foo.py", "mini_ork/bar.py"]


def test_do_not_sentences_default_joining_is_unchanged():
    from mini_ork.kickoff_scope import _logical_lines

    assert _logical_lines("Files you may touch:\nDo not modify `docs/`.\n") == [
        "Files you may touch: Do not modify `docs/`."]
