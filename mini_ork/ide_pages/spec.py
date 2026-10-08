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
- ``hero``   ``goal``, ``criteria``, ``pill``, ``meta`` (see :func:`hero`)
- ``triage`` ``text``, ``tone``, ``icon``, ``detail``, ``counts``, ``menu``
- ``callout`` ``text_md``, ``tone``; actions via the section's ``actions``
- ``story``  ``steps`` of step dicts (see :func:`story_step`)
- ``files``  ``files``, ``diff``, ``diff_note``, ``commits`` (see :func:`files`)
- ``findings`` ``verdict``, ``reasons``, ``items`` (see :func:`findings`)
- ``checks`` ``rows`` + ``summary`` (``passing``/``failing``/``pending``/``na``)
- ``agents`` ``rows`` of agent dicts (see :func:`agent_row`)
- ``composer`` ``placeholder`` + ``cli`` — the IDE appends the typed text
- ``columns`` ``cols`` of column dicts (see :func:`column`)

Colours are names, never hex: ``text body muted sub dim blue green red yellow
purple cyan orange`` or a lane family ``fam:<lane>`` (``fam:sonnet``). The IDE
maps them onto its theme. State words are ``done running failed skipped pending
needs_you``.

:func:`ide_level` reads ``MINI_ORK_IDE_SPEC`` on every call (default ``1``): an
IDE that sets ``MINI_ORK_IDE_SPEC=2`` gets the section types above; an older IDE
(env unset) keeps getting today's pages.

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

import os
from collections.abc import Callable, Iterable
from typing import Any

COLOURS = ("text", "body", "muted", "sub", "dim", "blue", "green", "red", "yellow",
           "purple", "cyan", "orange")

# Button kinds, as drawn in the design.
KINDS = ("primary", "default", "danger", "warn", "ghost")


# ── spec level ─────────────────────────────────────────────────────────────

def ide_level() -> int:
    """The IDE spec level the caller asked for, from ``MINI_ORK_IDE_SPEC``.

    The new IDE sets ``MINI_ORK_IDE_SPEC=2`` on every page build; builders use
    this to decide whether to emit the v2 section types. Read on each call —
    never cached — so a long-lived process sees a new value. Anything missing,
    non-numeric or below ``1`` is level ``1`` (today's pages).
    """
    raw = os.environ.get("MINI_ORK_IDE_SPEC")
    try:
        level = int(raw) if raw is not None else 1
    except (TypeError, ValueError):
        return 1
    return level if level >= 1 else 1


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


# ── v2 sections (MINI_ORK_IDE_SPEC=2) ──────────────────────────────────────
#
# Every helper below funnels through :func:`_section`, so ``note`` / ``actions``
# / ``full`` / ``col`` / ``row_span`` keep working and colours stay the COLOURS
# names. States are the words ``done running failed skipped pending needs_you``.

def _tc(v: Any, c: str = "sub") -> dict[str, Any] | None:
    """``None`` | ``str`` | ``(t, c)`` → ``{"t", "c"}`` (``None`` stays ``None``)."""
    if v is None:
        return None
    if isinstance(v, (tuple, list)):
        return {"t": str(v[0]) if v else "", "c": str(v[1]) if len(v) > 1 else c}
    return {"t": str(v), "c": c}


def _lines(lines: Iterable[Any]) -> list[dict[str, Any]]:
    """``str`` | ``(t, c)`` rows → ``{"t", "c"}`` items (default colour ``body``)."""
    out: list[dict[str, Any]] = []
    for line in lines:
        if isinstance(line, str):
            out.append({"t": line, "c": "body"})
        else:
            row = list(line)
            out.append({"t": str(row[0]), "c": row[1] if len(row) > 1 else "body"})
    return out


def meta_item(t: Any, c: str = "sub", *, mono: bool = False) -> dict[str, Any]:
    """One ``hero`` meta chip: ``{"t", "c", "mono"}``."""
    return {"t": str(t), "c": c, "mono": mono}


def hero(title: str, *, goal: str = "", criteria: Iterable[Any] = (), pill: Any = None,
         meta: Iterable[Any] = (), **opt: Any) -> dict[str, Any]:
    """A run's headline block. ``pill`` is ``(t, c)`` | ``str`` | ``None``."""
    opt.setdefault("full", True)
    return _section("hero", title, {
        "goal": str(goal),
        "criteria": [str(c) for c in criteria],
        "pill": _tc(pill),
        "meta": list(meta),
    }, **opt)


def triage(text: str, *, tone: str = "muted", icon: str = "", detail: str = "",
           counts: Iterable[Any] = (), actions: Iterable[dict[str, Any]] = (),
           menu: Iterable[dict[str, Any]] = (), **opt: Any) -> dict[str, Any]:
    """The "what needs you" strip. ``counts``: ``(t, c)`` or ``{"t", "c"}``."""
    opt.setdefault("full", True)
    out_counts = [c if isinstance(c, dict) else _tc(c) for c in counts]
    return _section("triage", "", {
        "text": str(text),
        "tone": tone,
        "icon": icon,
        "detail": str(detail),
        "counts": out_counts,
        "menu": list(menu),
    }, actions=actions, **opt)


def callout(title: str, text_md: str = "", *, tone: str = "orange",
            actions: Iterable[dict[str, Any]] = (), **opt: Any) -> dict[str, Any]:
    """A short markdown note; buttons ride the section's ``actions`` field."""
    return _section("callout", title, {"text_md": str(text_md), "tone": tone},
                    actions=actions, **opt)


def story_step(step_id: str, title: str, *, kind: str = "", state: str = "pending",
               lane: str = "", model: str = "", headline: Any = None,
               meta: Iterable[Any] = (), dur: str = "", cost: str = "", open: bool = False,
               do: dict[str, Any] | None = None, body: Iterable[Any] = ()) -> dict[str, Any]:
    """One step of a :func:`story`. ``headline`` is ``(t, c)`` or ``str``."""
    return {"id": str(step_id), "title": str(title), "kind": str(kind), "state": state,
            "lane": str(lane), "model": str(model), "headline": _tc(headline),
            "meta": list(meta), "dur": str(dur), "cost": str(cost), "open": bool(open),
            "do": do, "body": list(body)}


def block_md(text: str) -> dict[str, Any]:
    return {"kind": "md", "text": str(text)}


def block_lines(lines: Iterable[Any]) -> dict[str, Any]:
    """``str`` | ``(t, c)`` lines, like :func:`code`."""
    return {"kind": "lines", "lines": _lines(lines)}


def block_files(files: Iterable[Any], *, diff: str = "", diff_note: str = "",
                commits: Iterable[Any] = ()) -> dict[str, Any]:
    return {"kind": "files", "files": list(files), "diff": str(diff),
            "diff_note": str(diff_note), "commits": list(commits)}


def block_findings(items: Iterable[Any], *, verdict: Any = None,
                   reasons: Iterable[Any] = ()) -> dict[str, Any]:
    return {"kind": "findings", "items": list(items), "verdict": _tc(verdict),
            "reasons": [str(r) for r in reasons]}


def block_checks(rows: Iterable[Any], *, summary: dict[str, Any] | None = None) -> dict[str, Any]:
    rows = list(rows)
    return {"kind": "checks", "rows": rows,
            "summary": summary if summary is not None else _checks_summary(rows)}


def story(title: str, steps: Iterable[Any], **opt: Any) -> dict[str, Any]:
    """A vertical run story; ``steps`` come from :func:`story_step`."""
    return _section("story", title, {"steps": list(steps)}, **opt)


def file_entry(path: str, *, abs: str = "", status: str = "M", added: int = 0,
               removed: int = 0) -> dict[str, Any]:
    """One changed file for :func:`files` / :func:`block_files`."""
    return {"path": str(path), "abs": str(abs), "status": status,
            "added": int(added or 0), "removed": int(removed or 0)}


def files(title: str, files: Iterable[Any], *, diff: str = "", diff_note: str = "",
          commits: Iterable[Any] = (), **opt: Any) -> dict[str, Any]:
    """Changed files + the cumulative diff; entries come from :func:`file_entry`."""
    return _section("files", title, {"files": list(files), "diff": str(diff),
                                     "diff_note": str(diff_note), "commits": list(commits)}, **opt)


def finding(issue: str, *, severity: str = "", file: str = "", line: Any = None,
            abs: str = "", snippet: str = "", source: str = "") -> dict[str, Any]:
    """One review finding row for :func:`findings` / :func:`block_findings`."""
    return {"issue": str(issue), "severity": str(severity), "file": str(file),
            "line": line, "abs": str(abs), "snippet": str(snippet), "source": str(source)}


def findings(title: str, items: Iterable[Any], *, verdict: Any = None,
             reasons: Iterable[Any] = (), **opt: Any) -> dict[str, Any]:
    """A findings list. ``verdict`` is ``(t, c)`` | ``str`` | ``None``."""
    return _section("findings", title, {"verdict": _tc(verdict),
                                        "reasons": [str(r) for r in reasons],
                                        "items": list(items)}, **opt)


def check_row(name: str, state: str, *, detail: str = "", log: Iterable[Any] = (),
              do: dict[str, Any] | None = None) -> dict[str, Any]:
    """One check for :func:`checks` / :func:`block_checks`; ``log`` lines like :func:`code`."""
    return {"name": str(name), "state": state, "detail": str(detail),
            "log": _lines(log), "do": do}


_PASS_STATES = frozenset({"pass", "done", "ok"})
_FAIL_STATES = frozenset({"fail", "failed", "error"})
_PENDING_STATES = frozenset({"pending", "running"})


def _checks_summary(rows: Iterable[Any]) -> dict[str, int]:
    """Count check-row states into ``passing`` / ``failing`` / ``pending`` / ``na``."""
    out = {"passing": 0, "failing": 0, "pending": 0, "na": 0}
    for row in rows:
        state = str((row.get("state") if isinstance(row, dict) else "") or "").lower()
        if state in _PASS_STATES:
            out["passing"] += 1
        elif state in _FAIL_STATES:
            out["failing"] += 1
        elif state in _PENDING_STATES:
            out["pending"] += 1
        else:
            out["na"] += 1
    return out


def checks(title: str, rows: Iterable[Any], *, summary: dict[str, Any] | None = None,
           **opt: Any) -> dict[str, Any]:
    """A check list; ``summary`` is computed from the row states when ``None``."""
    rows = list(rows)
    return _section("checks", title, {
        "rows": rows,
        "summary": summary if summary is not None else _checks_summary(rows),
    }, **opt)


def agent_row(node_id: str, state: str, *, lane: str = "", model: str = "", step: str = "",
              last: str = "", cost: str = "", dur: str = "",
              do: dict[str, Any] | None = None) -> dict[str, Any]:
    """One agent for :func:`agents`."""
    return {"id": str(node_id), "state": state, "lane": str(lane), "model": str(model),
            "step": str(step), "last": str(last), "cost": str(cost), "dur": str(dur), "do": do}


def agents(title: str, rows: Iterable[Any], **opt: Any) -> dict[str, Any]:
    """The run's agents; rows come from :func:`agent_row`."""
    return _section("agents", title, {"rows": list(rows)}, **opt)


def composer(placeholder: str, cli_args: Iterable[str], *, title: str = "",
             **opt: Any) -> dict[str, Any]:
    """An input box that runs ``mini-ork <cli> …`` — the IDE appends typed text.

    ``title`` labels the box (e.g. "Steer the run"); it is the section title, so
    the layout keys (``full`` / ``col`` / ``note``) still ride ``**opt``.
    ``cli_args`` is the argv prefix as a list; the typed text becomes its last
    argument — e.g. ``["board", "steer", run_id, "--text"]``.
    """
    return _section("composer", title, {"placeholder": str(placeholder),
                                        "cli": [str(a) for a in cli_args]}, **opt)


def column(title: str, cards: Iterable[Any], *, c: str = "sub") -> dict[str, Any]:
    """One column of a :func:`columns` section; ``count`` tracks the card list."""
    cards = list(cards)
    return {"title": str(title), "c": c, "count": len(cards), "cards": cards}


def columns(title: str, cols: Iterable[Any], **opt: Any) -> dict[str, Any]:
    """Side-by-side columns; entries come from :func:`column`."""
    return _section("columns", title, {"cols": list(cols)}, **opt)


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
