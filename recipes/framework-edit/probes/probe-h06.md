---
oracle: git
holdout: true
---
# Frozen probe probe-h06 — framework-edit harvested task

FROZEN probe kickoff harvested by mini_ork.learning.probe_harvest from mini-ork's failure history. Never edit it to fit a candidate. It is graded by hidden tests, not by this text.

# LF-R11b a finding whose fix landed on origin/main stops being offered

## Goal

R11a decays findings by age and by whether the quoted code still exists at
HEAD. That misses the common case where the run's own change **landed** but the
finding points at a line the change did not touch: the snippet is still there,
so the row looks live, yet the run it came from is already merged into main and
its reviewers' complaints have been answered or superseded. The panel's
decision (Q4, R11 in `docs/plans/2026-10-15-learning-loop-fixes.md`) is a
**patch-id landing check against `origin/main`** — never `task_runs.status`.

The identity R11b uses is git's own: `git patch-id --stable` is invariant to
rebase, cherry-pick and line drift, so a run's diff and the commit that merged
it have the same id even after the follow-up merge rebased the branch. The
existing helper `scripts/mine_heldout_tasks.py:_patch_id` is the precedent for
this exact idiom.

R11a and R12 have merged: depend on `annotate_staleness`, `repo_identity` and
`target_repo`; do not reimplement any of them, and do not alter their
behaviour.

All paths are relative to the repository root.

## Mechanism (exact)

1. **Migration `db/migrations/0079_code_findings_landing.sql`** (0078 is R12;
   renumber if taken by the time this runs): one additive table, guarded with
   `CREATE TABLE IF NOT EXISTS`, ending with the `INSERT OR IGNORE INTO
   schema_migrations(...)` self-record line, in the shape of
   `0073_diagnosis_ledger.sql`:

   ```sql
   CREATE TABLE IF NOT EXISTS code_findings_landing (
       run_id        TEXT PRIMARY KEY,
       patch_id      TEXT NOT NULL,
       checked_ref   TEXT NOT NULL,
       landed_commit TEXT,              -- NULL = not found on the ref
       checked_at    INTEGER NOT NULL
   );
   CREATE INDEX IF NOT EXISTS idx_code_findings_landing_patch
       ON code_findings_landing(patch_id);
   ```

   `landed_commit` NULL is load-bearing: "checked and not landed" must be
   distinguishable from "never checked". `code_findings.ensure_schema` adds
   the same DDL (runtime twin).

2. **`mini_ork/learning/code_findings.py` gains the landing primitives** —
   pure where possible, never raising:

   - `run_patch_id(run_dir) -> str | None` — read `<run_dir>/framework-edit.diff`
     when it exists, else the first `*.diff` in the run dir (sorted); feed its
     text to `git patch-id --stable` via `subprocess.run(..., input=diff,
     capture_output=True, text=True)`. Empty text, a non-diff, or a failed
     command → `None`. No repo argument: `git patch-id` is a pure filter.
   - `landed_patch_ids(repo_root, *, ref="origin/main", since=None) -> dict[str, str]`
     — one call for a whole repo: run
     `git -C repo_root log --no-merges --no-renames --format=commit %H -p [--since=<iso>] <ref>`,
     pipe the output through `git patch-id --stable`, and map each returned
     patch-id to the commit hash in its second column. Newest commit wins on a
     duplicate id. Any git/subprocess error, missing repo, or unknown ref →
     `{}` (callers then treat every run as not landed — a broken probe must
     never hide history).
   - `check_landing(home, *, db=None, ref="origin/main", repo_root=None) -> dict`
     — iterate `home/runs/*` that have a `code_findings` row for that run and
     no landing row with the same `checked_ref`; for each:
     `patch_id = run_patch_id(run_dir)` (skip when `None`); repo =
     `repo_identity(_run_target_cwd(run_dir))` or `repo_root` or
     `MINI_ORK_ROOT`/cwd; `since` = that run's minimum finding `ts` as ISO;
     look the id up in a per-repo `landed_patch_ids` result (cache one call per
     repo). Upsert `INSERT OR REPLACE INTO code_findings_landing`. Returns
     `{"runs_checked", "landed", "reason", "home", "db", "ref"}` with the same
     `reason` vocabulary as `harvest` (`ok`, `no_home`, `no_runs_dir`, `no_db`,
     `db_error`). Idempotent: a second call checks nothing new and returns
     `runs_checked == 0`. Cold-safe and never raises.
   - `landing_enabled(environ=None) -> bool` — `MO_FINDINGS_LANDING` unset or
     any value other than `0`/`false`/`no` (case-insensitive) → `True`.
   - `annotate_landing(rows, *, db=None) -> list[dict]` — copies each row and
     adds `"landed": bool` and `"landed_commit": str | None`, from one query
     over `code_findings_landing` for the rows' distinct `run_id` values. A
     missing table, DB error or unknown run → `landed: False` (fail-open keeps
     findings).
   - CLI subcommand `check-landing` (`--ref`, `--repo-root` flags beside the
     common `--db`/`--home`/`--json`), calling `check_landing`.

3. **`context_v2.scope_findings`** applies the landing decay after R11a's
   staleness decay: `annotate_landing(rows)` and, when `landing_enabled()`,
   drop `landed` rows. The stats companion (the one R11a added) gains
   `"n_landed"`; `build()`'s pack gains `"n_findings_landed"` beside
   `"n_findings_stale"`. `scope_findings` keeps its existing signature and
   return type.

4. **The "Your code" tab** (`mini_ork/ide_pages/learn/code.py` `_window_findings`)
   excludes `landed` rows from the findings listing (unless the flag disables
   it) and prints `N finding(s) hidden as landed on origin/main` above the
   listing, next to R11a's stale line; do not merge the two counts. Stale rows
   and landed rows are hidden, never deleted.

5. **A flag for the risky half:** `MO_FINDINGS_LANDING=0` disables the drop
   (rows are still annotated and counted). Default on.

Known limitation, record it in the code comment: a run that shipped several
commits has a union diff whose patch-id no single commit carries, so it never
matches and stays not-landed. The check fails open — a missed landing is a
finding kept, never a finding hidden wrongly.

## Files in scope

- `db/migrations/0079_code_findings_landing.sql` (new; renumber if taken)
- `mini_ork/learning/code_findings.py`
- `mini_ork/context_v2.py`
- `mini_ork/ide_pages/learn/code.py`
- `tests/unit/test_code_findings_landing.py` (new)
- `tests/unit/test_context_v2.py`
- `tests/unit/test_ide_pages_learn_code.py`

## Tests (`tests/unit/test_code_findings_landing.py`, hermetic temp git repos + temp DB)

- `run_patch_id` of a diff equals `landed_patch_ids`' key for a commit made
  from that exact diff in a temp repo; a textually different diff yields a
  different id; a missing or empty `framework-edit.diff` yields `None`;
- `landed_patch_ids` returns both commits of a two-commit history and returns
  `{}` for a path that is not a git checkout and for an unknown ref;
- `check_landing` records `landed_commit` for a run whose diff was committed to
  the ref, records NULL for one whose diff was not, and a second call returns
  `runs_checked == 0`;
- `annotate_landing` marks the landed run's rows `landed: True`, leaves an
  unknown run `landed: False`, and a DB with no landing table marks every row
  `False` without raising;
- `scope_findings` drops the landed row and keeps the not-landed one; with
  `MO_FINDINGS_LANDING=0` the landed row comes back; the pack's
  `n_findings_landed` equals the number dropped;
- the tab hides a landed row and shows its own hidden-count line;
- the migration applies through `mini_ork.stores.migrate.migrate_apply` on a
  fresh DB AND on a DB where `ensure_schema` already created the table.

## Verification commands

- `/Volumes/docker-ssd/ps/mini-ork/.venv/bin/python -m pytest -q tests/unit/test_code_findings_landing.py tests/unit/test_code_findings.py tests/unit/test_code_findings_staleness.py tests/unit/test_context_v2.py tests/unit/test_ide_pages_learn_code.py tests/unit/test_migrate_py.py`

## Self-application measurement

```measure
/Volumes/docker-ssd/ps/mini-ork/.venv/bin/python -m pytest -q tests/unit/test_code_findings_landing.py tests/unit/test_context_v2.py --junitxml="${MINI_ORK_RUN_DIR}/lf-r11b-junit.xml"
```

## Required artifacts — declare EXACTLY these and nothing else

- `${MINI_ORK_RUN_DIR}/lf-r11b-junit.xml`

## Rules

Edit files directly; do not emit unified diffs. Never rebase, merge or reset
the worktree during the run. Do not change `_fingerprint`, `classify_note`,
`repo_identity`, `annotate_staleness`, the `target_repo` selection rules,
`parse_contract`, `recurring()`, or rule actions; do not alter R11a's age or
snippet checks; do not delete rows (hiding is a read-path decision); do not
write to the live DB under `/Volumes/docker-ssd/ps/mini-ork/.mini-ork/` (tests
use temp DBs).
