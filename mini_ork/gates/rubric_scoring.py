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
- ``artifact_summary`` has since DIVERGED deliberately, so it is no
  longer byte-comparable to the retired bash twin (which no longer
  exists to compare against). The bash version clipped every text file
  to its first 25 lines with no marker, which hid long work products —
  see the function docstring.
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


_TEXT_SUFFIXES = (".md", ".json", ".txt", ".yaml", ".log")

# Hard ceiling on how much of one artifact is read into memory. Larger
# files are reported by size instead of being sampled — a partial read
# cannot honestly state how many lines the file has.
_MAX_READ_CHARS = 5_000_000

# Guaranteed per-file share of the budget, so a run dir whose headline
# artifact dwarfs its companions still lists the companions' contents.
_MIN_FILE_CHARS = 512


def artifact_summary(run_dir: str, max_chars: int = 12000) -> str:
    """Bounded work-product summary handed to the rubric grader.

    Lists the files in ``run_dir`` (skipping dotfiles), emitting a
    ``### <filename> (<size> bytes)`` header for each and a text sample
    for non-empty ``.md`` / ``.json`` / ``.txt`` / ``.yaml`` / ``.log``
    files. The whole return value is bounded by ``max_chars``.

    Budgeting. Headers are reserved first, so a starved budget still
    tells the grader which artifacts exist. Each text file then gets a
    foothold (``_MIN_FILE_CHARS``, or its whole content when smaller),
    and the surplus above the footholds is shared in proportion to the
    remaining size. Any slack left by files that fit in full is handed
    to the largest still-cut files. A run dir's headline artifact — the
    one carrying the actual work product — is normally also the
    biggest, so it receives most of the budget instead of whichever
    file happens to sort first, without starving its companions.

    Truncation is always labelled: a cut sample ends with
    ``… [<shown> of <total> lines, <shown> of <size> bytes shown]`` so
    the grader can distinguish a short artifact from a clipped view of
    a long one. Silently clipping made a complete document look like it
    ended mid-section, which the grader then failed on "no truncation".
    """
    try:
        names = sorted(os.listdir(run_dir))
    except FileNotFoundError:
        return ""

    entries: list[tuple[str, int, bool]] = []
    for name in names:
        path = os.path.join(run_dir, name)
        if not os.path.isfile(path) or name.startswith("."):
            continue
        try:
            size = os.path.getsize(path)
        except OSError:
            continue
        is_text = name.endswith(_TEXT_SUFFIXES) and size > 0
        entries.append((name, size, is_text))
    if not entries:
        return ""

    header_cost = sum(len(f"### {n} ({s} bytes)") + 1 for n, s, _ in entries)
    budget = max(max_chars - header_cost, 0)

    text = [(n, s) for n, s, t in entries if t]
    quota: dict[str, int] = {}
    if text:
        # Every text file gets a foothold — its whole content when it is small —
        # so one dominant artifact cannot squeeze its companions out of the
        # listing entirely. Only the surplus above that foothold is shared out.
        base = {n: min(s, _MIN_FILE_CHARS) for n, s in text}
        base_total = sum(base.values())
        if base_total > budget:
            base = {n: v * budget // base_total for n, v in base.items()}
        quota = dict(base)
        need = {n: s - base[n] for n, s in text}
        need_total = sum(need.values())
        rest = budget - sum(quota.values())
        if rest > 0 and need_total > 0:
            for n in need:
                quota[n] += min(need[n], rest * need[n] // need_total)
    # Give back what the small files cannot use, to the largest files that
    # are still cut. Bounded by the file count; each pass either frees
    # slack or stops.
    for _ in range(len(text)):
        slack = int(sum(max(0, quota[n] - s) for n, s in text))
        cut = sorted((e for e in text if quota[e[0]] < e[1]), key=lambda e: -e[1])
        if slack < 1 or not cut:
            break
        for entry in cut:
            quota[entry[0]] += slack // len(cut)
        for n, s in text:
            quota[n] = min(quota[n], s)

    lines: list[str] = []
    for name, size, is_text in entries:
        lines.append(f"### {name} ({size} bytes)")
        if is_text:
            path = os.path.join(run_dir, name)
            if size > _MAX_READ_CHARS:
                lines.append(f"… [not sampled: {size} bytes exceeds the read cap]")
            else:
                try:
                    with open(path, errors="replace") as f:
                        full = f.read()
                except Exception:
                    full = ""
                if full:
                    all_lines = full.splitlines(keepends=True)
                    limit = quota[name]
                    kept, used = [], 0
                    for ln in all_lines:
                        if used + len(ln) > limit:
                            break
                        kept.append(ln)
                        used += len(ln)
                    if len(kept) == len(all_lines):
                        body = "".join(kept).rstrip()
                    else:
                        # The label must fit inside this file's share too, or the
                        # global cap clips it and the sample ends mid-sentence with
                        # nothing saying it was cut. Drop lines until it fits.
                        body = ""
                        while True:
                            kept_text = "".join(kept).rstrip()
                            tail = (
                                f"… [{len(kept)} of {len(all_lines)} lines, "
                                f"{len(kept_text)} of {size} bytes shown]"
                            )
                            if len(kept_text) + len(tail) + (
                                1 if kept_text else 0
                            ) <= limit:
                                body = f"{kept_text}\n{tail}" if kept_text else tail
                                break
                            if not kept:
                                break
                            kept.pop()
                    if body:
                        lines.append(body)
        lines.append("")
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
