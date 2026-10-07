"""Stdio MCP server exposing ``mini-ork`` observability + control data.

JSON-RPC 2.0 over stdin/stdout, one JSON object per line. ``stdout`` is the
protocol channel — every diagnostic this module emits goes to ``stderr``;
importing the module prints nothing.

Default mode exposes the read-only observability tools and opens the
``StateDB`` via :func:`mini_ork.sqlite_read.connect_readonly` (which
prefers ``mode=ro`` + ``PRAGMA query_only`` and survives an idle WAL
database):

* :func:`list_runs` — newest-first ``task_runs`` rows with a derived
  ``title`` (first non-empty ``#``-prefixed kickoff line, ≤80 chars).
* :func:`run_detail` — full ``task_runs`` row + node lifecycle events +
  first 2000 chars of the kickoff + ``verdict.json`` (if present).
* :func:`learnings` — failure-mode gradients (per task_class) +
  emergent patterns + memory (tasks + agents), each filtered by a
  case-insensitive substring query.
* :func:`cost` — last N days of ``cost_by_day`` plus a total.
* :func:`lanes` — the ``lane_name → family`` map from
  :func:`mini_ork.web.recipes.load_lanes`.

When the server is started with ``--control`` (or env ``MO_MCP_CONTROL=1``)
six more tools are added: ``list_recipes``, ``start_run``, ``run_status``,
``wait_for_run``, ``stop_run``, ``certify``. These delegate to
:mod:`mini_ork.web.control` and the certify subprocess seam — they never
write through the server's read-only ``StateDB`` handle.

Resolution: ``MINI_ORK_HOME`` else ``<cwd>/.mini-ork`` (via
:func:`mini_ork.web.db.resolve_home`). A missing home OR missing
``state.db`` is reported as a per-tool ``{"error": ...}`` object — never
an exception — so a stale workspace can't break a long-running MCP
client.
"""
from __future__ import annotations

import datetime as _dt
import json
import os
import subprocess
import sys
import time
from importlib import metadata as _imeta
from pathlib import Path
from typing import Any

# Public, re-exported so ``mini_ork.mcp_context.server.dispatch`` is the
# testable surface for the in-process request/response loop.
__all__ = [
    "VERSION",
    "PROTOCOL_VERSION",
    "TOOL_DEFS",
    "dispatch",
    "serve",
    "_log",
]


PROTOCOL_VERSION = "2025-06-18"
SERVER_NAME = "mini-ork-context"


def _version() -> str:
    try:
        return _imeta.version("mini-ork")
    except _imeta.PackageNotFoundError:
        return "0.1.0"


VERSION = _version()


def _log(msg: str) -> None:
    """Stderr-only logging; stdout is the MCP protocol channel."""
    print(f"[mini-ork-mcp-context] {msg}", file=sys.stderr, flush=True)


# ── home + DB resolution ───────────────────────────────────────────────────


def _resolve_home() -> Path:
    """``MINI_ORK_HOME`` else ``<cwd>/.mini-ork`` (resolved).

    Z-W1: when the cwd is a git linked worktree and the worktree itself
    has no ``.mini-ork``, fall through to the **main** checkout's
    ``.mini-ork`` (kickoff §Home resolution). A thread or worker launched
    from inside a linked worktree reaches the same project state as one
    launched from the main checkout.

    Note: this fallback only fires when ``MINI_ORK_HOME`` is NOT set —
    an explicit ``MINI_ORK_HOME`` always wins (test fixtures use it to
    point at a hermetic tmp home).
    """
    from mini_ork.web.db import resolve_home

    if os.environ.get("MINI_ORK_HOME"):
        return resolve_home(None)
    home = resolve_home(None)
    cwd = Path.cwd()
    try:
        from mini_ork import workspaces as _workspaces

        if _workspaces.is_linked_worktree(cwd):
            main = _workspaces.main_checkout(cwd)
            if main is not None:
                main_home = main / ".mini-ork"
                if main_home.is_dir() and main_home != home:
                    return main_home
    except Exception:  # noqa: BLE001 — defensive against missing helper
        pass
    return home


def _missing_home_error(home: Path) -> dict[str, str]:
    return {"error": f"no mini-ork home at {home}"}


def _open_db(home: Path):
    """Return a ``StateDB`` for ``home`` or ``None`` when the DB is absent.

    Returns ``None`` (not raises) when the home is missing OR the DB has
    not been initialised — the caller maps that to a per-tool error
    object so a misconfigured workspace cannot crash the server.
    """
    from mini_ork.web.db import StateDB

    if not home.exists():
        return None
    db_file = home / "state.db"
    if not db_file.exists():
        return None
    return StateDB(db_file)


# ── tool: list_runs ─────────────────────────────────────────────────────────


def _read_kickoff_title(path: Path | None, *, max_chars: int = 80) -> str | None:
    """First non-empty kickoff line with a leading ``#`` stripped.

    Returns ``None`` when the file is missing, unreadable, or empty —
    the caller stores ``None`` rather than a placeholder so consumers
    can distinguish "no title" from "blank title".
    """
    if path is None:
        return None
    try:
        with path.open("r", encoding="utf-8", errors="replace") as f:
            for raw in f:
                line = raw.strip()
                if not line:
                    continue
                if line.startswith("#"):
                    line = line.lstrip("#").strip()
                if not line:
                    continue
                return line[:max_chars]
    except OSError:
        return None
    return None


def _list_runs(home: Path, args: dict[str, Any]) -> dict[str, Any]:
    db = _open_db(home)
    if db is None:
        return _missing_home_error(home)
    if not db.has_table("task_runs"):
        return {"runs": []}
    try:
        limit = max(1, min(int(args.get("limit") or 20), 200))
    except (TypeError, ValueError):
        limit = 20
    status = args.get("status")
    sql = (
        "SELECT id, status, recipe, cost_usd, created_at, kickoff_path "
        "FROM task_runs"
    )
    params: tuple[Any, ...] = ()
    if isinstance(status, str) and status.strip():
        sql += " WHERE status = ?"
        params = (status.strip(),)
    sql += " ORDER BY created_at DESC LIMIT ?"
    params = (*params, limit)
    rows = db.rows(sql, params)
    runs: list[dict[str, Any]] = []
    for r in rows:
        kickoff_path = r.get("kickoff_path")
        kp = Path(kickoff_path) if kickoff_path else None
        # Prefer the live run-local kickoff (always present) over the
        # stored path (which may point at a moved/deleted file).
        live = home / "runs" / str(r.get("id") or "") / "kickoff.md"
        title = _read_kickoff_title(live) if live.exists() else _read_kickoff_title(kp)
        runs.append({
            "id": r.get("id"),
            "status": r.get("status"),
            "recipe": r.get("recipe"),
            "cost_usd": r.get("cost_usd"),
            "created_at": r.get("created_at"),
            "title": title,
        })
    count_sql = "SELECT COUNT(*) FROM task_runs"
    count_params: tuple[Any, ...] = ()
    if isinstance(status, str) and status.strip():
        count_sql += " WHERE status = ?"
        count_params = (status.strip(),)
    count_rows = db.rows(count_sql, count_params)
    total = int(count_rows[0].get("COUNT(*)", 0)) if count_rows else 0
    return {"runs": runs, "total": total}


# ── tool: run_detail ────────────────────────────────────────────────────────


def _run_detail(home: Path, args: dict[str, Any]) -> dict[str, Any]:
    db = _open_db(home)
    if db is None:
        return _missing_home_error(home)
    run_id = args.get("run_id")
    if not isinstance(run_id, str) or not run_id.strip():
        return {"error": "run_id is required"}
    run_id = run_id.strip()
    if not db.has_table("task_runs"):
        return {"error": f"run not found: {run_id}"}
    row = db.row("SELECT * FROM task_runs WHERE id = ?", (run_id,))
    if row is None:
        return {"error": f"run not found: {run_id}"}

    from mini_ork.web.repositories import RunDetailRepository

    repo = RunDetailRepository(db)
    events = repo.fetch_node_lifecycle_events(run_id)

    kickoff_text: str | None = None
    kickoff_candidates = []
    if row.get("kickoff_path"):
        kickoff_candidates.append(Path(str(row["kickoff_path"])))
    kickoff_candidates.append(home / "runs" / run_id / "kickoff.md")
    for cand in kickoff_candidates:
        try:
            with cand.open("r", encoding="utf-8", errors="replace") as f:
                kickoff_text = f.read(2000)
            break
        except OSError:
            continue

    verdict_path = home / "runs" / run_id / "verdict.json"
    verdict: Any = None
    if verdict_path.exists():
        try:
            with verdict_path.open("r", encoding="utf-8") as f:
                verdict = json.load(f)
        except (OSError, json.JSONDecodeError):
            verdict = None

    return {
        "task_run": row,
        "node_lifecycle_events": events,
        "kickoff_preview": kickoff_text,
        "verdict": verdict,
    }


# ── tool: learnings ────────────────────────────────────────────────────────


def _matches(query: str | None, *sources: Any) -> list[Any]:
    """Case-insensitive substring filter over the JSON text of ``sources``.

    Each ``source`` is ``json.dumps``-ed and tested against ``query``.
    Empty/None ``query`` returns every source unchanged. Returns one
    output per source position so the caller can unpack positionally
    even when the needle matches nothing (an empty list / dict when the
    schema is fresh is not "evidence" — it's the absence of it).
    """
    if not query:
        return list(sources)
    needle = query.lower()
    out: list[Any] = []
    for src in sources:
        if src is None:
            out.append(src)
            continue
        if needle in json.dumps(src, default=str).lower():
            out.append(src)
        else:
            # Preserve source shape so the result schema is stable.
            out.append(type(src)() if isinstance(src, (list, dict)) else src)
    return out


def _learnings(home: Path, args: dict[str, Any]) -> dict[str, Any]:
    db = _open_db(home)
    if db is None:
        return _missing_home_error(home)
    task_class = args.get("task_class")
    query = args.get("query") if isinstance(args.get("query"), str) else None
    try:
        limit = max(1, min(int(args.get("limit") or 20), 200))
    except (TypeError, ValueError):
        limit = 20

    from mini_ork.web.repositories import LearningRepository
    from mini_ork.web.routes import learning as learning_routes

    repo = LearningRepository(db)

    failure_modes: list[dict[str, Any]] = []
    if isinstance(task_class, str) and task_class.strip():
        try:
            failure_modes = repo.fetch_failure_mode_gradients(task_class.strip())
        except Exception:
            failure_modes = []

    try:
        patterns = learning_routes.emergent_patterns(db, limit=limit)
    except Exception:
        patterns = []

    try:
        mem = learning_routes.memory(db, limit=limit)
    except Exception:
        mem = {"tasks": [], "agents": []}

    if query:
        fm_filtered, patterns_filtered, mem_filtered = _matches(
            query, failure_modes, patterns, mem
        )
        failure_modes = fm_filtered
        patterns = patterns_filtered
        mem = mem_filtered

    return {
        "failure_modes": failure_modes,
        "patterns": patterns,
        "memory": mem,
    }


# ── tool: cost ─────────────────────────────────────────────────────────────


def _cost(home: Path, args: dict[str, Any]) -> dict[str, Any]:
    db = _open_db(home)
    if db is None:
        return _missing_home_error(home)
    try:
        days = int(args.get("days") or 7)
    except (TypeError, ValueError):
        days = 7
    if days < 1:
        days = 1

    from mini_ork.web.routes.trajectory import cost_by_day

    try:
        rows = cost_by_day(db)
    except Exception:
        rows = []

    # `created_at` is a unix timestamp (seconds). Keep the last `days`
    # calendar days by comparing the day string (UTC).
    today = _dt.datetime.now(_dt.timezone.utc).date()
    cutoff = today.toordinal() - (days - 1)
    filtered = []
    total = 0.0
    for r in rows:
        day_str = r.get("day")
        if not day_str:
            continue
        try:
            d = _dt.date.fromisoformat(str(day_str))
        except ValueError:
            continue
        if d.toordinal() < cutoff:
            continue
        filtered.append(r)
        try:
            total += float(r.get("cost") or 0)
        except (TypeError, ValueError):
            continue
    return {"days": filtered, "total_cost_usd": round(total, 6)}


# ── tool: lanes ────────────────────────────────────────────────────────────


def _lanes(home: Path, args: dict[str, Any]) -> dict[str, Any]:
    del args
    from mini_ork.web.recipes import load_lanes

    try:
        return {"lanes": load_lanes(home)}
    except Exception as exc:  # malformed overlay or yaml error
        _log(f"load_lanes failed: {exc}")
        return {"lanes": {}}


# ── control tool: list_recipes ─────────────────────────────────────────────


def _list_recipes(home: Path, args: dict[str, Any]) -> dict[str, Any]:
    """List project + engine recipes via ``mini_ork.recipes_catalog``.

    The catalog is the single source of truth (S0 zed-integration): a
    project recipe under ``<home>/recipes/`` shadows the engine's with
    the same id. Engine root is still resolved through
    ``control._mini_ork_root()`` so a future divergence with
    ``recipes.mini_ork_root()`` cannot silently shift the list the
    orchestrator sees. The catalog never raises; a missing home / engine
    / recipes dir degrades to the entries that can be read.

    The ``grade`` and ``runs`` fields are computed by the same projection
    that powers ``/recipes`` (``mini_ork.acp.recipe_view.recipe_rows``)
    so the orchestrator's picker and the ACP surface stay coherent.
    """
    del args
    try:
        from mini_ork import recipes_catalog
    except Exception as exc:
        return {"error": f"could not import recipes catalog: {exc}"}
    try:
        entries = recipes_catalog.list_recipes(home)
    except Exception as exc:
        return {"error": f"could not list recipes: {exc}"}
    # Cheap aggregate fields: ``grade`` from the static eval module and
    # ``runs`` from the ``task_runs`` table grouped by recipe. Both lookups
    # are wrapped — a missing db / eval module degrades to zeros so the
    # picker stays usable.
    grade_by_id: dict[str, tuple[str, int]] = {}
    runs_by_id: dict[str, int] = {}
    engine_root: Path | None = None
    eval_recipe_fn: Any = None
    grade_fn: Any = None
    try:
        from mini_ork.cli.recipe_eval import _grade, eval_recipe
        from mini_ork.web.control import _mini_ork_root

        engine_root = _mini_ork_root()
        eval_recipe_fn = eval_recipe
        grade_fn = _grade
    except Exception:
        pass
    for entry in entries:
        if engine_root is None or eval_recipe_fn is None or grade_fn is None:
            grade_by_id[entry.id] = ("—", 0)
            continue
        try:
            root = entry.path.parent.parent
            res = eval_recipe_fn(root, entry.id)
        except Exception:
            grade_by_id[entry.id] = ("—", 0)
            continue
        score = res.get("score") if isinstance(res, dict) else None
        if not isinstance(score, (int, float)):
            score = 0
        try:
            grade_by_id[entry.id] = (grade_fn(int(score)), int(score))
        except Exception:
            grade_by_id[entry.id] = ("—", 0)
    try:
        from mini_ork.web.deps import db_for

        db = db_for(home) if home is not None else None
    except Exception:
        db = None
    if db is not None and getattr(db, "has_table", None) and db.has_table("task_runs"):
        try:
            agg_rows = db.rows(
                "SELECT recipe, COUNT(*) AS runs FROM task_runs GROUP BY recipe"
            )
            for row in agg_rows or []:
                rid = row.get("recipe")
                if isinstance(rid, str) and rid:
                    try:
                        runs_by_id[rid] = int(row.get("runs") or 0)
                    except (TypeError, ValueError):
                        pass
        except Exception:
            pass
    out: list[dict[str, Any]] = []
    for entry in entries:
        item: dict[str, Any] = {
            "id": entry.id,
            "description": entry.description,
            "source": entry.source,
            "nodes": entry.node_count,
            "grade": grade_by_id.get(entry.id, ("—", 0))[0],
            "grade_score": grade_by_id.get(entry.id, ("—", 0))[1],
            "runs": runs_by_id.get(entry.id, 0),
        }
        if entry.shadows_engine:
            item["overrides_engine"] = True
        out.append(item)
    return {"recipes": out}


# ── read-only tool: describe_recipe ───────────────────────────────────────


def _describe_recipe(home: Path, args: dict[str, Any]) -> dict[str, Any]:
    """Card payload for one recipe (read-only MCP tool).

    Returns the full card as a serialisable dict; unknown id → ``{"error"}``.
    Wrapped in a try/except so a broken recipe file or missing eval module
    degrades to a structured error rather than crashing the JSON-RPC layer.
    """
    rid = args.get("id")
    if not isinstance(rid, str) or not rid.strip():
        return {"error": "id is required"}
    try:
        from mini_ork.acp import recipe_view
    except Exception as exc:
        return {"error": f"could not import recipe view: {exc}"}
    try:
        payload = recipe_view.describe_recipe_payload(home, rid.strip())
    except Exception as exc:
        return {"error": f"describe_recipe failed: {exc}"}
    if payload is None:
        return {"error": f"recipe not found: {rid}"}
    return {"recipe": payload}


# ── control tool: start_run ────────────────────────────────────────────────


def _start_run(home: Path, args: dict[str, Any]) -> dict[str, Any]:
    recipe = args.get("recipe")
    kickoff_markdown = args.get("kickoff_markdown")
    if not isinstance(recipe, str) or not recipe.strip():
        return {"error": "recipe is required"}
    if not isinstance(kickoff_markdown, str) or not kickoff_markdown.strip():
        return {"error": "kickoff_markdown is required"}

    # Project root = the directory that OWNS .mini-ork/. Not
    # MINI_ORK_PROJECT_HOME: the launcher sets that to the home itself, so an
    # in-place run would target <project>/.mini-ork.
    project_root = home.absolute().parent
    workspace_mode = str(args.get("workspace") or os.environ.get("MO_WORKSPACE_MODE") or "worktree")
    if workspace_mode not in ("worktree", "in-place"):
        return {"error": f"unknown workspace: {workspace_mode!r}"}

    from mini_ork.web.control import launch_run

    extra_env: dict[str, str] = {"MO_TARGET_CWD": str(project_root)}
    # The orchestrator's spawned MCP server inherits MO_THREAD_ID from the
    # thread that started the turn; naming it as the run's owner lets the
    # run-failure path (retry_notify) tell that thread when the run dies on an
    # unavailable lane. Read the same way MO_THREAD_CWD is below.
    thread_id = os.environ.get("MO_THREAD_ID") or ""
    if thread_id:
        extra_env["MO_RUN_OWNER"] = f"thread:{thread_id}"
    note: str | None = None
    workspace_meta: dict[str, Any] = {"workspace": workspace_mode}
    pre_minted_run_id: str | None = None

    if workspace_mode == "worktree":
        # Mint the run id up front so workspaces.create can use it as the
        # branch + record key. launch_run accepts a caller-supplied run_id
        # (the safe-token shape is the same one it would mint internally).
        from mini_ork.acp.agent import mint_run_id
        from mini_ork import workspaces as _workspaces

        pre_minted_run_id = mint_run_id()
        # Derive the task title from the kickoff markdown for the worktree
        # directory name (Z-W1). First non-empty line, leading ``#``
        # stripped; falls back to "" when the text is empty.
        title_line = ""
        for raw in (kickoff_markdown or "").splitlines():
            line = raw.strip()
            if not line:
                continue
            if line.startswith("#"):
                line = line.lstrip("#").strip()
            title_line = line
            break
        thread_cwd = Path(os.environ.get("MO_THREAD_CWD") or project_root)
        try:
            if (_workspaces.is_linked_worktree(thread_cwd)
                    and not os.environ.get("MO_DELIVER_VIA_CLIENT")):
                # The thread runs in a (Zed) linked worktree: that worktree IS
                # the task's workspace — the Git panel shows its changes.
                ws = _workspaces.adopt(thread_cwd, home, pre_minted_run_id)
            else:
                ws = _workspaces.create(
                    thread_cwd,  # the thread's own checkout is the base
                    home,
                    pre_minted_run_id,
                    name=_workspaces.task_name(title_line, pre_minted_run_id),
                )
            extra_env["MO_TARGET_CWD"] = str(ws.path)
            workspace_meta = {
                "workspace": "worktree",
                "worktree": str(ws.path),
                "branch": ws.branch,
                "adopted": ws.adopted,
            }
        except RuntimeError as exc:
            # Non-git repo (or a transient git failure) — fall back to
            # in-place so a fresh checkout can still kick a run.
            note = f"workspace=worktree unavailable ({exc}); falling back to in-place"
            workspace_mode = "in-place"
            pre_minted_run_id = None
            workspace_meta = {"workspace": "in-place"}

    result = launch_run(
        home,
        recipe.strip(),
        kickoff_markdown,
        run_id=pre_minted_run_id,
        extra_env=extra_env,
    )
    if not isinstance(result, dict):
        return {"error": f"launch_run returned non-dict: {result!r}"}
    if not result.get("ok"):
        err = result.get("error") or "launch_run failed"
        return {"error": err, "launch_result": result}
    if workspace_meta:
        result.update(workspace_meta)
    if note:
        result["note"] = note
    return result


# ── control tool: workspaces ───────────────────────────────────────────────


def _workspaces(home: Path, args: dict[str, Any]) -> dict[str, Any]:
    """List open task-isolation workspaces + a status snapshot per row.

    Read-only. A missing or empty ``<home>/worktrees/`` directory is not an
    error: ``workspaces: []`` is the right answer. Broken git status calls
    on a single row surface as ``status: {"exists": false}`` so one bad
    worktree does not poison the whole list.
    """
    from mini_ork import workspaces as _workspaces

    rows: list[dict[str, Any]] = []
    try:
        for ws in _workspaces.list_open(home):
            try:
                snap = _workspaces.status(ws)
            except Exception as exc:  # noqa: BLE001 — one bad row must not poison the list
                snap = {"exists": False, "error": str(exc)}
            rows.append({
                "run_id": ws.run_id,
                "path": str(ws.path),
                "branch": ws.branch,
                "base_branch": ws.base_branch,
                "base_sha": ws.base_sha,
                "project": str(ws.project),
                "status": snap,
            })
    except Exception as exc:  # noqa: BLE001
        return {"error": f"list_open failed: {exc}"}
    return {"workspaces": rows}


# ── control tool: run_status ───────────────────────────────────────────────


_TERMINAL_STATUSES = frozenset({"published", "rolled_back", "failed"})


def _run_status(home: Path, args: dict[str, Any]) -> dict[str, Any]:
    run_id = args.get("run_id")
    if not isinstance(run_id, str) or not run_id.strip():
        return {"error": "run_id is required"}
    run_id = run_id.strip()

    db = _open_db(home)
    if db is None:
        return _missing_home_error(home)
    if not db.has_table("task_runs"):
        return {"error": f"run not found: {run_id}"}

    from mini_ork.web.repositories import RunDetailRepository

    repo = RunDetailRepository(db)
    row = repo.fetch_task_run_row(run_id)
    if row is None:
        return {"error": f"run not found: {run_id}"}

    # Reduce node lifecycle events into per-node_id state.
    # node_end → done; node_start only → running; otherwise pending.
    nodes: dict[str, str] = {}
    events = repo.fetch_node_lifecycle_events(run_id)
    for ev in events:
        payload = ev.get("payload_json") or "{}"
        try:
            pj = json.loads(payload) if isinstance(payload, str) else (payload or {})
        except json.JSONDecodeError:
            pj = {}
        if not isinstance(pj, dict):
            pj = {}
        node_id = pj.get("node_id")
        if not isinstance(node_id, str) or not node_id:
            continue
        et = ev.get("event_type")
        if et == "node_end":
            nodes[node_id] = "done"
        elif et == "node_start":
            nodes.setdefault(node_id, "running")

    nodes_list = [{"node_id": nid, "state": state} for nid, state in sorted(nodes.items())]
    try:
        cost_usd = float(row.get("cost_usd") or 0.0)
    except (TypeError, ValueError):
        cost_usd = 0.0

    return {
        "run_id": run_id,
        "status": row.get("status"),
        "recipe": row.get("recipe"),
        "cost_usd": cost_usd,
        "nodes": nodes_list,
    }


# ── control tool: wait_for_run ─────────────────────────────────────────────


def _wait_for_run(home: Path, args: dict[str, Any]) -> dict[str, Any]:
    run_id = args.get("run_id")
    if not isinstance(run_id, str) or not run_id.strip():
        return {"error": "run_id is required"}
    run_id = run_id.strip()

    try:
        timeout_s = int(args.get("timeout_s") or 600)
    except (TypeError, ValueError):
        timeout_s = 600
    timeout_s = max(30, min(timeout_s, 1800))

    try:
        poll_s = int(os.environ.get("MO_MCP_POLL_S") or 5)
    except (TypeError, ValueError):
        poll_s = 5
    if poll_s < 1:
        poll_s = 1

    deadline = time.monotonic() + timeout_s
    started = time.monotonic()
    last: dict[str, Any] | None = None
    terminal = False
    while time.monotonic() < deadline:
        last = _run_status(home, {"run_id": run_id})
        if not isinstance(last, dict) or last.get("error"):
            return last if isinstance(last, dict) else {"error": "run_status non-dict"}
        status = last.get("status")
        if isinstance(status, str) and status in _TERMINAL_STATUSES:
            terminal = True
            break
        time.sleep(poll_s)

    if last is None:
        last = {"run_id": run_id, "status": None, "recipe": None, "cost_usd": 0.0, "nodes": []}

    waited = int(time.monotonic() - started)
    out: dict[str, Any] = {**last, "terminal": terminal, "waited_s": waited}

    if terminal:
        # Surface verdict.json + last 30 lines of the launch log so the
        # orchestrator has a compact, parseable terminal snapshot.
        verdict_path = home / "runs" / run_id / "verdict.json"
        if verdict_path.is_file():
            try:
                with verdict_path.open("r", encoding="utf-8") as f:
                    out["verdict"] = json.load(f)
            except (OSError, json.JSONDecodeError):
                out["verdict"] = None
        log_path = home / "runs-inbox" / f"{run_id}.launch.log"
        if log_path.is_file():
            try:
                with log_path.open("r", encoding="utf-8", errors="replace") as f:
                    lines = f.readlines()
                out["launch_log_tail"] = "".join(lines[-30:])
            except OSError:
                out["launch_log_tail"] = None

    return out


# ── control tool: stop_run ─────────────────────────────────────────────────


def _stop_run(home: Path, args: dict[str, Any]) -> dict[str, Any]:
    run_id = args.get("run_id")
    if not isinstance(run_id, str) or not run_id.strip():
        return {"error": "run_id is required"}
    run_id = run_id.strip()
    hard = bool(args.get("hard"))

    db = _open_db(home)
    if db is None:
        return _missing_home_error(home)

    from mini_ork.web.control import stop_run as _ctl_stop, kill_run as _ctl_kill

    fn = _ctl_kill if hard else _ctl_stop
    try:
        result = fn(home, db, run_id)
    except Exception as exc:
        return {"error": f"stop_run failed: {exc}"}
    if not isinstance(result, dict):
        return {"error": f"stop_run returned non-dict: {result!r}"}
    return result


# ── control tool: certify ──────────────────────────────────────────────────


def _certify(home: Path, args: dict[str, Any]) -> dict[str, Any]:
    issue = args.get("issue")
    if not isinstance(issue, str) or not issue.strip():
        return {"error": "issue is required"}
    issue = issue.strip()
    base = args.get("base") or "HEAD~1"
    head = args.get("head") or "HEAD"
    if not isinstance(base, str) or not isinstance(head, str):
        return {"error": "base and head must be strings"}

    try:
        timeout_s = int(os.environ.get("MO_MCP_CERTIFY_TIMEOUT_S") or 900)
    except (TypeError, ValueError):
        timeout_s = 900
    if timeout_s < 30:
        timeout_s = 30

    from mini_ork.verify.test_env import scrubbed_test_env
    from mini_ork.web.control import _mini_ork_root

    try:
        root = _mini_ork_root()
    except Exception as exc:
        return {"error": f"could not resolve engine root: {exc}"}

    bin_path = root / "bin" / "mini-ork"
    if not bin_path.is_file():
        return {"error": f"bin/mini-ork not found under {root}"}

    # Strip operator credentials + live-run pointers from the subprocess
    # env so the cert cannot leak or rebind against the operator's state.
    env = scrubbed_test_env()
    env.pop("MINI_ORK_VENV_ACTIVE", None)  # mirror launch_run: drop the re-exec marker
    env["MINI_ORK_ROOT"] = str(root)
    env["MINI_ORK_HOME"] = str(home)

    cmd = [
        sys.executable, str(bin_path), "certify",
        "--repo", str(home.parent),
        "--base", base,
        "--head", head,
        "--issue", issue,
        "--json",
    ]
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(root),
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout_s,
        )
    except subprocess.TimeoutExpired:
        return {"error": f"certify timed out after {timeout_s}s", "exit_code": None}
    except OSError as exc:
        return {"error": f"certify spawn failed: {exc}", "exit_code": None}

    out: dict[str, Any] = {"exit_code": proc.returncode}
    stdout = proc.stdout or ""
    stderr = proc.stderr or ""
    parsed: Any = None
    try:
        parsed = json.loads(stdout)
    except json.JSONDecodeError:
        parsed = None
    if isinstance(parsed, (dict, list)):
        out["certificate"] = parsed
    else:
        out["certificate"] = None
        # Non-JSON stdout/stderr tails (≤2 KiB each) so callers can see
        # what the cert subprocess actually produced.
        out["stdout_tail"] = stdout[-2048:]
        out["stderr_tail"] = stderr[-2048:]
    return out


# ── tool dispatch ─────────────────────────────────────────────────────────


def _tool_home(home: Path) -> dict[str, Any]:
    """Common gate run before every tool.

    Empty dict → proceed; non-empty → return it verbatim as the tool
    result (the JSON-RPC envelope wraps it as ``isError: true``).
    """
    if not home.exists():
        return _missing_home_error(home)
    return {}


TOOL_DEFS: list[dict[str, Any]] = [
    {
        "name": "list_runs",
        "description": (
            "List mini-ork runs newest-first (id, status, recipe, cost, "
            "created_at, derived title). Read-only. ``total`` is the number "
            "of matching runs; ``runs`` holds at most ``limit`` (default 20)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "minimum": 1, "maximum": 200},
                "status": {"type": "string"},
            },
        },
    },
    {
        "name": "run_detail",
        "description": (
            "Full task_runs row + node lifecycle events + first 2000 chars "
            "of the kickoff + verdict.json for a single run_id."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"run_id": {"type": "string"}},
            "required": ["run_id"],
        },
    },
    {
        "name": "learnings",
        "description": (
            "Failure-mode gradients (per task_class), emergent patterns, "
            "and memory (tasks + agents), each filterable by substring."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "task_class": {"type": "string"},
                "query": {"type": "string"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 200},
            },
        },
    },
    {
        "name": "cost",
        "description": "Cost-per-day rows for the last N days plus a total.",
        "inputSchema": {
            "type": "object",
            "properties": {"days": {"type": "integer", "minimum": 1}},
        },
    },
    {
        "name": "lanes",
        "description": "lane_name → family map from the active agents config.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "describe_recipe",
        "description": (
            "Everything about one recipe: purpose, request keywords, steps "
            "with their model roles, checks, grade with fix hints, track "
            "record, files. Returns the card dict; unknown id → error object."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"id": {"type": "string"}},
            "required": ["id"],
        },
    },
    {
        "name": "list_automations",
        "description": (
            "List every automation in the project: id, name, recipe, "
            "schedule, when (human description), enabled, workspace, "
            "next_fire (ISO, null when paused), last_run, runs (audit). "
            "Plus the OS scheduler status. Read-only."
        ),
        "inputSchema": {"type": "object", "properties": {}},
    },
]


_CONTROL_TOOL_DEFS: list[dict[str, Any]] = [
    {
        "name": "list_recipes",
        "description": (
            "List recipes visible to the current project. Each entry covers "
            "either an engine recipe (under <engine_root>/recipes/) or a "
            "project recipe (under <home>/recipes/); a project recipe with "
            "the same id as an engine recipe wins on the name clash and the "
            "engine one is omitted. Returns "
            "{id, description, source, nodes, overrides_engine?} per recipe."
        ),
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "start_run",
        "description": (
            "Launch a new mini-ork run via `bin/mini-ork run <recipe> <kickoff>`. "
            "Sets MO_TARGET_CWD to the project root so the spawned run operates "
            "on the user's checkout. Returns {ok, run_id, recipe, pid, "
            "kickoff_path, log_path} on success or {error} on failure."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "recipe": {"type": "string"},
                "kickoff_markdown": {"type": "string"},
                "workspace": {
                    "type": "string",
                    "enum": ["worktree", "in-place"],
                    "description": (
                        "Per-run workspace: 'worktree' isolates the run on "
                        "its own git branch under <home>/worktrees/<run_id>; "
                        "'in-place' runs against the project checkout. "
                        "Default: MO_WORKSPACE_MODE env, else 'worktree'. "
                        "A non-git project falls back to 'in-place' with a note."
                    ),
                },
            },
            "required": ["recipe", "kickoff_markdown"],
        },
    },
    {
        "name": "workspaces",
        "description": (
            "List open task-isolation workspaces (Zed S4). Each row carries "
            "the workspace's branch / base / path plus a status snapshot "
            "(commits_ahead, uncommitted, added, removed, files). "
            "Read-only — Merge / Discard live in a later slice."
        ),
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "run_status",
        "description": (
            "Snapshot of a single run: status, recipe, cost_usd, and "
            "per-node state derived from node_start/node_end lifecycle events."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"run_id": {"type": "string"}},
            "required": ["run_id"],
        },
    },
    {
        "name": "wait_for_run",
        "description": (
            "Poll run_status until the run reaches a terminal state "
            "(published, rolled_back, failed) or timeout_s passes. "
            "timeout_s is clamped to [30, 1800] (default 600). Returns the "
            "last status plus {terminal, waited_s}; when terminal, includes "
            "verdict.json and the last 30 lines of the launch log."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "run_id": {"type": "string"},
                "timeout_s": {"type": "integer", "minimum": 30, "maximum": 1800},
            },
            "required": ["run_id"],
        },
    },
    {
        "name": "stop_run",
        "description": (
            "Stop a running task. soft (hard=false) writes a stop-requested "
            "flag the dispatcher honours before the next node; hard "
            "(hard=true) SIGTERMs then SIGKILLs the dispatcher pid."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "run_id": {"type": "string"},
                "hard": {"type": "boolean"},
            },
            "required": ["run_id"],
        },
    },
    {
        "name": "certify",
        "description": (
            "Spawn `bin/mini-ork certify --json` against base..head in the "
            "project root (timeout MO_MCP_CERTIFY_TIMEOUT_S, default 900). "
            "Returns the parsed certificate plus exit_code on JSON output; "
            "stdout/stderr tails on non-JSON output."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "issue": {"type": "string"},
                "base": {"type": "string"},
                "head": {"type": "string"},
            },
            "required": ["issue"],
        },
    },
    {
        "name": "recipe_guide",
        "description": (
            "Describe what a recipe spec looks like: JSON schema, step "
            "types, the home's lane map, short authoring rules, and an "
            "example. Read-only; no project state changes. Returns "
            "{spec_schema, step_types, roles, rules, example}."
        ),
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "draft_recipe",
        "description": (
            "Render a recipe spec into <home>/recipe-drafts/<id>/ "
            "WITHOUT writing into <home>/recipes/. The user must approve "
            "before any commit (no commit tool is exposed via MCP). "
            "Returns {ok, draft_id, target, exists, files, warnings, grade} "
            "on success; {ok: false, errors} on validation failure. The "
            "spec object follows recipe_guide().spec_schema; `base` is "
            "optional and, when given, must equal the spec's id (editing "
            "an existing project recipe)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "spec": {"type": "object"},
                "base": {"type": "string"},
            },
            "required": ["spec"],
        },
    },
    {
        "name": "get_recipe_spec",
        "description": (
            "Return the spec a project or engine recipe was authored from "
            "(the parsed <recipe>/recipe.spec.json). Returns {error: ...} "
            "when the recipe was hand-edited and not authored from a spec; "
            "copy it into the project first to edit it via draft_recipe."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"id": {"type": "string"}},
            "required": ["id"],
        },
    },
    {
        "name": "propose_automation",
        "description": (
            "Propose a scheduled run of a recipe. This does NOT schedule "
            "anything: the user sees the proposal and creates it with a "
            "button. Schedule is a 5-field cron string in local time "
            "(minute hour day-of-month month day-of-week; 0 = Sunday), e.g. "
            "`0 9 * * 1-5` = every weekday at 09:00. Calling it again with "
            "the same id replaces the proposal."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "id": {"type": "string"},
                "name": {"type": "string"},
                "recipe": {"type": "string"},
                "schedule": {"type": "string"},
                "kickoff_markdown": {"type": "string"},
                "workspace": {
                    "type": "string",
                    "enum": ["worktree", "in-place"],
                },
            },
            "required": [
                "id", "name", "recipe", "schedule", "kickoff_markdown",
            ],
        },
    },
    {
        "name": "draft_kickoff",
        "description": (
            "Show the user a kickoff before any run starts. It does NOT "
            "start a run: the user starts it with a button. Fix every "
            "finding of severity error and draft again."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "recipe": {"type": "string"},
                "kickoff_markdown": {"type": "string"},
            },
            "required": ["recipe", "kickoff_markdown"],
        },
    },
]


def _all_tool_defs(control: bool) -> list[dict[str, Any]]:
    """Tool list for ``tools/list``. Additive: read-only 5 + control 9 when
    ``control=True``; the read-only 5 only by default — default mode MUST
    stay byte-identical to the pre-`--control` schema.
    """
    if control:
        return list(TOOL_DEFS) + list(_CONTROL_TOOL_DEFS)
    return list(TOOL_DEFS)


# ── tool: recipe_guide / draft_recipe / get_recipe_spec ───────────────────────


def _recipe_guide(home: Path, args: dict[str, Any]) -> dict[str, Any]:
    from mini_ork.recipe_author import guide

    return guide(home)


def _draft_recipe(home: Path, args: dict[str, Any]) -> dict[str, Any]:
    spec = args.get("spec")
    if not isinstance(spec, dict):
        return {"error": "spec object is required"}
    base = args.get("base")
    if base is not None and not isinstance(base, str):
        return {"error": "base must be a string when provided"}
    from mini_ork.recipe_author import draft

    try:
        return draft(home, spec, base=base)
    except Exception as exc:
        _log(f"draft_recipe raised: {exc}")
        return {"error": f"draft_recipe: {exc}"}


def _draft_kickoff(home: Path, args: dict[str, Any]) -> dict[str, Any]:
    """Lint + write a single-file kickoff under ``<home>/kickoff-drafts/<slug>.md``.

    Mirrors :func:`_draft_recipe`'s staging discipline but writes ONE file
    (not a tree) — a kickoff is a markdown document. The slug is derived
    from the title line (``mini_ork.kickoff_lint.slug``); re-drafting the
    same kickoff atomically replaces the staged file (``os.replace``).
    Never starts a run: the user starts it with the button.
    """
    import tempfile
    from mini_ork import kickoff_lint
    from mini_ork.recipes_catalog import find_recipe

    if not isinstance(args, dict):
        return {"error": "draft_kickoff: arguments must be an object"}

    recipe = args.get("recipe")
    markdown = args.get("kickoff_markdown")
    if not isinstance(recipe, str) or not recipe.strip():
        return {"error": "draft_kickoff: `recipe` is required"}
    if not isinstance(markdown, str):
        return {"error": "draft_kickoff: `kickoff_markdown` is required"}

    if find_recipe(recipe.strip(), home) is None:
        return {"ok": False, "error": f"unknown recipe: {recipe}"}

    try:
        findings = kickoff_lint.lint(
            markdown, project=home.parent, recipe=recipe.strip(), home=home,
        )
    except Exception as exc:  # noqa: BLE001
        _log(f"kickoff_lint.lint raised: {exc}")
        return {"error": f"draft_kickoff: lint failed: {exc}"}

    draft_id = kickoff_lint.slug(markdown) or "kickoff"
    drafts = home / "kickoff-drafts"
    drafts.mkdir(parents=True, exist_ok=True)
    draft_path = drafts / f"{draft_id}.md"
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=str(drafts),
            prefix=f"{draft_id}.",
            suffix=".md.tmp",
            delete=False,
        ) as tmp:
            tmp.write(markdown)
            tmp_path = Path(tmp.name)
        os.replace(tmp_path, draft_path)
    except Exception as exc:  # noqa: BLE001
        _log(f"draft_kickoff write raised: {exc}")
        return {"error": f"draft_kickoff: write failed: {exc}"}

    return {
        "ok": True,
        "draft_id": draft_id,
        "recipe": recipe.strip(),
        "path": str(draft_path),
        "findings": findings,
    }


def _get_recipe_spec(home: Path, args: dict[str, Any]) -> dict[str, Any]:
    recipe_id = args.get("id")
    if not isinstance(recipe_id, str) or not recipe_id.strip():
        return {"error": "id is required"}
    rid = recipe_id.strip()
    from mini_ork.recipe_author import get_spec

    try:
        spec = get_spec(home, rid)
    except Exception as exc:
        _log(f"get_recipe_spec raised: {exc}")
        return {"error": f"get_recipe_spec: {exc}"}
    if spec is None:
        return {
            "error": (
                f"recipe {rid!r} was not authored from a spec; "
                "copy it into the project to edit via draft_recipe"
            )
        }
    return spec


# ── tool: list_automations / propose_automation (Zed S6b-1) ──────────────────


def _list_automations(home: Path, args: dict[str, Any]) -> dict[str, Any]:
    """Read-only view of every automation + scheduler status."""
    del args
    from mini_ork import automations as _auto

    items: list[dict[str, Any]] = []
    try:
        automations = _auto.load(home)
    except Exception as exc:  # noqa: BLE001
        return {"error": f"list_automations: {exc}"}
    statuses = _auto.run_statuses(home) if automations else {}
    for a in automations:
        aid = a.get("id")
        if not isinstance(aid, str):
            continue
        schedule = str(a.get("schedule") or "")
        next_fire: str | None
        if not a.get("enabled", True):
            next_fire = None
        else:
            try:
                fires = _auto.next_fires(schedule, n=1)
            except Exception:  # noqa: BLE001
                fires = []
            next_fire = fires[0].isoformat() if fires else None
        runs = list(a.get("runs") or [])
        last_run: str
        last_run = _auto.last_run_status(home, a, statuses=statuses)
        items.append({
            "id": aid,
            "name": a.get("name") or aid,
            "recipe": a.get("recipe") or "",
            "schedule": schedule,
            "when": _auto.describe(schedule),
            "enabled": bool(a.get("enabled", True)),
            "workspace": a.get("workspace") or "worktree",
            "next_fire": next_fire,
            "last_run": last_run,
            "runs": runs,
        })
    scheduler: dict[str, Any]
    try:
        scheduler = _auto.scheduler_status(home)
    except Exception as exc:  # noqa: BLE001
        scheduler = {"error": str(exc)}
    return {"automations": items, "scheduler": scheduler}


def _propose_automation(home: Path, args: dict[str, Any]) -> dict[str, Any]:
    """Validate + write a draft automation; never touches the store."""
    from mini_ork import automations as _auto

    if not isinstance(args, dict):
        return {"error": "propose_automation: arguments must be an object"}

    def _str(key: str) -> str | None:
        v = args.get(key)
        if isinstance(v, str):
            return v
        return None

    aid = _str("id")
    name = _str("name")
    recipe = _str("recipe")
    schedule = _str("schedule")
    kickoff = _str("kickoff_markdown")
    workspace = _str("workspace") or "worktree"
    missing = [
        k for k, v in (
            ("id", aid), ("name", name), ("recipe", recipe),
            ("schedule", schedule), ("kickoff_markdown", kickoff),
        ) if not v
    ]
    if missing:
        return {"error": f"propose_automation: required: {', '.join(missing)}"}

    try:
        result = _auto.propose(
            home,
            id=aid,  # type: ignore[arg-type]
            name=name,  # type: ignore[arg-type]
            recipe=recipe,  # type: ignore[arg-type]
            kickoff=kickoff,  # type: ignore[arg-type]
            schedule=schedule,  # type: ignore[arg-type]
            workspace=workspace,
        )
    except Exception as exc:  # noqa: BLE001
        _log(f"propose_automation raised: {exc}")
        return {"error": f"propose_automation: {exc}"}
    return result


# ── tool: dispatch ───────────────────────────────────────────────────────────


def _call_tool(
    home: Path,
    name: str,
    args: dict[str, Any],
    *,
    control: bool = False,
) -> dict[str, Any]:
    """Invoke ``name`` with ``args``. Returns a result dict.

    Two failure shapes:
    * Missing home/DB → ``{"error": ...}`` (per kickoff spec).
    * Raised exception or unknown tool → ``{"error": ...}`` so the JSON-RPC
      envelope can flip ``isError: true`` and the client sees a string
      message rather than an opaque internal exception.

    When ``control=False`` (default) the six control tools are not just
    rejected at dispatch — calling one returns ``{"error": "unknown tool"}``
    so a misconfigured client sees the same shape as any other missing
    tool, never a 401-style privilege error.
    """
    gate = _tool_home(home)
    if gate:
        return gate
    try:
        if name == "list_runs":
            return _list_runs(home, args)
        if name == "run_detail":
            return _run_detail(home, args)
        if name == "learnings":
            return _learnings(home, args)
        if name == "cost":
            return _cost(home, args)
        if name == "lanes":
            return _lanes(home, args)
        if name == "describe_recipe":
            return _describe_recipe(home, args)
        if name == "list_automations":
            return _list_automations(home, args)
        if control:
            if name == "list_recipes":
                return _list_recipes(home, args)
            if name == "start_run":
                return _start_run(home, args)
            if name == "workspaces":
                return _workspaces(home, args)
            if name == "run_status":
                return _run_status(home, args)
            if name == "wait_for_run":
                return _wait_for_run(home, args)
            if name == "stop_run":
                return _stop_run(home, args)
            if name == "certify":
                return _certify(home, args)
            if name == "recipe_guide":
                return _recipe_guide(home, args)
            if name == "draft_recipe":
                return _draft_recipe(home, args)
            if name == "get_recipe_spec":
                return _get_recipe_spec(home, args)
            if name == "propose_automation":
                return _propose_automation(home, args)
            if name == "draft_kickoff":
                return _draft_kickoff(home, args)
    except Exception as exc:  # defensive — kickoff says no exception ever
        _log(f"tool {name} raised: {exc}")
        return {"error": f"{name}: {exc}"}
    return {"error": f"unknown tool: {name}"}


def dispatch(req: dict[str, Any], *, control: bool = False) -> dict[str, Any] | None:
    """Run ONE JSON-RPC request through the server and return its response.

    Returns ``None`` for notifications (no response). ``None`` for
    malformed/empty input. Always returns a dict for everything else.

    Exposed as a public function so tests can drive the loop
    in-process without spawning a subprocess or touching stdio. The
    ``control`` flag is the single source of truth — the server does
    NOT re-read ``MO_MCP_CONTROL`` from the environment, so a default
    ``control=False`` keeps the read-only 5-tool list even when a
    parent shell leaked the env var.
    """
    if not isinstance(req, dict):
        return None
    method = req.get("method")
    req_id = req.get("id")
    params = req.get("params") or {}

    if method == "initialize":
        client_version = None
        if isinstance(params, dict):
            client_version = params.get("protocolVersion")
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "protocolVersion": client_version or PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": SERVER_NAME, "version": VERSION},
            },
        }

    if method == "notifications/initialized":
        return None

    if method == "tools/list":
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {"tools": _all_tool_defs(control)},
        }

    if method == "tools/call":
        if not isinstance(params, dict):
            return _error_envelope(req_id, -32602, "tools/call params must be an object")
        tool = params.get("name")
        args = params.get("arguments") or {}
        if not isinstance(args, dict):
            args = {}
        result = _call_tool(_resolve_home(), str(tool or ""), args, control=control)
        is_error = bool(result.get("error")) if isinstance(result, dict) else False
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "content": [{
                    "type": "text",
                    "text": json.dumps(result, indent=1, default=str),
                }],
                "isError": is_error,
            },
        }

    if method is None:
        return None
    return _error_envelope(req_id, -32601, f"unknown method: {method}")


def _error_envelope(req_id: Any, code: int, message: str) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": req_id,
        "error": {"code": code, "message": message},
    }


def serve(stdin=None, stdout=None, *, control: bool = False) -> int:
    """Run the JSON-RPC loop on the given streams (defaults: real stdio).

    Returns 0 on clean EOF. Malformed JSON lines are skipped (logged to
    stderr). Handler exceptions are converted to a `-32603` JSON-RPC
    error so a tool bug never kills the loop. The ``control`` keyword
    enables the six write-capable tools — pass it from the CLI which
    is the only place that should ever resolve the flag.
    """
    if stdin is None:
        stdin = sys.stdin
    if stdout is None:
        stdout = sys.stdout
    _log(f"started v{VERSION} home={_resolve_home()} control={control}")
    for raw in stdin:
        raw = raw.strip()
        if not raw:
            continue
        try:
            req = json.loads(raw)
        except json.JSONDecodeError as exc:
            _log(f"bad json: {exc}")
            continue
        try:
            resp = dispatch(req, control=control)
        except Exception as exc:
            _log(f"handler error: {exc}")
            resp = _error_envelope(
                req.get("id") if isinstance(req, dict) else None,
                -32603,
                str(exc),
            )
        if resp is not None:
            stdout.write(json.dumps(resp) + "\n")
            stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(serve())