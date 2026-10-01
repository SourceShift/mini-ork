"""Engine bundle management + health (kickoff requirement 6).

The node-agent archives engine bundles the control plane uploads as git
bundles into ``engines/<sha>/`` and smoke-imports ``mini_ork`` against
each staged sha. ``/v1/health`` aggregates that state plus session
counts and Docker reachability for the control plane's polling loop.

The bundle transport is the no-op-safe subset of epic 07's git
semantics: ``git bundle verify``, then ``git fetch`` into
``refs/mo/sync/*`` — no automatic ``git checkout``. The control plane
decides when to switch.
"""
from __future__ import annotations

import hashlib
import os
import subprocess
import tarfile
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .procs import _run as default_run


@dataclass
class EngineState:
    sha: str
    staged: bool = False
    importable: bool = False

    def to_dict(self) -> dict:
        return {"sha": self.sha, "staged": self.staged, "importable": self.importable}


class EngineManager:
    """Owns the per-sha engine bundles staged under ``<state>/engines/``."""

    def __init__(
        self,
        state_dir: Path,
        version: str,
        *,
        session_count_fn: Callable[[], int] | None = None,
        _run_fn: Callable | None = None,
    ) -> None:
        self.state_dir = Path(state_dir)
        self.engines_dir = self.state_dir / "engines"
        self.engines_dir.mkdir(parents=True, exist_ok=True)
        self.version = version
        self._session_count = session_count_fn or (lambda: 0)
        self._run = _run_fn or default_run
        self._engines: dict[str, EngineState] = {}
        self._rebuild()

    # ---- rebuild ------------------------------------------------------

    def _rebuild(self) -> None:
        for d in self.engines_dir.iterdir() if self.engines_dir.exists() else []:
            if not d.is_dir():
                continue
            self._engines[d.name] = EngineState(sha=d.name, staged=True, importable=self._smoke(d))

    def _smoke(self, engine_dir: Path) -> bool:
        """Return True iff ``python3 -c 'import mini_ork'`` succeeds against the engine."""
        py = sys_executable()
        env = {**os.environ, "PYTHONPATH": str(engine_dir)}
        try:
            r = subprocess.run(
                [py, "-c", "import mini_ork"],
                env=env, capture_output=True, text=True, check=False, timeout=30,
            )
        except (OSError, subprocess.TimeoutExpired):
            return False
        return r.returncode == 0

    # ---- public -------------------------------------------------------

    def stage_bundle(self, sha: str, bundle_bytes: bytes) -> EngineState:
        """Verify the bundle, archive it into ``engines/<sha>``, then smoke-import."""
        with tempfile.NamedTemporaryFile(suffix=".bundle", delete=False) as tf:
            tf.write(bundle_bytes)
            bundle_path = Path(tf.name)
        try:
            verify = self._run(["git", "bundle", "verify", str(bundle_path)])
            if verify.returncode != 0:
                raise RuntimeError(
                    f"git bundle verify failed: {verify.stderr.strip() or verify.stdout.strip()}"
                )
            target = self.engines_dir / sha
            target.mkdir(parents=True, exist_ok=True)
            archive = self._run(
                ["git", "archive", "--format=tar", sha, "-o", str(target / "_src.tar")]
            )
            if archive.returncode != 0:
                raise RuntimeError(
                    f"git archive failed: {archive.stderr.strip() or archive.stdout.strip()}"
                )
            with tarfile.open(target / "_src.tar", "r:") as tf:
                tf.extractall(path=target, filter="data")
            (target / "_src.tar").unlink()
        finally:
            bundle_path.unlink(missing_ok=True)
        importable = self._smoke(target)
        state = EngineState(sha=sha, staged=True, importable=importable)
        self._engines[sha] = state
        return state

    def list(self) -> list[dict]:
        return [s.to_dict() for s in self._engines.values()]

    def health(self, *, docker_ok: bool) -> dict:
        return {
            "version": self.version,
            "engine_shas": [s.to_dict() for s in self._engines.values()],
            "docker_ok": docker_ok,
            "sessions": self._session_count(),
            "capacity": self._capacity(),
        }

    def _capacity(self) -> dict:
        return {
            "engines": len(self._engines),
            "engines_dir": str(self.engines_dir),
        }


def sys_executable() -> str:
    return sys_executable_cached


sys_executable_cached = "/usr/bin/env python3"


def file_sha256(path: Path, *, chunk: int = 1 << 16) -> str:
    """SHA-256 over a file, used by ``POST /v1/files/manifest``."""
    h = hashlib.sha256()
    with path.open("rb") as f:
        while True:
            buf = f.read(chunk)
            if not buf:
                break
            h.update(buf)
    return h.hexdigest()


def manifest_for(root: Path) -> dict[str, str]:
    """Return ``{relpath: sha256}`` for every file under ``root``."""
    out: dict[str, str] = {}
    for p in root.rglob("*"):
        if p.is_file():
            out[str(p.relative_to(root))] = file_sha256(p)
    return out


__all__ = ["EngineManager", "EngineState", "manifest_for", "file_sha256"]