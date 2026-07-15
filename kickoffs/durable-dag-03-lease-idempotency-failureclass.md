# E3 — Single-writer lease + idempotent recovery + failure-class state machine

Depends on E1+E2. See design note §5, §7. Makes recovery concurrency-safe and classifies stops. Completes the correctness foundation (E1–E3) before turn-resume (E4).

## Goal
Prevent two workers/operator-clicks from resuming the same run concurrently, make repeated recovery requests idempotent, and classify each stop so only genuinely terminal conditions fail the whole run.

## Requirements
1. **`run_leases` table + fencing**: a recovery acquires a lease (owner_token, expires_at) before any dispatch or checkpoint write. Any checkpoint/terminal write must present the current token or be **rejected**. A stale worker with an expired token cannot publish.
2. **`recovery_requests` idempotency**: same `(run_id, from_node, strategy)` returns the in-flight/last recovery, not a duplicate dispatch.
3. **Failure-class state machine** (design §5): classify each stopped attempt as `infra_interrupt | provider_limit | output_invalid | input_required | terminal`. First four leave a durable recovery record; only `terminal` marks the run failed. Default policy: auto-recover only `infra_interrupt`; any new LLM attempt (`provider_limit`/`repair`) is explicit + budget-bounded, never an unbounded auto-retry loop.
4. Wire the lease around E2's recovery dispatch and E1's checkpoint publish.

## Files / areas in scope (touch ONLY these)
- The migrations directory (new migration: `run_leases`, `recovery_requests`)
- The recovery-planner module (E2) + checkpoint writer (E1) — add lease/fencing
- A new failure-classifier module (Python)
- `tests/`
Do NOT add turn-resume, trace, or UI.

## Verification command
```bash
bash tests/run-all.sh unit && python -m pytest tests/ -q -k "lease or idempoten or failure_class"
```
Must exit 0.

## Acceptance
- Scenario 6 (two concurrent recovery requests): one acquires ownership; the other returns a safe descriptive result; the node runs once.
- A stale worker cannot publish a checkpoint after a new recovery acquires the lease.
- A `max-turns` stop is classified `provider_limit` and does NOT auto-retry unboundedly.
- Only `terminal` marks the run failed; the other four leave a recovery record.
