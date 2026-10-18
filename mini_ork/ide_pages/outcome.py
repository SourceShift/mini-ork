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

import json
import time
from pathlib import Path
from typing import Any

from mini_ork import run_status as _run_status
from mini_ork.ide_pages import spec as S

# The five verification levels, in report order (``mini_ork.verify.levels``).
# Which levels count as "withheld" is NOT decided here — ``task_state``
# ``withheld_levels``/``withheld_publish`` own that rule; these names only drive
# the badge row.
LEVELS = ("applies", "executes", "target", "preserve", "contract")
_PROVEN = "PROVEN"
_REFUTED = "REFUTED"

# The two sets differ, and the badge rows that read them care: a `not_needed`
# run is over, but it is not a failure to explain or repair.
_TERMINAL = tuple(sorted(_run_status.TERMINAL))
_FAILED = tuple(sorted(_run_status.UNSUCCESSFUL))


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

# ── seen-before helpers (read-only, never call retrieve) ───────────────────
#
# The ``mini-ork cn seen-before <run-id>`` verb writes
# ``<run_dir>/contextnest-seen-before.json`` once per run. The failed
# outcome card reads THAT file verbatim — the page render path never
# re-hits ContextNest (retrieve takes 8–38 s live; calling it on the
# page-render path blows the page budget). When the file is absent, the
# card surfaces a one-line "Find similar past failures" button that runs
# the verb (the only path that writes the file). The same wire shape
# feeds the failed node page.

_SEEN_BEFORE_FILE = "contextnest-seen-before.json"
_SEEN_BEFORE_PAGE_LIMIT = 3


def _seen_before_path(run) -> str:
    """Absolute path to the run's stored seen-before envelope.

    The same convention the run page uses: ``$MINI_ORK_HOME/runs/<run-id>``.
    Returns ``""`` when the run has no run_dir on disk; the caller
    treats that as "no stored file" and renders the button row.
    """
    run_dir = getattr(run, "run_dir", None)
    if run_dir is None:
        return ""
    return str(run_dir / _SEEN_BEFORE_FILE)


def _read_seen_before(path: str) -> dict[str, Any] | None:
    """Best-effort read of the stored envelope, or ``None`` on miss / parse error.

    A missing or unparseable file is the same as "not yet cached": the
    page renders the button row, not a broken-JSON error. A real envelope
    carries ``hits: [{id, content, importance, similarity, metadata{},
    session_id}]`` in the verified live 2026-10-15 shape; we walk the
    top :data:`_SEEN_BEFORE_PAGE_LIMIT` by similarity.
    """
    if not path:
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.loads(fh.read())
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _seen_before_cards(envelope: dict[str, Any], *, run_id: str) -> list[dict[str, Any]]:
    """Up to three evidence cards for the failed outcome card.

    Each card's ``t`` is the hit's content (truncated to 240 chars so a
    long memory does not push the action buttons off the card), ``sub``
    carries ``sim=<score> age=<since>`` (age in compact "Nd"/"Nh" form,
    never the raw epoch), and the row's ``do`` opens the hit's
    ``cn_session`` page so the operator can drill into the trajectory.
    Each card also carries the ``Helped`` / ``Wrong`` buttons, bound to
    *that card's* atom id (the hit's ``id``) — see
    :func:`_card_outcome_actions`.
    """
    raw_hits = envelope.get("hits") if isinstance(envelope, dict) else None
    if not isinstance(raw_hits, list):
        return []
    sorted_hits = sorted(
        (h for h in raw_hits if isinstance(h, dict)),
        key=lambda h: float(h.get("similarity") or 0.0),
        reverse=True,
    )
    ts = int(envelope.get("ts") or 0)
    cards: list[dict[str, Any]] = []
    for h in sorted_hits[:_SEEN_BEFORE_PAGE_LIMIT]:
        content = str(h.get("content") or "").strip().replace("\n", " ")
        if len(content) > 240:
            content = content[:237] + "..."
        try:
            sim = float(h.get("similarity") or 0.0)
        except (TypeError, ValueError):
            sim = 0.0
        age = _compact_age(ts)
        sub = f"sim={sim:.2f} · {age}"
        sid = h.get("session_id")
        acts: list[dict[str, Any]] = []
        if isinstance(sid, str) and sid:
            acts.append(S.btn("Open session",
                              S.page_link("cn_session", id=sid), "ghost"))
        atom_id = h.get("id")
        if isinstance(atom_id, str) and atom_id:
            acts.extend(_card_outcome_actions(atom_id, run_id))
        cards.append({
            "m": "•",
            "mc": "sub",
            "t": content or "(empty memory)",
            "tc": "text",
            "sub": sub,
            "mono": False,
            "when": "",
            "acts": acts,
        })
    return cards


def _compact_age(ts: int) -> str:
    """A short age string ("just now", "Nh", "Nd") for the card sub-line.

    Missing or non-positive ``ts`` returns ``""`` so the sub-line drops
    the bullet rather than showing a wall-clock stamp the operator
    cannot place.
    """
    if not ts or ts <= 0:
        return ""
    delta = max(0, int(time.time()) - int(ts))
    if delta < 60:
        return "just now"
    if delta < 3600:
        return f"{delta // 60}m ago"
    if delta < 86400:
        return f"{delta // 3600}h ago"
    return f"{delta // 86400}d ago"


def _seen_before_section(run) -> dict[str, Any] | None:
    """The Seen-before evidence section for the failed outcome card.

    Renders NOTHING for a published / running / needs-you outcome — the
    section is failure-only, because the cache is the failing-run cache.
    The section is a ``list`` of up to three evidence cards plus the
    Helped/Wrong action row when a stored envelope is present; when no
    file is on disk, the section is a single button row that runs
    ``mini-ork cn seen-before <run-id> --node <failing-node>`` so the
    next render will have something to show.

    The page NEVER calls retrieve — that lives in the verb. The Helped/
    Wrong buttons are confirm-gated; the IDE surfaces a confirm prompt
    on click, the CLI requires ``--yes`` (see
    :func:`mini_ork.cli.cn_cmd._cmd_outcome`).
    """
    if state_word(run) != "failed":
        return None
    run_id = str(getattr(run, "id", "") or "")
    if not run_id:
        return None
    path = _seen_before_path(run)
    envelope = _read_seen_before(path)
    if envelope is not None:
        cards = _seen_before_cards(envelope, run_id=run_id)
        if not cards:
            # Stored file but empty hit list — the verb succeeded with
            # nothing to show. A single "no similar past failures" row
            # plus the Helped/Wrong action row is the truthful state.
            cards = [{
                "m": "•",
                "mc": "sub",
                "t": "No similar past failures in ContextNest.",
                "tc": "text",
                "sub": "",
                "mono": False,
                "when": "",
                "acts": [],
            }]
        # Helped / Wrong live on each card's acts, bound to that card's
        # atom id — a verdict lands on the memory that was shown, not on
        # a run-scoped row the operator never saw.
        return S.lst("Seen before", cards, full=True)
    # No stored file. Surface the single "Find similar past failures"
    # button. The button's CLI dispatches the verb on click — the page
    # render itself is cache-slot-only.
    failing = _failing_node_id(run) or ""
    btn_args: list[str] = ["cn", "seen-before", run_id]
    if failing:
        btn_args += ["--node", failing]
    button = S.btn("Find similar past failures",
                   S.cli(*btn_args,
                         confirm=f"Run the seen-before retrieve for {run_id}?",
                         home=False))
    return S.lst("Seen before", [{
        "m": "•",
        "mc": "sub",
        "t": "No retrieve has been run for this run yet.",
        "tc": "text",
        "sub": "",
        "mono": False,
        "when": "",
        "acts": [button],
    }], full=True)


def _card_outcome_actions(atom_id: str, run_id: str) -> list[dict[str, Any]]:
    """The Helped / Wrong buttons for one seen-before evidence card.

    Confirm-gated through :func:`S.cli` — the IDE shows a confirm
    prompt, the CLI asks on the TTY. The action settles *this card's*
    atom id (the hit's ``id``), so the operator's verdict lands on the
    memory that was shown; ``--run`` carries the failing run's id for
    provenance. The page passes ``home=False`` because ``mini-ork cn
    outcome`` has no ``--home`` flag.
    """
    helped = S.btn(
        "Helped",
        S.cli("cn", "outcome", atom_id, "success", "--run", run_id,
              confirm=f"Record {atom_id} as 'helped' for {run_id}?",
              home=False),
        "ghost",
    )
    wrong = S.btn(
        "Wrong",
        S.cli("cn", "outcome", atom_id, "failure", "--run", run_id,
              confirm=f"Record {atom_id} as 'wrong' for {run_id}?",
              home=False),
        "ghost",
    )
    return [helped, wrong]


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


# ── display words ──────────────────────────────────────────────────────────
#
# One table, one helper. The page is read by a person deciding "retry or
# abandon", so a database enum must never reach a chip or the triage line. Every
# emit site calls ``display_word`` rather than spelling the substitution inline:
# a new enum then gets human words in one place instead of leaking snake_case.

_DISPLAY_WORDS = {
    "needs_change": "Needs changes",
    "needs_you": "Needs you",
    "awaiting_review": "Awaiting review",
    "passed": "Passed",
    "failed": "Failed",
    "done": "Done",
    "running": "Running",
    "published": "Published",
    "rolled_back": "Rolled back",
    "not_needed": "Not needed",
    "reviewing": "Reviewing",
    "pending": "Pending",
    "skipped": "Skipped",
}


def chip_word(run, state: str) -> str:
    """The header chip's word for the outcome ``state``.

    A run the database marks failed always says so on the chip, even when the
    card's own word is ``needs_you`` (a retry gate is open): the reader must
    not see "Needs you" on a run the database calls failed. Every other state
    maps straight through :func:`display_word`, so a parked run still reads
    "Needs you" and a published one "Done".
    """
    if state == "needs_you" and _status(run) in _FAILED:
        return display_word("failed")
    return display_word(state)


def display_word(value: Any) -> str:
    """``value`` as display text a person reads — never a database enum.

    The table names the enums the run page actually renders; anything else is
    spaced and capitalised, so a newly added enum still cannot put snake_case
    on a chip. Empty input stays empty (the caller omits the field).
    """
    raw = str(value or "").strip()
    if not raw:
        return ""
    known = _DISPLAY_WORDS.get(raw.casefold())
    if known:
        return known
    words = raw.replace("_", " ").strip()
    return words[:1].upper() + words[1:]


# ── triage subject ─────────────────────────────────────────────────────────

# A node in one of these states is finished and must never be named as the
# thing that failed or blocked the run (kickoff c:0).
_PASSED_STATES = ("done", "passed")


def _first_failed_node(run):
    """The first node whose state is ``failed`` in story order, or ``None``."""
    for node in run.nodes:
        if str(getattr(node, "state", "") or "") == "failed":
            return node
    return None


# ── fault attribution ──────────────────────────────────────────────────────
#
# J24/C25: a failed run must answer "is this a mini-ork bug or a task fault?".
# The verdict is NOT re-derived here — ``mini_ork.triage.blame.attribute`` owns
# the rules (framework traceback frames → ``mini_ork``, a legitimate artifact
# verdict or consumer-side traceback → ``consumer``, infra / unlocalisable →
# ``unknown``). This page only feeds it the fields the run carries and renders
# the answer. Every failure on the way — a missing module, an unreadable or
# absent node log, a malformed node — fails closed to ``unknown``: a broken
# diagnostic must never blank the outcome card or raise into the board.

_BLAME_CALLOUT_TITLE = "Whose fault is this"
_OPEN_TRIAGE_CONFIRM = "File a bug and queue a framework-edit fix epic?"


def _failed_node_log(run, node_id: str) -> str:
    """A bounded tail of the failed node's log, or ``""`` — never raises.

    ``triage.failures._find_node_log`` already knows every stem a node's log is
    written under (``verifier-<stem>.log``, ``<stem>.log``, ``impl-<stem>.log``,
    ``logs/``, ``evidence/``) and reads only a bounded tail, so the page render
    path reuses it rather than re-deriving the convention. Imported lazily: the
    page never drags the triage I/O driver in at module load.
    """
    run_dir = getattr(run, "run_dir", None)
    if run_dir is None or not node_id:
        return ""
    try:
        from mini_ork.triage import failures as failures_mod

        return failures_mod._find_node_log(Path(run_dir), node_id)
    except Exception:  # noqa: BLE001 — a log read must never blank the card
        return ""


def _attribute_failure(run) -> tuple[str, list[Any]]:
    """``(verdict, evidence)`` for the story's first failed node, fail-closed.

    Uses exactly the fields the run page has (node id, type, ``finish`` and a
    best-effort log excerpt); ``NodeFailure`` carries no exit code, and inventing
    one would mean editing ``mini_ork/triage/`` (out of scope). Any failure —
    missing ``triage`` module, unreadable log, bad node data — returns
    ``("unknown", [])``: "we could not tell" is the honest verdict, never a
    guess at the framework's fault.
    """
    try:
        from mini_ork.triage import blame as blame_mod

        first = _first_failed_node(run)
        if first is None:
            failure = blame_mod.NodeFailure(node_id="")
        else:
            node_id = str(getattr(first, "id", "") or "")
            failure = blame_mod.NodeFailure(
                node_id=node_id,
                node_type=str(getattr(first, "type", "") or ""),
                finish_reason=str(getattr(first, "finish", "") or "") or None,
                log_excerpt=_failed_node_log(run, node_id),
            )
        verdict, evidence = blame_mod.attribute(failure)
        return str(verdict), list(evidence)
    except Exception:  # noqa: BLE001 — attribution optional: the card still renders
        return "unknown", []


def _first_evidence_line(evidence: list[Any]) -> str:
    """The first non-empty ``Evidence.detail``, or ``""``."""
    for item in evidence:
        detail = str(getattr(item, "detail", "") or "").strip()
        if detail:
            return detail
    return ""


def _blame_sentence(verdict: str, evidence: list[Any]) -> str:
    """The kickoff's one plain-words sentence for the blame verdict.

    Each sentence names the first evidence line, so the operator can see *why*
    the verdict was reached instead of trusting an unattributed label.
    """
    line = _first_evidence_line(evidence) or "no evidence line was recorded"
    if verdict == "mini_ork":
        return f"Looks like a mini-ork fault ({line}) — fix it at the source."
    if verdict == "consumer":
        return f"Looks like a task fault ({line}) — retry the failure, or adjust the kickoff."
    return ("Can't tell yet whether this is a mini-ork fault or a task fault "
            f"— the evidence is {line}.")


def _blame_callout(run, verdict: str, evidence: list[Any]) -> dict[str, Any]:
    """The failure-only blame callout, with ``Open triage`` on a mini-ork fault.

    ``Open triage`` writes (it files a bug and queues a fix epic), so it is
    confirm-gated; ``consumer`` / ``unknown`` add no button — the card's retry
    actions already carry the task-fault path, and duplicating a retry here
    would race the needs-you lane's single decision.
    """
    actions: list[dict[str, Any]] = []
    if verdict == "mini_ork":
        actions.append(S.btn(
            "Open triage",
            S.cli("triage", "--run", run.id, "--promote",
                  confirm=_OPEN_TRIAGE_CONFIRM),
            "primary",
        ))
    tone = "red" if verdict == "mini_ork" else "orange"
    return S.callout(_BLAME_CALLOUT_TITLE, _blame_sentence(verdict, evidence),
                     tone=tone, actions=actions)


def _nameable(run, node_id: str) -> str:
    """``node_id`` unless the story says that node is done/passed (then ``""``).

    The lifecycle can disagree with the folded story after a recover re-run; the
    page never names a node it also draws as finished.
    """
    for node in run.nodes:
        if node.id == node_id:
            return "" if str(node.state or "") in _PASSED_STATES else node_id
    return node_id


def _reason_for(run, node_id: str) -> str:
    """``run._node_reason`` for ``node_id`` — its review → verifier → hint
    fallbacks, imported rather than re-implemented (kickoff c:1)."""
    for node in run.nodes:
        if node.id == node_id:
            try:
                return str(_run_mod()._node_reason(run, node) or "").strip()
            except Exception:  # noqa: BLE001 — a reason is never worth the page
                return ""
    return ""


def _triage_subject(run) -> tuple[str, str]:
    """The node the triage names and its one-line reason, or ``("", "")``.

    The kickoff's rule order, first match wins:

    1. the first node whose state is ``failed`` in story order;
    2. else the node awaiting a human — a pending review, or the node an open
       ask blocks on;
    3. else the node of the open retry gate the callout is about.

    A ``done``/``passed`` node is never named. When none of the rules finds a
    node the caller omits the field rather than guessing.
    """
    failed = _first_failed_node(run)
    if failed is not None:
        return failed.id, _reason_for(run, failed.id)

    rm = _run_mod()
    try:
        awaiting = rm._awaiting_node_ids(run)
    except Exception:  # noqa: BLE001 — awaiting a human is optional
        awaiting = []
    for node_id in awaiting:
        # The done/passed guard is absolute (c:0): a block is named only when
        # the story does not already draw that node as finished. When the
        # pending review's own node has finished, fall through to the next
        # awaiting node and then to the gate rather than naming a passed node.
        nameable = _nameable(run, node_id) if node_id else ""
        if nameable:
            return nameable, ""

    gate = _pending_gate(run)
    if isinstance(gate, dict):
        hint = _gate_hint(gate)
        for key in ("from_node", "failed_node", "node_id", "node"):
            node_id = str(hint.get(key) or "").strip()
            nameable = _nameable(run, node_id) if node_id else ""
            if nameable:
                return nameable, _reason_for(run, nameable)
        ctxt = gate.get("context") if isinstance(gate.get("context"), dict) else {}
        for key in ("node_id", "node"):
            node_id = str(ctxt.get(key) or "").strip()
            nameable = _nameable(run, node_id) if node_id else ""
            if nameable:
                return nameable, _reason_for(run, nameable)

    return "", ""


# ── counts / menu ──────────────────────────────────────────────────────────

def _verifier_checks(run) -> tuple[int, int]:
    """Delegates to :func:`mini_ork.ide_pages.run.verifier_check_counts`, which
    the races panel also calls — the two pages must not disagree about one run."""
    return _run_mod().verifier_check_counts(run.run_dir)


def _findings_count(run) -> int | None:
    rev = _review(run)
    if not isinstance(rev, dict):
        return None
    findings = rev.get("findings")
    return len(findings) if isinstance(findings, list) else None


def verification_report(run) -> dict[str, Any] | None:
    """The run's five-level report — ``run-verdict.json`` or a stamped verdict.

    Public because the run page's Verification tab ships the whole vector as
    data (``spec.levels``); the verdict row (:func:`_level_badges`) stays a
    plain list of facts.
    """
    return _run_verdict(run)


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
    """The evidence badges under the verdict: checks, findings, levels.

    Progress (nodes), cost and elapsed are the header chips' job. Repeating
    them here made the verdict restate the chrome directly above it, so this
    list answers only what the chips cannot: did the evidence pass, and what
    did the checks find.
    """
    out: list[dict[str, Any]] = []
    passed, total = _verifier_checks(run)
    if total:
        out.append({"t": f"checks {passed}/{total}",
                    "c": "green" if passed == total else "yellow"})
    findings = _findings_count(run)
    if findings is not None:
        out.append({"t": f"{findings} finding{'s' if findings != 1 else ''}",
                    "c": "red" if findings else "sub"})
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


# The callout title is a contract with ``run._attention_lane``: once the
# needs-you lane carries this decision, the story tab drops the callout so the
# retry decision renders exactly once.
GATE_CALLOUT_TITLE = "This run needs your decision"

# ``retry_hint`` strategies rendered as the operator-facing verb (the kickoff's
# ``Re-verify`` / ``Retry`` / ``Merge``), never as the raw enum.
_STRATEGY_WORDS = {
    "verify": "Re-verify",
    "re-verify": "Re-verify",
    "resume": "Resume",
    "resume-cost": "Resume from the cost pause",
    "retry": "Retry",
    "merge": "Merge",
    "none": "Retry",
}


def _strategy_word(raw: Any) -> str:
    text = str(raw or "").strip()
    if not text:
        return "Retry"
    return _STRATEGY_WORDS.get(text.lower(), f"Retry ({text})")


def _gate_merged_hint(run, gate: dict[str, Any]) -> dict[str, Any]:
    """The gate's own hint with anything it omits filled from ``retry-hint.json``.

    The gate row is authoritative for the fields it carries; the persisted hint
    is the fallback for the common case where ``fix_steps`` renders empty (a
    retry-hint-only gate). Both reads fail soft to ``{}``.
    """
    merged: dict[str, Any] = {}
    persisted = _hint(run)
    if isinstance(persisted, dict):
        merged.update(persisted)
    own = _gate_hint(gate)
    merged.update(own)
    return merged


def _estimated_cost(hint: dict[str, Any]) -> float | None:
    """The hint's cost estimate, when it carries one (``None`` otherwise)."""
    for key in ("estimated_cost_usd", "estimated_cost", "cost_estimate_usd",
                "estimated_usd"):
        raw = hint.get(key)
        if raw is None:
            continue
        try:
            value = float(raw)
        except (TypeError, ValueError):
            continue
        if value > 0:
            return value
    return None


def _verification_wording(run) -> str:
    """Which levels are unverified, and the rule publishing enforces."""
    rv = _run_verdict(run)
    levels = rv.get("levels") if isinstance(rv, dict) else None
    unverified: list[str] = []
    if isinstance(levels, dict):
        for name in LEVELS:
            value = str(levels.get(name) or "").strip()
            if value != _PROVEN:
                unverified.append(f"{name} ({value or 'not recorded'})")
    if unverified:
        return ("Unverified levels: " + ", ".join(unverified)
                + ". Publishing requires every level PROVEN.")
    return "Publishing requires every level PROVEN."


def _recover_command(run, hint: dict[str, Any]) -> str:
    command = str(hint.get("command") or "").strip()
    if command:
        return command
    strategy = str(hint.get("strategy") or "").strip().lower() or "retry"
    if strategy not in _STRATEGY_WORDS:
        strategy = "retry"
    return f"mini-ork recover {run.id} --strategy {strategy}"


def _gate_explanation(run, gate: dict[str, Any]) -> str:
    """What approving this retry will do — never an empty string.

    C4: every gate/retry callout says the strategy, the from-node, the exact
    command, and (when known) the estimated cost. Where the gate's hint has no
    ``fix_steps``, the persisted ``retry-hint.json`` supplies ``failed_node`` /
    ``notes[0]`` / the command. A verification gate also names the unverified
    levels and the every-level-PROVEN rule. A broken hint still yields generic
    text naming the decision — a broken part costs detail, never the sentence.
    """
    hint = _gate_merged_hint(run, gate)
    nc = hint.get("needs_change")
    nc = nc if isinstance(nc, dict) else {}
    summary = str(nc.get("summary") or "").strip()
    strategy_raw = str(hint.get("strategy") or "").strip().lower()
    from_node = str(hint.get("from_node") or hint.get("failed_node") or "").strip()

    steps: list[str] = []
    try:
        from mini_ork.recovery import retry_notify

        steps = [str(s).strip() for s in retry_notify.fix_steps(hint, home=run.home)
                 if str(s).strip()]
    except Exception:  # noqa: BLE001 — a broken steps renderer must not blank the gate
        steps = []
    raw_notes = hint.get("notes")
    notes = [str(n).strip() for n in raw_notes if str(n).strip()] \
        if isinstance(raw_notes, list) else []

    lines: list[str] = []
    headline = (f"{_strategy_word(strategy_raw)} from {from_node}" if from_node
                else f"{_strategy_word(strategy_raw)} from the failed node")
    lines.append(headline + ".")
    if summary and summary not in headline:
        lines.append(summary)
    if steps:
        lines.extend(f"{i}. {step}" for i, step in enumerate(steps, 1))
    elif notes:
        lines.append(f"Next: {notes[0]}")
    else:
        lines.append("No fix steps are recorded; the retry re-runs the failed node.")
    lines.append(f"Command: {_recover_command(run, hint)}")
    cost = _estimated_cost(hint)
    if cost is not None:
        lines.append(f"Estimated cost: {S.money(cost)}")
    if strategy_raw in ("verify", "re-verify") or hint.get("verification") is True:
        lines.append(_verification_wording(run))
    return "\n".join(lines)


def gate_attention_item(run) -> dict[str, Any] | None:
    """The pending retry gate as one needs-you lane item, or ``None``.

    The item carries the primary decision (Approve retry) plus a non-empty
    explanation; the lane is the single place the retry decision renders, so
    the outcome's own callout is dropped from the story tab when this exists.
    The reject command is named in the detail rather than drawn as a second
    board-gate control (the lane promises one decision, one block).
    """
    gate = _pending_gate(run)
    if gate is None:
        return None
    inbox_id = str(gate.get("inbox_id") or "")
    actions: list[dict[str, Any]] = []
    if inbox_id:
        actions.append(S.btn(
            "Approve retry",
            S.cli("board", "gate", "approve", inbox_id,
                  confirm="Approve the retry and dispatch the fix?"),
            "primary"))
    lines = [ln.strip() for ln in _gate_explanation(run, gate).splitlines() if ln.strip()]
    detail = "\n".join(lines[1:])
    if inbox_id:
        detail = (detail + "\n" if detail else "") + \
            f"To leave the run as it is: mini-ork board gate reject {inbox_id}."
    return {
        "id": f"gate:{inbox_id or getattr(run, 'id', 'run')}",
        "t": lines[0] if lines else "This run needs your decision.",
        "sub": detail,
        "acts": actions,
    }


def _gate_needs_you(run, gate: dict[str, Any]) -> dict[str, Any]:
    hint = _gate_hint(gate)
    nc = hint.get("needs_change")
    summary = str((nc or {}).get("summary") or "") if isinstance(nc, dict) else ""
    inbox_id = str(gate.get("inbox_id") or "")
    text_md = _gate_explanation(run, gate)
    callout = S.callout(
        GATE_CALLOUT_TITLE, text_md, tone="orange",
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
    # The story's own first failed node beats the hint's node: the hint may be
    # a best-effort guess from an impl log, while a node the page draws as
    # failed is the fact the reader can check. A done/passed node is never
    # named (the hint can outlive a recover re-run that finished it).
    first = _first_failed_node(run)
    hint_node = str(hint.get("from_node") or hint.get("failed_node") or "") \
        if isinstance(hint, dict) else ""
    node = _nameable(run, first.id if first is not None else hint_node) \
        if (first is not None or hint_node) else ""
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
    if not detail and node:
        # The retry-hint notes are ``_node_reason``'s last fallback, so a run
        # whose only record of the failure is the hint still says why.
        detail = _reason_for(run, node)
    actions, revised = _failed_actions(run, hint)
    if revised and revised not in detail:
        detail = f"{detail.rstrip('. ')}. {revised}" if detail else revised
    # The seen-before section is the failure-only evidence surface. A
    # running / needs-you / done card carries no Seen-before row — the
    # cache is the failing-run cache and a card without a failure has
    # nothing to compare against. The callout is added directly here
    # (rather than via ``_menu`` / ``_counts``) so the section is bound
    # to the failed card specifically.
    # Fault attribution (J24/C25) rides only the failed card: whose bug is it,
    # and — for a mini-ork fault — the confirm-gated triage action. It is added
    # before the seen-before evidence so the answer to "what next?" precedes
    # the supporting memories. ``consumer`` / ``unknown`` add no button; the
    # retry actions above already carry the task-fault path.
    blame_verdict, blame_evidence = _attribute_failure(run)
    callouts: list[dict[str, Any]] = [_blame_callout(run, blame_verdict, blame_evidence)]
    seen = _seen_before_section(run)
    if seen is not None:
        callouts.append(seen)
    return {
        "state": "failed",
        "tone": "red",
        "icon": "✗",
        "text": text,
        "detail": detail,
        "actions": actions,
        "callouts": callouts,
        "blame": blame_verdict,
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
    """``(actions, revised_note)`` — ``revised_note`` is a plain-language
    reason the run is not simply retryable.

    A retryable hint whose ``needs_change.kind`` is in
    ``retry_hint.NO_CHANGE_KINDS`` (an ``interrupted`` run) gets a single
    ``Resume from <node>`` button with no ``--ack-change`` — resuming IS the
    fix. A ``lane`` kind is satisfied by its ``--lane`` switch; any other
    ``needs_change`` is a real operator fix (``--ack-change``).

    A not-retryable hint splits in two. ``code`` means the change itself is
    wrong, so the note says so and no retry is offered — retrying would fail
    the same way. Anything else (``unknown``, or no hint at all) means the
    cause was never established: the note says *that*, and a forced retry is
    still offered, because "we could not tell" is not a reason to leave the
    operator with no button.
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
            # Not retryable. Only a ``code`` kind actually knows the change is
            # at fault; the rest of them mean retry_hint could not classify the
            # failure at all (``unknown`` is case 5's "not classified"). The old
            # blanket "the change itself must be revised" asserted a cause we do
            # not have and left the row with no way forward, so say what is
            # true and offer the forced retry — ``--force`` re-runs the failed
            # step, which is a real (if uninformed) option.
            kind = str(nc.get("kind") or "") if isinstance(nc, dict) else ""
            if kind == "code":
                revised = "The change itself must be revised."
            else:
                # Deliberately short: the hint's own summary ("the cause was
                # not classified") is already in ``detail``, and the caller
                # appends this after it — a second sentence saying the same
                # thing read as a stutter.
                revised = "Retrying re-runs the failed step."
                actions.append(S.btn(
                    "Retry anyway",
                    S.cli("board", "retry", run.id, "--force",
                          confirm=f"Retry {run.id} with no diagnosis?"),
                    "primary"))
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
    # Three readers of "who failed or is blocked": the chip (state), the triage
    # text (the sentence) and these two fields (the node and the one-line
    # reason). A run that already ended (landed / published) names no node —
    # its chip says done and the triage must not contradict it.
    node, reason = ("", "") if out["state"] == "done" else _triage_subject(run)
    if out["state"] == "needs_you" and node and _first_failed_node(run) is not None:
        # A retry gate over a run whose story records a failed node: the gate's
        # hint can be stale (its node has since finished), so the headline names
        # the node the page actually draws as failed. The gate's own callout
        # below still carries the decision and its fix steps.
        out["text"] = f"Failed at {node} · needs your decision"
        if reason:
            out["detail"] = reason
    out["node"] = node
    out["reason"] = reason
    out["counts"] = _counts(run)
    out["menu"] = _menu(run, out["actions"])
    return out
