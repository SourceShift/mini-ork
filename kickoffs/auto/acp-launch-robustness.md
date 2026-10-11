# ACP / web launch: start the run on the right Python, and never wait forever on a dead launch

## Goal

Measured end to end (a real `mini-ork acp` process driven by the official
`agent-client-protocol` client, in a scratch repo): `initialize` and `session/new`
work, `session/prompt` launches a run, and the turn then **never ends**. The launched
run died at startup:

```
mini-ork: Python 3.11+ is required, but this is 3.9.23 (/Users/admin/micromamba/bin/python3).
```

Cause: `mini_ork/web/control.py::launch_run` spawns `[bin/mini-ork, "run", ...]`, so the
child runs under the shebang's `env python3` — whatever `python3` is first on PATH (macOS
ships 3.9). The parent already re-exec'd into the project venv and set
`MINI_ORK_VENV_ACTIVE=1`; the child inherits that flag, skips its own venv re-exec, and
exits on the version check. Then `mini_ork/acp/agent.py::_await_terminal` polls a run
that will never get a `task_runs` row, forever. In Zed that is a thread that spins
indefinitely.

## Files in scope

- `mini_ork/web/control.py` — `launch_run`.
- `mini_ork/acp/agent.py` — launch bookkeeping + `_await_terminal`.
- `tests/unit/test_acp_agent_py.py` — extend.
- `tests/unit/test_web_control_launch.py` — NEW (or extend the existing launch test file
  if one already covers `launch_run`; `grep -rl "launch_run" tests/` first).

No other file changes.

## Mechanism (exact spec)

### 1. `launch_run` starts the child on the current interpreter

- argv becomes `[sys.executable, str(bin_path), "run", rcp, str(kickoff_path)]`.
- The child env drops `MINI_ORK_VENV_ACTIVE` (pop it) so a child started some other way
  still re-execs into the venv on its own.
- Everything else (cwd, env vars, detach, log file, return dict) stays as is.

### 2. The ACP agent ends the turn when the launch is dead

- `_launch` keeps the launch result: store `pid` and `log_path` per session
  (`self._launches[run_id] = result`). Injected launchers in tests may return a dict
  without them — treat missing keys as "unknown", never crash.
- In `_await_terminal`, on every poll where the snapshot has no status yet
  (`snapshot.get("status") is None`):
  - if a `pid` is known, check it with `os.waitpid(pid, os.WNOHANG)`: a return of
    `(pid, status)` or a `ChildProcessError` means the launcher process is gone;
    `(0, 0)` means still running. Any other `OSError` → treat as still running.
  - If the launcher is gone and the next snapshot read still has no status, the run
    failed to start: emit one `AgentMessageChunk` (same helper shape as
    `_build_terminal_message`) whose text is
    `mini-ork run failed to start:\n` + the last 20 lines of `log_path` (or
    `(no launch log)` if missing/unreadable), then return `"refusal"`.
  - Independently, if no status has appeared for `MO_ACP_START_TIMEOUT_S` seconds
    (env, default 300, read once in `__init__`; injectable via a constructor kwarg for
    tests), do the same with text `mini-ork run did not start within <N>s` plus the log
    tail, and return `"refusal"`.
- Once a status has been seen, behaviour is unchanged (wait for a terminal status).
- Cancellation keeps working exactly as today.

## Tests

`tests/unit/test_web_control_launch.py`:
- monkeypatch `subprocess.Popen` to capture argv/env: argv[0] is `sys.executable`,
  argv[1] ends with `bin/mini-ork`, argv[2:4] == `["run", <recipe>]`;
  `MINI_ORK_VENV_ACTIVE` absent from the child env even when set in `os.environ`;
  `MINI_ORK_RUN_ID` / `MINI_ORK_HOME` / `MINI_ORK_ROOT` still set.

`tests/unit/test_acp_agent_py.py` (reuse the file's existing fakes/seams):
- launcher returns `{"ok": True, "run_id": rid, "pid": <pid of an already-exited child>, "log_path": <tmp file with 3 lines>}`
  and the reader always returns `{"status": None, ...}` → prompt returns
  `stop_reason == "refusal"` quickly (poll interval tiny), and an agent message chunk
  containing `failed to start` and the log lines was emitted.
  (Create the exited child with `subprocess.Popen([sys.executable, "-c", "pass"])` and
  `wait()` it — then waitpid raises ChildProcessError, which must count as gone.)
- launcher without `pid`, reader never sets a status, start timeout 0.2s → `"refusal"`
  with `did not start within`.
- launcher with a live pid (a `sleep 5` child), reader returns `running` then `completed`
  → `"end_turn"` (unchanged path); kill the child in teardown.
- every existing test in the file still passes unchanged.

## Success criteria

- `python3.11 -m pytest -q tests/unit/test_acp_agent_py.py tests/unit/test_web_control_launch.py` passes, plus any existing test that references `launch_run` (`grep -rl launch_run tests/`).
- `ruff check` clean on the touched files.
- Diff touches only the files in scope.
