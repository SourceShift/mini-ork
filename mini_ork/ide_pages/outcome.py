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
"""
from __future__ import annotations

from typing import Any

from mini_ork.ide_pages import spec as S

# The five verification levels, in report order (``mini_ork.verify.levels``).
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
    node = ""
    if isinstance(hint, dict):
        node = str(hint.get("from_node") or hint.get("failed_node") or "")
    card = _card_detail(run)
    if card == "Failed":  # task_state's FAILED_FALLBACK: no failing node recorded
        card = ""
    text = card or (f"Failed at {node}" if node else "Failed")
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
    says the change itself must be revised (no retry button is offered)."""
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
