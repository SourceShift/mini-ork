# Planner Prompt — epic-runner

You are the planner for a multi-epic delivery recipe.

Inputs:
- `MINI_ORK_EPIC_DOC` — path to the multi-epic markdown doc.
- `MINI_ORK_EPIC_TARGET_REPO` — target repository the epics mutate.
- `MINI_ORK_EPIC_PUBLISH` — `true`/`false`; whether child runs may publish.
- `MINI_ORK_EPIC_VERIFIER_SCRIPT` — optional path to an operator-supplied verifier.

Kickoff content:
```text
{{KICKOFF_CONTENT}}
```

Emit a single JSON file `${MINI_ORK_RUN_DIR}/epic-runner-plan.json` with this
strict schema (no optional keys; use defaults where a value is unknown):

```json
{
  "recipe_name": "epic-runner",
  "task_class": "epic_runner_delivery",
  "derived_task_class": "epic_runner_delivery",
  "objective": "one-sentence delivery objective",
  "epic_doc": "$MINI_ORK_EPIC_DOC",
  "target_repo": "$MINI_ORK_EPIC_TARGET_REPO",
  "publish_enabled": false,
  "verifier_script": "",
  "epics": [
    {
      "id": "kebab-case-id",
      "title": "human title",
      "description": "one paragraph",
      "depends_on": [],
      "framework_edit_kickoff": "verbatim kickoff text for child run",
      "scope_allow": []
    }
  ],
  "waves": [
    ["epic-id-1"],
    ["epic-id-2", "epic-id-3"]
  ],
  "verdict_schema": {
    "pass": false,
    "epics_total": 0,
    "epics_passed": 0,
    "waves_total": 0,
    "final_artifact_ref": ""
  }
}
```

Rules:
1. Parse the epic doc and build a directed acyclic graph from `depends_on`.
2. Compute topological waves: wave 0 contains all epics with no dependencies;
   wave N contains epics whose dependencies all appear in waves < N.
3. Every epic must have a `framework_edit_kickoff` field suitable for passing to
   `bin/mini-ork run framework-edit`.
4. Do NOT emit planning nodes, arxiv/prior-art nodes, drafters, or any
   meta-recipe machinery. The generated recipe has exactly 8 nodes:
   planner, epic_dispatcher, wave_aggregator, epic_verifier, final_reviewer,
   publisher, rollback, reflector.
5. Write the `epic-runner-plan.json` file above.
6. **ALSO emit a SECOND file `${MINI_ORK_RUN_DIR}/plan.json`** to satisfy
   the framework's universal D-015 plan-gate (`bin/mini-ork-plan` requires
   `verifier_contract.checks` to be non-empty on every plan, including
   multi-epic recipes). Use this minimal shape — it composes with
   `epic-runner-plan.json` rather than duplicating it:

   ```json
   {
     "recipe_name": "epic-runner",
     "task_class": "epic_runner_delivery",
     "objective": "<copy from epic-runner-plan.json>",
     "decomposition": [
       { "step_number": 1, "description": "Dispatch each wave's epics as child framework-edit runs, wait for all to terminate before advancing.", "files": [] }
     ],
     "verifier_contract": {
       "checks": [
         {
           "id": "epic-graph-complete",
           "description": "Deterministic schema gate — every declared epic appears in epic-results.json with passed/failed/skipped status and dependency order is respected.",
           "command": "bash recipes/epic-runner/verifiers/epic-graph-complete.sh"
         },
         {
           "id": "epic-runner-delivery-pass",
           "description": "epic-runner-delivery.json reports pass=true after final_reviewer.",
           "command": "jq -e '.pass == true' \"$MINI_ORK_RUN_DIR/epic-runner-delivery.json\""
         }
       ]
     }
   }
   ```

7. Write BOTH `epic-runner-plan.json` and `plan.json`. No other files.
