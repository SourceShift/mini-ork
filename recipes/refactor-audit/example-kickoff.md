# Audit ~/ps/mini-ork for scalability bottlenecks

## Problem

mini-ork v0.1.1 runs cleanly at 1K task_runs/day on a dev box. We need
to know what breaks first at 100K/day, what breaks first at 10M/day,
and what the architectural path is to scale through both.

## Definition of Done

The recipe produces:

1. Five lens reports under `${MINI_ORK_RUN_DIR}/lens-*.md` covering:
   - GLM tactical bottlenecks (15-30 findings with file:line anchors)
   - Kimi code-level refactors (8-15 with before/after diffs)
   - Codex LLM-dispatch cost cuts (10-15 with savings estimates)
   - Opus architectural shape (1500-2500 words, 7 sections,
     numbered recommendations)
   - MiniMax cross-system integration and data-flow tracing
2. An anonymous panel bundle at `${MINI_ORK_RUN_DIR}/panel-responses.md`.
   The synthesizer receives this bundle as `Response A` through `Response E`;
   the original lane-to-label map remains system-only.
3. A synthesis at `${MINI_ORK_RUN_DIR}/synthesis.md` ranking findings
   by severity × leverage / effort, with consensus markers for findings
   that appear in 2+ anonymous responses.
4. The synthesis publishes to `docs/refactor/SCALABILITY-AUDIT.md` for
   commit.

## Scope

- Target dir: `~/ps/mini-ork/` (~145 files, 13 sqlite migrations)
- Dimensions: scalability, performance, cost; security is handled by
  a separate audit (`docs/SECURITY-AUDIT.md` already shipped)
- Depth: 5 parallel lenses + anonymization + 1 synthesis = ~30-60 min wall-clock
- Budget: $20-40 (per the task_class cost model)
- Output: read-only audit; no code changes by default. A follow-up
  `code-fix` recipe run may apply specific findings.

## Success Criteria

- All 5 lens reports exist + non-empty + cite ≥1 file:line each
- Anonymous panel has Response A through Response E and synthesis
  cross-references all 5 responses with consensus markers
- `verifiers/lens-completeness.sh` exits with pass=true
- Total cost ≤ `MO_REFACTOR_AUDIT_BUDGET_USD` (default $40)

## Non-goals

- Do NOT modify any source file under `bin/`, `lib/`, `db/`, `recipes/`
  during the audit (read-only analysis only)
- Do NOT generate code diffs that need testing — code-level refactor
  proposals from Kimi lens are illustrative; applying them is a
  separate code-fix run
- Do NOT audit dependencies (sqlite3, jq, claude CLI) — those are
  out-of-scope per the framework's threat model

## Lineage

This kickoff was used to produce the v0.1.1 baseline scalability audit
at `docs/refactor/SCALABILITY-AUDIT.md`. Re-running it after each major
release captures the framework's evolution under its own improvement
loop.
