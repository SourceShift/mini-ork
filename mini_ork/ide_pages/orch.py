"""Orchestrator & intake — how work gets into mini-ork.

Tabs: the orchestrator thread's defaults and history, saved kickoffs (with the
deterministic kickoff lint), races, the latest run's classification and plan,
and multi-agent coordination (Concord, through ContextNest).
"""
from __future__ import annotations

import contextlib
import functools
import json
import os
import sqlite3
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from mini_ork.ide_pages import spec as S
from mini_ork.ide_pages.run import plan_artifact, plan_steps, verifier_check_counts

TABS = [("session", "Session"), ("dispatch", "Dispatch"), ("decide", "Decide"),
        ("kickoffs", "Kickoffs"), ("races", "Races"), ("planner", "Classify & plan"),
        ("coord", "Coordination"), ("intake", "Intake")]

_WORKSPACE_LABELS = {"worktree": "New worktree per task", "in-place": "In place (this checkout)"}
_HISTORY_THREADS = 8
_HISTORY_RUNS = 8
_KICKOFF_LIMIT = 25
#: What the kickoff lists' lint badge actually checks — named once so the
#: Kickoffs and Dispatch tabs cannot describe it differently.
_KICKOFF_LINT_NOTE = ("Lint checks: paths that do not exist, no success section, "
                      "sections the recipe’s examples have and yours lacks.")
#: A picker axis renders at most this many chips before the rest are summarised
#: in its note — a home with forty recipes must not push the tab's content off.
_CHIP_CAP = 12
_RACE_SCAN = 200
_RACE_WINDOW_S = 180
#: Races the panel draws in full before it says how many it left out. Each race
#: is its own section with its contestants, so this is a rendering budget like
#: :data:`_CHIP_CAP` — the data is not truncated, only the drawing is.
_RACE_PANEL_MAX = 5


# ── shared reads ───────────────────────────────────────────────────────────

def _rows(home: Path, sql: str, params: tuple = ()) -> list[dict[str, Any]]:
    """Read-only rows from the home's state.db; ``[]`` when there is none."""
    db = home / "state.db"
    if not db.is_file():
        return []
    # `db_for` opens the same WAL db the other pages do; a ro URI without the
    # `-shm` sidecar fails and the page renders empty.
    from mini_ork.web.db import db_for
    state = db_for(home)
    try:
        return state.rows(sql, params)
    except sqlite3.OperationalError:
        return []


def _epoch(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return int(value)
    text = str(value).strip()
    if text.isdigit():
        return int(text)
    try:
        from datetime import datetime

        return int(datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp())
    except ValueError:
        return None


def _latest_run_with(home: Path, *names: str) -> tuple[str, Path] | None:
    """The newest run whose run dir holds one of ``names``."""
    for row in _rows(home, "SELECT id FROM task_runs ORDER BY created_at DESC, rowid DESC LIMIT 60"):
        run_dir = home / "runs" / str(row["id"])
        if any((run_dir / n).is_file() for n in names):
            return str(row["id"]), run_dir
    return None


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


# ── session ────────────────────────────────────────────────────────────────

def _env_source(home: Path, name: str) -> str:
    """Where ``name``'s value is read from, for a picker's note.

    ``apply_defaults`` lets an operator's shell outrank the store, so the file's
    value is the effective one only when the environment agrees with it; a
    disagreement means the shell won. An unreadable store degrades to the shell
    answer rather than raising — this is a picker, not a gate.
    """
    from mini_ork.features.store import effective_env, store_path

    try:
        declared = effective_env(store_path(home))
    except Exception:  # noqa: BLE001 — a broken store must not blank the picker
        declared = {}
    value = os.environ.get(name)
    if value and declared.get(name) == value:
        return "features.yaml"
    return "shell environment" if value else "built-in default"


def _lane_source(home: Path) -> str:
    """Where the thread's orchestrator lane comes from.

    Reports ``default_lane``'s own precedence instead of re-deriving it: the
    model override, the env name, the home's lane map, then the built-in.
    """
    if os.environ.get("MO_ACP_DEFAULT_MODEL"):
        return "MO_ACP_DEFAULT_MODEL"
    source = _env_source(home, "MO_ORCHESTRATOR_LANE")
    if source != "built-in default":
        return source
    try:
        from mini_ork.acp_orchestrator.config import load_lanes

        lanes = load_lanes(home)
        if isinstance(lanes, dict) and lanes.get("orchestrator"):
            return "lane map"
    except Exception:  # noqa: BLE001 — a lane map we cannot read is not a failure
        pass
    return "built-in default"


def _picker(title: str, items: list[dict[str, Any]], name: str, source: str,
            extra: str = "", note: str | None = None) -> dict[str, Any]:
    """One ``chips`` axis: the values, the active one, and where it came from.

    The active chip is hoisted ahead of the cap, so the current value stays
    visible on an axis with more options than the cap — a picker whose marked
    chip was truncated away reads as "nothing is set" and names no way back.

    ``note`` overrides the default sentence so a *page-scoped* axis (the
    Dispatch tab's overrides) can say it only affects this page, rather than
    claiming it writes ``features.yaml``.
    """
    active = [item for item in items if item.get("on")]
    shown = (active + [item for item in items if not item.get("on")])[:_CHIP_CAP]
    hidden = len(items) - len(shown)
    if note is None:
        note = f"Current value from {source} — picking one writes {name} to features.yaml."
    if hidden > 0:
        note += f" Showing {len(shown)} of {len(items)}."
    return S.chips(title, shown, full=True, note=note + extra)


def _thread_defaults(home: Path) -> list[dict[str, Any]]:
    """The thread's three pickers: orchestrator lane, recipe, workspace.

    Each was a read-only ``kv`` row naming the env var that would change it — the
    knob was legible but not turnable. Each is now a chip row whose click
    persists the choice through ``features set-env``, so the next render reads it
    back. Every note names the layer the current value came from, because the
    three layers disagree in practice: a shell export outranks the file.
    """
    from mini_ork.acp_orchestrator.config import default_lane, orchestrator_lanes

    lane = os.environ.get("MO_ACP_DEFAULT_MODEL") or default_lane(home)
    lanes = orchestrator_lanes()
    lane_ids = [str(entry["id"]) for entry in lanes if entry.get("id")]
    lane_extra = (" The thread is a control plane — the orchestrator decides per request"
                  " whether to answer, start a run, or edit directly.")
    if lane not in lane_ids:
        lane_extra += f" The active lane {lane!r} is not in the lane map."
    lane_items = [{"t": str(entry.get("name") or entry["id"]), "on": entry["id"] == lane,
                   "do": S.cli("features", "set-env", "MO_ORCHESTRATOR_LANE",
                               str(entry["id"]), home=False)}
                  for entry in lanes if entry.get("id")]

    recipe = os.environ.get("MO_ACP_RECIPE") or "code-fix"
    try:
        from mini_ork import recipes_catalog

        recipes = [entry.id for entry in recipes_catalog.list_recipes(home)]
    except Exception:  # noqa: BLE001 — an unreadable catalog still shows the active recipe
        recipes = []
    if recipe not in recipes:
        recipes.insert(0, recipe)

    ws = os.environ.get("MO_WORKSPACE_MODE") or "worktree"
    workspaces = dict(_WORKSPACE_LABELS)
    if ws not in workspaces:
        workspaces = {ws: ws, **workspaces}

    return [
        _picker("Orchestrator lane", lane_items, "MO_ORCHESTRATOR_LANE",
                _lane_source(home), lane_extra),
        _picker("Recipe",
                [{"t": rid, "on": rid == recipe,
                  "do": S.cli("features", "set-env", "MO_ACP_RECIPE", rid, home=False)}
                 for rid in recipes],
                "MO_ACP_RECIPE", _env_source(home, "MO_ACP_RECIPE")),
        _picker("Workspace",
                [{"t": label, "on": key == ws,
                  "do": S.cli("features", "set-env", "MO_WORKSPACE_MODE", key, home=False)}
                 for key, label in workspaces.items()],
                "MO_WORKSPACE_MODE", _env_source(home, "MO_WORKSPACE_MODE")),
    ]


def _mcp_tools() -> dict[str, Any]:
    from mini_ork.mcp_context import server

    read = [str(t.get("name")) for t in server.TOOL_DEFS]
    control = [str(t.get("name")) for t in server._CONTROL_TOOL_DEFS]
    return S.pills(f"MCP context server · {len(read) + len(control)} tools",
                   [(n, "muted") for n in read] + [(n, "blue") for n in control],
                   note="Grey: read-only, served to every agent in Zed. Blue: control tools, "
                        "orchestrator only (--control). Drafts and proposals never take effect "
                        "without a button.")


def _thread_cost(store: Any, thread_id: str) -> float | None:
    costs = None
    for rec in store.read(thread_id):
        if isinstance(rec, dict) and isinstance(rec.get("costs"), dict):
            costs = rec["costs"]
    if not costs:
        return None
    try:
        return sum(float(v or 0) for v in costs.values())
    except (TypeError, ValueError):
        return None


def _history(home: Path) -> dict[str, Any]:
    from mini_ork.acp.history import list_runs
    from mini_ork.acp.threads import ThreadStore

    entries: list[tuple[int, dict[str, Any]]] = []
    store = ThreadStore(home)
    for t in store.list_threads(limit=_HISTORY_THREADS):
        cost = _thread_cost(store, t["thread_id"])
        entries.append((_epoch(t.get("updated_at")) or 0, {
            "cells": [t.get("title") or "mini-ork thread", S.muted("▤ thread"),
                      S.mono(S.money(cost) if cost is not None else "—")],
            "do": S.page_link("thread", thread=t["thread_id"])}))
    runs, _ = list_runs(home, limit=_HISTORY_RUNS)
    for r in runs:
        entries.append((_epoch(r.get("updated_at") or r.get("created_at")) or 0, {
            "cells": [r.get("title") or r["run_id"], S.muted("◇ run"),
                      S.mono(S.money(r.get("cost_usd")) if r.get("cost_usd") is not None else "—")],
            "do": S.open_run(r["run_id"], r.get("title") or "")}))
    entries.sort(key=lambda e: e[0], reverse=True)
    rows = [e[1] for e in entries] or [[S.muted("No threads or runs yet"), "", ""]]
    return S.table("History · import threads", [S.col(fr=1), S.col(80), S.col(60)],
                   ["title", "source", "cost"], rows,
                   note="A thread row opens that thread — the next prompt is the same "
                        "conversation. A run row opens the run.")


# ── kickoffs ───────────────────────────────────────────────────────────────

def _front_matter_recipe(text: str) -> str | None:
    """The kickoff's declared recipe — shared with ``board start``.

    Kept as a page-local name because the page reads it in two places, but the
    reader lives in ``kickoff_lint`` so a started run resolves the same recipe
    the row displayed.
    """
    from mini_ork import kickoff_lint

    return kickoff_lint.front_matter_recipe(text)


@contextlib.contextmanager
def _recipe_lookup_cached() -> Iterator[None]:
    """``kickoff_lint.lint`` looks its recipe up with ``find_recipe``, which
    re-parses every recipe's YAML on each call (~0.25 s). Linting a page of
    kickoffs against one recipe asks the same question each time, so answer it
    once for the duration of this page build."""
    from mini_ork import recipes_catalog

    original = recipes_catalog.find_recipe
    cached = functools.lru_cache(maxsize=16)(original)
    recipes_catalog.find_recipe = cached  # type: ignore[assignment]
    try:
        yield
    finally:
        recipes_catalog.find_recipe = original  # type: ignore[assignment]


def _kickoffs(home: Path) -> dict[str, Any]:
    with _recipe_lookup_cached():
        return _kickoffs_section(home)


def _finding_text(finding: dict[str, Any]) -> str:
    """One lint finding as ``{glyph} {msg} — {fix}``.

    The format ``kickoff_lint.lint`` documents for its own card renderer, so a
    person reading the tab and one reading the agent's card are reading the same
    sentence about the same finding. The ``fix`` is dropped only when the linter
    did not compute one.
    """
    glyph = "✗" if finding.get("sev") == "error" else "⚠"
    msg = str(finding.get("msg") or "").strip()
    fix = str(finding.get("fix") or "").strip()
    return f"{glyph} {msg} — {fix}" if fix else f"{glyph} {msg}"


def _run_log(home: Path, run_id: str) -> str | None:
    """The run's own log, or ``None`` when it has not written one yet.

    Two real locations, in the order a run produces them: the detached launch
    log ``launch_run`` opens before the child starts, and the run's own
    ``execute.log`` once it is underway. Naming a path that does not exist would
    draw a dead button, so the caller only gets one that is on disk."""
    for candidate in (home / "runs" / run_id / "execute.log",
                      home / "runs-inbox" / f"{run_id}.launch.log"):
        if candidate.is_file():
            return str(candidate)
    return None


def _scan_kickoffs(home: Path) -> list[dict[str, Any]]:
    """Every saved kickoff, newest first, each with its lint findings and run.

    Shared by the Kickoffs and Dispatch tabs so the two cannot disagree about
    what a kickoff says or which recipe it resolves to.
    """
    from mini_ork import kickoff_lint

    folder = home / "kickoffs"
    files = sorted((p for p in folder.glob("*.md") if p.is_file()),
                   key=lambda p: p.stat().st_mtime, reverse=True) if folder.is_dir() else []
    runs_by_path: dict[str, dict[str, Any]] = {}
    for r in _rows(home, "SELECT id, status, kickoff_path FROM task_runs "
                         "WHERE kickoff_path LIKE ? ORDER BY created_at DESC", (f"{folder}%",)):
        runs_by_path.setdefault(str(r["kickoff_path"]), r)
    out: list[dict[str, Any]] = []
    for path in files:
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        declared = _front_matter_recipe(text)
        recipe = declared or os.environ.get("MO_ACP_RECIPE") or "code-fix"
        out.append({
            "path": path,
            "declared": declared,
            "recipe": recipe,
            "findings": kickoff_lint.lint(text, project=home.parent, recipe=recipe, home=home),
            "ran": runs_by_path.get(str(path)),
        })
    return out


def _lint_badge(findings: list[dict[str, Any]]) -> tuple[str, str]:
    """A kickoff's one-line lint summary and the marker colour for it."""
    errors = [f for f in findings if f.get("sev") == "error"]
    warns = [f for f in findings if f.get("sev") != "error"]
    if not findings:
        return "lint clean", "sub"
    first = _finding_text((errors or warns)[0])
    more = f" (+{len(findings) - 1} more)" if len(findings) > 1 else ""
    return f"lint: {first}{more}", "red" if errors else "yellow"


def _kickoff_parts(row: dict[str, Any]) -> list[str]:
    """The ``sub`` text of a kickoff row, shared by both lists."""
    parts = ["saved", _lint_badge(row["findings"])[0],
             row["recipe"] if row["declared"] else f"{row['recipe']} (default)"]
    if row["ran"]:
        parts.append(f"ran as {row['ran']['id']} · {row['ran'].get('status') or '—'}")
    return parts


def _kickoffs_section(home: Path) -> dict[str, Any]:
    folder = home / "kickoffs"
    rows = _scan_kickoffs(home)
    items = []
    for row in rows[:_KICKOFF_LIMIT]:
        path = row["path"]
        _lint_text, mc = _lint_badge(row["findings"])
        acts = [S.btn("Open", S.open_path(str(path)), "ghost")]
        if row["ran"]:
            ran = row["ran"]
            acts.append(S.btn("Open run", S.open_run(str(ran["id"]), path.stem)))
            log = _run_log(home, str(ran["id"]))
            if log:
                acts.append(S.btn("Log", S.open_path(log), "ghost"))
        acts.append(S.btn("Start run", S.thread(f"Start a run from the kickoff {path}"), "primary"))
        items.append(S.item(path.name, " · ".join(_kickoff_parts(row)), m="▤", mc=mc, acts=acts))
    if not items:
        items = [S.dot("No saved kickoffs", f"{folder} is empty — /kickoff in a thread drafts one")]
    note = _KICKOFF_LINT_NOTE
    if len(rows) > _KICKOFF_LIMIT:
        note += f" Showing the {_KICKOFF_LIMIT} newest of {len(rows)}."
    return S.lst("Kickoffs · .mini-ork/kickoffs/", items, full=True, note=note,
                 actions=[S.btn("New kickoff", S.thread("/kickoff "))])


# ── dispatch ───────────────────────────────────────────────────────────────

def _dispatch(home: Path, args: dict[str, str]) -> list[dict[str, Any]]:
    """Pick a recipe and a workspace, start a saved run, or draft a new one.

    The two pickers are deliberately *page-scoped*: a chip writes an ``--arg``
    (``S.set_args``), so choosing a recipe here changes only what this tab's
    Start buttons launch. The Session tab's pickers persist to ``features.yaml``
    because they set the orchestrator thread's own defaults; conflating the two
    would let a one-off dispatch silently re-aim every future thread.
    """
    recipe = args.get("recipe") or os.environ.get("MO_ACP_RECIPE") or "code-fix"
    workspace = args.get("workspace") or os.environ.get("MO_WORKSPACE_MODE") or "worktree"

    try:
        from mini_ork import recipes_catalog

        recipes = [entry.id for entry in recipes_catalog.list_recipes(home)]
    except Exception:  # noqa: BLE001 — an unreadable catalog still shows the active recipe
        recipes = []
    if recipe not in recipes:
        recipes.insert(0, recipe)
    workspaces = dict(_WORKSPACE_LABELS)
    if workspace not in workspaces:
        workspaces = {workspace: workspace, **workspaces}

    sections = [
        _picker(
            "Recipe · this page",
            [{"t": rid, "on": rid == recipe, "do": S.set_args(recipe=rid)} for rid in recipes],
            "recipe", "page override",
            note=(f"Start buttons here launch with {recipe}. Page-scoped: the Session tab's "
                  "Recipe picker is the one that changes the orchestrator's own default."),
        ),
        _picker(
            "Workspace · this page",
            [{"t": label, "on": key == workspace, "do": S.set_args(workspace=key)}
             for key, label in workspaces.items()],
            "workspace", "page override",
            note=(f"Start buttons here launch "
                  f"{_WORKSPACE_LABELS.get(workspace, workspace)}. Page-scoped, like the recipe above."),
        ),
    ]
    sections.append(_dispatch_list(home, recipe, workspace, args.get("kickoff", "")))
    sections.append(S.composer(
        "One sentence describing the work…", ["kickoff", "new"],
        title="New kickoff",
        note=("Writes .mini-ork/kickoffs/<slug>.md seeded with the recipe above, then refuses "
              "to overwrite an existing file."),
        full=True))
    panel = _lint_panel(home, args.get("kickoff", ""))
    if panel is not None:
        sections.append(panel)
    return sections


def _dispatch_list(home: Path, recipe: str, workspace: str, selected: str) -> dict[str, Any]:
    rows = _scan_kickoffs(home)
    items = []
    for row in rows[:_KICKOFF_LIMIT]:
        path = row["path"]
        _lint_text, mc = _lint_badge(row["findings"])
        on = str(path) == selected
        acts = [
            S.btn("Open", S.open_path(str(path)), "ghost"),
            S.btn("Clear lint" if on else "Lint",
                  S.set_args(kickoff="" if on else str(path)), "ghost"),
        ]
        if row["ran"]:
            ran = row["ran"]
            acts.append(S.btn("Open run", S.open_run(str(ran["id"]), path.stem)))
            log = _run_log(home, str(ran["id"]))
            if log:
                acts.append(S.btn("Log", S.open_path(log), "ghost"))
        acts.append(S.btn("Start run", S.cli(
            "board", "start", "--kickoff", str(path),
            "--recipe", recipe, "--workspace", workspace), "primary"))
        items.append(S.item(path.name, " · ".join(_kickoff_parts(row)),
                            m="▦" if on else "▤", mc=mc, acts=acts))
    if not items:
        items = [S.dot("No kickoffs yet",
                       "Draft one in the box below — it lands in .mini-ork/kickoffs/ and appears here.")]
    note = ("Start launches the run detached with the recipe and workspace picked above — no "
            "confirmation, because starting a run is what this tab is for. A red row is a lint "
            "error: advice, not a gate, so its Start button stays live.")
    if len(rows) > _KICKOFF_LIMIT:
        note += f" Showing the {_KICKOFF_LIMIT} newest of {len(rows)}."
    return S.lst("Start a run · .mini-ork/kickoffs/", items, full=True, note=note)


def _lint_panel(home: Path, selected: str) -> dict[str, Any] | None:
    """The lint findings for the kickoff named by ``args["kickoff"]``.

    ``None`` when nothing is selected, or when the selection is not a readable
    file — a panel with no subject is noise on a tab whose job is to list
    subjects, and a stale path is not worth an error card.

    Each finding is a check row: an error is ``fail``, a warning is ``pending``
    (there is no warn state the renderer draws), and the ``fix`` rides the row's
    detail. Level-1 builders get a markdown fallback, since ``checks`` is a v2
    section type.
    """
    if not selected:
        return None
    path = Path(selected)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    from mini_ork import kickoff_lint

    recipe = _front_matter_recipe(text) or os.environ.get("MO_ACP_RECIPE") or "code-fix"
    findings = kickoff_lint.lint(text, project=home.parent, recipe=recipe, home=home)
    title = f"Lint · {path.name}"
    if S.ide_level() < 2:
        body = "\n".join(f"- {_finding_text(f)}" for f in findings) or "- Lint clean."
        return S.markdown(title, body, full=True)
    rows = [
        S.check_row(str(f.get("msg") or ""),
                    "fail" if f.get("sev") == "error" else "pending",
                    detail=str(f.get("fix") or ""))
        for f in findings
    ] or [S.check_row("Lint clean", "pass", detail=f"recipe {recipe}")]
    return S.checks(title, rows, full=True,
                    note="Errors are advice, not a gate — the Start buttons above stay live.")


# ── races ──────────────────────────────────────────────────────────────────

def _race_lane(run_dir: Path) -> str:
    try:
        import yaml

        doc = yaml.safe_load((run_dir / "config" / "agents.race.yaml").read_text(encoding="utf-8")) or {}
    except Exception:  # noqa: BLE001
        return "?"
    lanes = doc.get("lanes") if isinstance(doc, dict) else None
    if isinstance(lanes, dict):
        for role in ("implementer", "implement"):
            if lanes.get(role):
                return str(lanes[role])
        values = sorted({str(v) for v in lanes.values() if v})
        return values[0] if len(values) == 1 else "?"
    return "?"


def _race_keep_action(
    home: Path, race: list[dict[str, Any]], winner: dict[str, Any]
) -> tuple[dict[str, Any], str]:
    """The "keep this contestant" button, and why it is off when it is off.

    The confirm is a ``race-keep --dry-run`` answer rather than a sentence the
    page composed: the page cannot compute which worktrees a discard frees, and
    a confirm that guessed would be asking the operator to approve numbers
    nobody checked. A plan that cannot be built — an unreadable kickoff, a
    worktree already gone — disables the button, because a keep that would be
    refused on click is worse than one that is visibly off.

    ``--force`` rides along: the confirm is the approval. ``merge`` records it
    against the worktree's tree sha and then checks for it — the same thing the
    run page's Merge button does — so this asserts an approval rather than
    skipping the gate.
    """
    from mini_ork.cli.board_cmd import _act_race_keep

    winner_id = str(winner["id"])
    siblings = [str(c["id"]) for c in race if str(c["id"]) != winner_id]
    label = f"Keep {winner['lane']}"
    plan = _act_race_keep(home, winner_id, siblings, dry_run=True)
    if plan.get("ok") is not True:
        return S.btn(label, None, "ghost"), str(plan.get("error") or "the plan was refused")

    kept = f"{winner['lane']} ({winner_id})"
    dropped = " · ".join(f"{c['lane']} ({c['id']})" for c in race if str(c["id"]) != winner_id)
    frees = ", ".join(str(path) for path in (plan.get("frees") or []))
    argv = ["board", "race-keep", winner_id]
    for sibling in siblings:
        argv += ["--discard", sibling]
    return (
        S.btn(label, S.cli(*argv, "--force", confirm=(
            f"Keep {kept}. Discard {dropped or 'nothing'}. "
            f"Frees: {frees or 'no worktree — this race used in-place workspaces'}.")), "primary"),
        "",
    )


def _open_workspaces(home: Path) -> set[str]:
    """Run ids with an open worktree record — what a keep would free.

    One read shared by the races panel and the attention strip: a race is
    "undecided" exactly when its contestants still hold worktrees, and the two
    views must not answer that differently.
    """
    wt_dir = home / "worktrees"
    if not wt_dir.is_dir():
        return set()
    return {p.stem for p in wt_dir.glob("*.json")}


def _race_groups(home: Path) -> list[list[dict[str, Any]]]:
    """The races, grouped — contestants launched on one kickoff within a few
    seconds of each other (``config/agents.race.yaml`` marks a contestant).

    Grouping lives here rather than in :func:`_races` because FR-040's strip
    counts undecided races with the same definition the panel draws them with;
    two spellings of "the same race" would let the strip say 1 and the panel
    show 2.
    """
    contestants = []
    for r in _rows(home, "SELECT id, status, cost_usd, created_at, kickoff_path FROM task_runs "
                         "ORDER BY created_at DESC LIMIT ?", (_RACE_SCAN,)):
        run_dir = home / "runs" / str(r["id"])
        if (run_dir / "config" / "agents.race.yaml").is_file():
            contestants.append({**r, "lane": _race_lane(run_dir), "run_dir": run_dir})
    races: list[list[dict[str, Any]]] = []
    for c in contestants:
        key = _kickoff_key(home, c)
        start = _epoch(c.get("created_at")) or 0
        for race in races:
            if race[0]["_key"] == key and abs((race[0]["_start"]) - start) <= _RACE_WINDOW_S:
                race.append({**c, "_key": key, "_start": start})
                break
        else:
            races.append([{**c, "_key": key, "_start": start}])
    return races


def _undecided(groups: list[list[dict[str, Any]]], open_ws: set[str]) -> list[list[dict[str, Any]]]:
    """The races nobody has decided yet: two or more contestants, and two or
    more worktrees still open. Keeping one discards the others, so a race with
    a single open worktree has already been decided."""
    return [race for race in groups
            if len(race) > 1 and sum(1 for c in race if str(c["id"]) in open_ws) > 1]


def _races(home: Path, now: int) -> list[dict[str, Any]]:
    """One section per race — the contestants *are* the decision input.

    The panel used to be one row per race, which named neither the lane being
    kept nor the worktrees being given up — and those are exactly what
    ``board race-keep`` needs, so the decision belonged somewhere the candidates
    were not on screen.
    """
    open_ws = _open_workspaces(home)
    races = _race_groups(home)

    lanes_env = os.environ.get("MO_RACE_LANES")
    note = (f"Default lanes come from MO_RACE_LANES ({lanes_env})." if lanes_env
            else "Default lanes: MO_RACE_LANES, else the race picks from your configured lanes.")
    note += " A race costs about one run per model."

    if not races:
        return [S.lst("Races", [S.item(
            "No races yet", "/race <task> runs one task on several models",
            acts=[S.btn("New race", S.thread("/race "), "primary")])], full=True, note=note)]

    sections: list[dict[str, Any]] = []
    for race in races[:_RACE_PANEL_MAX]:
        title = race[0]["_key"] or str(race[0]["id"])
        rows = []
        for c in race:
            passed, total = verifier_check_counts(c["run_dir"])
            open_here = str(c["id"]) in open_ws
            rows.append({
                "cells": [
                    S.cell(str(c["lane"]), "text", b=True),
                    S.mono(str(c["id"])),
                    S.cell(str(c.get("status") or ""), "sub"),
                    S.cell(f"{passed}/{total}" if total else "—",
                           "green" if total and passed == total else
                           ("yellow" if total else "sub"), mono=True),
                    S.mono(S.money(float(c.get("cost_usd") or 0))),
                    # An open workspace is the worktree a keep would free; the
                    # em dash says there is none to free, not that it is unknown.
                    S.cell("open" if open_here else "—", "yellow" if open_here else "sub"),
                ],
                "acts": [S.btn("Open run", S.open_run(str(c["id"]), title), "ghost")],
            })

        actions: list[dict[str, Any]] = []
        refusals: list[str] = []
        for c in race:
            action, why = _race_keep_action(home, race, c)
            actions.append(action)
            if why:
                refusals.append(f"{c['lane']}: {why}")
        detail = (f"{len(race)} contestants · started {S.age(race[0]['_start'], now)} · "
                  f"keeping one merges it and discards the others. ")
        detail += ("No keep is offered — " + "; ".join(refusals) if refusals else
                   "The confirm names the runs discarded and the worktrees freed.")
        sections.append(S.table(
            f"Race · {title}",
            [S.col(fr=1.4), S.col(fr=2.2), S.col(90), S.col(70), S.col(60), S.col(70)],
            ["lane", "run", "outcome", "checks", "cost", "worktree"],
            rows, full=True, note=detail, actions=actions,
        ))
    if len(races) > _RACE_PANEL_MAX:
        sections.append(S.lst("Earlier races", [S.item(
            f"{len(races) - _RACE_PANEL_MAX} more race(s) decided or awaiting a decision",
            "Newest first — the panel draws the most recent ones in full.",
            mc="sub")], full=True))
    # The lanes note and the button that starts a race describe how a race is
    # set up, which is not any one race's story — so they get their own line
    # rather than riding on the newest contestant table.
    sections.append(S.lst("Race lanes", [S.item("How lanes are chosen", note, mc="sub")],
                          full=True, actions=[S.btn("New race", S.thread("/race "))]))
    return sections


def _kickoff_key(home: Path, run: dict[str, Any]) -> str:
    """One run's task identity — see :func:`mini_ork.acp.history.kickoff_key`.

    ``board race-keep`` refuses a discard whose run does not share the winner's
    key, so the two must not spell "the same task" differently.
    """
    from mini_ork.acp.history import kickoff_key

    return kickoff_key(home, str(run["id"]), run.get("kickoff_path"))


# ── classify & plan ────────────────────────────────────────────────────────

#: How long a run may sit on ``needs_answers`` before the Decide tab starts
#: naming the recipe and start time it is holding — the cost of leaving it
#: parked. An operator tunes this to their own standby posture; one working hour
#: by default.
_WAIT_ALERT_S = int(os.environ.get("MO_DECIDE_ALERT_S") or 3600)
#: Waiting runs drawn on the Decide tab. Past this the tab points at the Planner
#: tab rather than growing an unbounded column of questions.
_DECIDE_LIMIT = 10
#: How many of the newest runs are asked whether they hold a profile. The same
#: newest-first window ``_latest_run_with`` scans.
_PROFILE_SCAN = 60


def _waited(seconds: int) -> str:
    """A wait at its coarsest useful grain: ``47m``, ``2h 05m``, ``3d 4h``."""
    if seconds < 60:
        return f"{max(seconds, 0)}s"
    if seconds < 5400:
        return f"{seconds // 60}m"
    if seconds < 86400:
        return f"{seconds // 3600}h {(seconds % 3600) // 60:02d}m"
    return f"{seconds // 86400}d {(seconds % 86400) // 3600}h"


def _wait_tone(seconds: int) -> tuple[str, str]:
    """The marker + colour a wait earns. Steadily hotter with age, so a run
    nobody has looked at for hours cannot read like one that just parked.
    Colours are the renderer's names, never hex."""
    if seconds >= 3 * _WAIT_ALERT_S:
        return ("!!", "red")
    if seconds >= _WAIT_ALERT_S:
        return ("!", "orange")
    if seconds >= 900:
        return ("?", "yellow")
    return ("?", "sub")


def _clock(stamp: int) -> str:
    """The wait's start as the operator's own wall clock."""
    return time.strftime("%H:%M", time.localtime(stamp))


def _question_text(value: Any) -> str:
    """A profile question as its promptable text.

    ``gen_profile`` writes plain strings, but the file is written by whatever
    planner ran, so a dict degrades to its text — keyed the same way
    ``plan._question_text`` keys it, because that is the key ``save_answers``
    has to match on re-dispatch.
    """
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return str(value.get("text") or value.get("question") or value)
    return str(value)


def _pending_questions(profile: dict[str, Any],
                       answers: dict[str, Any]) -> list[tuple[str, str]]:
    """``[(id, text)]`` for the questions this run has not answered yet.

    The id is the question's 1-based position (``q1``) over the *whole* list,
    which is what ``board answer --for`` resolves — so the box the operator sees
    and the argv the composer sends name the same question.
    """
    out: list[tuple[str, str]] = []
    for index, raw in enumerate(profile.get("human_questions") or [], 1):
        text = _question_text(raw)
        if text and text not in answers:
            out.append((f"q{index}", text))
    return out


def _recent_profiles(home: Path, errors: dict[str, str],
                     limit: int = _PROFILE_SCAN) -> list[dict[str, Any]]:
    """Every recent run that asked a question, longest-waiting first.

    ``_latest_run_with`` answers "what did the newest run decide", so a run
    parked on ``needs_answers`` three runs back is invisible to it. This walks
    the same newest-first window and keeps each run that actually asked
    something: a dir with no ``run_profile.json`` never asked, so it is skipped
    without an error, while a profile that will not parse is recorded in
    ``errors`` — a page that silently dropped it would report "nothing waiting"
    while a run sat blocked.

    The ``needs_answers`` predicate is ``web.control.get_profile``'s
    (``profile_status == "needs_answers" and len(questions) > len(answers)``),
    restated here because ``get_profile`` degrades an unparseable profile to
    ``{}`` and so cannot report the parse failure FR-020 asks for.

    The wait is measured from the profile file's mtime: the question was asked
    when the gate wrote it, and nothing rewrites the file until it is answered.
    """
    rows = _rows(home, "SELECT id, created_at FROM task_runs "
                       "ORDER BY created_at DESC, rowid DESC LIMIT ?", (limit,))
    found: list[dict[str, Any]] = []
    for row in rows:
        run_id = str(row["id"])
        run_dir = home / "runs" / run_id
        profile_path = run_dir / "run_profile.json"
        if not profile_path.is_file():
            continue
        try:
            asked_at = int(profile_path.stat().st_mtime)
            profile = json.loads(profile_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            errors[f"profile:{run_id}"] = f"run_profile.json unreadable: {exc}"
            continue
        if not isinstance(profile, dict):
            errors[f"profile:{run_id}"] = "run_profile.json is not an object"
            continue
        answers = _read_json(run_dir / "profile-answers.json")
        answers = answers if isinstance(answers, dict) else {}
        pending = _pending_questions(profile, answers)
        found.append({
            "run_id": run_id,
            "asked_at": asked_at,
            "created_at": str(row["created_at"] or ""),
            "recipe": str(profile.get("recipe") or ""),
            "profile_status": str(profile.get("profile_status") or ""),
            "pending": pending,
            "needs_answers": profile.get("profile_status") == "needs_answers" and bool(pending),
        })
    found.sort(key=lambda item: item["asked_at"])
    return found


def _decide(home: Path, now: int, errors: dict[str, str]) -> list[dict[str, Any]]:
    """The Decide tab: every run the profile gate parked, waiting longest first,
    each with a composer per question so the typed text is the answer.

    One box per question rather than one per run: ``board answer`` refuses a bare
    ``--text`` when several questions are outstanding, because
    ``save_answers`` clears them all at once and an answer attached to the wrong
    one would take the others with it. Binding each box to its own ``--for`` is
    what keeps a single-run answer unambiguous.
    """
    waiting = [item for item in _recent_profiles(home, errors) if item["needs_answers"]]
    if not waiting:
        return [S.lst("Blocked on you", [S.dot("Nothing is waiting for an answer")], full=True,
                      note="A run lands here when the profile gate parks it on "
                           "needs_answers. Answering it makes the run ready to re-dispatch.")]
    out: list[dict[str, Any]] = []
    for item in waiting[:_DECIDE_LIMIT]:
        run_id = item["run_id"]
        short = run_id[-12:] if len(run_id) > 12 else run_id
        waited = max(now - item["asked_at"], 0)
        marker, tone = _wait_tone(waited)
        detail = f"waiting {_waited(waited)}"
        if waited >= _WAIT_ALERT_S:
            # Past the threshold the *cost* is the point: name what is parked.
            detail += (f" · {item['recipe'] or 'no recipe'} · "
                       f"started {item['created_at'][:16] or '—'}")
        rows = [S.item(text, detail, m=marker, mc=tone, when=_clock(item["asked_at"]))
                for _qid, text in item["pending"]]
        out.append(S.lst(f"Blocked on you · {short}", rows, full=True,
                         note="One box per question below; each answer is bound to its "
                              "own question id, so it cannot land on another.",
                         actions=[S.btn("Open run", S.open_run(run_id), "ghost")]))
        for index, (qid, text) in enumerate(item["pending"], 1):
            out.append(S.composer(f"Answer q{index}: {text[:80]}",
                                  ["board", "answer", run_id, "--for", qid, "--text"],
                                  title=f"Answer · {short}", full=True))
    if len(waiting) > _DECIDE_LIMIT:
        out.append(S.lst(f"{len(waiting) - _DECIDE_LIMIT} more waiting",
                         [S.dot("See the Planner tab for the rest")], full=True))
    return out


def _planner(home: Path) -> list[dict[str, Any]]:
    # Keyed on the profile alone: requiring a literal plan.json hid every run
    # whose recipe plans with its own artefact (epic-runner), so this page
    # reported "no run classified yet" while such runs sat in the run dir.
    found = _latest_run_with(home, "run_profile.json")
    if found is None:
        return [S.lst("Classification", [S.dot("No run has been classified yet")], full=True)]
    run_id, run_dir = found
    short = run_id[-12:] if len(run_id) > 12 else run_id
    out: list[dict[str, Any]] = []
    profile = _read_json(run_dir / "run_profile.json")
    if isinstance(profile, dict):
        conf = profile.get("confidence")
        out.append(S.kv(f"Classification · {short}", [
            ("task_class", profile.get("task_class") or "—"),
            ("recipe", profile.get("recipe") or "—"),
            ("risk", profile.get("risk_tolerance") or "—"),
            ("confidence", f"{float(conf):.2f}" if isinstance(conf, (int, float)) else "—"),
            ("status", profile.get("profile_status") or "—",
             "green" if profile.get("profile_status") == "ready" else "yellow"),
        ], actions=[S.btn("Open run", S.open_run(run_id, str(profile.get("user_goal") or "")), "ghost")]))
    plan_path = plan_artifact(run_dir)
    plan = _read_json(plan_path) if plan_path else None
    if isinstance(plan, dict):
        items = []
        for step in plan.get("decomposition") or []:
            if not isinstance(step, dict):
                continue
            action = str(step.get("action") or "").strip()
            first = action.split(". ")[0][:140]
            sub = " · ".join(x for x in (str(step.get("node_type") or ""),
                                         str(step.get("target_file") or "")) if x)
            items.append(S.dot(f"{step.get('step', '·')} · {first}", sub))
        if not items:
            # Whatever the recipe called its plan, it is a plan: epic-runner's
            # planner emits epics[] + waves[], which the decomposition loop
            # above cannot see.
            items = [S.dot(title, note if note not in (title, sid) else "")
                     for sid, title, note in plan_steps(plan)]
        if not items:
            items = [S.dot("Plan has no steps")]
        out.append(S.lst(f"Plan · {short}", items, note=str(plan.get("objective") or "")[:300],
                         actions=[S.btn(f"Open {plan_path.name}", S.open_path(str(plan_path)), "ghost")]))
    questions = profile.get("human_questions") if isinstance(profile, dict) else None
    q_items = []
    for q in questions or []:
        text = q.get("question") if isinstance(q, dict) else q
        if text:
            q_items.append(S.item(str(text), f"profile_gate · {short}", m="?", mc="yellow",
                                  acts=[S.btn("Answer", S.thread(f"Answer for {run_id}: "), "primary")]))
    out.append(S.lst("Planner questions", q_items or [S.dot("No open questions", f"latest profiled run {short}")],
                     full=True, note="When the profile gate finds ambiguous inputs the run waits for "
                                     "answers, then resumes."))
    return out


# ── coordination ───────────────────────────────────────────────────────────

def _concord_stop(principal: str, what: str) -> dict[str, Any]:
    """A confirm-gated ``concord stop`` for one principal.

    ``--yes`` because the IDE spawns the verb with no tty, where it otherwise
    refuses; the gate the operator actually sees is this action's ``confirm``.
    ``home=False`` because the concord parser takes no ``--home`` — it reaches
    ContextNest over HTTP, and the IDE sets ``MINI_ORK_HOME`` on the spawn.
    """
    return S.cli("concord", "stop", principal, "--yes",
                 confirm=f"Stop {what}? Kills its process group.", home=False)


def _open_abs(path: Any) -> dict[str, Any] | None:
    """``open_path`` for an absolute path, else ``None``.

    A claim records the path it was made on, which may be repo-relative; the
    IDE resolves an ``open_path`` against its own cwd, so a relative one opens
    the wrong file or nothing at all. No button beats a wrong one.
    """
    text = str(path or "")
    return S.open_path(text) if text.startswith("/") else None


def _cn_up() -> bool:
    os.environ["CN_TIMEOUT_SEC"] = "0.5"
    from mini_ork import cn_client

    return bool(cn_client.available())


def _coord(home: Path, now: int) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    up = _cn_up()
    if up:
        os.environ["CN_COORD_TIMEOUT_SEC"] = "0.5"
        from mini_ork import cn_client

        def principals() -> dict[str, Any]:
            data = cn_client.coord_list_principals(status="active") or {}
            rows: list[Any] = []
            for p in data.get("principals") or []:
                pid = str(p.get("principal_id") or "")
                pids = p.get("pids") or []
                acts = []
                if pid:
                    acts = [
                        S.btn("Inbox", S.cli("concord", "inbox", "--principal", pid, home=False),
                              "ghost"),
                        # A send needs a body, which a click cannot supply: the
                        # button opens the thread with the verb and the target
                        # already typed, so the operator writes only the message.
                        S.btn("Send", S.thread(f"/concord send {pid} "), "ghost"),
                        S.btn("Stop", _concord_stop(pid, pid), "warn"),
                    ]
                row: dict[str, Any] = {
                    "cells": [S.mono(pid), S.mono(pids[0] if pids else p.get("pgid") or "—"),
                              S.cell(p.get("status") or "—",
                                     "green" if p.get("status") == "active" else "sub"),
                              S.mono(p.get("unacked_messages", 0))],
                    "acts": acts,
                }
                # Only where the payload actually names a run — a principal
                # does not carry one by contract, so this is usually absent
                # rather than a link that resolves to nothing.
                run_id = cn_client.extract_run_id(p)
                if run_id:
                    row["do"] = S.open_run(run_id)
                rows.append(row)
            return S.table("Principals · concord", [S.col(fr=1), S.col(60), S.col(80), S.col(50)],
                           ["principal", "pid", "state", "inbox"],
                           rows or [[S.muted("No active principals"), "", "", ""]],
                           actions=[S.btn("Send message", S.thread("/concord send "))])

        def claims() -> dict[str, Any]:
            data = cn_client.coord_hot_claims() or {}
            rows: list[Any] = []
            for c in data.get("claims") or []:
                holder = str(c.get("principal_id") or "")
                acts = []
                opened = _open_abs(c.get("path"))
                if opened:
                    acts.append(S.btn("Open", opened, "ghost"))
                if holder:
                    acts.append(S.btn("Stop holder",
                                      _concord_stop(holder, f"the holder of {c.get('path') or '?'}"),
                                      "warn"))
                rows.append({
                    "cells": [S.mono(c.get("path") or ""), S.muted(holder),
                              S.muted(c.get("expires_at") or "")],
                    "acts": acts,
                })
            return S.table("Claims on hot files", [S.col(fr=1), S.col(120), S.col(50)],
                           ["file", "holder", "until"],
                           rows or [[S.muted("No live hot-file claims"), "", ""]])

        def violations() -> dict[str, Any]:
            """Recent ``--owns`` violations, each with the writer's own actions.

            No admit button: ``concord admit`` is scoped to an epic whose plan
            overlaps one in progress and takes no principal, so it is not a
            per-claim or per-violation act.
            """
            data = cn_client.coord_owns_violations(0) or {}
            items = []
            for v in (data.get("violations") or [])[-8:]:
                holder = str(v.get("worktree_principal") or "")
                acts = []
                opened = _open_abs(v.get("path"))
                if opened:
                    acts.append(S.btn("Open", opened, "ghost"))
                if holder:
                    acts.append(S.btn("Stop holder",
                                      _concord_stop(holder, f"the writer of {v.get('path') or '?'}"),
                                      "warn"))
                items.append(S.bad(f"{holder or '?'} edited {v.get('path') or '?'}",
                                   f"caller {v.get('caller_principal') or '—'} · {v.get('ts') or ''}",
                                   acts=acts))
            return S.lst("Scope violations (--owns)", items or [S.ok("No --owns violations recorded")])

        def overlaps() -> dict[str, Any]:
            """Open → escalated → acked, capped at 30, with one confirm-gated
            Ack per open/escalated row.

            ``/api/v1/coord/overlaps`` returns an envelope
            (``{"overlaps": [...]}``) where each overlap carries ``id``,
            ``kind``, ``a`` and ``b`` (principal ids), ``state``
            (``open`` / ``escalated`` / ``acked``), a numeric ``count``, and
            ``first_seen`` (epoch int). The Ack button posts to
            ``/api/v1/coord/overlaps/<id>/ack`` through the new
            ``mini-ork cn ack-overlap`` verb — ``home=False`` because the
            cn parser rejects ``--home``, and ``confirm`` so the IDE does
            not silently bind the operator's choice to the click. Ack
            never appears on already-acked rows.

            The read also writes the ``coord-overlaps`` cache slot — the
            slot the Runs / Tasks board's attention peek reads via
            ``cn_client._read_section_cache``. The Board's peek is
            read-only and never blocks on ContextNest, so a healthy
            render here is what keeps the overlap badges on the board's
            cards accurate. Scope is the empty string because the live
            endpoint's filter is server-side, not per-project — the
            board's matcher filters overlaps to the project's runs
            itself. The write is best-effort: a cache failure is logged
            out by ``_write_section_cache`` and cannot blank the page.
            """
            data = cn_client._coord_request(
                "GET", "/api/v1/coord/overlaps"
            ) or {}
            items = [it for it in (data.get("overlaps") or data.get("items") or [])
                     if isinstance(it, dict)]
            # Mirror the live envelope into ``coord-overlaps`` so the Board's
            # attention peek has fresh data on the next board build. Always
            # write when the read returned a usable envelope — an empty list
            # is preferable to a stale slot still serving yesterday's badges.
            try:
                cn_client._write_section_cache("coord-overlaps", "", {
                    "overlaps": list(items),
                    "_as_of": int(time.time()),
                    "_from_cache": False,
                })
            except Exception:  # noqa: BLE001 — best-effort graceful degradation
                pass
            total = len(items)
            items.sort(key=_overlap_sort_key)
            items = items[:30]
            rendered = len(items)

            rows: list[dict[str, Any]] = []
            for o in items:
                oid = str(o.get("id") or o.get("overlap_id") or "")
                a = str(o.get("a") or "—")
                b = str(o.get("b") or "—")
                kind = str(o.get("kind") or "—")
                state = str(o.get("state") or "acked")
                count_raw = o.get("count")
                if isinstance(count_raw, (int, float)):
                    count_text = str(int(count_raw))
                else:
                    count_text = "—"
                age_epoch = _epoch(o.get("first_seen"))
                row: dict[str, Any] = {
                    "cells": [
                        S.mono(oid or "—"),
                        S.cell(kind, "sub"),
                        S.cell(f"{a} ↔ {b}", "text"),
                        S.cell(state,
                               "yellow" if state == "open"
                               else "orange" if state == "escalated"
                               else "sub"),
                        S.mono(count_text),
                        S.mono(S.age(age_epoch, now) if age_epoch else "—"),
                    ],
                }
                if oid and state in ("open", "escalated"):
                    ack_action = S.cli(
                        "cn", "ack-overlap", oid,
                        confirm=f"Acknowledge overlap {oid}?",
                        home=False,
                    )
                    row["acts"] = [S.btn("Ack", ack_action, "warn")]
                rows.append(row)
            if not rows:
                rows = [[S.muted("No live overlaps"), "", "", "", "", ""]]
                note = "No overlaps reported."
            else:
                note = (f"Showing {rendered} of {total}. "
                        "Open rows sort first, then escalated, then acked. "
                        "Ack posts to /api/v1/coord/overlaps/<id>/ack "
                        "through `mini-ork cn ack-overlap` (run with confirm).")
            return S.table(
                "Overlaps · concord",
                [S.col(fr=1), S.col(70), S.col(fr=2), S.col(70),
                 S.col(50), S.col(50)],
                ["id", "kind", "a ↔ b", "state", "count", "age"],
                rows, full=True,
                note=note,
            )

        def freezes() -> dict[str, Any]:
            """Operator-applied path freezes from ``/api/v1/coord/freezes``."""
            data = cn_client._coord_request(
                "GET", "/api/v1/coord/freezes"
            ) or {}
            items = [it for it in (data.get("freezes") or data.get("items") or [])
                     if isinstance(it, dict)]
            rows: list[list[Any]] = []
            for f in items:
                rows.append([
                    S.mono(f.get("path") or "—"),
                    S.muted(f.get("principal_id") or f.get("holder") or "—"),
                    S.mono(f.get("expires_at") or f.get("until") or "—"),
                    S.muted(S.age(_epoch(f.get("started_at") or f.get("ts")
                                          or f.get("created_at")), now)),
                ])
            if not rows:
                rows = [[S.muted("No active path freezes"), "", "", ""]]
            return S.table(
                "Active path freezes",
                [S.col(fr=1), S.col(120), S.col(80), S.col(50)],
                ["path", "holder", "until", "age"],
                rows,
            )

        def topic_pairs() -> dict[str, Any]:
            """Pairs of live sessions CN scored as the same basin.

            Each pair carries ``a`` and ``b`` (principal ids), a textual
            ``a_text`` / ``b_text`` for each side (the session's intent),
            and a numeric ``similarity`` (0..1). The intents are cut to
            120 characters so a session with a long kickoff does not push
            another column off the page; pairs are sorted by similarity,
            highest first.
            """
            data = cn_client._coord_request(
                "GET", "/api/v1/coord/topic-pairs"
            ) or {}
            pairs = [it for it in (data.get("pairs") or data.get("topic_pairs")
                                    or data.get("items") or [])
                     if isinstance(it, dict)]
            total = len(pairs)

            def _similarity_key(p: dict[str, Any]) -> float:
                sim = p.get("similarity")
                if isinstance(sim, (int, float)):
                    return float(sim)
                return 0.0

            pairs.sort(key=_similarity_key, reverse=True)
            pairs = pairs[:30]
            rendered = len(pairs)

            items: list[dict[str, Any]] = []
            for p in pairs:
                a = str(p.get("a") or "—")
                b = str(p.get("b") or "—")
                a_text = _truncate(p.get("a_text"), 120)
                b_text = _truncate(p.get("b_text"), 120)
                sim = p.get("similarity")
                if isinstance(sim, (int, float)):
                    sim_text = f"{float(sim):.2f}"
                else:
                    sim_text = "—"
                items.append(S.dot(
                    f"{a} ↔ {b}",
                    f"{a_text}  ×  {b_text}  ·  similarity {sim_text}",
                    mc="yellow",
                ))
            if not items:
                items = [S.dot("No duplicate-work pairs reported")]
                note = "Two live sessions scored as the same basin would show here."
            else:
                note = (f"Showing {rendered} of {total}. Two live sessions scored "
                        "as the same basin; the strongest pair sits at the top.")
            return S.lst(
                "Duplicate work · concord",
                items,
                note=note,
            )

        errors: dict[str, str] = {}
        out += S.guarded(errors, "Principals · concord", principals)
        out += S.guarded(errors, "Claims on hot files", claims)
        out += S.guarded(errors, "Scope violations (--owns)", violations)
        out += S.guarded(errors, "Overlaps · concord", overlaps)
        out += S.guarded(errors, "Active path freezes", freezes)
        out += S.guarded(errors, "Duplicate work · concord", topic_pairs)
    else:
        out.append(S.lst("Principals · concord", [S.warn(
            "ContextNest is not reachable", "Concord principals, hot-file claims and --owns violations "
            "live in ContextNest (CN_BASE_URL); start it to see them")], full=True))
    out.append(_spawn_tree(home, now))
    return out


def _overlap_sort_key(overlap: dict[str, Any]) -> tuple[int, int]:
    """``(bucket, -age)`` — open (0) → escalated (1) → acked (2); anything
    else lands at bucket 3 so it never out-orders a real state.

    The inner term is the negation of ``age`` (epoch from ``first_seen``),
    so a NEWER overlap in any bucket sorts FIRST. ``now`` is intentionally
    not threaded through — the key is pure on the row itself so the sort
    is stable across renders.
    """
    state = str(overlap.get("state") or "")
    bucket = _OVERLAP_STATE_BUCKET.get(state, 3)
    age = _epoch(overlap.get("first_seen")) or 0
    return (bucket, -age if age else 0)


# State → bucket for the Overlaps sort. Unknown states (or a missing one)
# sort AFTER acked so they never silently demote a real row to the tail.
_OVERLAP_STATE_BUCKET = {"open": 0, "escalated": 1, "acked": 2}


def _truncate(text: Any, limit: int) -> str:
    """``text`` cut to ``limit`` characters; ``None`` / non-strings → ``""``."""
    if text is None:
        return ""
    s = str(text)
    return s[:limit] if len(s) > limit else s


def _spawn_tree(home: Path, now: int) -> dict[str, Any]:
    roots = _rows(home, "SELECT root_run_id FROM run_spawns ORDER BY created_at DESC LIMIT 1")
    if not roots:
        return S.code("Spawn tree", [("No run has spawned children yet", "dim")])
    root = str(roots[0]["root_run_id"])
    kids = _rows(home, "SELECT child_run_id, recipe, status, depth FROM run_spawns "
                       "WHERE root_run_id = ? ORDER BY created_at", (root,))
    root_row = _rows(home, "SELECT recipe FROM task_runs WHERE id = ?", (root,))
    lines: list[tuple[str, str]] = [(f"{root}  {root_row[0]['recipe'] if root_row else ''}".rstrip(), "text")]
    marks = {"completed": "✓", "failed": "✗", "running": "●"}
    for i, k in enumerate(kids):
        branch = "└─" if i == len(kids) - 1 else "├─"
        indent = "   " * max(0, int(k.get("depth") or 1) - 1)
        mark = marks.get(str(k.get("status")), "○")
        colour = "red" if k.get("status") == "failed" else "muted"
        lines.append((f"{indent}{branch} {k['child_run_id']}  {k.get('recipe') or ''}  {mark}", colour))
    return S.code("Spawn tree", lines[:40])


# ── intake ─────────────────────────────────────────────────────────────────

# ``(registered, enabled)`` → what ``mini-ork intake status`` calls it. A source
# the config enables but no adapter could be built for is *skipped*, which is its
# own answer: the config is fine and the token is not.
_SOURCE_STATE = {
    (False, False): ("skipped", "sub"),
    (False, True): ("skipped", "sub"),
    (True, False): ("disabled", "yellow"),
    (True, True): ("registered", "green"),
}

# What one ledger row did. ``launched``/``continued`` put a run on the machine;
# ``steered`` reached one already there; ``skipped`` is a board's normal state.
_ACTION_TONE = {
    "launched": "green",
    "continued": "green",
    "steered": "blue",
    "duplicate": "sub",
    "skipped": "sub",
    "failed": "red",
    "ignored": "sub",
}


def _trigger_text(rules: Any) -> str:
    """A source's rules as one AND-joined line, or the reason there is nothing.

    Deliberately the same reading ``mini-ork intake status`` prints, so a person
    comparing the tab and the terminal is not comparing two phrasings.
    """
    if not rules.configured:
        return "no trigger configured"
    parts = []
    if rules.label is not None:
        parts.append(f"label {rules.label!r}")
    if rules.assignee is not None:
        parts.append(f"assignee {rules.assignee!r}")
    if rules.states:
        parts.append("state in (" + ", ".join(str(state) for state in rules.states) + ")")
    return " AND ".join(parts) or "no trigger configured"


def _source_statuses(home: Path) -> list[Any]:
    """Every intake source this build knows about.

    The Intake tab and the attention strip both ask, and a source present in one
    and missing from the other is the disagreement FR-040 forbids — so both read
    it through here rather than each calling the registry themselves.
    """
    from mini_ork.intake.poll import source_statuses
    from mini_ork.web.db import resolve_db

    return source_statuses(resolve_db(home))


def _chain_health(home: Path, statuses: list[Any]) -> dict[str, Any]:
    """The intake chain as a checklist — one row per link, each naming its fix.

    "Nothing is starting" is answered by finding the first link that is not
    passing, so the page renders the links as rows rather than as a sentence the
    reader has to scan themselves. Two deliberate omissions:

    * The run reaper is not a row. It runs *before* this page is built
      (``board_cmd.build_payload`` reaps, then builds), so its failure is already
      on the page as the error banner — and reaping again here would run a write
      a second time to learn something the caller already holds.
    * A source that is registered but switched off is ``na``, not ``fail``: it is
      a decision someone made, and the summary must not read it as a problem.
    """
    rows: list[dict[str, Any]] = []
    for status in statuses:
        # Only a source that is registered *and* enabled is a passing link: one
        # that is switched off is ``na``, not a fault, because someone decided
        # that — the reading ``_SOURCE_STATE`` gives the intake table's own
        # state column, so the two views cannot disagree about one source.
        if not status.registered:
            state, detail = "na", "not registered"
        elif status.enabled:
            state, detail = "pass", "registered"
        else:
            state, detail = "na", "registered but switched off"
        rows.append(S.check_row(f"{status.source} source", state,
                                detail=status.skipped_reason or detail))
    registered = [s for s in statuses if s.registered and s.enabled]
    for status in registered:
        rows.append(S.check_row(
            f"{status.source} trigger",
            "pass" if status.trigger.configured else "fail",
            detail=_trigger_text(status.trigger),
        ))
    for status in statuses:
        if status.sync_error:
            rows.append(S.check_row(f"{status.source} last sync", "fail", detail=status.sync_error))
    try:
        from mini_ork.automations import scheduler_status

        scheduler = scheduler_status(home)
    except Exception as exc:  # noqa: BLE001 — a broken read must never read as passing
        rows.append(S.check_row("Scheduler", "fail", detail=f"{type(exc).__name__}: {exc}"))
    else:
        installed = bool(scheduler.get("installed"))
        rows.append(S.check_row(
            "Scheduler",
            "pass" if installed else "fail",
            detail=(f"last tick {scheduler.get('last_tick') or 'never'}" if installed
                    else "the OS tick is not installed"),
        ))
    return S.checks("Intake chain", rows, full=True,
                    note="Each row is a link in the chain from a board event to a run. "
                         "The first one that is not passing is why nothing is starting.",
                    actions=[S.btn("Open config", S.open_path(str(home / "config" / "pm.yaml")),
                                   "ghost")])


def _intake(home: Path, errors: dict[str, str]) -> list[dict[str, Any]]:
    """Every source, its trigger, its last sync — and what arrived lately."""
    statuses = _source_statuses(home)
    rows = []
    for status in statuses:
        state, colour = _SOURCE_STATE[(status.registered, status.enabled)]
        detail = status.skipped_reason or status.sync_error or ""
        rows.append(
            {
                "cells": [
                    S.cell(status.source, "text", b=True),
                    S.cell(state, colour),
                    S.cell(_trigger_text(status.trigger),
                           "sub" if not status.trigger.configured else "text"),
                    S.cell(status.trigger.recipe or "—", "sub", mono=True),
                    S.cell(str(status.mirrored), "sub", mono=True),
                    S.cell(status.synced_at or "never", "sub"),
                    S.cell(detail, "red" if status.sync_error else "sub"),
                ],
                # On demand, one source at a time — the same verb the tab's own
                # "Poll now" runs, narrowed by `--source`. A source the registry
                # never built has no adapter to call, so it gets no button
                # rather than one that fails on stderr when pressed.
                "acts": [S.btn("Sync", S.cli("intake", "once", "--source", status.source, confirm=(
                    f"Poll {status.source} now? Every trigger it satisfies starts a run."
                )), "ghost")] if status.registered else [],
            }
        )
    sections: list[dict[str, Any]] = [
        S.table(
            "Sources",
            [S.col(fr=1.0), S.col(90), S.col(fr=2.0), S.col(fr=1.0),
             S.col(70), S.col(fr=1.0), S.col(fr=1.6)],
            ["source", "state", "trigger", "recipe", "mirrored", "synced", "note"],
            rows,
            full=True,
            note="All four rule keys are opt-in and ANDed. A source with none of them "
                 "starts NOTHING — a first poll against a real board must not launch an "
                 "agent per ticket.",
        )
    ]
    if S.ide_level() >= 2:
        # The checklist states this as a failing row carrying its own detail, so
        # the callout's sentence would be the same finding said twice. An older
        # IDE has no checks section and keeps the callout.
        sections.append(_chain_health(home, statuses))
    elif not any(status.trigger.configured for status in statuses if status.registered):
        sections.append(
            S.callout(
                "Nothing will start",
                "No registered source has a trigger. Add `trigger_label`, "
                "`trigger_assignee` or `trigger_states` under a source in "
                "`config/pm.yaml` — the label is the human act that says an agent "
                "should do this.",
                tone="orange",
            )
        )
    sections += S.guarded(errors, "Recent arrivals", lambda: _arrivals(home))
    return sections


def _arrivals(home: Path) -> dict[str, Any]:
    """The intake ledger, newest first — what a trigger acted on, and what folded."""
    # `run_id` arrives with migration 0075 and its runtime twin, and a page can
    # be the first thing to open a state.db an older release wrote — the IDE
    # never migrates a home. So the column is probed, not assumed: on a ledger
    # that has not widened yet the arrivals still render, every run an em dash,
    # which is precisely what those rows recorded. Assuming the column would
    # make `_rows` swallow the OperationalError and blank the whole table.
    has_run_id = bool(_rows(
        home, "SELECT 1 FROM pragma_table_info('intake_events') WHERE name = 'run_id'"
    ))
    columns = ("provider, action, issue_uid, thread_id" + (", run_id" if has_run_id else "")
               + ", created_at")
    records = _rows(
        home,
        f"SELECT {columns} FROM intake_events ORDER BY id DESC LIMIT 25",
    )
    rows = []
    for record in records:
        run_id = str(record.get("run_id") or "")
        rows.append(
            {
                "cells": [
                    S.cell(record.get("provider") or "", "text", b=True),
                    S.cell(record.get("action") or "",
                           _ACTION_TONE.get(str(record.get("action")), "sub")),
                    S.mono(record.get("issue_uid") or "—"),
                    S.mono(record.get("thread_id") or "—"),
                    # An arrival that started no run — a replay that folded into
                    # a live one, or a row written before the column existed —
                    # says so with an em dash. That is a claim, and an empty
                    # cell would read as a link that failed to draw.
                    S.mono(run_id or "—", "sub" if not run_id else None),
                    S.cell(str(record.get("created_at") or ""), "sub"),
                ],
                # Only a row that named a run gets the button: with nothing to
                # open the action is absent, not disabled.
                "acts": [S.btn("Open run", S.open_run(run_id), "ghost")] if run_id else [],
            }
        )
    return S.table(
        "Recent arrivals",
        [S.col(fr=1.0), S.col(90), S.col(fr=2.0), S.col(fr=2.0), S.col(fr=2.0), S.col(fr=1.4)],
        ["source", "action", "issue", "thread", "run", "when"],
        rows,
        full=True,
        note="The ledger that makes a webhook replay or a re-poll a no-op: one row per "
             "arrival, keyed by provider and delivery id. `run` is the run the arrival "
             "started; an em dash means it started none.",
    )


# ── page ───────────────────────────────────────────────────────────────────

# ── the attention strip (FR-040) ───────────────────────────────────────────

def _waiting_runs(home: Path, errors: dict[str, str]) -> list[dict[str, Any]]:
    """The runs parked on a question nobody has answered."""
    return [item for item in _recent_profiles(home, errors) if item["needs_answers"]]


def _failing_links(home: Path) -> list[dict[str, Any]]:
    """The intake checklist's failing rows.

    Built through :func:`_chain_health` — the intake tab's own call — so the
    strip's count and the tab's rows are the same objects and cannot drift. A
    source that is switched off is ``NA``, not a fault: someone decided that.
    """
    checks = _chain_health(home, _source_statuses(home))
    return [row for row in checks["rows"] if str(row.get("state") or "") == "fail"]


def attention(home: Path, tab: str, errors: dict[str, str]) -> dict[str, Any] | None:
    """FR-040 — one strip saying what is blocked on the reader, or ``None``.

    Four inputs, each read through the helper its own tab reads it with, so the
    strip cannot say 2 where the tab shows 3. Every item's button is the action
    that resolves it, which is why the errors item's is a reload of this page: a
    read that failed may succeed on the next one, and the banner at the foot of
    the page carries the text.

    ``errors`` is the page's own map and is written to in place — a run profile
    that cannot be parsed is discovered here on tabs that would never have
    looked, and it belongs with every other read failure in that banner.
    """
    waiting = _waiting_runs(home, errors)
    races = _undecided(_race_groups(home), _open_workspaces(home))
    links = _failing_links(home)

    # (pill, what it is, the button that resolves it), in the order a reader
    # should care: work already paid for that is stuck, then money still burning,
    # then the chain that starts everything, then this page's own reads.
    parts: list[tuple[dict[str, Any], str, dict[str, Any]]] = []
    if waiting:
        n = len(waiting)
        parts.append((
            {"t": f"{n} waiting", "c": "yellow"},
            f"{n} run{'s' if n != 1 else ''} waiting on an answer",
            S.btn("Answer", S.page_link("orch", tab="decide"), "primary")))
    if races:
        n = len(races)
        parts.append((
            {"t": f"{n} race{'s' if n != 1 else ''}", "c": "orange"},
            f"{n} undecided race{'s' if n != 1 else ''}",
            S.btn("Decide", S.page_link("orch", tab="races"))))
    if links:
        n = len(links)
        parts.append((
            {"t": f"{n} chain link{'s' if n != 1 else ''}", "c": "red"},
            "intake chain: " + ", ".join(str(row["name"]) for row in links),
            S.btn("Fix the chain", S.page_link("orch", tab="intake"))))
    if errors:
        n = len(errors)
        parts.append((
            {"t": f"{n} error{'s' if n != 1 else ''}", "c": "red"},
            "could not be read: " + ", ".join(errors),
            S.btn("Re-read", S.page_link("orch", tab or None), "ghost")))
    if not parts:
        return None

    total = len(waiting) + len(races) + len(links) + len(errors)
    return S.triage(
        f"{total} {'thing' if total == 1 else 'things'} "
        f"{'needs' if total == 1 else 'need'} you",
        tone="red" if any(pill["c"] == "red" for pill, _what, _do in parts) else "yellow",
        icon="✋",
        detail=" · ".join(what for _pill, what, _do in parts),
        counts=[pill for pill, _what, _do in parts],
        actions=[do for _pill, _what, do in parts],
    )


def build(home: Path, tab: str | None, args: dict[str, str]) -> dict[str, Any]:
    tab = tab if tab in dict(TABS) else "session"
    now = int(time.time())
    errors: dict[str, str] = {}
    sections: list[dict[str, Any]] = []
    if tab == "session":
        sections += S.guarded(errors, "Thread defaults", lambda: _thread_defaults(home))
        sections += S.guarded(errors, "MCP context server", _mcp_tools)
        sections += S.guarded(errors, "History · import threads", lambda: _history(home))
    elif tab == "dispatch":
        with _recipe_lookup_cached():
            sections += S.guarded(errors, "Dispatch", lambda: _dispatch(home, args))
    elif tab == "decide":
        sections += S.guarded(errors, "Decide", lambda: _decide(home, now, errors))
    elif tab == "kickoffs":
        sections += S.guarded(errors, "Kickoffs", lambda: _kickoffs(home))
    elif tab == "races":
        sections += S.guarded(errors, "Races", lambda: _races(home, now))
    elif tab == "planner":
        sections += S.guarded(errors, "Classify & plan", lambda: _planner(home))
    elif tab == "intake":
        sections += S.guarded(errors, "Intake", lambda: _intake(home, errors))
    else:
        sections += S.guarded(errors, "Coordination", lambda: _coord(home, now))
    actions = [S.btn("New thread", S.thread(""), "primary")]
    if tab == "intake":
        # Both are the CLI verb, so the tab adds no path to a running agent that
        # `mini-ork intake` does not already have. The preview starts nothing and
        # therefore asks nothing; the real pass confirms first.
        actions = [
            S.btn("Preview a pass", S.cli("intake", "once", "--dry-run")),
            S.btn(
                "Poll now",
                S.cli("intake", "once", confirm="Start the runs this pass selects?"),
                "primary",
            ),
            S.btn("Open threads", S.page_link("thread"), "ghost"),
        ]
    return S.page("orch", "Orchestrator & intake",
                  "How work gets in: the orchestrator thread, kickoffs, races, the planner, "
                  "multi-agent coordination, and the tickets that start runs on their own.",
                  chips_=[S.chip("mini-ork acp")],
                  actions=actions,
                  tabs=TABS, tab=tab, args=args, sections=sections, errors=errors)
