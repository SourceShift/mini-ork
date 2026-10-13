"""Typed, evidence-cited diagnosis of a failed run.

``auto_repair.decide`` answers *what should happen next*; ``retry_hint`` answers
*what class of failure is this*. Neither names **who owns the failure** —
which layer (``engine`` / ``provider`` / ``change`` / ``operator``), which stage
(``plan`` / ``execute`` / ``verify`` / ``review`` / ``deliver``) and which node.
This module adds exactly that answer, as a first-match-wins rule set:

  * ``R1`` publish withheld at the publisher      → engine / deliver / reverify
  * ``R2`` traceback inside mini-ork's own source → engine / <stage> / framework-edit
  * ``R3`` infra or provider trouble              → provider / execute / retry
  * ``R4`` a reviewer asked for a revision        → change / review / revise
  * ``R5`` a verifier refuted the change          → change / verify / revise
  * ``R6`` a node started and never ended         → engine / execute / resume
  * ``R7`` an owner-only fix (environment/credentials/budget) → operator / human

and an **unclassified fall-through** at confidence ``0.0``.

Two invariants make the answer trustworthy:

  * **Evidence is load-bearing.** Every matching rule cites at least one
    evidence item (a file reference, a ``run_events`` id, a log excerpt or the
    retry hint's own field). A rule that cannot cite evidence does not match:
    the diagnosis degrades to ``unclassified`` rather than becoming a confident
    guess. The unclassified fall-through is the one result with no evidence.
  * **The done-node invariant.** ``node`` never names a node whose *latest*
    ``node_end`` finished ``done`` — a node that succeeded is not where the run
    stopped, so naming it sends the operator to a step with nothing to fix.
    The publisher's abstain is likewise read from its latest ``node_end``, so a
    publisher that was retried and later finished ``done`` is not misreported.

Read-only by construction: the module reads the run dir, ``run_events`` /
``node_attempts`` and the reused helpers, and (when ``write=True``) writes only
``<run_dir>/diagnosis.json``. It never raises — ``auto_repair.decide`` calls it
on every failed run, so a missing run dir, a half-written artefact or an absent
DB degrades to ``unclassified``.

CLI::

    python3 -m mini_ork.recovery.diagnose <run_id> [--json] [--no-write]
    python3 -m mini_ork.recovery.diagnose --replay [--limit N] [--json]

``--replay`` diagnoses the most recent terminal failed runs of the home and
prints an aggregate; it never writes anything.
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DIAGNOSIS_VERSION = 1
DIAGNOSIS_FILE = "diagnosis.json"
PUBLISH_ABSTAIN_FILE = "publish-abstain.json"

# Layers — who owns the fix.
LAYER_ENGINE = "engine"
LAYER_PROVIDER = "provider"
LAYER_CHANGE = "change"
LAYER_OPERATOR = "operator"
LAYER_UNCLASSIFIED = "unclassified"

RULE_UNCLASSIFIED = "unclassified"

# The publisher's own abstain signals. ``publisher.py`` returns them as the
# publisher node's ``finish_reason``; ``task_state._TERMINAL_OK_FINISH``
# deliberately treats ``abstain`` / ``levels_unverified`` as NOT failures (a
# withheld publish is not a crashed node), so R1 — not ``_failing_node`` — is
# what names them.
_ABSTAIN_FINISH = frozenset({"publish_abstain", "levels_unverified", "abstain"})

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


def _ev(kind: str, ref: str, detail: str) -> dict[str, str]:
    """One evidence item: what kind of citation this is, where, and what it says."""
    return {"kind": str(kind), "ref": str(ref), "detail": str(detail)[:400]}


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
            source: str, confidence: float, evidence: list[dict[str, str]],
            routes_to: str, summary: str,
            ends: dict[str, dict[str, Any]] | None = None) -> dict[str, Any]:
    return {
        "version": DIAGNOSIS_VERSION,
        "run_id": str(run_id),
        "rule_id": rule_id,
        "layer": layer,
        "stage": stage,
        "node": _guard_node(node, ends or {}),
        "source": source,
        "confidence": float(confidence),
        "evidence": list(evidence),
        "routes_to": routes_to,
        "summary": summary,
        "computed_at": _now_iso(),
    }


def _fallthrough(run_id: str, layer: str, summary: str) -> dict[str, Any]:
    """The unclassified result — no rule could cite evidence, so we say so.

    ``confidence`` is ``0.0`` and ``evidence`` is empty by construction: this is
    the honest "nothing localised this failure" answer, not a guess.
    """
    return {
        "version": DIAGNOSIS_VERSION,
        "run_id": str(run_id),
        "rule_id": RULE_UNCLASSIFIED,
        "layer": layer,
        "stage": "unknown",
        "node": None,
        "source": "",
        "confidence": 0.0,
        "evidence": [],
        "routes_to": "human",
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


def _rule_publish_abstain(run_dir: Path, run_id: str, ends: dict[str, dict[str, Any]]
                          ) -> dict[str, Any] | None:
    """R1 — the publisher withheld the publish."""
    from mini_ork.acp.task_state import withheld_publish
    pub = _last_match(ends, "publisher")
    reason = str((ends.get(pub) or {}).get("finish_reason") or "")
    if reason.lower() == "done":
        # The LATEST publisher end finished ``done``: it published. Whatever an
        # older abstain artefact still says, this run is not withheld.
        return None
    abstain = _read_json(run_dir / PUBLISH_ABSTAIN_FILE)
    abstain = abstain if isinstance(abstain, dict) else None
    if abstain is None and reason.lower() not in _ABSTAIN_FINISH:
        return None

    evidence: list[dict[str, str]] = []
    if abstain is not None:
        evidence.append(_ev("file", PUBLISH_ABSTAIN_FILE,
                            f"publish withheld: {abstain.get('reason') or '?'} — "
                            f"{str(abstain.get('detail') or '')[:200]}"))
    if pub and reason:
        evidence.append(_ev("event", f"run_events:{ends[pub].get('event_id') or '?'}",
                            f"publisher {pub} latest node_end finish_reason={reason}"))
    try:
        levels = withheld_publish(run_dir, None)
    except Exception:  # noqa: BLE001
        levels = None
    if levels:
        evidence.append(_ev("report", "verdict.json",
                            f"levels not proven: {', '.join(levels)}"))
    if not evidence:
        return None
    node = pub or "publisher"
    return _result(
        run_id, rule_id="R1", layer=LAYER_ENGINE, stage="deliver", node=node,
        source=evidence[0]["ref"], confidence=0.9, evidence=evidence,
        routes_to="reverify",
        summary=f"the publisher withheld the publish ({node}) — re-verify, do not revise",
        ends=ends,
    )


def _rule_framework_error(run_dir: Path, run_id: str, ends: dict[str, dict[str, Any]],
                          failed_node: str | None, fail_reason: str, log: str
                          ) -> dict[str, Any] | None:
    """R2 — a traceback inside mini-ork's own source (via ``triage.blame``)."""
    if not failed_node:
        return None
    from mini_ork.triage.blame import NodeFailure, attribute
    verdict, blamed = attribute(NodeFailure(
        node_id=failed_node, finish_reason=fail_reason or None, log_excerpt=log or "",
    ))
    if verdict != "mini_ork":
        return None
    evidence = [_ev("log", f"node:{failed_node}", f"{e.rule}: {e.detail}") for e in blamed]
    if not evidence and log:
        evidence = [_ev("log", f"node:{failed_node}", log[:300])]
    if not evidence:
        return None
    stage = _stage_for(failed_node, str((ends.get(failed_node) or {}).get("node_type") or ""))
    return _result(
        run_id, rule_id="R2", layer=LAYER_ENGINE, stage=stage, node=failed_node,
        source=evidence[0]["ref"], confidence=0.85, evidence=evidence,
        routes_to="framework-edit",
        summary=f"an error inside mini-ork's own source at {failed_node} — framework fix",
        ends=ends,
    )


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
        source=evidence[0]["ref"], confidence=0.8, evidence=evidence,
        routes_to="retry",
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
        source=evidence[0]["ref"], confidence=0.85, evidence=evidence,
        routes_to="revise",
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
        source=evidence[0]["ref"], confidence=0.8, evidence=evidence,
        routes_to="revise",
        summary=f"a verifier refuted the change at {node} — send the change back",
        ends=ends,
    )


def _rule_interrupted(run_id: str, events: list[dict[str, Any]] | None,
                      ends: dict[str, dict[str, Any]]) -> dict[str, Any] | None:
    """R6 — a node started and never ended."""
    if not events:
        return None
    starts: dict[str, int] = {}
    ends_count: dict[str, int] = {}
    last_open = ""
    for ev in events:
        node_id, _t = _node_of(ev)
        if not node_id:
            continue
        if ev.get("event_type") == "node_start":
            starts[node_id] = starts.get(node_id, 0) + 1
            last_open = node_id
        elif ev.get("event_type") == "node_end":
            ends_count[node_id] = ends_count.get(node_id, 0) + 1
    open_nodes = [n for n in starts if starts[n] > ends_count.get(n, 0)]
    if not open_nodes:
        return None
    node = last_open if last_open in open_nodes else open_nodes[-1]
    row = next((ev for ev in reversed(events)
                if ev.get("event_type") == "node_start" and _node_of(ev)[0] == node), None)
    evidence = [_ev("event", f"run_events:{(row or {}).get('event_id') or '?'}",
                    f"{node} started and never ended — the run was interrupted mid-node")]
    return _result(
        run_id, rule_id="R6", layer=LAYER_ENGINE, stage="execute", node=node,
        source=evidence[0]["ref"], confidence=0.9, evidence=evidence,
        routes_to="resume",
        summary=f"the run was interrupted during {node} — resume it",
        ends=ends,
    )


def _rule_operator(home: Path, run_id: str, ends: dict[str, dict[str, Any]],
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
        run_id, rule_id="R7", layer=LAYER_OPERATOR, stage=_stage_for(failed_node),
        node=failed_node, source=evidence[0]["ref"], confidence=0.6, evidence=evidence,
        routes_to="human",
        summary=f"owner-only fix required ({kind}) — the loop cannot make it",
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

    for rule in (
        lambda: _rule_publish_abstain(run_dir, run_id, ends),
        lambda: _rule_framework_error(run_dir, run_id, ends, failed_node, fail_reason, log),
        lambda: _rule_infra(home, run_id, run_dir, ends, failed_node, fail_reason, log),
        lambda: _rule_review(run_dir, run_id, ends, failed_node, fail_reason, artifacts),
        lambda: _rule_verifier(run_dir, run_id, ends, failed_node, fail_reason, artifacts),
        lambda: _rule_interrupted(run_id, events, ends),
        lambda: _rule_operator(home, run_id, ends, failed_node),
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


def diagnose(home: Path, run_id: str, *, write: bool = True) -> dict[str, Any]:
    """The typed diagnosis for ``run_id``. Never raises.

    ``write=True`` persists ``<run_dir>/diagnosis.json`` (best effort — a
    read-only home degrades to "not written", never an error). ``write=False``
    is the read-only mode ``--replay`` and the IDE use.
    """
    home = Path(home)
    run_id = str(run_id)
    try:
        result = _diagnose(home, run_id)
    except Exception as exc:  # noqa: BLE001 — auto_repair calls this on every failed run
        result = _fallthrough(run_id, LAYER_UNCLASSIFIED,
                              f"the run could not be read ({type(exc).__name__}: {exc})")
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
        runs.append({k: d.get(k) for k in
                     ("run_id", "rule_id", "layer", "stage", "node",
                      "confidence", "routes_to", "summary")})
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
        f"  [{result.get('rule_id')} conf={result.get('confidence')}]"
        f"  → {result.get('routes_to')}",
        f"  {result.get('summary')}",
    ]
    for item in result.get("evidence") or []:
        lines.append(f"  - {item.get('kind')}: {item.get('ref')} — {item.get('detail')}")
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
    args = parser.parse_args(argv)

    home = Path(args.home).expanduser() if args.home else _default_home()

    if args.replay:
        data = replay(home, limit=args.limit)
        print(json.dumps(data, indent=2) if args.json else _format_replay(data))
        return 0

    if not args.run_id:
        parser.error("run_id is required unless --replay is given")
    result = diagnose(home, args.run_id, write=not args.no_write)
    print(json.dumps(result, indent=2) if args.json else _format(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
