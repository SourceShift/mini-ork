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
    verdict_known: bool = True,
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

    ``verdict_known=False`` marks a wave whose failing set was never MEASURED
    (the wave timed out, crashed, or its verifier emitted no unit list). Its
    ``failing_after`` is empty because nothing was observed, so the record
    carries no signature and accrues no fix-hash: an unobserved wave must not
    look like a satisfied goal, and a timeout must not look like "attempted
    twice, same failure" to the quarantine detector.

    Backward-compatible: ``reasons=None`` yields the historical set-only
    signature and id-only fix-hash; ``attempted=None`` falls back to hashing
    every still-failing unit (the original test-suite-loop contract).
    ``diagnostics`` carries the wave's evidence fingerprints, the child's
    self-verdict, and the operator class each unit's failure called for; callers
    without them omit it, and each key is attached only when non-empty.
    ``verdict_known=True`` (the default) emits no extra key, so a caller that
    does not know about the flag writes a byte-identical record to before.
    """
    sig = wave_signature(failing_after, reasons) if verdict_known else None
    wave_record = {
        "wave": wave,
        "run_id": run_id,
        "failing_before": sorted(failing_before),
        "failing_after": sorted(failing_after),
        "cost_usd": float(cost_usd),
        "signature": sig,
    }
    if not verdict_known:
        # No signature: the hash of the empty set is exactly what a satisfied
        # goal's signature looks like, and this wave did not observe that.
        wave_record["verdict_known"] = False
    if attempted is not None:
        wave_record["attempted"] = sorted(attempted)
    if diagnostics is not None:
        prev_waves = state.get("waves", [])
        prev_sig = prev_waves[-1].get("signature") if prev_waves else None
        wave_record["headroom_closed"] = (
            len(failing_before) - len(failing_after) if verdict_known else None
        )
        wave_record["predicate_moved"] = (
            None if (prev_sig is None or not verdict_known) else (sig != prev_sig)
        )
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
    if not verdict_known:
        # An unobserved wave cannot say whether a fix worked, so it accrues no
        # sighting: a timeout must not read as "attempted twice, same failure"
        # and quarantine a unit that was never scored.
        hash_units: list[str] = []
    elif attempted is not None:
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


def measured_tail(waves: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The maximal trailing run of waves that actually MEASURED a failing set.

    A wave whose verdict never arrived records ``verdict_known: False`` — it
    timed out, crashed, or its verifier emitted an ``error`` panel, so its
    failing set is UNOBSERVED rather than empty. Scanning back to the first
    measured wave keeps such a wave out of every detector that reasons about the
    failing set. Without the cut, a wave folded as "zero failing" makes the next
    wave's real count look like a regression from zero, and the loop kills a
    campaign that is working.

    The cut is a BREAK, not a filter: a divergence pattern is ``patience``
    *consecutive* waves, so an unobserved wave between two measured ones means
    the pattern was not sustained across the window.
    """
    tail: list[dict[str, Any]] = []
    for wave in reversed(waves):
        if not wave.get("verdict_known", True):
            break
        tail.append(wave)
    tail.reverse()
    return tail


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
    measured = measured_tail(waves)
    if len(measured) < patience:
        # Fewer measured waves than the window. An unobserved wave breaks the
        # run, so no pattern can be asserted across it — silence here is the
        # absence of evidence, not evidence of progress.
        return None

    if rdisc:
        for detector in (self_verdict_mirage, evidence_informativeness):
            hit = detector(state, patience)
            if hit is not None:
                return hit

    recent = measured[-patience:]

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
    recent = measured_tail(waves)[-patience:]
    if len(recent) < patience:
        return None
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
    recent = measured_tail(waves)[-patience:]
    if len(recent) < patience:
        return None
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


# ── Goal-level diagnostics: what the green did NOT look at ───────────────────
#
# Everything above answers "did the loop stall?". These answer a different
# question: "did the loop look at enough to know?". They run on the PASS path,
# where ``divergence()`` is unreachable — the driver returns ``goal_met`` before
# it. A goal can be met against every axis the predicate reads while the target
# still violates an obligation no axis of the predicate can express; that is the
# figure blind spot, and no amount of retrying the wave can reveal it.

# A predicate reason is space-separated ``key=value`` pairs (chapter_predicate.py):
# ``ch1 PASS status=completed rubric=pass mdlen=912 quality=unset``. Scanning
# stops at the first nested annotation or terminal field, because everything
# past either is a SUB-annotation whose keys are not predicate axes: a PASS may
# carry ``... quality=<probe detail> [figure-loss attached=8 live=0 ...]``, and
# ``live=0`` is a fact about one chapter's figures, not an axis the predicate
# read. Without the cut a uniform ``live=0`` across every unit would be
# reported as the axis the green was vacuous with respect to — which is both
# the wrong name and the wrong mechanism. The lookbehind rejects a ``-`` prefix
# so the ``rubric-healed=true`` suffix is not read as an axis named ``healed``,
# and ``\b`` keeps ``stderr=``/``lasterr=`` from matching.
_AXIS_KEY = re.compile(r"(?<![A-Za-z0-9_/-])([A-Za-z_][A-Za-z0-9_]*)=")
_AXIS_CUT = re.compile(r"\[|\berr=")

# A value with no discriminating power: absent, a sentinel, or zero. ``quality``
# resolves to ``unset`` for EVERY unit whenever ``MO_GOAL_QUALITY_CMD`` is unset,
# which is the live case — a goal that names quality and measures none.
_DEGENERATE = frozenset({"", "-", "n/a", "na", "none", "null", "unset", "0", "false"})


def _reason_axes(reason: str) -> dict[str, str]:
    """Parse a predicate reason line into its ``axis -> value`` map.

    Scanning stops at ``quality=``, the last axis the predicate itself emits.
    Everything after it is the quality probe's own detail — ``sections=4
    total=23090 headings=4`` — a nested string that is not part of the
    predicate's grammar. Reading its keys as axes let a probe key that happens
    to be uniform and zero (``live=0`` off a ``[figure-loss ...]`` bracket)
    present itself as the axis the green was vacuous with respect to.

    An unparseable reason yields ``{}`` — the caller must treat "no axes" as
    "cannot diagnose", never as "no problem".
    """
    if not reason:
        return {}
    head = _AXIS_CUT.split(reason, 1)[0]
    axes: dict[str, str] = {}
    for match in _AXIS_KEY.finditer(head):
        rest = head[match.end():]
        key = match.group(1)
        axes[key] = rest.split(None, 1)[0] if rest else ""
        if key == "quality":
            break
    return axes


def goal_vacuity(
    state: State,
    reasons: dict[str, str] | None = None,
) -> str | None:
    """Did the goal pass without any axis of the predicate ever discriminating?

    Fires ``vacuous_goal_met:<axis>+<axis>`` when the last wave cleared every
    unit while at least one axis it reported is degenerate for ALL of them —
    absent, a sentinel, or zero. Returns ``None`` when the goal is still failing,
    when no reasons were supplied, or when a reason cannot be parsed.

    The reason is not "the loop is broken": it is that the green rests on axes
    that were never in a position to say otherwise. On the live book every
    chapter passes with ``quality=unset``, so the loop is certifying a quality
    goal having measured no quality at all.
    """
    waves = state.get("waves", [])
    if not waves or not reasons:
        return None
    # The green must be one the loop actually OBSERVED. A trailing unobserved
    # wave is not a green one, and reading its empty failing set as "all clear"
    # would hang a vacuity finding on a measurement that never happened.
    measured = measured_tail(waves)
    if not measured or measured[-1].get("failing_after"):
        return None

    per_axis: dict[str, set[str]] = {}
    parsed = 0
    for reason in reasons.values():
        axes = _reason_axes(reason)
        if not axes:
            return None
        parsed += 1
        for key, value in axes.items():
            per_axis.setdefault(key, set()).add(value)
    if not parsed:
        return None

    # Uniform AND degenerate. A uniform-but-meaningful axis (``rubric=pass`` on a
    # pass) is what a met goal is SUPPOSED to look like and must not fire.
    dead = sorted(
        key for key, values in per_axis.items()
        if len(values) == 1 and next(iter(values)).lower() in _DEGENERATE
    )
    if not dead:
        return None
    return f"vacuous_goal_met:{'+'.join(dead)}"


def parse_obligations(text: str) -> list[dict[str, Any]]:
    """Parse ``<name>|<declared>|<satisfied>|<detail>`` rows from the sensor.

    The obligation sensor is DATA the operator declares (``MO_GOAL_OBLIGATION_CMD``),
    never logic in here: each row names an obligation the target's contract
    imposes, how many instances exist, and how many are satisfied. A malformed
    row is dropped rather than guessed at — a sensor that invented obligations
    would fire on nothing and be ignored.
    """
    rows: list[dict[str, Any]] = []
    for line in (text or "").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split("|")
        if len(parts) < 3:
            continue
        name, declared, satisfied = (p.strip() for p in parts[:3])
        if not name or not declared.isdigit() or not satisfied.isdigit():
            continue
        rows.append({
            "name": name,
            "declared": int(declared),
            "satisfied": int(satisfied),
            "detail": parts[3].strip() if len(parts) > 3 else "",
        })
    return rows


def obligation_gap(obligations: list[dict[str, Any]] | None) -> str | None:
    """Name the FIRST declared obligation the target has left unsatisfied.

    Returns ``obligation_gap:<name>:<satisfied>/<declared>``, or ``None`` when
    every declared obligation is met (or none was declared). Only a genuinely
    declared obligation counts: an obligation nobody declared is invisible here
    by construction, and saying so is the honest limit of a sensor.

    Declaration order decides, not gap size. The counts are incommensurable —
    "10 chapters need a figure" and "16 rubric axes go unread" are not comparable
    magnitudes, so ranking by difference would let a wide-but-minor row bury the
    one the operator put first. The sensor is ordered most-important-first by
    whoever seeded it; that ordering is the priority.
    """
    for row in obligations or ():
        if row["declared"] - row["satisfied"] > 0:
            return f"obligation_gap:{row['name']}:{row['satisfied']}/{row['declared']}"
    return None