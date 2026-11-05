"""Pure-logic port of ``lib/config_resolve.sh``.

Faithful port of the per-run config isolation pattern (roadmap T1.0):
each run FREEZES its launch-time lane policy by snapshotting the
global agents.yaml into ``run_dir/config/`` at launch, and every
dispatch-time resolver reads ``run_dir/config/`` FIRST. This is the
first concrete slice of the actor-per-run isolation epic — a design
pattern (not a band-aid) that survives the Bash→Python migration.

Strangler-fig co-existence: ``lib/config_resolve.sh`` is byte-identical
before and after this module exists.
``tests/unit/test_config_resolve_parity.py`` is the gate that proves the
port produces byte-identical stdout and filesystem state against the
live bash subprocess (no mocking).

Public API::

    from mini_ork.dispatch.config_resolve import (
        resolve_agents_yaml,    # effective agents.yaml path, run-dir first
        snapshot_run_config,    # freeze launch-time policy into <run_dir>
    )
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

from mini_ork.dispatch import agents_config


def resolve_agents_yaml() -> None:
    """Echo the EFFECTIVE agents.yaml path, run-dir first.

    Precedence: ``$MINI_ORK_RUN_DIR/config/agents.yaml`` →
    ``agents_config.effective_path()`` (the merged template + personal
    overlay at ``<home>/config/.agents.effective.yaml`` when an overlay
    exists, otherwise the tracked template path). With NO overlay the
    effective path is byte-equivalent to the prior HOME/ROOT fallback.

    Always echoes a non-empty path. Callers ``[ -f ]``-guard the
    "not configured" case. Prints ``path + "\\n"`` to stdout, mirroring
    bash's ``printf '%s\\n'``.

    Path strings are constructed with ``os.path.join`` (NOT ``Path /``)
    so the literal ``./`` prefix is preserved when bash's default
    root of ``.`` kicks in. ``Path /`` normalises the dot away and
    would diverge from bash's stdout byte.
    """
    run_dir = os.environ.get("MINI_ORK_RUN_DIR", "")
    if run_dir:
        candidate = os.path.join(run_dir, "config", "agents.yaml")
        if Path(candidate).is_file():
            print(candidate)
            return

    print(agents_config.effective_path())


def snapshot_run_config(run_dir: str | None = None) -> bool:
    """Freeze the EFFECTIVE agents.yaml (merged template + overlay) into
    ``run_dir/config/``.

    Idempotent: never overwrites an existing snapshot, so a re-entrant
    execute keeps the launch-time policy. Best-effort: any failure
    returns ``True`` (the resolvers fall back to the global file) so
    it can never break a run. Source is the merged effective file
    produced by ``agents_config`` — NOT the raw template — so a run
    freezes the USER's merged policy, not the team's default.
    """
    rd = run_dir if run_dir is not None else os.environ.get("MINI_ORK_RUN_DIR", "")
    if not rd:
        return True

    dest = os.path.join(rd, "config", "agents.yaml")
    if Path(dest).is_file():
        return True  # already frozen — keep launch-time policy

    try:
        src = agents_config.effective_path()
    except ValueError as exc:
        # Never break the run from here, but never hide the user's own
        # mistake either: the dispatch-time resolvers raise the same error.
        print(f"mini-ork: lane config: {exc}", file=sys.stderr)
        return True
    if not Path(src).is_file():
        return True  # nothing to snapshot

    try:
        Path(os.path.join(rd, "config")).mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest)
    except (OSError, IOError):
        return True  # mirror bash's `2>/dev/null || return 0` semantic

    _apply_lane_overrides(dest)
    return True


#: ``<alias>=<lane>,…`` — a launch-time lane assignment for THIS run
#: (``board start --lane implementer=glm``). Read here rather than in the
#: launcher because the snapshot below is the one place every dispatch-time
#: resolver already agrees on, so an override written here is honoured by
#: every node without a second precedence rule.
LANE_OVERRIDES_ENV = "MO_LANE_OVERRIDES"


def lane_overrides(raw: str | None = None) -> dict[str, str]:
    """Parse :data:`LANE_OVERRIDES_ENV` into ``{alias: lane}``.

    Empty and malformed pairs are dropped rather than raised: the snapshot is
    best-effort by contract, and a bad pair reaching here has already been
    refused by the CLI that set it. ``board start`` validates before launching,
    so this path must not be able to fail a run that is already dispatched.
    """
    text = (raw if raw is not None else os.environ.get(LANE_OVERRIDES_ENV, "")).strip()
    out: dict[str, str] = {}
    for pair in text.split(","):
        alias, sep, lane = pair.partition("=")
        if sep and alias.strip() and lane.strip():
            out[alias.strip()] = lane.strip()
    return out


def _apply_lane_overrides(dest: str) -> None:
    """Merge the launch-time assignment into the just-copied snapshot.

    Only after a fresh copy — an existing snapshot is the frozen policy of an
    earlier launch and is never rewritten (the function above returns before
    this is reached). A YAML that cannot be read or re-written is left as the
    effective file copied there: the run keeps working with the merged policy,
    which is what it had before this feature existed.
    """
    overrides = lane_overrides()
    if not overrides:
        return
    try:
        import yaml
    except ImportError:  # pragma: no cover — config_resolve is used with yaml installed
        return
    try:
        with open(dest, encoding="utf-8") as fh:
            doc = yaml.safe_load(fh) or {}
    except (OSError, ValueError, yaml.YAMLError):
        return
    if not isinstance(doc, dict):
        return
    lanes = doc.get("lanes")
    doc["lanes"] = {**(lanes if isinstance(lanes, dict) else {}), **overrides}
    try:
        with open(dest, "w", encoding="utf-8") as fh:
            yaml.safe_dump(doc, fh, sort_keys=True)
    except (OSError, ValueError, yaml.YAMLError):
        return
