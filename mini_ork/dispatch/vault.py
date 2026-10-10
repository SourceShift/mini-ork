"""The secret vault: one name → one value, from whichever backend is configured.

``secrets.py`` owns the *format* of the simple store (an owner-only
``export NAME=value`` file) and the ``${NAME}`` placeholder that config files
use. This module owns *where the value comes from*, because a machine that
tests mini-ork and a machine that runs it for a team need different answers to
that question and the rest of the system should not care which one it is.

Backends, cheapest first:

* ``env``      — the ambient environment. Always present, always wins: a shell
  ``export`` rotates a credential for one invocation, which is what a debug
  session wants. This also preserves the historical precedence
  (``{**stored, **runtime}``).
* ``file``     — ``$MINI_ORK_HOME/config/secrets.local.sh`` (0600). The default
  and the whole story for a person trying mini-ork: no server, no daemon, no
  network.
* ``sops``     — a ``secrets.enc.yaml`` encrypted with age/PGP, decrypted once
  per process. Git-committable, still no server.
* ``command``  — any executable that prints one value for a key. One backend
  covers ``pass``, ``op read``, ``bws secret get``, ``infisical secrets get``,
  and anything else an operator already runs.
* ``openbao``  — a self-hosted OpenBao (MPL-2.0) KV-v2 store read over its HTTP
  API. The one to reach for when a run is shared, audited, or cloud-scale, and
  the only backend whose *server* enforces which paths an identity may read.

The chain is exposed as a read-only ``Mapping``, which matters more than it
looks: ``secrets.expand_secret_references`` already takes a ``Mapping`` and
``${NAME}`` resolution already goes through it, so every existing caller —
MCP registry headers, ``env.yaml``, node boundaries — gains vault support
without changing a line of its own logic.

Values are fetched lazily and cached for the life of the process. Nothing here
ever writes a value to stdout, stderr, or a file: errors name the key and the
backend, never the secret.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar

import yaml

from .secrets import SecretStoreError, read_secret_exports

#: Backends a run gets when no vault config exists. Deliberately the two that
#: need nothing installed, so the default path has no setup and no failure mode
#: beyond "the file is not there".
DEFAULT_BACKEND_TYPES = ("env", "file")


class VaultError(SecretStoreError):
    """A configured backend cannot serve values, so no lookup can succeed.

    A ``SecretStoreError`` subclass on purpose: every existing caller already
    handles an unresolvable ``${NAME}`` by catching ``SecretStoreError``, so a
    vault failure travels the same path and is reported the same way, instead
    of escaping as an unhandled error at a node boundary.
    """


@dataclass(frozen=True)
class BackendStatus:
    """What ``mini-ork vault status`` prints: a name, and whether it can serve."""

    backend: str
    ready: bool
    detail: str
    source: str = ""


class SecretBackend:
    """One source of values. Subclasses implement ``get`` and ``describe``."""

    name = "backend"

    #: Whether a value this backend serves is protected somewhere other than the
    #: plaintext of a file or the environment that spawned the process. ``env``
    #: and ``file`` are the deliberate zero-setup tier, so they are not; a chain
    #: that is *only* those has a protection gap worth naming once (see
    #: ``report_protection_gap``). This is advice about posture, not a gate.
    protected: ClassVar[bool] = False

    def get(self, key: str) -> str | None:
        raise NotImplementedError

    def known_keys(self) -> Iterator[str]:
        """Names this backend can enumerate, for splatting into a dict.

        Optional: a backend that cannot list its keys (a lookup-by-name tool)
        returns nothing, and ``${NAME}`` still resolves through ``get``.
        """
        return iter(())

    def describe(self) -> str:
        """Where values come from, for an error message. Never a value."""
        return self.name

    def status(self) -> BackendStatus:
        return BackendStatus(self.name, True, "ready", self.describe())


class EnvBackend(SecretBackend):
    """The ambient environment. Always first, so a shell export wins."""

    name = "env"

    def __init__(self, environ: Mapping[str, str] | None = None) -> None:
        self._environ = os.environ if environ is None else environ

    def get(self, key: str) -> str | None:
        return self._environ.get(key)

    def known_keys(self) -> Iterator[str]:
        return iter(self._environ)

    def describe(self) -> str:
        return "the environment"


class FileBackend(SecretBackend):
    """The owner-only ``secrets.local.sh`` store. Read once, then cached."""

    name = "file"

    def __init__(self, path: str | Path | None = None) -> None:
        from .secrets import secret_store_path

        self._path = Path(path) if path is not None else secret_store_path()
        self._values: dict[str, str] | None = None

    def _load(self) -> dict[str, str]:
        if self._values is None:
            try:
                self._values = read_secret_exports(self._path)
            except SecretStoreError as exc:
                raise VaultError(f"file backend cannot read {self._path}: {exc}") from exc
        return self._values

    def get(self, key: str) -> str | None:
        return self._load().get(key)

    def known_keys(self) -> Iterator[str]:
        if not self._path.exists():
            return iter(())
        try:
            return iter(self._load())
        except VaultError as exc:
            # Enumerating is best-effort; a broken store still reports per-key.
            sys.stderr.write(f"[vault] {exc}\n")
            return iter(())

    def describe(self) -> str:
        return f"the secret store ({self._path})"

    def status(self) -> BackendStatus:
        exists = self._path.exists()
        detail = "ready" if exists else "no file yet — that is the unconfigured state"
        return BackendStatus(self.name, True, detail, str(self._path))


class SopsBackend(SecretBackend):
    """A SOPS-encrypted document, decrypted once per process.

    The point of SOPS+age is that the *encrypted* file is committable, so this
    is the middle path: no server, but the secret is not sitting in a file
    that only one machine can read.
    """

    name = "sops"
    protected = True

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._values: dict[str, str] | None = None

    def _load(self) -> dict[str, str]:
        if self._values is not None:
            return self._values
        if not self._path.is_file():
            raise VaultError(f"sops backend: {self._path} does not exist")
        executable = shutil.which("sops")
        if executable is None:
            raise VaultError(
                f"sops backend: 'sops' is not on PATH (install: brew install sops), "
                f"needed to decrypt {self._path}"
            )
        try:
            completed = subprocess.run(
                [executable, "--decrypt", str(self._path)],
                capture_output=True, text=True, check=False,
            )
        except OSError as exc:
            raise VaultError(f"sops backend: cannot run sops: {exc}") from exc
        if completed.returncode != 0:
            # sops' stderr can name a key file or an age recipient, never a
            # value, so it is safe to relay.
            message = (completed.stderr or "").strip().splitlines()
            raise VaultError(
                f"sops backend: decrypting {self._path} failed: "
                f"{message[-1] if message else 'exit ' + str(completed.returncode)}"
            )
        try:
            document = yaml.safe_load(completed.stdout) or {}
        except yaml.YAMLError as exc:
            raise VaultError(f"sops backend: {self._path} is not YAML: {exc}") from exc
        if not isinstance(document, Mapping):
            raise VaultError(f"sops backend: {self._path} is not a mapping of NAME: value")
        self._values = {str(k): str(v) for k, v in document.items() if v is not None}
        return self._values

    def get(self, key: str) -> str | None:
        return self._load().get(key)

    def describe(self) -> str:
        return f"the sops file ({self._path})"

    def status(self) -> BackendStatus:
        if not self._path.is_file():
            return BackendStatus(self.name, False, "file not found", str(self._path))
        if shutil.which("sops") is None:
            return BackendStatus(self.name, False, "'sops' not on PATH", str(self._path))
        return BackendStatus(self.name, True, "ready", str(self._path))


class CommandBackend(SecretBackend):
    """Any executable that prints one value for a key.

    ``argv`` is a template; ``{key}`` is substituted verbatim into one argument
    (never through a shell, so a key containing a space cannot become a second
    command). This is the escape hatch that makes the vault usable with tooling
    mini-ork will never know about.
    """

    name = "command"
    protected = True

    def __init__(self, argv: list[str], cache: bool = True) -> None:
        if not argv:
            raise VaultError("command backend: 'argv' must name an executable")
        self._argv = [str(part) for part in argv]
        self._cache: dict[str, str | None] = {}
        self._cache_enabled = cache

    def _render(self, key: str) -> list[str]:
        rendered: list[str] = []
        for part in self._argv:
            if "{key}" in part:
                rendered.append(part.replace("{key}", key))
            else:
                rendered.append(part)
        return rendered

    def get(self, key: str) -> str | None:
        if key in self._cache:
            return self._cache[key]
        command = self._render(key)
        executable = shutil.which(command[0])
        if executable is None:
            raise VaultError(
                f"command backend: {command[0]!r} is not on PATH (used for {key})"
            )
        try:
            completed = subprocess.run(
                [executable, *command[1:]], capture_output=True, text=True, check=False
            )
        except OSError as exc:
            raise VaultError(f"command backend: cannot run {command[0]!r}: {exc}") from exc
        # A lookup tool that fails is not the same as a key that is absent:
        # `pass show missing` exits non-zero and so does a network timeout, and
        # treating both as "unset" would silently drop a credential.
        if completed.returncode != 0:
            message = (completed.stderr or "").strip().splitlines()
            raise VaultError(
                f"command backend: {' '.join(command)} exited {completed.returncode} "
                f"for {key}: {message[-1] if message else 'no stderr'}"
            )
        value = completed.stdout.rstrip("\n")
        result = value if value else None
        if self._cache_enabled:
            self._cache[key] = result
        return result

    def describe(self) -> str:
        return f"the command {self._argv[0]!r}"

    def status(self) -> BackendStatus:
        executable = shutil.which(self._argv[0])
        if executable is None:
            return BackendStatus(self.name, False, f"{self._argv[0]!r} not on PATH", self.describe())
        return BackendStatus(self.name, True, "ready", " ".join(self._argv))


class OpenBaoBackend(SecretBackend):
    """OpenBao (MPL-2.0, the Linux Foundation fork of Vault) over its HTTP API.

    Reads KV-v2 at ``{address}/v1/{mount}/data/{path}`` and merges the
    ``data.data`` maps of every configured path into one namespace. The design
    point is that the *server* decides what this identity may read — a
    deny-by-default policy per node means a token that can read the collector's
    path cannot read the reviewer's, and mini-ork cannot widen that by accident
    because it never holds a broader credential.

    Fetching is deferred to the first lookup so a backend that is declared but
    unused costs nothing, and a 404 on one path is not an error (an operator
    may add paths as they need them).
    """

    name = "openbao"
    protected = True

    def __init__(
        self,
        address: str,
        *,
        mount: str = "secret",
        paths: list[str] | None = None,
        token: str = "",
        token_env: str = "OPENBAO_TOKEN",
        namespace: str = "",
        timeout: float = 5.0,
    ) -> None:
        if not address:
            raise VaultError("openbao backend: 'address' is required")
        self._address = address.rstrip("/")
        self._mount = mount.strip("/") or "secret"
        self._paths = [str(p).strip("/") for p in (paths or []) if str(p).strip("/")]
        self._token = token
        self._token_env = token_env
        self._namespace = namespace
        self._timeout = timeout
        self._values: dict[str, str] | None = None

    def _resolve_token(self) -> str:
        if self._token:
            return self._token
        return str(os.environ.get(self._token_env) or "")

    def _fetch_path(self, path: str, token: str) -> dict[str, str]:
        url = f"{self._address}/v1/{self._mount}/data/{path}"
        request = urllib.request.Request(url, method="GET")
        request.add_header("X-Vault-Token", token)
        if self._namespace:
            request.add_header("X-Vault-Namespace", self._namespace)
        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return {}
            if exc.code in (401, 403):
                raise VaultError(
                    f"openbao backend: {self._token_env} is not permitted to read "
                    f"{self._mount}/{path} (HTTP {exc.code})"
                ) from exc
            raise VaultError(
                f"openbao backend: reading {self._mount}/{path} failed with HTTP {exc.code}"
            ) from exc
        except (urllib.error.URLError, OSError) as exc:
            raise VaultError(
                f"openbao backend: cannot reach OpenBao at {self._address}: {exc}"
            ) from exc
        except ValueError as exc:
            raise VaultError(f"openbao backend: {url} did not return JSON: {exc}") from exc
        data = payload.get("data")
        if not isinstance(data, Mapping):
            return {}
        inner = data.get("data")
        if not isinstance(inner, Mapping):
            return {}
        return {str(k): str(v) for k, v in inner.items() if v is not None}

    def _load(self) -> dict[str, str]:
        if self._values is not None:
            return self._values
        if not self._paths:
            raise VaultError(
                "openbao backend: no 'paths' configured; a deny-by-default policy "
                "means an unlisted path cannot be read, so this would always miss"
            )
        token = self._resolve_token()
        if not token:
            raise VaultError(
                f"openbao backend: no token; set {self._token_env} (a short-lived, "
                "path-scoped token, not the root token)"
            )
        merged: dict[str, str] = {}
        for path in self._paths:
            merged.update(self._fetch_path(path, token))
        self._values = merged
        return merged

    def get(self, key: str) -> str | None:
        return self._load().get(key)

    def describe(self) -> str:
        return f"OpenBao at {self._address} (mount {self._mount})"

    def status(self) -> BackendStatus:
        if not self._paths:
            return BackendStatus(self.name, False, "no paths configured", self._address)
        if not self._resolve_token():
            return BackendStatus(self.name, False, f"{self._token_env} is not set", self._address)
        try:
            self._load()
        except VaultError as exc:
            return BackendStatus(self.name, False, str(exc), self._address)
        return BackendStatus(
            self.name, True, f"ready ({len(self._values or {})} keys over {len(self._paths)} paths)",
            self._address,
        )


@dataclass
class VaultConfig:
    """The declared backend chain, in precedence order."""

    backends: list[SecretBackend] = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return not self.backends


def vault_config_path(env: Mapping[str, str] | None = None) -> Path:
    e = os.environ if env is None else env
    home = str(e.get("MINI_ORK_HOME") or ".mini-ork").strip()
    return Path(home) / "config" / "vault.yaml"


def vault_gap_marker_path(env: Mapping[str, str] | None = None) -> Path:
    """Where the last-reported protection gap is remembered.

    Holds a signature of the backend chain (backend names and where they read
    from) — never a value — so the same note is not repeated on every run while
    the situation is unchanged.
    """
    e = os.environ if env is None else env
    home = str(e.get("MINI_ORK_HOME") or ".mini-ork").strip()
    return Path(home) / "state" / "vault-protection.sig"


def _backend_from_spec(spec: Mapping[str, Any], *, base: Path) -> SecretBackend:
    kind = str(spec.get("type") or "").strip().lower()
    if kind == "env":
        return EnvBackend()
    if kind == "file":
        path = spec.get("file") or spec.get("path")
        if path:
            candidate = Path(str(path)).expanduser()
            if not candidate.is_absolute():
                candidate = base / candidate
            return FileBackend(candidate)
        return FileBackend()
    if kind == "sops":
        raw = str(spec.get("file") or "").strip()
        if not raw:
            raise VaultError("sops backend needs 'file:'")
        candidate = Path(raw).expanduser()
        if not candidate.is_absolute():
            candidate = base / candidate
        return SopsBackend(candidate)
    if kind == "command":
        argv = spec.get("argv")
        if isinstance(argv, str):
            argv = argv.split()
        if not isinstance(argv, list) or not argv:
            raise VaultError("command backend needs 'argv:' — a list, e.g. ['pass', 'show', '{key}']")
        return CommandBackend([str(part) for part in argv], cache=bool(spec.get("cache", True)))
    if kind == "openbao":
        paths = spec.get("paths") or []
        if isinstance(paths, str):
            paths = [part.strip() for part in paths.split(",") if part.strip()]
        if not isinstance(paths, list):
            raise VaultError("openbao backend: 'paths' must be a list or comma-separated string")
        timeout = spec.get("timeout", 5.0)
        try:
            timeout_value = float(timeout)
        except (TypeError, ValueError) as exc:
            raise VaultError(f"openbao backend: 'timeout' must be a number, got {timeout!r}") from exc
        return OpenBaoBackend(
            str(spec.get("address") or "http://127.0.0.1:8200"),
            mount=str(spec.get("mount") or "secret"),
            paths=[str(part) for part in paths],
            token=str(spec.get("token") or ""),
            token_env=str(spec.get("token_env") or "OPENBAO_TOKEN"),
            namespace=str(spec.get("namespace") or ""),
            timeout=timeout_value,
        )
    raise VaultError(
        f"unknown vault backend {kind!r}; known: env, file, sops, command, openbao"
    )


def parse_vault_config(
    document: object, *, base: Path | None = None
) -> VaultConfig:
    """Validate a parsed ``vault.yaml`` into a backend chain."""
    directory = base or Path.cwd()
    if document is None:
        return VaultConfig([EnvBackend(), FileBackend()])
    if not isinstance(document, Mapping):
        raise VaultError("vault config must be a mapping with a 'backends:' list")
    unknown = sorted(set(document) - {"backends"})
    if unknown:
        # A typo like `backend:` would otherwise silently leave the default
        # chain in place and resolve from the wrong place, or from nowhere.
        raise VaultError(f"unknown vault section(s) {', '.join(unknown)}; expected backends:")
    raw = document.get("backends")
    if raw is None:
        specs: list[Any] = list(DEFAULT_BACKEND_TYPES)
    elif isinstance(raw, list):
        specs = raw
    elif isinstance(raw, Mapping):
        specs = [raw]
    else:
        raise VaultError("vault config 'backends' must be a list")
    backends: list[SecretBackend] = []
    for spec in specs:
        if isinstance(spec, str):
            backends.append(_backend_from_spec({"type": spec}, base=directory))
        elif isinstance(spec, Mapping):
            backends.append(_backend_from_spec(spec, base=directory))
        else:
            raise VaultError(f"vault backend entry must be a mapping or a type name, got {spec!r}")
    return VaultConfig(backends)


def load_vault_config(env: Mapping[str, str] | None = None) -> VaultConfig:
    """Read ``$MINI_ORK_HOME/config/vault.yaml``. Absent → env + file."""
    e = os.environ if env is None else env
    path = vault_config_path(e)
    if not path.is_file():
        return parse_vault_config(None)
    try:
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise VaultError(f"cannot read vault config {path}: {exc}") from exc
    return parse_vault_config(document, base=path.parent)


class VaultEnvironment(Mapping[str, str]):
    """The backend chain, as the read-only mapping the placeholder expander wants.

    Lookups are lazy and cached, so declaring a backend costs nothing until a
    ``${NAME}`` actually needs it, and a value fetched for one node is not
    re-fetched for the next.
    """

    def __init__(self, backends: list[SecretBackend]) -> None:
        self._backends = backends
        self._cache: dict[str, str | None] = {}

    @property
    def backends(self) -> list[SecretBackend]:
        return list(self._backends)

    def describe(self) -> str:
        if not self._backends:
            return "no configured secret source"
        return " or ".join(backend.describe() for backend in self._backends)

    def protection_gap(self) -> str | None:
        """How to harden, when nothing in the chain protects a value at rest.

        ``env`` + ``file`` is a legitimate choice — the zero-setup tier — so this
        is a note and never a failure. A chain that already includes a backend
        which encrypts (``sops``) or delegates to a policy engine (``command``,
        ``openbao``) is already past the gap and reports nothing.
        """
        if not self._backends:
            return None
        if any(backend.protected for backend in self._backends):
            return None
        sources = " and ".join(backend.describe() for backend in self._backends)
        return (
            f"secrets resolve in the clear, from {sources}; that is the "
            f"zero-setup tier and fine for trying mini-ork. A shared or audited "
            f"run wants a 'sops', 'command' or 'openbao' backend in "
            f"{vault_config_path()}."
        )

    def signature(self) -> str:
        """A fingerprint of the chain for change detection. Carries no values."""
        return "|".join(f"{b.name}:{b.describe()}" for b in self._backends)

    def resolve(self, key: str) -> str | None:
        """The first backend that has the key wins. Never returns ``""``."""
        if key in self._cache:
            return self._cache[key]
        value: str | None = None
        for backend in self._backends:
            candidate = backend.get(key)
            if candidate is not None:
                value = candidate
                break
        self._cache[key] = value
        return value

    # ``expand_secret_references`` uses ``in`` and ``[]``; these two are the
    # whole contract. The rest exists so the object is a well-formed Mapping and
    # can be splatted anywhere the old ``{**stored, **runtime}`` dict went.
    def __getitem__(self, key: str) -> str:
        value = self.resolve(key)
        if value is None:
            raise KeyError(key)
        return value

    def __contains__(self, key: object) -> bool:
        return isinstance(key, str) and self.resolve(key) is not None

    def __iter__(self) -> Iterator[str]:
        seen: dict[str, None] = {}
        for backend in self._backends:
            seen.update(dict.fromkeys(backend.known_keys()))
        return iter(seen)

    def __len__(self) -> int:
        return sum(1 for _ in self)

    def status(self) -> list[BackendStatus]:
        return [backend.status() for backend in self._backends]


def vault_environment(environ: Mapping[str, str] | None = None) -> VaultEnvironment:
    """Build the chain from config + environment. Raises on a bad config.

    Raised at construction rather than at first lookup: a declared backend that
    cannot work means no lookup against it can succeed, and finding that out
    when a node is already running is worse than finding out at the boundary.
    """
    e = os.environ if environ is None else environ
    config = load_vault_config(e)
    return VaultEnvironment(config.backends)


def report_protection_gap(
    environment: VaultEnvironment, environ: Mapping[str, str] | None = None
) -> bool:
    """Write the protection gap to stderr once, and only when the chain changes.

    Repeating a warning on every run is a warning that gets ignored, and this one
    describes a deliberate choice rather than a fault. So the chain's signature is
    remembered beside the vault: the note appears when the situation changes — a
    backend is added or removed — and stays quiet in between. Never raises and
    never blocks the run; it is advice, and it is printed where the run's other
    diagnostics go.

    Returns whether it wrote the note, which is what the tests assert on.
    """
    try:
        gap = environment.protection_gap()
        signature = environment.signature()
    except VaultError:
        return False
    if gap is None:
        return False

    e = os.environ if environ is None else environ
    marker = vault_gap_marker_path(e)
    try:
        if marker.is_file() and marker.read_text(encoding="utf-8").strip() == signature:
            return False
    except (OSError, ValueError):
        # An unreadable marker just means the note is repeated once more.
        pass

    sys.stderr.write(f"[vault] {gap}\n")
    try:
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(signature, encoding="utf-8")
    except OSError as exc:
        # Advice that could not be recorded is still advice worth giving.
        sys.stderr.write(f"[vault] could not record the note: {exc}\n")
    return True
