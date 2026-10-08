# A node timeout kills everything the agent started, including commands outside its process group

## Why (live, 2026-10-08)

Run `ide-orca-f2b-flow-20261008172728` (code-fix on the Zed fork). The round-2 implementer
(Claude CLI on the deepseek_flash lane) hit `MO_NODE_TIMEOUT_S` (3600 s) and the node ended
`timeout`. 57 minutes later, its Bash tool's command was still running as an orphan:

- `/bin/bash -c '… script/mini-ork-build …'` with ppid 1;
- its `cargo build -p zed --profile release-fast` child.

That orphan held the cargo build-directory lock, and burned CPU and memory on a host already
at about 26 GB of swap. The run's own post-run verify then blocked on that lock: `Blocking
waiting for file lock on build directory`.

**Why the existing kill missed it.** `mini_ork/dispatch/core.py` `_terminate_process_group`
does `os.killpg(os.getpgid(proc.pid), SIGKILL)`. That reaps the session the agent CLI leads,
but the Claude CLI runs each Bash tool command in its OWN process group. So those commands, and
everything under them, survive and are reparented to launchd.

## Files in scope (touch ONLY these)

- `mini_ork/dispatch/core.py`: ONLY `_terminate_process_group` and a new private helper
- `tests/unit/test_dispatch_timeout_orphans.py` (new)

Do NOT touch any other file.

## Changes (exact)

1. **New helper `_descendant_pids(root_pid) -> list[int]`.** It returns every live descendant
   of `root_pid`.
   - Build a ppid map from one `ps -A -o pid=,ppid=` call (`subprocess.run`, `timeout=5`,
     `capture_output=True`). No psutil dependency.
   - Walk the map breadth-first.
   - Fail-soft: any error → `[]`.
2. **`_terminate_process_group(proc)`:**
   - BEFORE killing anything, collect `_descendant_pids(proc.pid)`. After the leader dies its
     children reparent to 1 and the tree link is lost.
   - Then, as today, `os.killpg(getpgid(proc.pid), SIGKILL)`.
   - Then, for each collected descendant: `os.killpg(os.getpgid(pid), SIGKILL)` when its pgid
     differs from the leader's and from our own (`os.getpgrp()`); else `os.kill(pid, SIGKILL)`.
     Ignore `ProcessLookupError`, `PermissionError` and `OSError`.
   - Never signal our own process group or pid 1.
3. Keep the docstring accurate: say why the group kill alone was not enough (the Claude CLI's
   per-command process groups).

## Tests (`tests/unit/test_dispatch_timeout_orphans.py`)

- Use `spawn_local` (or the public entry that wraps it, whichever the existing tests use; see
  `tests/unit/test_dispatch_live_stream_py.py`) with a short timeout, on a `python3 -c` child.
  - **The child** starts a grandchild with `start_new_session=True`. That grandchild runs
    `sleep 60` and writes its pid to a temp file. The child then sleeps 60 s.
  - **After the timeout returns,** the grandchild pid is dead within 2 s: poll `os.kill(pid, 0)`
    until it raises `ProcessLookupError`.
- `_descendant_pids` of a pid with no children → `[]`. A failing `ps` (monkeypatch
  `subprocess.run` to raise) → `[]`.
- The kill never targets `os.getpgrp()`: monkeypatch `os.killpg` to record its calls, and assert
  our own pgid is not among them.

## Verification command

The command that proves this run succeeded (per file; the host kills one CPU-bound process
that runs longer than 30 s):

```bash
for f in tests/unit/test_dispatch_timeout_orphans.py tests/unit/test_dispatch_live_stream_py.py tests/unit/test_spawn_workspace_isolation_py.py; do env -u MINI_ORK_RUN_ID -u MINI_ORK_DB -u MINI_ORK_HOME python3.11 -m pytest -q -p no:asyncio "$f" || exit 1; sleep 3; done   # must exit 0
```

## Done when

- The verification command → 0 failed. Paste the summary lines.
- `uvx ruff check mini_ork/dispatch/core.py tests/unit/test_dispatch_timeout_orphans.py` → clean.
- `git diff --stat` touches only the files in scope.
