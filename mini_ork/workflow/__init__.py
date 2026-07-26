"""Compiled workflow and artifact-flow contracts.

This package is the data plane between recipe YAML and the existing executor.
Legacy recipes keep using their filename conventions; recipes that declare
ports gain deterministic graph ordering, durable manifests, and scoped inputs.
"""

from .artifacts import ArtifactContractError, ArtifactLedger, ArtifactRef, PreparedInputs
from .compiler import (
    ArtifactBinding,
    ArtifactInput,
    ArtifactOutput,
    CompiledWorkflow,
    WorkflowCompileError,
    WorkflowNode,
    compile_workflow,
)

__all__ = [
    "ArtifactBinding",
    "ArtifactContractError",
    "ArtifactInput",
    "ArtifactLedger",
    "ArtifactOutput",
    "ArtifactRef",
    "CompiledWorkflow",
    "PreparedInputs",
    "WorkflowCompileError",
    "WorkflowNode",
    "compile_workflow",
]
