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

r5 contract (kickoff ide-node-stream-r5): every visible entry carries an
absolute ``_line`` (transcript line index or log file line index). The
cost-state note carries NO ``_line`` and does NOT advance the offset
cursor — it is a stateless derivation that fires on every full read
(``offset == 0``) and on the first incremental poll past the live→
finished edge. ``offset`` therefore counts only physical transcript/log
lines; the note never appears in the offset math.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from mini_ork.ide_pages import spec as S
from mini_ork.ide_pages.node_changes import MD_FILE_CAP, USER_FULL_CAP, build_changes_view
from mini_ork.ide_pages.run import Run, Node, _epoch, _load, _wall, _REVIEW_TYPES

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
NODE_CMD_LINE_CAP = 5000

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
_VIEWS = ("stream", "output", "prompt", "telemetry", "learning", "changes")

# Map a node's ``type`` (workflow role) onto an ``operator_steering`` role.
# ``_fetch_steer_rows`` keys on this map so the IDE stream shows only rows
# targeted at the node's role or ``any``. ``_VALID_ROLES`` lives in
# ``mini_ork.steering.operator_steering:40`` — every value here must be in
# that set.
#
# r4 fix #3: ``_REVIEW_TYPES`` (``mini_ork.ide_pages.run:28``) lists every
# node type that should read reviewer-targeted steers; we reuse it verbatim
# instead of duplicating the set. The ``researcher`` type maps to ``reviewer``
# only when its id ends in ``_lens`` (the convention the recipe uses to mark
# review lenses — non-lens researchers, e.g. scouts, stay on the default role).
_NODE_ROLE_MAP: dict[str, str] = {
    "planner": "planner",
    "decomposer": "planner",
    "implementer": "implementer",
    "worker": "implementer",
    "verifier": "verifier",
    "static_check": "verifier",
    "test": "verifier",
}
for _rt in _REVIEW_TYPES:
    _NODE_ROLE_MAP[_rt] = "reviewer"


def _role_for_node(node: Node) -> str:
    """Role key used by ``_fetch_steer_rows``.

    r4 fix #3: ``researcher`` nodes whose id ends in ``_lens`` map to
    ``reviewer`` so code_impact_lens / prior_art_lens (and any other
    recipe-defined ``*_lens`` researcher) sees reviewer-targeted steers.
    Non-lens researchers fall through to the default ``any`` role.
    """
    ntype = str(node.type or "")
    if ntype == "researcher" and str(node.id or "").endswith("_lens"):
        return "reviewer"
    return _NODE_ROLE_MAP.get(ntype, "any")

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
    start_s = int(node.start) - 5 if node.start is not None else None
    end_s = int(node.end) + 5 if node.end is not None else None
    # ``llm_calls.ts`` is TEXT (ISO format, see ``db/migrations/0002...sql:241``).
    # SQLite compares ISO strings chronologically when both bounds are also ISO.
    # Integer bounds would never match: SQLite sorts every INTEGER below every
    # TEXT value, so ``ts BETWEEN <int> AND <int>`` is false for each row.
    from datetime import datetime, timezone
    def _iso(epoch: int | None) -> str:
        if epoch is None:
            return ""
        return datetime.fromtimestamp(int(epoch), tz=timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%S.000Z"
        )
    bound_lo = _iso(start_s) or "0000-01-01T00:00:00.000Z"
    bound_hi = _iso(end_s) or "9999-12-31T23:59:59.999Z"
    sql = ("SELECT session_id, ts FROM llm_calls "
           "WHERE run_id = ? AND actor = ? "
           "  AND session_id IS NOT NULL AND session_id != '' "
           "  AND ts BETWEEN ? AND ? "
           "ORDER BY ts DESC")
    try:
        rows = db.rows(sql, (run.id, actor, bound_lo, bound_hi))
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
                    target: Node, offset: int,
                    run_dir: Path | None = None,
                    transcript_has_result: bool = False,
                    is_live: bool = False) -> tuple[list[dict[str, Any]], int]:
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
        raw.extend(_log_path_entries(log_path, offset=int(offset)))
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

    # r4 fix #1: bound steers against REAL transcript timestamps.
    # ``lower_bound`` = max ``_ts`` over transcript entries already consumed
    # (line at offset-1, in real seconds — never ``node_start + idx``). The
    # upper bound is the max ``_ts`` over transcript entries this poll would
    # consume (the "last consumed line's _ts" after the poll). A steer is
    # returned iff its ``_ts`` is strictly newer than the lower bound AND
    # not later than the upper bound.
    #
    # r3 fix #3 had the wrong complement: ``consumed_line_ts`` skipped lines
    # with ``_line < offset`` (so it held the *to-be-consumed* lines, not the
    # already-consumed ones), and the lower bound fell back to
    # ``node_start + prev_line_idx`` — a proxy that equals the real bound
    # only when every transcript line is exactly 1 s apart. With 60 s spacing
    # (kickoff fixture) the proxy is off by tens of seconds and the steer
    # boundary collapses.
    #
    # H1 (lens §5): timestamp-less appended lines fall back to
    # ``node_start + line_idx`` (set in :func:`_with_ts`). The fallback
    # cannot pull ``lower_bound`` backwards in the common case because
    # consumed lines carry real ISO timestamps and are *older* than
    # appended lines — taking ``max`` over all consumed ``_ts`` therefore
    # uses the real ISO value whenever it is available.
    # r5 fix #1 (log-backed): for shell/verifier nodes, ``_log_path_entries``
    # only returns lines at-or-after ``offset`` (no consumed-line entries).
    # The "last consumed line" timestamp must be reconstructed from the
    # line position: ``node_start + (offset - 1)`` seconds. Without this,
    # the lower bound is empty on every incremental poll and any steer
    # whose ``_ts`` precedes the first unconsumed line would re-emit.
    #
    # r5 fix #3 (no-source): for nodes without a transcript AND a log
    # (e.g. ``researcher`` lenses whose work lives outside the stream),
    # there are no line entries to bound against. Default the bounds to
    # the node's lifetime ``[start, end]`` so role-targeted steers that
    # fall inside the run window still surface.
    lower_bound_ts: list[int] = []  # transcript entries with _line < offset
    upper_bound_ts: list[int] = []  # transcript entries with _line >= offset
    if session_path is None and log_path is None:
        node_end_s = int(target.end) if target.end is not None else None
        lower_bound_ts.append(node_start_s)
        if node_end_s is not None:
            upper_bound_ts.append(node_end_s)
    # r6 fix #1 — the log-line proxy only fires when the stream is genuinely
    # log-backed. When both a transcript AND an ``impl-<id>.log`` exist (the
    # common implementer shape), ``log_path is not None`` is true but the
    # transcript already supplies real ``_ts`` values; appending the proxy
    # here would raise the lower bound and drop steers that belong to this
    # poll. Same discriminator as the log-backed branch above
    # (``session_path is None and log_path is not None``).
    if session_path is None and log_path is not None and int(offset) > 0:
        lower_bound_ts.append(node_start_s + int(offset) - 1)
    for e in raw:
        if e.get("_src") == "steer":
            continue
        line_idx = int(e.get("_line") or 0)
        ts_val = int(e.get("_ts") or 0)
        if line_idx < int(offset):
            lower_bound_ts.append(ts_val)
        else:
            upper_bound_ts.append(ts_val)
    has_upper = bool(upper_bound_ts)
    max_upper_ts = max(upper_bound_ts) if has_upper else None
    max_lower_ts = max(lower_bound_ts) if lower_bound_ts else None

    out: list[dict[str, Any]] = []
    for e in raw:
        is_steer = e.get("_src") == "steer"
        if is_steer:
            steer_ts = int(e.get("_ts") or 0)
            # No new lines consumed → nothing to bound the steer against.
            # The kickoff's "the one after returns nothing" guard: a drain
            # poll (offset >= total lines) returns no steers so a caller
            # polling at the same offset can't double-include them.
            if not has_upper:
                continue
            # "Newer than the line at N-1" — strict so we never repeat.
            if max_lower_ts is not None and steer_ts <= max_lower_ts:
                continue
            # "Not later than the last line consumed" — drops steers that
            # were added by the operator after the transcript froze.
            if max_upper_ts is not None and steer_ts > max_upper_ts:
                continue
        else:
            # ``offset`` = number of transcript lines already consumed.
            # Return entries whose source line is at-or-after that point.
            line_idx = int(e.get("_line") or 0)
            if line_idx < int(offset):
                continue
        out.append(e)

    next_offset = int(offset)
    # r4 fix #5 minor: ``offset`` counts every non-blank transcript/log line,
    # including ones that didn't emit (failed parse, blank line). For session
    # transcripts this is the file's non-blank line count; for logs the
    # matching helper.
    if session_path is not None:
        try:
            text = session_path.read_text(encoding="utf-8", errors="replace")
            physical_lines = sum(1 for ln in text.splitlines() if ln.strip())
        except OSError:
            physical_lines = 0
    elif log_path is not None:
        physical_lines = _log_line_count(log_path)
    else:
        physical_lines = 0
    if physical_lines > next_offset:
        next_offset = physical_lines
    for e in out:
        if e.get("_src") == "steer":
            continue
        line_idx = int(e.get("_line") or 0)
        if line_idx + 1 > next_offset:
            next_offset = line_idx + 1

    # r5 fix #2: the cost-state note is STATELESS — the read path never
    # writes. The "once-ness" property is reconstructed from data the
    # builder already has. Two emission paths, both gated on the node
    # being finished and the cost-state being available:
    #
    #   (a) every full read (``offset == 0``) of a finished node — the
    #       note appears for every viewer that opens the panel cold;
    #   (b) the first incremental poll past the live→finished edge —
    #       a follower who was polling during live state should see the
    #       note once when the node finishes, then never again until
    #       they restart with offset=0.
    #
    # "Without changing offset" (kickoff wording): the note does NOT
    # advance ``next_offset`` and carries no ``_line``. It does not count
    # in the status pill's ``transcript_entry_count`` either.
    #
    # Edge predicate (b): the newest consumed transcript ``_ts`` BEFORE
    # this poll is older than the node's end (or the cost-state
    # timestamp) AND this poll's upper bound reaches it. Computed below
    # from the consumed entries' ``_ts`` (transcript/log) vs the
    # cost-state envelope (read on demand).
    if (run_dir is not None
            and not transcript_has_result
            and not is_live
            and not any(e.get("k") == _KIND_NOTE for e in out)):
        emit_note = False
        if int(offset) == 0:
            # Path (a): full read of a finished node.
            emit_note = True
        else:
            # Path (b): first incremental poll past the live→finished edge.
            # Compare the newest consumed entry's ``_ts`` (we already
            # resolved it above) to the cost-state timestamp.
            newest_consumed_ts = max_lower_ts
            cs_env: dict[str, Any] | None = None
            try:
                cs_env = _fetch_cost_state(run_dir, target.id)
            except Exception:  # noqa: BLE001
                cs_env = None
            cs_ts: int | None = None
            # r6 fix #3 — drop the dead ``cs_env.get("ts")`` read. The inner
            # envelope returned by ``_fetch_cost_state`` carries the session
            # aggregate only (session_id / total_cost_usd / num_turns); the
            # outer ``live.jsonl`` record's ``t`` is discarded upstream. Fall
            # straight through to the ``target.end`` fallback.
            if isinstance(cs_env, dict) and target.end is not None:
                cs_ts = int(target.end)
            if (cs_ts is not None
                    and newest_consumed_ts is not None
                    and newest_consumed_ts < cs_ts
                    and max_upper_ts is not None
                    and max_upper_ts >= cs_ts):
                emit_note = True
        if emit_note:
            note = _build_note_from_cost_state(run_dir, target)
            if note:
                out.append({
                    "k": _KIND_NOTE,
                    "head": "",
                    "arg": note,
                    "lines": [{"t": note, "c": "muted"}],
                })

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
                        "arg": text[:USER_FULL_CAP],
                        "lines": [{"t": text[:USER_HEAD_CAP], "c": "body"}],
                        "md": True,
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
                    text = _text_block(b, "text")[:USER_FULL_CAP]
                    out.append(_with_ts({
                        "k": _KIND_TEXT,
                        "head": "",
                        "arg": text,
                        "lines": [{"t": text[:TEXT_CAP], "c": "body"}],
                        "md": True,
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


def _log_path_entries(log_path: Path, offset: int = 0) -> list[dict[str, Any]]:
    """Log-file entries for shell/verifier nodes.

    r3 fix #4: one entry per log line so ``offset`` counts log lines and
    each new line surfaces as its own ``text`` entry. ``_src="log"`` and
    ``_line=<index>`` keep the offset contract identical to transcript
    entries.

    r4 fix #2: index by ABSOLUTE file line number so incremental polls at
    ``offset=N`` return exactly the new lines, not a re-slice of the tail.
    The ``SHELL_LOG_LINES`` cap is applied only on a full read (``offset==0``):
    a 45-line log returns the last 40 lines (with absolute ``_line`` 5..44)
    on the first poll, and the next 5 lines on a subsequent poll at ``offset=40``.
    """
    try:
        text = log_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        text = ""
    raw_lines = text.splitlines()
    total = len(raw_lines)
    if int(offset) <= 0:
        # Full read: cap to the last SHELL_LOG_LINES lines but keep
        # absolute line indices so the caller can pick up at offset N.
        start_idx = max(0, total - SHELL_LOG_LINES)
    else:
        # Incremental poll: every line at-or-after ``offset``, no cap.
        start_idx = min(int(offset), total)
    out: list[dict[str, Any]] = []
    for abs_idx in range(start_idx, total):
        raw = raw_lines[abs_idx]
        c = "red" if "Traceback" in raw or "ERROR" in raw else (
            "green" if "PASS" in raw or "ok" in raw.lower() else "body")
        out.append({
            "k": _KIND_TEXT,
            "head": "",
            "arg": raw[:LINE_CHARS],
            "lines": [{"t": raw[:LINE_CHARS], "c": c}],
            "_src": "log",
            "_line": abs_idx,
        })
    return out


def _log_line_count(log_path: Path) -> int:
    """Total non-blank log lines — what ``offset`` advances against.

    r4 fix #5 minor: ``offset`` counts every non-blank transcript line,
    including ones that don't emit a visible entry (failed parse, blank
    line). Same rule for shell logs: a 45-line log with 40 emit-cap on a
    full read still has ``offset == 45`` after the first poll, so the
    second poll at ``offset=45`` returns empty (and "NEW LINE" appended
    at line 45 is correctly reported by the poll at offset=45).
    """
    try:
        text = log_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return 0
    return sum(1 for ln in text.splitlines() if ln.strip())


# ── non-agent command path (kickoff ide-node-commands §2) ──────────────────────
# When a node has no session transcript and either its type is not an LLM
# type (verifier / publisher / rollback / shell / gate / transform) or it
# has no ``llm_calls``, the stream view is rebuilt from commands instead of
# from a Claude Code session. The kickoff names 5 entry kinds (a–e); this
# helper returns the entries + the consumed offset, or ``None`` to signal
# "no command artefacts for this node — fall back to the existing log
# line stream". That additive shape (lens §3.1 option A) preserves the
# mandate-green test_ide_pages_node.py:318 fixture, which only ships a
# verifier_<id>.log.


def _verifier_stem(target: Node) -> str | None:
    """Stem of the verifier script for this node (basename minus extension).

    ``Node.prompt`` is ``<recipe>/<verifier_ref>`` per ``run.py:258``; for
    a framework-edit node with ``verifier_ref: verifiers/static-check.py``
    it reads ``framework-edit/static-check.py`` and ``Path.stem`` yields
    ``static-check`` — the correct stem the kickoff's evidence files
    (``verifier_static-check.json``) are keyed on. The fall-back
    node-id-with-``_``→``-`` rule from the kickoff is intentionally
    avoided: it would yield ``static-check-verifier`` for the framework
    edit verifier, which does not exist on disk (lens §3.2).
    """
    if not target.prompt:
        return None
    p = Path(target.prompt)
    if p.suffix in (".py", ".sh"):
        return p.stem
    return None


def _is_command_backed(target: Node, session_path: Path | None,
                       has_log: bool) -> bool:  # has_log kept in signature; build_node passes log_path-is-not-None
    """Branch gate: should the stream be built from commands?

    True when:

    * the node has no session transcript (an LLM-bearing node would have one),
    * AND the node has no recorded LLM calls (no Claude Code transcript),
    * OR its ``type`` is one of the deterministic (non-LLM) types in
      ``run._DETERMINISTIC`` (verifier, publisher, rollback, shell, gate,
      transform).

    A node with ``calls > 0`` and a non-deterministic type falls back to
    the existing path (it should have a session somewhere — missing
    session is a real bug, not something the command branch should paper
    over).
    """
    if session_path is not None:
        return False
    from mini_ork.ide_pages.run import _DETERMINISTIC

    if (target.type or "") in _DETERMINISTIC:
        return True
    if target.calls and target.calls > 0:
        return False
    # No session, no calls, non-LLM — treat as command-backed even when
    # the workflow forgot to tag the type. ``_resolve_log_path`` would
    # also have surfaced a log; the command branch handles the case where
    # there is no log either (built-in steps that only print to
    # execute.log). Both shapes qualify — the helpers return [] otherwise.
    return True


def _command_stream_entries(run_dir: Path, target: Node, log_path: Path | None,
                            recipe_dir: Path | None, *,
                            home: Path | None = None,
                            offset: int = 0) -> tuple[list[dict[str, Any]], int, str] | None:  # log_path kept in signature; build_node's call shape
    """Build the stream entries (kickoff §2a–e) from command artefacts.

    Returns ``(entries, next_offset, source)`` when command artefacts are
    available. Returns ``None`` when this is a non-agent node that
    nevertheless has no ``node-cmd`` record and no legacy
    ``verifier_<stem>.json`` AND no built-in log lines — caller should
    fall back to the existing log-line path.

    r2 (kickoff fixes 1/4/5/6/7): every branch returns
    ``(entries[offset:], len(entries), source)`` so polling at the returned
    offset yields no duplicates. The reconstructed-command branch now
    keeps §2b (sub-commands + ``_*cmd*.log``) and §2c (verifier logs)
    after the ``reconstructed`` marker, and computes the command string as
    ``python3 <recipe_dir>/<verifier_ref>`` (not the relative
    ``target.prompt``). The §2c log block skips any path equal to the
    record's ``output_path`` so the verifier's own log does not appear
    twice.
    """
    entries: list[dict[str, Any]] = []
    line_idx = 0
    source_name = ""
    stem = _verifier_stem(target)
    skip_paths: set[str] = set()  # paths already shown via §2a output

    record_path = (run_dir / "node-cmd" / f"verifier_{stem}.json") if stem else None
    legacy_evidence = (run_dir / f"verifier_{stem}.json") if stem else None

    record: dict[str, Any] | None = None
    if record_path and record_path.is_file():
        try:
            parsed = json.loads(record_path.read_text(encoding="utf-8", errors="replace"))
            if isinstance(parsed, dict):
                record = parsed
        except (OSError, ValueError):
            record = None

    if isinstance(record, dict):
        argv = [str(a) for a in (record.get("argv") or [])]
        cmd_str = str(record.get("cmd") or "") or shlex_join_safe(argv)
        cwd = str(record.get("cwd") or "")
        rc_raw = record.get("rc")
        try:
            rc = int(rc_raw) if rc_raw is not None else 0
        except (TypeError, ValueError):
            rc = 0
        out_path = str(record.get("output_path") or "")
        started = float(record.get("started_at") or 0.0)
        ended = float(record.get("ended_at") or 0.0)
        dur = max(0.0, ended - started)
        if out_path:
            skip_paths.add(out_path)

        arg_str = f"cd {cwd} && {cmd_str}" if cwd else cmd_str

        # ── §2a — the command itself ─────────────────────────────────
        output_lines = _read_output_lines(out_path, NODE_CMD_LINE_CAP)
        is_error = (rc != 0)
        for ln in output_lines:
            ln["c"] = "red" if is_error else ln.get("c", "body")
        if output_lines and output_lines[-1].get("t", "").startswith("exit "):
            pass  # already an exit line below
        # Tail cap notice
        total_lines = _count_lines(out_path)
        if total_lines > NODE_CMD_LINE_CAP:
            output_lines.append({
                "t": f"… {total_lines - NODE_CMD_LINE_CAP} more lines — {out_path}",
                "c": "muted",
            })
        output_lines.append({
            "t": f"exit {rc} · {dur:.2f}s",
            "c": "red" if is_error else "muted",
        })
        entries.append({
            "k": _KIND_TOOL,
            "head": "$",
            "arg": arg_str,
            "lines": output_lines or [{"t": "(no output)", "c": "muted"}],
            "_src": "cmd",
            "_line": line_idx,
        })
        line_idx += 1
        source_name = str(record_path.name) if record_path else ""

        # ── §2b — sub-commands the verifier ran ─────────────────────
        ev_doc = _read_json_safely(out_path)
        sub = _subcommand_entries(ev_doc)
        for e in sub:
            e["_line"] = line_idx
            entries.append(e)
            line_idx += 1
        # ── §2b — _*cmd*.log files (researcher verifier smoke runs) ──
        try:
            cmd_logs = _own_cmd_logs(run_dir, stem)
        except OSError:
            cmd_logs = []
        for cmd_log in cmd_logs:
            if str(cmd_log) in skip_paths:
                continue
            blocks = _parse_cmd_log_blocks(cmd_log)
            for blk in blocks:
                blk["_line"] = line_idx
                entries.append(blk)
                line_idx += 1

        # ── §2c — the verifier's own logs ───────────────────────────
        for rel in (f"verifier-{stem}.log",):
            lp = run_dir / rel
            if lp.is_file() and str(lp) not in skip_paths:
                e = _log_path_entry(lp, head="log", arg=str(lp),
                                    line_idx=line_idx, cap=NODE_CMD_LINE_CAP)
                entries.append(e)
                line_idx += 1
                if not source_name:
                    source_name = lp.name
        ev_dir = run_dir / "evidence"
        if ev_dir.is_dir():
            try:
                # The newest evidence log only: older ones are earlier attempts.
                logs = sorted(
                    [p for p in ev_dir.glob(f"{stem}*.log") if p.is_file()],
                    key=lambda p: p.stat().st_mtime,
                    reverse=True,
                )[:1]
            except OSError:
                logs = []
            for lp in logs:
                if str(lp) in skip_paths:
                    continue
                e = _log_path_entry(lp, head="log", arg=str(lp),
                                    line_idx=line_idx, cap=NODE_CMD_LINE_CAP)
                entries.append(e)
                line_idx += 1

        return _slice_with_offset(entries, offset, source_name)

    # ── §2 reconstruction when the run predates command recording ───
    # A verifier counts when its JSON OR any of its own logs exist (a
    # verifier that wrote only ``verifier-<stem>.log`` still has output).
    has_own_logs = bool(stem) and (
        (run_dir / f"verifier-{stem}.log").is_file()
        or any((run_dir / "evidence").glob(f"{stem}*.log"))
    )
    if (legacy_evidence and legacy_evidence.is_file()) or has_own_logs:
        # Best-effort cwd from the run profile's pinned target.
        cwd = ""
        try:
            from mini_ork.runtime.run_roots import load_run_roots
            roots = load_run_roots(str(run_dir))
            if roots:
                cwd = str(roots.target or "")
        except Exception:
            cwd = ""

        # r2 fix #7: build the command as ``python3 <recipe_dir>/<verifier_ref>``
        # instead of the relative ``target.prompt`` (``<recipe>/<verifier_ref>``,
        # which only resolves when cwd is the engine root). ``<recipe_id>`` is
        # the segment before the first ``/`` in ``target.prompt``;
        # ``<recipe_dir>`` is resolved via ``recipes_catalog.find_recipe`` with
        # the engine ``recipes/<recipe>`` fallback.
        recipe_id, rel_ref = "", ""
        if target.prompt:
            prompt = target.prompt
            # ``node.prompt`` is ``<recipe dir name>/<verifier_ref>``; keep the
            # ref's own subdirectory (``verifiers/cycle-gate.py``).
            if "/" in prompt:
                recipe_id, rel_ref = prompt.split("/", 1)
            else:
                rel_ref = prompt
        resolved_dir = recipe_dir
        if resolved_dir is None and recipe_id:
            try:
                from mini_ork.recipes_catalog import find_recipe
                info = find_recipe(recipe_id, home)
                if info is not None:
                    resolved_dir = info.path
            except Exception:
                resolved_dir = None
            if resolved_dir is None:
                engine_root = Path(__file__).resolve().parent.parent.parent
                candidate = engine_root / "recipes" / recipe_id
                if candidate.is_dir():
                    resolved_dir = candidate
        if resolved_dir and rel_ref:
            cmd_str = f"python3 {Path(resolved_dir) / rel_ref}"
        elif target.prompt:
            cmd_str = f"python3 {target.prompt}"
        else:
            cmd_str = ""

        arg_str = f"cd {cwd} && {cmd_str}" if cwd else cmd_str
        legacy_evidence_str = str(legacy_evidence)
        skip_paths.add(legacy_evidence_str)
        output_lines = (_read_output_lines(legacy_evidence_str, NODE_CMD_LINE_CAP)
                        if legacy_evidence.is_file() else [])
        entries.append({
            "k": _KIND_TOOL,
            "head": "$",
            "arg": arg_str or "(reconstructed command)",
            "lines": output_lines or [{"t": "(its direct output was not stored — see its logs below)",
                                       "c": "muted"}],
            "_src": "cmd",
            "_line": line_idx,
        })
        line_idx += 1
        note_text = "Command reconstructed from the recipe — this run predates command recording."
        entries.append({
            "k": _KIND_NOTE,
            "head": "reconstructed",
            "arg": note_text,
            "lines": [{"t": note_text, "c": "muted"}],
            "_src": "note",
            "_line": line_idx,
        })
        line_idx += 1

        # r2 fix #1: legacy runs keep §2b and §2c — every researcher run
        # predates recording, so this branch is the main case.
        ev_doc = _read_json_safely(legacy_evidence_str)
        for e in _subcommand_entries(ev_doc):
            e["_line"] = line_idx
            entries.append(e)
            line_idx += 1
        try:
            cmd_logs = _own_cmd_logs(run_dir, stem)
        except OSError:
            cmd_logs = []
        for cmd_log in cmd_logs:
            if str(cmd_log) in skip_paths:
                continue
            for blk in _parse_cmd_log_blocks(cmd_log):
                blk["_line"] = line_idx
                entries.append(blk)
                line_idx += 1

        for rel in (f"verifier-{stem}.log",):
            lp = run_dir / rel
            if lp.is_file() and str(lp) not in skip_paths:
                e = _log_path_entry(lp, head="log", arg=str(lp),
                                    line_idx=line_idx, cap=NODE_CMD_LINE_CAP)
                entries.append(e)
                line_idx += 1
        ev_dir = run_dir / "evidence"
        if ev_dir.is_dir():
            try:
                # The newest evidence log only: older ones are earlier attempts.
                logs = sorted(
                    [p for p in ev_dir.glob(f"{stem}*.log") if p.is_file()],
                    key=lambda p: p.stat().st_mtime,
                    reverse=True,
                )[:1]
            except OSError:
                logs = []
            for lp in logs:
                if str(lp) in skip_paths:
                    continue
                e = _log_path_entry(lp, head="log", arg=str(lp),
                                    line_idx=line_idx, cap=NODE_CMD_LINE_CAP)
                entries.append(e)
                line_idx += 1

        return _slice_with_offset(entries, offset,
                                  legacy_evidence.name if legacy_evidence.is_file() else f"verifier-{stem}.log")

    # ── §2d — built-in steps (rollback, publisher, skips) ────────────
    log_path_x = run_dir / "execute.log"
    if log_path_x.is_file() and target.id:
        try:
            all_lines = log_path_x.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            all_lines = []
        id_tag = f"[{target.id}]"
        type_tag = f"[{target.type}]" if target.type else ""
        node_id_tag = f"node_id={target.id}"
        # r2 kickoff fix #3 — also keep built-in prefix lines that researchers
        # write without an `[<id>]` tag (e.g. ``[ok] rollback complete``,
        # ``[fail] rollback``, ``[info] rollback``). The `<id>` in the
        # kickoff means either the full id or the short type — operators
        # log ``[ok] rollback`` not ``[ok] rollback_node``. ``[skip]`` is
        # always ``node_id=<id>`` (the existing shape with a prefix).
        ok_prefixes: list[str] = [f"[ok] {target.id}"]
        fail_prefixes: list[str] = [f"[fail] {target.id}"]
        info_prefixes: list[str] = [f"[info] {target.id}"]
        skip_prefixes: list[str] = [f"[skip] node_id={target.id}"]
        if target.type:
            ok_prefixes.append(f"[ok] {target.type}")
            fail_prefixes.append(f"[fail] {target.type}")
            info_prefixes.append(f"[info] {target.type}")

        def _line_matches(ln: str) -> bool:
            ln = ln.lstrip()  # execute.log indents its lines with two spaces
            if id_tag in ln or (type_tag and type_tag in ln) or node_id_tag in ln:
                return True
            if ln.startswith(id_tag):
                return True
            if type_tag and ln.startswith(type_tag):
                return True
            for prefixes in (ok_prefixes, fail_prefixes, info_prefixes, skip_prefixes):
                for p in prefixes:
                    if p and ln.startswith(p):
                        return True
            return False

        matched = [ln for ln in all_lines if _line_matches(ln)]
        if matched:
            entry_lines = [
                {"t": ln.strip()[:LINE_CHARS], "c": "body"}
                for ln in matched[:NODE_CMD_LINE_CAP]
            ]
            entries.append({
                "k": _KIND_TOOL,
                "head": "built-in",
                "arg": f"{target.type or ''} · {target.id}",
                "lines": entry_lines or [{"t": "(no output)", "c": "muted"}],
                "_src": "builtin",
                "_line": line_idx,
            })
            line_idx += 1
            for name, head in (("rolled-back.json", "rollback"),
                               ("salvage.json", "salvage")):
                rp = run_dir / name
                if rp.is_file():
                    try:
                        txt = rp.read_text(encoding="utf-8", errors="replace")
                    except OSError:
                        txt = ""
                    entries.append({
                        "k": _KIND_NOTE,
                        "head": head,
                        "arg": rp.name,
                        "lines": [
                            {"t": ln[:LINE_CHARS], "c": "muted"}
                            for ln in txt.splitlines()
                        ],
                        "_src": "builtin",
                        "_line": line_idx,
                    })
                    line_idx += 1
            return _slice_with_offset(entries, offset, log_path_x.name)

    # No artefacts at all → caller falls back to existing log path or
    # emits the §2e "nothing was stored" note (build_node handles that).
    return None


def _own_cmd_logs(run_dir: Path, stem: str) -> list[Path]:
    """``<run_dir>/_<kind>_cmd*.log`` files that belong to this verifier.

    They sit in the run dir, not per node, so a file is this verifier's only
    when its ``<kind>`` names it: ``_smoke_cmd_W5-91.log`` belongs to
    ``live-smoke``, not to ``cycle-gate`` or ``no-scope-creep``.
    """
    try:
        found = sorted(run_dir.glob("_*cmd*.log"))
    except OSError:
        return []
    words = {w for w in stem.lower().replace("_", "-").split("-") if w}
    own = []
    for path in found:
        kind = path.name.lstrip("_").split("_cmd", 1)[0].lower()
        if kind and kind in words:
            own.append(path)
    return own


def _command_rc(run_dir: Path, target: Node) -> int | None:
    """The recorded exit code of a verifier's command, or ``None``."""
    stem = _verifier_stem(target)
    record = run_dir / "node-cmd" / f"verifier_{stem}.json" if stem else None
    if record is None or not record.is_file():
        return None
    try:
        rc = json.loads(record.read_text(encoding="utf-8", errors="replace")).get("rc")
        return int(rc) if rc is not None else None
    except (OSError, ValueError, TypeError, AttributeError):
        return None


def _slice_with_offset(entries: list[dict[str, Any]], offset: int,
                       source_name: str) -> tuple[list[dict[str, Any]], int, str]:
    """Return ``(entries[offset:], len(entries), source_name)``.

    r2 kickoff fix #4 — append-only offset contract that mirrors
    ``_stream_entries`` and ``_log_path_entries``: the returned offset is
    the **total** entry count, so the next poll at that offset returns no
    duplicates (legacy and built-in too).
    """
    start = max(0, int(offset))
    return entries[start:], len(entries), source_name


def _read_output_lines(path: str, cap: int) -> list[dict[str, str]]:
    """Read up to ``cap`` lines from ``path`` as colour-tagged dicts."""
    if not path:
        return []
    p = Path(path)
    if not p.is_file():
        return []
    try:
        text = p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    out: list[dict[str, str]] = []
    for ln in text.splitlines()[:cap]:
        out.append({"t": ln[:LINE_CHARS], "c": "body"})
    return out


def _count_lines(path: str) -> int:
    if not path:
        return 0
    p = Path(path)
    if not p.is_file():
        return 0
    try:
        text = p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return 0
    return len(text.splitlines())


def _read_json_safely(path: str) -> dict[str, Any]:
    if not path:
        return {}
    p = Path(path)
    if not p.is_file():
        return {}
    try:
        doc = json.loads(p.read_text(encoding="utf-8", errors="replace"))
    except (OSError, ValueError):
        return {}
    return doc if isinstance(doc, dict) else {}


def _verifier_pass_flag(run_dir: Path, *, stem: str | None) -> bool | None:
    """Return the verifier evidence ``pass`` flag, when recoverable.

    r2 kickoff fix #5: when the command branch has no recoverable rc
    (legacy / built-in entries), derive the pill from the verifier
    evidence's ``pass`` flag so ``pass: false`` still colours the pill
    red. Looks at ``<run_dir>/verifier_<stem>.json`` first, then
    ``<run_dir>/evidence/<stem>*.json|.log``. Returns ``None`` when no
    evidence JSON can be parsed (caller falls through to node state).
    """
    candidates: list[Path] = []
    if stem:
        candidates.append(run_dir / f"verifier_{stem}.json")
        ev_dir = run_dir / "evidence"
        if ev_dir.is_dir():
            try:
                candidates.extend(
                    sorted(
                        [p for p in ev_dir.glob(f"{stem}*.json") if p.is_file()],
                        key=lambda p: p.stat().st_mtime,
                        reverse=True,
                    )
                )
                candidates.extend(
                    sorted(
                        [p for p in ev_dir.glob(f"{stem}*.log") if p.is_file()],
                        key=lambda p: p.stat().st_mtime,
                        reverse=True,
                    )
                )
            except OSError:
                pass
    for path in candidates:
        doc = _read_json_safely(str(path))
        if not doc:
            continue
        val = doc.get("pass")
        if isinstance(val, bool):
            return val
    return None


def _log_path_entry(log_path: Path, *, head: str, arg: str,
                    line_idx: int, cap: int) -> dict[str, Any]:
    try:
        text = log_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        text = ""
    raw = text.splitlines()
    lines: list[dict[str, str]] = []
    for ln in raw[:cap]:
        c = "red" if "Traceback" in ln or "ERROR" in ln else (
            "green" if "PASS" in ln or "ok" in ln.lower() else "body")
        lines.append({"t": ln[:LINE_CHARS], "c": c})
    if len(raw) > cap:
        lines.append({"t": f"… {len(raw) - cap} more lines", "c": "muted"})
    return {
        "k": _KIND_TOOL,
        "head": head,
        "arg": arg,
        "lines": lines or [{"t": "(no output)", "c": "muted"}],
        "_src": "log",
        "_line": line_idx,
    }


def _subcommand_entries(ev_doc: dict[str, Any]) -> list[dict[str, Any]]:
    """Mine sub-commands from a verifier evidence JSON (§2b).

    r2 kickoff fix #2 — researcher ``gate_cmd`` + ``gate_cmd_output_tail`` +
    ``gate_cmd_exit`` keys now collapse into a single entry: ``arg`` is the
    command (``gate_cmd`` or a sentinel when the key is missing), ``lines``
    is the output tail, with a muted ``exit <n>`` line appended. The
    surfaces[] / target shape maps to ``arg=target`` + ``lines=status · reason``
    on a single line.
    """
    out: list[dict[str, Any]] = []
    seen: set[str] = set()

    def _add(head: str, arg: str, lines: list[str], rc: int | None) -> None:
        if not arg and not lines:
            return
        key = f"{head}|{arg}"
        if key in seen:
            return
        seen.add(key)
        body = [{"t": ln[:LINE_CHARS], "c": "body"} for ln in lines[:NODE_CMD_LINE_CAP]]
        if rc is not None:
            body.append({"t": f"[rc={rc}]", "c": "red" if rc else "muted"})
        out.append({
            "k": _KIND_TOOL,
            "head": head,
            "arg": arg,
            "lines": body or [{"t": "(no output)", "c": "muted"}],
            "_src": "subcmd",
        })

    # gate_cmd / gate_cmd_output_tail / gate_cmd_exit — researcher verifier.
    # r2: ONE entry, not three. ``gate_cmd_exit`` may be int (legacy fixture
    # ``verifier_cycle-gate.json``) or str; coerce, do not string-compare.
    gate_cmd_raw = ev_doc.get("gate_cmd")
    if gate_cmd_raw is not None:
        arg = str(gate_cmd_raw).strip() if isinstance(gate_cmd_raw, str) else (
            str(gate_cmd_raw).strip() if str(gate_cmd_raw).strip() else ""
        )
        if not arg:
            arg = "the step's gate command"
    else:
        arg = "the step's gate command"

    tail_raw = ev_doc.get("gate_cmd_output_tail")
    if isinstance(tail_raw, str):
        tail_lines = tail_raw.splitlines()
    elif isinstance(tail_raw, list):
        tail_lines = [str(x) for x in tail_raw]
    else:
        tail_lines = []

    exit_raw = ev_doc.get("gate_cmd_exit")
    exit_int: int | None = None
    if isinstance(exit_raw, bool):
        exit_int = int(exit_raw)
    elif isinstance(exit_raw, int):
        exit_int = exit_raw
    elif isinstance(exit_raw, str):
        try:
            exit_int = int(exit_raw.strip())
        except ValueError:
            exit_int = None

    # Reuse ``_add`` so dedup / line-cap semantics stay aligned with the rest
    # of the function; the muted ``exit <n>`` line is appended here.
    if tail_lines or exit_int is not None:
        key = f"subcmd|{arg}"
        if key not in seen:
            seen.add(key)
            body = [
                {"t": ln[:LINE_CHARS], "c": "body"}
                for ln in tail_lines[:NODE_CMD_LINE_CAP]
            ]
            if exit_int is not None:
                body.append({"t": f"exit {exit_int}", "c": "muted"})
            out.append({
                "k": _KIND_TOOL,
                "head": "subcmd",
                "arg": arg,
                "lines": body or [{"t": "(no output)", "c": "muted"}],
                "_src": "subcmd",
            })
    elif arg != "the step's gate command":
        # ``gate_cmd`` present but no tail / no exit — emit one entry to
        # preserve the command text in the stream.
        _add("subcmd", arg, [], None)

    # surfaces[] / surface:cmd — live_smoke verifier. r2: arg=target,
    # lines=status · reason on a single line (not the surfaces[].lines
    # output, which was the wrong field for the stream view).
    surfaces = ev_doc.get("surfaces")
    if isinstance(surfaces, list):
        for s in surfaces:
            if not isinstance(s, dict):
                continue
            if s.get("surface") != "cmd":
                continue
            target = str(s.get("target") or "").strip()
            if not target:
                continue
            status = str(s.get("status") or "").strip()
            reason = str(s.get("reason") or "").strip()
            line = f"{status} · {reason}".strip(" ·") or target
            _add("subcmd", target, [line], None)
    if ev_doc.get("surface") == "cmd" and ev_doc.get("target"):
        target = str(ev_doc.get("target") or "").strip()
        if target:
            _add("subcmd", target, [str(ev_doc.get("reason") or ev_doc.get("status") or "")], None)

    # _*cmd*.log files in the run dir — handled separately by
    # ``_parse_cmd_log_blocks`` (the JSON is one source, the log is the
    # other; the caller merges them).
    return out


def _parse_cmd_log_blocks(path: Path) -> list[dict[str, Any]]:
    """Split a ``_<run_dir>_cmd_<step>.log`` into ``$ cmd / output / [rc=N]`` blocks.

    Each block becomes one ``subcmd`` entry. Lines starting with ``$`` open
    a new block; a line matching ``[rc=N]`` closes one (carrying its rc).
    Output lines belong to the most recently opened command.
    """
    if not path or not path.is_file():
        return []
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    out: list[dict[str, Any]] = []
    cur_cmd: str | None = None
    cur_lines: list[str] = []
    cur_rc: int | None = None
    for raw in text.splitlines():
        line = raw.rstrip()
        if line.startswith("$ "):
            if cur_cmd is not None:
                out.append(_build_cmd_block(cur_cmd, cur_lines, cur_rc))
            cur_cmd = line[2:].strip()
            cur_lines = []
            cur_rc = None
        elif line.startswith("[rc="):
            try:
                cur_rc = int(line[4:].rstrip("]").strip())
            except ValueError:
                cur_rc = None
            if cur_cmd is not None:
                out.append(_build_cmd_block(cur_cmd, cur_lines, cur_rc))
                cur_cmd = None
                cur_lines = []
                cur_rc = None
        else:
            if cur_cmd is not None:
                cur_lines.append(line)
    if cur_cmd is not None:
        out.append(_build_cmd_block(cur_cmd, cur_lines, cur_rc))
    return out


def _build_cmd_block(cmd: str, lines: list[str], rc: int | None) -> dict[str, Any]:
    body: list[dict[str, str]] = []
    for ln in lines[:NODE_CMD_LINE_CAP]:
        body.append({"t": ln[:LINE_CHARS], "c": "body"})
    if rc is not None:
        body.append({"t": f"[rc={rc}]", "c": "red" if rc else "muted"})
    return {
        "k": _KIND_TOOL,
        "head": "subcmd",
        "arg": cmd,
        "lines": body or [{"t": "(no output)", "c": "muted"}],
        "_src": "subcmd",
    }


def shlex_join_safe(argv: list[str]) -> str:
    try:
        import shlex as _shlex
        return _shlex.join(argv)
    except ImportError:
        return " ".join(argv)


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

    Kickoff §2: when the matched report is a markdown file, the response
    ALSO carries a top-level ``markdown`` block built from the untruncated
    file text (capped at ``MD_FILE_CAP`` chars), so the IDE panel renders
    the report whole instead of the last ``SHELL_LOG_LINES`` lines. The
    truncated ``block`` is kept for back-compat.
    """
    block: list[dict[str, str]] = []
    block_title = "Node output"
    md_block: dict[str, Any] | None = None
    for path in _report_paths(run_dir, node.id):
        if not path.is_file():
            continue
        text = _read_text(path)
        if text is None:
            continue
        block_title = f"Node output · {path.name}"
        for ln in text.splitlines()[-SHELL_LOG_LINES:]:
            block.append({"t": ln[:LINE_CHARS], "c": "body"})
        if path.suffix == ".md":
            md_block = {"title": path.name,
                        "text": text[:MD_FILE_CAP],
                        "path": str(path)}
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
    out: dict[str, Any] = {"block_title": block_title, "block": block,
                           "list_title": "Artifacts", "list": arts}
    if md_block is not None:
        out["markdown"] = md_block
    return out


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


def _prompt_view(node: Node, session_path: Path | None,
                 recipe_dir: Path | None = None) -> dict[str, Any]:
    """Kickoff §2: the response carries the FULL prompt (capped at
    ``MD_FILE_CAP`` chars) as a top-level ``markdown`` block, plus the
    truncated ``block`` for back-compat. The prompt file path comes from
    ``node.prompt`` (``recipe_name/<prompt_ref>``) resolved against
    ``recipe_dir`` when present."""
    block_title = "Rendered prompt · as dispatched"
    block: list[dict[str, str]] = []
    full_text = ""
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
                full_text = content
                break
            if not isinstance(content, list):
                continue
            for b in content:
                if isinstance(b, dict) and b.get("type") == "text":
                    full_text = str(b.get("text") or "")
                    break
            break
    if not full_text:
        # Fix #8: when no transcript prompt was captured, read the prompt
        # file ``_resolve_prompt_file`` finds and use its contents.
        # Fall back to the recipe ref only when no file resolves.
        prompt_path = _resolve_prompt_file(node, recipe_dir)
        if prompt_path:
            try:
                full_text = Path(prompt_path).read_text(encoding="utf-8", errors="replace")
            except OSError:
                full_text = ""
        if not full_text and node.prompt:
            full_text = node.prompt
    if full_text:
        for ln in full_text.splitlines():
            block.append({"t": ln[:LINE_CHARS], "c": "body"})
    if not block:
        block = [{"t": "No prompt recorded for this node.", "c": "muted"}]
    list_items: list[dict[str, Any]] = []
    if node.prompt:
        list_items.append(S.item(node.prompt, "recipe prompt ref",
                                  acts=[S.btn("Open", S.open_path(node.prompt), "ghost")]))
    out: dict[str, Any] = {"block_title": block_title, "block": block,
                           "list_title": "Prompt ref", "list": list_items}
    if full_text:
        out["markdown"] = {"title": "Rendered prompt · as dispatched",
                           "text": full_text[:MD_FILE_CAP],
                           "path": _resolve_prompt_file(node, recipe_dir)}
    return out


def _resolve_prompt_file(node: Node, recipe_dir: Path | None) -> str | None:
    """Resolve ``node.prompt`` (shape ``recipe_name/<prompt_ref>``) against
    ``recipe_dir`` to an existing file path, or ``None`` when missing."""
    if not node.prompt or recipe_dir is None:
        return None
    parts = node.prompt.split("/", 1)
    if len(parts) != 2:
        return None
    path = recipe_dir / parts[1]
    return str(path) if path.is_file() else None


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


# ── per-node learning view (kickoff `learn-node-tab`) ───────────────────────
#
# Three independent sources, in order:
#
#   1. The ``learned/<node.id>.{md,json}`` pair the parallel ``learn-inject``
#      worktree writes — the verbatim injected text (markdown block) and the
#      sources it was assembled from (list items).
#   2. Role-targeted ``operator_steering`` rows — the existing
#      ``_fetch_steer_rows`` discipline, retitled to "Pending steering · [sev]"
#      so they can't be confused with already-injected steering.
#   3. Run-scoped gradients whose evidence trace is THIS run AND starts with
#      ``f"tr-{node.type}-{node.id}-"`` — replaces the old time-window query
#      that leaked gradients across concurrent same-class runs.
#
# The view returns ``{"list_title", "list", "markdown"?: ...}`` — ``markdown``
# is OPTIONAL (omitted when no ``.md`` file exists, so the Zed panel can tell
# "no injected text" from "injected empty text"). All other fields stay
# additive over the kickoff's pre-fix view, no consumer has to change.

_LEARNED_NODE_TYPES = frozenset({"researcher", "implementer", "reviewer"})
_LEARNED_GRADIENT_LIMIT = 10
_PENDING_STEER_LIMIT = 5


def _learned_record(run_dir: Path, node_id: str) -> dict[str, Any] | None:
    """Read ``learned/<node_id>.json`` — the per-node injection record.

    Returns ``None`` if the file is missing or unparseable. Tolerates a
    missing ``learned/`` directory (the producer's contract; runs from
    before that producer existed have no per-node file).
    """
    path = run_dir / "learned" / f"{node_id}.json"
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except (json.JSONDecodeError, OSError):
        return None
    return data if isinstance(data, dict) else None


def _learning_md_block(run_dir: Path, node_id: str) -> dict[str, Any] | None:
    """Build the optional top-level ``markdown`` block from ``learned/<id>.md``.

    Returns ``None`` when the file is missing — ``build_node`` then omits the
    top-level ``markdown`` key entirely (kickoff §1: the key's *absence* is
    the "nothing was injected" signal).
    """
    path = run_dir / "learned" / f"{node_id}.md"
    if not path.is_file():
        return None
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    return {"title": "Injected into this node's prompt",
            "text": text[:MD_FILE_CAP],
            "path": str(path)}


def _learning_sources_items(record: dict[str, Any]) -> list[dict[str, Any]]:
    """Render per-source ``S.item`` rows from a learned-record.

    Three source kinds (gradient / pattern / steering) plus the
    ``injected: false`` short-circuits. Empty sources + ``injected: true``
    yields no items — the record was injected but matched nothing.
    """
    out: list[dict[str, Any]] = []
    if not bool(record.get("injected", True)):
        reason = str(record.get("reason") or "")
        if reason == "opt-out":
            sub = "MO_INJECT_LEARNINGS=0 for this run"
        elif reason == "nothing matched":
            sub = ("no learned failure modes or lessons matched "
                   "this task class")
        else:
            sub = reason or "injection was disabled for this run"
        out.append(S.item("Nothing injected", sub))
        return out
    sources = record.get("sources")
    if not isinstance(sources, list):
        return out
    for src in sources:
        if not isinstance(src, dict):
            continue
        kind = str(src.get("kind") or "")
        if kind == "gradient":
            signal = str(src.get("signal") or "")[:200]
            fix = str(src.get("suggested_change") or "")[:200]
            target = str(src.get("target") or "")
            sub = f"fix: {fix}"
            if target:
                sub = f"{sub} · {target}"
            out.append(S.item(signal, sub, m="✦", mc="purple"))
        elif kind == "pattern":
            text = str(src.get("text") or "")[:200]
            sub = f"pattern {str(src.get('id') or '')}".strip()
            out.append(S.item(text, sub, m="◆", mc="purple"))
        elif kind == "steering":
            message = str(src.get("message") or "")
            severity = str(src.get("severity") or "info")
            source = str(src.get("source") or "")
            sub = f"steering · {severity} · from {source}"
            out.append(S.item(message, sub, m="→", mc="blue"))
    return out


def _learning_learned_from(run: Run, node: Node) -> list[dict[str, Any]]:
    """Run-scoped gradients for this node — JOIN through ``execution_traces``.

    Kickoff §5 — gradients whose evidence trace is in *this* run AND starts
    with ``f"tr-{node.type}-{node.id}-"``. The prefix filter happens in
    Python because node ids contain ``_`` (a SQL ``LIKE`` wildcard) and a
    naive ``LIKE 'tr-implementer-implementer-%'`` would over-match
    ``implementerX``. Tolerates a missing ``execution_traces`` OR
    ``gradient_records`` table — returns an empty list rather than
    blanking the panel.
    """
    out: list[dict[str, Any]] = []
    if not run.row.get("id"):
        return out
    try:
        from mini_ork.web.deps import db_for

        db = db_for(run.home)
        if not db.has_table("gradient_records"):
            return out
        if not db.has_table("execution_traces"):
            return out
        prefix = f"tr-{node.type}-{node.id}-"
        rows = db.rows(
            "SELECT g.gradient_id, g.target, g.signal, g.suggested_change, "
            "       g.confidence, g.created_at, t.trace_id "
            "FROM gradient_records g "
            "JOIN execution_traces t ON t.trace_id = g.evidence "
            "WHERE t.run_id = ? "
            "ORDER BY g.created_at DESC",
            (run.id,),
        )
        for g in rows or []:
            trace_id = str(g.get("trace_id") or "")
            if not trace_id.startswith(prefix):
                continue
            try:
                c = float(g.get("confidence") or 0.0)
            except (TypeError, ValueError):
                c = 0.0
            sig = str(g.get("signal") or "")[:200]
            sc = str(g.get("suggested_change") or "")[:160]
            out.append(S.item(
                sig,
                f"fix: {sc} · confidence {c:.2f}",
                m="✦", mc="green",
                acts=[S.btn("Open", S.open_path(str(run.home / "runs" / run.id)), "ghost")],
            ))
            if len(out) >= _LEARNED_GRADIENT_LIMIT:
                break
    except Exception:  # noqa: BLE001 — silent table-missing tolerance
        return out
    return out


def _learning_view(run: Run, node: Node) -> dict[str, Any]:
    """Per-node Learning tab — what was injected, what was learned.

    Returns ``{"list_title": "Learning", "list": [...], "markdown"?: {...}}``.
    The ``markdown`` key is included ONLY when ``learned/<id>.md`` exists
    (Zed ``render_facts`` reads it as a top-level kv block).
    """
    out: list[dict[str, Any]] = []

    # Sources list — one row per source in the .json record, or a single
    # "Nothing injected" / "Not recorded" / "No learning is injected" row
    # when there is no record.
    md_block = _learning_md_block(run.run_dir, node.id)
    record = _learned_record(run.run_dir, node.id)
    if record is not None:
        out.extend(_learning_sources_items(record))
    elif node.type in _LEARNED_NODE_TYPES:
        out.append(S.item("Not recorded",
                         "this run predates per-node learning records"))
    else:
        out.append(S.item("No learning is injected into this node type",
                         str(node.type or "shell")))

    # Pending steering — same role-scoped read as before, retitled so it
    # can't be confused with steering that the .json record injected above.
    try:
        steer_rows = _fetch_steer_rows(run.id, run.home,
                                       role=_role_for_node(node)) or []
    except Exception:  # noqa: BLE001
        steer_rows = []
    for r in steer_rows[:_PENDING_STEER_LIMIT]:
        sev = str(r.get("severity") or "info")
        msg = str(r.get("message") or "")
        if not msg:
            continue
        out.append(S.item(f"Pending steering · [{sev}]", msg, m="→", mc="blue"))

    # Learned from this node — run-scoped gradients joined on the trace id.
    learned = _learning_learned_from(run, node)
    if learned:
        out.extend(learned)
    elif node.state in ("done", "failed"):
        out.append(S.item(
            "Nothing learned from this node yet",
            "reflection writes gradients after the run's last node",
        ))

    if not out:
        out.append(S.item("No learning signals yet.", ""))

    result: dict[str, Any] = {"list_title": "Learning", "list": out}
    if md_block is not None:
        result["markdown"] = md_block
    return result


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
    command_backed_used = False
    command_backed_no_artefacts = False  # helper returned None (vs. []-at-end of stream)
    command_rc: int | None = None
    command_source_name = ""
    if view == "stream":
        # r3 fix #7 minor: ``finished · N events`` reports the TOTAL
        # transcript entry count, not this poll's slice. Parse the source
        # once for the count, then again inside ``_stream_entries`` for
        # the per-poll slice — both reads are O(file size).
        if session_path is not None:
            transcript_entry_count = len(_session_entries(session_path))
        elif log_path is not None:
            # r4 fix #2: status pill counts emit-cap-violating entries too —
            # a 45-line log reports "finished · 45 events", not "… 40 events"
            # from the SHELL_LOG_LINES cap.
            transcript_entry_count = _log_line_count(log_path)

        # Kickoff ide-node-commands §2 — non-agent branch. When there is
        # no session transcript AND the node has no llm_calls OR its
        # ``type`` is one of the deterministic shapes (verifier, publisher,
        # rollback, shell, gate, transform), build the stream from
        # commands. Additive with the existing log-line path: when no
        # command artefacts exist, fall back transparently.
        command_backed_branch = _is_command_backed(
            target, session_path, log_path is not None,
        )
        if command_backed_branch:
            cmd = _command_stream_entries(
                run_dir, target, log_path,
                getattr(run_obj, "recipe_dir", None),
                home=home,
                offset=int(offset),
            )
            if cmd is not None:
                stream_entries, next_offset, command_source_name = cmd
                command_backed_used = True
                # From the record, not the first entry: a poll past offset 0
                # does not carry the ``$`` entry.
                command_rc = _command_rc(run_dir, target)
                # Non-agent nodes take no operator steering (steers target
                # agent roles), so command-backed streams carry none.
            else:
                command_backed_no_artefacts = True

        if not command_backed_used:
            stream_entries, next_offset = _stream_entries(
                session_path, log_path, run_id, home,
                target=target, offset=int(offset),
                run_dir=run_dir,
                transcript_has_result=transcript_has_result,
                is_live=is_live,
            )
        # Kickoff §2e — non-agent node with NO session, NO log, NO
        # command artefacts → one note entry. Fires only on the first
        # poll (``offset == 0``) when the helper returned ``None`` —
        # polling past the end of a real stream must NOT re-emit this
        # note, otherwise the consumer would see it duplicate with each
        # poll (r2 kickoff fix #4 — append-only offset).
        if (command_backed_no_artefacts and not stream_entries
                and session_path is None and log_path is None
                and int(offset) == 0):
            note_text = "No command or output was stored for this node."
            stream_entries = [{
                "k": _KIND_NOTE,
                "head": "empty",
                "arg": note_text,
                "lines": [{"t": note_text, "c": "muted"}],
                "_src": "note",
                "_line": 0,
            }]
            next_offset = 0

    status, status_c = _stream_status(target, session_path is not None,
                                      log_path is not None, is_live,
                                      transcript_entry_count)
    # Kickoff ide-node-commands §2f — override the pill when the command
    # branch owns the stream. r2 (fix #5): widen to a non-zero rc, an
    # evidence ``pass: false``, or a failed node state — and never let the
    # underlying ``_stream_status`` emit ``"no transcript"`` when the
    # command branch already produced entries.
    # The pill describes the whole stream (``next_offset`` = total entries),
    # not this poll's slice, which is empty once the client has caught up.
    if command_backed_used and next_offset > 0:
        node_failed = (target.state == "failed")
        # r2 kickoff fix #5 — never let ``command_rc == 0`` mask a refuted
        # verifier verdict. The pill widens to red whenever rc != 0, the
        # verifier wrote ``{"pass": false}`` in its evidence, or the node
        # state is failed. When none of those trip, default to green so
        # ``_stream_status`` cannot leak ``"no transcript"`` while entries
        # exist.
        ev_pass = _verifier_pass_flag(run_dir, stem=_verifier_stem(target))
        red = (
            (command_rc is not None and command_rc != 0)
            or ev_pass is False
            or node_failed
        )
        if red:
            status, status_c = ("failed · command", "red")
        else:
            status, status_c = ("finished · command", "green")
    elif command_backed_used:
        # §2e — single empty-note pill state.
        status, status_c = ("no command recorded", "yellow")

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
            "source": str(command_source_name or (
                session_path.name if session_path else (
                    log_path.name if log_path else ""))),
            "done_note": _done_note(target),
        })
    elif view == "output":
        base.update(_output_view(run_dir, target))
    elif view == "prompt":
        base.update(_prompt_view(target, session_path, run_obj.recipe_dir))
    elif view == "telemetry":
        base.update(_telemetry_view(run_obj, target))
    elif view == "learning":
        base.update(_learning_view(run_obj, target))
    elif view == "changes":
        base.update(build_changes_view(run_obj, target))
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
