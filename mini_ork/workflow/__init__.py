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
from .store import (
    ArtifactStore,
    ArtifactStoreError,
    LocalArtifactStore,
    make_artifact_store,
    register_artifact_backend,
    resolve_run_root,
)

__all__ = [
    "ArtifactBinding",
    "ArtifactContractError",
    "ArtifactInput",
    "ArtifactLedger",
    "ArtifactOutput",
    "ArtifactRef",
    "ArtifactStore",
    "ArtifactStoreError",
    "CompiledWorkflow",
    "LocalArtifactStore",
    "PreparedInputs",
    "WorkflowCompileError",
    "WorkflowNode",
    "compile_workflow",
    "make_artifact_store",
    "register_artifact_backend",
    "resolve_run_root",
]
