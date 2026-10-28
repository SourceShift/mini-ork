# Gap S2: a scheduled reflect pass that fills the gaps post-run reflect leaves

## Goal

Reflect runs after every run (post-run, `mini_ork/cli/main.py:2271`) and as an
in-workflow node, but nothing runs it on a schedule. A killed post-run pass,
or traces skipped while the cost circuit was open, are never revisited: an
unscoped `mini-ork reflect` only looks back 24 hours (`mini_ork/cli/reflect.py`
~320) and nothing invokes it.

Two hazards a schedule must avoid:

- **Overlap.** `reflect` has no lock. Its "already has a gradient?" check
  (`gradient_extractor.has_watermark`, `reflection_pipeline.py` ~788) runs
  before the LLM call and is not atomic, so two overlapping passes can pay for
  the same trace twice. A fresh run's traces are also being reflected by that
  run's own post-run pass.
- **Spend.** It starts paid LLM calls, so it ships OFF, like
  `dispatch_epics` in `config/self-driving.example.yaml`.

Model it on `mini-ork supervise` (`mini_ork/cli/supervise.py`): one config
file, a lease, a `--once`-style simple tier and an `--interval` service tier.

All paths are relative to the repository root.

## Mechanism (exact spec)

### 1. `mini_ork/learning/reflect_schedule.py` (new)

```
LEASE_KEY = "__reflect_schedule__"
STATE_REL = "state/reflect-schedule.json"
DEFAULTS = {"enabled": False, "interval_s": 3600, "deadline_s": 900,
            "settle_s": 1800, "max_window_s": 604800}
def load_config(home: str, path: str | None) -> dict
def run_once(home: str, db: str, cfg: dict, *, now: int | None = None, reflect_fn=None) -> int
def run_forever(home: str, db: str, cfg: dict, *, sleep_fn=time.sleep, reflect_fn=None) -> int
```

1. **Config.** `load_config` reads the `reflect_schedule:` mapping from
   `path`, or from `<home>/config/reflect-schedule.yaml` when `path` is None,
   merged over `DEFAULTS`. A named `path` that does not exist or does not
   parse → raise `SystemExit(2)` with a clear stderr message (fail closed). No
   file at the default location → the defaults (so `enabled: false`).
2. **`run_once`.**
   - `enabled` false → print `reflect schedule: disabled` and return 0 (no
     lease, no reflect).
   - Acquire `LEASE_KEY` with `mini_ork.stores.lease.acquire_lease` the way
     `supervise.py` does with `__supervisor__`. Lease held elsewhere → print
     `reflect schedule: another scheduled pass holds the lease` and return 0.
   - Window: `since = max(state.last_ok_until, now - max_window_s)` (state
     missing → `now - max_window_s`); `until = now - settle_s`. When
     `until <= since`, print `reflect schedule: nothing settled yet`, release,
     return 0.
   - Run one reflect pass (`reflect_fn`, default `mini_ork.cli.reflect.main`)
     with `--since <since>` and, through `mini_ork.context.scoped_environ`,
     `MO_REFLECT_DEADLINE_S=<deadline_s>` and
     `MO_REFLECTION_UNTIL_TS=<until>`.
   - Exit 0 → write `{"last_ok_until": until, "at": now}` to
     `<home>/STATE_REL` atomically (tmp + `os.replace`). Non-zero → keep the
     old state so the window is retried next time.
   - Release the lease in `finally`. Return the reflect exit code.
3. **`run_forever`.** Loop `run_once` then `sleep_fn(interval_s)`; stop on
   `KeyboardInterrupt` with return 0.

### 2. `mini_ork/learning/reflection_pipeline.py` — upper bound

`_extract_trace_ids_sql` (~140-155) gains an upper bound: when
`MO_REFLECTION_UNTIL_TS` is set to a positive integer, also require
`CAST(strftime('%s', created_at) AS INTEGER) <= <until>`. Unset or invalid →
unchanged behaviour (no upper bound). This keeps a scheduled pass off traces
a fresh run's post-run pass is still reflecting.

### 3. CLI — `mini_ork/cli/reflect.py`

`mini-ork reflect --scheduled [--config PATH] [--interval]`:
`--scheduled` alone → `run_once` (the simple tier: one line in cron or a
launchd job); `--scheduled --interval` → `run_forever` (the service tier).
Without `--scheduled`, `reflect` behaves exactly as today.

### 4. `config/reflect-schedule.example.yaml` (new)

The `reflect_schedule:` keys with the defaults above, `enabled: false`, and a
comment block showing the simple tier (a crontab line and a launchd
`StartInterval` snippet running `mini-ork reflect --scheduled`) and the
service tier (`mini-ork reflect --scheduled --interval`), and noting that it
spends LLM budget under `MO_DAILY_BUDGET_USD`.

### 5. `docs/operator/reflect-schedule.md` (new)

What it is for, both tiers, the lease, the settle window, the state file, how
to turn it on (copy the example to `<home>/config/reflect-schedule.yaml`,
set `enabled: true`).

## Files in scope (touch ONLY these)

- `mini_ork/learning/reflect_schedule.py` (new)
- `mini_ork/learning/reflection_pipeline.py`
- `mini_ork/cli/reflect.py`
- `config/reflect-schedule.example.yaml` (new)
- `docs/operator/reflect-schedule.md` (new)
- `tests/unit/test_reflect_schedule.py` (new)

Do not modify `mini_ork/stores/lease.py`, `mini_ork/automations.py`, `mini_ork/cli/supervise.py`, `mini_ork/cli/main.py`, any migration, or any path in `config/kernel-frozen.yaml`. Every existing test must pass unchanged.

## Tests (`tests/unit/test_reflect_schedule.py`, hermetic, no LLM)

Use a tmp home and a tmp DB initialised with `mini_ork.stores.migrate.init_db`;
pass a recording `reflect_fn`.

1. No config file → `run_once` returns 0, prints `disabled`, never calls
   `reflect_fn`.
2. A named `--config` that does not exist → `SystemExit` with code 2.
3. Enabled, no state, `now=10_000_000`, defaults → `reflect_fn` gets
   `--since 9_395_200` (`now - 604800`); the env it saw has
   `MO_REFLECTION_UNTIL_TS == 9_998_200` (`now - 1800`) and
   `MO_REFLECT_DEADLINE_S == 900`; afterwards the state file has
   `last_ok_until == 9_998_200`.
4. A second `run_once` at `now + 3600` uses `--since 9_998_200`.
5. A failing `reflect_fn` (returns 1) leaves the state file unchanged.
6. With the lease already held (acquire it in the test first), `run_once`
   returns 0 and never calls `reflect_fn`.
7. `_extract_trace_ids_sql` with `MO_REFLECTION_UNTIL_TS` set excludes a trace
   newer than the bound and keeps an older one; unset → both returned.
8. `run_forever` with a `sleep_fn` that raises `KeyboardInterrupt` after the
   second call runs the pass twice and returns 0.

## Verification commands

- `python3.11 -m pytest -q -p no:asyncio tests/unit/test_reflect_schedule.py tests/unit/test_mini_ork_reflect_py.py tests/unit/test_reflect_run_scope.py tests/unit/test_reflect_deadline.py tests/unit/test_reflect_trace_finalize.py tests/unit/test_supervisor.py`

## Self-application measurement

```measure
python3.11 -m pytest -q -p no:asyncio tests/unit/test_reflect_schedule.py --junitxml="${MINI_ORK_RUN_DIR}/gap-s2-junit.xml"
```

## Required artifacts — declare EXACTLY these and nothing else

- `${MINI_ORK_RUN_DIR}/gap-s2-junit.xml`

## Success criteria

- The verification command passes; every pre-existing test is unchanged.
- `ruff check --select F,E9 mini_ork/learning/reflect_schedule.py mini_ork/learning/reflection_pipeline.py mini_ork/cli/reflect.py tests/unit/test_reflect_schedule.py` is clean.
- Off by default; two scheduled passes never overlap; a scheduled pass never touches traces younger than the settle window.
