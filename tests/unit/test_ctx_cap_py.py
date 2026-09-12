"""F6a: cap lens/reviewer context per call.

Live-DB receipts the audit measured (llm_calls, 4,679 rows): 195 calls with
>200K input tokens cost $140.23 of $1,235.64 total spend (11%), dominated by
lens lanes (codex_lens $48.69/84 calls, minimax_lens $28.64/5, glm_lens
$14.00/20). Two mechanisms, both fixed here:

  1. uncapped prompt inlines — _assemble_reviewer_inputs read whole verifier
     JSONs / ledgers / diffs into the prompt (worst section: multi-MB);
  2. agentic read-surface amplification — lens agents enumerated OTHER runs'
     artifacts (.mini-ork/runs held 529 runs / 3.8 GB inside MO_TARGET_CWD)
     and re-billed that context on every tool round-trip (worst call:
     14.77M input tokens, $2.89; 27 sibling lens calls returned no output).
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from mini_ork.cli.execute import _assemble_reviewer_inputs
from mini_ork.cli.execute_handlers import scope_guard_block
from mini_ork.context_assembler import (
    DEFAULT_SECTION_MAX_CHARS,
    cap_block,
    section_max_chars,
)


# ── cap_block ───────────────────────────────────────────────────────────────


def test_cap_block_passthrough_under_limit(monkeypatch):
    monkeypatch.delenv("MO_CTX_SECTION_MAX_CHARS", raising=False)
    text = "x" * 100
    assert cap_block(text) is text
    assert cap_block(text, 150) == text  # explicit cap above length


def test_cap_block_keeps_head_and_tail_with_marker():
    text = "A" * 600 + "MIDDLE" + "B" * 600
    out = cap_block(text, 100, label="verifier_test.json")
    assert len(out) < len(text)
    assert out.startswith("A" * 60)
    assert out.endswith("B" * 40)
    assert "omitted" in out and "verifier_test.json" in out


def test_cap_block_env_knob(monkeypatch):
    monkeypatch.setenv("MO_CTX_SECTION_MAX_CHARS", "10")
    out = cap_block("A" * 100 + "Z")
    assert "omitted" in out
    monkeypatch.setenv("MO_CTX_SECTION_MAX_CHARS", "0")  # disabled
    assert cap_block("A" * 100) == "A" * 100
    monkeypatch.setenv("MO_CTX_SECTION_MAX_CHARS", "not-an-int")
    assert section_max_chars() == DEFAULT_SECTION_MAX_CHARS


# ── reviewer-input assembly is capped ───────────────────────────────────────


def test_reviewer_inputs_caps_giant_section(tmp_path, monkeypatch):
    monkeypatch.setenv("MO_CTX_SECTION_MAX_CHARS", "2000")
    giant = tmp_path / "verifier_test.json"
    giant.write_text("{" + '"blob": "' + "x" * 500_000 + '"}')
    block = _assemble_reviewer_inputs(str(tmp_path))
    assert len(block) < 100_000
    assert "context cap: omitted" in block
    assert "verifier_test.json" in block  # label carried into the marker


def test_reviewer_inputs_caps_giant_diff(tmp_path, monkeypatch):
    monkeypatch.setenv("MO_CTX_SECTION_MAX_CHARS", "2000")
    # no git repo here → diff file is created empty → "(no diff)" branch
    block = _assemble_reviewer_inputs(str(tmp_path))
    assert "(no diff)" in block


# ── scope guard ─────────────────────────────────────────────────────────────


def test_scope_guard_names_allowed_run_dir_and_forbids_sibling_runs():
    guard = scope_guard_block("/runs/run-123")
    assert "/runs/run-123" in guard
    assert ".mini-ork/runs" in guard
    assert "node_modules" in guard
    assert ".git" in guard
