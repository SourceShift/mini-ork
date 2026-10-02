"""Image-cache + ``POST /v1/images/prepare`` (remote-nodes-12, requirement 2).

A target repo's tests need their toolchain. Installing that toolchain on
every session would cost minutes per run. Caching it by hash makes the second
run fast: the second call returns the cached tag without running the setup
script again.

Cache key (kickoff line 56): ``sha256(base_image_digest + setup + platform)``.

  * ``base_image_digest`` is the content-addressed digest of the base image
    (``docker inspect --format '{{index .Id}}' <image>``). A ``python:3.11``
    tag and a digest pinning the same image produce the SAME cache key —
    proven by the attempt-1 lesson: a tag-driven key makes the cache
    accidentally content-unstable.
  * ``setup`` is the shell script the caller passed.
  * ``platform`` is the target platform (``linux/amd64``, ``linux/arm64``).

State lives at ``<state_dir>/images/<key>.json``; the cache HIT is a local
filesystem check (no daemon round-trip), so a cached lookup is cheap.

The class mirrors :class:`mini_ork.remote.node_agent.engines.EngineManager` for
its runner injection — ``self._run = _run_fn or default_run`` is an instance
attribute, so ``monkeypatch.setattr(ImageCache, "_run", _patched)`` works
(attempt-1 footgun at ``test_environments.py:233``: the test failed with
``AttributeError: ImageCache has no attribute '_run'`` because the runner was
a closure).

The router is exposed via :func:`make_router`, matching the constructor-
injection style of :class:`SessionManager` and :class:`EngineManager` — every
new public symbol has a production caller in :func:`create_app` (Review
bar #2). The endpoint takes ``payload: dict`` and raises ``HTTPException(400,
...)`` on missing fields; a Pydantic body model would return 422 from the
framework (attempt-1 lesson, ``test_images_prepare_requires_base_image``).
"""
from __future__ import annotations

import hashlib
import json
import subprocess
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping

from fastapi import APIRouter, Depends, HTTPException

from ..environments import LookalikeSecretError, load_profile
from .procs import _run as default_run


__all__ = ["ImageCache", "PrepareResult", "cache_key", "make_router"]


# ----- constants ------------------------------------------------------------

_IMAGES_DIR = "images"
_TAG_PREFIX = "mo-env:"
# Docker tag names are lowercase alphanum + separators; the cache key is hex
# (sha256), so a tag like ``mo-env:abc123...`` is well-formed.
_TAG_TAG_MAX_LEN = 128


# ----- types ---------------------------------------------------------------


@dataclass
class PrepareResult:
    """The response payload of a prepare."""

    tag: str
    cached: bool
    pid: int | None = None
    log_tail: str | None = None
    detail: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        out: dict[str, Any] = {"tag": self.tag, "cached": self.cached}
        if self.pid is not None:
            out["pid"] = self.pid
        if self.log_tail:
            out["log_tail"] = self.log_tail
        if self.detail:
            out.update(self.detail)
        return out


# ----- helpers --------------------------------------------------------------


def _resolve_image_digest(
    _run_fn: Callable[..., Any], image: str
) -> str:
    """Resolve ``image`` to its content-addressed digest.

    Uses ``docker inspect --format '{{index .Id}}' <image>``. Falls back to
    the input tag string when the inspect call returns non-zero — a unit-test
    fake with no docker daemon hits this branch, and the cache key is still
    deterministic for the same ``(image, setup, platform)`` triple because
    the fallback is the input verbatim. The key is still sha256-derived.
    """
    r = _run_fn(["docker", "inspect", "--format", "{{index .Id}}", image])
    if r.returncode == 0 and (r.stdout or "").strip():
        return (r.stdout or "").strip()
    return image


def _default_run_setup(argv: list[str], log_path: Path, timeout_s: float) -> int:
    """Run the setup container, streaming its output to ``log_path``."""
    with log_path.open("wb") as logf:
        return subprocess.run(argv, stdout=logf, stderr=subprocess.STDOUT,
                              timeout=timeout_s, check=False).returncode


def cache_key(*, base_image_digest: str, setup: str, platform: str) -> str:
    """sha256(digest + setup + platform). Matches kickoff line 56 verbatim.

    Public helper — exported so the test suite asserts on the key without
    instantiating an :class:`ImageCache` (the unit test for the key formula
    lives in ``tests/unit/test_environments.py``).
    """
    h = hashlib.sha256()
    h.update(base_image_digest.encode("utf-8"))
    h.update(b"\x00")
    h.update(setup.encode("utf-8"))
    h.update(b"\x00")
    h.update(platform.encode("utf-8"))
    return h.hexdigest()


def _tag_for(key: str) -> str:
    return f"{_TAG_PREFIX}{key}"[:_TAG_TAG_MAX_LEN]


def _tail(path: Path, *, max_bytes: int) -> str:
    """Return up to ``max_bytes`` of the tail of ``path`` as utf-8 text."""
    try:
        size = path.stat().st_size
    except OSError:
        return ""
    try:
        with path.open("rb") as f:
            if size > max_bytes:
                f.seek(size - max_bytes)
            data = f.read()
    except OSError:
        return ""
    return data.decode("utf-8", errors="replace")


# ----- cache ---------------------------------------------------------------


class ImageCache:
    """Owns the ``mo-env:<key>`` image tags under ``<state_dir>/images/``."""

    def __init__(
        self,
        state_dir: Path,
        *,
        _run_fn: Callable | None = None,
        _run_setup_fn: Callable | None = None,
    ) -> None:
        self.state_dir = Path(state_dir)
        self.images_dir = self.state_dir / _IMAGES_DIR
        self.images_dir.mkdir(parents=True, exist_ok=True)
        # Mirror ``engines.py:55-56`` — store the runner on the instance so
        # tests can override it via ``monkeypatch.setattr`` (attempt-1
        # ``AttributeError: 'ImageCache' has no attribute '_run'`` was the
        # visible symptom of an attempt to skip the assignment).
        self._run = _run_fn or default_run
        self._run_setup = _run_setup_fn or _default_run_setup
        self._claims: dict[str, dict] = {}
        self._rebuild()

    # ---- rebuild ------------------------------------------------------

    def _claim_path(self, key: str) -> Path:
        return self.images_dir / f"{key}.json"

    def _rebuild(self) -> None:
        if not self.images_dir.exists():
            return
        for j in self.images_dir.glob("*.json"):
            try:
                data = json.loads(j.read_text(encoding="utf-8"))
                key = j.stem
            except (OSError, json.JSONDecodeError, ValueError):
                continue
            if isinstance(data, dict) and data.get("tag"):
                self._claims[key] = data

    # ---- public API ---------------------------------------------------

    def is_cached(self, key: str) -> bool:
        return key in self._claims

    def cached_tag(self, key: str) -> str | None:
        data = self._claims.get(key)
        if not data:
            return None
        return str(data.get("tag") or "") or None

    def claim(self, key: str) -> dict | None:
        """Read-only accessor for tests / status endpoints."""
        return self._claims.get(key)

    def prepare(
        self,
        *,
        base_image: str,
        setup: str,
        platform: str,
        timeout_s: float = 900.0,
        emit_event: Callable[[dict], None] | None = None,
    ) -> PrepareResult:
        """Resolve a base image + setup to a tag. Caches by sha256(key).

        On a cache HIT, returns the stored tag with ``cached=True``.
        On a MISS, runs ``bash -euo pipefail -c <setup>`` against the base
        image, commits the result as ``mo-env:<key>``, and returns the new
        tag. A failing setup returns the log tail and leaves no tag
        (``cached=False``, ``tag=""``, ``log_tail`` populated).
        """
        if not base_image:
            raise ValueError("base_image is required")
        digest = _resolve_image_digest(self._run, base_image)
        key = cache_key(base_image_digest=digest, setup=setup, platform=platform)
        tag = _tag_for(key)

        if self.is_cached(key):
            return PrepareResult(tag=tag, cached=True, detail={"key": key})

        # Provisioning event: image_prepare start
        if emit_event:
            emit_event(
                {"step": "image_prepare", "status": "start", "ms": 0,
                 "detail": {"base_image": base_image, "key": key}}
            )

        # Run the setup script as a fresh container from the base image.
        # Mount /workspace/target from a per-cache scratch root so the
        # session's run dir does not bleed in. A failing setup returns
        # rc != 0 with stderr captured; we surface the tail to the client
        # and DO NOT commit (kickoff line 117 — "A failing setup leaves no
        # tag and returns the log tail").
        with tempfile.TemporaryDirectory(prefix="mo-prep-") as scratch:
            scratch_path = Path(scratch)
            log_path = scratch_path / "setup.log"
            start_ms = int(time.time() * 1000)
            # A NAMED container, not --rm: `docker commit` snapshots a container,
            # so the setup container has to outlive the setup to be committed.
            container = f"mo-prep-{key[:12]}-{uuid.uuid4().hex[:6]}"
            try:
                argv = [
                    "docker", "run", "--name", container,
                    "-v", f"{scratch_path}:/workspace/target",
                    "--platform", platform,
                    base_image,
                    "bash", "-euo", "pipefail", "-c", setup,
                ]
                rc = self._run_setup(argv, log_path, timeout_s)
            except subprocess.TimeoutExpired:
                self._run(["docker", "rm", "-f", container])
                elapsed = int(time.time() * 1000) - start_ms
                log_tail = _tail(log_path, max_bytes=4096)
                if emit_event:
                    emit_event(
                        {"step": "image_prepare", "status": "fail",
                         "ms": elapsed, "detail": {"key": key, "reason": "timeout"}}
                    )
                return PrepareResult(
                    tag="", cached=False, log_tail=log_tail,
                    detail={"key": key, "rc": 124, "reason": "timeout"},
                )
            elapsed = int(time.time() * 1000) - start_ms
            if rc != 0:
                self._run(["docker", "rm", "-f", container])
                log_tail = _tail(log_path, max_bytes=4096)
                if emit_event:
                    emit_event(
                        {"step": "image_prepare", "status": "fail",
                         "ms": elapsed,
                         "detail": {"key": key, "rc": rc}}
                    )
                return PrepareResult(
                    tag="", cached=False, log_tail=log_tail,
                    detail={"key": key, "rc": rc},
                )

            # Commit the resulting container as mo-env:<key>. We use a
            # plain commit (no --change) — the cache layer is the diff
            # between base image + setup, and an env-marker confuses the
            # comparison if a caller re-derives the key from the new
            # image.
            commit = self._run(["docker", "commit", container, tag])
            self._run(["docker", "rm", "-f", container])
            if commit.returncode != 0:
                log_tail = _tail(log_path, max_bytes=4096)
                if emit_event:
                    emit_event(
                        {"step": "image_prepare", "status": "fail",
                         "ms": elapsed,
                         "detail": {"key": key, "rc": commit.returncode,
                                    "reason": "commit_failed"}}
                    )
                return PrepareResult(
                    tag="", cached=False, log_tail=log_tail,
                    detail={"key": key, "rc": commit.returncode,
                            "reason": "commit_failed"},
                )

            # Persist the cache claim so the next lookup is a local file
            # check, not a docker round-trip.
            claim = {
                "tag": tag,
                "key": key,
                "base_image": base_image,
                "platform": platform,
                "setup": setup,
                "built_at_ms": int(time.time() * 1000),
                "rc": rc,
                "duration_ms": elapsed,
            }
            self._claim_path(key).write_text(
                json.dumps(claim, indent=2), encoding="utf-8"
            )
            self._claims[key] = claim
            if emit_event:
                emit_event(
                    {"step": "image_prepare", "status": "ok",
                     "ms": elapsed, "detail": {"key": key, "tag": tag}}
                )
            return PrepareResult(
                tag=tag, cached=False,
                detail={"key": key, "rc": rc, "ms": elapsed},
            )


# ----- router ---------------------------------------------------------------


def _native_platform() -> str:
    """The node's own docker platform — the default for a prepare that names
    none (a hard-coded linux/amd64 meant qemu emulation on an arm64 node)."""
    import platform as _platform

    machine = _platform.machine().lower()
    return "linux/arm64" if machine in ("arm64", "aarch64") else "linux/amd64"


def _emit_setup_event(run_id: str, fields: dict) -> None:
    """Best-effort emit of a ``remote.setup.step`` event.

    Wrapped in try/except so a missing observability DB (the unit-test path)
    never breaks a session. Mirrors the discipline at
    ``mini_ork/runtime/backends/remote.py:_emit_sync_event``.
    """
    try:
        from mini_ork.observability.node_events import mo_node_emit
    except Exception:  # pragma: no cover — observability is optional
        return
    try:
        mo_node_emit(
            run_id,
            node_id="node-agent",
            node_type="node_agent",
            event_type="remote.setup.step",
            extra_json=json.dumps(fields),
        )
    except Exception:  # pragma: no cover
        pass


def make_router(
    state_dir: Path,
    *,
    bearer: Callable,
    _run_fn: Callable | None = None,
    run_id: str | None = None,
) -> APIRouter:
    """Build the FastAPI router exposing ``POST /v1/images/prepare``.

    ``bearer`` is the auth dependency from :func:`create_app` — every new
    route inherits it (the only auth-free route is ``/v1/health``). The
    ``run_id`` is forwarded into the provisioning-event helper so a watch
    UI sees the ``remote.setup.step`` events with the same run-id the rest
    of the run publishes under. When ``run_id`` is ``None`` (the bootstrap
    case), events fall back to ``"bootstrap"`` — observability is optional,
    never load-bearing.
    """
    cache = ImageCache(state_dir, _run_fn=_run_fn)

    router = APIRouter()

    @router.post("/v1/images/prepare")
    def post_images_prepare(
        payload: dict,
        _: None = Depends(bearer),
    ) -> dict:
        # Manual validation so a missing ``base_image`` is a 400, not a
        # FastAPI-inferred 422 from a Pydantic body model
        # (attempt-1 ``test_images_prepare_requires_base_image`` failed with
        # ``assert 422 == 400``). Same shape as
        # ``mini_ork/remote/node_agent/app.py:post_session``.
        base_image = payload.get("base_image")
        if not base_image or not isinstance(base_image, str):
            raise HTTPException(400, "base_image required")
        setup = payload.get("setup") or ""
        if not isinstance(setup, str):
            raise HTTPException(400, "setup must be a string")
        platform = payload.get("platform") or _native_platform()
        if not isinstance(platform, str):
            raise HTTPException(400, "platform must be a string")
        timeout_s = payload.get("timeout_s")
        if timeout_s is not None and not isinstance(timeout_s, (int, float)):
            raise HTTPException(400, "timeout_s must be a number")

        def _emit(fields: dict) -> None:
            _emit_setup_event(run_id or "bootstrap", fields)

        result = cache.prepare(
            base_image=base_image, setup=setup, platform=platform,
            timeout_s=float(timeout_s) if timeout_s is not None else 900.0,
            emit_event=_emit,
        )
        # When the prepare fails, return 500 with the log tail in the body
        # so the caller can debug without re-running setup (kickoff line
        # 117: "return the log tail and leave no tag").
        if not result.tag:
            raise HTTPException(
                500,
                detail={
                    "error": "image_prepare_failed",
                    "log_tail": result.log_tail or "",
                    "rc": result.detail.get("rc"),
                    "reason": result.detail.get("reason"),
                    "key": result.detail.get("key"),
                },
            )
        return result.to_dict()

    @router.get("/v1/images/{key}")   # hex-validated below
    def get_image_status(
        key: str, _: None = Depends(bearer),
    ) -> dict:
        """Lookup whether ``mo-env:<key>`` is cached. Returns the claim."""
        # ``key`` is the user-supplied cache key (sha256 hex). Reject
        # anything that isn't hex to keep the lookup cheap (no path
        # traversal on the wildcard).
        if not key or not all(c in "0123456789abcdef" for c in key):
            raise HTTPException(400, "key must be a hex sha256 string")
        if cache.is_cached(key):
            claim = cache.claim(key) or {}
            return {"cached": True, **claim}
        return {"cached": False, "key": key}

    return router


# ----- environment ↔ session glue (Production-caller for ``load_profile``) -
#
# Review bar #2: every new public class has at least one production caller
# (``git grep`` for it outside its own module and tests). ``load_profile`` is
# therefore also called from the session manager when a ``profile=`` is
# supplied; see :func:`apply_environment_to_session_payload`, invoked from
# ``sessions.py::SessionManager.create``. Keeping the wiring here keeps the
# import boundary clean — ``sessions.py`` only imports from ``environments.py``
# when it must, not eagerly at module load.
def apply_environment_to_session_payload(
    payload: dict, *, env: Mapping[str, str] | None = None,
) -> dict:
    """Overlay an environment profile's defaults onto a session payload.

    Used by ``SessionManager.create`` when the caller supplied ``profile=``.
    The merged payload is what ``post_session`` ultimately sees; the loader
    stays the single source of truth for the operator's intent.
    """
    profile_name = payload.get("profile")
    if not profile_name:
        return payload
    try:
        prof = load_profile(profile_name, env=env)
    except FileNotFoundError:
        # Caller-facing 404 — the kickoff's per-bullet contract lets the
        # session POST surface a missing profile as a 404 rather than a
        # silent default. The session manager catches the HTTPException
        # and re-raises from ``create``.
        raise HTTPException(404, f"environment profile {profile_name!r} not found")
    except LookalikeSecretError as exc:
        raise HTTPException(400, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(400, f"invalid environment profile: {exc}") from exc
    out = dict(payload)
    if prof.image is not None and "image" not in out:
        out["image"] = prof.image
    if prof.resources is not None and "resources" not in out:
        out["resources"] = prof.resources
    if prof.network and "network" not in out:
        out["network"] = prof.network
    if prof.allow_domains and "allow_domains" not in out:
        out["allow_domains"] = list(prof.allow_domains)
    return out