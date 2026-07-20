# obs-smoke kickoff

Trigger phrase: `obs-smoke`. The two LLM nodes have deterministic
prompts that produce ~30s wall time and ~$0.05-$0.15 in tokens.

## Success criteria

- `lens-tiny.md` exists with ≥4 lines
- `review-tiny_reviewer.json` exists with `{"verdict": "pass"}`
- `verifier-result-lens-exists.json` shows `pass=true`

## In scope

- Run the obs-smoke recipe nodes (tiny_lens researcher, tiny_reviewer,
  lens_exists verifier, publisher) end to end
- Write artifacts only under `.mini-ork/runs/<run_id>/`
- Confirm telemetry rows land in llm_calls / run_events / task_runs

## Verify

Run: bash tests/test_obs_surface.sh

## Why this exists

This recipe is the **canonical observability regression suite**. After
any change to `mini_ork/ported/mini_ork_execute.py`, `lib/llm-dispatch.sh`, or any
emit point, re-run this and `tests/test_obs_surface.sh` to confirm
every surface still populates:

- `task_runs.trace_id` not NULL
- `llm_calls` rows: ≥2 (researcher + reviewer); each carrying
  `metadata_json.node_id` matching a recipe node, `traceparent`
  embedding `task_runs.trace_id`, `session_id` populated for
  stream-json paths, `input_tokens` + `output_tokens` non-zero
- `run_events`: ≥8 rows (node_start + node_end × 4 nodes)
- Filesystem: `lens-tiny.md`, `review-tiny_reviewer.json`,
  `verifier-result-lens-exists.json`, `execute.log`
