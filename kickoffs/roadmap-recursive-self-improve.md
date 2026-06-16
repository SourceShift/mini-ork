# Recursive mini-ork improvement roadmap

This is a data-driven roadmap. Every epic below is sourced from one of:
- iter-34 self-improve synthesis (unshipped patches)
- `gradient_records WHERE task_class='__cross_class__'` (the 120 cross-class
  lessons promoted by today's E7 wiring, confidence ≥ 0.92)
- Today's audit showing 12 learning tables still empty despite schema being
  in place (agent_performance_memory, lessons_bank, recovery_memory,
  user_preference_memory, etc.)

The autonomous scheduler will walk these in dep order, dispatch each via
`epic-runner`, and (if `MO_OPEN_PR=1`) open a PR per epic.

Use `bin/mini-ork epics ingest kickoffs/roadmap-recursive-self-improve.md`
to load them into the scheduler queue.

## HOT-A llm_calls producer wiring (id: hot-a-llm-calls-producer)

iter-34 synthesis Patch #1, conf 0.88. The `llm_calls` table has 0 producers
in code. Provider-policy verifier (`v8_provider_policy_respected`) queries it
and is silently vacuous. Adds `_mo_llm_write_llm_calls_row` to
`lib/llm-dispatch.sh` and updates the verifier to query `llm_calls.actor`/`ts`
instead of nonexistent `llm_dispatch` table.

Foundation epic — observability for everything below depends on it.

## HOT-B Reviewer verdict enum + fail-closed (id: hot-b-reviewer-verdict-enum)

Three cross-class lessons converge here (conf 0.92 each):
- `cross_class:verifier.reviewer` — verdict 'unknown' slipped through as success
- `cross_class:verifier.reviewer_contract` — status=success despite ESCALATE
- `cross_class:verifier.reviewer_verdict` — finish_reason=done without verdict check

Reviewer prompt must emit explicit `{pass|fail|needs_revision}` enum and
fail-closed when missing. Dispatcher must treat anything else as hard
failure. One change ships three fixes.

- depends on: hot-a-llm-calls-producer

## HOT-C Rubric strict aggregation (id: hot-c-rubric-strict)

`cross_class:verifier.rubric` (conf 0.92): rubric returned pass=true with
score=6 despite two FAIL items on critical labels (Success criteria, Quality
floor). Short-circuit pass=false when any critical-label item fails OR when
reviewer_verdict=needs_revision OR when a rollback node fired.

- depends on: hot-b-reviewer-verdict-enum

## HOT-D Implementer patch-applied assertion (id: hot-d-implementer-patch-assert)

`cross_class:workflow.node.implementer` (conf 0.95): `git apply` silently
no-op'd because throwaway copies lacked `.git`, so git resolved the enclosing
repo and copies were never patched. Implementer must use
`git apply --directory=<copy>` (or `patch -p1 -d <copy>`) and immediately
assert the patch landed via grep for a known added symbol.

- depends on: hot-a-llm-calls-producer

## HOT-E framework-edit-shape verifier hardening (id: hot-e-shape-verifier-strict)

`cross_class:verifier.framework-edit-shape` (conf 0.95): passed 21 checks
against a draft with node_count=9 when kickoff bound 8 nodes. Extend the
verifier to (a) read expected node_count from kickoff and assert equality,
(b) grep-deny meta-recipe node names (opus_arbiter, verifier_smith, drafter_*).

- depends on: hot-d-implementer-patch-assert

## HOT-F recipe-validator patch-presence check (id: hot-f-validator-patch-presence)

`cross_class:verifier.recipe-validator` (conf 0.95): checks.tsv said
diff-applies-to-copy=true and tests pass while py_compile/bash -n/pytest
actually ran against baseline HEAD. Validator must prove patch presence
(grep diff-introduced symbol/file) before running checks. Diff-applied
sentinel must contain a content hash of patched files.

- depends on: hot-d-implementer-patch-assert

## HOT-G trace_completeness gate before gradient extraction (id: hot-g-gradient-input-gate)

`cross_class:verifier.trace_completeness` (conf 0.92): traces captured with
status='running' + zero tool_calls + zero files_read + empty
context_bundle_hash + duration_ms=0 reached gradient extraction. Gate
extraction on status ∈ {completed, failed, verified} AND
(non-empty tool_calls OR files_written) at the dispatcher level.

- depends on: hot-a-llm-calls-producer

## HOT-H agent_performance_memory writer (id: hot-h-agent-perf-writer)

Schema-only since migration 0009 — zero writers in code. Add upsert helper
to `lib/memory.sh` (`memory_upsert_agent_perf <role> <model> <task_class>
<outcome> <cost> <duration_ms>`) and call from `bin/mini-ork-execute`
per-node trace_write. Required for lane routing decisions in the planner.

- depends on: hot-a-llm-calls-producer

## HOT-I empty memory namespace writers (id: hot-i-cold-memory-writers)

Wire writers for the remaining cold tables:
- `lessons_bank` — write on resolved learning_record rows
- `recovery_memory` — write when a node retries after failure
- `user_preference_memory` — write when MO_PREFER_* env vars are observed

All schema-ready since migration 0009. Best-effort like D3.

- depends on: hot-h-agent-perf-writer

## HOT-J Auto-merge soak default + per-epic override (id: hot-j-auto-merge-soak-config)

E8 hardcodes 24h soak. Per the SaaSBench (arxiv 2605.17526) precedent,
operational epics need variable soak: docs PR = 1h, infra PR = 72h.
Move `MO_PR_SOAK_HOURS` into `agents.yaml` with per-epic-kind override
(`{epic_kind: docs, soak_hours: 1}`).

- depends on: hot-c-rubric-strict
- depends on: hot-f-validator-patch-presence
