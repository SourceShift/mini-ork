# Phase 0: close the learning loop + make conductor_decisions a trainable dataset

Goal: turn mini-ork's learning system from open-loop (writes stats, never acts on
them, schema drifted) into a closed loop where every epic outcome produces one
normalized `(state, action, reward)` row in `conductor_decisions`. This is both the
fix for the audited open-loop learning bug AND Phase 0 of the RL-Conductor training
readiness plan (`.mini-ork/research-notes/conductor-training-readiness.md`): the loop
closure IS the training-data pipeline.

## Background (verified this session)
- Migration runner `db/init.sh` aborts on the first failing migration (`exit 1`).
  `0020_idea_tree.sql` uses bare `CREATE TABLE` (no `IF NOT EXISTS`) and dies on DBs
  where `idea_tree_nodes` was lazy-created earlier — blocking every migration after
  it (0022–0038 never apply).
- Consequences: `schema_migrations` jumps 0021→0027 (0022–0026 unapplied), so
  `policy_decisions`/`policy_state` (from 0026) don't exist; `0031` is marked applied
  but `execution_traces.process_reward` column is absent; `agent_performance_memory`
  exists but has 0 rows; `grounded_rejections` has no migration anywhere.
- `conductor_decisions` already has the right shape (task_class/budget = state,
  chosen_topology/recipe/lane_hints = action, predicted_score = value,
  realized_score = reward) but `realized_score`/`outcome` are never written back —
  all 9 rows are `pending`.

## Success criteria (what the verifier checks)
1. **Runner unblocked**: `db/init.sh` runs to completion on a copy of the live DB
   with zero parse errors. Drifted migrations (`0020`, and any other bare DDL in the
   0020–0038 range) are made idempotent (`CREATE TABLE/INDEX IF NOT EXISTS`).
2. **Schema repaired**: after running init on the copy, all of these exist —
   `execution_traces.process_reward` column, `policy_decisions` table, `policy_state`
   table, `agent_performance_memory` table, and a new `grounded_rejections` table
   (add a migration for it). Idempotent: re-running init makes no further changes.
3. **Loop closed**: a new function writes `realized_score` (REAL in [0,1]) and
   `outcome` ('success'/'failure') into `conductor_decisions` when an epic resolves.
   Add a normalizer that maps the verified-reward stack (verifier pass + panel
   agreement + cost penalty) to the [0,1] scalar. Unit-tested with a fixture epic.
4. **GRPO signal populated**: a writer fills `agent_performance_memory.relative_advantage`
   using `(r - mean)/std` over grouped attempts at the same (node_type, task_class),
   matching arXiv:2512.04388 eq. 2. At least one non-empty row after the writer runs
   on seeded traces.
5. **All-phases smoke harness**: `scripts/smoke-learning-loops.sh` exists and, against
   a seeded temp DB, asserts each loop shapes: (a) RHO `prompt_win_rates` increments,
   (b) PRM `execution_traces.process_reward` becomes non-null, (c) GRPO
   `agent_performance_memory` gains a row, (d) `gradient_records` grows, (e)
   `grounded_rejections` gains a row on a refuted draft, (f) a `learning_governed`
   routing decision differs from static when win-rates are seeded, (g)
   `conductor_decisions.realized_score` is written back. The harness exits non-zero if
   any phase fails ("vacuous is not success": zero assertions run must fail).

## Scope (explicitly in/out)
- IN: `db/migrations/*.sql` idempotency fixes, one new `grounded_rejections`
  migration, one `process_reward` repair migration, the realized_score writeback +
  reward normalizer, the relative_advantage writer, `scripts/smoke-learning-loops.sh`.
- IN: a `learning_governed` policy branch in `_mo_policy_route_lane`
  (`bin/mini-ork-execute`) that reads `prompt_win_rates`/`relative_advantage` and
  falls back to static when sample_size is low.
- OUT: no GPU training, no trained model, no WorkflowSpec unification (that is
  Phase 2). No libwit source edits in this epic.
- DB SAFETY: never mutate the live `.mini-ork/state.db` directly in the implementer
  nodes; all schema work is validated on a COPY. A backup already exists at
  `.mini-ork/state.db.bak-20260623-phase0`.

## Proof command (what proves success)
```
cp .mini-ork/state.db /tmp/p0-proof.db && \
  MINI_ORK_DB=/tmp/p0-proof.db bash db/init.sh >/tmp/p0-init.log 2>&1 && \
  ! grep -qiE 'parse error|Error:' /tmp/p0-init.log && \
  sqlite3 /tmp/p0-proof.db "SELECT COUNT(*) FROM pragma_table_info('execution_traces') WHERE name='process_reward';" | grep -q '^1$' && \
  sqlite3 /tmp/p0-proof.db "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name IN ('policy_decisions','policy_state','grounded_rejections');" | grep -q '^3$' && \
  test -x scripts/smoke-learning-loops.sh && \
  bash scripts/smoke-learning-loops.sh
```

## Validation bar (epic-sized → medium validation)
Panel review (cross-family lenses) + Krippendorff-α on the smoke-harness design +
citation_verifier_mechanical on the schema claims + refute-or-promote on "the loop is
actually closed". Single-lens self-review is insufficient.
