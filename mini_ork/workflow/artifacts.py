"""Run-local artifact manifests and scoped consumer input materialization."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .compiler import CompiledWorkflow


class ArtifactContractError(RuntimeError):
    """Raised when a declared artifact is missing, corrupt, or not visible."""


@dataclass(frozen=True)
class ArtifactRef:
    artifact_id: str
    name: str
    kind: str
    rel_path: str
    sha256: str
    bytes: int
    visibility: str


@dataclass(frozen=True)
class PreparedInputs:
    manifest_path: Path
    input_root: Path
    paths: dict[str, tuple[Path, ...]]


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_json_atomically(target: Path, payload: dict[str, Any]) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_suffix(target.suffix + ".tmp")
    temp.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    os.replace(temp, target)


class ArtifactLedger:
    """Filesystem-backed semantic ledger for one run.

    ``run_artifacts`` remains call telemetry. This ledger records only declared
    recipe outputs and the exact copies a consumer is allowed to receive.
    """

    def __init__(self, run_dir: str | Path, run_id: str) -> None:
        self.run_dir = Path(run_dir).resolve()
        self.run_id = run_id or self.run_dir.name
        self.workspace = self.run_dir / "workspace"
        self.manifest_dir = self.workspace / "manifests"
        self.inputs_root = self.workspace / "inputs"
        self._prepared: dict[str, PreparedInputs] = {}

    def _resolve_rel(self, rel_path: str) -> Path:
        candidate = (self.run_dir / rel_path).resolve()
        if candidate != self.run_dir and self.run_dir not in candidate.parents:
            raise ArtifactContractError(f"artifact path escapes run directory: {rel_path}")
        return candidate

    def _manifest_path(self, node_id: str) -> Path:
        return self.manifest_dir / f"{node_id}.outputs.json"

    def output_path(self, workflow: "CompiledWorkflow", node_id: str, output_name: str) -> Path:
        try:
            output = workflow.nodes[node_id].outputs[output_name]
        except KeyError as exc:
            raise ArtifactContractError(f"unknown declared artifact {node_id}.{output_name}") from exc
        return self._resolve_rel(output.path)

    def publish_node_outputs(self, workflow: "CompiledWorkflow", node_id: str) -> tuple[ArtifactRef, ...]:
        node = workflow.nodes.get(node_id)
        if node is None or not node.outputs:
            return ()
        produced: list[ArtifactRef] = []
        for output in node.outputs.values():
            path = self._resolve_rel(output.path)
            if not path.is_file() or path.stat().st_size <= 0:
                raise ArtifactContractError(
                    f"declared artifact {node_id}.{output.name} is missing or empty: {output.path}"
                )
            digest = _sha256(path)
            produced.append(
                ArtifactRef(
                    artifact_id=f"{self.run_id}:{node_id}:{output.name}:{digest[:16]}",
                    name=output.name,
                    kind=output.kind,
                    rel_path=output.path,
                    sha256=digest,
                    bytes=path.stat().st_size,
                    visibility=output.visibility,
                )
            )
        payload = {
            "node_id": node_id,
            "run_id": self.run_id,
            "produced": [artifact.__dict__ for artifact in produced],
        }
        _write_json_atomically(self._manifest_path(node_id), payload)
        return tuple(produced)

    def _load_output(self, node_id: str, output_name: str) -> ArtifactRef:
        path = self._manifest_path(node_id)
        if not path.is_file():
            raise ArtifactContractError(f"producer {node_id} has not published an output manifest")
        try:
            payload: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
            raw = next(item for item in payload.get("produced", []) if item.get("name") == output_name)
            artifact = ArtifactRef(**raw)
        except (OSError, ValueError, StopIteration, TypeError) as exc:
            raise ArtifactContractError(f"invalid output manifest for {node_id}.{output_name}") from exc
        actual = self._resolve_rel(artifact.rel_path)
        if not actual.is_file() or actual.stat().st_size != artifact.bytes or _sha256(actual) != artifact.sha256:
            raise ArtifactContractError(f"artifact integrity check failed for {node_id}.{output_name}")
        return artifact

    def prepare_inputs(self, workflow: "CompiledWorkflow", node_id: str) -> PreparedInputs:
        node = workflow.nodes.get(node_id)
        if node is None:
            raise ArtifactContractError(f"unknown workflow node: {node_id}")
        input_root = self.inputs_root / node_id
        paths: dict[str, list[Path]] = {name: [] for name in node.inputs}
        records: list[dict[str, Any]] = []
        for binding in workflow.bindings_for(node_id):
            artifact = self._load_output(binding.producer_node, binding.producer_output)
            if artifact.visibility == "system_only" and node.type != "transform":
                raise ArtifactContractError(
                    f"system-only artifact {binding.producer_node}.{binding.producer_output} cannot be materialized"
                )
            source = self._resolve_rel(artifact.rel_path)
            destination_dir = input_root / binding.consumer_input
            destination_dir.mkdir(parents=True, exist_ok=True)
            destination = destination_dir / source.name
            suffix = 1
            while destination.exists() and _sha256(destination) != artifact.sha256:
                suffix += 1
                destination = destination_dir / f"{suffix}-{source.name}"
            if not destination.exists():
                shutil.copy2(source, destination)
            paths[binding.consumer_input].append(destination)
            records.append(
                {
                    "name": binding.consumer_input,
                    "kind": artifact.kind,
                    "path": str(destination.relative_to(input_root)),
                    "sha256": artifact.sha256,
                    "bytes": artifact.bytes,
                }
            )
        for input_spec in node.inputs.values():
            if input_spec.required and not paths[input_spec.name]:
                raise ArtifactContractError(f"required input {node_id}.{input_spec.name} is unavailable")
        manifest_path = self.manifest_dir / f"{node_id}.inputs.json"
        _write_json_atomically(
            manifest_path,
            {"node_id": node_id, "inputs": records},
        )
        prepared = PreparedInputs(
            manifest_path=manifest_path,
            input_root=input_root,
            paths={name: tuple(value) for name, value in paths.items()},
        )
        self._prepared[node_id] = prepared
        return prepared

    def prepared_inputs(self, node_id: str) -> PreparedInputs:
        try:
            return self._prepared[node_id]
        except KeyError as exc:
            raise ArtifactContractError(f"inputs for {node_id} were not prepared") from exc

    @staticmethod
    def prompt_context(prepared: PreparedInputs) -> str:
        if not any(prepared.paths.values()):
            return ""
        lines = [
            "\n--- Declared artifact inputs ---",
            "Read only the files listed in the node input manifest. Do not infer or scan other run artifacts.",
            f"Input manifest: {prepared.manifest_path}",
            f"Input root: {prepared.input_root}",
        ]
        for name, paths in sorted(prepared.paths.items()):
            for path in paths:
                lines.append(f"- {name}: {path}")
        lines.append("--- /declared artifact inputs ---\n")
        return "\n".join(lines)
