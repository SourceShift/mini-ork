"""Auto-repair — a failed run fixes itself in a bounded feedback loop.

A failed run used to wait for a human click (``mini-ork recover``) or be
abandoned. This module is the *policy* + *apply* half of restoring the user
rule "instead of accepting that these failed … always revive and fix":

    diagnose → pick a repair → apply it → resume the SAME run from the failed
    node → repeat, bounded → hand over to the human only when stuck.

A diagnosis that blames the *engine* (``layer == "engine"``, ``routes_to ==
"fix_epic"``) escapes that loop: ``decide`` returns ``escalate`` and
``mini_ork.recovery.escalation`` opens a linked ``framework-edit`` fix epic, parks
the run in ``waiting_on_fix`` and waits for it instead of handing over — see that
module for the admission policy (``escalation_policy.should_escalate``, frozen)
and :data:`_SPAWNABLE` for why nothing is spawned.

A *parked* run (``repair.json`` state ``waiting_on_fix``) is INERT to this loop:
the fix epic owns the failure, so ``decide`` returns ``none`` citing the epic
before every other rule and ``apply`` leaves ``repair.json`` byte-identical and
spawns nothing. Until SDE-4 revives the run, every later pass over the same
failure is a pure no-op.

``decide`` is pure policy (no side effects): it reads the retry hint, the run's
lifecycle events, the level report and the ``repair.json`` history and returns a
decision dict. ``apply`` performs it: it writes the revise/prove feedback file,
appends the attempt to ``repair.json``, emits a ``run_events`` row, and — when
there is something to resume — spawns a detached ``mini-ork recover``.

The intended loop is a chain of detached processes: the ``run`` / ``recover``
flow that owns a terminal failure calls :func:`maybe_repair`; when it spawns a
``recover``, that child inherits ``MO_AUTO_REPAIR_ATTEMPT`` and, when *it* fails
again, decides the next attempt through the same hooks. Attempts are bounded by
``MO_AUTO_REPAIR_MAX`` (default 2) and a spend ceiling
``MO_AUTO_REPAIR_BUDGET_USD`` (default 10).

Default-on, explicit: the ``run`` flow publishes ``MO_AUTO_REPAIR=1`` before
execute when the operator left it unset. ``MO_AUTO_REPAIR=0`` is never
overridden and turns the whole loop off (``decide`` returns ``action="none"``).
The manual ``mini-ork repair`` CLI treats unset as *on*.

Failure-triage sequencing: ``mini_ork.triage`` escalates a failure to a separate
framework-edit epic. Auto-repair fixes the SAME run first, so it owns the trigger
and calls triage exactly once on *give-up* (and only when ``MO_FAILURE_TRIAGE=1``
opts in), recording ``"triaged"`` on every give-up so absence is explicit.

Hand-off: the ``run`` / ``recover`` flows spawn their repair from inside the
lifecycle that still OWNS the run record (``main._close_run_record`` runs in the
caller's ``finally``, after reflect). A child that dispatched immediately would
have its ``executing`` status flipped to ``failed`` by that teardown, so those
callers mark the spawn with ``MO_AUTO_REPAIR_WAIT_PID`` and the child blocks in
:func:`wait_for_spawner` until the spawner has exited. A spawn from a process
that does not own the run (``repair --sweep``, the board) is not marked. A
spawned recover that bails out before dispatching is handed to the human by
:func:`note_stuck` rather than left with ``state='repairing'`` and no owner.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from mini_ork import run_status as _run_status
from mini_ork.context import context_env

# ── tuning (env-overridable) ────────────────────────────────────────────────
_DEFAULT_MAX_ATTEMPTS = 2
_DEFAULT_BUDGET_USD = 10.0
_DEFAULT_ESCALATE_LANE = "opus"
# Safety valve for ``wait_for_spawner``: the spawner is a run lifecycle that may
# still be reflecting (``MO_REFLECT_TIMEOUT_SECONDS`` = 360 s by default), so the
# bound only exists to keep a pathological spawner from stalling a repair forever.
_DEFAULT_WAIT_S = 1800.0

_ATTEMPT_FILE = "repair.json"
_TERMINAL_FAILED = tuple(sorted(_run_status.UNSUCCESSFUL))

# Child env markers set by ``_spawn_recover``: the attempt number this child is
# executing, the run it is repairing (``note_stuck`` needs both), and the pid of
# the process that spawned it (lifecycle-owned spawns only — see
# ``wait_for_spawner``).
_ATTEMPT_ENV = "MO_AUTO_REPAIR_ATTEMPT"
_RUN_ID_ENV = "MO_AUTO_REPAIR_RUN_ID"
_WAIT_PID_ENV = "MO_AUTO_REPAIR_WAIT_PID"

# Explicit failing signals that mean "the environment/transport died, not the
# change". The kickoff's full vocabulary (killed, sigterm, rc_137, …) is kept so
# a future executor that emits it is classified; ``timeout`` / ``cost_limit`` are
# the values this codebase actually emits today (``finish_reason_for_failure``).
_INFRA_FINISH = frozenset({
    "timeout", "cost_limit", "killed", "sigterm", "rc_137", "rc_143",
    "network", "provider_error", "overloaded", "rate_limited",
})
# ``node_attempts.failure_class`` values that mean provider/infra trouble.
_INFRA_FAILURE_CLASSES = frozenset({"infra_interrupt", "provider_limit"})

# Hint ``needs_change.kind`` values the *owner* must fix — auto-repair scans
# these for a human rather than spawning anything.
_OWNER_KINDS = frozenset({"environment", "credentials", "budget"})

# Reviewer verdicts that mean "the change needs another round".
_FAIL_VERDICTS = frozenset({
    "needs_revision", "reject", "fail", "request_changes", "escalate",
    "failed", "crash",
})

# Actions that resume the run through ``mini-ork recover``.
_SPAWNABLE = frozenset({"revise", "prove", "lane", "infra", "reverify"})

# Re-entrancy guard: run ids this *process* already spawned a repair for. The
# loop is a chain of detached processes (a spawned recover that fails again
# spawns the next one), so a process that has already handed a run to a child
# must not spawn a second one for the same failure.
_SPAWNED_THIS_PROCESS: set[str] = set()


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _env_int(key: str, default: int) -> int:
    try:
        return int(context_env(key, str(default)) or default)
    except (TypeError, ValueError):
        return default


def _env_float(key: str, default: float) -> float:
    try:
        return float(context_env(key, str(default)) or default)
    except (TypeError, ValueError):
        return default


def _max_attempts() -> int:
    return _env_int("MO_AUTO_REPAIR_MAX", _DEFAULT_MAX_ATTEMPTS)


def _budget_usd() -> float:
    return _env_float("MO_AUTO_REPAIR_BUDGET_USD", _DEFAULT_BUDGET_USD)


def _escalate_lane() -> str:
    return context_env("MO_AUTO_REPAIR_ESCALATE_LANE", _DEFAULT_ESCALATE_LANE) or _DEFAULT_ESCALATE_LANE


# ── small readers ────────────────────────────────────────────────────────────


def _run_dir(home: Path, run_id: str) -> Path:
    return Path(home) / "runs" / run_id


def _read_json(path: Path) -> Any:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None


def _load_repair(run_dir: Path) -> dict[str, Any]:
    """``repair.json`` as a dict — a fresh skeleton when absent/unreadable."""
    data = _read_json(run_dir / _ATTEMPT_FILE)
    if not isinstance(data, dict):
        data = {}
    attempts = data.get("attempts")
    if not isinstance(attempts, list):
        data["attempts"] = []
    return data


def _write_repair(run_dir: Path, data: dict[str, Any]) -> None:
    """Best-effort atomic write of ``repair.json`` (temp + ``os.replace``)."""
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


def _recipe_of(home: Path, run_id: str) -> str:
    """The run's recipe from ``run_profile.json``, else the ``task_runs`` row."""
    profile = _read_json(_run_dir(home, run_id) / "run_profile.json")
    if isinstance(profile, dict) and profile.get("recipe"):
        return str(profile["recipe"])
    try:
        from mini_ork.web.db import db_for
        db = db_for(Path(home))
        if db.has_table("task_runs"):
            row = db.row("SELECT recipe FROM task_runs WHERE id = ? LIMIT 1", (run_id,))
            if row and row.get("recipe"):
                return str(row["recipe"])
    except Exception:  # noqa: BLE001 — recipe is advisory; never raise
        pass
    return ""


def _run_status(home: Path, run_id: str) -> str:
    from mini_ork.recovery import retry_hint
    return retry_hint._current_run_status(Path(home), run_id)


def _is_withheld(run_dir: Path) -> bool:
    """True when the run's publish was *withheld* — the level report abstains.

    ``publisher.py`` forces ``status='failed'`` when it withholds (``abstain``),
    so a withheld run is already a failed run and rule 1 admits it. But a run
    whose level report still abstains while ``task_runs.status`` reads
    ``published`` — a later status rewrite, e.g. a manual republish that
    bypassed the level gate — is that *same* withheld run. Rule 5 (reverify)
    owns it, so rule 1 must not short-circuit it as a non-failed run. Reuses
    ``task_state.withheld_publish`` so it can never disagree with the retry
    hint, the run tile or the outcome page about "merely withheld".
    """
    try:
        from mini_ork.acp.task_state import withheld_publish
        return withheld_publish(Path(run_dir), None) is not None
    except Exception:  # noqa: BLE001 — a missing report is simply "not withheld"
        return False


def _events(home: Path, run_id: str) -> list[dict[str, Any]] | None:
    from mini_ork.recovery import retry_hint
    return retry_hint._run_node_events(Path(home), run_id)


def _publisher_finished_done(events: list[dict[str, Any]] | None) -> bool:
    """True when the publisher's LATEST ``node_end`` carries ``finish_reason == "done"``.

    A publisher that finished ``done`` DELIVERED the run: it is published
    whatever ``verdict.json``'s level report still says, so the loop must not
    treat it as a merely-withheld run and spawn a re-verify (the recover
    incident: auto-repair re-verified an already-published run). Only a
    publisher that ended ``levels_unverified`` / ``publish_abstain`` really did
    withhold — that override stays.

    Reads the events ``decide`` already loaded; ``None`` (no lifecycle
    available) is never a "published" claim.
    """
    if not events:
        return False
    from mini_ork.acp.task_state import _parse_payload
    latest = None
    for ev in events:
        if ev.get("event_type") != "node_end":
            continue
        payload = _parse_payload(ev.get("payload_json"))
        node_id = str(payload.get("node_id") or ev.get("node_id") or "")
        node_type = str(payload.get("node_type") or ev.get("node_type") or "")
        if "publisher" not in (node_id, node_type):
            continue
        latest = str(payload.get("finish_reason") or "")
    return latest == "done"


def _workflow(home: Path, recipe: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    from mini_ork.recovery import retry_hint
    return retry_hint._recipe_workflow(Path(home), recipe)


def _node_type(home: Path, recipe: str, node_name: str | None) -> str:
    if not node_name:
        return ""
    nodes, _edges = _workflow(home, recipe)
    for n in nodes:
        if str(n.get("name")) == node_name:
            return str(n.get("type") or "")
    return ""


def _first_node_of_type(home: Path, recipe: str, node_type: str) -> str:
    """The first workflow node of ``node_type`` in topo order (``""`` on miss)."""
    from mini_ork.recovery import retry_hint
    nodes, edges = _workflow(home, recipe)
    by_name = {str(n.get("name")): n for n in nodes if n.get("name")}
    for name in retry_hint._topo_order(nodes, edges):
        if str((by_name.get(name) or {}).get("type") or "") == node_type:
            return name
    return ""


def _first_verifier_node(home: Path, recipe: str) -> str:
    from mini_ork.recovery import retry_hint
    nodes, edges = _workflow(home, recipe)
    return retry_hint._first_verifier_node(nodes, edges)


def _role_lane(home: Path, run_id: str, node_name: str) -> str:
    """The lane a workflow node ran on — from ``ide_pages.run._load`` (best effort)."""
    if not node_name:
        return ""
    try:
        from mini_ork.ide_pages import run as run_page
        run = run_page._load(Path(home), run_id)
        if run is None:
            return ""
        node = run.nodes.get(node_name)
        return str(getattr(node, "role_lane", "") or "") if node is not None else ""
    except Exception:  # noqa: BLE001 — lane is advisory
        return ""


def _failure_class(home: Path, run_id: str, node_name: str | None) -> str:
    from mini_ork.recovery import retry_hint
    try:
        if node_name:
            fc, _tail = retry_hint._failure_class_for_node(Path(home), run_id, node_name)
            if fc:
                return str(fc)
        fc, _node = retry_hint._failure_class_for_any(Path(home), run_id)
        return str(fc or "")
    except Exception:  # noqa: BLE001
        return ""


def _levels_reasons(run_dir: Path) -> dict[str, Any]:
    try:
        from mini_ork.acp.task_state import run_level_report
        report = run_level_report(run_dir)
        if isinstance(report, dict) and isinstance(report.get("levels_reasons"), dict):
            return report["levels_reasons"]
    except Exception:  # noqa: BLE001
        pass
    return {}


def _first_finding(run_dir: Path) -> str:
    """The first reviewer finding issue, else the first failing verifier check id."""
    for path in sorted(run_dir.glob("review-*.json")):
        data = _read_json(path)
        if not isinstance(data, dict):
            continue
        findings = data.get("findings")
        if isinstance(findings, list):
            for f in findings:
                if isinstance(f, dict) and f.get("issue"):
                    return str(f["issue"])[:200]
    for path in sorted(run_dir.glob("verifier_*.json")):
        data = _read_json(path)
        if isinstance(data, dict):
            for key in ("failed_checks", "checks"):
                checks = data.get(key)
                if isinstance(checks, list):
                    for c in checks:
                        if isinstance(c, dict) and (
                            c.get("status") in ("FAIL", "fail")
                            or c.get("pass") is False
                        ):
                            return str(c.get("id") or c.get("name") or "")[:200]
    return ""


def _escalation(run_dir: Path) -> str:
    """The reviewer's stated action when its latest verdict is ESCALATE, else "".

    ``review-verdict.json`` is rewritten by every reviewer/synth round, so it is
    the verdict that failed the run, not an earlier round's.
    """
    data = _read_json(run_dir / "review-verdict.json")
    if not isinstance(data, dict) or str(data.get("verdict") or "").strip().lower() != "escalate":
        return ""
    for key in ("next_action_recommended", "reasons", "notes"):
        value = data.get(key)
        if isinstance(value, list):
            value = next((str(item) for item in value if str(item).strip()), "")
        if value and str(value).strip():
            return str(value).strip()[:300]
    return "the reviewer asked for a person"


def _signature(failed_node: str | None, reason: str, finding: str) -> str:
    raw = f"{failed_node or ''}|{reason or ''}|{finding or ''}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]


def _decision(action: str, *, from_node: str | None = None,
              lanes: dict[str, str] | None = None, ack_change: bool = False,
              feedback: str = "", reason: str = "", signature: str = "",
              stop: bool = False, fix_epic_id: str | None = None) -> dict[str, Any]:
    """A decision. ``stop`` marks a *stop rule* (attempts exhausted, budget,
    no progress) — a give-up the loop reached itself, as opposed to a first
    failure it hands over immediately. See ``apply``'s ``repair.json`` state.

    ``fix_epic_id`` is ADDITIVE and carried only by the parked ``none`` decision
    (the run is ``waiting_on_fix``, so the fix epic — not this loop — owns the
    failure). It never changes an action; it names the epic the reason cites.
    """
    return {
        "action": action,
        "from_node": from_node,
        "lanes": dict(lanes or {}),
        "ack_change": bool(ack_change),
        "feedback": feedback,
        "reason": reason,
        "signature": signature,
        "stop": bool(stop),
        "fix_epic_id": fix_epic_id,
    }


# ── policy ───────────────────────────────────────────────────────────────────


def _escalatable(diagnosis: dict[str, Any] | None) -> bool:
    """True when the diagnosis blames the engine *and* routes to a fix epic.

    Both halves are required: ``layer == "engine"`` is who owns the failure, and
    ``routes_to == "fix_epic"`` is the diagnosis's own recommendation that the
    remedy is a change to mini-ork rather than to the run. A missing/invalid
    diagnosis (``decide`` computes it best-effort) never escalates.
    """
    if not isinstance(diagnosis, dict):
        return False
    from mini_ork.recovery.diagnose import LAYER_ENGINE, ROUTE_FIX_EPIC
    return (str(diagnosis.get("layer") or "") == LAYER_ENGINE
            and str(diagnosis.get("routes_to") or "") == ROUTE_FIX_EPIC)


def _wants_model_diagnosis(decision: dict[str, Any],
                           diagnosis: dict[str, Any] | None) -> bool:
    """May ``decide`` spend a model call on this run's diagnosis?

    Only when the rule set came back ``unclassified`` AND the rule policy is
    live for the run. A ``none`` decision is every reason the loop is inert —
    ``MO_AUTO_REPAIR=0``, a run parked on its fix epic, a run that is not failed,
    a published run — so none of those ever dispatches, and a green run's
    trivially-unclassified diagnosis costs nothing. ``MO_MODEL_DIAGNOSER``
    (default on — the operator's aggressive-escalation decision) owns the
    feature; ``MINI_ORK_DRY_RUN=1`` keeps a dry run off the provider.
    """
    if context_env("MO_MODEL_DIAGNOSER", "1") == "0":
        return False
    if context_env("MINI_ORK_DRY_RUN", "0") == "1":
        return False
    if str(decision.get("action") or "none") == "none":
        return False
    from mini_ork.recovery.diagnose import LAYER_UNCLASSIFIED
    return (isinstance(diagnosis, dict)
            and str(diagnosis.get("layer") or "") == LAYER_UNCLASSIFIED)


def decide(home: Path, run_id: str) -> dict[str, Any]:
    """Pure policy — the repair decision for ``run_id`` (no side effects).

    Returns ``{"action", "from_node", "lanes", "ack_change", "feedback",
    "reason", "signature", "diagnosis", "fix_epic_id"}``. ``action`` is one of
    ``none``, ``human``, ``lane``, ``infra``, ``reverify``, ``prove``,
    ``revise`` or ``escalate``. First match wins; the rule order is the
    kickoff's policy table, prefixed by two rules that outrank all of it: the
    ``MO_AUTO_REPAIR=0`` opt-out, then the parked-run guard (a run in
    ``waiting_on_fix`` decides ``none``, citing its fix epic).

    ``escalate`` is the self-driving action: the diagnosis says the *engine* is
    at fault and routes to a fix epic, so instead of a revise round the loop
    opens a linked ``framework-edit`` epic and waits on it. It is not a repair of
    this run (see ``_SPAWNABLE``), and the frozen operator policy
    (``escalation_policy.should_escalate``) is what admits it — ``decide`` only
    routes to it, gated on ``MO_AUTO_ESCALATE`` (default on, only the literal
    ``"0"`` off) and the diagnosis's ``layer``/``routes_to``.

    ``diagnosis`` is ADDITIVE and never load-bearing: the typed
    layer/stage/node naming of *who owns this failure*
    (:func:`mini_ork.recovery.diagnose.diagnose`). It refines the rule-8
    reason text and rides along into ``repair.json``; it changes no action.

    Computed with ``write=False`` so ``decide`` stays side-effect-free — a
    ``--dry-run`` sweep must leave the run dir untouched. :func:`apply` is the
    half that persists it (``<run_dir>/diagnosis.json``).

    When the rule set comes back ``unclassified`` a *model* may diagnose the run
    (``MO_MODEL_DIAGNOSER``, default on — ``diagnose(..., allow_model=True)``).
    The rule policy runs FIRST, so ``MO_AUTO_REPAIR=0``, the parked-run guard
    and every other inert case have already answered ``none`` before any
    dispatch (see ``_wants_model_diagnosis``); a grounded model diagnosis then
    re-runs the policy, which is how an engine-owned failure reaches
    ``escalate``. The rule result stands whenever the model declines. The model
    is asked at most once per run: ``apply`` persists its diagnosis, and the next
    pass reuses it.
    """
    home = Path(home)
    run_id = str(run_id)
    try:
        from mini_ork.recovery.diagnose import SOURCE_MODEL, diagnose
        diagnosis: dict[str, Any] | None = diagnose(home, run_id, write=False)
    except Exception:  # noqa: BLE001 — a diagnosis failure must never break the loop
        diagnosis = None
    decision = _decide_policy(home, run_id, diagnosis)
    if diagnosis is not None and _wants_model_diagnosis(decision, diagnosis):
        try:
            refined = diagnose(home, run_id, write=False, allow_model=True)
        except Exception:  # noqa: BLE001 — the model diagnoser must never break the loop
            refined = None
        if isinstance(refined, dict) and str(refined.get("source") or "") == SOURCE_MODEL:
            diagnosis = refined
            decision = _decide_policy(home, run_id, diagnosis)
    decision["diagnosis"] = diagnosis
    return decision


def _decide_policy(home: Path, run_id: str,
                   diagnosis: dict[str, Any] | None) -> dict[str, Any]:
    """``decide``'s rule table. ``diagnosis`` is advisory (rule 8's reason)."""
    home = Path(home)
    if context_env("MO_AUTO_REPAIR", "1") == "0":
        return _decision("none", reason="auto-repair disabled (MO_AUTO_REPAIR=0)")

    run_dir = _run_dir(home, run_id)
    status = _run_status(home, run_id)
    # The run is PARKED on a fix epic (SDE-3a's ``escalation.escalate`` wrote
    # ``waiting_on_fix``): the epic owns the failure now, so this loop is inert.
    # Placed before EVERY other rule — rule 1 included, so a parked run is not
    # even re-classified by its status — because a parked run must not reach the
    # stop rules (a second same-signature pass would record a "no progress"
    # give-up on a run that is waiting, not stalling) nor the escalate rule (it
    # already has its epic; a second one would double-file the failure). Only the
    # ``MO_AUTO_REPAIR=0`` opt-out above outranks it. ``apply`` mirrors this with
    # a byte-identical short-circuit; SDE-4 revives the run by clearing the state
    # when the epic lands — that is the only thing that un-parks it.
    repair = _load_repair(run_dir)
    if str(repair.get("state") or "") == "waiting_on_fix":
        fix_epic_id = str(repair.get("fix_epic_id") or "")
        return _decision(
            "none",
            reason=f"waiting on fix epic {fix_epic_id}" if fix_epic_id
                   else "waiting on a fix epic",
            fix_epic_id=fix_epic_id or None,
        )
    events = _events(home, run_id)
    # Rule 1 is "the run is not failed/rolled_back". A withheld publish counts
    # as failed even when the status flag disagrees (see ``_is_withheld``): the
    # loop must not go blind to a withheld run that a status rewrite republished.
    if status not in _TERMINAL_FAILED and not _is_withheld(run_dir):
        return _decision("none", reason=f"run is not failed/rolled_back (status={status or 'unknown'})")
    # ...but a publisher whose LATEST node_end finished ``done`` PUBLISHED the
    # run — whatever ``verdict.json``'s level report still says. Without this a
    # published run whose level report lingered at ``abstain`` was re-verified
    # by the loop. Only a publisher that ended ``levels_unverified`` /
    # ``publish_abstain`` (``_publisher_finished_done`` is False) stays withheld.
    if _publisher_finished_done(events):
        return _decision("none",
                         reason="the publisher finished done — the run is published")

    recipe = _recipe_of(home, run_id)
    hint = _load_hint(home, run_id)
    failing = None
    if events is not None:
        from mini_ork.acp.task_state import _failing_node
        failing = _failing_node(events)
    failed_node = failing[0] if failing else None
    fail_reason = failing[1] if failing else ""

    withheld: list[str] | None = None
    if events is not None:
        from mini_ork.acp.task_state import withheld_publish
        withheld = withheld_publish(run_dir, events)

    # ``repair`` is the snapshot the parked guard above already read: ``decide``
    # is pure, so re-loading here could only disagree with it. One read per
    # decide.
    attempts = list(repair.get("attempts") or [])

    finding = _first_finding(run_dir)
    sig_reason = fail_reason or (f"withheld:{','.join(withheld)}" if withheld else "")
    signature = _signature(failed_node, sig_reason, finding)

    nc = hint.get("needs_change") if isinstance(hint, dict) else None
    nc = nc if isinstance(nc, dict) else None
    hint_kind = str(nc.get("kind") or "") if nc else ""

    # 2. Stop: human needed.
    if len(attempts) >= _max_attempts():
        return _decision("human", from_node=failed_node, signature=signature, stop=True,
                         reason=f"repair attempts exhausted ({len(attempts)}/{_max_attempts()})")
    spent = _cost_since_first_repair(home, run_id, attempts)
    if spent is not None and spent > _budget_usd():
        return _decision("human", from_node=failed_node, signature=signature, stop=True,
                         reason=f"repair budget exceeded (${spent:.2f} > ${_budget_usd():.2f})")
    # "No progress" is the same failure signature twice. It does NOT apply to a
    # withheld run: the second identical attempt deliberately escalates
    # reverify → prove (a *different* action), which is progress, not a stall.
    if attempts and str(attempts[-1].get("signature") or "") == signature \
            and not (withheld and not failed_node):
        return _decision("human", from_node=failed_node, signature=signature, stop=True,
                         reason="no progress — the same failure signature repeats")

    # 2.5 The engine is at fault → escalate to a fix epic (SDE-3a). A revise
    # round re-implements the change against a failure the framework owns, which
    # is the same wrong turn the reviewer-escalated branch (5.5) avoids; the run
    # is parked in ``waiting_on_fix`` instead. Placed AFTER the stop rules so an
    # exhausted/budget/short-circuiting run still gives up rather than opening a
    # new epic, and BEFORE the lane rule so an engine fault outranks a dead lane.
    # ``decide`` routes only: whether the escalation is *admitted* (daily cap,
    # breaker, calibrated bar) is the frozen policy's call, made in ``apply``.
    if context_env("MO_AUTO_ESCALATE", "1") != "0" and _escalatable(diagnosis):
        stage = str(diagnosis.get("stage") or "?")
        node = str(diagnosis.get("node") or "")
        return _decision(
            "escalate",
            from_node=failed_node or (node or None),
            reason=f"engine fault at {stage}{'/' + node if node else ''}"
                   " — escalate to a fix epic",
            signature=signature,
        )

    # 3. Dead lane → switch it.
    if hint_kind == "lane" and isinstance(nc.get("suggestions"), list) and nc["suggestions"]:
        first = nc["suggestions"][0]
        lane = str((first or {}).get("lane") or "")
        alias = str(nc.get("alias") or "")
        if lane and alias:
            return _decision(
                "lane",
                from_node=str((hint or {}).get("from_node") or failed_node or "") or None,
                lanes={alias: lane},
                reason=f"lane {alias} is unavailable — switch to {lane}",
                signature=signature,
            )

    # 3.5 Interrupted mid-node (``needs_change.kind == "interrupted"``, retry
    # hint case 1.6) → resume the dangling node. Nothing needs changing:
    # re-running the step IS the fix, so it is a plain ``infra`` resume from the
    # hint's node. A run a *person* stopped is not auto-resumed: ``board kill``
    # appends ``"killed-by-user"`` to ``task_runs.notes`` (``web/control.py``
    # ``_writeback_terminal``). Notes are APPEND-ONLY (``notes = COALESCE(notes
    # || '; ', '') || ?``) and never reset, so a note written BEFORE the kill
    # pushes the marker off the start of the column — the test is per note, not
    # a prefix on the whole string (cf. ``metrics_sdd``).
    if hint_kind == "interrupted":
        killed_by_user = False
        try:
            from mini_ork.web.db import db_for
            db = db_for(home)
            if db.has_table("task_runs"):
                row = db.row("SELECT notes FROM task_runs WHERE id = ? LIMIT 1", (run_id,))
                notes = str((row or {}).get("notes") or "")
                killed_by_user = any(
                    seg.startswith("killed-by-user") for seg in notes.split("; ")
                )
        except Exception:  # noqa: BLE001 — an unreadable notes column: treat as live
            killed_by_user = False
        if killed_by_user:
            return _decision("none", reason="killed by the user — not auto-resumed",
                             signature=signature)
        node = str((hint or {}).get("from_node") or failed_node or "") or None
        return _decision(
            "infra",
            from_node=node,
            reason=f"interrupted during {node or '?'} — resume it",
            signature=signature,
        )

    # 3.6 A run a person stopped before it entered a node (``kind ==
    # "stopped"``, retry hint case 1.7) is never auto-resumed. The mid-node
    # kill is caught above by the ``interrupted`` branch's ``killed-by-user``
    # veto; this is its sibling for the run that died with no node at all.
    if hint_kind == "stopped":
        return _decision("none", reason="stopped by the user — not auto-resumed",
                         signature=signature)

    # 3.7 Planned but never executed (``kind == "not-started"``, retry hint case
    # 4.5): no node ran, so there is nothing to change — re-entering at the
    # first node IS the fix.
    if hint_kind == "not-started":
        node = str((hint or {}).get("from_node") or "") or None
        return _decision(
            "infra",
            from_node=node,
            reason=f"no node ever started{' — resume from ' + node if node else ''}",
            signature=signature,
        )

    # 4. Infra / provider trouble → same node, same lane.
    if _is_infra(home, run_id, hint, nc, failed_node, fail_reason):
        return _decision(
            "infra",
            from_node=failed_node or (str((hint or {}).get("from_node") or "") or None),
            reason=f"infrastructure failure ({fail_reason or hint_kind or 'provider'})",
            signature=signature,
        )

    # 5. Withheld publish with no failing node → re-verify; a repeat → prove.
    if withheld and not failed_node:
        prior_reverify = any(str(a.get("action")) == "reverify" for a in attempts)
        if prior_reverify:
            impl = _first_node_of_type(home, recipe, "implementer")
            level = withheld[0]
            lr = str(_levels_reasons(run_dir).get(level, ""))
            feedback = (
                f"Every check passed but level `{level}` is UNVERIFIED (`{lr}`). "
                "Add the smallest test that exercises the change so the level can "
                "be proven; change nothing else."
            )
            return _decision("prove", from_node=impl or None, feedback=feedback,
                             reason=f"withheld level {level} — prove it with a test",
                             signature=signature)
        return _decision(
            "reverify",
            from_node=_first_verifier_node(home, recipe) or None,
            reason=f"withheld publish ({', '.join(withheld)} unverified) — re-verify",
            signature=signature,
        )

    node_type = _node_type(home, recipe, failed_node)

    # 5.5 The reviewer ESCALATED: it judged the code right and the wall one only
    # a person can clear (an unapplied migration, a spec or scope decision). A
    # revise round re-implements code the reviewer called correct and meets the
    # same wall — the researcher home's cjd-dfy-b3 / bhq-p7 and the RVI panels of
    # c2 / w1 / m2a each burned one before reaching a human.
    if node_type == "reviewer":
        escalation = _escalation(run_dir)
        if escalation:
            return _decision("human", from_node=failed_node, signature=signature,
                             reason=f"the reviewer escalated: {escalation}")

    # 6. Code: revision exhausted → send the implementer back with the errors.
    if hint_kind == "code" or node_type in ("reviewer", "verifier", "eval") \
            or str(fail_reason).lower() in _FAIL_VERDICTS:
        impl = _first_node_of_type(home, recipe, "implementer")
        prior_revise = sum(1 for a in attempts if str(a.get("action")) == "revise")
        lanes: dict[str, str] = {}
        if prior_revise >= 1:
            role_lane = _role_lane(home, run_id, impl)
            if role_lane:
                lanes = {role_lane: _escalate_lane()}
        return _decision("revise", from_node=impl or None, lanes=lanes,
                         ack_change=True, reason="the change needs a revision",
                         signature=signature)

    # 7. Owner-only fixes the loop cannot make.
    if hint_kind in _OWNER_KINDS:
        return _decision("human", from_node=failed_node, signature=signature,
                         reason=f"owner-only fix required ({hint_kind})")

    # 8. Anything else — hand over with the diagnosis summary when it localised
    # the failure (a named layer/stage/node reads better than a bare hint
    # summary), else the hint summary + last log lines. The diagnosis refines
    # the REASON only: the action and the stop rules are untouched.
    from mini_ork.recovery.diagnose import LAYER_UNCLASSIFIED
    summary = str((hint or {}).get("needs_change", {}).get("summary", "")) \
        if isinstance(hint, dict) and isinstance(hint.get("needs_change"), dict) else ""
    reason = summary or str((hint or {}).get("notes") or "unclassified failure")
    if isinstance(diagnosis, dict) and diagnosis.get("summary") \
            and str(diagnosis.get("layer") or "") not in ("", LAYER_UNCLASSIFIED):
        reason = str(diagnosis["summary"])
    return _decision("human", from_node=failed_node or (str((hint or {}).get("failed_node") or "") or None),
                     signature=signature, reason=reason or "unclassified failure")


def _load_hint(home: Path, run_id: str) -> dict[str, Any] | None:
    try:
        from mini_ork.recovery import retry_hint
        hint = retry_hint.load_or_compute(Path(home), run_id, write=False)
        return hint if isinstance(hint, dict) else None
    except Exception:  # noqa: BLE001 — a missing hint degrades the policy, never raises
        return None


def _is_infra(home: Path, run_id: str, hint: dict[str, Any] | None,
              nc: dict[str, Any] | None, failed_node: str | None,
              fail_reason: str) -> bool:
    if fail_reason and str(fail_reason).lower() in _INFRA_FINISH:
        return True
    if nc is not None and str(nc.get("kind") or "") == "infra":
        return True
    fc = _failure_class(home, run_id, failed_node)
    if fc in _INFRA_FAILURE_CLASSES:
        return True
    # Provider trouble surfaces as ``strategy: resume`` with no ``needs_change``;
    # a withheld publish is ``strategy: verify`` (handled later), never this.
    if isinstance(hint, dict) and nc is None and str(hint.get("strategy") or "") == "resume":
        return True
    return False


def _cost_since_first_repair(home: Path, run_id: str,
                             attempts: list[dict[str, Any]]) -> float | None:
    """The run's ``cost_usd`` when at least one repair has been attempted.

    The kickoff says "cost since the first repair"; ``task_runs.cost_usd`` is the
    run's cumulative spend, the best available proxy (and the one the retry hint
    already reads). ``None`` when there are no attempts or the DB is unreadable.
    """
    if not attempts:
        return None
    try:
        from mini_ork.web.db import db_for
        db = db_for(Path(home))
        if not db.has_table("task_runs"):
            return None
        row = db.row("SELECT cost_usd FROM task_runs WHERE id = ? LIMIT 1", (run_id,))
        if not row:
            return None
        return float(row.get("cost_usd") or 0.0)
    except Exception:  # noqa: BLE001
        return None


# ── feedback file (revise / prove) ───────────────────────────────────────────


def _next_round(run_dir: Path) -> int:
    highest = 0
    revise_dir = run_dir / "revise"
    if revise_dir.is_dir():
        for path in revise_dir.glob("round-*.md"):
            stem = path.stem[len("round-"):]
            if stem.isdigit():
                highest = max(highest, int(stem))
    return highest + 1


def _real_error_lines(path: str, max_lines: int = 40, max_bytes: int = 6000) -> list[str]:
    """Error-bearing lines from a verifier log, each with its next 2 lines of context.

    Uses ``execute._ERROR_LINE_RE`` (the exact regex the in-run revise rounds use)
    so the auto-repair round file reads the same diagnostics a human would.
    """
    from mini_ork.cli import execute as execute_mod
    regex = execute_mod._ERROR_LINE_RE
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            all_lines = fh.read().splitlines()
    except OSError:
        return []
    out: list[str] = []
    used = 0
    i = 0
    n = len(all_lines)
    while i < n and len(out) < max_lines:
        if regex.search(all_lines[i]):
            for j in range(i, min(i + 3, n)):
                line = all_lines[j]
                used += len(line) + 1
                if used > max_bytes:
                    return out
                out.append(line)
                if len(out) >= max_lines:
                    return out
            i += 3
        else:
            i += 1
    return out


def _declared_evidence_path(data: dict, run_dir: Path) -> str:
    """The verifier's own ``evidence_path``, when it points inside ``run_dir``.

    A verifier records where it wrote its log, and that name need not match the
    ``verifier_<stem>.log`` / ``evidence/<stem>.log`` conventions
    :func:`mini_ork.cli.execute._verifier_log_candidates` searches:
    ``tier1-compile-typecheck`` writes ``tier1-evidence.log`` and leaves only a
    one-line JSON summary at ``evidence/<stem>.log``, so the stem-based search
    finds a file with no tool output at all.

    Only a path inside ``run_dir`` is honoured — an absolute path read out of a
    run artifact must not become an arbitrary-file read.
    """
    raw = str(data.get("evidence_path") or "").strip()
    if not raw:
        return ""
    try:
        root = os.path.realpath(str(run_dir))
        full = os.path.realpath(raw)
        if os.path.commonpath([root, full]) != root:
            return ""
    except (OSError, ValueError):
        return ""
    return full if os.path.isfile(full) else ""


def _log_tail(paths: list[str], max_lines: int = 20) -> tuple[str, list[str]]:
    """``(path, last max_lines)`` of the first readable log in ``paths``."""
    for path in paths:
        try:
            with open(path, encoding="utf-8", errors="replace") as fh:
                lines = fh.read().splitlines()
        except OSError:
            continue
        return path, lines[-max_lines:]
    return "", []


def _failing_verifier_sections(run_dir: Path) -> list[str]:
    """One section per failing verifier: checks + the real captured error text."""
    from mini_ork.cli import execute as execute_mod
    from mini_ork.verify import levels as _levels  # noqa: PLC0415

    sections: list[str] = []
    for path in sorted(run_dir.glob("verifier_*.json")):
        data = _read_json(path)
        if not isinstance(data, dict):
            continue
        if _levels.abstain_kind(data):
            # The tool could not run: there is nothing here to repair in the
            # artifact, and a "here is what to fix" section would send the
            # implementer after a defect no verifier observed (§failure_kind).
            continue
        status = str(data.get("status") or "").upper()
        if not (data.get("pass") is False or status in ("REFUTED", "FAIL")):
            continue
        stem = path.stem[len("verifier_"):]
        lines = [f"## Verifier ({stem})"]
        if status:
            lines.append(f"status: {status}")
        summary = data.get("error_summary") or data.get("reason") or data.get("reasons")
        if summary:
            lines.append(f"error summary: {summary}")
        checks = data.get("failed_checks") or data.get("checks")
        if isinstance(checks, list):
            for c in checks:
                if not isinstance(c, dict):
                    continue
                if not (c.get("pass") is False or str(c.get("status") or "").upper() in ("FAIL", "REFUTED")):
                    continue
                cid = c.get("id") or c.get("name") or "check"
                lines.append(f"failing check: {cid}")
                log = c.get("log") or c.get("output")
                if isinstance(log, str) and log.strip():
                    lines.extend(log.splitlines()[:8])
        real = []
        declared = _declared_evidence_path(data, run_dir)
        logs = list(execute_mod._verifier_log_candidates(str(run_dir), stem))
        if declared:
            # The verifier's declared log is the tool output; a stem-derived
            # sibling may be only a summary of it. Prefer the declaration.
            logs = [declared] + [path for path in logs if path != declared]
        for candidate in logs:
            real = _real_error_lines(candidate)
            if real:
                lines.append(f"verifier errors ({os.path.basename(candidate)}):")
                lines.extend(real)
                break
        if not real:
            shown, tail = _log_tail(logs)
            if tail:
                # The log exists and has content, but no line is error-shaped:
                # the command died before reaching source errors — a toolchain
                # or environment wall. Saying "nothing was captured" here sent
                # the implementer to re-run a command that cannot succeed, so
                # the round made no progress and auto-repair gave up with "the
                # same failure signature repeats" (run-1791821930-72687,
                # 2026-10-12: the npm `EBADDEVENGINES` refusal sat unread in
                # `evidence_path` while the round claimed no output existed).
                lines.append(f"the verification command reported no source-level "
                             f"error lines; raw tail of {os.path.basename(shown)}:")
                lines.extend(tail)
            else:
                lines.append(
                    "the build/test output was not captured; re-run the verification "
                    "command first and read its errors"
                )
        sections.append("\n".join(lines))
    return sections


def _compose_feedback(run_dir: Path) -> tuple[int, str]:
    """``(round_no, text)`` for a revise/prove round file."""
    round_no = _next_round(run_dir)
    header = (
        f"Repair round {round_no} (auto-repair): the run failed after its revise "
        "rounds; these problems are still open. Fix ONLY these, on top of the "
        "working tree; do not start over."
    )
    sections: list[str] = []

    for path in sorted(run_dir.glob("review-*.json")):
        data = _read_json(path)
        if not isinstance(data, dict):
            continue
        verdict = str(data.get("verdict") or "").lower()
        if verdict not in _FAIL_VERDICTS:
            continue
        lines = [f"## Reviewer ({path.stem})", f"verdict: {data.get('verdict', 'unknown')}"]
        findings = data.get("findings")
        if isinstance(findings, list) and findings:
            lines.append("findings:")
            for f in findings:
                if not isinstance(f, dict):
                    continue
                issue = str(f.get("issue") or "").strip()
                where = str(f.get("file") or "").strip()
                line_no = f.get("line")
                snippet = str(f.get("snippet") or "").strip()
                bullet = f"- {issue}"
                if where:
                    bullet += f" · {where}:{line_no}"
                if snippet:
                    bullet += f" · {snippet}"
                lines.append(bullet)
        reasons = data.get("reasons")
        if reasons:
            lines.append("reasons: " + (reasons if isinstance(reasons, str)
                                        else json.dumps(reasons)[:4000]))
        sections.append("\n".join(lines))

    sections.extend(_failing_verifier_sections(run_dir))

    body = "\n\n".join(sections) if sections else \
        "(no reviewer findings or failing verifier checks were recorded)"
    return round_no, header + "\n\n" + body + "\n"


def _write_current(run_dir: Path, round_no: int, feedback_path: Path) -> None:
    """Write ``revise/current.json`` — the channel ``_read_revise_feedback`` reads."""
    revise_dir = run_dir / "revise"
    revise_dir.mkdir(parents=True, exist_ok=True)
    payload = {"round": round_no, "max_rounds": round_no, "feedback": str(feedback_path)}
    (revise_dir / "current.json").write_text(json.dumps(payload), encoding="utf-8")


# ── apply ────────────────────────────────────────────────────────────────────


def apply(home: Path, run_id: str, decision: dict[str, Any], *,
          spawn: bool = True, wait_for_exit: bool = False) -> dict[str, Any]:
    """Perform ``decision``: write feedback, record the attempt, resume the run.

    Returns a result dict — for a spawn, ``{"pid", "log", "command", "attempt"}``;
    for ``human``, ``{"action": "human", "gate": <bool>}``; for an ``escalate``
    that the policy admitted, ``{"escalated": true, "fix_epic_id", "fix_bug_id"}``
    (nothing is spawned); for a dry/no-op, ``{"action": <action>}``. Writes the
    attempt to ``repair.json`` and emits a ``run_events`` ``auto_repair`` row in
    every case — except a ``none`` decision for a run parked on a fix epic, which
    is a byte-identical no-op (``{"action": "none", "parked": true, ...}``).
    ``wait_for_exit`` is for a spawn made by the process that still owns the run
    record (see ``wait_for_spawner``).
    """
    home = Path(home)
    run_dir = _run_dir(home, run_id)
    action = str(decision.get("action") or "none")
    repair = _load_repair(run_dir)
    attempts = list(repair.get("attempts") or [])
    n = len(attempts) + 1

    # A run PARKED on a fix epic is inert (``decide``'s parked rule returned
    # ``none``): the epic owns the failure, so applying the no-op must touch
    # nothing. Returning here — before the escalate branch, the feedback write,
    # the attempt append, ``_write_repair``, ``_emit_run_event`` and any spawn —
    # keeps ``repair.json`` byte-identical and keeps the attempt count (and with
    # it the no-progress/budget stop rules) from drifting on every sweep that
    # re-decides a parked run. Keyed on the run's PERSISTED state, not the
    # decision's reason, so whichever rationale produced the ``none`` (the parked
    # rule, or ``MO_AUTO_REPAIR=0``) the parked record stands until SDE-4 revives
    # the run.
    if action == "none" and str(repair.get("state") or "") == "waiting_on_fix":
        return {"action": "none", "parked": True,
                "reason": decision.get("reason") or "",
                "fix_epic_id": repair.get("fix_epic_id")}

    result: dict[str, Any] = {"action": action, "attempt": n}

    # ``escalate`` (SDE-3a) is resolved BEFORE the attempt is recorded, because
    # ``escalation.escalate`` rewrites ``repair.json`` itself (state
    # ``waiting_on_fix`` + the epic it opened). The refusal path becomes an
    # ordinary ``human`` hand-off carrying the policy's reason verbatim — the
    # operator reads the cap/breaker/bar explanation, not a generic "handed over".
    if action == "escalate":
        from mini_ork.recovery import escalation
        esc = escalation.escalate(home, run_id, decision.get("diagnosis") or {})
        decision = dict(decision)
        if esc.get("escalated"):
            result["escalated"] = True
            result["fix_epic_id"] = esc.get("fix_epic_id")
            result["fix_bug_id"] = esc.get("fix_bug_id")
            result["kickoff_path"] = esc.get("kickoff_path")
            # Re-read: escalate() just wrote state/fix_epic_id/fix_bug_id/
            # triaged, and the snapshot loaded above predates that. Without this
            # the attempt append below would clobber the fix link.
            repair = _load_repair(run_dir)
            attempts = list(repair.get("attempts") or [])
            n = len(attempts) + 1
            result["attempt"] = n
        else:
            action = "human"
            result["action"] = "human"
            result["escalated"] = False
            decision["reason"] = str(esc.get("reason") or "escalation refused")
            result["reason"] = decision["reason"]

    if action in ("revise", "prove"):
        round_no, text = _compose_feedback(run_dir)
        if action == "prove" and decision.get("feedback"):
            # prove carries an explicit instruction; keep it as the header line.
            text = str(decision["feedback"]) + "\n\n" + text
        revise_dir = run_dir / "revise"
        revise_dir.mkdir(parents=True, exist_ok=True)
        feedback_path = revise_dir / f"round-{round_no}.md"
        feedback_path.write_text(text, encoding="utf-8")
        _write_current(run_dir, round_no, feedback_path)
        result["feedback"] = str(feedback_path)

    gate_pending = False
    if action == "human":
        gate_pending = _ensure_human_gate(home, run_id, run_dir)
        result["gate"] = gate_pending

    # Record the attempt.
    attempt = {
        "n": n,
        "ts": _now_iso(),
        "action": action,
        "from_node": decision.get("from_node"),
        "lanes": decision.get("lanes") or {},
        "reason": decision.get("reason") or "",
        "signature": decision.get("signature") or "",
    }
    # The typed diagnosis rides along with the attempt: ``repair.json`` then
    # records not only what was tried but who owned the failure. Compact — the
    # evidence list is deliberately left in ``diagnosis.json``, which ``apply``
    # (the side-effecting half) persists here; ``decide`` stays pure.
    dx = decision.get("diagnosis")
    if isinstance(dx, dict):
        from mini_ork.recovery.diagnose import write_diagnosis
        write_diagnosis(home, run_id, dx)
        attempt["diagnosis"] = {k: dx.get(k) for k in
                                ("rule_id", "layer", "stage", "node",
                                 "confidence", "routes_to", "summary")}
    prior_attempts = len(attempts)
    attempts.append(attempt)
    repair["attempts"] = attempts
    # ``gave_up`` is the loop's own verdict: a stop rule fired, or at least one
    # repair was actually attempted. A first-failure hand-off (environment /
    # owner-only / unclassified) gives the run to the human without that claim,
    # so it must not be labelled ``gave_up``.
    if action == "human":
        if prior_attempts >= 1 or decision.get("stop"):
            repair["state"] = "gave_up"
        else:
            repair.pop("state", None)
    elif action in _SPAWNABLE:
        repair["state"] = "repairing"
    # ``escalate`` matches neither branch on purpose: ``escalation.escalate``
    # already set ``waiting_on_fix`` and ``escalate`` must NOT be added to
    # ``_SPAWNABLE`` — the run waits for its fix epic (SDE-4 revives it), it is
    # not resumed here.
    repair["triaged"] = bool(repair.get("triaged", False))
    if action == "human":
        repair["triaged"] = _triage_on_give_up(home, run_id, repair)
    _write_repair(run_dir, repair)
    _emit_run_event(home, run_id, attempt)

    if action in _SPAWNABLE and spawn:
        spawned = _spawn_recover(home, run_id, run_dir, decision, n,
                                 wait_for_exit=wait_for_exit)
        result.update(spawned)
        result["command"] = spawned.get("command", "")
    elif action in _SPAWNABLE and not spawn:
        result["spawned"] = False
    return result


def _ensure_human_gate(home: Path, run_id: str, run_dir: Path) -> bool:
    """Make sure the existing needs-you path is armed — idempotently.

    ``retry_notify.notify`` dedupes a pending gate internally; we still skip the
    call entirely when a gate is already pending so we never churn the row.
    Returns whether a gate is pending afterwards.
    """
    try:
        from mini_ork.recovery import retry_notify
    except Exception:  # noqa: BLE001
        return False
    try:
        if retry_notify.pending_fix_for_run(home, run_dir) is not None:
            return True
    except Exception:  # noqa: BLE001
        pass
    try:
        retry_notify.notify(home, run_id)
    except Exception:  # noqa: BLE001 — needs-you path is best-effort
        return False
    try:
        return retry_notify.pending_fix_for_run(home, run_dir) is not None
    except Exception:  # noqa: BLE001
        return False


def _triage_on_give_up(home: Path, run_id: str, repair: dict[str, Any]) -> bool:
    """Call ``triage_run`` exactly once per run, on give-up, when opted in.

    Returns the value recorded in ``repair.json["triaged"]``. Fail-soft: triage
    must never raise into the run that just gave up.
    """
    if repair.get("triaged") is True:
        return True
    if context_env("MO_FAILURE_TRIAGE", "") != "1":
        return False
    try:
        from mini_ork.triage.failures import triage_run
        triage_run(
            run_id,
            home=str(home),
            db=str(Path(home) / "state.db"),
            root=context_env("MINI_ORK_ROOT") or None,
            promote=context_env("MO_FAILURE_TRIAGE_PROMOTE", "") == "1",
        )
        return True
    except Exception:  # noqa: BLE001 — triage never fails a run
        return False


def _spawn_recover(home: Path, run_id: str, run_dir: Path,
                   decision: dict[str, Any], n: int, *,
                   wait_for_exit: bool = False) -> dict[str, Any]:
    """Spawn a detached ``mini-ork recover`` for the SAME run, from the failed node."""
    argv = [sys.executable, "-m", "mini_ork.recovery.planner", str(run_id)]
    from_node = decision.get("from_node")
    if from_node:
        argv += ["--from-node", str(from_node)]
    for alias, lane in (decision.get("lanes") or {}).items():
        argv += ["--lane", f"{alias}={lane}"]
    if decision.get("ack_change"):
        argv += ["--ack-change"]
    # ``--force`` is the loop's own override: the retry hint is written for a
    # *human* deciding whether to retry, and it marks a code-shaped failure
    # ``retryable: false`` (strategy='none'). Without this the spawned recover
    # refuses every revise/prove attempt outright ("retry-hint refuses this run
    # … pass --force") and the main repair case could never resume.
    argv += ["--force"]

    env = dict(os.environ)
    env[_ATTEMPT_ENV] = str(n)
    env[_RUN_ID_ENV] = str(run_id)
    # The child must resolve the SAME home/db as this process: the CLI's own
    # cwd is ``MINI_ORK_ROOT``, which is not the mini-ork home, so an inherited
    # MINI_ORK_HOME (or its absence) would strand the child on the wrong
    # state.db and run dir.
    env["MINI_ORK_HOME"] = str(home)
    # Auto-repair owns this failure: the child's execute must not also triage
    # it (``execute._maybe_triage_failed_run`` stands down only on an explicit
    # "1"), or one failure would have two owners.
    env["MO_AUTO_REPAIR"] = "1"
    if wait_for_exit:
        env[_WAIT_PID_ENV] = str(os.getpid())
    log_path = run_dir / f"repair-{n}.log"
    cwd = context_env("MINI_ORK_ROOT") or os.getcwd()
    proc = _spawn(argv, cwd=cwd, env=env, stdout_path=log_path)
    return {
        "pid": getattr(proc, "pid", None),
        "log": str(log_path),
        "command": " ".join(argv),
    }


def _spawn(argv: list[str], *, cwd: str, env: dict[str, str],
           stdout_path: Path) -> subprocess.Popen:
    """Spawn ``argv`` detached, logging to ``stdout_path`` (mirrors acp.commands._spawn)."""
    log_fh = open(stdout_path, "ab")
    try:
        return subprocess.Popen(
            argv,
            cwd=cwd,
            env=env,
            stdout=log_fh,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
    finally:
        log_fh.close()


def _emit_run_event(home: Path, run_id: str, payload: dict[str, Any]) -> None:
    """Append an ``auto_repair`` row to ``run_events`` (best-effort).

    Written through a plain read-write ``sqlite3`` connection, the way
    ``cli/plan.py`` records ``asks_blocked``: ``web.db.StateDB`` opens its
    connection ``PRAGMA query_only = ON`` (the observability path is
    read-only), so an INSERT through it fails with "attempt to write a
    readonly database" — silently, since this helper swallows ``sqlite3.Error``.
    ``event_id`` embeds ``run_id`` because ``run_events.event_id`` is UNIQUE:
    a sweep repairing two runs in the same second would otherwise collide.
    """
    db_path = Path(home) / "state.db"
    if not (run_id and db_path.is_file()):
        return
    import sqlite3
    now = int(time.time())
    event_id = f"evt-auto_repair-{run_id}-{now}-{payload.get('n')}"
    try:
        con = sqlite3.connect(db_path, timeout=5.0)
        try:
            con.execute("PRAGMA busy_timeout = 5000")
            con.execute(
                "INSERT INTO run_events(event_id, run_id, event_type, payload_json, created_at) "
                "VALUES (?,?,?,?,?)",
                (event_id, run_id, "auto_repair", json.dumps(payload), now),
            )
            con.commit()
        finally:
            con.close()
    except sqlite3.Error:  # noqa: BLE001 — the event log is a side channel
        pass


# ── entry point ──────────────────────────────────────────────────────────────


def maybe_repair(home: Path, run_id: str, *, spawn: bool = True,
                 wait_for_exit: bool = False) -> dict[str, Any] | None:
    """``decide`` → ``apply``. Never raises; a no-op when auto-repair is off.

    Called unconditionally by the ``run`` and ``recover`` flow hooks — the run's
    DB status decides whether anything happens, so a green run pays only a few
    cheap reads. A withheld publish sets ``status='failed'`` while the flow's rc
    is 0, which is exactly why the hooks must not gate on rc.

    A ``none`` decision is returned without applying it — so a run parked on its
    fix epic is a pure no-op here (and ``apply``'s own parked short-circuit
    covers a direct call).

    ``wait_for_exit`` belongs to those two hooks: they spawn while their own
    lifecycle still owns the run record, so the child is told to wait for them
    to exit (``wait_for_spawner``).
    """
    try:
        if context_env("MO_AUTO_REPAIR", "1") == "0":
            return None

        # Re-entrancy guard (see ``_SPAWNED_THIS_PROCESS``): the loop is a chain
        # of detached processes, so a process that already handed this run to a
        # child must not spawn a second one for the same failure.
        if run_id in _SPAWNED_THIS_PROCESS:
            return None

        decision = decide(home, run_id)
        action = str(decision.get("action") or "none")
        if action == "none":
            _mark_repaired_if_done(home, run_id)
            return decision
        result = apply(home, run_id, decision, spawn=spawn,
                       wait_for_exit=wait_for_exit)
        if spawn and action in _SPAWNABLE:
            _SPAWNED_THIS_PROCESS.add(run_id)
        return result
    except Exception as exc:  # noqa: BLE001 — auto-repair must never fail a run
        try:
            sys.stderr.write(f"auto_repair: {exc}\n")
        except Exception:  # noqa: BLE001
            pass
        return None


def wait_for_spawner() -> None:
    """Block until the process that spawned this repair has exited.

    Called at the top of ``recovery.planner.cli_main`` when — and only when —
    the spawning process marked the child with ``MO_AUTO_REPAIR_WAIT_PID``:
    the ``run`` / ``recover`` lifecycle, which still owns the run record while
    it reflects and only closes it in ``main._close_run_record`` (the caller's
    ``finally``). Dispatching before that teardown would let it observe the
    child's ``executing`` status and flip the *live* repair to ``failed``.

    Liveness is read from ``os.getppid()``: the kernel reparents us the instant
    the spawner exits, so a spawner that is briefly a zombie (not yet reaped by
    its own parent) can never stall the wait. Best-effort and bounded — a
    spawner that outlives ``MO_AUTO_REPAIR_WAIT_S`` is logged and ignored.
    """
    raw = context_env(_WAIT_PID_ENV, "")
    if not raw.isdigit():
        return
    pid = int(raw)
    if pid <= 1 or pid == os.getpid() or os.getppid() != pid:
        return
    wait_s = _env_float("MO_AUTO_REPAIR_WAIT_S", _DEFAULT_WAIT_S)
    deadline = time.monotonic() + wait_s
    while os.getppid() == pid and time.monotonic() < deadline:
        time.sleep(0.2)
    if os.getppid() == pid:
        try:
            sys.stderr.write(
                f"auto_repair: spawner {pid} still alive after "
                f"{wait_s:.0f}s; resuming without the hand-off\n")
        except Exception:  # noqa: BLE001
            pass


def note_stuck(home: Path | None = None, run_id: str = "", *,
               reason: str = "") -> bool:
    """Hand a stranded repair to the human — a spawned recover that bailed out.

    A recover this loop spawned can return *before* it dispatches: the retry
    hint refuses it, ``plan_recovery`` raises ``RecoveryRefused``, another
    process holds the lease, a carry-patch is refused, or every node is
    reusable. The attempt is already recorded, so leaving ``repair.json`` at
    ``state='repairing'`` would strand the run with no owner and no hand-off.
    Records the give-up (stop rule) and arms the needs-you gate instead.

    ``home`` / ``run_id`` default to the markers ``_spawn_recover`` exports to
    the child (``MINI_ORK_HOME`` / ``MO_AUTO_REPAIR_RUN_ID``), so the child's
    ``cli_main`` need only pass ``reason``.

    Only the attempt that is still the live one may hand over: the child
    carries its attempt number in ``MO_AUTO_REPAIR_ATTEMPT``, so an attempt a
    newer (concurrent) repair superseded is never stepped on. Returns whether
    the hand-off was recorded; never raises.
    """
    raw = context_env(_ATTEMPT_ENV, "")
    if not raw.isdigit():
        return False  # not a spawned repair attempt — nothing to hand over
    run_id = run_id or context_env(_RUN_ID_ENV, "")
    home_str = str(home or context_env("MINI_ORK_HOME", ""))
    if not (run_id and home_str):
        return False
    try:
        run_dir = _run_dir(Path(home_str), run_id)
        repair = _load_repair(run_dir)
        attempts = list(repair.get("attempts") or [])
        if repair.get("state") != "repairing":
            return False
        if not attempts or str(attempts[-1].get("n")) != raw:
            return False
        decision = _decision(
            "human",
            reason=f"the repair attempt could not resume the run: {reason}"
                   if reason else "the repair attempt could not resume the run",
            stop=True)
        apply(Path(home_str), run_id, decision, spawn=False)
        return True
    except Exception as exc:  # noqa: BLE001 — the hand-off is best-effort
        try:
            sys.stderr.write(f"auto_repair: note_stuck failed: {exc}\n")
        except Exception:  # noqa: BLE001
            pass
        return False


def _mark_repaired_if_done(home: Path, run_id: str) -> None:
    """Flip ``repair.json`` to ``state="repaired"`` when the run has published.

    Only touches a run that already carries a repair history — a green run with
    no ``repair.json`` stays untouched.
    """
    run_dir = _run_dir(home, run_id)
    path = run_dir / _ATTEMPT_FILE
    if not path.is_file():
        return
    try:
        status = _run_status(home, run_id)
    except Exception:  # noqa: BLE001
        return
    if status not in ("published", "done"):
        return
    repair = _load_repair(run_dir)
    if repair.get("state") == "repaired":
        return
    repair["state"] = "repaired"
    _write_repair(run_dir, repair)
