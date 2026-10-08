"""B7 — a lane's suite run must be scoped to a file, never a bare name filter.

Observed live: an implementer ran `jest -t "<name>"`, which collects the whole
repo and spawns one worker per test file — 11 workers × ~900 MB on an 11-core
box, load average 124–138 for 8+ minutes. A name filter is not a scoping tool;
the file path is.

Two prompt surfaces carry the rule and this file pins both:

* ``recipes/code-fix/prompts/implementer.md`` — the implementer's own rule;
* ``scope_guard_block`` — the prelude every agentic lane prompt already has.

These are source-level pins (the same shape as the existing code-fix verifier
source pins): the remediation IS the prompt text, so the text is the contract.
"""
from __future__ import annotations

from pathlib import Path

from mini_ork.cli.execute_handlers import scope_guard_block

REPO = Path(__file__).resolve().parents[2]
IMPLEMENTER = REPO / "recipes" / "code-fix" / "prompts" / "implementer.md"


def test_implementer_prompt_scopes_the_suite_to_a_file():
    text = IMPLEMENTER.read_text(encoding="utf-8")

    # the positive form: run the changed file path
    assert "path/to/x.test.ts" in text
    assert "--maxWorkers=2" in text
    # the negative form: a bare name filter is the failure mode
    assert "--testNamePattern" in text
    assert "jest -t" in text


def test_implementer_prompt_says_the_name_filter_is_what_fans_out():
    # collapse the markdown line wraps so the phrases below are contiguous
    text = " ".join(IMPLEMENTER.read_text(encoding="utf-8").split())

    # the *why* must be present, or a future edit drops the rule as pedantry
    assert "collect the whole repo" in text
    assert "worker per file" in text


def test_scope_guard_carries_the_resource_guard_for_every_lane():
    block = scope_guard_block("/tmp/run-x")

    assert "Resource guard" in block
    assert "--maxWorkers=2" in block
    assert "--testNamePattern" in block
    # it names the file-path form, not just the prohibition
    assert "test FILE path" in block


def test_scope_guard_wording_matches_the_implementer_rule():
    """Both surfaces must agree — a lane that reads one and not the other
    still gets the same instruction."""
    block = scope_guard_block("/tmp/run-x")
    text = IMPLEMENTER.read_text(encoding="utf-8")

    for phrase in ("--maxWorkers=2", "--testNamePattern"):
        assert phrase in block, phrase
        assert phrase in text, phrase
