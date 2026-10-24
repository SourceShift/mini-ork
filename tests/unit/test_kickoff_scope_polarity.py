"""Scope polarity inside an allow section (RSI Tier-0 #7, loop defect D3).

A negative clause used to be ignored when it appeared under an allow-kind
heading, so ``Leave `.mini-ork/` (run state) untouched`` put ``.mini-ork/`` in
``allow`` — the inverse of the kickoff's intent. These tests pin the
clause-aware rule in ``mini_ork.kickoff_scope``.

Hermetic: inline text only, no I/O, no environment.
"""
from __future__ import annotations

from mini_ork.kickoff_scope import do_not_sentences, kickoff_scope

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
