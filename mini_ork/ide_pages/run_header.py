"""The run page's header: the tab label and the run's controls, on every tab.

Before this module the run's controls lived inside the Story tab's triage, so
on the Graph, Agents or Kickoff tab a user could not stop a runaway run — and
the IDE tab was labelled with the run id, because the page carried no short
label. ``build_header`` folds the outcome the page already resolved into the
four header fields the contract names (``label``, ``actions``, ``menu``,
``updated_at``); ``run._build_v2`` passes them to :func:`spec.page` and blanks
the triage's own buttons, so the two never draw the same control twice.

Like every other page builder here, each part absorbs its own failure: a
broken card costs the label, a broken outcome costs the buttons — never the
page. No model, no network, no DB beyond what the caller already read.
"""
from __future__ import annotations

import time
from typing import Any

from mini_ork.ide_pages import spec as S

# The tab label is a layout contract: the IDE draws it in a fixed-width tab.
LABEL_CHARS = 40

# Offered only from the Verify page while the run is in flight — a change that
# is still being written cannot be certified. Removed from the header's controls
# in every state, and from the menu while the run is running.
CERTIFY = "Certify this change"

_TERMINAL = ("published", "failed", "rolled_back")


def _is_running(run: Any) -> bool:
    """The run is in flight — its status is not one of the three terminal ones.

    The same rule ``outcome.is_running`` and ``run._running`` apply, re-stated
    here because this module reads a stub ``Run`` too (the blind spec tests).
    """
    try:
        return str(run.card.get("status") or "") not in _TERMINAL
    except Exception:  # noqa: BLE001 — an unreadable card is not "running"
        return False


def _clip(text: str, limit: int = LABEL_CHARS) -> str:
    """``text`` cut at the last word boundary before ``limit``, ellipsis included.

    The same rule :func:`mini_ork.ide_pages.run._clip` applies to the header
    description, so a title cut for the tab reads exactly like a title cut
    anywhere else on the page.
    """
    text = " ".join(str(text or "").split())
    if len(text) <= limit:
        return text
    cut = text.rfind(" ", 0, limit)
    return f"{text[: cut if cut > 0 else limit].rstrip()}…"


def _label(run: Any) -> str:
    """The tab label: ``_title_v2(run)`` cut to 40 chars, else the run id.

    ``_title_v2`` already falls back to the run id when the run has neither a
    title nor a goal — that fallback is returned whole rather than clipped, so
    the tab is never labelled with a truncated id (an id that lost its tail
    identifies no run at all). Imported lazily: ``run.py`` imports this module
    at top level, so a top-level import back would close the cycle.
    """
    try:
        from mini_ork.ide_pages.run import _title_v2

        title = str(_title_v2(run) or "").strip()
    except Exception:  # noqa: BLE001 — a broken card costs the label, never the page
        title = ""
    run_id = str(getattr(run, "id", "") or "")
    if not title or title == run_id:
        return run_id
    return _clip(title, LABEL_CHARS)


def _actions(outcome: dict[str, Any]) -> list[dict[str, Any]]:
    """The header's buttons: the triage's actions for this state, at most two.

    "Certify this change" is never one of them — certification is a judgement
    made from the Verify page, and a header that offered it on a run in flight
    (or before the operator has read the diff) is the bug this header exists to
    fix. The first button is the run's next action, which is why running runs
    get Stop then Kill and a finished run with a worktree gets Merge first.
    """
    out = [a for a in (outcome.get("actions") or [])
           if isinstance(a, dict) and a.get("label") != CERTIFY]
    return out[:2]


def _menu(run: Any, outcome: dict[str, Any], actions: list[dict[str, Any]],
          running: bool, with_summary: bool) -> list[dict[str, Any]]:
    """The menu: the outcome's secondary actions, minus the drawn ones.

    A label that is already a header button is subtracted, so a caller that
    lists the same action in both places draws it once. "Certify this change"
    is dropped while the run is running.

    "Copy summary" is offered once the outcome is settled; ``_build_v2`` adds
    the same entry for a parked (``needs_you``) run at the page level, so the
    rendered menu carries it on every tab. The entry is built here rather than
    in ``outcome`` because the markdown needs the run's own records (levels,
    diff, cost) as well.
    """
    drawn = {a.get("label") for a in actions}
    out: list[dict[str, Any]] = []
    for entry in outcome.get("menu") or []:
        if not isinstance(entry, dict):
            continue
        label = entry.get("label")
        if label in drawn:
            continue
        if running and label == CERTIFY:
            continue
        out.append(entry)
    settled = str(outcome.get("state") or "") not in ("running", "needs_you")
    if (with_summary and settled
            and not any(m.get("label") == "Copy summary" for m in out)):
        out.append(copy_summary_action(run, outcome))
    return out


def copy_summary_action(run: Any, outcome: dict[str, Any]) -> dict[str, Any]:
    """The ``Copy summary`` menu entry: clipboard action, toast included.

    A menu row rather than a header button — the rendered page draws it behind
    the header's ▾, never beside the run's top-two controls.
    """
    return S.btn("Copy summary", S.copy(_summary(run, outcome), done="Summary copied"),
                 "ghost")


def _card_value(run: Any, keys: tuple[str, ...]) -> str:
    """The first non-empty value for ``keys`` on the card, then the DB row.

    Every source is optional — a card in this tree may name an epic, an attempt
    or a recovery, or none of them. A dict value (an epic object rather than an
    id) yields its ``id``/``run_id`` when it carries one. An unreadable source
    yields ``""``, so the caller omits the crumb rather than inventing one.
    """
    for attr in ("card", "row"):
        try:
            source = getattr(run, attr, None)
        except Exception:  # noqa: BLE001 — an unreadable card has no relations
            continue
        if not isinstance(source, dict):
            continue
        for key in keys:
            try:
                value = source.get(key)
            except Exception:  # noqa: BLE001
                continue
            if isinstance(value, dict):
                value = value.get("id") or value.get("run_id") or ""
            if value not in (None, ""):
                text = str(value).strip()
                if text:
                    return text
    return ""


def _attempt(run: Any) -> int | None:
    """The card's attempt number when it carries one — ``None`` otherwise."""
    raw = _card_value(run, ("attempt", "attempt_number", "attempts"))
    try:
        number = int(raw) if raw else 0
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _crumbs(run: Any) -> list[dict[str, Any]]:
    """``[{t, do, mono}]`` — where the run sits, per CONTRACT v2.1.

    ``epic › run › retry #N``, with each part emitted only when the run's
    records actually carry it: a root run yields ``[]`` (the key is then
    omitted), never a fabricated crumb. A crumb that can navigate carries a
    ``do``; one with nowhere to go (the run itself, a retry whose origin is not
    recorded) carries ``do: None``.
    """
    run_id = str(getattr(run, "id", "") or "")
    parent = _card_value(run, ("epic_id", "epic", "parent_run_id", "parent_run"))
    attempt = _attempt(run)
    recovered = _card_value(run, ("recovered_from", "recovered_from_id"))
    if not parent and attempt is None and not recovered:
        return []
    out: list[dict[str, Any]] = []
    if parent:
        out.append({"t": parent, "do": S.page_link("run", run=parent), "mono": False})
    out.append({"t": run_id, "do": None, "mono": True})
    if attempt is not None or recovered:
        do = S.page_link("run", run=recovered) if recovered else None
        out.append({"t": f"retry #{attempt or 1}", "do": do, "mono": False})
    return out


def _epoch(value: Any) -> int | None:
    try:
        return None if value in (None, "") else int(float(value))
    except (TypeError, ValueError):
        return None


def _goal(run: Any) -> str:
    """The kickoff title ``_title_v2`` resolves — never the run id.

    The same reader the tab label uses: the card title, else the operator's
    goal from ``run_profile.json``. A broken card costs the goal line.
    """
    try:
        from mini_ork.ide_pages.run import _title_v2

        title = str(_title_v2(run) or "").strip()
    except Exception:  # noqa: BLE001
        title = ""
    return "" if title == str(getattr(run, "id", "") or "") else title


def _level_lines(run: Any) -> list[str]:
    """The verification vector as one line per level, in the report's order."""
    try:
        from mini_ork.ide_pages import outcome as O

        report = O.verification_report(run)
    except Exception:  # noqa: BLE001 — a broken report costs its lines
        return []
    levels = report.get("levels") if isinstance(report, dict) else None
    if not isinstance(levels, dict):
        return []
    return [f"{name}: {value}" for name, value in levels.items()
            if str(value or "").strip()]


def _diff_stat(run: Any) -> str:
    """``N files, +A −R`` from the card's changed-file rows, else ``""``."""
    try:
        files = run.card.get("files")
    except Exception:  # noqa: BLE001
        return ""
    if not isinstance(files, (list, tuple)) or not files:
        return ""

    def count(row: dict[str, Any], key: str) -> int:
        try:
            return int(row.get(key) or 0)
        except (TypeError, ValueError):
            return 0

    rows = [f for f in files if isinstance(f, dict)]
    n = len(rows)
    added = sum(count(f, "added") for f in rows)
    removed = sum(count(f, "removed") for f in rows)
    return f"{n} file{'s' if n != 1 else ''}, +{added} −{removed}"


def _cost(run: Any) -> float:
    """The card's rollup, else the DB row's cost — ``0.0`` when neither reads."""
    try:
        total = float(run.card.get("cost_total") or 0.0)
    except Exception:  # noqa: BLE001
        total = 0.0
    if total > 0:
        return total
    try:
        return float(run.row.get("cost_usd") or 0.0)
    except Exception:  # noqa: BLE001
        return 0.0


def _wall(run: Any) -> str:
    """The run's own duration, from its row; ``""`` while it has no end."""
    try:
        row = run.row
    except Exception:  # noqa: BLE001
        return ""
    if not isinstance(row, dict):
        return ""
    try:
        start = _epoch(row.get("created_at"))
        end = _epoch(row.get("ended_at")) or _epoch(row.get("updated_at"))
    except Exception:  # noqa: BLE001 — an unreadable row costs the duration
        return ""
    if start is None or end is None:
        return ""
    return S.duration(max(0, end - start))


def _link(run: Any) -> str:
    """The card's run page URL when it carries one, else ``mini-ork://run/<id>``."""
    for key in ("url", "page_url"):
        try:
            value = run.card.get(key)
        except Exception:  # noqa: BLE001
            value = None
        if value:
            return str(value)
    run_id = str(getattr(run, "id", "") or "")
    return f"mini-ork://run/{run_id}" if run_id else ""


def _summary(run: Any, outcome: dict[str, Any]) -> str:
    """The pasteable summary: goal, outcome, vector, diff, cost, link.

    Build from records the page already read. Every part absorbs its own
    failure: a broken vector or a missing diff is an omitted line, never an
    exception — the goal and the link keep the summary non-empty.
    """
    lines: list[str] = []
    goal = _goal(run)
    if goal:
        lines.append(f"**{goal}**")
    text = str(outcome.get("text") or "").strip() if isinstance(outcome, dict) else ""
    if text:
        lines.append(text)
    bullets = list(_level_lines(run))
    diff = _diff_stat(run)
    if diff:
        bullets.append(diff)
    cost, wall = _cost(run), _wall(run)
    money = S.money(cost) if cost > 0 else ""
    if money or wall:
        bullets.append(" · ".join(p for p in (f"cost {money}" if money else "", wall) if p))
    link = _link(run)
    if link:
        bullets.append(link)
    if bullets:
        if lines:
            lines.append("")
        lines.extend(f"- {b}" for b in bullets)
    return "\n".join(lines).strip()


def build_header(run: Any, outcome: dict[str, Any]) -> dict[str, Any]:
    """``{"label", "actions", "menu", "updated_at"}`` for a run page's header.

    ``outcome`` is the dict :func:`mini_ork.ide_pages.outcome.resolve` returns;
    a missing or malformed one costs the buttons, never the page. The four
    contract-v2.1 keys are always present whatever the sources did, and
    ``crumbs`` rides alongside them only when the run's records place it — an
    absent key and an empty list both read as "a root run". The caller passes
    them straight to :func:`spec.page`, which emits each only when non-empty.
    """
    # A resolved outcome (even an empty dict from a caller that passed no
    # fields) can carry the Copy-summary offer; ``None`` means the page could
    # not resolve one at all, and then the menu stays exactly the old one.
    has_outcome = isinstance(outcome, dict)
    outcome = outcome if has_outcome else {}
    try:
        running = _is_running(run)
    except Exception:  # noqa: BLE001 — an unreadable card is not "running"
        running = False
    try:
        actions = _actions(outcome)
    except Exception:  # noqa: BLE001 — a broken outcome costs the buttons
        actions = []
    try:
        menu = _menu(run, outcome, actions, running, has_outcome)
    except Exception:  # noqa: BLE001 — a broken menu costs the menu
        menu = []
    try:
        label = _label(run)
    except Exception:  # noqa: BLE001 — a broken card costs the label
        label = str(getattr(run, "id", "") or "")
    try:
        crumbs = _crumbs(run)
    except Exception:  # noqa: BLE001 — a broken card costs its crumbs
        crumbs = []
    out = {
        "label": label,
        "actions": actions,
        "menu": menu,
        "updated_at": int(time.time()),
    }
    # Additive and absent when empty: a root run has no place to point, so the
    # key stays out and every existing four-field reader is unchanged.
    if crumbs:
        out["crumbs"] = crumbs
    return out
