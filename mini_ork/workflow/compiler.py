"""Compile recipe YAML into a deterministic execution and artifact graph.

The historical executor accepts a flat list of node tuples.  This module keeps
that compatibility surface while making dependencies and artifact handoffs
explicit for recipes that opt into ``inputs`` / ``outputs`` declarations.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Mapping

import yaml


class WorkflowCompileError(ValueError):
    """Raised when a workflow cannot be executed safely as an artifact graph."""


@dataclass(frozen=True)
class ArtifactOutput:
    name: str
    kind: str
    path: str
    visibility: str = "consumer"


@dataclass(frozen=True)
class ArtifactInput:
    name: str
    required: bool = True
    many: bool = False


@dataclass(frozen=True)
class WorkflowNode:
    name: str
    type: str
    description: str = ""
    prompt_ref: str = ""
    verifier_ref: str = ""
    model_lane: str = ""
    dispatch_mode: str = "serial"
    requires_capabilities: tuple[str, ...] = ()
    inputs: dict[str, ArtifactInput] = field(default_factory=dict)
    outputs: dict[str, ArtifactOutput] = field(default_factory=dict)
    transform: str = ""

    def dispatch_fields(self, separator: str) -> str:
        requires = ",".join(self.requires_capabilities)
        values = (
            self.name,
            self.type,
            self.description or self.name,
            self.prompt_ref,
            self.dispatch_mode,
            self.verifier_ref,
            self.model_lane or self.type,
            requires,
        )
        return separator.join(value.replace(separator, " ") for value in values)


@dataclass(frozen=True)
class ArtifactBinding:
    producer_node: str
    producer_output: str
    consumer_node: str
    consumer_input: str


@dataclass(frozen=True)
class CompiledWorkflow:
    path: Path
    nodes: dict[str, WorkflowNode]
    declared_order: tuple[str, ...]
    topological_order: tuple[str, ...]
    bindings: tuple[ArtifactBinding, ...]
    control_parents: dict[str, tuple[str, ...]]

    def node_fields(self, separator: str) -> list[str]:
        return [self.nodes[node_id].dispatch_fields(separator) for node_id in self.topological_order]

    def bindings_for(self, node_id: str) -> tuple[ArtifactBinding, ...]:
        return tuple(binding for binding in self.bindings if binding.consumer_node == node_id)


def _safe_relative_path(value: str, *, context: str) -> str:
    path = PurePosixPath(value)
    if not value or path.is_absolute() or ".." in path.parts:
        raise WorkflowCompileError(f"{context} must be a non-empty path relative to the run directory")
    return path.as_posix()


def _mapping(value: Any, *, context: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise WorkflowCompileError(f"{context} must be an object")
    return value


def _parse_inputs(raw: Any, *, node_id: str) -> dict[str, ArtifactInput]:
    if raw is None:
        return {}
    if not isinstance(raw, Mapping):
        raise WorkflowCompileError(f"node {node_id} inputs must be an object keyed by input name")
    parsed: dict[str, ArtifactInput] = {}
    for name, config in raw.items():
        if not isinstance(name, str) or not name:
            raise WorkflowCompileError(f"node {node_id} has an invalid input name")
        details = config if isinstance(config, Mapping) else {}
        parsed[name] = ArtifactInput(
            name=name,
            required=bool(details.get("required", True)),
            many=bool(details.get("many", False)),
        )
    return parsed


def _parse_outputs(raw: Any, *, node_id: str) -> dict[str, ArtifactOutput]:
    if raw is None:
        return {}
    entries: list[Mapping[str, Any]] = []
    if isinstance(raw, list):
        entries = [_mapping(value, context=f"node {node_id} output") for value in raw]
    elif isinstance(raw, Mapping):
        for name, config in raw.items():
            details = dict(_mapping(config, context=f"node {node_id} output {name}"))
            details.setdefault("name", name)
            entries.append(details)
    else:
        raise WorkflowCompileError(f"node {node_id} outputs must be a list or object")

    parsed: dict[str, ArtifactOutput] = {}
    for details in entries:
        name = str(details.get("name") or "").strip()
        if not name or name in parsed:
            raise WorkflowCompileError(f"node {node_id} has a missing or duplicate output name")
        visibility = str(details.get("visibility") or "consumer")
        if visibility not in {"consumer", "system_only"}:
            raise WorkflowCompileError(
                f"node {node_id} output {name} has unsupported visibility {visibility!r}"
            )
        parsed[name] = ArtifactOutput(
            name=name,
            kind=str(details.get("kind") or "file"),
            path=_safe_relative_path(str(details.get("path") or ""), context=f"node {node_id} output {name}"),
            visibility=visibility,
        )
    return parsed


def _node_from_yaml(raw: Any) -> WorkflowNode:
    details = _mapping(raw, context="workflow node")
    name = str(details.get("name") or "").strip()
    node_type = str(details.get("type") or "").strip()
    if not name or not node_type:
        raise WorkflowCompileError("every workflow node requires name and type")
    requires = details.get("requires_capabilities") or []
    if isinstance(requires, str):
        capabilities = tuple(part.strip() for part in requires.split(",") if part.strip())
    elif isinstance(requires, list):
        capabilities = tuple(str(part).strip() for part in requires if str(part).strip())
    else:
        raise WorkflowCompileError(f"node {name} requires_capabilities must be a string or list")
    transform = str(details.get("transform") or "").strip()
    if node_type == "transform" and not transform:
        raise WorkflowCompileError(f"transform node {name} requires a transform identifier")
    if transform and node_type != "transform":
        raise WorkflowCompileError(f"node {name} declares transform but type is {node_type!r}")
    inputs = _parse_inputs(details.get("inputs"), node_id=name)
    outputs = _parse_outputs(details.get("outputs"), node_id=name)
    if outputs and node_type in {"planner", "reflector", "rollback"}:
        raise WorkflowCompileError(
            f"node {name} type {node_type!r} cannot declare outputs because its handler does not publish artifacts"
        )
    return WorkflowNode(
        name=name,
        type=node_type,
        description=str(details.get("description") or ""),
        prompt_ref=str(details.get("prompt_ref") or ""),
        verifier_ref=str(details.get("verifier_ref") or ""),
        model_lane=str(details.get("model_lane") or ""),
        dispatch_mode=str(details.get("dispatch_mode") or "serial"),
        requires_capabilities=capabilities,
        inputs=inputs,
        outputs=outputs,
        transform=transform,
    )


def _stable_topological_order(
    node_ids: tuple[str, ...], parents: dict[str, set[str]]
) -> tuple[str, ...]:
    order_index = {node_id: index for index, node_id in enumerate(node_ids)}
    children: dict[str, set[str]] = {node_id: set() for node_id in node_ids}
    indegree = {node_id: len(parents[node_id]) for node_id in node_ids}
    for child, parent_ids in parents.items():
        for parent in parent_ids:
            children[parent].add(child)
    ready = [node_id for node_id in node_ids if indegree[node_id] == 0]
    ordered: list[str] = []
    while ready:
        ready.sort(key=order_index.__getitem__)
        current = ready.pop(0)
        ordered.append(current)
        for child in children[current]:
            indegree[child] -= 1
            if indegree[child] == 0:
                ready.append(child)
    if len(ordered) != len(node_ids):
        cycle = sorted(node_id for node_id, degree in indegree.items() if degree > 0)
        raise WorkflowCompileError(f"workflow contains a control/data cycle: {', '.join(cycle)}")
    return tuple(ordered)


def _validate_output_ownership(nodes: Mapping[str, WorkflowNode]) -> None:
    """Reserve each declared output path for one producer in one workflow.

    Ledger manifests and materialized inputs are runtime-owned files, not recipe
    outputs. Allowing a node to claim either namespace would let a valid recipe
    overwrite the metadata that enforces the artifact contract.
    """
    owners: dict[str, str] = {}
    reserved_prefixes = (("workspace", "manifests"), ("workspace", "inputs"))
    for node in nodes.values():
        for output in node.outputs.values():
            parts = PurePosixPath(output.path).parts
            if any(parts[: len(prefix)] == prefix for prefix in reserved_prefixes):
                raise WorkflowCompileError(
                    f"node {node.name} output {output.name} uses reserved ledger path: {output.path}"
                )
            owner = owners.get(output.path)
            if owner is not None:
                raise WorkflowCompileError(
                    f"output path {output.path} is declared by both {owner} and {node.name}"
                )
            owners[output.path] = node.name


def compile_workflow(path: str | Path) -> CompiledWorkflow:
    """Compile one recipe workflow, preserving legacy declarations.

    A legacy edge still contributes ordering. A data edge becomes a real artifact
    binding only when it names ``from_output`` and ``to_input``; this keeps old
    recipes runnable while preventing a filename guess from becoming a new API.
    """
    workflow_path = Path(path)
    if not workflow_path.is_file():
        raise WorkflowCompileError(f"workflow not found: {workflow_path}")
    try:
        document = yaml.safe_load(workflow_path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise WorkflowCompileError(f"invalid workflow YAML {workflow_path}: {exc}") from exc
    if not isinstance(document, Mapping):
        raise WorkflowCompileError("workflow root must be an object")
    raw_nodes = document.get("nodes") or []
    if not isinstance(raw_nodes, list):
        raise WorkflowCompileError("workflow nodes must be a list")

    nodes: dict[str, WorkflowNode] = {}
    declared: list[str] = []
    for raw_node in raw_nodes:
        node = _node_from_yaml(raw_node)
        if node.name in nodes:
            raise WorkflowCompileError(f"duplicate workflow node: {node.name}")
        nodes[node.name] = node
        declared.append(node.name)

    _validate_output_ownership(nodes)
    parents: dict[str, set[str]] = {node_id: set() for node_id in declared}
    bindings: list[ArtifactBinding] = []
    raw_edges = document.get("edges") or []
    if not isinstance(raw_edges, list):
        raise WorkflowCompileError("workflow edges must be a list")
    for raw_edge in raw_edges:
        edge = _mapping(raw_edge, context="workflow edge")
        source = str(edge.get("from") or "").strip()
        target = str(edge.get("to") or "").strip()
        if source not in nodes or target not in nodes:
            raise WorkflowCompileError(f"workflow edge references unknown node: {source} -> {target}")
        edge_type = str(edge.get("edge_type") or "depends_on")
        recursive = bool(edge.get("recursive", False))
        if not recursive and edge_type not in {"escalates_to", "retries"}:
            parents[target].add(source)

        source_output = str(edge.get("from_output") or "").strip()
        target_input = str(edge.get("to_input") or "").strip()
        if bool(source_output) != bool(target_input):
            raise WorkflowCompileError(
                f"artifact edge {source} -> {target} must declare both from_output and to_input"
            )
        if not source_output:
            continue
        output = nodes[source].outputs.get(source_output)
        input_spec = nodes[target].inputs.get(target_input)
        if output is None:
            raise WorkflowCompileError(f"artifact edge references undeclared output {source}.{source_output}")
        if input_spec is None:
            raise WorkflowCompileError(f"artifact edge references undeclared input {target}.{target_input}")
        if output.visibility == "system_only" and nodes[target].type != "transform":
            raise WorkflowCompileError(
                f"system-only artifact {source}.{source_output} cannot be exposed to node {target}"
            )
        prior = [binding for binding in bindings if binding.consumer_node == target and binding.consumer_input == target_input]
        if prior and not input_spec.many:
            raise WorkflowCompileError(f"input {target}.{target_input} accepts one artifact but has multiple bindings")
        bindings.append(ArtifactBinding(source, source_output, target, target_input))

    for node in nodes.values():
        for input_spec in node.inputs.values():
            has_binding = any(
                binding.consumer_node == node.name and binding.consumer_input == input_spec.name
                for binding in bindings
            )
            if input_spec.required and not has_binding:
                raise WorkflowCompileError(f"required input {node.name}.{input_spec.name} has no artifact binding")

    declared_tuple = tuple(declared)
    return CompiledWorkflow(
        path=workflow_path,
        nodes=nodes,
        declared_order=declared_tuple,
        topological_order=_stable_topological_order(declared_tuple, parents),
        bindings=tuple(bindings),
        control_parents={node_id: tuple(sorted(parent_ids, key=declared.index)) for node_id, parent_ids in parents.items()},
    )
