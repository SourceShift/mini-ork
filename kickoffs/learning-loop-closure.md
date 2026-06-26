# Kickoff: Close the learning loop — runtime policy router (auto-1-policy-router)

## Goal

mini-ork's five learning loops **write** `prompt_win_rates`,
`agent_performance_memory.relative_advantage`, `execution_traces.process_reward`,
`grounded_rejections`, and `self_improve` `learning_record` — but the dispatcher
still selects prompt variant and lane from **static config**. The learning is
write-only: invisible to the next run. This epic closes the read side so the
dispatcher acts on what it learned. It is the keystone — until it closes, every
other learning improvement is write-only.

This is the read side of `d1-enable-learning-pipeline` (which fills the tables).
Keep the router **pure-read** so it can never corrupt the learning substrate.

## Scope (in scope)

1. **`lib/policy_router.sh`** — a read-only policy reader consulted by the
   dispatcher *before* it dispatches a node. Three readers:
   - `policy_route_prompt(task_class, node_type, agent_role)` → winning
     `prompt_version_hash` from `prompt_win_rates` when `sample_size >= MIN_SAMPLE`
     (default 8) and `win_rate` clears a margin; else the configured default.
   - `policy_route_lane(task_class, node_type)` → ranks lenses by
     `relative_advantage` (GRPO) and returns the positive-advantage leader, with
     a configurable ε-exploration floor so under-sampled lanes still get traffic.
   - `policy_node_guard(trace_id)` → consults `process_reward`; a node whose
     predicted process reward is below a floor is flagged for an extra reviewer.
   - **Vacuous policy = configured default.** Empty/cold tables must degrade to
     the configured default and never hard-fail.
2. **Audit trail.** Every routing decision emits a structured row recording
   `(decision_point, chosen, alternatives, reason, source_table, sample_size)`.
   - **DO NOT overload the existing `policy_decisions` table** — that is a
     governance/safety table (`event_type`, `policy_name`,
     `result IN ALLOW/DENY/REQUIRE_APPROVAL/LOG_ONLY`). Reusing it would corrupt
     the safety audit. Instead: prefer extending the existing `conductor_decisions`
     table if it fits, else add a **new** migration
     `db/migrations/00NN_routing_decisions.sql` for a dedicated `routing_decisions`
     table. Pick the lowest unused migration number; make it idempotent
     (`CREATE TABLE IF NOT EXISTS`, `CREATE INDEX IF NOT EXISTS`).
3. **Dispatcher wiring.** Wire `_mo_policy_route_lane` (bin/mini-ork-execute:960)
   so a new `learning_governed` policy case consults `lib/policy_router.sh`. A
   real run under that policy must record >=1 routing-decision row.
4. **Config, not hardcode.** `MIN_SAMPLE`, the win-rate margin, the ε-exploration
   floor, and the process-reward floor are all config-driven.
5. **Conductor write-back (close the prediction loop).** After an epic resolves,
   write `realized_score` back into `conductor_decisions` and set `outcome` so the
   9 stuck `outcome=pending`/`predicted_score=0.6` rows get reconciled and the
   next self-improve iter can grade the router.

## Out of scope

- No generative conductor, no GRPO-in-prompt, no model training (later epics).
- No external-product integration (that is handled by a separate adapter recipe).
- Do not modify the governance `policy_decisions` schema or semantics.

## Definition of Done

- `lib/policy_router.sh` with the three readers; pure-read (no writes to learning
  tables); degrades to configured defaults when tables are empty/cold.
- One idempotent migration for the routing audit trail (or a justified reuse of
  `conductor_decisions`), applied cleanly via `db/init.sh`.
- Dispatcher call site wired so a real `learning_governed` run records >=1
  routing-decision row.
- Exploration floor + min-sample thresholds are config, not hardcoded.
- Conductor `realized_score` write-back wired after epic resolution.

## Success Criteria (proof)

- `shellcheck` clean; `bash -n` clean on all touched scripts.
- **Unit test `tests/unit/test_policy_router.sh`:** seed `prompt_win_rates` +
  `agent_performance_memory` with a known winner → assert the router returns it;
  seed empty tables → assert it returns the configured default (cold-start safe).
- **Integration test:** a dispatch with seeded policy tables records a
  routing-decision row whose `chosen` matches the seeded winner.
- **Smoke harness `scripts/smoke-learning-loops.sh`:** on synthetic fixtures,
  assert each phase shapes — RHO win-rate increments, PRM `process_reward`
  non-null, a GRPO `relative_advantage` row exists, `gradient_records` grows, a
  `grounded_rejections` row is written, the read seam picks the seeded winner
  (not the static default), and `conductor_decisions.realized_score` is written
  back. The harness is the end-to-end proof the loop is closed.
- **Medium-tier validation** on the routing-policy design (panel + Krippendorff-α
  + citation_verifier_mechanical + refute-or-promote): confirm "vacuous policy =
  configured default" cannot silently degrade a cold system.

## Model Preference

Router design/judgment node → `opus` (strongest reasoner for the cold-start /
safety judgment). Implementer inherits the task-class default.

## Proof command

```sh
shellcheck lib/policy_router.sh && bash -n lib/policy_router.sh \
  && bash tests/unit/test_policy_router.sh \
  && bash scripts/smoke-learning-loops.sh
```
