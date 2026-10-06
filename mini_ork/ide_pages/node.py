"""One DAG node's detail panel — stream / output / prompt / telemetry / learning.

``mini-ork board node <run_id> <node_id> [--view V] [--offset N]`` resolves to
``build_node(home, run_id, node_id, view, offset)`` and returns one dict with
the shape the IDE's node-detail side panel draws. The function is deliberately
named ``build_node`` rather than ``build`` because this module is NOT a
registered ``PAGES`` entry — the kickoff routes ``board node`` directly to
``mini_ork.ide_pages.node.build_node`` so there is no second path to the same
data through ``build_page``.

Stream view sources, in order:

* the run's ``sessions/<uuid>.jsonl`` (Claude Code transcript), resolved by
  the three-rule mapping from the prior-art lens (live result event →
  ``llm_calls.session_id`` → newest post-start session file);
* the node's shell/verifier log (``verifier_<node>.log``,
  ``evidence/<node>*.log``, ``impl-<node>.log``) when there is no session;
* ``operator_steering`` rows for the run, merged by timestamp.

The transcript reader does NOT reuse ``mini_ork.acp.live.normalize`` — that
function emits ACP kinds (``text/thought/tool/tool_output``); the IDE wants
``user/think/text/tool/todo/steer/note`` with Edit-diff colouring. The
byte-offset discipline from ``LiveTail`` is mirrored here for the
``agent-<node>.live.jsonl`` sidecar so the session-id lookup doesn't read a
partial line; the session file itself is read in one shot because it is
written once at agent-finish time.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

from mini_ork.ide_pages import spec as S
from mini_ork.ide_pages.run import Run, Node, _load, _wall

# Live-detection window per the kickoff §"live": running AND transcript grew
# in the last 120 s. Named so a future tweak is one edit.
LIVE_WINDOW_SECONDS = 120

# Per-field caps from the kickoff §"stream":
USER_HEAD_CAP = 400
THINK_HEAD_CAP = 600
TEXT_CAP = 4_000
TOOL_RESULT_LINES = 12
TOOL_INPUT_SUMMARY_CAP = 160
SHELL_LOG_LINES = 40
LINE_CHARS = 220

# Stream entry kinds in display order. The "kind" keys are the kickoff's
# verbatim shape — the IDE panel maps them onto draw routines.
_KIND_USER = "user"
_KIND_THINK = "think"
_KIND_TEXT = "text"
_KIND_TOOL = "tool"
_KIND_TODO = "todo"
_KIND_STEER = "steer"
_KIND_NOTE = "note"

_DEFAULT_VIEW = "stream"
_VIEWS = ("stream", "output", "prompt", "telemetry", "learning")

# Edit-family tools whose input carries an old/new body the IDE renders as a
# coloured diff. Listed verbatim per the kickoff.
_EDIT_TOOLS = frozenset({"Edit", "MultiEdit", "apply_diff"})


def _reject_unsafe(name: str, value: str) -> str | None:
    """Path-traversal guard. Mirrors ``web/routes/node_live._live_path``."""
    if not value or ".." in value or "/" in value or "\\" in value:
        return f"invalid {name}"
    return None


def _resolve_session_path(run_dir: Path, node: Node) -> Path | None:
    """Three-rule node→session resolver.

    1. ``session_id`` in the ``agent-<node>.live.jsonl`` result event.
    2. (Skipped in this surface — no DB write here. The session_id from step
       1 is already authoritative when the live file exists.)
    3. For a still-running node, the newest ``sessions/*.jsonl`` created
       after ``node.start`` whose name carries the node id; we match by
       mtime relative to ``node.start``.
    """
    live_path = run_dir / f"agent-{node.id}.live.jsonl"
    if live_path.is_file():
        try:
            tail = live_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            tail = ""
        # Walk the last 50 lines — result events arrive at session end and
        # there are usually <50 stdout/lines before them.
        for raw in tail.splitlines()[-50:]:
            line = raw.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except (json.JSONDecodeError, TypeError):
                continue
            inner = rec.get("line") if isinstance(rec, dict) else None
            if not isinstance(inner, str):
                continue
            try:
                env = json.loads(inner)
            except (json.JSONDecodeError, TypeError):
                continue
            if not isinstance(env, dict):
                continue
            sid = env.get("session_id")
            if not isinstance(sid, str) or not sid:
                continue
            candidate = run_dir / "sessions" / f"{sid}.jsonl"
            if candidate.is_file():
                return candidate

    if node.start is not None:
        sessions_dir = run_dir / "sessions"
        if sessions_dir.is_dir():
            try:
                cands = sorted(
                    (p for p in sessions_dir.glob("*.jsonl")),
                    key=lambda p: p.stat().st_mtime,
                    reverse=True,
                )
            except OSError:
                cands = []
            for cand in cands[:5]:
                try:
                    if cand.stat().st_mtime >= node.start - 5:
                        return cand
                except OSError:
                    continue
    return None


def _resolve_log_path(run_dir: Path, node_id: str) -> Path | None:
    """Shell/verifier nodes have no session; their log is the stream source."""
    for rel in (f"verifier_{node_id}.log",
                f"evidence/{node_id}.log",
                f"impl-{node_id}.log"):
        path = run_dir / rel
        if path.is_file():
            return path
    return None


def _live(node: Node, session_path: Path | None, log_path: Path | None) -> bool:
    """``live`` = running AND transcript/log grew in the last 120 s.

    Falls back to file SIZE when mtime resolution is too coarse (macOS
    filesystem caches the mtime even though bytes grew — observed in
    feedback note ``feedback_opencode_build_hangs_on_full_disk`` neighbours).
    """
    if node.state != "running":
        return False
    cutoff = time.time() - LIVE_WINDOW_SECONDS
    for p in (session_path, log_path):
        if p is None:
            continue
        try:
            if p.stat().st_mtime >= cutoff:
                return True
        except OSError:
            continue
    return False


def _stream_status(node: Node, has_session: bool, has_log: bool) -> tuple[str, str]:
    if node.state == "running":
        if has_session or has_log:
            return ("attached · live", "green")
        return ("not dispatched yet", "sub")
    if has_session or has_log:
        return ("finished", "sub")
    return ("no transcript", "yellow")


def _meta(node: Node) -> str:
    parts: list[str] = []
    lane = node.family or node.role_lane or "—"
    parts.append(lane)
    if node.calls:
        parts.append(f"{node.calls} calls")
    if node.cost:
        parts.append(f"${node.cost:.2f}")
    return " · ".join(parts) if parts else "—"


def _kv_items(node: Node, duration: str) -> list[dict[str, str]]:
    return [
        {"k": "Lane", "v": node.role_lane or "—", "c": "text"},
        {"k": "Provider", "v": node.family or "—", "c": "text"},
        {"k": "Cost", "v": S.money(node.cost) + (f" · {node.calls} call"
                                                  f"{'s' if node.calls != 1 else ''}"
                                                  if node.calls else ""), "c": "text"},
        {"k": "Duration", "v": duration, "c": "text"},
        {"k": "Tokens", "v": "—", "c": "muted"},
        {"k": "Gates", "v": ", ".join(node.gates) or "—", "c": "text"},
    ]


def _actions(node: Node, run_id: str) -> list[dict[str, Any]]:
    if node.state == "running":
        return [
            S.btn("Stop run",
                  S.cli("board", "stop", run_id,
                        confirm="Stop this run after its current node?"),
                  "warn"),
            S.btn("Kill",
                  S.cli("board", "kill", run_id,
                        confirm=f"Kill {run_id}? SIGTERM, then SIGKILL after 2 s."),
                  "danger"),
        ]
    return [S.btn("Open run folder", S.reveal(str(_run_dir_for(run_id))), "ghost")]


def _run_dir_for(run_id: str) -> Path:
    """Resolve a run's directory from the active home — read-only hint for buttons.

    The button's ``reveal`` action only opens the directory in a file browser,
    so a missing directory on the IDE side just shows the empty-folder screen —
    no exception leaks. The home is taken from ``MINI_ORK_HOME`` first, then
    ``<cwd>/.mini-ork``, mirroring ``board_cmd._default_home``.
    """
    home = os.environ.get("MINI_ORK_HOME", "").strip() or str(Path.cwd() / ".mini-ork")
    return Path(home).expanduser().absolute() / "runs" / run_id


# ── transcript parsing ──────────────────────────────────────────────────────

def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    out: list[dict[str, Any]] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(obj, dict):
            out.append(obj)
    return out


def _tool_input_summary(input: Any) -> str:
    """One-line summary of a tool_use ``input``: file_path, pattern, or generic str."""
    if isinstance(input, dict):
        for key in ("file_path", "command", "pattern", "url", "path"):
            v = input.get(key)
            if isinstance(v, str):
                return v[:TOOL_INPUT_SUMMARY_CAP]
        try:
            return json.dumps(input, default=str)[:TOOL_INPUT_SUMMARY_CAP]
        except (TypeError, ValueError):
            return repr(input)[:TOOL_INPUT_SUMMARY_CAP]
    return str(input)[:TOOL_INPUT_SUMMARY_CAP]


def _edit_diff_lines(name: str, input: Any) -> list[dict[str, str]]:
    """Edit/MultiEdit: ``- old`` / ``+ new`` lines with bg colours."""
    out: list[dict[str, str]] = []
    if not isinstance(input, dict):
        return out
    if name == "Edit":
        old_lines = (input.get("old_string") or "").splitlines()[:6]
        new_lines = (input.get("new_string") or "").splitlines()[:6]
    elif name == "MultiEdit":
        edits_raw = input.get("edits")
        edits = edits_raw if isinstance(edits_raw, list) else []
        old_lines = []
        new_lines = []
        for edit in edits[:6]:
            if not isinstance(edit, dict):
                continue
            old_lines.extend((edit.get("old_string") or "").splitlines()[:3])
            new_lines.extend((edit.get("new_string") or "").splitlines()[:3])
    else:
        return out
    for ln in old_lines:
        out.append({"t": f"- {ln}", "c": "red", "bg": "red-bg"})
    for ln in new_lines:
        out.append({"t": f"+ {ln}", "c": "green", "bg": "green-bg"})
    return out


def _text_block(b: Any, kind: str) -> str:
    if not isinstance(b, dict):
        return ""
    if kind == "text":
        return str(b.get("text") or "")
    if kind == "thinking":
        return str(b.get("thinking") or "")
    return ""


def _stream_entries(session_path: Path | None, log_path: Path | None,
                    run_id: str, home: Path) -> list[dict[str, Any]]:
    """Build the stream view's ``entries`` list."""
    out: list[dict[str, Any]] = []
    if session_path is not None:
        out.extend(_session_entries(session_path))
    elif log_path is not None:
        out.extend(_log_path_entries(log_path))
    # Steering rows: merge AFTER transcript/log so they always show up at
    # the bottom of the panel as the most recent operator input.
    out.extend(_steer_entries(run_id, home))
    return out


def _session_entries(session_path: Path) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    entries = _read_jsonl(session_path)
    first_user_done = False
    pending: dict[str, dict[str, Any]] = {}  # tool_use_id → tool info

    for entry in entries:
        typ = str(entry.get("type") or "")
        message = entry.get("message") if isinstance(entry.get("message"), dict) else {}
        content = message.get("content") if isinstance(message, dict) else None

        if typ == "user":
            if not isinstance(content, list):
                continue
            if not first_user_done:
                first_user_done = True
                text = ""
                for b in content:
                    if isinstance(b, dict) and b.get("type") == "text":
                        text = str(b.get("text") or "")
                        break
                if text:
                    out.append({
                        "k": _KIND_USER,
                        "head": "",
                        "arg": text[:USER_HEAD_CAP],
                        "lines": [{"t": text[:USER_HEAD_CAP], "c": "body"}],
                    })
                continue
            # Subsequent user envelopes carry tool_results.
            for b in content:
                if not isinstance(b, dict) or b.get("type") != "tool_result":
                    continue
                tool_use_id = str(b.get("tool_use_id") or "")
                tool_info = pending.pop(tool_use_id, None)
                if tool_info is None:
                    continue
                result_text, is_error = _result_text(b)
                lines: list[dict[str, str]] = []
                if _is_edit_tool(tool_info["name"]):
                    lines.extend(_edit_diff_lines(tool_info["name"], tool_info["input"]))
                res_lines = result_text.splitlines()
                shown = res_lines[:TOOL_RESULT_LINES]
                for ln in shown:
                    lines.append({"t": ln[:LINE_CHARS], "c": "red" if is_error else "body"})
                if len(res_lines) > TOOL_RESULT_LINES:
                    lines.append({"t": f"… {len(res_lines) - TOOL_RESULT_LINES} more lines",
                                  "c": "muted"})
                out.append({
                    "k": _KIND_TOOL,
                    "head": tool_info["name"],
                    "arg": tool_info["summary"],
                    "lines": lines or [{"t": "(no output)", "c": "muted"}],
                })
        elif typ == "assistant":
            if not isinstance(content, list):
                continue
            for b in content:
                if not isinstance(b, dict):
                    continue
                bt = str(b.get("type") or "")
                if bt == "thinking":
                    text = _text_block(b, "thinking")[:THINK_HEAD_CAP]
                    out.append({
                        "k": _KIND_THINK,
                        "head": "Thinking… ",
                        "arg": text,
                        "lines": [{"t": text, "c": "muted"}],
                    })
                elif bt == "text":
                    text = _text_block(b, "text")[:TEXT_CAP]
                    out.append({
                        "k": _KIND_TEXT,
                        "head": "",
                        "arg": text,
                        "lines": [{"t": text, "c": "body"}],
                    })
                elif bt == "tool_use":
                    name = str(b.get("name") or "tool")
                    tin = b.get("input") or {}
                    tid = str(b.get("id") or "")
                    pending[tid] = {"name": name, "input": tin,
                                    "summary": _tool_input_summary(tin)}
                    if name == "TodoWrite" and isinstance(tin, dict):
                        todos_raw = tin.get("todos")
                        todos = todos_raw if isinstance(todos_raw, list) else []
                        todo_lines: list[dict[str, str]] = []
                        for t in todos:
                            if not isinstance(t, dict):
                                continue
                            mark = "☒" if str(t.get("status") or "") == "completed" else "☐"
                            todo_lines.append({"t": f"{mark} {t.get('content') or ''}",
                                               "c": "body"})
                        out.append({
                            "k": _KIND_TODO,
                            "head": "",
                            "arg": "",
                            "lines": todo_lines,
                        })
        elif typ == "result":
            result = str(entry.get("result") or "")
            cost = entry.get("total_cost_usd") or entry.get("usage", {}).get("total_cost_usd")
            turns = entry.get("num_turns")
            note_parts: list[str] = []
            if cost is not None:
                try:
                    note_parts.append(f"${float(cost):.2f}")
                except (TypeError, ValueError):
                    pass
            if turns is not None:
                note_parts.append(f"{turns} turns")
            note = "Done · " + " · ".join(note_parts) if note_parts else "Done"
            out.append({
                "k": _KIND_NOTE,
                "head": "",
                "arg": note,
                "lines": [{"t": result[:TEXT_CAP] if result else note, "c": "muted"}],
            })
    return out


def _is_edit_tool(name: str) -> bool:
    return name in _EDIT_TOOLS


def _result_text(block: dict[str, Any]) -> tuple[str, bool]:
    is_error = bool(block.get("is_error"))
    rc = block.get("content")
    text = ""
    if isinstance(rc, list):
        for c in rc:
            if isinstance(c, dict) and c.get("type") == "text":
                text = str(c.get("text") or "")
                break
    elif isinstance(rc, str):
        text = rc
    return text, is_error


def _log_path_entries(log_path: Path) -> list[dict[str, Any]]:
    try:
        text = log_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        text = ""
    lines = []
    for raw in text.splitlines()[-SHELL_LOG_LINES:]:
        c = "red" if "Traceback" in raw or "ERROR" in raw else (
            "green" if "PASS" in raw or "ok" in raw.lower() else "body")
        lines.append({"t": raw[:LINE_CHARS], "c": c})
    return [{
        "k": _KIND_TEXT,
        "head": "",
        "arg": "log output",
        "lines": lines,
    }]


def _steer_entries(run_id: str, home: Path) -> list[dict[str, Any]]:
    """Read operator_steering rows for this run DIRECTLY — NOT via fetch_for.

    ``operator_steering.fetch_for`` marks rows consumed in an UPDATE; using
    it here would consume rows the dispatcher's context_assembler still needs
    (the consume-on-read is shared state with ``cli/execute.py``). The IDE
    stream view is read-only display, so a direct SELECT is correct.
    """
    rows = _fetch_steer_rows(run_id, home)
    out: list[dict[str, Any]] = []
    for r in rows:
        sev = str(r.get("severity") or "info")
        msg = str(r.get("message") or "")
        c = "red" if sev == "critical" else ("yellow" if sev == "warn" else "blue")
        out.append({
            "k": _KIND_STEER,
            "head": "You · steering ",
            "arg": f"[{sev}] {msg}",
            "lines": [{"t": f"[{sev}] {msg}", "c": c}],
        })
    return out


def _fetch_steer_rows(run_id: str, home: Path) -> list[dict[str, Any]]:
    try:
        from mini_ork.web.deps import db_for
    except Exception:  # noqa: BLE001 — steering is optional display content
        return []
    if not (home / "state.db").exists():
        return []
    try:
        db = db_for(home)
    except Exception:  # noqa: BLE001
        return []
    if not db.has_table("operator_steering"):
        return []
    try:
        return db.rows(
            "SELECT run_id, role_target, severity, message, source, "
            "       confidence, created_at "
            "FROM operator_steering "
            "WHERE (run_id = ? OR run_id IS NULL OR run_id = '') "
            "  AND (expires_at IS NULL OR expires_at > ?) "
            "ORDER BY created_at",
            (run_id, int(time.time() * 1000)),
        )
    except Exception:  # noqa: BLE001
        return []


# ── output / prompt / telemetry / learning views ────────────────────────────

def _output_view(run_dir: Path, node: Node) -> dict[str, Any]:
    """Final text + report files the node wrote."""
    report_candidates = [
        run_dir / f"lens-{node.id}.md",
        run_dir / f"review-{node.id}.json",
        run_dir / f"verifier_{node.id}.json",
    ]
    block: list[dict[str, str]] = []
    block_title = "Node output"
    for path in report_candidates:
        if not path.is_file():
            continue
        text = _read_text(path)
        if text is None:
            continue
        block_title = f"Node output · {path.name}"
        for ln in text.splitlines()[-SHELL_LOG_LINES:]:
            block.append({"t": ln[:LINE_CHARS], "c": "body"})
        break
    if not block:
        block = [{"t": "No output file found for this node.", "c": "muted"}]
    arts: list[dict[str, Any]] = []
    for path in sorted(run_dir.glob("*")):
        if not path.is_file():
            continue
        rel = path.name
        if rel.startswith(".") or rel.endswith((".tmp", ".swp")):
            continue
        if any(rel.endswith(ext) for ext in (".log", ".jsonl")) and (
            node.id not in rel):
            continue
        arts.append(S.item(rel, f"{_size(path)} bytes", acts=[S.btn("Open", S.open_path(str(path)), "ghost")]))
    return {"block_title": block_title, "block": block,
            "list_title": "Artifacts", "list": arts}


def _read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def _size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def _prompt_view(node: Node, session_path: Path | None) -> dict[str, Any]:
    block_title = "Rendered prompt · as dispatched"
    block: list[dict[str, str]] = []
    if session_path is not None:
        for entry in _read_jsonl(session_path):
            if str(entry.get("type") or "") != "user":
                continue
            msg = entry.get("message") if isinstance(entry.get("message"), dict) else {}
            content = msg.get("content") if isinstance(msg, dict) else None
            if not isinstance(content, list):
                continue
            for b in content:
                if isinstance(b, dict) and b.get("type") == "text":
                    text = str(b.get("text") or "")
                    for ln in text.splitlines():
                        block.append({"t": ln[:LINE_CHARS], "c": "body"})
                    break
            break
    if not block and node.prompt:
        for ln in node.prompt.splitlines():
            block.append({"t": ln[:LINE_CHARS], "c": "body"})
    if not block:
        block = [{"t": "No prompt recorded for this node.", "c": "muted"}]
    list_items = []
    if node.prompt:
        list_items.append(S.item(node.prompt, "recipe prompt ref",
                                  acts=[S.btn("Open", S.open_path(node.prompt), "ghost")]))
    return {"block_title": block_title, "block": block,
            "list_title": "Prompt ref", "list": list_items}


def _telemetry_view(run: Run, node: Node) -> dict[str, Any]:
    calls = [c for c in (run.calls or []) if _call_for_node(c, node)]
    if not calls:
        return {"block_title": "LLM calls",
                "block": [{"t": "Deterministic node — no LLM calls.", "c": "muted"}],
                "list_title": "Provenance", "list": []}
    head: list[dict[str, str]] = [{"t": "turn", "c": "muted"}, {"t": "model", "c": "muted"},
                                  {"t": "in", "c": "muted"}, {"t": "out", "c": "muted"},
                                  {"t": "cache", "c": "muted"}, {"t": "cost", "c": "muted"},
                                  {"t": "ms", "c": "muted"}]
    rows: list[list[dict[str, str]]] = [head]
    for i, c in enumerate(calls, 1):
        usage_raw = c.get("usage") if isinstance(c, dict) else None
        usage = usage_raw if isinstance(usage_raw, dict) else {}
        cost_raw = c.get("cost_usd") if isinstance(c, dict) else None
        try:
            cost_val = float(cost_raw or 0)
        except (TypeError, ValueError):
            cost_val = 0.0
        rows.append([
            {"t": str(i), "c": "body"},
            {"t": str(c.get("model_id") or "—") if isinstance(c, dict) else "—", "c": "body"},
            {"t": str(usage.get("input_tokens", "—")), "c": "body"},
            {"t": str(usage.get("output_tokens", "—")), "c": "body"},
            {"t": str(usage.get("cache_read_input_tokens", "—")), "c": "body"},
            {"t": f"${cost_val:.4f}", "c": "body"},
            {"t": str(usage.get("duration_ms", "—")), "c": "body"},
        ])
    list_items = []
    sid = str(run.row.get("trace_id") or "") if isinstance(run.row, dict) else ""
    if sid:
        list_items.append(S.item(f"trace_id: {sid}", ""))
    return {"block_title": "LLM calls", "block": rows,
            "list_title": "Provenance", "list": list_items}


def _call_for_node(call: dict[str, Any], node: Node) -> bool:
    """Match an llm_calls row to a node by actor (role_lane) and time window."""
    actor = str(call.get("actor") or "")
    if actor and actor == node.role_lane:
        return True
    return False


def _learning_view(run: Run, node: Node) -> dict[str, Any]:
    out: list[dict[str, Any]] = []
    if run.row.get("id"):
        try:
            from mini_ork.web.deps import db_for

            home = run.home
            db = db_for(home)
            if db.has_table("gradient_records"):
                now_ms = int(time.time() * 1000)
                for g in db.rows(
                    "SELECT gradient_id, target, signal, suggested_change, confidence "
                    "FROM gradient_records "
                    "WHERE task_class = ? AND created_at > ? "
                    "ORDER BY created_at DESC LIMIT 5",
                    (str(run.row.get("task_class") or ""), now_ms - 24 * 60 * 60 * 1000),
                ):
                    out.append(S.item(
                        f"{g.get('gradient_id')} → {g.get('target') or ''}",
                        str(g.get("signal") or ""),
                        acts=[S.btn("Open", S.open_path(str(run.home / "runs" / run.id)), "ghost")],
                    ))
        except Exception:  # noqa: BLE001
            pass
    if node.gates:
        out.append(S.item("Gates", ", ".join(node.gates)))
    if not out:
        out.append(S.item("No learning signals yet.", ""))
    return {"list_title": "Learning", "list": out}


# ── public builder ──────────────────────────────────────────────────────────

def build_node(home: Path, run_id: str, node_id: str, view: str | None = None,
               offset: int = 0) -> dict[str, Any]:
    """One DAG node's detail panel — entry point for ``board node``."""
    for label, value in (("run_id", run_id), ("node_id", node_id)):
        err = _reject_unsafe(label, value)
        if err is not None:
            return {"ok": False, "error": err}

    view = view or _DEFAULT_VIEW
    if view not in _VIEWS:
        return {"ok": False, "error": f"unknown view {view!r} "
                                       f"(expected one of {', '.join(_VIEWS)})"}

    run_obj = _load(home, run_id)
    if run_obj is None:
        return {"ok": False, "error": f"no run {run_id}"}

    target = _find_node(run_obj, node_id)
    if target is None:
        return {"ok": False, "error": f"no node {node_id} in run {run_id}"}

    run_dir = run_obj.run_dir
    session_path = _resolve_session_path(run_dir, target)
    log_path = _resolve_log_path(run_dir, node_id)
    is_live = _live(target, session_path, log_path)

    duration = _wall(target)
    status, status_c = _stream_status(target, session_path is not None,
                                      log_path is not None)

    base: dict[str, Any] = {
        "ok": True,
        "run": run_id,
        "node": node_id,
        "label": target.id,
        "state": target.state,
        "role": target.role_lane,
        "lane": target.role_lane,
        "family": target.family,
        "stage": str(_stage_for(run_obj, target)),
        "stage_index": _stage_index_for(run_obj, target),
        "stage_count": _stage_count_for(run_obj, target),
        "live": is_live,
        "kv": _kv_items(target, duration),
        "acts": _actions(target, run_id),
        "view": view,
    }

    if view == "stream":
        all_entries = _stream_entries(session_path, log_path, run_id, home)
        entries = all_entries[offset:]
        base.update({
            "entries": entries,
            "offset": offset + len(entries),
            "status": status,
            "status_c": status_c,
            "meta": _meta(target),
            "source": str(session_path.name if session_path else (
                log_path.name if log_path else "")),
            "done_note": _done_note(target),
        })
    elif view == "output":
        base.update(_output_view(run_dir, target))
    elif view == "prompt":
        base.update(_prompt_view(target, session_path))
    elif view == "telemetry":
        base.update(_telemetry_view(run_obj, target))
    elif view == "learning":
        base.update(_learning_view(run_obj, target))
    return base


def _find_node(run: Run, node_id: str) -> Node | None:
    for n in run.nodes:
        if n.id == node_id:
            return n
    return None


def _stage_for(run: Run, node: Node) -> int:
    for i, col in enumerate(run.cols):
        if node.id in col:
            return i
    return -1


def _stage_index_for(run: Run, node: Node) -> int:
    for col in run.cols:
        if node.id in col:
            return col.index(node.id)
    return 0


def _stage_count_for(run: Run, node: Node) -> int:
    for col in run.cols:
        if node.id in col:
            return len(col)
    return 1


def _done_note(node: Node) -> str:
    if node.state == "running":
        return ""
    if node.state == "pending":
        return ("This node has not started. Its stream opens when it is dispatched.")
    return "Agent finished — steering only reaches running nodes."