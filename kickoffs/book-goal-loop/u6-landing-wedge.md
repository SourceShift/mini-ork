# U6 landing RSI: land one pinned compose book, draft → written, full-auto

## Goal

- Fix all researcher bugs standing between compose job
  `job_1790005402603_0bd1f22b` (the "Better Harness" book,
  `?job=…&step=plan`) and a LANDED book: every chapter written, rubric-pass
  quality, at the plan-declared minimum chapter length.
- Two phases, one launcher (`launch-landing-armed.sh`):
  - **Phase A (new binding)** drives the compose FSM from `draft` into the
    writing region (`generating`/`completed`) — fix children repair the
    `server/` code that drops transitions silently; after each live deploy the
    loop itself fires the next golden-path FSM action through the product's
    sanctioned pipeline.
  - **Phase B (reused verbatim)** chains the verified chapter loop
    (`launch-book-armed.sh`) over the freshly-minted generated-book uuid.
    Quality + min-length live THERE: `chapter_quality.py` enforces
    `max(plan-declared floor, env floor)` per chapter.
- No new recipe, no drive.py change. Same loop machinery as U4/U5; only the
  binding surfaces differ.

## The wedge, and why BOTH existing loops are blind to it

Measured 2026-09-22 (run uuid `4a9194b1-c15a-4fbc-bc99-b2520d9d1bc2`):

- FSM `draft`, state_revision 8, last move 2026-09-21 15:44. UI parked on
  `?step=plan`. `draft_form_data` autosave climbed to write_revision 26 across
  two days — API/auth/DB all alive.
- ZERO planning session, ZERO planning events, ZERO lifecycle rows, no
  error_class/error_message, no queue dispatch. The `next` transition
  (draft→plan) was either never fired or refused SILENTLY.
- The planning loop (U5) detects thrash from `compose_planning_events` — no
  events, no signal. The chapter loop (U4) lists `book_chapter_lifecycle`
  rows — no plan, no rows. A pre-planning wedge produces only ABSENCE, so no
  statistical detector can see it. **The unit is therefore PINNED by the
  operator** (`MO_GOAL_LANDING_RUN_UUID`), and the lister is non-vacuous by
  construction (exits 2 when the DB is unreadable, so empty = landed, never
  blind).

## Binding surfaces (all in `binding/`)

| Surface | File | Contract |
|---|---|---|
| units | `landing_units.py` | emits the pinned run uuid while FSM is pre-writing; empty once `generating`/`completed`/`cancelled`; withholds a fresh `plan_sketching` burst (stall window 900s); exit 2 on DB failure |
| predicate | `landing_predicate.py` | PASS = FSM ∈ {generating, completed}. Destination, not departure — closes `job_predicate.py`'s vacuous-pass hole for draft jobs. `cancelled` is NOT a pass |
| evidence | `landing_evidence.py` | states each ABSENCE explicitly (session/events/dispatch), plus the autosave revision trail and the identity-trap probe |
| re-dispatch | `advance_fsm.py` | one golden-path action per wave via `applyComposeFsmActionCore` (actor `system:goal-loop-landing`): draft→next, plan→start_planning, plan_ready→confirm_plan, failed→retry…; shim at `<wt>/.cache/goal-loop-advance.ts` (plain .ts CJS — an .mts shim cannot see the CJS tree's named exports; NOT node_modules/, which is a symlink to the primary and would load UNFIXED code) |
| terminal-fail | `job_terminal_fail.py` | reused from U5 |
| deploy | `deploy_branch_local.py` | reused: branch `rsi/landing-1790005402603`, local commit, worker restart; nothing pushed |
| kickoffs | `landing-wave-kickoff.md`, `landing-child-kickoff.md` | wave contract + child briefing |

## AUTO-CONFIRM is deliberate

`plan_ready → confirm_plan` is a human review gate in the product. This loop's
charter is full-auto landing, so the gate is driven through — same posture as
the TRUE-RSI approval-gate removal. The quality backstop is Phase B's chapter
loop plus the frozen bar, not the human.

## The frozen bar (MO_GOAL_PROTECTED_PATHS)

- `server/compose/fsm/graphDefinition.json` — the legal transitions. A
  draft→generating shortcut edge is the pass being laundered, not earned.
- `server/services/bookGeneration/evidenceBank/evidenceRetrievalPolicy.ts` —
  the per-topic evidence floor planning must clear.
- `server/compose/operatorExecutors.ts` — where the floor is enforced.

`transition.ts`/`guards.ts`/`inputs.ts`/routes are deliberately fixable — the
bug lives in there.

## Phase B chain

Gated on the LIVE predicate (not the driver's rc). The generated-book uuid does
not exist at launch; it is resolved at chain time from the canonical plan:
`book_run_artifacts.artifact_payload->>'bookUUID'` (verified against the RSI
book: resolves `c12de1d9…`). `run.book_id` is a different uuid — using it
returns zero lifecycle rows and lies.

## Go / no-go

Dry (default, plan-only):

    bash kickoffs/book-goal-loop/launch-landing-armed.sh

Live (commit + worker restart + FSM actions + Phase B chain):

    MO_GOAL_APPLY_DRY=0 bash kickoffs/book-goal-loop/launch-landing-armed.sh

Preconditions: `PGPASSWORD` exported; worktree
`worktrees/goalloop-landing` exists (created 2026-09-22 via
`make codex-worktree SLUG=goalloop-landing`, branch `codex/goalloop-landing`,
node_modules symlinked to the primary, `server/.env` copied). All four binding
probes smoked green 2026-09-22 against the live DB.
