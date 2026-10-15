"""Inbox — what needs the operator first, across every run of the project.

No tabs. Data: ``mini_ork.acp.fleet.fleet_rows`` (the same read ``runs.py``
uses) supplies the candidate rows; ``outcome.resolve`` turns each into one
decision; ``task_state.landed_report`` and ``repair.json`` drop the runs that
are already done or owned by auto-repair.

**One row per task, not per run.** A retry launches a *new* run from a copied
kickoff (``scheduler.build_carry_over_kickoff`` writes ``<id>-attempt-<n>.md``),
so a task that failed twice and then published leaves three runs in the
window — two dead ancestors and the live one. Grouping by
``fleet.lineage_key`` collapses them to the newest attempt, which is the only
one that describes the task's current state; the dead ancestors are counted,
not listed.

A row that cannot be read costs its own row only (``spec.guarded``), never the
page.

The "From agent sessions" section is fed by ContextNest's per-urgency
``/api/v1/inbox`` queries (now/soon/later) plus ``/api/v1/sessions/attention``
for the "why" line. Items are scoped both server-side (``project=``) and
client-side (``metadata.project_cwd`` must equal the project root — the same
fail-closed rule ``lanes.py`` applies to atoms). The section degrades to the
one-line ``_cn.section_status`` state when CN is down / disabled /
config-errored, so the existing inbox renders unchanged in those paths.
"""
from __future__ import annotations

import json
import re as _re
import time
import urllib.parse as _url
from pathlib import Path
from typing import Any

from mini_ork.acp.fleet import lineage_key
from mini_ork.acp.history import list_runs
from mini_ork.ide_pages import spec as S

# "Runs from the last 7 days" — the inbox's candidate window.
_WINDOW_DAYS = 7
# Runs swept to answer "did some *other* attempt of this task settle it?".
# Matches ``fleet.CANDIDATE_LIMIT``; a lineage older than this is beyond the
# window anyway.
_CANDIDATE_LIMIT = 200
# Statuses that mean a run shipped — a sibling in one of these settles the
# whole task. Deliberately a positive list: ``executing``/``planned`` are
# live, and treating "not failed" as settled would hide a task in flight.
_SETTLED_STATUSES = frozenset({"published", "done", "complete", "completed",
                               "succeeded", "success"})
# At most this many grouped rows; the rest collapse into one "N more…" link.
_ROW_CAP = 20
# Only the newest rows get a full outcome card: each one loads the run and
# resolves its outcome (~0.65 s CPU on a large home), and the page must stay
# well inside the host's 30 s per-process budget. The rest are compact rows.
_DETAILED_ROWS = 6
# ``fleet_rows`` clamps ``limit`` to its own ``MAX_LIMIT = 50``.
_FLEET_LIMIT = 50

# Run ids / titles that are harness noise, not operator work: smoke-test
# drivers, probes, and the vt5/vt6 verification bots. They run on a timer,
# are expected to "fail" whenever the thing they poke is down, and would
# otherwise fill the whole window — which is exactly how a 34-row inbox
# reads as unusable. Matched case-insensitively against id + title; the
# count is always shown and ``?show=all`` overrides the filter.
_NOISE_MARKERS = ("obs-smoke", "__probe", "vt5-", "vt6-", "smoke-probe")


def _run_mod():
    """The sibling ``run`` page — reached lazily so importing this module stays light."""
    from mini_ork.ide_pages import run as run_mod

    return run_mod


def _outcome_mod():
    from mini_ork.ide_pages import outcome

    return outcome


def _fleet(home: Path, state: str):
    from mini_ork.acp.fleet import fleet_rows

    return fleet_rows(home, state=state, limit=_FLEET_LIMIT)


def _run_dir(home: Path, run_id: str) -> Path:
    return Path(home) / "runs" / run_id


def _landed(home: Path, run_id: str) -> bool:
    """True when the change landed elsewhere (``landed.json``) — the run is done."""
    try:
        from mini_ork.acp.task_state import landed_report

        return landed_report(_run_dir(home, run_id)) is not None
    except Exception:  # noqa: BLE001 — task_state optional: keep the row
        return False


def _repairing(home: Path, run_id: str) -> bool:
    """True when auto-repair owns the run (``repair.json`` state is ``repairing``)."""
    try:
        data = json.loads((_run_dir(home, run_id) / "repair.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return isinstance(data, dict) and str(data.get("state") or "") == "repairing"


def _dismissed(home: Path, run_id: str) -> bool:
    """True when the operator hid this run (``board dismiss`` wrote ``dismissed.json``)."""
    return (_run_dir(home, run_id) / "dismissed.json").is_file()


def _in_window(row: Any, cutoff: int) -> bool:
    """A row with a known start inside the window; an unknown start is kept."""
    return row.started_at is None or row.started_at >= cutoff


def _is_noise(row: Any) -> bool:
    """True for harness runs (smoke/probe/verifier bots) that are not operator work.

    The recipe is part of the haystack because the marker often lives there and
    not in the title: ``obs-smoke-nopub`` runs are titled after whatever they
    poked ("Smoke: reflect trace finalize"), which carries no marker at all.
    """
    text = f"{row.run_id} {row.title} {row.recipe}".lower()
    return any(marker in text for marker in _NOISE_MARKERS)


def _age(seconds: int) -> str:
    """A coarse "how long ago" for a meta cell."""
    if seconds < 90:
        return "just now"
    if seconds < 3600:
        return f"{seconds // 60}m ago"
    if seconds < 86400:
        return f"{seconds // 3600}h ago"
    return f"{seconds // 86400}d ago"


def _meta(row: Any, attempts: int, now: int) -> list[dict[str, Any]]:
    """The identity row under a detailed callout: what run this *is*.

    The old rows carried a title and nothing else, so three attempts of one
    task, or two different tasks that share a title, were indistinguishable.
    """
    cells: list[dict[str, Any]] = [S.mono(row.run_id)]
    if row.recipe:
        cells.append(S.muted(row.recipe))
    if attempts > 1:
        cells.append(S.muted(f"{attempts} attempts"))
    if row.started_at:
        cells.append(S.muted(_age(max(0, now - row.started_at))))
    return cells


def _dismiss_button(run_id: str) -> dict[str, Any]:
    return S.btn("Dismiss", S.cli("board", "dismiss", run_id,
                                  confirm="Hide this run from the inbox?"), "ghost")


def _row(home: Path, row: Any, attempts: int, now: int) -> dict[str, Any]:
    """One inbox row: the run's outcome as a full-width callout."""
    run = _run_mod()._load(home, row.run_id)
    if run is None:
        raise RuntimeError(f"no run {row.run_id}")
    out = _outcome_mod().resolve(run)
    title = str(run.card.get("title") or row.title or row.run_id)
    text = str(out.get("text") or "")
    detail = str(out.get("detail") or "").strip()
    text_md = f"{text}\n\n{detail}" if detail else text
    actions = list(out.get("actions") or [])
    actions.append(S.btn("Open run", S.open_run(run.id, title), "ghost"))
    actions.append(_dismiss_button(row.run_id))
    return S.callout(title, text_md, tone=str(out.get("tone") or "muted"),
                     icon=str(out.get("icon") or ""), meta=_meta(row, attempts, now),
                     actions=actions, full=True)


def _compact_item(row: Any, now: int) -> dict[str, Any]:
    """A compact row for the tail of the list — same facts, no outcome resolve."""
    sub = row.recipe or ("needs you" if row.state == "needs_you" else "failed")
    if row.started_at:
        sub = f"{sub} · {_age(max(0, now - row.started_at))}"
    return S.dot(row.title or row.run_id, sub,
                 acts=[S.btn("Open", S.open_run(row.run_id, row.title or row.run_id), "ghost"),
                       _dismiss_button(row.run_id)])


# r-ide-review: "Awaiting review" — the human-approval inbox rows surfaced
# on the page the operator sees first thing. Rows come from the same
# ``pending`` read the CLI ``mini-ork approvals list`` runs — no subprocess,
# no second source of truth — so the inbox can never disagree with what the
# CLI would print. A missing DB / table / context drops the section silently.


def _awaiting_review_rows(home: Path) -> list[dict[str, Any]]:
    """Decode pending node / merge rows into the shape ``_awaiting_review_section`` draws.

    Two families — and ONLY these two — surface here:

    * ``node:<run>:<node>`` rows from ``review.after_node`` (a finished node
      whose output is awaiting human review).
    * ``merge:<run>`` rows from ``review.before_merge`` (a run whose merge is
      blocked on approval).

    The "awaiting-workspace" rows (open worktrees under a ``required`` merge
    policy that have not yet been enqueued) are deliberately omitted: the CLI
    walks that path via ``approvals_cmd._cmd_list``, but to know whether a
    workspace still needs approval it must call ``workspaces.content_sha`` —
    a ``git write-tree`` per open workspace, every page build. The kickoff
    forbids subprocesses on page builds other than the one ``git show-ref``
    on the node page; the inbox must stay strictly in-process. The CLI's
    listing and the IDE listing can disagree here; the kickoff accepts that.

    Returns ``[]`` on every failure (the inbox page must never raise here).
    """
    out: list[dict[str, Any]] = []
    try:
        db_path = str(Path(home) / "state.db")
        if not Path(db_path).is_file():
            return out
        from mini_ork.gates.oversight_inbox import pending as pending_gates
        try:
            rows = pending_gates(db_path=db_path) or []
        except Exception:  # noqa: BLE001 — pending() may self-bootstrap / fail
            rows = []
        for r in rows:
            if not isinstance(r, dict):
                continue
            gate_id = str(r.get("gate_id") or "")
            feature = str(r.get("feature") or "")
            ctx = r.get("context") if isinstance(r.get("context"), dict) else {}
            row_age = _age_seconds(r.get("enqueued_at"))
            enqueued_at = r.get("enqueued_at")
            if gate_id.startswith("node:") and feature == "review.after_node":
                run_id = str(ctx.get("run_id") or _gate_run_id(gate_id) or "")
                node_id = str(ctx.get("node_id") or "")
                if not run_id or not node_id:
                    continue
                out.append({
                    "kind": "node", "run": run_id, "node": node_id,
                    "node_type": str(ctx.get("node_type") or ""),
                    "enqueued_at": _to_float(enqueued_at),
                    "age_text": _age_str(row_age),
                })
            elif gate_id.startswith("merge:") and feature == "review.before_merge":
                run_id = str(ctx.get("run_id") or _gate_run_id(gate_id) or "")
                if not run_id:
                    continue
                out.append({
                    "kind": "merge", "run": run_id, "node": "",
                    "node_type": "",
                    "enqueued_at": _to_float(enqueued_at),
                    "age_text": _age_str(row_age),
                })
    except Exception:  # noqa: BLE001 — every failure path omits the section
        return []
    return out


def _gate_run_id(gate_id: str) -> str:
    """Second ``:``-segment of ``node:run:node:created_at`` / ``merge:run[:head]``."""
    parts = gate_id.split(":", 2)
    return parts[1] if len(parts) >= 2 else ""


def _age_seconds(value: Any) -> int:
    try:
        return max(0, int(float(value or 0)))
    except (TypeError, ValueError):
        return 0


def _to_float(value: Any) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _age_str(seconds: int) -> str:
    if seconds < 60:
        return "just now"
    if seconds < 3600:
        return f"{seconds // 60}m ago"
    if seconds < 86400:
        return f"{seconds // 3600}h ago"
    return f"{seconds // 86400}d ago"


def _awaiting_review_section(home: Path, now: int) -> list[dict[str, Any]]:
    """``[callout]`` listing rows awaiting approval, or ``[]`` when empty.

    The kickoff says the section is shown only when non-empty: an inbox with
    nothing pending leaves no trace on the page. Per-row actions: Approve
    for every row, Request changes… for node rows. Clicking the row opens
    the run.
    """
    rows = _awaiting_review_rows(home)
    if not rows:
        return []
    items: list[dict[str, Any]] = []
    for row in rows:
        kind_label = "merge" if row["kind"] == "merge" else "node"
        run_id = row["run"]
        sub_bits = [kind_label]
        if row["kind"] == "node":
            sub_bits.append(row["node"] or "node")
        if row["age_text"]:
            sub_bits.append(row["age_text"])
        sub = " · ".join(b for b in sub_bits if b)
        actions: list[dict[str, Any]] = []
        if row["kind"] == "node" and row["node"]:
            actions.append(S.btn(
                "Approve",
                S.cli("approvals", "approve", run_id, "--node", row["node"],
                      confirm=(f"Approve {row['node']}'s output and resume the run?"),
                      home=False),
                "primary"))
            actions.append(S.btn(
                "Request changes…",
                S.thread(f"/request-changes {run_id} {row['node']} "),
                "default"))
        else:
            actions.append(S.btn(
                "Approve",
                S.cli("approvals", "approve", run_id,
                      confirm=f"Approve merging {run_id} into the base branch?",
                      home=False),
                "primary"))
        items.append(S.dot(
            f"{kind_label} · {row['node']}" if row["kind"] == "node"
            else f"merge · {run_id}",
            sub,
            acts=[S.btn("Open run", S.open_run(run_id, run_id), "ghost"), *actions],
        ))
    return [S.lst(
        "Awaiting review", items, full=True,
        note="The same rows `mini-ork approvals list --json` reports. "
             "Click a row to open the run, or approve / request changes inline.")]


def _inbox(home: Path, now: int, errors: dict[str, str], ctx: dict[str, Any],
           show_all: bool) -> list[dict[str, Any]]:
    cutoff = now - _WINDOW_DAYS * 86400
    pulled: list[Any] = []
    for bucket in ("needs_you", "failed"):
        try:
            got, _ = _fleet(home, bucket)
        except Exception as exc:  # noqa: BLE001 — one fleet read must not blank the page
            errors[f"inbox {bucket}"] = f"{type(exc).__name__}: {exc}"
            continue
        pulled.extend(got)

    # One classification pass over every candidate: each of the three
    # filesystem probes runs at most once per run, and the runs are then
    # ordered once, so the group head below is simply the first member.
    classified: list[tuple[Any, dict[str, bool]]] = []
    seen: set[str] = set()
    for row in pulled:
        if row.run_id in seen or not _in_window(row, cutoff):
            continue
        seen.add(row.run_id)
        classified.append((row, {
            "repairing": _repairing(home, row.run_id),
            "landed": _landed(home, row.run_id),
            "dismissed": _dismissed(home, row.run_id),
            "noise": _is_noise(row),
        }))
    classified.sort(key=lambda pair: pair[0].started_at or 0, reverse=True)

    groups: dict[str, list[tuple[Any, dict[str, bool]]]] = {}
    order: list[str] = []
    repairing_rows: list[Any] = []
    hidden_noise: list[Any] = []
    for row, flags in classified:
        if flags["repairing"]:
            repairing_rows.append(row)
            continue
        if flags["noise"]:
            if not show_all:
                hidden_noise.append(row)
                continue
        key = lineage_key(row.recipe, row.kickoff) or row.run_id
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append((row, flags))

    # A task's *successful* attempt is in neither bucket we pulled — it is
    # ``published``, which is neither "needs you" nor "failed" — so a group
    # whose own head failed still looks live even though the task shipped.
    # One ``list_runs`` sweep (200 rows, ~50 ms, no ``task_state``) carries
    # every recent run's status and kickoff, which is all a lineage needs to
    # find the sibling that settled it.
    settled: set[str] = set()
    recent: list[Any] = []
    try:
        recent, _ = list_runs(Path(home), limit=_CANDIDATE_LIMIT, offset=0)
        for run in recent:
            if str(run.get("status") or "") in _SETTLED_STATUSES:
                key = lineage_key(str(run.get("recipe") or ""),
                                  str(run.get("kickoff_path") or ""))
                if key:
                    settled.add(key)
    except Exception as exc:  # noqa: BLE001 — without it we show rows we might hide
        errors["inbox settled"] = f"{type(exc).__name__}: {exc}"

    # The questions. A run blocked on an ask is a state the outcome resolver
    # describes ("needs you") but cannot *show* — only the ask file holds the
    # question itself, and a question the operator cannot read is not
    # answerable. One listdir per recent run that has an ``asks`` dir; a run
    # without one costs a failed ``listdir`` and nothing else.
    asking: list[tuple[str, str, str, int, int]] = []
    epoch = _run_mod()._epoch
    for run in recent:
        run_id = str(run.get("run_id") or "")
        created = epoch(run.get("created_at"))
        # ``list_runs`` rows carry both stamps as ISO strings; an unparseable
        # one is kept rather than dropped, the way ``_in_window`` treats an
        # unknown start.
        if not run_id or (created is not None and created < cutoff):
            continue
        asks = _run_mod().open_asks(_run_dir(home, run_id))
        if not asks:
            continue
        # Same reasons to leave as any other row: the change landed elsewhere,
        # a later attempt published, the operator hid it, auto-repair owns it.
        key = lineage_key(str(run.get("recipe") or ""), str(run.get("kickoff_path") or ""))
        if (key and key in settled) or _landed(home, run_id) or _dismissed(home, run_id) \
                or _repairing(home, run_id):
            continue
        asking.append((run_id, str(run.get("title") or run_id), str(asks[0]["question"]),
                       len(asks), epoch(run.get("updated_at")) or 0))
    asking.sort(key=lambda item: -item[4])

    waiting: list[tuple[Any, int]] = []
    superseded = 0
    asking_ids = {item[0] for item in asking}
    for key in order:
        members = groups[key]
        head_row, head_flags = members[0]  # newest attempt: the task's live state
        # The task is settled — its change landed elsewhere, a later attempt
        # published, or the operator hid it — so the whole group leaves the
        # inbox, dead ancestors and all.
        if head_flags["landed"] or head_flags["dismissed"] or key in settled:
            continue
        # A run paused on a question is owned by the questions group above,
        # which carries the question text instead of only the state it leaves
        # the run in. Not counted as superseded either: nothing settled it.
        if head_row.run_id in asking_ids:
            continue
        waiting.append((head_row, len(members)))
        superseded += len(members) - 1

    # "What needs you first, then what failed": a decision the human owes
    # outranks a failure the human can defer, regardless of age.
    waiting.sort(key=lambda pair: (pair[0].state != "needs_you", -(pair[0].started_at or 0)))

    # Every run left in ``asking`` is one the questions group shows instead of a
    # ``waiting`` row, and an unanswered ask is by definition the operator's —
    # the engine pauses the run on it. So it counts as "needs you" even though
    # it never reached ``waiting``.
    ctx["needs_you"] = sum(1 for row, _ in waiting if row.state == "needs_you") + len(asking)
    ctx["questions"] = len(asking)
    ctx["failed"] = sum(1 for row, _ in waiting if row.state != "needs_you")
    ctx["repairing"] = len(repairing_rows)
    ctx["superseded"] = superseded
    ctx["hidden"] = len(hidden_noise)
    noise_total = sum(1 for _, flags in classified if flags["noise"])

    sections: list[dict[str, Any]] = []
    if asking:
        items = [
            S.warn(question,
                   f"{count} questions · {title}" if count > 1 else title,
                   acts=[S.btn("Answer", S.open_run(run_id, title, "questions"), "primary"),
                         _dismiss_button(run_id)])
            for run_id, title, question, count, _ in asking
        ]
        sections.append(S.lst(
            "Needs your answer", items, full=True,
            note="Runs paused on a question. Answer on the run's Questions tab and the run "
                 "continues from where it stopped."))
    shown = waiting[:_ROW_CAP]
    for row, attempts in shown[:_DETAILED_ROWS]:
        sections += S.guarded(errors, row.run_id,
                              lambda r=row, a=attempts: [_row(home, r, a, now)])
    compact = shown[_DETAILED_ROWS:]
    if compact:
        sections.append(S.lst("Also waiting", [_compact_item(r, now) for r, _ in compact],
                              full=True))
    if not shown:
        sections.append(S.callout(
            "Nothing needs you",
            "Runs that wait on a decision, a fix, or a cost cap land here.",
            tone="green", icon="✓", full=True))
    if repairing_rows:
        items = [
            S.dot(r.title or r.run_id,
                  f"{r.recipe} · being repaired" if r.recipe else "being repaired",
                  acts=[S.btn("Open", S.open_run(r.run_id, r.title or r.run_id), "ghost")])
            for r in repairing_rows
        ]
        sections.append(S.lst("Being repaired", items, full=True,
                              note="Auto-repair owns these runs; they are not waiting on you."))
    if len(waiting) > _ROW_CAP:
        more = len(waiting) - _ROW_CAP
        sections.append(S.lst(
            "More",
            [S.dot(f"{more} more…", "Older runs that still need you",
                   acts=[S.btn("Open the runs page", S.page_link("runs"), "ghost")])],
            full=True))
    if show_all and noise_total:
        # The override is two-way: without this the only way back from
        # ``?show=all`` would be to edit the URL by hand.
        sections.append(S.lst(
            "Filtered",
            [S.dot(f"{noise_total} smoke / probe runs", "Shown because ?show=all",
                   acts=[S.btn("Hide", S.page_link("inbox", show=""), "ghost")])],
            full=True))
    elif hidden_noise:
        sections.append(S.lst(
            "Filtered",
            [S.dot(f"{len(hidden_noise)} smoke / probe runs",
                   "Harness runs that are not operator work",
                   acts=[S.btn("Show all", S.page_link("inbox", show="all"), "ghost")])],
            full=True))
    return sections


# ── ContextNest: "From agent sessions" (Inbox rank 1) ─────────────────────
#
# The kickoff (rank 1 — "Which sessions are blocked on me, and on which run?")
# asks the Inbox page to surface ContextNest's per-urgency asks
# (``/api/v1/inbox?urgency=now|soon|later&project=``) plus the per-session
# ``why`` (``/api/v1/sessions/attention?project=``), bucketed now → soon →
# later and capped at 30 rows total. Items are scoped to this project both
# server-side (project=) and client-side (``metadata.project_cwd`` must equal
# the project root — the same fail-closed rule ``lanes.py`` applies to atoms).
# Each row opens the source session and, when the ask names a run id, opens
# the run too. A chip on the page shows the "now" count, and the section
# degrades to the one-line ``_cn.section_status`` state when CN is down /
# disabled / config-errored, so the existing inbox renders unchanged in those
# paths. Resolving an item is out of scope until ContextNest adds a resolve
# route; no fake resolve button is wired.

_URGENCY_ORDER = ("now", "soon", "later")
_AGENT_SECTION_LIMIT = 30
# Bounded word-boundary search: ``run-`` is preceded by a non-word character
# (or text start), the digits are any run, and the trailing token is a
# ``[A-Za-z0-9]`` followed by ``[A-Za-z0-9-]*`` — so ``run-123-456`` matches
# (3 digits, 3 alnum) but ``runa-123-456`` does not, and a bare ``run-``
# without a tail does not either. Bare substring matching would hyperlink
# non-run text; the ``cn_client.extract_run_id`` anchored regex would miss
# the acceptance's ``run-123-456`` fixture.
_RUN_ID_RE = _re.compile(r"\brun-\d+-[A-Za-z0-9][A-Za-z0-9-]*")


def _read_inbox_urgency(project: str, urgency: str) -> dict:
    """Read ``/api/v1/inbox?urgency=<u>&project=<p>`` and return the parsed body.

    A failed read (``_get`` returns ``"{}"``) returns ``{}`` so the
    surrounding ``cached_section`` slot treats it as unreachable — the same
    contract ``_read_atoms`` in ``lanes.py`` follows. A non-dict JSON body
    (server typo) becomes ``{}`` for the same reason.
    """
    from mini_ork import cn_client
    path = (
        f"/api/v1/inbox?urgency={_url.quote(urgency)}"
        f"&project={_url.quote(project)}&limit=30"
    )
    body = cn_client._get(path)
    if not body:
        return {}
    try:
        data = json.loads(body)
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _read_sessions_attention(project: str) -> dict:
    """Read ``/api/v1/sessions/attention?project=<p>`` and return the parsed body.

    Same fail-soft contract as :func:`_read_inbox_urgency` so a single CN
    outage degrades both reads to the same one-line state on the page.
    """
    from mini_ork import cn_client
    path = f"/api/v1/sessions/attention?project={_url.quote(project)}"
    body = cn_client._get(path)
    if not body:
        return {}
    try:
        data = json.loads(body)
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _item_project_cwd(item: Any) -> str:
    """The project root an inbox item claims, or ``""`` when the item has none.

    Items carry the cwd under ``metadata.project_cwd``; older payloads use a
    top-level ``project_cwd`` or ``cwd``. Only a non-empty string is treated
    as a claim — anything else is rejected so the page can never leak a
    foreign-project item through a missing field.
    """
    if not isinstance(item, dict):
        return ""
    for key in ("project_cwd", "cwd"):
        v = item.get(key)
        if isinstance(v, str) and v:
            return v
    meta = item.get("metadata")
    if isinstance(meta, dict):
        v = meta.get("project_cwd")
        if isinstance(v, str) and v:
            return v
    return ""


def _filter_sessions(sessions: Any, project: str) -> list:
    """Drop attention sessions whose ``project_cwd`` is not the project root.

    Same fail-closed defence as the inbox-item filter: server-side
    ``project=`` is one layer, the client-side equality check is the
    second. A nested checkout (``/ps/mini-ork/child``) is not equal to the
    project root and is dropped.
    """
    if not project:
        return []
    out: list = []
    for s in sessions or []:
        if not isinstance(s, dict):
            continue
        v = s.get("project_cwd")
        if isinstance(v, str) and v == project:
            out.append(s)
    return out


def _attention_lookup(sessions: list) -> dict[str, dict]:
    """Map ``session_id`` → attention record, so a row can render the "why" line."""
    out: dict[str, dict] = {}
    for s in sessions:
        sid = str(s.get("session_id") or "")
        if sid:
            out[sid] = s
    return out


def _ask_text(item: dict) -> str:
    """The ask itself, preferring the live ``content`` / ``subject`` / ``action`` fields."""
    if not isinstance(item, dict):
        return ""
    for key in ("content", "subject", "action", "text", "body"):
        v = item.get(key)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return ""


def _ask_session_id(item: dict) -> str:
    """The session id, preferring ``session_id`` then the bare ``id`` field."""
    if not isinstance(item, dict):
        return ""
    for key in ("session_id", "id"):
        v = item.get(key)
        if isinstance(v, str) and v:
            return v
    return ""


def _attention_why(attention: dict) -> str:
    """A short "why this needs you" line for the row sub cell.

    The attention record carries ``attention_count`` and a ``by_kind`` map;
    the shortest honest summary is the count plus the most-common kind.
    Empty / malformed input renders ``""`` so the row's sub cell stays clean.
    """
    if not isinstance(attention, dict):
        return ""
    count = int(attention.get("attention_count") or 0)
    by_kind = attention.get("by_kind") if isinstance(attention.get("by_kind"), dict) else {}
    if not count and not by_kind:
        return ""
    if not by_kind:
        return f"{count} pending" if count else ""
    top = max(by_kind.items(), key=lambda kv: int(kv[1] or 0))
    return f"{count} pending · {top[0]}"


def _extract_run_id(text: str) -> str:
    """A bounded word-boundary search for ``run-<digits>-<alnum>…`` in text.

    The acceptance fixture ``run-123-456`` is looser than
    :func:`mini_ork.cn_client.extract_run_id`'s ``run-\\d{6,}-[A-Za-z0-9]{4,}``
    anchor, so the page needs its own bounded search. The leading ``run-``
    is preceded by a non-word character (or text start) and the trailing
    token is bounded by ``[A-Za-z0-9][A-Za-z0-9-]*`` — bare substring
    matching would hyperlink non-run text.
    """
    if not text:
        return ""
    m = _RUN_ID_RE.search(text)
    return m.group(0) if m else ""


def _row_actions(ask_text: str, session_id: str) -> list[dict[str, Any]]:
    """The buttons for one ask row: open the session, and the run when named."""
    acts: list[dict[str, Any]] = []
    if session_id:
        acts.append(S.btn("Open session",
                          S.page_link("cn_session", id=session_id), "ghost"))
    run_id = _extract_run_id(ask_text)
    if run_id:
        acts.append(S.btn("Open run", S.open_run(run_id, run_id), "ghost"))
    return acts


def _build_bucketed_rows(items: list, sessions: list,
                         project: str) -> tuple[list[dict[str, Any]], int]:
    """Bucket items by urgency, drop foreign-project ones, cap at 30 rows total.

    Returns ``(rows, overflow)`` where ``rows`` is the rendered slice and
    ``overflow`` is the count of items dropped by the cap (carried in the
    section note the way ``inbox.py``'s "N more…" rows do). The cap is
    COMBINED across the three buckets — ``now`` fills first, then ``soon``,
    then ``later`` — so a 25-now + 5-soon render carries a now-count of 25.
    """
    bucket_map: dict[str, list[dict]] = {u: [] for u in _URGENCY_ORDER}
    for it in items:
        if not isinstance(it, dict):
            continue
        if _item_project_cwd(it) != project:
            continue
        bucket = str(it.get("urgency") or "")
        if bucket not in bucket_map:
            continue
        bucket_map[bucket].append(it)

    by_sid = _attention_lookup(sessions)
    rows: list[dict[str, Any]] = []
    for urgency in _URGENCY_ORDER:
        for it in bucket_map[urgency]:
            ask = _ask_text(it)
            sid = _ask_session_id(it)
            why = _attention_why(by_sid.get(sid, {}))
            label = ask or why or "(empty ask)"
            sub_bits = [urgency]
            if why:
                sub_bits.append(why)
            rows.append({
                "urgency": urgency,
                "text": label,
                "sub": " · ".join(sub_bits),
                "ask_text": ask,
                "session_id": sid,
            })
    overflow = 0
    if len(rows) > _AGENT_SECTION_LIMIT:
        overflow = len(rows) - _AGENT_SECTION_LIMIT
        rows = rows[:_AGENT_SECTION_LIMIT]
    return rows, overflow


def _agent_sessions_section(home: Path) -> tuple[list[dict[str, Any]], int]:
    """The "From agent sessions" section + the "now" chip count.

    Returns ``(sections, now_count)``. ``now_count`` is the number of "now"
    items in the rendered 30-row slice — used by :func:`build` to add the
    page chip ONLY on a healthy read with at least one now item, so a
    degraded render adds no chip and the existing inbox kanban tests
    (which do not stub CN) see no new chip.

    Never raises: a render-time bug falls back to the one-line state so the
    existing inbox renders unchanged.
    """
    from mini_ork.ide_pages import _cn
    try:
        project = str(home.parent.resolve()) if home else ""
        if not project:
            return [], 0
        payloads: dict[str, dict] = {}
        for urgency in _URGENCY_ORDER:
            payloads[urgency] = _cn.render_cached_section(
                "inbox-needs-you", f"{urgency}:{project}",
                lambda u=urgency: _read_inbox_urgency(project, u),
            )
        payloads["attention"] = _cn.render_cached_section(
            "sessions-attention", f"attention:{project}",
            lambda: _read_sessions_attention(project),
        )
        # Degraded state from any of the four reads: the one-line state, with
        # no chip and no bucket rows. The existing inbox renders unchanged.
        for payload in payloads.values():
            if not isinstance(payload, dict):
                continue
            if (payload.get("_disabled") or payload.get("_unreachable")
                    or payload.get("_config_error")):
                return [S.lst("From agent sessions",
                              [_cn.section_status(payload)], full=True)], 0
        # Healthy read: stamp the bucket on each item and collect.
        items: list = []
        for urgency in _URGENCY_ORDER:
            for it in (payloads[urgency].get("items") or []):
                if not isinstance(it, dict):
                    continue
                if _item_project_cwd(it) != project:
                    continue
                stamped = dict(it)
                stamped["urgency"] = urgency
                items.append(stamped)
        sessions: list = []
        if isinstance(payloads["attention"], dict):
            sessions = _filter_sessions(
                payloads["attention"].get("sessions") or [], project)
        rows, overflow = _build_bucketed_rows(items, sessions, project)
        if not rows:
            return [S.lst("From agent sessions",
                          [S.dot("No asks recorded",
                                 "ContextNest returned no inbox or attention "
                                 "rows for this project")],
                          full=True,
                          note="ContextNest asks · bucketed by urgency · "
                               "scoped to this project")], 0
        now_count = sum(1 for r in rows if r["urgency"] == "now")
        sections: list[dict[str, Any]] = []
        for urgency in _URGENCY_ORDER:
            bucket_rows = [r for r in rows if r["urgency"] == urgency]
            if not bucket_rows:
                continue
            items_list = [
                S.dot(r["text"], r["sub"],
                      acts=_row_actions(r["ask_text"], r["session_id"]))
                for r in bucket_rows
            ]
            sections.append(S.lst(
                f"From agent sessions · {urgency}", items_list, full=True))
        if overflow:
            sections.append(S.lst(
                "More",
                [S.dot(f"{overflow} more…",
                       "Older asks beyond the 30-row cap; resolve them "
                       "in ContextNest")],
                full=True))
        return sections, now_count
    except Exception:
        # Defensive: a render-time bug must NEVER blank the page. A
        # one-line dot section stands in so the operator can still see
        # the rest of the inbox unchanged.
        return [S.lst("From agent sessions",
                      [S.dot("Could not read this", "agent sessions read failed")],
                      full=True)], 0


# ── ContextNest: "Shipped since yesterday" (Inbox rank 4) ──────────────────
#
# The kickoff rank-4 ask is to surface features shipped in the last 24 hours
# (``GET /api/v1/features?since=24h``) so the operator can see what's landed
# since yesterday at a glance. The endpoint takes no server-side ``project=``
# filter — that field is keyed on session cwd, not files — so scoping is done
# client-side on ``files[]`` against the project root (``home.parent.resolve()``,
# the same rule ``_agent_sessions_section`` applies).
#
# A feature is "in-project" when at least one of its ``files[]`` entries
# resolves inside the project root (relative paths joined to the root,
# absolute paths taken as-is, ``..``-escapes rejected — the same
# ``cn_session._opens_path`` convention). A feature with no in-project file
# is dropped even when its session cwd matches the project (explicit
# acceptance: a feature is a project feature only when one of its real
# files lives in the project root).
#
# The section degrades to the one-line ``_cn.section_status`` state on
# down/disabled/config-error and adds NO page chip — exactly the way
# ``_agent_sessions_section`` adds a chip only on a healthy read with at
# least one now item. The existing inbox (and ``test_ide_pages_inbox_kanban``
# which does not stub CN) renders unchanged on every degraded path.
#
# The Re-test button seeds a **draft mini-ork agent thread** — a ``thread``
# action carrying ``how_to_test`` verbatim as the draft thread's first
# prompt — the page never executes the recipe. A feature without
# ``how_to_test`` still gets the button; the seed text just stays shorter.

_SHIPPED_SINCE = "24h"
_SHIPPED_CACHE_SECTION = "inbox-shipped-today"
_SHIPPED_ROW_LIMIT = 30


def _fetch_shipped_recent() -> dict:
    """``GET /api/v1/features?since=24h`` → ``dict``.

    No ``project=`` query: that key filters by session cwd, which would let
    cross-cwd features slip in; client-side ``files[]`` scoping against the
    project root is the only correct gate. ``cn_client._get`` returns
    ``"{}"`` on any network error, so :func:`_cn.render_cached_section`
    converts the failure to the unreachable one-line state.
    """
    from mini_ork import cn_client
    path = f"/api/v1/features?since={_url.quote(_SHIPPED_SINCE, safe='')}"
    raw = cn_client._get(path)
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _feature_path_resolves_inside(raw: Any, project_root: Path) -> bool:
    """Whether ``raw`` resolves to a path *contained in* ``project_root``.

    Relative paths are joined to the root; absolute paths (and ``~``-led
    paths) are taken as-is. Paths whose ``..`` components escape the root,
    or absolute paths that point outside the project, do NOT resolve inside.
    A missing or non-string ``raw`` returns ``False``. The file does NOT
    need to exist on disk — the filter is about path containment, which is
    cheap, deterministic, and never raises on a transient FS hiccup.
    """
    if not isinstance(raw, str) or not raw or project_root is None:
        return False
    try:
        root = project_root.resolve()
    except OSError:
        return False
    if raw.startswith("/") or raw.startswith("~"):
        candidate = Path(raw)
    else:
        candidate = project_root / raw
    try:
        resolved = candidate.resolve()
    except OSError:
        return False
    try:
        resolved.relative_to(root)
    except ValueError:
        return False
    return True


def _feature_files_inside(feature: dict[str, Any], project_root: Path) -> bool:
    """A feature's ``files[]`` has at least one entry whose path resolves
    inside the project root.

    Mirrors ``cn_session._opens_path`` for resolution semantics, minus the
    file-existence check (which the entry may not yet have — a feature
    shipped "since yesterday" may name a path the in-flight session created
    that the page's filesystem does not see).
    """
    if not isinstance(feature, dict):
        return False
    files_field = feature.get("files")
    if not isinstance(files_field, list):
        return False
    for entry in files_field:
        if isinstance(entry, str) and _feature_path_resolves_inside(entry, project_root):
            return True
        if isinstance(entry, dict):
            for v in entry.values():
                if isinstance(v, str) and _feature_path_resolves_inside(v, project_root):
                    return True
    return False


def _feature_files_list(feature: dict[str, Any]) -> list[str]:
    """The feature's ``files[]`` as a flat string list (handles ``{key: path}``
    dict entries the way ``cn_session._files`` does — paths in CN are
    sometimes raw strings, sometimes a single-key dict)."""
    out: list[str] = []
    files_field = feature.get("files")
    if not isinstance(files_field, list):
        return out
    for entry in files_field:
        if isinstance(entry, str):
            out.append(entry)
        elif isinstance(entry, dict):
            out.extend(str(v) for v in entry.values() if isinstance(v, str))
    return out


def _feature_ts(feature: dict[str, Any]) -> int:
    """Sort key (newest first); ``0`` sorts last when the field is absent."""
    ts = feature.get("ts")
    if isinstance(ts, (int, float)):
        return int(ts)
    if isinstance(ts, str):
        try:
            return int(float(ts))
        except (TypeError, ValueError):
            return 0
    return 0


def _shipped_today_section(home: Path) -> list[dict[str, Any]]:
    """The "Shipped since yesterday" list, with one row per in-project feature.

    A feature is included only when at least one of its ``files[]`` paths
    resolves inside ``home.parent.resolve()`` (the project root). Features
    with no in-project file are dropped — even when the session cwd
    matches the project — because the kickoff's explicit acceptance says
    "a feature is in-project only when one of its files lives in the
    project root".

    The section degrades to the one-line ``_cn.section_status`` state on
    down/disabled/config-error; the page never raises here and adds NO
    page chip (so the existing inbox renders unchanged on every degraded
    path, and ``test_ide_pages_inbox_kanban`` keeps passing without a CN
    stub). Each row carries:

    * feature name (read from ``feature``/``name``/``label`` — the verified
      2026-10-15 wire key is ``feature``; older payloads used ``name``);
    * the verbatim files list as the row sub cell;
    * an Open session link via ``S.page_link("cn_session", id=session_id)``;
    * a Re-test button that seeds a draft mini-ork agent thread with
      ``how_to_test`` verbatim as the first prompt — the page never runs
      the recipe.

    A feature without ``how_to_test`` still gets the Re-test button; the
    seed text just stays shorter (the same rule ``cn_session._features``
    follows).
    """
    from mini_ork.ide_pages import _cn as CN
    try:
        project_root = home.parent.resolve() if home else None
        if project_root is None:
            return [S.lst("Shipped since yesterday",
                          [S.dot("Could not read this", "project root unknown")],
                          full=True)]
        scope = str(project_root)
        payload = CN.render_cached_section(
            _SHIPPED_CACHE_SECTION, scope,
            lambda: _fetch_shipped_recent(),
        )
        if not isinstance(payload, dict):
            return [S.lst("Shipped since yesterday",
                          [S.dot("Could not read this", "ContextNest read failed")],
                          full=True)]
        if payload.get("_disabled") or payload.get("_unreachable") or payload.get("_config_error"):
            return [S.lst("Shipped since yesterday",
                          [CN.section_status(payload)], full=True)]

        features_field = payload.get("features")
        if not isinstance(features_field, list):
            features_field = []
        # Client-side scoping: a feature ships here only when one of its
        # files resolves inside the project root. Out-of-project features
        # (even ones whose session cwd matches the project) are dropped.
        in_project = [f for f in features_field
                      if isinstance(f, dict) and _feature_files_inside(f, project_root)]
        in_project.sort(key=_feature_ts, reverse=True)
        in_project = in_project[:_SHIPPED_ROW_LIMIT]
        if not in_project:
            return [S.lst("Shipped since yesterday",
                          [S.dot("No features shipped in the last 24h",
                                 "ContextNest returned no in-project features")],
                          full=True)]
        items: list[dict[str, Any]] = []
        for feat in in_project:
            name = (str(feat.get("feature") or feat.get("name") or feat.get("label") or "")
                    .strip())
            if not name:
                continue
            files = _feature_files_list(feat)
            files_text = ", ".join(files) if files else "(no files recorded)"
            how = str(feat.get("how_to_test") or "").strip()
            session_id = str(feat.get("session_id") or "").strip()
            seed = (
                f"Re-test the **{name}** feature.\n\nHow to test:\n\n{how}\n"
                if how
                else f"Re-test the **{name}** feature."
            )
            acts: list[dict[str, Any]] = []
            if session_id:
                acts.append(S.btn("Open session",
                                  S.page_link("cn_session", id=session_id), "ghost"))
            acts.append(S.btn("Re-test", S.thread(seed), "default"))
            items.append(S.dot(name, files_text,
                               acts=acts, mc="green"))
        if not items:
            return [S.lst("Shipped since yesterday",
                          [S.dot("No features shipped in the last 24h",
                                 "ContextNest returned no in-project features")],
                          full=True)]
        return [S.lst("Shipped since yesterday", items, full=True,
                      note="Features shipped in the last 24h, scoped to files "
                           "inside this project. Re-test opens a draft agent "
                           "thread with how_to_test as the first prompt — the "
                           "page never runs the recipe itself.")]
    except Exception:
        # Defensive: a render-time bug must NEVER blank the page. A
        # one-line dot section stands in so the operator can still see
        # the rest of the inbox unchanged.
        return [S.lst("Shipped since yesterday",
                      [S.dot("Could not read this", "shipped-recent read failed")],
                      full=True)]


def build(home: Path, tab: str | None, args: dict[str, str]) -> dict[str, Any]:
    now = int(time.time())
    show_all = str(args.get("show") or "").strip().lower() in ("all", "1", "true", "yes")
    errors: dict[str, str] = {}
    ctx: dict[str, Any] = {}
    sections = _awaiting_review_section(home, now) + S.guarded(
        errors, "Inbox",
        lambda: _inbox(home, now, errors, ctx, show_all))
    agent_sections, agent_now = _agent_sessions_section(home)
    sections = sections + agent_sections + _shipped_today_section(home)
    chips = [S.chip(f"✋ needs you {ctx.get('needs_you', 0)}", "yellow"),
             S.chip(f"✗ failed {ctx.get('failed', 0)}", "red")]
    if ctx.get("questions"):
        chips.append(S.chip(f"? {ctx['questions']} waiting for an answer", "yellow"))
    if ctx.get("repairing"):
        chips.append(S.chip(f"⟳ being repaired {ctx['repairing']}", "blue"))
    if ctx.get("superseded"):
        chips.append(S.chip(f"⤵ {ctx['superseded']} superseded", "sub"))
    if ctx.get("hidden") and not show_all:
        chips.append(S.chip(f"◌ {ctx['hidden']} filtered", "sub"))
    if agent_now:
        chips.append(S.chip(f"✋ now {agent_now}", "yellow"))
    return S.page("inbox", "Inbox", "What needs you first, then what failed.",
                  chips_=chips, sections=sections, errors=errors, args=args)
