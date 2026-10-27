# RSI Tier-1 #7b: route typed workflow.yaml edits through the apply gate (default off)

## Goal

`mini_ork/learning/workflow_edit.py` (kickoff `rsi-t1-harness-evolution`, which
must be merged first) turns ONE typed edit (`node_order`, `lane_pin`,
`tool_grant` or `retry_count`) into a validated, compiled, reversible
`workflow.yaml` text. This kickoff makes those edits candidate mutations of
the existing apply loop:

- A **new target form, `harness.<recipe>.workflow.yaml`**. Its
  `gradient_records.suggested_change` is the edit's JSON. It cannot collide
  with `harness.<recipe>.<node>`, because a node name cannot contain `.`
  (schema pattern `^[a-z][a-z0-9_-]*$`), so `_harness_target_file_for`
  already returns `''` for it.
- **The same gate.** The candidate is scored by
  `probe_scorer.probe_score` on a temp recipe copy whose `workflow.yaml` is
  the edited text, using the control arm and the per-task no-regression rule.
  The harness-cost-regression flip applies because the target starts with
  `harness.`. An unmeasured candidate is quarantined. A fabricating scorer
  never promotes.
- **Rollback.** A promote writes the live file only when it is
  byte-identical to what was scored. It registers a `version_registry` row of
  kind `workflow` that carries both texts, so the existing
  `version_registry.rollback("workflow", <path>)` restores the old bytes.
- **Default off** behind a new flag, `MO_APPLY_HARNESS_WORKFLOW=1`, and
  still behind the `MO_APPLY_ENABLED=1` master gate for any write.

`agent.*` and `harness.<recipe>.<node>` targets must behave byte-identically.
No migration: `apply_attempts.target_kind` already allows `workflow_node`
(`db/migrations/0048_apply_attempts.sql`), and `version_registry.kind`
already allows `workflow`.

All paths are relative to the repository root.

## Mechanism (exact spec)

### 1. `mini_ork/learning/probe_scorer.py`

- **`_materialize_arm`.** It gains a keyword-only parameter `workflow_text: str | None = None`.
  - The parameter is checked right after the copied `probes/` directory is
    removed.
  - When it is not `None`, the function writes it as utf-8 to
    `os.path.join(dst, "workflow.yaml")`, overwriting the file. It then
    returns `(name, that path)`, and `directive_block` is ignored.
  - Otherwise the function is unchanged.
- **`probe_score`.** It gains a keyword-only parameter `workflow_text: str | None = None`.
  - The ONLY behavioural change: the candidate-arm call becomes
    `_materialize_arm(task_class, target_file, block, 1, workflow_text=workflow_text)`.
  - The baseline arm, the control arm, the budget handling and the result
    shape are unchanged.
  - With `workflow_text=None`, behaviour is byte-identical.

### 2. `mini_ork/cli/apply.py`

- **Docstring env contract.** Add an entry below the `MO_APPLY_HARNESS_TARGETS` one:
  `MO_APPLY_HARNESS_WORKFLOW=1  opt-in: typed workflow.yaml edits for harness.<recipe>.workflow.yaml targets (default OFF)`.
- **New constant** `WORKFLOW_TARGET_SUFFIX = ".workflow.yaml"`.
- **New helper** `_is_workflow_target(target: str) -> bool`. It returns
  `target.startswith("harness.") and target.endswith(WORKFLOW_TARGET_SUFFIX) and len(target) > len("harness.") + len(WORKFLOW_TARGET_SUFFIX)`.
- **New helper** `_workflow_file_for(recipe_dir: str, target: str) -> str`.
  - It returns `""` unless `recipe_dir` is non-empty and `_is_workflow_target(target)` is true.
  - Let `recipe = target[len("harness."):-len(WORKFLOW_TARGET_SUFFIX)]`. It
    returns `""` unless `os.path.basename(os.path.normpath(recipe_dir))` is in
    `{recipe, recipe.replace("_", "-"), recipe.replace("-", "_")}`.
  - It returns `os.path.join(recipe_dir, "workflow.yaml")` when that file
    exists, and `""` otherwise.
- **`materialize_candidate`.** It gains keyword-only parameters
  `mutation_kind: str = "prompt_change"` and `field: str = "system_prompt"`,
  used as the `"kind"` and `"field"` values of the mutations JSON. The
  defaults keep the output byte-identical.
- **New public function**
  `apply_workflow_mutation(candidate_id: str, target_file: str, result: dict, db: str | None = None) -> str`.
  Add it to `__all__`.
  - **The master gate.** When `MO_APPLY_DRY_RUN == "1"` or
    `MO_APPLY_ENABLED != "1"`, it writes a one-line stderr note starting
    `apply_workflow_mutation: dry-run` and returns `""`.
  - **The write.** With `workflow_edit` imported locally, when
    `workflow_edit.apply_to_file(target_file, result)` returns `False`, it
    writes a stderr note containing `changed since scoring` and returns `""`.
  - **The version row.** It calls
    `version_registry.register("workflow", json.dumps(payload), db=db)`.
    `payload` has EXACTLY these keys:
    - `name`: `target_file`;
    - `version_id`: `None`;
    - `status`: `"stable"`;
    - `utility_score`: `0.0`;
    - `rollback_hash`: `result["before_sha256"]`;
    - `content`: `result["after_text"]`;
    - `baseline_content`: `result["before_text"]`;
    - `candidate_id`;
    - `target_path`: `target_file`;
    - `edit`: `result["edit"]`;
    - `signature`: `result["signature"]`.

    Any exception there makes the return value `""`. The file stays written,
    the same as `apply_mutation`.
  - It returns the version id.
- **`apply_run`.** Let `wf_mode = _is_workflow_target(target_name)`. Every
  change below applies ONLY when `wf_mode` is true.
  - **(a) The refusal block.** It goes right after the step-1b
    `_previously_failed` early return and before step 2's
    `materialize_candidate`. It computes a `reason` string, checked in this order:
    1. `os.environ.get("MO_APPLY_HARNESS_WORKFLOW", "0") != "1"` gives
       `"workflow edits disabled (MO_APPLY_HARNESS_WORKFLOW!=1)"`;
    2. `os.environ.get("MO_APPLY_SCORER", "probe") == "code"` gives
       `"workflow edits are scored by the probe scorer, not the code scorer"`;
    3. otherwise it calls
       `wf_result = workflow_edit.materialize(target_file, suggested_change)`.
       A `WorkflowEditError` gives `f"workflow edit refused: {exc}"`.

    A non-empty `reason` does the same as the existing step-1b early return:
    - `attempt_record(...)` with decision `"rejected"` and rationale `reason`;
    - writes the attempt id line;
    - writes the JSON line
      `{"decision":"rejected","task_class":"<tc>","target":"<target>","source_id":"<sid>"}`;
    - returns `0`.

    The rejected row then feeds `_previously_failed`, so a malformed or
    refused edit is never re-proposed.
  - **(b) Step 2.** It passes `mutation_kind="workflow_edit"` and
    `field=wf_result["edit"]["op"]`.
  - **(c) Step 3, `scorer == "probe"` branch.** It calls
    `_ps.probe_score(task_class, target_file, suggested_change, source_ref=<unchanged expr>, context=<unchanged expr>, workflow_text=wf_result["after_text"])`.
  - **(d) Steps 4 and 4b are unchanged.**
  - **(e) The stale-base check.** It goes immediately before step 5
    (`record_promotion`). When `gate_decision == "promoted"` and
    `workflow_edit.file_sha256(target_file) != wf_result["before_sha256"]`:
    - set `gate_decision = "quarantined"`;
    - append `" | workflow-base-changed: the live workflow.yaml changed after scoring"`
      to `gate_rationale`.
  - **(f) Step 6.** Inside the unchanged
    `gate_decision == "promoted" and target_file and suggested_change`
    condition, it calls
    `apply_workflow_mutation(candidate_id, target_file, wf_result, db=db)`
    instead of `apply_mutation`.
- **`auto_sweep`.** Let `wf_on = os.environ.get("MO_APPLY_HARNESS_WORKFLOW", "0") == "1"`.
  - **The selection SQL.**
    - When `harness_on` is true, the existing query is unchanged.
    - When it is false and `wf_on` is true, the query filters on
      `(target LIKE 'agent.%' OR target LIKE 'harness.%.workflow.yaml')`.
      The other clauses (`task_class=?`, `NOT LIKE 'cross_class:%'`,
      `ORDER BY confidence DESC`) are unchanged.
    - Otherwise the query is unchanged.
  - **The per-target loop.** Before the existing `_prompt_file_for` call:
    when `wf_on and _is_workflow_target(target)`, let
    `target_file = _workflow_file_for(recipe, target)`.
    - When it is `""`: print `[auto-apply] {target}: skipped (no workflow file in recipe)`,
      append `{"target": target, "skipped": "no workflow file"}` and continue.
    - Otherwise: call `apply_run(task_class, "workflow_node", target, target_file, db=db)`,
      then do the same `apply_attempts` decision lookup, print and append as
      the prompt path.
  - With `wf_on` false, the loop is byte-identical. A workflow target that
    `harness_on` selects still gets the existing `no prompt file` skip.
- **`main()`.** Before the existing `if target_name.startswith("harness."):`
  block, handle a target where `_is_workflow_target(target_name)` is true:
  - set `target_file = _workflow_file_for(_recipe_dir_for(task_class), target_name)`;
  - when it is `""`, write `apply: {target_name}: no workflow file in recipe`
    to stderr and return `2`;
  - otherwise return `apply_run(task_class, "workflow_node", target_name, target_file, db=db)`.

## Files in scope (touch ONLY these)

- `mini_ork/cli/apply.py`
- `mini_ork/learning/probe_scorer.py`
- `tests/unit/test_apply_workflow_edit.py` (new)

Do NOT modify any other file. `mini_ork/learning/workflow_edit.py` is
read-only here.

## Tests (`tests/unit/test_apply_workflow_edit.py`: hermetic tmp_path + sqlite, no network, no real probe run)

**Helpers to copy.** Copy these from `tests/unit/test_apply_harness_targets.py`:
`FIXTURE_DDL`, the `harness_env` and `harness_db` fixtures, `_seed_gradient`,
`_rows` and `_common_apply_kwargs`. Add `"MO_APPLY_HARNESS_WORKFLOW"` to
`harness_env`'s scrub list.

**The recipe.** A `wf_recipe` fixture writes the YAML below to
`tmp_path/"recipes"/"fw-edit"/"workflow.yaml"`. Pass `wf_recipe` wherever
`_common_apply_kwargs` takes `harness_recipe`, because it roots at
`.parent.parent`. Also set `MINI_ORK_ROOT=str(tmp_path)`.

```
version: "0.1.0"
task_class: fw_edit
nodes:
  - name: planner
    type: planner
    dispatch_mode: serial
    model_lane: sonnet
    prompt_ref: prompts/planner.md
  - name: implementer
    type: implementer
    dispatch_mode: serial
    model_lane: sonnet
    prompt_ref: prompts/implementer.md
    tools:
      native: [Read, Write]
      mcp: [codegraph]
    retry:
      max_attempts: 3
  - name: lint
    type: verifier
    dispatch_mode: serial
    verifier_ref: verifiers/lint.py
    tools:
      native: [Read, Bash]
  - name: docs
    type: researcher
    dispatch_mode: serial
    model_lane: sonnet
    prompt_ref: prompts/docs.md
    tools:
      native: [Read]
  - name: publisher
    type: publisher
    dispatch_mode: serial
    prompt_ref: null
edges:
  - {from: planner, to: implementer, edge_type: depends_on}
  - {from: planner, to: docs, edge_type: depends_on}
  - {from: implementer, to: lint, edge_type: depends_on}
  - {from: lint, to: publisher, edge_type: depends_on}
  - {from: docs, to: publisher, edge_type: depends_on}
```

**The edit and the target.** `T = "harness.fw-edit.workflow.yaml"` and
`EDIT = {"op": "lane_pin", "node": "implementer", "lane": "glm"}`. Seed with
`_seed_gradient(..., target=T, change=json.dumps(EDIT), confidence=0.9, task_class="fw_edit")`.

**The two stub shapes.** Unless a test says otherwise, `MO_APPLY_SCORER=probe`.
- **"Promote numbers"** are those of
  `test_apply_run_harness_target_lower_cost_promotes`: `n=2`,
  `before_avg=0.5`, `after_avg=1.0`, `before_bin=[0, 1]`,
  `after_bin=[1, 1]`, and `0.04` / `0.04` costs.
- **"Cost-flip numbers"** are the same with `candidate_total_cost=0.40`.

When a test must see the kwargs, re-monkeypatch
`mini_ork.learning.probe_scorer.probe_score` with a capturing lambda that
returns the same dict.

Tests:

- **Off by default.**
  - With neither flag, `ap.auto_sweep("fw_edit", db=...)` never calls the
    stub, and `apply_attempts` has no row for `T`.
  - With only `MO_APPLY_HARNESS_TARGETS=1`, the result for `T` is
    `{"target": T, "skipped": "no prompt file"}` and the stub is not called.
- **On: the sweep routes through the gate.** Set `MO_APPLY_HARNESS_WORKFLOW=1`
  and `MO_APPLY_ENABLED=1`, and use the promote numbers.
  - `auto_sweep` returns one entry for `T` with `decision == "promoted"`.
  - The stub got the positional target file
    `str(wf_recipe / "workflow.yaml")` and a `workflow_text` kwarg.
    `yaml.safe_load` of that text has the implementer's `model_lane == "glm"`.
  - The `apply_attempts` row for `T` has `target_kind == "workflow_node"`.
  - `json.loads(workflow_candidates.mutations)[0]` has `kind == "workflow_edit"`
    and `field == "lane_pin"`.
- **A promote writes, registers, and rolls back.** Run
  `ap.apply_run("fw_edit", "workflow_node", T, str(wf_recipe/"workflow.yaml"), db=harness_db)`
  with the flag on and the promote numbers.
  - The live YAML now has the implementer's `model_lane == "glm"`.
  - `version_registry` has a `stable` row with `kind='workflow'`,
    `name == target_file` and `json.loads(payload)["edit"] == EDIT`.
  - `mini_ork.registries.version_registry.rollback("workflow", target_file, db=harness_db)`
    restores the original file bytes exactly.
- **A dry run never writes.** The same run with `MO_APPLY_DRY_RUN=1` leaves
  the live bytes unchanged, and the last stdout JSON line has `"version_id":""`.
- **The cost flip quarantines.** With the cost-flip numbers:
  - the decision is `quarantined`;
  - the rationale contains `harness-cost-regression`;
  - the live bytes are unchanged.
- **A refused edit is rejected and remembered.** Seed `change` as
  `{"op": "lane_pin", "node": "lint", "lane": "glm"}`.
  - `apply_run` records `rejected`, with a rationale containing
    `workflow edit refused` and `cannot pin a lane`. The stub is not called.
  - A second `apply_run` records `rejected` with a rationale containing `edit memory`.
- **apply_run itself checks the flag.** Without `MO_APPLY_HARNESS_WORKFLOW`,
  `apply_run` records `rejected`, with a rationale containing
  `workflow edits disabled`. The live bytes are unchanged.
- **The code scorer is refused.** `MO_APPLY_SCORER=code` with the flag on
  gives `rejected`, with a rationale containing `probe scorer`.
- **A stale base is quarantined.** Use a capturing stub that appends
  `"# drift\n"` to the live `workflow.yaml`, then returns the promote numbers.
  - The decision is `quarantined`, with a rationale containing `workflow-base-changed`.
  - The live file still ends with `# drift\n`, and its implementer lane is
    still `sonnet`.
- **The probe scorer copies the edited text.**
  - Setup: `monkeypatch.setattr(ps, "_ROOT", str(tmp_path))`, then call
    `ps._materialize_arm("fw_edit", str(wf_recipe/"workflow.yaml"), "BLOCK", 1, workflow_text="x: 1\n")`.
    It returns `(name, path)`.
  - `path == str(tmp_path/"recipes"/name/"workflow.yaml")`, its content is
    `"x: 1\n"`, and the original recipe file is unchanged.
  - The same call without `workflow_text` and with `target_file=None`
    returns `mutated is None`.
  - Remove the copies with `shutil.rmtree`.
- **The CLI resolves the workflow target.**
  - `main()` writes `MO_APPLY_DRY_RUN`, `MO_APPLY_SCORER`, `MINI_ORK_HOME`
    and `MINI_ORK_DB` straight into `os.environ`. So `monkeypatch.setenv`
    each of them to a tmp-safe value BEFORE calling it; teardown then
    restores them.
  - Monkeypatch `ap._resolve_root` to `str(tmp_path)` and `ap.apply_run` to a
    capturing lambda `(tc, kind, name, tfile, db=None)`, then call
    `ap.main(["--task-class", "fw_edit", "--target", T, "--dry-run"])`.
    It returns `0` with `kind == "workflow_node"` and `tfile` ending in
    `recipes/fw-edit/workflow.yaml`.
  - The target `"harness.nope.workflow.yaml"` returns `2`.
- **The helpers.**
  - `_is_workflow_target` is `True` for `T`. It is `False` for
    `"harness.fw-edit.implementer"`, `"harness..workflow.yaml"` and
    `"agent.x.prompt"`.
  - `_workflow_file_for` returns `""` when the recipe name mismatches.

## Verification commands

- `pytest -q tests/unit/test_apply_workflow_edit.py tests/unit/test_workflow_edit.py`
- `pytest -q tests/unit/test_apply_harness_targets.py tests/unit/test_cli_apply_py.py tests/unit/test_eco6_k7_apply_hygiene.py tests/unit/test_apply_significance.py`
- `pytest -q tests/unit/test_probe_scorer_control_arm.py tests/unit/test_probe_scorer_code_arm.py tests/unit/test_version_registry_py.py tests/unit/test_harness_operator_py.py`

## Self-application measurement

```measure
python3 -m pytest -q tests/unit/test_apply_workflow_edit.py --junitxml="${MINI_ORK_RUN_DIR}/apply-workflow-edit-junit.xml"
```

## Required artifacts — declare EXACTLY these and nothing else

- `${MINI_ORK_RUN_DIR}/apply-workflow-edit-junit.xml`

## Success criteria

- All three verification commands pass with every pre-existing test unchanged.
- `ruff check --select F,E9 mini_ork/cli/apply.py mini_ork/learning/probe_scorer.py` is clean.
- `git diff --stat` lists only the three files in scope.

## Rules

Edit files directly; do not emit unified diffs. Execute this spec exactly; do not
research and do not add lens or drafter nodes.
