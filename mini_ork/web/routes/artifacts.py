"""Read surface over the run_artifacts trajectory store (migration 0047).

Complements run_detail's filesystem-scan artifact endpoints: those walk the
run dir; these read the DB registry (run_id + node_id + kind → rel_path) so a
run's trajectory files are queryable without scanning disk. Mounted at
/artifact-records (not /artifacts) so the SPA's filesystem-scan endpoints
keep their paths — first-registered-wins would otherwise shadow them.

Security: raw bytes are served only for rows whose rel_path honours the
portable-rel convention AND resolves under the run dir — resolve_artifact_abs
does NOT enforce the under-run-dir invariant itself, so the realpath check
lives here. A path-escape row is rejected 403; a missing row/file is 404.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Path as PathParam
from fastapi.responses import FileResponse

from ...dispatch.telemetry import resolve_artifact_abs
from ..db import StateDB
from ..deps import get_db, get_home
from ..repositories import ArtifactsRepository

router = APIRouter(prefix="/api/v1/task-runs", tags=["run-artifacts"])


@router.get("/{run_id}/artifact-records")
def list_run_artifacts(
    run_id: str = PathParam(...),
    kind: str | None = None,
    db: StateDB = Depends(get_db),
) -> list[dict[str, Any]]:
    """run_artifacts registry rows for a run (optional ?kind= filter)."""
    return ArtifactsRepository(db).list_artifacts(run_id, kind=kind)


def _run_dir(home: Path, run_id: str) -> Path:
    # run_id flows from a path segment straight to the filesystem — same
    # strict rejection as mini_ork.web.artifacts._validate_run_id.
    if not run_id or ".." in run_id or "/" in run_id or "\\" in run_id:
        raise HTTPException(status_code=403, detail=f"invalid run_id: {run_id!r}")
    return home / "runs" / run_id


@router.get("/{run_id}/artifact-records/{artifact_id}/raw")
def get_artifact_raw(
    run_id: str = PathParam(...),
    artifact_id: int = PathParam(...),
    db: StateDB = Depends(get_db),
    home: Path = Depends(get_home),
) -> FileResponse:
    """Serve the raw bytes of one registered artifact."""
    row = ArtifactsRepository(db).fetch_artifact(run_id, artifact_id)
    if not row:
        raise HTTPException(
            status_code=404, detail=f"artifact {artifact_id} not found for run {run_id}"
        )

    rel_path = str(row.get("rel_path") or "")
    # rel_path convention (db/migrations/0047): relative to the run dir, no
    # leading '/', no '..' component. A row violating it is a path-escape
    # attempt — reject before touching the filesystem.
    if rel_path.startswith("/") or ".." in rel_path.split("/"):
        raise HTTPException(status_code=403, detail=f"path escape: {rel_path}")

    run_dir = _run_dir(home, run_id)
    abs_path = resolve_artifact_abs(
        db.db_path,
        run_id,
        row.get("node_id"),
        str(row["kind"]),
        run_dir=run_dir,
    )
    if abs_path is None:
        raise HTTPException(status_code=404, detail=f"artifact file missing: {rel_path}")
    # Belt + braces: resolve_artifact_abs joins rel_path under the run dir but
    # does not itself verify the realpath stays under it.
    run_root = run_dir.resolve()
    if abs_path != run_root and run_root not in abs_path.parents:
        raise HTTPException(status_code=403, detail=f"path escape: {rel_path}")
    return FileResponse(abs_path)
