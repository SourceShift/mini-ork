# Per-Feature Dispatcher

For each P0 feature in `feature-index.json`, write one child kickoff and run
the inner implementation recipe:

```bash
bin/mini-ork run recursive-validate-impl <child-kickoff.md>
```

Each child kickoff must include:

- The feature ID, title, source evidence, and dependencies.
- A scoped implementation request for only that feature.
- Concrete Definition of Done probes derived from the feature index.
- The arxiv-search-tool modern techniques references that justify the approach.
- A reminder that the child run must preserve unrelated user changes and keep
  its patch scoped.

Record each child dispatch as JSON under `${MINI_ORK_RUN_DIR}/child-runs/`.
Use this shape:

```json
{
  "feature_id": "stable-kebab-id",
  "child_kickoff": "path",
  "child_run_id": "run-id-or-null",
  "child_run_dir": "path-or-null",
  "status": "passed|failed|pending",
  "verdict_path": "path-or-null",
  "final_artifact_ref": "path-or-null",
  "files_written": []
}
```

Treat missing child run IDs, malformed verdicts, and incomplete child runs as
pending or failed. Never mark a feature passed unless the child verdict says
`pass: true`.
