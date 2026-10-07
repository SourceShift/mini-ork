# Learning themes — revision 2 (Opus review of run learn-themes-20261007124729)

WIP commit a8e8df54 holds revision 1. Its schema, migration 0063, the reflect hook, the bug
roll-up and the CLI shape are fine; keep them. Opus returned `needs_revision`. The clustering
itself fails on accuracy, speed and proof. Fix ONLY the list below.

## Files in scope

- `mini_ork/learning/themes.py`, `tests/unit/test_themes.py`
- `mini_ork/cli/reflect.py` (only the themes block, if a signature changes)
- `db/migrations/0063_lesson_themes.sql`: only if the centroid storage must change (see 3)

Do NOT modify any other file.

## Fixes (exact)

1. **`FRAMEWORK_FIELDS` must match the kickoff, no more and no less.**
   - Drop `target` and `gradient_id`. "target" is ordinary English; it made 162 live rows
     `framework` by accident.
   - Match `reward_\w+` and `reward_g` explicitly. Note: `\breward_` fails before a word char.
   - The list is: `verifier_output`, `tool_calls`, `files_read`, `files_written`, `duration_ms`,
     `cost_usd`, `prompt_version_hash`, `context_bundle_hash`, `reward_\w+`, `process_reward`,
     `reviewer_verdict` with `recipe_fallback`, `trace_id`, `execution_traces`, `run context`,
     `finish_reason`, `node_type`.
   - Report the live framework share in the proof. The expectation is near the measured 58%; if
     it isn't, say which fields drive the difference.
2. **Real test signals.** `classify_kind` tests use 6 signals copied verbatim from the live
   `gradient_records` (3 framework, 3 task), selected by a query you paste into the test's
   docstring. Add `classify_kind('', 'reward_score missing', '') == 'framework'` and a task signal
   containing "the target file" → `task`.
3. **Replace hashed embeddings with TF-IDF and an inverted index.** The default `HashEmbedder`
   counts every token equally, so stopwords dominate and the kickoff's paraphrases only join at
   0.3. Implement:
   - Tokens = `normalize(text)` split on non-word, lowercased, with English stopwords (a ~120-word
     module constant) and tokens < 3 chars dropped.
   - IDF computed once over all gradient signals at backfill start; `assign_new` reuses IDF
     persisted in a small `theme_idf(token PRIMARY KEY, df INTEGER)` table plus a `meta` row with
     N. Rebuild IDF when N has grown by ≥ 25%. Add the table to migration 0063 (still
     unreleased).
   - A theme's vector = a sparse dict of the top 40 TF-IDF tokens (L2-normalized), stored as JSON
     in `centroid` (TEXT/BLOB both fine).
   - Candidates for a gradient = themes sharing any of its 8 highest-IDF tokens (an in-memory
     inverted index `token → {theme_id}` built once per call from stored centroids). Compute
     cosine only against those candidates, at most 50, by shared-token count.
   - Default `MO_THEME_SIM` = whatever the proof below supports (start the search at 0.35).
   - The kickoff's paraphrase pair must join at the chosen default, and the unrelated task lesson
     must not. Tests run at the default, not at a hand-picked low threshold.
4. **Speed.** `assign_new` with nothing to assign must return in < 0.5 s on the live-sized DB (it
   runs every reflect). Only re-derive `representative` / centroid for themes touched in this
   call, and only when membership grew by ≥ 25% since the last refresh (store
   `n_at_refresh`). A full backfill of all ~10.3k gradients must take < 2 min.
5. **Dry-run writes nothing to disk.** Use `sqlite3.connect(src).backup(mem)` into `:memory:`.
   The backup includes the WAL, and nothing is left in `$TMPDIR`. Process in batches of 1,000 as
   the kickoff said. Fix the `--dry-run` help text and the two false docstrings (`:37`, `:130`)
   that claim paraphrases get identical embeddings.
6. **Required proof.** Three dry-run stat blocks on the live DB (`--db
   /Volumes/docker-ssd/ps/mini-ork/.mini-ork/state.db --dry-run`, read-only through backup) at
   `--sim` 0.30 / 0.35 / 0.45: themes per kind, themes with ≥ 3 members, share of gradients
   covered by themes with ≥ 3 members, top 10 themes (representative[:100] + size), and wall
   time. Pick and set the default. Also print 5 random member pairs from one large theme so a
   reader can see they really are the same idea.

## Verification command

The command that proves this run succeeded:

```bash
python3.11 -m pytest -q -p no:asyncio tests/unit/test_themes.py tests/unit/test_pattern_induction_py.py   # must exit 0
```

## Done when

- The verification command → 0 failed. Paste the summary line.
- `uvx ruff check mini_ork/learning/themes.py mini_ork/cli/reflect.py tests/unit/test_themes.py` → clean.
- The proof in fix 6 is pasted. No `themes-dryrun-*` file exists in `$TMPDIR` afterwards.
- `git diff a8e8df54 --stat` touches only the files in scope.
