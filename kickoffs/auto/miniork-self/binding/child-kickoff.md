# Kickoff: close one measured defect in mini-ork itself

## What `{{unit_id}}` is

{{unit_id}} is a defect in THIS repository, reproduced by an executable probe
against the tree you are editing. You are fixing the CODE that causes it. The
probe is re-run after you finish; it decides pass/fail, and it is not
negotiable.

Five units are listed below and their fixes are unrelated — do only the one
named above. Read its section, not the others.

## Goal

Make `{{unit_id}}`'s probe exit 0, honestly. Each unit's probe runs SEVERAL
checks: one or more that must go green, and at least one that must NOT change —
a control that feeds the guard the thing it exists to refuse. Satisfying the
green checks by weakening or removing the control fails it, and the unit stays
open.

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
- **Trap: collapse all whitespace to compare.** The obvious canonical form —
  strip comments, then `re.sub(r"\s+", " ", s)` over the whole file — also
  collapses whitespace INSIDE string literals, so `VALUES ('a  b')` and
  `VALUES ('a b')` compare equal and a real data edit is re-baselined in
  silence. A fourth probe writes exactly that pair and requires the edit to be
  REFUSED. Normalize whitespace in the SQL *syntax* only; the bytes inside
  quotes are data.
- **Trap: normalize so hard that a real edit passes.** The same fourth probe's
  twin requires a genuine SQL change to still be detected. Any normalization you
  add needs a control showing what it must still reject.
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

### `migration-order-is-reported`

`migrate_apply` walks `sorted(dir.glob("*.sql"))`. A file absent from
`schema_migrations` is simply "pending", so applying a migration that sorts
BEFORE one already applied is indistinguishable from a normal apply. On the live
db that already happened: `0057` and `0059` are applied, `0054` is not, and
running `mini-ork update` rebuilds `execution_traces` from a column list written
when `route_margin` did not exist — dropping the columns the later migrations
added, with nothing reported beforehand.

- **Trap: make the out-of-order apply FAIL.** Refusing it is the wrong answer
  here, and it breaks a second unit: the live-path probe for
  `migration-0054-drops-columns` requires the pending `0054` to apply
  successfully (rc 0) on an upgrading db. The requirement is a WARNING — the
  fact must reach the operator — with the apply still succeeding.
- **Trap: print the warning to stdout.** The migration-verdict path in the probe
  captures ONLY the err channel; the runner's routine `  [apply] <file>` stdout
  is not read. A warning on stdout satisfies nothing.
- **Trap: warn whenever a migration is pending.** A fresh install has every
  migration pending and applies them in order — that is normal, not drift. One
  probe runs a fresh db and requires the message NOT to name `0054`; a warning
  keyed on "is pending" fails it.
- **Trap: compare against the max applied filename.** View files (`v_*.sql`)
  sort AFTER `0*.sql`, so a db whose newest applied row is a view file has a
  "max applied" that no numbered migration precedes. The invariant that actually
  holds is: the pending set must be a SUFFIX of the sorted list. Anything
  pending that sorts before an applied file is the signal.
- Keep `migrate_verify` / `migrate_status` consistent — they share the walk.

### `reflect-step-is-bounded`

`mini_ork/cli/main.py` spawns the reflect child with NO `timeout=`, and
`mini_ork/cli/execute_handlers.py::_handle_reflector_early` does the same with
its own hand-built environment. Reflect runs AFTER the verdict is already final
and takes seconds, so a lane that never returns holds `mini-ork run` open
forever with nothing left to decide. The campaign launcher already carries a
shell watchdog that kills reflect processes past six minutes — that is a fix
living in a shell script instead of in the code that spawns the child.

- **Trap: bound only the site the message names.** Both spawn sites are checked,
  in both files. The message names the ones that were red when the probe was
  written; a patch that fixes one and leaves the other still fails.
- **Trap: pass `timeout=None`, or a negative/zero value.** Not a bound. The
  probe requires a positive numeric literal.
- **Trap: delete the reflect step, or flip `MO_AUTO_REFLECT`'s default.** A
  second probe requires reflection to still be ON by default — bounding the step
  by removing it, or by turning it off for everyone, satisfies nothing.
- **Trap: bound it in the launcher instead.** The shell watchdog already exists;
  a bound that lives only there is exactly the defect.
- Reflect is best-effort: a timeout must not turn a finished run into a failed
  one. Let the step be killed and the run continue to its already-decided
  verdict.

### `module-child-engine-pin`

A `python -m mini_ork.cli.*` child puts its WORKING DIRECTORY at `sys.path[0]`,
ahead of the `PYTHONPATH` that names the engine. The goal-loop runs children with
cwd set to the repo under repair (`MO_GOAL_TARGET_CWD`), so a repo that contains
a `mini_ork/` tree silently shadows the engine: the child executes the target's
stale copy. This already cost a run — the plan child wrote a rehearsal
placeholder over the live run's own `plan.json`, so every child was planned
against `"<dry-run: not generated>"` and a fix merged to `main` stayed inert
inside the running loop.

- **Trap: patch only the spawn the probe names.** The probe scans EVERY spawn of
  a `mini_ork.cli.*` module across `mini_ork/`. Several of them already route
  through one shared environment helper; fix that helper and they are all
  covered, which is the point — a per-site patch leaves the next site to
  reintroduce the hole.
- **Trap: append PYTHONPATH, or reorder it.** The cwd entry is ahead of
  `PYTHONPATH` by definition for `-m`/`-c`, so no PYTHONPATH ordering fixes it.
  Drop the implicit cwd entry (Python 3.11+ `PYTHONSAFEPATH`, or an equivalent
  that achieves the same), so the declared path resolves.
- **Trap: stop the child importing `mini_ork`.** The probe's control requires
  the SAME child, under the SAME env minus the pin, to STILL load a decoy
  package planted in the cwd. Removing the import does not satisfy that control;
  it fails it.
- **Trap: change the child's cwd.** The cwd must stay the target tree — that is
  the contract, because the child edits the target.
- Whatever you change must be applied to the env-building helper the other
  module children already share, so the policy cannot drift between sites.

## Scope

Edit only the target worktree's `mini_ork/` tree and `db/migrations/`. Do NOT
touch the binding directory (`kickoffs/auto/miniork-self/**`), tests, or anything
else — the binding is the instrument scoring you and is protected. Keep the
diff minimal.

## Success criteria

- The predicate for `{{unit_id}}` exits 0, and every one of its other probes is
  still satisfied — including the control that must stay red.
- The fix holds on every path the probe exercises: where a defect is specific to
  an upgrading database or a fresh install, both must hold.
- Minimal, reviewable diff.

## Model preference

`minimax` for the implementer (code lane — never glm).

## Verification (the outer loop runs it, not you)

Your patch is committed in the target worktree and the predicate is re-run
against it — it reads the worktree you edited, so your change is scored in
place. `unit_predicate.py` exits 0 only when the unit is genuinely closed. Your
job is the correct, minimal patch — nothing else.
