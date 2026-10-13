# cloud-providers A2 — Teardown registry (reapers, sandbox-gc, kill_run)

Part of `docs/architecture/cloud-sandbox-providers.md` (epic **A**, second
half). Single deliverable: every teardown path dispatches through a registry
instead of naming backends. It runs in parallel with A1 (disjoint files), and
B (node-provider seam) follows it.

## Goal

Killing a run, sweeping leaked sandboxes, and recording a session id work for
any backend or node provider that registers a hook. No teardown module
contains a backend name, apart from the one place where each built-in
registers itself.

## Why (traced 2026-10-13)

- `mini_ork/web/control.py:229-292` (`kill_run`): a `docker` branch shells
  `docker rm -f`, and a `remote` branch hand-builds a `RemoteWorkspace` (~45
  lines, private attribute `_sid`) to `kill_all` + `down`. A new backend or
  provider leaks until the 6 h TTL.
- `mini_ork/runtime/sandbox_reaper.py:200-207` `reap_sandboxes` knows only
  `docker`/`microvm` and raises for anything else. `reap_by_marker`
  (`:212-259`) reaps only `docker` and silently drops every other marker.
- `mini_ork/cli/sandbox_gc.py:43` hardcodes `choices=("all", "docker",
  "microvm")`.
- `mini_ork/runtime/workspace_session.py:136-160` `_extract_session_id`
  branches on `backend == "docker"` / `"remote"` and reads private attributes.
- Cloud node providers now exist as a contract
  (`mini_ork/remote/providers/base.py`: `reap_node_providers`,
  `known_node_providers`). Nothing sweeps them yet.

## Requirements

1. **Sweep registry** in `sandbox_reaper.py`:
   - `register_sandbox_reaper(name, fn)`, where `fn(*, max_age_s: int,
     dry_run: bool) -> list[str]`.
   - Built-ins `docker` → `reap_docker` and `microvm` → `reap_microvm`,
     registered at import (dict insert only).
   - `known_sandbox_reapers() -> list[str]`.
   - `reap_sandboxes(*, backend="all", max_age_s=None, dry_run=False, env=None)`:
     - `"all"` runs every registered sweep **and**
       `mini_ork.remote.providers.reap_node_providers(max_age_s=…,
       dry_run=…, env=env or os.environ)`, merged into one
       `{name: [ids]}` dict.
     - A single name runs that sweep, or that node provider if the name is in
       `known_node_providers()`.
     - Anything else → `ValueError` listing both sets.
     - Each sweep stays fail-open: one that raises reports `[]`.
2. **Marker-reaper registry** in `sandbox_reaper.py`:
   - `register_marker_reaper(backend, fn)`, where `fn(info: dict, run_dir:
     Path, *, dry_run: bool) -> list[str]`, plus a lazy map
     `_LAZY_MARKER_REAPERS = {"remote": "mini_ork.runtime.backends.remote"}`,
     so a process that never imported the backend (the `serve` process running
     `kill_run`) still finds the hook.
   - `reap_by_marker` reads the marker, resolves the hook by
     `info["backend"]`, calls it, and unlinks the marker (unless `dry_run`).
   - The built-in `docker` marker reaper is today's `docker rm -f <cid>` logic,
     moved, not rewritten.
   - Backends with no hook (e.g. `local`) keep today's behavior: unlink, return
     `[]`.
   - Never raises.
3. **Remote marker reaper** in `mini_ork/runtime/backends/remote.py`: a
   module-level `_reap_remote_marker(info, run_dir, *, dry_run)`, registered at
   import next to `register()`. It contains the logic now inline in
   `control.kill_run`'s remote branch: build the workspace from the marker's
   `node` / `token_env`, set the session id, `kill_all(budget_s=10.0)`, then
   `down()`. It is best-effort and never raises.

   Also add `RemoteWorkspace.session_id() -> str` (`self._sid or ""`).
4. **`kill_run`** (`web/control.py`): replace the whole marker block (the
   `docker` and `remote` branches) with one call to
   `sandbox_reaper.reap_by_marker(run_dir)`. Keep `_marker_reaped` and the
   surrounding behavior identical.
5. **`_extract_session_id`** (`workspace_session.py`): if the workspace has a
   callable `session_id()`, use its result. Otherwise use the first non-empty
   of the private `_cid`, `_sid`, `_name`, else `""`. No backend-name
   comparison remains.
6. **`sandbox-gc`** (`cli/sandbox_gc.py`): `--backend` choices are computed
   when the parser is built: `["all", *sorted(known_sandbox_reapers() ∪
   known_node_providers())]`. Output format unchanged.

## Files in scope (touch ONLY these)

- `mini_ork/runtime/sandbox_reaper.py`
- `mini_ork/runtime/workspace_session.py` (`_extract_session_id` only)
- `mini_ork/runtime/backends/remote.py` (marker reaper + `session_id()` only)
- `mini_ork/web/control.py` (`kill_run` marker block only)
- `mini_ork/cli/sandbox_gc.py`
- `tests/unit/test_sandbox_reaper.py` (extend), `tests/unit/test_sandbox_teardown_registry.py` (new)

## Out of scope

- `mini_ork/runtime/sandbox.py`, `agent_workspace.py`, and the docker/microvm
  factories: epic A1 is editing those in parallel. Do not touch them.
- Provisioning nodes through providers, the `static` provider module, and
  profile `provider:` (epic B).

## Run rules

- Never rebase, merge, reset or stash during the run. The diff is the tree delta.
- Run tests with `python3.11 -m pytest`. For CLI reads in a worktree use
  `MINI_ORK_ROOT=$PWD PYTHONPATH=$PWD python3.11 -m mini_ork.cli.main sandbox-gc --dry-run`
  (`bin/mini-ork` runs MAIN).

## Verification command

```bash
python3.11 -m pytest -q tests/unit/test_sandbox_teardown_registry.py \
  tests/unit/test_sandbox_reaper.py tests/unit/test_workspace_session.py \
  tests/unit/test_remote_live_and_kill.py tests/unit/test_remote_node_hardening.py \
  tests/unit/test_remote_workspace.py tests/unit/test_node_providers_contract.py \
  tests/unit/test_ide_pages_nodes.py
ruff check mini_ork/runtime mini_ork/web/control.py mini_ork/cli/sandbox_gc.py
MINI_ORK_ROOT=$PWD PYTHONPATH=$PWD python3.11 -m mini_ork.cli.main sandbox-gc --dry-run --backend all
```

## Acceptance (each bullet has its own test in `test_sandbox_teardown_registry.py`)

- A reaper registered under a new name (`"plugbox"`) is run by
  `reap_sandboxes(backend="all")` and by `reap_sandboxes(backend="plugbox")`,
  and appears in `sandbox-gc --help` choices. No teardown module was edited to
  add it.
- `reap_sandboxes(backend="all")` includes the node providers' results: with
  a fake provider registered through
  `mini_ork.remote.providers.register_node_provider`, its handles appear under
  its name. A provider whose `reap` raises reports `[]`.
- `reap_by_marker` with a marker `{"backend": "plugbox", ...}` calls the
  registered marker hook with the parsed marker and the run dir, then unlinks
  the marker. With `dry_run=True` it leaves the marker.
- `kill_run` on a run dir whose marker says `remote` invokes the remote marker
  reaper. Prove it by monkeypatching the registered hook and calling the
  `kill_run` entrypoint, not the helper.
- The remote marker reaper, driven against the in-process node-agent fixture
  already used by `test_remote_live_and_kill.py`, kills the session's procs
  and deletes the session, exactly as the old inline branch did. The existing
  `test_remote_live_and_kill.py` kill tests pass unmodified.
- The docker marker path still issues `docker rm -f <cid>` through the single
  `_run` seam (faked; no daemon).
- `_extract_session_id` returns `ws.session_id()` when defined, and the
  `_cid`/`_sid`/`_name` fallback otherwise.
  `git grep -n 'backend == "docker"\|backend == "remote"' mini_ork/runtime/workspace_session.py mini_ork/runtime/sandbox_reaper.py mini_ork/web/control.py`
  returns nothing.

## Review bar

Rejected unless every Acceptance bullet has its own test driving the
production entrypoint (`kill_run`, `reap_sandboxes`, the `sandbox-gc` CLI
`main`). Every new public function must have a production caller. The
pre-existing tests in the verification command must pass unmodified.
