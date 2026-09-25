# Kickoff: close one measured defect in mini-ork's own migration layer

## What `{{unit_id}}` is

{{unit_id}} is a defect in THIS repository, reproduced by an executable probe
against the tree you are editing. You are fixing the CODE that causes it. The
probe is re-run after you finish; it decides pass/fail, and it is not
negotiable.

There are two units and their fixes are unrelated — do only the one named
above.

## Goal

Make `{{unit_id}}`'s probe exit 0, honestly. The probe for each unit below runs
TWO checks: one that must go green, and one that must NOT change. Satisfying
the first by weakening the guard fails the second, and the unit stays open.

## Evidence

{{evidence}}

(One-line predicate reason: `{{reason}}`. Full evidence also at
`{{evidence_path}}`.)

## The traps — read before you edit

These are the shortcuts that make the obvious check go green without fixing
anything. The predicate is built to catch each of them.

### `migrate-drift-comment-only`

`mini-ork update` returns 1 on every db that applied the pre-edit
`db/migrations/0038_gradient_records.sql`, because commit `d6bc90a3` reworded
ONE COMMENT LINE in it. The reword bumped the file's sha256, `migrate_apply`
cannot tell a comment reword from a semantic edit, and it dies on the FIRST
drifted file — which is why `0054`/`0055`/`0056`/`0058` have sat unapplied ever
since. One root cause, five symptoms.

- **Trap: revert the comment.** Restoring the original bytes makes the checksum
  match again — and the probe still fails, because it feeds the guard a
  *comment-only variant* of the file and requires the guard to ACCEPT it. You
  must make the guard tolerant of comment-only differences, not make this one
  difference disappear.
- **Trap: neuter the guard.** Returning 0 on any mismatch would pass the accept
  probe and immediately fail its twin, which feeds the guard a real SQL edit and
  requires it to still be REFUSED. "Add a NEW migration instead of editing a
  shipped one" must remain true advice.
- **Trap: tell people to set `MO_MIGRATE_ALLOW_DRIFT=1`.** That is the guard
  being switched off wholesale, and a third check requires the upgrade path to
  come up drift-clean WITHOUT the escape hatch — i.e. the fix must carry the
  one-time repair itself.
- Read `mini_ork/stores/migrate.py::migrate_apply` and `ensure_table` before
  designing. Note `ensure_table`'s comment: the `schema_migrations` CREATE text
  is byte-parity-checked against `lib/migrate.sh`, so adding a column to that
  table is not free. `migrate_verify` and `migrate_status` are read-only and
  share the same comparison — keep them consistent with whatever you change.

### `migration-0054-drops-columns`

`db/migrations/0054_execution_traces_status_blocked.sql` rebuilds
`execution_traces` (create-copy-drop-rename) to widen the `status` CHECK to
admit `'blocked'`. Its column list omits two columns that exist on real
databases — `route_margin` and `predicted_error` — and adds nothing back. On an
upgrade path those columns already exist (0057/0059 added them), so the rebuild
DESTROYS them: 6401 rows, including the only two carrying a `route_margin`, and
`route_margin` is the entire training set the UCCI calibration map fits from.

- **Trap: name the two columns in the CREATE.** They are not there on a FRESH
  install — 0054 runs before 0057/0059 — so the INSERT's `SELECT route_margin`
  is a parse error and the migration rolls back. The probe runs a fresh install
  for exactly this reason; a fix that only holds where the columns already
  exist is not a fix.
- **Trap: add the columns to the new table unconditionally.** Then 0057's
  `ALTER TABLE execution_traces ADD COLUMN route_margin` fails with "duplicate
  column name" on a fresh install. 0057/0059 are shipped and already applied on
  live databases — they will not run again — but on a fresh install they run
  after 0054, unguarded.
- The column set genuinely depends on which migrations have already run, which
  a static SQL statement cannot express. `migrate.py`'s migration runner
  already supports the sqlite3 CLI dot-commands that shipped migrations use
  (`.read "|<cmd>"` executes a pipeline's stdout as SQL, with `MINI_ORK_DB` and
  `MINI_ORK_ROOT` exported) — see `0037`, `0041`, `0056` for working examples
  and `mini_ork/stores/migrate.py::_exec_statements` for the contract. One
  consequence to keep in mind: the statements you generate run inside 0054's
  transaction, so a second connection cannot see tables 0054 has just created.
- 0057's index on `route_margin` went down with the table it belonged to, and
  0057 is recorded as applied so it will not run again to recreate it. The
  replacement needs that index back.

## Scope

Edit only `mini_ork/stores/` and `db/migrations/` in the target worktree. Do
NOT touch the binding directory (`kickoffs/auto/miniork-self/**`), tests, or
anything else — the binding is the instrument scoring you and is protected.
Keep the diff minimal.

## Success criteria

- The predicate for `{{unit_id}}` exits 0, and every one of its other probes is
  still satisfied.
- The fix holds on BOTH paths where that is meaningful: a database upgrading
  from an older release, and a fresh install from empty.
- Minimal, reviewable diff.

## Model preference

`minimax` for the implementer (code lane — never glm).

## Verification (the outer loop runs it, not you)

Your patch is committed in the target worktree and the predicate is re-run
against it — it reads the worktree you edited, so your change is scored in
place. `unit_predicate.py` exits 0 only when the unit is genuinely closed. Your
job is the correct, minimal patch — nothing else.
