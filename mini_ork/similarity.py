"""Canonical deterministic TF-IDF cosine ranker.

The module owns pure tokenization, weighting, cosine, and ranking behavior.
Callers own data access and product policy such as score thresholds, per-source
limits, citations, and result shaping. ``rank_raw`` exposes unrounded scores so
policy callers can filter and sort without losing precision; ``rank`` is the
rounded compatibility API.

``tests/unit/test_similarity_py.py`` pins the deterministic contract after the
Bash predecessor was retired.

Public API::

    from mini_ork.similarity import (
        tok, tf, cos,                     # deterministic primitives
        allowed_table_col, ALLOWED,       # table/column whitelist
        rank_raw, rank,                   # raw and rounded ranking APIs
    )

    scored = rank("auth bug", ["auth fix in middleware", "unrelated doc"], limit=5)
    # -> [(0.83, 0)]              # (rounded score, original doc index)

Score rendering uses ``round(s, 4)`` via the ``round_ndigits=4`` default.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from typing import Iterable


def tok(s: str) -> list[str]:
    """Lowercase, replace non-``[\\w./_-]`` runs with a space, drop tokens shorter than 3."""
    s = (s or "").lower()
    s = re.sub(r"[^\w./_-]+", " ", s)
    return [t for t in s.split() if len(t) >= 3]


def tf(toks: Iterable[str]) -> dict[str, float]:
    """Term frequency: ``count(t) / max(1, total)`` per token. Empty input -> empty dict."""
    c = Counter(toks)
    total = sum(c.values()) or 1
    return {t: cnt / total for t, cnt in c.items()}


def cos(a: dict[str, float], b: dict[str, float]) -> float:
    """Cosine similarity of two sparse term-weight dicts. Zero if either side is empty."""
    keys = set(a) | set(b)
    dot = sum(a.get(k, 0.0) * b.get(k, 0.0) for k in keys)
    na = math.sqrt(sum(v * v for v in a.values()))
    nb = math.sqrt(sum(v * v for v in b.values()))
    return dot / (na * nb) if na and nb else 0.0


# Supported table/text-column pairs retained for callers that validate context
# retrieval sources before delegating pure ranking to this module.
ALLOWED: dict[str, set[str]] = {
    "bug_reports":      {"title", "description", "suggested_fix"},
    "gradient_records": {"signal", "suggested_change", "target"},
    "learning_record":  {"title", "patch_summary"},
    "pattern_records":  {"description"},
}


def allowed_table_col(table: str, text_col: str) -> bool:
    """True iff ``(table, text_col)`` is whitelisted in :data:`ALLOWED`."""
    return table in ALLOWED and text_col in ALLOWED[table]


def rank_raw(
    query: str,
    docs: Iterable[str],
    limit: int | None = None,
) -> list[tuple[float, int]]:
    """Return positive matches ordered by their unrounded cosine score.

    Equal scores retain document order. Passing ``None`` keeps all positive
    matches so a policy caller can apply its own threshold before truncating.
    """
    doc_list = list(docs)
    doc_toks = [tok(d) for d in doc_list]
    df: Counter[str] = Counter()
    for d in doc_toks:
        for t in set(d):
            df[t] += 1
    n = max(len(doc_list), 1)
    idf = {t: math.log(1.0 + n / (1 + c)) for t, c in df.items()}

    def vec(toks: list[str]) -> dict[str, float]:
        return {t: w * idf.get(t, 0.0) for t, w in tf(toks).items()}

    q_vec = vec(tok(query))
    scored: list[tuple[float, int]] = []
    for i, d in enumerate(doc_toks):
        s = cos(q_vec, vec(d))
        if s > 0:
            scored.append((s, i))
    scored.sort(key=lambda p: p[0], reverse=True)
    return scored if limit is None else scored[:limit]


def rank(
    query: str,
    docs: Iterable[str],
    limit: int = 5,
    round_ndigits: int = 4,
) -> list[tuple[float, int]]:
    """Return rounded top matches while preserving raw-score ordering."""
    return [
        (round(score, round_ndigits), index)
        for score, index in rank_raw(query, docs, limit=limit)
    ]
