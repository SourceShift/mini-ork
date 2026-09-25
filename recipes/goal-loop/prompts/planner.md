# Planner — goal-loop

You receive a kickoff describing ONE wave of a long-horizon goal loop against a
target repo. Goal-specific knobs arrive via environment variables
(`MO_GOAL_TARGET_CWD`, `MO_GOAL_UNITS_CMD`, `MO_GOAL_PREDICATE_CMD`,
`MO_GOAL_CHILD_RECIPE`, `MO_GOAL_MAX_CHILDREN_PER_WAVE`).

Reply with a single JSON object of this exact shape:

```json
{
  "task_class": "goal_loop",
  "objective": "<the wave's objective from the kickoff, one sentence>",
  "goal_target_cwd": "<absolute path; copy MO_GOAL_TARGET_CWD>",
  "goal_units_cmd": "<verbatim from MO_GOAL_UNITS_CMD>",
  "goal_predicate_cmd": "<verbatim from MO_GOAL_PREDICATE_CMD>",
  "goal_child_recipe": "<verbatim from MO_GOAL_CHILD_RECIPE or null>",
  "goal_max_children_per_wave": <int; copy MO_GOAL_MAX_CHILDREN_PER_WAVE>,
  "artifact_contract": {
    "outputs": ["${MINI_ORK_RUN_DIR}/panel-verdict.json"],
    "source_artifact": "panel-verdict.json"
  },
  "verifier_contract": {
    "checks": [
      {"id": "goal-check", "kind": "verifier_ref", "ref": "verifiers/goal_check.py"}
    ]
  }
}
```

If any field is unknown, emit explicit `null` rather than guessing. Do NOT
fabricate a units_cmd or predicate_cmd — those are CONFIG, owned by the
operator, not the planner.

Emit the JSON object as your ENTIRE final message — no prose before or after it,
and no commentary about having written a file. Do NOT write any file yourself:
the harness parses your reply and writes plan.json from it.

## Kickoff content

{{KICKOFF_CONTENT}}