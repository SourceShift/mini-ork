# SDE-4b supervisor: mark superseded failures and back-link delivered epics

## Goal

A failed run whose work was delivered some other way stays `failed` forever:
on 2026-10-15, 22 of 28 recent failed runs were already delivered elsewhere,
yet they sat in the failed bucket and stayed eligible for repair sweeps (gap
G1 in `docs/todos/2026-10-15-failed-runs-4d-revive-plan.md`). And an epic closed
by another route keeps `last_run_id` pointing at the failed run, so nothing
says which run or commit delivered it (gap G3).

Give such runs a terminal status of their own, `superseded` (terminal, not
unsuccessful, like `not_needed`), and teach the SDE-4a supervisor
(`mini_ork/recovery/supervisor.py:sweep`) to apply it and to record the
delivering run or commit on the epic.

All paths are relative to the repository root.

## Mechanism (exact spec)

1. New migration `db/migrations/0080_task_runs_status_superseded.sql` (this exact
   name is claimed for this worktree): widen the `task_runs.status` CHECK to add
   `'superseded'`, using exactly the table-rebuild shape of
   `db/migrations/0076_task_runs_status_not_needed.sql` (PRAGMA foreign_keys off
   outside its own BEGIN, same columns, same indexes recreated, self-record in
   `schema_migrations`).
2. `mini_ork/run_status.py`: `superseded` joins `TERMINAL` (not `UNSUCCESSFUL`),
   `DEFAULT_BUCKET["superseded"] = "not_needed"`, `LABELS["superseded"] = "Superseded"`.
   `BUCKETS` is unchanged.
3. `mini_ork/orchestration/run_reaper.py`: both `status NOT IN ('published',
   'failed', 'rolled_back')` queries use the terminal set from
   `mini_ork.run_status.TERMINAL` instead of the hard-coded tuple (today the
   reaper re-selects `not_needed` rows, and it would flip a `superseded` row back
   to `failed`).
4. `mini_ork/recovery/supervisor.py`, two new rules run in every `sweep` after the
   existing per-`repair.json` rules (same `dry_run` contract: compute the action,
   write nothing):
   - **R4 — supersede.** Over the 500 most recent `task_runs` rows with
     `status='failed'`, skipping any run whose `repair.json` state is
     `waiting_on_fix`, `reviving` or `repairing` (owned by a loop): the run is
     superseded when (a) a later `task_runs` row (greater `created_at`) with the
     same `kickoff_path` is `published`; or (b) the patch id of the run's
     `framework-edit.diff` (`code_findings.run_patch_id(run_dir)`) is in
     `code_findings.landed_patch_ids(engine_root, "origin/main")` (computed at most
     once per sweep, sharing the cache R1 already uses); or (c) the run appears in
     `epic_attempts` for an epic whose `status='done'`. Apply with a
     compare-and-set `UPDATE task_runs SET status='superseded', notes=COALESCE(notes,'')
     || ' superseded: <reason>' WHERE id=? AND status='failed'`. Action
     `{"run_id", "action": "supersede", "reason"}` where reason names the signal
     (`later run <id> published` / `diff landed on origin/main` / `epic <id> done`).
   - **R5 — back-link.** For every epic with `status='done'` whose `last_run_id`
     run is not `published` and whose `notes` do not already contain
     `delivered_by=`: the delivering evidence is a `published` run among its
     `epic_attempts` (latest first), else a commit on `origin/main` whose message
     contains the epic id (`git log origin/main --format=%H -F --grep=<id>`, first
     line). Append ` delivered_by=<run_id>` or ` delivered_by=commit:<sha>` to
     `epics.notes`; no evidence → no action. Action
     `{"epic_id", "action": "backlink", "reason"}`.
   - The sweep's return list keeps its existing per-run dicts; R4/R5 dicts are
     appended after them.
5. `docs/operator/self-driving.md`: add R4 and R5 to the rule list.

## Files in scope (touch ONLY these)

- `db/migrations/0080_task_runs_status_superseded.sql` (new)
- `mini_ork/run_status.py`
- `mini_ork/orchestration/run_reaper.py`
- `mini_ork/recovery/supervisor.py`
- `docs/operator/self-driving.md`
- `tests/unit/test_supervisor_superseded.py` (new)

Do NOT modify any other file.

## Tests (`tests/unit/test_supervisor_superseded.py`, hermetic temp home + sqlite built by applying the repo's real migrations, a temp git repo with `refs/remotes/origin/main` as engine_root)

- the migrated schema accepts `status='superseded'` and still rejects an
  unknown status;
- R4 (a): failed run + a later published run with the same `kickoff_path` →
  `superseded`, note names the later run; an EARLIER published run does not count;
- R4 (b): a failed run whose `framework-edit.diff` was committed on the engine
  repo's `origin/main` → `superseded`;
- R4 (c): failed run in `epic_attempts` of a `done` epic → `superseded`;
- R4 skips a run whose `repair.json` is `waiting_on_fix`;
- R4 never touches a `published` or `executing` run;
- R5: a `done` epic whose `last_run_id` failed and whose attempts include a
  published run → notes gain `delivered_by=<that run>`; a second sweep adds
  nothing; with no published attempt but an `origin/main` commit naming the epic
  → `delivered_by=commit:<sha>`;
- `dry_run=True` → the same R4/R5 actions and no DB change;
- `run_status`: `superseded` is in `TERMINAL`, not in `UNSUCCESSFUL`, buckets
  as `not_needed`;
- the reaper (`run_reaper.reap` with a dead pid) leaves a `superseded` and a
  `not_needed` row unchanged.

## Verification commands

- `pytest -q tests/unit/test_supervisor_superseded.py tests/unit/test_supervisor.py tests/unit/test_run_status.py tests/unit/test_run_reaper.py tests/unit/test_run_reaper_notes.py tests/unit/test_run_reaper_remote_dead.py tests/unit/test_run_reaper_review_pause.py tests/unit/test_reaper_kills_agents.py tests/unit/test_migrate_py.py`

## Self-application measurement

```measure
python3 -m pytest -q tests/unit/test_supervisor_superseded.py tests/unit/test_supervisor.py --junitxml="${MINI_ORK_RUN_DIR}/superseded-junit.xml"
```

## Required artifacts — declare EXACTLY these and nothing else

- `${MINI_ORK_RUN_DIR}/superseded-junit.xml`

## Success criteria

- The verification command passes with every pre-existing test unchanged.
- `ruff check --select F,E9 mini_ork/recovery/supervisor.py mini_ork/run_status.py mini_ork/orchestration/run_reaper.py` is clean.

## Rules

Edit files directly; do not emit unified diffs. Execute this spec exactly; do not
research and do not add lens or drafter nodes. Epic statuses allowed by the
schema are `not started`, `in progress`, `in review`, `done`, `blocked`,
`escalated`, `abandoned`; never insert any other value in a test fixture.
