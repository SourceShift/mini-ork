---
oracle: git
---
# Frozen probe probe-h11 — code-fix harvested task

FROZEN probe kickoff harvested by mini_ork.learning.probe_harvest from mini-ork's failure history. Never edit it to fit a candidate. It is graded by hidden tests, not by this text.

## Problem

fix(epics): harden ingest — skip (no-epic) headings + resolve dep targets (#144)

Two generic footguns in `epics ingest`:

1. Every `## heading` became an epic, so preamble/context sections
   (Symptom, Evidence, Constraints, ...) turned into junk epic rows. Add an
   opt-out marker: a heading ending in `(no-epic)`/`(context)`/`(ignore)` is
   skipped (heading + body). Backward-compatible.

2. Dependency targets were split on whitespace and inserted raw without
   matching a real epic id, so a natural `depends on: Epic 0` produced bogus
   or dropped edges and a silent `0 dep edges`. Now split dep lists on commas
   only and resolve each token to a real epic id (exact -> slug-exact ->
   unique slug-prefix -> unique slug-substring, across parsed + existing
   epics). Unresolved/ambiguous tokens emit a WARNING and are counted as
   skipped in the summary — never silently dropped.

Applied identically to the legacy bash CLI (bin/mini-ork-epics) and the
Python port (mini_ork/ported/mini_ork_epics.py) so the parity test stays
green; adds unit tests for marker-skip, dep resolution, and the
unresolved-warning path.

Claude-Session: https://claude.ai/code/session_01MEQ3iCysDCxmTbvYa8PgMj

## Files in scope

- bin/mini-ork-epics
- mini_ork/ported/mini_ork_epics.py

## Definition of Done

Fix the behaviour described above so the repository state at HEAD satisfies the spec.
