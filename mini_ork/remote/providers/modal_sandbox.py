"""Modal node provider: one Modal Sandbox (gVisor) per run.

``provider: modal`` in an environment profile makes ``--placement remote``
create a Modal Sandbox for the run, boot the node-agent inside it, and reach it
through Modal's authenticated HTTP proxy with a **connect token**. The boot
sequence is shared by every sandbox cloud
(:mod:`mini_ork.remote.providers.sandbox_node`); this module is only the Modal
adapter. (Named ``modal_sandbox`` so it never shadows the ``modal`` SDK.)

Modal specifics:

* sandboxes live in a Modal app (``MO_MODAL_APP``, default ``mini-ork-nodes``,
  created on first use; one setting so lookups and the sweep always agree). Sandbox names are unique per app,
  so ``mo-<run_id>`` is the idempotency key, and ``mo.*`` tags carry the TTL
  sweep metadata.
* auth: ``Sandbox.create_connect_token(port=7091)``. The token goes in the
  ``_modal_connect_token`` **cookie**, because the node-agent's own bearer
  already occupies the ``Authorization`` header Modal also accepts.
* image: ``profile.image`` (any registry image) or Modal's ``debian_slim``. The
  agent CLI and test tooling come from the profile's ``setup:``.
* lifetime: ``timeout`` (Modal's max is 24 h), default ``MO_SANDBOX_MAX_AGE``.
  ``cpu`` / ``memory`` come from the profile's ``resources``.
* egress: ``network: allowlist`` is refused (Modal filters by CIDR, not domain).

``MODAL_TOKEN_ID`` / ``MODAL_TOKEN_SECRET`` are read through the vault chain and
fail closed. Verified offline only (fake SDK); the live smoke is
``scripts/smoke_sandbox_node.py --provider modal``.
"""
from __future__ import annotations

import shlex
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from mini_ork.remote.providers.base import (
    ProviderUnavailableError,
    register_node_provider,
    resolve_provider_secret,
)
from mini_ork.remote.providers.sandbox_node import (
    LABEL_PROVIDER,
    LABEL_SANDBOX,
    BootOptions,
    SandboxNodeProvider,
    memory_gib,
    run_labels,
    sandbox_name,
    ttl_minutes_default,
)

__all__ = ["CONNECT_COOKIE", "ModalOptions", "ModalProvider", "NAME"]

NAME = "modal"
CONNECT_COOKIE = "_modal_connect_token"
_MAX_TIMEOUT_S = 24 * 3600
_DEFAULT_APP = "mini-ork-nodes"


@dataclass(frozen=True)
class ModalOptions:
    """``provider_options`` keys (all optional): ``environment``, ``timeout_s``,
    ``python``, ``bootstrap``, ``ready_timeout_s``."""

    app: str = _DEFAULT_APP
    environment: str = ""
    image: str = ""
    timeout_s: int = _MAX_TIMEOUT_S
    cpu: float | None = None
    memory_mib: int | None = None
    env: Mapping[str, str] = field(default_factory=dict)
    boot: BootOptions = field(default_factory=BootOptions)

    _KNOWN = frozenset({"environment", "timeout_s", "python", "bootstrap",
                        "ready_timeout_s"})

    @classmethod
    def resolve(cls, profile: Any, env: Mapping[str, str]) -> "ModalOptions":
        opts = dict(getattr(profile, "provider_options", None) or {})
        unknown = sorted(set(opts) - cls._KNOWN)
        if unknown:
            raise ProviderUnavailableError(
                f"modal: unknown provider_options {unknown}; known: {sorted(cls._KNOWN)}")
        if str(getattr(profile, "network", "") or "full") == "allowlist":
            raise ProviderUnavailableError(
                "modal: network: allowlist is not supported (Modal filters egress by "
                "CIDR, not domain); use network: full")
        resources = dict(getattr(profile, "resources", None) or {})
        cpus = resources.get("cpus")
        mem = memory_gib(resources.get("memory"), provider=NAME)
        timeout_s = int(opts.get("timeout_s") or ttl_minutes_default(env) * 60)
        if timeout_s > _MAX_TIMEOUT_S:
            raise ProviderUnavailableError(f"modal: timeout_s {timeout_s} exceeds Modal's "
                                           f"24 h maximum ({_MAX_TIMEOUT_S})")
        return cls(
            app=_app_name(env),
            environment=str(opts.get("environment") or env.get("MODAL_ENVIRONMENT") or ""),
            image=str(getattr(profile, "image", "") or ""),
            timeout_s=timeout_s,
            cpu=float(cpus) if cpus not in (None, "") else None,
            memory_mib=mem * 1024 if mem else None,
            env={str(k): str(v) for k, v in (getattr(profile, "env", None) or {}).items()},
            boot=BootOptions.from_profile(profile, opts),
        )


# --- SDK seams ------------------------------------------------------------------


def _app_name(env: Mapping[str, str]) -> str:
    return (env.get("MO_MODAL_APP") or "").strip() or _DEFAULT_APP


def _sdk() -> Any:
    try:
        import modal  # type: ignore[import-not-found]
    except ImportError as exc:
        raise ProviderUnavailableError(
            "modal: the Modal SDK is not installed — pip install 'mini-ork[modal]'") from exc
    return modal


def _client(env: Mapping[str, str]) -> Any:
    token_id = resolve_provider_secret("MODAL_TOKEN_ID", env, provider=NAME)
    token_secret = resolve_provider_secret("MODAL_TOKEN_SECRET", env, provider=NAME)
    return _sdk().Client.from_credentials(token_id, token_secret)


def _is(exc: BaseException, *names: str) -> bool:
    return any(cls.__name__ in names for cls in type(exc).__mro__)


def _unavailable(action: str, exc: BaseException) -> ProviderUnavailableError:
    return ProviderUnavailableError(f"modal: {action} failed: {type(exc).__name__}: {exc}")


# --- the adapter ------------------------------------------------------------------


class _ModalSandbox:
    """A Modal Sandbox as :class:`~.sandbox_node.SandboxHandle`."""

    def __init__(self, sandbox: Any, labels: Mapping[str, str] | None = None) -> None:
        self._sb = sandbox
        self.id = str(sandbox.object_id)
        self.name = self.id
        self.labels = dict(labels or {})
        self._home = ""

    def alive(self) -> bool:
        try:
            return self._sb.poll() is None
        except Exception:  # noqa: BLE001
            return False

    def home(self) -> str:
        if not self._home:
            rc, out = self.run("printf %s \"$HOME\"", timeout=30)
            if rc != 0 or not out.strip():
                raise ProviderUnavailableError(f"modal: cannot read $HOME in {self.id}: {out}")
            self._home = out.strip()
        return self._home

    def upload(self, data: bytes, path: str) -> None:
        try:
            self._sb.filesystem.write_bytes(data, path)
        except Exception as exc:  # noqa: BLE001
            raise _unavailable(f"upload {path}", exc) from exc

    def download(self, path: str) -> bytes:
        return bytes(self._sb.filesystem.read_bytes(path))

    def run(self, command: str, *, timeout: int) -> tuple[int, str]:
        try:
            proc = self._sb.exec("bash", "-c", command, timeout=timeout)
            proc.wait()
            out = f"{proc.stdout.read() or ''}{proc.stderr.read() or ''}"
            return int(proc.returncode if proc.returncode is not None else 1), out
        except Exception as exc:  # noqa: BLE001
            if _is(exc, "ExecTimeoutError"):
                return 124, f"timed out after {timeout}s"
            raise _unavailable("exec", exc) from exc

    def start_background(self, command: str) -> None:
        # Detach inside the sandbox: the exec returns at once and the
        # node-agent keeps running for the sandbox's lifetime.
        rc, out = self.run(f"nohup bash -c {shlex.quote(command)} >/dev/null 2>&1 &", timeout=60)
        if rc != 0:
            raise ProviderUnavailableError(f"modal: node-agent start exited {rc}: {out[-500:]}")

    def endpoint(self, port: int) -> tuple[str, dict[str, str]]:
        try:
            creds = self._sb.create_connect_token(port=port)
        except Exception as exc:  # noqa: BLE001
            raise _unavailable("connect token", exc) from exc
        return str(creds.url), {"Cookie": f"{CONNECT_COOKIE}={creds.token}"}

    def delete(self, *, wait: bool) -> None:
        try:
            self._sb.terminate(wait=wait)
        except Exception as exc:  # noqa: BLE001
            if _is(exc, "NotFoundError"):
                return
            raise _unavailable(f"sandbox terminate {self.id}", exc) from exc


class ModalProvider(SandboxNodeProvider):
    name = NAME
    kind = "Modal gVisor sandbox per run"
    sdk_module = "modal"
    credential_keys = ("MODAL_TOKEN_ID", "MODAL_TOKEN_SECRET")

    def boot_options(self, profile: Any, env: Mapping[str, str]) -> BootOptions:
        return ModalOptions.resolve(profile, env).boot

    @staticmethod
    def _app(sdk: Any, client: Any, opts: ModalOptions) -> Any:
        kwargs: dict[str, Any] = {"client": client, "create_if_missing": True}
        if opts.environment:
            kwargs["environment_name"] = opts.environment
        return sdk.App.lookup(opts.app, **kwargs)

    def spawn_sandbox(self, *, run_id: str, profile: Any,
                      env: Mapping[str, str], node_id: str = "") -> _ModalSandbox:
        opts = ModalOptions.resolve(profile, env)
        sdk = _sdk()
        client = _client(env)
        labels = run_labels(NAME, run_id, node_id)
        image = (sdk.Image.from_registry(opts.image) if opts.image
                 else sdk.Image.debian_slim().apt_install("git", "curl"))
        kwargs: dict[str, Any] = {
            "app": self._app(sdk, client, opts), "name": sandbox_name(run_id, node_id),
            "tags": labels, "image": image, "env": dict(opts.env),
            "timeout": opts.timeout_s, "client": client,
        }
        if opts.cpu:
            kwargs["cpu"] = opts.cpu
        if opts.memory_mib:
            kwargs["memory"] = opts.memory_mib
        try:
            return _ModalSandbox(sdk.Sandbox.create("sleep", "infinity", **kwargs), labels)
        except Exception as exc:  # noqa: BLE001
            raise _unavailable("sandbox create", exc) from exc

    def find_run_sandbox(self, *, run_id: str, env: Mapping[str, str],
                         node_id: str = "") -> _ModalSandbox | None:
        name = sandbox_name(run_id, node_id)
        try:
            sb = _sdk().Sandbox.from_name(_app_name(env), name, client=_client(env))
        except ProviderUnavailableError:
            raise
        except Exception as exc:  # noqa: BLE001
            if _is(exc, "NotFoundError"):
                return None
            raise _unavailable(f"sandbox lookup {name}", exc) from exc
        return _ModalSandbox(sb, self._tags(sb))

    def get_sandbox(self, handle: str, env: Mapping[str, str]) -> _ModalSandbox | None:
        try:
            sb = _sdk().Sandbox.from_id(handle, client=_client(env))
        except ProviderUnavailableError:
            raise
        except Exception as exc:  # noqa: BLE001
            if _is(exc, "NotFoundError"):
                return None
            raise _unavailable(f"sandbox get {handle}", exc) from exc
        return _ModalSandbox(sb, self._tags(sb))

    @staticmethod
    def _tags(sb: Any) -> dict[str, str]:
        try:
            return dict(sb.get_tags() or {})
        except Exception:  # noqa: BLE001
            return {}

    def list_sandboxes(self, env: Mapping[str, str]) -> Iterable[_ModalSandbox]:
        sdk = _sdk()
        client = _client(env)
        app = self._app(sdk, client, ModalOptions(app=_app_name(env)))
        return [_ModalSandbox(sb, self._tags(sb))
                for sb in sdk.Sandbox.list(app_id=app.app_id, client=client,
                                           tags={LABEL_SANDBOX: "1", LABEL_PROVIDER: NAME})]


register_node_provider(NAME, ModalProvider)
