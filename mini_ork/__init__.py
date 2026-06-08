"""Python framework facade for mini-ork.

The Python API is intentionally a facade over the existing mini-ork runtime:
it gives integrators typed objects, extension hooks, and transparent run
artifacts while preserving compatibility with the CLI/Bash engine.
"""

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

__all__ = [
    "EdgeSpec",
    "ExtensionRegistry",
    "MiniOrk",
    "MiniOrkError",
    "NodeSpec",
    "ProviderPolicy",
    "RecipeBuilder",
    "RecipeSpec",
    "RunEvent",
    "RunRequest",
    "RunResult",
    "SpawnRequest",
    "SpawnResult",
    "TaskClassSpec",
    "WorkflowSpec",
]
