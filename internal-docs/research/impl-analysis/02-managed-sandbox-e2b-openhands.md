# Impl analysis 02 — Managed cloud sandbox + controller/runtime split (E2B + OpenHands)

**Gap targeted:** mini-ork has no managed/remote execution and no controller↔runtime split —
the orchestrator and the worker share one host FS. To run agents in the cloud you need (a) a
sandbox the orchestrator does NOT share a disk with, and (b) a transport so the same recipe
drives a local or remote sandbox. doc 01 covered the *interface* (swe-rex/mini-swe-agent); this
covers the *managed/cloud* realization.

**Sources read:** `/private/tmp/miniork-ref-analysis/code-interpreter` (E2B SDK) and
`…/OpenHands` (new `openhands/app_server/sandbox/*` service architecture).

---

## A. E2B — managed microVM sandbox SDK (`e2b-dev/code-interpreter`)
`class Sandbox(BaseSandbox)` (`code_interpreter_sync.py:34`). Each sandbox is a **Firecracker
microVM** created over the E2B API. Surface:
- `Sandbox(...)` / `Sandbox.create(...)` → boots a microVM (own kernel, memory, no shared state).
- `sandbox.run_code(code, language=...)` → code-interpreter execution with rich results.
- `sandbox.commands.run(cmd)` → arbitrary shell.
- `sandbox.files.read/write/list(...)` → **filesystem transfer** (the orchestrator never needs
  the files locally).
- `sandbox.set_timeout(...)`, `sandbox.kill()` → lifecycle.
- `create_code_context(...)` → a persistent execution context (stateful kernel) vs one-shot.

The point for mini-ork: you get a **real isolated FS + file put/get over an API**, so execution
is fully decoupled from the orchestrator's disk. Self-hostable (E2B is OSS, Firecracker).

## B. OpenHands — the controller/runtime split as a service (`openhands/app_server/sandbox/`)
The new OpenHands moved the runtime behind a **`SandboxService(ABC)`** (`sandbox_service.py:30`):
```python
class SandboxService(ABC):
    async def start_sandbox(...) -> SandboxInfo
    async def resume_sandbox(id) -> bool
    async def pause_sandbox(id) -> bool
    async def delete_sandbox(id) -> bool
    async def wait_for_sandbox_running(...)
    async def archive_conversation_workspace(...)   # ship the workspace out
    async def pause_old_sandboxes(max_num) -> [...]  # pooling / GC
    def _get_agent_server_url(self, sandbox) -> str  # the agent server runs INSIDE the sandbox
    async def _check_agent_server_alive(...)
```
Typed lifecycle models (`sandbox_models.py`): `SandboxStatus` (STARTING/RUNNING/PAUSED/ERROR),
`SandboxInfo` (id, spec_id, status, **`exposed_urls: list[ExposedUrl]`**), `SandboxRecord`
(persisted identity).

**Two interchangeable implementations of the SAME interface:**
- `ProcessSandboxService` (local): `subprocess.Popen` to launch the agent server on a localhost
  port, then `httpx` health-checks `http://localhost:{port}/alive` and talks to it over HTTP.
- `RemoteSandboxService` (cloud): same contract, HTTP to a remote sandbox host.

So **local == remote** to the controller — it always talks to an "agent server" over HTTP at an
exposed URL; only *where that server runs* changes. This is the same shape as swe-rex
(runtime-inside-sandbox + HTTP client) — convergent design across the two leaders.

Key extras mini-ork lacks entirely:
- **Workspace archiving** (`workspace_archive.py`, `archive_conversation_workspace`) — tar the
  workspace in/out so the orchestrator never needs the files on its own disk.
- **Pause/resume + pooling** (`resume_sandbox`, `pause_old_sandboxes`) — cost control for cloud.
- **Exposed-URL model** — a sandbox advertises its reachable endpoints.

---

## C. The convergent cloud pattern (E2B + OpenHands + swe-rex all agree)
1. **Agent/runtime server runs INSIDE the sandbox**, exposed over HTTP.
2. **Controller is a thin HTTP client** to that server (local port or remote URL).
3. **Typed lifecycle**: start → wait-alive → (pause/resume) → delete.
4. **Workspace moves via archive/file-transfer**, not a shared mount.
5. **Backend is swappable** behind one service interface (process/docker/microVM/remote).

mini-ork today violates 1–4 (worker is a child process on the orchestrator's host, shared disk).

---

## D. Adoption plan for mini-ork (R3 — builds on doc 01's R0/R1/R2 seam)

### Step 1 — a "mini-ork agent server" inside the sandbox
Wrap the existing node-execution logic so it can run as a tiny HTTP server *inside* a sandbox
exposing: `POST /exec` (run a recipe node's command), `GET/PUT /files` (workspace transfer),
`GET /alive`. This is the OpenHands `_get_agent_server_url` + `/alive` pattern. The orchestrator
becomes an HTTP client (`lib/runtime/remote.sh` implementing doc 01's `mo_env_*` contract via curl).

### Step 2 — a `SandboxService`-style bash contract
`lib/sandbox/service.sh`:
```
mo_sbx_start  <spec>  -> sandbox_id (+ exposed url)     # process|docker|e2b|daytona backend
mo_sbx_alive  <id>    ;  mo_sbx_pause/resume/delete <id>
mo_sbx_archive_in <id> <local_workspace>               # tar workspace -> sandbox
mo_sbx_archive_out <id> <local_dest>                   # tar results <- sandbox
```
Backends: `process` (local subprocess+localhost, the OpenHands ProcessSandboxService analog),
`docker`, and **managed `e2b`/`daytona`** (curl their REST API). Default `process` for dev.

### Step 3 — wire into the run lifecycle
`bin/mini-ork-execute`: at run start `mo_sbx_start` + `archive_in` the worktree; each node's
command goes through `mo_env_exec` → (remote backend) → the in-sandbox `/exec`; at publish,
`archive_out` the diff/artifacts; `mo_sbx_delete` (or `pause` for pooling). Add
`pause_old_sandboxes`-style GC + a cost cap per sandbox (ties into I-4).

### Build vs wrap
- **Wrap E2B/Daytona** for untrusted/cloud (don't build microVM mgmt) — fastest path to cloud.
- **Self-host** the `process`/`docker` backends for dev/CI.
- Reuse doc 01's `mo_env_*` contract as the in-sandbox exec surface so local and remote share code.

**Effort:** high (the agent-server-in-sandbox + archive transport is the real work). **Payoff:**
true cloud execution, sandbox pooling/cost control, and — combined with doc 01's bubblewrap —
a complete isolation story that closes A1.
