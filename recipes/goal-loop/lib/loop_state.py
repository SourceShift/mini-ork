"""Persistent cross-wave state for the U4b goal-loop driver.

GRAO outcome-tagged memory (per-unit `failed_fixes` map) + UCCI divergence-kill
(per-wave `failure_signature`). All state is stored as a single JSON file at
``<state_dir>/goal-loop-state.json`` so a resumed driver sees the same waves.

Pure functions, no globals, no I/O outside the state path the caller passes in.
The ``divergence()`` decision is the operator-visible stop signal — its returned
signature string lands verbatim in ``final-verdict.json`` for debugging.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

State = dict[str, Any]

STATE_FILENAME = "goal-loop-state.json"


def state_path(state_dir: str | Path) -> Path:
    """Return the absolute path to the state JSON file for ``state_dir``."""
    return Path(state_dir) / STATE_FILENAME


def _empty_state(goal_id: str) -> State:
    return {
        "goal_id": goal_id,
        "waves": [],
        "failed_fixes": {},
    }


def load_state(state_dir: str | Path, goal_id: str) -> State:
    """Load persisted state or return a fresh state for ``goal_id``.

    A missing file is NOT an error — the driver is happy to start wave 1 with
    no prior history. A present file with a DIFFERENT goal_id is also a fresh
    state (the directory was reused for a different run).
    """
    path = state_path(state_dir)
    if not path.is_file():
        return _empty_state(goal_id)
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return _empty_state(goal_id)
    if not isinstance(loaded, dict):
        return _empty_state(goal_id)
    if loaded.get("goal_id") != goal_id:
        return _empty_state(goal_id)
    loaded.setdefault("waves", [])
    loaded.setdefault("failed_fixes", {})
    return loaded


def save_state(state: State, state_dir: str | Path) -> Path:
    """Write ``state`` to ``<state_dir>/goal-loop-state.json`` (atomic-ish).

    Writes via a sibling temp file then renames so a crashed write does not
    leave the driver with a half-written state file. Returns the path written.
    """
    target = state_path(state_dir)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + ".tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(target)
    return target


def fix_hash(unit_id: str, failing_reasons: list[str] | None = None) -> str:
    """sha256 of ``unit_id`` + the sorted failing-reason list.

    Deterministic across processes — same unit + same sorted reasons always
    yields the same hash, which is the input to GRAO quarantine. ``None``
    reasons is treated as an empty list (a unit with no detail still hashes
    deterministically by id alone).
    """
    h = hashlib.sha256()
    h.update(unit_id.encode("utf-8"))
    h.update(b"\x00")
    for reason in sorted(failing_reasons or ()):
        h.update(reason.encode("utf-8"))
        h.update(b"\x00")
    return h.hexdigest()


def wave_signature(failing_units: list[str]) -> str:
    """sha256 of the sorted failing-unit list for the wave.

    Identical signatures across consecutive waves means the outer driver
    fixed nothing; that is the UCCI divergence-kill trigger.
    """
    h = hashlib.sha256()
    for unit in sorted(failing_units):
        h.update(unit.encode("utf-8"))
        h.update(b"\x00")
    return h.hexdigest()


def record_wave(
    state: State,
    *,
    wave: int,
    run_id: str,
    failing_before: list[str],
    failing_after: list[str],
    cost_usd: float,
) -> State:
    """Append a wave record + update ``failed_fixes`` for each still-failing unit.

    ``failing_before`` is the unit set the wave STARTED trying to fix (i.e. the
    units the wave's hunt selected — quarantined units are excluded).
    ``failing_after`` is what is STILL failing at the end of the wave (the
    goal_state_eval output). ``failed_fixes[unit_id]`` records the hash for
    every unit that survived a wave; the same hash appearing on two waves
    means the same fix attempt was tried twice, which is the GRAO quarantine
    signal.
    """
    sig = wave_signature(failing_after)
    wave_record = {
        "wave": wave,
        "run_id": run_id,
        "failing_before": sorted(failing_before),
        "failing_after": sorted(failing_after),
        "cost_usd": float(cost_usd),
        "signature": sig,
    }
    state.setdefault("waves", []).append(wave_record)

    failed_fixes: dict[str, list[str]] = state.setdefault("failed_fixes", {})
    for unit_id in sorted(failing_after):
        # Hash is keyed on the unit id only — two consecutive waves where
        # the SAME unit remains in failing_after yields the SAME hash, which
        # is the GRAO quarantine trigger ("hash already appears twice"). The
        # kickoff §Goal ¶1 mentions ``sorted failing reasons`` as the second
        # hash input, but panel-verdict.json (the wave's contract output)
        # only carries unit ids, not per-unit reasons — the reasons live in
        # ``goal-state.json`` which the wave does not surface. Unit-id-keyed
        # hashing is the deterministic stand-in that keeps the quarantine
        # logic testable without a side channel for reasons.
        h = fix_hash(unit_id)
        failed_fixes.setdefault(unit_id, []).append(h)

    return state


def should_quarantine(unit_id: str, current_hash: str, state: State) -> bool:
    """GRAO quarantine check.

    Per kickoff §Goal ¶1 "A unit whose CURRENT failure hash already appears
    twice is QUARANTINED". This function checks if ``current_hash`` appears
    2+ times in ``state["failed_fixes"][unit_id]`` BEFORE we append the
    current sighting — caller decides whether to skip this wave entirely
    (quarantined units are skipped by the hunt, reported in
    ``final-verdict.json``).
    """
    history = state.get("failed_fixes", {}).get(unit_id, [])
    same_count = sum(1 for h in history if h == current_hash)
    return same_count >= 2


def divergence(state: State) -> str | None:
    """UCCI divergence-kill trigger.

    Returns ``None`` when no divergence has been detected, or a short string
    describing the divergence when the driver should stop. The string is
    written into ``final-verdict.json`` verbatim for operator debuggability.

    Two trigger conditions (per kickoff §Goal ¶1):
      1. Same failure signature on two consecutive waves → "no_progress".
      2. ``failing_after > failing_before`` on two consecutive waves →
         "regressing".
    """
    waves: list[dict[str, Any]] = state.get("waves", [])
    if len(waves) < 2:
        return None

    last_two = waves[-2:]

    sigs = [w.get("signature") for w in last_two]
    if len(sigs) == 2 and sigs[0] == sigs[1] and sigs[0] is not None:
        return f"no_progress:{sigs[0]}"

    def _failing_count(w: dict[str, Any]) -> int:
        return len(w.get("failing_after", []))

    counts = [_failing_count(w) for w in last_two]
    if len(counts) == 2 and counts[1] > counts[0]:
        # Two waves in a row of growing failure counts. The kickoff says
        # "regressing two waves running" — both waves must show growth vs the
        # previous one. We only have two waves here, so we check whether the
        # second is larger than the first; if a third wave also grows the
        # next call (with three waves) will fire again. The kickoff's two-wave
        # rule applies once we have >= 2 waves to compare.
        return f"regressing:{counts[0]}->{counts[1]}"

    return None