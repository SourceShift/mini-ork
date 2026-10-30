---
oracle: git
---
# Frozen probe probe-h02 — code-fix harvested task

FROZEN probe kickoff harvested by mini_ork.learning.probe_harvest from mini-ork's failure history. Never edit it to fit a candidate. It is graded by hidden tests, not by this text.

## Problem

fix(researcher): recover lens-<family>.json from stdout when the agent only printed it

The chapter-review lens prompt asks each agent to do two things: emit its score
object on stdout, and write `$MINI_ORK_RUN_DIR/lens-<family>.json` with a tool.
The harness only enforces the first — the researcher handler captures stdout to
`lens-<family>.md` via `_researcher_output_file`, so the `.md` is always
produced, while the `.json` is a model-side coin flip.

Live consequence: in run-1789884609-10608 the kimi and opus lenses printed
their objects and wrote nothing (lens-kimi.md / lens-opus.md are pure JSON, no
.json sibling) while glm wrote the file and printed prose. The panel-completeness
verifier reads `lens-<family>.json` unconditionally, so it failed, the
synthesizer never emitted `chapter-review.json`, and the run was discarded as
`failed_nodes=4` with rubric 2/8 — the chapter's rubric verdict could never
flip on that attempt.

Fix: after the researcher handler persists the agent output, a lens node whose
canonical `.json` is missing or empty recovers the object from the text we
already hold, using the same brace-balanced scanner idiom the repo already uses
in `gates/rubric_scoring.py` and `gates/mutation_adversary.py`. The scanner is
keyed on `"lens"` so recovery stays anchored to the artifact's own shape, and
an existing non-empty sibling always wins.

Fail-soft by design: unparseable output writes nothing and the node still
returns rc 0, so a malformed object degrades to today's behaviour (verifier red)
rather than crashing the node.

## Files in scope

- mini_ork/cli/execute_handlers.py

## Definition of Done

Fix the behaviour described above so the repository state at HEAD satisfies the spec.
