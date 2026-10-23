"""One run's tab: DAG, overview, agents, learnings, artifacts.

``args["run"]`` is the run id; ``args["node"]`` the DAG node to inspect.
Everything is read from the run's own records — the ``task_runs`` row, its
lifecycle events and ``llm_calls``, the recipe's ``workflow.yaml`` and the
files in ``<home>/runs/<run>``. Nothing here starts a model or touches the
network beyond a loopback check for the web UI.
"""
from __future__ import annotations

import json
import os
import re
import socket
import time
import urllib.parse
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

from mini_ork.ide_pages import _cn as _CN
from mini_ork.ide_pages import orch_tab
from mini_ork.ide_pages import outcome as O
from mini_ork.ide_pages import run_messages
from mini_ork.ide_pages import spec as S
from mini_ork.ide_pages.run_header import build_header, copy_summary_action
from mini_ork.ide_pages.cn_file import (
    _SECTION_BY_FILE as _CN_FILE_BY_FILE,
    _fetch_by_file as _cn_file_fetch_by_file,
    _fetch_summary as _cn_file_fetch_summary,
    _last_ts_epoch as _cn_file_last_ts_epoch,
    _unpack_summary as _cn_file_unpack_summary,
)


def _node_session_id(run: "Run", node: Node) -> str | None:
    """Lazy proxy for :func:`mini_ork.ide_pages.node._resolve_session_id`.

    ``node.py`` imports ``Run`` / ``Node`` from this module at top level,
    which means a top-level ``from mini_ork.ide_pages.node import …``
    here would close the cycle. ``node_artifacts.py`` uses the same
    pattern (lazy import inside a function); this keeps the discipline
    consistent across the run / node pair.
    """
    from mini_ork.ide_pages.node import _resolve_session_id
    return _resolve_session_id(run, node)

TABS = [("dag", "DAG"), ("kickoff", "Kickoff"), ("overview", "Overview"),
        ("orchestrator", "Orchestrator"), ("messages", "Messages"), ("agents", "Agents"),
        ("learnings", "Learnings"), ("artifacts", "Artifacts")]

# The v2 tab bar (``MINI_ORK_IDE_SPEC=2``) follows the Dispatch Console design:
# the DAG leads, the outcome replaces the story, and the questions fold into the
# DAG tab beside the nodes that stopped on them. Old keys still resolve so a
# stale IDE link lands somewhere sensible.
V2_TABS = [("dag", "DAG"), ("outcome", "Outcome"), ("checks", "Checks"),
           ("changes", "Changes"), ("orchestrator", "Orchestrator"),
           ("messages", "Messages"), ("kickoff", "Kickoff"), ("agents", "Agents"),
           ("learnings", "Learnings"), ("artifacts", "Artifacts")]
V2_ALIASES = {"story": "outcome", "graph": "dag", "questions": "dag",
              "overview": "outcome", "verification": "checks",
              "orch": "orchestrator", "concord": "messages"}

# The kickoff text is capped well above the 20 KB the run card shows — the kickoff
# tab is the place to read the whole task brief. Beyond the cap we append a
# trailing marker instead of silently chopping.
_KICKOFF_TAB_CAP = 200_000

# Node types that run a script, not a model, unless llm_calls say otherwise.
_DETERMINISTIC = {"verifier", "publisher", "rollback", "shell", "gate", "transform"}
_REVIEW_TYPES = {"reviewer", "eval", "judge", "lens", "synthesizer"}
_STATE_COLOUR = {"done": "green", "running": "blue", "failed": "red", "pending": "dim",
                 "skipped": "dim"}
_STATE_MARK = {"done": "✓", "running": "●", "failed": "✗", "pending": "○", "skipped": "–"}
_STATE_TEXT = {"done": "done", "running": "working", "failed": "failed", "pending": "pending",
               "skipped": "not run"}
_OUTPUT_LINES = 40
_LINE_CHARS = 220
# The run description in the header: one paragraph, at most this many lines,
# wrapped at this width. The IDE draws one row per line, so the cap is a
# layout contract, not a suggestion.
_DESC_LINES = 5
_DESC_LINE_CHARS = 104
_GREEN = re.compile(r"\bPASS\b|✓|\bapprove|\bpass(ed)?\b|\bok\b", re.I)
_RED = re.compile(r"\bFAIL|\bERROR\b|Traceback|\berror:|✗|\brc=[1-9]", re.I)
_ENGINE_ROOT = Path(__file__).resolve().parents[2]


@dataclass
class Node:
    id: str
    type: str = ""
    role_lane: str = ""      # the agents.yaml role key (worker, reviewer, glm_lens…)
    family: str = ""         # the provider lane that served it (minimax, glm…) or "shell"
    state: str = "pending"
    start: int | None = None
    end: int | None = None
    finish: str = ""
    cost: float = 0.0
    calls: int = 0
    gates: list[str] = field(default_factory=list)
    prompt: str = ""
    in_workflow: bool = True


@dataclass
class Run:
    id: str
    home: Path
    run_dir: Path
    row: dict[str, Any]
    card: dict[str, Any]
    nodes: list[Node]
    cols: list[list[str]]
    calls: list[dict[str, Any]]
    recipe_dir: Path | None
    workspace: Any
    # The recipe's declared edges, verbatim from ``workflow.yaml``. ``cols`` is a
    # projection of these that drops feedback; keeping the raw list is what lets
    # the canvas draw the edges the recipe actually declares.
    edges: list[dict[str, Any]] = field(default_factory=list)


# ── reading ─────────────────────────────────────────────────────────────────

# Re-exported here because every run-page reader already imports from this
# module; the resolver itself lives beside ``_plan_file_name`` and the
# ``task_runs.plan_path`` write it mirrors, so the writer and the readers share
# one definition of "the run's plan".
from mini_ork.cli.plan import plan_artifact  # noqa: E402


def plan_steps(plan: Any) -> list[tuple[str, str, str]]:
    """``(id, title, note)`` for every step a plan declares, whatever its shape.

    Three shapes serve the same role on the run pages: the generic runtime's
    ``steps[]`` / ``decomposition[]``; epic-runner's ``epics[]`` ordered by
    ``waves[][]``; and a plan that carries nothing but an ``objective``. A reader
    that knows one shape draws an empty pane for a perfectly good plan of
    another — the epic-runner node reported "No plan" over eight declared epics
    for exactly that reason. Shape knowledge lives here once; each page decides
    only how a row is drawn.
    """
    if not isinstance(plan, dict):
        return []
    steps = plan.get("steps") or plan.get("decomposition") or []
    rows: list[tuple[str, str, str]] = []
    if isinstance(steps, list):
        for step in steps:
            if not isinstance(step, dict):
                continue
            sid = str(step.get("id") or step.get("name") or step.get("step") or "")
            stype = str(step.get("type") or step.get("node_type") or "")
            lane = str(step.get("lane") or step.get("model_lane") or "")
            note = " · ".join(part for part in (stype, lane) if part)
            # Framework-edit planners routinely name every step after its role
            # (``id == node_type == "implementer"``); rendering that verbatim
            # turns N role-named steps into N identical rows, so a description
            # takes the row over and the role stays in the note.
            title = " ".join(str(step.get("description") or step.get("action") or "").split())
            if len(title) > 100:
                title = title[:97] + "…"
            rows.append((sid, title or sid or stype, note))
    if rows:
        return rows
    epics = plan.get("epics")
    if isinstance(epics, list):
        wave_of: dict[str, int] = {}
        for index, wave in enumerate(plan.get("waves") or []):
            if isinstance(wave, list):
                for epic_id in wave:
                    wave_of.setdefault(str(epic_id), index)
        for epic in epics:
            if not isinstance(epic, dict):
                continue
            epic_id = str(epic.get("id") or "")
            depends = [str(dep) for dep in (epic.get("depends_on") or [])]
            note = " · ".join(part for part in (
                f"wave {wave_of[epic_id]}" if epic_id in wave_of else "",
                "depends on " + ", ".join(depends) if depends else "",
            ) if part)
            rows.append((epic_id,
                         str(epic.get("title") or "").strip() or epic_id,
                         note))
    if rows:
        return rows
    objective = str(plan.get("objective") or "").strip()
    return [("", objective, "")] if objective else []


def _yaml(path: Path) -> dict[str, Any]:
    try:
        import yaml

        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 — a typo in a config must not break the tab
        return {}
    return data if isinstance(data, dict) else {}


def _epoch(value: Any) -> int | None:
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str) and value:
        try:
            return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp())
        except ValueError:
            return None
    return None


def _recipe_dir(home: Path, recipe: str) -> Path | None:
    if not recipe:
        return None
    try:
        from mini_ork.recipes_catalog import find_recipe

        info = find_recipe(recipe, home)
    except Exception:  # noqa: BLE001
        info = None
    return info.path if info is not None else None


def _lane_map(home: Path, run_dir: Path) -> dict[str, str]:
    """role key → first provider lane, from the run's agents.yaml snapshot."""
    for path in (run_dir / "config" / "agents.yaml", home / "config" / "agents.yaml",
                 _ENGINE_ROOT / "config" / "agents.yaml"):
        lanes = _yaml(path).get("lanes")
        if isinstance(lanes, dict) and lanes:
            out = {}
            for role, chain in lanes.items():
                first = str(chain or "").split(",")[0].strip()
                if first:
                    out[str(role)] = first
            return out
    return {}


def _providers(home: Path) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for path in (_ENGINE_ROOT / "config" / "providers.yaml", home / "config" / "providers.yaml"):
        entries = _yaml(path).get("providers")
        if isinstance(entries, dict):
            out.update({str(k): v for k, v in entries.items() if isinstance(v, dict)})
    return out


def _layers(wf_nodes: list[dict[str, Any]], edges: list[Any]) -> list[list[str]]:
    """Columns by longest ``depends_on``/``verifies`` path; escalation-only nodes
    last; ``retries`` (revise-loop) edges are ignored."""
    names = [str(n.get("name")) for n in wf_nodes if n.get("name")]
    known = set(names)
    preds: dict[str, set[str]] = {n: set() for n in names}
    escalated: set[str] = set()
    for e in edges or []:
        if not isinstance(e, dict):
            continue
        src, dst, kind = str(e.get("from") or ""), str(e.get("to") or ""), str(e.get("edge_type") or "")
        if src not in known or dst not in known or src == dst:
            continue
        if kind == "retries":
            # A revise loop (verifier/reviewer → implementer) is feedback, not
            # order: counting it put the implementer after its own reviewer.
            continue
        if kind == "escalates_to":
            escalated.add(dst)
        else:
            preds[dst].add(src)
    for n in wf_nodes:  # node-level depends_on, when a workflow declares it that way
        deps = n.get("depends_on")
        if isinstance(deps, list):
            preds.setdefault(str(n.get("name")), set()).update(d for d in map(str, deps) if d in known)
    layer: dict[str, int] = {}

    def depth(name: str, stack: tuple[str, ...]) -> int:
        if name in layer:
            return layer[name]
        if name in stack:  # a cycle: break it here
            return 0
        ps = preds.get(name) or set()
        layer[name] = 0 if not ps else 1 + max(depth(p, stack + (name,)) for p in ps)
        return layer[name]

    for name in names:
        depth(name, ())
    tail = [n for n in names if n in escalated and not preds.get(n)]
    cols: dict[int, list[str]] = {}
    for name in names:
        if name in tail:
            continue
        cols.setdefault(layer[name], []).append(name)
    out = [cols[k] for k in sorted(cols)]
    if tail:
        out.append(tail)
    return out


def _time_columns(steps: list[dict[str, Any]]) -> list[list[str]]:
    """No workflow: steps that overlap in time share a column."""
    cols: list[list[str]] = []
    col_end: int | None = None
    for s in sorted(steps, key=lambda s: (s.get("start") or 0)):
        start, end = s.get("start") or 0, s.get("end") or s.get("start") or 0
        if cols and col_end is not None and start < col_end:
            cols[-1].append(s["node_id"])
            col_end = min(col_end, end)
        else:
            cols.append([s["node_id"]])
            col_end = end
    return cols


def _match_calls(nodes: dict[str, Node], calls: list[dict[str, Any]], now: int
                 ) -> list[tuple[Node, dict[str, Any]]]:
    """Pair each llm_call with the node whose role lane and time window match.

    The one attribution rule, shared by the cost/call counters and by the
    roster's fallback read — so a node's family cell and its fallback note can
    never disagree about which calls belong to it.
    """
    # Index nodes by role_lane once — without it every call walked all N nodes,
    # turning a long run into O(N·M) when callers typically have hundreds of calls.
    nodes_by_lane_started: dict[str, list[Node]] = {}
    nodes_by_lane_idle: dict[str, list[Node]] = {}
    for node in nodes.values():
        if node.role_lane:
            if node.start is not None:
                nodes_by_lane_started.setdefault(node.role_lane, []).append(node)
            else:
                nodes_by_lane_idle.setdefault(node.role_lane, []).append(node)
    matched: list[tuple[Node, dict[str, Any]]] = []
    for call in calls:
        actor = str(call.get("actor") or "")
        ts_raw = _epoch(call.get("ts"))
        cands = nodes_by_lane_started.get(actor, [])
        best: Node | None = None
        if ts_raw is not None and cands:
            ts = ts_raw  # narrow to int for the lambda capture below
            inside: list[tuple[Node, int]] = []
            for n in cands:
                start = n.start if n.start is not None else 0
                end = n.end if n.end is not None else now
                if start - 2 <= ts <= end + 5:
                    inside.append((n, end))
            if inside:
                best = min(inside, key=lambda pair: abs(pair[1] - ts))[0]
        if best is None:
            # A node the executor ran outside the lifecycle stream (the Python
            # plan runtime's planner): the one never-started node on that lane.
            idle = nodes_by_lane_idle.get(actor, [])
            if len(idle) == 1:
                best = idle[0]
        if best is None:
            continue
        matched.append((best, call))
    return matched


def _attribute_calls(nodes: dict[str, Node], calls: list[dict[str, Any]], now: int) -> None:
    """Give each llm_call to the node whose role lane and time window match."""
    for best, call in _match_calls(nodes, calls, now):
        best.cost += float(call.get("cost_usd") or 0.0)
        best.calls += 1
        model = str(call.get("model_id") or "")
        if model and not best.family:
            best.family = model


def _load(home: Path, run_id: str, *, with_files: bool = True) -> Run | None:
    from mini_ork.acp import fleet
    from mini_ork.web.db import db_for

    card = fleet.run_card(home, run_id, with_files=with_files)
    if card is None:
        return None
    db = db_for(home)
    row = db.row("SELECT * FROM task_runs WHERE id = ?", (run_id,)) or {}
    run_dir = home / "runs" / run_id
    now = int(time.time())
    recipe = str(card.get("recipe") or row.get("recipe") or "")
    recipe_dir = _recipe_dir(home, recipe)
    wf = _yaml(recipe_dir / "workflow.yaml") if recipe_dir else {}
    wf_nodes = [n for n in (wf.get("nodes") or []) if isinstance(n, dict) and n.get("name")]
    lane_map = _lane_map(home, run_dir)

    nodes: dict[str, Node] = {}
    for n in wf_nodes:
        name = str(n["name"])
        gates = n.get("gates") if isinstance(n.get("gates"), list) else []
        prompt = n.get("prompt_ref") or n.get("verifier_ref") or ""
        nodes[name] = Node(id=name, type=str(n.get("type") or ""),
                           role_lane=str(n.get("model_lane") or ""),
                           gates=[str(g) for g in gates],
                           prompt=f"{recipe_dir.name}/{prompt}" if prompt and recipe_dir else "")
    steps = card.get("steps") or []
    for s in steps:
        nid = str(s.get("node_id") or "")
        if not nid:
            continue
        node = nodes.get(nid)
        if node is None:
            node = nodes[nid] = Node(id=nid, in_workflow=False)
        node.type = node.type or str(s.get("node_type") or "")
        node.role_lane = str(s.get("lane") or "") or node.role_lane
        node.state = str(s.get("state") or "running")
        node.start, node.end = s.get("start"), s.get("end")
        node.finish = str(s.get("finish_reason") or "")

    from mini_ork.web.repositories import RunDetailRepository

    try:
        calls = RunDetailRepository(db).fetch_llm_calls_by_run_id(run_id) or []
    except Exception:  # noqa: BLE001 — no llm_calls: nothing to attribute
        calls = []
    _attribute_calls(nodes, calls, now)

    terminal = card.get("status") in ("published", "failed", "rolled_back", "not_needed")
    for node in nodes.values():
        if node.start is None and node.calls:
            node.state = "done"
        elif node.start is None and terminal:
            node.state = "skipped"
        if not node.family:
            if node.type in _DETERMINISTIC or (node.start is not None and node.end is not None
                                              and not node.calls and node.type == "verifier"):
                node.family = "shell"
            else:
                node.family = lane_map.get(node.role_lane, node.role_lane or "shell")

    if wf_nodes:
        cols = _layers(wf_nodes, wf.get("edges") or [])
        placed = {n for c in cols for n in c}
        extra = [n for n in nodes if n not in placed]
        for nid in extra:  # fan-out nodes the workflow did not name: next to their stem
            stem = next((c for c in cols for w in c if len(w) >= 4 and nid.startswith(w)), None)
            if stem is None:
                if not cols:
                    cols.append([])
                stem = cols[-1]
            stem.append(nid)
    else:
        cols = _time_columns([s for s in steps if s.get("node_id")])

    workspace = None
    try:
        from mini_ork import workspaces

        workspace = workspaces.load(home, run_id)
    except Exception:  # noqa: BLE001
        workspace = None
    order = [n for c in cols for n in c]
    edges = [e for e in (wf.get("edges") or []) if isinstance(e, dict)]
    return Run(id=run_id, home=home, run_dir=run_dir, row=row, card=card,
               nodes=[nodes[n] for n in order if n in nodes], cols=cols, calls=calls,
               recipe_dir=recipe_dir, workspace=workspace, edges=edges)


# ── header ──────────────────────────────────────────────────────────────────

def _serve_url(run_id: str) -> str | None:
    try:
        port = int(os.environ.get("MO_SERVE_PORT", "7090") or 7090)
    except ValueError:
        return None
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.15):
            return f"http://127.0.0.1:{port}/runs/{run_id}"
    except OSError:
        return None


def _running(run: Run) -> bool:
    # ``not_needed`` belongs here: a closed run is over, and leaving it out
    # would keep the page polling it as live — and stamp its elapsed time with
    # ``now`` forever — instead of showing the duration it actually ran.
    return run.card.get("status") not in (
        "published", "failed", "rolled_back", "not_needed",
    )


def _state_chip(run: Run) -> dict[str, Any]:
    status = str(run.card.get("status") or "")
    state = (run.card.get("task_state") or {}).get("state") or ""
    verdict = (run.card.get("verdict") or {}).get("verdict") if run.card.get("verdict") else None
    if status == "published":
        return S.chip("published · verified" if verdict == "pass" else "published", "green")
    if status == "rolled_back":
        return S.chip("rolled back", "red")
    if status == "failed":
        return S.chip("failed", "red")
    if status == "not_needed":
        # Neutral, not red: the run is over but nothing went wrong here.
        return S.chip("not needed", "sub")
    if state == "needs_you":
        return S.chip("needs you", "yellow")
    return S.chip("running", "blue")


def _placement_chip(run: Run) -> dict[str, Any] | None:
    """``remote · <environment>`` for a run whose nodes executed off this machine
    (``run_profile.json`` records the resolved placement and ``--env``), so a
    run on a Daytona/E2B/Modal sandbox or a remote node says so in its header."""
    profile = _json_obj(run.run_dir / "run_profile.json") or {}
    if str(profile.get("placement") or "").strip().lower() != "remote":
        return None
    env = str(profile.get("environment") or "").strip()
    return S.chip(f"remote · {env}" if env else "remote", "blue")


def _elapsed(run: Run) -> str:
    start = _epoch(run.row.get("created_at"))
    end = _epoch(run.row.get("ended_at")) if not _running(run) else None
    if end is None and not _running(run):
        end = _epoch(run.row.get("updated_at"))
    if start is None:
        return "—"
    return S.duration((end or int(time.time())) - start)


def _cost(run: Run) -> float:
    total = float(run.card.get("cost_total") or 0.0)
    return total if total > 0 else float(run.row.get("cost_usd") or 0.0)


# ── spend against the caps ──────────────────────────────────────────────────
#
# The page has always shown a cost chip; C16 (the run-page JTBD review) asks for
# the comparison that makes the number actionable — spent against the per-run
# cap, the daily cap, and a projection while the run is still spending. The cap
# comes from the watcher's own resolver so the page and the pause that actually
# stops a run can never disagree.


def _has_cost_data(run: Run) -> bool:
    """Whether the run recorded a cost at all (a real ``$0.00`` still counts)."""
    return run.card.get("cost_total") is not None or run.row.get("cost_usd") is not None


def _per_run_cap() -> float | None:
    """The run's spend cap, from ``watch.cost_cap_usd`` — never re-derived here.

    Imported lazily and looked up on the module at call time: the fallback
    chain (``MO_WATCH_COST_USD`` → ``MO_ORCH_BUDGET_USD`` → 1.0) lives in one
    place, and a broken read returns ``None`` so the caller can show the spent
    number without inventing a cap.
    """
    try:
        from mini_ork.acp_orchestrator import watch

        cap = float(watch.cost_cap_usd())
    except Exception:  # noqa: BLE001 — a broken cap source costs the cap, not the page
        return None
    return cap if cap > 0 else None


def _daily_cap() -> float:
    """``MO_DAILY_BUDGET_USD`` (default 50.0), the scheduler's own knob."""
    try:
        value = float(str(os.environ.get("MO_DAILY_BUDGET_USD", "")).strip())
    except (TypeError, ValueError):
        return 50.0
    return value if value > 0 else 50.0


def _daily_spend(run: Run) -> float | None:
    """Today's spend, only when it is one cheap query on the page's own DB.

    The page already opens ``state.db``; a single ``SUM`` is affordable, while
    anything more (or a closed home, as in a synthetic fixture) is omitted
    rather than guessed.
    """
    try:
        from datetime import datetime as _datetime

        from mini_ork.web.db import db_for

        db = db_for(run.home)
        if not db.has_table("task_runs"):
            return None
        midnight = int(_datetime.now().replace(
            hour=0, minute=0, second=0, microsecond=0).timestamp())
        row = db.row(
            "SELECT COALESCE(SUM(cost_usd), 0) AS n FROM task_runs "
            "WHERE created_at >= ?", (midnight,))
        return float((row or {}).get("n") or 0.0)
    except Exception:  # noqa: BLE001 — an unreadable DB omits the daily number
        return None


def _recent_completion_seconds(run: Run) -> float | None:
    """The recent-completion estimate: mean wall time of the recipe's last runs.

    One query over ``task_runs``; ``None`` when the DB or the history is
    missing, so the projection falls back to the run's own pace.
    """
    try:
        from mini_ork.web.db import db_for

        db = db_for(run.home)
        if not db.has_table("task_runs"):
            return None
        recipe = str(run.card.get("recipe") or run.row.get("recipe") or "")
        row = db.row(
            "SELECT AVG(ended_at - created_at) AS avg_s FROM ("
            " SELECT ended_at, created_at FROM task_runs"
            " WHERE ended_at IS NOT NULL AND created_at IS NOT NULL AND recipe = ?"
            " ORDER BY ended_at DESC LIMIT 20)", (recipe,))
        avg = float((row or {}).get("avg_s") or 0.0)
        return avg if avg > 0 else None
    except Exception:  # noqa: BLE001 — no history means no completion estimate
        return None


def _projected_spend(run: Run, spent: float) -> float | None:
    """``rate × completion estimate`` while a run is still spending.

    Only a ``running`` run that has spent at least a minute's worth of elapsed
    time projects; a finished run has nothing left to project. The estimate is
    the recipe's recent mean duration when it looks further out than what the
    run already took, else the run's own pace doubled (assume it is halfway).
    """
    if not _running(run) or spent <= 0:
        return None
    start = _epoch(run.row.get("created_at"))
    if start is None:
        return None
    elapsed = int(time.time()) - start
    if elapsed < 60:
        return None
    estimate = _recent_completion_seconds(run)
    if estimate is None or estimate <= elapsed:
        estimate = elapsed * 2
    return round(spent / elapsed * estimate, 2)


def _spend_numbers(run: Run) -> dict[str, float] | None:
    """The outcome's ``spend`` block; ``None`` when the run recorded no cost.

    ``remaining`` is clamped at zero, ``daily_cap`` and ``projected`` are
    present only when they can be known — never fabricated.
    """
    if not _has_cost_data(run):
        return None
    spent = _cost(run)
    out: dict[str, float] = {"spent": round(spent, 2)}
    cap = _per_run_cap()
    if cap is not None:
        out["cap"] = round(cap, 2)
        out["remaining"] = round(max(cap - spent, 0.0), 2)
    out["daily_cap"] = round(_daily_cap(), 2)
    projected = _projected_spend(run, spent)
    if projected is not None:
        out["projected"] = projected
    return out


def _spend_section(run: Run) -> dict[str, Any]:
    """One compact Spend section: spent, caps, remaining and the projection.

    Shared verbatim by the Checks and Outcome tabs so the two can never drift.
    Every failure inside is absorbed: a broken cap source drops the cap and
    leaves the spent sentence, never an exception and never a made-up number.
    """
    spent = _cost(run)
    cap = _per_run_cap()
    items: list[dict[str, Any]] = []
    if cap is None:
        items.append(S.dot(f"{S.money(spent)} spent",
                           "per-run cap unavailable — the watcher could not be read"))
    else:
        remaining = max(cap - spent, 0.0)
        sentence = (f"{S.money(spent)} of a {S.money(cap)} per-run cap"
                    f" · {S.money(remaining)} left")
        # Green strictly under the cap; at the cap the sentence already says
        # nothing is left, so it carries the same warning tone as going over.
        items.append(S.bad(sentence) if spent >= cap else S.ok(sentence))
        daily_cap = _daily_cap()
        daily_spent = _daily_spend(run)
        if daily_spent is None:
            items.append(S.dot(f"Daily cap {S.money(daily_cap)}"))
        else:
            items.append(S.dot(
                f"Daily {S.money(daily_spent)} of a {S.money(daily_cap)} cap"))
        projected = _projected_spend(run, spent)
        if projected is not None:
            items.append(S.dot(f"at this rate, ~{S.money(projected)} by the end"))
    return S.lst(
        "Spend", items, full=True,
        note="What this run has spent against its per-run and daily caps.")


def _sub(run: Run) -> str:
    parts = [str(run.card.get("recipe") or ""), str(run.card.get("title") or "")]
    ws = run.workspace
    if ws is not None:
        try:
            shown = str(Path(ws.path).relative_to(run.home.absolute().parent))
        except ValueError:
            shown = str(ws.path)
        parts += [f"worktree {shown}", f"branch {ws.branch}"]
    return " · ".join(p for p in parts if p)


def _stop_kill_actions(run: Run) -> list[dict[str, Any]]:
    """Stop / Kill — the two controls a running run offers.

    ``pending`` is what the button says while the command runs and ``done``
    the toast it shows after; a Stop takes effect at the next node boundary,
    so its toast says *when* rather than pretending the run is gone.
    """
    return [
        S.btn("Stop", S.cli("board", "stop", run.id,
                            confirm="Stop this run after its current node?"), "warn",
              pending="Stopping…", done="Run will stop after this node"),
        S.btn("Kill", S.cli("board", "kill", run.id,
                            confirm=f"Kill {run.id}? SIGTERM, then SIGKILL after 2 s."),
              "danger", pending="Killing…", done="Run killed"),
    ]


def _discard_action(run: Run) -> dict[str, Any] | None:
    """Discard the run's worktree and branch — ``None`` when it has no workspace."""
    if run.workspace is None:
        return None
    return S.btn("Discard", S.cli("board", "discard", run.id,
                                  confirm="Discard this run's worktree and branch?"),
                 "danger")


def _review_actions(run: Run) -> list[dict[str, Any]]:
    """Merge / Discard — the decision a finished run with a worktree needs.

    The toasts name the branch the merge landed in (or that the worktree is
    gone), so the operator does not have to check the tree to know what
    happened.
    """
    if run.workspace is None:
        return []
    base = getattr(run.workspace, "base_branch", "") or "base"
    return [
        S.btn(f"Merge into {base}",
              S.cli("board", "merge", run.id,
                    confirm=f"Merge this run's branch into {base}?"),
              "primary", pending="Merging…", done=f"Merged into {base}"),
        S.btn("Discard", S.cli("board", "discard", run.id,
                               confirm="Discard this run's worktree and branch?"),
              "danger", pending="Discarding…", done="Worktree discarded"),
    ]


def _orchestrator_action(run: Run) -> dict[str, Any]:
    """Open this run's orchestrator thread — the run's own narrator.

    A live run gets the stage-by-stage narration and the stall reports; a
    finished one gets its closing report. Either way the thread is the
    human-in-the-loop surface: type an instruction and it is applied at the
    next seam (`steer`, `answers`, `resume`), never mid-node.
    """
    return S.btn("Orchestrator", S.thread(f"/orchestrator {run.id}"), "ghost")


def _actions(run: Run) -> list[dict[str, Any]]:
    acts: list[dict[str, Any]] = []
    if _running(run):
        acts.extend(_stop_kill_actions(run))
    else:
        acts.extend(_review_actions(run))
        acts.append(S.btn("Certify this change", S.page_link("verify", "certify", run=run.id)))
        acts.append(S.btn("Open run folder", S.reveal(str(run.run_dir)), "ghost"))
    # Last: the orchestrator is a way to *read* the run, so it sits after the
    # controls that change it, in both the running and the finished branch.
    acts.append(_orchestrator_action(run))
    return acts


# r-ide-review: the park / merge-review sections are additive — a page that
# is not parked and not gated by ``before_merge=required`` gets no extra
# section at all. Every helper absorbs its own failures (missing table, DB,
# worktree) so the page build cannot raise on the way past them.


def _is_parked_run(run: Run) -> bool:
    """A run paused for human review (status ``reviewing`` + ``.review-pause``).

    Mirrors the convention in ``cli.executors_handlers.park_reviewing_run``:
    both the run-card status and the dotfile have to point at the same state
    before we ask the operator to act. Either missing means the run is doing
    something else, even if the other signal alone fired.
    """
    status = str(run.card.get("status") or "")
    pause = (run.run_dir / ".review-pause").is_file()
    return status == "reviewing" and pause


def _parked_review_section(run: Run) -> list[dict[str, Any]]:
    """The "Waiting for your review" callout for a parked run, or ``[]``.

    Lists the awaiting nodes (the same rows ``mini-ork approvals list``
    reports) and surfaces the Approve / Request-changes buttons for the
    first awaiting node. A run parked with no inbox rows still gets the
    callout — the operator has to know the run stopped for a human reason,
    even when the node-review table does not say which node that was.
    """
    if not _is_parked_run(run):
        return []
    awaiting = _pending_node_ids(run)
    if awaiting:
        first = awaiting[0]
        actions = [
            S.btn("Approve",
                  S.cli("approvals", "approve", run.id, "--node", first,
                        confirm=(f"Approve {first}'s output and resume the run?"),
                        home=False),
                  "primary"),
            S.btn("Request changes…",
                  S.thread(f"/request-changes {run.id} {first} "),
                  "default"),
        ]
        text = (f"Paused for human review of: "
                f"{', '.join(awaiting)}.")
    else:
        actions = []
        text = ("The run is paused for a human review. "
                "Open its node pages to approve or request changes.")
    return [S.callout(
        "Waiting for your review", text,
        tone="yellow", icon="✋", full=True, actions=actions,
        note="Approve a node to resume, or request changes with a note.")]


def _pending_node_ids(run: Run) -> list[str]:
    """The distinct ``node_id`` values awaiting review for this run.

    Reads the same DB the CLI ``approvals list`` reads — ``mo_inbox_gates``,
    via ``node_review.pending_for_run`` — so the section never disagrees
    with ``mini-ork approvals show <run>``. Errors fall through to ``[]``
    so a missing / unreadable DB never blanks the callout.
    """
    try:
        db_path = str(run.home / "state.db")
        if not Path(db_path).is_file():
            return []
        from mini_ork.gates import node_review
        rows = node_review.pending_for_run(run.id, db_path=db_path)
    except Exception:  # noqa: BLE001 — pending review is optional
        return []
    out: list[str] = []
    for r in rows or []:
        if not isinstance(r, dict):
            continue
        ctx = r.get("context") if isinstance(r.get("context"), dict) else {}
        nid = str(ctx.get("node_id") or "").strip()
        if nid and nid not in out:
            out.append(nid)
    return out


def _awaiting_node_ids(run: Run, *, include_pending_reviews: bool = True) -> list[str]:
    """The nodes a human still has to act on: pending review rows, then the
    node an unanswered ask blocks on, in that order.

    One owner for both readers: the DAG's focus (``_graph``) and the triage's
    node both ask "who is waiting on a person?", so they cannot name different
    nodes for the same run. ``include_pending_reviews`` lets ``_graph`` keep its
    parked-only DB read (a dock render stays DB-free). Every reader fails soft
    to ``[]``: one broken source costs its own rows, never the caller.
    """
    awaiting: list[str] = []
    if include_pending_reviews:
        awaiting.extend(_pending_node_ids(run))
    try:
        asks = run.run_dir / "asks"
        for name in sorted(n for n in os.listdir(asks)
                           if n.startswith("ask-") and n.endswith(".json")):
            ask = _json_obj(asks / name)
            if not isinstance(ask, dict) or str(ask.get("answer") or "").strip():
                continue
            nid = str(ask.get("node_id") or ask.get("node") or "").strip()
            refs = ask.get("blocking_refs")
            if not nid and isinstance(refs, list) and refs:
                nid = str(refs[0]).strip()
            if nid and nid not in awaiting:
                awaiting.append(nid)
                break
    except Exception:  # noqa: BLE001 — asks are optional; never blank the caller
        pass
    return awaiting


def _merge_review_section(run: Run) -> list[dict[str, Any]]:
    """The "Merge review" chip for a run whose policy requires human approval.

    Reads ``review_policy.load_policy`` (the same source the executor uses)
    and only fires when ``before_merge == "required"``. The state and the
    approver come from ``merge_approval.status``. After
    ``merge_approval.approve(...)`` the section still shows, just without
    the Approve button — the operator can still see who cleared the merge.
    """
    if run.workspace is None:
        return []
    try:
        from mini_ork.gates.review_policy import load_policy
        policy = load_policy(home=run.home)
    except Exception:  # noqa: BLE001 — a missing policy is not an error
        return []
    if str(getattr(policy, "before_merge", "off")) != "required":
        return []
    try:
        from mini_ork.gates import merge_approval
        st = merge_approval.status(run.id, db_path=str(run.home / "state.db"))
    except Exception:  # noqa: BLE001 — a missing inbox row is "none"
        st = {"state": "none", "head_sha": "", "approver": "", "inbox_id": None}
    state = str(st.get("state") or "none")
    approver = str(st.get("approver") or "")
    if state == "approved":
        items = [S.ok(f"approved by {approver}" if approver else "approved",
                      "the merge can land")]
        return [S.lst("Merge review", items, full=True,
                      note="Human approval is on file for this run's merge.")]
    label = "awaiting your approval" if state == "none" else f"state: {state}"
    items = [S.warn("Merge review", label)]
    return [S.lst("Merge review", items, full=True,
                  note="A human must approve the merge before this run lands.",
                  actions=[S.btn(
                      "Approve merge",
                      S.cli("approvals", "approve", run.id,
                            confirm=f"Approve merging {run.id} into the base branch?",
                            home=False),
                      "primary")])]


# ── the needs-you lane (v2 only) ───────────────────────────────────────────
#
# One ordered list of every decision on the run that waits on a person — the
# parked review, the open retry gate, a cost pause, a merge approval, an
# unanswered ask — emitted as ONE section at the top of every v2 tab. Empty
# sources contribute nothing: a healthy run gets no section at all, never an
# empty "Needs you" box. Each source is built behind its own try/except so a
# broken part costs that item, never the tab (the same rule the rest of the
# page follows).


def _attention_item(text: str, detail: str, *, item_id: str,
                    actions: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """One lane item: ``id``, text, optional detail and its own buttons.

    ``spec.item``'s shape carries the rendering; ``id`` is additive (an IDE
    that does not know it ignores the key) and stable across renders so the
    lane cannot shuffle under the operator between two builds.
    """
    item = S.item(text, detail, acts=actions or [])
    item["id"] = item_id
    return item


def _parked_attention_items(run: Run) -> list[dict[str, Any]]:
    """The parked run's review decision, or ``[]`` when the run is not parked."""
    if not _is_parked_run(run):
        return []
    awaiting = _pending_node_ids(run)
    if awaiting:
        first = awaiting[0]
        actions = [
            S.btn("Approve",
                  S.cli("approvals", "approve", run.id, "--node", first,
                        confirm=f"Approve {first}'s output and resume the run?",
                        home=False),
                  "primary"),
            S.btn("Request changes…",
                  S.thread(f"/request-changes {run.id} {first} "),
                  "default"),
        ]
        return [_attention_item(
            f"Paused for human review of: {', '.join(awaiting)}.",
            "Approve a node to resume, or request changes with a note.",
            item_id=f"review:{run.id}:{first}", actions=actions)]
    return [_attention_item(
        "The run is paused for a human review. Open its node pages to approve "
        "or request changes.",
        "No node-review row names which node stopped it.",
        item_id=f"review:{run.id}")]


def _gate_attention_items(run: Run, outcome: dict[str, Any]) -> list[dict[str, Any]]:
    """The open retry gate, when the outcome is a needs-you decision."""
    if str(outcome.get("state") or "") != "needs_you":
        return []
    item = O.gate_attention_item(run)
    return [item] if isinstance(item, dict) else []


def _cost_pause_attention_items(run: Run) -> list[dict[str, Any]]:
    """The cost-cap pause, or ``[]`` when the run is not paused on budget."""
    try:
        paused = (run.run_dir / ".cost-pause").is_file()
    except OSError:
        return []
    if not paused:
        return []
    return [_attention_item(
        "Paused at the cost cap",
        "The run stopped spending at its budget cap. Resuming continues from "
        "the same step with the remaining budget.",
        item_id=f"cost-pause:{run.id}",
        actions=[S.btn("Resume",
                       S.cli("board", "resume", run.id,
                             confirm=f"Resume cost-paused {run.id}?"),
                       "primary")])]


def _merge_attention_items(run: Run) -> list[dict[str, Any]]:
    """The human merge approval, while the run still awaits it."""
    if run.workspace is None:
        return []
    try:
        from mini_ork.gates.review_policy import load_policy

        policy = load_policy(home=run.home)
    except Exception:  # noqa: BLE001 — a missing policy is not an error
        return []
    if str(getattr(policy, "before_merge", "off")) != "required":
        return []
    try:
        from mini_ork.gates import merge_approval

        st = merge_approval.status(run.id, db_path=str(run.home / "state.db"))
    except Exception:  # noqa: BLE001 — a missing inbox row is "none"
        st = {"state": "none", "approver": "", "inbox_id": None}
    state = str(st.get("state") or "none")
    if state == "approved":
        # Nothing waits on a person: no item, hence no empty needs-you box.
        return []
    text = "Merge approval needed"
    if state != "none":
        text += f" · {state}"
    return [_attention_item(
        text, "A human must approve the merge before this run lands.",
        item_id=f"merge:{run.id}",
        actions=[S.btn("Approve merge",
                       S.cli("approvals", "approve", run.id,
                             confirm=f"Approve merging {run.id} into the base branch?",
                             home=False),
                       "primary")])]


def _ask_attention_items(run: Run) -> list[dict[str, Any]]:
    """One item per unanswered ``asks/ask-N.json``, in ask order."""
    out: list[dict[str, Any]] = []
    for ask in open_asks(run.run_dir):
        question = str(ask.get("question") or "").strip()
        if not question:
            continue
        detail: list[str] = []
        options = [str(o) for o in ask.get("options") or [] if str(o).strip()]
        if options:
            detail.append("Options: " + ", ".join(options))
        if ask.get("default") is not None:
            detail.append(f"If unanswered, the run uses: “{ask['default']}”")
        detail.append("Answer on the Questions tab; the run continues from where it stopped.")
        out.append(_attention_item(
            question, "\n".join(detail),
            item_id=f"ask:{ask.get('ask_id') or len(out)}",
            actions=[S.btn("Answer",
                           S.page_link("run", "questions", run=run.id),
                           "primary")]))
    return out


def _attention_lane(run: Run, outcome: dict[str, Any]
                    ) -> tuple[list[dict[str, Any]], set[str]]:
    """``(sections, absorbed_callout_titles)`` for the needs-you lane.

    Sources are appended in a fixed order (parked review, open gate, cost
    pause, merge approval, open asks) and each is guarded on its own. When the
    gate item is present the outcome's own callout is "absorbed": the story tab
    drops it so the retry decision renders exactly once. No items → no section.
    """
    items: list[dict[str, Any]] = []
    absorbed: set[str] = set()
    try:
        items.extend(_parked_attention_items(run))
    except Exception:  # noqa: BLE001 — one broken source costs its item
        pass
    try:
        gate_items = _gate_attention_items(run, outcome)
        if gate_items:
            items.extend(gate_items)
            absorbed.add(O.GATE_CALLOUT_TITLE)
    except Exception:  # noqa: BLE001
        pass
    try:
        items.extend(_cost_pause_attention_items(run))
    except Exception:  # noqa: BLE001
        pass
    try:
        items.extend(_merge_attention_items(run))
    except Exception:  # noqa: BLE001
        pass
    try:
        items.extend(_ask_attention_items(run))
    except Exception:  # noqa: BLE001
        pass
    if not items:
        return [], absorbed
    return [S.lst("Needs you", items, full=True,
                  note="Everything on this run that waits on a person.")], absorbed


# ── node output ─────────────────────────────────────────────────────────────

def _tail(path: Path, n: int) -> list[str]:
    try:
        with path.open("rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - 256_000))
            data = f.read().decode("utf-8", "replace")
    except OSError:
        return []
    return data.splitlines()[-n:]


def _live_lines(path: Path) -> list[str]:
    out = []
    for raw in _tail(path, 400):
        try:
            line = json.loads(raw).get("line")
        except (ValueError, AttributeError):
            line = raw
        if not isinstance(line, str) or not line.strip():
            continue
        if line.lstrip().startswith("{") and len(line) > _LINE_CHARS:
            try:  # the agent's final JSON envelope: show its result text
                env = json.loads(line)
                line = str(env.get("result") or env.get("stop_reason") or "")[:_LINE_CHARS]
            except ValueError:
                pass
        out.append(line)
    return out[-_OUTPUT_LINES:]


def _node_output(run: Run, node: Node) -> list[str]:
    d = run.run_dir
    for path in (d / f"verifier_{node.id}.log", d / f"agent-{node.id}.live.jsonl",
                 d / f"impl-{node.id}.log", d / "evidence" / f"{node.id}.log"):
        if not path.is_file():
            continue
        lines = _live_lines(path) if path.suffix == ".jsonl" else _tail(path, _OUTPUT_LINES)
        if lines:
            return lines
    review = d / f"review-{node.id}.json"
    if review.is_file():
        lines = _tail(review, _OUTPUT_LINES)
        if lines:
            return lines
    pat = re.compile(rf"\b{re.escape(node.id)}\b")
    return [ln for ln in _tail(d / "execute.log", 2000) if pat.search(ln)][-_OUTPUT_LINES:]


def verifier_check_counts(run_dir: Path) -> tuple[int, int]:
    """``(passed, total)`` over a run dir's ``verifier_*.json`` envelopes.

    A file that is not a JSON object carrying a ``pass`` key is not a check and
    is skipped, so a stray log fragment does not become a failure. Two pages
    report this number — the run's outcome card and the races panel, where it is
    the input to "keep this one or that one" — and they must agree, so the
    definition lives here beside :func:`_json_obj` rather than in either page.
    """
    passed = total = 0
    for path in sorted(Path(run_dir).glob("verifier_*.json")):
        data = _json_obj(path)
        if not isinstance(data, dict) or "pass" not in data:
            continue
        total += 1
        if data.get("pass"):
            passed += 1
    return passed, total


def _json_obj(path: Path) -> dict[str, Any] | None:
    """A JSON object from ``path`` — verifier files may carry log lines before it."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    try:
        data = json.loads(text)
        return data if isinstance(data, dict) else None
    except ValueError:
        pass
    for line in reversed(text.splitlines()):
        if line.lstrip().startswith("{"):
            try:
                data = json.loads(line)
            except ValueError:
                continue
            if isinstance(data, dict):
                return data
    start = text.find("{")
    if start >= 0:
        try:
            data = json.loads(text[start:])
            return data if isinstance(data, dict) else None
        except ValueError:
            return None
    return None


def _colour(line: str) -> str:
    if _RED.search(line):
        return "red"
    if _GREEN.search(line):
        return "green"
    return "muted"


def _verdict(run: Run, node: Node) -> tuple[str, str]:
    d = run.run_dir
    vj = d / f"verifier_{node.id}.json"
    data = _json_obj(vj) if vj.is_file() else None
    if data is not None and "pass" in data:
        return ("pass", "green") if data.get("pass") else ("fail", "red")
    rv = d / f"review-{node.id}.json"
    if rv.is_file():
        try:
            m = re.search(r'"verdict"\s*:\s*"([^"]+)"', rv.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            m = None
        if m:
            v = m.group(1)
            return v, "green" if v in ("pass", "approve", "approved") else "red"
    if node.state == "done":
        return "ok", "green"
    if node.state == "failed":
        return node.finish or "failed", "red"
    return "—", "sub"


# A review verdict that says the change passed. When the node itself is in a
# failed state, such a file is the earlier attempt's leftover (a re-dispatched
# reviewer whose last attempt died before writing its verdict is the common
# case) — its note explains no failure and must not become the reason.
_PASSING_VERDICTS = frozenset({"pass", "passed", "ok", "approve", "approved", "done"})


def _node_reason(run: Run, node: Node) -> str:
    """One line saying why ``node`` failed or is blocked — ``""`` when nothing does.

    Sources, in the kickoff's order:

    1. the node's verdict note — the first reason text of its
       ``review-<id>.json`` (``reasons`` → ``notes`` → ``findings``), the read
       ``ide_pages.node._overview_reviewer_headline(run_dir, node)`` uses, so the
       DAG and the node tab name one vocabulary rather than two. A review that
       *passed* is skipped for a failed node: it is a leftover from an earlier
       attempt and explains nothing about this failure;
    2. the first ``error`` / ``summary`` line of its ``verifier_<id>.json``;
    3. ``retry-hint.json``'s ``notes[0]`` when its ``failed_node`` is this node;
    4. the node's own ``finish`` (``verdict_fail``, ``error``, ``timeout``) in
       display words, when no artefact explains it.

    Guarded: any exception returns ``""``. The caller omits the key, so a
    dotted or unreadable run dir costs the reason, never the page.
    """
    try:
        d = run.run_dir

        def first_line(value: Any) -> str:
            """The first non-empty line of a str / list / dict payload."""
            if isinstance(value, str):
                text = value
            elif isinstance(value, (list, tuple)):
                text = next((str(v) for v in value if str(v).strip()), "")
            elif isinstance(value, dict):
                text = next((str(value.get(k) or "") for k in
                             ("note", "text", "title", "summary")
                             if str(value.get(k) or "").strip()), "")
            else:
                text = ""
            lines = [ln.strip() for ln in str(text).splitlines() if ln.strip()]
            return lines[0] if lines else ""

        # 1. the node's own verdict note — unless the review passed while the
        #    node is down: that note describes an earlier, successful attempt.
        review = d / f"review-{node.id}.json"
        if review.is_file():
            obj = _json_obj(review)
            if isinstance(obj, dict):
                verdict = str(obj.get("verdict") or "").strip().lower()
                stale_pass = (str(node.state or "") == "failed"
                              and verdict in _PASSING_VERDICTS)
                if not stale_pass:
                    for key in ("reasons", "notes", "findings"):
                        text = first_line(obj.get(key))
                        if text:
                            return _clip(text, 80)

        # 2. the first error / summary line of its verifier json.
        verifier = _json_obj(d / f"verifier_{node.id}.json")
        if isinstance(verifier, dict):
            for key in ("error", "summary", "error_summary"):
                text = first_line(verifier.get(key))
                if text:
                    return _clip(text, 80)

        # 3. retry-hint.json's notes[0], only for the node it names.
        hint = _json_obj(d / "retry-hint.json")
        if isinstance(hint, dict) and str(hint.get("failed_node") or "") == node.id:
            text = first_line(hint.get("notes"))
            if text:
                return _clip(text, 80)

        # 4. the node's own finish reason, in display words (never the raw
        #    enum), when no artefact explains it.
        finish = str(getattr(node, "finish", "") or "").strip()
        if str(node.state or "") == "failed" and finish:
            return _clip(O.display_word(finish), 80)
        return ""
    except Exception:  # noqa: BLE001 — a reason must never blank the page
        return ""


def _wall(node: Node) -> str:
    if node.start is None:
        return "—"
    end = node.end if node.end is not None else int(time.time())
    seconds = max(0, end - node.start)
    # Under a minute the raw seconds are the clearest unit; past that the
    # page's own duration formatter keeps an hour-long node from reading
    # "3725s".
    return f"{seconds}s" if seconds < 60 else S.duration(seconds)


def _clock(epoch: int | None) -> str:
    """When something happened: time of day today, date and time otherwise.

    A hostname-free ``14:03:22`` is unambiguous only while the reader is
    looking at a run from today. Runs are re-opened days later, so a stamp
    from another day carries that day; the year appears only when it differs
    from the current one.
    """
    if not epoch:
        return "—"
    stamp = time.localtime(epoch)
    today = time.localtime()
    if (stamp.tm_year, stamp.tm_yday) == (today.tm_year, today.tm_yday):
        return time.strftime("%H:%M:%S", stamp)
    if stamp.tm_year == today.tm_year:
        return time.strftime("%b %d %H:%M", stamp)
    return time.strftime("%Y-%m-%d %H:%M", stamp)


# ── tabs ────────────────────────────────────────────────────────────────────

def _selected(run: Run, wanted: str | None) -> Node | None:
    if wanted:
        for n in run.nodes:
            if n.id == wanted:
                return n
    for state in ("running", "failed"):
        for n in run.nodes:
            if n.state == state:
                return n
    done = [n for n in run.nodes if n.state == "done"]
    if done:
        return done[-1]
    return run.nodes[0] if run.nodes else None


# Which kind survives when two edges join the same pair. Mirrors
# ``ui/src/thumb.rs``'s ``edge_priority`` so the board thumbnails and the full
# canvas name the same relationship for the same pair.
_EDGE_KIND_ORDER = ("verifies", "escalates_to", "retries", "supplies_context_to", "depends_on")


def _edge_rank(kind: str) -> int:
    try:
        return _EDGE_KIND_ORDER.index(kind)
    except ValueError:
        return len(_EDGE_KIND_ORDER)


def _graph_edges(run: Run) -> list[dict[str, Any]]:
    """The recipe's edges, shaped for the canvas.

    Only edges whose endpoints reached the drawing survive: a runtime-only node
    is named by no edge, and an edge to a node the run never placed would
    dangle. ``feedback`` marks the edges that loop back — ``_layers`` keeps those
    out of the column ordering on purpose, so the canvas draws them as arcs
    rather than pretending they are forward steps.
    """
    col_of: dict[str, int] = {}
    for index, col in enumerate(run.cols):
        for nid in col:
            col_of[nid] = index
    best: dict[tuple[str, str], dict[str, Any]] = {}
    for edge in run.edges:
        src, dst = str(edge.get("from") or ""), str(edge.get("to") or "")
        if not src or not dst or src == dst:
            continue
        if src not in col_of or dst not in col_of:
            continue
        kind = str(edge.get("edge_type") or "depends_on")
        recursive = bool(edge.get("recursive"))
        item = {
            "from": src,
            "to": dst,
            "edge_type": kind,
            "condition": str(edge.get("condition") or ""),
            "recursive": recursive,
            "feedback": recursive or kind == "retries" or col_of[dst] <= col_of[src],
        }
        prev = best.get((src, dst))
        if prev is None or _edge_rank(kind) < _edge_rank(str(prev["edge_type"])):
            best[(src, dst)] = item
    return sorted(best.values(), key=lambda e: (col_of[e["from"]], e["from"], col_of[e["to"]], e["to"]))


def _graph(run: Run, wanted: str | None = None) -> dict[str, Any]:
    """The run's node graph — the DAG columns plus the header the IDE draws.

    ``_dag_tab``'s ``dag`` section and the page's top-level ``graph`` key both
    build from here, so they cannot drift::

        {"cols": [[node_dict]], "heads": [str], "edges": [edge_dict],
         "run_title": str, "recipe": str, "state": <outcome state word>,
         "focus": <node id>}

    ``focus`` is the node the DAG strip opens on — the first failed node in
    DAG column order, else the node a human is holding, else the first
    running node, else the last that finished. The key is omitted only when
    the run has no nodes: there is nothing to open on, and a definite id
    would be a lie. A failed or blocked ``node_dict`` also carries
    ``reason`` — one line (≤ 80 chars) saying why — and omits that key when
    no source has text.
    """
    sel = _selected(run, wanted)
    by_id = {n.id: n for n in run.nodes}

    # The nodes a human still has to act on: a parked run's pending review
    # rows (the same DB read the parked callout makes — skipped unless the
    # run is actually parked, so a dock render stays DB-free), then an
    # unanswered ask's node. One helper with the triage's own reader, so the
    # DAG focus and the triage never name different waiting nodes.
    awaiting = _awaiting_node_ids(run, include_pending_reviews=_is_parked_run(run))
    holding = set(awaiting)

    def state_of(n: Node) -> str:
        """The node's story state — a skipped finish overrides ``state``."""
        if str(n.finish or "").strip().lower() == "skipped":
            return "skipped"
        return str(n.state or "").strip().lower() or "pending"

    cols: list[list[dict[str, Any]]] = []
    for c in run.cols:
        col = []
        for nid in c:
            n = by_id.get(nid)
            if n is None:
                continue
            cost = S.money(n.cost) if n.cost else ""
            node_dict = S.dag_node(n.id, n.id, n.family, n.state, cost=cost,
                                   selected=sel is not None and n.id == sel.id,
                                   do=S.set_args(node=n.id))
            # ``node.py``'s stream view reads ``role``/``dur``/``gates`` off
            # the same dict the IDE draws from — augment in place rather
            # than editing ``spec.dag_node``'s positional signature (the
            # additive spec.py change was rejected to keep the diff inside
            # the kickoff's 4-file scope).
            #
            # A deterministic node ran on no model, so ``role_lane`` — the lane
            # agents.yaml *declares* for its role — names nothing that served
            # it. The IDE prints this field beside the serving lane
            # (``lane_label(family) · role``), so a shell node would read
            # "command · minimaxm3_lens": a MiniMax node, which it is not. The
            # declaration still names the *stage*, so it moves to
            # ``stage_lane`` for ``_head_for`` instead of being dropped here.
            node_dict["role"] = "" if n.family == "shell" else n.role_lane
            node_dict["stage_lane"] = n.role_lane
            node_dict["dur"] = _wall(n)
            node_dict["gates"] = ", ".join(n.gates) or "—"
            # The kickoff: only a node that failed or is blocked gets a
            # reason, and only when a source actually has text.
            if state_of(n) == "failed" or n.id in holding:
                reason = _node_reason(run, n)
                if reason:
                    node_dict["reason"] = reason
            col.append(node_dict)
        if col:
            cols.append(col)
    graph = {
        "cols": cols,
        "heads": [_head_for(col, idx) for idx, col in enumerate(cols)],
        "edges": _graph_edges(run),
        "run_title": str(run.card.get("title") or ""),
        "recipe": str(run.card.get("recipe") or run.row.get("recipe") or ""),
        "state": O.state_word(run),
    }
    # "DAG column order" is the order the columns draw in, not ``run.nodes``:
    # the failed node of ``hr-smoke-d-191036`` ran *before* the nodes that
    # finished after it, and only the column order says so.
    ordered: list[Node] = []
    seen: set[str] = set()
    for c in run.cols:
        for nid in c:
            n = by_id.get(nid)
            if n is not None and n.id not in seen:
                seen.add(n.id)
                ordered.append(n)
    for n in run.nodes:  # a node the workflow did not place still counts
        if n.id not in seen:
            seen.add(n.id)
            ordered.append(n)
    if ordered:
        focus = ""
        for n in ordered:  # 1. the first failed node
            if state_of(n) == "failed":
                focus = n.id
                break
        if not focus and awaiting:  # 2. a node awaiting a human
            rank = {n.id: i for i, n in enumerate(ordered)}
            placed = [(rank[nid], nid) for nid in awaiting if nid in rank]
            focus = min(placed)[1] if placed else awaiting[0]
        if not focus:  # 3. the first running node
            for n in ordered:
                if state_of(n) == "running":
                    focus = n.id
                    break
        if not focus:  # 4. the last node that finished (a skipped one never ran)
            for n in reversed(ordered):
                if state_of(n) == "done":
                    focus = n.id
                    break
        graph["focus"] = focus or ordered[-1].id
    return graph


def _dag_tab(run: Run, wanted: str | None, *, select: bool = False,
             outcome: dict[str, Any] | None = None,
             callout_actions: Iterable[dict[str, Any]] | None = None
             ) -> list[dict[str, Any]]:
    """The redesign's DAG tab: outcome, questions, graph, the selected node,
    the coalition and the verifier rows — in the order the design reads.

    ``select`` is the run page's own DAG: a node click picks it in place
    (the IDE re-renders with ``args.node``) instead of opening the node tab.
    Level 1 keeps the old behaviour and the old section order.

    ``callout_actions`` is the outcome's own buttons already filtered by the
    caller: the header sits above every tab and draws up to two controls, so
    the page subtracts those before handing the rest to the callout body — the
    design's ``bodyActs``.
    """
    errors: dict[str, str] = {}
    sel = _selected(run, wanted)
    resolved_outcome = outcome if outcome is not None else O.resolve(run)
    drawn_actions = list(callout_actions or ())

    def outcome_block() -> dict[str, Any]:
        actions: list[dict[str, Any]] = list(drawn_actions)
        return S.callout(resolved_outcome.get("text") or "",
                         resolved_outcome.get("detail") or "",
                         tone=resolved_outcome.get("tone") or "muted",
                         icon=resolved_outcome.get("icon") or "",
                         meta=resolved_outcome.get("counts") or (),
                         actions=actions, full=True)

    def questions() -> list[dict[str, Any]]:
        # The Questions tab folded in here: a stopped run says what it waits
        # for beside the graph it stopped on, and the composer answers it in
        # place. A run that never asked gets no empty box.
        if not open_asks(run.run_dir):
            return []
        return _questions_tab(run)

    def build_dag() -> dict[str, Any]:
        if not run.nodes:
            return S.lst("DAG", [S.dot("No nodes recorded yet",
                                       "The run has not started a node and its recipe has no workflow.yaml.")],
                         full=True)
        graph = _graph(run, wanted)
        recipe = str(run.card.get("recipe") or run.row.get("recipe") or "")
        sec = S.dag("DAG", graph["cols"],
                    legend=("depends_on → · verifiers check the node before them · "
                            "rollback runs on escalates_to after a failure · "
                            "click a node to inspect it"), full=True,
                    sub=" · ".join(x for x in (recipe, f"{len(run.nodes)} nodes") if x))
        # ``spec.dag`` forwards ``**opt`` into ``_section`` which has no
        # ``heads`` kwarg — mutate the returned dict in place so the kickoff's
        # "stage label per column" reaches the IDE without a spec.py edit.
        sec["heads"] = graph["heads"]
        sec["edges"] = graph["edges"]
        sec["run_title"] = graph["run_title"]
        sec["recipe"] = graph["recipe"]
        if select:
            # The IDE's marker: a click here selects in-page. The node's own
            # tab stays one button away, from the detail's "Open node".
            sec["select"] = True
        return sec

    def run_overview() -> list[dict[str, Any]]:
        """The run's own state — what the DAG tab opens on until a node is clicked.

        ``_selected`` answers a different question: which node the *canvas*
        should point at (first failed, else the last that finished). Using that
        answer to fill this box described the publisher on every finished run —
        a node whose own cost, start and end say nothing about the run. The box
        is about the run until a click makes it about a node.
        """
        live = _running(run)
        state = str(resolved_outcome.get("state") or "")
        # Cost, progress, checks and recipe are already on the header chips and
        # in the callout above the graph. This box answers the one question
        # nothing else on the tab does: when did it run.
        facts: list[tuple[Any, ...]] = [
            ("Status", {"done": "done", "running": "running", "failed": "failed",
                        "needs_you": "needs you"}.get(state, state or "—"),
             str(resolved_outcome.get("tone") or "text")),
            ("Started", _clock(_epoch(run.row.get("created_at")))),
            ("Ended", "running" if live else _clock(
                _epoch(run.row.get("ended_at")) or _epoch(run.row.get("updated_at")))),
        ]
        sub = " · ".join(("run", "overall", "live" if live else "finished"))
        return [S.kv("Run", facts, sub=sub, cols=3, big=True, full=True)]

    def node_detail(node: Node) -> list[dict[str, Any]]:
        # Lazy: ``node.py`` imports this module (same dance as ``run_story``).
        from mini_ork.ide_pages.node import _kv_items, _resolve_prompt_file

        items = _kv_items(node, _wall(node), run)
        by_key = {item["k"]: item for item in items}
        facts: list[tuple[Any, ...]] = [
            ("Status", _STATE_TEXT.get(node.state, node.state),
             _STATE_COLOUR.get(node.state, "sub")),
            ("Lane", by_key["Lane"]["v"], by_key["Lane"]["c"]),
            ("Provider", by_key["Provider"]["v"], by_key["Provider"]["c"]),
            ("Cost", by_key["Cost"]["v"], by_key["Cost"]["c"]),
            ("Started", _clock(node.start)),
            ("Ended", _clock(node.end)),
            ("Duration", _wall(node)),
            ("Tokens", by_key["Tokens"]["v"], by_key["Tokens"]["c"]),
            ("Skills", by_key["Skills"]["v"], by_key["Skills"]["c"]),
            ("Gates", by_key["Gates"]["v"], by_key["Gates"]["c"]),
        ]
        if node.finish and node.finish != "done":
            facts.append(("Finish", node.finish, "red" if node.state == "failed" else "text"))
        if node.prompt:
            facts.append(("Prompt", node.prompt, "muted"))
        stage = next((i for i, column in enumerate(run.cols, 1) if node.id in column), None)
        sub = " · ".join(x for x in (
            "node",
            f"stage {stage} of {len(run.cols)}" if stage else "",
            node.family) if x)
        actions = [S.btn("Open node", S.page_link("run", "dag", run=run.id, node=node.id),
                         "primary"),
                   # An empty ``set`` value removes the key (see the IDE's
                   # ``set_args``), so the box can go back to describing the run.
                   S.btn("Run overview", {"set": {"node": ""}}, "ghost")]
        prompt_path = _resolve_prompt_file(node, run.recipe_dir)
        if prompt_path:
            actions.append(S.btn("Prompt", S.open_path(prompt_path), "ghost"))
        sections = [S.kv(node.id, facts, sub=sub, cols=3, big=True, actions=actions, full=True)]
        lines = [(ln[:_LINE_CHARS], _colour(ln)) for ln in _node_output(run, node)]
        if lines:
            sections.append(S.code(f"Transcript · {node.id}", lines, full=True))
        return sections

    def verifiers() -> dict[str, Any]:
        rows = [n for n in run.nodes
                if n.type == "verifier" or n.type in _REVIEW_TYPES
                or "verifier" in n.id or "review" in n.id or "lens" in n.id]
        if not rows:
            return S.lst("Verifiers", [S.dot("No verifiers have run",
                                             "Zero verifiers would read vacuous, never success.")],
                         full=True)
        items, failed, pending = [], 0, 0
        for n in rows:
            verdict, colour = _verdict(run, n)
            if colour == "red":
                failed += 1
            if n.state in ("pending", "skipped"):
                pending += 1
            mark = {"done": "✓", "running": "●", "failed": "✗"}.get(n.state, "○")
            sub = " · ".join(x for x in (verdict, _node_reason(run, n)) if x)
            items.append(S.item(n.id, sub, m=mark, mc=colour, when=_wall(n)))
        # "all pass" is a claim about every row: a half-run set still waiting
        # on its last verifiers must not borrow the sentence a green run earned.
        if failed:
            title = f"Verifiers · {failed} failed"
        elif pending:
            title = f"Verifiers · {pending} not run"
        else:
            title = "Verifiers · all pass"
        return S.lst(title, items, full=True,
                     sub=f"{len(rows)} verifier/review node{'s' if len(rows) != 1 else ''}")

    def build_coalition() -> dict[str, Any]:
        fams: dict[str, int] = {}
        for n in run.nodes:
            if n.state == "skipped":
                continue
            fams[n.family] = fams.get(n.family, 0) + 1
        model_fams = [f for f in fams if f != "shell"]
        reviewers = [n for n in run.nodes if n.state != "skipped" and n.family != "shell"
                     and (n.type in _REVIEW_TYPES or "review" in n.id or "lens" in n.id)]
        rev_fams: dict[str, int] = {}
        for n in reviewers:
            rev_fams[n.family] = rev_fams.get(n.family, 0) + 1
        if not reviewers:
            verdict, note = "no review panel", "This run has no reviewer or lens nodes."
        elif len(rev_fams) == 1:
            fam = next(iter(rev_fams))
            verdict = "single family"
            note = (f"Every review node ran on {fam}: agreement here is one model's view sampled "
                    f"{len(reviewers)} time{'s' if len(reviewers) != 1 else ''}.")
        else:
            top, k = max(rev_fams.items(), key=lambda kv: kv[1])
            if k * 2 > len(reviewers):
                verdict, note = "mixed", f"{top} holds {k} of {len(reviewers)} review nodes."
            else:
                verdict, note = "heterogeneous", "No family holds a majority of the review nodes."
        counted = sum(fams.values())
        title = (f"Coalition · {verdict} — {len(model_fams)} model famil"
                 f"{'y' if len(model_fams) == 1 else 'ies'} across {counted} nodes")
        pills = [(f"{f} ×{k}" if k > 1 else f, f"fam:{f}") for f, k in
                 sorted(fams.items(), key=lambda kv: (-kv[1], kv[0]))]
        return S.pills(title, pills, full=True, note=note)

    if select:
        # A click is the only thing that makes this tab about a node: ``wanted``
        # names it, so the detail follows the click and never a guess.
        explicit = next((n for n in run.nodes if n.id == wanted), None) if wanted else None

        def detail() -> list[dict[str, Any]]:
            return node_detail(explicit) if explicit is not None else run_overview()

        # The redesign's order: what the run decided, what it waits for, the
        # graph, the run's own state (or the clicked node), then who checked whom.
        return (S.guarded(errors, "Outcome", outcome_block)
                + S.guarded(errors, "Questions", questions)
                + S.guarded(errors, "DAG", build_dag)
                + S.guarded(errors, "Node", detail)
                + S.guarded(errors, "Coalition", build_coalition)
                + S.guarded(errors, "Verifiers", verifiers))

    # Level 1: the DAG section stays first (the IDE draws it as the page's
    # strip, and a test pins `sections[0]`); totals follow so the page still
    # answers "for how much". The roster lives on Agents only — one table,
    # one place, instead of the same rows drawn on two tabs.
    def totals() -> dict[str, Any]:
        counts: dict[str, int] = {}
        for n in run.nodes:
            counts[n.state] = counts.get(n.state, 0) + 1
        states = ", ".join(f"{k} {counts[k]}" for k in
                           ("done", "running", "failed", "skipped", "pending") if k in counts)
        return S.kv("Run totals", [
            ("Cost", S.money(_cost(run))),
            ("Wall time", _elapsed(run)),
            ("Nodes", f"{len(run.nodes)} · {states}" if run.nodes else "no nodes"),
            ("Recipe", str(run.card.get("recipe") or run.row.get("recipe") or "—")),
        ], full=True)

    def build_inspector() -> dict[str, Any]:
        if sel is None:
            return S.lst("", [S.dot("Nothing to inspect yet")], full=True)
        lines = [(ln[:_LINE_CHARS], _colour(ln)) for ln in _node_output(run, sel)]
        prov = _providers(run.home).get(sel.family, {})
        provider = ("deterministic · no model" if sel.family == "shell"
                    else " · ".join(p for p in (str(prov.get("model") or ""), str(prov.get("kind") or "")) if p)
                    or sel.family)
        items = [
            # A shell node ran on no lane, so the lane its role declares would
            # read here as the lane that served it — "shell · minimaxm3_lens".
            ("Lane", f"{sel.family} · {sel.role_lane}"
             if sel.role_lane and sel.role_lane != sel.family and sel.family != "shell"
             else sel.family, f"fam:{sel.family}"),
            ("Provider", provider),
            ("Gates", ", ".join(sel.gates) or "—"),
            ("Cost", S.money(sel.cost) + (f" · {sel.calls} call{'s' if sel.calls != 1 else ''}" if sel.calls else "")),
            ("Wall time", _wall(sel)),
        ]
        if sel.finish and sel.finish != "done":
            items.append(("Finish", sel.finish, "red" if sel.state == "failed" else "text"))
        if sel.prompt:
            items.append(("Prompt", sel.prompt, "muted"))
        return S.inspector(sel.id, sel.type or "node", _STATE_TEXT.get(sel.state, sel.state),
                           lines, items, full=True)

    return (S.guarded(errors, "DAG", build_dag)
            + S.guarded(errors, "Totals", totals)
            + S.guarded(errors, "Inspector", build_inspector)
            + S.guarded(errors, "Coalition", build_coalition))


def _head_for(col: list[dict[str, Any]], idx: int) -> str:
    """One stage label per DAG column: the lane its nodes declare, else ``stage N``.

    Read from ``stage_lane`` rather than ``role`` because ``role`` is blank on
    a deterministic node — the label names the *stage*, not who served the
    node, and a column may hold nothing but shell steps.
    """
    for node_dict in col:
        lane = str(node_dict.get("stage_lane") or node_dict.get("role") or "")
        if lane:
            return lane
    return f"stage {idx}"


def _overview_tab(run: Run, *, include_retry: bool = True) -> list[dict[str, Any]]:
    errors: dict[str, str] = {}
    d = run.run_dir

    def inputs() -> dict[str, Any]:
        items = []
        kick = run.row.get("kickoff_path") or ""
        if kick and Path(kick).is_file():
            items.append(S.dot(Path(kick).name, "What the run was asked to do · " + _size(Path(kick)),
                               [S.btn("Open", S.open_path(kick), "ghost")]))
        plan_path = plan_artifact(run.run_dir, str(run.row.get("plan_path") or ""))
        for name, what in ((plan_path.name if plan_path else "plan.json",
                            "The planner's objective and steps"),
                           ("run_profile.json", "Task class and risk the classifier assigned"),
                           # Audit only: no model reads this file. What the planner
                           # actually received is learned/planner.md.
                           ("context-pack.json", "Memory available at plan time (audit only, not sent to the planner)"),
                           ("context-pack.v2.json", "Context selected for this run's files (context v2)"),
                           ("learned/planner.md", "Exactly what the planner was given")):
            p = d / name
            if name == "context-pack.json" and (d / "context-pack.v2.json").is_file():
                # v2 is the pack the planner actually consumed; the v1 file is
                # still in the folder but listing both reads as two inputs.
                continue
            if p.is_file():
                items.append(S.dot(name, f"{what} · {_size(p)}", [S.btn("Open", S.open_path(str(p)), "ghost")]))
        if not items:
            items = [S.dot("No inputs recorded", "The run directory has no kickoff, plan or context pack.")]
        return S.lst("Run inputs", items, note="What it knew going in.")

    def retry_section() -> dict[str, Any] | list[dict[str, Any]]:
        # Deferred import keeps a cold-path ``run_page`` import from pulling
        # in ``retry_hint`` (which imports ``db_for`` + ``recipes_catalog``).
        try:
            from mini_ork.recovery import retry_hint
        except Exception:  # noqa: BLE001 — a missing import must not blank the page
            return []
        if str(run.card.get("status") or "") not in ("failed", "rolled_back"):
            return []
        try:
            hint = retry_hint.load_or_compute(run.home, run.id, write=False)
        except Exception:  # noqa: BLE001 — hint crash drops the section, never the page
            return []
        if not isinstance(hint, dict):
            return []
        needs_change = hint.get("needs_change") if isinstance(hint.get("needs_change"), dict) else None
        from_node = str(hint.get("from_node") or hint.get("failed_node") or "")
        strategy = str(hint.get("strategy") or "")
        # Cost-pause first — the hint carries a ``needs_change: budget`` block,
        # but the operator action is to lift the cost cap (resume), not to ack
        # a code change. Treat it as the dedicated branch it is.
        if hint.get("retryable") and strategy == "resume-cost":
            # Name the cap that was hit, from the same watcher source the pause
            # uses; the button and its confirmation stay byte-identical.
            cap = _per_run_cap()
            cap_text = f" (the {S.money(cap)} per-run cap)" if cap is not None else ""
            return S.lst("Retry", [S.ok(
                f"Resume this cost-paused run with `mini-ork resume {run.id}`{cap_text}.")],
                         full=True,
                         actions=[S.btn("Resume",
                                        S.cli("board", "retry", run.id,
                                              confirm=f"Resume cost-paused {run.id}?"),
                                        "primary")])
        if needs_change is not None:
            kind = str(needs_change.get("kind") or "?")
            summary = str(needs_change.get("summary") or "")
            detail = str(needs_change.get("detail") or "")
            evidence = str(needs_change.get("evidence") or "")
            notes = [str(n) for n in (hint.get("notes") or []) if isinstance(n, str)]
            if not hint.get("retryable"):
                # Case 3 (or any case where the change itself must be
                # revised) — surface the judgement but offer no button so
                # the operator reads the detail before re-running.
                items = [S.bad(f"Can't be resumed: {summary}" if summary
                               else "Can't be resumed")]
                for note in notes:
                    items.append(S.dot(note, mc="muted"))
                head = S.lst("Retry", items, full=True,
                             note="No retry button — the hint says the change itself must be revised.")
                if detail:
                    return [head, S.code("Detail",
                                         [(ln, "body") for ln in detail.splitlines() or [detail]],
                                         full=True)]
                return head
            items: list[dict[str, Any]] = [S.warn(
                f"Needs a change before retrying ({kind}): {summary}")]
            for note in notes:
                items.append(S.dot(note, mc="muted"))
            actions: list[dict[str, Any]] = []
            if evidence and Path(evidence).exists():
                actions.append(S.btn("Open evidence", S.open_path(evidence), "ghost"))
            actions.append(S.btn("I fixed it — retry",
                                 S.cli("board", "retry", run.id, "--ack-change",
                                       confirm=f"Retry {run.id} after the change?"), "primary"))
            head = S.lst("Retry", items, full=True, actions=actions,
                         note="The hint blocked spawning until a change is acknowledged.")
            if detail:
                return [head, S.code("Detail",
                                     [(ln, "body") for ln in detail.splitlines() or [detail]],
                                     full=True)]
            return head
        if hint.get("retryable"):
            msg = f"Can resume from {from_node}, reusing finished nodes" if from_node \
                else "Can resume this run, reusing finished nodes"
            return S.lst("Retry", [S.ok(msg)], full=True,
                         actions=[S.btn("↻ Retry",
                                        S.cli("board", "retry", run.id,
                                              confirm=f"Retry from {from_node}?"
                                              if from_node else f"Retry {run.id}?"),
                                        "primary")])
        # not retryable
        summary = (needs_change or {}).get("summary") if needs_change else "Not retryable"
        detail = (needs_change or {}).get("detail") if needs_change else ""
        items = [S.bad(str(summary) if summary else "Can't be resumed")]
        head = S.lst("Retry", items, full=True,
                     note="No retry button — the hint says the change itself must be revised.")
        if detail:
            return [head, S.code("Detail",
                                 [(ln, "body") for ln in detail.splitlines() or [detail]],
                                 full=True)]
        return head

    def correlation() -> dict[str, Any]:
        from mini_ork.web.db import db_for

        db = db_for(run.home)
        events = db.row("SELECT COUNT(*) AS n FROM run_events WHERE run_id = ?", (run.id,)) \
            if db.has_table("run_events") else None
        trace = run.row.get("trace_id") or ""
        return S.kv("Correlation", [
            ("trace_id", trace or "—", "text" if trace else "sub"),
            ("events", (events or {}).get("n", 0)),
            ("LLM calls", len(run.calls)),
            ("linked by", "run id" if not trace else "trace_id", "green"),
        ])

    def recent() -> dict[str, Any]:
        return S.code("Recent events",
                      recent_lines(run) or [("No events recorded for this run.", "dim")])

    def orchestrator() -> list[dict[str, Any]]:
        """A pointer to the Orchestrator tab, which owns the whole layer.

        The activity feed, the steering queue and the watcher's state moved to
        their own tab; Overview keeps one link so the layer stays discoverable
        from the page a reader arrives on.
        """
        return [S.lst("Orchestrator", [S.dot(
            "The orchestrator has its own tab",
            "Activity, the steering queue and the watcher's state live there.",
            mc="sub",
            acts=[S.btn("Open Orchestrator",
                        S.page_link("run", "orchestrator", run=run.id), "ghost")])],
            full=True)]

    def files() -> list[dict[str, Any]]:
        # Resolve the per-file session-history once per render. The snapshot
        # path keeps the finished run from re-fetching CN for the line; live
        # runs go through the section cache so a slow CN still renders the
        # one-line state under each row rather than a blank line.
        snapshot = _read_run_memory_snapshot(run)
        snapshot_files: dict[str, dict[str, Any]] = {}
        if snapshot is not None and isinstance(snapshot.get("files"), list):
            for entry in snapshot["files"]:
                if not isinstance(entry, dict):
                    continue
                p = str(entry.get("path") or "")
                if p:
                    snapshot_files[p] = entry
        items = []
        file_history_items: list[dict[str, Any]] = []
        for f in _card_files(run):
            acts = [S.btn("Open", S.open_path(f["abs"]), "ghost")] if f.get("abs") else []
            items.append(S.item(f["path"], f"+{f['added']} −{f['removed']}", m="±", mc="blue",
                                is_mono=True, acts=acts))
            history = snapshot_files.get(str(f.get("path") or ""))
            if history is None:
                # Live fetch (the snapshot path doesn't apply to live runs).
                sess = _cn_sessions_for_path(str(f.get("path") or ""))
                matches = sess.get("matches") if isinstance(sess, dict) else None
                if isinstance(matches, list) and matches:
                    history = {"path": str(f.get("path") or ""),
                               "session_ids": [str(m.get("session_id")) for m in matches
                                               if isinstance(m, dict)
                                               and str(m.get("session_id") or "").strip()],
                               "last_ts": max((int(m.get("last_ts") or 0)
                                              for m in matches if isinstance(m, dict)), default=0)}
            if history and isinstance(history, dict) and history.get("session_ids"):
                sids = [str(s) for s in (history.get("session_ids") or []) if str(s)]
                last_ts = int(history.get("last_ts") or 0)
                age = S.age(last_ts, int(time.time())) if last_ts else "—"
                open_acts = [S.btn("Open", S.page_link("cn_session", id=sid), "ghost")
                             for sid in sids[:3]]
                file_history_items.append(S.item(
                    str(f.get("path") or ""),
                    f"{len(sids)} session{'s' if len(sids) != 1 else ''} · last touch {age}",
                    m="•", mc="sub", acts=open_acts,
                ))
        if not items:
            items = [S.dot("No file changes recorded")]
        sections_list = [S.lst("Files changed", items, full=True,
                                note=("From the run's cached diff; the worktree is gone."
                                      if run.card.get("files_from_cache") else ""))]
        if file_history_items:
            sections_list.append(S.lst("Files changed · session history",
                                       file_history_items, full=True,
                                       note="Newest-first by summary.last_ts. Open jumps to the "
                                            "source session."))
        return sections_list

    return ((S.guarded(errors, "Retry", retry_section) if include_retry else [])
            + S.guarded(errors, "Run inputs", inputs)
            + S.guarded(errors, "Why? — evidence",
                        lambda: why_section(run, title="Why? — evidence"))
            + S.guarded(errors, "Orchestrator", orchestrator)
            + S.guarded(errors, "Correlation", correlation) + S.guarded(errors, "Recent events", recent)
            + S.guarded(errors, "Files changed", files)
            + _run_memory_sections(run))


def _resolve_kickoff(run: Run) -> tuple[str, str]:
    """The run's kickoff markdown and its absolute source path.

    Three-tier resolution: ``task_runs.kickoff_path`` → ``runs-inbox/<id>.md``
    → the first ``kickoff*.md`` globbed in the run dir. ``history._read_kickoff``
    does the same two-tier read but returns only text — we re-read it here so
    we can also report which path actually served the bytes.
    """
    from mini_ork.acp.history import _is_safe_token

    def _try(path: Path) -> str:
        try:
            return path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""

    row_path = run.row.get("kickoff_path") or ""
    if row_path:
        text = _try(Path(row_path))
        if text:
            return text, str(Path(row_path).resolve())

    if _is_safe_token(run.id):
        inbox = run.home / "runs-inbox" / f"{run.id}.md"
        if inbox.is_file():
            text = _try(inbox)
            if text:
                return text, str(inbox.resolve())

    for candidate in sorted(run.run_dir.glob("kickoff*.md")):
        if candidate.is_file():
            text = _try(candidate)
            if text:
                return text, str(candidate.resolve())

    return "", ""


def _ask_index(name: str) -> tuple[int, str]:
    """``ask-10.json`` sorts after ``ask-9.json``: the number orders, the name breaks ties."""
    digits = name[len("ask-"):-len(".json")]
    return (int(digits), name) if digits.isdigit() else (1 << 31, name)


def open_asks(run_dir: Path) -> list[dict[str, Any]]:
    """The run's *unanswered* asks (``asks/ask-N.json``, ``ask@1``), in ask order.

    One dict per ask that still waits on a human: ``{"ask_id", "question",
    "options", "default"}``. An ask carrying an ``answer`` is a fact about the
    run, not a question, so it is left out — the same rule
    ``orchestration.run_reaper._has_unanswered_ask`` uses to decide that a run
    is blocked. A file that cannot be read, parsed, or that holds no question is
    skipped: one bad ask costs its own row, never the tab (or the inbox).
    """
    asks_dir = run_dir / "asks"
    try:
        names = [n for n in os.listdir(asks_dir)
                 if n.startswith("ask-") and n.endswith(".json")]
    except OSError:  # no asks dir, or unreadable — the same answer: none
        return []
    out: list[dict[str, Any]] = []
    for name in sorted(names, key=_ask_index):
        try:
            data = json.loads((asks_dir / name).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict) or str(data.get("answer") or "").strip():
            continue
        question = str(data.get("question") or "").strip()
        if not question:
            continue
        default = data.get("default")
        out.append({
            "ask_id": str(data.get("ask_id") or name[:-len(".json")]),
            "question": question,
            "options": [str(o) for o in data.get("options") or [] if str(o).strip()],
            "default": None if default is None else str(default),
        })
    return out


def answered_asks(run_dir: Path) -> list[dict[str, Any]]:
    """The asks a human already answered, in ask order.

    The open list tells the operator what still waits; this one tells them
    what they decided and why the run continued. It is read from the same
    files (``answer`` non-empty) so the two lists can never count an ask
    twice.
    """
    asks_dir = run_dir / "asks"
    try:
        names = [n for n in os.listdir(asks_dir)
                 if n.startswith("ask-") and n.endswith(".json")]
    except OSError:
        return []
    out: list[dict[str, Any]] = []
    for name in sorted(names, key=_ask_index):
        try:
            data = json.loads((asks_dir / name).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        answer = str(data.get("answer") or "").strip()
        question = str(data.get("question") or "").strip()
        if not answer or not question:
            continue
        out.append({"ask_id": str(data.get("ask_id") or name[:-len(".json")]),
                    "question": question, "answer": answer})
    return out


def _questions_tab(run: Run) -> list[dict[str, Any]]:
    """What the run is waiting to be told, and one box per question to tell it.

    The whole loop already exists in the engine — ``plan._write_ask_files``
    writes the asks, ``resume --answer`` records an answer and continues the run
    — so this tab is the surface that was missing, not new machinery. Answering
    is ``mini-ork resume <run> --answer ask-N=<text>``; the composer appends the
    typed text as the last argument of that argv.
    """
    errors: dict[str, str] = {}

    def build() -> list[dict[str, Any]]:
        asks = open_asks(run.run_dir)
        if not asks:
            quiet: list[dict[str, Any]] = [S.callout(
                "No open questions",
                "This run is not waiting on an answer. A question lands here — and in the "
                "inbox — when the planner stops on something only you can decide.",
                tone="green", icon="✓", full=True)]
            # A run that answered questions earlier keeps that record: "not
            # waiting" and "never asked anything" are different histories.
            answered = answered_asks(run.run_dir)
            if answered:
                rows = [S.item(a["ask_id"],
                               f"{a['question'][:140]} → {a['answer'][:140]}")
                        for a in answered[-5:]]
                quiet.append(S.lst(f"Answered · {len(answered)}", rows, full=True,
                                   note="The answers this run continued on."))
            return quiet
        sections: list[dict[str, Any]] = [S.callout(
            f"{len(asks)} question{'s' if len(asks) > 1 else ''} waiting",
            "The run continues once every one is answered — same run id, same recipe, "
            "from where it stopped.",
            tone="yellow", icon="✋", full=True)]
        for ask in asks:
            text = ask["question"]
            if ask["options"]:
                text += "\n\n" + "\n".join(f"- {o}" for o in ask["options"])
            if ask["default"] is not None:
                text += f"\n\nIf you don't answer, the run will use: “{ask['default']}”"
            sections.append(S.callout(str(ask["ask_id"]), text, tone="yellow", full=True))
            sections.append(S.composer(
                "Your answer… (Enter to send)",
                ["resume", run.id, "--answer", f"{ask['ask_id']}="],
                title=f"Answer {ask['ask_id']}", home=False, full=True))
        return sections

    return S.guarded(errors, "Questions", build)


def _kickoff_tab(run: Run, *, code_presentation: bool = False) -> list[dict[str, Any]]:
    errors: dict[str, str] = {}

    def build() -> dict[str, Any]:
        text, path = _resolve_kickoff(run)
        if not text:
            return S.lst("Kickoff", [S.dot("No kickoff file recorded for this run",
                                            "Checked task_runs.kickoff_path, runs-inbox, and the run dir.")],
                          full=True)
        truncated = len(text) > _KICKOFF_TAB_CAP
        if truncated:
            text = text[:_KICKOFF_TAB_CAP] + "\n\n(truncated)\n"
        title = Path(path).name if path else "Kickoff"
        if code_presentation:
            # The design reads the kickoff as the source it is: a code block,
            # not a rendered page. Reading it is the tab's whole job — the file
            # is not offered for editing, because it records what the run was
            # asked, not what it is about to be asked.
            return S.code(title, [(line, "body") for line in text.splitlines()], full=True)
        return S.markdown(title, text, path=path, full=True)

    return S.guarded(errors, "Kickoff", build)


_ROSTER_NOTE = "Where the money went inside the run. Click a row to inspect the node."


def _fallback_facts(calls: list[dict[str, Any]]) -> tuple[list[str], int, str]:
    """Models in time order, plus the failed-before-success story, for one node.

    A fallback is a node the executor served from more than one model: the
    roster names the first and the last, and the note names the sequence. The
    story is only told when a non-success call precedes a success — a failure
    *after* the success is a fallback, but not a "failed call, then …".
    """
    ordered = sorted(calls, key=lambda c: _epoch(c.get("ts")) or 0)
    models: list[str] = []
    for call in ordered:
        model = str(call.get("model_id") or "")
        if model and model not in models:
            models.append(model)
    failed = 0
    failed_then = ""
    for call in ordered:
        if str(call.get("status") or "") == "success":
            if failed:
                failed_then = str(call.get("model_id") or "")
                break
        else:
            failed += 1
    return models, failed, failed_then


def _calls_by_node(run: Run) -> dict[str, list[dict[str, Any]]]:
    """The run's calls grouped by the node the shared attribution rule gives them to."""
    out: dict[str, list[dict[str, Any]]] = {}
    for node, call in _match_calls({n.id: n for n in run.nodes}, run.calls, int(time.time())):
        out.setdefault(node.id, []).append(call)
    return out


def _roster_section(run: Run, *, tokens: bool = False) -> dict[str, Any]:
    """Every node of the run as one table: what it ran on, its state and cost.

    The single roster builder — the Agents tab draws it. ``tokens`` adds the
    tokens column — the v2 Agents tab asks for it, the level-1 payload does not,
    so its shape stays byte-identical.

    A node whose attributed calls carry more than one ``model_id`` fell back
    mid-node: its family cell reads ``first → last``, it gains a ``⇄`` marker
    cell and a ``fallback`` list on the row, and the section note names the
    sequence. Zero- and one-model nodes render exactly as before.
    """
    from mini_ork.ide_pages.node import _node_tokens  # lazy: node.py imports this module

    by_node = _calls_by_node(run)
    rows: list[dict[str, Any]] = []
    fallback_lists: list[list[str] | None] = []
    fell_back: list[tuple[str, str, int, str]] = []
    for n in run.nodes:
        verdict, vc = _verdict(run, n)
        ran = n.state not in ("pending", "skipped")
        models, failed, failed_then = _fallback_facts(by_node.get(n.id, []))
        fallback = models if len(models) > 1 else None
        family = f"{models[0]} → {models[-1]}" if fallback else n.family
        cells = [
            S.cell(n.id, "text", b=True),
            S.cell(family, f"fam:{models[-1]}" if fallback else f"fam:{n.family}"),
            S.cell(_STATE_TEXT.get(n.state, n.state), _STATE_COLOUR.get(n.state, "sub")),
            S.cell(verdict, vc),
            S.mono(S.money(n.cost) if ran else "—"),
        ]
        if tokens:
            counted = _node_tokens(n, run) if ran else None
            cells.append(S.mono(f"{counted:,}" if counted is not None else "—"))
        cells.extend([S.mono(_wall(n) if ran else "—"),
                      S.muted(", ".join(n.gates) or "—")])
        if fallback:
            cells.append(S.muted("⇄"))
            fell_back.append((n.id, f"{models[0]} → {models[-1]}", failed, failed_then))
        rows.append({"cells": cells,
                     "do": S.page_link("run", "dag", run=run.id, node=n.id)})
        fallback_lists.append(fallback)
    head = ["node", "family", "status", "verdict", "cost"]
    widths = [S.col(150), S.col(90), S.col(80), S.col(90), S.col(64)]
    if tokens:
        head.append("tokens")
        widths.append(S.col(90))
    head.extend(["time", "gates"])
    widths.extend([S.col(64), S.col(fr=1, min=140)])
    if not rows:
        rows = [[S.muted("No nodes recorded")] + ["" for _ in head[1:]]]
    note = _ROSTER_NOTE
    if fell_back:
        details = []
        for node_id, sequence, failed, failed_then in fell_back:
            detail = f"{node_id}: {sequence}"
            if failed and failed_then:
                detail += (f"; {failed} failed call{'s' if failed != 1 else ''}, "
                           f"then {failed_then}")
            details.append(detail)
        count = len(fell_back)
        note += (f" {count} node{'s' if count != 1 else ''} fell back "
                 f"{', '.join(f'({d})' for d in details)}.")
    section = S.table("Agent roster", widths, head, rows, full=True, note=note)
    # ``S.table`` keeps only the known row keys; the IDE's row badge rides
    # ``fallback`` beside the cells, so attach it to the emitted rows.
    for row, fallback in zip(section["rows"], fallback_lists):
        if fallback:
            row["fallback"] = fallback
    return section


def _agents_tab(run: Run) -> list[dict[str, Any]]:
    errors: dict[str, str] = {}

    def run_level() -> dict[str, Any]:
        stages = run.card.get("cost_by_stage") or {}
        total = sum(float(v or 0) for v in stages.values()) or 1.0
        items = [(k, 100.0 * float(v or 0) / total, S.money(v)) for k, v in
                 sorted(stages.items(), key=lambda kv: -float(kv[1] or 0)) if float(v or 0) > 0]
        if not items:
            return S.lst("Cost by stage", [S.dot("No metered LLM calls for this run")], full=True)
        return S.bars("Cost by stage", items, full=True,
                      note="Every metered call of the run, including planning and reflection.")

    return (S.guarded(errors, "Agent roster", lambda: _roster_section(run, tokens=True))
            + S.guarded(errors, "Cost by stage", run_level))


# `tr-<type>-<node>-<hash>` is the trace-id format the gradient extractor writes
# as evidence (see ``mini_ork/learning/gradient_extractor.py``); strip the type
# prefix and the trailing hash to surface the node id in the UI. If a trace id
# doesn't match the shape, return it whole — the caller's fallback is "show the
# raw id" rather than "show nothing".
_NODE_FROM_TRACE = re.compile(r"^tr-[a-z_]+-([a-z0-9_]+)-([A-Za-z0-9]+)$")

_PACK_DETAIL_CAP = 8


def _parse_trace_node(trace_id: str) -> str:
    """Pull the node id out of ``tr-<type>-<node>-<hash>``; fall back to ``trace_id``."""
    m = _NODE_FROM_TRACE.match(trace_id or "")
    return m.group(1) if m else (trace_id or "")


def _pack_failure_modes_items(value: Any) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for fm in (value if isinstance(value, list) else []):
        if not isinstance(fm, dict):
            continue
        title = str(fm.get("signal") or "")[:200]
        fix = ("fix: " + str(fm.get("suggested_change") or ""))[:160]
        parts = [fix]
        target = str(fm.get("target") or "")
        if target:
            parts.append("· " + target)
        conf = fm.get("confidence")
        if conf is not None:
            try:
                parts.append(f"· confidence {float(conf):.2f}")
            except (TypeError, ValueError):
                pass
        out.append(S.item(title, " ".join(parts)))
    return out


def _pack_similar_lessons_items(value: Any) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for sl in (value if isinstance(value, list) else []):
        if not isinstance(sl, dict):
            continue
        title = str(sl.get("title") or "")[:200]
        fix = ("fix: " + str(sl.get("suggested_fix") or ""))[:160]
        sub = fix
        score = sl.get("score")
        if score is not None:
            try:
                sub += f" · similarity {float(score):.2f}"
            except (TypeError, ValueError):
                pass
        out.append(S.item(title, sub))
    return out


def _pack_patterns_items(value: Any) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for vp in (value if isinstance(value, list) else []):
        if not isinstance(vp, dict):
            continue
        lesson = str(vp.get("lesson_text") or "")
        cluster = str(vp.get("cluster_label") or "")
        title = lesson if lesson else cluster
        cite = str(vp.get("cite") or "")
        # Pattern id lives after the last '/' in cite ("execution_traces/pat-xyz" → "pat-xyz").
        pattern_id = cite.rsplit("/", 1)[-1] if "/" in cite else cite
        parts = [f"pattern {pattern_id}"] if pattern_id else []
        strength = vp.get("strength_score")
        if strength is not None:
            try:
                parts.append(f"· strength {float(strength):.0f}")
            except (TypeError, ValueError):
                pass
        if not lesson:
            parts.append("· no authored lesson (frequency only)")
        out.append(S.item(title, " ".join(parts) if parts else ""))
    return out


def _pack_prior_runs_items(value: Any) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for pr in (value if isinstance(value, list) else []):
        if not isinstance(pr, dict):
            continue
        rid = str(pr.get("run_id") or pr.get("trace_id") or "")
        try:
            nodes = int(pr.get("nodes") or 0)
        except (TypeError, ValueError):
            nodes = 0
        try:
            bad = int(pr.get("non_success_nodes") or 0)
        except (TypeError, ValueError):
            bad = 0
        try:
            cost = float(pr.get("cost_usd") or 0.0)
        except (TypeError, ValueError):
            cost = 0.0
        try:
            duration_ms = int(pr.get("duration_ms") or 0)
        except (TypeError, ValueError):
            duration_ms = 0
        created_at = str(pr.get("created_at") or "")
        bits = []
        if nodes:
            bits.append(f"{nodes} nodes")
        bits += [f"${cost:.2f}", f"{duration_ms // 1000}s"]
        # Prefer the discriminating `outcome` (landed / failed / no artifact).
        # The old `reward_g` was a saturated 1.0 and is no longer emitted; keep
        # the read for one release so packs written before this change still
        # render, but never lead with a constant.
        outcome = str(pr.get("outcome") or "").strip()
        if outcome:
            bits.append(outcome)
        else:
            reward = pr.get("reward_g")
            try:
                if reward is not None and float(reward) not in (0.0, 1.0):
                    bits.append(f"reward {float(reward):.2f}")
            except (TypeError, ValueError):
                pass
        if bad:
            bits.append(f"{bad} non-success")
        if created_at:
            bits.append(created_at[:16])
        legacy_status = str(pr.get("status") or "")
        mc = "green" if (bad == 0 and legacy_status != "failure") else "sub"
        out.append(S.item(rid or "(run)", " · ".join(bits), mc=mc))
    return out


def _pack_kv_items(value: Any) -> list[dict[str, Any]]:
    """Constraints / user_preferences — list of scalars/dicts or a flat dict.

    A dict becomes one item per ``f"{k}: {v}"`` entry; a list of scalars or
    one-key dicts becomes one item per entry."""
    out: list[dict[str, Any]] = []
    if isinstance(value, list):
        for v in value:
            if isinstance(v, dict):
                for k2, v2 in v.items():
                    out.append(S.item(f"{k2}: {v2}"[:200]))
            else:
                out.append(S.item(str(v)[:200]))
    elif isinstance(value, dict):
        for k2, v2 in value.items():
            out.append(S.item(f"{k2}: {v2}"[:200]))
    return out


_PACK_ITEM_BUILDERS = {
    "known_failure_modes": _pack_failure_modes_items,
    "similar_lessons": _pack_similar_lessons_items,
    "verified_emergent_patterns": _pack_patterns_items,
    "prior_similar_runs": _pack_prior_runs_items,
    "constraints": _pack_kv_items,
    "user_preferences": _pack_kv_items,
}


def _pack_section_items(key: str, value: Any, pack_path: Path) -> list[dict[str, Any]]:
    """Build the detail list for one non-empty pack section.

    Caps visible items at ``_PACK_DETAIL_CAP``; the overflow becomes a
    ``+N more`` row with an Open action that jumps to the pack file."""
    builder = _PACK_ITEM_BUILDERS.get(key)
    raw_items = builder(value) if builder else []
    if len(raw_items) > _PACK_DETAIL_CAP:
        overflow = len(raw_items) - _PACK_DETAIL_CAP
        visible = raw_items[:_PACK_DETAIL_CAP]
        visible.append(S.item(f"+{overflow} more",
                              acts=[S.btn("Open", S.open_path(str(pack_path)), "ghost")]))
        return visible
    return raw_items


def _read_json_obj(path: Path) -> dict[str, Any]:
    """Parsed JSON object at ``path``, or ``{}`` on any read/parse error."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _injection_records(run: Run) -> list[dict[str, Any]]:
    """The raw ``learned/<node>.json`` records — the run's injection ledger.

    ``learned/`` is the INJECTION ledger (what actually reached the model),
    not produced learnings. :func:`_injection_ledger` renders these for the
    page; :func:`_injected_usage` reads the same records' ``sources[].id`` to
    tell which pack items a node was actually given.
    """
    learned_dir = run.run_dir / "learned"
    if not learned_dir.is_dir():
        return []
    out: list[dict[str, Any]] = []
    for p in sorted(learned_dir.glob("*.json")):
        d = _read_json_obj(p)
        # A missing/unparseable file still gets a row (its stem names the node),
        # so a broken record is visible rather than silently dropped.
        out.append({**d, "node_id": str(d.get("node_id") or p.stem)})
    return out


def _injection_ledger(run: Run) -> list[dict[str, Any]]:
    """Per-node rows: what each node was given, and why the rest was skipped.

    Rendering the INJECTION ledger (``learned/<node>.json``) closes the
    "display != injection" gap: the planner injects the v2 pack, so the tab
    must show the v2 blocks (and each node's source ids / skipped blocks), not
    the v1 builder output.
    """
    out: list[dict[str, Any]] = []
    for d in _injection_records(run):
        node = str(d.get("node_id") or "")
        injected = bool(d.get("injected"))
        srcs = d.get("sources") if isinstance(d.get("sources"), list) else []
        kinds = sorted({str(s.get("kind") or "") for s in srcs if isinstance(s, dict)})
        skipped = d.get("skipped_blocks") if isinstance(d.get("skipped_blocks"), dict) else {}
        mark = "●" if injected else "○"
        sub_bits = [f"{len(srcs)} source(s)"]
        if kinds:
            sub_bits.append("/".join(k for k in kinds if k))
        if skipped:
            sub_bits.append("skipped: " + ", ".join(sorted(skipped)))
        if d.get("reason"):
            sub_bits.append(str(d.get("reason"))[:60])
        out.append(S.item(f"{mark} {node}", " · ".join(sub_bits),
                          m="✦" if injected else "○",
                          mc="purple" if injected else "sub"))
    return out


def _injected_usage(run: Run) -> set[str] | None:
    """The pack item ids the ledger records as read or cited by a node.

    ``None`` when the run has no injection ledger at all: the ledger cannot
    tell, and the tab says so rather than guessing. A node "reads" an item by
    being given it (the ledger's ``sources[].id``) and "cites" one by naming it
    in the planner's ``context_used`` list
    (:func:`mini_ork.context_v2.acknowledgements`).
    """
    records = _injection_records(run)
    if not records:
        return None
    used: set[str] = set()
    for d in records:
        for s in d.get("sources") or []:
            if isinstance(s, dict) and s.get("id") is not None:
                used.add(str(s["id"]))
    cited = _read_json_obj(run.run_dir / "plan.json").get("context_used")
    if isinstance(cited, list):
        used.update(str(c) for c in cited)
    return used


# What the Learnings tab says when the injection ledger has no field telling
# whether an injected lesson was used — one note on the section, and the rows
# stay unmarked (kickoff: "usage not recorded for this run").
_USAGE_UNKNOWN = "usage not recorded for this run"


def _forget_action(lesson_id: str) -> dict[str, Any]:
    """The row's Forget button: ``lessons forget <id>``, confirmed.

    ``S.btn`` carries no ``done`` toast, so the kickoff's success text rides
    the action itself.
    """
    do = S.cli("lessons", "forget", lesson_id,
               confirm=f"Forget lesson {lesson_id}? Future runs stop receiving it.")
    do["done"] = "Lesson forgotten"
    return S.btn("Forget", do, "ghost")


def _lesson_acts(lesson_id: str) -> list[dict[str, Any]]:
    """A lesson row's buttons: Forget (only when it has an id) + Open in Learn."""
    acts = [_forget_action(lesson_id)] if lesson_id else []
    acts.append(S.btn("Open in Learn", S.page_link("learn", "lessons"), "ghost"))
    return acts


def _lesson_item(title: str, lesson_id: str, usage: set[str] | None, level2: bool) -> dict[str, Any]:
    """One injected lesson row: its id, its used/unused mark and its buttons.

    ``usage`` is the id set the injection ledger records, or ``None`` when the
    ledger cannot tell — and ``None`` marks nothing (the section carries
    :data:`_USAGE_UNKNOWN` instead). Outside IDE spec 2 the row is exactly what
    today's tab draws.
    """
    sub = lesson_id
    if level2 and usage is not None and lesson_id:
        sub = f"{lesson_id} · {'used' if lesson_id in usage else 'unused'}"
    return S.item(title, sub, acts=_lesson_acts(lesson_id) if level2 else [])


def _member_trace_ids(members_json: Any) -> list[str] | None:
    """The trace ids a pattern's member JSON names, or ``None`` when unreadable.

    Two shapes are live (see ``lessons_cmd._seen_in_runs``): bare trace-id
    strings and ``{item_table, item_id}`` dicts. Anything that is not a JSON
    list is unreadable — the caller drops the section rather than guessing a
    membership it cannot prove.
    """
    try:
        members = json.loads(members_json) if members_json else []
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(members, list):
        return None
    ids: list[str] = []
    for m in members:
        if isinstance(m, dict):
            if (m.get("item_table") or "execution_traces") != "execution_traces":
                continue
            mid = m.get("item_id")
        else:
            mid = m
        if mid:
            ids.append(str(mid))
    return ids


def _proposed_lessons(run: Run) -> list[dict[str, Any]]:
    """The run's "Proposed lessons" section, or ``[]`` when there is none.

    A proposed pattern appears here only when ``member_item_ids_json`` names a
    trace id from **this** run's own ``execution_traces`` rows — trace
    membership is the only link between a run and a pattern. Every failure
    (missing table, unreadable JSON) costs this section, never the tab.
    """
    from mini_ork.web.db import db_for

    db = db_for(run.home)
    if not (db.has_table("emergent_patterns") and db.has_table("execution_traces")):
        return []
    trace_ids = {str(r.get("trace_id") or "")
                 for r in db.rows("SELECT trace_id FROM execution_traces WHERE run_id = ?",
                                  (run.id,))}
    trace_ids.discard("")
    if not trace_ids:
        return []
    cols = "pattern_id, cluster_label, member_item_ids_json, strength_score"
    try:
        has_lesson = any(str(r.get("name") or "") == "lesson_text"
                         for r in db.rows("PRAGMA table_info(emergent_patterns)", ()))
    except Exception:  # noqa: BLE001 — an unreadable schema is just no lesson column
        has_lesson = False
    rows = db.rows(
        f"SELECT {cols}{', lesson_text' if has_lesson else ''} "  # noqa: S608 — fixed cols
        "FROM emergent_patterns WHERE status = 'proposed'",
        (),
    )
    items: list[dict[str, Any]] = []
    for r in rows:
        members = _member_trace_ids(r.get("member_item_ids_json"))
        if members is None:
            return []  # unreadable membership: the whole section cannot be trusted
        if not any(t in trace_ids for t in members):
            continue
        pid = str(r.get("pattern_id") or "")
        lesson = str(r.get("lesson_text") or "").strip() if has_lesson else ""
        title = (lesson or str(r.get("cluster_label") or ""))[:200]
        try:
            strength = float(r.get("strength_score") or 0.0)
        except (TypeError, ValueError):
            strength = 0.0
        acts: list[dict[str, Any]] = []
        if lesson:
            do = S.cli("lessons", "promote", pid,
                       confirm=f"Promote lesson {pid}? Future runs receive it.")
            do["done"] = "Lesson promoted"
            acts.append(S.btn("Promote", do, "primary"))
        acts.append(_forget_action(pid))
        items.append(S.item(title, f"{pid} · proposed · strength {strength:.0f}",
                            m="✦", mc="purple", acts=acts))
    if not items:
        return []
    return [S.lst(f"Proposed lessons · {len(items)}", items, full=True,
                  note="Promote to give future runs this lesson; reject to stop proposing it.")]


def _learnings_tab(run: Run) -> list[dict[str, Any]]:
    errors: dict[str, str] = {}
    # The Forget / Open-in-Learn buttons and the used/unused marking are IDE
    # spec 2 surfaces, like every other v2 field: an older IDE keeps getting
    # exactly today's page.
    level2 = S.ide_level() >= 2

    def produced() -> dict[str, Any]:
        from mini_ork.web.db import db_for

        db = db_for(run.home)
        items: list[dict[str, Any]] = []
        if db.has_table("gradient_records"):
            # Join on execution_traces by trace_id (= gradient.evidence) and
            # filter by run_id so gradients from other same-class runs in the
            # same window don't bleed in. Tolerate a missing execution_traces
            # table: skip the run-scoped join (no rows, no crash).
            if db.has_table("execution_traces"):
                join_sql = (
                    "SELECT g.gradient_id, g.signal, g.suggested_change, g.evidence, "
                    "g.confidence, g.target, g.task_class, g.created_at "
                    "FROM gradient_records g "
                    "JOIN execution_traces t ON t.trace_id = g.evidence "
                    "WHERE t.run_id = ? "
                    "ORDER BY g.created_at DESC LIMIT 20"
                )
                # Bind `run.id` directly: live `execution_traces.run_id` holds the
                # text run id from `task_runs.id` (e.g. ``run-<ts>-<pid>``),
                # not an integer — a `CAST(? AS INTEGER)` would coerce text to 0
                # and silently drop every real row.
                rows = db.rows(join_sql, (run.id,))
            else:
                rows = []
            for g in rows:
                trace_id = str(g.get("evidence") or "")
                node = _parse_trace_node(trace_id) or "—"
                title = str(g.get("signal") or "")[:200]
                fix = ("fix: " + str(g.get("suggested_change") or ""))[:160]
                try:
                    conf = float(g.get("confidence") or 0.0)
                except (TypeError, ValueError):
                    conf = 0.0
                sub = f"{fix} · {node} · confidence {conf:.2f}"
                items.append(S.item(title, sub, m="✦", mc="purple",
                                    acts=_lesson_acts(str(g.get("gradient_id") or ""))
                                    if level2 else []))
        if db.has_table("learning_record"):
            for r in db.rows("SELECT title, category, outcome, confidence FROM learning_record "
                             "WHERE run_id = ? ORDER BY rank LIMIT 20", (run.id,)):
                items.append(S.item(str(r.get("title") or ""),
                                    f"{r.get('category')} · {r.get('outcome')} · confidence "
                                    f"{float(r.get('confidence') or 0):.2f}", m="✦", mc="purple",
                                    acts=_lesson_acts("") if level2 else []))
        if not items:
            items = [S.dot("Nothing recorded", "Reflection writes gradients after the run's last node.")]
        return S.lst("Written back" if level2 else "Produced by the run", items, full=True,
                     note="Gradients reflection wrote about this run's own nodes.")

    def available() -> list[dict[str, Any]]:
        sections: list[dict[str, Any]] = []
        items: list[dict[str, Any]] = []
        # The injection ledger drives both the "Injected per node" list and the
        # used/unused mark on each injected lesson. A ledger that cannot be read
        # costs those two facts, never the tab (nor the rest of this section).
        try:
            ledger = _injection_ledger(run)
        except Exception as exc:  # noqa: BLE001 — one broken source, one lost list
            errors["Injected per node"] = f"{type(exc).__name__}: {exc}"
            ledger = []
        try:
            usage = _injected_usage(run) if level2 else None
        except Exception as exc:  # noqa: BLE001
            errors["Injected lesson usage"] = f"{type(exc).__name__}: {exc}"
            usage = None
        # The ledger cannot tell → say so on the sections that list injected
        # lessons, and leave every row unmarked.
        usage_note = _USAGE_UNKNOWN if (level2 and usage is None) else ""

        def toc(item: dict[str, Any]) -> None:
            """A summary row, level 1 only.

            On level 2 the sections below already carry the same counts, and
            a table of contents that repeats every heading immediately above
            the headings is noise. Level 1 keeps the list it always had.
            """
            if not level2:
                items.append(item)

        # Prefer the INJECTED artifact. The planner writes context-pack.v2.json
        # and SUPPRESSES the v1 builder output, so rendering v1 showed a pack the
        # model never saw. v2 first; v1 only as a fallback for older runs.
        v2_path = run.run_dir / "context-pack.v2.json"
        v1_path = run.run_dir / "context-pack.json"
        if v2_path.is_file():
            v2 = _read_json_obj(v2_path)
            items.append(S.ok("Injected context", "v2 pack — what the planner received"))
            cons = v2.get("constraints") if isinstance(v2.get("constraints"), list) else []
            if cons:
                detail = [_lesson_item(f"{c.get('kind', '?')}: {str(c.get('text', ''))[:180]}",
                                       str(c.get("id", "")), usage, level2)
                          for c in cons if isinstance(c, dict)]
                toc(S.ok("Constraints", f"{len(cons)} injected"))
                sections.append(S.lst(f"Constraints (injected) · {len(cons)}", detail,
                                      full=True, note=usage_note))
            else:
                toc(S.dot("Constraints", "none injected"))
            themes = v2.get("themes") if isinstance(v2.get("themes"), list) else []
            if themes:
                detail = [_lesson_item(str(t.get("text") or "")[:180], str(t.get("id") or ""),
                                       usage, level2)
                          for t in themes if isinstance(t, dict)]
                toc(S.ok("Verified lessons", f"{len(themes)} injected"))
                sections.append(S.lst(f"Verified lessons (injected) · {len(themes)}", detail,
                                      full=True, note=usage_note))
            contract = v2.get("contract") if isinstance(v2.get("contract"), dict) else {}
            ff = v2.get("file_findings") if isinstance(v2.get("file_findings"), list) else []
            if ff:
                detail = [S.item(str(f.get("file") or f.get("path") or "file"),
                                 str(f.get("finding") or f.get("summary") or "")[:160])
                          for f in ff if isinstance(f, dict)]
                toc(S.ok("File findings", f"{len(ff)} file(s)"))
                sections.append(S.lst(f"File findings · {len(ff)}", detail, full=True))
            elif contract:
                detail = _pack_kv_items(contract)
                if detail:
                    toc(S.ok("Contract", f"{len(detail)} clause(s)"))
                    sections.append(S.lst("Contract (injected)", detail, full=True))
            pa = v2.get("prior_attempts") if isinstance(v2.get("prior_attempts"), list) else []
            if pa:
                toc(S.ok("Prior attempts", f"{len(pa)} attempt(s)"))
        elif v1_path.is_file():
            items.append(S.ok("Context pack", "v1 builder output"))
            pack = _read_json_obj(v1_path)
            labels = (("prior_similar_runs", "Prior same-class runs"),
                      ("known_failure_modes", "Learned failure modes"),
                      ("verified_emergent_patterns", "Verified patterns"),
                      ("similar_lessons", "Similar lessons"),
                      ("constraints", "Constraints"),
                      ("user_preferences", "Your preferences"))
            for key, label in labels:
                value = pack.get(key)
                n = len(value) if isinstance(value, (list, dict)) else 0
                if n:
                    items.append(S.ok(label, f"{n} item{'s' if n != 1 else ''}"))
                    detail = _pack_section_items(key, value, v1_path)
                    if detail:
                        sections.append(S.lst(f"{label} · {n}", detail, full=True))
                else:
                    items.append(S.dot(label, "none given"))
            graph = pack.get("graph_context") if isinstance(pack.get("graph_context"), dict) else {}
            linked = graph.get("linked_gradients") if isinstance(graph, dict) else None
            if linked:
                items.append(S.ok("Graph context", f"{len(linked)} linked gradients"))
            if pack.get("tokens_estimated"):
                items.append(S.dot("Pack size", f"~{pack.get('tokens_estimated')} tokens of "
                                                f"{pack.get('budget_tokens') or '—'} budget"))
        else:
            items.append(S.dot("No context pack", "This run was not given a context pack."))

        if ledger:
            toc(S.ok("Per-node injection", f"{len(ledger)} node(s) recorded"))
            sections.append(S.lst(f"Injected per node · {len(ledger)}", ledger, full=True))

        from mini_ork.web.db import db_for

        db = db_for(run.home)
        if db.has_table("operator_steering"):
            n = (db.row("SELECT COUNT(*) AS n FROM operator_steering WHERE run_id = ?", (run.id,)) or {}).get("n", 0)
            items.append(S.ok("Operator steering", f"{n} message(s) this run") if n
                         else S.dot("Operator steering", "none this run"))
        summary = S.lst("Injected into prompts" if level2 else "Available to the run",
                        items, full=True,
                        note="The context the planner actually received (v2 pack), "
                             "plus each node's own injection ledger.")
        return [summary, *sections]

    # The design reads "what it was given" before "what it taught": the
    # injected lessons lead, the written-back gradients follow. Level 1 keeps
    # the old order and the old titles byte for byte.
    if level2:
        # The proposed lessons sit before the injected sections: a lesson the
        # operator can accept belongs ahead of the lessons already injected.
        try:
            proposed = _proposed_lessons(run)
        except Exception as exc:  # noqa: BLE001 — one broken source, one lost section
            errors["Proposed lessons"] = f"{type(exc).__name__}: {exc}"
            proposed = []
        return (proposed
                + S.guarded(errors, "Injected into prompts", available)
                + S.guarded(errors, "Written back", produced))
    return (S.guarded(errors, "Produced by the run", produced)
            + S.guarded(errors, "Available to the run", available))


# ── artifacts ───────────────────────────────────────────────────────────────
#
# Level 1 draws one flat "Artifacts" table; level 2 (the IDE's own spec) splits
# the same rows into a "Key artifacts" header section plus one table per
# producing node. Everything here is gated on ``S.ide_level()``, so an older IDE
# keeps getting today's single table byte for byte.

_ARTIFACT_COLS = [S.col(fr=1, min=200), S.col(110), S.col(70), S.col(110)]
_ARTIFACT_HEAD = ["file", "kind", "size", "from"]

# The tab draws at most this many FILE rows in total — the key artifacts count
# toward it. Everything past the cap collapses into one final "+N more in the
# run folder" row whose ``do`` reveals the run directory.
_ARTIFACT_ROW_CAP = 200

# The row-level ``kind`` is the file's extension, and this one rule feeds the
# row key, the intra-table order and the count sub alike. It is deliberately
# NOT the visible "kind" column: ``board_cmd._artifact_group`` fills that with a
# semantic group (Diffs, Results, Logs…), and unifying the two would change the
# column every level-1 table already draws.
_ARTIFACT_KINDS = ("patch", "json", "log", "jsonl", "md", "other")
_ARTIFACT_KIND_BY_SUFFIX = {
    ".diff": "patch", ".patch": "patch",
    ".json": "json", ".log": "log", ".jsonl": "jsonl", ".md": "md",
}


def _artifact_kind(name: str) -> str:
    """The row-level kind, from the file name's extension.

    A directory label ("evidence/") has no extension and is ``other`` — the
    honest answer, since it is not one of the five file kinds.
    """
    return _ARTIFACT_KIND_BY_SUFFIX.get(Path(name.rstrip("/")).suffix.lower(), "other")


def _artifact_kind_rank(name: str) -> int:
    """Sort key for one bucket: ``patch → json → log → jsonl → md → other``.

    Only the rank is returned, so Python's stable sort keeps the incoming
    (``_artifacts`` path) order inside a kind.
    """
    return _ARTIFACT_KINDS.index(_artifact_kind(name))


def _artifact_kind_sub(labels: list[str]) -> str:
    """``2 json · 1 log · 1 patch`` for a table, or ``""`` when there is nothing
    to summarise (fewer than two file rows).

    Counts come from the same :func:`_artifact_kind` as the rows and the sort.
    """
    if len(labels) < 2:
        return ""
    counts = dict.fromkeys(_ARTIFACT_KINDS, 0)
    for label in labels:
        counts[_artifact_kind(label)] += 1
    return " · ".join(f"{counts[kind]} {kind}"
                      for kind in _ARTIFACT_KINDS if counts[kind])


def _attach_row_kinds(section: dict[str, Any], labels: list[str]) -> dict[str, Any]:
    """Re-attach the row-level ``kind`` after ``S.table`` rebuilt the rows.

    ``spec.table`` keeps only ``cells``/``do``/``sel``/``acts`` on each row
    (spec.py is out of scope here), so a ``kind`` set on the dict handed to it
    is silently dropped. Attaching after the rebuild is what makes the key
    reach the rendered page JSON. ``labels`` is parallel to the table's file
    rows; the caller never passes a collapse or placeholder row.
    """
    for row, label in zip(section.get("rows") or [], labels):
        row["kind"] = _artifact_kind(label)
    return section

# The kickoff's "Key artifacts" slots, in the order the tab must show them. The
# diff is ONE slot whose first alternative wins; ``evidence`` is a directory.
_KEY_ARTIFACTS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("file", ("review-diff.patch", "framework-edit.diff")),
    ("file", ("verdict.json",)),
    ("file", ("review-verdict.json",)),
    ("file", ("rubric.json",)),
    ("file", ("retry-hint.json",)),
    ("dir", ("evidence",)),
    ("file", ("execute.log",)),
)


def _artifact_size(path: Path) -> str:
    """A key artifact's size: a directory sums the files under it."""
    try:
        if path.is_dir():
            total = sum(f.stat().st_size for f in path.rglob("*") if f.is_file())
        else:
            total = path.stat().st_size
    except OSError:
        return "—"
    return _human(total)


def _key_artifacts(run: Run) -> list[tuple[str, Path]]:
    """``(label, path)`` for each key artifact that exists, in kickoff order."""
    out: list[tuple[str, Path]] = []
    for kind, names in _KEY_ARTIFACTS:
        for name in names:
            path = run.run_dir / name
            if (path.is_dir() if kind == "dir" else path.is_file()):
                out.append((name + ("/" if kind == "dir" else ""), path))
                break
    return out


def _key_artifacts_section(run: Run) -> dict[str, Any]:
    """The tab's first section: the handful of files a reviewer opens first.

    Order and styling stay as they were; each file row additionally carries
    the row-level ``kind`` (re-attached after ``S.table`` rebuilds the rows).
    """
    from mini_ork.cli.board_cmd import _artifact_group

    keys = _key_artifacts(run)
    rows: list[dict[str, Any]] = []
    for label, path in keys:
        do = S.reveal(str(path)) if path.is_dir() else S.open_path(str(path))
        rows.append({"cells": [S.mono(label), S.cell(_artifact_group(label).lower()),
                               S.mono(_artifact_size(path)), S.muted("")],
                     "do": do, "kind": _artifact_kind(label)})
    if not rows:
        rows = [{"cells": [S.muted("No key artifacts yet"), "", "", ""]}]
    section = S.table("Key artifacts", _ARTIFACT_COLS, _ARTIFACT_HEAD, rows, full=True,
                      note="The files a reviewer opens first — the diff, the verdicts, "
                           "the rubric, the evidence and the run's log.")
    return _attach_row_kinds(section, [label for label, _ in keys])


def _artifact_row(a: dict[str, Any], ids: set[str]) -> dict[str, Any]:
    """One file row, in the flat table's own columns."""
    return {"cells": [S.mono(a["path"]), S.cell(a["group"].lower()),
                      S.mono(_human(a["size"])), S.muted(_from(a["path"], ids))],
            "do": S.open_path(a["abs"]), "kind": _artifact_kind(a["path"])}


def _artifact_file_sections(run: Run) -> list[dict[str, Any]]:
    """One table per producing node, then the run-level files.

    Attribution is the flat table's own :func:`_from` (``impl-<node>``,
    ``review-<node>``, …); a file that belongs to no node lands in "Run files".
    Each bucket is ordered by the one :func:`_artifact_kind` rule, and a table
    with two or more file rows gains a kind-count ``sub`` from the same rule.
    Files the "Key artifacts" table already shows are skipped here.
    """
    from mini_ork.cli.board_cmd import _artifacts

    files = _artifacts(run.run_dir)
    ids = {n.id for n in run.nodes}
    # A file already drawn in "Key artifacts" is not drawn again below: the
    # reviewer's shortlist and the full listing are the same file list, and
    # repeating the diff and the verdicts was most of the tab's length.
    keys = _key_artifacts(run)
    key_files = {label for label, _ in keys if not label.endswith("/")}
    key_dirs = tuple(label for label, _ in keys if label.endswith("/"))
    dropped = 0
    buckets: dict[str, list[dict[str, Any]]] = {n.id: [] for n in run.nodes}
    run_level: list[dict[str, Any]] = []
    for a in files:
        if a["path"] in key_files or any(a["path"].startswith(d) for d in key_dirs):
            dropped += 1
            continue
        node = _from(a["path"], ids)
        (buckets[node] if node in buckets else run_level).append(a)

    # The key artifacts consume part of the row budget, so the tab as a whole
    # stays under the cap.
    cap = max(0, _ARTIFACT_ROW_CAP - len(keys))
    ordered = [(f"From {n.id}", buckets[n.id]) for n in run.nodes if buckets[n.id]]
    ordered.append(("Run files", run_level))

    rendered: list[dict[str, Any]] = []
    shown = 0
    for title, entries in ordered:
        entries = sorted(entries, key=lambda a: _artifact_kind_rank(a["path"]))
        rows: list[dict[str, Any]] = []
        labels: list[str] = []
        for a in entries:
            if shown >= cap:
                break
            shown += 1
            rows.append(_artifact_row(a, ids))
            labels.append(a["path"])
        if rows:
            sub = _artifact_kind_sub(labels)
            section = S.table(title, _ARTIFACT_COLS, _ARTIFACT_HEAD, rows, full=True,
                              **({"sub": sub} if sub else {}))
            rendered.append(_attach_row_kinds(section, labels))

    if not rendered:
        return [S.table("Run files", _ARTIFACT_COLS, _ARTIFACT_HEAD,
                        [[S.muted("No artifacts yet"), "", "", ""]], full=True,
                        note="Nothing in the run folder yet.")]

    # Every file the cap left out is accounted for by this one row, which is
    # the last thing the tab draws.
    collapsed = len(files) - shown - dropped
    if collapsed > 0:
        rendered[-1]["rows"].append({
            "cells": [S.mono(f"+{collapsed} more in the run folder"), S.muted(""),
                      S.muted(""), S.muted("")],
            "do": S.reveal(str(run.run_dir))})

    return rendered


def _artifacts_tab(run: Run, *, changes: bool = False) -> list[dict[str, Any]]:
    errors: dict[str, str] = {}

    def table() -> dict[str, Any]:
        from mini_ork.cli.board_cmd import _artifacts

        rows = []
        ids = {n.id for n in run.nodes}
        for a in _artifacts(run.run_dir):
            rows.append({"cells": [S.mono(a["path"]), S.cell(a["group"].lower()),
                                   S.mono(_human(a["size"])), S.muted(_from(a["path"], ids))],
                         "do": S.open_path(a["abs"])})
        if not rows:
            rows = [[S.muted("No artifacts yet"), "", "", ""]]
        return S.table("Artifacts", _ARTIFACT_COLS, _ARTIFACT_HEAD, rows, full=True,
                       note="Only what the run produced, grouped. Click a file to open it.")

    def changed_files() -> dict[str, Any] | list[dict[str, Any]]:
        # The run's diff leads the tab: an artifact inventory answers "what did
        # it write", the changed-file table answers "what did it touch in the
        # repo" — the question the operator came with when the run finished.
        files = _card_files(run)
        if not files:
            return []
        rows = [{"cells": [S.mono(f.get("path") or ""),
                           S.mono(f"+{f.get('added') or 0}"),
                           S.mono(f"−{f.get('removed') or 0}")],
                 "do": S.open_path(str(f.get("abs") or "")) if f.get("abs") else None}
                for f in files]
        # The design's sub-line totals the diff; level 1 keeps the page it had.
        sub = ""
        if S.ide_level() >= 2:
            added = sum(int(f.get("added") or 0) for f in files)
            removed = sum(int(f.get("removed") or 0) for f in files)
            sub = (f"+{added} −{removed} · {len(files)} "
                   f"file{'s' if len(files) != 1 else ''}")
        return S.table("Changes", [S.col(fr=1, min=240), S.col(60), S.col(60)],
                       ["file", "added", "removed"], rows, full=True, sub=sub,
                       note="The run's working-tree diff. Click a file to open it.")

    head = S.guarded(errors, "Changes", changed_files) if changes else []
    if S.ide_level() < 2:
        return head + S.guarded(errors, "Artifacts", table)
    return head + (S.guarded(errors, "Key artifacts",
                          lambda: _key_artifacts_section(run))
                   + S.guarded(errors, "Artifacts",
                              lambda: _artifact_file_sections(run)))


# ── helpers ─────────────────────────────────────────────────────────────────

# ``patch``/``diff`` are named too: a node's ``impl-<node>.patch`` is the file
# that node produced, and the grouped tab has to file it under ``From <node>``
# rather than leaving it in "Run files" — the same attribution rule as its
# json/log/md siblings.
_FROM = re.compile(r"^(?:agent-|impl-|verifier_|review-|lens-)([A-Za-z0-9_.-]+?)(?:\.live)?\.(?:jsonl|log|json|md|patch|diff)$")


def _from(rel: str, node_ids: set[str]) -> str:
    m = _FROM.match(rel.rsplit("/", 1)[-1])
    return m.group(1) if m and m.group(1) in node_ids else "—"


def _human(n: int) -> str:
    if n < 1024:
        return f"{n} B"
    if n < 1_048_576:
        return f"{n / 1024:.1f} KB"
    return f"{n / 1_048_576:.1f} MB"


def _size(p: Path) -> str:
    try:
        return _human(p.stat().st_size)
    except OSError:
        return "—"


def _rel(path: str, home: Path) -> str:
    try:
        return str(Path(path).relative_to(home))
    except ValueError:
        return path


# ── entry point ─────────────────────────────────────────────────────────────

def _run_profile(run: Run) -> dict[str, Any]:
    """The run's ``run_profile.json`` — the classifier's task class and the
    operator's goal / success criteria. ``{}`` when the run has none."""
    try:
        data = json.loads((run.run_dir / "run_profile.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _branch(run: Run) -> str:
    return str(getattr(run.workspace, "branch", "") or "")


# ── run memory: decisions + per-file history (ContextNest) ──────────────────
#
# The run page surfaces two ContextNest-derived memories per node session:
# the ``decision_made`` / ``assumption`` / ``risk_flag`` atoms from each
# node's trajectory (labelled with the node, each opening the source
# ``cn_session`` page) and a per-file session-history line on the
# changed-files view. Both are fetched via
# :mod:`mini_ork.ide_pages._cn.render_cached_section` so a slow / down
# ContextNest still renders the established one-line state instead of a
# blank section.
#
# Once the run reaches a terminal state (``published`` / ``failed`` /
# ``rolled_back``) the page writes a small JSON snapshot to
# ``<run_dir>/contextnest-memory.json``. The finished page then reads the
# snapshot without re-fetching ContextNest — the snapshot's atom text and
# session ids are stable from then on, and a finished run never re-asks
# CN. A live page also computes the same data, but never persists it.

_RUN_MEMORY_SNAPSHOT = "contextnest-memory.json"
_DECISION_KINDS = ("decision_made", "assumption", "risk_flag")


def _read_run_memory_snapshot(run: Run) -> dict[str, Any] | None:
    """The run-memory snapshot for a finished run, or ``None`` when the
    file is missing or unreadable.

    Returning ``None`` is the only signal that the page must fall through
    to a live fetch — a corrupt snapshot degrades to the same path, so
    the section never crashes the build on a transient write race."""
    path = run.run_dir / _RUN_MEMORY_SNAPSHOT
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _write_run_memory_snapshot(run: Run, payload: dict[str, Any]) -> bool:
    """Persist the snapshot for a finished run. Returns ``True`` when the
    file was written; never raises — a missing / read-only run dir is
    silently ignored so the page build never crashes on a snapshot write.

    Live runs do not get a snapshot: the kickoff says "once the run
    finishes" and persisting during the run would race a slow
    ContextNest into a stale on-disk record."""
    if _running(run):
        return False
    path = run.run_dir / _RUN_MEMORY_SNAPSHOT
    try:
        path.write_text(json.dumps(payload, ensure_ascii=False),
                        encoding="utf-8")
    except OSError:
        return False
    return True


def _atom_ts_epoch(value: Any) -> int:
    """Newest-first sort key for a trajectory atom's ``ts`` field.

    Accepts the ISO-8601 string the verified live wire shape carries, the
    int / float epochs older payloads used, and the missing-or-malformed
    case (``0``) so an unparseable record sinks rather than blanking the
    list. Mirrors the existing ``cn_session._record_ts`` discipline so
    the run page and the session page can never disagree on recency."""
    if value is None or value == "":
        return 0
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str):
        try:
            iso = value.replace("Z", "+00:00")
            dt = datetime.fromisoformat(iso)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=datetime.now().astimezone().tzinfo or
                                datetime.now().tzinfo)
            return int(dt.timestamp())
        except (ValueError, TypeError):
            return 0
    return 0


def _fetch_trajectory_records(session_id: str) -> dict[str, Any]:
    """``GET /api/v1/sessions/<sid>/trajectory`` → ``dict``.

    Mirrors the wire contract the run page reads (the same one
    :mod:`mini_ork.ide_pages.cn_session` uses): ``records[]`` with
    ``kind`` / ``content`` / ``ts``. ``cn_client._get`` returns ``"{}"``
    on a down CN so the cached-section wrapper falls through to the
    unreachable marker — this helper never raises on a network error."""
    from mini_ork import cn_client
    raw = cn_client._get(f"/api/v1/sessions/{urllib.parse.quote(session_id, safe='')}/trajectory")
    try:
        data = json.loads(raw) if raw else {}
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _node_decision_atoms(run: Run, node: Node) -> list[dict[str, Any]]:
    """Fetch the node's trajectory, return its ``decision_made`` /
    ``assumption`` / ``risk_flag`` records (newest first), or ``[]``.

    The trajectory cache slot lives under the run's section cache, not
    the page-level cache, so two runs do not share an atom window.
    Each atom carries ``kind`` / ``content`` / ``ts``; older payloads
    used ``summary`` / ``text`` — both keys are checked so a regression
    that flips one to the other lights up at the unit test instead of
    dropping atoms on the run page."""
    sid = _node_session_id(run, node)
    if not sid:
        return []
    payload = _CN.render_cached_section(
        "run-node-trajectory", f"{run.id}__{node.id}__{sid}",
        lambda sid=sid: _fetch_trajectory_records(sid),
    )
    if payload.get("_unreachable") or payload.get("_disabled") or payload.get("_config_error"):
        return []
    records = payload.get("records")
    if not isinstance(records, list):
        return []
    out: list[dict[str, Any]] = []
    for r in records:
        if not isinstance(r, dict):
            continue
        kind = str(r.get("kind") or "").strip().lower()
        if kind not in _DECISION_KINDS:
            continue
        content = ""
        for key in ("content", "summary", "text", "title"):
            v = r.get(key)
            if isinstance(v, str) and v.strip():
                content = v.strip().replace("\n", " ")[:300]
                break
        if not content:
            continue
        out.append({"kind": kind, "content": content, "ts": str(r.get("ts") or "")})
    out.sort(key=lambda a: _atom_ts_epoch(a["ts"]), reverse=True)
    return out


def _cn_sessions_for_path(path: str) -> dict[str, Any]:
    """Read ``/sessions/by-file`` plus one summary fetch per match, return
    ``{"matches": [{"session_id", "last_ts"}, ...]}`` ordered
    newest-first by ``summary.last_ts`` (so two pages never show
    different recency for the same file).

    Reuses :mod:`mini_ork.ide_pages.cn_file`'s wire-shape decoders and
    the ``_SECTION_BY_FILE`` cache key — the fan-out is the same
    cn_file._sessions runs, so a cache populated by the file page is
    readable here and vice-versa. Returns ``{}`` on a CN outage so the
    caller renders the established one-line state rather than a silent
    empty list."""
    payload = _CN.render_cached_section(
        _CN_FILE_BY_FILE, path, lambda: _cn_file_fetch_by_file(path)
    )
    if payload.get("_unreachable") or payload.get("_disabled") or payload.get("_config_error"):
        return dict(payload)
    matches = payload.get("matches")
    if not isinstance(matches, list) or not matches:
        return {"matches": []}
    enriched: list[tuple[int, str]] = []
    for m in matches:
        if not isinstance(m, dict):
            continue
        sid = str(m.get("session_id") or "").strip()
        if not sid:
            continue
        sp = _CN.render_cached_section(
            _CN_FILE_BY_FILE, f"{path}__summary__{sid}",
            lambda sid=sid: _cn_file_fetch_summary(sid),
        )
        if sp.get("_unreachable") or sp.get("_disabled") or sp.get("_config_error"):
            inner: dict[str, Any] = {}
        else:
            inner = _cn_file_unpack_summary(sp) if isinstance(sp, dict) else {}
        ts = _cn_file_last_ts_epoch(inner) if isinstance(inner, dict) else 0
        enriched.append((ts, sid))
    enriched.sort(key=lambda t: t[0], reverse=True)
    return {"matches": [{"session_id": sid, "last_ts": ts} for ts, sid in enriched]}


def _collect_run_memory(run: Run) -> dict[str, Any]:
    """Build the full run-memory payload from live ContextNest data.

    Returns the same shape the snapshot file carries: ``{"decisions":
    [{node, session_id, atoms}], "files": [{path, session_ids, last_ts}]}``.
    Each list is omitted when no entries were found, so a run with no
    trajectory atoms and no file changes produces a snapshot that
    still parses but renders empty sections — the page never invents
    data the run never had.
    """
    decisions: list[dict[str, Any]] = []
    for node in run.nodes:
        atoms = _node_decision_atoms(run, node)
        if not atoms:
            continue
        sid = _node_session_id(run, node) or ""
        decisions.append({"node": node.id, "session_id": sid, "atoms": atoms})

    files_payload: list[dict[str, Any]] = []
    seen_paths: set[str] = set()
    try:
        from mini_ork.cli.board_cmd import _card_fields
        fields = _card_fields(run.card, run.home.absolute().parent)
    except Exception:  # noqa: BLE001 — a missing card must not blank the memory
        fields = {}
    for f in fields.get("files") or []:
        if not isinstance(f, dict):
            continue
        path = str(f.get("path") or "")
        if not path or path in seen_paths:
            continue
        seen_paths.add(path)
        sess = _cn_sessions_for_path(path)
        matches = sess.get("matches") if isinstance(sess, dict) else None
        if not matches:
            continue
        sids = [str(m.get("session_id")) for m in matches if isinstance(m, dict)
                and str(m.get("session_id") or "").strip()]
        if not sids:
            continue
        last_ts = 0
        for m in matches:
            if isinstance(m, dict):
                try:
                    last_ts = max(last_ts, int(m.get("last_ts") or 0))
                except (TypeError, ValueError):
                    continue
        files_payload.append({"path": path, "session_ids": sids, "last_ts": last_ts})

    return {"schema": "contextnest-memory/v1", "decisions": decisions,
            "files": files_payload}


def _decisions_assumptions_section(run: Run) -> dict[str, Any] | None:
    """The "Decisions and assumptions" section.

    For each node with a session id and at least one trajectory atom of
    a :data:`_DECISION_KINDS` kind, renders one sub-row carrying the
    node id, the atom kind, the (truncated) text, and a ``cn_session``
    link for the source session. A finished run reads the same data
    from the run-memory snapshot and never re-asks ContextNest; a live
    run fetches live and writes the snapshot on the first finished
    build.
    """
    snapshot = _read_run_memory_snapshot(run)
    if snapshot is not None and "decisions" in snapshot:
        nodes_data = snapshot.get("decisions") or []
        error = None
    else:
        try:
            payload = _collect_run_memory(run)
            nodes_data = payload.get("decisions") or []
            error = None
            # The first finished build writes the snapshot; subsequent
            # finished builds read it. Live runs skip the write so a slow
            # CN cannot corrupt an in-flight record.
            if not _running(run):
                _write_run_memory_snapshot(run, payload)
        except Exception as exc:  # noqa: BLE001 — the section must not blank the page
            return S.lst("Decisions and assumptions",
                        [S.dot(f"Could not read this: {type(exc).__name__}: {exc}",
                               mc="red")], full=True,
                        note="ContextNest fetches raised; the run page is built "
                             "from the snapshot on a finished run.")
    if error:
        return S.lst("Decisions and assumptions",
                      [S.dot(error, mc="red")], full=True)
    if not nodes_data:
        return None
    items: list[dict[str, Any]] = []
    total_atoms = 0
    for entry in nodes_data:
        if not isinstance(entry, dict):
            continue
        node = str(entry.get("node") or "?")
        sid = str(entry.get("session_id") or "")
        for atom in entry.get("atoms") or []:
            if not isinstance(atom, dict):
                continue
            kind = str(atom.get("kind") or "")
            label = f"{kind}: {atom.get('content') or ''}"
            sub_bits = [f"node {node}"]
            ts = str(atom.get("ts") or "")
            if ts:
                sub_bits.append(ts)
            acts = [S.btn("Open", S.page_link("cn_session", id=sid), "ghost")] if sid else []
            items.append(S.item(label, " · ".join(sub_bits),
                                m="•", mc="sub", acts=acts))
            total_atoms += 1
    if not items:
        return None
    note = (f"{len(nodes_data)} node session{'s' if len(nodes_data) != 1 else ''} · "
            f"{total_atoms} atom{'s' if total_atoms != 1 else ''} · "
            "open jumps to the source session.")
    return S.lst("Decisions and assumptions", items, full=True, note=note)


# ── sections the two page families share ────────────────────────────────────


# Patch artefacts a run can leave behind, most specific first. ``review-diff``
# is the reviewer-facing full diff; the others are older names for the same
# evidence. A run whose worktree was pruned keeps one of these on disk even
# when the card's ``files`` list was never stamped, which is why the page
# reads them before deciding a finished run changed nothing.
_DIFF_PATCH_NAMES = ("review-diff.patch", "framework-edit.diff", "diff.patch", "changes.diff")
_DIFF_FILE_HEADER = re.compile(r"^diff --git a/(.+?) b/(.+?)\s*$")


def _patch_changed_files(run: Run) -> list[dict[str, Any]]:
    """Per-file rows parsed from the run's recorded patch, ``[]`` when none.

    ``diffstat.json`` carries only totals, so the patch is the one run-local
    artefact that can still name the files after the worktree is gone. Counts
    skip ``+++``/``---`` headers the same way ``git diff --numstat`` does.
    """
    from mini_ork.cli.board_cmd import _project_file

    project = run.home.absolute().parent
    for name in _DIFF_PATCH_NAMES:
        path = run.run_dir / name
        if not path.is_file():
            continue
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        rows: dict[str, dict[str, Any]] = {}
        current: dict[str, Any] | None = None
        for line in lines:
            header = _DIFF_FILE_HEADER.match(line)
            if header:
                current = rows.setdefault(header.group(2),
                                          {"added": 0, "removed": 0})
                continue
            if current is None or line.startswith(("+++", "---")):
                continue
            if line.startswith("+"):
                current["added"] += 1
            elif line.startswith("-"):
                current["removed"] += 1
        if not rows:
            continue
        out = []
        for rel, counts in rows.items():
            display, absolute = _project_file(rel, project)
            out.append({"path": display, "abs": absolute,
                        "added": counts["added"], "removed": counts["removed"]})
        return out
    return []


def _card_files(run: Run) -> list[dict[str, Any]]:
    """The run's changed-file rows — ``path``/``abs``/``added``/``removed``.

    The card is the first source. A delivered run whose card carries no
    ``files`` (the worktree was pruned before the card was stamped) falls back
    to the recorded patch, so "no file changes recorded" is only said when
    there is genuinely no evidence of a change.
    """
    from mini_ork.cli.board_cmd import _card_fields

    files = list(_card_fields(run.card, run.home.absolute().parent).get("files") or [])
    if files:
        return files
    try:
        return _patch_changed_files(run)
    except Exception:  # noqa: BLE001 — a malformed patch costs the list, never the page
        return []


def _diff_totals(run: Run) -> tuple[int, int]:
    """``(added, removed)`` for the run's change, from the file rows or diffstat."""
    files = _card_files(run)
    if files:
        return (sum(int(f.get("added") or 0) for f in files),
                sum(int(f.get("removed") or 0) for f in files))
    try:
        data = json.loads((run.run_dir / "diffstat.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return 0, 0
    if not isinstance(data, dict):
        return 0, 0
    try:
        return int(data.get("added") or 0), int(data.get("removed") or 0)
    except (TypeError, ValueError):
        return 0, 0


def _diff_open_action(run: Run) -> dict[str, Any] | None:
    """A button that opens the run's own diff, when one is on disk."""
    for name in _DIFF_PATCH_NAMES:
        path = run.run_dir / name
        if path.is_file():
            return S.btn("Open diff", S.open_path(str(path)))
    return None


def _landed_commit(run: Run) -> str:
    """``<short sha> <subject>`` the publisher recorded, ``""`` when none.

    A finished run that landed its change proves it here: the run folder keeps
    the commit the publisher wrote, and without this row the page could not
    tell a landed change from a discarded one.
    """
    try:
        from mini_ork.ide_pages.node_changes import _fallback_recorded_commit

        commits, _note = _fallback_recorded_commit(run)
    except Exception:  # noqa: BLE001 — a missing commit costs its row, never the page
        return ""
    if not isinstance(commits, list) or not commits or not isinstance(commits[0], dict):
        return ""
    sha = str(commits[0].get("sha") or "")
    subject = str(commits[0].get("subject") or "").strip()
    if not sha:
        return ""
    return f"{sha[:12]} {subject}".strip()


def _attempts_section(run: Run) -> list[dict[str, Any]]:
    """Every dispatch of every node the retry loop had to re-run.

    ``retry-exhausted/<node>.attempts.json`` is the attempt ledger — one entry
    per dispatch, ``finish_reason`` and ``rc`` per entry. ``done`` with rc 0 is
    a success (the file is named for the node's history, not its final state),
    so the rows are classified per entry rather than per file. The ledger is
    the only place a reader can see that an implementer took three tries; the
    retry hint says where to continue, never how often it already tried.
    """
    ledger = run.run_dir / "retry-exhausted"
    try:
        paths = sorted(ledger.glob("*.attempts.json"))
    except OSError:
        return []
    items: list[dict[str, Any]] = []
    for path in paths:
        node = path.name[:-len(".attempts.json")]
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        entries = data if isinstance(data, list) else [data]
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            finish = str(entry.get("finish_reason") or "")
            rc = entry.get("rc")
            passed = finish == "done" and rc in (0, "0", None)
            when = _clock(int(entry["ts"])) if entry.get("ts") else ""
            sub = " · ".join(x for x in
                             (finish or "—",
                              f"rc={rc}" if rc is not None else "",
                              when) if x)
            items.append(S.item(f"{node} · attempt {entry.get('attempt') or '?'}", sub,
                                m="✓" if passed else "✗",
                                mc="green" if passed else "red"))
    if not items:
        return []
    return [S.lst(f"Attempts · {len(items)}", items, full=True,
                  note="One row per dispatch of a node. A failed attempt stays "
                       "listed after a later one succeeds.")]


def why_section(run: Run, *, title: str = "Why — evidence") -> dict[str, Any]:
    """The verifier verdicts: the page's own answer to "why did this end here".

    One row per ``verifier_*.json``, pass or fail, carrying the verifier's own
    reason. It is the section an operator actually needs after a failure, so the
    v2 story places it directly under the triage that states the outcome rather
    than leaving the reason to be inferred from a log tail. ``title`` is a
    parameter because the Overview tab has shown this list under a slightly
    different title since before the story existed, and that page is not this
    change's to rewrite.
    """
    items = []
    seen: set[str] = set()
    for path in sorted(run.run_dir.glob("verifier_*.json")):
        data = _json_obj(path)
        if data is None:
            continue
        name = str(data.get("verifier") or path.stem.removeprefix("verifier_"))
        seen.add(name)
        rc = data.get("post_rc")
        rc_text = f" rc={rc}" if rc not in (None, "") else ""
        # The reason keys, in the order the engine's other failure readers try
        # them (``outcome._first_failing_verifier``): a verifier states its case
        # in whichever one its shape uses, and a row reading only "FAIL" is the
        # thing this section exists to replace.
        reason = next((str(data.get(key) or "").strip() for key in
                       ("error_summary", "reason", "detail")
                       if str(data.get(key) or "").strip()), "")
        evidence_path = data.get("evidence_path") or ""
        sub = " · ".join(x for x in
                         (reason.splitlines()[0][:160] if reason else "",
                          _rel(evidence_path, run.home) if evidence_path else "") if x)
        acts = [S.btn("Open", S.open_path(evidence_path), "ghost")] \
            if evidence_path and Path(evidence_path).is_file() else []
        items.append(S.ok(f"{name} → PASS{rc_text}", sub, acts) if data.get("pass")
                     else S.bad(f"{name} → FAIL{rc_text}", sub, acts))
    # Reviewers and lenses write ``review-<id>.json`` instead, so the verifier
    # glob alone omitted half the panel: the DAG's Verifiers section counts
    # them, and a page whose reason section disagreed with its own DAG read as
    # two different outcomes for one run.
    for node in run.nodes:
        if node.id in seen:
            continue
        if not (node.type in _REVIEW_TYPES or "review" in node.id or "lens" in node.id):
            continue
        verdict, colour = _verdict(run, node)
        if colour == "sub":
            continue
        reason = _node_reason(run, node)
        sub = reason.splitlines()[0][:160] if reason else ""
        label = f"{node.id} → {verdict.upper()}"
        items.append(S.ok(label, sub) if colour == "green" else S.bad(label, sub))
    if not items:
        items = [S.dot("No verifiers have run",
                       "Zero verifiers would read vacuous, never success.")]
    return S.lst(title, items)


def recent_lines(run: Run) -> list[tuple[str, str]]:
    """The run's last events, oldest first, as ``(text, colour)`` lines."""
    from mini_ork.web.db import db_for

    db = db_for(run.home)
    rows = db.rows("SELECT event_type, payload_json, created_at FROM run_events WHERE run_id = ? "
                   "ORDER BY created_at DESC, rowid DESC LIMIT 8", (run.id,)) \
        if db.has_table("run_events") else []
    start = _epoch(run.row.get("created_at")) or 0
    lines = []
    for row in reversed(rows):
        try:
            payload = json.loads(row.get("payload_json") or "{}")
        except ValueError:
            payload = {}
        node = payload.get("node_id") or ""
        lane = payload.get("model_lane") or payload.get("lane") or ""
        finish = payload.get("finish_reason") or ""
        offset = S.duration(max(0, int(row.get("created_at") or start) - start)) if start else ""
        text = f"{offset:<8}  {row.get('event_type', ''):<11} {node}"
        if lane:
            text += f"  lane={lane}"
        if finish:
            text += f"  {finish}"
        lines.append((text, "red" if finish and finish != "done" else "muted"))
    return lines


def _log_step(step_id: str, title: str, tail: str) -> dict[str, Any]:
    """One collapsed step holding a log tail."""
    return S.story_step(step_id, title, kind="log",
                        body=[S.block_lines([(line, "muted") for line in tail.splitlines()])])


def diagnostics_sections(run: Run) -> list[dict[str, Any]]:
    """Raw run material — log tails, recent events, changed files — folded away.

    Each of these held prime space on the Overview tab: a log tail sat in a
    top-level section literally titled ``Detail``, second of eight. None of it
    explains anything by itself — a tail is evidence for one step, and a hundred
    lines of it buries the answer the operator opened the page for. One
    collapsed step each, so the section costs a row until someone opens the one
    they need, and nothing is lost: ``_tail_log`` is the same reader, and so the
    same text, the ``Detail`` block used.

    A run with no logs, no events and no changed files contributes nothing.
    """
    from mini_ork.recovery.retry_hint import _tail_log

    nodes: list[str] = []
    for pattern, prefix, suffix in (("impl-*.log", "impl-", ".log"),
                                    ("agent-*.live.jsonl", "agent-", ".live.jsonl")):
        for path in sorted(run.run_dir.glob(pattern)):
            node = path.name[len(prefix):-len(suffix)]
            if node and node not in nodes:
                nodes.append(node)
    steps = []
    for node in nodes:
        tail = _tail_log(run.run_dir, node)
        if tail:
            steps.append(_log_step(f"diag-log-{node}", f"Log tail — {node}", tail))
    if not steps:
        # No per-node log, so the run-wide one is the only tail there is — and it
        # is the one the old ``Detail`` block showed.
        tail = _tail_log(run.run_dir, "")
        if tail:
            steps.append(_log_step("diag-log-run", "Log tail — the run", tail))
    events = recent_lines(run)
    if events:
        steps.append(S.story_step("diag-events", "Recent events", kind="events",
                                  body=[S.block_lines(events)]))
    files = _card_files(run)
    if files:
        steps.append(S.story_step("diag-files", "Files changed", kind="files",
                                  body=[S.block_files([
                                      S.file_entry(str(f.get("path") or ""),
                                                   abs=str(f.get("abs") or ""),
                                                   added=int(f.get("added") or 0),
                                                   removed=int(f.get("removed") or 0))
                                      for f in files])]))
    if not steps:
        return []
    return [S.story("Diagnostics", steps,
                    note="Raw run material. Folded — open the one you need.")]


def _run_memory_sections(run: Run) -> list[dict[str, Any]]:
    """The run-page sections sourced from the run-memory snapshot.

    Currently: "Decisions and assumptions" only — the per-file history
    line lives inside the existing "Files changed" section so the
    changed-files view itself gets the recency annotation. Both read
    from the same snapshot (or compute once and persist it) so the
    finished-run page renders without ever asking ContextNest.

    A run with no atoms contributes nothing: "nothing was recorded" is not
    worth a card, and a card whose only content is its own emptiness is what
    made the page unreadable. A read that *failed* is a different fact and
    still gets one (:func:`S.guarded` is the same distinction).
    """
    try:
        section = _decisions_assumptions_section(run)
    except Exception as exc:  # noqa: BLE001 — additive sections must not blank the page
        return [S.lst("Decisions and assumptions",
                      [S.dot(f"Could not read this: {type(exc).__name__}: {exc}",
                             mc="red")], full=True)]
    return [section] if section is not None else []


def _title_v2(run: Run) -> str:
    """The kickoff title, else the goal's first line, else the run id.

    ``fleet.run_card`` derives the card title from the kickoff's first line and
    falls back to ``"<recipe> run"`` when the kickoff cannot be read. That
    placeholder carries no information about what the run is *for*, so it is
    skipped in favour of the operator's goal — otherwise a run whose kickoff
    lives only in ``run_profile.json`` would be titled "framework-edit run".
    """
    recipe = str(run.card.get("recipe") or run.row.get("recipe") or "")
    title = str(run.card.get("title") or "").strip()
    if title and title != f"{recipe or 'mini-ork'} run":
        # The card title is the heading cut to 80 chars; show the whole heading
        # when the card title is just its prefix.
        heading = str(_run_profile(run).get("user_goal") or "").strip()
        heading = heading.splitlines()[0].strip() if heading else ""
        if len(title) >= 80 and len(heading) > len(title) and heading.startswith(title):
            return heading[:200]
        return title
    goal = str(_run_profile(run).get("user_goal") or "").strip()
    if goal:
        first = goal.splitlines()[0].strip()
        if first:
            return first[:120]
    return run.id


def _open_question_count(run: Run) -> int:
    """The number the header chip shows: open ``asks/ask-N.json`` files.

    Deliberately not a "decisions blocking this run" count. A parked run and a
    run stopped on a retry gate each wait on the operator with no ask file
    behind them, but the chip beside the tab counts ask files only, and two
    numbers for the same thing — one in the header, one in the tab bar — read
    as a bug. Those runs are signalled by tone instead (see :func:`_v2_tabs`),
    never by an extra count here.
    """
    return len(open_asks(run.run_dir))


def _retry_decision_pending(run: Run) -> bool:
    """True when ``retry-gate.json`` points at an unresolved inbox row."""
    if not (run.run_dir / "retry-gate.json").is_file():
        return False
    from mini_ork.recovery.retry_notify import pending_fix_for_run

    return pending_fix_for_run(run.home, run.run_dir) is not None


def _questions_badge(run: Run) -> tuple[int, str]:
    """``(count, tone)`` for the DAG tab: the open asks and who is waiting.

    ``count`` is exactly what the header chip counts, so the tab bar and the
    chip beside it cannot disagree. A run that waits on the operator without
    an ask file — parked for review, or stopped on an unresolved retry gate —
    keeps the attention tone at count 0 rather than inventing a number the
    chip does not show. Nothing waiting is the quiet ``sub``.
    """
    count = _open_question_count(run)
    if count:
        return count, "yellow"
    try:
        waiting = _is_parked_run(run) or _retry_decision_pending(run)
    except Exception:  # noqa: BLE001 — an unreadable marker costs only the tone
        waiting = False
    return 0, "yellow" if waiting else "sub"


_CHECK_STATE_WORD = {"PROVEN": "PROVEN", "UNVERIFIED": "UNVERIFIED",
                     "REFUTED": "FAILED", "FAILED": "FAILED"}
_CHECK_ROW_STATE = {"PROVEN": "pass", "UNVERIFIED": "pending", "FAILED": "fail"}
# Plain words for each level, in the kickoff's own vocabulary. The Checks tab
# exists because "target UNVERIFIED" is not a sentence: the row says what the
# level means before it says whether the run met it.
_CHECK_LEVEL_MEANINGS = {
    "applies": "the change applies to the true base and touches only the files "
               "the kickoff named",
    "executes": "every changed file compiles and the change runs at the true base",
    "target": "the failing test now passes at the true base",
    "preserve": "the rest of the suite still passes at the true base",
    "contract": "the kickoff's promised artifact exists and matches its contract",
}
# Node types whose failure is a check the operator should see on this tab.
_CHECK_NODE_TYPES = _REVIEW_TYPES | {"verifier", "gate"}


def _failed_check_count(run: Run) -> int:
    """Levels the evidence refuted, plus failed verification nodes.

    ``UNVERIFIED`` is not a failure — the run simply has not proven that level
    — so it never turns the badge red. A reviewer/verifier node that actually
    failed is a check that failed even when the level vector is silent about
    it (``hr-smoke-d-191036``: the reviewer died, the vector only abstained).
    """
    report = O.verification_report(run) or {}
    levels = report.get("levels") if isinstance(report.get("levels"), dict) else {}
    failed = sum(1 for value in levels.values()
                 if str(value or "").upper() in ("REFUTED", "FAILED"))
    failed += sum(1 for node in run.nodes
                  if node.state == "failed" and node.type in _CHECK_NODE_TYPES)
    return failed


def _proven_count(run: Run) -> int:
    report = O.verification_report(run) or {}
    levels = report.get("levels") if isinstance(report.get("levels"), dict) else {}
    return sum(1 for value in levels.values() if str(value or "").upper() == "PROVEN")


def _checks_badge(run: Run) -> tuple[str | int, str]:
    """``(count, tone)`` for the Checks tab: failed first, else proven.

    A failed level is the one thing the badge must shout about, so it wins over
    the proven count. When nothing failed the proven count is the useful
    number; when the run has no vector at all the badge is quiet (``0 · sub``).
    """
    try:
        failed = _failed_check_count(run)
        if failed:
            return f"{failed} ✗", "red"
        proven = _proven_count(run)
        if proven:
            return proven, "green"
    except Exception:  # noqa: BLE001 — a broken vector costs the badge, never the page
        return 0, "sub"
    return 0, "sub"


def _learnings_count(run: Run) -> int:
    """How many lessons this run wrote back (gradients + learning rows)."""
    try:
        from mini_ork.web.db import db_for

        db = db_for(run.home)
        n = 0
        if db.has_table("gradient_records") and db.has_table("execution_traces"):
            row = db.row("SELECT COUNT(*) AS n FROM gradient_records g "
                         "JOIN execution_traces t ON t.trace_id = g.evidence "
                         "WHERE t.run_id = ?", (run.id,)) or {}
            n += int(row.get("n") or 0)
        if db.has_table("learning_record"):
            row = db.row("SELECT COUNT(*) AS n FROM learning_record WHERE run_id = ?",
                         (run.id,)) or {}
            n += int(row.get("n") or 0)
        return n
    except Exception:  # noqa: BLE001 — an unreadable ledger costs its badge
        return 0


def _v2_tabs(run: Run, mailbox: run_messages.Mailbox | None = None) -> list[dict[str, Any]]:
    """``V2_TABS`` with a ``count``/``tone`` badge where the tab has one.

    Labels stay the plain tab name (``Checks``, not ``Checks · 1``): the badge
    is its own field (CONTRACT v2.1), so the IDE draws it and an older reader
    still sees the same label. A tab whose content would be empty carries
    ``count: 0`` — the quiet state — rather than dropping out of the bar.
    """
    from mini_ork.cli.board_cmd import _artifacts

    try:
        # ``asks/`` holds conversation, not run output; counting it here would
        # put the same number on two tabs and read as a second, phantom build.
        # Live transcripts (``*.live.jsonl``) are streamed while a node runs and
        # would inflate the badge by one per in-flight node.
        artifacts = sum(1 for a in _artifacts(run.run_dir)
                        if not str(a.get("path") or "").startswith("asks/")
                        and not str(a.get("path") or "").endswith(".live.jsonl"))
    except Exception:  # noqa: BLE001
        artifacts = 0
    checks_count, checks_tone = _checks_badge(run)
    badges: dict[str, tuple[str | int, str]] = {
        # The questions tab folded into the DAG; its count rides here so a
        # stopped run still advertises itself from the tab bar. The header chip
        # counts ask files only; the badge also counts the review/retry
        # decisions that block a run with no ask file.
        # Count is ask files only, matching the header chip. A parked run or an
        # open retry decision is still waiting on the operator, so it keeps the
        # attention tone even at count 0; a run with nothing waiting is quiet.
        "dag": _questions_badge(run),
        "checks": (checks_count, checks_tone),
        "agents": (len(run.nodes), "sub"),
        "learnings": (_learnings_count(run), "sub"),
        "artifacts": (artifacts, "sub"),
    }
    messages_badge = run_messages.badge(mailbox) if mailbox is not None else None
    if messages_badge is not None:
        badges["messages"] = messages_badge
    out: list[dict[str, Any]] = []
    for key, label in V2_TABS:
        entry: dict[str, Any] = {"key": key, "label": label}
        if key in badges:
            entry["count"], entry["tone"] = badges[key]
        out.append(entry)
    return out


def _sub_v2(run: Run) -> str:
    parts = [str(run.card.get("recipe") or run.row.get("recipe") or "")]
    branch = _branch(run)
    if branch:
        parts.append(f"branch {branch}")
    parts.append(run.id)
    return " · ".join(p for p in parts if p)


def _sub_worktree(sub: str, run: Run) -> str:
    """Append `` · worktree <relpath>`` when the run has a workspace.

    The v2 sub lost the worktree path the legacy ``_sub`` still draws, so a
    reader could not tell where the change was written. Appended, never
    substituted: the recipe / title / branch / id already on the line stay in
    their order. The path is shown relative to the project root (home's
    parent), the same rule ``_sub`` uses.
    """
    ws = run.workspace
    if ws is None:
        return sub
    try:
        shown = str(Path(ws.path).relative_to(run.home.absolute().parent))
    except ValueError:
        shown = str(ws.path)
    except Exception:  # noqa: BLE001 — a broken workspace costs its segment
        return sub
    segment = f"worktree {shown}"
    return f"{sub} · {segment}" if sub else segment


def _read_run_json(run: Run, name: str) -> dict[str, Any]:
    """A JSON object from the run dir, ``{}`` when missing or unreadable."""
    try:
        data = json.loads((run.run_dir / name).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _read_run_plan(run: Run) -> dict[str, Any]:
    """The run's plan object, whatever name the recipe gave the file."""
    path = plan_artifact(run.run_dir, str((run.row or {}).get("plan_path") or ""))
    if path is None:
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _clip(text: str, limit: int) -> str:
    """``text`` cut at the last word boundary before ``limit``, ellipsis included."""
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    cut = text.rfind(" ", 0, limit)
    return f"{text[: cut if cut > 0 else limit].rstrip()}…"


def _prefix_of(left: str, right: str) -> bool:
    """True when either string is the other's prefix, ignoring case.

    The header draws the title directly above the description, so an intent
    that is the title (or a cut of it) would be the same words twice. Prefix
    rather than equality because the title is already truncated to 200 chars
    while the goal it came from is not.
    """
    a, b = left.strip().casefold(), right.strip().casefold()
    if not a or not b:
        return False
    return a.startswith(b) or b.startswith(a)


def _desc_intent(run: Run) -> str:
    """What the run is *for*, in words already on disk.

    The planner is the first model node and already writes the run's own
    one-sentence ``objective`` into ``plan.json`` — asking for a success
    criteria in plain language. It wins over the operator's goal because it
    describes *this* plan, not the brief. The goal's first line is next, then
    the run card's title. No prompt change is needed for this: the field the
    planner is already required to emit is exactly the sentence wanted here.
    """
    objective = str(_read_run_plan(run).get("objective") or "").strip()
    if objective:
        return objective
    goal = str(_run_profile(run).get("user_goal") or "").strip()
    if goal:
        first = goal.splitlines()[0].strip()
        if first:
            return first
    return str(run.card.get("title") or "").strip()


def _desc_node_label(node: Node) -> str:
    """``tier4_synth (opus55)`` — the node and the lane that served it."""
    family = str(node.family or "")
    return f"{node.id} ({family})" if family and family != "shell" else node.id


def _desc_headline(run: Run, node: Node) -> str:
    """The node's own headline — the same sentence its story row draws, so the
    header and the story can never describe the same node differently."""
    try:
        from mini_ork.ide_pages.node import _overview_headline

        return str(_overview_headline(run, node, run.run_dir).get("t") or "").strip()
    except Exception:  # noqa: BLE001 — a headline is never worth failing a page
        return ""


def _desc_outcome(run: Run, outcome: dict[str, Any] | None = None) -> str:
    """What *happened*: the run's own outcome sentence, and nothing else.

    The lead is ``outcome.resolve(run)["text"]`` — the exact sentence the
    Story tab's triage box shows — so the header and the page cannot disagree
    about what became of the run. Only when that sentence is generic (it does
    not name the deciding node) is the node's own headline added; otherwise it
    would be the same news twice.

    The counts (nodes, cost, elapsed) deliberately stay out: the triage counts
    sit under the verdict on the page and the header chips already carry the
    same numbers. Repeating them in the description made the header restate
    the page instead of introducing it.
    """
    nodes = run.nodes
    if not nodes:
        return ""
    failed = [n for n in nodes if n.state == "failed"]
    working = [n for n in nodes if n.state == "running"]

    lead = str((outcome or {}).get("text") or "").strip().rstrip(".")
    if not lead:
        lead = (f"Failed at {_desc_node_label(failed[0])}" if failed
                else f"Working: {working[0].id}" if working else "")
    if failed and failed[0].id.casefold() not in lead.casefold():
        detail = _desc_headline(run, failed[0])
        if detail:
            lead = f"{lead}: {detail}" if lead else detail
    return f"{lead}." if lead else ""


def _desc_wrap(text: str, width: int = _DESC_LINE_CHARS, lines: int = _DESC_LINES) -> str:
    """Wrap ``text`` into at most ``lines`` lines of ``width`` characters.

    The IDE draws one row per line, so the wrap is a layout contract: overflow
    is cut with an ellipsis rather than left to the renderer to clip.
    """
    rest = " ".join(text.split())
    out: list[str] = []
    while rest and len(out) < lines:
        if len(rest) <= width:
            out.append(rest)
            return "\n".join(out)
        cut = rest.rfind(" ", 0, width + 1)
        if cut <= 0:
            cut = width
        out.append(rest[:cut].rstrip())
        rest = rest[cut:].lstrip()
    if rest and out:
        out[-1] = f"{out[-1][: width - 1].rstrip()}…"
    return "\n".join(out)


def _desc_fold(run: Run, outcome: dict[str, Any] | None = None, title: str = "") -> str:
    """The run's description: one paragraph, at most ``_DESC_LINES`` lines —
    what the run is for, then what happened.

    Both halves are already written down somewhere (the planner's objective,
    the run's own outcome sentence, the nodes' headlines), so this only folds
    them: no model call, no new artefact on disk, and nothing that can go stale
    against the run. ``outcome`` is passed in when the caller has already
    resolved it, so the description never pays for a second read; ``title`` is
    what the header draws above it, so an intent that only repeats the title is
    dropped.

    The intent is clipped to two lines so the outcome always survives, and
    anything unreadable costs an empty string — the header then renders exactly
    as it did before the description existed.
    """
    try:
        intent = _clip(_desc_intent(run), _DESC_LINE_CHARS * 2)
        if _prefix_of(intent, title):
            intent = ""
        if outcome is None:
            outcome = O.resolve(run)
        rest = _desc_outcome(run, outcome)
    except Exception:  # noqa: BLE001 — a description is never worth failing a page
        return ""
    return _desc_wrap(" ".join(p for p in (intent, rest) if p))


# ── success criteria: what the run proved, criterion by criterion ────────────
#
# The hero lists the kickoff's criteria as prose and the Checks tab lists the
# levels, but nothing joined them: a green level never said *which* criterion it
# proved. This checklist joins them — one row per criterion, decided by the
# verdict's own criterion entries, then a failed check, then a PROVEN level's
# evidence, in that order and stopping at the first source that names the
# criterion. A run that merely finished decides nothing.

# The hero caps its list at 8; both readers must agree on what the run promised.
_CRITERIA_CAP = 8
_CRITERIA_WHY_LIMIT = 120
_CRITERIA_EVIDENCE_READ = 200_000
# Four-plus-char function words a substring test would otherwise match on —
# "without" in one criterion must not decide an unrelated check that also says
# "without". Content words ("lint", "suite", "dependencies") stay matchable.
_CRITERIA_STOPWORDS = frozenset({
    "the", "and", "but", "for", "with", "without", "from", "into", "onto",
    "over", "under", "this", "that", "these", "those", "there", "their",
    "them", "they", "when", "what", "which", "while", "your", "yours",
    "have", "has", "had", "been", "being", "were", "was", "are", "does",
    "did", "doing", "each", "every", "than", "then", "also", "only", "must",
    "should", "will", "shall", "would", "could", "stays", "still", "same",
    "more", "most", "such", "some", "other", "otherwise", "after", "before",
})
_CRITERIA_MET_WORDS = frozenset({"met", "pass", "passed", "ok", "done", "true",
                                 "yes", "green"})
_CRITERIA_UNMET_WORDS = frozenset({"unmet", "fail", "failed", "error", "false",
                                   "no", "red"})
_CRITERIA_CHECK_FAIL_WORDS = frozenset({"fail", "failed", "error", "unmet"})


def _criteria_words(text: Any) -> list[str]:
    """Key words of ``text``: alphanumeric runs of 4+ chars, function words out."""
    return [w for w in re.findall(r"[a-z0-9]+", str(text or "").casefold())
            if len(w) >= 4 and w not in _CRITERIA_STOPWORDS]


def _criterion_named(criterion: str, text: Any) -> bool:
    """True when ``text`` names ``criterion`` through one of its key words.

    A case-folded substring test on 4+ char words is the fallback the kickoff
    prescribes when the codebase has no token normaliser of its own.
    """
    haystack = str(text or "").casefold()
    return any(word in haystack for word in _criteria_words(criterion))


def _criteria_read(path: Path) -> str:
    """A file's text for criterion matching, empty when it cannot be read."""
    try:
        return path.read_text(encoding="utf-8", errors="replace")[:_CRITERIA_EVIDENCE_READ]
    except OSError:
        return ""


def _criteria_path_action(run: Run, candidates: Iterable[Any]) -> dict[str, Any] | None:
    """``S.open_path`` for the first candidate that is a real file, else ``None``.

    ``candidates`` carry paths as the verdict wrote them; both absolute and
    run-relative forms resolve through ``run.run_dir``. A link to a file that
    does not exist is worse than no link, so only ``is_file`` earns one.
    """
    for value in candidates:
        if not isinstance(value, str) or not value.strip():
            continue
        path = run.run_dir / value
        try:
            if path.is_file():
                return S.open_path(str(path))
        except OSError:
            continue
    return None


def _criteria_envelopes(run: Run) -> list[tuple[Path, dict[str, Any]]]:
    """Every readable ``verifier_*.json`` envelope, sorted by name."""
    out: list[tuple[Path, dict[str, Any]]] = []
    try:
        paths = sorted(run.run_dir.glob("verifier_*.json"))
    except OSError:
        return out
    for path in paths:
        data = _json_obj(path)
        if isinstance(data, dict):
            out.append((path, data))
    return out


def _criteria_verdict_entries(run: Run) -> list[tuple[Path, dict[str, Any]]]:
    """Criterion entries from the run's verdict files, in decision order.

    ``run-verdict.json`` comes first (the executor's own report), then
    ``framework-edit.verdict.json``. Both ``criteria`` and ``criteria_results``
    shapes are read. A malformed file is simply absent — never fake evidence.
    """
    out: list[tuple[Path, dict[str, Any]]] = []
    for name in ("run-verdict.json", "framework-edit.verdict.json"):
        path = run.run_dir / name
        data = _json_obj(path)
        if not isinstance(data, dict):
            continue
        for key in ("criteria", "criteria_results"):
            entries = data.get(key)
            if not isinstance(entries, list):
                continue
            for entry in entries:
                if isinstance(entry, dict):
                    out.append((path, entry))
    return out


def _criteria_state_word(value: Any) -> str:
    if isinstance(value, bool):
        return "met" if value else "unmet"
    word = str(value or "").strip().casefold()
    if word in _CRITERIA_MET_WORDS:
        return "met"
    if word in _CRITERIA_UNMET_WORDS:
        return "unmet"
    return "unknown"


def _criteria_verdict_state(entry: dict[str, Any]) -> str:
    """A verdict entry's decision, tolerating ``met`` / ``state`` / ``status``."""
    for key in ("met", "state", "status", "verdict", "result"):
        if key not in entry:
            continue
        state = _criteria_state_word(entry.get(key))
        if state != "unknown":
            return state
    return "unknown"


def _criteria_from_verdict(run: Run, entries: list[tuple[Path, dict[str, Any]]],
                           criterion: str) -> dict[str, Any] | None:
    """The first verdict entry naming ``criterion``, or ``None``."""
    wanted = " ".join(criterion.split()).casefold()
    for path, entry in entries:
        raw = entry.get("criterion")
        if raw is None:
            raw = entry.get("c")
        if not isinstance(raw, str):
            continue
        if " ".join(raw.split()).casefold() != wanted:
            continue
        state = _criteria_verdict_state(entry)
        evidence: list[Any] = []
        for key in ("evidence", "evidence_path", "path", "file", "artifact"):
            value = entry.get(key)
            if isinstance(value, str):
                evidence.append(value)
            elif isinstance(value, list):
                evidence.extend(v for v in value if isinstance(v, str))
        do = _criteria_path_action(run, evidence) or S.open_path(str(path))
        name = Path(str((do or {}).get("path") or "")).name
        if state == "met":
            why = f"Run verdict marks this met — {name}." if name else \
                "The run verdict marks this met."
        elif state == "unmet":
            why = f"Run verdict marks this unmet — {name}." if name else \
                "The run verdict marks this unmet."
        else:
            why = "The run verdict records no decision for this criterion."
        return {"c": criterion, "state": state,
                "why": _clip(why, _CRITERIA_WHY_LIMIT), "do": do}
    return None


def _criteria_failed_checks(envelopes: list[tuple[Path, dict[str, Any]]]
                            ) -> list[tuple[str, str, str]]:
    """``(name, matched text, target)`` for every failed check/probe.

    The matched text is the check's own title / command / detail — never the
    whole envelope, so a passing sibling cannot lend it words it did not use.
    """
    out: list[tuple[str, str, str]] = []
    for path, envelope in envelopes:
        evidence = str(envelope.get("evidence_path") or "")
        target = evidence if evidence and Path(evidence).is_file() else str(path)
        found = False
        for key in ("checks", "probes"):
            items = envelope.get(key)
            if not isinstance(items, list):
                continue
            for check in items:
                if not isinstance(check, dict):
                    continue
                word = str(check.get("state") or check.get("status") or "").casefold()
                if check.get("pass") is not False and word not in _CRITERIA_CHECK_FAIL_WORDS:
                    continue
                found = True
                name = str(check.get("name") or check.get("title")
                           or check.get("command") or check.get("probe") or "check")
                text = " ".join(str(check.get(k) or "") for k in
                                ("name", "title", "command", "probe", "detail",
                                 "expected", "actual"))
                out.append((name, text, target))
        if not found and envelope.get("pass") is False:
            # A verifier that failed before emitting per-check rows still states
            # its case in its own summary; it decides only what it names.
            name = str(envelope.get("verifier") or path.stem.removeprefix("verifier_"))
            text = " ".join(str(envelope.get(k) or "") for k in
                            ("verifier", "error_summary", "reason", "detail"))
            out.append((name, text, target))
    return out


def _criteria_proven_levels(run: Run, envelopes: list[tuple[Path, dict[str, Any]]]
                            ) -> list[tuple[str, str, str]]:
    """``(level, reason, evidence text)`` for every PROVEN level in the report."""
    try:
        report = O.verification_report(run) or {}
    except Exception:  # noqa: BLE001 — a broken verdict costs this source
        return []
    levels = report.get("levels") if isinstance(report.get("levels"), dict) else {}
    reasons = report.get("levels_reasons") if isinstance(report.get("levels_reasons"), dict) else {}
    out: list[tuple[str, str, str]] = []
    for level, value in levels.items():
        if str(value or "").strip().upper() != "PROVEN":
            continue
        reason = str(reasons.get(level) or "")
        texts = [reason]
        # The reason names the file that decided the level (``verifier_test.json:
        # …``); its text is the evidence the criterion is matched against.
        for name in re.findall(r"[A-Za-z0-9_.-]+\.(?:json|log|md|txt|patch)", reason):
            texts.append(_criteria_read(run.run_dir / name))
        for path, envelope in envelopes:
            names = (path.name, str(envelope.get("verifier") or ""))
            if not any(n and n in reason for n in names):
                continue
            texts.append(_criteria_read(Path(str(envelope.get("evidence_path") or ""))))
        out.append((str(level), reason, "\n".join(t for t in texts if t)))
    return out


def _criteria_row(run: Run, criterion: str,
                  verdict_entries: list[tuple[Path, dict[str, Any]]],
                  failed_checks: list[tuple[str, str, str]],
                  proven_levels: list[tuple[str, str, str]],
                  envelopes: list[tuple[Path, dict[str, Any]]]) -> dict[str, Any]:
    """One criterion's row, decided in the kickoff's source order."""
    row = _criteria_from_verdict(run, verdict_entries, criterion)
    if row is not None:
        return row
    for name, text, target in failed_checks:
        if _criterion_named(criterion, text):
            return {"c": criterion, "state": "unmet",
                    "why": _clip(f"Check '{name}' failed.", _CRITERIA_WHY_LIMIT),
                    "do": S.open_path(target)}
    for level, reason, text in proven_levels:
        if _criterion_named(criterion, text):
            why = (f"Level {level} is PROVEN: {reason}." if reason
                   else f"Level {level} is PROVEN.")
            return {"c": criterion, "state": "met",
                    "why": _clip(why, _CRITERIA_WHY_LIMIT),
                    "do": _checks_evidence_action(run, level, reason, envelopes)
                          or S.page_link("run", "checks", run=run.id)}
    return {"c": criterion, "state": "unknown",
            "why": "No check names this criterion.", "do": None}


def _criteria_checklist(run: Run, outcome: dict[str, Any]) -> list[dict[str, Any]]:
    """One row per kickoff success criterion: ``met`` / ``unmet`` / ``unknown``.

    The list is the hero's — ``run_profile.json``'s ``success_criteria``, capped
    the same way — so the two never disagree about what the run promised. Every
    source is read fail-soft: a missing or malformed verdict costs ``unknown``
    rows, never the section or the page. The rows are also stamped on
    ``outcome["criteria"]`` so the IDE can draw them without parsing sections;
    a run with no criteria gets no key.
    """
    try:
        raw = _run_profile(run).get("success_criteria")
    except Exception:  # noqa: BLE001 — an unreadable profile costs the checklist
        return []
    criteria = [str(c) for c in raw] if isinstance(raw, list) else []
    criteria = criteria[:_CRITERIA_CAP]
    if not criteria:
        return []
    try:
        verdict_entries = _criteria_verdict_entries(run)
    except Exception:  # noqa: BLE001 — one unreadable verdict costs its entries
        verdict_entries = []
    envelopes = _criteria_envelopes(run)
    try:
        failed_checks = _criteria_failed_checks(envelopes)
    except Exception:  # noqa: BLE001
        failed_checks = []
    try:
        proven_levels = _criteria_proven_levels(run, envelopes)
    except Exception:  # noqa: BLE001
        proven_levels = []
    rows = [_criteria_row(run, criterion, verdict_entries, failed_checks,
                          proven_levels, envelopes)
            for criterion in criteria]
    try:
        outcome["criteria"] = rows
    except (TypeError, AttributeError):
        pass
    return rows


_CRITERIA_COLS = [S.col(90), S.col(fr=2, min=220), S.col(fr=3, min=260)]
_CRITERIA_TONE = {"met": "green", "unmet": "red"}


def _criteria_section(run: Run, outcome: dict[str, Any]) -> list[dict[str, Any]]:
    """The checklist as one table section; empty when the run has no criteria."""
    rows = _criteria_checklist(run, outcome)
    if not rows:
        return []
    table_rows = []
    for row in rows:
        state = str(row["state"])
        table_rows.append({
            "cells": [S.cell(state.upper(), _CRITERIA_TONE.get(state, "sub"), b=True),
                      S.cell(row["c"]),
                      S.cell(row["why"], "sub")],
            "do": row.get("do"),
        })
    met = sum(1 for row in rows if row["state"] == "met")
    unmet = sum(1 for row in rows if row["state"] == "unmet")
    return [S.table(
        "Did it meet the criteria", _CRITERIA_COLS, ["", "Criterion", "Why"],
        table_rows, full=True,
        sub=f"{met} met · {unmet} unmet · {len(rows) - met - unmet} unknown",
        note="A criterion is met only when the verdict or a PROVEN level "
             "names it and its evidence.")]


def _outcome_tab(run: Run, outcome: dict[str, Any],
                 callout_actions: Iterable[dict[str, Any]] | None = None,
                 absorbed: set[str] | tuple[str, ...] = ()
                 ) -> list[dict[str, Any]]:
    """The v2 outcome: what happened, what it was for, what it ran on.

    Order is the design's: the verdict, the evidence under it, the goal and
    its numbers, then the run's own facts — how it was placed, when, on what
    branch — before the operator can steer it, read the story, and see the
    decision material. Everything the old Overview tab carried that has no
    newer home (retry hints, inputs, orchestrator, correlation, events, files)
    tails the page rather than being dropped.

    ``callout_actions`` is the outcome's own buttons minus the ones the header
    draws, so the verdict carries the design's ``bodyActs`` without repeating
    a control the header already offers.

    ``absorbed`` names the callouts the needs-you lane already carries (the
    retry decision, when the lane owns it): they are dropped here so one
    decision has one block on the tab.
    """
    triage = S.triage(outcome["text"], tone=outcome["tone"], icon=outcome["icon"],
                      detail=outcome["detail"], counts=outcome["counts"],
                      actions=list(callout_actions or ()), menu=[])
    # The node and reason the triage names, when there is one. ``spec.triage``
    # has no keyword for them, so they are attached additively here — an IDE
    # that does not know the keys ignores them, and a run with no failed or
    # blocked node omits both rather than guessing.
    if str(outcome.get("node") or "").strip():
        triage["node"] = str(outcome["node"]).strip()
    if str(outcome.get("reason") or "").strip():
        triage["reason"] = str(outcome["reason"]).strip()
    sections = [triage]
    # The Spend section sits directly under the outcome summary: the verdict
    # says what happened, this says what it cost against its caps. One shared
    # builder with the Checks tab so the two sections are always equal.
    sections.append(_spend_section(run))
    dropped = set(absorbed or ())
    # The criteria checklist, directly under the verdict: the triage states what
    # happened, this states which of the run's promises the evidence actually
    # proves. Additive — every existing section keeps its place below it.
    sections.extend(S.guarded({}, "Did it meet the criteria",
                              lambda: _criteria_section(run, outcome)))
    # The reason, directly under the verdict it explains. The Overview tab has
    # always carried this list; the story tab had no path to it at all, so a
    # failure was stated and never explained.
    sections.extend(S.guarded({}, "Why — evidence", lambda: why_section(run)))
    profile = _run_profile(run)
    goal = str(profile.get("user_goal") or "").strip()
    raw_criteria = profile.get("success_criteria")
    criteria = [str(c) for c in raw_criteria] if isinstance(raw_criteria, list) else []
    # The hero's goal is the kickoff's first heading, which is also the run
    # page title — so an unedited goal would repeat the title word for word.
    # Blank the hero goal when it matches the title, leaving just the criteria.
    title = _title_v2(run).strip().casefold()
    if goal and title and goal.casefold().startswith(title) \
            and (goal.casefold() == title or len(title) >= 80):
        goal = ""
    files = _card_files(run)
    added, removed = _diff_totals(run)
    # Progress, cost and elapsed are the header chips' job — they stay visible
    # on every tab. The hero answers what this run is *for*, so its cells carry
    # the one fact the chips do not: how much code moved.
    cells = []
    if files or added or removed:
        cells.append((f"+{added} −{removed}", "lines changed", "blue"))
    if goal or criteria or cells:
        sections.append(S.hero("What this run is for", goal=goal,
                               criteria=criteria[:8], cells=cells))

    def run_facts() -> dict[str, Any]:
        recipe = str(run.card.get("recipe") or run.row.get("recipe") or "")
        task_class = str(profile.get("task_class") or run.row.get("task_class") or "")
        workspace = run.workspace
        worktree = branch = ""
        if workspace is not None:
            worktree = _rel(str(workspace.path), run.home.absolute().parent)
            branch = str(workspace.branch or "")
        started = _epoch(run.row.get("created_at"))
        _, kickoff_file = _resolve_kickoff(run)
        kickoff = Path(kickoff_file).name if kickoff_file else ""
        risk = str(profile.get("risk_tolerance") or "")
        commit = _landed_commit(run)
        environment = ("remote · " + str(profile.get("environment") or "")) \
            if str(profile.get("placement") or "").lower() == "remote" else "local"
        items = [
            ("Recipe", recipe or "—"),
            ("Task class", task_class or "—"),
            ("Worktree", worktree or "—"),
            ("Branch", branch or "—"),
            ("Commit", commit or "—"),
            ("Started", _clock(started) if started else "—"),
            ("Kickoff", kickoff or "—"),
            ("Risk", risk or "—"),
            ("Environment", environment),
        ]
        actions = []
        if kickoff_file:
            actions.append(S.btn("Open kickoff", S.open_path(kickoff_file), "ghost"))
        if workspace is not None:
            actions.append(S.btn("Open worktree", S.open_path(str(workspace.path)), "ghost"))
        return S.kv("This run", items, sub=run.id, cols=4, actions=actions, full=True)

    sections.extend(S.guarded({}, "This run", run_facts))
    # Imported inside the function: ``node.py`` imports ``run.py``, so a
    # top-level ``run_story`` import (which pulls in ``node.py``) would be
    # circular. Guarded like the Overview sections it replaces: a broken story
    # costs the story, never the triage / callouts / hero above it.
    from mini_ork.ide_pages import run_story

    # The snapshot path keeps a finished run from re-fetching ContextNest;
    # live runs go through the section cache and never persist the snapshot.
    sections.extend(S.guarded({}, "What happened", lambda: run_story.story_section(run)))
    sections.extend(S.guarded({}, "Decisions and assumptions",
                              lambda: _run_memory_sections(run)))
    # ``Seen before`` rides the failed card's callouts. It is prior art on the
    # same failure, so it belongs with the decision material below the narrative
    # — not wedged between the verdict and the reason that explains it.
    sections.extend(c for c in outcome.get("callouts") or []
                    if str(c.get("title") or "") not in dropped)
    sections.extend(S.guarded({}, "Diagnostics", lambda: diagnostics_sections(run)))
    # A live run can still be steered: the box runs ``mini-ork board steer
    # <run> --text <typed>``. Finished runs get no composer — there is nothing
    # left to steer. ``_running`` is the page's own running test (the one the
    # live chip and ``_actions`` use), so the composer cannot appear on a run
    # the header already calls done.
    if _running(run):
        sections.append(S.composer(
            "Tell the running agent what to change… (Enter to send)",
            ["board", "steer", run.id, "--text"],
            title="Steer this run"))
    # The Overview tab's tail is not this page's tail. The orchestrator has its
    # own tab, correlation and recent events are diagnostics, the file list
    # lives on Artifacts, and the memory sections are already above. What is
    # genuinely outcome-only is the retry decision and what the run knew going
    # in; the attempt ledger explains a failure the retry hint is about.
    already = {str(section.get("title") or "") for section in sections}
    keep = {"Retry", "Run inputs"}
    sections.extend(section for section in _overview_tab(run)
                    if str(section.get("title") or "") in keep
                    and str(section.get("title") or "") not in already)
    sections.extend(_attempts_section(run))
    return sections


def _verification_tab(run: Run) -> list[dict[str, Any]]:
    """The run's verification in full — the five-level vector and every check.

    The vector is shipped as data (:func:`spec.levels`): value, whether the
    task class required it, and why it abstained. Which levels to foreground,
    and how an abstention that could not fail the run should read, is the
    renderer's call — the engine does not decide that here.
    """
    report = O.verification_report(run) or {}
    level_values = report.get("levels") if isinstance(report.get("levels"), dict) else {}
    reasons = report.get("levels_reasons") if isinstance(report.get("levels_reasons"), dict) else {}
    stamped = report.get("levels_required")
    stamped_required = ([str(name) for name in stamped]
                        if isinstance(stamped, list) else None)
    profile = _run_profile(run)
    task_class = str(profile.get("task_class") or run.row.get("task_class") or "")
    decision = str(report.get("levels_decision") or "")
    sections: list[dict[str, Any]] = []

    if not level_values:
        sections.append(S.callout(
            "No verification report for this run",
            "The executor stamps the five-level vector when it writes the run's "
            "verdict; this run has neither `run-verdict.json` levels nor a stamped "
            "`verdict.json`. The verifier checks below are the run's own evidence.",
            tone="muted"))
    else:
        rows = []
        for name in O.LEVELS:
            rows.append(S.level_row(
                name, str(level_values.get(name) or "—"),
                required=(name in stamped_required) if stamped_required is not None else None,
                reason=str(reasons.get(name) or "")))
        sections.append(S.levels("Levels", rows, decision=decision,
                                 task_class=task_class, full=True))

    # One section per verifier envelope: the same files the ``checks n/m``
    # count reads, with each check's own verdict rather than only the roll-up.
    for path in sorted(run.run_dir.glob("verifier_*.json")):
        envelope = _json_obj(path)
        if not isinstance(envelope, dict):
            continue
        name = str(envelope.get("verifier") or path.stem.removeprefix("verifier_"))
        checks = envelope.get("checks")
        rows = []
        if isinstance(checks, list):
            for check in checks:
                if not isinstance(check, dict):
                    continue
                check_passed = bool(check.get("pass"))
                detail = str(check.get("expected") or "").strip()
                if not check_passed:
                    detail = " · ".join(part for part in
                                        (detail, str(check.get("actual") or "").strip()) if part)
                rows.append(S.check_row(str(check.get("name") or "check"),
                                        "pass" if check_passed else "fail",
                                        detail=detail))
        evidence = str(envelope.get("evidence_path") or "")
        actions = ([S.btn("Open evidence", S.open_path(evidence), "ghost")]
                   if evidence and Path(evidence).is_file() else [])
        passed = bool(envelope.get("pass"))
        title = f"{name} · {'pass' if passed else 'FAIL'}"
        if rows:
            sections.append(S.checks(
                title, rows, full=True, actions=actions,
                sub=f"{sum(1 for row in rows if row['state'] == 'pass')}/{len(rows)} checks"))
        else:
            item = S.ok(title) if passed else S.bad(title)
            sections.append(S.lst(name, [item], full=True, actions=actions))
    return sections


def _checks_evidence_action(run: Run, level: str, reason: str,
                            envelopes: list[tuple[Path, dict[str, Any]]]
                            ) -> dict[str, Any] | None:
    """The click-through that decided ``level``, when one exists.

    Two sources, in order of directness: the file the level's own reason names
    (``verifier_test.json: …``), then a verifier envelope whose name or checks
    talk about this level. A level with no decidable evidence gets no action —
    a dead "Open evidence" button would be worse than none.
    """
    for name in re.findall(r"[A-Za-z0-9_.-]+\.(?:json|log|md|txt|patch)", reason or ""):
        path = run.run_dir / name
        try:
            if path.is_file():
                return S.open_path(str(path))
        except OSError:
            continue
    pattern = re.compile(rf"\b{re.escape(level)}\b", re.I)
    for path, envelope in envelopes:
        try:
            checks = envelope.get("checks")
            words = [str(envelope.get("verifier") or ""), str(envelope.get("error_summary") or "")]
            if isinstance(checks, list):
                for check in checks:
                    if isinstance(check, dict):
                        words.extend(str(check.get(key) or "")
                                     for key in ("name", "detail", "expected", "actual"))
            if not any(pattern.search(word) for word in words):
                continue
            evidence = str(envelope.get("evidence_path") or "")
            target = evidence if evidence and Path(evidence).is_file() else str(path)
            return S.open_path(target)
        except (OSError, TypeError, ValueError):
            continue
    return None


def _checks_envelope_sections(run: Run) -> list[dict[str, Any]]:
    """The checks behind the vector, one section per ``verifier_*.json``.

    Same envelopes :func:`verifier_check_counts` counts, with each check's own
    verdict and the file that decided it. One broken envelope costs its own
    section, never the tab.
    """
    sections: list[dict[str, Any]] = []
    for path in sorted(run.run_dir.glob("verifier_*.json")):
        try:
            envelope = _json_obj(path)
            if not isinstance(envelope, dict):
                continue
            name = str(envelope.get("verifier") or path.stem.removeprefix("verifier_"))
            checks = envelope.get("checks")
            rows = []
            if isinstance(checks, list):
                for check in checks:
                    if not isinstance(check, dict):
                        continue
                    check_passed = bool(check.get("pass"))
                    detail = str(check.get("expected") or "").strip()
                    if not check_passed:
                        detail = " · ".join(part for part in
                                            (detail, str(check.get("actual") or "").strip())
                                            if part)
                    rows.append(S.check_row(str(check.get("name") or "check"),
                                            "pass" if check_passed else "fail",
                                            detail=detail))
            evidence = str(envelope.get("evidence_path") or "")
            if evidence and Path(evidence).is_file():
                actions = [S.btn("Open evidence", S.open_path(evidence), "ghost")]
            else:
                actions = [S.btn("Open check row", S.open_path(str(path)), "ghost")]
            passed = bool(envelope.get("pass"))
            title = f"{name} · {'pass' if passed else 'FAIL'}"
            if rows:
                sections.append(S.checks(
                    title, rows, full=True, actions=actions,
                    sub=f"{sum(1 for row in rows if row['state'] == 'pass')}/{len(rows)} checks"))
            else:
                item = S.ok(title) if passed else S.bad(title)
                sections.append(S.lst(name, [item], full=True, actions=actions))
        except Exception:  # noqa: BLE001 — one broken envelope costs its section
            continue
    return sections


def _checks_tab(run: Run) -> list[dict[str, Any]]:
    """The verification levels, one self-explaining row each.

    Every row carries its state in the kickoff's three-word vocabulary, a
    plain-words sentence saying what the level means, and — when the run's
    evidence names one — a click-through to the file that decided it. Sources
    are read fail-soft: a missing or malformed verdict costs the data on that
    row, never the tab.
    """
    try:
        report = O.verification_report(run) or {}
    except Exception:  # noqa: BLE001 — a broken verdict costs the rows' data
        report = {}
    levels = report.get("levels") if isinstance(report.get("levels"), dict) else {}
    reasons = report.get("levels_reasons") if isinstance(report.get("levels_reasons"), dict) else {}
    envelopes: list[tuple[Path, dict[str, Any]]] = []
    try:
        for path in sorted(run.run_dir.glob("verifier_*.json")):
            envelope = _json_obj(path)
            if isinstance(envelope, dict):
                envelopes.append((path, envelope))
    except OSError:
        envelopes = []

    rows = []
    for level in O.LEVELS:
        raw = str(levels.get(level) or "").upper()
        state = _CHECK_STATE_WORD.get(raw, "UNVERIFIED")
        reason = str(reasons.get(level) or "").strip()
        meaning = _CHECK_LEVEL_MEANINGS.get(level, "a required check on this run")
        detail = f"{state} — {meaning}."
        if reason:
            detail += f" Why: {reason}"
        rows.append(S.check_row(
            level, _CHECK_ROW_STATE[state], detail=detail,
            do=_checks_evidence_action(run, level, reason, envelopes)))
    sections = [S.checks(
        "Verification levels", rows, full=True,
        note="PROVEN = the evidence at the true base shows it; UNVERIFIED = not "
             "proven yet; FAILED = the evidence refutes it. Click a row to open "
             "the file that decided it.")]
    # Spend follows the level rows: the checks say whether the run proved what
    # it promised, the section under them says what proving it cost.
    sections.append(_spend_section(run))
    # The vector itself stays shipped as data for the renderer (``spec.levels``),
    # and the per-check envelopes below mirror ``_checks_envelope_sections`` —
    # so only the non-check sections of the old tab ride along here.
    try:
        sections.extend(s for s in _verification_tab(run)
                        if s.get("type") != "checks")
    except Exception:  # noqa: BLE001 — the data vector costs its section, not the tab
        pass
    sections.extend(_checks_envelope_sections(run))
    return sections


def _story_tab(run: Run, outcome: dict[str, Any]) -> list[dict[str, Any]]:
    """The retired Story tab: the outcome tab under its old name.

    The tab bar no longer offers it (``story`` resolves to ``outcome``); the
    name survives because the header tests pin the triage it leads with.
    """
    return _outcome_tab(run, outcome)


# Patch artefacts a run can leave behind, in the order the Changes tab's
# "Open as patch" action prefers them.
_PATCH_CANDIDATES = ("framework-edit.diff", "diff.patch", "changes.diff")


def _patch_action(run: Run) -> dict[str, Any] | None:
    """``Open as patch`` for the first patch artefact on disk, else ``None``.

    A button is only emitted for a file that really exists — a dead link is
    worse than no link.
    """
    for name in _PATCH_CANDIDATES:
        path = run.run_dir / name
        if path.is_file():
            return S.btn("Open as patch", S.open_path(str(path)))
    return None


def _changes_tab(run: Run, errors: dict[str, str]) -> list[dict[str, Any]]:
    """The Changes tab: the dock's changes sections, reused fail-soft.

    The dock already owns the diff loader, the changed-file list and the
    commits (``run_dock._changes_sections``), so this tab composes that
    builder rather than re-deriving it. Imported lazily because ``run_dock``
    imports this module — the same dance as ``_attach_flow``. A builder that
    raises costs the tab one quiet empty-state section, never the page.
    """
    try:
        from mini_ork.ide_pages import run_dock

        sections = run_dock._changes_sections(run, errors)
    except Exception:  # noqa: BLE001 — a broken builder must not blank the tab
        sections = []
    if not isinstance(sections, list) or not sections:
        sections = [S.lst("Changed files",
                          [S.dot("No changes recorded for this run")], full=False)]
    action = _patch_action(run)
    if action is not None:
        first = dict(sections[0])
        first["actions"] = [action, *(first.get("actions") or [])]
        sections[0] = first
    return sections


def _orch_args(args: dict[str, Any]) -> dict[str, Any]:
    """The Orchestrator tab's view state, carried through the page's own args.

    Filters are server-side (``S.set_args`` round-trips them), so the page has
    to echo the current ones or a refresh would drop the reader back to "all".
    """
    return {key: args[key] for key in ("orch_view", "orch_limit") if args.get(key)}


def _build_v2(run: Run, tab: str | None, args: dict[str, str]) -> dict[str, Any]:
    """The ``MINI_ORK_IDE_SPEC=2`` page: the run's title, its outcome, its graph.

    The design leads with the DAG. The header leads with the human title when
    there is one and puts the recipe / branch on the sub line; the run id is
    the copyable handle the IDE draws beside the chips. A header that led with
    ``run-1792233993-49139`` made the reader decode a machine handle before
    they knew what the page was about.
    """
    outcome = O.resolve(run)
    # The IDE badges the header from this block without parsing sections. A run
    # with no recorded cost emits no key at all, so the badge stays absent for
    # a run that never spent rather than reading as $0.00 spent.
    spend = _spend_numbers(run)
    if spend is not None:
        outcome["spend"] = spend
    keys = {k for k, _ in V2_TABS}
    wanted = (tab or "").strip()
    resolved = V2_ALIASES.get(wanted, wanted)
    if resolved not in keys:
        resolved = "dag"
    node = args.get("node")
    # One needs-you lane, at the top of every v2 tab. ``absorbed`` names the
    # outcome callouts the lane already owns, so the tab does not draw the
    # retry decision twice. A broken lane costs the lane, never the tab.
    try:
        lane, absorbed = _attention_lane(run, outcome)
    except Exception:  # noqa: BLE001 — a broken lane must not blank the page
        lane, absorbed = [], set()
    # The header fields are the same on every tab: the label names the run and
    # the controls (Stop/Kill while it runs, Merge/Discard when it is done) sit
    # beside them, so a user on the DAG tab can still stop a runaway run.
    header = build_header(run, outcome)
    actions = [a for a in (S.btn("Open run folder", S.open_path(str(run.run_dir))),
                           _orchestrator_action(run)) if a]
    # One label, one control: subtract everything the header draws — its own
    # buttons plus the page's — before the outcome's remaining buttons go to
    # the callout body (the design's ``bodyActs``).
    drawn = {str(a.get("label") or "") for a in header["actions"] + actions}
    callout_actions = [a for a in (outcome.get("actions") or [])
                       if isinstance(a, dict) and str(a.get("label") or "") not in drawn]
    # The design's done state offers the diff beside the verdict. Opening the
    # recorded patch is the direct route; a run with only per-file rows (no
    # patch on disk) sends the reader to the Changes table instead.
    if str(outcome.get("state") or "") == "done":
        diff_action = _diff_open_action(run)
        if diff_action is None and _card_files(run):
            diff_action = S.btn("Open diff", S.page_link("run", "artifacts", run=run.id))
        if diff_action is not None:
            callout_actions.append(diff_action)
    # The verdict row is a summary; the level vector, each decision's reason and
    # the checks behind ``checks n/m`` live in the Verification tab.
    callout_actions.append(
        S.btn("Checks", S.page_link("run", "checks", run=run.id)))
    callout_labels = {str(a.get("label") or "") for a in callout_actions}
    # A callout is drawn on the DAG and Outcome tabs only: on the others the
    # menu must keep the buttons no callout is showing.
    in_callout = callout_labels if resolved in ("dag", "outcome") else set()
    menu = [m for m in header["menu"]
            if str(m.get("label") or "") not in drawn | in_callout]
    # A run parked for review has a change and its checks; the summary is safe
    # to hand off now, so the page offers the entry the header withholds while
    # a run is still in flight. One entry, on every tab.
    if (str(outcome.get("state") or "") != "running"
            and not any(str(m.get("label") or "") == "Copy summary" for m in menu)):
        try:
            menu.append(copy_summary_action(run, outcome))
        except Exception:  # noqa: BLE001 — a broken summary costs its entry
            pass

    # One inbox read per build feeds both the tab badge and the Messages tab.
    try:
        mailbox = run_messages.read_mailbox(run.id)
    except Exception as exc:  # noqa: BLE001 — a broken mailbox costs its tab, never the page
        mailbox = run_messages.Mailbox(run_messages.principal_for(run.id), "error",
                                       error=f"{type(exc).__name__}: {exc}")
    errors: dict[str, str] = {}
    sections = {
        "dag": lambda: _dag_tab(run, node, select=True, outcome=outcome,
                                callout_actions=callout_actions),
        "outcome": lambda: _outcome_tab(run, outcome, callout_actions, absorbed),
        "orchestrator": lambda: orch_tab.orchestrator_tab(run, args),
        "messages": lambda: S.guarded(errors, run_messages.RECEIVED_TITLE,
                                      lambda: run_messages.messages_tab(run.id, mailbox)),
        "verification": lambda: _verification_tab(run),
        "checks": lambda: _checks_tab(run),
        "changes": lambda: _changes_tab(run, errors),
        "kickoff": lambda: _kickoff_tab(run, code_presentation=True),
        "agents": lambda: _agents_tab(run),
        "learnings": lambda: _learnings_tab(run),
        "artifacts": lambda: _artifacts_tab(run, changes=True),
    }[resolved]()
    if lane:
        sections = lane + sections

    # The design's header strip: state, how far, what it cost, which recipe.
    # Placement and elapsed are this page's older chips; they trail the four
    # the design names rather than pushing them out of order.
    waiting = len(open_asks(run.run_dir))
    chips = []
    if waiting:
        chips.append(S.chip(f"✋ {waiting} question{'s' if waiting > 1 else ''}", "yellow"))
    chips.append(S.chip(O.chip_word(run, outcome["state"]), outcome["tone"]))
    done = sum(1 for n in run.nodes if n.state == "done")
    if run.nodes:
        chips.append(S.chip(f"{done}/{len(run.nodes)} nodes"))
    chips.append(S.chip(S.money(_cost(run))))
    recipe = str(run.card.get("recipe") or run.row.get("recipe") or "")
    if recipe:
        chips.append(S.chip(recipe))
    placement = _placement_chip(run)
    if placement:
        chips.append(placement)
    chips.append(S.chip(_elapsed(run)))

    title = _title_v2(run)
    # The human title is the page's headline; the run id is a handle the IDE
    # draws beside it with a copy button. A header that leads with
    # ``run-1792233993-49139`` makes the reader decode a machine handle before
    # they know what the page is about. Without a human title the id is the
    # title, and the sub keeps the recipe / branch context.
    human_title = bool(title) and title != run.id
    if human_title:
        sub = " · ".join(x for x in (
            str(run.card.get("recipe") or run.row.get("recipe") or ""),
            f"branch {_branch(run)}" if _branch(run) else "") if x)
    else:
        sub = _sub_v2(run)
    sub = _sub_worktree(sub, run)
    page = S.page("run", title if human_title else run.id, sub, chips_=chips,
                  actions=header["actions"] + actions,
                  tabs=_v2_tabs(run, mailbox), tab=resolved,
                  args={"run": run.id, **({"node": node} if node else {}),
                        **_orch_args(args)},
                  sections=sections, errors=errors,
                  desc=_desc_fold(run, outcome, title),
                  label=header["label"], menu=menu,
                  crumbs=header.get("crumbs") or (),
                  updated_at=header["updated_at"],
                  refresh=orch_tab.refresh_seconds(run))
    # The header draws no GOAL / SPEC labels on this page: the sub is the
    # kickoff's own heading, and a label above it would read as a field name.
    page["field_labels"] = False
    page["graph"] = _graph(run, node)
    # The header badge reads this without walking sections; absent (never
    # $0.00) when the run recorded no cost at all.
    if spend is not None:
        page["spend"] = spend
    return page


def _attach_flow(page: dict[str, Any], run_obj: Run) -> None:
    """Level-≥2 only: add ``page["flow"]`` next to ``graph``, fail-soft.

    Built with a lazy import (``run_flow`` imports this module, so a top-level
    import would be circular — the same dance as ``run_dock``). On error the key
    is ``None`` and ``errors["flow"]`` says why: a broken flow costs the flow,
    never the page. Level 1 stays byte-identical.
    """
    try:
        from mini_ork.ide_pages import run_flow

        page["flow"] = run_flow.build_flow(run_obj)
    except Exception as exc:  # noqa: BLE001 — a broken flow must not blank the page
        page["flow"] = None
        page.setdefault("errors", {})["flow"] = f"{type(exc).__name__}: {exc}"


def build(home: Path, tab: str | None, args: dict[str, str]) -> dict[str, Any]:
    run_id = (args.get("run") or "").strip()
    if not run_id:
        return {"ok": False, "key": "run", "error": "no run given (--arg run=<id>)"}
    home = Path(home)
    run = _load(home, run_id)
    if run is None:
        return {"ok": False, "key": "run", "error": f"no run {run_id}"}
    # The dock view has its own minimal panels (right-dock contract);
    # compute the r-ide-review sections only when we're staying on the run
    # tab. Doing it above the dock early-return means building two DB reads
    # (parked review, merge review) for every dock render — an effort the
    # dock doesn't use.
    if S.ide_level() >= 2 and (args.get("view") or "") == "dock":
        # The right dock panel: one column, its own tab bar. Imported here
        # (not at module top) — ``run_dock`` imports this module, so a
        # top-level import would be circular (same dance as ``_story_tab``).
        from mini_ork.ide_pages import run_dock

        t = tab if tab in {k for k, _ in run_dock.DOCK_TABS} else "changes"
        errors: dict[str, str] = {}
        sub = f"{O.state_word(run)} · {S.money(_cost(run))} · {_elapsed(run)}"
        page = S.page("run", _title_v2(run), sub, chips_=[], actions=[],
                      tabs=run_dock.DOCK_TABS, tab=t,
                      args={"run": run.id, "view": "dock"},
                      sections=run_dock.build(run, t, errors), errors=errors)
        page["graph"] = _graph(run, args.get("node"))
        _attach_flow(page, run)
        return page
    if S.ide_level() >= 2:
        # Level 2 owns the top slot with the single needs-you lane built inside
        # ``_build_v2`` (parked review, open gate, cost pause, merge approval,
        # open asks) — one decision, one block on every tab.
        page = _build_v2(run, tab, args)
        _attach_flow(page, run)
        return page
    # r-ide-review (level 1 only): top-of-page review sections. Built once,
    # returned (often empty) here, then prepended to whichever ``sections``
    # the tab picks. Level 2 folds these sources into the attention lane.
    review_sections = _parked_review_section(run) + _merge_review_section(run)
    tab = tab if tab in {k for k, _ in TABS} else "dag"
    done = sum(1 for n in run.nodes if n.state == "done")
    chips = [_state_chip(run), S.chip(f"{done}/{len(run.nodes)} nodes"),
             S.chip(S.money(_cost(run))), S.chip(_elapsed(run))]
    placement = _placement_chip(run)
    if placement:
        chips.insert(1, placement)
    sections = {
        "dag": lambda: _dag_tab(run, args.get("node")),
        "kickoff": lambda: _kickoff_tab(run),
        "overview": lambda: _overview_tab(run),
        "orchestrator": lambda: orch_tab.orchestrator_tab(run, args),
        "messages": lambda: S.guarded({}, run_messages.RECEIVED_TITLE,
                                      lambda: run_messages.messages_tab(run.id)),
        "agents": lambda: _agents_tab(run),
        "learnings": lambda: _learnings_tab(run),
        "artifacts": lambda: _artifacts_tab(run),
    }[tab]()
    page = S.page("run", run.id, _sub(run), chips_=chips, actions=_actions(run), tabs=TABS,
                  tab=tab, args={"run": run.id,
                                 **({"node": args["node"]} if args.get("node") else {}),
                                 **_orch_args(args)},
                  sections=review_sections + sections, desc=_desc_fold(run, None, run.id),
                  refresh=orch_tab.refresh_seconds(run))
    # Additive at every level: the graph is available to an old IDE too.
    page["graph"] = _graph(run, args.get("node"))
    return page
