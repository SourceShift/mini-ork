"""Escalation — turn an engine fault into a linked framework-edit fix epic.

The self-driving loop's third action (SDE-3a). ``auto_repair.decide`` hands a
failed run to a human when nothing it can *do* would fix it; when the typed
diagnosis says the failure is the **engine's** own fault
(``layer == "engine"``, ``routes_to == "fix_epic"``) the right move is instead to
open a ``framework-edit`` epic that fixes mini-ork and park the run on it until
that epic lands. This module is that half:

    gather_inputs → should_escalate (the frozen operator policy) → file/promote
    the fix → record ``waiting_on_fix`` → tell the oversight inbox

``mini_ork/recovery/escalation_policy.py`` is the operator's decision and is
**frozen** (``config/kernel-frozen.yaml``): this module only gathers its
``EscalationInputs`` and obeys the returned ``EscalationDecision``. An auto-opened
fix epic can therefore never edit the policy that admitted it.

``file_diagnosis`` (``mini_ork.triage.failures``) is the filing half: unlike
``triage_run`` it takes the attribution from the diagnosis rather than from
Python traceback frames, so a delivery-stage engine fault with no traceback is
filed against mini-ork instead of being missed.

Every diagnosis this loop *acts on* — an escalation it opened, or a refusal it
handed to a human — is written to the SDE-3b2 diagnosis ledger
(``mini_ork.stores.diagnosis_ledger``), which is what ``gather_inputs`` reads
back (via ``labelled``) to calibrate the bar a model diagnosis must clear.
Recording is best-effort: a ledger that cannot be written must never fail an
escalation, so ``_record_ledger`` swallows its own errors.

Hard constraint ``c:0``: :func:`escalate` never raises into the run. Any failure
along the way — a broken home, an unreadable DB, a promote that produced no epic
— degrades to ``{"escalated": false, "reason": "escalation error: ..."}`` and the
run falls back to the ordinary human hand-off.
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
from pathlib import Path
from typing import Any

from mini_ork.context import context_env
from mini_ork.recovery import escalation_policy
from mini_ork.recovery.escalation_policy import (
    EscalationDecision,
    EscalationInputs,
    should_escalate,
)
from mini_ork.triage.failures import diagnosis_fingerprint, file_diagnosis

_DEFAULT_DAILY_CAP = 3
_DEFAULT_BREAKER_N = 3
_WINDOW_S = 86400

# ``epics.status`` values that mean the fix epic did not land. ``escalated`` is
# the live one (``scheduler._set_status`` on a failed epic); ``failed`` /
# ``abandoned`` are accepted so a deployment that closes epics that way still
# counts them, rather than silently treating a dead fix as a live one.
_TERMINAL_UNSUCCESSFUL = frozenset({"failed", "escalated", "abandoned"})

# The per-run attempt record ``auto_repair`` also reads and writes. This module
# only adds the ``waiting_on_fix`` keys to it; ``apply`` appends the attempt.
_ATTEMPT_FILE = "repair.json"

# ``run_events.event_type`` for the escalation decision (sibling of
# ``auto_repair``'s row, written the same way — see ``auto_repair``'s
# ``_emit_run_event`` on why this is a direct sqlite3 write).
ESCALATE_EVENT = "auto_repair.escalate"

# The oversight item's gate/feature. Non-blocking by construction: the run is
# already parked in ``waiting_on_fix``, so the item is a *notice*, not a brake.
_OVERSIGHT_GATE = "self-driving-escalation"
_OVERSIGHT_FEATURE = "fix-epic"


def _env_int(key: str, default: int) -> int:
    try:
        return int(context_env(key, str(default)) or default)
    except (TypeError, ValueError):
        return default


def _daily_cap() -> int:
    """``MO_FIX_EPIC_DAILY_CAP`` — default 3, non-int → 3, negative → 0."""
    return max(0, _env_int("MO_FIX_EPIC_DAILY_CAP", _DEFAULT_DAILY_CAP))


def _breaker_n() -> int:
    """``MO_FIX_EPIC_BREAKER_N`` — default 3. Below 1 the breaker cannot judge."""
    return _env_int("MO_FIX_EPIC_BREAKER_N", _DEFAULT_BREAKER_N)


def _run_dir(home: Path, run_id: str) -> Path:
    return Path(home) / "runs" / run_id


def _read_json(path: Path) -> Any:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None


def _load_repair(run_dir: Path) -> dict[str, Any]:
    data = _read_json(run_dir / _ATTEMPT_FILE)
    if not isinstance(data, dict):
        data = {}
    if not isinstance(data.get("attempts"), list):
        data["attempts"] = []
    return data


def _write_repair(run_dir: Path, data: dict[str, Any]) -> None:
    """Best-effort atomic write of ``repair.json`` (mirrors ``auto_repair``'s).

    Kept local rather than imported from ``auto_repair``: that module imports
    *this* one (inside ``apply``), and a shared private helper is not worth the
    import cycle.
    """
    target = run_dir / _ATTEMPT_FILE
    tmp = run_dir / f".{_ATTEMPT_FILE}.tmp.{os.getpid()}"
    try:
        tmp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n",
                       encoding="utf-8")
        os.replace(tmp, target)
    except OSError:
        try:
            tmp.unlink()
        except OSError:
            pass


def _connect(db_path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(str(db_path), timeout=5.0)
    con.execute("PRAGMA busy_timeout=5000")
    return con


# ── inputs (the frozen policy's view of the world) ───────────────────────────


def _self_driving_promotions_since(db_path: Path, since: int) -> int:
    """Fix epics this loop opened in the trailing window.

    ``bug_reports.updated_at`` is INTEGER epoch seconds and is stamped on
    promotion (``bug_report_promote``), so it is the cheapest accurate clock for
    "when was this fix epic opened".
    """
    con = _connect(db_path)
    try:
        row = con.execute(
            "SELECT COUNT(*) FROM bug_reports "
            "WHERE agent_role='self-driving' "
            "  AND promoted_to_epic_id IS NOT NULL "
            "  AND updated_at >= ?",
            (int(since),),
        ).fetchone()
        return int(row[0] or 0) if row else 0
    finally:
        con.close()


def _signature_fix_failed_before(db_path: Path, fingerprint: str) -> bool:
    """GRAO: a fix epic for this exact failure signature already failed.

    The signature is the diagnosis-derived title's fingerprint, so a recurrence
    of the *same* engine fault is recognised and not re-proposed.
    """
    con = _connect(db_path)
    try:
        rows = con.execute(
            "SELECT e.status FROM bug_reports b "
            "LEFT JOIN epics e ON e.id = b.promoted_to_epic_id "
            "WHERE b.agent_role='self-driving' AND b.fingerprint=?",
            (fingerprint,),
        ).fetchall()
    finally:
        con.close()
    # A missing epic row (NULL status) is not evidence of failure — fail open so
    # an inconsistent DB cannot silently block a real escalation.
    return any(str(r[0] or "") in _TERMINAL_UNSUCCESSFUL for r in rows)


def _collapse_tripped(db_path: Path, n: int) -> bool:
    """The circuit breaker: the last *n* fix epics all failed.

    Fewer than *n* auto fix epics → ``False``: a short history is not a collapse.
    """
    if n < 1:
        return False
    con = _connect(db_path)
    try:
        rows = con.execute(
            "SELECT e.status FROM epics e "
            "JOIN bug_reports b ON b.promoted_to_epic_id = e.id "
            "WHERE b.agent_role='self-driving' "
            "ORDER BY e.created_at DESC LIMIT ?",
            (int(n),),
        ).fetchall()
    finally:
        con.close()
    if len(rows) < n:
        return False
    return all(str(r[0] or "") in _TERMINAL_UNSUCCESSFUL for r in rows)


def _labelled_model_rows(db_path: Path) -> list[tuple[float, bool]]:
    """The labelled model diagnoses the calibration runs over (best effort).

    A home with no ledger table yet — or one whose state.db cannot be read —
    yields ``[]``, which is the bootstrap path, not an error: ``gather_inputs``
    runs on *every* escalate, including from homes that predate migration 0073,
    and must never be the thing that fails one.
    """
    try:
        from mini_ork.stores import diagnosis_ledger
        return diagnosis_ledger.labelled(db_path, "model")
    except Exception:  # noqa: BLE001 — calibration input, never a hard failure
        return []


def _calibrated_bar(rows: list[tuple[float, bool]]) -> float | None:
    """The LTT bar over labelled model diagnoses, or ``None`` when uncertifiable.

    Below ``escalation_policy.MIN_LABELS`` there is nothing to calibrate and the
    bar stays ``None`` — the frozen policy applies its bootstrap bar there, so
    the threshold is only consulted at (or above) the policy's own label gate.
    At or above it, an *uncertifiable* diagnoser (no threshold controls the risk)
    also maps to ``None``: the policy reads that as "refuse", never as "wave
    through on the bootstrap" — the fail-closed direction.

    ``MIN_LABELS`` is imported from the policy rather than re-hard-coded, and
    ``abstain_gate`` (which now holds only the LTT bar) is imported lazily so
    the common path, where nothing is labelled yet, pays nothing.
    """
    if len(rows) < escalation_policy.MIN_LABELS:
        return None
    try:
        from mini_ork.gates import abstain_gate
        # (min_confidence, uncertifiable, accepted_n, accepted_wrong, bound)
        bar, uncertifiable = abstain_gate._ltt_min_confidence(
            rows, abstain_gate.alpha())[:2]
    except Exception:  # noqa: BLE001 — uncomputable bar means uncertifiable
        return None
    return None if uncertifiable else float(bar)


def gather_inputs(home: Path, run_id: str, diagnosis: dict) -> EscalationInputs:
    """Build the frozen policy's ``EscalationInputs`` for this diagnosis.

    ``layer`` / ``source`` / ``confidence`` are passed through from the
    diagnosis **unchanged** — the policy's rule-vs-model branch and its bar
    comparison are only reachable if they are.

    ``labelled_count`` / ``calibrated_bar`` come from the SDE-3b2 diagnosis
    ledger (``mini_ork.stores.diagnosis_ledger``) over labelled **model**
    diagnoses: the count of rows that can calibrate, and — once it reaches
    ``escalation_policy.MIN_LABELS`` — the Learn-Then-Test bar they certify.
    Below that gate, and whenever the diagnoser is uncertifiable, the bar is
    ``None`` and the policy's bootstrap bar is what applies. The ledger is a
    read here and nothing more: this module never writes it (``escalate`` does).

    ``run_id`` is carried for the caller's context; it is deliberately not
    load-bearing here (this run's own epic has not been filed yet when the
    inputs are gathered).
    """
    db_path = Path(home) / "state.db"
    try:
        confidence = float(diagnosis.get("confidence") or 0.0)
    except (TypeError, ValueError):
        confidence = 0.0
    now = int(time.time())
    rows = _labelled_model_rows(db_path)
    return EscalationInputs(
        layer=str(diagnosis.get("layer") or ""),
        source=str(diagnosis.get("source") or ""),
        confidence=confidence,
        calibrated_bar=_calibrated_bar(rows),
        labelled_count=len(rows),
        fix_epics_last_24h=_self_driving_promotions_since(db_path, now - _WINDOW_S),
        daily_cap=_daily_cap(),
        signature_fix_failed_before=_signature_fix_failed_before(
            db_path, diagnosis_fingerprint(diagnosis)),
        collapse_tripped=_collapse_tripped(db_path, _breaker_n()),
    )


# ── side effects ─────────────────────────────────────────────────────────────


def _record_waiting_on_fix(home: Path, run_id: str, res: Any) -> None:
    """Park the run: ``repair.json`` records the epic it is waiting on.

    ``triaged`` is set so ``auto_repair._triage_on_give_up`` never files a second
    report for this run, and so a later give-up on the same run does not
    double-file the failure this epic already owns. The policy's ``reason`` is
    not stored here: ``apply`` records it on the attempt (``repair.json``
    ``attempts[].reason``) like every other action.
    """
    run_dir = _run_dir(home, run_id)
    repair = _load_repair(run_dir)
    repair["state"] = "waiting_on_fix"
    repair["fix_epic_id"] = res.epic_id
    repair["fix_bug_id"] = res.bug_id
    repair["triaged"] = True
    _write_repair(run_dir, repair)


def _emit_escalate_event(home: Path, run_id: str, diagnosis: dict,
                         decision: EscalationDecision, res: Any) -> None:
    """Append the ``auto_repair.escalate`` row to ``run_events`` (best effort).

    A direct read-write ``sqlite3`` connection: ``web.db.StateDB`` opens
    ``PRAGMA query_only = ON``, so an INSERT through it fails silently — the
    same reason ``auto_repair._emit_run_event`` does this by hand.
    """
    db_path = Path(home) / "state.db"
    if not db_path.is_file():
        return
    now = int(time.time())
    payload = {
        "action": "escalate",
        "decision": {"escalate": bool(decision.escalate), "reason": decision.reason},
        "run_id": run_id,
        "layer": diagnosis.get("layer"),
        "stage": diagnosis.get("stage"),
        "node": diagnosis.get("node"),
        "source": diagnosis.get("source"),
        "confidence": diagnosis.get("confidence"),
        "fix_epic_id": res.epic_id,
        "fix_bug_id": res.bug_id,
        "reason": decision.reason,
    }
    try:
        con = _connect(db_path)
        try:
            # ``run_events.event_id`` is UNIQUE and the timestamp is only
            # second-resolution, so the id carries this run's escalate ordinal
            # (as ``auto_repair``'s carries its attempt number) rather than the
            # clock alone.
            row = con.execute(
                "SELECT COUNT(*) FROM run_events WHERE run_id=? AND event_type=?",
                (run_id, ESCALATE_EVENT),
            ).fetchone()
            seq = int(row[0] or 0) + 1
            con.execute(
                "INSERT INTO run_events(event_id, run_id, event_type, payload_json, created_at) "
                "VALUES (?,?,?,?,?)",
                (f"evt-escalate-{run_id}-{seq}", run_id, ESCALATE_EVENT,
                 json.dumps(payload), now),
            )
            con.commit()
        finally:
            con.close()
    except sqlite3.Error:  # noqa: BLE001 — the event log is a side channel
        pass


def _enqueue_oversight(home: Path, run_id: str, res: Any, reason: str) -> None:
    """Tell the oversight inbox once — non-blocking, best effort."""
    try:
        from mini_ork.gates import oversight_inbox
    except Exception:  # noqa: BLE001 — the notice must never fail an escalation
        return
    try:
        oversight_inbox.enqueue(
            _OVERSIGHT_GATE,
            _OVERSIGHT_FEATURE,
            context={"run_id": run_id, "fix_epic_id": res.epic_id, "reason": reason},
            db_path=str(Path(home) / "state.db"),
        )
    except Exception:  # noqa: BLE001
        return


def _record_ledger(home: Path, run_id: str, diagnosis: dict, *,
                   action: str, fix_epic_id: str | None = None) -> None:
    """Record the acted-on diagnosis in the SDE-3b2 ledger (best effort).

    ``action`` is what the loop *did*: ``escalate`` when a fix epic was opened,
    ``human`` when the policy refused or the fix could not be filed. Refusals
    are recorded too — SDE-4 can only measure the diagnoser's error rate if it
    sees both outcomes, not only the escalations that panned out.

    The never-raise wrapper is not redundant with ``escalate``'s own ``c:0``
    envelope: without it a broken ledger DB path would turn a *successful*
    escalation into an ``escalation error`` and cost the run its fix epic. The
    local import mirrors ``_enqueue_oversight``'s — this is a side channel, not
    a load-bearing dependency of the decision.
    """
    try:
        from mini_ork.stores import diagnosis_ledger
        diagnosis_ledger.record(
            Path(home) / "state.db", run_id, diagnosis,
            action=action, fix_epic_id=fix_epic_id,
        )
    except Exception:  # noqa: BLE001 — the ledger must never fail an escalation
        return


# ── the action ───────────────────────────────────────────────────────────────


def escalate(home: Path, run_id: str, diagnosis: dict) -> dict[str, Any]:
    """Escalate an engine fault to a linked fix epic. Never raises (``c:0``).

    Returns a dict with ``escalated`` and, in every case, a human-readable
    ``reason``:

      * ``{"escalated": True, "fix_epic_id", "fix_bug_id", "kickoff_path"}`` —
        the policy admitted it, the bug was filed, the ``framework-edit`` epic
        was promoted, the run recorded ``waiting_on_fix``, the event was logged
        and the oversight item queued. **No recover is spawned** (``escalate`` is
        not in ``auto_repair._SPAWNABLE``): the run waits for the fix epic.
      * ``{"escalated": False, "reason": <policy reason>}`` — the frozen policy
        refused (cap, breaker, GRAO, bar). Nothing is filed; the reason is meant
        to be recorded verbatim.
      * ``{"escalated": False, "reason": "escalation error: ..."}`` — anything
        else went wrong. The run falls back to the human hand-off.
    """
    home = Path(home)
    run_id = str(run_id)
    diagnosis = diagnosis if isinstance(diagnosis, dict) else {}
    try:
        decision = should_escalate(gather_inputs(home, run_id, diagnosis))
        if not decision.escalate:
            # The refusal is an outcome too: the loop acted on this diagnosis by
            # handing the run to a human, and the ledger's calibration needs it.
            _record_ledger(home, run_id, diagnosis, action="human")
            return {"escalated": False, "reason": decision.reason,
                    "fix_epic_id": None, "fix_bug_id": None}

        res = file_diagnosis(
            run_id,
            diagnosis,
            home=str(home),
            db=str(home / "state.db"),
            root=context_env("MINI_ORK_ROOT") or None,
            promote=True,
        )
        if not res.epic_id:
            # A bug row without an epic is not a fix: parking the run on it
            # would leave it waiting for something that does not exist.
            # ``apply`` turns this into the ordinary ``human`` hand-off too.
            _record_ledger(home, run_id, diagnosis, action="human")
            return {"escalated": False,
                    "reason": f"escalation error: no fix epic was opened ({res.reason or 'promote produced no epic'})",
                    "fix_epic_id": None, "fix_bug_id": res.bug_id}

        _record_ledger(home, run_id, diagnosis, action="escalate",
                       fix_epic_id=res.epic_id)
        _record_waiting_on_fix(home, run_id, res)
        _emit_escalate_event(home, run_id, diagnosis, decision, res)
        _enqueue_oversight(home, run_id, res, decision.reason)
        return {
            "escalated": True,
            "reason": decision.reason,
            "fix_epic_id": res.epic_id,
            "fix_bug_id": res.bug_id,
            "kickoff_path": res.kickoff_path,
        }
    except Exception as exc:  # noqa: BLE001 — hard constraint c:0
        return {"escalated": False, "reason": f"escalation error: {exc}",
                "fix_epic_id": None, "fix_bug_id": None}
