"""Thread — one durable unit of work, and the box that continues it.

A thread supersedes the agent-server's one-DAG conversation: it carries a
transcript, every run that has worked it, and a status that survives a run
ending. So "follow up in the same thread" is a turn on a row rather than a new
conversation, and the thing that used to be a 409 is the ``Continue`` composer
at the bottom of this page.

With no ``thread`` argument the page is the list of threads; with one it is that
thread. Rows come from :mod:`mini_ork.threads.store` — the same reader the CLI
and the agent-server's projection use — so a thread the page shows and a thread
``mini-ork threads show`` shows cannot disagree.

Read-only plus one input box. The composer runs ``mini-ork threads send <id>
--text``, which is the same call the CLI makes, so this page adds no way to
start work that the terminal does not already have.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from mini_ork.ide_pages import spec as S

#: Transcript lines are capped because a thread that has run forty times is not
#: a page a person reads top to bottom — the tail is where the answer is.
_MESSAGE_LIMIT = 60

#: How many threads the index lists.
_THREAD_LIMIT = 50

_STATE_TONE = {"running": "blue", "finished": "green", "error": "red", "idle": "sub"}
_RUN_TONE = {"running": "blue", "finished": "green", "error": "red"}

_ROLE_LABEL = {"user": "You", "assistant": "Agent", "system": "System", "file": "File"}


def _db(home: Path) -> str:
    from mini_ork.web.db import resolve_db

    return resolve_db(home)


def _threads(home: Path) -> list[dict[str, Any]]:
    from mini_ork.threads import store

    rows = []
    for thread in store.list_threads(limit=_THREAD_LIMIT, db=_db(home)):
        rows.append(
            {
                "cells": [
                    S.cell(thread.title or thread.id, "text", b=True),
                    S.mono(thread.id),
                    S.cell(str(thread.origin), "sub"),
                    S.cell(str(thread.status), _STATE_TONE.get(str(thread.status), "sub")),
                    S.cell(str(thread.last_turn), "sub", mono=True),
                    S.cell(str(thread.updated_at or ""), "sub"),
                ],
                "do": S.page_link("thread", thread=thread.id),
            }
        )
    return rows


def _index(home: Path) -> list[dict[str, Any]]:
    rows = _threads(home)
    if not rows:
        return [
            S.callout(
                "No threads yet",
                "A thread is opened by a ticket with a trigger label, by a canvas "
                "conversation, or by `mini-ork threads send`. Nothing has opened one here.",
                tone="sub",
            )
        ]
    return [
        S.table(
            "Threads",
            [S.col(fr=1.6), S.col(fr=2.0), S.col(80), S.col(90), S.col(60), S.col(fr=1.2)],
            ["title", "id", "origin", "state", "turn", "updated"],
            rows,
            full=True,
            note="Newest first. A thread's status is a cache of its latest run; "
                 "`mini-ork threads show` recomputes it.",
        )
    ]


def _runs(thread: Any, db: str) -> dict[str, Any]:
    from mini_ork.threads import store

    rows = []
    for run in store.runs_for(thread.id, db=db):
        rows.append(
            [
                S.mono(run.attempt),
                S.mono(run.run_id),
                S.cell(str(run.status), _RUN_TONE.get(str(run.status), "sub")),
                S.cell(run.recipe or "—", "sub"),
                S.cell(run.started_at or "", "sub"),
                S.cell(run.ended_at or "", "sub"),
            ]
        )
    return S.table(
        "Runs",
        [S.col(60), S.col(fr=2.0), S.col(80), S.col(fr=1.4), S.col(fr=1.2), S.col(fr=1.2)],
        ["attempt", "run", "state", "recipe", "started", "ended"],
        rows,
        full=True,
        note="Attempt 1 is the initial run; every later attempt is a continuation in "
             "this same thread — which is what replaced the old 409.",
    )


def _transcript(thread: Any, db: str) -> dict[str, Any]:
    from mini_ork.threads import store

    messages = store.messages(thread.id, limit=_MESSAGE_LIMIT, db=db)
    if not messages:
        return S.callout("Transcript", "This thread has no messages yet.", tone="sub")
    lines: list[str] = []
    for message in messages:
        who = _ROLE_LABEL.get(str(message.role), str(message.role))
        lines.append(f"### {who} · turn {message.turn}")
        lines.append("")
        lines.append(message.text.strip() or "_(empty)_")
        lines.append("")
        for path in message.attachments:
            lines.append(f"- attachment: `{path}`")
        if message.attachments:
            lines.append("")
    return S.markdown("Transcript", "\n".join(lines).rstrip() + "\n", full=True)


def _one(home: Path, thread_id: str) -> list[dict[str, Any]]:
    from mini_ork.threads import store

    db = _db(home)
    thread = store.get_thread(thread_id, db=db)
    if thread is None:
        return [
            S.callout(
                f"No thread {thread_id!r}",
                "No row with that id in this project's state database.",
                tone="red",
                actions=[S.btn("All threads", S.page_link("thread"), "ghost")],
            )
        ]

    sections: list[dict[str, Any]] = [
        S.kv(
            thread.title or thread.id,
            [
                ("Id", thread.id, "text", "the key the canvas, the websocket and MINI_ORK_RUN_ID share"),
                ("Origin", str(thread.origin), "text", thread.origin_ref or ""),
                ("State", str(thread.status), _STATE_TONE.get(str(thread.status), "sub"), ""),
                ("Recipe", thread.recipe or "—", "text", ""),
                ("Turns", str(thread.last_turn), "text", "one turn per launch"),
                ("Updated", thread.updated_at or "", "sub", f"created {thread.created_at or ''}"),
            ],
            full=True,
        ),
        _runs(thread, db),
        _transcript(thread, db),
        S.composer(
            "Continue this thread…",
            ["threads", "send", thread.id, "--text"],
            title="Continue",
            full=True,
            note="A message while the run is live steers it; once it has finished the "
                 "same message continues this thread at the next attempt.",
        ),
    ]
    return sections


def build(home: Path, tab: str | None, args: dict[str, str]) -> dict[str, Any]:
    thread_id = str(args.get("thread") or "").strip()
    errors: dict[str, str] = {}
    if thread_id:
        sections = S.guarded(errors, "Thread", lambda: _one(home, thread_id))
        title, sub = "Thread", thread_id
    else:
        sections = S.guarded(errors, "Threads", lambda: _index(home))
        title, sub = "Threads", "Every durable unit of work in this project."
    return S.page(
        "thread",
        title,
        sub,
        chips_=[S.chip("mini-ork threads")],
        actions=[S.btn("All threads", S.page_link("thread"), "ghost")]
        if thread_id
        else [],
        args=args,
        sections=sections,
        errors=errors,
    )
