# Kickoff: cut the cost + wall-clock of the chapter-write pipeline for chapter {{unit_id}}

## Goal

Book chapter **{{unit_id}}** is measured by the PERFORMANCE goal-loop as costing
too much or taking too long to generate. Find the dominant `verified-artifact`
node in the evidence and land a minimal, correct fix in the researcher
`server/` code that reduces its measured token cost and wall-clock WITHOUT
losing quality (`committed_complete=true AND rubric_status=pass`). You are
fixing the CODE, not regenerating the chapter.

## Evidence (read this first — it is the signal, not a one-line summary)

The goal-loop harvested the per-node cost + duration picture for THIS chapter:
which `verified-artifact` node ran how many times, at what `cost_usd` and
duration, the node with the largest share, generation attempts, recovery
cycles, and a re-roll count:

{{evidence}}

(One-line predicate reason, for reference: `{{reason}}`. The full evidence is
also written to `{{evidence_path}}`.)

## How to think about this

**Re-wording prompts is not a cost fix.** A cheaper prompt that still re-rolls
the same node costs MORE, not less: every re-roll is a full `verified-artifact`
dispatch — the dominant line item. The evidence names the dominant node; trace
WHY that node re-rolls and eliminate the re-roll (or the redundant dispatch
that feeds it). A node that runs once at its current cost is usually cheaper
than one that runs three times on a re-worded prompt.

Target the measured axes the predicate checks:
- COST — `sum(cost_usd)` of `verified-artifact` `task_runs` since the current
  generation epoch.
- SPEED — wall-clock of the chapter's most recent attempt.

The re-roll count in the evidence is the lever: re-rolling a node is the cost.
Cut the re-rolls and both axes fall together.

Prefer a **class-level** fix — one that repairs every node sharing the re-roll
mechanism — over a single-node patch, when the evidence shows the same
mechanism can hit sibling nodes.

## Scope

Edit only files under `server/`. Do NOT touch tests, infra, migrations, or
anything outside `server/`. Keep the diff minimal and targeted at the dominant
node's cost/time.

## Success criteria

- The fix reduces the dominant node's re-rolls / redundant dispatches — the
  measured cost and wall-clock, not a cosmetic prompt tweak the lane ignores.
- Quality is preserved: the chapter must still reach `committed_complete=true`
  AND `rubric_status=pass`.
- Minimal, reviewable diff — no drive-by refactors.
- `tsc` stays green for the files you touched.

## Model preference

`minimax` / `codex` for the implementer (code lane — never glm).

## Verification (the outer loop runs it, not you)

After your patch lands on researcher `main` and the book-generation worker
restarts, chapter {{unit_id}} regenerates and the performance predicate
(`binding/unit_predicate.py`) re-measures its cost and wall-clock against the
budget file. Your job is the correct, minimal `server/` patch — nothing else.
