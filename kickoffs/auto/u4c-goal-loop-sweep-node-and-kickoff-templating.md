# U4c — goal-loop: wire the sweep execution node + per-unit child kickoff templating

## Goal

The goal-loop recipe (U4a/U4b) plans fix children but NEVER SPAWNS them. `recipes/goal-loop/register.py:62` registers the spawn submode under `node_id="sweep_dispatcher"`, but that node is `type: transform` in `recipes/goal-loop/workflow.yaml` — and implementer submodes are only consulted inside `_handle_implementer` (`mini_ork/cli/execute_handlers.py:546`). A transform node routes to `_handle_transform`, so `sweep_run()` in `recipes/goal-loop/lib/drive.py` is dead code in the wave: `goal_check` re-evaluates an unchanged target and the outer driver kills the loop as `diverged:no_progress` after 2 waves. Additionally, every spawned child receives an IDENTICAL kickoff body (`MO_GOAL_CHILD_KICKOFF` is static), so a child cannot know which unit it must fix.

Fix both: add a real sweep execution node, and template per-unit fields into the child kickoff.

## Mechanisms (exact spec)

### 1. `recipes/goal-loop/workflow.yaml` — add the sweep node

Insert between `sweep_dispatcher` and `goal_check` in the `nodes:` list (list order drives serial dispatch):

```yaml
  - name: sweep
    type: implementer
    prompt_ref: null
    dispatch_mode: serial
```

No `outputs:` block (sweep-result.json is written by the submode script; the existing submode `results_artifact` mechanism handles it). Update `edges:` — replace `{ from: sweep_dispatcher, to: goal_check, edge_type: depends_on }` with:

```yaml
  - { from: sweep_dispatcher, to: sweep,      edge_type: depends_on }
  - { from: sweep,            to: goal_check, edge_type: depends_on }
```

### 2. `recipes/goal-loop/register.py` — re-key the submode

Change `node_id="sweep_dispatcher"` → `node_id="sweep"` in the `register_implementer_submode(...)` call. Update the module docstring sentence that names the submode key accordingly.

### 3. `recipes/goal-loop/lib/drive.py` — per-unit kickoff templating in `_default_spawn_fn`

Current behavior (lines 74-93): if `kickoff_text` is a file path it is passed to `spawn()` unchanged; otherwise the raw text is written to `run_dir/_inline_kickoff_{unit_id}.md`. Replace with:

- Resolve `kickoff_text` exactly as today (`kickoff_hint.get("kickoff") or MO_GOAL_CHILD_KICKOFF`, empty → ValueError as today).
- If `os.path.isfile(kickoff_text)`: `body = Path(kickoff_text).read_text(encoding="utf-8")`, else `body = kickoff_text`.
- Materialize per-unit fields: `unit = str(plan_entry.get("unit_id", "") or "")`, `reason = str((plan_entry.get("kickoff_hint") or {}).get("reason", "") or "")`; then `materialized = body.replace("{{unit_id}}", unit).replace("{{reason}}", reason)`.
- If `materialized != body` OR the source was NOT a file: write `materialized` to `run_dir/_inline_kickoff_<slug>.md` and pass that path to `spawn()`, where `slug = re.sub(r"[^A-Za-z0-9._-]", "_", unit)[:80] or "x"` (unit ids are often relative FILE PATHS containing `/` — the current f-string filename breaks on them). If the source was a file and contained no placeholders, keep passing the original file path unchanged (exact current behavior, zero regression).
- Add `import re` at module top.
- Keep `MO_GOAL_SPAWN_DRY=1` short-circuit exactly as-is (before templating).

### 4. `mini_ork/cli/execute_handlers.py` — interpreter pin for submode scripts

Line 558: `rc = subprocess.run(["python3", script]).returncode` → `rc = subprocess.run([sys.executable, script]).returncode`. Bare `python3` resolves to whatever is ambient (on operator machines a conda 3.9 without `mini_ork` installed); `sys.executable` is the venv interpreter the executor itself runs on, so submode scripts that `import mini_ork` (drive.py's `sweep_run` imports `mini_ork.cli.spawn`) keep working. `sys` is already imported in the module. Touch NOTHING else in this file.

## Tests (extend the two existing files, keep all existing tests green)

### `tests/unit/test_goal_loop_recipe.py`

- Workflow declares a node named `sweep` with `type: implementer` positioned between `sweep_dispatcher` and `goal_check` in the nodes list.
- Edges contain `sweep_dispatcher→sweep` and `sweep→goal_check`, and do NOT contain a direct `sweep_dispatcher→goal_check` edge.
- Loading `recipes/goal-loop/register.py` (file-path import, same pattern the file itself uses) results in `mini_ork.cli.execute_handlers._IMPLEMENTER_SUBMODES` containing key `("goal-loop", "sweep")` mapping to `("sweep-result.json", "goal-loop/lib/drive.py")` — and NOT key `("goal-loop", "sweep_dispatcher")`.

### `tests/unit/test_goal_loop_driver.py`

- Templating test: with `MO_GOAL_CHILD_KICKOFF` pointing at a template FILE containing `{{unit_id}}` and `{{reason}}`, monkeypatch `mini_ork.cli.spawn.spawn` to capture its `kickoff=` argument; call `drive_mod._default_spawn_fn({"unit_id": "docs/ch-01.md", "child_recipe": "code-fix", "kickoff_hint": {"unit_id": "docs/ch-01.md", "reason": "lens 04 failed"}})` with `MINI_ORK_RUN_DIR` set to a tmp dir. Assert: the captured kickoff path is under the tmp run dir, its FILENAME contains no `/` from the unit id (sanitized), and its body contains both `docs/ch-01.md` and `lens 04 failed` with no `{{` remaining.
- Passthrough test: a template file WITHOUT placeholders → `spawn` receives the ORIGINAL template path unchanged.
- Inline test: `MO_GOAL_CHILD_KICKOFF` set to a non-file inline body with placeholders → materialized file written under run dir, placeholders substituted.

## Files in scope

- recipes/goal-loop/workflow.yaml
- recipes/goal-loop/register.py
- recipes/goal-loop/lib/drive.py
- mini_ork/cli/execute_handlers.py (ONLY the one subprocess line in the submode branch)
- tests/unit/test_goal_loop_recipe.py
- tests/unit/test_goal_loop_driver.py

## Verification commands

- `python3.11 -m pytest tests/unit/test_goal_loop_driver.py tests/unit/test_goal_loop_recipe.py -q` → all green
- `python3.11 -m pytest tests/unit/test_mini_ork_execute_py.py -q` → no regression (execute_handlers touched)
- `python3.11 -c "import yaml,sys; wf=yaml.safe_load(open('recipes/goal-loop/workflow.yaml')); names=[n['name'] for n in wf['nodes']]; assert names.index('sweep_dispatcher') < names.index('sweep') < names.index('goal_check'), names; print('node order ok')"`

## Done When

- All three verification commands pass.
- `_IMPLEMENTER_SUBMODES` carries `("goal-loop", "sweep")` after register.py loads; the workflow has the `sweep` implementer node wired sweep_dispatcher→sweep→goal_check.
- `_default_spawn_fn` materializes `{{unit_id}}`/`{{reason}}` per unit with a filesystem-safe filename; file templates without placeholders pass through untouched.
- The submode subprocess uses `sys.executable`.
