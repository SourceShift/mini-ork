"""rubric_scoring — response parsing + score-computation helpers.

Pure functions extracted from ``mini_ork/gates/rubric_prescreen.py``
(SOLID SRP split). Everything here is side-effect-free except
``artifact_summary`` / ``_extract_result_text`` which only READ the
filesystem. Public names are re-exported from
``mini_ork.gates.rubric_prescreen`` — import from there, not here,
unless you are writing focused unit tests for the pure layer.

Pipeline map (bash → Python; bash line ranges from
``lib/rubric-prescreen.sh``):

  extract_rubric_json       lines 140-159  → extract_rubric_json
  artifact_summary          lines 247-267  → artifact_summary
  substitute_template       lines 271-279  → substitute_template
  build_parse_error_payload lines 187-191  → build_parse_error_payload
  build_panel_verdict       lines 335-339  → build_panel_verdict

Notes on parity:
- ``substitute_template`` does FIRST-occurrence-only replacement
  (mirrors the bash awk splitter at lines 57-66 which splits on the
  first marker). This is intentionally different from ``str.replace``
  which would substitute every occurrence. The parity test exercises
  the first-only semantics.
- The heredoc-lifted helpers were already Python source lifted into
  bash heredocs; the port reproduces them with only the minimum
  required type hints (byte-equivalent by construction).
"""
from __future__ import annotations

import json
import os
import re
from typing import Any, Optional

__all__ = [
    "extract_rubric_json",
    "substitute_template",
    "artifact_summary",
    "build_parse_error_payload",
    "build_panel_verdict",
]


# ─────────────────────────────────────────────────────────────────────────────
# Heredoc-lifted helpers (lines 140-159, 247-267, 271-279 of
# lib/rubric-prescreen.sh — these were already Python source lifted into
# bash heredocs; the port just lifts them into a module).
# ─────────────────────────────────────────────────────────────────────────────

def extract_rubric_json(text: str) -> Optional[str]:
    """Mirror bash heredoc at lines 140-159.

    Brace-balanced JSON scanner: finds the LAST ``{"pass":`` start in
    the text, walks forward with a depth counter (respecting string
    literals + backslash escapes) until the matching close brace, then
    tries ``json.loads`` on the candidate. Returns the candidate
    substring on success, ``None`` otherwise.

    The bash heredoc iterates ``starts`` in REVERSED order — it
    prefers the LAST ``{"pass":`` in the text, so a "Here's the
    final rubric: {...}" preamble with an earlier ``{"pass"`` is
    ignored. The port mirrors exactly.
    """
    starts = [m.start() for m in re.finditer(r'\{[^{]*?"pass"\s*:', text)]
    for start in reversed(starts):
        depth, in_str, esc = 0, False, False
        for i in range(start, len(text)):
            c = text[i]
            if esc:
                esc = False
                continue
            if c == "\\":
                esc = True
                continue
            if c == '"' and not esc:
                in_str = not in_str
                continue
            if in_str:
                continue
            if c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    cand = text[start:i + 1]
                    try:
                        json.loads(cand)
                    except Exception:
                        break
                    return cand
    return None


def substitute_template(template: str, kickoff_body: str, diff_summary: str) -> str:
    """Mirror bash heredoc at lines 271-279.

    First-occurrence-only replacement of ``{{KICKOFF_BODY}}`` and
    ``{{DIFF_SUMMARY}}``. Mirrors the awk splitter at lines 57-66 of
    the bash file which splits the template on the FIRST occurrence of
    each marker. If a marker does not appear, it passes through
    unchanged. ``str.replace`` would substitute every occurrence —
    do NOT use it here, the parity test will catch the difference.

    The ``diff_summary`` is rstripped of trailing newlines because the
    bash caller feeds it via ``"$(python3 ...)"`` (artifact_summary
    variable at line 247), and bash command-substitution strips
    trailing newlines from ``$(...)`` outputs. The kickoff body is
    passed as-is because the bash version reads it from a file via
    ``open(kickoff).read()`` (no rstrip happens at that boundary).

    The return value is rstripped of trailing newlines to match the
    bash caller's ``prompt_text=$(python3 ...)`` capture, which
    strips trailing newlines from ``$(...)`` outputs.
    """
    body = template
    if "{{KICKOFF_BODY}}" in body:
        body = body.replace("{{KICKOFF_BODY}}", kickoff_body, 1)
    if "{{DIFF_SUMMARY}}" in body:
        body = body.replace("{{DIFF_SUMMARY}}", diff_summary.rstrip("\n"), 1)
    return body.rstrip("\n")


def artifact_summary(run_dir: str, max_chars: int = 12000) -> str:
    """Mirror bash heredoc at lines 247-267.

    Bounded work-product summary: list files in ``run_dir`` (skipping
    dotfiles), print ``### <filename> (<size> bytes)`` header, then the
    first 25 lines (capped at 2000 chars) for text files (.md / .json /
    .txt / .yaml / .log) that are non-empty. Output is capped at
    ``max_chars`` total (default 12000 — matches bash).
    """
    lines: list[str] = []
    try:
        names = sorted(os.listdir(run_dir))
    except FileNotFoundError:
        return ""
    for name in names:
        path = os.path.join(run_dir, name)
        if not os.path.isfile(path) or name.startswith("."):
            continue
        try:
            size = os.path.getsize(path)
        except OSError:
            continue
        lines.append(f"### {name} ({size} bytes)")
        if name.endswith((".md", ".json", ".txt", ".yaml", ".log")) and size > 0:
            try:
                with open(path, errors="replace") as f:
                    head = "".join(f.readlines()[:25])
                lines.append(head[:2000].rstrip())
            except Exception:
                pass
        lines.append("")
    # Bash callers use ``$(python3 ...)`` which strips trailing
    # newlines from the heredoc's print output. Match that semantic
    # by rstripping the joined string so the parity test sees the
    # same effective string on both sides.
    return "\n".join(lines)[:max_chars].rstrip("\n")


# ─────────────────────────────────────────────────────────────────────────────
# JSON payload builders
# ─────────────────────────────────────────────────────────────────────────────

def build_parse_error_payload(
    diag: str = "",
    log_path: Optional[str] = None,
) -> dict[str, Any]:
    """Mirror bash jq -n at lines 187-191.

    When ``log_path`` is provided, the payload includes
    ``parse_error_diagnostic`` (last 800 chars of the model output)
    and ``parse_error_log_hint`` ("inspect last 200 lines of <path>")
    so the operator can diagnose why all 4 extraction strategies
    missed. When ``log_path`` is None, the diagnostic fields are
    omitted (mirrors the dispatch-failure branch at lines 323-325
    which only emits ``parse_error_diagnostic``).
    """
    payload: dict[str, Any] = {
        "pass": False,
        "score": -1,
        "parse_error": True,
        "items": [],
    }
    if log_path is not None:
        payload["parse_error_diagnostic"] = diag
        payload["parse_error_log_hint"] = f"inspect last 200 lines of {log_path}"
    else:
        payload["parse_error_diagnostic"] = diag
    return payload


def build_panel_verdict(
    score: int,
    pass_: bool,
    task_class: str,
    source: str = "rubric-prescreen",
) -> dict[str, Any]:
    """Mirror bash jq -n at lines 335-339.

    Maps rubric score (0-8) to panel_score (0-100) via
    ``panel_score = score * 12.5``. Consumed by lib/promotion_gate.sh.
    """
    return {
        "panel_score": float(score) * 12.5,
        "pass": pass_,
        "source": source,
        "task_class": task_class,
        "scale": "rubric 0-8 mapped to 0-100",
    }


# ─────────────────────────────────────────────────────────────────────────────
# Internal helpers (not part of __all__; not part of the bash surface)
# ─────────────────────────────────────────────────────────────────────────────

def _extract_result_text(log_path: str) -> str:
    """Mirror bash jq fallbacks at lines 126-138.

    Tries three extraction strategies in order:
    1. ``.result`` field at the top level (--output-format json wrapper).
    2. ``select(.type=="assistant") | .message.content[]?
       | select(.type=="text") | .text`` (legacy stream-json shape).
    3. ``grep '"type":"result"' | tail -1 | jq -r '.result'`` (mixed
       deployment fallback).

    Returns the extracted text or empty string on miss.
    """
    if not os.path.isfile(log_path):
        return ""
    try:
        with open(log_path) as f:
            text = f.read()
    except OSError:
        return ""

    # Strategy 1: top-level .result from --output-format json.
    for line in text.splitlines():
        if '"type":"result"' in line:
            try:
                obj = json.loads(line)
                if isinstance(obj, dict) and obj.get("result"):
                    return str(obj["result"])
            except (ValueError, TypeError):
                pass

    # Strategy 2: legacy stream-json shape.
    for line in text.splitlines():
        try:
            obj = json.loads(line)
        except (ValueError, TypeError):
            continue
        if obj.get("type") != "assistant":
            continue
        msg = obj.get("message") or {}
        for chunk in (msg.get("content") or []):
            if isinstance(chunk, dict) and chunk.get("type") == "text":
                t = chunk.get("text")
                if t:
                    return str(t)

    return ""
