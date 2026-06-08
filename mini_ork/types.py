"""Typed public contracts for mini-ork Python integrations."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

NodeType = Literal[
    "planner",
    "researcher",
    "implementer",
    "reviewer",
    "verifier",
    "reflector",
    "publisher",
    "rollback",
]

DispatchMode = Literal["serial", "parallel", "partitioned", "speculative"]
EdgeType = Literal["depends_on", "supplies_context_to", "verifies", "blocks", "retries", "escalates_to"]
RunMode = Literal["dry-run", "live"]


@dataclass(frozen=True)
class NodeSpec:
    name: str
    type: NodeType
    model_lane: str | None = None
    prompt_ref: str | None = None
    verifier_ref: str | None = None
    dispatch_mode: DispatchMode = "serial"
    gates: tuple[str, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "name": self.name,
            "type": self.type,
            "dispatch_mode": self.dispatch_mode,
        }
        for key in ("model_lane", "prompt_ref", "verifier_ref"):
            value = getattr(self, key)
            if value:
                data[key] = value
        if self.gates:
            data["gates"] = list(self.gates)
        data.update(self.metadata)
        return data


@dataclass(frozen=True)
class EdgeSpec:
    from_node: str
    to_node: str
    edge_type: EdgeType = "depends_on"

    def to_dict(self) -> dict[str, str]:
        return {"from": self.from_node, "to": self.to_node, "edge_type": self.edge_type}


@dataclass(frozen=True)
class WorkflowSpec:
    task_class: str
    nodes: tuple[NodeSpec, ...]
    edges: tuple[EdgeSpec, ...] = ()
    version: str = "0.1.0"
    description: str = ""
    rollback_strategy: str | None = None
    max_self_correction_iterations: int | None = None

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "version": self.version,
            "task_class": self.task_class,
            "description": self.description,
            "nodes": [node.to_dict() for node in self.nodes],
            "edges": [edge.to_dict() for edge in self.edges],
        }
        if self.rollback_strategy:
            data["rollback_strategy"] = self.rollback_strategy
        if self.max_self_correction_iterations is not None:
            data["max_self_correction_iterations"] = self.max_self_correction_iterations
        return data


@dataclass(frozen=True)
class TaskClassSpec:
    name: str
    description: str = ""
    keywords: tuple[str, ...] = ()
    default_workflow: str = "workflow.yaml"
    risk_class: str = "medium"

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "keywords": list(self.keywords),
            "default_workflow": self.default_workflow,
            "risk_class": self.risk_class,
        }


@dataclass(frozen=True)
class RecipeSpec:
    name: str
    task_class: TaskClassSpec
    workflow: WorkflowSpec
    root: Path | None = None


@dataclass(frozen=True)
class ProviderPolicy:
    lanes: dict[str, str]
    per_epic_usd: float = 5.0
    per_run_usd: float = 0.5
    daily_cap_usd: float = 50.0

    @classmethod
    def codex_only(cls) -> "ProviderPolicy":
        lanes = {
            lane: "codex"
            for lane in (
                "planner",
                "researcher",
                "implementer",
                "worker",
                "reviewer",
                "verifier",
                "reflector",
                "publisher",
                "rollback",
                "decomposer",
                "spec_author",
                "spec_reviewer",
                "bdd_runner",
                "healer",
                "brain",
                "worker_default",
                "reviewer_default",
                "glm_lens",
                "kimi_lens",
                "codex_lens",
                "opus_lens",
                "minimax_lens",
            )
        }
        return cls(lanes=lanes)


@dataclass(frozen=True)
class RunRequest:
    kickoff: Path
    recipe: str | None = None
    mode: RunMode = "dry-run"
    cwd: Path | None = None
    provider_policy: ProviderPolicy | None = None
    auto_init: bool = True
    timeout_seconds: int = 1800
    extra_env: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class RunEvent:
    line: str
    stream: Literal["stdout", "stderr", "combined"] = "combined"


@dataclass(frozen=True)
class RunResult:
    returncode: int
    command: tuple[str, ...]
    cwd: Path
    output: str
    events: tuple[RunEvent, ...]
    run_id: str = ""
    task_class: str = ""
    plan_path: Path | None = None
    verdict: str = ""
    retained_home: Path | None = None
    init_ran: bool = False
    init_output: str = ""

    @property
    def ok(self) -> bool:
        return self.returncode == 0
