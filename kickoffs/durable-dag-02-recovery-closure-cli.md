# E2 — Dependency-closure recovery + `mini-ork recover` CLI/API

Depends on E1 (`node_checkpoints` + `is_node_reusable` must exist). See design note §3, §5, §9. Adds the recovery walk and the operator entrypoint. Do NOT add leases/idempotency (E3) or turn-resume (E4) here.

## Goal
Resume a run from the earliest non-reusable node using dependency closure, reusing valid ancestors, via a new CLI/API entrypoint distinct from the cost-pause `resume`.

## Requirements
1. **Recovery planner**: given a run_id, compute the DAG from `workflow.yaml` (reuse `lib/topology.sh` / the python topology port), mark each node reusable via E1's `is_node_reusable`, find the earliest non-reusable node, and return the **dependency closure** to rerun (the node + its transitive dependents). A successful parallel branch must NOT be in the set.
2. **`mini-ork recover <run_id> [--from-node X] [--strategy resume|retry|repair|pause]`** CLI + matching API. `resume` = from first incomplete node; `retry` = the failed node; `repair` = rerun with failure evidence + a bounded turn/cost budget; `pause` = wait for human.
3. **`mini-ork recover --status <run_id>`**: print valid checkpoints, exactly which nodes will be **reused** vs **rerun**, and the cost boundary — WITHOUT dispatching.
4. **Keep `mini-ork resume <run_id>` (cost pause) and steering pause unchanged and separate.** A distinct subcommand/path so the two are never conflated.
5. Recovered nodes reuse E1 checkpoints/artifacts — no new LLM dispatch for reused ancestors.

## Files / areas in scope (touch ONLY these)
- `bin/mini-ork` + `mini_ork/ported/mini_ork_cli.py` (new `recover` subcommand)
- A new recovery-planner module (Python)
- `mini_ork/ported/mini_ork_execute.py` (enter the loop at the closure's first node)
- `tests/`
Do NOT modify the existing `resume`/steering code paths beyond calling them; do NOT add leases or turn-resume.

## Verification command
```bash
bash tests/run-all.sh unit && python -m pytest tests/ -q -k "recover or closure"
```
Must exit 0.

## Acceptance
- Scenario 1 (nodes A–C checkpointed, D failed): `recover` reruns only D; A–C get zero new dispatches/LLM calls.
- Scenario 5 (parallel DAG, one branch failed): only the failed branch + dependents rerun.
- `recover --status` shows the reuse/rerun split and cost with no dispatch.
- `mini-ork resume` (cost pause) behavior unchanged.
