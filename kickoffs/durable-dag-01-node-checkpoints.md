# E1 — Node checkpoints: durable state + hash validity + crash-safe publish

Foundation of durable DAG resume. See `internal-docs/architecture/2026-07-15-durable-dag-resume-design.md` §2–4. This epic alone kills the "rerun the whole DAG" failure and is correct on its own; E2+ build on it. Do NOT add recovery/CLI here — publish + validity only.

## Goal
Persist a crash-safe checkpoint at each node's completion, and provide a validity check that decides whether a completed node is reusable.

## Requirements
1. **Migration** adding `node_checkpoints` and `node_attempts` (design §2 shapes). Additive; legacy runs read unchanged.
2. **Checkpoint writer seam** (Python shared runtime, callable from both bash and python execute paths): after a node completes, write artifacts → fsync → compute `sha256` of each artifact → commit the `node_checkpoints` row (input_hash, recipe_version, config_hash, artifact_manifest_json) in one transaction. Never commit the row before artifacts+manifest are durable and self-consistent.
3. **Validity check** `is_node_reusable(run_id, node_id)`: returns reusable iff input_hash matches, recipe_version+config_hash match, and every manifest artifact exists and its sha256 verifies. Fails **closed** on any mismatch (design §3–4).
4. **Node-attempt row** appended per attempt (result, failure_class, cost, provider_session_id). Append-only.
5. Hook the writer into the node loop in `mini_ork/ported/mini_ork_execute.py` at node completion.

## Files / areas in scope (touch ONLY these)
- The migrations directory (new migration file) + schema doc
- `mini_ork/ported/mini_ork_execute.py` (call the writer at node completion)
- A new checkpoint-writer module (Python) + validity function
- `tests/` (new unit tests)
Do NOT add CLI, recovery, leases, or turn-resume — those are E2–E4.

## Verification command
```bash
bash tests/run-all.sh unit && python -m pytest tests/ -q -k checkpoint
```
Must exit 0.

## Acceptance
- A completed node writes a `node_checkpoints` row only after its artifacts verify.
- `is_node_reusable` returns False when an artifact is deleted or its bytes change, or when input/recipe/config hash differs.
- Crash window 1 (artifacts exist, no row) and window 2 (row exists, artifact corrupt) both resolve to "not reusable → rerun".
- Existing suites stay green; legacy runs with no checkpoint rows still read.
