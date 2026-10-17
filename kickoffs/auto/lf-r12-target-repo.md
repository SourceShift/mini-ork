# LF-R12 findings remember which repo they are about

## Goal

`code_findings` has no repo column and `context_v2.scope_findings` matches on a
relative path prefix only, so a finding about `calc.py` or `tally.py` in one
target repo is offered to any other repo with the same relative path. The
panel saw it happen across checkouts: `tally.py` findings from runs targeting
`…/vt-smoke-engine-b/` were delivered to `run-1791930074-82118`, launched from
the primary checkout, and `calc.py` findings come from both the `vt5-smoke` and
`vt6-smoke` repos. The parent-directory fallback in `scope_findings` and
`prior_attempts` cross repos the same way. Decision record:
`docs/plans/2026-10-15-learning-loop-fixes.md` (R12; Q8: a finding with no
recorded repo is ENGINE-LOCAL — offered only when the current target is the
engine checkout, never as a wildcard).

All paths are relative to the repository root.

## Mechanism (exact)

1. Migration `db/migrations/0078_code_findings_target_repo.sql` (0076 and 0077
   are taken on `origin/main` by `0076_task_runs_status_not_needed.sql` and
   `0077_emergent_pattern_task_class.sql`; renumber again if 0078 is taken by
   the time this runs) adds `target_repo TEXT` to `code_findings` and an
   index on it. Use the guarded `.read "|sh -c '…'"` ALTER idiom of
   `0072_pm_mirror_body.sql` (check `pragma_table_info` first, because the
   runtime `ensure_schema` may add the column before this file is recorded) and
   end with the `INSERT OR IGNORE INTO schema_migrations(...)` self-record line.
   `code_findings.ensure_schema` adds the column when missing (same definition).
2. `code_findings.repo_identity(path) -> str | None`: the realpath of the
   directory that holds the repo's COMMON git dir (`git -C <path> rev-parse
   --path-format=absolute --git-common-dir`, parent of the returned `.git`), so
   every worktree of one repository has one identity. Path missing or not a git
   repo → `os.path.realpath(path)` when it exists, else None. Cached per path;
   never raises.
3. `_harvest_run` stores `target_repo = repo_identity(_run_target_cwd(run_dir))`
   on every row it inserts. Add `backfill_target_repo(home, *, db=None) -> int`
   that fills NULL `target_repo` for rows whose run dir still has a
   `run_profile.json` (CLI: `backfill-target-repo`; cold-safe; idempotent).
4. `context_v2`: `scope_findings(paths, *, …, target_repo=None)`,
   `prior_attempts(…, target_repo=None)` and `build(…, target_repo=None)`. When
   `build` gets no `target_repo`, it uses
   `repo_identity(context_env("MO_TARGET_CWD"))`, else the engine root's identity.
   Selection keeps a row when `row.target_repo = current`, or when
   `row.target_repo IS NULL` AND `current` is the engine identity
   (`repo_identity(MINI_ORK_ROOT or the package's parent)`). The parent-directory
   fallback uses the same filter. `pack_for_run` passes the identity of the run
   profile's target cwd.
5. The "Your code" tab (`mini_ork/ide_pages/learn/code.py` `_window_findings`)
   shows rows whose `target_repo` is the tab's repo identity, or NULL when that
   repo is the engine.

## Files in scope

- `db/migrations/0078_code_findings_target_repo.sql` (new; renumber if taken)
- `mini_ork/learning/code_findings.py`
- `mini_ork/context_v2.py`
- `mini_ork/ide_pages/learn/code.py`
- `tests/unit/test_code_findings_target_repo.py` (new)

## Tests (`tests/unit/test_code_findings_target_repo.py`, hermetic temp git repos + temp DB)

- `repo_identity` of a repo and of a `git worktree add` of it are equal; of a
  second repo, different; of a missing path, None;
- harvesting a run whose `run_profile.json` targets repo A stamps A's identity;
- `scope_findings(['calc.py'], target_repo=B)` does not return A's `calc.py`
  finding; with `target_repo=A` it does; the parent-directory fallback obeys
  the same rule;
- a NULL-`target_repo` finding is returned when the current repo is the engine
  identity and not otherwise;
- `backfill_target_repo` fills NULL rows from run profiles and a second call returns 0;
- the migration applies through `mini_ork.stores.migrate.migrate_apply` on a
  fresh DB AND on a DB where `ensure_schema` already added the column.

## Verification commands

- `/Volumes/docker-ssd/ps/mini-ork/.venv/bin/python -m pytest -q tests/unit/test_code_findings_target_repo.py tests/unit/test_code_findings.py tests/unit/test_context_v2.py tests/unit/test_ide_pages_learn_code.py tests/unit/test_migrate_py.py`

## Self-application measurement

```measure
/Volumes/docker-ssd/ps/mini-ork/.venv/bin/python -m pytest -q tests/unit/test_code_findings_target_repo.py tests/unit/test_context_v2.py --junitxml="${MINI_ORK_RUN_DIR}/lf-r12-junit.xml"
```

## Required artifacts — declare EXACTLY these and nothing else

- `${MINI_ORK_RUN_DIR}/lf-r12-junit.xml`

## Rules

Edit files directly; do not emit unified diffs. Never rebase, merge or reset
the worktree during the run. Do not change `_fingerprint`, the classifier,
`parse_contract`, `recurring()`, or rule actions, and do not write to the live
DB under `/Volumes/docker-ssd/ps/mini-ork/.mini-ork/` (tests use temp DBs).
