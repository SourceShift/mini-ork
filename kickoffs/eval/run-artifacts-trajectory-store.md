# Turn-jsonl trajectory store: persist every turn + link its path in the DB

## Problem
mini-ork already writes per-node turn logs to the run dir — `agent-<node>.stream.jsonl` (streamed
turn events) and `agent-<node>.transcript.json` (final transcript), via
`_mo_llm_persist_agent_transcript` (`lib/llm-dispatch.sh:500`), and the Python dispatch already
handles `.stream.jsonl` paths (`mini_ork/dispatch/providers.py:403`). BUT:

1. **The DB does not store a path to these files.** `llm_calls` has no `transcript_path`/`stream_path`
   column; `execution_traces` only has `files_read/files_written/final_artifact_ref`. So the raw
   turns are orphaned from the DB — you cannot query "give me the trajectory for this run/call"
   without reverse-engineering the path from run_id + node naming.
2. **Coverage is inconsistent.** The implementer node emits `.stream.jsonl`; the reviewer node only
   got `.transcript.json`. Not every dispatched turn writes the streamed jsonl, so trajectories are
   partial.
3. **No retention policy.** At ~677k input tokens/run × ~255 runs/month, raw jsonl is a few MB/run
   (~1GB/month) and the run dir grows unbounded.

This layer is the precondition for the evidence bundle, trajectory eval/judge scoring, replay, and
the insight distiller (see `internal-docs/research/2026-07-03-adding-eval-to-miniork-run-flow.md`).

## Objective
Every dispatched turn writes a `.stream.jsonl` capturing the real turn events, and each is registered
as a queryable DB row keyed by run_id + node, with a RELATIVE path so it survives vendoring /
foreign-home / cloud exec. Migration-aware: the schema + conventions are language-agnostic and land
now; the canonical writer lives in the Python dispatch (`mini_ork/dispatch/telemetry.py`) where the
migration is heading, with a minimal bash mirror only for nodes still dispatched by bash.

## Deliverables

### A. Schema (migration-agnostic — land first)
1. New migration `db/migrations/00NN_run_artifacts.sql`:
   ```
   run_artifacts(
     id INTEGER PRIMARY KEY,
     run_id TEXT NOT NULL,
     node_id TEXT,
     call_id INTEGER,              -- FK-ish to llm_calls.id (nullable)
     kind TEXT NOT NULL,           -- turn_jsonl | transcript | evidence_bundle | diff | verifier_json
     rel_path TEXT NOT NULL,       -- RELATIVE to the run dir (or MINI_ORK_HOME), never absolute
     bytes INTEGER,
     sha256 TEXT,
     created_at INTEGER,
     UNIQUE(run_id, node_id, kind, rel_path)
   );
   -- index on (run_id), (run_id, kind)
   ```
   Additive only. Do NOT touch existing tables.

### B. Conventions (migration-agnostic)
2. Normalize turn-log naming so EVERY dispatched node writes `agent-<node>.stream.jsonl` (fix the
   reviewer/verifier gap). The stream jsonl must contain per-turn events: role, tool calls + args,
   tool results, token usage, timing — not just the final message.
3. Paths stored in `run_artifacts.rel_path` are relative to the run dir. A helper resolves
   rel→abs at read time against `MINI_ORK_RUN_DIR`.
4. Retention: gzip `.stream.jsonl` on run completion; a prune step drops raw `turn_jsonl` older than
   `MO_TRAJECTORY_TTL_DAYS` (default 30) while KEEPING derived `evidence_bundle` rows forever.

### C. Writer (migration-aligned)
5. **Canonical write in Python**: in `mini_ork/dispatch/telemetry.py`, right after the `llm_calls`
   row is written, insert a `run_artifacts` row (kind=`turn_jsonl`, and `transcript` if present) with
   the relative path + bytes + sha256. Follow telemetry.py's existing schema-agnostic pattern (only
   write if the table exists) so it is a no-op on older DBs.
6. **Minimal bash mirror**: at the `_mo_llm_persist_agent_transcript` site in `lib/llm-dispatch.sh`,
   insert the same `run_artifacts` row for nodes still dispatched by bash. Mark it clearly
   `# MIGRATION: remove when this node moves to mini_ork.dispatch`. Keep it to a few lines.
7. Both writers target the same table + same rel-path convention, so coverage is complete today and
   the bash mirror deletes cleanly as nodes migrate.

## Smoke / DoD (must pass)
- `tests/unit/test_run_artifacts.sh` (or `tests/test_run_artifacts_py.py`): after a dispatched node,
  a `run_artifacts` row exists with kind=`turn_jsonl`, a rel_path that resolves to a real non-empty
  `.stream.jsonl` under the run dir, correct bytes/sha256, and NOT an absolute path.
- Coverage: a `code-fix` run registers a `turn_jsonl` artifact for EVERY dispatched node (implementer,
  reviewer, verifier), not just the implementer.
- Migration is additive: existing `pytest` + bash executor tests still green; the Python telemetry
  writer is a no-op when `run_artifacts` is absent (old DB).
- Retention: gzip-on-complete produces `.stream.jsonl.gz`; the rel_path in the DB points at the
  actual (possibly gzipped) file; prune leaves `evidence_bundle` rows untouched.

## Constraints
- Additive schema only; do not alter existing tables or the existing transcript-writing behavior.
- Relative paths ONLY in the DB (portability across vendor/foreign-home/cloud).
- The bash mirror is minimal + marked for removal; the canonical writer is Python `telemetry.py`.
- No new runtime deps. Do not block the run if the artifact write fails (best-effort, like the
  existing transcript persist).
