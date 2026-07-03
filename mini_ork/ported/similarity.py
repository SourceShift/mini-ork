"""Pure-logic port of ``lib/similarity.sh``'s TF-IDF cosine ranker.

Faithful port of the deterministic core of ``lib/similarity.sh::similarity_query``.
The bash function is a thin shell that opens sqlite3 and delegates all math
to an embedded Python heredoc. This module lifts that math into a regular
import, strips the sqlite3 I/O (the test layer owns I/O), and exposes a
``rank(query, docs, limit)`` public entry point plus the underlying
deterministic primitives (``tok``, ``tf``, ``cos``).

``tests/unit/test_similarity_parity.py`` enforces exact parity (floats
within ``1e-6``, strings exact, JSON byte-stable on score rendering) between
this module's output and the live bash function over a corpus of
representative inputs.

Public API::

    from mini_ork.ported.similarity import (
        tok, tf, cos,                     # deterministic primitives
        allowed_table_col, ALLOWED,       # table/column whitelist
        rank,                             # top-level ranking pipeline
    )

    scored = rank("auth bug", ["auth fix in middleware", "unrelated doc"], limit=5)
    # -> [(0.83, 0)]              # (rounded score, original doc index)

Score rendering matches bash's ``round(s, 4)`` via the ``round_ndigits=4``
default — keep these values in lockstep.
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


# Mirrors the ALLOWED table+text-column whitelist in lib/similarity.sh verbatim.
# Extend here ONLY in lockstep with the bash file.
ALLOWED: dict[str, set[str]] = {
    "bug_reports":      {"title", "description", "suggested_fix"},
    "gradient_records": {"signal", "suggested_change", "target"},
    "learning_record":  {"title", "patch_summary"},
    "pattern_records":  {"description"},
}


def allowed_table_col(table: str, text_col: str) -> bool:
    """True iff ``(table, text_col)`` is whitelisted in :data:`ALLOWED`."""
    return table in ALLOWED and text_col in ALLOWED[table]


def rank(
    query: str,
    docs: Iterable[str],
    limit: int = 5,
    round_ndigits: int = 4,
) -> list[tuple[float, int]]:
    """Score ``docs`` against ``query`` by TF-IDF cosine, return top ``limit``.

    Returns ``(score, doc_index)`` pairs sorted by score descending, only
    entries with score > 0, truncated to ``limit``. Identical math to
    ``similarity_query`` in ``lib/similarity.sh``; parity (including score
    rounding) is verified by ``test_similarity_parity``.
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
            scored.append((round(s, round_ndigits), i))
    scored.sort(key=lambda p: p[0], reverse=True)
    return scored[:limit]
