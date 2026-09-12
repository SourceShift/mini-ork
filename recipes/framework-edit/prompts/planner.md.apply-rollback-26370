# Planner Prompt

You are planning a routine mini-ork framework edit. Output strict JSON
matching the schema below — the mini-ork-plan validator parses your
output with `json.load()` and rejects anything that does not contain
a non-empty `verifier_contract.checks` array.

Inputs:
- Natural-language change request (kickoff body).
- Optional file-glob hint that narrows the affected subtree.
- Any explicit `scope_allow` override for high-blast-radius files.

Kickoff content:

```text
{{KICKOFF_CONTENT}}
```

## Required output shape

Emit ONE JSON object on stdout. No markdown wrapper, no fenced code
block around the JSON itself, no commentary before or after. Schema:

```json
{
  "objective": "one-sentence requested outcome",
  "assumptions": [
    "any kickoff assumption worth recording"
  ],
  "candidate_files": [
    "concrete paths or globs the implementer will edit or read"
  ],
  "out_of_scope": [
    "files explicitly excluded by the kickoff"
  ],
  "decomposition": [
    {
      "id": "implementer",
      "description": "produce the unified diff per the kickoff scope",
      "node_type": "implementer",
      "depends_on": []
    }
  ],
  "artifact_contract": {
    "outputs": [
      "${MINI_ORK_RUN_DIR}/framework-edit.diff"
    ]
  },
  "verifier_contract": {
    "checks": [
      {
        "id": "diff_exists",
        "description": "${MINI_ORK_RUN_DIR}/framework-edit.diff is non-empty"
      },
      {
        "id": "diff_applies",
        "description": "git apply --check succeeds against repo root"
      },
      {
        "id": "shell_syntax_clean",
        "description": "bash -n on every shell file in the diff"
      },
      {
        "id": "python_compile_clean",
        "description": "py_compile on every Python file in the diff"
      }
    ]
  },
  "risk_notes": [],
  "pass": true
}
```

## Rules

- **`verifier_contract.checks` MUST contain at least one item.** The
  validator rejects empty arrays with `missing_verifier_contract`.
  Add one check row per success criterion in the kickoff. When the
  kickoff lists explicit "verifier contract checks", port each one
  into a `checks[]` row with a stable id + the bash command text in
  the description.
- **`decomposition[].node_type` MUST be one of**:
  `planner | researcher | implementer | reviewer | verifier |
   reflector | publisher | rollback`. Anything else fails the
  D-008b validator.
- **No placeholder values.** Strings wrapped in `<>` (e.g.
  `"<TODO>"`) are rejected as `placeholder_plan`.
- **Do NOT add recipe-authoring nodes.** This is a code-edit recipe,
  not a drafter panel.
- **Do NOT wrap the JSON in a markdown fence** like
  ` ```json … ``` `. The validator reads stdin directly; a fence
  prefix makes `json.load()` raise.

## Binding artifact manifest

The framework requires the implementer to produce:

- `${MINI_ORK_RUN_DIR}/framework-edit.diff` — unified diff applying
  cleanly to the repo root via `git apply`.

The verifier nodes write `${MINI_ORK_RUN_DIR}/verdict.json` after
checks complete:

```json
{
  "files_changed": <int>,
  "tests_pass": <bool>,
  "static_pass": <bool>,
  "pass": <bool>
}
```

The implementer does NOT write `verdict.json` (D-13/2026 fix at
commit `ab781a9`); only verifiers do.
