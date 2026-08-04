"""microVM-backed ``Workspace`` (sandbox P3, Piece 1) — the default isolation.

Runs each agent's commands inside its own hardware-isolated **microVM** (via the
open-source `microsandbox <https://github.com/superradcompany/microsandbox>`_
platform) while bind-mounting the run's shared drive at a fixed in-VM path, so
every microVM in a run sees the same ``/workspace``. Satisfies the ``Workspace``
protocol from :mod:`mini_ork.runtime.sandbox` and registers itself as
``microvm`` at import.

Why microVM over the ``docker`` backend: millisecond boot, hardware isolation
(KVM / Apple Hypervisor.framework via libkrun) rather than a shared kernel,
OCI-compatible images, and — critically — the *same* SDK drives an embedded
**local** backend on this host or a **remote** backend in the cloud
(``microsandbox.set_default_backend(kind, url=, api_key=)``), so this backend is
the scalable cloud-launch path with no proprietary provider lock-in. ``docker``
demotes to the local/offline fallback.

Transport: the microsandbox **Python SDK** (``pip install microsandbox``). Its
``Sandbox`` methods are pyo3-**async** — ``Sandbox.create``/``exec``/``stop``
return awaitables and must be driven from a running event loop (calling one with
no loop raises ``RuntimeError: no running event loop``). Because our
``Workspace`` seam is sync with *separate* ``up()``/``exec()``/``down()`` calls,
we cannot use the SDK's ``async with`` context manager; instead we hold ONE
persistent event loop per instance (created in ``up()``, closed in ``down()``)
and drive every coroutine through ``loop.run_until_complete`` — the Sandbox's
session stays bound to the loop it was created on.

The default ``local`` backend runs the microVM in-process (no separate server
daemon) but needs a one-time ``microsandbox.install()`` to fetch ``msb`` +
``libkrunfw`` into ``~/.microsandbox/``; ``up()`` checks ``is_installed()`` and
fails loudly with that remedy rather than booting blindly.

The microVM is **cattle** (``down()`` stops + removes it); the shared drive is
the **pet** and is never destroyed here — only :meth:`SharedDrive.down` may
remove the mount source. The SDK is imported lazily (inside ``up()``), so merely
importing this module to register the factory pulls in no pip dependency and
does no I/O — the same import-time contract as the built-in ``local`` backend.
"""
from __future__ import annotations

import asyncio
import os
import uuid
from typing import Any, Mapping, Sequence

from mini_ork.runtime.sandbox import register_workspace_backend

__all__ = ["MicrovmWorkspace", "register"]

_DEFAULT_MOUNT = "/workspace"
_TIMEOUT_RC = 124  # conventional "killed by timeout" return code (GNU timeout)


class MicrovmWorkspace:
    """A ``Workspace`` backed by one microsandbox microVM with a bound drive.

    The microVM stays alive between ``exec`` calls so a node can issue many
    commands against warm state; ``down()`` stops and removes it. ``drive_root``
    (a host directory, typically a :class:`LocalBindDrive` ``mount_path``) is
    bind-mounted at ``mount_path`` so every microVM in a run shares one
    ``/workspace``.
    """

    def __init__(
        self,
        *,
        image: str,
        drive_root: str,
        mount_path: str = _DEFAULT_MOUNT,
        name: str | None = None,
        cpus: int | None = None,
        memory: int | None = None,
    ) -> None:
        if not image:
            raise ValueError("MicrovmWorkspace requires a non-empty image")
        if not drive_root:
            raise ValueError("MicrovmWorkspace requires a non-empty drive_root")
        self._image = image
        self._drive_root = os.path.abspath(drive_root)
        self._mount_path = mount_path
        self._name = name or f"mo-mvm-{uuid.uuid4().hex[:12]}"
        self._cpus = cpus
        self._memory = memory
        # Set together in up(), cleared together in down().
        self._sb: Any | None = None
        self._loop: asyncio.AbstractEventLoop | None = None

    # --- async→sync bridge ----------------------------------------------
    def _run(self, factory: Any) -> Any:
        """Drive one SDK call on this instance's persistent loop.

        ``factory`` is a zero-arg thunk that *invokes* a pyo3-async SDK method
        and returns its awaitable. It must be CALLED inside a coroutine already
        running on the loop: this SDK grabs ``asyncio.get_running_loop()`` at
        call time, so evaluating ``Sandbox.create(...)`` as a plain argument —
        before ``run_until_complete`` has started the loop — raises
        ``RuntimeError: no running event loop``. Wrapping the call in ``_driver``
        defers it until the loop is live.
        """
        if self._loop is None:
            raise RuntimeError("MicrovmWorkspace used before up()")

        async def _driver() -> Any:
            return await factory()

        return self._loop.run_until_complete(_driver())

    @staticmethod
    def _merge_output(out: Any) -> str:
        """Merge an ``ExecOutput``'s stdout+stderr (parity with the runtime contract)."""
        stdout = getattr(out, "stdout_text", "") or ""
        stderr = getattr(out, "stderr_text", "") or ""
        return stdout + stderr

    # --- Workspace protocol ---------------------------------------------
    def up(self) -> None:
        # Lazy import: keeps the default/registration path free of the pip dep.
        try:
            import microsandbox
            from microsandbox import Sandbox, Volume
        except ImportError as exc:
            raise RuntimeError(
                "microvm workspace needs the microsandbox SDK: "
                "`pip install microsandbox`, or set MO_SANDBOX_BACKEND=docker / "
                "leave it unset for the host"
            ) from exc

        # The embedded local backend needs a one-time install of msb+libkrunfw.
        # Fail loudly with the exact remedy rather than a cryptic boot error.
        if not microsandbox.is_installed():
            raise RuntimeError(
                "microsandbox local backend not installed: run "
                "`python -c 'import microsandbox; microsandbox.install()'` "
                "(fetches msb + libkrunfw into ~/.microsandbox/), point at a "
                "remote backend via microsandbox.set_default_backend(...), or "
                "set MO_SANDBOX_BACKEND=docker."
            )

        os.makedirs(self._drive_root, exist_ok=True)
        # One microVM per instance; bind the shared drive at the mount path so
        # every agent's VM reads/writes the same directory. ``replace=True`` makes
        # boot idempotent if a same-named VM lingers from a crashed prior run.
        kwargs: dict[str, Any] = {
            "image": self._image,
            "volumes": {self._mount_path: Volume.bind(self._drive_root)},
            "replace": True,
        }
        if self._cpus is not None:
            kwargs["cpus"] = self._cpus
        if self._memory is not None:
            kwargs["memory"] = self._memory

        self._loop = asyncio.new_event_loop()
        try:
            self._sb = self._run(lambda: Sandbox.create(self._name, **kwargs))
        except Exception as exc:  # boot failure → loud, never a silent fallback
            self._loop.close()
            self._loop = None
            raise RuntimeError(
                f"microVM boot failed for {self._name!r} (image {self._image!r}): "
                f"{exc}. Check the microsandbox backend (is_installed / "
                "set_default_backend), or set MO_SANDBOX_BACKEND=docker."
            ) from exc

    def exec(self, cmd: str, *, cwd: str, timeout: int) -> tuple[int, str]:
        sb = self._sb
        if sb is None:
            raise RuntimeError("MicrovmWorkspace.exec called before up()")
        # `sh -lc <cmd>` mirrors the docker backend and the runtime contract:
        # a login shell, cwd pinned per call, output buffered.
        timeout_excs: tuple[type[BaseException], ...] = (asyncio.TimeoutError,)
        try:  # up() already proved the SDK importable; add its typed timeout.
            from microsandbox import ExecTimeoutError

            timeout_excs = (asyncio.TimeoutError, ExecTimeoutError)
        except ImportError:  # pragma: no cover
            pass
        try:
            out = self._run(
                lambda: sb.exec(
                    "sh", ["-lc", cmd], cwd=cwd, timeout=float(timeout)
                )
            )
        except timeout_excs:
            return _TIMEOUT_RC, f"timeout: exec exceeded {timeout}s and was killed\n"
        except Exception as exc:  # any other SDK timeout wording surfaces uniformly
            if "timeout" in str(exc).lower():
                return _TIMEOUT_RC, f"timeout: exec exceeded {timeout}s: {exc}\n"
            raise
        rc = getattr(out, "exit_code", None)
        return (rc if rc is not None else 0), self._merge_output(out)

    def spawn(
        self,
        argv: Sequence[str],
        *,
        stdin: str,
        timeout: float,
        env: Mapping[str, str],
        cwd: str | None,
    ) -> tuple[int, str, str]:
        """Spawn the harness CLI *inside* the microVM (scope=agent).

        NOT YET IMPLEMENTED — this is the hybrid's Increment-3 slice. It will
        drive the microsandbox SDK's exec with the prompt on stdin, streams kept
        separate, and a timeout mapped to ``rc=124`` — the A.1 invariant holds by
        construction (the CLI runs in a hardware-isolated VM with no host
        ``/dev/tty`` and dies with the VM). Until then it fails loudly rather
        than silently dropping a scope=agent CLI spawn back onto the host."""
        raise NotImplementedError(
            "MicrovmWorkspace.spawn (scope=agent CLI-in-microVM) is not yet "
            "implemented — it lands in SE-3 Increment 3 (microsandbox SDK exec). "
            "Use MO_SANDBOX_SCOPE=tool for microVM tool-exec, or keep the harness "
            "on the host (MO_SANDBOX_SCOPE unset/tool)."
        )

    def put(self, content: str) -> str:
        # Use the SDK's filesystem API, not `exec(stdin=)`: this SDK's ``stdin``
        # is a *mode* selector (null/pipe/bytes), not the payload, so piping
        # content through it fails. ``fs.write`` takes the bytes directly.
        sb = self._sb
        if sb is None:
            raise RuntimeError("MicrovmWorkspace.put called before up()")
        dest = f"{self._mount_path}/put-{uuid.uuid4().hex[:8]}.txt"
        self._run(lambda: sb.fs.write(dest, content.encode("utf-8")))
        return dest

    def get(self, path: str) -> str:
        sb = self._sb
        if sb is None:
            raise RuntimeError("MicrovmWorkspace.get called before up()")
        return self._run(lambda: sb.fs.read_text(path))

    def down(self) -> None:
        # Cattle: stop + remove the microVM. Idempotent and best-effort — a
        # failed stop must not mask the caller's real result. The bind-mount
        # source (the shared drive) is the pet and is deliberately untouched.
        sb = self._sb
        if sb is not None:
            try:
                self._run(lambda: sb.stop())
            except Exception:
                # Fall back to a hard kill; never raise from teardown.
                try:
                    self._run(lambda: sb.kill())
                except Exception:
                    pass
        if self._loop is not None:
            self._loop.close()
        self._sb = None
        self._loop = None
        return None


def _factory(**kwargs: Any) -> MicrovmWorkspace:
    """Build a :class:`MicrovmWorkspace`, filling image/drive/limits from env.

    Missing required config raises a clear ``RuntimeError`` (not a ``TypeError``
    traceback) so ``get_workspace("microvm")`` with nothing configured fails
    loudly and legibly.
    """
    image = kwargs.pop("image", None) or os.environ.get("MO_SANDBOX_IMAGE")
    if not image:
        raise RuntimeError(
            "microvm workspace requires an image: pass image= or set "
            "MO_SANDBOX_IMAGE"
        )
    drive_root = kwargs.pop("drive_root", None) or os.environ.get(
        "MO_SHARED_DRIVE_ROOT"
    )
    if not drive_root:
        raise RuntimeError(
            "microvm workspace requires a drive_root: pass drive_root= or set "
            "MO_SHARED_DRIVE_ROOT"
        )
    kwargs.setdefault("cpus", _int_env("MO_SANDBOX_CPU"))
    kwargs.setdefault("memory", _int_env("MO_SANDBOX_MEMORY"))
    return MicrovmWorkspace(image=image, drive_root=drive_root, **kwargs)


def _int_env(name: str) -> int | None:
    """Parse an int env var, or ``None`` when unset/blank (limits are optional)."""
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def register() -> None:
    """Register the ``microvm`` backend (idempotent, last-write-wins)."""
    register_workspace_backend("microvm", _factory)


# Import-time effect: register the factory. This is the ONLY thing importing
# this module does — no SDK import, no I/O — matching the ``local`` backend.
register()
