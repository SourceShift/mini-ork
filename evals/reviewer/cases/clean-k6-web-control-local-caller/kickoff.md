# ECOSYSTEM §6 · K6: stop / kill / answers refuse foreign-origin callers

## Goal

`mini_ork/web/routes/control.py` exposes three mutating routes with no caller
check (the module docstring calls them "loopback-trust-based"):

- `POST /api/v1/task-runs/{id}/stop` → touches `.stop-requested`
- `POST /api/v1/task-runs/{id}/kill` → SIGTERM then SIGKILL to the run's pid
- `POST /api/v1/task-runs/{id}/answers` → writes planner answers

Binding to 127.0.0.1 does not protect them from a browser. Stop and kill take
no body, so any web page the operator visits can fire them with a `no-cors`
POST (the CORS middleware controls reads, not the side effect), and there is no
Host-header check, so DNS rebinding reaches them too. Run ids are guessable
(`run-<epoch>-<pid>`, slugs). In both cases the browser sends the foreign
page's `Origin`.

The repo already has the right guard: `require_local_caller` in
`mini_ork/web/routes/agent_server.py`, used by `/api/conversations` and the
workspace bash route (`mini_ork/web/routes/workspace.py` imports it with
`from .agent_server import require_local_caller`). Apply it to these three
routes. `require_token` is not used here because it returns 401 on every
install that has no `auth-tokens.txt`, which would break the IDE's stop button.

All paths are relative to the repository root.

## Mechanism (exact spec)

1. In `mini_ork/web/routes/control.py`, import
   `from .agent_server import require_local_caller` (the same way
   `workspace.py` does) and add `dependencies=[Depends(require_local_caller)]`
   to the `@router.post(...)` decorators of `stop`, `kill` and
   `save_answers_route`. Do not change their bodies, status codes or the
   `pause-cost` / `resume-cost` routes (they keep `require_token`).
2. Update the module docstring: the three routes now refuse a foreign
   `Origin` (CSRF / DNS-rebinding guard) through `require_local_caller`; the
   cost routes still need a bearer token.
3. Do not change `require_local_caller` itself.

In-tree callers are unaffected: the IDE uses `board stop`
(`mini_ork/ide_pages/run.py`) and ACP calls the control functions in-process.

## Files in scope (touch ONLY these)

- `mini_ork/web/routes/control.py`
- `tests/unit/test_eco6_k6_control_local_caller.py` (new)

## Tests (`tests/unit/test_eco6_k6_control_local_caller.py`, hermetic)

Build the app the way existing web route tests do (find one with
`git grep -n "TestClient" tests/unit | grep -i control`), with a tmp
`MINI_ORK_HOME` holding `runs/<id>/` and a matching `task_runs` row if the stop
path needs it. Monkeypatch `mini_ork.web.control.kill_run` so no signal is ever
sent.

- `POST /api/v1/task-runs/<id>/stop` with header `Origin: https://evil.example`
  → 403, and `<run_dir>/.stop-requested` does **not** exist afterwards.
- The same POST with no `Origin` header → not 403 (the existing success or 409
  path), and the sentinel is written when the run exists.
- The same POST with `Origin: http://127.0.0.1:7090` → not 403.
- `POST .../kill` with a foreign `Origin` → 403 and the monkeypatched
  `kill_run` is never called.
- `POST .../answers` with a foreign `Origin` and body `{"q": "a"}` → 403 and no
  answers file is written.

## Verification commands

- `python3.11 -m pytest -q -p no:asyncio tests/unit/test_eco6_k6_control_local_caller.py $(git grep -l -E "routes\.control|routes import control|/task-runs/.*/(stop|kill|answers)|require_local_caller" -- tests | tr '\n' ' ')`

## Self-application measurement

```measure
python3.11 -m pytest -q -p no:asyncio tests/unit/test_eco6_k6_control_local_caller.py --junitxml="${MINI_ORK_RUN_DIR}/eco6-k6-junit.xml"
```

## Required artifacts — declare EXACTLY these and nothing else

- `${MINI_ORK_RUN_DIR}/eco6-k6-junit.xml`

## Success criteria

- The verification command passes with every pre-existing test unchanged.
- `ruff check --select F,E9 mini_ork/web/routes/control.py` is clean.
