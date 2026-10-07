# Learning themes — group 10k gradients into themes, split mini-ork self-notes from task lessons

Plan: `docs/plans/2026-10-07-learning-memory-page-refactor.md`, phase P2 (data work D1 + D2).

## Why

`gradient_records` holds 10,248 rows (8,370 at confidence ≥ 0.6, eligible for prompts) across
1,710 distinct targets. Most are the same few observations restated: e.g. "verifier_output for
this node is only {node_type: …}" appears in hundreds of paraphrases. 5,964 of the 10,248 (58%)
describe mini-ork's OWN trace fields (`verifier_output`, `tool_calls`, `files_read`,
`duration_ms`, `cost_usd`, `prompt_version_hash`, `context_bundle_hash`, `reward_*`,
`reviewer_verdict: recipe_fallback`, …). Those are bugs in mini-ork's tracing, not guidance for
the task, yet they can be injected into task prompts. Nobody can read 10k rows. The IDE needs
one row per idea.

This phase only BUILDS themes. It does not change what gets injected (a later phase does).

## Files in scope (touch ONLY these)

- `mini_ork/learning/themes.py` (new)
- `db/migrations/0063_lesson_themes.sql` (new). Follow the style of
  `db/migrations/0056_pattern_lesson_text.sql`; applied by `mini_ork/stores/migrate.py`.
- `mini_ork/cli/reflect.py`: ONLY a new side-channel block after gradient extraction/storage
  (same shape as the `pattern_induct` block at ~:297-320)
- `tests/unit/test_themes.py` (new)

Do NOT modify any other file.

## Schema (`0063_lesson_themes.sql`, additive, `CREATE TABLE IF NOT EXISTS`)

- `lesson_themes(theme_id TEXT PRIMARY KEY, kind TEXT NOT NULL CHECK(kind IN ('task','framework')),
  role TEXT, task_class TEXT, representative TEXT NOT NULL, centroid BLOB NOT NULL,
  n_gradients INTEGER NOT NULL DEFAULT 0, n_runs INTEGER NOT NULL DEFAULT 0, first_seen INTEGER,
  last_seen INTEGER, lesson_text TEXT, status TEXT NOT NULL DEFAULT 'candidate',
  bug_report_id INTEGER, updated_at INTEGER)`
- `gradient_theme(gradient_id TEXT PRIMARY KEY, theme_id TEXT NOT NULL, similarity REAL)`, plus
  an index on `theme_id`.

`themes.ensure_schema(db)` runs the same DDL idempotently. A database that hasn't run the
migration must still work: every entry point calls it first.

## `mini_ork/learning/themes.py` (exact behaviour)

1. `normalize(text) -> str`: lowercase; replace trace ids (`tr-[\w-]+`), hex ≥ 6, numbers,
   file paths and quoted JSON values with placeholders; collapse whitespace.
2. `classify_kind(target, signal, suggested_change) -> "framework" | "task"`: `framework` when the
   signal mentions mini-ork trace/telemetry fields. Use ONE module-level regex `FRAMEWORK_FIELDS`
   covering the list above plus `trace_id`, `execution_traces`, `run context`, `finish_reason`,
   `process_reward`, `node_type`. Otherwise `task`. Pure function, unit-tested.
3. `role_of(target) -> str`: the target family. `verifier.researcher` → `verifier`;
   `workflow.node.implementer` → `node:implementer`; `agent.reviewer.prompt` → `agent:reviewer`;
   `workflow.recipe.code_fix` → `recipe`; anything else → its first dotted segment.
4. Assignment = greedy leader clustering, deterministic, in `created_at, gradient_id` order. Embed
   `normalize(signal)` with `mini_ork.memory.semantic.get_embedder()`. Compare against existing
   theme centroids of the SAME kind. Join the best one if cosine ≥ `MO_THEME_SIM` (default
   **0.6**), else start a new theme. The centroid is the L2-normalized running mean (store packed
   float32). `theme_id` = `"th-" + sha256(kind + first member gradient_id)[:12]`. Update
   `n_gradients`, `first_seen` / `last_seen`; `n_runs` = distinct
   `execution_traces.run_id` over member `evidence`. `task_class` = the single class, or `"*"`
   when members span ≥ 3 classes. `role` = most common `role_of`. `representative` = the member
   signal closest to the centroid (refresh it when membership grows by ≥ 25%).
5. `assign_new(db) -> dict`: assigns gradients that have no `gradient_theme` row. Returns
   `{"assigned", "themes_new", "themes_total"}`.
6. `backfill(db, *, sim=None, dry_run=False) -> dict`: `assign_new` over everything, in batches
   of 1,000. Returns stats: themes per kind, themes with ≥ 3 members, the share of gradients
   covered by themes with ≥ 3 members, the top 10 themes by size, and wall time. With `dry_run`
   it computes in memory and writes nothing.
7. `rollup_framework_bugs(db, min_members=5) -> int`: for each `framework` theme with ≥
   `min_members` gradients, upsert ONE `bug_reports` row:
   - `fingerprint = "theme:" + theme_id`; `agent_role='learning'`; `observed_in = role`;
   - `title` = representative[:200];
   - `description` = `f"{n} gradients across {r} runs since {first_seen:%Y-%m-%d}."` plus up to 3
     distinct member quotes;
   - `suggested_fix` = the most common `suggested_change`[:2000]; `frequency = n`;
     `severity='medium'`; `confidence=0.8`; `status='open'` on insert; never overwrite a status a
     human changed;
   - `first_seen_at` / `last_seen_at` / `updated_at` epoch seconds.
   Store the bug id in `lesson_themes.bug_report_id`.
8. `python -m mini_ork.learning.themes {backfill [--sim X] [--dry-run], stats, rollup}` prints
   JSON. DB from `--db`, else `MINI_ORK_DB`, else `$MINI_ORK_HOME/state.db`.

## Reflect hook (`reflect.py`)

After gradients are stored for this pass: `themes.assign_new(db)` then
`themes.rollup_framework_bugs(db)`. Print one line:
`  [themes] assigned N gradient(s) → M theme(s) (K new), B framework bug(s) updated`.
Opt-out `MO_THEMES=0`. A side-channel must never crash reflect: catch, write
`[themes] skipped: <exc>` to stderr.

## Tests (`tests/unit/test_themes.py`, temp DB, no network)

- `classify_kind`: 6 real signals from the live DB (copy them verbatim into the test). 3 trace
  field complaints → framework; 3 prompt/code guidance → task.
- Paraphrases join one theme: "verifier_output for this node is only {node_type: researcher}" and
  "this node's verifier_output records only {node_type: planner}". An unrelated task lesson
  starts a new theme. Determinism: running twice gives identical theme ids.
- `assign_new` is incremental: second call assigns 0.
- `n_runs` counts distinct runs through `evidence → execution_traces.run_id`.
- `rollup_framework_bugs`: one row per qualifying theme; idempotent on a second call (frequency
  updated, no duplicate); a `status` a human set to `wontfix` stays.
- A DB without the migration works (`ensure_schema`).

## Verification command

The command that proves this run succeeded:

```bash
python3.11 -m pytest -q -p no:asyncio tests/unit/test_themes.py tests/unit/test_pattern_induction_py.py   # must exit 0
```

## Done when

- The verification command → 0 failed. Paste the summary line.
- `uvx ruff check mini_ork/learning/themes.py mini_ork/cli/reflect.py tests/unit/test_themes.py` → clean.
- Read-only proof on a COPY of the live DB (never write to the live one):
  `cp /Volumes/docker-ssd/ps/mini-ork/.mini-ork/state.db /tmp/themes-proof.db && python3.11 -m mini_ork.learning.themes backfill --dry-run --db /tmp/themes-proof.db --sim 0.5`
  then `--sim 0.6` and `--sim 0.7`. Paste the three stat blocks (themes per kind, ≥3-member
  coverage, top 10, wall time; must be < 10 min each). Recommend a default from them.
- `git diff --stat` touches only the files in scope.
