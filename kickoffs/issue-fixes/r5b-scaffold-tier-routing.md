# R5b: wire the minimal-agent scaffold tier into the executor (default harness)

## Context
Epic `docs/epics/EPIC-cloud-exec-runtime-sandbox.md`, phase R5b. R5a landed the standalone native
agent `mini_ork.agent.minimal` (`run_minimal(task, *, cwd, max_turns, model)`), which runs a
stateless bash-command loop via `dispatch_model` and executes commands through the runtime seam
(`mo_runtime_exec`). Nothing routes to it yet. This phase adds the routing so bounded nodes can run
on the cheap minimal tier instead of the full Claude/Codex CLI harness — WITHOUT changing default
behavior.

## Hard constraints (delivery-safety — see epic)
- **Default scaffold tier is `harness`.** With nothing opted in, behavior is byte-identical to today.
- Opt-in only: a node runs minimal ONLY when `MO_SCAFFOLD_TIER=minimal` is set OR the node/recipe
  explicitly declares `scaffold: minimal`. No automatic reclassification in this phase (keep it
  deterministic + conservative; GRPO-learned routing is a later refinement).
- researcher + all consumers unaffected until they opt in. No new global state/lock.

## Deliverables
1. A scaffold-tier resolver in `lib/lane_router.sh` (or a small new `lib/scaffold_tier.sh` sourced
   by it): `mo_scaffold_tier <node_type> <task_class>` → echoes `minimal` or `harness`. Rules:
   returns `minimal` only when `MO_SCAFFOLD_TIER=minimal` (global opt-in) or a node declares it;
   otherwise `harness`. Document the bounded node types that are SAFE candidates (e.g. single-file
   mechanical implementer nodes, doc edits) but do NOT auto-route them yet — leave that to the env/
   declaration switch.
2. In `bin/mini-ork-execute`, at the worker/implementer dispatch site: if the resolved scaffold
   tier is `minimal`, run the node via the native minimal agent
   (`python3 -m mini_ork.agent ...` or a thin invoker calling `run_minimal`) with the node's task
   prompt + cwd, instead of the full CLI harness. Capture its output/result into the same
   node-result shape the harness path produces (so downstream verify/review is unchanged). Default
   (`harness`) path is untouched.
3. Pass `MO_RUNTIME_BACKEND` through to the minimal agent so its commands stay sandboxed when the
   backend is set (it already routes through the seam).

## Smoke / DoD (must pass)
- `bash -n bin/mini-ork-execute lib/lane_router.sh` (+ any new lib) clean.
- `tests/unit/test_scaffold_tier.sh`: resolver returns `harness` by default; returns `minimal`
   when `MO_SCAFFOLD_TIER=minimal`; returns `minimal` for a node that declares it.
- `pytest` still green; `tests/unit/test_runtime_contract.sh` + `test_executor_runtime_routing.sh`
   still green (default path unchanged).
- A focused test or documented probe: a node run with `MO_SCAFFOLD_TIER=minimal` invokes the
   minimal agent path (not the CLI harness) and produces a usable node result.

## Constraints (scope guard)
- Touch ONLY `bin/mini-ork-execute`, the scaffold-tier resolver lib, and tests.
- Do NOT change the default tier, recipes, `mini_ork/agent/*` (R5a done), or `lib/runtime/*`.
- No new pip dep. Keep the harness path the default and unchanged.
