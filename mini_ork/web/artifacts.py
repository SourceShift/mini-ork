"""Filesystem artifact walker for .mini-ork/runs/<run-id>/.

The DB tells you a run happened; the filesystem holds what was actually
produced (lens-*.md, synthesis.md, plan.json, verifier logs). Both must
be exposed.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

# Safety: the artifact endpoint serves files; we constrain reads to under
# the runs root. Any path that resolves outside is rejected.

MAX_BYTES = 2 * 1024 * 1024  # 2 MiB per file; UI shouldn't load larger blobs

# run_id flows from path params straight to the filesystem. Reject anything
# that could escape runs_root: `..`, slashes, leading dot, control chars.
# Real run ids are `run-<unix>-<rand>` or `self-improve-iter-N-<ts>`, so the
# allowed alphabet is intentionally tight.
_RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def _validate_run_id(run_id: str) -> None:
    if not run_id or ".." in run_id or "/" in run_id or "\\" in run_id:
        raise PermissionError(f"invalid run_id: {run_id!r}")
    if not _RUN_ID_RE.match(run_id):
        raise PermissionError(f"invalid run_id: {run_id!r}")


def runs_root(home: Path) -> Path:
    return home / "runs"


def list_run_dirs(home: Path) -> list[dict[str, Any]]:
    root = runs_root(home)
    if not root.exists():
        return []
    out: list[dict[str, Any]] = []
    for p in sorted(root.iterdir(), reverse=True):
        if not p.is_dir():
            continue
        try:
            st = p.stat()
        except OSError:
            continue
        out.append(
            {
                "id": p.name,
                "path": str(p.relative_to(home)),
                "mtime": int(st.st_mtime),
                "file_count": sum(1 for _ in p.iterdir()),
            }
        )
    return out


def list_artifacts(home: Path, run_id: str) -> list[dict[str, Any]]:
    _validate_run_id(run_id)
    root = runs_root(home) / run_id
    if not root.exists() or not root.is_dir():
        return []
    items: list[dict[str, Any]] = []
    for fp in sorted(root.rglob("*")):
        if not fp.is_file():
            continue
        try:
            st = fp.stat()
        except OSError:
            continue
        items.append(
            {
                "name": fp.name,
                "relpath": str(fp.relative_to(root)),
                "size": st.st_size,
                "mtime": int(st.st_mtime),
                "kind": _classify(fp.name),
            }
        )
    return items


def read_artifact(home: Path, run_id: str, relpath: str) -> dict[str, Any]:
    _validate_run_id(run_id)
    runs = runs_root(home).resolve()
    base = (runs / run_id).resolve()
    # Belt + braces: even after id validation, refuse if the resolved base
    # is not a direct child of runs_root (catches future allow-list bugs).
    if base.parent != runs:
        raise PermissionError(f"invalid run_id: {run_id!r}")
    target = (base / relpath).resolve()
    if base not in target.parents and target != base:
        raise PermissionError(f"path escape: {relpath}")
    if not target.exists() or not target.is_file():
        raise FileNotFoundError(relpath)
    size = target.stat().st_size
    if size > MAX_BYTES:
        kind = _classify(target.name)
        if kind == "log":
            # Logs fail at the end (test summaries, tracebacks) — serve the
            # tail instead of refusing the whole file.
            with target.open("rb") as f:
                f.seek(size - MAX_BYTES)
                tail = f.read().decode("utf-8", errors="replace")
            # Drop the partial first line left by the byte-offset seek.
            tail = tail.split("\n", 1)[-1]
            content = f"[showing last {MAX_BYTES} of {size} bytes]\n{tail}"
        else:
            content = f"[file too large: {size} bytes; max {MAX_BYTES}]"
        return {
            "name": target.name,
            "relpath": relpath,
            "size": size,
            "truncated": True,
            "content": content,
            "kind": kind,
        }
    try:
        content = target.read_text(encoding="utf-8")
        binary = False
    except UnicodeDecodeError:
        content = "[binary file]"
        binary = True
    return {
        "name": target.name,
        "relpath": relpath,
        "size": size,
        "truncated": False,
        "binary": binary,
        "content": content,
        "kind": _classify(target.name),
    }


def _classify(name: str) -> str:
    low = name.lower()
    if low.endswith(".md"):
        return "markdown"
    if low.endswith(".json"):
        return "json"
    if low.endswith((".log", ".txt")):
        return "log"
    if low.endswith((".yaml", ".yml")):
        return "yaml"
    if low.endswith(".sh"):
        return "shell"
    return "other"
