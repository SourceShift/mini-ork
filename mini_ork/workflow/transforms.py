"""Deterministic artifact transforms registered independently of agent harnesses."""

from __future__ import annotations

import hashlib
import json
import re
import shutil
from collections.abc import Callable
from pathlib import Path

from mini_ork.gates.panel_bias import panel_anonymize

from .artifacts import ArtifactContractError, ArtifactLedger
from .compiler import CompiledWorkflow

TransformFn = Callable[[CompiledWorkflow, ArtifactLedger, str], Path]
_TRANSFORMS: dict[str, TransformFn] = {}


def register_transform(name: str) -> Callable[[TransformFn], TransformFn]:
    def decorator(fn: TransformFn) -> TransformFn:
        _TRANSFORMS[name] = fn
        return fn

    return decorator


def execute_transform(workflow: CompiledWorkflow, ledger: ArtifactLedger, node_id: str) -> Path:
    node = workflow.nodes.get(node_id)
    if node is None or node.type != "transform":
        raise ArtifactContractError(f"{node_id} is not a transform node")
    try:
        fn = _TRANSFORMS[node.transform]
    except KeyError as exc:
        raise ArtifactContractError(f"unknown artifact transform: {node.transform}") from exc
    return fn(workflow, ledger, node_id)


def _scrub_source_markers(text: str, reports: tuple[Path, ...]) -> str:
    """Remove source-family identifiers before an anonymous panel handoff.

    This is deliberately limited to the source marker encoded in each declared
    ``lens-<family>.md`` name. It prevents routine self-identification such as
    "GLM lens" or "from lens-glm" without pretending to solve arbitrary
    linguistic deanonymization. The original labels remain in the system-only
    receipt so operators can audit the transform without exposing them to the
    synthesizer.
    """
    markers = {
        report.stem.removeprefix("lens-")
        for report in reports
        if report.stem.startswith("lens-")
    }
    patterns = [re.escape(marker) for marker in sorted(markers, key=len, reverse=True) if marker]
    if not patterns:
        return text
    return re.sub(
        r"(?i)(?<![A-Za-z0-9])(?:" + "|".join(patterns) + r")(?![A-Za-z0-9])",
        "[redacted source]",
        text,
    )


@register_transform("panel.anonymize@v1")
def panel_anonymize_v1(workflow: CompiledWorkflow, ledger: ArtifactLedger, node_id: str) -> Path:
    """Materialize an anonymous markdown bundle and a system-only label map."""
    prepared = ledger.prepared_inputs(node_id)
    reports = prepared.paths.get("reports", ())
    if not reports:
        raise ArtifactContractError("panel.anonymize@v1 requires one or more reports inputs")
    if any(not path.name.startswith("lens-") or path.suffix != ".md" for path in reports):
        raise ArtifactContractError("panel.anonymize@v1 accepts only lens-*.md report artifacts")
    node = workflow.nodes[node_id]
    if "panel_responses" not in node.outputs or "label_map" not in node.outputs:
        raise ArtifactContractError(
            "panel.anonymize@v1 requires panel_responses and label_map outputs"
        )
    seed_material = "|".join(sorted(hashlib.sha256(path.read_bytes()).hexdigest() for path in reports))
    seed = int(hashlib.sha256(f"{ledger.run_id}|{node_id}|{seed_material}".encode()).hexdigest()[:8], 16)
    staging = ledger.workspace / "scratch" / f"{node_id}-{seed:08x}"
    sanitized_reports = staging / "reports"
    response_dir = staging / "responses"
    sanitized_reports.mkdir(parents=True, exist_ok=True)
    for report in reports:
        (sanitized_reports / report.name).write_text(
            _scrub_source_markers(report.read_text(encoding="utf-8", errors="replace"), reports),
            encoding="utf-8",
        )
    panel_anonymize(str(sanitized_reports), str(response_dir), seed=seed)

    bundle_path = ledger.output_path(workflow, node_id, "panel_responses")
    bundle_path.parent.mkdir(parents=True, exist_ok=True)
    sections = ["# Anonymous panel responses", ""]
    for response in sorted(response_dir.glob("resp-*.md")):
        label = response.stem.removeprefix("resp-")
        sections.extend([f"## Response {label}", "", response.read_text(encoding="utf-8", errors="replace").rstrip(), ""])
    bundle_path.write_text("\n".join(sections).rstrip() + "\n", encoding="utf-8")

    source_map = Path(f"{response_dir}.label_map.json")
    label_map_path = ledger.output_path(workflow, node_id, "label_map")
    label_map_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source_map, label_map_path)
    (staging / "transform-receipt.json").write_text(
        json.dumps({"transform": "panel.anonymize@v1", "seed": seed, "report_count": len(reports)}) + "\n",
        encoding="utf-8",
    )
    return bundle_path
