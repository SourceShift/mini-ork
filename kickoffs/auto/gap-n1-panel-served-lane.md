# Gap N1: a judge panel's artifacts record the lane that actually served each judge

## Goal

The panel gate already reads the SERVED lane: the 4l verifier's
`check_served_models` (`recipes/audit-judge-panel-4l/verifiers/panel-completeness.py`
~330-360) grades `llm_calls.model_id` per lens alias, and
`mini_ork/verify/panel_heterogeneity.py` (`lane_family` :105, `check_panel`
:158) fails a panel on fallbacks or a repeated family.

The panel's ARTIFACTS do not. Each judge's `context-<node>.json` /
`lens-<family>.json` carries the LLM's self-reported `lane`/`model`
(`prompts/opus-judge.md` asks for "the model id that actually answered"), and
the sidecar check only compares that label with the recipe's
(`obj.get("lane") not in (None, lane, node)`, 4l ~262, 3l ~241). In
`run-1791725633-31802`, `context-opus_judge.json` said lane=opus while
`llm_calls` shows `opus_lens` served by glm. A reader of the artifacts cannot
tell which model judged.

The served lane is known at dispatch: `selected_model` in
`mini_ork/dispatch/llm_dispatch.py` (~1323-1329, written to `llm_calls.model_id`
and the run-wide `.last-llm-lane`). `.last-llm-lane` races between parallel
lenses (they share one run dir), so it cannot be used per node.

All paths are relative to the repository root.

## Mechanism (exact spec)

### 1. `mini_ork/dispatch/llm_dispatch.py` — a per-node lane record

Where the success path calls `_write_lane_sidecar(selected_model)` (~1329),
also write `<MINI_ORK_RUN_DIR>/.lane-<MO_NODE_ID>.json` when both are set:
`{"requested": <first lane of the dispatched chain>, "served": selected_model,
"fallback": served != requested, "at": <epoch>}`. Atomic (tmp +
`os.replace`), and any error is swallowed (must never change a dispatch
outcome). A node id with characters outside `[A-Za-z0-9_.-]` is skipped.

### 2. `mini_ork/cli/execute_handlers.py` — stamp the artifact

Add `_stamp_served_lane(run_dir, node_id, out_file)` and call it in the
researcher handler right after `_materialize_lens_json(...)` (~1628):

- read `<run_dir>/.lane-<node_id>.json`; missing or unreadable → return
  (nothing stamped);
- for each of `<run_dir>/context-<node_id>.json` and any
  `lens-*.json` that `_materialize_lens_json` just wrote for this node, when
  it is a JSON object: set `served_lane`, `requested_lane` and
  `served_family` (`mini_ork.verify.panel_heterogeneity.lane_family(served)`),
  OVERWRITING whatever the LLM wrote under those keys, and rewrite the file
  atomically;
- when the node's output is markdown only (no JSON artifact), write
  `<out_file>.lane.json` with the same three keys;
- never raise.

### 3. Both panel verifiers

In `recipes/audit-judge-panel-3l/verifiers/panel-completeness.py` and
`recipes/audit-judge-panel-4l/verifiers/panel-completeness.py`:

- add the per-judge served lanes and families the verifier already computes
  (or reads from the artifacts' `served_lane`) to the stdout verdict JSON under
  `"served"` (a mapping node → lane) and `"families"` (node → family);
- when an artifact's harness-stamped `served_lane` disagrees with the lane
  `llm_calls` shows serving that node, add a gating failure
  `sidecar_served_mismatch:<node>`. When either side is unknown, no failure
  (missing evidence is not a mismatch).

## Files in scope (touch ONLY these)

- `mini_ork/dispatch/llm_dispatch.py`
- `mini_ork/cli/execute_handlers.py`
- `recipes/audit-judge-panel-3l/verifiers/panel-completeness.py`
- `recipes/audit-judge-panel-4l/verifiers/panel-completeness.py`
- `tests/unit/test_served_lane_stamp.py` (new)
- `tests/unit/test_audit_judge_panel_4l.py`
- `tests/unit/test_audit_judge_panel_3l.py` (new)

Do not modify `mini_ork/verify/panel_heterogeneity.py`, `mini_ork/trace_store.py`, `mini_ork/cli/execute.py`, recipe prompts or workflow.yaml files, `docs/`, or any path in `config/kernel-frozen.yaml`. Every existing test must pass unchanged.

## Tests (hermetic, no LLM)

`tests/unit/test_served_lane_stamp.py`:

1. Through `llm_dispatch` with a fake dispatch where the chain is
   `opus,glm` and `glm` serves (follow `tests/unit/test_lane_fuse_and_chain_py.py`
   ~264-302), with `MINI_ORK_RUN_DIR` and `MO_NODE_ID=opus_judge` set →
   `.lane-opus_judge.json` has requested `opus`, served `glm`, fallback true.
2. `_stamp_served_lane` over a `context-opus_judge.json` that claims
   `"lane": "opus", "served_lane": "opus"` → afterwards `served_lane == "glm"`,
   `requested_lane == "opus"`, `served_family == lane_family("glm")`, and the
   other keys are unchanged.
3. Markdown-only output → `<out_file>.lane.json` written.
4. No `.lane-<node>.json` → nothing written, no exception.
5. A node id with `/` → no per-node file written.

`tests/unit/test_audit_judge_panel_4l.py` (append) and
`tests/unit/test_audit_judge_panel_3l.py` (new, same harness style):

6. A complete panel whose artifacts' `served_lane` match `llm_calls` passes,
   and the verdict JSON has `served` and `families` for every judge.
7. An artifact whose `served_lane` disagrees with `llm_calls` fails with
   `sidecar_served_mismatch:<node>`.
8. An artifact with no `served_lane` (older run) and a matching
   `llm_calls` → no mismatch failure.

## Verification commands

- `python3.11 -m pytest -q -p no:asyncio tests/unit/test_served_lane_stamp.py tests/unit/test_audit_judge_panel_4l.py tests/unit/test_audit_judge_panel_3l.py tests/unit/test_panel_heterogeneity.py tests/unit/test_lane_fuse_and_chain_py.py tests/unit/test_llm_dispatch_py.py tests/unit/test_prompt_bearing_early_nodes.py tests/test_node_artifact_completion.py`

## Self-application measurement

```measure
python3.11 -m pytest -q -p no:asyncio tests/unit/test_served_lane_stamp.py --junitxml="${MINI_ORK_RUN_DIR}/gap-n1-junit.xml"
```

## Required artifacts — declare EXACTLY these and nothing else

- `${MINI_ORK_RUN_DIR}/gap-n1-junit.xml`

## Success criteria

- The verification command passes; every pre-existing test is unchanged.
- `ruff check --select F,E9 mini_ork/dispatch/llm_dispatch.py mini_ork/cli/execute_handlers.py recipes/audit-judge-panel-3l/verifiers/panel-completeness.py recipes/audit-judge-panel-4l/verifiers/panel-completeness.py tests/unit/test_served_lane_stamp.py tests/unit/test_audit_judge_panel_4l.py tests/unit/test_audit_judge_panel_3l.py` is clean.
- Every judge artifact names the lane that served it, written by the harness, not the model.
