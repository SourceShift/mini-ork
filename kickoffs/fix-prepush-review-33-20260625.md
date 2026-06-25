# code-fix: resolve 3 pre-push review findings (review_id=33) blocking main

## Goal
Fix 3 code defects flagged by the Layer-3 pre-push reviewer (review_id=33) so the
squash on `main` passes a fresh review and can be pushed to public main. Each fix
is surgical and scoped to the named file. Do NOT refactor neighboring code. Do
NOT change learning weights, GRPO math, or decay knobs. Preserve all unrelated
behavior.

## Scope Hint — touch ONLY these 3 files
- `bin/mini-ork-execute`
- `db/migrations/0039_learning_column_repairs.sql`
- `recipes/mo-vs-omnigent/verifiers/source-completeness.sh`

## Fix 1 (HIGH) — learning writeback must not crash on a cold/partial DB
File: `bin/mini-ork-execute` (helper defs at lines ~304 and ~338; call site ~2718)

Root cause: at end of an otherwise-successful run (call site near line 2718),
`mo_learning_update_conductor_outcomes` and `mo_learning_write_grpo_advantages`
run by default (`MO_LEARNING_WRITEBACK=1`). Both Python helpers assume the newer
learning schema exists:
- `mo_learning_update_conductor_outcomes` (def ~304) wraps its body in
  `try: ... finally: con.close()` with **NO `except`** — a missing
  `conductor_decisions` or `epics` table raises `sqlite3.OperationalError` that
  propagates, Python exits non-zero, and bash `set -e` fails the whole run.
- `mo_learning_write_grpo_advantages` (def ~338) does
  `raise SystemExit("agent_performance_memory.relative_advantage is missing")`
  when the column/table is absent, and also reads `execution_traces`; a missing
  table raises `OperationalError`. Either way Python exits non-zero → run fails.

Do (in BOTH helpers, minimally):
- Wrap the schema-dependent body so that a missing learning table/column is a
  graceful no-op, NOT a crash. Concretely: catch `sqlite3.OperationalError`
  (and the absent-column / absent-table cases) inside each helper, `print(0)`,
  and return with exit code 0 so the caller sees success. Keep the existing
  happy-path behavior byte-for-byte when the schema IS present.
- For `mo_learning_update_conductor_outcomes`: add an `except
  sqlite3.OperationalError` arm to the existing `try` (before `finally`) that
  prints 0 and continues to a clean close. Do not swallow non-OperationalError
  exceptions silently — only the missing-schema class.
- For `mo_learning_write_grpo_advantages`: replace the bare
  `raise SystemExit(...)` for the missing `relative_advantage` column with a
  clean `print(0); sys.exit(0)` (cold-schema no-op), and guard the
  `execution_traces` read the same way (missing table → print 0, exit 0).
- Do NOT change the call site's redirection semantics beyond what's needed; the
  helpers themselves must be cold-DB-safe (per the reviewer). The existing
  `[ -f "$_db" ] || return 0` guards stay.

Verify: against a DB with NO `conductor_decisions` / `agent_performance_memory`
tables, sourcing the helpers and calling each returns 0 and prints `0` with no
traceback; against the full live schema, behavior and printed counts are
unchanged.

## Fix 2 (MEDIUM) — make 0039 self-safe even on direct (non-init.sh) application
File: `db/migrations/0039_learning_column_repairs.sql`

Root cause: 0039 creates indexes on `execution_traces(process_reward)` and
`agent_performance_memory(relative_advantage)`, but the column repair lives in
`db/init.sh` (`ensure_column`). A direct `sqlite3 DB < 0039.sql` outside the
init path (or any runner that skips init.sh) fails with `no such column` because
the columns may not exist yet.

Do: make the migration resilient on its own. Pure-SQL SQLite cannot do
conditional DDL, so guard each index so a missing base column degrades to a
no-op instead of an error. Acceptable approaches (pick the one that keeps the
init.sh-guaranteed happy path identical):
- Prefer: wrap the two `CREATE INDEX IF NOT EXISTS` statements so they only run
  when the target column exists — e.g. emit them via a `SELECT ... WHERE EXISTS
  (SELECT 1 FROM pragma_table_info('<table>') WHERE name='<col>')`-gated path,
  or convert 0039 to a small guarded block the migration runner executes such
  that absence of the column is a clean skip, not a hard `no such column` abort.
- Keep the two indexes' names and definitions unchanged when the columns exist.
- Update the header comment to state 0039 is now self-guarding: it creates the
  repair indexes only when the columns are present (init.sh still guarantees
  presence on the normal path; this just makes direct application safe).

Constraint: on the normal `db/init.sh` path (columns already added by
`ensure_column`), 0039 must still create BOTH indexes exactly as today, and a
re-run must not error (idempotent).

## Fix 3 (MEDIUM) — verifier must serialize missing entries as real JSON
File: `recipes/mo-vs-omnigent/verifiers/source-completeness.sh` (around line 76)

Root cause: the failing-path JSON builds the `missing` array from a
shell-expanded string fed to Python `.split()`, so an entry like
`lens-glm.md (only 0 sources, need ≥5)` (spaces + the unicode `≥`) is shattered
into many bogus array elements. Consumers get malformed diagnostics and cannot
identify which artifacts are missing.

Do: serialize the missing entries safely so each entry is exactly one JSON array
element, preserving spaces and unicode. Pass each missing item as a separate
argv to Python and emit `json.dumps(sys.argv[1:])`, OR build the array with
`jq`. Do not split on whitespace. Keep the surrounding verifier verdict logic
and output shape (same JSON keys) unchanged — only the construction of the
`missing` array changes.

Verify: a missing entry containing spaces and a `≥` character round-trips as a
single element in the emitted JSON array (valid JSON, one element).

## Requirements
- Touch only the 3 listed files. No new deps. Keep diffs minimal.
- Do NOT change learning weights, GRPO decay knobs (MO_LEARNING_DECAY_ALPHA,
  MO_LEARNING_HALFLIFE_DAYS), PRM weights, or ACTIVITY_CAP.
- Happy-path behavior (full schema present) must be unchanged for all 3 fixes.

## Done When
- All 3 files edited per the fixes above.
- `bash -n bin/mini-ork-execute recipes/mo-vs-omnigent/verifiers/source-completeness.sh` pass.
- On a schema-less scratch DB, both learning helpers return 0 and print `0` with
  no Python traceback; on the full schema their counts are unchanged.
- `verdict.json` written with pass:true summarizing the 3 fixes.
