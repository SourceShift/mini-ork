"""Pluggable physical backend for run-scoped artifacts.

``ArtifactLedger`` (``artifacts.py``) owns the *semantic* contract — declared
ports, sha256 manifests, visibility, scoped input materialization. This module
owns the *physical* substrate underneath it: given a ``(run_id, rel_path)``
address, where do the bytes live and how does an agent in another process
reach them?

Two invariants make artifacts both shareable and leak-proof:

1. **Address by run_id, resolve centrally.** A run's identity is ``run_id`` —
   injected once, never mutated. The physical run directory is *derived* from
   it (``<MINI_ORK_HOME>/runs/<run_id>``). Nothing downstream recomputes that
   path from an ambient ``MINI_ORK_RUN_DIR``, which a long-lived worker can
   leak into the environment and thereby split one run across two directories
   (producer writes here, verifier reads there). The store is the single
   resolver, and it keys on the stable identity — not the leak-prone path.

2. **Agents see a local path; the backend owns durability.** An external
   harness (Claude Code, Codex, …) can only read and write a local filesystem
   path. Every backend therefore exposes a *local working path* for an address
   via :meth:`ArtifactStore.local_path`. Durable/remote backends mirror that
   local materialization through :meth:`publish` / :meth:`fetch`; the local
   backend's mirror is a no-op. New backends (s3, gcs, db) register via
   :func:`register_artifact_backend` and are selected with
   ``MO_ARTIFACT_BACKEND``.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable
from pathlib import Path

from mini_ork.context import context_env


class ArtifactStoreError(RuntimeError):
    """Raised when an artifact address is invalid or escapes its run root."""


def resolve_home(home: str | Path | None = None) -> Path:
    """Resolve ``MINI_ORK_HOME`` — mirrors ``execute.py`` but contextvar-aware.

    Explicit ``home`` wins; else the contextvar/env binding; else the
    ``<cwd>/.mini-ork`` default the rest of the runtime uses.
    """
    raw = str(home) if home else (context_env("MINI_ORK_HOME") or "")
    return Path(raw) if raw else (Path.cwd() / ".mini-ork")


def resolve_run_root(
    run_id: str,
    *,
    base_dir: str | Path | None = None,
    home: str | Path | None = None,
) -> Path:
    """Resolve a run's LOCAL root from its stable identity — never blindly from
    a leak-prone ambient ``MINI_ORK_RUN_DIR``.

    Order (first hit wins) — explicit argument beats derived guess beats leak:

    1. the explicit ``base_dir`` the caller computed (the dispatcher's
       plan-derived ``run_dir``). It is authoritative AND leak-proof: it is a
       function argument, not process env, so a long-lived worker can't leak it.
       For a real ``mini-ork run`` it already equals ``<home>/runs/<run_id>``,
       so this is a no-op on the happy path; it only decides the outcome when
       they disagree (benchmarks/tests, or a run_id collision), and there the
       explicit argument must win over both a guessed home dir and a stray env.
    2. ``<home>/runs/<run_id>`` when that directory already exists — the
       run_id-addressed path for callers that hold only the stable identity
       (a bare ``execute`` with no plan, or a peer agent fetching by run_id).
       Immune to a leaked ambient run-dir because it keys on run_id.
    3. the ambient ``MINI_ORK_RUN_DIR`` — preserved only as a last resort for a
       bare ``execute`` with no plan and no scaffolded run dir.
    4. ``<home>/runs/<run_id>`` even if not yet created (a fresh run about to
       scaffold it).
    """
    if base_dir:
        return Path(base_dir).resolve()
    home_runs = resolve_home(home) / "runs"
    if run_id and (home_runs / run_id).exists():
        return (home_runs / run_id).resolve()
    ambient = context_env("MINI_ORK_RUN_DIR")
    if ambient:
        return Path(ambient).resolve()
    if run_id:
        return (home_runs / run_id).resolve()
    raise ArtifactStoreError("cannot resolve run root without run_id or base_dir")


class ArtifactStore(ABC):
    """Physical backend for one run's artifacts, addressed by ``rel_path``.

    ``rel_path`` is the recipe-declared path, relative to the run root. A store
    instance is scoped to a single ``run_id``; the run is implicit in every
    call so callers never pass a run directory around (and so can't leak one).
    """

    run_id: str

    @property
    @abstractmethod
    def run_root(self) -> Path:
        """Local working root — the physical directory an agent reads/writes."""

    def _guarded(self, rel_path: str) -> Path:
        """Resolve ``rel_path`` under ``run_root``, rejecting any escape."""
        root = self.run_root
        candidate = (root / rel_path).resolve()
        if candidate != root and root not in candidate.parents:
            raise ArtifactStoreError(f"artifact path escapes run root: {rel_path}")
        return candidate

    @abstractmethod
    def local_path(self, rel_path: str) -> Path:
        """Local filesystem path for ``rel_path`` (what a harness writes to)."""

    @abstractmethod
    def exists(self, rel_path: str) -> bool: ...

    @abstractmethod
    def read_bytes(self, rel_path: str) -> bytes: ...

    @abstractmethod
    def write_bytes(self, rel_path: str, data: bytes) -> None: ...

    @abstractmethod
    def size(self, rel_path: str) -> int: ...

    def uri(self, rel_path: str) -> str:
        """Backend-agnostic address a peer agent can resolve: ``artifact://…``."""
        return f"artifact://{self.run_id}/{str(rel_path).lstrip('/')}"

    # ── Durability mirror — a no-op for a purely local backend, the sync seam
    #    a remote backend (s3/gcs/db) overrides. ``publish`` pushes a freshly
    #    written local artifact to the backing store; ``fetch`` pulls one into
    #    the local working root before a consumer reads it. ─────────────────
    def publish(self, rel_path: str) -> None:  # noqa: B027 - intentional default no-op
        return None

    def fetch(self, rel_path: str) -> None:  # noqa: B027 - intentional default no-op
        return None


class LocalArtifactStore(ArtifactStore):
    """Filesystem backend: the local run directory *is* the durable store, so
    :meth:`publish` / :meth:`fetch` are no-ops."""

    def __init__(self, root: str | Path, run_id: str = "") -> None:
        self._root = Path(root).resolve()
        self.run_id = run_id or self._root.name

    @classmethod
    def for_run(
        cls,
        run_id: str,
        *,
        base_dir: str | Path | None = None,
        home: str | Path | None = None,
    ) -> "LocalArtifactStore":
        """Construct from the stable identity, resolving the root leak-proofly."""
        return cls(resolve_run_root(run_id, base_dir=base_dir, home=home), run_id)

    @property
    def run_root(self) -> Path:
        return self._root

    def local_path(self, rel_path: str) -> Path:
        return self._guarded(rel_path)

    def exists(self, rel_path: str) -> bool:
        return self._guarded(rel_path).is_file()

    def read_bytes(self, rel_path: str) -> bytes:
        return self._guarded(rel_path).read_bytes()

    def write_bytes(self, rel_path: str, data: bytes) -> None:
        target = self._guarded(rel_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)

    def size(self, rel_path: str) -> int:
        return self._guarded(rel_path).stat().st_size


# ── Backend registry (OCP seam, mirroring register_embedder_provider) ────────
StoreFactory = Callable[..., ArtifactStore]
_ARTIFACT_BACKENDS: dict[str, StoreFactory] = {}


def register_artifact_backend(name: str, factory: StoreFactory) -> None:
    """Register a pluggable artifact backend, selected via ``MO_ARTIFACT_BACKEND``.

    ``factory(run_id, *, base_dir=None, home=None) -> ArtifactStore``.
    """
    _ARTIFACT_BACKENDS[name] = factory


def _local_factory(run_id: str, *, base_dir=None, home=None) -> ArtifactStore:
    return LocalArtifactStore.for_run(run_id, base_dir=base_dir, home=home)


register_artifact_backend("local", _local_factory)


def make_artifact_store(
    run_id: str,
    *,
    base_dir: str | Path | None = None,
    home: str | Path | None = None,
    backend: str | None = None,
) -> ArtifactStore:
    """Build the artifact store for ``run_id`` using the selected backend.

    Backend precedence: explicit ``backend`` arg, else ``MO_ARTIFACT_BACKEND``,
    else ``local``.
    """
    name = (backend or context_env("MO_ARTIFACT_BACKEND") or "local").strip() or "local"
    try:
        factory = _ARTIFACT_BACKENDS[name]
    except KeyError as exc:
        raise ArtifactStoreError(
            f"unknown artifact backend {name!r}; registered: {sorted(_ARTIFACT_BACKENDS)}"
        ) from exc
    return factory(run_id, base_dir=base_dir, home=home)
