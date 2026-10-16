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
from typing import Any

from mini_ork.ide_pages import _cn as _CN
from mini_ork.ide_pages import outcome as O
from mini_ork.ide_pages import spec as S
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

TABS = [("dag", "DAG"), ("kickoff", "Kickoff"), ("overview", "Overview"), ("agents", "Agents"),
        ("learnings", "Learnings"), ("artifacts", "Artifacts")]

# The v2 tab bar (``MINI_ORK_IDE_SPEC=2``): the story replaces the overview and
# the graph replaces the DAG. The old keys still resolve (``dag`` → ``graph``,
# ``overview`` → ``story``) so a stale IDE link lands somewhere sensible.
V2_TABS = [("story", "Story"), ("graph", "Graph"), ("questions", "Questions"),
           ("kickoff", "Kickoff"), ("agents", "Agents"), ("learnings", "Learnings"),
           ("artifacts", "Artifacts")]
V2_ALIASES = {"dag": "graph", "overview": "story"}

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


def _attribute_calls(nodes: dict[str, Node], calls: list[dict[str, Any]], now: int) -> None:
    """Give each llm_call to the node whose role lane and time window match."""
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

    terminal = card.get("status") in ("published", "failed", "rolled_back")
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
    return run.card.get("status") not in ("published", "failed", "rolled_back")


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
    """Stop / Kill — the two controls a running run offers."""
    return [
        S.btn("Stop", S.cli("board", "stop", run.id,
                            confirm="Stop this run after its current node?"), "warn"),
        S.btn("Kill", S.cli("board", "kill", run.id,
                            confirm=f"Kill {run.id}? SIGTERM, then SIGKILL after 2 s."),
              "danger"),
    ]


def _discard_action(run: Run) -> dict[str, Any] | None:
    """Discard the run's worktree and branch — ``None`` when it has no workspace."""
    if run.workspace is None:
        return None
    return S.btn("Discard", S.cli("board", "discard", run.id,
                                  confirm="Discard this run's worktree and branch?"),
                 "danger")


def _review_actions(run: Run) -> list[dict[str, Any]]:
    """Merge / Discard — the decision a finished run with a worktree needs."""
    if run.workspace is None:
        return []
    base = getattr(run.workspace, "base_branch", "") or "base"
    return [
        S.btn(f"Merge into {base}",
              S.cli("board", "merge", run.id,
                    confirm=f"Merge this run's branch into {base}?"),
              "primary"),
        S.btn("Discard", S.cli("board", "discard", run.id,
                               confirm="Discard this run's worktree and branch?"),
              "danger"),
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


def _wall(node: Node) -> str:
    if node.start is None:
        return "—"
    end = node.end if node.end is not None else int(time.time())


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
         "run_title": str, "recipe": str, "state": <outcome state word>}
    """
    sel = _selected(run, wanted)
    by_id = {n.id: n for n in run.nodes}
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
            col.append(node_dict)
        if col:
            cols.append(col)
    return {
        "cols": cols,
        "heads": [_head_for(col, idx) for idx, col in enumerate(cols)],
        "edges": _graph_edges(run),
        "run_title": str(run.card.get("title") or ""),
        "recipe": str(run.card.get("recipe") or run.row.get("recipe") or ""),
        "state": O.state_word(run),
    }


def _dag_tab(run: Run, wanted: str | None) -> list[dict[str, Any]]:
    errors: dict[str, str] = {}
    sel = _selected(run, wanted)

    def build_dag() -> dict[str, Any]:
        if not run.nodes:
            return S.lst("DAG", [S.dot("No nodes recorded yet",
                                       "The run has not started a node and its recipe has no workflow.yaml.")],
                         full=True)
        graph = _graph(run, wanted)
        sec = S.dag("", graph["cols"],
                    legend=("depends_on → · verifiers check the node before them · "
                            "rollback runs on escalates_to after a failure · "
                            "click a node to inspect it"), full=True)
        # ``spec.dag`` forwards ``**opt`` into ``_section`` which has no
        # ``heads`` kwarg — mutate the returned dict in place so the kickoff's
        # "stage label per column" reaches the IDE without a spec.py edit.
        sec["heads"] = graph["heads"]
        sec["edges"] = graph["edges"]
        sec["run_title"] = graph["run_title"]
        sec["recipe"] = graph["recipe"]
        return sec

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

    # The DAG section stays first: the IDE draws it as the page's strip, and a
    # test pins `sections[0]`. The roster and the totals follow it, so a run
    # read without opening a node still answers "what ran, on what, for how
    # much" — the same table the Agents tab shows, from one source.
    return (S.guarded(errors, "DAG", build_dag)
            + S.guarded(errors, "Roster", lambda: _roster_section(run))
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
            return S.lst("Retry", [S.ok(f"Resume this cost-paused run with `mini-ork resume {run.id}`.")],
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

    def evidence() -> dict[str, Any]:
        items = []
        for p in sorted(d.glob("verifier_*.json")):
            data = _json_obj(p)
            if data is None:
                continue
            name = str(data.get("verifier") or p.stem.removeprefix("verifier_"))
            rc = data.get("post_rc")
            rc_text = f" rc={rc}" if rc not in (None, "") else ""
            ev = data.get("evidence_path") or ""
            sub = " · ".join(x for x in (str(data.get("error_summary") or "")[:160],
                                         _rel(ev, run.home) if ev else "") if x)
            acts = [S.btn("Open", S.open_path(ev), "ghost")] if ev and Path(ev).is_file() else []
            items.append(S.ok(f"{name} → PASS{rc_text}", sub, acts) if data.get("pass")
                         else S.bad(f"{name} → FAIL{rc_text}", sub, acts))
        if not items:
            items = [S.dot("No verifiers have run",
                           "Zero verifiers would read vacuous, never success.")]
        return S.lst("Why? — evidence", items)

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
        from mini_ork.web.db import db_for

        db = db_for(run.home)
        rows = db.rows("SELECT event_type, payload_json, created_at FROM run_events WHERE run_id = ? "
                       "ORDER BY created_at DESC, rowid DESC LIMIT 8", (run.id,)) \
            if db.has_table("run_events") else []
        start = _epoch(run.row.get("created_at")) or 0
        lines = []
        for r in reversed(rows):
            try:
                payload = json.loads(r.get("payload_json") or "{}")
            except ValueError:
                payload = {}
            node = payload.get("node_id") or ""
            lane = payload.get("model_lane") or payload.get("lane") or ""
            fin = payload.get("finish_reason") or ""
            off = S.duration(max(0, int(r.get("created_at") or start) - start)) if start else ""
            text = f"{off:<8}  {r.get('event_type', ''):<11} {node}"
            if lane:
                text += f"  lane={lane}"
            if fin:
                text += f"  {fin}"
            lines.append((text, "red" if fin and fin != "done" else "muted"))
        return S.code("Recent events", lines or [("No events recorded for this run.", "dim")])

    def orchestrator() -> list[dict[str, Any]]:
        """The per-run orchestrator, always shown — even when it never spoke.

        It is armed on every dependency-graph run, but it emits a row only when
        a node needs an intervention (help · repair · mutate · promote); a clean
        run stays silent, which used to make the layer invisible. This section
        is unconditional, so "on but quiet" reads as a fact, not an absence.
        """
        from mini_ork.web.db import db_for

        db = db_for(run.home)
        rows = db.rows("SELECT event_type, payload_json, created_at FROM run_events "
                       "WHERE run_id = ? AND event_type LIKE 'orchestrator.%' "
                       "ORDER BY created_at DESC, rowid DESC LIMIT 6", (run.id,)) \
            if db.has_table("run_events") else []
        try:
            from mini_ork.orchestration import runner_orchestrator as _orch
            armed = _orch.enabled()
        except Exception:  # noqa: BLE001 — a missing import must not hide the layer
            armed = True  # the layer is default-on
        start = _epoch(run.row.get("created_at")) or 0
        items = []
        for r in reversed(rows):
            try:
                payload = json.loads(r.get("payload_json") or "{}")
            except ValueError:
                payload = {}
            kind = str(r.get("event_type") or "").removeprefix("orchestrator.")
            node = payload.get("node_id") or ""
            reason = payload.get("reason") or ""
            off = S.duration(max(0, int(r.get("created_at") or start) - start)) if start else ""
            items.append(S.item(f"{off}  {kind}".strip(),
                                " · ".join(x for x in (node, reason) if x),
                                m="→", mc="yellow" if kind != "advance" else "blue"))
        if not items:
            items = [S.dot(
                "Armed — no interventions this run" if armed else "Off (MO_RUN_ORCHESTRATOR)",
                ("Speaks only when a DAG node needs help · repair · mutate · promote; "
                 "green nodes are skipped, so a clean run stays quiet." if armed else
                 "The per-run orchestrator is explicitly disabled for this engine."),
                mc="green" if armed else "sub")]
        return [S.lst(f"Orchestrator — {'armed' if armed else 'off'}", items, full=True,
                      note="The per-run decision core at each dependency-graph wave. "
                           "It never fails a run — it only picks an action.")]

    def files() -> list[dict[str, Any]]:
        from mini_ork.cli.board_cmd import _card_fields

        fields = _card_fields(run.card, run.home.absolute().parent)
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
        for f in fields.get("files") or []:
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
            + S.guarded(errors, "Why? — evidence", evidence)
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
            return S.callout(
                "No open questions",
                "This run is not waiting on an answer. A question lands here — and in the "
                "inbox — when the planner stops on something only you can decide.",
                tone="green", icon="✓", full=True)
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


def _kickoff_tab(run: Run) -> list[dict[str, Any]]:
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
        return S.markdown(title, text, path=path, full=True)

    return S.guarded(errors, "Kickoff", build)


def _roster_section(run: Run) -> dict[str, Any]:
    """Every node of the run as one table: what it ran on, its state and cost.

    Shared by the Graph tab's run-level panel and the Agents tab, so the two
    cannot report different numbers for the same node.
    """
    rows = []
    for n in run.nodes:
        verdict, vc = _verdict(run, n)
        ran = n.state not in ("pending", "skipped")
        rows.append({"cells": [
            S.cell(n.id, "text", b=True),
            S.cell(n.family, f"fam:{n.family}"),
            S.cell(_STATE_TEXT.get(n.state, n.state), _STATE_COLOUR.get(n.state, "sub")),
            S.cell(verdict, vc),
            S.mono(S.money(n.cost) if ran else "—"),
            S.mono(_wall(n) if ran else "—"),
            S.muted(", ".join(n.gates) or "—"),
        ], "do": S.page_link("run", "dag", run=run.id, node=n.id)})
    if not rows:
        rows = [[S.muted("No nodes recorded"), "", "", "", "", "", ""]]
    return S.table("Agent roster",
                   [S.col(150), S.col(90), S.col(80), S.col(90), S.col(64), S.col(64),
                    S.col(fr=1, min=140)],
                   ["node", "family", "status", "verdict", "cost", "time", "gates"], rows,
                   full=True, note="Where the money went inside the run. Click a row to inspect the node.")


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

    return (S.guarded(errors, "Agent roster", lambda: _roster_section(run))
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


def _injection_ledger(run: Run) -> list[dict[str, Any]]:
    """Per-node rows from ``learned/<node>.json`` — what each node was given.

    ``learned/`` is the INJECTION ledger (what actually reached the model),
    not produced learnings. Rendering it closes the "display != injection"
    gap: the planner injects the v2 pack, so the tab must show the v2 blocks
    (and each node's source ids / skipped blocks), not the v1 builder output.
    """
    learned_dir = run.run_dir / "learned"
    if not learned_dir.is_dir():
        return []
    out: list[dict[str, Any]] = []
    for p in sorted(learned_dir.glob("*.json")):
        d = _read_json_obj(p)
        node = str(d.get("node_id") or p.stem)
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


def _learnings_tab(run: Run) -> list[dict[str, Any]]:
    errors: dict[str, str] = {}

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
                items.append(S.item(title, sub, m="✦", mc="purple"))
        if db.has_table("learning_record"):
            for r in db.rows("SELECT title, category, outcome, confidence FROM learning_record "
                             "WHERE run_id = ? ORDER BY rank LIMIT 20", (run.id,)):
                items.append(S.item(str(r.get("title") or ""),
                                    f"{r.get('category')} · {r.get('outcome')} · confidence "
                                    f"{float(r.get('confidence') or 0):.2f}", m="✦", mc="purple"))
        if not items:
            items = [S.dot("Nothing recorded", "Reflection writes gradients after the run's last node.")]
        return S.lst("Produced by the run", items, full=True,
                     note="Gradients reflection wrote about this run's own nodes.")

    def available() -> list[dict[str, Any]]:
        sections: list[dict[str, Any]] = []
        items: list[dict[str, Any]] = []
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
                detail = [S.item(f"{c.get('kind', '?')}: {str(c.get('text', ''))[:180]}",
                                 str(c.get("id", "")))
                          for c in cons if isinstance(c, dict)]
                items.append(S.ok("Constraints", f"{len(cons)} injected"))
                sections.append(S.lst(f"Constraints (injected) · {len(cons)}", detail, full=True))
            else:
                items.append(S.dot("Constraints", "none injected"))
            themes = v2.get("themes") if isinstance(v2.get("themes"), list) else []
            if themes:
                detail = [S.item(str(t.get("text") or "")[:180], str(t.get("id") or ""))
                          for t in themes if isinstance(t, dict)]
                items.append(S.ok("Verified lessons", f"{len(themes)} injected"))
                sections.append(S.lst(f"Verified lessons (injected) · {len(themes)}", detail, full=True))
            contract = v2.get("contract") if isinstance(v2.get("contract"), dict) else {}
            ff = v2.get("file_findings") if isinstance(v2.get("file_findings"), list) else []
            if ff:
                detail = [S.item(str(f.get("file") or f.get("path") or "file"),
                                 str(f.get("finding") or f.get("summary") or "")[:160])
                          for f in ff if isinstance(f, dict)]
                items.append(S.ok("File findings", f"{len(ff)} file(s)"))
                sections.append(S.lst(f"File findings · {len(ff)}", detail, full=True))
            elif contract:
                detail = _pack_kv_items(contract)
                if detail:
                    items.append(S.ok("Contract", f"{len(detail)} clause(s)"))
                    sections.append(S.lst("Contract (injected)", detail, full=True))
            pa = v2.get("prior_attempts") if isinstance(v2.get("prior_attempts"), list) else []
            if pa:
                items.append(S.ok("Prior attempts", f"{len(pa)} attempt(s)"))
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

        ledger = _injection_ledger(run)
        if ledger:
            items.append(S.ok("Per-node injection", f"{len(ledger)} node(s) recorded"))
            sections.append(S.lst(f"Injected per node · {len(ledger)}", ledger, full=True))

        from mini_ork.web.db import db_for

        db = db_for(run.home)
        if db.has_table("operator_steering"):
            n = (db.row("SELECT COUNT(*) AS n FROM operator_steering WHERE run_id = ?", (run.id,)) or {}).get("n", 0)
            items.append(S.ok("Operator steering", f"{n} message(s) this run") if n
                         else S.dot("Operator steering", "none this run"))
        summary = S.lst("Available to the run", items, full=True,
                        note="The context the planner actually received (v2 pack), "
                             "plus each node's own injection ledger.")
        return [summary, *sections]

    return (S.guarded(errors, "Produced by the run", produced)
            + S.guarded(errors, "Available to the run", available))


def _artifacts_tab(run: Run) -> list[dict[str, Any]]:
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
        return S.table("Artifacts", [S.col(fr=1, min=200), S.col(110), S.col(70), S.col(110)],
                       ["file", "kind", "size", "from"], rows, full=True,
                       note="Only what the run produced, grouped. Click a file to open it.")

    return S.guarded(errors, "Artifacts", table)


# ── helpers ─────────────────────────────────────────────────────────────────

_FROM = re.compile(r"^(?:agent-|impl-|verifier_|review-|lens-)([A-Za-z0-9_.-]+?)(?:\.live)?\.(?:jsonl|log|json|md)$")


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


def _decisions_assumptions_section(run: Run) -> dict[str, Any]:
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
        return S.lst("Decisions and assumptions",
                      [S.dot("No decision atoms recorded",
                             "Each node session is queried once; nothing came back.")],
                      full=True)
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
        return S.lst("Decisions and assumptions",
                      [S.dot("No decision atoms recorded")], full=True)
    note = (f"{len(nodes_data)} node session{'s' if len(nodes_data) != 1 else ''} · "
            f"{total_atoms} atom{'s' if total_atoms != 1 else ''} · "
            "open jumps to the source session.")
    return S.lst("Decisions and assumptions", items, full=True, note=note)


def _run_memory_sections(run: Run) -> list[dict[str, Any]]:
    """The run-page sections sourced from the run-memory snapshot.

    Currently: "Decisions and assumptions" only — the per-file history
    line lives inside the existing "Files changed" section so the
    changed-files view itself gets the recency annotation. Both read
    from the same snapshot (or compute once and persist it) so the
    finished-run page renders without ever asking ContextNest.
    """
    try:
        return [_decisions_assumptions_section(run)]
    except Exception as exc:  # noqa: BLE001 — additive sections must not blank the page
        return [S.lst("Decisions and assumptions",
                      [S.dot(f"Could not read this: {type(exc).__name__}: {exc}",
                             mc="red")], full=True)]


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


def _sub_v2(run: Run) -> str:
    parts = [str(run.card.get("recipe") or run.row.get("recipe") or "")]
    branch = _branch(run)
    if branch:
        parts.append(f"branch {branch}")
    parts.append(run.id)
    return " · ".join(p for p in parts if p)


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
    """What *happened*: the run's own outcome sentence, then the counts.

    The lead is ``outcome.resolve(run)["text"]`` — the exact sentence the
    Story tab's triage box shows — so the header and the page cannot disagree
    about what became of the run. Only when that sentence is generic (it does
    not name the deciding node) is the node's own headline added; otherwise it
    would be the same news twice.
    """
    nodes = run.nodes
    total = len(nodes)
    if not total:
        return ""
    done = sum(1 for n in nodes if n.state == "done")
    skipped = sum(1 for n in nodes if n.state == "skipped")
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

    parts = [f"{done}/{total} nodes done"]
    if skipped:
        # "not run" is the word the rest of the page uses for this state.
        parts.append(f"{skipped} not run")
    if failed:
        parts.append(f"{len(failed)} failed")
    parts += [S.money(_cost(run)), _elapsed(run)]
    counts = " · ".join(parts) + "."
    return f"{lead}. {counts}" if lead else counts


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


def _story_tab(run: Run, outcome: dict[str, Any]) -> list[dict[str, Any]]:
    """The v2 story: triage, the outcome's callouts, the goal/criteria hero,
    then the step-by-step story — one row per pipeline node with its evidence."""
    sections = [S.triage(outcome["text"], tone=outcome["tone"], icon=outcome["icon"],
                         detail=outcome["detail"], counts=outcome["counts"],
                         actions=outcome["actions"], menu=outcome["menu"])]
    sections.extend(outcome.get("callouts") or [])
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
    if goal or criteria:
        recipe = str(run.card.get("recipe") or run.row.get("recipe") or "")
        meta = []
        if recipe:
            meta.append(S.meta_item(recipe))
        branch = _branch(run)
        if branch:
            meta.append(S.meta_item(f"branch {branch}", mono=True))
        meta.append(S.meta_item(run.id, mono=True))
        sections.append(S.hero("What this run is for", goal=goal,
                               criteria=criteria[:8], meta=meta))
    # Imported inside the function: ``node.py`` imports ``run.py``, so a
    # top-level ``run_story`` import (which pulls in ``node.py``) would be
    # circular. Guarded like the Overview sections it replaces: a broken story
    # costs the story, never the triage / callouts / hero above it.
    from mini_ork.ide_pages import run_story

    # The snapshot path keeps a finished run from re-fetching ContextNest;
    # live runs go through the section cache and never persist the snapshot.
    # Placed before the story so the story stays the closing section the
    # IDE scrolls to.
    sections.extend(S.guarded({}, "Decisions and assumptions",
                              lambda: _run_memory_sections(run)))
    sections.extend(S.guarded({}, "What happened", lambda: run_story.story_section(run)))
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
    return sections


def _build_v2(run: Run, tab: str | None, args: dict[str, str]) -> dict[str, Any]:
    """The ``MINI_ORK_IDE_SPEC=2`` page: one true outcome, next action next to it."""
    outcome = O.resolve(run)
    keys = {k for k, _ in V2_TABS}
    wanted = (tab or "").strip()
    resolved = V2_ALIASES.get(wanted, wanted)
    if resolved not in keys:
        resolved = "story"
    node = args.get("node")
    sections = {
        "story": lambda: _story_tab(run, outcome),
        "graph": lambda: _dag_tab(run, node),
        "questions": lambda: _questions_tab(run),
        "kickoff": lambda: _kickoff_tab(run),
        "agents": lambda: _agents_tab(run),
        "learnings": lambda: _learnings_tab(run),
        "artifacts": lambda: _artifacts_tab(run),
    }[resolved]()
    chips = [S.chip(outcome["state"], outcome["tone"]),
             S.chip(S.money(_cost(run))), S.chip(_elapsed(run))]
    placement = _placement_chip(run)
    if placement:
        chips.insert(1, placement)
    # A run that stopped on a question says so in the header: the Questions tab
    # is otherwise an empty tab on every run that never asked anything, and a
    # blocked run would look like any other.
    waiting = len(open_asks(run.run_dir))
    if waiting:
        chips.insert(0, S.chip(f"✋ {waiting} question{'s' if waiting > 1 else ''}", "yellow"))
    title = _title_v2(run)
    page = S.page("run", title, _sub_v2(run), chips_=chips, actions=[],
                  tabs=V2_TABS, tab=resolved,
                  args={"run": run.id, **({"node": node} if node else {})},
                  sections=sections, desc=_desc_fold(run, outcome, title))
    page["graph"] = _graph(run, node)
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
    # r-ide-review: top-of-page review sections. Built once, returned (often
    # empty) here, then prepended to whichever ``sections`` the tab picks.
    review_sections = _parked_review_section(run) + _merge_review_section(run)
    if S.ide_level() >= 2:
        page = _build_v2(run, tab, args)
        _attach_flow(page, run)
        # Additive on the story / graph / kickoff / agents / learnings /
        # artifacts tabs at level 2: prepend the review sections so they
        # own the top slot an empty ``Waiting for your review`` callout
        # already reserved on every parked run.
        if review_sections and isinstance(page.get("sections"), list):
            page["sections"] = review_sections + page["sections"]
        return page
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
        "agents": lambda: _agents_tab(run),
        "learnings": lambda: _learnings_tab(run),
        "artifacts": lambda: _artifacts_tab(run),
    }[tab]()
    page = S.page("run", run.id, _sub(run), chips_=chips, actions=_actions(run), tabs=TABS,
                  tab=tab, args={"run": run.id, **({"node": args["node"]} if args.get("node") else {})},
                  sections=review_sections + sections, desc=_desc_fold(run, None, run.id))
    # Additive at every level: the graph is available to an old IDE too.
    page["graph"] = _graph(run, args.get("node"))
    return page
