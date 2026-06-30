# Runtime & Sandbox Abstraction: Source-Code Analysis
## References: mini-swe-agent + swe-rex — Adoption Plan for mini-ork

**Date:** 2026-06-30  
**Scope:** A1 gap — mini-ork agents run directly on the host OS filesystem (`bin/mini-ork-execute`
`cd`s into repos and runs bash/CLI on the host). No per-agent sandbox, no runtime abstraction.
Blocks cloud execution; caused cross-repo git corruption.

**Sources analyzed (real code, no docs):**
- `/private/tmp/miniork-ref-analysis/mini-swe-agent` (v2.4.3) — Python ~100-line agent, stateless `subprocess.run`-per-action, ships local/docker/bubblewrap/singularity backends
- `/private/tmp/miniork-ref-analysis/swe-rex` — "run any command on any environment — local/Docker/Modal/AWS/Daytona"; runtime-interface + deployment abstraction powering SWE-agent

---

## 1. The Runtime / Exec Abstraction

### 1.1 mini-swe-agent: Single `execute(action, cwd)` Interface

mini-swe-agent's entire execution contract is one method, implemented by every backend:

```python
# src/minisweagent/environments/local.py — signature repeated identically in docker.py, bubblewrap.py, singularity.py
def execute(self, action: dict, cwd: str = "", *, timeout: int | None = None) -> dict[str, Any]:
    command = action.get("command", "")
    cwd = cwd or self.config.cwd or os.getcwd()
    result = _run(command, cwd, os.environ | self.config.env, timeout or self.config.timeout)
    output = {"output": result.stdout, "returncode": result.returncode, "exception_info": ""}
    self._check_finished(output)
    return output
```

Returns `{"output": str, "returncode": int, "exception_info": str}`. The **agent loop** in `agents/default.py` calls it without knowing which backend is active:

```python
# agents/default.py:152-155
def execute_actions(self, message: dict) -> list[dict]:
    outputs = [self.env.execute(action) for action in message.get("extra", {}).get("actions", [])]
    return self.add_messages(*self.model.format_observation_messages(message, outputs, self.get_template_vars()))
```

**Statelessness:** Every `execute()` call creates a fresh subprocess — no persistent shell state between calls. The `cwd` is passed per-call; it is not ambient process state. The local backend's `_run` helper starts a new session and kills the whole process group on timeout:

```python
# environments/local.py:72-92  — process-group kill prevents orphans
process = subprocess.Popen(command, shell=True, text=True, cwd=cwd, env=env,
    stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    start_new_session=os.name == "posix")
try:
    stdout, _ = process.communicate(timeout=timeout)
except subprocess.TimeoutExpired:
    os.killpg(process.pid, signal.SIGKILL) if os.name == "posix" else process.kill()
    stdout, _ = process.communicate()
    raise subprocess.TimeoutExpired(command, timeout, output=stdout)
```

### 1.2 swe-rex: Two-Layer Interface (Deployment + Runtime)

swe-rex splits the concern: **what** runs (runtime) vs **where** it runs and its lifecycle (deployment).

#### AbstractRuntime (`src/swerex/runtime/abstract.py`)

```python
class AbstractRuntime(ABC):
    async def is_alive(self, *, timeout=None) -> IsAliveResponse: ...
    async def create_session(self, request: CreateSessionRequest) -> CreateSessionResponse: ...
    async def run_in_session(self, action: Action) -> Observation: ...
    async def close_session(self, request: CloseSessionRequest) -> CloseSessionResponse: ...
    async def execute(self, command: Command) -> CommandResponse: ...
    async def read_file(self, request: ReadFileRequest) -> ReadFileResponse: ...
    async def write_file(self, request: WriteFileRequest) -> WriteFileResponse: ...
    async def upload(self, request: UploadRequest) -> UploadResponse: ...
    async def close(self) -> CloseResponse: ...
```

Two execution paths coexist:
- **Stateless** (`execute`): one-shot subprocess, like `subprocess.run`. Takes `Command(command, cwd, env, timeout, shell, check)`.
- **Stateful sessions** (`create_session` / `run_in_session`): a persistent bash REPL managed via `pexpect`. `BashSession` (in `runtime/local.py`) spawns `/usr/bin/env bash`, sends commands, reads until PS1 returns. Multiple named sessions coexist. Exit codes are extracted by injecting `echo EXITCODESTART$?EXITCODEEND` and parsing the sentinel:

```python
# runtime/local.py:324-337 — reliable exit code extraction in a stateful REPL
self.shell.sendline(f"\necho {_exit_code_prefix}$?{_exit_code_suffix}")
self.shell.expect(_exit_code_suffix, timeout=1)
exit_code_raw: str = _strip_control_chars(self.shell.before)
exit_code = re.findall(f"{_exit_code_prefix}([0-9]+)", exit_code_raw)
```

#### AbstractDeployment (`src/swerex/deployment/abstract.py`)

```python
class AbstractDeployment(ABC):
    async def start(self, *args, **kwargs): ...      # boot the sandbox (+ start runtime server inside)
    async def stop(self, *args, **kwargs): ...       # teardown
    async def is_alive(self, *, timeout=None) -> IsAliveResponse: ...
    @property
    def runtime(self) -> AbstractRuntime: ...        # returns the runtime to use
    def add_hook(self, hook: DeploymentHook): ...    # status callbacks
```

The agent code only touches `deployment.runtime.execute(...)` or `deployment.runtime.run_in_session(...)`. The deployment class is responsible for making that runtime callable — whether local, in a container, or on Modal cloud.

---

## 2. Each Sandbox Backend

### 2.1 mini-swe-agent Backends

#### LocalEnvironment (`environments/local.py`)

No isolation. `subprocess.Popen(command, shell=True, cwd=cwd, env=env, start_new_session=True)`. Workspace = host filesystem at `cwd`. No namespace separation.

#### DockerEnvironment (`environments/docker.py`)

Boot (`__init__` → `_start_container`):
```python
# environments/docker.py:76-99
cmd = [self.config.executable, "run", "-d", "--name", container_name,
       "-w", self.config.cwd, *self.config.run_args, self.config.image,
       "sleep", self.config.container_timeout]
result = subprocess.run(cmd, capture_output=True, text=True, timeout=self.config.pull_timeout, check=True)
self.container_id = result.stdout.strip()
```

Execute (per-call):
```python
# environments/docker.py:107-113
cmd = [self.config.executable, "exec", "-w", cwd]
for key, value in self.config.env.items():
    cmd.extend(["-e", f"{key}={value}"])
cmd.extend([self.container_id, *self.config.interpreter, command])
# interpreter default: ["bash", "-lc"]
```

Container started once, reused for all `execute()` calls. Teardown: async `docker stop || docker rm -f` via `Popen(..., shell=True)` (non-blocking).

#### BubblewrapEnvironment (`environments/extra/bubblewrap.py`)

Creates a per-instance tmpdir workspace on init:
```python
# environments/extra/bubblewrap.py:78-79
self.working_dir = Path(tempfile.gettempdir()) / f"minisweagent-{uuid.uuid4().hex[:8]}"
self.working_dir.mkdir(parents=True, exist_ok=True)
```

Execute — builds the `bwrap` command per-call:
```python
# environments/extra/bubblewrap.py:86-92
cmd = [self.config.executable] + self.config.wrapper_args + ["--bind", cwd, cwd, "--chdir", cwd]
for key, value in self.config.env.items():
    cmd.extend(["--setenv", key, value])
cmd.extend(["bash", "-c", command])
```

Default `wrapper_args`:
```python
["--unshare-user-try",
 "--ro-bind", "/usr", "/usr",  "--ro-bind", "/bin", "/bin",
 "--ro-bind", "/lib", "/lib",  "--ro-bind", "/lib64", "/lib64",
 "--ro-bind", "/etc", "/etc",
 "--tmpfs", "/tmp",  "--proc", "/proc",  "--dev", "/dev",
 "--new-session",
 "--setenv", "PATH", "/usr/local/bin:/usr/sbin:/usr/bin:/bin"]
```

**Key isolation facts:**
- Entire host is **read-only** (`--ro-bind`)
- `/tmp` is a **fresh tmpfs per call** (not shared between calls)
- Only `--bind cwd cwd` is writable — the agent's workspace
- `--unshare-user-try` = unprivileged user namespaces, no root needed
- No persistent container — each `execute()` spawns a fresh `bwrap` process

Teardown: `shutil.rmtree(self.working_dir)`.

#### SingularityEnvironment (`environments/singularity.py`)

Boot: `singularity build --sandbox <tmpdir> <image>` — converts image into a writable sandbox directory on host:
```python
# environments/singularity.py:46-65
sandbox_dir = Path(tempfile.gettempdir()) / f"minisweagent-{uuid.uuid4().hex[:8]}"
subprocess.run([self.config.executable, "build", "--sandbox", sandbox_dir, self.config.image], ...)
```

Execute:
```python
# environments/singularity.py:82-96
cmd = [self.config.executable, *self.config.global_args, "exec", *self.config.exec_args]
# exec_args default: ["--contain", "--cleanenv", "--fakeroot", "--writable"]
if work_dir and work_dir != "/":
    cmd.extend(["--pwd", work_dir])
cmd.extend(["--writable", str(self.sandbox_dir), "bash", "-c", command])
```

`--contain` prevents host home/tmp leaking in. `--cleanenv` scrubs host env. `--fakeroot` gives apparent root. `--writable` allows writes to sandbox. Sandbox dir is durable — all calls share the same mutable filesystem state.

Teardown: `shutil.rmtree(self.sandbox_dir)`.

### 2.2 swe-rex Backends

#### LocalDeployment (`deployment/local.py`)

`start()` instantiates `LocalRuntime(logger=...)`. The `runtime` property returns it directly. No isolation.

#### LocalRuntime (`runtime/local.py`)

`execute(command)` → stateless `subprocess.run`. `create_session` spawns `BashSession` (pexpect). `read_file` / `write_file` / `upload` operate on the local filesystem.

#### DockerDeployment (`deployment/docker.py`) — critical difference from mini-swe-agent

swe-rex does **not** use `docker exec` for commands. It installs a `swerex-server` HTTP server **inside** the container and communicates via HTTP:

```python
# deployment/docker.py:252-282 — start()
token = self._get_token()
cmds = [self._config.container_runtime, "run", "--rm",
        "-p", f"{self._config.port}:8000",
        *self._config.docker_args, "--name", self._container_name, image_id,
        *self._get_swerex_start_cmd(token)]
# _get_swerex_start_cmd = ["/bin/sh", "-c", "swerex-server --auth-token <token>"]
self._container_process = subprocess.Popen(cmds, stdout=PIPE, stderr=PIPE)
self._runtime = RemoteRuntime.from_config(
    RemoteRuntimeConfig(host="http://127.0.0.1", port=self._config.port, auth_token=token))
await self._wait_until_alive(timeout=self._config.startup_timeout)
```

The container runs the swerex server. The client talks to it over HTTP. `docker exec` is never used after boot.

#### RemoteRuntime (`runtime/remote.py`)

Every method is an async HTTP POST to the swerex server with exponential-backoff retry:

```python
# runtime/remote.py:165-196 — _request()
async with aiohttp.ClientSession(...) as session:
    async with session.post(f"{self._api_url}/{endpoint}",
                            json=payload.model_dump(), headers=headers) as resp:
        await self._handle_response_errors(resp)
        return output_class(**await resp.json())
```

Auth via `X-API-Key` header. Exceptions serialized as `_ExceptionTransfer` and re-raised on client. `upload()` uses multipart form-data (zips directories).

#### ModalDeployment (`deployment/modal.py`)

```python
# deployment/modal.py:220-246 — start()
self._sandbox = await modal.Sandbox.create.aio(
    "/usr/bin/env", "bash", "-c", self._start_swerex_cmd(token),
    image=self._image, timeout=int(self._deployment_timeout),
    unencrypted_ports=[self._port], app=self._app, **self._modal_kwargs)
tunnels = await self._sandbox.tunnels.aio()
tunnel = tunnels[self._port]
self._runtime = RemoteRuntime(host=tunnel.url, timeout=self._runtime_timeout, auth_token=token)
```

Modal/Fargate/Daytona all follow the same pattern: boot a container, start swerex-server inside it, expose a URL, create `RemoteRuntime` pointing to that URL. From the agent's perspective, `deployment.runtime.execute(...)` is identical regardless of whether the sandbox is local, Modal, or Fargate.

---

## 3. Backend Selection (Config / Factory)

### mini-swe-agent

`src/minisweagent/environments/__init__.py`:

```python
_ENVIRONMENT_MAPPING = {
    "docker":        "minisweagent.environments.docker.DockerEnvironment",
    "singularity":   "minisweagent.environments.singularity.SingularityEnvironment",
    "local":         "minisweagent.environments.local.LocalEnvironment",
    "swerex_docker": "minisweagent.environments.extra.swerex_docker.SwerexDockerEnvironment",
    "swerex_modal":  "minisweagent.environments.extra.swerex_modal.SwerexModalEnvironment",
    "bubblewrap":    "minisweagent.environments.extra.bubblewrap.BubblewrapEnvironment",
}

def get_environment(config: dict, *, default_type: str = "") -> Environment:
    config = copy.deepcopy(config)
    environment_class = config.pop("environment_class", default_type)
    return get_environment_class(environment_class)(**config)
```

Selection by `environment_class` field in the YAML config. CLI flag `--environment-class` overrides. The benchmark runner passes `environment_class: docker` (or `singularity`, `bubblewrap`, etc.) at dispatch time; the agent code does not change.

### swe-rex

`src/swerex/deployment/config.py` defines a `DeploymentConfig` union with a `type` discriminator:

```python
DeploymentConfig = (
    LocalDeploymentConfig | DockerDeploymentConfig | ModalDeploymentConfig
    | FargateDeploymentConfig | RemoteDeploymentConfig | DaytonaDeploymentConfig
)

def get_deployment(config: DeploymentConfig) -> AbstractDeployment:
    return config.get_deployment()   # each config class implements get_deployment()
```

```python
class DockerDeploymentConfig(BaseModel):
    type: Literal["docker"] = "docker"    # discriminator
    image: str = "python:3.11"
    ...
    def get_deployment(self) -> AbstractDeployment:
        from swerex.deployment.docker import DockerDeployment
        return DockerDeployment.from_config(self)
```

A single YAML key `type: docker` selects the entire backend stack (boot, runtime, teardown). The agent never changes.

---

## 4. Parallelism

### mini-swe-agent

`src/minisweagent/run/benchmarks/swebench.py`:

```python
# swebench.py:256-262
with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
    futures = {
        executor.submit(process_instance, instance, output_dir, config, progress_manager)
        for instance in instances
    }
```

Each `process_instance` call creates its own `env = get_sb_environment(config, instance)` — a fresh `DockerEnvironment` with its own container — and its own `DefaultAgent`. Each container is isolated; agents cannot corrupt each other's filesystems. The only shared resource is the host Docker daemon socket. A file-level mutex (`_OUTPUT_FILE_LOCK`) serializes writes to the shared predictions JSON.

**Parallelism degree:** `--workers N` CLI flag.

### swe-rex

swe-rex is a library; parallelism is the caller's responsibility. Because `RemoteRuntime` is just a host+port+token struct, multiple independent callers can create independent deployments and run them concurrently without interference even on the same host.

---

## 5. mini-ork Current State (What Exists, What's Missing)

### What Exists

mini-ork has `lib/sandbox/` with a 4-function contract:
- `mo_sandbox_<backend>_provision <child_run_id>` → workspace path
- `mo_sandbox_<backend>_dispatch <workspace> <recipe> <kickoff>`
- `mo_sandbox_<backend>_retrieve <workspace> <run_dir>`
- `mo_sandbox_<backend>_cleanup <workspace>`

Backends: `local.sh` (complete), `modal.sh` (stub — falls back to local), `daytona.sh` (stub — falls back to local).

### The Gap

`lib/sandbox/` covers **run-level provisioning** (where to put artifacts). It does NOT intercept **command-level execution** (how individual bash commands and LLM calls actually run). The true execution path is:

1. `bin/mini-ork-execute` parses `plan.json`/`workflow.yaml` and dispatches node types.
2. Per node it calls `lib/llm-dispatch.sh:mo_llm_dispatch <model> <prompt> <output>` — which sources a `cl_*.sh` wrapper and then runs `claude --print`/`kimi`/etc. directly on the **host process**, inheriting all env and cwd.
3. Verifiers run as `( cd "$_verify_cwd" && bash "$_script" )` on the **host filesystem** (line 114 of `bin/mini-ork-execute`).
4. The implementer writes files via Claude's Edit/Write tools, which write directly into the **host git worktree** (`$MO_TARGET_CWD`).

**There is no intercept point between "node dispatched" and "bash command runs on host."** The modal/daytona adapters in `lib/sandbox/` never actually send commands into a container; `mo_sandbox_modal_dispatch` calls `mo_sandbox_local_dispatch` which still does `cd "$_workspace" && mini-ork run ...` on the host.

The cross-repo git clobber happened because two implementer nodes running in `parallel` dispatch mode both `cd`'d into the same target repo and issued git commands — no filesystem isolation existed to prevent it.

---

## 6. Adoption Plan

### R0 — Stateless Exec Through One Function (1–2 days, pure bash, zero new deps)

**Goal:** Introduce `mo_runtime_exec` as the single execution seam. Refactor every naked `cd + bash` pattern to call it. No isolation yet — just the hook point.

**New file:** `lib/runtime/local.sh`

```bash
#!/usr/bin/env bash
# lib/runtime/local.sh — local (no-isolation) runtime backend.
# Implements the mo_runtime_exec/put/get contract for the host filesystem.
# This is the R0 baseline: same behavior as today, but through an explicit seam.

mo_runtime_exec() {
  local _cmd="$1"
  local _cwd="${2:-$PWD}"
  local _timeout="${3:-120}"
  # Run in a subshell so env/cwd changes don't propagate.
  # Use setsid + process-group kill (matches mini-swe-agent's orphan fix).
  ( cd "$_cwd" && timeout "$_timeout" bash -c "$_cmd" )
}

mo_runtime_put() { local _src="$1" _dst="$2"; cp -R "$_src" "$_dst"; }
mo_runtime_get()  { local _src="$1" _dst="$2"; cp -R "$_src" "$_dst"; }

# Sessions: no-op for local backend (each exec is already independent)
mo_runtime_session_create() { : ; }
mo_runtime_session_run() {
  local _session_id="$1" _cmd="$2" _cwd="${3:-$PWD}"
  mo_runtime_exec "$_cmd" "$_cwd"
}
mo_runtime_session_close() { : ; }
```

**New file:** `lib/runtime.sh` (router, sourced by mini-ork-execute at startup):

```bash
#!/usr/bin/env bash
# lib/runtime.sh — runtime backend router.
MO_RUNTIME_BACKEND="${MO_RUNTIME_BACKEND:-local}"
source "${MINI_ORK_ROOT}/lib/runtime/${MO_RUNTIME_BACKEND}.sh"
```

**Change to `bin/mini-ork-execute`** startup block (after existing `_require_lib` calls):
```bash
source "$MINI_ORK_ROOT/lib/runtime.sh"
```

**Change to `_run_verifier_ref`** (replaces line 114):
```bash
# Before:
( cd "$_verify_cwd" && MINI_ORK_PLAN_PATH="$PLAN_PATH" ARTIFACT_PATH="$ARTIFACT_PATH" \
  bash "$_script" ) > "$_evidence" 2>&1

# After:
MINI_ORK_PLAN_PATH="$PLAN_PATH" ARTIFACT_PATH="$ARTIFACT_PATH" \
  mo_runtime_exec "bash $(realpath "$_script")" "$_verify_cwd" \
  > "$_evidence" 2>&1
```

### R1 — Runtime Interface Contract (1 day)

**Define the 6-function bash contract** (maps directly to swe-rex's `AbstractRuntime`):

| Bash function | Signature | Analogous to |
|---|---|---|
| `mo_runtime_exec` | `<cmd> <cwd> [timeout]` | `AbstractRuntime.execute(Command)` |
| `mo_runtime_put` | `<src_path> <dst_path>` | `AbstractRuntime.upload(UploadRequest)` |
| `mo_runtime_get` | `<src_path> <dst_path>` | `AbstractRuntime.read_file(ReadFileRequest)` |
| `mo_runtime_session_create` | `<session_id> [startup_cmd]` | `AbstractRuntime.create_session(...)` |
| `mo_runtime_session_run` | `<session_id> <cmd> [cwd]` | `AbstractRuntime.run_in_session(Action)` |
| `mo_runtime_session_close` | `<session_id>` | `AbstractRuntime.close_session(...)` |

All backends implement these 6 functions. The router in `lib/runtime.sh` sources one backend file based on `MO_RUNTIME_BACKEND`. The caller code (`bin/mini-ork-execute`, `lib/llm-dispatch.sh`, verifier dispatch) never changes when the backend changes.

Session state for backends with persistent containers: store the handle (container name, pexpect PID) in a temp file keyed by session ID: `/tmp/mo-session-${session_id}.container`.

### R2a — Bubblewrap Backend (2–3 days, Linux-only, no root, stops the clobber today)

**New file:** `lib/runtime/bubblewrap.sh`

```bash
#!/usr/bin/env bash
# lib/runtime/bubblewrap.sh — unprivileged Linux sandbox via bwrap.
# Mirrors mini-swe-agent BubblewrapEnvironment exactly.
# Requires: bubblewrap (bwrap), Linux kernel with user namespaces.

_MO_BWRAP="${MO_BWRAP_EXECUTABLE:-bwrap}"

_mo_bwrap_base_args() {
  # System paths read-only; workspace is the only writable bind.
  printf '%s\0' \
    --unshare-user-try \
    --ro-bind /usr /usr \
    --ro-bind /bin /bin \
    --ro-bind-try /lib /lib \
    --ro-bind-try /lib64 /lib64 \
    --ro-bind /etc /etc \
    --tmpfs /tmp \
    --proc /proc \
    --dev /dev \
    --new-session \
    --setenv PATH /usr/local/bin:/usr/sbin:/usr/bin:/bin
}

mo_runtime_exec() {
  local _cmd="$1"
  local _cwd="${2:-$PWD}"
  local _timeout="${3:-120}"

  mkdir -p "$_cwd"

  local _bwrap_args=()
  while IFS= read -r -d '' _arg; do
    _bwrap_args+=("$_arg")
  done < <(_mo_bwrap_base_args)

  timeout "$_timeout" "$_MO_BWRAP" \
    "${_bwrap_args[@]}" \
    --bind "$_cwd" "$_cwd" \
    --chdir "$_cwd" \
    bash -c "$_cmd"
}

# bwrap has no persistent container; put/get are host-side copies.
mo_runtime_put() { local _src="$1" _dst="$2"; cp -R "$_src" "$_dst"; }
mo_runtime_get()  { local _src="$1" _dst="$2"; cp -R "$_src" "$_dst"; }

# Each bwrap exec is already stateless; sessions are no-op.
mo_runtime_session_create() { : ; }
mo_runtime_session_run() {
  local _session_id="$1" _cmd="$2" _cwd="${3:-$PWD}"
  mo_runtime_exec "$_cmd" "$_cwd"
}
mo_runtime_session_close() { : ; }
```

**Isolation achieved:** Each `mo_runtime_exec` call gets:
- System paths (`/usr`, `/bin`, `/lib`, `/etc`) read-only from host
- `/tmp` as a fresh tmpfs — ephemeral, not shared between calls
- Only the explicit workspace dir (`_cwd`) is bind-mounted read-write
- No root required (`--unshare-user-try` uses kernel user namespaces)

The workspace is the run's directory under `$MINI_ORK_HOME/runs/<run_id>/workspace/`, provisioned by the existing `lib/sandbox/local.sh:mo_sandbox_local_provision`. Implementer writes go into this workspace, not into the target repo's main checkout.

**macOS note:** bwrap is Linux-only. The local backend remains the macOS fallback (`MO_RUNTIME_BACKEND=local`). Use bubblewrap in CI and cloud.

### R2b — Docker Backend (3–4 days)

**New file:** `lib/runtime/docker.sh`

```bash
#!/usr/bin/env bash
# lib/runtime/docker.sh — Docker-container runtime backend.
# One container per session; commands via docker exec.
# Mirrors mini-swe-agent DockerEnvironment at the bash layer.

_MO_DOCKER="${MO_DOCKER_EXECUTABLE:-docker}"
_MO_DOCKER_IMAGE="${MO_DOCKER_IMAGE:-ubuntu:22.04}"
_MO_DOCKER_RUN_ARGS="${MO_DOCKER_RUN_ARGS:---rm}"

mo_runtime_session_create() {
  local _session_id="$1"
  local _cwd="${2:-/workspace}"
  local _name="mo-runtime-${_session_id}"

  "$_MO_DOCKER" run -d --name "$_name" -w "$_cwd" \
    ${_MO_DOCKER_RUN_ARGS} "$_MO_DOCKER_IMAGE" sleep 7200

  printf '%s' "$_name" > "/tmp/mo-session-${_session_id}.container"
}

mo_runtime_session_run() {
  local _session_id="$1" _cmd="$2" _cwd="${3:-}"
  local _container
  _container=$(cat "/tmp/mo-session-${_session_id}.container")
  local _w_args=()
  [ -n "$_cwd" ] && _w_args=(-w "$_cwd")
  "$_MO_DOCKER" exec "${_w_args[@]}" "$_container" bash -lc "$_cmd"
}

mo_runtime_session_close() {
  local _session_id="$1"
  local _container
  _container=$(cat "/tmp/mo-session-${_session_id}.container" 2>/dev/null || true)
  [ -n "$_container" ] && "$_MO_DOCKER" stop "$_container" 2>/dev/null || true
  rm -f "/tmp/mo-session-${_session_id}.container"
}

mo_runtime_exec() {
  local _cmd="$1" _cwd="${2:-/workspace}" _timeout="${3:-120}"
  local _id="eph-$(date +%s%N | sha1sum | head -c8)"
  mo_runtime_session_create "$_id" "$_cwd"
  local _rc=0
  timeout "$_timeout" mo_runtime_session_run "$_id" "$_cmd" "$_cwd" || _rc=$?
  mo_runtime_session_close "$_id"
  return $_rc
}

mo_runtime_put() {
  local _src="$1" _dst="$2" _session_id="$3"
  local _container
  _container=$(cat "/tmp/mo-session-${_session_id}.container")
  "$_MO_DOCKER" cp "$_src" "${_container}:${_dst}"
}

mo_runtime_get() {
  local _src="$1" _dst="$2" _session_id="$3"
  local _container
  _container=$(cat "/tmp/mo-session-${_session_id}.container")
  "$_MO_DOCKER" cp "${_container}:${_src}" "$_dst"
}
```

### R2c — swerex Bridge for Cloud (Phase 2, 1 week)

For Modal/Fargate/Daytona, mini-ork can shell out to a Python helper that wraps `swerex.deployment.*` rather than reimplementing cloud SDKs in bash.

**New file:** `lib/runtime/swerex_bridge.sh`

```bash
#!/usr/bin/env bash
# lib/runtime/swerex_bridge.sh — delegates all exec to swe-rex Python runtime.
# Requires: pip install swe-rex, set MO_SWEREX_DEPLOYMENT_TYPE (local|docker|modal|fargate).

mo_runtime_exec() {
  local _cmd="$1" _cwd="${2:-$PWD}" _timeout="${3:-120}"
  python3 "$MINI_ORK_ROOT/lib/runtime/swerex_exec.py" \
    --deployment-type "${MO_SWEREX_DEPLOYMENT_TYPE:-docker}" \
    --image "${MO_SWEREX_IMAGE:-ubuntu:22.04}" \
    --cwd "$_cwd" --timeout "$_timeout" --cmd "$_cmd"
}
```

`lib/runtime/swerex_exec.py` (thin wrapper):
```python
import asyncio, argparse
from swerex.deployment.config import get_deployment, DockerDeploymentConfig, ModalDeploymentConfig
from swerex.runtime.abstract import Command

async def main(args):
    config = {
        "docker": DockerDeploymentConfig(image=args.image),
        "modal": ModalDeploymentConfig(image=args.image),
    }[args.deployment_type]
    dep = config.get_deployment()
    await dep.start()
    result = await dep.runtime.execute(Command(
        command=args.cmd, cwd=args.cwd, timeout=args.timeout, shell=True, merge_output_streams=True))
    await dep.stop()
    print(result.stdout, end="")

asyncio.run(main(argparse.ArgumentParser().parse_args()))
```

This reuses swe-rex's entire Modal/Fargate stack — `ModalDeployment.start()` creates the sandbox and returns a `RemoteRuntime` talking to the swerex HTTP server inside it — without any bash reimplementation of the Modal SDK.

---

## 7. Changes to `bin/mini-ork-execute`

### Current problematic patterns

**Pattern 1 — verifier runs directly on host (line ~114):**
```bash
( cd "$_verify_cwd" && MINI_ORK_PLAN_PATH="$PLAN_PATH" ARTIFACT_PATH="$ARTIFACT_PATH" \
  bash "$_script" ) > "$_evidence" 2>&1
```

**Pattern 2 — node dispatch in subshell (in lib/llm-dispatch.sh):**
```bash
( source "$MINI_ORK_ROOT/lib/$cl_script" && claude --print ... )
```

**Pattern 3 — implementer writes directly to host worktree** via Claude's Edit/Write tool calls, operating on `$MO_TARGET_CWD`.

### Changes

Add to `bin/mini-ork-execute` startup (after existing lib sources, before `PLAN_PATH` resolution):
```bash
source "$MINI_ORK_ROOT/lib/runtime.sh"
```

Refactor `_run_verifier_ref`:
```bash
# Replace the inline subshell (line ~114):
MINI_ORK_PLAN_PATH="$PLAN_PATH" ARTIFACT_PATH="$ARTIFACT_PATH" \
  mo_runtime_exec "bash $(realpath "$_script")" "$_verify_cwd" \
  > "$_evidence" 2>&1
_exit=$?
# ... json parse and return unchanged ...
```

For parallel nodes, key each node's workspace to its `node_id` so they don't share a workspace dir:
```bash
_node_workspace="$RUN_DIR/workspaces/$_node_id"
mkdir -p "$_node_workspace"
# Then all mo_runtime_exec calls for this node use $_node_workspace as cwd
```

---

## 8. Backend Selection in mini-ork

### Environment variable (immediate):
```bash
MO_RUNTIME_BACKEND=bubblewrap mini-ork run recipe kickoff.md
MO_RUNTIME_BACKEND=docker MO_DOCKER_IMAGE=ubuntu:22.04 mini-ork run recipe kickoff.md
MO_RUNTIME_BACKEND=swerex_bridge MO_SWEREX_DEPLOYMENT_TYPE=modal mini-ork run recipe kickoff.md
```

### Per-run config in workflow.yaml (Phase 2):
```yaml
runtime:
  backend: bubblewrap     # or: local | docker | swerex_bridge
  docker_image: ubuntu:22.04
  workspace_mount: /workspace
```

`lib/config_resolve.sh:mo_snapshot_run_config` already snapshots config into `$RUN_DIR`. Runtime backend gets added to the snapshot and exported as `MO_RUNTIME_BACKEND`.

### Fallback chain (mirrors swe-rex's graceful degradation):
```
bubblewrap → if bwrap absent → local
docker     → if docker absent → local
swerex_bridge → if swe-rex absent → docker → local
```

---

## 9. Parallelism

mini-ork's `parallel` dispatch mode already runs multiple nodes via background jobs. The missing piece is that parallel nodes share the same host filesystem — they can corrupt each other's git state.

With the runtime interface:
- Each parallel node gets its own workspace via a per-node workspace dir (`$RUN_DIR/workspaces/$node_id`)
- Each node routes all `mo_runtime_exec` calls through its workspace
- With bubblewrap: each exec is isolated (fresh tmpfs, read-only system paths, only the node's workspace is writable)
- With docker: each node session gets its own container (keyed by `node_id`)

No changes to the parallel dispatch logic itself — just routing each node's exec calls through the runtime interface with the correct workspace.

---

## 10. Key Differences Between the Two References

| Concern | mini-swe-agent | swe-rex |
|---|---|---|
| Exec model | Stateless `execute(action, cwd)` per call | Both stateless `execute(Command)` AND stateful `run_in_session(Action)` |
| Filesystem persistence across calls | No (each exec is a new process) | Yes via sessions (pexpect bash stays alive between calls) |
| Remote execution | Via SwerexDockerEnvironment adapter → HTTP | Native via RemoteRuntime → HTTP server inside sandbox |
| Backend selection | String key `environment_class` in YAML config | Discriminated union Pydantic config, `get_deployment()` factory |
| Parallelism unit | ThreadPoolExecutor worker = one container = one agent | Caller responsibility; each Deployment instance is independent |
| Bubblewrap | Full implementation, no-root, per-exec | Not implemented (swe-rex focuses on docker/modal/fargate) |
| File transfer | Not needed (single-machine assumption) | Native: `upload`, `read_file`, `write_file` in AbstractRuntime |
| Cloud execution | Via swerex_modal adapter (bridges to swe-rex) | Native: ModalDeployment, FargateDeployment |

---

## 11. Implementation Checklist

### R0 — Stateless exec hook (1–2 days, zero new deps)
- [ ] Create `lib/runtime.sh` (backend router)
- [ ] Create `lib/runtime/local.sh` (no-op wrapper, same behavior as today)
- [ ] Source `lib/runtime.sh` in `bin/mini-ork-execute` startup block
- [ ] Replace `_run_verifier_ref`'s `( cd ... && bash ... )` with `mo_runtime_exec`
- [ ] Replace naked `cd + bash` patterns in `lib/llm-dispatch.sh` with `mo_runtime_exec`
- [ ] Add process-group kill on timeout (steal mini-swe-agent's `setsid`/`kill -- -$pgid`)

### R1 — Runtime interface contract (1 day)
- [ ] Document the 6-function contract in `lib/runtime/README.md`
- [ ] Add a contract smoke test (source backend + call each function with a noop cmd)

### R2a — Bubblewrap backend (2–3 days, Linux-only, no root)
- [ ] Create `lib/runtime/bubblewrap.sh`
- [ ] Wire node workspace dir (per-node `$RUN_DIR/workspaces/$node_id`) as the bind-mount target
- [ ] Add `MO_RUNTIME_BACKEND=bubblewrap` to `.env.example` and CI
- [ ] Test: verifier cannot write outside its workspace; parallel nodes cannot interfere

### R2b — Docker backend (3–4 days)
- [ ] Create `lib/runtime/docker.sh`
- [ ] Wire `mo_runtime_session_create/close` to `docker run -d` / `docker stop` lifecycle
- [ ] Wire `mo_runtime_put/get` to `docker cp`
- [ ] Test: parallel implementer nodes each get an isolated container

### R2c — swerex bridge for cloud (Phase 2, ~1 week)
- [ ] Create `lib/runtime/swerex_bridge.sh` + `lib/runtime/swerex_exec.py`
- [ ] Support `MO_SWEREX_DEPLOYMENT_TYPE=modal` for serverless cloud runs
- [ ] Test: implementer writes files inside Modal sandbox, retrieve them via `mo_runtime_get`
- [ ] Document `MO_SWEREX_*` env vars in runbook

### Phase 2 — Implementer isolation (requires prompt-level changes)
- [ ] Pass workspace path into implementer prompt so Claude writes to sandbox, not host checkout
- [ ] After implementer finishes, `mo_runtime_get` the workspace back and apply as a patch to target repo
- [ ] This fully closes the cross-repo corruption vector
