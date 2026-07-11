"""Python port of lib/context_role_packs.sh — role-specific ContextNest context
packs assembled for each node in the loop.

Strangler-fig parity port. The module has two deterministic pure-logic pieces
and a dispatcher whose entire observable contract without a live ContextNest is
graceful degradation to empty output:

    extract_query(brief_path)        -> first non-stopword concept token (len>=4)
    extract_task_class(brief_path)   -> task_class from a JSON brief, else ""
    role_pack_md(role, brief, files) -> role sub-pack markdown, or "" when CN off

The role sub-packs (_role_pack_planner/researcher/implementer/…) are pure
ContextNest orchestration: every line is guarded by ``declare -f cn_* && …`` in
bash and produces nothing unless a live ContextNest answers. In any environment
without ContextNest — which is every test environment and the degraded runtime
path — ``role_pack_md`` returns "" after its guard chain (MO_DISABLE_CN,
missing brief, missing cn_client, cn_available()==False). This port mirrors
that contract exactly; the CN-integration sub-packs are a follow-on that will
delegate to the Python CN client once it is wired, gated behind ``cn_available``.
"""
from __future__ import annotations

import json
import os
import re

# Structural / filler words that must never become the scoping token — they
# match unrelated atoms across the whole substrate. Verbatim from the bash.
_STOP = {
    "kickoff", "phase", "goal", "task", "wire", "into", "from", "with",
    "this", "that", "then", "plain", "english", "objective", "summary",
    "step", "steps", "title", "intro", "overview", "context", "change",
    "changes", "implement", "implementation", "ship", "shipped", "fix",
    "fixes", "make", "adds", "added", "using", "when", "where", "what",
    "which", "should", "would", "will", "must", "each", "their", "they",
    "have", "been", "does", "doing", "done", "onto", "over", "under",
}


def _pick(text: str) -> str:
    """First concept token (len>=4) that isn't structural boilerplate."""
    for raw_tok in text.split()[:60]:
        tok = re.sub(r"^[^A-Za-z0-9]+|[^A-Za-z0-9_-]+$", "", raw_tok)
        if len(tok) >= 4 and tok.lower() not in _STOP:
            return tok
    return ""


def extract_query(brief_path: str | os.PathLike) -> str:
    """Mirror _role_pack_extract_query: pick the scoping token from a brief.
    Returns "" for a missing file or when no concept token is found."""
    if not os.path.isfile(brief_path):
        return ""
    try:
        with open(brief_path) as f:
            raw = f.read()
    except OSError:
        return ""
    try:
        d = json.loads(raw)
        parts = []
        for k in ("title", "objective", "description", "task_class"):
            v = d.get(k) if isinstance(d, dict) else None
            if isinstance(v, str) and v.strip():
                parts.append(v.strip())
        text = " ".join(parts)[:600] if parts else raw[:512].strip()
    except Exception:
        # Markdown brief: drop heading markers, code fences and inline
        # backticks before tokenising.
        lines = []
        for ln in raw.splitlines():
            s = re.sub(r"^\s*#+\s*", "", ln)   # heading markers
            if s.strip().startswith("```"):     # fenced code start/end
                continue
            lines.append(s)
        text = re.sub(r"`+", " ", "\n".join(lines))[:512].strip()
    return _pick(text)


def extract_task_class(brief_path: str | os.PathLike) -> str:
    """Mirror _role_pack_extract_task_class: task_class from a JSON brief.
    Empty when the file is missing, is markdown, or lacks task_class."""
    if not os.path.isfile(brief_path):
        return ""
    try:
        with open(brief_path) as f:
            d = json.load(f)
    except Exception:
        return ""
    if isinstance(d, dict):
        return d.get("task_class", "") or ""
    return ""


def role_pack_md(role: str, task_brief_path: str | os.PathLike, files_csv: str = "",
                 *, cn_available: bool = False) -> str:
    """Mirror context_role_pack_md's guard/degradation contract.

    Returns "" when: MO_DISABLE_CN=1, the brief is missing, or ContextNest is
    unavailable (the default in any environment without a live CN — exactly
    when the bash guard chain short-circuits). With ``cn_available=True`` the
    role sub-packs would run; that CN-integration path is a follow-on.
    """
    if not role:
        raise ValueError("role required")
    if os.environ.get("MO_DISABLE_CN", "0") == "1":
        return ""
    if not os.path.isfile(task_brief_path):
        return ""
    if not cn_available:
        return ""
    # CN-available role dispatch is the follow-on (needs the Python CN client);
    # bash produces nothing here without ContextNest, so "" preserves parity.
    return ""
