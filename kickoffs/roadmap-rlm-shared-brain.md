# mini-ork shared-brain RLM refactor roadmap

Goal: let one mini-ork brain serve multiple consumers (the dev-time
engineering-coding loop AND the book-gen RLM serving path) without duplicating
the learning loop. The brain (decisions + offline learning) is shared once; the
*mechanics* (spawn / deliver / page / schedule) stay native per consumer.

Architecture decision (locked): share the **decision + learning layer**, not the
bash stage-binaries in the request path. The seam follows the substrate fault
line — mechanics can't cross bash→Daytona, decisions can. The compounding
learning loop is the moat and is inherently offline, so it never enters the 30s
request path.

## Dispatch model (read before running the scheduler)

- Epics tagged **[framework-edit]** change mini-ork's OWN behavior. They dispatch
  via `mini-ork run framework-edit <kickoff>` — PROPOSE-NOT-COMMIT: verify in a
  worktree (this is the live smoke test), emit `framework-edit.diff`, roll back,
  exit 1 (EXPECTED). A human reviews + applies each diff. Do NOT auto-merge
  learning-loop changes to `main` via the scheduler.
- Epics tagged **[researcher-repo]** edit the PRIVATE book-gen serving code and
  are NOT dispatched by this queue (IP separation: book-gen specifics never go
  to public mini-ork OSS). They are listed here for the full dependency picture
  and dispatch against the researcher repo separately.
- Epics tagged **[doc]** are documentation-only.

Load with `bin/mini-ork epics ingest kickoffs/roadmap-rlm-shared-brain.md` then
`bin/mini-ork epics split kickoffs/roadmap-rlm-shared-brain.md`. Walk them with
`bin/mini-ork epics ready` and dispatch each ready epic one at a time as a
framework-edit, applying the diff before the next.

---

## Trace contract: objective_domain + structured normalized reward (id: rlm-1-trace-contract)

[framework-edit] Foundation. Add the objective-aware, normalized reward contract
to the trace/state schema so every decision→outcome row carries enough to train
a multi-consumer policy without one consumer's signal dominating.

Schema migration adds to the execution-trace row in `db/` (new migration under
`db/migrations/`) and the writer helper in `lib/trace_store.sh`:
- `objective_domain` TEXT NOT NULL (mandatory partition, e.g. `code-delivery` |
  `book-gen`) — nothing is ever pooled across this.
- `segment` TEXT (sub-key: recipe/repo for code, genre/audience for book-gen).
- structured reward: `reward_primary_metric` TEXT, `reward_direction` INT
  (+1/-1), `reward_anchor` REAL (baseline/SOTA), `reward_g` REAL (the
  direction-normalized scale-free gap `dir*(value-anchor)/|anchor|`),
  `reward_vector_json` TEXT (multi-dimensional, e.g. factuality/coherence/style),
  `reward_source` TEXT (pluggable provider + version, e.g.
  `BOOK_GEN_AUTORUBRIC@v3` | `verifier@v1`), and `validity` TEXT (post-hoc judge
  verdict: accept|flagged).

Writer helper `lib/trace_store.sh` gains a function that accepts the structured
reward object and computes `reward_g` from value+anchor+direction. Back-compat:
existing single-scalar callers default `objective_domain='code-delivery'`,
`reward_source='verifier@v1'`.

Done when (smoke):
- `bash -n lib/trace_store.sh` passes.
- a migration smoke proves the new columns exist on a fresh `.mini-ork/state.db`
  (`./bin/mini-ork init` in a temp home, then assert columns via sqlite).
- `./bin/mini-ork doctor` still exits 0.

## lane_router objective_domain grouping on normalized g (id: rlm-2-lane-router-grouping)

[framework-edit] The keystone. Change `lib/lane_router.sh` relative-advantage
grouping key from `(run_id, task_class, node_type)` to
`(objective_domain, task_class, node_type)` and compute `relative_advantage` over
the normalized `reward_g`, never over a raw cross-objective score. This is what
makes book-gen DRIVE its own policy slice instead of riding the eng-team's
majority signal. Keep the existing `sample_size >= 3` noise floor — thin slices
correctly fall back to config defaults (isolation, not contamination).

Touches `lib/lane_router.sh` and any caller passing the group key.

- depends on: rlm-1-trace-contract

Done when (smoke):
- `bash -n lib/lane_router.sh` passes.
- `MO_VALIDATE_DO_RUNS=1 MO_LEARNING_MIN_SAMPLES=1 scripts/learning-loop-live-validate.sh 1`
  shows the router flipping on a seeded objective slice.
- `bash scripts/learning-loop-closure-gate.sh` exits 0.

## Store-port abstraction for the brain libs (id: rlm-3-store-port)

[framework-edit] Abstract the brain libs' DB access behind a store interface so
the SAME learning-loop implementation targets SQLite (dev / eng-team) and
Postgres (serving / book-gen, per-tenant). Introduce `lib/policy_store.sh` as the
seam over `lib/db_open.sh`; route `lib/lane_router.sh`, `lib/process_reward.sh`,
and the reflect→`gradient_records` write through it. Default backend = SQLite
(no behavior change for eng-team). Postgres backend is a stub interface here;
the real PG impl lands researcher-side.

Touches `lib/policy_store.sh` (new), `lib/lane_router.sh`, `lib/process_reward.sh`.

- depends on: rlm-1-trace-contract

Done when (smoke):
- `bash -n lib/policy_store.sh` passes.
- existing learning-loop closure gate still exits 0 on the SQLite default backend
  (`bash scripts/learning-loop-closure-gate.sh`).

## Stateless decision service (read-path) (id: rlm-4-decision-service)

[framework-edit] The one inference-time surface both consumers call.
`decide(node, candidates, ctx)` returns `{route, panel, coalition_ok, reward,
recursion_hint}` by reading the current policy slice (keyed by
objective_domain/task_class/segment/node_type) via the store-port, wrapping the
existing `lib/lane_router.sh` + `lib/coalition_gate.sh` + `lib/process_reward.sh`.
Holds NO per-request state. Below the `sample_size >= 3` floor it returns the
config-default lane (cold-start safe).

Touches `lib/decision_service.sh` (new). The eng-team execute path
(`bin/mini-ork-execute`) reads routing through it to prove it in the trusted
path.

- depends on: rlm-2-lane-router-grouping
- depends on: rlm-3-store-port

Done when (smoke):
- `bash -n lib/decision_service.sh` passes.
- a unit smoke shows `decide()` returns the config lane when the slice has
  `sample_size < 3`, and the learned lane when `>= 3`.
- `bash scripts/learning-loop-closure-gate.sh` exits 0.

## Per-request --deadline budget (id: rlm-5-deadline-budget)

[framework-edit] Add a wall-clock `--deadline <seconds>` to the run loop budget
(alongside the existing `$`-cap in `lib/cost_pause.sh`). The loop checks
remaining budget BETWEEN stages and returns best-so-far rather than running till
done — the serving path's "you have 30s" contract. Mirrors NatureBench's
`/time_remaining` + pause-clock-during-scoring pattern.

Touches `lib/cost_pause.sh` (or a sibling `lib/deadline_budget.sh`) and the loop
driver in `bin/mini-ork`.

- depends on: rlm-1-trace-contract

Done when (smoke):
- `bash -n bin/mini-ork` passes.
- a smoke run with `--deadline 1` exits cleanly with a best-so-far artifact and a
  `deadline_hit` trace marker, not a hang.

## Paged-context seam (id: rlm-6-context-paging)

[framework-edit] Replace the hard 64K truncate in `lib/context_assembler.sh`
with an on-demand slice-provider interface. Default provider = current truncate
behavior (no change for eng-team); a paged provider can serve a "next slice" of a
large manuscript. This is the seam the book-gen Body binds `groundChapterQuery`
into.

Touches `lib/context_assembler.sh`.

- depends on: rlm-1-trace-contract

Done when (smoke):
- `bash -n lib/context_assembler.sh` passes.
- a smoke shows the default provider reproduces the existing 64K-bounded output
  byte-for-byte on a fixture (no regression for the eng-team consumer).

## Integration validation: shared brain end-to-end (id: rlm-7-integration-validate)

[framework-edit] Cross-cutting calibration gate (NatureBench reproduce-mode
idea): prove the harness, not just the agents. Seed two objective slices
(`code-delivery`, `book-gen`) with known traces, run the live learning-loop
validation, and assert the two slices learn INDEPENDENT policies (book-gen slice
unaffected by abundant code-delivery traces). Wire it as a script
`scripts/rlm-shared-brain-smoke.sh`.

Touches `scripts/rlm-shared-brain-smoke.sh` (new).

- depends on: rlm-4-decision-service
- depends on: rlm-5-deadline-budget
- depends on: rlm-6-context-paging

Done when (smoke):
- `bash scripts/rlm-shared-brain-smoke.sh` exits 0: two seeded objectives, two
  independent learned policies, decision service returns the right slice per
  `objective_domain`, closure gate green.

## [researcher-repo] Book-gen reward_source plugin (id: rlm-8-bookgen-reward-source)

[researcher-repo] NOT dispatched by this queue. Implements the `reward_source`
interface for book-gen: chapter-audit pass-rate + `BOOK_GEN_AUTORUBRIC` →
structured normalized reward vector (factuality/coherence/style; primary =
audit_pass_rate). This is the mechanism that makes book-gen drive the loop.

- depends on: rlm-1-trace-contract

## [researcher-repo] Offline learning loop on book-gen traces (Layer 1) (id: rlm-9-offline-loop-bookgen)

[researcher-repo] NOT dispatched by this queue. Point mini-ork
`recursive-self-improve` at book-gen production audit traces in the shared store;
reflect→gradients→policy keyed by genre/audience, written back via the store-port
(Postgres). Closes the compounding loop with ZERO request-path change — book-gen
starts getting smarter before the Body adapter exists. Back-fills the book-gen
policy slice from historical audit logs so it crosses the sample floor on its own
data.

- depends on: rlm-4-decision-service
- depends on: rlm-8-bookgen-reward-source

## [researcher-repo] Native Daytona Body adapter (Layer 2) (id: rlm-10-daytona-body)

[researcher-repo] NOT dispatched by this queue. The actual RLM body: paged
`groundChapterQuery` context, bounded per-section sub-agents (depth 2-3, native
Daytona sub-sandboxes orchestrated by Hatchet), hard per-request deadline,
per-tenant isolation, artifact+SSE streaming. Calls the shared decision service
for route/panel/recursion decisions and emits production traces back to the
store. Brain (shared, learned) + Body (native) = self-improving internal RLM.

- depends on: rlm-5-deadline-budget
- depends on: rlm-6-context-paging
- depends on: rlm-9-offline-loop-bookgen

## [doc] Strategy doc update — Brain/Body + decision-service framing (id: rlm-11-doc-update)

[doc] Update `docs/book_gen/research/20260625-miniork-as-rlm-strategy.md` in the
researcher repo: replace the Layer-2 "reimplement the 3 PORT capabilities"
framing with this decision-service + offline-policy boundary; adopt Brain/Body
vocabulary; state explicitly that step-4 (the native Body) is still required and
that the reward signal must let book-gen drive, not ride. Owned by this agent; no
parallel edits.

- should follow: rlm-1-trace-contract
