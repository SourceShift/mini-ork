"""One run's outcome — the single answer to "how did this run end, and what next?".

Read-only: nothing here writes into the run dir. The card's ``status`` (the DB
row) always beats any verdict file, so a run the executor marked ``failed`` /
``rolled_back`` is never reported as done even when a stale ``verdict.json``
says ``pass: true``.

:func:`resolve` returns the shape the run page feeds to ``spec.triage`` and
``spec.callout``::

    {"state": "running"|"needs_you"|"failed"|"done",
     "tone": <colour>, "icon": str, "text": str, "detail": str,
     "counts": [{"t", "c"}], "actions": [button], "menu": [button],
     "callouts": [section]}

Rules, first match wins (``state_word`` names the branch):

2. **Needs you** — a pending retry gate, a cost pause, or a finished run with
   a worktree to review.
1. **Running** — in flight, nothing waiting on the operator.
3. **Failed / rolled back** — tone red; the retry actions come from
   ``retry_hint`` (lane switch, ack-change retry, resume-cost).
4. **Published** — tone green, " · verified" only when a verdict file passes.

Two terminal branches are intercepted ahead of that dispatch, because the
card's live ``task_state`` would otherwise route them to the wrong word:

0. **Landed elsewhere** — a failed row carrying ``landed.json`` is done.
0. **Withheld publish** — every node passed but the publisher abstained
   (``levels_decision == "abstain"``): tone orange, "Not published —
   <level> unverified", with Certify / Publish again. A level that is
   explicitly REFUTED, or a node that actually failed, instead renders the
   failed rule (the level's change is wrong, or the run died mid-node — a
   stale ``abstain`` report does not make it a "needs you" decision).
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from mini_ork.ide_pages import spec as S

# The five verification levels, in report order (``mini_ork.verify.levels``).
# Which levels count as "withheld" is NOT decided here — ``task_state``
# ``withheld_levels``/``withheld_publish`` own that rule; these names only drive
# the badge row.
LEVELS = ("applies", "executes", "target", "preserve", "contract")
_PROVEN = "PROVEN"
_REFUTED = "REFUTED"

_TERMINAL = ("published", "failed", "rolled_back")
_FAILED = ("failed", "rolled_back")


def _run_mod():
    """The sibling page module — imported lazily to keep ``run`` ⟷ ``outcome``
    a one-way top-level import (``run`` imports ``outcome``; ``outcome`` reaches
    back only at call time, when the module is fully loaded)."""
    from mini_ork.ide_pages import run as run_mod

    return run_mod


def _ts_mod():
    """``mini_ork.acp.task_state`` — the one owner of the landed / withheld rules.

    Reached lazily (like ``_run_mod``) so importing this page never drags the
    fleet projection in at module load. Every caller fails soft: a missing
    ``task_state`` degrades the card rather than raising into the board.
    """
    from mini_ork.acp import task_state as ts_mod

    return ts_mod


def _node_events(run) -> list[dict[str, Any]]:
    """This run's nodes shaped as ``task_state`` ``node_end`` events.

    The *degraded* lifecycle: ``run.nodes`` already folds the job to one row per
    node, but the card carries only ``finish_reason`` — no payload ``verdict`` /
    ``error`` — so this loses the failure signals ``_node_end_failure`` also
    reads. ``_lifecycle_events`` is the faithful source; this stands in only
    when the DB read fails, and then a ``finish_reason`` failure (``timeout``,
    ``error``) is still caught.
    """
    return [
        {"event_type": "node_end",
         "payload_json": {"node_id": n.id, "finish_reason": n.finish}}
        for n in run.nodes
    ]


def _lifecycle_events(run) -> list[dict[str, Any]] | None:
    """The run's real ``node_start`` / ``node_end`` rows, or ``None`` if unreadable.

    The same query ``fleet._steps`` and ``task_state``'s snapshot use, so this
    card applies "did a node fail?" to exactly the bytes the fleet row does. It
    matters for the common real crash shape: ``kill_run`` / the run reaper close
    a dangling start with ``{verdict: "CRASH", interrupted: true}`` and NO
    ``finish_reason`` (``web/control.py:_close_dangling_node_events``) — a
    ``run.nodes`` reconstruction keeps only ``finish_reason`` and would miss it.
    """
    try:
        from mini_ork.web.db import db_for
        from mini_ork.web.repositories import RunDetailRepository

        rows = RunDetailRepository(db_for(run.home)).fetch_node_lifecycle_events(run.id)
    except Exception:  # noqa: BLE001 — a missing DB degrades to the card's nodes
        return None
    return [r for r in rows if isinstance(r, dict)] if isinstance(rows, list) else None


def _events_for_gate(run) -> list[dict[str, Any]]:
    """The events to judge a node failure by: the real lifecycle, else run.nodes."""
    events = _lifecycle_events(run)
    return events if events is not None else _node_events(run)


def _failing_node_id(run) -> str | None:
    """The id of a node that failed, per ``task_state._failing_node``; else ``None``.

    ``None`` also when ``task_state`` cannot be imported — the caller then reads
    "no failing node" and renders a bare "Failed" rather than guessing one.
    """
    try:
        failing = _ts_mod()._failing_node(_events_for_gate(run))
    except Exception:  # noqa: BLE001 — task_state optional: never blank the card
        return None
    return failing[0] if failing else None


# ── cheap predicates (also used by ``run._graph`` for its state word) ───────

def _status(run) -> str:
    return str(run.card.get("status") or "")


def _ts_state(run) -> str:
    ts = run.card.get("task_state")
    return str((ts or {}).get("state") or "") if isinstance(ts, dict) else ""


def is_running(run) -> bool:
    """The run is in flight — its status is not one of the three terminal ones."""
    return _status(run) not in _TERMINAL


def state_word(run) -> str:
    """The outcome state word — one of ``running needs_you failed done``.

    Pure and cheap (no DB, no hint): it reads only the card's status and
    ``task_state``. ``run._graph`` calls it so the DAG's ``state`` cannot drift
    from the triage's.
    """
    if _ts_state(run) == "needs_you":
        return "needs_you"
    if is_running(run):
        return "running"
    if _status(run) in _FAILED:
        return "failed"
    if _status(run) == "published":
        return "done"
    return "running"


def _card_detail(run) -> str:
    detail = str(run.card.get("detail") or "").strip()
    if detail:
        return detail
    ts = run.card.get("task_state")
    return str((ts or {}).get("detail") or "").strip() if isinstance(ts, dict) else ""


def _step(run) -> str:
    ts = run.card.get("task_state")
    if isinstance(ts, dict) and str(ts.get("step") or "").strip():
        return str(ts["step"]).strip()
    if str(run.card.get("step") or "").strip():
        return str(run.card["step"]).strip()
    for node in run.nodes:
        if node.state == "running":
            return node.id
    return ""


# ── read-only file helpers ─────────────────────────────────────────────────

def _hint(run) -> dict[str, Any] | None:
    """The retry hint, or ``None``. Never writes (``write=False``)."""
    try:
        from mini_ork.recovery import retry_hint
    except Exception:  # noqa: BLE001 — a missing recovery module: no hint
        return None
    try:
        hint = retry_hint.load_or_compute(run.home, run.id, write=False)
    except Exception:  # noqa: BLE001 — a hint crash must not blank the outcome
        return None
    return hint if isinstance(hint, dict) else None


def _no_change_kinds() -> frozenset[str]:
    """``retry_hint.NO_CHANGE_KINDS`` — needs_change kinds whose retry needs no
    operator change (an ``interrupted`` run: re-running the step IS the fix).

    Empty when the recovery module is absent, so the branch simply never fires.
    """
    try:
        from mini_ork.recovery import retry_hint
    except Exception:  # noqa: BLE001 — a missing recovery module: no exemption
        return frozenset()
    return frozenset(getattr(retry_hint, "NO_CHANGE_KINDS", frozenset()))


def _review(run) -> dict[str, Any] | None:
    """``review-reviewer.json`` else the first ``review-*.json``."""
    rm = _run_mod()
    primary = run.run_dir / "review-reviewer.json"
    if primary.is_file():
        data = rm._json_obj(primary)
        if isinstance(data, dict):
            return data
    for path in sorted(run.run_dir.glob("review-*.json")):
        if path.name == "review-reviewer.json":
            continue
        data = rm._json_obj(path)
        if isinstance(data, dict):
            return data
    return None


def _run_verdict(run) -> dict[str, Any] | None:
    """The level report: ``run-verdict.json``, else ``verdict.json`` when execute
    stamped the levels there (recipes that do not own verdict.json)."""
    rm = _run_mod()
    data = rm._json_obj(run.run_dir / "run-verdict.json")
    if isinstance(data, dict):
        return data
    vj = rm._json_obj(run.run_dir / "verdict.json")
    if isinstance(vj, dict) and ("levels" in vj or "levels_decision" in vj):
        return vj
    return None


def _first_failing_verifier(run) -> str:
    rm = _run_mod()
    for path in sorted(run.run_dir.glob("verifier_*.json")):
        data = rm._json_obj(path)
        if not isinstance(data, dict) or data.get("pass") is not False:
            continue
        for key in ("error_summary", "reason", "detail"):
            val = str(data.get(key) or "").strip()
            if val:
                return val.splitlines()[0][:200]
    return ""


def _pending_gate(run) -> dict[str, Any] | None:
    try:
        from mini_ork.recovery import retry_notify
    except Exception:  # noqa: BLE001
        return None
    try:
        row = retry_notify.pending_fix_for_run(run.home, run.run_dir)
    except Exception:  # noqa: BLE001
        return None
    return row if isinstance(row, dict) else None


def _gate_hint(gate: dict[str, Any]) -> dict[str, Any]:
    ctxt = gate.get("context")
    if isinstance(ctxt, dict):
        hint = ctxt.get("hint")
        if isinstance(hint, dict):
            return hint
    hint = gate.get("hint")
    return hint if isinstance(hint, dict) else {}


# ── counts / menu ──────────────────────────────────────────────────────────

def _verifier_checks(run) -> tuple[int, int]:
    rm = _run_mod()
    passed = total = 0
    for path in sorted(run.run_dir.glob("verifier_*.json")):
        data = rm._json_obj(path)
        if not isinstance(data, dict) or "pass" not in data:
            continue
        total += 1
        if data.get("pass"):
            passed += 1
    return passed, total


def _findings_count(run) -> int | None:
    rev = _review(run)
    if not isinstance(rev, dict):
        return None
    findings = rev.get("findings")
    return len(findings) if isinstance(findings, list) else None


def _level_badges(run) -> list[dict[str, Any]]:
    rv = _run_verdict(run)
    levels = rv.get("levels") if isinstance(rv, dict) else None
    if not isinstance(levels, dict):
        return []
    out: list[dict[str, Any]] = []
    for name in LEVELS:
        value = str(levels.get(name) or "") if name in levels else ""
        if not value:
            continue
        colour = "green" if value == _PROVEN else ("red" if value == _REFUTED else "yellow")
        out.append({"t": f"{name} {value}", "c": colour})
    return out


def _counts(run) -> list[dict[str, Any]]:
    rm = _run_mod()
    out: list[dict[str, Any]] = []
    n = len(run.nodes)
    if n:
        done = sum(1 for node in run.nodes if node.state == "done")
        out.append({"t": f"{done}/{n} nodes", "c": "sub"})
    passed, total = _verifier_checks(run)
    if total:
        out.append({"t": f"checks {passed}/{total}",
                    "c": "green" if passed == total else "yellow"})
    findings = _findings_count(run)
    if findings is not None:
        out.append({"t": f"{findings} finding{'s' if findings != 1 else ''}",
                    "c": "red" if findings else "sub"})
    cost = rm._cost(run)
    if cost > 0:
        out.append({"t": S.money(cost), "c": "sub"})
    elapsed = rm._elapsed(run)
    if elapsed and elapsed != "—":
        out.append({"t": elapsed, "c": "sub"})
    out.extend(_level_badges(run))
    return out


def _menu(run, actions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    labels = {str(a.get("label") or "") for a in actions}
    out: list[dict[str, Any]] = []
    if "Certify this change" not in labels:
        out.append(S.btn("Certify this change", S.page_link("verify", "certify", run=run.id)))
    out.append(S.btn("Open run folder", S.reveal(str(run.run_dir)), "ghost"))
    web = _run_mod()._serve_url(run.id)
    if web:
        out.append(S.btn("Open in web UI", S.url(web), "ghost"))
    return out


# ── rule 1: running ────────────────────────────────────────────────────────

def _running(run) -> dict[str, Any]:
    step = _step(run)
    return {
        "state": "running",
        "tone": "yellow",
        "icon": "●",
        "text": f"Running · {step}" if step else "Running",
        "detail": "",
        "actions": _run_mod()._stop_kill_actions(run),
        "callouts": [],
    }


# ── rule 2: needs you ──────────────────────────────────────────────────────

def _needs_you(run) -> dict[str, Any]:
    gate = _pending_gate(run)
    if gate is not None:
        return _gate_needs_you(run, gate)
    if (run.run_dir / ".cost-pause").is_file():
        return {
            "state": "needs_you",
            "tone": "yellow",
            "icon": "✋",
            "text": "Paused at the cost cap",
            "detail": "",
            "actions": [S.btn("Resume",
                              S.cli("board", "resume", run.id,
                                    confirm=f"Resume cost-paused {run.id}?"),
                              "primary")],
            "callouts": [],
        }
    actions = _run_mod()._review_actions(run)
    return {
        "state": "needs_you",
        "tone": "yellow",
        "icon": "✋",
        "text": _card_detail(run) or "Ready to review",
        "detail": "",
        "actions": actions,
        "callouts": [],
    }


def _gate_needs_you(run, gate: dict[str, Any]) -> dict[str, Any]:
    hint = _gate_hint(gate)
    nc = hint.get("needs_change")
    summary = str((nc or {}).get("summary") or "") if isinstance(nc, dict) else ""
    inbox_id = str(gate.get("inbox_id") or "")
    steps: list[str] = []
    if hint:
        try:
            from mini_ork.recovery import retry_notify

            steps = retry_notify.fix_steps(hint, home=run.home)
        except Exception:  # noqa: BLE001 — a broken steps renderer must not blank the gate
            steps = []
    text_md = "\n".join(f"{i}. {s}" for i, s in enumerate(steps, 1))
    callout = S.callout(
        "This run needs your decision", text_md, tone="orange",
        actions=[
            S.btn("Approve retry",
                  S.cli("board", "gate", "approve", inbox_id,
                        confirm="Approve the retry and dispatch the fix?"),
                  "primary"),
            S.btn("Leave it",
                  S.cli("board", "gate", "reject", inbox_id,
                        confirm="Reject this retry and leave the run as it is?"),
                  "ghost"),
        ])
    return {
        "state": "needs_you",
        "tone": "orange",
        "icon": "?",
        "text": summary or "needs a fix",
        "detail": "",
        "actions": [],
        "callouts": [callout],
    }


# ── rule 3: failed / rolled back ───────────────────────────────────────────

def _failed(run, hint: dict[str, Any] | None) -> dict[str, Any]:
    card = _card_detail(run)
    if card == "Failed":  # task_state's FAILED_FALLBACK: no failing node recorded
        card = ""
    node = ""
    if isinstance(hint, dict):
        node = str(hint.get("from_node") or hint.get("failed_node") or "")
    if card:
        text = card
    elif _failing_node_id(run) is None:
        # No node in this run failed (per the fixed ``_failing_node``); the hint
        # may still name a best-effort node pulled from an impl log — a guess.
        # The kickoff forbids rendering it: the text stays "Failed" and the
        # hint's own summary carries the detail.
        text = "Failed"
    else:
        text = f"Failed at {node}" if node else "Failed"
    detail = _failure_detail(run, hint)
    actions, revised = _failed_actions(run, hint)
    if revised and revised not in detail:
        detail = f"{detail.rstrip('. ')}. {revised}" if detail else revised
    return {
        "state": "failed",
        "tone": "red",
        "icon": "✗",
        "text": text,
        "detail": detail,
        "actions": actions,
        "callouts": [],
    }


def _failure_detail(run, hint: dict[str, Any] | None) -> str:
    if isinstance(hint, dict):
        nc = hint.get("needs_change")
        summary = str((nc or {}).get("summary") or "") if isinstance(nc, dict) else ""
        if summary:
            return summary
    rev = _review(run)
    if isinstance(rev, dict):
        reasons = rev.get("reasons")
        if isinstance(reasons, list) and reasons:
            return str(reasons[0])
        findings = rev.get("findings")
        if isinstance(findings, list) and findings and isinstance(findings[0], dict):
            issue = str(findings[0].get("issue") or "")
            if issue:
                return issue
    return _first_failing_verifier(run)


def _failed_actions(run, hint: dict[str, Any] | None) -> tuple[list[dict[str, Any]], str]:
    """``(actions, revised_note)`` — ``revised_note`` is non-empty when the hint
    says the change itself must be revised (no retry button is offered).

    A retryable hint whose ``needs_change.kind`` is in
    ``retry_hint.NO_CHANGE_KINDS`` (an ``interrupted`` run) gets a single
    ``Resume from <node>`` button with no ``--ack-change`` — resuming IS the
    fix. A ``lane`` kind is satisfied by its ``--lane`` switch; any other
    ``needs_change`` is a real operator fix (``--ack-change``).
    """
    rm = _run_mod()
    actions: list[dict[str, Any]] = []
    revised = ""
    nc = hint.get("needs_change") if isinstance(hint, dict) else None
    if isinstance(hint, dict):
        if hint.get("retryable"):
            strategy = str(hint.get("strategy") or "")
            if strategy == "resume-cost":
                actions.append(S.btn("Resume",
                                     S.cli("board", "retry", run.id,
                                           confirm=f"Resume cost-paused {run.id}?"),
                                     "primary"))
            elif isinstance(nc, dict) and str(nc.get("kind") or "") == "lane":
                actions.extend(_lane_actions(run, nc))
            elif isinstance(nc, dict) and str(nc.get("kind") or "") in _no_change_kinds():
                node = str(hint.get("from_node") or hint.get("failed_node") or "")
                label = f"Resume from {node}" if node else "Resume"
                actions.append(S.btn(
                    label,
                    S.cli("board", "retry", run.id,
                          confirm=f"Resume {run.id} from {node}?" if node
                                  else f"Resume {run.id}?"),
                    "primary"))
            elif isinstance(nc, dict):
                actions.append(S.btn("I fixed it — retry",
                                     S.cli("board", "retry", run.id, "--ack-change",
                                           confirm=f"Retry {run.id} after the change?"),
                                     "primary"))
            else:
                node = str(hint.get("from_node") or hint.get("failed_node") or "")
                label = f"Retry from {node}" if node else "Retry"
                actions.append(S.btn(
                    label,
                    S.cli("board", "retry", run.id,
                          confirm=f"Retry from {node}?" if node else f"Retry {run.id}?"),
                    "primary"))
        else:
            revised = "The change itself must be revised."
    discard = rm._discard_action(run)
    if discard is not None:
        actions.append(discard)
    return actions, revised


def _lane_actions(run, nc: dict[str, Any]) -> list[dict[str, Any]]:
    """One primary button per suggested lane (max 3) + a default same-lane retry."""
    alias = str(nc.get("alias") or "")
    suggestions = nc.get("suggestions") if isinstance(nc.get("suggestions"), list) else []
    out: list[dict[str, Any]] = []
    for sug in suggestions[:3]:
        if not isinstance(sug, dict):
            continue
        lane = str(sug.get("lane") or "")
        if not lane:
            continue
        reason = str(sug.get("reason") or "").strip()
        confirm = f"Switch {alias} → {lane}?" + (f" {reason}" if reason else "")
        out.append(S.btn(f"Switch {alias} → {lane}",
                         S.cli("board", "retry", run.id, "--lane", f"{alias}={lane}",
                               confirm=confirm),
                         "primary"))
    out.append(S.btn("Retry on the same lane",
                     S.cli("board", "retry", run.id,
                           confirm=f"Retry {run.id} on the same lane?")))
    return out


# ── landed elsewhere / withheld publish (§4, §3) ───────────────────────────

def _landed(run) -> dict[str, Any] | None:
    """``landed.json`` for a terminal-failed run whose change landed elsewhere.

    ``{"commit", "repo", "note"}`` (the sha kept whole; the card truncates to
    9) or ``None`` when the file is missing / unparsable / carries no commit.
    Written by the operator or a later tool. Delegates to
    ``task_state.landed_report`` — the single reader ``task_state`` and
    ``run_mark`` already share, so the card can never disagree with the tile.
    """
    if _status(run) not in _FAILED:
        return None
    try:
        return _ts_mod().landed_report(run.run_dir)
    except Exception:  # noqa: BLE001 — task_state optional: no landed card
        return None


def _landed_out(run, landed: dict[str, Any]) -> dict[str, Any]:
    """The done card for a change delivered elsewhere (§4).

    "Open commit" reveals the repo directory — the closest surface the IDE has
    to a commit (there is no commit-opening verb; ``S.reveal`` is the kickoff's
    §4 action). The label is the kickoff's wording.
    """
    actions: list[dict[str, Any]] = []
    repo = landed["repo"]
    if repo and (Path(repo) / ".git").exists():
        actions.append(S.btn("Open commit", S.reveal(str(repo)), "primary"))
    return {
        "state": "done",
        "tone": "green",
        "icon": "✓",
        "text": f"Landed via {landed['commit'][:9]}",
        "detail": landed["note"],
        "actions": actions,
        "callouts": [],
    }


def _withheld(run) -> dict[str, Any] | None:
    """The withheld-publish card — every step passed, only the publisher abstained.

    ``None`` unless the run is terminal-failed and its level report says
    ``levels_decision == "abstain"`` (a retry gate or a cost pause outranks it,
    mirroring ``task_state``'s rule order). A level that is explicitly
    ``REFUTED``, or a node that actually failed, means the change itself is
    wrong, so that renders through the failed rule. Evaluated before the
    ``state_word`` dispatch: the card's live ``task_state`` routes a withheld
    run to ``needs_you``, which would otherwise reach ``_needs_you`` (the
    review/gate UX) instead of this card.

    Both the levels and the failing-node gate come from ``task_state``
    (``withheld_levels`` / ``withheld_publish``) so the card, the tile and the
    retry hint classify "withheld" by one rule. The gate is fed the run's real
    lifecycle rows (:func:`_lifecycle_events`) — the same bytes ``task_state``
    rule 2.5 and ``retry_hint`` judge — so a withheld verdict over a run that
    was killed or reaped mid-node renders through the failed rule here exactly
    as it does in the fleet row, and "Publish again" is never offered for a run
    whose re-run actually died.
    """
    if _status(run) not in _FAILED:
        return None
    if _pending_gate(run) is not None or (run.run_dir / ".cost-pause").is_file():
        return None
    try:
        ts = _ts_mod()
        raw = ts.withheld_levels(run.run_dir)
        if raw is None:
            return None
        # A node failed (a stale abstain verdict.json from an earlier attempt,
        # plus a recover re-run that died mid-node) → the failed rule owns it.
        # Gate on the SAME rule ``task_state`` rule 2.5 applies, over the run's
        # REAL lifecycle rows — the card's ``run.nodes`` keep only
        # ``finish_reason`` and would miss the reaper's verdict-only CRASH end.
        # Read-only, so the two probes share one try.
        gate = ts.withheld_publish(run.run_dir, _events_for_gate(run))
    except Exception:  # noqa: BLE001 — task_state optional: no withheld card
        return None
    unproven, refuted = raw
    if refuted:
        out = _failed(run, _hint(run))
        out["text"] = f"Not published — {', '.join(refuted)} refuted"
        return out
    if gate is None:
        return None
    rv = _run_verdict(run)
    rev = _review(run)
    verdict = str((rev or {}).get("verdict") or "").strip() or "pass"
    passed, total = _verifier_checks(run)
    detail = (
        f"Every step passed ({verdict}, checks {passed}/{total}). "
        "mini-ork publishes only when every level is PROVEN."
    )
    reasons = rv.get("levels_reasons") if isinstance(rv.get("levels_reasons"), dict) else {}
    lines = [
        f"{name}: {str(reasons.get(name) or '').strip()[:160]}"
        for name in unproven if str(reasons.get(name) or "").strip()
    ]
    if lines:
        detail += "\n" + "\n".join(lines)
    actions: list[dict[str, Any]] = [
        S.btn("Certify this change",
              S.page_link("verify", "certify", run=run.id), "primary"),
        S.btn("Publish again",
              S.cli("board", "retry", run.id,
                    confirm="Re-run the publisher? It publishes only if the "
                            "levels are now proven.")),
    ]
    for btn in _run_mod()._review_actions(run) or []:
        # "Certify this change" is THE primary action here — the reviewed card
        # must not carry two primaries (Certify + the Merge button that
        # ``_review_actions`` marks primary). Demote the copied Merge to ghost;
        # Discard already arrives as "danger".
        actions.append({**btn, "kind": "ghost"} if btn.get("kind") == "primary" else btn)
    return {
        "state": "needs_you",
        "tone": "orange",
        "icon": "?",
        "text": f"Not published — {', '.join(unproven)} unverified",
        "detail": detail,
        "actions": actions,
        "callouts": [],
    }


# ── rule 4: published ──────────────────────────────────────────────────────

def _verified(run) -> bool:
    rv = _run_verdict(run)
    if isinstance(rv, dict) and rv.get("levels_decision"):
        return str(rv.get("levels_decision")) == "publish"
    vj = _run_mod()._json_obj(run.run_dir / "verdict.json")
    return isinstance(vj, dict) and str(vj.get("verdict") or "") == "pass"


def _published(run) -> dict[str, Any]:
    return {
        "state": "done",
        "tone": "green",
        "icon": "✓",
        "text": "Published · verified" if _verified(run) else "Published",
        "detail": "",
        "actions": [S.btn("Certify this change",
                          S.page_link("verify", "certify", run=run.id))],
        "callouts": [],
    }


# ── entry point ────────────────────────────────────────────────────────────

def resolve(run) -> dict[str, Any]:
    """The run's outcome: ``state``, ``tone``, ``icon``, ``text``, ``detail``,
    ``counts``, ``actions``, ``menu`` and ``callouts``.

    Read-only — ``run`` is a :class:`mini_ork.ide_pages.run.Run`; every file the
    outcome reads is read with ``write=False`` (the retry hint) or not at all.
    """
    landed = _landed(run)
    withheld = None if landed is not None else _withheld(run)
    if landed is not None:
        out = _landed_out(run, landed)
    elif withheld is not None:
        out = withheld
    else:
        state = state_word(run)
        if state == "needs_you":
            out = _needs_you(run)
        elif state == "running":
            out = _running(run)
        elif state == "failed":
            out = _failed(run, _hint(run))
        else:
            out = _published(run)
    out.setdefault("callouts", [])
    out["counts"] = _counts(run)
    out["menu"] = _menu(run, out["actions"])
    return out
