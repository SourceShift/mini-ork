# Kickoff: get compose run {{unit_id}} out of the draft region and into writing

## What `{{unit_id}}` is

{{unit_id}} is a `book_generation_runs.id` (a run UUID), NOT a chapter. The job
is parked BEFORE the planning region: FSM `draft`, no planning session, no
planning events, no lifecycle rows — invisible to both the planning-thrash
detector and the chapter loop, which is why this loop pins it explicitly. You
are fixing the CODE that keeps it from advancing, not re-running the job.

## Goal

Make the compose FSM traverse its own golden path for this job:
`draft →next→ plan →start_planning→ plan_sketching →sketch_ready→ plan_ready
→confirm_plan→ generating` (server/compose/fsm/graphDefinition.json). The fix
is in the researcher `server/` tree. Land a minimal, correct change so the
transitions this job needs are APPLIED and ACCEPTED — honestly, through the
graph as defined.

## Evidence (read this first — the signal is mostly ABSENCE)

{{evidence}}

(One-line predicate reason: `{{reason}}`. Full evidence also at
`{{evidence_path}}`.)

## The one thing to understand before you touch anything

**The measured shape (2026-09-22): the UI sits on `?step=plan` while the FSM
sits in `draft` with `state_revision=8`, and `draft_form_data` climbed to
write_revision 25 over two days.** Autosave round-trips work — API, auth, and
DB are all alive — yet the `next` transition (draft→plan) was either never
FIRED by the step navigation or never ACCEPTED by the transition pipeline. No
error_message, no error_class, no queue dispatch was recorded: whatever
refused or dropped the transition did so silently. Your fix must make the
failure either not happen or become loud enough to fix — a silent wedge is the
worst outcome and it is the current one.

After this loop deploys your fix it fires the next golden-path action itself
through `applyComposeFsmActionCore` (the same pipeline the UI buttons use, as
a `system:` actor). If that call is REFUSED, the refusal (status + mapped
error body) lands in the wave report — treat it as the sharpest evidence yet.

## Where to trace (leads to confirm, not a checklist)

- `server/compose/fsm/transition.ts` + `guards.ts` — is a guard refusing
  `next` from `draft` for THIS job's input snapshot, and is the refusal
  swallowed instead of surfaced?
- `server/compose/fsm/inputs.ts` / `service.ts`
  (`COMPOSE_INPUT_EDITABLE_STATES`) — input-snapshot validation that could
  reject the draft payload while autosave still succeeds.
- `server/routes/bookGeneration.ts` — the step/transition routes: does the
  plan step's entry actually fire a transition for jobs created on FSM v8, and
  does `POST /compose/job/:jobId/fsm/transition` map this job's id form
  correctly?
- Client step navigation is OUT of scope (server-side fix only), but if the
  server offers a step endpoint the client never calls for `draft` jobs, the
  server-side auto-advance / recovery path (`recovery.ts`,
  `backfillClassifier.ts`) is the legitimate place to close it.

## The bar is FROZEN — traverse the graph, do not rewire it

`server/compose/fsm/graphDefinition.json` (the legal transitions),
`server/services/bookGeneration/evidenceBank/evidenceRetrievalPolicy.ts` (the
per-topic evidence floor) and `server/compose/operatorExecutors.ts` (its
enforcement) are DECLARED PART OF THE INSTRUMENT that scores you. The deploy
stage refuses to ship any change to them. Adding a shortcut edge
(`draft→generating`) or lowering the evidence floor is not a fix — it is the
loop being made to lie, and it will be refused. Reach the writing region
through the graph; do not move the graph.

## Scope

Edit only files under `server/`. Do NOT touch tests, infra, migrations, or
anything outside `server/`. Keep the diff minimal.

## Success criteria

- The change makes the draft→plan→…→generating traversal succeed for this job
  class (any job created on the same FSM version with a valid draft), not just
  this run.
- Silent refusals on this path become recorded errors (error_class /
  transition audit), so the next wedge of this class is visible.
- Minimal, reviewable diff; `tsc` stays green for the files you touched.

## Model preference

`minimax` / `codex` for the implementer (code lane — never glm).

## Verification (the outer loop runs it, not you)

Your patch is committed on a branch in the worktree and the book-generation
worker is restarted against it. The goal-loop then fires the next golden-path
FSM action and awaits the writing region — `landing_predicate.py` scores a
pass only when the FSM reads `generating` or `completed`. Your job is the
correct, minimal `server/` patch — nothing else.
