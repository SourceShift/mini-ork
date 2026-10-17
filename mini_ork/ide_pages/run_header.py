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


def _menu(outcome: dict[str, Any], actions: list[dict[str, Any]], running: bool
          ) -> list[dict[str, Any]]:
    """The menu: the outcome's secondary actions, minus the drawn ones.

    A label that is already a header button is subtracted, so a caller that
    lists the same action in both places draws it once. "Certify this change"
    is dropped while the run is running.
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
    return out


def build_header(run: Any, outcome: dict[str, Any]) -> dict[str, Any]:
    """``{"label", "actions", "menu", "updated_at"}`` for a run page's header.

    ``outcome`` is the dict :func:`mini_ork.ide_pages.outcome.resolve` returns;
    a missing or malformed one costs the buttons, never the page. Every key is
    present in the result whatever the sources did — the caller passes them
    straight to :func:`spec.page`, which emits each only when non-empty.
    """
    outcome = outcome if isinstance(outcome, dict) else {}
    try:
        running = _is_running(run)
    except Exception:  # noqa: BLE001 — an unreadable card is not "running"
        running = False
    try:
        actions = _actions(outcome)
    except Exception:  # noqa: BLE001 — a broken outcome costs the buttons
        actions = []
    try:
        menu = _menu(outcome, actions, running)
    except Exception:  # noqa: BLE001 — a broken menu costs the menu
        menu = []
    try:
        label = _label(run)
    except Exception:  # noqa: BLE001 — a broken card costs the label
        label = str(getattr(run, "id", "") or "")
    return {
        "label": label,
        "actions": actions,
        "menu": menu,
        "updated_at": int(time.time()),
    }
