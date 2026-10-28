# Gap N3: recover never reuses a node whose verifier failed after it

## Goal

`mini-ork recover <run>` re-runs only the nodes that are not reusable. Reuse
is decided per node from that node's own checkpoint:
`is_node_reusable` (`mini_ork/stores/checkpoints.py:658-751`) checks status,
hashes and artifact shas, and `_reusable_set` (`mini_ork/recovery/plan.py:289-317`)
collects the result.

Verifier nodes never get a `node_checkpoints` row (their handler writes only
a `node_attempts` row; `execute_handlers.py:1208-1213`, `execute.py:4729-4741`),
and nothing in the reuse path reads verifier outcomes. So when an
implementer's verifier FAILED, recover still reuses the implementer's success
checkpoint and re-runs only the verifier and what follows it, against an
implementer output already known to be bad. Today the only way around it is
`--from-node implementer`.

All paths are relative to the repository root.

## Mechanism (exact spec)

In `mini_ork/recovery/plan.py`:

1. Add a read-only helper
   `_invalidate_on_failed_verifiers(db, run_id, dag, node_types, reuse, reason) -> None`
   that, for every node `P` in `reuse` whose `reason[P] == ""` (reusable
   through a real checkpoint, not the planner `plan_json` shortcut):
   - finds every node `V` with `node_types[V] == "verifier"` and `P` in
     `dag.parents[V]` (a direct data edge; `dag` already excludes
     `escalates_to`/`retries`);
   - reads `V`'s LATEST `node_attempts` row for `run_id` (highest `id`);
   - treats it as a real failure when `result IN ('failure', 'error')` AND
     `failure_class` is not NULL and not one of `infra_interrupt`,
     `provider_limit`, `input_required` (a NULL class is an abstain: rc 3,
     finish `done`; those do not count);
   - treats it as NEWER than `P`'s output when its `id` is greater than the
     `id` of `P`'s latest `node_attempts` row with `checkpoint_produced = 1`
     (no such row → compare `V.ended_at >= node_checkpoints.created_at` for
     `P`; a tie counts as newer);
   - when both hold, removes `P` from `reuse` and sets
     `reason[P] = f"verifier_failed:{V}"`.
   Any DB error → leave `reuse` unchanged (fail open to today's behaviour)
   and write one stderr line.
2. Call it in `compute_recovery` right after `_reusable_set` (~466, before the
   closure is computed ~481). Do NOT call it when `from_node` is given (the
   operator's choice wins) or for the `verify` strategy: `plan_recovery` passes
   a keyword (`check_verifiers=False`) for `verify`; the default is `True`.
3. Nothing else changes: `P` now becomes the first non-reusable node, and the
   existing fixpoint pulls its descendants into the closure.
   `is_node_reusable` is not modified.

## Files in scope (touch ONLY these)

- `mini_ork/recovery/plan.py`
- `tests/test_recovery_closure.py`
- `tests/unit/test_recover_verify.py`

Do not modify `mini_ork/stores/checkpoints.py`, `mini_ork/recovery/planner.py`, `mini_ork/recovery/auto_repair.py` (frozen kernel), `mini_ork/cli/execute.py`, `docs/`, any migration, or any path in `config/kernel-frozen.yaml`. Every existing test must pass unchanged.

## Tests

In `tests/test_recovery_closure.py` (its fixture already creates
`node_attempts`, ~66; add a small helper to seed attempt rows), with a DAG
`implementer -> verifier_a`, `implementer -> verifier_b`, `verifier_* -> reviewer`:

1. `verifier_a`'s latest attempt `result='failure'`, `failure_class='terminal'`,
   newer than the implementer's checkpoint → `implementer` is NOT in `reuse`,
   `reason["implementer"] == "verifier_failed:verifier_a"`, and the closure
   contains `implementer`, both verifiers and `reviewer`.
2. Same, but `failure_class` NULL (an abstain) → `implementer` is reused.
3. Same, but `failure_class='infra_interrupt'` → `implementer` is reused.
4. The failure is OLDER than a newer implementer checkpoint (the implementer
   re-ran after it) → `implementer` is reused.
5. With `from_node="verifier_a"` → behaviour unchanged from today
   (`implementer` reused).
6. Both verifiers passed → `implementer` reused (today's behaviour).

In `tests/unit/test_recover_verify.py`:

7. The `verify` strategy over a run whose first verifier failed still
   dispatches (no refusal), as `test_verify_dispatches_with_failed_first_verifier`
   already pins.

## Verification commands

- `python3.11 -m pytest -q -p no:asyncio tests/test_recovery_closure.py tests/unit/test_recover_verify.py tests/test_node_checkpoints.py tests/unit/test_resume_refresh_pre_impl_baseline.py tests/unit/test_recover_task_class.py`

## Self-application measurement

```measure
python3.11 -m pytest -q -p no:asyncio tests/test_recovery_closure.py --junitxml="${MINI_ORK_RUN_DIR}/gap-n3-junit.xml"
```

## Required artifacts — declare EXACTLY these and nothing else

- `${MINI_ORK_RUN_DIR}/gap-n3-junit.xml`

## Success criteria

- The verification command passes; every pre-existing test is unchanged.
- `ruff check --select F,E9 mini_ork/recovery/plan.py tests/test_recovery_closure.py tests/unit/test_recover_verify.py` is clean.
- Recover re-runs a node whose direct verifier failed after its last checkpoint; abstains, infra failures and `--from-node` keep today's behaviour.
