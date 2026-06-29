"""Adapters for Codex, Claude Code, and mini-ork trajectory artifacts."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from .models import Episode, Labels, Outcome, Step
from .redact import redact_text


def discover_files(roots: Iterable[Path]) -> list[Path]:
    files: list[Path] = []
    for root in roots:
        if not root.exists():
            continue
        if root.is_file():
            files.append(root)
            continue
        for pattern in ("**/*.jsonl", "**/COMPLETION_REPORT.md", "**/*summary*.json", "**/*verdict*.json"):
            files.extend(path for path in root.glob(pattern) if path.is_file())
    return sorted(set(files))


def load_jsonl(path: Path, limit: int | None = None) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            if not line.strip():
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
            if limit and len(rows) >= limit:
                break
    return rows


def parse_file(path: Path, max_events: int | None = None) -> Episode | None:
    if path.suffix == ".jsonl":
        rows = load_jsonl(path, max_events)
        if not rows:
            return None
        if ".codex" in str(path) or path.name.startswith("rollout-"):
            return _parse_codex_jsonl(path, rows)
        return _parse_claude_jsonl(path, rows)
    return _parse_mini_ork_artifact(path)


def _episode_id(path: Path, rows: list[dict[str, Any]] | None = None) -> str:
    h = hashlib.sha256()
    h.update(str(path).encode("utf-8"))
    if rows:
        h.update(json.dumps(rows[:3], sort_keys=True, default=str).encode("utf-8"))
    return "ep_" + h.hexdigest()[:16]


def _repo_from_cwd(cwd: str) -> str:
    low = cwd.lower()
    if "researcher" in low:
        return "researcher"
    if "mini-ork" in low:
        return "mini-ork"
    if "contextnest" in low:
        return "contextnest"
    return "other" if cwd else "unknown"


def _classify_task(goal: str) -> str:
    low = goal.lower()
    if "review" in low:
        return "code_review"
    if "fix" in low or "bug" in low:
        return "bug_fix"
    if "doc" in low or "write" in low or "plan" in low:
        return "docs"
    if "train" in low or "model" in low or "dataset" in low:
        return "training"
    return "unknown"


def _parse_codex_jsonl(path: Path, rows: list[dict[str, Any]]) -> Episode:
    steps: list[Step] = []
    user_goal = ""
    cwd = ""
    started = rows[0].get("timestamp")
    ended = rows[-1].get("timestamp")
    for row in rows:
        payload = row.get("payload") or {}
        if row.get("type") == "turn_context":
            cwd = payload.get("cwd") or cwd
        text = _extract_text(payload)
        if not text:
            continue
        role = payload.get("role") or payload.get("type") or row.get("type", "event")
        if role == "user" and not user_goal:
            user_goal = redact_text(text, 1000)
        kind = _step_kind(text, payload)
        steps.append(
            Step(
                index=len(steps),
                actor="user" if role == "user" else "assistant" if role in {"assistant", "message"} else "tool",
                action_kind=kind,
                summary=redact_text(text.replace("\n", " "), 1200),
                command=_extract_command(payload),
                grounded_in_evidence=kind in {"read", "search", "test", "edit"},
                noisy=len(text) > 8000,
            )
        )
    return _finish_episode(path, "codex_jsonl", rows, cwd, user_goal, steps, started, ended)


def _parse_claude_jsonl(path: Path, rows: list[dict[str, Any]]) -> Episode:
    steps: list[Step] = []
    user_goal = ""
    cwd = ""
    started = rows[0].get("timestamp")
    ended = rows[-1].get("timestamp")
    for row in rows:
        cwd = row.get("cwd") or cwd
        msg = row.get("message") or {}
        role = msg.get("role") or row.get("type", "event")
        text = _extract_text(msg) or _extract_text(row)
        if not text:
            continue
        if role == "user" and not user_goal:
            user_goal = redact_text(text, 1000)
        kind = _step_kind(text, msg)
        steps.append(
            Step(
                index=len(steps),
                actor="user" if role == "user" else "assistant" if role == "assistant" else "tool",
                action_kind=kind,
                summary=redact_text(text.replace("\n", " "), 1200),
                command=_extract_command(msg),
                grounded_in_evidence=kind in {"read", "search", "test", "edit"},
                noisy=len(text) > 8000,
            )
        )
    return _finish_episode(path, "claude_jsonl", rows, cwd, user_goal, steps, started, ended)


def _parse_mini_ork_artifact(path: Path) -> Episode | None:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    if not text.strip():
        return None
    goal = path.stem.replace("-", " ")
    step = Step(
        index=0,
        actor="tool",
        action_kind="artifact",
        summary=redact_text(text.replace("\n", " "), 1400),
        grounded_in_evidence=True,
    )
    outcome = Outcome(status="completed" if "APPROVE" in text or "completed" in text.lower() else "partial")
    return Episode(
        episode_id=_episode_id(path),
        source="mini_ork_run",
        source_path=str(path),
        cwd=str(path.parent),
        repo=_repo_from_cwd(str(path)),
        user_goal=goal,
        task_type=_classify_task(goal),
        scope="docs",
        steps=[step],
        outcome=outcome,
        labels=Labels(should_imitate=outcome.status == "completed", process_score=0.4),
    )


def _finish_episode(
    path: Path,
    source: str,
    rows: list[dict[str, Any]],
    cwd: str,
    user_goal: str,
    steps: list[Step],
    started: str | None,
    ended: str | None,
) -> Episode:
    status = "completed" if any(s.action_kind == "final" for s in steps) else "partial"
    tests = [s.command or s.summary for s in steps if s.action_kind == "test"][:20]
    labels = Labels(
        should_imitate=status == "completed" and bool(tests),
        useful_skill_candidates=_candidate_skill_lines(steps),
        failure_modes=_failure_modes(steps),
        process_score=min(1.0, 0.2 + (0.2 if tests else 0.0) + (0.2 if status == "completed" else 0.0)),
        cost_efficiency_score=0.5,
    )
    return Episode(
        episode_id=_episode_id(path, rows),
        source=source,
        source_path=str(path),
        cwd=cwd,
        repo=_repo_from_cwd(cwd or str(path)),
        started_at=started,
        ended_at=ended,
        user_goal=user_goal or path.stem,
        task_type=_classify_task(user_goal or path.stem),
        scope="multi_file" if sum(bool(s.files) for s in steps) > 1 else "unknown",
        steps=steps,
        outcome=Outcome(status=status, tests_run=tests, tests_passed=True if tests and status == "completed" else None),
        labels=labels,
    )


def _extract_text(payload: dict[str, Any]) -> str:
    content = payload.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict):
                if isinstance(item.get("text"), str):
                    parts.append(item["text"])
                elif isinstance(item.get("content"), str):
                    parts.append(item["content"])
            elif isinstance(item, str):
                parts.append(item)
        return "\n".join(parts)
    if isinstance(payload.get("message"), str):
        return payload["message"]
    if isinstance(payload.get("tool_use_result"), str):
        return payload["tool_use_result"]
    return ""


def _extract_command(payload: dict[str, Any]) -> str | None:
    if isinstance(payload.get("cmd"), str):
        return payload["cmd"]
    content = payload.get("content")
    if isinstance(content, list):
        for item in content:
            if isinstance(item, dict):
                maybe = item.get("input") or {}
                if isinstance(maybe, dict) and isinstance(maybe.get("command"), str):
                    return maybe["command"]
    return None


def _step_kind(text: str, payload: dict[str, Any]) -> str:
    low = text.lower()
    command = _extract_command(payload)
    probe = (command or low).lower()
    if any(x in probe for x in ("pytest", "vitest", "pnpm test", "npm test", "go test", "cargo test")):
        return "test"
    if any(x in probe for x in ("apply_patch", "edit", "write_file", "*** begin patch")):
        return "edit"
    if any(x in probe for x in ("rg ", "grep ", "find ", "sed -n", "cat ")):
        return "search"
    if low.startswith("final") or "<z-insight>" in low:
        return "final"
    if "plan" in low or "i’ll" in low or "i'm going to" in low:
        return "plan"
    return "other"


def _candidate_skill_lines(steps: list[Step]) -> list[str]:
    skills: list[str] = []
    if any("worktree" in s.summary.lower() for s in steps):
        skills.append("Use a task-specific worktree for non-trivial repository implementation.")
    if any(s.action_kind == "test" for s in steps):
        skills.append("Run focused verification after implementation and record exact commands.")
    if any("dirty" in s.summary.lower() or "unrelated" in s.summary.lower() for s in steps):
        skills.append("Preserve unrelated dirty-tree changes and stage only owned files.")
    return skills


def _failure_modes(steps: list[Step]) -> list[str]:
    modes: list[str] = []
    if any("timeout" in s.summary.lower() for s in steps):
        modes.append("timeout")
    if any("blocked" in s.summary.lower() for s in steps):
        modes.append("blocked")
    if any("failed" in s.summary.lower() for s in steps):
        modes.append("command_failed")
    return modes

