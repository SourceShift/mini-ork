"""Assemble a ``mini-ork.public-run/v1`` bundle from a finished run.

The bundle is what a public share page renders: the objective, the node DAG,
the verification envelope, and per-agent summaries. The conversation
transcripts are *not* in it — they travel beside it and are fetched per agent,
so the page stays small no matter how long the run was.

Nothing here invents data. Every field is read from a file the run wrote or
computed from those files; a missing file becomes ``null``/``[]`` and the page
renders "not recorded", never a fabricated pass.

Owner of the format: ``mini-ork.public-run/v1``. The consumer's validator is
``server/publicruns/schema.ts``; a change here is a change there.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from .redact import RedactionReport, redact_value

__all__ = ["SCHEMA", "build_bundle", "collect_bundle"]

SCHEMA = "mini-ork.public-run/v1"

# Per-message and per-agent caps. A share page is a reading surface, not a
# backup: a 200k-line reviewer transcript is truncated at a marked boundary
# rather than shipped whole.
MAX_MESSAGE_CHARS = 4_000
MAX_AGENT_BYTES = 2_000_000
MAX_TOTAL_TRANSCRIPT_BYTES = 6_000_000
MAX_DESCRIPTION_CHARS = 1_500
MAX_KICKOFF_CHARS = 3_000

_AGENT_GLOB = re.compile(r"^agent-(?P<role>[A-Za-z0-9_.\-]+)\.(?:live|stream)\.jsonl$")
_ISSUE_URL_RE = re.compile(
    r"https://github\.com/[A-Za-z0-9_.\-]+/[A-Za-z0-9_.\-]+/(?:issues|pull)/\d+"
)
_VERDICT_MAP = {
    "pass": "PROVEN", "passed": "PROVEN", "approve": "PROVEN",
    "approved": "PROVEN", "publish": "PROVEN", "success": "PROVEN",
    "fail": "REFUTED", "failed": "REFUTED", "refute": "REFUTED",
    "refuted": "REFUTED", "abstain": "UNVERIFIED", "withhold": "UNVERIFIED",
    "unverified": "UNVERIFIED",
}


# ── small readers ───────────────────────────────────────────────────────────

def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _read_text(path: Path, limit: int | None = None) -> str:
    try:
        with path.open(encoding="utf-8", errors="replace") as fh:
            return fh.read(limit) if limit else fh.read()
    except OSError:
        return ""


def _iso(ts: float | None) -> str | None:
    if ts is None:
        return None
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def _git(repo: str | None, *args: str) -> str | None:
    if not repo:
        return None
    try:
        out = subprocess.run(
            ["git", "-C", repo, *args],
            capture_output=True, text=True, timeout=5, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    value = out.stdout.strip()
    return value or None


def _normalise(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name.lower())


def _clip(text: str, limit: int) -> str:
    text = text.strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


# ── verdict ─────────────────────────────────────────────────────────────────

def _read_verdict_run_dir(run_dir: Path) -> dict[str, Any]:
    """Precedence matches ``mini_ork.scheduler``: run-verdict, verdict, panel."""
    for name in ("run-verdict.json", "verdict.json", "panel-verdict.json"):
        data = _read_json(run_dir / name)
        if data is not None:
            return {"file": name, **data}
    return {}


def _public_verdict(data: dict[str, Any]) -> str:
    levels_decision = str(data.get("levels_decision") or "").lower()
    if levels_decision == "withhold" and not data.get("levels_ok"):
        return "UNVERIFIED"
    raw = str(data.get("verdict") or "").lower()
    return _VERDICT_MAP.get(raw, "UNVERIFIED")


# ── transcripts ─────────────────────────────────────────────────────────────

def _content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "text":
            parts.append(str(block.get("text") or ""))
        elif block.get("type") == "tool_result":
            inner = block.get("content")
            parts.append(_content_text(inner) if not isinstance(inner, str) else inner)
    return "\n".join(p for p in parts if p)


def _normalise_message(inner: Any) -> dict[str, Any] | None:
    """One SDK/ACP message → the compact shape the share page renders."""
    if not isinstance(inner, dict):
        return None
    kind = inner.get("type")
    if kind == "system":
        if inner.get("subtype") != "init":
            return None
        return {
            "role": "system",
            "model": inner.get("model"),
            "cwd": inner.get("cwd"),
            "tools": inner.get("tools") or [],
        }
    if kind in ("stream_event", "tool_progress", "rate_limit_event",
                "rate_limit", "hook_response"):
        return None
    if kind in ("assistant", "user"):
        raw_message = inner.get("message")
        message: dict[str, Any] = raw_message if isinstance(raw_message, dict) else {}
        body = message.get("content", inner.get("content"))
        blocks: list[dict[str, Any]] = []
        if isinstance(body, str):
            blocks.append({"type": "text", "text": _clip(body, MAX_MESSAGE_CHARS)})
        elif isinstance(body, list):
            for block in body:
                if not isinstance(block, dict):
                    continue
                btype = block.get("type")
                if btype == "text":
                    value = _clip(str(block.get("text") or ""), MAX_MESSAGE_CHARS)
                    if value:
                        blocks.append({"type": "text", "text": value})
                elif btype == "thinking":
                    blocks.append({"type": "thinking", "text": ""})
                elif btype == "tool_use":
                    blocks.append({
                        "type": "tool_use",
                        "name": block.get("name"),
                        "input": block.get("input"),
                    })
                elif btype == "tool_result":
                    text = _clip(_content_text(block.get("content")), MAX_MESSAGE_CHARS)
                    blocks.append({
                        "type": "tool_result",
                        "tool_use_id": block.get("tool_use_id"),
                        "is_error": bool(block.get("is_error")),
                        "text": text,
                    })
        if not blocks:
            return None
        return {"role": kind, "blocks": blocks, "ts": inner.get("timestamp")}
    if kind == "result":
        raw_usage = inner.get("usage")
        usage: dict[str, Any] = raw_usage if isinstance(raw_usage, dict) else {}
        return {
            "role": "result",
            "cost_usd": inner.get("total_cost_usd"),
            "duration_ms": inner.get("duration_api_ms"),
            "turns": inner.get("num_turns"),
            "usage": {
                "input_tokens": usage.get("input_tokens"),
                "output_tokens": usage.get("output_tokens"),
                "cache_read_input_tokens": usage.get("cache_read_input_tokens"),
            },
        }
    return None


def _read_transcript(path: Path, budget: int) -> tuple[list[dict[str, Any]], int, bool]:
    """Parse one ``agent-*.live.jsonl``; stop at *budget* bytes."""
    messages: list[dict[str, Any]] = []
    used = 0
    truncated = False
    try:
        fh = path.open(encoding="utf-8", errors="replace")
    except OSError:
        return [], 0, False
    with fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except ValueError:
                continue
            inner = row
            if isinstance(row, dict) and isinstance(row.get("line"), str):
                try:
                    inner = json.loads(row["line"])
                except ValueError:
                    continue
            message = _normalise_message(inner)
            if message is None:
                continue
            size = len(json.dumps(message, ensure_ascii=False))
            if used + size > budget:
                truncated = True
                break
            used += size
            messages.append(message)
    if truncated:
        messages.append({
            "role": "notice",
            "blocks": [{"type": "text",
                        "text": f"[transcript truncated at {budget} bytes]"}],
        })
    return messages, used, truncated


def _agent_files(run_dir: Path) -> list[tuple[str, Path]]:
    found: list[tuple[str, Path]] = []
    for path in sorted(run_dir.glob("agent-*.jsonl")):
        m = _AGENT_GLOB.match(path.name)
        if not m:
            continue
        role = m.group("role")
        if role.startswith("post-run"):
            continue
        found.append((role, path))
        # ``agent-x.live.jsonl.1`` continuation files are merged by the
        # ``.1`` suffix on the role; keep them adjacent to the base role.
    return found


def _continuation_files(run_dir: Path, role: str) -> list[Path]:
    return sorted(run_dir.glob(f"agent-{role}.live.jsonl.*"))


def _summarise_messages(messages: Iterable[dict[str, Any]]) -> dict[str, Any]:
    turns = tool_calls = 0
    model = None
    cost = 0.0
    duration_ms = 0
    for message in messages:
        role = message.get("role")
        if role == "assistant":
            turns += 1
            for block in message.get("blocks") or []:
                if block.get("type") == "tool_use":
                    tool_calls += 1
        elif role == "system" and message.get("model"):
            model = message["model"]
        elif role == "result":
            if isinstance(message.get("cost_usd"), (int, float)):
                cost += float(message["cost_usd"])
            if isinstance(message.get("duration_ms"), (int, float)):
                duration_ms += int(message["duration_ms"])
    return {
        "turns": turns,
        "tool_calls": tool_calls,
        "model": model,
        "cost_usd": round(cost, 6) if cost else None,
        "duration_ms": duration_ms or None,
    }


# ── verification ────────────────────────────────────────────────────────────

def _verifier_envelopes(run_dir: Path) -> list[dict[str, Any]]:
    envelopes: list[dict[str, Any]] = []
    for path in sorted(run_dir.glob("verifier_*.json")):
        data = _read_json(path)
        if data is None:
            continue
        raw_checks = data.get("checks")
        checks: list[Any] = raw_checks if isinstance(raw_checks, list) else []
        raw_failed = data.get("failed_checks")
        failed: list[Any] = raw_failed if isinstance(raw_failed, list) else []
        envelopes.append({
            "id": path.stem.removeprefix("verifier_"),
            "verdict": data.get("verdict"),
            "pass": data.get("pass"),
            "checks_run": data.get("checks_run") or [c.get("name") for c in checks if isinstance(c, dict)],
            "failed_checks": failed,
            "reasons": (data.get("reasons") or [])[:10],
            "checks": [
                {
                    "name": c.get("name"),
                    "pass": c.get("pass"),
                    "expected": _clip(str(c.get("expected") or ""), 300),
                    "actual": _clip(str(c.get("actual") or ""), 300),
                }
                for c in checks[:60] if isinstance(c, dict)
            ],
        })
    return envelopes


def _instruments(run_dir: Path) -> list[dict[str, Any]]:
    data = _read_json(run_dir / "verification_manifest.json")
    if not data or not isinstance(data.get("instruments"), list):
        return []
    return [
        {
            "instrument": item.get("instrument"),
            "status": item.get("status"),
            "knob": item.get("knob") or "",
            "detail": _clip(str(item.get("detail") or ""), 300),
        }
        for item in data["instruments"] if isinstance(item, dict)
    ]


# ── DAG ─────────────────────────────────────────────────────────────────────

def _node_evidence(run_dir: Path, node_id: str, node_type: str | None = None) -> list[str]:
    """Relative artifact names attributable to a node.

    Two levels, in decreasing confidence: the node's *id* appears in the file
    name, or its *node_type* does (``implementer`` → ``impl-implementer.log``,
    ``agent-implementer.live.jsonl``, ``implementer-summary.json``). The
    type-level match is coarser — two ``verifier`` nodes share the same
    envelopes — so the file names ship with the bundle and the page shows
    them; a reader can see what the evidence actually is.
    """
    key = _normalise(node_id)
    type_key = _normalise(node_type or "")
    hits: list[str] = []
    for path in run_dir.iterdir():
        if not path.is_file():
            continue
        name = path.name
        if name in (f"verifier_{node_id}.json", f"impl-{node_id}.log"):
            hits.append(name)
            continue
        stem = f"-{_normalise(name)}-"
        if key and f"-{key}-" in stem:
            hits.append(name)
            continue
        m = _AGENT_GLOB.match(name)
        if m and _normalise(m.group("role")) == key:
            hits.append(name)
            continue
        if type_key and type_key != "node" and f"-{type_key}-" in stem:
            hits.append(name)
    return sorted(hits)[:20]


def _failed_node_ids(run_dir: Path, run_verdict: dict[str, Any]) -> set[str]:
    """Best-effort per-node failure set.

    ``run-verdict.json`` records only a *count* (``failed_nodes``), so the node
    names come from the places that do name one: the terminal rows under
    ``retry-exhausted/`` and ``execute.log``'s ``node=… fail`` lines. A node is
    never invented — an empty set just means the page shows no red node.

    Only a terminal row counts. ``retry-exhausted/`` holds *two* shapes — the
    ``<node>.error.json`` terminal row (``marker: retry_exhausted``) and the
    ``<node>.attempts.json`` ledger of every attempt, successes included — so
    the directory's presence proves nothing and the payload has to be read.
    """
    failed: set[str] = set()
    explicit = run_verdict.get("failed_node_ids")
    if isinstance(explicit, list):
        failed.update(str(n) for n in explicit)
    retry_dir = run_dir / "retry-exhausted"
    if retry_dir.is_dir():
        for path in retry_dir.glob("*.json"):
            if path.name.endswith(".attempts.json"):
                continue
            data = _read_json(path) or {}
            if data.get("marker") != "retry_exhausted":
                continue
            node = str(data.get("node_id") or path.name.split(".json", 1)[0])
            if node:
                failed.add(node)
    log = _read_text(run_dir / "execute.log")
    for match in re.finditer(
        r"(?im)^.*\bnode[=:\s]+[\"']?([A-Za-z0-9_.\-]+)[\"']?\b.*"
        r"\b(?:fail|failed|error|abort|aborted)\b.*$",
        log,
    ):
        failed.add(match.group(1))
    return failed


def _dag(plan: dict[str, Any], run_verdict: dict[str, Any], run_dir: Path) -> dict[str, Any]:
    decomposition = plan.get("decomposition")
    if not isinstance(decomposition, list):
        decomposition = []
    failed = _failed_node_ids(run_dir, run_verdict)
    failed_count = run_verdict.get("failed_nodes")
    nodes: list[dict[str, Any]] = []
    ids: set[str] = set()
    for item in decomposition:
        if not isinstance(item, dict):
            continue
        node_id = str(item.get("id") or item.get("node_id") or "").strip()
        if not node_id:
            continue
        ids.add(node_id)
        node_type = item.get("node_type") or item.get("type") or "node"
        evidence = _node_evidence(run_dir, node_id, node_type)
        if node_id in failed:
            status = "failed"
        elif evidence:
            status = "done"
        else:
            status = "unknown"
        nodes.append({
            "id": node_id,
            "type": node_type,
            "description": _clip(str(item.get("description") or ""), MAX_DESCRIPTION_CHARS),
            "depends_on": [str(d) for d in (item.get("depends_on") or [])],
            "status": status,
            "artifacts": evidence,
        })
    edges = [
        {"from": dep, "to": node["id"]}
        for node in nodes for dep in node["depends_on"] if dep in ids
    ]
    return {
        "nodes": nodes,
        "edges": edges,
        "dispatched": run_verdict.get("dispatched"),
        "failed_nodes": failed_count,
    }


# ── run metadata ────────────────────────────────────────────────────────────

def _title_and_issue(kickoff: str, profile: dict[str, Any], plan: dict[str, Any]) -> tuple[str, str | None]:
    title = ""
    for line in kickoff.splitlines():
        if line.startswith("# "):
            title = line[2:].strip()
            break
    if not title:
        title = str(profile.get("user_goal") or "").strip()
    if not title:
        title = _clip(str(plan.get("objective") or "mini-ork run"), 120)
    match = _ISSUE_URL_RE.search(kickoff) or _ISSUE_URL_RE.search(str(plan.get("objective") or ""))
    return _clip(title, 160), (match.group(0) if match else None)


def _timing(run_dir: Path) -> tuple[str | None, int | None]:
    try:
        st = run_dir.stat()
        start = getattr(st, "st_birthtime", None) or st.st_ctime
        end = st.st_mtime
    except OSError:
        return None, None
    if end < start:
        return _iso(start), None
    return _iso(start), int(end - start)


def collect_bundle(
    run_dir: Path | str,
    *,
    include_transcripts: bool = True,
    max_agent_bytes: int = MAX_AGENT_BYTES,
    max_total_bytes: int = MAX_TOTAL_TRANSCRIPT_BYTES,
    title: str | None = None,
) -> tuple[dict[str, Any], dict[str, list[dict[str, Any]]], RedactionReport]:
    """Build ``(bundle, transcripts, report)`` for *run_dir*.

    The bundle is already redacted; the transcripts are returned separately and
    are redacted here too, because both leave the machine together.
    """
    run_dir = Path(run_dir).expanduser().resolve()
    profile = _read_json(run_dir / "run_profile.json") or {}
    plan = _read_json(run_dir / "plan.json") or {}
    verdict = _read_verdict_run_dir(run_dir)
    kickoff = _read_text(run_dir / "kickoff.md", MAX_KICKOFF_CHARS * 4)
    raw_roots = profile.get("roots")
    roots: dict[str, Any] = raw_roots if isinstance(raw_roots, dict) else {}
    raw_target = roots.get("target")
    target = raw_target if isinstance(raw_target, str) else None
    base_sha = _read_text(run_dir / "pre-implementer-ref").strip() or None
    head_sha = _git(target, "rev-parse", "HEAD")
    remote = _git(target, "remote", "get-url", "origin")

    computed_title, issue_url = _title_and_issue(kickoff, profile, plan)
    started_at, duration_s = _timing(run_dir)

    transcripts: dict[str, list[dict[str, Any]]] = {}
    agent_summaries: list[dict[str, Any]] = []
    total_transcript_bytes = 0
    for role, path in _agent_files(run_dir):
        budget = max(0, min(max_agent_bytes, max_total_bytes - total_transcript_bytes))
        messages, used, truncated = ([], 0, False)
        if include_transcripts and budget > 0:
            messages, used, truncated = _read_transcript(path, budget)
            for extra in _continuation_files(run_dir, role):
                if used >= budget:
                    truncated = True
                    break
                more, more_used, more_trunc = _read_transcript(extra, budget - used)
                messages.extend(more)
                used += more_used
                truncated = truncated or more_trunc
        total_transcript_bytes += used
        summary = _summarise_messages(messages)
        agent_id = _normalise(role)
        if include_transcripts and messages:
            transcripts[agent_id] = messages
        agent_summaries.append({
            "id": agent_id,
            "role": role,
            "model": summary["model"] or (profile.get("provider_policy", {}) or {}).get("lanes", {}).get(role),
            "turns": summary["turns"],
            "tool_calls": summary["tool_calls"],
            "cost_usd": summary["cost_usd"],
            "duration_ms": summary["duration_ms"],
            "transcript_bytes": used,
            "transcript_messages": len(messages),
            "truncated": truncated,
        })

    total_cost = sum(a["cost_usd"] for a in agent_summaries if a["cost_usd"]) or None
    models = sorted({a["model"] for a in agent_summaries if a["model"]})

    bundle: dict[str, Any] = {
        "schema": SCHEMA,
        "run": {
            "id": run_dir.name,
            "title": _clip(title, 160) if title else computed_title,
            "recipe": profile.get("recipe"),
            "task_class": profile.get("task_class") or plan.get("task_class"),
            "objective": _clip(str(plan.get("objective") or profile.get("user_goal") or ""), 4_000),
            "user_goal": str(profile.get("user_goal") or "")[:500] or None,
            "verdict": _public_verdict(verdict),
            "verdict_raw": verdict.get("verdict"),
            "verdict_source": verdict.get("file"),
            "levels": verdict.get("levels") or {},
            "levels_reasons": verdict.get("levels_reasons") or {},
            "levels_required": verdict.get("levels_required") or [],
            "levels_ok": verdict.get("levels_ok"),
            "levels_decision": verdict.get("levels_decision"),
            "success_criteria": [str(c) for c in (profile.get("success_criteria") or [])][:20],
            "repo": {
                "remote": remote,
                "base": base_sha,
                # The target checkout's HEAD *when this bundle was built* — not
                # the run's result commit (the run does not always commit). The
                # name says so, because a reader will otherwise assume "head".
                "target_head_at_publish": head_sha,
            },
            "external_url": issue_url,
            "created_at": started_at,
            "duration_s": duration_s,
            "cost_usd": round(total_cost, 6) if total_cost else None,
            "models": models,
        },
        "kickoff": {
            "excerpt": _clip(kickoff, MAX_KICKOFF_CHARS),
            "url": issue_url,
        },
        "dag": _dag(plan, verdict, run_dir),
        "verification": {
            "manifest": _instruments(run_dir),
            "verifiers": _verifier_envelopes(run_dir),
        },
        "agents": agent_summaries,
        "redaction": {},
        "producer": {
            "tool": "mini-ork",
            "published_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        },
    }

    report = RedactionReport()
    bundle = redact_value(bundle, report)
    transcripts = redact_value(transcripts, report)
    bundle["redaction"] = report.to_dict()
    return bundle, transcripts, report


def build_bundle(*args: Any, **kwargs: Any):
    """Alias kept for callers that only want the bundle."""
    return collect_bundle(*args, **kwargs)


def discover_run_dir(run_id: str, home: str | os.PathLike[str] | None = None) -> Path:
    """Resolve ``<run_id>`` under the mini-ork home, or an existing path."""
    candidate = Path(run_id).expanduser()
    if candidate.is_dir():
        return candidate
    base = Path(home).expanduser() if home else Path(
        os.environ.get("MINI_ORK_HOME") or ".mini-ork"
    ).expanduser()
    run_dir = base / "runs" / run_id
    if run_dir.is_dir():
        return run_dir
    raise FileNotFoundError(f"run directory not found: {run_dir}")
