"""Recipe prompts must name artifacts the dispatcher actually publishes.

A prompt is text handed to a node agent. The agent sees it verbatim: nothing
``expandvars`` a recipe prompt, so a token is only useful if it is the NAME of
an environment variable the agent's shell has. ``mini_ork/context.py``
publishes ``MINI_ORK_RUN_DIR`` and not ``RUN_DIR``; nothing has published
``CONTEXT_FILE`` since the dispatcher stopped setting it.

Both dead tokens survived in the recursive-self-improve and obs-smoke prompts,
and the lens prompts additionally read `${RUN_DIR}/bottleneck-scan.md` — a name
that stopped existing when the scanner node became `bottleneck_lens` and the
dispatcher's `_lens` heuristic started writing `lens-bottleneck.md`. The
failure is silent: the agent writes to a relative path it never gets told
about, and the next node reads a file that is not there.

This guard is deliberately static and cheap. It does not try to model every
recipe (most lens prompts name no output file at all — their verifiers check
the artifact); it pins the two tokens that are never set, and the artifact
wiring for the prompts that were broken.
"""
from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
PROMPTS = sorted(REPO_ROOT.glob("recipes/*/prompts/*.md"))

# Dead tokens. `${RUN_DIR}` and `${CONTEXT_FILE}` are braced on purpose: a bare
# `$RUN_DIR` is legitimate inside a shell snippet that assigns it first
# (`recipes/recipe-creator/prompts/verifier-smith.md` does exactly that).
DEAD_TOKENS = ("CONTEXT_FILE", "${RUN_DIR}")

# prompt -> the artifact its node id selects. `bottleneck_lens` writes
# `lens-bottleneck.md`; the others follow the same stem rule.
WRITER_PROMPTS = {
    "recursive-self-improve/prompts/bottleneck-scan.md": "lens-bottleneck.md",
    "recursive-self-improve/prompts/lens-minimax-perf.md": "lens-perf.md",
    "recursive-self-improve/prompts/lens-kimi-correctness.md": "lens-correctness.md",
    "recursive-self-improve/prompts/lens-codex-arch.md": "lens-arch.md",
    "recursive-self-improve/prompts/arxiv-researcher.md": "lens-arxiv.md",
    "obs-smoke/prompts/tiny-researcher.md": "lens-tiny.md",
}

# Each lens reads the scanner's output, which is produced by `bottleneck_lens`.
LENS_PROMPTS = (
    "recursive-self-improve/prompts/lens-minimax-perf.md",
    "recursive-self-improve/prompts/lens-kimi-correctness.md",
    "recursive-self-improve/prompts/lens-codex-arch.md",
    "recursive-self-improve/prompts/arxiv-researcher.md",
)


def test_no_prompt_uses_an_unpublished_env_token() -> None:
    assert PROMPTS, "no recipe prompts found — wrong root?"
    offenders = []
    for path in PROMPTS:
        text = path.read_text(encoding="utf-8")
        for token in DEAD_TOKENS:
            if token in text:
                offenders.append(f"{path.relative_to(REPO_ROOT)}: {token}")
    assert not offenders, (
        "recipe prompts name an environment variable the node never receives:\n  "
        + "\n  ".join(offenders)
    )


def test_writer_prompts_name_the_artifact_their_node_id_selects() -> None:
    """`_researcher_output_file` picks `lens-<stem>.md` from the node id."""
    for rel, artifact in WRITER_PROMPTS.items():
        text = (REPO_ROOT / "recipes" / rel).read_text(encoding="utf-8")
        assert f"${{MINI_ORK_RUN_DIR}}/{artifact}" in text, (
            f"{rel} never names its own output, {artifact}"
        )


def test_lens_prompts_read_the_bottleneck_scanners_real_output() -> None:
    for rel in LENS_PROMPTS:
        text = (REPO_ROOT / "recipes" / rel).read_text(encoding="utf-8")
        assert "${MINI_ORK_RUN_DIR}/lens-bottleneck.md" in text, (
            f"{rel} does not read the scanner's lens-bottleneck.md"
        )
        assert "bottleneck-scan.md" not in text, (
            f"{rel} still points at the retired bottleneck-scan.md"
        )
