"""Local redaction helpers for transcript-derived training data."""

from __future__ import annotations

import re


TOKEN_PATTERNS = [
    re.compile(r"(?i)(api[_-]?key|token|secret|password|passwd)\s*[:=]\s*['\"]?[^'\"\s]+"),
    re.compile(r"(?i)(bearer\s+)[a-z0-9._\-]{16,}"),
    re.compile(r"sk-[a-zA-Z0-9_\-]{16,}"),
    re.compile(r"ghp_[a-zA-Z0-9_]{16,}"),
    re.compile(r"hf_[a-zA-Z0-9_]{16,}"),
]


def redact_text(value: str, max_chars: int = 4000) -> str:
    text = value
    for pattern in TOKEN_PATTERNS:
        text = pattern.sub(lambda m: m.group(1) + "[REDACTED]" if m.groups() else "[REDACTED]", text)
    if len(text) > max_chars:
        return text[:max_chars] + "\n[TRUNCATED]"
    return text

