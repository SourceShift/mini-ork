"""The page contract between ``mini-ork board page`` and the mini-ork IDE.

A page is plain JSON. The IDE (a Zed build) has one generic renderer for it, so
a page changes without rebuilding the IDE. Every helper here returns a dict;
page modules compose them and never hand-write the shapes.

Page::

    {"ok": true, "key": "runs", "title": "...", "sub": "...",
     "chips":   [chip],            # small mono tags next to the title
     "actions": [button],          # buttons on the right of the title
     "tabs":    [{"key", "label"}], "tab": "<current tab key>",
     "args":    {...},             # the page arguments it was built with
     "sections": [section],
     "errors":  {"<section title>": "<error>"}}

Sections (``type``) — each takes ``title``, ``note``, ``actions`` and a layout:
``full`` (spans both columns), or ``col`` 1|2 (+ ``row_span`` to span rows in
the left column, for a list/detail layout); with neither, sections flow two to
a row.

- ``table``  ``cols`` (widths, see :func:`col`), ``head``, ``rows`` of
  ``{"cells": [cell], "do": action|None, "sel": bool}``
- ``kv``     ``items`` of ``{"k", "v", "c", "sub"}`` — stat tiles
- ``list``   ``items`` of ``{"m", "mc", "t", "tc", "sub", "mono", "acts"}``
- ``bars``   ``items`` of ``{"label", "pct", "val", "c"}``
- ``flow``   ``items`` of ``{"label", "sub", "c"}`` joined by arrows
- ``code``   ``lines`` of ``{"t", "c"}`` — monospace
- ``pills``  ``items`` of ``{"t", "c"}``
- ``chips``  ``items`` of ``{"t", "on", "do"}`` — filter chips
- ``dag``    ``cols`` of node lists (see :func:`dag`) + ``legend``
- ``inspector`` one node's output lines and key/values (see :func:`inspector`)
- ``markdown`` ``text`` (raw markdown body) + ``path`` (absolute file path)

Colours are names, never hex: ``text body muted sub dim blue green red yellow
purple cyan orange`` or a lane family ``fam:<lane>`` (``fam:sonnet``). The IDE
maps them onto its theme.

Actions (``do``) — what a button, row or chip does in the IDE:

- ``{"cli": [...args]}``           run ``mini-ork <args> --home <home>`` then refresh
- ``{"page": key, "tab": t, "args": {...}}``  open (or switch) a page tab
- ``{"set": {...}}``               re-fetch this page with these args merged
- ``{"run": run_id, "title": t, "tab": t}``    open a run's tab
- ``{"path": abs}`` / ``{"reveal": abs}`` / ``{"url": u}``
- ``{"thread": text}``             new mini-ork thread, ``text`` as its first prompt
- ``{"project": abs}``             switch the IDE window to the project at ``abs``
- ``{"project_pick": true}``       pick a project folder, then switch to it
Any action may carry ``"confirm": "<question>"``.
"""
from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Any

COLOURS = ("text", "body", "muted", "sub", "dim", "blue", "green", "red", "yellow",
           "purple", "cyan", "orange")

# Button kinds, as drawn in the design.
KINDS = ("primary", "default", "danger", "warn", "ghost")


# ── actions ────────────────────────────────────────────────────────────────

def cli(*args: str, confirm: str | None = None, home: bool = True) -> dict[str, Any]:
    """An action that runs a mini-ork subcommand.

    ``home=False`` tells the IDE not to append ``--home``: the subcommand reads
    ``MINI_ORK_HOME`` from the environment instead. Use it for subcommands whose
    argparse rejects ``--home`` (``nodes ping|doctor``, ``sandbox-gc``,
    ``bugs sweep|promote``, ``traceotter``, ``usage-report``)."""
    out: dict[str, Any] = {"cli": [str(a) for a in args]}
    if not home:
        out["home"] = False
    return _with_confirm(out, confirm)


def page_link(key: str, tab: str | None = None, **args: Any) -> dict[str, Any]:
    out: dict[str, Any] = {"page": key}
    if tab:
        out["tab"] = tab
    if args:
        out["args"] = {k: v for k, v in args.items() if v is not None}
    return out


def set_args(**args: Any) -> dict[str, Any]:
    return {"set": args}


def open_run(run_id: str, title: str = "", tab: str | None = None) -> dict[str, Any]:
    out: dict[str, Any] = {"run": run_id, "title": title}
    if tab:
        out["tab"] = tab
    return out


def open_path(path: str) -> dict[str, Any]:
    return {"path": str(path)}


def reveal(path: str) -> dict[str, Any]:
    return {"reveal": str(path)}


def project(path: str) -> dict[str, Any]:
    """Switch the IDE window to the mini-ork project rooted at ``path``."""
    return {"project": path}


def project_pick() -> dict[str, Any]:
    """Ask for a project folder, then switch the IDE window to it."""
    return {"project_pick": True}


def url(u: str) -> dict[str, Any]:
    return {"url": u}


def thread(text: str = "") -> dict[str, Any]:
    return {"thread": text}


def _with_confirm(action: dict[str, Any], confirm: str | None) -> dict[str, Any]:
    if confirm:
        action["confirm"] = confirm
    return action


def btn(label: str, do: dict[str, Any] | None = None, kind: str = "default") -> dict[str, Any]:
    """A button. ``do=None`` draws it disabled (the design's no-op buttons)."""
    return {"label": label, "kind": kind if kind in KINDS else "default", "do": do}


# ── cells, chips, list items ───────────────────────────────────────────────

def cell(t: Any, c: str | None = None, *, mono: bool = False, b: bool = False) -> dict[str, Any]:
    return {"t": "" if t is None else str(t), "c": c or "body", "mono": mono, "b": b}


def mono(t: Any, c: str | None = None) -> dict[str, Any]:
    return cell(t, c, mono=True)


def muted(t: Any) -> dict[str, Any]:
    return cell(t, "sub")


def chip(t: Any, c: str = "sub") -> dict[str, Any]:
    return {"t": str(t), "c": c}


def item(t: Any, sub: Any = "", *, m: str = "•", mc: str = "sub", tc: str = "text",
         is_mono: bool = False, acts: Iterable[dict[str, Any]] | None = None) -> dict[str, Any]:
    return {"m": m, "mc": mc, "t": str(t), "tc": tc, "sub": "" if sub is None else str(sub),
            "mono": is_mono, "acts": list(acts or [])}


def ok(t: Any, sub: Any = "", acts: Iterable[dict[str, Any]] | None = None) -> dict[str, Any]:
    return item(t, sub, m="✓", mc="green", acts=acts)


def warn(t: Any, sub: Any = "", acts: Iterable[dict[str, Any]] | None = None) -> dict[str, Any]:
    return item(t, sub, m="!", mc="yellow", acts=acts)


def bad(t: Any, sub: Any = "", acts: Iterable[dict[str, Any]] | None = None) -> dict[str, Any]:
    return item(t, sub, m="✗", mc="red", acts=acts)


def dot(t: Any, sub: Any = "", acts: Iterable[dict[str, Any]] | None = None,
        mc: str = "sub") -> dict[str, Any]:
    return item(t, sub, m="•", mc=mc, acts=acts)


# ── sections ───────────────────────────────────────────────────────────────

def _section(kind: str, title: str, data: dict[str, Any], *, note: str = "",
             actions: Iterable[dict[str, Any]] | None = None, full: bool = False,
             col: int | None = None, row_span: int | None = None) -> dict[str, Any]:
    out = {"type": kind, "title": title or "", "note": note or "",
           "actions": list(actions or []), "full": full, **data}
    if col in (1, 2):
        out["col"] = col
    if row_span:
        out["row_span"] = row_span
    return out


def col(width: int | None = None, *, fr: float = 1.0, min: int = 0) -> dict[str, Any]:
    """A table column: fixed ``width`` px, or a flexible share ``fr`` with a ``min`` px."""
    return {"w": width} if width is not None else {"fr": fr, "min": min}


def table(title: str, cols: list[dict[str, Any]], head: list[str],
          rows: Iterable[Any], **opt: Any) -> dict[str, Any]:
    """``rows``: lists of cells (str or :func:`cell`), or ``{"cells", "do", "sel"}``."""
    out_rows = []
    for r in rows:
        r = r if isinstance(r, dict) else {"cells": r}
        cells = [c if isinstance(c, dict) else cell(c) for c in r.get("cells") or []]
        out_rows.append({"cells": cells, "do": r.get("do"), "sel": bool(r.get("sel"))})
    return _section("table", title, {"cols": cols, "head": head, "rows": out_rows}, **opt)


def kv(title: str, items: Iterable[tuple | list], **opt: Any) -> dict[str, Any]:
    """``items``: ``(k, v[, colour[, sub]])``."""
    out = []
    for i in items:
        i = list(i) + [None, None]
        out.append({"k": str(i[0]), "v": "" if i[1] is None else str(i[1]),
                    "c": i[2] or "text", "sub": "" if i[3] is None else str(i[3])})
    return _section("kv", title, {"items": out}, **opt)


def lst(title: str, items: Iterable[dict[str, Any]], **opt: Any) -> dict[str, Any]:
    return _section("list", title, {"items": list(items)}, **opt)


def bars(title: str, items: Iterable[tuple | list], **opt: Any) -> dict[str, Any]:
    """``items``: ``(label, pct 0-100, value text[, colour])``."""
    out = []
    for i in items:
        i = list(i) + [None]
        pct = max(0.0, min(100.0, float(i[1] or 0)))
        out.append({"label": str(i[0]), "pct": round(pct, 1), "val": str(i[2]), "c": i[3] or "blue"})
    return _section("bars", title, {"items": out}, **opt)


def flow(title: str, items: Iterable[tuple | list | str], **opt: Any) -> dict[str, Any]:
    """``items``: ``label`` or ``(label[, sub[, colour]])``."""
    out = []
    for i in items:
        i = [i] if isinstance(i, str) else list(i)
        i += [None, None]
        out.append({"label": str(i[0]), "sub": i[1] or "", "c": i[2] or "text"})
    return _section("flow", title, {"items": out}, **opt)


def code(title: str, lines: Iterable[tuple | list | str], **opt: Any) -> dict[str, Any]:
    out = []
    for line in lines:
        if isinstance(line, str):
            out.append({"t": line, "c": "body"})
        else:
            out.append({"t": str(line[0]), "c": line[1] if len(line) > 1 else "body"})
    return _section("code", title, {"lines": out}, **opt)


def markdown(title: str, text: str, path: str | None = None, **opt: Any) -> dict[str, Any]:
    """A full markdown body — the run's kickoff, rendered raw by the IDE.

    ``path`` is the absolute file the text was loaded from (``None`` when the
    text came from memory or the run had no kickoff).
    """
    return _section("markdown", title, {"text": text or "", "path": path or ""}, **opt)


def pills(title: str, items: Iterable[tuple | list | str], **opt: Any) -> dict[str, Any]:
    out = []
    for i in items:
        i = [i] if isinstance(i, str) else list(i)
        out.append({"t": str(i[0]), "c": i[1] if len(i) > 1 else "muted"})
    return _section("pills", title, {"items": out}, **opt)


def chips(title: str, items: Iterable[dict[str, Any]], **opt: Any) -> dict[str, Any]:
    """``items``: ``{"t", "on", "do"}``."""
    return _section("chips", title, {"items": [
        {"t": str(i.get("t")), "on": bool(i.get("on")), "do": i.get("do")} for i in items]}, **opt)


def dag_node(node_id: str, label: str, lane: str, state: str, *, cost: str = "",
             selected: bool = False, do: dict[str, Any] | None = None) -> dict[str, Any]:
    """``state``: pending | running | done | failed | skipped."""
    return {"id": node_id, "label": label, "lane": lane, "state": state, "cost": cost,
            "sel": selected, "do": do}


def dag(title: str, cols: list[list[dict[str, Any]]], legend: str = "", **opt: Any) -> dict[str, Any]:
    return _section("dag", title, {"cols": cols, "legend": legend}, **opt)


def inspector(label: str, role: str, state: str, lines: Iterable[tuple | list | str],
              items: Iterable[tuple | list], *, note: str = "",
              steer: dict[str, Any] | None = None, **opt: Any) -> dict[str, Any]:
    """One DAG node: its output ``lines`` and its key/values (``(k, v[, colour])``).

    ``steer`` — ``{"run": id, "node": id, "enabled": bool, "note": str}`` draws
    the "Steer this worker" input (``mini-ork steer``).
    """
    sec = code("", lines)
    kvs = kv("", items)
    return _section("inspector", "", {"label": label, "role": role, "state": state,
                                      "lines": sec["lines"], "items": kvs["items"],
                                      "steer": steer, "steer_note": note}, **opt)


# ── page ───────────────────────────────────────────────────────────────────

def page(key: str, title: str, sub: str = "", *, chips_: Iterable[dict[str, Any]] = (),
         actions: Iterable[dict[str, Any]] = (), tabs: Iterable[tuple[str, str]] = (),
         tab: str | None = None, args: dict[str, Any] | None = None,
         sections: Iterable[dict[str, Any]] = (), errors: dict[str, str] | None = None) -> dict[str, Any]:
    tab_list = [{"key": k, "label": label} for k, label in tabs]
    return {"ok": True, "key": key, "title": title, "sub": sub, "chips": list(chips_),
            "actions": list(actions), "tabs": tab_list,
            "tab": tab or (tab_list[0]["key"] if tab_list else ""), "args": dict(args or {}),
            "sections": list(sections), "errors": dict(errors or {})}


def guarded(errors: dict[str, str], title: str, build: Callable[[], dict[str, Any] | list[dict[str, Any]]]
            ) -> list[dict[str, Any]]:
    """Build one section (or several); on failure a list item says what broke.

    A broken data source must cost one section, never the page.
    """
    try:
        out = build()
    except Exception as exc:  # noqa: BLE001 — one broken source must not blank the page
        errors[title] = f"{type(exc).__name__}: {exc}"
        return [lst(title, [bad("Could not read this", f"{type(exc).__name__}: {exc}")], full=True)]
    return out if isinstance(out, list) else [out]


# ── formatting ─────────────────────────────────────────────────────────────

def money(v: Any) -> str:
    try:
        return f"${float(v or 0):.2f}"
    except (TypeError, ValueError):
        return "—"


def age(epoch: Any, now: int) -> str:
    try:
        secs = max(0, int(now) - int(epoch))
    except (TypeError, ValueError):
        return "—"
    if secs < 60:
        return "now"
    if secs < 3600:
        return f"{secs // 60}m"
    if secs < 86400:
        return f"{secs // 3600}h"
    return f"{secs // 86400}d"


def duration(secs: Any) -> str:
    try:
        s = max(0, int(secs))
    except (TypeError, ValueError):
        return "—"
    if s < 3600:
        return f"{s // 60}m {s % 60:02d}s"
    return f"{s // 3600}h {(s % 3600) // 60:02d}m"


# The run states the IDE draws: mark and colour.
STATE_MARK = {"working": ("●", "blue"), "needs_you": ("✋", "yellow"), "failed": ("✗", "red"),
              "done": ("✓", "green"), "queued": ("○", "sub")}
