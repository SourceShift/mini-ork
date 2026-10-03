"""Compute per-file diffs an ACP agent can hand to Zed's ``Review Changes``.

Slice Z4 of the Zed engineer surface (2026-10-03). ``run_diffs`` reads the
implementer's summary (``implementer-summary.json``) and the baseline commit
ref (``pre-implementer-ref``) the recipe writes BEFORE the implementer edits,
then for each ``files_changed`` entry returns ``{path, old_text, new_text}``
triples the agent attaches to the implementer's tool call under
``FileEditToolCallContent(type="diff", ...)``. ``cached_or_computed`` adds a
thin cache layer (``<run_dir>/acp-diffs.json``) so a future ``load_session``
of an old run replays the exact bytes — even after the user keeps editing
the worktree.

Pure functions: no ACP types, no async, no module-level state. One-way
dependency matches ``orchestration.py`` (``agent → diffs``, not the reverse).
Every failure (missing file, ``git`` non-zero, timeout, oversized, binary) is
swallowed at the file boundary — the function never raises.
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any

__all__ = ["run_diffs", "cached_or_computed", "CACHE_NAME"]

# Cap on number of changed files surfaced; matches the kickoff's "first N".
DEFAULT_MAX_FILES = 20
# Per-file cap (bytes). Larger files become placeholders to keep ACP chunks
# bounded (a 1 GB lockfile would otherwise blow up a single ToolCallProgress).
DEFAULT_MAX_BYTES = 200_000
# ``git show <baseline>:<rel>`` wall-clock budget. Mirrors the timeout posture
# in ``mini_ork.acp.live.LiveTail`` — bounded so a hung git never freezes the
# projection loop.
GIT_TIMEOUT_S = 10
# Placeholder when the content is binary or exceeds the byte cap. Zed's
# ``Review Changes`` renderer keys on the same string the kickoff mandates.
PLACEHOLDER = "(binary or large file changed — not shown)"
# Cache file basename; the agent stores the cache at ``<run_dir>/acp-diffs.json``.
CACHE_NAME = "acp-diffs.json"


def _read_summary(run_dir: Path) -> dict[str, Any] | None:
    """Return the parsed ``implementer-summary.json`` or ``None`` on any miss."""
    path = run_dir / "implementer-summary.json"
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _read_baseline(run_dir: Path) -> str | None:
    """Return the baseline commit sha (``pre-implementer-ref``) stripped, or None.

    An empty file or one with whitespace-only contents yields ``None`` — the
    caller treats "no baseline" the same as "git show failed".
    """
    path = run_dir / "pre-implementer-ref"
    try:
        with open(path, encoding="utf-8") as handle:
            ref = handle.read().strip()
    except OSError:
        return None
    return ref or None


def _git_show(worktree: Path, baseline: str, rel: str) -> bytes | None:
    """Return ``git -C <wt> show <baseline>:<rel>`` bytes, or ``None`` on failure.

    A non-zero exit code, timeout, or a binary stdout blob that ``ptype``
    would reject all return ``None`` — the caller falls back to the fixture
    copy or treats the file as new.
    """
    try:
        proc = subprocess.run(
            ["git", "-C", str(worktree), "show", f"{baseline}:{rel}"],
            capture_output=True,
            timeout=GIT_TIMEOUT_S,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout


def _fixture_text(run_dir: Path, rel: str) -> str | None:
    """Return the fixture copy's text, or ``None`` when missing / binary.

    The fixture copy is the ground-truth pre-impl state for files git does
    not track (untracked at baseline) — see ``mini_ork.cli.execute._write_*``
    for the writer. UTF-8 decode with ``errors="replace"`` so a half-binary
    file degrades to a question-mark-prefixed string rather than raising.
    """
    path = run_dir / "pre-impl-fixture" / "files" / rel
    try:
        raw = path.read_bytes()
    except OSError:
        return None
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return None
    return text


def _file_text(path: Path) -> str | None:
    """Return a file's UTF-8 text or ``None`` when it can't be decoded / opened."""
    try:
        raw = path.read_bytes()
    except OSError:
        return None
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return None


def _diff_for_file(
    run_dir: Path,
    worktree: Path,
    baseline: str | None,
    abs_path: str,
    *,
    max_bytes: int,
) -> dict[str, Any] | None:
    """Compute one file's ``{path, old_text, new_text}`` triple, or ``None`` to skip.

    Skips (returns ``None``) when the path is not under the worktree, so a
    run that declared an absolute path outside the claim window never leaks
    into the diff list. Per the kickoff:

    * ``old_text`` — ``git show <baseline>:<rel>`` stdout, falling back to the
      pre-impl fixture copy when the git read fails; ``None`` for a new file.
    * ``new_text`` — current file contents, ``""`` for a deletion.
    * Files over ``max_bytes`` or non-UTF-8 → ``old_text`` None, ``new_text`` the placeholder.
    """
    try:
        rel = os.path.relpath(abs_path, str(worktree))
    except ValueError:
        # Different drive on Windows or otherwise unrelatable.
        return None
    if rel.startswith("..") or os.path.isabs(rel):
        return None

    old_bytes: bytes | None = None
    if baseline:
        old_bytes = _git_show(worktree, baseline, rel)
    if old_bytes is None:
        fixture = _fixture_text(run_dir, rel)
        if fixture is not None:
            old_text: str | None = fixture
        else:
            old_text = None  # new file (or git AND fixture both missing)
    else:
        try:
            old_text = old_bytes.decode("utf-8")
        except UnicodeDecodeError:
            # A binary baseline is a changed binary file, not a new one.
            return {"path": abs_path, "old_text": None, "new_text": PLACEHOLDER}

    file_path = Path(abs_path)
    if file_path.exists():
        new_text = _file_text(file_path)
        if new_text is None:
            return {"path": abs_path, "old_text": None, "new_text": PLACEHOLDER}
    else:
        new_text = ""  # deletion

    # Size cap. ``new_text`` is the cheap check — a generator that wrote a
    # multi-megabyte lockfile would otherwise round-trip its full contents
    # through pydantic + ACP.
    if (
        old_text is not None and len(old_text.encode("utf-8")) > max_bytes
    ) or len(new_text.encode("utf-8")) > max_bytes:
        # Never the placeholder on both sides: identical texts read as "no change".
        return {"path": abs_path, "old_text": None, "new_text": PLACEHOLDER}

    return {"path": abs_path, "old_text": old_text, "new_text": new_text}


def run_diffs(
    run_dir: Path,
    *,
    max_files: int = DEFAULT_MAX_FILES,
    max_bytes: int = DEFAULT_MAX_BYTES,
    write_cache: bool = True,
) -> list[dict[str, Any]]:
    """Compute the diff list for ``run_dir``; ``[]`` on any miss.

    Reads ``implementer-summary.json`` and ``pre-implementer-ref``. A missing
    or unreadable summary, ``status == "no_changes"``, or an absent
    ``files_changed`` list all short-circuit to ``[]`` — the caller treats
    that as "no update needed". Successful (non-empty) results are persisted
    to ``<run_dir>/acp-diffs.json`` best-effort so a later
    ``cached_or_computed`` reads the cache without re-invoking git.

    Never raises: any git / IO / decode error skips the offending file.
    """
    summary = _read_summary(run_dir)
    if summary is None:
        return []
    if summary.get("status") == "no_changes":
        return []
    files = summary.get("files_changed") or []
    if not isinstance(files, list) or not files:
        return []
    worktree_raw = summary.get("worktree_path")
    if not isinstance(worktree_raw, str) or not worktree_raw:
        return []
    worktree = Path(worktree_raw)
    # A missing worktree (the implementer ran in a worktree that was since
    # cleaned up) can't compute any diffs — every ``rel`` would land on a
    # non-existent file and the run summary becomes meaningless. Bail out
    # before any per-file work rather than emit a row of empty diffs.
    if not worktree.exists():
        return []
    baseline = _read_baseline(run_dir)

    diffs: list[dict[str, Any]] = []
    for abs_path in files[:max_files]:
        if not isinstance(abs_path, str) or not abs_path:
            continue
        entry = _diff_for_file(
            run_dir,
            worktree,
            baseline,
            abs_path,
            max_bytes=max_bytes,
        )
        if entry is None:
            continue
        diffs.append(entry)

    if diffs and write_cache:
        _write_cache(run_dir, diffs)
    return diffs


def _write_cache(run_dir: Path, diffs: list[dict[str, Any]]) -> None:
    """Best-effort cache write — a read-only home degrades to "no cache"."""
    cache = run_dir / CACHE_NAME
    try:
        with open(cache, "w", encoding="utf-8") as handle:
            json.dump(diffs, handle)
    except OSError:
        pass


def cached_or_computed(run_dir: Path) -> tuple[list[dict[str, Any]], bool]:
    """Return ``(diffs, from_cache)``: cache wins when parseable.

    A missing cache or one that fails to parse (truncated write, schema
    drift) falls through to ``run_diffs`` so a fresh computation is always
    available. The boolean lets the caller distinguish the two paths for
    UX (a "may have changed since the run" note only appears on
    ``from_cache=False``).
    """
    cache = run_dir / CACHE_NAME
    try:
        with open(cache, encoding="utf-8") as handle:
            data = json.load(handle)
        if isinstance(data, list):
            return data, True
    except (OSError, ValueError, json.JSONDecodeError):
        pass
    # Computed now from the files as they are, so never cached: a cache must
    # only ever hold what the run itself produced (written at node_end).
    return run_diffs(run_dir, write_cache=False), False