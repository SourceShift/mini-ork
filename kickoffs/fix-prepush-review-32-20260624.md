# code-fix: resolve 7 pre-push review findings (review_id=32) blocking main

## Goal
Fix 7 code defects flagged by the Layer-3 pre-push reviewer so the squash
commit on `main` passes a fresh review. Each fix is surgical and scoped to the
named file(s). Do NOT refactor neighboring code. Preserve all unrelated
behavior.

## Scope Hint — touch ONLY these 6 files
- `db/migrations/0039_learning_column_repairs.sql`
- `db/init.sh`
- `lib/process_reward.sh`
- `recipes/doc-to-features-loop/lib/per_feature_dispatcher.py`
- `scripts/learning-loop-live-validate.sh`
- `bin/mini-ork`

## Fix 1 (CRITICAL) — real column repair, drop writable_schema hack
File: `db/migrations/0039_learning_column_repairs.sql` + `db/init.sh`

Root cause: 0039 rewrites `sqlite_schema` CREATE TABLE *text* via
`PRAGMA writable_schema=ON`. That never creates real column storage, and the
same sqlite3 session's follow-up `CREATE INDEX ... ON execution_traces(process_reward)`
sees the old schema and aborts → blocks every later migration.

Do:
- In `db/init.sh`, BEFORE the migration loop (after the WAL pragma line near
  line 42), add a guarded real column repair using `pragma_table_info`:
  for `execution_traces.process_reward` (REAL DEFAULT NULL) and
  `agent_performance_memory.relative_advantage` (REAL NOT NULL DEFAULT 0.0),
  only when the table exists AND the column is missing, run
  `ALTER TABLE <t> ADD COLUMN <c> ...`. This adds real storage, version-safe.
- Rewrite `0039_*.sql` to remove the entire `writable_schema` block. Keep ONLY
  the two `CREATE INDEX IF NOT EXISTS` statements (they are now safe because
  init.sh guarantees the columns exist before migrations run). Keep the header
  comment but update it to describe the new init.sh-guarded approach.

Verify: on a DB where the columns already exist, init re-runs cleanly (no
"duplicate column" error, since the ALTER is guarded by pragma_table_info).

## Fix 2 (HIGH) — never fabricate APPROVE; never pass on non-zero child exit
File: `recipes/doc-to-features-loop/lib/per_feature_dispatcher.py`

- `_write_deterministic_panel_fallback`: gate the synthetic APPROVE behind an
  explicit opt-in env `MO_TIER4_DETERMINISTIC_FALLBACK`. When it is not "1",
  return `None` (no fabrication) so a missing tier4_synth verdict stays visible.
- `_dispatch_feature`: remove the `fallback_pass` branch that accepts a
  non-zero child exit as success. Status must be PASSED only when
  `verdict.get("pass") is True AND proc.returncode == 0`. A non-zero child exit
  is never PASSED.

## Fix 3 (HIGH) — validate feature.id before it reaches paths/kickoff
File: `recipes/doc-to-features-loop/lib/per_feature_dispatcher.py`

Root cause: `fid` flows into filesystem paths (`child_run_id`, `kickoff_path`)
and the kickoff markdown. A malicious/garbled id enables path traversal.
Add a helper `_safe_feature_id(fid)` that raises `ValueError` unless `fid`
matches `^[A-Za-z0-9._-]{1,128}$`. Call it at the top of `_dispatch_feature`
(where `fid` is first derived) before any path is built. (`_success_command`
already returns only static templates — do not interpolate id/title into shell.)

## Fix 4 (HIGH) — remove broken same-family decontamination gate
File: `lib/process_reward.sh` (BOTH copies: `prm_score_trace` AND `prm_backfill`)

Root cause: the `+0.15` verdict term is zeroed by `_same_family(agent_version_id)`,
but that keys on the DOER's family, not the reviewer's (the schema has no
reviewer_model column). Lanes whose id contains opus/minimax/glm/kimi lose the
credit while codex/sonnet lanes keep it → asymmetric GRPO bias.

Do: in BOTH Python copies, delete the `and not _same_family(...)` clause from
the verdict gate so the term applies symmetrically:
`if status_success and v in {approve set}: score += W_VERDICT`. Remove the now
unused `_FAMILY_TOKENS` and `_same_family` definitions in both copies. Update
the header/inline comments to state same-family decontamination is removed
pending a real reviewer_model column. The two copies MUST stay byte-identical.

## Fix 5 (HIGH) — flag activity-cap state for backfill comparability
File: `lib/process_reward.sh`

Root cause: capped vs legacy-uncapped scores mix in GRPO groups. Minimal code
fix: in BOTH copies, emit a single `print(..., file=sys.stderr)` line once per
invocation noting `MO_PRM_ACTIVITY_CAP` state (e.g.
`[prm] activity_cap=on|off`), so legacy/new mixing is visible. (Operational
full re-backfill is run separately by the operator; no scoring-weight change.)

## Fix 6 (HIGH) — inspect mode must not mutate the live DB
File: `scripts/learning-loop-live-validate.sh`

Root cause: the `else` (inspect-only) branch calls `fire_writers`, which runs
`mo_learning_write_grpo_advantages` + `mo_learning_update_conductor_outcomes`
against the live DB. Do: remove the `fire_writers` call from the inspect
branch; update its echo to say it is read-only (router probe is read-only).
`fire_writers` stays in the `MO_VALIDATE_DO_RUNS=1` dispatch loop only.

## Fix 7 (MEDIUM) — anchor make-regex to verifier targets
File: `bin/mini-ork`

Root cause: `r"\bmake\s+[A-Za-z0-9_.:-]+..."` matches `make build`/`make deploy`,
mis-classifying build targets as verification commands. Change the make pattern
so the first target must start with a verifier keyword
(`test|check|verify|smoke|probe|coverage|lint|ci`), e.g.
`r"\bmake\s+(?:test|check|verify|smoke|probe|coverage|lint|ci)[A-Za-z0-9_.:-]*(?:\s+&&\s+make\s+[A-Za-z0-9_.:-]+)*[^\n\x60]*"`.
Keep the rest of the pattern list unchanged.

## Requirements
- Touch only the 6 listed files. No new deps. Keep diffs minimal.
- The two PRM heuristic copies in `lib/process_reward.sh` stay byte-identical.
- Do not change PRM weight values (W_STATUS etc.) or the ACTIVITY_CAP value.

## Done When
- All 6 files edited per the fixes above; no writable_schema in 0039.
- `bash -n bin/mini-ork db/init.sh scripts/learning-loop-live-validate.sh` pass.
- `python3 -c "import ast,sys; ast.parse(open('recipes/doc-to-features-loop/lib/per_feature_dispatcher.py').read())"` passes.
- `verdict.json` written with pass:true summarizing the 7 fixes.
