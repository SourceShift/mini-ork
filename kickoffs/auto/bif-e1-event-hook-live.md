# E1: `MINI_ORK_ON_EVENT` goes live — the no-server tier of event push

## Goal

`MINI_ORK_ON_EVENT` is documented (`docs/EVENT-HOOKS.md`) and shown as
working in the IDE (`mini_ork/ide_pages/setup.py:345-348`,
`mini_ork/ide_pages/runs.py` "Event hooks"), but nothing calls
`mini_ork.observability.emit_hook.mo_emit_hook`. `docs/ECOSYSTEM.md` §6 lists it
as a known gap.

The live push channels (SSE `/api/v1/stream`, WebSocket `/sockets/events/{id}`)
all need `mini-ork serve`. The hook is the simple tier, with no server: a
local command receives each event. Wire it.

Four defects in `emit_hook.py` must go first:
1. It is **synchronous**: `subprocess.run` blocks the caller up to 5 s per
   event, and `node_heartbeat` alone has 30,733 rows.
2. The value is used as ONE argv element, but the IDE and its test configure
   a command with arguments (`/usr/local/bin/notify-hook --token SECRET`),
   which would never run.
3. With no `timeout` binary and a missing hook path it raises
   `FileNotFoundError`, despite "Never raises".
4. Its docstrings cite the deleted `lib/mo_emit_hook.sh`.

All paths are relative to the repository root.

## Mechanism (exact spec)

### 1. `mini_ork/observability/emit_hook.py`

```
HEARTBEAT_EVENTS = frozenset({"node_heartbeat"})
def hook_argv(value: str) -> list[str] | None
def mo_emit_hook(event_type: str = "unknown", run_id: str = "", payload_json: str = "{}") -> None
```

1. **`hook_argv(value)`**: `shlex.split(value)` (a `ValueError` → `None`).
   - Returns `None` when the list is empty.
   - The first token goes through `_resolve_hook_path`, unchanged; it must be
     an existing executable file (`os.path.isfile` and `os.access(X_OK)`), or
     be resolvable with `shutil.which`, else `None`.
   - Otherwise returns the tokens with the resolved first token.
2. **`mo_emit_hook`**:
   - **No-op cases.** `MINI_ORK_ON_EVENT` unset or blank → return. An
     `event_type` in `HEARTBEAT_EVENTS` → return, unless
     `MINI_ORK_ON_EVENT_HEARTBEATS == "1"`.
   - **Invalid value.** `hook_argv` gives `None` → write ONE line per process
     to stderr: `mini-ork: MINI_ORK_ON_EVENT is not an executable command; event hook disabled`.
     Use a module-level flag. Never print the value, which may carry a token.
     Then return.
   - **Run it.** The argv is `hook_argv(...) + [event_type, run_id, payload_json]`.
     Start it with
     `subprocess.Popen(argv, stdin=DEVNULL, stdout=DEVNULL, stderr=DEVNULL, start_new_session=True)`
     and return immediately (non-blocking).
   - **Timeout.** A daemon thread waits on the process for `_HOOK_TIMEOUT_S`
     (5 s); if it is still running, it kills the process group
     (`os.killpg(proc.pid, SIGKILL)`, ignoring errors) and reaps it.
   - **Never raise.** Wrap the whole body in `try/except Exception: return`.
   - Drop the `gtimeout`/`timeout` prefix; `_resolve_timeout_bin` may stay for
     compatibility but is unused.
3. Update the module and function docstrings: no more `lib/mo_emit_hook.sh`
   citation; describe the non-blocking behaviour, the heartbeat opt-in, and
   the argv form (no shell).

### 2. Emit points

1. **`mini_ork/observability/node_events.py` `_insert_event`.** After a
   successful `con.commit()`, call
   `mo_emit_hook(event_type, run_id, json.dumps(payload, separators=(",", ":")))`
   (lazy import, inside `try/except Exception: pass`). Not on the error paths.
   This covers `mo_node_emit`, `mo_run_emit`, `mo_node_start`, `mo_node_end`
   and heartbeats.
2. **`mini_ork/orchestration/recursive.py` `mo_recursive_emit_event`.** After
   its commit, the same call with its event type and payload
   (`child.started` / `child.completed` / `child.failed`).
3. **`mini_ork/orchestration/run_reaper.py` `close_run_record`.** When it
   returns a terminal status, emit `run_finished` with payload
   `{"status": <status>, "verdict": <task_runs.verdict or null>}`, once per call.
   Read the verdict with the connection the function already opens; if none
   is open, use a read-only one. Same try/except.

### 3. IDE text

- `mini_ork/ide_pages/setup.py`: the "Event hook" line reads
  `MINI_ORK_ON_EVENT is set — every run/node event runs it (heartbeats only with MINI_ORK_ON_EVENT_HEARTBEATS=1)`.
- `mini_ork/ide_pages/runs.py`: the empty-state hint reads
  `Set MINI_ORK_ON_EVENT to a command (an executable plus its arguments, no shell); it is run with event_type, run_id, payload_json after each event.`
  Keep the rest of `_hook_sources` / `_hooks` unchanged.

### 4. Docs

- **`docs/EVENT-HOOKS.md`:**
  - the call form (argv, no shell; the command may carry arguments);
  - non-blocking, with a 5 s kill;
  - heartbeats opt-in;
  - the new `run_finished` event;
  - the invalid-value warning;
  - "SSE on `mini-ork serve`" is live (`/api/v1/stream`): name it the
    secure/served tier and the hook the simple tier.
- **`docs/ECOSYSTEM.md` §6:** replace the "Known gap found later:
  `MINI_ORK_ON_EVENT` … nothing calls `mo_emit_hook`" sentence with one saying
  it is live (node/run/child events plus `run_finished`, non-blocking).

## Files in scope (touch ONLY these)

- `mini_ork/observability/emit_hook.py`
- `mini_ork/observability/node_events.py`
- `mini_ork/orchestration/recursive.py`
- `mini_ork/orchestration/run_reaper.py`
- `mini_ork/ide_pages/setup.py`
- `mini_ork/ide_pages/runs.py`
- `docs/EVENT-HOOKS.md`
- `docs/ECOSYSTEM.md`
- `tests/unit/test_mo_emit_hook_py.py`
- `tests/unit/test_event_hook_wired.py` (new)

`tests/unit/test_mo_emit_hook_py.py` stubs `subprocess.run` and asserts the old
blocking form; update those assertions to the `Popen` form. Every other
existing test must pass unchanged. Do NOT modify any migration, `examples/`,
or any path in `config/kernel-frozen.yaml`.

## Tests (`tests/unit/test_event_hook_wired.py`, hermetic)

Use a tmp executable script that appends `"$1 $2 $3"` to a file, and a DB from
`mini_ork.stores.migrate.init_db(db=..., root=REPO)` (assert rc 0).

1. With `MINI_ORK_ON_EVENT=<script>`, `mo_node_start(...)` / `mo_node_end(...)`
   through `node_events` → within 3 s the file has `node_start` and
   `node_end` lines with the run id and parseable JSON.
2. `mo_node_emit(run_id, "node_heartbeat", ...)` writes no line, unless
   `MINI_ORK_ON_EVENT_HEARTBEATS=1`.
3. A command with arguments (`"<script> --tag x"`) runs with the arguments
   first; the file shows `--tag x node_start …`.
4. A non-executable or missing value → no exception, one stderr warning on
   the first call and none on the second; the warning does not contain the
   value.
5. **Non-blocking.** A hook that sleeps 10 s → `mo_emit_hook` returns in
   under 0.5 s, and the process is killed by about 5 s (poll for its pid file
   with a timeout).
6. `mo_recursive_emit_event(..., event_type="child.completed")` → a
   `child.completed` line.
7. `close_run_record` on a run that ends `failed` → one `run_finished` line
   with `"status": "failed"`.
8. Unset `MINI_ORK_ON_EVENT` → `Popen` is never called (monkeypatch it to
   raise).

## Verification commands

- `python3.11 -m pytest -q -p no:asyncio tests/unit/test_event_hook_wired.py tests/unit/test_mo_emit_hook_py.py tests/unit/test_ide_pages_runs.py tests/unit/test_ide_pages_setup.py tests/unit/test_mo_node_events_py.py`

(Drop any listed test file that does not exist; do not create it.)

## Self-application measurement

```measure
python3.11 -m pytest -q -p no:asyncio tests/unit/test_event_hook_wired.py --junitxml="${MINI_ORK_RUN_DIR}/bif-e1-junit.xml"
```

## Required artifacts — declare EXACTLY these and nothing else

- `${MINI_ORK_RUN_DIR}/bif-e1-junit.xml`

## Success criteria

- The verification command passes; every pre-existing test is unchanged
  except the `subprocess.run` → `Popen` assertions in `test_mo_emit_hook_py.py`.
- `ruff check --select F,E9` is clean on every touched `.py` file.
- No event emission can block a node or a run for more than a process spawn,
  and none can raise.
