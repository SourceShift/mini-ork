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
import re
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


def _reason_fingerprint(reason: str) -> str:
    """Stable per-unit failure fingerprint for progress detection.

    A wave makes no progress on a unit when this fingerprint is unchanged.
    It KEEPS the signal fields — ``status`` and the failing node / error head —
    and DROPS the volatile counters (``attempts=``, ``mdlen=``) that churn on
    every wave without reflecting real movement. So a fix that shifts the
    failure from ``W9`` to ``W15`` (or clears it) changes the fingerprint =
    progress, while a bare retry-counter tick does not. Empty/None reason →
    empty string (an un-detailed unit fingerprints deterministically as "").
    """
    if not reason:
        return ""
    status = ""
    m = re.search(r"status=(\S+)", reason)
    if m:
        status = m.group(1)
    err = ""
    m = re.search(r"err=(.*)$", reason)
    if m:
        tail = m.group(1)
        node = re.search(r"segment node '([^']+)'", tail)
        err = f"node:{node.group(1)}" if node else tail[:80]
    return f"{status}|{err}"


def wave_signature(
    failing_units: list[str],
    reasons: dict[str, str] | None = None,
) -> str:
    """sha256 of the sorted failing-unit list for the wave.

    Identical signatures across consecutive waves means the outer driver
    fixed nothing; that is the UCCI divergence-kill trigger. When ``reasons``
    is supplied (a ``unit_id -> reason`` map), each unit's stable failure
    fingerprint is folded in, so the signature reflects WHETHER EACH UNIT'S
    FAILURE MOVED, not merely whether the failing SET changed size. This is
    what keeps a per-unit loop (one attempt per wave over N units) from
    reading "no progress" the instant the set stops shrinking — the set can
    only shrink when a whole unit lands, but a fingerprint shift is progress.
    ``reasons=None`` preserves the historical set-only signature byte-for-byte.
    """
    h = hashlib.sha256()
    for unit in sorted(failing_units):
        h.update(unit.encode("utf-8"))
        h.update(b"\x00")
        if reasons is not None:
            h.update(_reason_fingerprint(reasons.get(unit, "")).encode("utf-8"))
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
    reasons: dict[str, str] | None = None,
    attempted: list[str] | None = None,
    diagnostics: dict[str, Any] | None = None,
) -> State:
    """Append a wave record + update ``failed_fixes`` for each attempted unit.

    ``failing_before`` is the unit set the wave STARTED trying to fix (i.e. the
    units the wave's hunt selected — quarantined units are excluded).
    ``failing_after`` is what is STILL failing at the end of the wave (the
    goal_state_eval output). ``reasons`` is a ``unit_id -> reason`` map
    (goal-state.json) folded into both the wave signature and each unit's
    fix-hash so the GRAO/UCCI detectors see per-unit failure fingerprints, not
    bare unit ids. ``attempted`` names the units the wave actually dispatched a
    fix for (sweep fan-out); ONLY those accrue a fix-hash sighting, so a unit
    that is failing merely because it has not been reached yet (e.g. a pending
    chapter the single-child-per-wave loop never got to) does not accrue
    identical hashes and cannot spuriously trip ``all_quarantined``.

    Backward-compatible: ``reasons=None`` yields the historical set-only
    signature and id-only fix-hash; ``attempted=None`` falls back to hashing
    every still-failing unit (the original test-suite-loop contract).
    ``diagnostics`` carries the wave's evidence fingerprints, the child's
    self-verdict, and the operator class each unit's failure called for; callers
    without them omit it, and each key is attached only when non-empty.
    """
    sig = wave_signature(failing_after, reasons)
    wave_record = {
        "wave": wave,
        "run_id": run_id,
        "failing_before": sorted(failing_before),
        "failing_after": sorted(failing_after),
        "cost_usd": float(cost_usd),
        "signature": sig,
    }
    if attempted is not None:
        wave_record["attempted"] = sorted(attempted)
    if diagnostics is not None:
        prev_waves = state.get("waves", [])
        prev_sig = prev_waves[-1].get("signature") if prev_waves else None
        wave_record["headroom_closed"] = len(failing_before) - len(failing_after)
        wave_record["predicate_moved"] = None if prev_sig is None else (sig != prev_sig)
        evidence = diagnostics.get("evidence")
        if evidence:
            wave_record["evidence"] = {str(k): str(v) for k, v in sorted(evidence.items())}
        child_diagnostics = diagnostics.get("child_diagnostics")
        if child_diagnostics:
            wave_record["child_diagnostics"] = {
                str(k): dict(v) for k, v in sorted(child_diagnostics.items())
            }
        # The operator CLASS each unit's failure called for — recorded, never
        # dispatched (SHADOW). It rides the same additive contract as `evidence`:
        # absent ⇒ no key, so a caller without operators yields a byte-identical
        # wave record to before this field existed.
        operators = diagnostics.get("operators")
        if operators:
            wave_record["operators"] = {str(k): str(v) for k, v in sorted(operators.items())}
    state.setdefault("waves", []).append(wave_record)

    failed_fixes: dict[str, list[str]] = state.setdefault("failed_fixes", {})
    # A unit accrues a fix-hash sighting only if the wave ATTEMPTED it; when
    # the caller declares no attempted set we fall back to every still-failing
    # unit (historical behavior). The hash folds in the unit's failure
    # fingerprint, so two identical hashes now means "attempted twice, and the
    # failure did not move" — the true GRAO quarantine signal — rather than
    # merely "still in the failing set".
    if attempted is not None:
        hash_units = sorted(set(attempted) & set(failing_after))
    else:
        hash_units = sorted(failing_after)
    for unit_id in hash_units:
        fp = _reason_fingerprint(reasons.get(unit_id, "")) if reasons else None
        h = fix_hash(unit_id, [fp] if fp else None)
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


def divergence(state: State, patience: int = 2, rdisc: bool = True) -> str | None:
    """UCCI divergence-kill trigger.

    Returns ``None`` when no divergence has been detected, or a short string
    describing the divergence when the driver should stop. The string is
    written into ``final-verdict.json`` verbatim for operator debuggability.

    Two trigger conditions (per kickoff §Goal ¶1):
      1. Same failure signature on ``patience`` consecutive waves →
         "no_progress".
      2. Strictly-growing failing count across ``patience`` consecutive waves
         → "regressing".

    With ``rdisc=True`` (default) the two reward-discrimination detectors run
    first and may short-circuit with an ``uninformative_evidence:<sha8>`` or
    ``mirage:<unit>:<bytes>`` reason; ``rdisc=False`` reproduces the historical
    behavior byte-for-byte.

    ``patience`` is the number of consecutive waves that must show the pattern
    before the driver gives up. The historical default of 2 makes a single
    repeat terminal, which is correct for a loop that attempts every failing
    unit per wave. A per-unit loop (one fix attempt per wave over many units)
    needs a larger window so the fixer has room to land a multi-wave fix before
    the loop declares stagnation; the driver sets it from
    ``MO_GOAL_DIVERGENCE_PATIENCE``. Values < 2 are clamped to 2.
    """
    if patience < 2:
        patience = 2
    waves: list[dict[str, Any]] = state.get("waves", [])
    if len(waves) < patience:
        return None

    if rdisc:
        for detector in (self_verdict_mirage, evidence_informativeness):
            hit = detector(state, patience)
            if hit is not None:
                return hit

    recent = waves[-patience:]

    sigs = [w.get("signature") for w in recent]
    if sigs[0] is not None and all(s == sigs[0] for s in sigs):
        return f"no_progress:{sigs[0]}"

    def _failing_count(w: dict[str, Any]) -> int:
        return len(w.get("failing_after", []))

    counts = [_failing_count(w) for w in recent]
    # Every step in the window must grow — a strictly-increasing failing count
    # sustained across ``patience`` waves is the "regressing" signal.
    if all(counts[i] < counts[i + 1] for i in range(len(counts) - 1)):
        return f"regressing:{counts[0]}->{counts[-1]}"

    return None


def evidence_informativeness(state: State, patience: int = 2) -> str | None:
    """r_disc: did the evidence this loop fed actually move the predicate?

    Fires ``uninformative_evidence:<sha8>`` when ``patience`` consecutive waves
    were handed the IDENTICAL per-unit evidence bundle AND the wave signature
    never moved across them. That pair means the evidence is not touching the
    cause — the loop must change what it LOOKS AT, not what the child patches.
    """
    waves = state.get("waves", [])
    if len(waves) < patience:
        return None
    recent = waves[-patience:]
    bundles = [w.get("evidence") or {} for w in recent]
    if any(not b for b in bundles):
        return None
    if any(b != bundles[0] for b in bundles):
        return None
    sigs = [w.get("signature") for w in recent]
    if len(set(sigs)) != 1:
        return None
    first = sorted(bundles[0].values())[0]
    return f"uninformative_evidence:{first[:8]}"


def self_verdict_mirage(state: State, patience: int = 2) -> str | None:
    """The child's own "pass" while the predicate stands still.

    Fires ``mirage:<unit>:<bytes>`` when ``patience`` consecutive waves all
    report ``child_verdict == "pass"``, the signature never moved, and the
    patch is the same size every time (or was a no-op). A self-verdict gate
    that accepts the identical non-fix forever is the accept-all degeneration.
    """
    waves = state.get("waves", [])
    if len(waves) < patience:
        return None
    recent = waves[-patience:]
    sigs = [w.get("signature") for w in recent]
    if len(set(sigs)) != 1:
        return None
    for w in recent:
        cd = w.get("child_diagnostics") or {}
        if not cd:
            return None
        if {(v or {}).get("child_verdict") for v in cd.values()} != {"pass"}:
            return None
    last = recent[-1].get("child_diagnostics") or {}
    unit = sorted(last)[0]
    entry = last.get(unit) or {}
    no_op = any(
        (v or {}).get("child_no_op")
        for w in recent
        for v in (w.get("child_diagnostics") or {}).values()
    )
    sizes = [
        tuple(sorted(
            str((v or {}).get("review_diff_bytes"))
            for v in (w.get("child_diagnostics") or {}).values()
        ))
        for w in recent
    ]
    if not no_op and len(set(sizes)) != 1:
        return None
    return f"mirage:{unit}:{entry.get('review_diff_bytes')}"