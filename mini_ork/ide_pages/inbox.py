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
"""
from __future__ import annotations

import json
import time
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


def build(home: Path, tab: str | None, args: dict[str, str]) -> dict[str, Any]:
    now = int(time.time())
    show_all = str(args.get("show") or "").strip().lower() in ("all", "1", "true", "yes")
    errors: dict[str, str] = {}
    ctx: dict[str, Any] = {}
    sections = S.guarded(errors, "Inbox",
                         lambda: _inbox(home, now, errors, ctx, show_all))
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
    return S.page("inbox", "Inbox", "What needs you first, then what failed.",
                  chips_=chips, sections=sections, errors=errors, args=args)
