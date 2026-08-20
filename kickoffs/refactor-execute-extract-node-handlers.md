# Refactor: extract node handlers out of cli/execute.py (Slice 1 of god-module decomposition)

## Objective

`mini_ork/cli/execute.py` is a 2934-LOC god module — the highest change-risk
surface in the repo. This is **Slice 1** of decomposing it: a **pure,
behavior-preserving move** of the node-handler block into a sibling module.
No logic changes, no new behavior, no signature changes. Only relocate code
and preserve every public and private name currently reachable through
`mini_ork.cli.execute`.

## What to move

Move the entire block from **line 1874 (`def dispatch_node`) to end-of-file**
into a NEW module `mini_ork/cli/execute_handlers.py`. That block defines,
among others:

- `dispatch_node`
- `register_implementer_submode`
- `class NodeDispatch`
- `_handle_planner_early`, `_handle_reflector_early`, `_handle_researcher`,
  `_handle_implementer`, `_handle_reviewer`, `_handle_transform`,
  `_handle_verifier`, `_handle_publisher`, `_handle_rollback`, `_handle_eval`
- support helpers: `_classify_review_node`, `_rollback_strategy`,
  `_revert_working_tree`, `_read_run_trajectory`, `_verifier_noise_rates`,
  `_eval_artifact_text`, `_stamp_run_eval_reward`,
  `_warn_if_jury_not_decorrelated`
- the two registry dicts `EARLY_NODE_HANDLERS`, `NODE_HANDLER_REGISTRY`
- `register_node_handler`

`execute_handlers.py` imports whatever it needs (`set_status`,
`charge_node_cost`, `apply_impl_output`, `_run_verifier_ref`,
`_assemble_reviewer_inputs`, `_learned_block`, `_default_llm_dispatch`,
`_make_trace_fn`, `_make_checkpoint_fn`, `_assert_lane_capability`,
`_watchdog_stale_heartbeat`, `_intervention_gate_check`,
`_execute_gate_check`, `_researcher_output_file`, `_synth_artifact_name`,
`_capture_pre_impl_baseline`, `_harvest_self_migrate_artifacts`,
`_write_self_migrate_implementer_summary`, and any others the moved code
references) **from `mini_ork.cli.execute`**. Every helper the block uses is
defined ABOVE line 1874, so importing them from `execute` is safe.

## Scope — files codex may edit

- `mini_ork/cli/execute.py` — remove the moved block; add a re-export at EOF.
- `mini_ork/cli/execute_handlers.py` — NEW; receives the moved block verbatim.

Do NOT edit any other file. No test edits. No config edits.

## Hard invariants (verifier will check these)

1. **Behavior-preserving.** The moved code is relocated verbatim. No renames,
   no signature changes, no logic edits, no reordering that changes behavior.

2. **Every name stays reachable through `mini_ork.cli.execute`.** Tests and
   callers do `import mini_ork.cli.execute as ex; ex._handle_eval(...)`,
   `from mini_ork.cli.execute import NodeDispatch`,
   `from mini_ork.cli.execute import NODE_HANDLER_REGISTRY`,
   `from mini_ork.cli.execute import _handle_eval`, etc. — for BOTH public and
   underscore-prefixed names. So at the END of `execute.py`, add an explicit
   re-import binding every moved name back into the `execute` namespace, e.g.
   `from mini_ork.cli.execute_handlers import (dispatch_node, NodeDispatch,
   register_node_handler, register_implementer_submode, EARLY_NODE_HANDLERS,
   NODE_HANDLER_REGISTRY, _handle_eval, _handle_planner_early, ... )`.
   `import *` will NOT do — it skips underscore names. Enumerate every moved
   top-level name.

3. **Registry object identity is preserved.** `EARLY_NODE_HANDLERS` and
   `NODE_HANDLER_REGISTRY` must be the SAME dict objects in both modules
   (import them, never recreate). `register_node_handler(...)` mutating the
   dict from either module must be visible everywhere. `dispatch_node`'s
   handler lookup must still resolve every built-in node type.

4. **No circular-import error.** `python3.11 -c 'import mini_ork.cli.execute'`
   must succeed. Break the cycle by placing the re-import at the BOTTOM of
   `execute.py` (after all helper definitions), and/or importing execute
   helpers lazily inside `execute_handlers.py` functions. `main()`,
   `_isolated_dispatch_worker`, and `_run_parallel_batch` (which stay in
   `execute.py` and call `dispatch_node`) must still resolve it at call time,
   including inside a freshly-spawned ProcessPool child.

5. **No import-time side effects added.** Neither module may run dispatch,
   touch the DB, or spawn processes at import.

## Out of scope (do NOT touch)

The scheduler/parallel machinery (`main`, `_run_parallel_batch`,
`_isolated_dispatch_worker`, `_max_parallel`, `_dispatch_dependency_graph`),
arg parsing, DB helpers (`set_status`, `charge_node_cost`), the verifier
runners, and every helper defined above line 1874 STAY in `execute.py`. This
slice moves handlers only.

## Verification (must all pass)

- `python3.11 -c 'import mini_ork.cli.execute as ex; [getattr(ex, n) for n in
  ("dispatch_node","NodeDispatch","register_node_handler",
  "register_implementer_submode","EARLY_NODE_HANDLERS","NODE_HANDLER_REGISTRY",
  "_handle_eval","_handle_implementer","_handle_reviewer","_handle_verifier",
  "_handle_rollback","_revert_working_tree")]'` — all resolve.
- `python3.11 -m pytest -q tests/unit/test_eval_judge.py
  tests/unit/test_node_handler_registry_py.py tests/unit/test_revert_branch_py.py
  tests/unit/test_verifier_dispatch_py.py tests/unit/test_mini_ork_execute_py.py
  tests/unit/test_execute_main_helpers_py.py tests/test_recovery_closure.py` —
  green.
- Full suite `python3.11 -m pytest -q` — no new failures vs baseline.
- `ruff check mini_ork/cli/execute.py mini_ork/cli/execute_handlers.py` (F+E9) — clean.

## Trajectory

This kickoff drives a two-file change (`execute.py` + new
`execute_handlers.py`). It is the first extraction in a multi-slice plan to
break the executor god module along its natural seams; later slices will
extract the scheduler and the DB/status helpers.
