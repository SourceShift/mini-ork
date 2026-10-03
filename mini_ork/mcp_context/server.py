"""Read-only stdio MCP server exposing ``mini-ork`` observability data.

JSON-RPC 2.0 over stdin/stdout, one JSON object per line. ``stdout`` is the
protocol channel — every diagnostic this module emits goes to ``stderr``;
importing the module prints nothing.

Tools (all read-only; the ``StateDB`` is opened with ``mode=ro`` +
``PRAGMA query_only`` at :class:`mini_ork.web.db.StateDB`):

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

Resolution: ``MINI_ORK_HOME`` else ``<cwd>/.mini-ork`` (via
:func:`mini_ork.web.db.resolve_home`). A missing home OR missing
``state.db`` is reported as a per-tool ``{"error": ...}`` object — never
an exception — so a stale workspace can't break a long-running MCP
client.
"""
from __future__ import annotations

import datetime as _dt
import json
import sys
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
    """``MINI_ORK_HOME`` else ``<cwd>/.mini-ork`` (resolved)."""
    from mini_ork.web.db import resolve_home

    return resolve_home(None)


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
    return {"runs": runs}


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
            "created_at, derived title). Read-only."
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
]


def _call_tool(home: Path, name: str, args: dict[str, Any]) -> dict[str, Any]:
    """Invoke ``name`` with ``args``. Returns a result dict.

    Two failure shapes:
    * Missing home/DB → ``{"error": ...}`` (per kickoff spec).
    * Raised exception or unknown tool → ``{"error": ...}`` so the JSON-RPC
      envelope can flip ``isError: true`` and the client sees a string
      message rather than an opaque internal exception.
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
    except Exception as exc:  # defensive — kickoff says no exception ever
        _log(f"tool {name} raised: {exc}")
        return {"error": f"{name}: {exc}"}
    return {"error": f"unknown tool: {name}"}


def dispatch(req: dict[str, Any]) -> dict[str, Any] | None:
    """Run ONE JSON-RPC request through the server and return its response.

    Returns ``None`` for notifications (no response). ``None`` for
    malformed/empty input. Always returns a dict for everything else.

    Exposed as a public function so tests can drive the loop
    in-process without spawning a subprocess or touching stdio.
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
            "result": {"tools": TOOL_DEFS},
        }

    if method == "tools/call":
        if not isinstance(params, dict):
            return _error_envelope(req_id, -32602, "tools/call params must be an object")
        tool = params.get("name")
        args = params.get("arguments") or {}
        if not isinstance(args, dict):
            args = {}
        result = _call_tool(_resolve_home(), str(tool or ""), args)
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


def serve(stdin=None, stdout=None) -> int:
    """Run the JSON-RPC loop on the given streams (defaults: real stdio).

    Returns 0 on clean EOF. Malformed JSON lines are skipped (logged to
    stderr). Handler exceptions are converted to a `-32603` JSON-RPC
    error so a tool bug never kills the loop.
    """
    if stdin is None:
        stdin = sys.stdin
    if stdout is None:
        stdout = sys.stdout
    _log(f"started v{VERSION} home={_resolve_home()}")
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
            resp = dispatch(req)
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