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
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from mini_ork.ide_pages import spec as S

TABS = [("dag", "DAG"), ("overview", "Overview"), ("agents", "Agents"),
        ("learnings", "Learnings"), ("artifacts", "Artifacts")]

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


# ── reading ─────────────────────────────────────────────────────────────────

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
    """Columns by longest ``depends_on``/``verifies`` path; escalation-only nodes last."""
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
                if n.start is None:
                    continue
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


def _load(home: Path, run_id: str) -> Run | None:
    from mini_ork.acp import fleet
    from mini_ork.web.deps import db_for

    card = fleet.run_card(home, run_id)
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
    return Run(id=run_id, home=home, run_dir=run_dir, row=row, card=card,
               nodes=[nodes[n] for n in order if n in nodes], cols=cols, calls=calls,
               recipe_dir=recipe_dir, workspace=workspace)


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


def _actions(run: Run) -> list[dict[str, Any]]:
    web = _serve_url(run.id)
    acts: list[dict[str, Any]] = []
    if _running(run):
        acts.append(S.btn("Stop", S.cli("board", "stop", run.id,
                                         confirm="Stop this run after its current node?"), "warn"))
        acts.append(S.btn("Kill", S.cli("board", "kill", run.id,
                                       confirm=f"Kill {run.id}? SIGTERM, then SIGKILL after 2 s."),
                          "danger"))
    else:
        if run.workspace is not None:
            base = getattr(run.workspace, "base_branch", "") or "base"
            acts.append(S.btn(f"Merge into {base}",
                              S.cli("board", "merge", run.id,
                                    confirm=f"Merge this run's branch into {base}?"),
                              "primary"))
            acts.append(S.btn("Discard", S.cli("board", "discard", run.id,
                                               confirm="Discard this run's worktree and branch?"),
                              "danger"))
        acts.append(S.btn("Certify this change", S.page_link("verify", "certify", run=run.id)))
        acts.append(S.btn("Open run folder", S.reveal(str(run.run_dir)), "ghost"))
    acts.append(S.btn("Open in web UI", S.url(web) if web else None, "ghost"))
    return acts


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
    return f"{max(0, end - node.start)}s"


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


def _dag_tab(run: Run, wanted: str | None) -> list[dict[str, Any]]:
    errors: dict[str, str] = {}
    sel = _selected(run, wanted)
    by_id = {n.id: n for n in run.nodes}

    def build_dag() -> dict[str, Any]:
        if not run.nodes:
            return S.lst("DAG", [S.dot("No nodes recorded yet",
                                       "The run has not started a node and its recipe has no workflow.yaml.")],
                         full=True)
        cols = []
        for c in run.cols:
            col = []
            for nid in c:
                n = by_id.get(nid)
                if n is None:
                    continue
                cost = S.money(n.cost) if n.cost else ""
                col.append(S.dag_node(n.id, n.id, n.family, n.state, cost=cost,
                                      selected=sel is not None and n.id == sel.id,
                                      do=S.set_args(node=n.id)))
            if col:
                cols.append(col)
        return S.dag("", cols, legend=("depends_on → · verifiers check the node before them · "
                                       "rollback runs on escalates_to after a failure · "
                                       "click a node to inspect it"), full=True)

    def build_inspector() -> dict[str, Any]:
        if sel is None:
            return S.lst("", [S.dot("Nothing to inspect yet")], full=True)
        lines = [(ln[:_LINE_CHARS], _colour(ln)) for ln in _node_output(run, sel)]
        prov = _providers(run.home).get(sel.family, {})
        provider = ("deterministic · no model" if sel.family == "shell"
                    else " · ".join(p for p in (str(prov.get("model") or ""), str(prov.get("kind") or "")) if p)
                    or sel.family)
        items = [
            ("Lane", f"{sel.family} · {sel.role_lane}" if sel.role_lane and sel.role_lane != sel.family
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

    return (S.guarded(errors, "DAG", build_dag) + S.guarded(errors, "Inspector", build_inspector)
            + S.guarded(errors, "Coalition", build_coalition))


def _overview_tab(run: Run) -> list[dict[str, Any]]:
    errors: dict[str, str] = {}
    d = run.run_dir

    def inputs() -> dict[str, Any]:
        items = []
        kick = run.row.get("kickoff_path") or ""
        if kick and Path(kick).is_file():
            items.append(S.dot(Path(kick).name, "What the run was asked to do · " + _size(Path(kick)),
                               [S.btn("Open", S.open_path(kick), "ghost")]))
        for name, what in (("plan.json", "The planner's objective and steps"),
                           ("run_profile.json", "Task class and risk the classifier assigned"),
                           ("context-pack.json", "Cite-tagged memory the planner was given")):
            p = d / name
            if p.is_file():
                items.append(S.dot(name, f"{what} · {_size(p)}", [S.btn("Open", S.open_path(str(p)), "ghost")]))
        if not items:
            items = [S.dot("No inputs recorded", "The run directory has no kickoff, plan or context pack.")]
        return S.lst("Run inputs", items, note="What it knew going in.")

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
        from mini_ork.web.deps import db_for

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
        from mini_ork.web.deps import db_for

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

    def files() -> dict[str, Any]:
        from mini_ork.cli.board_cmd import _card_fields

        fields = _card_fields(run.card, run.home.absolute().parent)
        items = []
        for f in fields.get("files") or []:
            acts = [S.btn("Open", S.open_path(f["abs"]), "ghost")] if f.get("abs") else []
            items.append(S.item(f["path"], f"+{f['added']} −{f['removed']}", m="±", mc="blue",
                                is_mono=True, acts=acts))
        if not items:
            items = [S.dot("No file changes recorded")]
        note = "From the run's cached diff; the worktree is gone." if run.card.get("files_from_cache") else ""
        return S.lst("Files changed", items, full=True, note=note)

    return (S.guarded(errors, "Run inputs", inputs) + S.guarded(errors, "Why? — evidence", evidence)
            + S.guarded(errors, "Correlation", correlation) + S.guarded(errors, "Recent events", recent)
            + S.guarded(errors, "Files changed", files))


def _agents_tab(run: Run) -> list[dict[str, Any]]:
    errors: dict[str, str] = {}

    def roster() -> dict[str, Any]:
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

    def run_level() -> dict[str, Any]:
        stages = run.card.get("cost_by_stage") or {}
        total = sum(float(v or 0) for v in stages.values()) or 1.0
        items = [(k, 100.0 * float(v or 0) / total, S.money(v)) for k, v in
                 sorted(stages.items(), key=lambda kv: -float(kv[1] or 0)) if float(v or 0) > 0]
        if not items:
            return S.lst("Cost by stage", [S.dot("No metered LLM calls for this run")], full=True)
        return S.bars("Cost by stage", items, full=True,
                      note="Every metered call of the run, including planning and reflection.")

    return S.guarded(errors, "Agent roster", roster) + S.guarded(errors, "Cost by stage", run_level)


def _learnings_tab(run: Run) -> list[dict[str, Any]]:
    errors: dict[str, str] = {}

    def produced() -> dict[str, Any]:
        from mini_ork.web.deps import db_for

        db = db_for(run.home)
        items = []
        start = _epoch(run.row.get("created_at"))
        # Reflection runs after the last node, so the window closes at the
        # row's last update, not at ``ended_at``.
        end = max(_epoch(run.row.get("ended_at")) or 0, _epoch(run.row.get("updated_at")) or 0) \
            or int(time.time())
        task_class = run.row.get("task_class") or ""
        if start and db.has_table("gradient_records"):
            for g in db.rows(
                "SELECT gradient_id, target, signal, confidence FROM gradient_records "
                "WHERE created_at BETWEEN ? AND ? AND (task_class = ? OR ? = '') "
                "ORDER BY created_at DESC LIMIT 20", (start, end + 60, task_class, task_class)):
                items.append(S.item(f"{g.get('gradient_id')} · {str(g.get('signal') or '')[:160]}",
                                    f"confidence {float(g.get('confidence') or 0):.2f} · {g.get('target')}",
                                    m="✦", mc="purple"))
        if db.has_table("learning_record"):
            for r in db.rows("SELECT title, category, outcome, confidence FROM learning_record "
                             "WHERE run_id = ? ORDER BY rank LIMIT 20", (run.id,)):
                items.append(S.item(str(r.get("title") or ""),
                                    f"{r.get('category')} · {r.get('outcome')} · confidence "
                                    f"{float(r.get('confidence') or 0):.2f}", m="✦", mc="purple"))
        if not items:
            items = [S.dot("Nothing recorded", "Reflection writes gradients after the run's last node.")]
        return S.lst("Produced by the run", items, full=True,
                     note="Gradients recorded while this run reflected (same task class, during the run).")

    def available() -> dict[str, Any]:
        pack_path = run.run_dir / "context-pack.json"
        items = []
        if pack_path.is_file():
            try:
                pack = json.loads(pack_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                pack = {}
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
                    first = value[0] if isinstance(value, list) else next(iter(value.values()))
                    cite = first.get("cite") if isinstance(first, dict) else ""
                    items.append(S.ok(label, f"{n} item{'s' if n != 1 else ''}" + (f" · e.g. {cite}" if cite else "")))
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
        from mini_ork.web.deps import db_for

        db = db_for(run.home)
        if db.has_table("operator_steering"):
            n = (db.row("SELECT COUNT(*) AS n FROM operator_steering WHERE run_id = ?", (run.id,)) or {}).get("n", 0)
            items.append(S.ok("Operator steering", f"{n} message(s) this run") if n
                         else S.dot("Operator steering", "none this run"))
        return S.lst("Available to the run", items, full=True)

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

def build(home: Path, tab: str | None, args: dict[str, str]) -> dict[str, Any]:
    run_id = (args.get("run") or "").strip()
    if not run_id:
        return {"ok": False, "key": "run", "error": "no run given (--arg run=<id>)"}
    home = Path(home)
    run = _load(home, run_id)
    if run is None:
        return {"ok": False, "key": "run", "error": f"no run {run_id}"}
    tab = tab if tab in {k for k, _ in TABS} else "dag"
    done = sum(1 for n in run.nodes if n.state == "done")
    chips = [_state_chip(run), S.chip(f"{done}/{len(run.nodes)} nodes"),
             S.chip(S.money(_cost(run))), S.chip(_elapsed(run))]
    sections = {
        "dag": lambda: _dag_tab(run, args.get("node")),
        "overview": lambda: _overview_tab(run),
        "agents": lambda: _agents_tab(run),
        "learnings": lambda: _learnings_tab(run),
        "artifacts": lambda: _artifacts_tab(run),
    }[tab]()
    return S.page("run", run.id, _sub(run), chips_=chips, actions=_actions(run), tabs=TABS,
                  tab=tab, args={"run": run.id, **({"node": args["node"]} if args.get("node") else {})},
                  sections=sections)
