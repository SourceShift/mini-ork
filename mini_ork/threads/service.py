"""The thread state machine — this *is* the feature.

One entry point, :func:`deliver`, turns "a message arrived for this thread" into
exactly one of four outcomes, and the terminal one is what replaced the
agent-server's 409:

    launched    nothing has run yet            → run attempt 1
    steered     a run is live                  → inject, do not spawn
    continued   the last run is terminal       → run attempt N, same thread
    duplicate   this external message is known → do nothing

Keeping "steered" verbatim matters: ``steer_run`` rides the next
``context_assemble`` pack and never starts a second run, and a conversation the
canvas is mid-way through must keep behaving that way. What is new is
``continued``: a follow-up to a finished run is a *new run in the same thread*,
carrying the transcript and the prior outcome in its kickoff
(:mod:`mini_ork.threads.kickoff`) rather than a new conversation.

The last run's *status* comes from ``thread_runs``; its *verdict* and artifact
ref come from ``execution_traces``, which is the only table that holds them
(``task_runs`` does not). Both are best-effort: a home without the traces table,
or a run that produced none, simply has nothing to carry, and the kickoff is
still composed.

This is the only module that calls :func:`mini_ork.web.control.launch_run` and
:func:`mini_ork.web.control.steer_run`. It calls them through the ``control``
module rather than by name so a test can substitute them, which is the house
pattern (``tests/test_web_smoke.py``).
"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from mini_ork.threads import kickoff as _kickoff
from mini_ork.threads import store
from mini_ork.threads.model import (
    MessageRole,
    RunStatus,
    Thread,
    ThreadOrigin,
    ThreadRun,
)
from mini_ork.web import control

#: Recipe a thread runs when neither the thread nor the caller names one.
#: Matches the agent-server's own default so a canvas conversation behaves the
#: same whether it is driven through the shim or straight through this module.
DEFAULT_RECIPE = "code-fix"

#: How many prior turns a continuation's kickoff carries. The composer bounds
#: the text; this bounds how many we hand it.
_HISTORY_LIMIT = 24

#: ``task_runs`` terminal statuses and the run status each settles to. Mirrors
#: the canvas's own map (``web/routes/agent_server.py``) without importing it —
#: this module must stay importable with no web server and no fastapi installed.
#: Any other status, including one this map has not seen, reads as *still
#: running*, which is the safe direction: it steers rather than spawning a
#: second run.
_TERMINAL_TASK_STATUS_MAP = {
    "published": RunStatus.FINISHED,
    "rolled_back": RunStatus.ERROR,
    "failed": RunStatus.ERROR,
    # A run the operator closed is over, but it is not a failure to steer back
    # from — FINISHED, matching the canvas's map.
    "not_needed": RunStatus.FINISHED,
}
_TERMINAL_TASK_STATUSES = frozenset(_TERMINAL_TASK_STATUS_MAP)


@dataclass(frozen=True, slots=True)
class Delivery:
    """What :func:`deliver` did with one message.

    ``action`` is one of ``launched``, ``steered``, ``continued``, ``duplicate``
    or ``error``. ``run_id`` is the run the message launched (or the live run it
    steered); it is ``None`` for a duplicate or a failed launch.
    """

    thread_id: str
    action: str
    turn: int
    message_id: int
    run_id: str | None = None
    kickoff_path: str | None = None
    log_path: str | None = None
    detail: str | None = None

    @property
    def ok(self) -> bool:
        return self.action != "error"

    def to_dict(self) -> dict[str, object]:
        return {
            "thread_id": self.thread_id,
            "action": self.action,
            "turn": self.turn,
            "message_id": self.message_id,
            "run_id": self.run_id,
            "kickoff_path": self.kickoff_path,
            "log_path": self.log_path,
            "detail": self.detail,
        }


def deliver(
    home: Path | str,
    thread_id: str,
    text: str,
    *,
    title: str = "",
    origin: ThreadOrigin | str = ThreadOrigin.IDE,
    origin_ref: str | None = None,
    recipe: str | None = None,
    ticket: str | None = None,
    external_id: str | None = None,
    issue_uid: str | None = None,
    source: str = "threads",
    attachments: Sequence[str] = (),
    db: str | None = None,
) -> Delivery:
    """Deliver one message to a thread. See the module docstring for the four
    outcomes. ``ticket`` is a pre-rendered block carried into attempt 1's
    kickoff; on a continuation it is already part of the transcript.

    ``attachments`` are absolute paths already stored under the thread's
    attachment directory (:mod:`mini_ork.threads.attachments`). Each becomes a
    ``FILE`` row on this message's turn, and the run is told where they are —
    in the kickoff of a launched or continued run, in the steer text of a live
    one. A run that is never told a file exists cannot read it."""
    path = _resolve_db(db, home)
    thread = store.open_thread(
        thread_id, title=title, origin=origin, origin_ref=origin_ref,
        recipe=recipe, db=path,
    )

    if external_id is not None:
        seen = store.message_by_external(thread_id, external_id, db=path)
        if seen is not None:
            live = store.live_run(thread_id, db=path)
            return Delivery(
                thread_id, "duplicate", seen.turn, seen.id,
                run_id=live.run_id if live is not None else None,
            )

    runs = store.runs_for(thread_id, db=path)
    live = store.live_run(thread_id, db=path)
    # A run this thread still calls live but the runner has already finished is
    # the ordinary case for a canvas conversation: the run writes task_runs,
    # whoever launched it wrote thread_runs, and nothing tells the thread. Left
    # unreconciled a finished conversation would be *steered* — a no-op — where
    # it should continue.
    if live is not None and _settle_if_terminal(path, live):
        runs = store.runs_for(thread_id, db=path)
        live = None
    live_run_id = live.run_id if live is not None else None
    if live_run_id is None and _task_run_active(path, runs[-1].run_id if runs else thread_id):
        # The mirror image: not marked live here, but task_runs says the run is
        # still going — the crash window between launch_run and link_run, or a
        # conversation created before threads existed (no thread_runs rows, only
        # the run id, which is the thread id). Steering is the only safe move:
        # launching would put two runs on one thread.
        live_run_id = runs[-1].run_id if runs else thread_id
    if live_run_id is not None:
        message = store.append_message(
            thread_id, role=MessageRole.USER, text=text,
            external_id=external_id, db=path,
        )
        _record_attachments(thread_id, message.turn, attachments, path)
        control.steer_run(
            _state_db(home), live_run_id, _with_attachments(text, attachments),
            source=source, severity="info",
        )
        return Delivery(thread_id, "steered", message.turn, message.id, run_id=live_run_id)

    attempt = len(runs) + 1
    message = store.append_message(
        thread_id, role=MessageRole.USER, text=text,
        external_id=external_id, db=path,
    )
    _record_attachments(thread_id, message.turn, attachments, path)
    # attempt 1 reuses the thread id (the canvas already routes on it); a
    # continuation is '{id}-t{turn}', unique because the turn is.
    run_id = thread_id if attempt == 1 else f"{thread_id}-t{message.turn}"
    recipe_name = recipe or thread.recipe or DEFAULT_RECIPE

    composed = _kickoff.compose(
        _kickoff.Kickoff(
            title=title or thread.title,
            task=text,
            ticket=ticket if attempt == 1 else None,
            history=_history(path, thread_id, exclude_id=message.id,
                             current_turn=message.turn),
            prior=_prior(path, runs[-1]) if runs else None,
            attachments=tuple(attachments),
        )
    )

    result = control.launch_run(home, recipe_name, composed, run_id=run_id)
    if not result.get("ok"):
        return Delivery(
            thread_id, "error", message.turn, message.id,
            detail=str(result.get("error") or "launch failed"),
        )

    store.link_run(
        thread_id, run_id, attempt=attempt, recipe=recipe_name,
        kickoff_path=result.get("kickoff_path"), db=path,
    )
    if issue_uid:
        _link_issue(issue_uid, run_id, path)

    return Delivery(
        thread_id, "launched" if attempt == 1 else "continued",
        message.turn, message.id, run_id=run_id,
        kickoff_path=result.get("kickoff_path"),
        log_path=result.get("log_path"),
    )


def on_run_terminal(
    home: Path | str,
    run_id: str,
    status: RunStatus | str,
    *,
    text: str = "",
    db: str | None = None,
) -> Thread | None:
    """Mark a run terminal and re-derive its thread's status, in one call.

    ``text`` — the run's final words, when a caller has them — is recorded as the
    run's closing assistant turn so the transcript a continuation reads is
    complete. Returns the settled thread, or ``None`` when the run is unknown or
    already terminal (a second completion signal must not disturb it).
    """
    path = _resolve_db(db, home)
    thread = store.settle_run(run_id, status, db=path)
    if thread is None or not text.strip():
        return thread
    runs = store.runs_for(thread.id, db=path)
    turn = runs[-1].attempt if runs else 1
    store.append_message(
        thread.id, role=MessageRole.ASSISTANT, text=text,
        turn=turn, event_id=f"{run_id}-final", db=path,
    )
    return store.get_thread(thread.id, db=path)


def _history(
    path: str, thread_id: str, exclude_id: int, current_turn: int | None = None
) -> tuple[_kickoff.Turn, ...]:
    """The transcript so far, oldest first, minus the message that is the task.

    A ``FILE`` row has no prose, so it is carried as the stored path(s) it holds —
    otherwise an earlier attachment silently vanished from every continuation.
    The current turn's files are left out: the kickoff lists them on their own.
    """
    turns: list[_kickoff.Turn] = []
    for row in store.messages(thread_id, limit=_HISTORY_LIMIT, db=path):
        if row.id == exclude_id:
            continue
        if str(row.role) == MessageRole.FILE.value:
            if row.turn == current_turn:
                continue
            text = "\n".join(f"`{item}`" for item in row.attachments)
        else:
            text = row.text
        if text.strip():
            turns.append(_kickoff.Turn(role=str(row.role), text=text))
    return tuple(turns)


def _record_attachments(
    thread_id: str, turn: int, attachments: Sequence[str], path: str
) -> None:
    """One ``FILE`` row per stored path, on the message's own turn."""
    for item in attachments:
        store.append_message(
            thread_id, role=MessageRole.FILE, text="", turn=turn,
            attachments=(str(item),), db=path,
        )


def _with_attachments(text: str, attachments: Sequence[str]) -> str:
    """The steer text, with the stored paths appended so a live run can read them."""
    if not attachments:
        return text
    listed = "\n".join(f"- `{item}`" for item in attachments)
    return f"{text}\n\nAttached files:\n{listed}".strip()


def _prior(path: str, run: ThreadRun) -> _kickoff.PriorRun:
    """The last run's outcome, enriched with its verdict and artifact when the
    traces table has them."""
    verdict, artifacts = _trace_outcome(path, run.run_id)
    return _kickoff.PriorRun(
        attempt=run.attempt,
        run_id=run.run_id,
        status=str(run.status),
        verdict=verdict,
        artifacts=artifacts,
    )


def _trace_outcome(path: str, run_id: str) -> tuple[str | None, tuple[str, ...]]:
    """``(reviewer_verdict, (final_artifact_ref,))`` for a run, or ``(None, ())``.

    ``execution_traces`` is the only table carrying both, keyed by ``run_id``. A
    home that predates it, or a run with no trace, simply has neither — not an
    error, so a missing table returns empty rather than raising.
    """
    try:
        con = sqlite3.connect(path)
        try:
            row = con.execute(
                "SELECT reviewer_verdict, final_artifact_ref FROM execution_traces "
                "WHERE run_id = ? ORDER BY created_at DESC LIMIT 1",
                (run_id,),
            ).fetchone()
        finally:
            con.close()
    except sqlite3.Error:
        return None, ()
    if row is None:
        return None, ()
    verdict = (row[0] or "").strip() or None
    artifact = (row[1] or "").strip()
    return verdict, ((artifact,) if artifact else ())


def _settle_if_terminal(path: str, run: ThreadRun) -> bool:
    """Settle ``run`` when ``task_runs`` already shows it over. True if it did.

    The run status is not guessed from the task status text:
    ``_TERMINAL_TASK_STATUS_MAP`` is the one place the two vocabularies meet.
    """
    status = _task_run_status(path, run.run_id)
    settled = _TERMINAL_TASK_STATUS_MAP.get(status or "")
    if settled is None:
        return False
    store.settle_run(run.run_id, settled, db=path)
    return True


def _task_run_status(path: str, run_id: str) -> str | None:
    """``task_runs.status`` for a run, or ``None``. Best-effort.

    A missing table or an unknown run id is ``None`` — the caller has already
    consulted the thread's own live index, and this only widens the net.
    """
    try:
        con = sqlite3.connect(path)
        try:
            row = con.execute(
                "SELECT status FROM task_runs WHERE id = ?", (run_id,)
            ).fetchone()
        finally:
            con.close()
    except sqlite3.Error:
        return None
    if row is None:
        return None
    return str(row[0] or "")


def _task_run_active(path: str, run_id: str) -> bool:
    """Whether ``task_runs`` shows this run still going."""
    status = _task_run_status(path, run_id)
    return status is not None and status not in _TERMINAL_TASK_STATUSES


def _link_issue(issue_uid: str, run_id: str, path: str) -> None:
    """Record that a run works an issue. Reuses ``pm_links``' ``run:`` uid, so
    the edge lives where the pm layer already puts such edges."""
    from mini_ork.pm import store as pm_store
    from mini_ork.pm.model import LinkKind

    pm_store.link(issue_uid, f"run:{run_id}", LinkKind.RUN, db=path)


def _state_db(home: Path | str):
    """The read-only StateDB ``steer_run`` expects. Imported lazily so this
    module stays importable without pulling the web package on every open."""
    from mini_ork.web.db import db_for

    return db_for(Path(home))


def _resolve_db(db: str | None, home: Path | str) -> str:
    """One path for the whole call, so the store and the traces read agree.

    Resolved once and threaded through every store call rather than left to
    the path differently is exactly the bug a dual-sourced schema invites.

    ``db`` → ``home/state.db``, with no environment in the chain on purpose: a
    caller that named a home meant it, and reading ``MINI_ORK_DB`` here would let
    a developer's shell silently redirect a call that passed an explicit tmp
    home. The CLI resolves ``MINI_ORK_DB`` itself and passes the result as ``db``,
    which is where that precedence belongs.
    """
    return db if db else str(Path(home) / "state.db")


__all__ = ["Delivery", "DEFAULT_RECIPE", "deliver", "on_run_terminal"]
