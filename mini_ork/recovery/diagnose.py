"""Typed, evidence-cited diagnosis of a failed run.

``auto_repair.decide`` answers *what should happen next*; ``retry_hint`` answers
*what class of failure is this*. Neither names **who owns the failure** —
which layer (``engine`` / ``provider`` / ``change`` / ``environment`` …), which
stage (``plan`` / ``execute`` / ``verify`` / ``review`` / ``deliver``) and which
node. This module adds exactly that answer, as a first-match-wins rule set:

  * ``R1``  publisher withheld with a proven level vector → engine / deliver / publisher
  * ``R1b`` publisher withheld with unproven levels      → change / verify / reverify
  * ``R2``  traceback inside mini-ork's own source       → engine / <stage> / fix_epic
  * ``R3``  infra or provider trouble                    → provider / execute / retry
  * ``R4``  a reviewer asked for a revision              → change / review / revise
  * ``R5``  a verifier refuted the change                → change / verify / revise
  * ``R6``  a node started and never ended               → user / none ( ``killed-by-user`` )
                                                           else provider / <stage> / retry
  * ``RN``  a ``task_runs.notes`` segment localises it   → provider / … / retry (see below)
  * ``R7``  an owner-only fix (environment/credentials/budget) → environment / owner
  * ``R8``  the retry hint points at an unavailable lane → model / swap_lane

and an **unclassified fall-through** at confidence ``0.0``.

``diagnose(..., allow_model=True)`` hands a fall-through to the *model*
diagnoser (:mod:`mini_ork.recovery.diagnoser_llm`), which may return a grounded
diagnosis with ``rule_id == "M1"`` and ``source == "model"`` instead. The rule
result is kept whenever the model declines — see :func:`diagnose`.

The note rules (``RN``) run after the artefact/event rules and before the
fall-through. They read the append-only ``task_runs.notes`` column — the same
``"; "``-joined, per-segment ``str.startswith`` read ``auto_repair.decide``
performs — because for a reaped or crashed lifecycle the notes are the only
place the cause is written down:

  * a ``reaped:`` segment            → provider / <stage of the last open node, else ``unknown``> / retry
  * a ``planner_dispatch_rc_<N>``    → provider / plan / planner / retry
  * ``lifecycle exited on an exception`` → engine / <stage> / fix_epic, but **only**
    when R2's own traceback evidence also exists for the run; without it the run
    stays unclassified.
  * ``lifecycle exited without a verdict`` stays unclassified — a model diagnoser
    owns it in a later epic; guessing here would be a confident lie.

Two invariants make the answer trustworthy:

  * **Evidence is load-bearing.** Every matching rule cites at least one
    evidence item (a file reference, a ``run_events`` id, a log excerpt, a note
    segment or the retry hint's own field), carried as
    ``{"kind", "ref", "excerpt"}``. A rule that cannot cite evidence does not
    match: the diagnosis degrades to ``unclassified`` rather than becoming a
    confident guess. The unclassified fall-through is the one result with no
    evidence. ``source`` is the literal ``"rule"`` on every rule result — the
    cited *file* lives in ``evidence[].ref`` — and ``""`` on the fall-through.
  * **The done-node invariant.** ``node`` never names a node whose *latest*
    ``node_end`` finished ``done`` — a node that succeeded is not where the run
    stopped, so naming it sends the operator to a step with nothing to fix.
    The publisher's abstain is likewise read from its latest ``node_end``, so a
    publisher that was retried and later finished ``done`` is not misreported.

``confidence`` is the **rule's own prior** — how reliable this rule's signal is
in general — not a model's claim about this run. It never varies with the
evidence; a rule is only ever matched or not.

Read-only by construction: the module reads the run dir, ``run_events`` /
``node_attempts`` / ``task_runs.notes`` and the reused helpers, and (when
``write=True``) writes only ``<run_dir>/diagnosis.json``. It never raises —
``auto_repair.decide`` calls it on every failed run, so a missing run dir, a
half-written artefact or an absent DB degrades to ``unclassified``.

CLI::

    python3 -m mini_ork.recovery.diagnose <run_id> [--json] [--no-write]
    python3 -m mini_ork.recovery.diagnose --replay [--limit N] [--json] [--out PATH]

``--replay`` diagnoses the most recent terminal failed runs of the home and
prints an aggregate; it never writes a run dir. ``--out`` writes the same JSON
payload to a path.
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DIAGNOSIS_VERSION = 2
DIAGNOSIS_FILE = "diagnosis.json"
PUBLISH_ABSTAIN_FILE = "publish-abstain.json"

# Layers — who owns the fix. The enum the contract routes on.
LAYER_CHANGE = "change"
LAYER_MODEL = "model"
LAYER_PROVIDER = "provider"
LAYER_ENVIRONMENT = "environment"
LAYER_ENGINE = "engine"
LAYER_KICKOFF = "kickoff"
LAYER_USER = "user"
# RBF-22a: a remote node-agent outage is infrastructure the change never
# touched. It keeps its own layer so an operator can separate "the sandbox was
# unreachable" from a provider/model fault and from the change itself.
LAYER_INFRA = "infra"
LAYER_UNCLASSIFIED = "unclassified"
LAYERS = frozenset({
    LAYER_CHANGE, LAYER_MODEL, LAYER_PROVIDER, LAYER_ENVIRONMENT,
    LAYER_ENGINE, LAYER_KICKOFF, LAYER_USER, LAYER_INFRA, LAYER_UNCLASSIFIED,
})

# ``routes_to`` — what the next step should do about it.
ROUTE_REVISE = "revise"
ROUTE_REVERIFY = "reverify"
ROUTE_SWAP_LANE = "swap_lane"
ROUTE_RETRY = "retry"
ROUTE_OWNER = "owner"
ROUTE_FIX_EPIC = "fix_epic"
ROUTE_KICKOFF_REPAIR = "kickoff_repair"
ROUTE_NONE = "none"
ROUTE_HUMAN = "human"
ROUTES = frozenset({
    ROUTE_REVISE, ROUTE_REVERIFY, ROUTE_SWAP_LANE, ROUTE_RETRY, ROUTE_OWNER,
    ROUTE_FIX_EPIC, ROUTE_KICKOFF_REPAIR, ROUTE_NONE, ROUTE_HUMAN,
})

# Stages — where in the pipeline the failure happened. The values
# ``_stage_for`` can produce, plus ``unknown`` (the fall-through's own value).
# Exported as the enum a model diagnoser validates its reply against
# (``diagnoser_llm``): a stage outside this set is not a diagnosis, it is noise.
STAGE_PLAN = "plan"
STAGE_EXECUTE = "execute"
STAGE_VERIFY = "verify"
STAGE_REVIEW = "review"
STAGE_DELIVER = "deliver"
STAGE_UNKNOWN = "unknown"
STAGES = frozenset({
    STAGE_PLAN, STAGE_EXECUTE, STAGE_VERIFY, STAGE_REVIEW, STAGE_DELIVER,
    STAGE_UNKNOWN,
})

# ``source`` is the literal ``"rule"`` on every rule result: the diagnosis came
# from the rule set, and the artefact it read is cited in ``evidence[].ref``.
SOURCE_RULE = "rule"
SOURCE_FALLTHROUGH = ""
# ``source`` on a model-made diagnosis (``diagnoser_llm``, rule id ``M1``): the
# frozen escalation policy branches on this to require a confidence bar.
SOURCE_MODEL = "model"

RULE_UNCLASSIFIED = "unclassified"

# RBF-22a: the finish reason the executor writes for a remote node-agent
# outage, and the strategy that resolves it (re-dispatch the node — the
# sandbox, not the change, was unavailable).
RULE_REMOTE_UNAVAILABLE = "remote_unavailable"
STRATEGY_RESUME = "resume"

# The publisher's own withheld signals. ``publisher.py`` returns them as the
# publisher node's ``finish_reason``; ``task_state._TERMINAL_OK_FINISH``
# deliberately treats them as NOT failures (a withheld publish is not a crashed
# node), so R1 / R1b — not ``_failing_node`` — are what name them. Only
# ``publish_abstain`` *with* a proven level vector is an engine fault; the rest
# are the change's own problem (re-verify it).
_ABSTAIN_FINISH = frozenset({"publish_abstain", "levels_unverified", "abstain"})
_PUBLISH_ABSTAIN = "publish_abstain"

# ``task_runs.notes`` vocabulary (append-only, ``"; "``-joined). Mirrors the
# read ``auto_repair.decide`` performs for the killed-by-user marker.
_NOTE_KILLED_BY_USER = "killed-by-user"
_NOTE_REAPED = "reaped:"
_NOTE_PLANNER_DISPATCH_RC = "planner_dispatch_rc_"
_NOTE_LIFECYCLE_EXCEPTION = "lifecycle exited on an exception"
_NOTE_LIFECYCLE_NO_VERDICT = "lifecycle exited without a verdict"

# Node-id token → stage. Deterministic and I/O-free: the same shape
# ``acp/task_state`` and ``ide_pages`` use to read a node's role off its name.
_STAGE_TOKENS = (
    ("publisher", "deliver"),
    ("review", "review"),
    ("verif", "verify"),
    ("check", "verify"),
    ("test", "verify"),
    ("gate", "verify"),
    ("plan", "plan"),
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _ev(kind: str, ref: str, excerpt: str) -> dict[str, str]:
    """One evidence item: what kind of citation this is, where, and what it says."""
    return {"kind": str(kind), "ref": str(ref), "excerpt": str(excerpt)[:400]}


def _read_json(path: Path) -> Any:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None


def _run_dir(home: Path, run_id: str) -> Path:
    return Path(home) / "runs" / str(run_id)


def _lifecycle(home: Path, run_id: str) -> list[dict[str, Any]] | None:
    """This run's ``run_events`` rows, ``event_id`` included, or ``None``.

    The same fail-soft read ``retry_hint._run_node_events`` performs, but the
    ``event_id`` is kept so a diagnosis can cite the exact row it read. ``None``
    when the DB / table / query is unavailable — the caller then declines to
    claim anything about the lifecycle rather than asserting a negative it
    cannot see.
    """
    try:
        from mini_ork.web.db import db_for
        db = db_for(Path(home))
        if not db.has_table("run_events"):
            return None
        rows = db.rows(
            "SELECT event_id, event_type, payload_json, created_at, rowid AS rid "
            "FROM run_events WHERE run_id = ? "
            "ORDER BY created_at ASC, event_id ASC",
            (str(run_id),),
        )
    except Exception:  # noqa: BLE001 — a missing / unreadable DB is "no lifecycle"
        return None
    return [
        {
            "event_id": str(r.get("event_id") or ""),
            "event_type": r.get("event_type"),
            "payload_json": r.get("payload_json"),
            "created_at": r.get("created_at"),
            "rowid": r.get("rid"),
        }
        for r in rows
    ]


def _notes(home: Path, run_id: str) -> str:
    """``task_runs.notes`` for this run, or ``""``.

    Fail-soft, like ``auto_repair.decide``'s own read: an unreadable or absent
    notes column must never raise and must never be confused with a note that
    *says* something. The caller re-derives meaning per ``"; "``-separated
    segment.
    """
    try:
        from mini_ork.web.db import db_for
        db = db_for(Path(home))
        if not db.has_table("task_runs"):
            return ""
        row = db.row("SELECT notes FROM task_runs WHERE id = ? LIMIT 1", (str(run_id),))
        return str((row or {}).get("notes") or "")
    except Exception:  # noqa: BLE001 — an unreadable notes column is "no notes"
        return ""


def _is_attempt_marker(segment: str) -> bool:
    """``attempt <n> started <UTC iso>`` — the live-status attempt boundary."""
    parts = str(segment or "").split(" ", 3)
    return (len(parts) >= 3 and parts[0] == "attempt"
            and parts[1].isdigit() and parts[2] == "started")


def _note_segments(home: Path, run_id: str) -> list[str]:
    """The non-empty ``"; "``-separated segments of ``task_runs.notes``.

    The exact split ``auto_repair.decide`` uses, so the two never disagree about
    whether a note is present. Only segments AFTER the last ``attempt <n>
    started`` marker are returned: the notes column is append-only, so a
    revived run still carries the dead attempt's ``reaped:`` line and would
    otherwise be diagnosed as a dispatcher death ten hours old (RB-11/RB-16).
    """
    notes = _notes(home, run_id)
    segments = [seg for seg in notes.split("; ") if seg.strip()]
    last_marker = -1
    for index, segment in enumerate(segments):
        if _is_attempt_marker(segment.strip()):
            last_marker = index
    return segments[last_marker + 1:]


def _levels_ok(run_dir: Path) -> bool | None:
    """The run-level report's ``levels_ok``, or ``None`` when it is not stamped.

    The report is written to ``verdict.json`` — or ``run-verdict.json`` when a
    recipe owns ``verdict.json`` as its own deliverable — by
    ``cli/execute.py`` / ``verify/levels.py``. ``None`` (no stamp, or no file)
    is treated as "not proven": only an explicit ``true`` makes a withheld
    publish an engine fault.
    """
    for name in ("verdict.json", "run-verdict.json"):
        data = _read_json(run_dir / name)
        if isinstance(data, dict) and "levels_ok" in data:
            return bool(data["levels_ok"])
    return None


def _payload(ev: dict[str, Any]) -> dict[str, Any]:
    from mini_ork.acp.task_state import _parse_payload
    return _parse_payload(ev.get("payload_json"))


def _node_of(ev: dict[str, Any]) -> tuple[str, str]:
    payload = _payload(ev)
    node_id = str(payload.get("node_id") or ev.get("node_id") or "")
    node_type = str(payload.get("node_type") or ev.get("node_type") or "")
    return node_id, node_type


def _latest_ends(events: list[dict[str, Any]] | None) -> dict[str, dict[str, Any]]:
    """``node_id -> {finish_reason, node_type, event_id}`` from the LATEST ``node_end``.

    Rows arrive in insertion order, so a later write overwrites an earlier one:
    a node that failed once and then passed reads as ``done``, which is what the
    done-node invariant needs.
    """
    ends: dict[str, dict[str, Any]] = {}
    for ev in events or []:
        if ev.get("event_type") != "node_end":
            continue
        node_id, node_type = _node_of(ev)
        if not node_id:
            continue
        ends[node_id] = {
            "finish_reason": str(_payload(ev).get("finish_reason") or ""),
            "node_type": node_type,
            "event_id": str(ev.get("event_id") or ""),
        }
    return ends


def _last_match(ends: dict[str, dict[str, Any]], token: str) -> str:
    """The latest node id whose id or type contains *token* (``""`` on miss)."""
    found = ""
    for node_id, info in ends.items():
        blob = f"{node_id} {info.get('node_type') or ''}".lower()
        if token in blob:
            found = node_id
    return found


def _open_state(events: list[dict[str, Any]] | None) -> tuple[list[str], str]:
    """``(open_nodes, last_open)`` for nodes that started and never ended.

    Insertion order is preserved (``node_start`` order), so ``last_open`` is the
    most recent node the run entered without a matching ``node_end`` — the node
    an interruption or a reap actually stranded.
    """
    if not events:
        return [], ""
    starts: dict[str, int] = {}
    ends_count: dict[str, int] = {}
    order: list[str] = []
    last_open = ""
    for ev in events:
        node_id, _t = _node_of(ev)
        if not node_id:
            continue
        if ev.get("event_type") == "node_start":
            starts[node_id] = starts.get(node_id, 0) + 1
            if node_id not in order:
                order.append(node_id)
            last_open = node_id
        elif ev.get("event_type") == "node_end":
            ends_count[node_id] = ends_count.get(node_id, 0) + 1
    open_nodes = [n for n in order if starts.get(n, 0) > ends_count.get(n, 0)]
    return open_nodes, last_open


def _stage_for(node_id: str | None, node_type: str = "") -> str:
    blob = f"{node_id or ''} {node_type or ''}".lower()
    for token, stage in _STAGE_TOKENS:
        if token in blob:
            return stage
    return "execute"


def _guard_node(node: str | None, ends: dict[str, dict[str, Any]]) -> str | None:
    """Apply the done-node invariant: never name a node whose latest end is ``done``."""
    if node and str((ends.get(node) or {}).get("finish_reason") or "").lower() == "done":
        return None
    return node


def _result(run_id: str, *, rule_id: str, layer: str, stage: str, node: str | None,
            confidence: float, evidence: list[dict[str, str]],
            routes_to: str, summary: str,
            ends: dict[str, dict[str, Any]] | None = None) -> dict[str, Any]:
    return {
        "version": DIAGNOSIS_VERSION,
        "run_id": str(run_id),
        "rule_id": rule_id,
        "layer": layer,
        "stage": stage,
        "node": _guard_node(node, ends or {}),
        "source": SOURCE_RULE,
        "confidence": float(confidence),
        "evidence": list(evidence),
        "routes_to": routes_to,
        "summary": summary,
        "computed_at": _now_iso(),
    }


def _fallthrough(run_id: str, layer: str, summary: str) -> dict[str, Any]:
    """The unclassified result — no rule could cite evidence, so we say so.

    ``confidence`` is ``0.0`` and ``evidence`` is empty by construction: this is
    the honest "nothing localised this failure" answer, not a guess. ``source``
    is ``""`` — there is no rule behind it.
    """
    return {
        "version": DIAGNOSIS_VERSION,
        "run_id": str(run_id),
        "rule_id": RULE_UNCLASSIFIED,
        "layer": layer,
        "stage": STAGE_UNKNOWN,
        "node": None,
        "source": SOURCE_FALLTHROUGH,
        "confidence": 0.0,
        "evidence": [],
        "routes_to": ROUTE_HUMAN,
        "summary": summary,
        "computed_at": _now_iso(),
    }


# ── shared inputs ────────────────────────────────────────────────────────────


def _hint(home: Path, run_id: str) -> dict[str, Any] | None:
    try:
        from mini_ork.recovery import retry_hint
        hint = retry_hint.load_or_compute(Path(home), str(run_id), write=False)
        return hint if isinstance(hint, dict) else None
    except Exception:  # noqa: BLE001 — a missing hint is not a diagnosis failure
        return None


def _failing(events: list[dict[str, Any]] | None) -> tuple[str | None, str]:
    if not events:
        return None, ""
    try:
        from mini_ork.acp.task_state import _failing_node
        got = _failing_node(events)
    except Exception:  # noqa: BLE001
        return None, ""
    if not got:
        return None, ""
    return str(got[0]), str(got[1])


def _tail(run_dir: Path, node: str | None) -> str:
    if not node:
        return ""
    try:
        from mini_ork.recovery import retry_hint
        return retry_hint._tail_log(Path(run_dir), node, n=40)
    except Exception:  # noqa: BLE001
        return ""


def _failure_class(home: Path, run_id: str, node: str | None) -> str:
    try:
        from mini_ork.recovery import retry_hint
        fc, _tail_text = retry_hint._failure_class_for_any(Path(home), str(run_id))
        if fc:
            return str(fc)
        if node:
            fc_node, _t = retry_hint._failure_class_for_node(Path(home), str(run_id), node)
            if fc_node:
                return str(fc_node)
    except Exception:  # noqa: BLE001
        return ""
    return ""


def _failing_artifacts(run_dir: Path) -> tuple[str | None, str, str, str]:
    """``(node, kind, ref, detail)`` for the first failing review / verifier artefact.

    ``kind`` is ``"review"`` or ``"verifier"``; ``""`` when nothing failed.
    Reuses ``retry_hint``'s own predicates so the artefact that a diagnosis
    names is exactly the one the retry hint classifies on.
    """
    if not run_dir.is_dir():
        return None, "", "", ""
    from mini_ork.recovery import retry_hint
    for path in sorted(run_dir.glob("review-*.json")):
        data = _read_json(path)
        if isinstance(data, dict) and retry_hint._review_failed(data):
            node = path.name[len("review-"):-len(".json")]
            reasons = data.get("reasons") or []
            detail = "; ".join(str(r) for r in reasons[:3]) or str(data.get("summary") or "")
            return node, "review", path.name, f"verdict={data.get('verdict')} {detail}".strip()
    for path in sorted(run_dir.glob("verifier_*.json")):
        data = _read_json(path)
        if isinstance(data, dict) and retry_hint._verifier_failed(data):
            stem = path.name[len("verifier_"):-len(".json")]
            detail = str(data.get("reason") or data.get("summary") or "")[:200]
            status = str(data.get("status") or ("pass=false" if data.get("pass") is False else ""))
            return stem, "verifier", path.name, f"status={status} {detail}".strip()
    return None, "", "", ""


# ── the rules ────────────────────────────────────────────────────────────────


def _publisher_withheld(run_dir: Path, ends: dict[str, dict[str, Any]]
                        ) -> tuple[str, str, dict[str, Any] | None] | None:
    """``(publisher, finish_reason, abstain_artefact)`` when the publisher withheld.

    ``None`` when the publisher's LATEST ``node_end`` finished ``done`` (it
    published — whatever an older abstain artefact still says) or when nothing
    in the run dir says it withheld. Read once by both R1 and R1b so the two can
    never disagree about *whether* the run was withheld.
    """
    pub = _last_match(ends, "publisher")
    reason = str((ends.get(pub) or {}).get("finish_reason") or "")
    if reason.lower() == "done":
        return None
    abstain = _read_json(run_dir / PUBLISH_ABSTAIN_FILE)
    abstain = abstain if isinstance(abstain, dict) else None
    if abstain is None and reason.lower() not in _ABSTAIN_FINISH:
        return None
    return pub, reason, abstain


def _abstain_evidence(pub: str, reason: str, abstain: dict[str, Any] | None,
                      levels_ok: bool | None, event_id: str = ""
                      ) -> list[dict[str, str]]:
    """The shared citations both withhold rules build from."""
    evidence: list[dict[str, str]] = []
    if abstain is not None:
        evidence.append(_ev("file", PUBLISH_ABSTAIN_FILE,
                            f"publish withheld: {abstain.get('reason') or '?'} — "
                            f"{str(abstain.get('detail') or '')[:200]}"))
    if pub and reason:
        evidence.append(_ev("event", f"run_events:{event_id or '?'}",
                            f"publisher {pub} latest node_end finish_reason={reason} "
                            f"(publish_abstain evidence)"))
    if levels_ok is not None:
        evidence.append(_ev("report", "verdict.json",
                            f"run-level level vector proven={str(levels_ok).lower()}"))
    return evidence


def _rule_publish_abstain(run_dir: Path, run_id: str, ends: dict[str, dict[str, Any]]
                          ) -> dict[str, Any] | None:
    """R1 — the publisher withheld the publish although every level was proven.

    Only ``finish_reason == "publish_abstain"`` with ``levels_ok`` true: the
    change verified cleanly, so a withheld commit is the *engine's* fault (the
    authored patch could not be committed) and warrants a fix epic. A withheld
    publish whose levels are unproven is the change's own problem — R1b.
    """
    got = _publisher_withheld(run_dir, ends)
    if got is None:
        return None
    pub, reason, abstain = got
    levels_ok = _levels_ok(run_dir)
    if reason.lower() != _PUBLISH_ABSTAIN or levels_ok is not True:
        return None
    evidence = _abstain_evidence(pub, reason, abstain, levels_ok,
                                 str((ends.get(pub) or {}).get("event_id") or ""))
    node = pub or "publisher"
    return _result(
        run_id, rule_id="R1", layer=LAYER_ENGINE, stage="deliver", node=node,
        confidence=0.9, evidence=evidence, routes_to=ROUTE_FIX_EPIC,
        summary=(f"the publisher withheld the publish at {node} although every "
                 f"level was proven — the engine could not deliver the change"),
        ends=ends,
    )


def _rule_withheld_unproven(run_dir: Path, run_id: str, ends: dict[str, dict[str, Any]]
                            ) -> dict[str, Any] | None:
    """R1b — the publisher withheld, but not as an engine fault.

    ``levels_unverified`` / ``abstain`` finishes, or a ``publish_abstain`` whose
    run-level report is missing or ``levels_ok=False``: the change itself is not
    proven, so it goes back through verify rather than opening a fix epic.
    """
    got = _publisher_withheld(run_dir, ends)
    if got is None:
        return None
    pub, reason, abstain = got
    levels_ok = _levels_ok(run_dir)
    if reason.lower() == _PUBLISH_ABSTAIN and levels_ok is True:
        return None  # R1 owns it
    evidence = _abstain_evidence(pub, reason, abstain, levels_ok,
                                 str((ends.get(pub) or {}).get("event_id") or ""))
    node = pub or "publisher"
    return _result(
        run_id, rule_id="R1b", layer=LAYER_CHANGE, stage="verify", node=node,
        confidence=0.8, evidence=evidence, routes_to=ROUTE_REVERIFY,
        summary=(f"the publisher withheld the publish at {node} with unproven "
                 f"levels — re-verify, do not revise"),
        ends=ends,
    )


def _traceback_evidence(failed_node: str | None, fail_reason: str, log: str
                        ) -> list[dict[str, str]]:
    """R2's citations: ``triage.blame`` attributed the failure to mini-ork's source.

    Empty when the failure is not inside the framework tree — which is exactly
    the test the note rule for ``lifecycle exited on an exception`` reuses, so a
    crashed lifecycle is never labelled ``engine`` without a traceback behind it.
    """
    if not failed_node:
        return []
    try:
        from mini_ork.triage.blame import NodeFailure, attribute
        verdict, blamed = attribute(NodeFailure(
            node_id=failed_node, finish_reason=fail_reason or None, log_excerpt=log or "",
        ))
    except Exception:  # noqa: BLE001
        return []
    if verdict != "mini_ork":
        return []
    evidence = [_ev("log", f"node:{failed_node}", f"{e.rule}: {e.detail}") for e in blamed]
    if not evidence and log:
        evidence = [_ev("log", f"node:{failed_node}", log[:300])]
    return evidence


def _log_traceback_evidence(run_dir: Path) -> list[dict[str, str]]:
    """A mini-ork traceback found in the run's OWN logs, when no node localises it.

    The second witness for ``lifecycle exited on an exception``: the lifecycle
    process died outside any node (so ``_failing_node`` names nothing and R2 has
    no node to blame), yet its log still carries the framework frame. Read
    fail-soft — a missing run dir or an unreadable log is simply no evidence.
    """
    if not run_dir.is_dir():
        return []
    try:
        from mini_ork.triage.blame import NodeFailure, attribute
    except Exception:  # noqa: BLE001
        return []
    names = sorted(p for p in run_dir.glob("execute*.log"))
    names += sorted(p for p in run_dir.glob("impl-*.log"))
    for path in names:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if "Traceback (most recent call last)" not in text:
            continue
        excerpt = "\n".join(text.splitlines()[-40:])[:2000]
        try:
            verdict, blamed = attribute(NodeFailure(node_id=path.name, log_excerpt=excerpt))
        except Exception:  # noqa: BLE001
            continue
        if verdict != "mini_ork":
            continue
        evidence = [_ev("log", f"log:{path.name}", f"{e.rule}: {e.detail}") for e in blamed]
        if evidence:
            return evidence
    return []


def _rule_framework_error(run_dir: Path, run_id: str, ends: dict[str, dict[str, Any]],
                          failed_node: str | None, fail_reason: str, log: str
                          ) -> dict[str, Any] | None:
    """R2 — a traceback inside mini-ork's own source (via ``triage.blame``)."""
    evidence = _traceback_evidence(failed_node, fail_reason, log)
    if not evidence:
        return None
    stage = _stage_for(failed_node, str((ends.get(failed_node) or {}).get("node_type") or ""))
    return _result(
        run_id, rule_id="R2", layer=LAYER_ENGINE, stage=stage, node=failed_node,
        confidence=0.85, evidence=evidence, routes_to=ROUTE_FIX_EPIC,
        summary=f"an error inside mini-ork's own source at {failed_node} — framework fix",
        ends=ends,
    )


def _rule_remote_unavailable(run_id: str, ends: dict[str, dict[str, Any]],
                             failed_node: str | None, fail_reason: str
                             ) -> dict[str, Any] | None:
    """R3a — a remote node-agent outage (RBF-22a).

    The executor names a remote read timeout / unreachable node-agent
    ``remote_unavailable``; before this rule it fell through to
    ``unclassified`` and the orchestrator blamed the verifier. The failure is
    transport, not the change: layer ``infra``, strategy ``resume`` (re-dispatch
    the same node), retryable. Only that exact finish reason matches — a
    generic ``error`` must not inherit it.
    """
    if str(fail_reason or "").lower() != RULE_REMOTE_UNAVAILABLE:
        return None
    end = ends.get(failed_node or "") or {}
    evidence = [_ev(
        "event", f"run_events:{end.get('event_id') or '?'}",
        f"finish_reason={RULE_REMOTE_UNAVAILABLE} — the remote node/agent was "
        f"unreachable, not the change",
    )]
    result = _result(
        run_id, rule_id=RULE_REMOTE_UNAVAILABLE, layer=LAYER_INFRA,
        stage=_stage_for(failed_node, str(end.get("node_type") or "")),
        node=failed_node, confidence=0.85, evidence=evidence,
        routes_to=ROUTE_RETRY,
        summary=(f"a remote node was unavailable at {failed_node or 'dispatch'} "
                 f"— resume the node; it is transport, not the change"),
        ends=ends,
    )
    result["strategy"] = STRATEGY_RESUME
    result["retryable"] = True
    return result


def _rule_infra(home: Path, run_id: str, run_dir: Path, ends: dict[str, dict[str, Any]],
                failed_node: str | None, fail_reason: str, log: str
                ) -> dict[str, Any] | None:
    """R3 — infra / provider trouble (the environment died, not the change)."""
    from mini_ork.recovery import auto_repair
    hint = _hint(home, run_id)
    nc = hint.get("needs_change") if isinstance(hint, dict) else None
    nc = nc if isinstance(nc, dict) else None
    if not auto_repair._is_infra(home, run_id, hint, nc, failed_node, fail_reason):
        return None
    fc = _failure_class(home, run_id, failed_node)
    evidence: list[dict[str, str]] = []
    if fail_reason and fail_reason.lower() in auto_repair._INFRA_FINISH:
        end = ends.get(failed_node or "") or {}
        evidence.append(_ev("event", f"run_events:{end.get('event_id') or '?'}",
                            f"finish_reason={fail_reason} is environment/transport, not the change"))
    if fc and fc in auto_repair._INFRA_FAILURE_CLASSES:
        evidence.append(_ev("db", f"node_attempts/{run_id}", f"failure_class={fc}"))
    try:
        from mini_ork.triage import blame as blame_mod
        sig = blame_mod._provider_hit(log or "")
    except Exception:  # noqa: BLE001
        sig = None
    if sig:
        evidence.append(_ev("log", f"node:{failed_node or '?'}",
                            f"provider/transport signature {sig!r}"))
    if not evidence:
        # The hint alone said "infra" but no artefact backs it: refuse to match
        # rather than present a confident guess as a diagnosis.
        return None
    return _result(
        run_id, rule_id="R3", layer=LAYER_PROVIDER, stage="execute", node=failed_node,
        confidence=0.8, evidence=evidence, routes_to=ROUTE_RETRY,
        summary=(f"infrastructure/provider failure"
                 f"{' at ' + failed_node if failed_node else ''} ({fail_reason or fc}) — retry"),
        ends=ends,
    )


def _rule_review(run_dir: Path, run_id: str, ends: dict[str, dict[str, Any]],
                 failed_node: str | None, fail_reason: str,
                 artifacts: tuple[str | None, str, str, str]) -> dict[str, Any] | None:
    """R4 — a reviewer judged the change wrong."""
    art_node, art_kind, art_ref, art_detail = artifacts
    node = art_node if art_kind == "review" else None
    evidence: list[dict[str, str]] = []
    if node:
        evidence.append(_ev("file", art_ref, f"reviewer {art_detail}"))
    elif (failed_node
          and str(fail_reason).lower() in ("needs_revision", "request_changes", "reject")
          and _stage_for(failed_node, str((ends.get(failed_node) or {}).get("node_type") or ""))
          == "review"):
        node = failed_node
        end = ends.get(failed_node) or {}
        evidence.append(_ev("event", f"run_events:{end.get('event_id') or '?'}",
                            f"reviewer {failed_node} verdict={fail_reason}"))
    if not node or not evidence:
        return None
    return _result(
        run_id, rule_id="R4", layer=LAYER_CHANGE, stage="review", node=node,
        confidence=0.85, evidence=evidence, routes_to=ROUTE_REVISE,
        summary=f"the reviewer asked for a revision at {node} — send the change back",
        ends=ends,
    )


def _rule_verifier(run_dir: Path, run_id: str, ends: dict[str, dict[str, Any]],
                   failed_node: str | None, fail_reason: str,
                   artifacts: tuple[str | None, str, str, str]) -> dict[str, Any] | None:
    """R5 — a verifier refuted the change."""
    art_node, art_kind, art_ref, art_detail = artifacts
    node = art_node if art_kind == "verifier" else None
    evidence: list[dict[str, str]] = []
    if node:
        evidence.append(_ev("file", art_ref, f"verifier {art_detail}"))
    elif failed_node and _stage_for(
            failed_node, str((ends.get(failed_node) or {}).get("node_type") or "")) == "verify":
        node = failed_node
        end = ends.get(failed_node) or {}
        evidence.append(_ev("event", f"run_events:{end.get('event_id') or '?'}",
                            f"verifier {failed_node} failed ({fail_reason or 'no reason'})"))
    if not node or not evidence:
        return None
    return _result(
        run_id, rule_id="R5", layer=LAYER_CHANGE, stage="verify", node=node,
        confidence=0.8, evidence=evidence, routes_to=ROUTE_REVISE,
        summary=f"a verifier refuted the change at {node} — send the change back",
        ends=ends,
    )


def _rule_interrupted(home: Path, run_id: str, events: list[dict[str, Any]] | None,
                      ends: dict[str, dict[str, Any]]) -> dict[str, Any] | None:
    """R6 — a node started and never ended.

    The *default* is provider / retry: a node that vanished mid-flight is
    transport trouble, not a framework bug — labelling it ``engine`` opened a
    spurious fix epic for every interruption. The one exception is a run a
    *person* stopped: ``board kill`` appends ``"killed-by-user"`` to
    ``task_runs.notes`` (``web/control.py`` ``_writeback_terminal``), and notes
    are append-only (``notes = COALESCE(notes || '; ', '') || ?``), so the
    marker is matched per ``"; "``-separated segment exactly as
    ``auto_repair.decide`` matches it.
    """
    open_nodes, last_open = _open_state(events)
    if not open_nodes:
        return None
    node = last_open if last_open in open_nodes else open_nodes[-1]
    node_type = str((ends.get(node) or {}).get("node_type") or "")
    row = next((ev for ev in reversed(events or [])
                if ev.get("event_type") == "node_start" and _node_of(ev)[0] == node), None)
    event_ref = f"run_events:{(row or {}).get('event_id') or '?'}"
    killed_by_user = any(seg.startswith(_NOTE_KILLED_BY_USER)
                         for seg in _note_segments(home, run_id))
    if killed_by_user:
        return _result(
            run_id, rule_id="R6", layer=LAYER_USER, stage=_stage_for(node, node_type),
            node=node, confidence=0.9,
            evidence=[_ev("event", event_ref,
                          f"{node} started and never ended — the run was stopped by the user"),
                      _ev("note", "task_runs.notes",
                          f"{_NOTE_KILLED_BY_USER}: a person stopped the run")],
            routes_to=ROUTE_NONE,
            summary=f"the run was stopped by the user during {node} — nothing to repair",
            ends=ends,
        )
    return _result(
        run_id, rule_id="R6", layer=LAYER_PROVIDER, stage=_stage_for(node, node_type),
        node=node, confidence=0.8,
        evidence=[_ev("event", event_ref,
                      f"{node} started and never ended — the run was interrupted mid-node")],
        routes_to=ROUTE_RETRY,
        summary=f"the run was interrupted during {node} — retry, it is not a code defect",
        ends=ends,
    )


def _rule_notes(run_id: str, segments: list[str], open_nodes: list[str],
                ends: dict[str, dict[str, Any]], failed_node: str | None,
                traceback_evidence: list[dict[str, str]]) -> dict[str, Any] | None:
    """RN — a ``task_runs.notes`` segment localises the cause.

    Evaluated after the artefact/event rules and before the fall-through: for a
    reaped or crashed lifecycle the notes are the only written cause. Order
    within is killed-by-user → reaped → planner-dispatch → lifecycle-exception,
    so the strongest signal wins when the append-only column carries several.
    Most interruptions never reach here — R6 already named the dangling node —
    but R6 declines when every node ended, and the notes are then all that is
    left.
    """
    if not segments:
        return None
    for seg in segments:
        if seg.startswith(_NOTE_KILLED_BY_USER):
            node = open_nodes[-1] if open_nodes else None
            return _result(
                run_id, rule_id="RN", layer=LAYER_USER,
                stage=_stage_for(node) if node else "unknown", node=node,
                confidence=0.9,
                evidence=[_ev("note", "task_runs.notes", seg)],
                routes_to=ROUTE_NONE,
                summary="the run was stopped by the user — nothing to repair",
                ends=ends,
            )
    for seg in segments:
        if seg.startswith(_NOTE_REAPED):
            node = open_nodes[-1] if open_nodes else None
            stage = _stage_for(node, "") if node else "unknown"
            return _result(
                run_id, rule_id="RN", layer=LAYER_PROVIDER, stage=stage, node=node,
                confidence=0.8,
                evidence=[_ev("note", "task_runs.notes", seg)],
                routes_to=ROUTE_RETRY,
                summary=(f"the reaper marked the run failed ({seg}) — the dispatcher "
                         f"died, retry it"),
                ends=ends,
            )
    for seg in segments:
        if _NOTE_PLANNER_DISPATCH_RC in seg:
            return _result(
                run_id, rule_id="RN", layer=LAYER_PROVIDER, stage="plan", node="planner",
                confidence=0.8,
                evidence=[_ev("note", "task_runs.notes", seg)],
                routes_to=ROUTE_RETRY,
                summary=f"the planner dispatch failed ({seg}) — retry the plan step",
                ends=ends,
            )
    for seg in segments:
        if _NOTE_LIFECYCLE_EXCEPTION in seg:
            if not traceback_evidence:
                # No traceback backs it: an exception in the lifecycle is not
                # enough to call it an engine fault. Stay unclassified.
                return None
            stage = _stage_for(failed_node,
                               str((ends.get(failed_node or "") or {}).get("node_type") or ""))
            return _result(
                run_id, rule_id="RN", layer=LAYER_ENGINE, stage=stage, node=failed_node,
                confidence=0.8,
                evidence=[_ev("note", "task_runs.notes", seg), *traceback_evidence],
                routes_to=ROUTE_FIX_EPIC,
                summary=f"the lifecycle crashed on an exception ({seg}) with a traceback "
                        f"in mini-ork's source — framework fix",
                ends=ends,
            )
    # ``lifecycle exited without a verdict`` is deliberately NOT matched here: a
    # model diagnoser owns it in a later epic, and guessing would be a lie.
    return None


def _rule_environment(home: Path, run_id: str, ends: dict[str, dict[str, Any]],
                      failed_node: str | None) -> dict[str, Any] | None:
    """R7 — an owner-only fix (environment / credentials / budget)."""
    hint = _hint(home, run_id)
    nc = hint.get("needs_change") if isinstance(hint, dict) else None
    nc = nc if isinstance(nc, dict) else None
    if not nc:
        return None
    kind = str(nc.get("kind") or "")
    from mini_ork.recovery import auto_repair
    if kind not in auto_repair._OWNER_KINDS:
        return None
    evidence = [_ev("hint", f"retry_hint:{run_id}",
                    f"needs_change.kind={kind}: {str(nc.get('summary') or '')[:200]}")]
    return _result(
        run_id, rule_id="R7", layer=LAYER_ENVIRONMENT, stage=_stage_for(failed_node),
        node=failed_node, confidence=0.6, evidence=evidence,
        routes_to=ROUTE_OWNER,
        summary=f"owner-only fix required ({kind}) — the loop cannot make it",
        ends=ends,
    )


def _rule_lane(home: Path, run_id: str, ends: dict[str, dict[str, Any]],
               failed_node: str | None) -> dict[str, Any] | None:
    """R8 — the retry hint points at an unavailable model lane."""
    hint = _hint(home, run_id)
    nc = hint.get("needs_change") if isinstance(hint, dict) else None
    nc = nc if isinstance(nc, dict) else None
    if not nc or str(nc.get("kind") or "") != "lane":
        return None
    evidence = [_ev("hint", f"retry_hint:{run_id}",
                    f"needs_change.kind=lane: {str(nc.get('summary') or '')[:200]}")]
    return _result(
        run_id, rule_id="R8", layer=LAYER_MODEL, stage=_stage_for(failed_node),
        node=failed_node, confidence=0.7, evidence=evidence,
        routes_to=ROUTE_SWAP_LANE,
        summary="the model lane is unavailable — swap the lane and retry",
        ends=ends,
    )


# ── the entry point ──────────────────────────────────────────────────────────


def _diagnose(home: Path, run_id: str) -> dict[str, Any]:
    run_dir = _run_dir(home, run_id)
    events = _lifecycle(home, run_id)
    ends = _latest_ends(events)
    failed_node, fail_reason = _failing(events)
    log = _tail(run_dir, failed_node)
    artifacts = _failing_artifacts(run_dir)
    open_nodes, _last_open = _open_state(events)
    # Computed once: both R2 and the lifecycle-exception note rule read it so
    # the two can never disagree about whether a traceback was found. The note
    # rule additionally accepts a traceback found in the run's own log when no
    # failing node localised it — R2 itself must still name a node.
    r2_evidence = _traceback_evidence(failed_node, fail_reason, log)
    lifecycle_evidence = r2_evidence or _log_traceback_evidence(run_dir)

    for rule in (
        lambda: _rule_publish_abstain(run_dir, run_id, ends),
        lambda: _rule_withheld_unproven(run_dir, run_id, ends),
        lambda: _rule_framework_error(run_dir, run_id, ends, failed_node, fail_reason, log),
        lambda: _rule_remote_unavailable(run_id, ends, failed_node, fail_reason),
        lambda: _rule_infra(home, run_id, run_dir, ends, failed_node, fail_reason, log),
        lambda: _rule_review(run_dir, run_id, ends, failed_node, fail_reason, artifacts),
        lambda: _rule_verifier(run_dir, run_id, ends, failed_node, fail_reason, artifacts),
        lambda: _rule_interrupted(home, run_id, events, ends),
        lambda: _rule_notes(run_id, _note_segments(home, run_id), open_nodes, ends,
                            failed_node, lifecycle_evidence),
        lambda: _rule_environment(home, run_id, ends, failed_node),
        lambda: _rule_lane(home, run_id, ends, failed_node),
    ):
        try:
            result = rule()
        except Exception:  # noqa: BLE001 — one rule's read failure must not stop the rest
            continue
        if result is not None:
            return result

    named = f" (last failing node: {failed_node})" if failed_node else ""
    return _fallthrough(
        run_id, LAYER_UNCLASSIFIED,
        f"no rule could cite evidence for this failure{named}",
    )


def _cached_model_diagnosis(home: Path, run_id: str) -> dict[str, Any] | None:
    """The persisted model diagnosis for this run, when one exists.

    The at-most-once gate: a ``diagnosis.json`` whose ``source`` is ``"model"``
    is the run's diagnosis already — re-dispatching a model for the same run
    would pay twice for the same answer.
    """
    data = _read_json(_run_dir(home, run_id) / DIAGNOSIS_FILE)
    if isinstance(data, dict) and str(data.get("source") or "") == SOURCE_MODEL:
        return data
    return None


def diagnose(home: Path, run_id: str, *, write: bool = True,
             allow_model: bool = False) -> dict[str, Any]:
    """The typed diagnosis for ``run_id``. Never raises.

    ``write=True`` persists ``<run_dir>/diagnosis.json`` (best effort — a
    read-only home degrades to "not written", never an error). ``write=False``
    is the read-only mode ``--replay`` and the IDE use.

    ``allow_model=True`` additionally asks the model diagnoser
    (:mod:`mini_ork.recovery.diagnoser_llm`) when — and only when — the rule set
    came back ``unclassified``. The rule result stands whenever the model
    declines, cannot ground its reply, or is refused as a self-diagnosis. A
    model diagnosis is computed AT MOST ONCE per run: a persisted
    ``diagnosis.json`` with ``source == "model"`` is reused verbatim instead of
    dispatching again.
    """
    home = Path(home)
    run_id = str(run_id)
    try:
        result = _diagnose(home, run_id)
    except Exception as exc:  # noqa: BLE001 — auto_repair calls this on every failed run
        result = _fallthrough(run_id, LAYER_UNCLASSIFIED,
                              f"the run could not be read ({type(exc).__name__}: {exc})")
    if allow_model and str(result.get("layer") or "") == LAYER_UNCLASSIFIED:
        cached = _cached_model_diagnosis(home, run_id)
        if cached is not None:
            result = cached
        else:
            try:
                from mini_ork.recovery.diagnoser_llm import diagnose_with_model
                model_result = diagnose_with_model(home, run_id, result)
            except Exception:  # noqa: BLE001 — the diagnoser must never break the loop
                model_result = None
            if model_result is not None:
                result = model_result
    if write:
        write_diagnosis(home, run_id, result)
    return result


def write_diagnosis(home: Path, run_id: str, result: dict[str, Any]) -> None:
    """Persist ``<run_dir>/diagnosis.json`` — best effort, never raises.

    Public because ``auto_repair.decide`` is deliberately side-effect-free: it
    computes the diagnosis with ``write=False`` and ``auto_repair.apply`` — the
    half that records the attempt — is what lands the file.
    """
    run_dir = _run_dir(home, run_id)
    if not run_dir.is_dir():
        return
    path = run_dir / DIAGNOSIS_FILE
    try:
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(result, indent=2), encoding="utf-8")
        tmp.replace(path)
    except OSError:
        pass


# ── replay (read-only) ───────────────────────────────────────────────────────

# The row fields the replay emits. ``source`` and the full ``evidence`` list are
# carried so a reader can audit *why* a layer was assigned, not only the verdict.
_RUN_ROW_FIELDS = (
    "run_id", "rule_id", "layer", "stage", "node", "source",
    "confidence", "evidence", "routes_to", "summary",
)


def _terminal_runs(home: Path, limit: int) -> list[str]:
    try:
        from mini_ork.web.db import db_for
        db = db_for(Path(home))
        if not db.has_table("task_runs"):
            return []
        rows = db.rows(
            "SELECT id FROM task_runs WHERE status IN ('failed', 'rolled_back') "
            "ORDER BY created_at DESC LIMIT ?",
            (int(limit),),
        )
    except Exception:  # noqa: BLE001
        return []
    return [str(r.get("id") or "") for r in rows if r.get("id")]


def replay(home: Path, *, limit: int = 25) -> dict[str, Any]:
    """Diagnose the most recent terminal failed runs — strictly read-only.

    Never writes a run dir and never touches the DB beyond the ``task_runs``
    read that selects the runs. The aggregate reports the per-rule and
    per-layer counts plus the unclassified share, which is the number that says
    whether the rule set is actually covering the failures it sees.
    """
    home = Path(home)
    run_ids = _terminal_runs(home, limit)
    runs: list[dict[str, Any]] = []
    by_rule: dict[str, int] = {}
    by_layer: dict[str, int] = {}
    for run_id in run_ids:
        d = diagnose(home, run_id, write=False)
        runs.append({k: d.get(k) for k in _RUN_ROW_FIELDS})
        by_rule[str(d.get("rule_id"))] = by_rule.get(str(d.get("rule_id")), 0) + 1
        by_layer[str(d.get("layer"))] = by_layer.get(str(d.get("layer")), 0) + 1
    total = len(runs)
    unclassified = by_layer.get(LAYER_UNCLASSIFIED, 0)
    return {
        "version": DIAGNOSIS_VERSION,
        "generated_at": _now_iso(),
        "home": str(home),
        "limit": int(limit),
        "runs": runs,
        "counts": {"by_rule": by_rule, "by_layer": by_layer},
        "unclassified_share": (unclassified / total) if total else 0.0,
    }


# ── CLI ──────────────────────────────────────────────────────────────────────


def _default_home() -> Path:
    from mini_ork.context import context_env
    return Path(context_env("MINI_ORK_HOME", "").strip() or (Path.cwd() / ".mini-ork"))


def _format(result: dict[str, Any]) -> str:
    lines = [
        f"{result.get('run_id')}: {result.get('layer')}/{result.get('stage')}"
        f"{'/' + str(result['node']) if result.get('node') else ''}"
        f"  [{result.get('rule_id')} src={result.get('source')} conf={result.get('confidence')}]"
        f"  → {result.get('routes_to')}",
        f"  {result.get('summary')}",
    ]
    for item in result.get("evidence") or []:
        lines.append(f"  - {item.get('kind')}: {item.get('ref')} — {item.get('excerpt')}")
    return "\n".join(lines)


def _format_replay(data: dict[str, Any]) -> str:
    lines = [f"replayed {len(data.get('runs') or [])} failed run(s) from {data.get('home')}"]
    lines.append(f"  by rule: {data['counts']['by_rule']}")
    lines.append(f"  by layer: {data['counts']['by_layer']}")
    lines.append(f"  unclassified share: {data.get('unclassified_share'):.2f}")
    for run in data.get("runs") or []:
        node = f"/{run['node']}" if run.get("node") else ""
        lines.append(f"  {run['run_id']}: {run['layer']}/{run['stage']}{node} "
                     f"[{run['rule_id']}] → {run['routes_to']}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python3 -m mini_ork.recovery.diagnose",
        description="Typed, evidence-cited diagnosis of a failed mini-ork run.",
    )
    parser.add_argument("run_id", nargs="?", default="",
                        help="the run to diagnose")
    parser.add_argument("--home", default="",
                        help="the mini-ork home (default: $MINI_ORK_HOME or ./.mini-ork)")
    parser.add_argument("--json", action="store_true", help="emit JSON")
    parser.add_argument("--no-write", action="store_true",
                        help="never write <run_dir>/diagnosis.json")
    parser.add_argument("--replay", action="store_true",
                        help="diagnose the most recent terminal failed runs (read-only)")
    parser.add_argument("--limit", type=int, default=25,
                        help="how many runs --replay reads (default 25)")
    parser.add_argument("--out", default="",
                        help="write the JSON payload to this path (same bytes as --json)")
    args = parser.parse_args(argv)

    home = Path(args.home).expanduser() if args.home else _default_home()

    if args.replay:
        data = replay(home, limit=args.limit)
        if args.out:
            _write_payload(Path(args.out).expanduser(), data)
        print(json.dumps(data, indent=2) if args.json else _format_replay(data))
        return 0

    if not args.run_id:
        parser.error("run_id is required unless --replay is given")
    result = diagnose(home, args.run_id, write=not args.no_write)
    if args.out:
        _write_payload(Path(args.out).expanduser(), result)
    print(json.dumps(result, indent=2) if args.json else _format(result))
    return 0


def _write_payload(path: Path, payload: dict[str, Any]) -> None:
    """Write the JSON payload to *path* — best effort, never raises."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    except OSError:
        pass


if __name__ == "__main__":
    raise SystemExit(main())
