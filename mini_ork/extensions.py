"""Extension registry and builder helpers for Python integrations."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .types import EdgeSpec, NodeSpec, RecipeSpec, TaskClassSpec, WorkflowSpec

VerifierFn = Callable[[Path, Path], bool]


@dataclass
class ExtensionRegistry:
    """In-memory registry for embedding mini-ork in a Python application."""

    recipes: dict[str, RecipeSpec] = field(default_factory=dict)
    verifiers: dict[str, VerifierFn] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)

    def recipe(self, spec: RecipeSpec) -> RecipeSpec:
        self.recipes[spec.name] = spec
        return spec

    def verifier(self, name: str) -> Callable[[VerifierFn], VerifierFn]:
        def decorator(fn: VerifierFn) -> VerifierFn:
            self.verifiers[name] = fn
            return fn

        return decorator


class RecipeBuilder:
    """Small Python DSL for building recipe specs without hand-writing YAML."""

    def __init__(self, name: str, task_class: str, description: str = "") -> None:
        self.name = name
        self.task_class = task_class
        self.description = description
        self._nodes: list[NodeSpec] = []
        self._edges: list[EdgeSpec] = []
        self._keywords: list[str] = []

    def keywords(self, *values: str) -> "RecipeBuilder":
        self._keywords.extend(values)
        return self

    def node(self, node: NodeSpec) -> "RecipeBuilder":
        self._nodes.append(node)
        return self

    def edge(self, from_node: str, to_node: str, edge_type: str = "depends_on") -> "RecipeBuilder":
        self._edges.append(EdgeSpec(from_node, to_node, edge_type))  # type: ignore[arg-type]
        return self

    def build(self) -> RecipeSpec:
        task = TaskClassSpec(
            name=self.task_class,
            description=self.description,
            keywords=tuple(self._keywords),
        )
        workflow = WorkflowSpec(
            task_class=self.task_class,
            description=self.description,
            nodes=tuple(self._nodes),
            edges=tuple(self._edges),
        )
        return RecipeSpec(name=self.name, task_class=task, workflow=workflow)
