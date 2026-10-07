# `mini-ork recover` verify strategy — revision 2 (Opus review of run recover-verify-20261007103743)

WIP commit 1c89410c passed review on: catalog-first recipe resolution, planner `plan_json` reuse,
`restore.py` precedence and statuses, `_USAGE`, scope. Fix only the list below.

## Files in scope

- `mini_ork/recovery/plan.py`, `mini_ork/recovery/planner.py`, `mini_ork/recovery/restore.py`
- `tests/unit/test_recover_verify.py`

## Fixes (exact)

1. **Verify must accept a failed entry verifier** (`plan.py:595`). `_ancestors(dag, verify_entry)`
   includes the entry itself, so `--strategy verify` refuses whenever the first verifier never
   succeeded — the main reason to use verify. Iterate `_ancestors(dag, verify_entry) - {verify_entry}`.
2. **No node in both reuse and rerun** (`plan.py:602`, and the `from_node` path in
   `compute_recovery`). After computing the closure, `plan.reuse -= plan.closure`. The status
   output must never list one node under both.
3. **`--status` never refuses** (`planner.py:612`, `:626`). Guard the needs-change and
   not-retryable refusals with `not status_only`; `--status` prints the hint block, the restore
   plan and the full reuse/rerun plan, exit 0.
4. **Hint source** (`planner.py:605`). Try `from mini_ork.recovery.retry_hint import
   load_or_compute` and call `load_or_compute(Path(home), run_id)`; only on ImportError (or when
   it returns None) read `<run_dir>/retry-hint.json`.
5. **Restore after the lease** (`planner.py:657` vs `:714`). Call `restore_carry_patch` only after
   `acquire_lease` succeeded (or when lease tables are absent), so a refused second worker never
   touches the tree.
6. **Code-changing types agree with the docstring** (`planner.py:384` vs `:394`): restore is needed
   when the reuse set contains a node whose type is `implementer` or `fix` / `edit` / any type the
   workflow marks code-changing; update the docstring to exactly what the code checks (do not list
   `rollback`).

## Tests (each must fail on 1c89410c)

- Upstream reusable (planner with plan.json, a lens, implementer success) + FIRST verifier failed
  (no success checkpoint) → `--strategy verify` dispatches: inject `execute_fn`, assert it is called
  once, entry = that verifier, the verifier is not under reuse.
- Gate success + smoke failed, `--from-node smoke` and `--strategy verify` → no node appears in
  both reuse and rerun in `format_status` output.
- `--status` with a `needs_change` hint → exit 0, output has the hint block AND the reuse/rerun plan.
- `retry_hint.load_or_compute` importable (monkeypatch a fake module into `sys.modules`) returning a
  hint with no file on disk → the gate uses it.
- Lease held by another worker → `restore_carry_patch` is never called (spy) and the tree is unchanged.

## Done when

- `python3.11 -m pytest -q -p no:asyncio tests/unit/test_recover_verify.py tests/unit/test_recover*.py tests/unit/test_recovery*.py` passes — paste the summary line and the real test count.
- `uvx ruff check mini_ork/recovery tests/unit/test_recover_verify.py` is clean — paste it.
- Read-only proof on the real run (never dispatch against a real home), paste the full output:
  `MINI_ORK_HOME=/Volumes/docker-ssd/Migration/Development/researcher/.mini-ork bin/mini-ork recover run-le-1791359434-64879-1 --status --strategy verify --carry-patch cycle-delta.patch`
  → reuse = planner, invariants_lens, operations_lens, falsifier_lens, synthesizer, implementer;
  entry `cycle_gate`; rerun = cycle_gate, live_smoke, scope_guard, opus_judge, publisher (each
  exactly once, none of them under reuse); a restore line naming `cycle-delta.patch` and the target
  worktree.
- Diff touches only files in scope.
