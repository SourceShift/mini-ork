# Epic — Sandbox P2: per-agent Docker environments + shared drive

**Status:** IN PROGRESS (opened 2026-07-31)
**Owner:** Amir + Claude
**Roadmap:** cloud-exec sandbox A1 → P2 (first *real* cross-environment backend).
**Design context:** `internal-docs/research/2026-07-30-sandbox-shared-drive-design.md`
(gitignored); prior phases `kickoffs/sandbox-p0-workspace-protocol.md`,
`mini_ork/runtime/{sandbox,shared_drive,run_drive}.py` (P0/P1a/P1b, on main).

---

## Why (the user ask, verbatim intent)

> Run mini-ork as the **main brain** on the host, spawning each agent in a
> **different environment**, with all agents able to read/write a **shared set
> of directories** accessible to every agent in the run.

Today (verified 2026-07-31, post-reconcile at merge `555f788b`):
- **Brain spawning agents** — already works (executor + dispatch).
- **Shared directory** — works *on one host only*, opt-in via `local-bind`
  (`MO_SHARED_DRIVE_BACKEND=local-bind`), and only the implementer cwd is
  routed (`mini_ork/cli/execute.py:2251`).
- **Each agent in a different env** — **does NOT work.** There is exactly one
  environment: the host. `get_workspace`/`Workspace.exec` has **zero callers**
  outside `sandbox.py`; only the `local` backend is registered; agents run as
  host subprocesses. The `Workspace` protocol (P0) is the seam, unused.

This epic delivers the first backend that puts **each agent in its own Docker
container** while every container **bind-mounts the same shared drive**, so the
user's ask is satisfied on a single machine with **no cloud credentials**
(Docker 29.2.1 via colima is present). Cross-*machine* (cloud Volumes) remains
a later phase (P3/P4) and is explicitly out of scope here.

## Non-goals (protect scope)
- No cloud provider backends (e2b/modal/daytona) — P3+.
- No microVM/gVisor — Docker first, harden later.
- No change to default behavior: everything below is **opt-in** and a **no-op**
  unless `MO_SANDBOX_BACKEND` is set. `MO_SANDBOX_BACKEND=local` (default) =
  byte-for-byte today's host execution.
- No new orchestration model — reuse `Workspace`/`SharedDrive` protocols and the
  registry seams already shipped.

---

## Design — three pieces (each a sub-phase)

### Piece 1 — `DockerWorkspace` backend  (`Workspace` impl)
A backend registered as `docker` satisfying the existing `Workspace` protocol
(`sandbox.py`): 
- `up()` → `docker run -d` a long-lived container from `MO_SANDBOX_IMAGE`,
  bind-mounting the run's shared-drive host dir at a fixed in-container path
  (`/workspace`), passing through provider env (API keys / gateway base_urls),
  cpu/memory limits from `MO_SANDBOX_CPU`/`_MEMORY`. Container name is
  run+node scoped so parallel agents get distinct containers.
- `exec(cmd, *, cwd, timeout)` → `docker exec -w <cwd> <cid> sh -lc <cmd>`,
  merged stdout+stderr, returncode; timeout kills via `docker exec` wrapper +
  `docker stop`. Mirror `mo_runtime_exec`'s `(rc, output)` return.
- `put(content)` / `get(path)` → `docker cp` (or `exec sh -c 'cat > …'`).
- `down()` → `docker rm -f <cid>`. Container = **cattle** (safe to destroy);
  the drive = **pet** (never destroyed here — that's `SharedDrive.down`).
- Register via `register_workspace_backend("docker", …)` at import of a new
  `mini_ork/runtime/backends/docker.py` (keep `sandbox.py` dependency-free;
  the docker backend imports only stdlib `subprocess`/`shutil`, no docker SDK —
  shell out to the `docker` CLI so there is no new pip dependency).

**Acceptance (P1):**
1. `mini_ork/runtime/backends/docker.py` defines `DockerWorkspace` + registers
   `docker`. `get_workspace("docker", image=…, drive_root=…, mount_path="/workspace")`
   returns a live instance; `get_workspace("docker")` with no daemon raises a
   clear `RuntimeError` (not a traceback).
2. Round-trip test (skipped when `docker` absent): `up()` →
   `put`/`get` a file on the mounted drive → `exec("echo hi", cwd="/workspace")`
   returns `(0, "hi\n")` → a file written by `exec` is visible on the host drive
   dir (proves the bind mount) → `down()` removes the container.
3. `exec` timeout kills a runaway (`sleep 999`, timeout=2 → non-zero, container
   still healthy or torn down; no host hang).
4. Nothing in the default path imports this module; adding it changes no
   existing test.

### Piece 2 — route agent execution through the `Workspace` seam (opt-in)
Wire the two execution seams so that, when `MO_SANDBOX_BACKEND` is set, the
agent runs **inside the workspace** instead of a host subprocess:
- **Agent tool-exec:** `mini_ork/agent/minimal.py` currently calls
  `mo_runtime_exec` directly. Route through `get_workspace(backend).exec(...)`
  (the `local` backend already delegates to `mo_runtime_exec`, so `local` is a
  no-op refactor; `docker` sends the tool command into the container).
- **Provider transport:** the coding-agent CLI launch
  (`dispatch/core.py:58`, `dispatch/lane_helpers.py`, `dispatch/codex_transport.py:403`)
  is the *stronger* isolation point — run the whole agent CLI in the container.
  This is riskier (needs the agent binary + keys + network egress inside the
  image). Gate it behind `MO_SANDBOX_SCOPE=tool|agent` (default `tool` = only
  route tool-exec; `agent` = route the CLI too). Ship `tool` first.
- The run provisions ONE `SharedDrive` (P1a) and passes its `mount_path` to each
  `DockerWorkspace` as the bind source, so every agent's container sees the same
  `/workspace`. Reuse `resolve_run_drive_cwd`; the cwd handed to `exec` is the
  in-container mount path, not the host path.

**Acceptance (P2):**
1. `MO_SANDBOX_BACKEND` unset → identical behavior + identical test output as
   today (dead-code proof, like P1b: full green gate unchanged).
2. `MO_SANDBOX_BACKEND=local` → agent tool-exec routes through `LocalWorkspace`
   with no observable change (parity test).
3. `MO_SANDBOX_BACKEND=docker MO_SANDBOX_SCOPE=tool` on a toy 2-node run →
   each node's shell tool runs in a container; a file one node writes to
   `/workspace/shared.txt` is read by the next node (proves cross-agent shared
   dir across **distinct environments**). Concurrency still guarded by CAID
   `--owns`.
4. Loud failure: unknown backend / dead daemon → `ValueError`/`RuntimeError`
   surfaced, never a silent host fallback.

### Piece 3 — verifier / ranking hooks (defer, tracked only)
Behavioral verification of the sandbox (does the agent's container actually
produce the artifact?) belongs to the **behavioral-verify** epic
(`wt/behavioral-verify-p0`). Cross-link only; not built here.

---

## Env surface (all opt-in)
| Var | Meaning | Default |
|---|---|---|
| `MO_SANDBOX_BACKEND` | `local` \| `docker` | `local` (host) |
| `MO_SANDBOX_SCOPE` | `tool` \| `agent` | `tool` |
| `MO_SANDBOX_IMAGE` | container image | e.g. `mini-ork/agent:latest` |
| `MO_SANDBOX_CPU` / `_MEMORY` | container limits | unset (no limit) |
| `MO_SANDBOX_TIMEOUT` | per-exec timeout | inherit node timeout |
| `MO_SHARED_DRIVE_BACKEND` | `local-bind` (P1a) — drive backing the mount | unset |
| `MO_SHARED_DRIVE_ROOT` | host dir bind-mounted into every container | run cwd |

## Files in scope
- `mini_ork/runtime/backends/__init__.py` (NEW)
- `mini_ork/runtime/backends/docker.py` (NEW — DockerWorkspace)
- `tests/unit/test_docker_workspace.py` (NEW — daemon-gated)
- `mini_ork/agent/minimal.py` (P2 — route tool-exec through Workspace)
- `mini_ork/cli/execute.py` (P2 — provision drive→workspace, pass mount)
- `tests/unit/test_sandbox_wiring.py` (NEW — parity + opt-in no-op proofs)
- this epic file (status updates)

## Risks & mitigations
- **Dispatch hot-path regression** (Piece 2 touches how every agent runs). →
  ship Piece 1 (additive, unused) first; Piece 2 behind opt-in env with a
  parity test proving `local` == today; never default-on.
- **Container can't reach the LLM gateway** (agent-scope). → start with
  `tool`-scope only; agent-scope needs egress + keys baked/passed — defer.
- **Bind-mount perms on colima/macOS** (uid mapping). → test writes both
  directions in Piece 1 acceptance #2; document the colima mount caveat.
- **Leaked containers** on crash. → `down()` idempotent `rm -f`; a run-scoped
  label + a `docker ps -f label=… | rm` sweep in teardown.
- **Drive is the pet** — never `rm` the mount source in `Workspace.down`; only
  `SharedDrive.down(ephemeral=True)` may.

## Test / green-gate plan
- Unit: `test_docker_workspace.py` (skip w/o daemon), `test_sandbox_wiring.py`.
- Parity: `MINI_ORK_TEST_CMD` scoped to the two new files + existing
  `test_sandbox_protocol.py` / `test_shared_drive_protocol.py` / `test_run_drive.py`.
- Full `python3 -m pytest -q` green before each merge.
- Manual E2E (Piece 2 #3): toy 2-node run writing/reading `/workspace/shared.txt`
  across two containers.

## Rollout / rollback
- Worktree-first (`make worktree SLUG=sandbox-p2-docker OWNS="mini_ork/runtime tests"`),
  merge per phase (Piece 1, then Piece 2). Default path unchanged, so rollback =
  leave `MO_SANDBOX_BACKEND` unset; no revert needed. Reconcile note: keep local
  `main` fast-forwarded (see the divergence gotcha from 2026-07-31).

---

## Task ledger
- [ ] **P1** DockerWorkspace backend + daemon-gated unit tests
- [ ] **P2a** route agent tool-exec through Workspace (opt-in, `local` parity)
- [ ] **P2b** provision run drive → mount into each container; 2-node E2E
- [ ] **P2c** provider-transport (agent-scope) routing — behind `MO_SANDBOX_SCOPE=agent`
- [ ] **P3** (separate epic) cloud Volume backends, behavioral-verify cross-link
- Log each merge commit here as it lands.
