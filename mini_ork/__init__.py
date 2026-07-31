"""Python framework facade for mini-ork.

Two public layers:

* **Primitives** — in-process building blocks with no YAML and no subprocess:
  execution-anchored verification (``Crucible``), heterogeneous model dispatch
  (``dispatch_model``), verified-outcome memory (``memory``), and cost-free
  bandit routing (``router`` / ``preferred_lane``). These run in your process
  and return typed objects, so any application can embed them directly::

      from mini_ork import Crucible, dispatch_model, memory, router

* **Orchestrator** — the full classify -> plan -> execute -> verify lifecycle via
  the ``MiniOrk`` client and the typed spec/request objects. It shells out to
  the ``mini-ork`` CLI behind a stable ``--json`` result contract.

The heavy primitive modules are imported lazily (PEP 562 ``__getattr__``) so
``import mini_ork`` stays cheap and free of import cycles — you only pay for
the dispatch/runtime/memory machinery when you actually reference it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .client import MiniOrk, MiniOrkError
from .extensions import ExtensionRegistry, RecipeBuilder
from .types import (
    EdgeSpec,
    NodeSpec,
    ProviderPolicy,
    RecipeSpec,
    RunEvent,
    RunRequest,
    RunResult,
    SpawnRequest,
    SpawnResult,
    TaskClassSpec,
    WorkflowSpec,
)

# Lazily-loaded primitives. Mapping is ``public name -> (module, attribute)``;
# the import happens on first attribute access, never at package import time.
_LAZY_ATTRS: dict[str, tuple[str, str]] = {
    "Crucible": ("mini_ork.runtime.engine", "Crucible"),
    "RuntimeSpec": ("mini_ork.runtime.engine", "RuntimeSpec"),
    "ExecOutcome": ("mini_ork.runtime.engine", "ExecOutcome"),
    "available_backends": ("mini_ork.runtime.engine", "available_backends"),
    "dispatch_model": ("mini_ork.dispatch", "dispatch_model"),
    "DispatchRequest": ("mini_ork.dispatch", "DispatchRequest"),
    "DispatchResult": ("mini_ork.dispatch", "DispatchResult"),
    "preferred_lane": ("mini_ork.lane_router", "preferred_lane"),
    "recompute_advantages": ("mini_ork.lane_router", "recompute_advantages"),
}

# Lazily-loaded submodule handles: ``mini_ork.memory`` / ``mini_ork.router``.
_LAZY_MODULES: dict[str, str] = {
    "memory": "mini_ork.memory",
    "router": "mini_ork.lane_router",
}

if TYPE_CHECKING:  # static-analysis / IDE resolution only — no runtime cost
    from . import lane_router as router  # noqa: F401
    from . import memory  # noqa: F401
    from .dispatch import DispatchRequest, DispatchResult, dispatch_model  # noqa: F401
    from .lane_router import preferred_lane, recompute_advantages  # noqa: F401
    from .runtime.engine import (  # noqa: F401
        Crucible,
        ExecOutcome,
        RuntimeSpec,
        available_backends,
    )


def __getattr__(name: str) -> object:
    import importlib

    target = _LAZY_ATTRS.get(name)
    if target is not None:
        module, attr = target
        return getattr(importlib.import_module(module), attr)
    module_path = _LAZY_MODULES.get(name)
    if module_path is not None:
        return importlib.import_module(module_path)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    return sorted(__all__)


__all__ = [
    # ── orchestrator facade ──
    "MiniOrk",
    "MiniOrkError",
    "RecipeBuilder",
    "ExtensionRegistry",
    "EdgeSpec",
    "NodeSpec",
    "ProviderPolicy",
    "RecipeSpec",
    "RunEvent",
    "RunRequest",
    "RunResult",
    "SpawnRequest",
    "SpawnResult",
    "TaskClassSpec",
    "WorkflowSpec",
    # ── primitives (lazy) ──
    "Crucible",
    "RuntimeSpec",
    "ExecOutcome",
    "available_backends",
    "dispatch_model",
    "DispatchRequest",
    "DispatchResult",
    "preferred_lane",
    "recompute_advantages",
    "memory",
    "router",
]
