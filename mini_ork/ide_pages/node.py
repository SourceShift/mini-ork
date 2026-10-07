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
import time
from pathlib import Path
from typing import Any

from mini_ork.ide_pages import spec as S
from mini_ork.ide_pages.run import Run, Node, _epoch, _load, _wall

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

# Map a node's ``type`` (workflow role) onto an ``operator_steering`` role.
# ``_fetch_steer_rows`` keys on this map so the IDE stream shows only rows
# targeted at the node's role or ``any``. ``_VALID_ROLES`` lives in
# ``mini_ork.steering.operator_steering:40`` — every value here must be in
# that set.
_NODE_ROLE_MAP: dict[str, str] = {
    "planner": "planner",
    "decomposer": "planner",
    "implementer": "implementer",
    "worker": "implementer",
    "reviewer": "reviewer",
    "synthesizer": "reviewer",
    "verifier": "verifier",
    "static_check": "verifier",
    "test": "verifier",
}


def _role_for_node(node: Node) -> str:
    return _NODE_ROLE_MAP.get(str(node.type or ""), "any")

# Edit-family tools whose input carries an old/new body the IDE renders as a
# coloured diff. Listed verbatim per the kickoff.
_EDIT_TOOLS = frozenset({"Edit", "MultiEdit", "apply_diff"})


def _reject_unsafe(name: str, value: str) -> str | None:
    """Path-traversal guard. Mirrors ``web/routes/node_live._live_path``."""
    if not value or ".." in value or "/" in value or "\\" in value:
        return f"invalid {name}"
    return None


def _resolve_session_path(run: "Run", node: Node) -> Path | None:
    """Three-rule node→session resolver (kickoff r2 fixes #2).

    1. ``session_id`` in the ``agent-<node>.live.jsonl`` cost-state/result
       envelope — most authoritative when the live file exists.
    2. ``llm_calls.session_id`` joined on this run + the node's lane/actor
       AND the call's timestamp inside the node's start/end window. When
       multiple sessions map, prefer the latest. Direct query against
       ``llm_calls`` (mirrors ``_fetch_steer_rows``) — the shared
       ``repositories._llm_calls_select`` deliberately does not return
       ``session_id``, and widening it would touch every LLM reader in the
       repo.
    3. **Only for running nodes.** Newest ``sessions/*.jsonl`` whose mtime
       is after ``node.start - 5 s`` AND whose first user prompt contains
       the node id/name or the prompt template text. A finished shell/
       verifier node with no mapped session falls through to
       ``_resolve_log_path`` — never borrow another node's transcript.
    """
    run_dir = run.run_dir

    # Rule 1 — live.jsonl cost-state/result envelope (unchanged).
    live_path = run_dir / f"agent-{node.id}.live.jsonl"
    if live_path.is_file():
        try:
            tail = live_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            tail = ""
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

    # Rule 2 — llm_calls.session_id for this run + actor + time window.
    candidate = _resolve_via_llm_calls(run, node)
    if candidate is not None:
        return candidate

    # Rule 3 — only for RUNNING nodes. Finished nodes fall through to logs.
    if node.state == "running" and node.start is not None:
        needle = _prompt_needle(run, node)
        if needle:
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
                        if cand.stat().st_mtime < node.start - 5:
                            continue
                    except OSError:
                        continue
                    first = _first_user_text(cand)
                    if first and any(n and n in first for n in needle):
                        return cand
    return None


def _resolve_via_llm_calls(run: "Run", node: Node) -> Path | None:
    """Rule 2 of the resolver: pick the latest ``llm_calls.session_id`` whose
    actor matches ``node.role_lane`` and whose timestamp is inside the
    node's ``[start - 2, end + 5]`` window (matches ``run._attribute_calls``).

    r3 fix #1: ``llm_calls.ts`` is the ISO column (not ``ts_ms`` — that column
    does not exist). The window is in epoch SECONDS, so we project ``ts`` and
    filter in Python via ``_epoch`` (mirrors ``run._attribute_calls``).

    Done as a direct ``db_for(home).rows(...)`` because the shared
    ``repositories._llm_calls_select`` does not project ``session_id`` and
    widening it would touch every LLM reader in the repo (lens §4.1).
    """
    if not (run.home / "state.db").exists():
        return None
    try:
        from mini_ork.web.deps import db_for
    except Exception:  # noqa: BLE001
        return None
    try:
        db = db_for(run.home)
    except Exception:  # noqa: BLE001
        return None
    if not db.has_table("llm_calls"):
        return None
    actor = str(node.role_lane or "")
    if not actor:
        return None
    start_s = int(node.start) - 2 if node.start is not None else None
    end_s = int(node.end) + 5 if node.end is not None else None
    sql = ("SELECT session_id, ts FROM llm_calls "
           "WHERE run_id = ? AND actor = ? "
           "  AND session_id IS NOT NULL AND session_id != '' "
           "ORDER BY ts DESC LIMIT 50")
    try:
        rows = db.rows(sql, (run.id, actor))
    except Exception:  # noqa: BLE001
        return None
    seen: set[str] = set()
    for r in rows:
        ts_s = _epoch(r.get("ts"))
        if ts_s is None:
            continue
        if start_s is not None and ts_s < start_s:
            continue
        if end_s is not None and ts_s > end_s:
            continue
        sid = str(r.get("session_id") or "")
        if not sid or sid in seen:
            continue
        seen.add(sid)
        cand = run.run_dir / "sessions" / f"{sid}.jsonl"
        if cand.is_file():
            return cand
    return None


def _prompt_needle(run: "Run", node: Node) -> tuple[str, ...]:
    """Substrings that mark a session as the node's transcript (rule 3).

    Returns ``(node.id, node.prompt_template_text)`` — the kickoff's "the
    session's first prompt contains the node's id/name or its prompt
    template text". Empty when neither is known, in which case rule 3 falls
    through to the log resolver (safe default).
    """
    out: list[str] = []
    if node.id:
        out.append(node.id)
    template = _prompt_template_text(run, node)
    if template:
        out.append(template)
    return tuple(out)


def _prompt_template_text(run: "Run", node: Node) -> str:
    """Read the prompt template the recipe dispatched for this node.

    ``node.prompt`` is a "<recipe_dir.name>/<prompt_ref>" string (set in
    ``run._load``). The recipe dir is on the loaded Run, so we can open
    the actual prompt file. Truncated to 200 chars so the substring check
    stays bounded.
    """
    if not node.prompt or run.recipe_dir is None:
        return ""
    # node.prompt looks like "framework-edit/prompts/implementer.md"
    parts = node.prompt.split("/", 1)
    fname = parts[1] if len(parts) == 2 else parts[0]
    path = run.recipe_dir / fname
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    return text[:200]


def _first_user_text(session_path: Path) -> str:
    """First user message in a session transcript — concatenated plain text.

    Used by resolver rule 3 to check the substring match against the node
    id and prompt template. Robust to ``content`` being ``str`` or ``list``.
    """
    try:
        text = session_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    for raw in text.splitlines():
        raw = raw.strip()
        if not raw:
            continue
        try:
            obj = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(obj, dict) or str(obj.get("type") or "") != "user":
            continue
        msg = obj.get("message") if isinstance(obj.get("message"), dict) else {}
        content = msg.get("content") if isinstance(msg, dict) else None
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts: list[str] = []
            for b in content:
                if isinstance(b, dict) and b.get("type") == "text":
                    parts.append(str(b.get("text") or ""))
            return "\n".join(parts)
    return ""


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

    Checks ``mtime`` only — see ``feedback_opencode_build_hangs_on_full_disk``
    neighbours for the context that originally motivated the window.
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


def _stream_status(node: Node, has_session: bool, has_log: bool,
                   is_live: bool, event_count: int) -> tuple[str, str]:
    """Status pill text + colour. Kickoff fix #7.

    * Running + source attached + grew in the last 120 s → ``attached · live``.
    * Running + source attached but stale → ``attached · idle`` (yellow).
    * Running + no source yet → ``not dispatched yet``.
    * Finished + any source → ``finished · N events`` so the operator sees
      the entry count without scrolling the list.
    * No source at all → ``no transcript``.
    """
    if node.state == "running":
        if has_session or has_log:
            return (("attached · live", "green") if is_live
                    else ("attached · idle", "yellow"))
        return ("not dispatched yet", "sub")
    if has_session or has_log:
        return (f"finished · {event_count} events", "sub")
    return ("no transcript", "yellow")


def _meta(node: Node, run: "Run") -> str:
    """``<lane> · <tokens> tokens · $<cost>`` per kickoff fix #7.

    Tokens come from the llm_calls attributed to this node (input +
    output + cached); falls back to the live.jsonl cost-state envelope.
    A ``—`` token placeholder is the kickoff's "tokens is unavailable".
    """
    parts: list[str] = []
    lane = node.role_lane or node.family or "—"
    parts.append(lane)
    tokens = _node_tokens(node, run)
    if tokens is not None:
        parts.append(f"{tokens:,} tokens")
    if node.cost:
        parts.append(f"${node.cost:.2f}")
    return " · ".join(parts) if parts else "—"


def _kv_items(node: Node, duration: str, run: "Run") -> list[dict[str, str]]:
    tokens_v = "—"
    tokens = _node_tokens(node, run)
    if tokens is not None:
        tokens_v = f"{tokens:,}"
    return [
        {"k": "Lane", "v": node.role_lane or "—", "c": "text"},
        {"k": "Provider", "v": node.family or "—", "c": "text"},
        {"k": "Cost", "v": S.money(node.cost) + (f" · {node.calls} call"
                                                  f"{'s' if node.calls != 1 else ''}"
                                                  if node.calls else ""), "c": "text"},
        {"k": "Duration", "v": duration, "c": "text"},
        {"k": "Tokens", "v": tokens_v, "c": "text" if tokens is not None else "muted"},
        {"k": "Gates", "v": ", ".join(node.gates) or "—", "c": "text"},
    ]


def _node_tokens(node: Node, run: "Run") -> int | None:
    """Sum input + output tokens for the node's attributed llm_calls.

    r3 fix #7 minor: ``cached_input_tokens`` is NOT summed — the kickoff
    renamed the operator-facing number to "input + output" so the cost
    badge doesn't double-count cache hits.

    Reads ``run.calls`` (the rows already attributed by ``_attribute_calls``
    to this exact node). When attribution yielded zero rows — common for
    deterministic shell/verifier nodes — falls back to the live.jsonl
    cost-state envelope. ``None`` when neither source has token numbers.
    """
    total = 0
    seen = False
    for c in run.calls or []:
        if not isinstance(c, dict):
            continue
        if not _call_for_node(c, node):
            continue
        for key in ("input_tokens", "output_tokens"):
            v = c.get(key)
            try:
                if v is not None:
                    total += int(v)
                    seen = True
            except (TypeError, ValueError):
                continue
    if seen:
        return total
    state = _fetch_cost_state(run.run_dir, node.id)
    if not isinstance(state, dict):
        return None
    for key in ("total_tokens", "input_tokens", "output_tokens", "tokens"):
        v = state.get(key)
        try:
            if v is not None:
                return int(v)
        except (TypeError, ValueError):
            continue
    return None


def _actions(node: Node, run: "Run") -> list[dict[str, Any]]:
    if node.state == "running":
        return [
            S.btn("Stop run",
                  S.cli("board", "stop", run.id,
                        confirm="Stop this run after its current node?"),
                  "warn"),
            S.btn("Kill",
                  S.cli("board", "kill", run.id,
                        confirm=f"Kill {run.id}? SIGTERM, then SIGKILL after 2 s."),
                  "danger"),
        ]
    # Kickoff fix #4: reveal path comes from the loaded Run's run_dir,
    # which already honours --home. Building a path from env/cwd here
    # ignores --home (CLAUDE.md: home is the path contract).
    return [S.btn("Open run folder", S.reveal(str(run.run_dir)), "ghost")]


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
                    run_id: str, home: Path, *,
                    target: Node, offset: int) -> tuple[list[dict[str, Any]], int]:
    """Build the stream view's ``entries`` list (kickoff fix #3).

    Returns ``(entries, next_offset)`` where ``next_offset`` is the number
    of transcript *lines* consumed (NOT an index into the merged list).
    Steering rows merge by timestamp, are filtered to the node's role
    (or ``any``) and this run (no ``run_id IS NULL`` rows), and are
    skipped when their timestamp is not strictly newer than the line
    at offset N — that is the "no repeated steer row" property.

    r3 fix #3: ordering is by ``_ts`` (epoch SECONDS) from each entry's
    own ``timestamp`` (transcript) or ``created_at // 1000`` (steer).
    Lines without a timestamp fall back to ``node.start + line_idx`` —
    the line index still drives the append-only contract.
    """
    raw: list[dict[str, Any]] = []
    if session_path is not None:
        raw.extend(_session_entries(session_path))
    elif log_path is not None:
        raw.extend(_log_path_entries(log_path))
    raw.extend(_steer_entries(run_id, home, target))

    node_start_s = int(target.start or 0)
    # Resolve ``_ts`` for every entry: prefer the per-line ISO timestamp,
    # else fall back to ``node.start + line_idx`` (seconds). Steer rows
    # already carry ``_ts`` in seconds (set in ``_steer_entries``).
    for e in raw:
        ts_val = e.get("_ts")
        if ts_val is None:
            line_idx = int(e.get("_line") or 0)
            e["_ts"] = node_start_s + line_idx
    raw.sort(key=lambda e: (int(e.get("_ts") or 0), 0 if e.get("_src") == "steer" else 1))

    # r3 fix #3: compute the timestamp of the LAST transcript line this
    # poll would consume. Steers live in the same timeline and are emitted
    # only when (a) the poll consumed at least one transcript line, AND
    # (b) the steer's timestamp is strictly newer than line N-1, AND
    # (c) the steer's timestamp is NOT later than the last line consumed.
    # "Before node.start" steers are emitted on a full read because
    # line N-1 has no timestamp (lower bound collapses to -inf).
    consumed_line_ts: list[int] = []
    for e in raw:
        if e.get("_src") == "steer":
            continue
        line_idx = int(e.get("_line") or 0)
        if line_idx < int(offset):
            continue
        consumed_line_ts.append(int(e.get("_ts") or 0))
    has_lines = bool(consumed_line_ts)
    max_line_ts = max(consumed_line_ts) if has_lines else None
    if int(offset) > 0:
        prev_line_idx = int(offset) - 1
        prev_line_ts = node_start_s + prev_line_idx
    else:
        # Full read — no "line N-1". Lower bound collapses so steers before
        # ``node.start`` appear at the top of the stream (kickoff rule).
        prev_line_ts = None

    out: list[dict[str, Any]] = []
    for e in raw:
        is_steer = e.get("_src") == "steer"
        if is_steer:
            steer_ts = int(e.get("_ts") or 0)
            # No new lines consumed → nothing to bound the steer against.
            if not has_lines:
                continue
            # "Newer than the line at N-1" — strict so we never repeat.
            if prev_line_ts is not None and steer_ts <= prev_line_ts:
                continue
            # "Not later than the last line consumed" — drops steers that
            # were added by the operator after the transcript froze.
            if max_line_ts is not None and steer_ts > max_line_ts:
                continue
        else:
            # ``offset`` = number of transcript lines already consumed.
            # Return entries whose source line is at-or-after that point.
            line_idx = int(e.get("_line") or 0)
            if line_idx < int(offset):
                continue
        out.append(e)

    next_offset = int(offset)
    for e in out:
        if e.get("_src") == "steer":
            continue
        line_idx = int(e.get("_line") or 0)
        if line_idx + 1 > next_offset:
            next_offset = line_idx + 1

    # Strip the private keys the IDE doesn't need to render.
    for e in out:
        for k in ("_src", "_line", "_ts", "_ts_skip"):
            e.pop(k, None)
    return out, next_offset


def _session_entries(session_path: Path) -> list[dict[str, Any]]:
    """Transcript → IDE entries.

    Each yielded entry carries ``_src="tx"`` and ``_line=<index>`` so
    ``_stream_entries`` can compute the append-only offset.

    r3 fix #3: every entry also carries ``_ts`` (epoch seconds) parsed from
    the line's own ``timestamp`` field (ISO). ``_stream_entries`` sorts and
    thresholds on ``_ts`` instead of a fabricated ``node_start + idx*1000``.
    """
    out: list[dict[str, Any]] = []
    try:
        text = session_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return out
    lines = text.splitlines()
    first_user_done = False
    pending: dict[str, dict[str, Any]] = {}  # tool_use_id → tool info

    def _with_ts(base: dict[str, Any], raw: dict[str, Any], line_idx: int) -> dict[str, Any]:
        ts_s = _epoch(raw.get("timestamp"))
        if ts_s is not None:
            base["_ts"] = ts_s
        else:
            # Fallback when the line lacks ``timestamp``: use ``line_idx``
            # as a stable secondary key. ``_stream_entries`` resolves the
            # final value when it knows the node's start.
            base.setdefault("_ts_skip", line_idx)
        return base

    for line_idx, raw in enumerate(lines):
        line = raw.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(entry, dict):
            continue
        typ = str(entry.get("type") or "")
        message = entry.get("message") if isinstance(entry.get("message"), dict) else {}
        content = message.get("content") if isinstance(message, dict) else None

        if typ == "user":
            # Kickoff fix #1: real transcripts store the first user
            # message's ``content`` as a plain ``str``. The first user
            # entry IS the prompt; a ``tool_result`` envelope must never
            # be taken as the prompt.
            if not first_user_done:
                first_user_done = True
                text = _extract_user_text(content)
                if text:
                    out.append(_with_ts({
                        "k": _KIND_USER,
                        "head": "",
                        "arg": text[:USER_HEAD_CAP],
                        "lines": [{"t": text[:USER_HEAD_CAP], "c": "body"}],
                        "_src": "tx",
                        "_line": line_idx,
                    }, entry, line_idx))
                continue
            # Subsequent user envelopes carry tool_results (never strings).
            if not isinstance(content, list):
                continue
            for b in content:
                if not isinstance(b, dict) or b.get("type") != "tool_result":
                    continue
                tool_use_id = str(b.get("tool_use_id") or "")
                tool_info = pending.pop(tool_use_id, None)
                if tool_info is None:
                    continue
                result_text, is_error = _result_text(b)
                lines_list: list[dict[str, str]] = []
                if _is_edit_tool(tool_info["name"]):
                    lines_list.extend(_edit_diff_lines(tool_info["name"], tool_info["input"]))
                res_lines = result_text.splitlines()
                shown = res_lines[:TOOL_RESULT_LINES]
                for ln in shown:
                    lines_list.append({"t": ln[:LINE_CHARS], "c": "red" if is_error else "body"})
                if len(res_lines) > TOOL_RESULT_LINES:
                    lines_list.append({"t": f"… {len(res_lines) - TOOL_RESULT_LINES} more lines",
                                       "c": "muted"})
                out.append(_with_ts({
                    "k": _KIND_TOOL,
                    "head": tool_info["name"],
                    "arg": tool_info["summary"],
                    "lines": lines_list or [{"t": "(no output)", "c": "muted"}],
                    "_src": "tx",
                    "_line": line_idx,
                }, entry, line_idx))
        elif typ == "assistant":
            if not isinstance(content, list):
                continue
            for b in content:
                if not isinstance(b, dict):
                    continue
                bt = str(b.get("type") or "")
                if bt == "thinking":
                    text = _text_block(b, "thinking")[:THINK_HEAD_CAP]
                    out.append(_with_ts({
                        "k": _KIND_THINK,
                        "head": "Thinking… ",
                        "arg": text,
                        "lines": [{"t": text, "c": "muted"}],
                        "_src": "tx",
                        "_line": line_idx,
                    }, entry, line_idx))
                elif bt == "text":
                    text = _text_block(b, "text")[:TEXT_CAP]
                    out.append(_with_ts({
                        "k": _KIND_TEXT,
                        "head": "",
                        "arg": text,
                        "lines": [{"t": text, "c": "body"}],
                        "_src": "tx",
                        "_line": line_idx,
                    }, entry, line_idx))
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
                        out.append(_with_ts({
                            "k": _KIND_TODO,
                            "head": "",
                            "arg": "",
                            "lines": todo_lines,
                            "_src": "tx",
                            "_line": line_idx,
                        }, entry, line_idx))
        elif typ == "result":
            out.append(_with_ts({
                "k": _KIND_NOTE,
                "head": "",
                "arg": _format_result_note(entry),
                "lines": [{"t": str(entry.get("result") or "")[:TEXT_CAP],
                           "c": "muted"}],
                "_src": "tx",
                "_line": line_idx,
            }, entry, line_idx))
    return out


def _extract_user_text(content: Any) -> str:
    """Render the first user message's text — accepts ``str`` or ``list``."""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    for b in content:
        if isinstance(b, dict) and b.get("type") == "text":
            return str(b.get("text") or "")
    return ""


def _format_result_note(entry: dict[str, Any]) -> str:
    """``Done · $X · N turns`` text from a transcript ``result`` envelope."""
    cost = entry.get("total_cost_usd") or entry.get("usage", {}).get("total_cost_usd")
    turns = entry.get("num_turns")
    parts: list[str] = []
    if cost is not None:
        try:
            parts.append(f"${float(cost):.2f}")
        except (TypeError, ValueError):
            pass
    if turns is not None:
        parts.append(f"{turns} turns")
    return "Done · " + " · ".join(parts) if parts else "Done"


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
    """Log-file entries for shell/verifier nodes.

    r3 fix #4: one entry per log line so ``offset`` counts log lines and
    each new line surfaces as its own ``text`` entry. ``_src="log"`` and
    ``_line=<index>`` keep the offset contract identical to transcript
    entries.
    """
    try:
        text = log_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        text = ""
    raw_lines = text.splitlines()[-SHELL_LOG_LINES:]
    out: list[dict[str, Any]] = []
    for idx, raw in enumerate(raw_lines):
        c = "red" if "Traceback" in raw or "ERROR" in raw else (
            "green" if "PASS" in raw or "ok" in raw.lower() else "body")
        out.append({
            "k": _KIND_TEXT,
            "head": "",
            "arg": raw[:LINE_CHARS],
            "lines": [{"t": raw[:LINE_CHARS], "c": c}],
            "_src": "log",
            "_line": idx,
        })
    return out


def _steer_entries(run_id: str, home: Path, target: Node) -> list[dict[str, Any]]:
    """Read operator_steering rows for this run + node role DIRECTLY.

    r3 fix #5: filter on the node's MAPPED ROLE (``_role_for_node``),
    not on the node's id. ``operator_steering.role_target`` keys on the
    five roles defined in ``operator_steering._VALID_ROLES`` (planner /
    implementer / reviewer / verifier / any) — the legacy comment claiming
    the CHECK keys on node ids was wrong.

    ``operator_steering.fetch_for`` marks rows consumed in an UPDATE; using
    it here would consume rows the dispatcher's context_assembler still needs
    (the consume-on-read is shared state with ``cli/execute.py``). The IDE
    stream view is read-only display, so a direct SELECT is correct.

    r3 fix #3: ``created_at`` is epoch milliseconds, so we divide by 1000
    to share the SECONDS timeline with transcript ``timestamp`` rows.
    """
    role = _role_for_node(target)
    rows = _fetch_steer_rows(run_id, home, role=role)
    out: list[dict[str, Any]] = []
    for r in rows:
        sev = str(r.get("severity") or "info")
        msg = str(r.get("message") or "")
        c = "red" if sev == "critical" else ("yellow" if sev == "warn" else "blue")
        ts_ms = int(r.get("created_at") or 0)
        out.append({
            "k": _KIND_STEER,
            "head": "You · steering ",
            "arg": f"[{sev}] {msg}",
            "lines": [{"t": f"[{sev}] {msg}", "c": c}],
            "_src": "steer",
            "_line": 0,
            "_ts": ts_ms // 1000,  # ms → seconds to share the timeline
        })
    return out


def _fetch_steer_rows(run_id: str, home: Path, role: str = "") -> list[dict[str, Any]]:
    """Read operator_steering rows for this run + role.

    r3 fix #5: ``role`` is one of the five strings in
    ``operator_steering._VALID_ROLES`` (``planner`` / ``implementer`` /
    ``reviewer`` / ``verifier`` / ``any``) — derived from the node's
    ``type`` via ``_role_for_node``. Rows whose ``role_target`` matches
    ``role`` (or ``any`` / blank for legacy) are returned.
    """
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
    role_filter = ""
    params: tuple[Any, ...] = (run_id, int(time.time() * 1000))
    if role:
        role_filter = "  AND (role_target = ? OR role_target = 'any' OR role_target = '') "
        params = (run_id, role, int(time.time() * 1000))
    try:
        return db.rows(
            "SELECT run_id, role_target, severity, message, source, "
            "       confidence, created_at "
            "FROM operator_steering "
            "WHERE (run_id = ? OR run_id = '') "
            + role_filter +
            "  AND (expires_at IS NULL OR expires_at > ?) "
            "ORDER BY created_at",
            params,
        )
    except Exception:  # noqa: BLE001
        return []


def _fetch_cost_state(run_dir: Path, node_id: str) -> dict[str, Any] | None:
    """Last cost-state / result envelope in ``agent-<node>.live.jsonl``.

    The fixture (and real Claude Code transcripts) writes a single envelope
    shaped ``{"session_id":..., "total_cost_usd":X, "num_turns":N}`` at
    agent-finish time — that is the "cost-state" the kickoff fix #5 names.
    Returns the most recent envelope that looks cost-like, or ``None``.
    """
    path = run_dir / f"agent-{node_id}.live.jsonl"
    if not path.is_file():
        return None
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    last: dict[str, Any] | None = None
    for raw in text.splitlines():
        raw = raw.strip()
        if not raw:
            continue
        try:
            rec = json.loads(raw)
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
        if ("total_cost_usd" in env or "num_turns" in env or "totalCostUSD" in env
                or "stop_reason" in env or env.get("type") == "result"):
            last = env
    return last


def _transcript_has_result(session_path: Path | None) -> bool:
    """Did the session file itself carry a ``result`` entry?

    Used to decide whether the cost-state fallback note is needed
    (kickoff fix #5): when the transcript has its own ``result`` the
    session parser already produced a ``Done · $X · N turns`` note and
    the live.jsonl envelope would just duplicate it.
    """
    if session_path is None or not session_path.is_file():
        return False
    try:
        text = session_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    for raw in text.splitlines():
        raw = raw.strip()
        if not raw:
            continue
        try:
            obj = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(obj, dict) and str(obj.get("type") or "") == "result":
            return True
    return False


def _build_note_from_cost_state(run_dir: Path, node: Node) -> str | None:
    """``Done · $X · N turns · Ys`` string from live.jsonl cost-state.

    Used when the transcript itself has no ``result`` entry — the common
    state in production Claude Code transcripts (kickoff fix #5).
    """
    env = _fetch_cost_state(run_dir, node.id)
    if not isinstance(env, dict):
        return None
    parts: list[str] = []
    for key in ("total_cost_usd", "totalCostUSD"):
        v = env.get(key)
        if v is None:
            continue
        try:
            parts.append(f"${float(v):.2f}")
            break
        except (TypeError, ValueError):
            continue
    turns = env.get("num_turns")
    if turns is not None:
        try:
            parts.append(f"{int(turns)} turns")
        except (TypeError, ValueError):
            pass
    if node.start is not None:
        end = int(node.end or int(time.time()))
        dur = max(0, end - int(node.start))
        parts.append(f"{dur}s")
    if not parts:
        return None
    return "Done · " + " · ".join(parts)


# ── output / prompt / telemetry / learning views ────────────────────────────

def _output_view(run_dir: Path, node: Node) -> dict[str, Any]:
    """Final text + report files the node wrote.

    Report lookup chain (kickoff fix #6):

    * ``lens-<id-without-_lens-suffix>.md``  — e.g. ``invariants_lens``
      → ``lens-invariants.md``
    * ``lens-<id>.md``
    * ``<id-without-_lens-suffix>.md``
    * ``<id>.md``
    * ``review-<id>.json``
    * ``verifier_<id>.json``
    """
    block: list[dict[str, str]] = []
    block_title = "Node output"
    for path in _report_paths(run_dir, node.id):
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


def _report_paths(run_dir: Path, node_id: str) -> list[Path]:
    """Build the ordered report-file lookup chain for ``_output_view``."""
    stripped = node_id[:-5] if node_id.endswith("_lens") else node_id
    seen: set[str] = set()
    out: list[Path] = []
    for name in (f"lens-{stripped}.md",
                 f"lens-{node_id}.md",
                 f"{stripped}.md",
                 f"{node_id}.md",
                 f"review-{node_id}.json",
                 f"verifier_{node_id}.json"):
        path = run_dir / name
        key = str(path)
        if key in seen:
            continue
        seen.add(key)
        out.append(path)
    return out


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
            # Kickoff fix #1: ``str`` is the canonical prompt shape in real
            # transcripts (e.g. Claude Code stores the first user message
            # as ``{"type":"user","message":{"content":"<prompt>"}}``).
            # A ``tool_result`` envelope is never a prompt.
            if isinstance(content, str):
                text = content
                for ln in text.splitlines():
                    block.append({"t": ln[:LINE_CHARS], "c": "body"})
                break
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
    """Per-call table + provenance list (kickoff fix #7).

    Telemetry rows from ``llm_calls`` filtered on ``actor AND time window
    inside the node's start/end``. Each row reads ``input_tokens``,
    ``output_tokens``, ``cached_input_tokens``, ``cost_usd``,
    ``duration_ms`` from the flat columns ``repositories._llm_calls_select``
    returns (the old ``c.get("usage")`` shape was a bug — the repository
    never returned a nested ``usage`` dict; the IDE printed ``—``).
    """
    direct_calls = _llm_calls_for_node(run, node)
    if direct_calls:
        calls = direct_calls
    else:
        calls = [c for c in (run.calls or []) if _call_for_node(c, node)]
    if not calls:
        return {"block_title": "LLM calls",
                "block": [{"t": "Deterministic node — no LLM calls.", "c": "muted"}],
                "list_title": "Provenance", "list": []}
    head: list[dict[str, str]] = [{"t": "turn", "c": "muted"}, {"t": "model", "c": "muted"},
                                  {"t": "in", "c": "muted"}, {"t": "out", "c": "muted"},
                                  {"t": "cache", "c": "muted"}, {"t": "cost", "c": "muted"},
                                  {"t": "ms", "c": "muted"}, {"t": "session", "c": "muted"}]
    rows: list[list[dict[str, str]]] = [head]
    for i, c in enumerate(calls, 1):
        try:
            cost_val = float(c.get("cost_usd") or 0)
        except (TypeError, ValueError):
            cost_val = 0.0
        rows.append([
            {"t": str(i), "c": "body"},
            {"t": str(c.get("model_id") or "—"), "c": "body"},
            {"t": str(c.get("input_tokens") if c.get("input_tokens") is not None else "—"),
             "c": "body"},
            {"t": str(c.get("output_tokens") if c.get("output_tokens") is not None else "—"),
             "c": "body"},
            {"t": str(c.get("cached_input_tokens") if c.get("cached_input_tokens") is not None else "—"),
             "c": "body"},
            {"t": f"${cost_val:.4f}", "c": "body"},
            {"t": str(c.get("duration_ms") if c.get("duration_ms") is not None else "—"),
             "c": "body"},
            {"t": str(c.get("session_id") or "—")[:14], "c": "muted"},
        ])
    list_items: list[dict[str, Any]] = []
    for sid in sorted({str(c.get("session_id") or "")
                       for c in calls if c.get("session_id")}):
        list_items.append(S.item(f"session_id: {sid[:14]}", ""))
    trace = str(run.row.get("trace_id") or "") if isinstance(run.row, dict) else ""
    if trace:
        list_items.append(S.item(f"trace_id: {trace}", ""))
    return {"block_title": "LLM calls", "block": rows,
            "list_title": "Provenance", "list": list_items}


def _llm_calls_for_node(run: Run, node: Node) -> list[dict[str, Any]]:
    """Direct llm_calls SELECT with the ``session_id`` column the shared
    repository reader omits. Same time window as ``run._attribute_calls``:
    ``start-2 <= ts <= end+5`` — but ``ts`` is the ISO column, so we filter
    in Python via ``_epoch`` (mirrors ``_call_for_node``).

    Returns an empty list on any DB error so the telemetry view can fall back
    to ``run.calls`` (which still has the cost/badge data even without
    session ids).
    """
    if not (run.home / "state.db").exists():
        return []
    try:
        from mini_ork.web.deps import db_for
    except Exception:  # noqa: BLE001
        return []
    try:
        db = db_for(run.home)
    except Exception:  # noqa: BLE001
        return []
    if not db.has_table("llm_calls"):
        return []
    actor = str(node.role_lane or "")
    if not actor:
        return []
    start_s = int(node.start) - 2 if node.start is not None else None
    end_s = int(node.end) + 5 if node.end is not None else None
    sql = ("SELECT actor, model_id, input_tokens, output_tokens, cached_input_tokens, "
           "       cost_usd, duration_ms, session_id, ts "
           "FROM llm_calls WHERE run_id = ? AND actor = ? ORDER BY ts ASC")
    try:
        rows = db.rows(sql, (run.id, actor))
    except Exception:  # noqa: BLE001
        return []
    out: list[dict[str, Any]] = []
    for r in rows:
        ts_s = _epoch(r.get("ts"))
        if ts_s is None:
            continue
        if start_s is not None and ts_s < start_s:
            continue
        if end_s is not None and ts_s > end_s:
            continue
        out.append(r)
    return out


def _call_for_node(call: dict[str, Any], node: Node) -> bool:
    """Match an llm_calls row to a node by actor AND time window.

    r3 fix #1: ``ts`` is the ISO column; ``_epoch`` converts it to epoch
    seconds and the window is ``start-2 ≤ ts ≤ end+5`` in seconds
    (mirrors ``run._attribute_calls``).
    """
    actor = str(call.get("actor") or "")
    if actor != node.role_lane:
        return False
    if node.start is None and node.end is None:
        return True
    ts_s = _epoch(call.get("ts"))
    if ts_s is None:
        return False
    start_s = int(node.start) - 2 if node.start is not None else None
    end_s = int(node.end) + 5 if node.end is not None else None
    if start_s is not None and ts_s < start_s:
        return False
    if end_s is not None and ts_s > end_s:
        return False
    return True


def _learning_view(run: Run, node: Node) -> dict[str, Any]:
    """Kickoff fix #7: context-pack gradients + role-targeted steering rows
    + run-window gradients. The same three sources the operator's recipe
    workflow reads for the planner (cf. ``mini_ork.context.assemble``).
    """
    out: list[dict[str, Any]] = []

    # 1. Injected gradients from the run's context-pack.
    pack_path = run.run_dir / "context-pack.json"
    if pack_path.is_file():
        try:
            pack = json.loads(pack_path.read_text(encoding="utf-8", errors="replace"))
        except (json.JSONDecodeError, OSError):
            pack = {}
        for key, label in (("prior_similar_runs", "Prior similar runs"),
                           ("known_failure_modes", "Known failure modes"),
                           ("verified_emergent_patterns", "Verified patterns"),
                           ("similar_lessons", "Similar lessons"),
                           ("constraints", "Constraints")):
            value = pack.get(key)
            if not isinstance(value, list) or not value:
                continue
            first = next((v for v in value if isinstance(v, dict)), None)
            cite = str((first or {}).get("cite") or "")
            out.append(S.item(f"{label} · {len(value)}",
                              cite or "from the context pack", m="✦", mc="purple"))

    # 2. Steering rows for this node's role (read directly so we don't
    # consume rows the dispatcher still needs — same discipline as
    # ``_fetch_steer_rows``). r3 fix #5: filter on the mapped role.
    role = _role_for_node(node)
    steer_rows = _fetch_steer_rows(run.id, run.home, role=role)
    for r in steer_rows[:5]:
        sev = str(r.get("severity") or "info")
        msg = str(r.get("message") or "")
        if not msg:
            continue
        out.append(S.item(f"Steering · [{sev}]", msg, m="→", mc="blue"))

    # 3. Gradients produced in the run's window (same rule as
    # ``run._learnings_tab``). r3 fix #6: ``gradient_records.created_at``
    # is epoch SECONDS — the previous ``* 1000`` + ``+ 60_000`` ms window
    # silently excluded every row. Match the run.py predicate one-for-one.
    if run.row.get("id"):
        try:
            from mini_ork.web.deps import db_for

            home = run.home
            db = db_for(home)
            if db.has_table("gradient_records"):
                start_s = int(node.start or 0)
                end_s = (int(node.end) if node.end is not None
                         else int(time.time()))
                task_class = str(run.row.get("task_class") or "")
                where = ["created_at BETWEEN ? AND ?",
                         "(task_class = ? OR ? = '')"]
                params: list[Any] = [start_s, end_s + 60, task_class, task_class]
                for g in db.rows(
                    "SELECT gradient_id, target, signal, suggested_change, confidence "
                    "FROM gradient_records WHERE " + " AND ".join(where) +
                    " ORDER BY created_at DESC LIMIT 5",
                    tuple(params),
                ):
                    out.append(S.item(
                        f"{g.get('gradient_id')} → {g.get('target') or ''}",
                        str(g.get('signal') or ''),
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
    session_path = _resolve_session_path(run_obj, target)
    log_path = _resolve_log_path(run_dir, node_id)
    is_live = _live(target, session_path, log_path)

    duration = _wall(target)

    # Stream entries must be built first so the status pill carries the
    # entry count and we can stitch a cost-state note when the transcript
    # has no ``result`` (kickoff fix #5).
    stream_entries: list[dict[str, Any]] = []
    next_offset = int(offset)
    transcript_has_result = _transcript_has_result(session_path) if session_path else False
    transcript_entry_count = 0  # total transcript entries (full file, not poll slice)
    if view == "stream":
        # r3 fix #7 minor: ``finished · N events`` reports the TOTAL
        # transcript entry count, not this poll's slice. Parse the source
        # once for the count, then again inside ``_stream_entries`` for
        # the per-poll slice — both reads are O(file size).
        if session_path is not None:
            transcript_entry_count = len(_session_entries(session_path))
        elif log_path is not None:
            transcript_entry_count = len(_log_path_entries(log_path))

        stream_entries, next_offset = _stream_entries(
            session_path, log_path, run_id, home,
            target=target, offset=int(offset),
        )
        # r3 fix #2: emit the cost-state note ONCE on a full read
        # (``offset == 0``). The note does NOT advance ``offset``, so
        # subsequent polls at the same offset return nothing and never
        # re-synthesise the note.
        if (not transcript_has_result
                and int(offset) == 0
                and not any(e.get("k") == _KIND_NOTE for e in stream_entries)):
            note = _build_note_from_cost_state(run_dir, target)
            if note:
                stream_entries.append({
                    "k": _KIND_NOTE,
                    "head": "",
                    "arg": note,
                    "lines": [{"t": note, "c": "muted"}],
                })

    status, status_c = _stream_status(target, session_path is not None,
                                      log_path is not None, is_live,
                                      transcript_entry_count)

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
        "kv": _kv_items(target, duration, run_obj),
        "acts": _actions(target, run_obj),
        "view": view,
    }

    if view == "stream":
        base.update({
            "entries": stream_entries,
            "offset": next_offset,
            "status": status,
            "status_c": status_c,
            "meta": _meta(target, run_obj),
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
