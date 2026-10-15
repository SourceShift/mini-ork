"""Close the loop: a finished run says so on the ticket it came from.

Intake has two halves and until now only one of them existed. A ticket becomes
a run (``dispatch``), and then — silence. The run reaches a terminal status, its
thread keeps saying ``running`` until the next message happens to settle it, and
the person watching the ticket hears nothing back. This module is the return
leg.

It is a *pass*, not a hook, and that is the load-bearing choice. A hook would
have to live inside the runner, so the runner would need a ticket, a thread and
a write-scoped credential for every run — including the ones nobody filed. A
pass instead reads what is already durable: the ``pm_links`` rows
:func:`~mini_ork.threads.service.deliver` wrote when it linked ``issue_uid`` to
``run:<run_id>``, and the run's own ``task_runs`` status. Nothing has to tell it
a run finished; it notices, on its own schedule, and this is the same schedule
the poller already keeps. So the simple tier closes the loop from the loop it
already runs (``poll_once`` calls :func:`report_pass`), and the secure tier runs
the very same function from a scheduler behind ``mini-ork intake report``.

Five steps per linked run, in this order because each one constrains the next:

1. **Skip unless the run is terminal.** The vocabulary bridge is
   :data:`mini_ork.threads.service._TERMINAL_TASK_STATUS_MAP` — a ``published``
   or ``failed`` ``task_runs`` row, not a guess from the run's own status text.
   A run still going is left alone.

2. **Skip if already reported.** The ledger row is keyed
   ``(provider, "report:<run_id>")`` — one report per *run*, not per arrival, so
   a run that has been re-read on a hundred passes still posts once.

3. **Settle the thread.** :func:`~mini_ork.threads.service.on_run_terminal` —
   its first production caller — so ``threads.status`` stops claiming a finished
   run is running. This happens whether or not the comment below lands: the run
   *is* over, and that fact does not depend on the source accepting a write.

4. **Compose the comment** — the outcome (the run's own ``verdict.json`` when it
   has one, else the terminal status), the run id, the recipe, and the published
   commit when the run dir records one. Plain prose; the Jira adapter turns it
   into ADF, so nothing here depends on a rich-text dialect.

5. **Post it through the write-back gate** —
   :func:`mini_ork.pm.writeback.comment`, never the adapter directly. The gate
   is the whole point of both tiers: *whether anything is posted* is decided by
   ``write_back: true`` plus a write-scoped capability, and both tiers fail
   closed. The three outcomes:

   * the gate refuses (:class:`~mini_ork.pm.provider.Unsupported` — write-back
     off, or a source that cannot comment) → ``skipped``, **not recorded**, so
     enabling the source later posts on a later pass;
   * the source fails (:class:`~mini_ork.pm.provider.ProviderError`) →
     ``error``, **not recorded**, retried next pass;
   * success → record the event, outcome ``reported``.

   Nothing recorded before the comment lands. That ordering is deliberate: a
   crash *after* posting but *before* recording re-posts once (visible, and
   fixable by hand), where recording first would drop the report silently — the
   one failure a person could not see.

**Nothing here may raise.** ``report_pass`` rides the poll loop, and a write-back
outage must never take down polling — the tickets still need to be dispatched.
Every provider failure becomes an outcome; every unreadable row is skipped.

``dry_run=True`` decides and reports what it would do and writes **nothing** —
no settle, no comment, no event — so the first look at a live board is free.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from mini_ork.intake.dispatch import payload_hash
from mini_ork.pm import writeback
from mini_ork.pm.model import LinkKind, split_uid
from mini_ork.pm.provider import ProviderError, Unsupported
from mini_ork.threads import service as thread_service
from mini_ork.threads import store as thread_store
from mini_ork.threads.service import _TERMINAL_TASK_STATUS_MAP

#: The tag that makes a report distinct from a dispatch in the one ledger both
#: use. Keyed by the *run*, so the report happens once per finished run however
#: many passes look at it.
REPORT_PREFIX = "report:"

#: The publisher's ``[publish] committed N file(s): <sha>`` line — the run dir's
#: own record that a change landed. The same line ``ide_pages/run_flow.py``
#: reads to build the run's delivery bar; kept here rather than imported so this
#: module stays free of the pages package.
_PUBLISH_RE = re.compile(
    r"\[publish\]\s+committed\s+\d+\s+file\(s\):\s*([0-9a-fA-F]{7,40})"
)

#: How much of a log to scan for the publish line. A publisher's line is at the
#: end of ``execute.log``, and the file can be a multi-megabyte transcript.
_LOG_TAIL_BYTES = 262_144


@dataclass(frozen=True, slots=True)
class ReportOutcome:
    """What one linked run's report did — or why it was passed over.

    ``action`` is one of ``reported``, ``skipped``, ``error`` or the dry run's
    ``would-report``.
    """

    issue_uid: str
    run_id: str
    action: str
    reason: str = ""
    thread_id: str | None = None

    @property
    def posted(self) -> bool:
        return self.action == "reported"

    def to_dict(self) -> dict[str, object]:
        return {
            "issue_uid": self.issue_uid,
            "run_id": self.run_id,
            "action": self.action,
            "reason": self.reason,
            "thread_id": self.thread_id,
        }


def report_pass(
    home: Path | str, *, db: str | None = None, dry_run: bool = False,
    source: str | None = None,
) -> list[ReportOutcome]:
    """Report every finished linked run back to its ticket, once each.

    Returns one :class:`ReportOutcome` per run whose report was attempted (or
    would have been, on a dry run). Runs still going, and runs already reported,
    produce nothing — the pass is quiet about work it is not doing.

    ``source`` limits the pass to tickets from one source — what ``poll_once``
    passes, so a pass over N sources sweeps each link once, not N times.

    Never raises: a home with no schema, a link to a source this process cannot
    resolve, a source down mid-post — each becomes a skipped row or an ``error``
    outcome, because this is called from the poll loop and must not be able to
    stop it.
    """
    path = _db_path(home, db)
    outcomes: list[ReportOutcome] = []
    for issue_uid, run_id in _run_links(path):
        if source is not None and not issue_uid.startswith(f"{source}:"):
            continue
        outcome = _report_one(home, path, issue_uid, run_id, dry_run=dry_run)
        if outcome is not None:
            outcomes.append(outcome)
    return outcomes


def _report_one(
    home: Path | str, path: str, issue_uid: str, run_id: str, *, dry_run: bool
) -> ReportOutcome | None:
    """The five steps for one linked run, or ``None`` when it is not our turn."""
    status = _task_run_status(path, run_id)
    settled = _TERMINAL_TASK_STATUS_MAP.get(status or "")
    if settled is None:
        # Not terminal, or a run whose status this build does not know. Either
        # way: nothing to report yet, and the row is left untouched.
        return None

    try:
        provider_id, _key = split_uid(issue_uid)
    except ValueError:
        # A link to something that is not a provider uid is not a reportable
        # run. Skipped silently rather than raising: the pass must not stop.
        return None

    delivery_id = f"{REPORT_PREFIX}{run_id}"
    if thread_store.intake_event(provider_id, delivery_id, db=path) is not None:
        return None  # already posted this run; the ledger is the guard

    thread_id = _thread_id_for_run(path, run_id)
    if dry_run:
        return ReportOutcome(
            issue_uid, run_id, "would-report",
            f"run is {status}; would post to {issue_uid}",
            thread_id=thread_id,
        )

    # Settle first and unconditionally: the run is over, and a thread still
    # claiming otherwise is wrong whatever the source decides about a comment.
    settled_thread = thread_service.on_run_terminal(home, run_id, settled, db=path)
    if settled_thread is not None:
        thread_id = settled_thread.id

    body = _compose(path, home, run_id, status)
    try:
        writeback.comment(issue_uid, body)
    except Unsupported as exc:
        # The gate refused: write-back is off for this source, or it cannot
        # comment. Not an error and not recorded — enabling it later posts.
        return ReportOutcome(issue_uid, run_id, "skipped", str(exc), thread_id=thread_id)
    except ProviderError as exc:
        # The source itself failed. Not recorded, so the next pass retries.
        return ReportOutcome(issue_uid, run_id, "error", str(exc), thread_id=thread_id)
    except Exception as exc:  # noqa: BLE001 — a report must never break the pass
        return ReportOutcome(
            issue_uid, run_id, "error",
            f"{type(exc).__name__}: {exc}", thread_id=thread_id,
        )

    thread_store.record_intake_event(
        provider_id,
        delivery_id,
        payload_hash(body),
        issue_uid=issue_uid,
        thread_id=thread_id,
        action="reported",
        detail=body,
        db=path,
    )
    return ReportOutcome(issue_uid, run_id, "reported", body, thread_id=thread_id)


def _compose(path: str, home: Path | str, run_id: str, status: str) -> str:
    """The comment body: what a person watching the ticket needs to know.

    Plain prose, one line, in the ticket's own voice — no dialect, because the
    adapter on the far side decides how it renders.
    """
    recipe = _task_run_recipe(path, run_id)
    who = f"mini-ork run {run_id}"
    if recipe:
        who += f" (recipe {recipe})"
    sentence = f"{who} finished: {_outcome(Path(home) / 'runs' / run_id, status)}."
    commit = _published_commit(Path(home) / "runs" / run_id)
    if commit:
        sentence += f" Published commit {commit}."
    return sentence


def _outcome(run_dir: Path, status: str) -> str:
    """The run's verdict when it wrote one, else the terminal status.

    ``verdict.json`` is the run's own last word; the status is the fallback for
    a run that never reached the verifier (a failed or rolled-back run often
    has no verdict at all).
    """
    verdict = _read_json(run_dir / "verdict.json")
    if isinstance(verdict, dict) and "pass" in verdict:
        return "passed" if verdict.get("pass") else "failed"
    return status or "finished"


def _run_links(path: str) -> list[tuple[str, str]]:
    """Every ``(issue_uid, run_id)`` the mirror has linked. Empty on a home
    whose schema predates the pm layer — there is nothing to report, not an
    error."""
    try:
        con = sqlite3.connect(path)
        try:
            con.row_factory = sqlite3.Row
            rows = con.execute(
                "SELECT from_uid, to_uid FROM pm_links WHERE kind = ? ORDER BY id",
                (str(LinkKind.RUN),),
            ).fetchall()
        finally:
            con.close()
    except sqlite3.Error:
        return []

    links: list[tuple[str, str]] = []
    for row in rows:
        pair = _split_run_link(row["from_uid"], row["to_uid"])
        if pair is not None:
            links.append(pair)
    return links


def _split_run_link(from_uid: str, to_uid: str) -> tuple[str, str] | None:
    """``(issue_uid, run_id)`` from a ``kind='run'`` edge, either direction.

    The convention is ``from_uid=issue``, ``to_uid=run:<id>``
    (:func:`~mini_ork.threads.service._link_issue`), but the edge is read the
    way :func:`~mini_ork.pm.store.links_for` reads it — either direction — so
    the run side is whichever one carries the ``run:`` prefix.
    """
    if to_uid.startswith("run:"):
        return from_uid, to_uid[len("run:"):]
    if from_uid.startswith("run:"):
        return to_uid, from_uid[len("run:"):]
    return None


def _task_run_status(path: str, run_id: str) -> str | None:
    """``task_runs.status`` for a run, or ``None``. Best-effort: a missing table
    or an unknown run id is "not terminal", which is the safe direction."""
    return _task_run_column(path, run_id, "status")


def _task_run_recipe(path: str, run_id: str) -> str:
    """``task_runs.recipe`` for a run, or ``""``."""
    return _task_run_column(path, run_id, "recipe") or ""


def _task_run_column(path: str, run_id: str, column: str) -> str | None:
    # `column` is a module-controlled literal, never caller input — the two
    # call sites above are the whole set, so interpolating it cannot inject.
    try:
        con = sqlite3.connect(path)
        try:
            row = con.execute(
                f"SELECT {column} FROM task_runs WHERE id = ?", (run_id,)
            ).fetchone()
        finally:
            con.close()
    except sqlite3.Error:
        return None
    if row is None or row[0] is None:
        return None
    return str(row[0])


def _thread_id_for_run(path: str, run_id: str) -> str | None:
    """The thread a run belongs to, or ``None``. Best-effort."""
    try:
        con = sqlite3.connect(path)
        try:
            row = con.execute(
                "SELECT thread_id FROM thread_runs WHERE run_id = ?", (run_id,)
            ).fetchone()
        finally:
            con.close()
    except sqlite3.Error:
        return None
    return str(row[0]) if row is not None and row[0] else None


def _published_commit(run_dir: Path) -> str | None:
    """The sha the publisher recorded in the run dir, or ``None``.

    Framework-edit and its kin deliver a diff and never commit, so their run dir
    has no such line and there is nothing to report — the absence is the answer,
    not a failure.
    """
    if not run_dir.is_dir():
        return None
    logs = [run_dir / "execute.log"]
    logs += [p for p in sorted(run_dir.glob("*.log")) if p.name != "execute.log"]
    for log in logs:
        match = _PUBLISH_RE.search(_tail(log))
        if match:
            return match.group(1)
    return None


def _tail(path: Path, limit: int = _LOG_TAIL_BYTES) -> str:
    """The last ``limit`` bytes of ``path`` as text, or ``""`` when unreadable."""
    try:
        size = path.stat().st_size
    except OSError:
        return ""
    try:
        with open(path, "rb") as handle:
            if size > limit:
                handle.seek(size - limit)
            return handle.read().decode("utf-8", "ignore")
    except OSError:
        return ""


def _read_json(path: Path) -> object:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _db_path(home: Path | str, db: str | None) -> str:
    """Explicit path → ``MINI_ORK_DB`` → ``<home>/state.db``.

    The same chain the rest of the control plane uses (``pm.store._db_path``),
    with the caller's ``home`` standing in for the ``MINI_ORK_HOME`` a caller
    that named one meant. Every store call is threaded this one path so the
    reads and the writes agree.
    """
    if db:
        return str(db)
    env = os.environ.get("MINI_ORK_DB")
    if env:
        return env
    return str(Path(home) / "state.db")


__all__ = ["REPORT_PREFIX", "ReportOutcome", "report_pass"]
