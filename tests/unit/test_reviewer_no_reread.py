"""C9a — a reviewer prompt must not tell the agent to re-read inlined inputs.

The classic reviewer node is handed its whole input set inlined: the runtime
(``mini_ork/cli/execute.py::_assemble_reviewer_inputs``) appends a
"Reviewer inputs" block carrying ``implementer-summary.json``, the verifier
verdicts, and ``review-diff.patch`` into the prompt itself. Yet two reviewer
prompts also said "read the whole diff, every changed file" — so the agent
issues Read calls against files it already has, spending tokens re-reading the
same bytes.

The remediation is prompt text, so the text is the contract: each reviewer
prompt must (a) declare the inlined block authoritative and (b) not instruct the
agent to read files it already holds. These are source pins, the same shape as
``tests/unit/test_lane_test_scoping.py``.
"""
from __future__ import annotations

from pathlib import Path

REPO = Path(__file__).resolve().parents[2]

REVIEWER_PROMPTS = {
    "code-fix": REPO / "recipes" / "code-fix" / "prompts" / "reviewer.md",
    "framework-edit": REPO / "recipes" / "framework-edit" / "prompts" / "reviewer.md",
}


def _flat(path: Path) -> str:
    # collapse markdown line wraps so multi-line phrases are contiguous
    return " ".join(path.read_text(encoding="utf-8").split())


def test_reviewer_prompts_do_not_tell_the_agent_to_re_read_the_diff():
    for name, path in REVIEWER_PROMPTS.items():
        text = _flat(path)
        # the failure mode: an imperative to read files that are already inlined
        assert "read the whole diff, every changed file" not in text, name


def test_reviewer_prompts_declare_the_inlined_block_authoritative():
    for name, path in REVIEWER_PROMPTS.items():
        text = _flat(path)
        # the positive contract: the block is what you read, not the paths
        assert "do not issue Read calls" in text, name
        assert "do not re-open" in text or "re-open them" in text, name


def test_reviewer_prompts_say_the_inputs_are_assembled_for_you():
    # both must tell the agent the inputs arrive inline (not as paths to open)
    for name, path in REVIEWER_PROMPTS.items():
        text = _flat(path)
        assert "assembled for you" in text, name
