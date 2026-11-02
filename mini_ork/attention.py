"""The one attention surface: what is waiting on a person, in one order.

mini-ork already knows every fact an operator needs, but it knows them in five
disconnected places — ``mo_inbox_gates`` (node reviews and merge approvals),
``oversight_inbox`` rows that are neither, ``workspaces`` records that still
hold a branch, ``runs/<id>/.cost-pause`` sentinels, and failed runs whose
retry hint says "this one is worth another attempt". Answering "what needs me
right now?" meant opening the IDE, then the shell, then a worktree directory,
and remembering which of the five you had already looked at.

Two things live here.

**A declared taxonomy.** :data:`TAXONOMY` is the ordered table of every reason
a run can be in front of a person. The table is the contract: adding a sixth
inbox source without adding a row to it is the defect this module exists to
make impossible, and ``tests/unit/test_attention_py.py`` fails on a row type
it cannot place. Order is severity, not arrival: a paused run costs money while
it waits, a merge approval costs a decision, a finished worktree costs a click.

**A stage resolver.** :func:`stage_of` answers, for one run, which of the four
stages it is in — ``person`` (only a human moves it), ``working`` (nothing to
do but wait), ``idle`` (nothing has started), ``ended`` (over, successfully or
not). It is derived, never stored: the same four answers the IDE's
``outcome.resolve`` produces, so the terminal and the GUI cannot disagree about
what "needs you" means.

:func:`inbox` unions the sources into rows. Every row carries ``run_id``,
``what`` (the title), ``why`` (the one sentence a stranger needs) and ``next``
(a command that actually exists). The command strings are built here, next to
the taxonomy, so a verb renamed in ``cli/board_cmd.py`` breaks one test rather
than every operator's muscle memory.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

__all__ = [
    "TAXONOMY",
    "STATE_PERSON",
    "STATE_WORKING",
    "STATE_IDLE",
    "STATE_ENDED",
    "TaxonomyRow",
    "Stage",
    "Row",
    "taxonomy",
    "stage_states",
    "stage_of",
    "inbox",
    "render",
    "row_for_outcome",
    "row_for_ticket",
]

STATE_PERSON = "person"
STATE_WORKING = "working"
STATE_IDLE = "idle"
STATE_ENDED = "ended"


@dataclass(frozen=True, slots=True)
class TaxonomyRow:
    """One declared reason a run can be in front of a person.

    ``command`` is a template with ``{run}`` / ``{node}`` / ``{inbox}`` slots.
    It is empty for the stages that need nothing (``working``, ``idle``,
    ``ended``) — an action verb on "wait" is a lie about what to do.

    ``outcome_states`` and ``ticket_states`` name the vocabularies this row
    *is*, so the coverage test can assert every word of both maps onto a row.
    """

    key: str
    state: str
    label: str
    waits_for: str
    must_do: str
    command: str = ""
    outcome_states: tuple[str, ...] = ()
    ticket_states: tuple[str, ...] = ()


#: Ordered most-consequential-first. The order is the inbox's order.
TAXONOMY: tuple[TaxonomyRow, ...] = (
    TaxonomyRow(
        key="cost_pause",
        state=STATE_PERSON,
        label="Paused",
        waits_for="your budget decision",
        must_do="raise the budget, then resume",
        command="mini-ork resume {run}",
    ),
    TaxonomyRow(
        key="merge_approval",
        state=STATE_PERSON,
        label="Needs you",
        waits_for="your approval to merge",
        must_do="read the diff, then approve or reject the merge",
        command="mini-ork approvals approve {run}",
    ),
    TaxonomyRow(
        key="node_review",
        state=STATE_PERSON,
        label="Needs you",
        waits_for="your review of one node",
        must_do="accept the node or send it back",
        command="mini-ork approvals approve {run} --node {node}",
    ),
    TaxonomyRow(
        key="worktree",
        state=STATE_PERSON,
        label="Needs you",
        waits_for="your keep-or-discard decision",
        must_do="inspect the worktree, then merge or discard it",
        command="mini-ork board merge {run}",
    ),
    TaxonomyRow(
        key="retry",
        state=STATE_PERSON,
        label="Failed",
        waits_for="your retry decision",
        must_do="read the retry hint, then retry or fix first",
        command="mini-ork board retry {run}",
        outcome_states=("failed",),
    ),
    # Last, deliberately: an oversight row is a note for the calibration
    # report. Resolving one does not unblock a run, and this channel is the
    # one that accumulates (rule-by-fatigue / abandoned oversight are its two
    # named failure modes), so it must not push a live decision off the page.
    TaxonomyRow(
        key="oversight_gate",
        state=STATE_PERSON,
        label="Needs you",
        waits_for="your resolution of a gate",
        must_do="approve or reject the gate",
        command="mini-ork oversight --resolve {inbox} --status approved",
    ),
    TaxonomyRow(
        key="working",
        state=STATE_WORKING,
        label="Working",
        waits_for="nobody — it is running",
        must_do="nothing",
        outcome_states=("running",),
        ticket_states=("in_progress",),
    ),
    TaxonomyRow(
        key="idle",
        state=STATE_IDLE,
        label="Idle",
        waits_for="the next arrival",
        must_do="nothing",
        ticket_states=("backlog", "todo"),
    ),
    TaxonomyRow(
        key="review",
        state=STATE_PERSON,
        label="Needs you",
        waits_for="a review",
        must_do="review it",
        ticket_states=("blocked", "in_review"),
    ),
    TaxonomyRow(
        key="done",
        state=STATE_ENDED,
        label="Done",
        waits_for="nothing",
        must_do="nothing",
        outcome_states=("done",),
        ticket_states=("done",),
    ),
    TaxonomyRow(
        key="ended",
        state=STATE_ENDED,
        label="Ended",
        waits_for="nothing",
        must_do="nothing",
    ),
)

_BY_KEY: dict[str, TaxonomyRow] = {row.key: row for row in TAXONOMY}

#: Lexicon keys that are NOT inbox rows: they exist so every word of the IDE's
#: two maps lands somewhere, but a run in ``working`` is not something to show
#: in an inbox of decisions.
_NOT_A_DECISION = frozenset({"working", "idle", "done", "ended", "review"})


def taxonomy(key: str) -> TaxonomyRow:
    """The declared row for ``key``. A KeyError here is a bug, not user input."""
    return _BY_KEY[key]


def stage_states() -> tuple[str, ...]:
    """The four stages, in the order a person reads them."""
    return (STATE_PERSON, STATE_WORKING, STATE_IDLE, STATE_ENDED)


def row_for_outcome(state: str) -> str:
    """``outcome.resolve``'s state word → taxonomy key."""
    if state == "needs_you":
        return "node_review"
    for row in TAXONOMY:
        if state in row.outcome_states:
            return row.key
    return "ended"


def row_for_ticket(state: str) -> str:
    """The kanban's ticket state → taxonomy key."""
    for row in TAXONOMY:
        if state in row.ticket_states:
            return row.key
    return "review"


@dataclass(frozen=True, slots=True)
class Stage:
    """Where one run stands, and the reason, in the declared vocabulary."""

    run_id: str
    state: str
    key: str
    why: str

    @property
    def label(self) -> str:
        return taxonomy(self.key).label


@dataclass(frozen=True, slots=True)
class Row:
    """One inbox row — a thing a person can act on, with the command to do it."""

    key: str
    run_id: str
    what: str
    why: str
    next: str
    detail: dict[str, Any] = field(default_factory=dict)

    @property
    def label(self) -> str:
        return taxonomy(self.key).label

    @property
    def state(self) -> str:
        return taxonomy(self.key).state

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "label": self.label,
            "state": self.state,
            "run_id": self.run_id,
            "what": self.what,
            "why": self.why,
            "next": self.next,
            **self.detail,
        }


# ── reading the sources ────────────────────────────────────────────────────


def _db_path(home: Path) -> str | None:
    """``home/state.db`` when it exists — never create one just to look."""
    path = home / "state.db"
    return str(path) if path.is_file() else None


def _run_dir(home: Path, run_id: str) -> Path:
    return home / "runs" / run_id


def _status(home: Path, run_id: str) -> str | None:
    """``task_runs.status`` for the run, or ``None`` (no row, no database)."""
    db_file = _db_path(home)
    if db_file is None:
        return None
    try:
        from mini_ork.web.db import db_for  # noqa: PLC0415

        row = db_for(home).row("SELECT status FROM task_runs WHERE id = ?", (run_id,))
    except Exception:  # noqa: BLE001 — a read must never take the inbox down
        return None
    return str(row["status"]) if row else None


def _gate_index(db_path: str | None) -> "_Gates":
    """One read of ``mo_inbox_gates``, split by what the row asks of a person.

    The split is by ``feature``: ``review.after_node`` rows await a node review,
    ``review.before_merge`` rows await a merge approval, and everything else is
    an oversight gate. The bootstrap in ``oversight_inbox`` creates the table on
    open, which is why this is only called when the database file exists.
    """
    if db_path is None:
        return _Gates()
    try:
        from mini_ork.gates import oversight_inbox  # noqa: PLC0415
        from mini_ork.gates import merge_approval, node_review  # noqa: PLC0415

        pending = oversight_inbox.pending(db_path=db_path)
    except Exception:  # noqa: BLE001
        return _Gates()
    node_rows: dict[str, list[dict]] = {}
    merge_rows: dict[str, list[dict]] = {}
    others: list[dict] = []
    for row in pending:
        feature = str(row.get("feature") or "")
        gate_id = str(row.get("gate_id") or "")
        if feature == node_review.FEATURE and gate_id.startswith("node:"):
            parts = gate_id.split(":")
            if len(parts) >= 3:
                entry = dict(row)
                entry["node_id"] = parts[2]
                node_rows.setdefault(parts[1], []).append(entry)
        elif feature == merge_approval.FEATURE and gate_id.startswith(merge_approval.GATE_ID_PREFIX):
            run_id = gate_id[len(merge_approval.GATE_ID_PREFIX):]
            merge_rows.setdefault(run_id, []).append(dict(row))
        else:
            others.append(dict(row))
    return _Gates(node=node_rows, merge=merge_rows, other=others)


@dataclass(slots=True)
class _Gates:
    node: dict[str, list[dict]] = field(default_factory=dict)
    merge: dict[str, list[dict]] = field(default_factory=dict)
    other: list[dict] = field(default_factory=list)


def _is_paused(home: Path, run_id: str) -> bool:
    """The dispatcher's cost-pause sentinel for this run."""
    return (_run_dir(home, run_id) / ".cost-pause").is_file()


def _retry_hint(home: Path, run_id: str) -> dict[str, Any] | None:
    """The retry hint for a failed run, read-only (never written into the run)."""
    try:
        from mini_ork.recovery.retry_hint import load_or_compute  # noqa: PLC0415

        return load_or_compute(home, run_id, write=False)
    except Exception:  # noqa: BLE001
        return None


def _gate_for_run(gates: _Gates, run_id: str) -> tuple[str, dict] | None:
    """The highest-priority pending gate on this run, as ``(key, row)``."""
    merge = gates.merge.get(run_id)
    if merge:
        return "merge_approval", merge[0]
    node = gates.node.get(run_id)
    if node:
        return "node_review", node[0]
    return None


# ── the two public questions ───────────────────────────────────────────────


def stage_of(
    home: Path | str,
    run_id: str,
    *,
    gates: _Gates | None = None,
    db_path: str | None = None,
) -> Stage:
    """Which of the four stages ``run_id`` is in, and why.

    Precedence is the taxonomy's order, because the most consequential reason a
    run is stuck is the one to report: a paused run whose merge is also pending
    is paused *first* (it is spending nothing and moving nowhere), and a
    worktree is only "awaiting you" once no gate is already holding the run.
    """
    home = Path(home)
    db_path = db_path if db_path is not None else _db_path(home)
    gates = gates if gates is not None else _gate_index(db_path)

    if _is_paused(home, run_id):
        return Stage(run_id, STATE_PERSON, "cost_pause",
                     "the run stopped at its spend threshold")

    held = _gate_for_run(gates, run_id)
    if held is not None:
        key, row = held
        what = "the merge awaits approval" if key == "merge_approval" else (
            f"node {row.get('node_id', '?')} awaits review")
        return Stage(run_id, STATE_PERSON, key, what)

    from mini_ork import run_status  # noqa: PLC0415

    status = _status(home, run_id)
    if status is None:
        return Stage(run_id, STATE_IDLE, "idle", "nothing has run for it yet")
    if not run_status.is_terminal(status):
        return Stage(run_id, STATE_WORKING, "working", f"the run is {run_status.label(status).lower()}")

    if status == "failed":
        hint = _retry_hint(home, run_id)
        if hint is not None and hint.get("retryable"):
            return Stage(run_id, STATE_PERSON, "retry",
                         f"failed at {hint.get('failed_node') or 'a node'}")
        return Stage(run_id, STATE_ENDED, "ended", "the run failed and cannot be retried as-is")

    return Stage(run_id, STATE_ENDED, "done", run_status.label(status).lower())


def _worktree_rows(home: Path, claimed: set[str]) -> Iterable[Row]:
    """Open worktrees whose run is over — the keep-or-discard queue.

    A live run's worktree is not waiting on anyone; only a terminal run's is. A
    run already shown for a merge approval is skipped: the merge is what blocks
    it, and two rows for one decision is how an inbox becomes noise.
    """
    from mini_ork import run_status, workspaces  # noqa: PLC0415

    try:
        open_workspaces = workspaces.list_open(home)
    except Exception:  # noqa: BLE001
        return
    for ws in open_workspaces:
        if ws.run_id in claimed:
            continue
        status = _status(home, ws.run_id)
        if status is None or not run_status.is_terminal(status):
            continue
        try:
            snap = workspaces.status(ws)
        except Exception:  # noqa: BLE001 — a broken worktree must not blank the page
            continue
        if not snap.get("exists"):
            continue
        if not (snap.get("commits_ahead") or snap.get("uncommitted")):
            continue
        claimed.add(ws.run_id)
        n = int(snap.get("commits_ahead") or 0)
        yield Row(
            key="worktree",
            run_id=ws.run_id,
            what=ws.run_id,
            why=(f"{n} commit{'s' if n != 1 else ''} on {ws.branch} are not on "
                 f"{ws.base_branch}"),
            next=taxonomy("worktree").command.format(run=ws.run_id),
            detail={"branch": ws.branch, "base_branch": ws.base_branch,
                    "added": snap.get("added"), "removed": snap.get("removed")},
        )


def inbox(
    home: Path | str,
    *,
    limit: int | None = None,
    run_limit: int = 200,
) -> list[Row]:
    """Every decision waiting on a person, most consequential first.

    ``limit`` is a presentation cap, not a correctness one: the library returns
    everything by default so a caller can say "40 of 62"; ``run_limit`` bounds
    only the run scan (the same candidate window the IDE's fleet rows pay for).
    Gates and worktrees are read directly because they are few and their run
    ids may be older than the scan window.
    """
    home = Path(home)
    db_path = _db_path(home)
    gates = _gate_index(db_path)
    rows: list[Row] = []
    claimed: set[str] = set()
    titles: dict[str, str] = {}

    try:
        from mini_ork.acp.history import list_runs  # noqa: PLC0415

        candidates, _ = list_runs(home, limit=run_limit, offset=0)
    except Exception:  # noqa: BLE001
        candidates = []
    for cand in candidates:
        run_id = str(cand.get("run_id") or "")
        if run_id:
            titles[run_id] = str(cand.get("title") or run_id)

    for cand in candidates:
        run_id = str(cand.get("run_id") or "")
        if not run_id or run_id in claimed:
            continue
        stage = stage_of(home, run_id, gates=gates, db_path=db_path)
        if stage.key in _NOT_A_DECISION:
            continue
        claimed.add(run_id)
        rows.append(_row_for_stage(home, stage, titles.get(run_id, run_id), gates))

    # Oversight gates with no run id of their own (the executor never enqueues
    # them; an operator or a recipe does). One row each, oldest first.
    for gate in gates.other:
        inbox_id = gate.get("inbox_id")
        rows.append(Row(
            key="oversight_gate",
            run_id="",
            what=f"gate {gate.get('gate_id')} · {gate.get('feature')}",
            why="a gate was enqueued and nothing has resolved it",
            next=taxonomy("oversight_gate").command.format(inbox=inbox_id),
            detail={"inbox_id": inbox_id, "gate_id": gate.get("gate_id"),
                    "feature": gate.get("feature")},
        ))

    rows.extend(_worktree_rows(home, claimed))

    rank = {row.key: i for i, row in enumerate(TAXONOMY)}
    rows.sort(key=lambda r: (rank.get(r.key, len(TAXONOMY)), r.run_id))
    return rows if limit is None else rows[:limit]


def _row_for_stage(home: Path, stage: Stage, title: str, gates: _Gates) -> Row:
    template = taxonomy(stage.key).command
    detail: dict[str, Any] = {}
    node = ""
    if stage.key == "node_review":
        rows = gates.node.get(stage.run_id) or []
        node = str(rows[0].get("node_id") or "") if rows else ""
        detail["node_id"] = node
    if stage.key == "merge_approval":
        rows = gates.merge.get(stage.run_id) or []
        if rows:
            detail["inbox_id"] = rows[0].get("inbox_id")
    if stage.key == "retry":
        hint = _retry_hint(home, stage.run_id) or {}
        detail["retry"] = {k: hint.get(k) for k in
                           ("failed_node", "strategy", "needs_change", "command")}
    return Row(
        key=stage.key,
        run_id=stage.run_id,
        what=title,
        why=stage.why,
        next=template.format(run=stage.run_id, node=node),
        detail=detail,
    )


def render(rows: list[Row], total: int | None = None) -> str:
    """The human page: one block per row, then a one-line summary.

    ``total`` is the full count when the caller capped ``rows`` — printing the
    capped list as if it were everything is how an operator concludes there is
    nothing left to do.
    """
    total = len(rows) if total is None else total
    if not rows:
        return "Nothing needs you.\n"
    lines: list[str] = []
    counts: dict[str, int] = {}
    for row in rows:
        counts[row.label] = counts.get(row.label, 0) + 1
        lines.append(f"{row.key:<16} {row.run_id or '—':<24} {row.what}")
        lines.append(f"  {row.why}")
        lines.append(f"  next: {row.next}")
    summary = " · ".join(f"{n} {label.lower()}" for label, n in counts.items())
    lines.append("")
    if total > len(rows):
        lines.append(f"showing {len(rows)} of {total} waiting — {summary}")
    else:
        lines.append(f"{len(rows)} waiting — {summary}")
    return "\n".join(lines) + "\n"


def main(rest: list[str], root: str) -> int:
    del root
    import json
    import sys

    home = Path(os.environ.get("MINI_ORK_HOME", "").strip() or (Path.cwd() / ".mini-ork"))

    args = list(rest)
    as_json = "--json" in args
    if as_json:
        args.remove("--json")
    limit = 40
    if "--limit" in args:
        i = args.index("--limit")
        try:
            limit = int(args[i + 1])
        except (IndexError, ValueError):
            sys.stderr.write("mini-ork inbox: --limit needs an integer\n")
            return 2
        del args[i:i + 2]
    if any(a in ("-h", "--help") for a in args):
        sys.stdout.write(__doc__ or "")
        return 0
    if args:
        sys.stderr.write(f"mini-ork inbox: unknown argument(s) {args!r}\n")
        return 2

    everything = inbox(home)
    rows = everything[:limit]
    if as_json:
        sys.stdout.write(json.dumps(
            {"rows": [r.to_dict() for r in rows], "total": len(everything)},
            indent=2,
        ) + "\n")
    else:
        sys.stdout.write(render(rows, total=len(everything)))
    return 0
