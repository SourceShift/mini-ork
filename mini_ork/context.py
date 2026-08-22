"""Canonical run-context and environment contract for the mini-ork runtime.

Historically the runtime communicated across layers by reading and mutating
``os.environ`` ad hoc (766 references across the package). This module is the
single source of truth for that contract:

- ``RunContext`` names the run-identity variables (``MINI_ORK_*``) as typed
  fields, with ``from_env`` / ``as_env`` / ``apply`` converting between the
  dataclass and the process environment in one tested place.
- ``node_env_overrides`` names the per-node dispatch variables (``MO_*``) the
  executor publishes before a node runs.
- ``apply_env_overrides`` / ``scoped_environ`` are the only sanctioned ways to
  mutate process env: overrides are explicit mappings where ``None`` means
  "remove", and ``scoped_environ`` restores prior state for scopes that must
  not leak.
- ``run_context_scope`` / ``context_env`` are the per-run isolation layer:
  overrides live in a ``contextvars.ContextVar`` (isolated per thread and per
  asyncio task) instead of the process-global ``os.environ``, so two runs can
  share one process without clobbering each other's variables. Readers fall
  back to ``os.environ`` for any key not bound in the current context, so the
  migration off the global env message bus can proceed incrementally.

Behavioral note: several consumers (in-process ``dispatch_fn`` seams, the
provider layer, and parity tests) intentionally read ``os.environ`` *during*
dispatch, so the executor still publishes these variables process-wide rather
than passing them purely as parameters. What changes is that the contract is
declared once, here, instead of being re-derived at every call site.
"""
from __future__ import annotations

import contextlib
import contextvars
import os
from dataclasses import dataclass, field
from typing import Iterator, Mapping, MutableMapping

# ── Run-identity variables (MINI_ORK_*) ──────────────────────────────────────

_ENV_ROOT = "MINI_ORK_ROOT"
_ENV_HOME = "MINI_ORK_HOME"
_ENV_DB = "MINI_ORK_DB"
_ENV_RUN_ID = "MINI_ORK_RUN_ID"
_ENV_RUN_DIR = "MINI_ORK_RUN_DIR"
_ENV_RECIPE = "MINI_ORK_RECIPE"
_ENV_TASK_CLASS = "MINI_ORK_TASK_CLASS"
_ENV_WORKFLOW = "MINI_ORK_WORKFLOW"
_ENV_PLAN_PATH = "MINI_ORK_PLAN_PATH"

# ── Per-node dispatch variables (MO_*) published by the executor ─────────────

ENV_RUN_DIR = _ENV_RUN_DIR
ENV_DISPATCH_CHAIN = "MO_DISPATCH_CHAIN"
ENV_NODE_ID = "MO_NODE_ID"
ENV_TARGET_CWD = "MO_TARGET_CWD"
ENV_RESUME_SESSION_ID = "MO_RESUME_SESSION_ID"


@dataclass(frozen=True)
class RunContext:
    """Typed view of the run-identity environment.

    Empty string means "unset" (matching the historical env contract, where
    unset and empty are treated alike by consumers).
    """

    root: str = ""
    home: str = ""
    db: str = ""
    run_id: str = ""
    run_dir: str = ""
    recipe: str = ""
    task_class: str = ""
    workflow: str = ""
    plan_path: str = ""
    extra: Mapping[str, str] = field(default_factory=dict)

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "RunContext":
        env = os.environ if env is None else env
        return cls(
            root=env.get(_ENV_ROOT, ""),
            home=env.get(_ENV_HOME, ""),
            db=env.get(_ENV_DB, ""),
            run_id=env.get(_ENV_RUN_ID, ""),
            run_dir=env.get(_ENV_RUN_DIR, ""),
            recipe=env.get(_ENV_RECIPE, ""),
            task_class=env.get(_ENV_TASK_CLASS, ""),
            workflow=env.get(_ENV_WORKFLOW, ""),
            plan_path=env.get(_ENV_PLAN_PATH, ""),
        )

    def db_or_default(self) -> str:
        """MINI_ORK_DB, else ``$MINI_ORK_HOME/state.db`` (home default ``.mini-ork``)."""
        return self.db or os.path.join(self.home or ".mini-ork", "state.db")

    def task_class_or_default(self) -> str:
        return self.task_class or "generic"

    def as_env(self) -> dict[str, str]:
        """The non-empty fields as their ``MINI_ORK_*`` variable names."""
        out = {
            _ENV_ROOT: self.root,
            _ENV_HOME: self.home,
            _ENV_DB: self.db,
            _ENV_RUN_ID: self.run_id,
            _ENV_RUN_DIR: self.run_dir,
            _ENV_RECIPE: self.recipe,
            _ENV_TASK_CLASS: self.task_class,
            _ENV_WORKFLOW: self.workflow,
            _ENV_PLAN_PATH: self.plan_path,
        }
        return {k: v for k, v in out.items() if v}

    def apply(self, env: MutableMapping[str, str] | None = None) -> None:
        """Publish the non-empty fields into ``env`` (default: process env)."""
        apply_env_overrides(self.as_env(), env=env)

    def child_env(self, **overrides: str | None) -> dict[str, str]:
        """A fresh env mapping: process env + this context + ``overrides``.

        ``None`` override values remove the key from the child env.
        """
        merged: dict[str, str] = {**os.environ, **self.as_env()}
        for key, value in overrides.items():
            if value is None:
                merged.pop(key, None)
            else:
                merged[key] = value
        return merged


def node_env_overrides(
    *,
    node_id: str,
    run_dir: str,
    dispatch_chain: str = "",
    target_cwd: str | None = None,
    resume_session_id: str | None = None,
) -> dict[str, str | None]:
    """The per-node variables the executor publishes before dispatching a node.

    ``resume_session_id=None`` means the variable is removed (stale session
    ids from a previous node must never leak into the next dispatch); pass a
    real id only when recovery has prepared a turn-resume for this node.
    ``target_cwd=None`` means "leave unchanged" — the implementer branch sets
    it, other node types must not clear a value a prior implementer exported
    for the provider wrappers.
    """
    overrides: dict[str, str | None] = {
        ENV_RUN_DIR: run_dir,
        ENV_NODE_ID: node_id,
        ENV_RESUME_SESSION_ID: resume_session_id,
    }
    if dispatch_chain:
        overrides[ENV_DISPATCH_CHAIN] = dispatch_chain
    if target_cwd is not None:
        overrides[ENV_TARGET_CWD] = target_cwd
    return overrides


def apply_env_overrides(
    overrides: Mapping[str, str | None],
    env: MutableMapping[str, str] | None = None,
) -> None:
    """Apply ``overrides`` to ``env`` (default: process env). ``None`` removes."""
    env = os.environ if env is None else env
    for key, value in overrides.items():
        if value is None:
            env.pop(key, None)
        else:
            env[key] = value


@contextlib.contextmanager
def scoped_environ(
    overrides: Mapping[str, str | None],
    env: MutableMapping[str, str] | None = None,
) -> Iterator[None]:
    """Apply ``overrides`` inside the block, then restore the previous state.

    Use for scopes that must not leak (subprocess env setup, tests). The
    executor's per-node publish deliberately does NOT use this yet: legacy
    consumers read the leaked values after ``dispatch_node`` returns, so that
    migration requires mapping every leak-dependent reader first.
    """
    env = os.environ if env is None else env
    sentinel = object()
    prior = {key: env.get(key, sentinel) for key in overrides}  # type: ignore[arg-type]
    apply_env_overrides(overrides, env=env)
    try:
        yield
    finally:
        for key, value in prior.items():
            if value is sentinel:
                env.pop(key, None)
            else:
                env[key] = value  # type: ignore[assignment]


# ── Per-run isolation layer (contextvars) ────────────────────────────────────
#
# ``scoped_environ`` restores prior state but still mutates the *process-global*
# ``os.environ`` inside the block, so two runs sharing one process still clobber
# each other's ``MINI_ORK_*``/``MO_*`` variables — the concurrency ceiling. This
# layer holds run-scoped overrides in a ``contextvars.ContextVar`` instead, which
# isolates per-OS-thread AND per-asyncio-task (a new Task runs in a copy of the
# context at creation), so concurrent runs never see one another's values.
#
# Migration is incremental: writers bind via ``run_context_scope`` and readers
# read via ``context_env``, which falls back to ``os.environ`` for any key not
# yet bound in the current context — so unmigrated readers/writers keep working.

_RUN_CONTEXT_VARS: contextvars.ContextVar[Mapping[str, str | None]] = (
    contextvars.ContextVar("mini_ork_run_context", default={})
)


@contextlib.contextmanager
def run_context_scope(overrides: Mapping[str, str | None]) -> Iterator[None]:
    """Bind run-scoped variables for the current execution context only.

    Unlike ``scoped_environ`` (which mutates the process-global ``os.environ``),
    this layers ``overrides`` onto a ``contextvars.ContextVar`` — so concurrent
    threads / asyncio tasks each keep their own values and never clobber one
    another. ``None`` masks a key: ``context_env`` then returns its ``default``
    rather than the process-env fallback (matching ``apply_env_overrides``'s
    "None means remove"). Nested scopes accumulate; on exit the prior binding is
    restored via the contextvar token.
    """
    merged: dict[str, str | None] = dict(_RUN_CONTEXT_VARS.get())
    merged.update(overrides)
    token = _RUN_CONTEXT_VARS.set(merged)
    try:
        yield
    finally:
        _RUN_CONTEXT_VARS.reset(token)


def context_env(key: str, default: str = "") -> str:
    """Read a run-scoped variable: contextvar binding first, else ``os.environ``.

    The read half of the isolation contract. A key bound in the current context
    via ``run_context_scope`` wins; a key bound to ``None`` is masked and returns
    ``default`` (it does NOT fall through to the process env, so a stale leaked
    value can't resurface). Any key not bound in the current context falls back
    to ``os.environ`` so unmigrated writers keep working during migration.
    """
    overrides = _RUN_CONTEXT_VARS.get()
    if key in overrides:
        value = overrides[key]
        return default if value is None else value
    return os.environ.get(key, default)
