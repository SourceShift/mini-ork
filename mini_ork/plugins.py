"""Out-of-tree plugin discovery through standard Python entry points.

The Python runtime already exposes registration APIs
(:func:`mini_ork.cli.execute.register_node_handler`,
:func:`mini_ork.gates.gate_registry.register_gate_evaluator`,
:func:`mini_ork.dispatch.routing.register_policy`,
:func:`mini_ork.cli.main.register_subcommand`,
:func:`mini_ork.runtime.sandbox.register_workspace_backend`,
:func:`mini_ork.runtime.shared_drive.register_shared_drive_backend`) — but
there was no discovery path, so a separate package had to be imported
explicitly (or core had to be patched) to use them.

This module is that path. A distribution declares one or more entry points in
the group ``mini_ork.plugins``::

    [project.entry-points."mini_ork.plugins"]
    acme = "acme_mini_ork:register"

Each target is either:

* a callable taking one :class:`PluginContext` argument, or
* a module exposing ``register(context)``.

Discovery is **explicit**: importing :mod:`mini_ork` never scans the
environment or executes third-party code. The host (the CLI, a service, or an
embedding application) calls :func:`bootstrap` once at startup — it is the
idempotent, never-raising wrapper the CLI itself uses::

    from mini_ork.plugins import bootstrap
    report = bootstrap()
    if report is not None and not report.ok:
        log.warning("plugin failures: %s", report.failed)

:mod:`mini_ork.cli` calls it at *import*, not from ``main()``: every native
subcommand is a fresh ``python -m mini_ork.cli.<sub>`` process, so a bootstrap
reached only through the top-level dispatcher would leave children such as
``resume`` and ``recover`` with no plugin registered at all.

Everything a plugin can reach goes through :class:`PluginContext`, which
delegates to the public registries. That keeps the OSS core free of private
imports and gives a private distribution a supported extension seam instead
of a fork. See ``docs/OPENCORE.md`` for the boundary rationale.
"""
from __future__ import annotations

import importlib.metadata
import os
import sys
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Mapping, MutableMapping, Protocol


ENTRY_POINT_GROUP = "mini_ork.plugins"

__all__ = [
    "ENTRY_POINT_GROUP",
    "BOOTSTRAP_ENV",
    "LoadedPlugin",
    "PluginContext",
    "PluginError",
    "PluginFailure",
    "PluginReport",
    "bootstrap",
    "load_plugins",
    "load_entry_point",
    "enabled_plugins",
]


#: Process-wide record of plugins a host has loaded (name -> descriptor).
_LOADED: dict[str, "LoadedPlugin"] = {}


class PluginError(RuntimeError):
    """A plugin could not be loaded or does not honour the plugin contract."""


class _EntryPoint(Protocol):
    name: str
    value: str

    def load(self) -> Any: ...


# ---- report types ---------------------------------------------------------


@dataclass(frozen=True)
class LoadedPlugin:
    name: str
    module: str
    version: str = ""


@dataclass(frozen=True)
class PluginFailure:
    name: str
    error: str


@dataclass
class PluginReport:
    """Outcome of one discovery pass. Failures never abort the pass unless
    ``strict=True`` was requested."""

    loaded: list[LoadedPlugin] = field(default_factory=list)
    failed: list[PluginFailure] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.failed

    def to_dict(self) -> dict[str, Any]:
        return {
            "loaded": [
                {"name": p.name, "module": p.module, "version": p.version}
                for p in self.loaded
            ],
            "failed": [{"name": f.name, "error": f.error} for f in self.failed],
        }


# ---- the only surface a plugin may touch ---------------------------------


class PluginContext:
    """Registration facade handed to each plugin.

    Every method delegates to a public OSS registry and imports it lazily, so
    loading a plugin that only adds a subcommand does not pay for the whole
    verification stack. A plugin therefore never imports an internal module
    to register itself — the seam is this class.
    """

    def node_handler(
        self, node_type: str, handler: Callable, *, phase: str = "main"
    ) -> None:
        from mini_ork.cli.execute import register_node_handler

        register_node_handler(node_type, handler, phase=phase)

    def gate_evaluator(self, gate_type: str, evaluator: Callable) -> None:
        from mini_ork.gates.gate_registry import register_gate_evaluator

        register_gate_evaluator(gate_type, evaluator)

    def routing_policy(self, name: str, policy: Callable) -> None:
        from mini_ork.dispatch.routing import register_policy

        register_policy(name, policy)

    def subcommand(
        self, name: str, handler: Callable, *, registry: MutableMapping | None = None
    ) -> None:
        # An explicit registry lets an embedder keep its own command table
        # without importing (and mutating) the CLI's global one.
        if registry is not None:
            registry[name] = handler
            return
        from mini_ork.cli.main import register_subcommand

        register_subcommand(name, handler)

    def workspace_backend(self, name: str, factory: Callable) -> None:
        from mini_ork.runtime.sandbox import register_workspace_backend

        register_workspace_backend(name, factory)

    def shared_drive_backend(self, name: str, factory: Callable) -> None:
        from mini_ork.runtime.shared_drive import register_shared_drive_backend

        register_shared_drive_backend(name, factory)

    def budget_policy(self, name: str, policy: Callable) -> None:
        """Register a per-run budget policy selectable with ``MO_BUDGET_POLICY``.

        Without this method the secure tier of :mod:`mini_ork.cost.budget` was
        unreachable for an out-of-tree deployment: the registry existed but the
        plugin seam — the only supported way to run code in the host — could not
        reach it, so ``MO_BUDGET_POLICY=acme`` could only ever fail closed.
        """
        from mini_ork.cost.budget import register_budget_policy

        register_budget_policy(name, policy)


# ---- discovery ------------------------------------------------------------


def _iter_entry_points(group: str) -> Iterable[_EntryPoint]:
    """``importlib.metadata.entry_points`` across 3.11 and 3.12.

    3.12 deprecates the mapping form; both accept ``group=``, and the returned
    ``EntryPoints`` is iterable. Tests inject a plain list instead.
    """
    eps = importlib.metadata.entry_points()
    if hasattr(eps, "select"):
        return eps.select(group=group)
    return eps.get(group, [])  # type: ignore[union-attr]


def _version_of(module_name: str) -> str:
    """Best-effort distribution version for the plugin's top-level package."""
    top = module_name.split(".", 1)[0]
    try:
        return importlib.metadata.version(top)
    except (importlib.metadata.PackageNotFoundError, ValueError):
        return ""


def load_entry_point(
    entry_point: _EntryPoint, *, context: PluginContext | None = None
) -> LoadedPlugin:
    """Load and invoke one entry point.

    The target is called exactly once with the context. Anything else — a
    missing callable, a non-module target, an exception during registration —
    is a :class:`PluginError`; callers that want partial success use
    :func:`load_plugins`.
    """
    ctx = context or PluginContext()
    try:
        target = entry_point.load()
    except Exception as exc:  # noqa: BLE001 — surfaced to the operator
        raise PluginError(f"{entry_point.name}: import failed: {exc}") from exc

    register = target if callable(target) else getattr(target, "register", None)
    if not callable(register):
        raise PluginError(
            f"{entry_point.name}: entry point must resolve to a callable or a "
            "module with register(context)"
        )

    try:
        register(ctx)
    except Exception as exc:  # noqa: BLE001 — plugin bug, not a host crash
        raise PluginError(f"{entry_point.name}: registration failed: {exc}") from exc

    module = getattr(target, "__module__", None) or getattr(target, "__name__", "")
    return LoadedPlugin(
        name=entry_point.name,
        module=str(module),
        version=_version_of(str(module)),
    )


def load_plugins(
    *,
    group: str = ENTRY_POINT_GROUP,
    context: PluginContext | None = None,
    strict: bool = False,
    entry_points: Iterable[_EntryPoint] | None = None,
) -> PluginReport:
    """Discover and register every plugin in ``group``.

    Args:
      group: entry-point group; default ``mini_ork.plugins``.
      context: custom registration facade (tests, embedders).
      strict: raise :class:`PluginError` on the first failure instead of
        collecting it in :attr:`PluginReport.failed`.
      entry_points: inject an explicit iterable instead of scanning the
        environment. Tests use this; hosts normally do not.
    """
    eps = list(entry_points if entry_points is not None else _iter_entry_points(group))
    report = PluginReport()
    for ep in eps:
        try:
            loaded = load_entry_point(ep, context=context)
            report.loaded.append(loaded)
            _LOADED[loaded.name] = loaded
        except (PluginError, SystemExit) as exc:
            # ``SystemExit`` from a plugin's own ``sys.exit()`` is contained to
            # that plugin: aborting here would skip every entry point after it.
            if strict:
                raise
            report.failed.append(PluginFailure(name=ep.name, error=str(exc)))
    return report


def enabled_plugins() -> Mapping[str, LoadedPlugin]:
    """The plugin registry one process has loaded, keyed by entry-point name.

    Populated by :func:`load_plugins`; empty until a host opts in. Useful for
    ``mini-ork doctor``-style reporting without re-running discovery.
    """
    return dict(_LOADED)


#: Env var that opts a process out of discovery (``MINI_ORK_NO_PLUGINS=1``).
BOOTSTRAP_ENV = "MINI_ORK_NO_PLUGINS"

#: Whether :func:`bootstrap` has run in this process.
_BOOTSTRAPPED = False


def bootstrap(
    *,
    group: str = ENTRY_POINT_GROUP,
    context: PluginContext | None = None,
    entry_points: Iterable[_EntryPoint] | None = None,
) -> PluginReport | None:
    """Discover plugins once per process — the startup seam every host runs.

    Called at import of :mod:`mini_ork.cli`, because a native subcommand is a
    *fresh interpreter*: ``mini-ork resume`` execs
    ``python -m mini_ork.cli.resume``, whose ``main()`` never passes through
    :func:`mini_ork.cli.main.main`. Discovery therefore belongs somewhere every
    child shares, or a run whose budget policy comes from a plugin could be
    given a refusal whose own remedy names a command that cannot see the
    plugin.

    Never raises: a broken plugin is reported and skipped, and a metadata scan
    that fails outright is reported the same way. Executing optional
    third-party code must never turn ``mini-ork help`` into a traceback.
    ``MINI_ORK_NO_PLUGINS=1`` opts a process out; being an environment knob it
    must be set before the process starts (a value a profile writes into the
    environment is too late for a bootstrap that runs at import).

    Returns the report, or ``None`` when discovery was skipped (already done,
    or opted out).
    """
    global _BOOTSTRAPPED
    if _BOOTSTRAPPED:
        return None
    _BOOTSTRAPPED = True
    if os.environ.get(BOOTSTRAP_ENV, "").strip() not in ("", "0"):
        return None
    try:
        report = load_plugins(group=group, context=context,
                              entry_points=entry_points)
    except (SystemExit, Exception) as exc:  # noqa: BLE001 — must not break the CLI
        # ``SystemExit`` included: a plugin that calls ``sys.exit()`` while
        # registering would otherwise terminate the interpreter mid-import,
        # i.e. break every command instead of warning about that one package.
        # ``KeyboardInterrupt`` is deliberately NOT caught — Ctrl-C is the user.
        sys.stderr.write(f"warning: plugin discovery failed: {exc}\n")
        return None
    for failure in report.failed:
        sys.stderr.write(f"warning: plugin {failure.name}: {failure.error}\n")
    return report
