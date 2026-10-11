# Kickoff: un-wedge the researcher DSP planning burst for run {{unit_id}}

## What `{{unit_id}}` is

{{unit_id}} is a `book_generation_runs.id` (a run UUID), NOT a chapter. This is
the PLANNING stage: the job is parked before `plan_ready`, so it has no chapters
yet — that is why the chapter loop never sees it. Its FSM cycles
`plan → plan_sketching → failed → plan` and never leaves. You are fixing the
CODE that produces that cycle, not re-running the job.

## Goal

Make planning reach `plan_ready` for a job whose per-topic evidence floor is
reachable in principle. The fix is in the researcher `server/` tree. Land a
minimal, correct change to how evidence queries are CONSTRUCTED, so the burst
can satisfy the floor it is measured against.

## Evidence (read this first — it is the signal, not a one-line summary)

The goal-loop's wedge detector measured this from the product's own ledgers and
wrote the burst's real history here: the repeated failure reasons verbatim, the
live burst snapshot (the FRESH heartbeat), the event histogram, and the cap /
floor the burst is measured against.

{{evidence}}

(One-line predicate reason: `{{reason}}`. Full evidence also at
`{{evidence_path}}`.)

## The one thing to understand before you touch anything

**Retrying this job cannot work, by construction — so a fix that merely makes
the retry cheaper, louder, or more persistent is worthless.** Burst work is
keyed by a stable hash: a re-plan that derives the SAME queries returns the
MEMOIZED result instead of re-running it. That is why the failure reason is
byte-identical across attempts and why the heartbeat stays fresh the whole time
(measured: 8808 heartbeats over 2 days with no progress). The product's own
detectors are blind here for the same reason — `stuckJobHealthChecker` only
flags `active`/`burst_running`/absent, and this burst IS `burst_running`, so it
looks healthy; Step-11 auto-resume needs `FSM=failed` and the job spends half
its cycle in `plan_sketching`.

So the ONLY fix that works is one that changes **which queries get derived**.
Read the evidence for what the burst actually asked for and what it got.

## Where to trace (a lead to confirm, not a checklist)

`server/compose/planningSessionService.ts` — `buildTopicScopedEvidenceQueries`
and the `englishTarget` resolution. The suspected mechanism: a NON-LATIN topic
label fails an anchored Latin-script test, so `englishTarget` resolves to `''`,
and every topic silently receives the SAME global seed list — a list far too
short to reach the per-topic floor. Confirm this in source against the evidence
before you change it; if the real cause is elsewhere, follow the evidence.

## The bar is FROZEN — fix the query construction, do not lower the floor

`server/services/bookGeneration/evidenceBank/evidenceRetrievalPolicy.ts` (the
floor `DSP_EVIDENCE_MIN_SOURCES_PER_TOPIC`, the query caps) and
`server/compose/operatorExecutors.ts` (where the floor is enforced and
`EvidenceCoverageShortfallError` is thrown) are DECLARED PART OF THE INSTRUMENT
that scores you. The deploy stage refuses to ship any change to them. A patch
that makes the floor easier to reach by moving the floor is not a fix — it is
the loop being made to lie, and it will be refused. Reach the bar; do not move
it.

## Scope

Edit only files under `server/`. Do NOT touch tests, infra, migrations, or
anything outside `server/`. `server/compose/planningSessionService.ts` is
deliberately IN scope — the bug lives there. Keep the diff minimal.

## Success criteria

- The change alters what queries the planning burst derives (or corrects the
  resolution that collapses per-topic scoping), not how loudly it fails.
- It is a class-level fix — any job hitting the same non-Latin-label path is
  repaired, not just this one run.
- Minimal, reviewable diff; `tsc` stays green for the files you touched.

## Model preference

`minimax` / `codex` for the implementer (code lane — never glm).

## Verification (the outer loop runs it, not you)

Your patch is committed on a branch in the worktree and the book-generation
worker is restarted against it. The goal-loop then re-dispatches this run and
awaits its FSM leaving the planning region — `job_predicate.py` scores a pass
only when the job is out of `plan`/`plan_sketching`/`failed` in a direction
toward work. Your job is the correct, minimal `server/` patch — nothing else.
