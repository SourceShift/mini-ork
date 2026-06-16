# Framework Edit: Close synthesis → learning_record → pattern_records pipeline

## Goal

Wire two missing populators in the recursive-self-improve loop so that
`learning_record` accumulates from every successful self-improve iter, and
`pattern_records` accumulates from execution_traces clusters.

Source of truth: `.mini-ork/runs/self-improve-iter-34-20260609115529/synthesis.md`
ranked patches **#4 (auto-promote synthesis findings)** and
**#5 (pattern_miner gated on `MO_PATTERN_MINER=1`)**.

Today both tables are empty despite 1115 execution_traces and 54 self_improve_runs.
The loop's own iter-34 synthesis flags this as the highest-leverage architectural gap.

## Scope Hint

- `bin/mini-ork-self-improve` (add `_promote_synthesis_findings`)
- `lib/pattern_store.sh` (already has writer at line 99 — no change here)
- `bin/mini-ork-reflect` (add invocation of pattern miner gated by env var)

## Expected Edit

**Patch #4 — `_promote_synthesis_findings` in `bin/mini-ork-self-improve`:**

1. Parse `${RUN_DIR}/synthesis.md` ranked patch table (markdown table with
   columns Rank | Bottleneck | Category | Patch summary | Evidence | Confidence).
2. For each row, upsert into `learning_record` keyed by `(run_id, iter, rank, title)`:
   - `category` from synthesis row's Category column
   - `title` from Bottleneck column (truncated to 200 chars)
   - `evidence_paths` JSON array from Evidence column file refs
   - `arxiv_refs` JSON array from Evidence column arXiv IDs
   - `patch_summary` from Patch summary column
   - `confidence` from Confidence column
   - `severity`: high if confidence ≥ 0.85, medium if ≥ 0.7, else low
   - `outcome`: `'open'` initially
3. Idempotent: re-running on the same synthesis.md must not duplicate rows.
4. Called from `_self_improve_record_success` (existing function, around L481).

**Patch #5 — `pattern_miner` invocation in `bin/mini-ork-reflect`:**

1. Source `lib/pattern_store.sh` (writer already exists at L99).
2. After existing gradient extraction, conditionally invoke pattern mining:
   ```sh
   if [ "${MO_PATTERN_MINER:-0}" = "1" ]; then
     pattern_store_mine_from_traces --window 7d --min-cluster 3
   fi
   ```
3. Function `pattern_store_mine_from_traces` to be added to `lib/pattern_store.sh`:
   group `execution_traces` rows by `(task_class, status)` over the window;
   when a cluster has ≥ `min_cluster` rows, upsert a `pattern_records` row with
   `evidence_trace_ids` JSON array and `frequency` count.
4. Gated by env var to be safe — default off; enable in burn-in (D4).

## Requirements

- Do NOT modify `.mini-ork/config/**`.
- Do NOT modify the existing `lib/pattern_store.sh:99` writer.
- Both patches must be opt-in safe: patch #5 gated by `MO_PATTERN_MINER=1`;
  patch #4 only runs on successful self-improve iters (no behavior change on
  failed/aborted/rejected iters).
- Schema is already in place. No migration needed.
- Add a unit test under `tests/unit/test_promote_synthesis_findings.sh`
  feeding a fixture synthesis.md and asserting `learning_record` rows are
  inserted idempotently.
- Add a unit test under `tests/unit/test_pattern_miner.sh` seeding
  `execution_traces` with a known cluster and asserting `pattern_records`
  upsert.

## Done When

- `${MINI_ORK_RUN_DIR}/framework-edit.diff` contains the proposed patch covering
  `bin/mini-ork-self-improve`, `bin/mini-ork-reflect`, `lib/pattern_store.sh`,
  and the two new test files.
- `${MINI_ORK_RUN_DIR}/verdict.json` contains:
  `{ "files_changed": 5, "tests_pass": true, "static_pass": true, "pass": true }`
- Static checks (shellcheck) pass in the isolated worktree.
- Both new unit tests pass.

## Verification commands

- `shellcheck bin/mini-ork-self-improve bin/mini-ork-reflect lib/pattern_store.sh`
- `bash tests/unit/test_promote_synthesis_findings.sh`
- `bash tests/unit/test_pattern_miner.sh`

## Out of Scope

- Patch #1 (llm_calls producer) — separate dispatch.
- Patch #2/#3 (trace_store cost/duration + silent-fail wrapper) — separate dispatch (D2).
- task_memory / failure_memory writers — separate dispatch (D3).
