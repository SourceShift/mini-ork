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
from mini_ork.ide_pages.run import plan_artifact, plan_steps

TABS = [("session", "Session"), ("kickoffs", "Kickoffs"), ("races", "Races"),
        ("planner", "Classify & plan"), ("coord", "Coordination"),
        ("intake", "Intake")]

_WORKSPACE_LABELS = {"worktree": "New worktree per task", "in-place": "In place (this checkout)"}
_HISTORY_THREADS = 8
_HISTORY_RUNS = 8
_KICKOFF_LIMIT = 25
_RACE_SCAN = 200
_RACE_WINDOW_S = 180


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

def _thread_defaults(home: Path) -> dict[str, Any]:
    if os.environ.get("MO_ACP_DEFAULT_MODEL"):
        lane, lane_env = os.environ["MO_ACP_DEFAULT_MODEL"], "MO_ACP_DEFAULT_MODEL"
    else:
        from mini_ork.acp_orchestrator.config import default_lane

        lane, lane_env = default_lane(home), "MO_ORCHESTRATOR_LANE"
    recipe = os.environ.get("MO_ACP_RECIPE") or "code-fix"
    ws = os.environ.get("MO_WORKSPACE_MODE") or "worktree"
    return S.kv("Thread defaults", [
        ("Orchestrator lane", lane, "purple", lane_env),
        ("Recipe", recipe, "text", "MO_ACP_RECIPE"),
        ("Workspace", _WORKSPACE_LABELS.get(ws, ws), "text", "MO_WORKSPACE_MODE"),
    ], full=True, note="The thread is a control plane — the orchestrator decides per "
                       "request whether to answer, start a run, or edit directly. "
                       "Model is the only picker in the thread; Recipe and Workspace "
                       "are the defaults the orchestrator proposes.")


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
        entries.append((_epoch(t.get("updated_at")) or 0, {"cells": [
            t.get("title") or "mini-ork thread", S.muted("thread"),
            S.mono(S.money(cost) if cost is not None else "—")]}))
    runs, _ = list_runs(home, limit=_HISTORY_RUNS)
    for r in runs:
        entries.append((_epoch(r.get("updated_at") or r.get("created_at")) or 0, {
            "cells": [r.get("title") or r["run_id"], S.muted("run"),
                      S.mono(S.money(r.get("cost_usd")) if r.get("cost_usd") is not None else "—")],
            "do": S.open_run(r["run_id"], r.get("title") or "")}))
    entries.sort(key=lambda e: e[0], reverse=True)
    rows = [e[1] for e in entries] or [[S.muted("No threads or runs yet"), "", ""]]
    return S.table("History · import threads", [S.col(fr=1), S.col(70), S.col(60)],
                   ["title", "source", "cost"], rows,
                   note="Opening one replays it; the next prompt continues the same conversation.")


# ── kickoffs ───────────────────────────────────────────────────────────────

def _front_matter_recipe(text: str) -> str | None:
    if not text.startswith("---"):
        return None
    for line in text.splitlines()[1:40]:
        if line.strip() == "---":
            break
        key, _, value = line.partition(":")
        if key.strip() == "recipe" and value.strip():
            return value.strip().strip("'\"")
    return None


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


def _kickoffs_section(home: Path) -> dict[str, Any]:
    from mini_ork import kickoff_lint

    folder = home / "kickoffs"
    files = sorted((p for p in folder.glob("*.md") if p.is_file()),
                   key=lambda p: p.stat().st_mtime, reverse=True) if folder.is_dir() else []
    runs_by_path: dict[str, dict[str, Any]] = {}
    for r in _rows(home, "SELECT id, status, kickoff_path FROM task_runs "
                         "WHERE kickoff_path LIKE ? ORDER BY created_at DESC", (f"{folder}%",)):
        runs_by_path.setdefault(str(r["kickoff_path"]), r)
    items = []
    for path in files[:_KICKOFF_LIMIT]:
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        declared = _front_matter_recipe(text)
        recipe = declared or os.environ.get("MO_ACP_RECIPE") or "code-fix"
        findings = kickoff_lint.lint(text, project=home.parent, recipe=recipe, home=home)
        errors = [f for f in findings if f.get("sev") == "error"]
        warns = [f for f in findings if f.get("sev") != "error"]
        if errors:
            lint_text, mc = f"lint: {errors[0].get('msg')}", "red"
        elif warns:
            first = str(warns[0].get("msg") or "")
            more = f" (+{len(warns) - 1} more)" if len(warns) > 1 else ""
            lint_text, mc = f"lint: {first}{more}", "yellow"
        else:
            lint_text, mc = "lint clean", "sub"
        parts = ["saved", lint_text, recipe if declared else f"{recipe} (default)"]
        ran = runs_by_path.get(str(path))
        acts = [S.btn("Open", S.open_path(str(path)), "ghost")]
        if ran:
            parts.append(f"ran as {ran['id']} · {ran.get('status') or '—'}")
            acts.append(S.btn("Open run", S.open_run(str(ran["id"]), path.stem)))
        acts.append(S.btn("Start run", S.thread(f"Start a run from the kickoff {path}"), "primary"))
        items.append(S.item(path.name, " · ".join(parts), m="▤", mc=mc, acts=acts))
    if not items:
        items = [S.dot("No saved kickoffs", f"{folder} is empty — /kickoff in a thread drafts one")]
    note = "Lint checks: paths that do not exist, no success section, sections the recipe’s examples have and yours lacks."
    if len(files) > _KICKOFF_LIMIT:
        note += f" Showing the {_KICKOFF_LIMIT} newest of {len(files)}."
    return S.lst("Kickoffs · .mini-ork/kickoffs/", items, full=True, note=note,
                 actions=[S.btn("New kickoff", S.thread("/kickoff "))])


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


def _races(home: Path, now: int) -> dict[str, Any]:
    """Race contestants are runs carrying ``config/agents.race.yaml``; one race =
    the contestants launched within a few minutes on the same kickoff."""
    open_ws: set[str] = set()
    wt_dir = home / "worktrees"
    if wt_dir.is_dir():
        open_ws = {p.stem for p in wt_dir.glob("*.json")}
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
    rows = []
    for race in races:
        lanes = " · ".join(c["lane"] for c in race)
        verified = sum(1 for c in race if c.get("status") == "published")
        open_n = sum(1 for c in race if c["id"] in open_ws)
        cost = sum(float(c.get("cost_usd") or 0) for c in race)
        kept = S.cell(f"undecided · {open_n} open", "yellow") if open_n else S.muted("—")
        title = race[0]["_key"] or race[0]["id"]
        rows.append({"cells": [title, S.muted(lanes),
                               S.cell(f"{verified} / {len(race)}", "green" if verified else "red"),
                               kept, S.mono(S.money(cost)), S.muted(S.age(race[0]["_start"], now))],
                     "do": S.open_run(str(race[0]["id"]), title)})
    if not rows:
        rows = [[S.muted("No races yet — /race <task> runs one task on several models"), "", "", "", "", ""]]
    lanes_env = os.environ.get("MO_RACE_LANES")
    note = (f"Default lanes come from MO_RACE_LANES ({lanes_env})." if lanes_env
            else "Default lanes: MO_RACE_LANES, else the race picks from your configured lanes.")
    return S.table("Races", [S.col(fr=1), S.col(150), S.col(70), S.col(110), S.col(60), S.col(50)],
                   ["task", "lanes", "verified", "kept", "cost", "age"], rows, full=True,
                   note=note + " A race costs about one run per model.",
                   actions=[S.btn("New race", S.thread("/race "))])


def _kickoff_key(home: Path, run: dict[str, Any]) -> str:
    from mini_ork.acp.history import _read_kickoff, _title_from_kickoff

    try:
        text = _read_kickoff(home, str(run["id"]), run.get("kickoff_path"))
    except Exception:  # noqa: BLE001
        return ""
    return _title_from_kickoff(text) or text[:80]


# ── classify & plan ────────────────────────────────────────────────────────

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
            rows = []
            for p in data.get("principals") or []:
                pids = p.get("pids") or []
                rows.append([S.mono(p.get("principal_id") or ""), S.mono(pids[0] if pids else p.get("pgid") or "—"),
                             S.cell(p.get("status") or "—", "green" if p.get("status") == "active" else "sub"),
                             S.mono(p.get("unacked_messages", 0))])
            return S.table("Principals · concord", [S.col(fr=1), S.col(60), S.col(80), S.col(50)],
                           ["principal", "pid", "state", "inbox"],
                           rows or [[S.muted("No active principals"), "", "", ""]],
                           actions=[S.btn("Send message", S.thread("/concord send "))])

        def claims() -> dict[str, Any]:
            data = cn_client.coord_hot_claims() or {}
            rows = [[S.mono(c.get("path") or ""), S.muted(c.get("principal_id") or ""),
                     S.muted(c.get("expires_at") or "")] for c in data.get("claims") or []]
            return S.table("Claims on hot files", [S.col(fr=1), S.col(120), S.col(50)],
                           ["file", "holder", "until"], rows or [[S.muted("No live hot-file claims"), "", ""]])

        def violations() -> dict[str, Any]:
            data = cn_client.coord_owns_violations(0) or {}
            items = [S.bad(f"{v.get('worktree_principal') or '?'} edited {v.get('path') or '?'}",
                           f"caller {v.get('caller_principal') or '—'} · {v.get('ts') or ''}")
                     for v in (data.get("violations") or [])[-8:]]
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
            """
            data = cn_client._coord_request(
                "GET", "/api/v1/coord/overlaps"
            ) or {}
            items = [it for it in (data.get("overlaps") or data.get("items") or [])
                     if isinstance(it, dict)]
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


def _intake(home: Path, errors: dict[str, str]) -> list[dict[str, Any]]:
    """Every source, its trigger, its last sync — and what arrived lately."""
    from mini_ork.intake.poll import source_statuses
    from mini_ork.web.db import resolve_db

    statuses = source_statuses(resolve_db(home))
    rows = []
    for status in statuses:
        state, colour = _SOURCE_STATE[(status.registered, status.enabled)]
        detail = status.skipped_reason or status.sync_error or ""
        rows.append(
            [
                S.cell(status.source, "text", b=True),
                S.cell(state, colour),
                S.cell(_trigger_text(status.trigger), "sub" if not status.trigger.configured
                       else "text"),
                S.cell(status.trigger.recipe or "—", "sub", mono=True),
                S.cell(str(status.mirrored), "sub", mono=True),
                S.cell(status.synced_at or "never", "sub"),
                S.cell(detail, "red" if status.sync_error else "sub"),
            ]
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
    if not any(status.trigger.configured for status in statuses if status.registered):
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
    records = _rows(
        home,
        "SELECT provider, action, issue_uid, thread_id, created_at FROM intake_events "
        "ORDER BY id DESC LIMIT 25",
    )
    rows = [
        [
            S.cell(record.get("provider") or "", "text", b=True),
            S.cell(record.get("action") or "", _ACTION_TONE.get(str(record.get("action")), "sub")),
            S.mono(record.get("issue_uid") or "—"),
            S.mono(record.get("thread_id") or "—"),
            S.cell(str(record.get("created_at") or ""), "sub"),
        ]
        for record in records
    ]
    return S.table(
        "Recent arrivals",
        [S.col(fr=1.0), S.col(90), S.col(fr=2.0), S.col(fr=2.0), S.col(fr=1.4)],
        ["source", "action", "issue", "thread", "when"],
        rows,
        full=True,
        note="The ledger that makes a webhook replay or a re-poll a no-op: one row per "
             "arrival, keyed by provider and delivery id.",
    )


# ── page ───────────────────────────────────────────────────────────────────

def build(home: Path, tab: str | None, args: dict[str, str]) -> dict[str, Any]:
    tab = tab if tab in dict(TABS) else "session"
    now = int(time.time())
    errors: dict[str, str] = {}
    sections: list[dict[str, Any]] = []
    if tab == "session":
        sections += S.guarded(errors, "Thread defaults", lambda: _thread_defaults(home))
        sections += S.guarded(errors, "MCP context server", _mcp_tools)
        sections += S.guarded(errors, "History · import threads", lambda: _history(home))
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
