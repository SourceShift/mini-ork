# SDE-4a supervisor: revive parked runs once their fix lands, label the ledger, clear orphans

## Goal

The self-driving loop parks an engine-fault run on a fix epic
(`mini_ork/recovery/escalation.py:_record_waiting_on_fix` writes
`runs/<id>/repair.json` with `state="waiting_on_fix"` and `fix_epic_id`), but
nothing ever comes back for it: a human still revives the run after the fix
merges. `diagnosis_ledger` has 0 labelled rows, so the calibrated escalation bar
can never activate. And a `repair.json` stuck in `repairing` with no live
process is never cleared (only the spawned child calls `note_stuck`).

Add `mini-ork supervise`: one sweep (or a loop of sweeps) that closes those
three gaps under a single lease. Do NOT add a human approval step anywhere: the
operator is notified through the oversight inbox, never blocked on it.

All paths are relative to the repository root.

## Mechanism (exact spec)

1. New `mini_ork/recovery/supervisor.py`:
   - `sweep(home: str, db: str, *, engine_root: str, now: float | None = None,
     dry_run: bool = False, spawn=None, max_revives: int = 2,
     orphan_after_s: int = 3600) -> list[dict]` — returns one action dict per
     run it acted on (`{"run_id", "action", "reason", ...}`); `dry_run`
     computes the same list and writes nothing and spawns nothing. `spawn` is
     an injectable `callable(run_id, from_node) -> int | None` (pid); the
     default spawns a detached recover subprocess modelled on
     `mini_ork/recovery/auto_repair.py:_spawn_recover`
     (`<engine python> -m mini_ork.recovery.planner <run_id> --from-node <node> --force`,
     `MINI_ORK_HOME`/`MINI_ORK_ROOT=engine_root` set, `MINI_ORK_RUN_ID`,
     `MINI_ORK_RUN_DIR`, `MINI_ORK_PLAN_PATH` removed, new session, stdout and
     stderr to `runs/<id>/supervisor-revive.log`).
   - **Rule R1 — revive.** For every `runs/*/repair.json` with
     `state == "waiting_on_fix"` and a `fix_epic_id`: read `epics.status`.
     - `done` AND the fix has landed → revive. "Landed" is either (a) the
       patch id of the epic's `last_run_id` run dir (`code_findings.run_patch_id`)
       is in `code_findings.landed_patch_ids(engine_root, "origin/main")`, or
       (b) `git -C <engine_root> log origin/main --format=%H --grep=<fix_epic_id> -F`
       returns at least one commit. `done` without either signal is NOT
       landed: leave the run parked, action `wait`, reason `fix epic done but
       not on origin/main`.
     - Revive = pick the from-node (`repair.json` `attempts[-1].from_node`, else
       `attempts[-1].diagnosis.node`, else `runs/<id>/diagnosis.json` `node`;
       none → action `skip`, reason `no failed node recorded`), rewrite
       `repair.json` `state="reviving"` with `revived_at`, `revive_from_node`,
       `revive_engine_sha` (`git -C engine_root rev-parse HEAD`) and
       `revive_pid`, then spawn. At most `max_revives` revives per sweep.
     - Epic status in `escalation._TERMINAL_UNSUCCESSFUL` → `repair.json`
       `state="gave_up"` with `note="fix epic <id> ended <status>"`, and one
       oversight item (`gate_id="self-driving-supervisor"`,
       `feature=f"fix epic failed: {run_id}"`) — skip the enqueue when a row
       with that gate_id and feature already exists in any status.
   - **Rule R2 — label.** For every `repair.json` with `state == "reviving"`:
     if `task_runs.status` for the run is terminal and `updated_at` is after
     `revived_at`: the diagnosis was right when the run is now `published` or
     its `node_checkpoints` row for `revive_from_node` is `success`; wrong when
     it failed again and that node is not `success`. Label the run's
     `diagnosis_ledger` rows that carry this `fix_epic_id`
     (`diagnosis_ledger.set_label(db, ledger_id, correct, reason)`), then set
     `state` to `revived` (published) or `revive_failed`. A run still running →
     no action.
   - **Rule R3 — orphaned `repairing`.** `state == "repairing"`, the last
     attempt's `ts` is older than `orphan_after_s`, and the run's `.pid` owner
     is dead (reuse `mini_ork/orchestration/run_reaper.py`'s pid probe) →
     `state="gave_up"`, `note="orphaned repairing state cleared by supervisor"`.
   - Every `repair.json` rewrite is atomic (write a temp file in the same
     directory, then `os.replace`) and keeps every key it does not change.
   - One unreadable or malformed run dir never stops the sweep: record action
     `error` for it and continue.
2. New `mini_ork/cli/supervise.py` — `mini-ork supervise [--once] [--interval S]
   [--dry-run] [--json] [--config PATH]`:
   - Acquire lease key `__supervisor__` with `mini_ork.stores.lease.acquire_lease`
     (ttl = 3 × interval, minimum 900 s). Not acquired → print
     `another supervisor holds the lease` and exit 0. Release on exit.
   - `--once` runs one sweep and exits 0 (1 if any action is `error`). Without
     `--once` it loops: sweep, `refresh_lease`, sleep `interval` (default 300).
   - `--json` prints the action list as JSON; otherwise one line per action.
   - Config (both tiers in one file): `--config` or `MO_SUPERVISE_CONFIG`
     names a YAML file with keys `interval_s`, `max_revives`, `orphan_after_s`.
     No config named → built-in defaults. A config path that is named but
     missing or unparseable → exit 2 with a clear error (never a silent
     default).
   - The module ends with an `if __name__ == "__main__":` guard.
3. `mini_ork/cli/main.py`: add `"supervise": "mini_ork.cli.supervise"` to
   `_NATIVE_MODULE_SUBS` and one `_HELP` line.
4. `tests/unit/test_native_dispatch_py.py`: add `"supervise"` to the
   exact-equality expected set.
5. New `config/self-driving.example.yaml`: the three keys with defaults and
   comments describing the two tiers — simple (`mini-ork supervise --once` from
   cron/launchd, local SQLite lease) and service (`mini-ork supervise
   --interval 300` as a long-running process against the shared DB, operator
   notified through the oversight inbox).
6. New `docs/operator/self-driving.md`: what the supervisor does (R1–R3), how
   to run each tier, and how to read its actions.

## Files in scope (touch ONLY these)

- `mini_ork/recovery/supervisor.py` (new)
- `mini_ork/cli/supervise.py` (new)
- `mini_ork/cli/main.py`
- `tests/unit/test_native_dispatch_py.py`
- `tests/unit/test_supervisor.py` (new)
- `config/self-driving.example.yaml` (new)
- `docs/operator/self-driving.md` (new)

Do NOT modify any other file. Do not touch `escalation_policy.py`,
`escalation.py`, `auto_repair.py`, `diagnosis_ledger.py` or `lease.py`.

## Tests (`tests/unit/test_supervisor.py`, hermetic temp home + sqlite, a temp git repo as engine_root)

- R1: parked run + epic `done` + a commit on the engine repo's `origin/main`
  whose message contains the epic id → one `revive` action, `repair.json`
  `state == "reviving"` with `revive_from_node`, `spawn` called once with
  `(run_id, from_node)`;
- R1: epic `done` with no landing signal → `wait`, `repair.json` unchanged;
- R1: epic `failed` → `gave_up` + exactly one oversight row; a second sweep
  adds no second row;
- R1: three landed parked runs with `max_revives=2` → two revives;
- R2: `reviving` run now `published` → ledger row labelled correct, state
  `revived`; failed again at the same node → labelled wrong, state
  `revive_failed`; still `executing` → no action;
- R3: `repairing` with a dead pid and an old attempt → `gave_up`; with a
  young attempt → no action;
- `dry_run=True` → same action list, no file or DB change, `spawn` not called;
- a run dir with a corrupt `repair.json` → `error` action, other runs still
  processed;
- CLI: a second `supervise --once` while the lease is held exits 0 with the
  lease message; `--config /nonexistent.yaml` exits 2.

## Verification commands

- `pytest -q tests/unit/test_supervisor.py tests/unit/test_native_dispatch_py.py tests/unit/test_diagnosis_ledger.py tests/unit/test_escalation.py`

## Self-application measurement

```measure
python3 -m pytest -q tests/unit/test_supervisor.py tests/unit/test_native_dispatch_py.py --junitxml="${MINI_ORK_RUN_DIR}/supervisor-junit.xml"
```

## Required artifacts — declare EXACTLY these and nothing else

- `${MINI_ORK_RUN_DIR}/supervisor-junit.xml`

## Success criteria

- The verification command passes.
- `ruff check --select F,E9 mini_ork/recovery/supervisor.py mini_ork/cli/supervise.py` is clean.

## Rules

Edit files directly; do not emit unified diffs. Execute this spec exactly; do not
research and do not add lens or drafter nodes.
