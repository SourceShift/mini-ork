# Zed S6a — automations engine: scheduled recipe runs that work with the editor closed

Engine-only (the Zed surface is S6b). No `mini_ork/acp/` changes.

## Goal

Scheduled runs are where agent work compounds — nightly dependency checks, a morning
triage, a weekly dead-code sweep — because they happen whether or not anyone remembered to
ask. mini-ork has no scheduler. Add per-project automations: a recipe + kickoff + schedule,
fired by a small OS-scheduled tick, each firing an ordinary mini-ork run (in its own
worktree when the project is a git repo) that shows up in history like any other.

## Files in scope

- `mini_ork/automations.py` — NEW: store, cron matching, tick, scheduler install.
- `mini_ork/cli/automations_cmd.py` — NEW: `mini-ork automations …`.
- `mini_ork/cli/main.py` — register the subcommand (follow how other native module
  subcommands are registered; mind the exact-set guard test `tests/unit/test_native_dispatch_py.py`
  — update it in the same change).
- `tests/unit/test_automations.py` — NEW; `tests/unit/test_native_dispatch_py.py` — update.

No other file changes.

## Mechanism (exact spec)

### Store — `<home>/automations.json`

`{"automations": [ {id, name, recipe, kickoff, schedule, workspace, enabled, created_at,
last_fired_at, last_run_id, runs: [run ids, newest first, ≤ 20]} ]}` — `id`
`^[a-z][a-z0-9-]{1,47}$`; `schedule` a 5-field cron string (minute hour day-of-month month
day-of-week, local time; `*`, numbers, `a-b`, `a,b`, `*/n`, `a-b/n`; day-of-week 0–6 with
0 = Sunday, 7 also Sunday); `workspace` `worktree` | `in-place`. Writes are atomic (temp +
rename) under a lock file (`fcntl.flock`) so a tick and an edit never interleave.

```python
def load(home) -> list[dict]
def add(home, *, id, name, recipe, kickoff, schedule, workspace="worktree") -> dict
def update(home, id, **fields) -> dict          # enabled / schedule / kickoff / name / recipe / workspace
def remove(home, id) -> dict
def parse_cron(expr) -> CronSpec                # ValueError with a plain message on bad input
def next_fire(spec, after: datetime) -> datetime
def describe(expr) -> str                       # "every weekday at 09:00", "every 15 minutes", else the expression
def due(automation, now: datetime) -> bool      # fired at most once per matching minute: last_fired_at < the matching minute
def tick(home, now: datetime | None = None, *, launcher=None) -> list[dict]   # fire every due, enabled automation
def fire(home, id, *, launcher=None) -> dict    # run now regardless of schedule
```

- `add` validates id uniqueness, recipe existence (`mini_ork.recipes_catalog.find_recipe`),
  the cron expression and a non-empty kickoff; never writes on error (returns `{"ok":
  False, "error"}`).
- `fire`: when `workspace == "worktree"` and the project (`home.parent`) is a git repo,
  mint a run id, `mini_ork.workspaces.create(project, home, run_id)` and launch with
  `MO_TARGET_CWD` = the worktree; else in place with `MO_TARGET_CWD` = the project. Launch
  through `mini_ork.web.control.launch_run(home, recipe, kickoff, run_id=…, extra_env=…)`
  (the `launcher` seam replaces it in tests). Also set `MO_AUTOMATION_ID=<id>` in the run's
  env. Record `last_fired_at`, `last_run_id`, prepend to `runs`. Append one JSON line per
  firing to `<home>/automations.log` (`ts`, `id`, `run_id`, `ok`, `error`).
- `tick`: for each enabled automation with `due(...)` → `fire`; returns the firings. A
  missed window (machine asleep) does not backfill: only the current minute counts.

### OS scheduler (explicit install only)

```python
def scheduler_status(home) -> dict
def install_scheduler(home) -> dict
def uninstall_scheduler(home) -> dict
```

- One job per project, ticking every 60 s: `<python> <engine>/bin/mini-ork automations tick
  --home <home>` with `MINI_ORK_HOME` and `MINI_ORK_ROOT` set (python = `sys.executable`;
  engine = `mini_ork.web.control._mini_ork_root()`), stdout/stderr appended to
  `<home>/automations-tick.log`.
- macOS: a LaunchAgent plist `~/Library/LaunchAgents/ai.mini-ork.automations.<hash>.plist`
  (`<hash>` = first 10 hex of sha256 of the resolved home path) with `StartInterval` 60,
  `RunAtLoad` false, then `launchctl bootstrap gui/<uid> <plist>` (fall back to `launchctl
  load -w` when bootstrap fails). Uninstall: `launchctl bootout gui/<uid>/<label>` (or
  `unload`), delete the plist.
- Linux: a crontab line `* * * * * <command> # mini-ork automations <hash>` added via
  `crontab -l` / `crontab -` (idempotent: replace a line carrying the same marker).
  Uninstall removes it.
- Any other platform → `{"ok": False, "error": "unsupported platform"}`.
- All OS calls go through one `_os_run(argv)` seam and the paths through
  `_launch_agents_dir()`; tests never touch the real launchd/crontab.
- `scheduler_status`: installed or not (plist exists / crontab line present), the
  command, the last tick time (mtime of the tick log).

### CLI — `mini-ork automations <sub>` (`--home` optional; default as other subcommands)

`list` (table: id, name, recipe, schedule in words, enabled, last run + status, next fire),
`add --id --name --recipe --schedule "<cron>" --kickoff-file <path> [--in-place]`,
`remove <id>`, `pause <id>`, `resume <id>`, `run <id>` (fire now), `tick`,
`scheduler status|install|uninstall`, `--json` on list/status. Exit 0 / 2 on usage errors /
1 on failures, messages on stderr.

## Tests (`tests/unit/test_automations.py`; temp homes; launcher, `_os_run` and the launch
agents dir monkeypatched)

cron parsing (valid forms, every invalid form with its message) and `next_fire` across
day/month boundaries and day-of-week; `describe`; `due` fires once per matching minute and
not for a past window; add/update/remove validation and atomic writes; `fire` worktree mode
in a real temp git repo (workspace created, `MO_TARGET_CWD` and `MO_AUTOMATION_ID` passed),
in-place mode, non-git project → in place; `tick` fires only due + enabled ones and records
them and the log line; install/uninstall/status on macOS (plist content: interval, command,
env) and Linux (crontab text in/out) via the seams; CLI `list --json`, `add`, `run`, usage
errors; the native dispatch exact-set includes `automations`.

## Success criteria

- `python3.11 -m pytest -q -p no:asyncio tests/unit/test_automations.py tests/unit/test_native_dispatch_py.py tests/unit/test_workspaces.py` passes.
- `ruff check` clean; diff touches only the files in scope.
