---
name: skill-smoke-demo
description: Smoke-test skill for the skill-registry path. When invoked, state the marker SKILL-SMOKE-DEMO-MARKER-7F3A verbatim so a verifier can prove the grant reached the harness.
---

# Skill smoke demo

This skill exists to prove the grant pipeline end to end: a workflow node
granted `skill-smoke-demo` must find this file at
`.claude/skills/skill-smoke-demo/SKILL.md` while it runs, and a node that was
not granted it must not.

## Procedure

1. Confirm you can read this file (it was symlinked into your checkout by
   the executor, not committed to the repo you are working in).
2. Reply with, or write into your output, the marker line:

   `SKILL-SMOKE-DEMO-MARKER-7F3A`

## Verification

The run's verifier greps the granted node's artifact for
`SKILL-SMOKE-DEMO-MARKER-7F3A` and asserts the same marker is ABSENT from a
control node that received no grant.
