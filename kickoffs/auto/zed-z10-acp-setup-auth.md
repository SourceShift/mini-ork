# Zed Z10 — first-run setup as ACP terminal auth (`mini-ork acp --setup`)

Plan: `docs/plans/2026-10-03-zed-integration.md`. Depends on Z6 merged (serial: every Zed
slice edits `mini_ork/acp/agent.py`).

## Goal

A new user who adds mini-ork to Zed has nothing to tell them what is missing: no
`.mini-ork` in the project, no `claude` CLI or login for the orchestrator, no keys for
the worker lanes. Their first thread just fails. ACP has a standard answer — auth
methods: the agent advertises a **terminal** method, the editor runs the agent with the
method's args in a terminal for an interactive setup, then calls `authenticate`. The
ACP Registry also requires every listed agent to advertise one (its CI checks the
handshake). Add an interactive setup and advertise it.

## Files in scope

- `mini_ork/acp/setup.py` — NEW: readiness check + interactive setup.
- `mini_ork/cli/acp_cmd.py` — accept `--setup`.
- `mini_ork/acp/agent.py` — `auth_methods` in `initialize`, `authenticate`, gate on new threads.
- `tests/unit/test_acp_setup.py` — NEW.
- `tests/unit/test_acp_agent_py.py` — extend.

No other file changes.

## Mechanism (exact spec)

### `setup.py`

```python
@dataclass
class Check:
    name: str          # "project", "orchestrator", "lanes"
    ok: bool
    detail: str        # one plain line for the user
    fix: str = ""      # the command or step that fixes it

def readiness(cwd: Path, *, lane: str | None = None) -> list[Check]
def run_interactive(cwd: Path, *, stdin=sys.stdin, stdout=sys.stderr) -> int
```

`readiness` (no prompts, no network beyond the `claude` subprocess, each check
independent and never raising):
1. **project** — `<cwd>/.mini-ork` is a directory. Fix: `mini-ork init`.
2. **orchestrator** — the lane (`lane` or `acp_orchestrator.config.default_lane(home)`):
   for a subscription lane (`opus`/`sonnet`): `shutil.which("claude")` exists and
   `claude auth status --json` (timeout 15s, a module-level `_run` seam) returns JSON
   with `"loggedIn": true`. Use only that field — never log or return the email, org or
   any other field of that output. Fix: install Claude Code / `claude auth login`. For
   another claude-CLI lane: `claude` exists and `mini_ork.dispatch.providers` reports the
   lane's credentials present (reuse what `mini-ork providers status <lane>` uses — read
   `mini_ork/cli/providers*` first). Fix: `mini-ork providers configure <lane>`.
3. **lanes** — the worker roles in the home's lane map (`mini_ork.web.recipes.load_lanes(home)`;
   roles `implementer`, `reviewer`, `planner` when present): each lane's credentials
   present (same helper). Detail lists missing lanes. Fix:
   `mini-ork providers configure <lane>` for each.

`run_interactive`: print a short header, run `readiness`, print each check as
`✓/✗ name — detail`; for each failing check with a fix, ask `Fix now? [y/N]` and on `y`
run it as a subprocess attached to the terminal (`mini-ork init`;
`claude auth login`; `mini-ork providers configure <lane>` — the `mini-ork` launcher is
`[sys.executable, <engine root>/bin/mini-ork, ...]`), then re-check. Exit 0 when every
check passes, else 1 with the remaining fixes listed. All prompts/output on the given
streams (stderr by default — never stdout, which is the ACP wire in agent mode).

### `acp_cmd.py`

`mini-ork acp --setup` → `setup.run_interactive(Path.cwd())` and return its code; does
not need the `acp` extra. Update the usage text.

### `agent.py`

- `initialize` returns `auth_methods=[TerminalAuthMethod(type="terminal",
  id="mini-ork-setup", name="Set up mini-ork", description="Check the project, the
  orchestrator's Claude login and the worker lanes' keys, and fix what is missing.",
  args=["acp", "--setup"], env={})]`.
- `async def authenticate(self, method_id, **kwargs)`: unknown id → invalid params;
  otherwise run `readiness(Path(self._resolve_home()).parent)` (in a thread:
  `asyncio.to_thread`); all ok → `AuthenticateResponse()`; else raise
  `RequestError.auth_required({"message": <failing checks, one line each>})`.
- `new_session` for a THREAD (not a client-minted run id): when the `orchestrator` check
  for the session's cwd fails, raise `RequestError.auth_required(...)` with the check's
  detail and fix, so Zed offers the setup. Cache a passing result per cwd for 10 minutes
  (the `claude auth status` call is not free). Project/lanes checks do not block a
  thread (direct-mode runs report their own errors). `MO_ACP_SKIP_SETUP_CHECK=1`
  disables the gate.

## Tests

`tests/unit/test_acp_setup.py` (the `_run` seam and `shutil.which` monkeypatched — never
call the real `claude`): each check ok/failing; `loggedIn` false and non-JSON output both
fail; the returned detail never contains fields other than login state (feed an output
with an email and assert it is absent); `run_interactive` with scripted stdin: declining
leaves exit 1, accepting runs the fix (seam) and re-checks to exit 0; output goes to the
given stream, nothing to stdout.

`tests/unit/test_acp_agent_py.py` (no pytest-asyncio — `asyncio.run`; keep the autouse
guard): `initialize` advertises the terminal method with args `["acp", "--setup"]`;
`authenticate` succeeds when readiness passes and raises auth_required when it fails, and
rejects an unknown method id; a thread `new_session` raises auth_required when the
orchestrator check fails, succeeds when it passes, and is not gated with
`MO_ACP_SKIP_SETUP_CHECK=1`; existing tests keep passing (set that env var in an autouse
fixture of the existing module if they would otherwise hit the real check — say so in a
comment).

## Success criteria

- `python3.11 -m pytest -q -p no:asyncio tests/unit/test_acp_setup.py tests/unit/test_acp_agent_py.py tests/unit/test_acp_commands.py tests/unit/test_acp_threads.py tests/unit/test_acp_history.py` passes.
- `ruff check` clean; diff touches only the files in scope.
