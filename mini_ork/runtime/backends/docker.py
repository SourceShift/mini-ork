"""Docker-backed ``Workspace`` (sandbox P2, Piece 1).

Runs each agent's commands inside its own long-lived Docker container while
bind-mounting the run's shared drive at a fixed in-container path, so every
container in a run sees the same ``/workspace``. Satisfies the ``Workspace``
protocol from :mod:`mini_ork.runtime.sandbox` and registers itself as
``docker`` at import.

No new pip dependency: it shells out to the ``docker`` CLI via ``subprocess``
(no ``docker-py``). Nothing in the default execution path imports this module —
importing it only registers the ``docker`` factory (the same import-time-effect
contract as the built-in ``local`` backend). The container is **cattle**
(``down()`` = ``docker rm -f``); the shared drive is the **pet** and is never
destroyed here — only :meth:`SharedDrive.down` may remove the mount source.
"""
from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import uuid
from typing import Any, Mapping, Sequence

from mini_ork.runtime.sandbox import register_workspace_backend

__all__ = ["DockerWorkspace", "register"]

_DEFAULT_MOUNT = "/workspace"
_TIMEOUT_RC = 124  # conventional "killed by timeout" return code (GNU timeout)
_RUN_LABEL = "mo.sandbox=1"  # sweepable: `docker ps -qf label=mo.sandbox=1`

# --- scope=agent env boundary (allowlist) ----------------------------------
# The host process env handed to ``spawn`` is a grab-bag: the whole
# ``os.environ`` (host PATH/HOME that don't exist in a Linux image; unrelated
# SSH/AWS/npm/GitHub creds) plus the per-dispatch overrides. Forwarding it
# verbatim would both clobber the container's own shell identity AND leak every
# host secret into the container. So env crosses the boundary ALLOWLIST-first:
# only the harness (``MO_*``) + LLM-provider namespaces + a generous
# ``*_API_KEY`` suffix are forwarded. A dropped key fails LOUD (the CLI can't
# auth → caught by the live docker smoke), whereas a leaked secret would be
# silent — so we bias to the allowlist on this security axis. The ``*_API_KEY``
# suffix deliberately does NOT match AWS ``*_ACCESS_KEY_ID`` /
# ``*_SECRET_ACCESS_KEY``, so the catch-all cannot re-open that leak.
# Decision: docs/decisions/20260804-docker-spawn-env-injection.md
_AGENT_ENV_PREFIXES = (
    "MO_",
    "OPENAI_",
    "ANTHROPIC_",
    "CLAUDE_",
    "CODEX_",
    "GEMINI_",
    "GOOGLE_GENAI_",
    "GLM_",
    "ZHIPU_",
    "ZHIPUAI_",
    "KIMI_",
    "MOONSHOT_",
    "MINIMAX_",
    "DEEPSEEK_",
    "OPENROUTER_",
    "GROQ_",
    "XAI_",
    "MISTRAL_",
    "TOGETHER_",
    "FIREWORKS_",
    "CEREBRAS_",
    "PERPLEXITY_",
)
_AGENT_ENV_SUFFIXES = ("_API_KEY",)


def _container_env(env: Mapping[str, str]) -> dict[str, str]:
    """Filter a host env down to the keys a scope=agent CLI may carry into the
    container (allowlist — see the module note above). Case-insensitive on the
    key name; values pass through unchanged. Pure + daemon-free so the boundary
    policy is unit-tested in one place (Increment 3's microVM backend will reuse
    it)."""
    out: dict[str, str] = {}
    for key, val in env.items():
        upper = key.upper()
        if upper.startswith(_AGENT_ENV_PREFIXES) or upper.endswith(_AGENT_ENV_SUFFIXES):
            out[key] = val
    return out


class DockerWorkspace:
    """A ``Workspace`` backed by one Docker container with a bind-mounted drive.

    The container stays alive (``sleep infinity``) between ``exec`` calls so a
    node can issue many commands against warm state; ``down()`` force-removes it.
    ``drive_root`` (a host directory, typically a :class:`LocalBindDrive`
    ``mount_path``) is bind-mounted at ``mount_path`` so every container in a run
    shares one ``/workspace``.
    """

    def __init__(
        self,
        *,
        image: str,
        drive_root: str,
        mount_path: str = _DEFAULT_MOUNT,
        name: str | None = None,
        cpu: str | None = None,
        memory: str | None = None,
        env_passthrough: Sequence[str] | None = None,
        docker_bin: str = "docker",
    ) -> None:
        if not image:
            raise ValueError("DockerWorkspace requires a non-empty image")
        if not drive_root:
            raise ValueError("DockerWorkspace requires a non-empty drive_root")
        self._image = image
        self._drive_root = os.path.abspath(drive_root)
        self._mount_path = mount_path
        self._name = name or f"mo-ws-{uuid.uuid4().hex[:12]}"
        self._cpu = cpu
        self._memory = memory
        self._env_passthrough = list(env_passthrough or [])
        self._docker_bin = docker_bin
        self._cid: str | None = None

    # --- daemon plumbing -------------------------------------------------
    def _exe(self) -> str:
        return shutil.which(self._docker_bin) or self._docker_bin

    def _docker(
        self,
        *args: str,
        timeout: int | None = None,
        input_text: str | None = None,
    ) -> subprocess.CompletedProcess:
        """Run a ``docker`` subcommand with captured (separate) streams."""
        try:
            return subprocess.run(
                [self._exe(), *args],
                capture_output=True,
                text=True,
                timeout=timeout,
                input=input_text,
                check=False,
            )
        except FileNotFoundError as exc:
            raise RuntimeError(
                f"docker CLI {self._docker_bin!r} not found on PATH; install "
                "Docker or leave MO_SANDBOX_BACKEND unset to run on the host"
            ) from exc

    def _require_daemon(self) -> None:
        """Raise a clear ``RuntimeError`` (not a traceback) if docker is down."""
        r = self._docker("info", "--format", "{{.ServerVersion}}", timeout=20)
        if r.returncode != 0:
            raise RuntimeError(
                "docker daemon not reachable (`docker info` exited "
                f"{r.returncode}): {r.stderr.strip() or r.stdout.strip()}"
            )

    # --- Workspace protocol ---------------------------------------------
    def up(self) -> None:
        self._require_daemon()
        os.makedirs(self._drive_root, exist_ok=True)
        args = [
            "run",
            "-d",
            "--name",
            self._name,
            "--label",
            _RUN_LABEL,
            "-v",
            f"{self._drive_root}:{self._mount_path}",
            "-w",
            self._mount_path,
        ]
        if self._cpu:
            args += ["--cpus", str(self._cpu)]
        if self._memory:
            args += ["--memory", str(self._memory)]
        for key in self._env_passthrough:
            val = os.environ.get(key)
            if val is not None:
                args += ["-e", f"{key}={val}"]
        # `sleep infinity` keeps the container warm; `exec` targets it repeatedly.
        args += [self._image, "sh", "-c", "sleep infinity"]
        r = self._docker(*args, timeout=180)
        if r.returncode != 0:
            raise RuntimeError(
                f"docker run failed ({r.returncode}) for image {self._image!r}: "
                f"{r.stderr.strip() or r.stdout.strip()}"
            )
        self._cid = r.stdout.strip()

    def exec(self, cmd: str, *, cwd: str, timeout: int) -> tuple[int, str]:
        if self._cid is None:
            raise RuntimeError("DockerWorkspace.exec called before up()")
        try:
            r = subprocess.run(
                [self._exe(), "exec", "-w", cwd, self._cid, "sh", "-lc", cmd],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,  # merge, matching the runtime contract
                text=True,
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired:
            # Killing the host-side `docker exec` client does NOT stop the
            # in-container process — only stopping the container reaps it.
            self._docker("stop", "-t", "1", self._cid, timeout=30)
            return _TIMEOUT_RC, f"timeout: exec exceeded {timeout}s and was killed\n"
        return r.returncode, r.stdout or ""

    def spawn(
        self,
        argv: Sequence[str],
        *,
        stdin: str,
        timeout: float,
        env: Mapping[str, str],
        cwd: str | None,
    ) -> tuple[int, str, str]:
        """Spawn the harness CLI *inside* the container (scope=agent) — the
        hybrid's container TRANSPORT for the dispatch spawn contract.

        Runs ``argv`` DIRECTLY (no shell) via ``docker exec -i`` into the warm
        container, so it mirrors the host ``spawn_local`` shape byte-for-byte:
        the prompt rides on **stdin** (never argv/env → E2BIG-proof), stdout and
        stderr are kept **separate** (unlike :meth:`exec`, which merges — a
        merged stream would corrupt the provider's JSON envelope on stdout), and
        the return code is faithful (docker surfaces the CLI's own code, incl.
        ``127`` when the CLI is not found in the image).

        The A.1 invariant is re-established structurally in the container's PID
        namespace: **no ``-t``** means no TTY is allocated, so nothing the CLI
        spawns can open ``/dev/tty`` to block a headless run; and the whole tree
        dies with the container (:meth:`down` = ``docker rm -f``), which is the
        container-native equivalent of the host's ``start_new_session`` +
        process-group SIGKILL. On timeout we ``docker stop`` the container —
        killing the host-side ``docker exec`` client would leave the in-container
        process running — and return the conventional ``rc=124`` with empty
        stdout, so ``dispatch`` finalizes it exactly like a host timeout.

        ``env`` is filtered through :func:`_container_env` (allowlist) so only the
        harness + provider config crosses the boundary, never the host's shell
        identity or unrelated secrets."""
        if self._cid is None:
            raise RuntimeError("DockerWorkspace.spawn called before up()")
        workdir = cwd or self._mount_path
        exec_argv = [self._exe(), "exec", "-i", "-w", workdir]
        for key, val in sorted(_container_env(env).items()):
            exec_argv += ["-e", f"{key}={val}"]
        exec_argv += [self._cid, *argv]
        try:
            r = subprocess.run(
                exec_argv,
                input=stdin,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,  # SEPARATE — contrast exec's merge
                text=True,
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired:
            # Reap the in-container process by stopping the container (the
            # host-side exec client dying does not stop it) — the container's
            # PID-namespace analogue of the host pgid SIGKILL.
            self._docker("stop", "-t", "1", self._cid, timeout=30)
            return _TIMEOUT_RC, "", f"timeout after {timeout}s"
        return r.returncode, r.stdout or "", r.stderr or ""

    def put(self, content: str) -> str:
        if self._cid is None:
            raise RuntimeError("DockerWorkspace.put called before up()")
        dest = f"{self._mount_path}/put-{uuid.uuid4().hex[:8]}.txt"
        r = self._docker(
            "exec",
            "-i",
            self._cid,
            "sh",
            "-c",
            f"cat > {shlex.quote(dest)}",
            input_text=content,
            timeout=60,
        )
        if r.returncode != 0:
            raise RuntimeError(f"docker put failed: {r.stderr.strip()}")
        return dest

    def get(self, path: str) -> str:
        if self._cid is None:
            raise RuntimeError("DockerWorkspace.get called before up()")
        r = self._docker("exec", self._cid, "cat", path, timeout=60)
        if r.returncode != 0:
            raise RuntimeError(
                f"docker get failed for {path!r} ({r.returncode}): "
                f"{r.stderr.strip()}"
            )
        return r.stdout

    def down(self) -> None:
        # Cattle: force-remove the container. Idempotent — a missing container
        # just returns non-zero, which we ignore. The bind-mount source
        # (the shared drive) is the pet and is deliberately left untouched.
        if self._cid is None:
            return None
        self._docker("rm", "-f", self._cid, timeout=30)
        self._cid = None
        return None


def _factory(**kwargs: Any) -> DockerWorkspace:
    """Build a :class:`DockerWorkspace`, filling image/drive/limits from env.

    Missing required config raises a clear ``RuntimeError`` (not a ``TypeError``
    traceback) so ``get_workspace("docker")`` with nothing configured fails
    loudly and legibly.
    """
    image = kwargs.pop("image", None) or os.environ.get("MO_SANDBOX_IMAGE")
    if not image:
        raise RuntimeError(
            "docker workspace requires an image: pass image= or set "
            "MO_SANDBOX_IMAGE"
        )
    drive_root = kwargs.pop("drive_root", None) or os.environ.get(
        "MO_SHARED_DRIVE_ROOT"
    )
    if not drive_root:
        raise RuntimeError(
            "docker workspace requires a drive_root: pass drive_root= or set "
            "MO_SHARED_DRIVE_ROOT"
        )
    kwargs.setdefault("cpu", os.environ.get("MO_SANDBOX_CPU"))
    kwargs.setdefault("memory", os.environ.get("MO_SANDBOX_MEMORY"))
    return DockerWorkspace(image=image, drive_root=drive_root, **kwargs)


def register() -> None:
    """Register the ``docker`` backend (idempotent, last-write-wins)."""
    register_workspace_backend("docker", _factory)


# Import-time effect: register the factory. This is the ONLY thing importing
# this module does — no daemon call, no I/O — matching the ``local`` backend.
register()
