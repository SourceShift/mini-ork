"""Persist a DispatchResult to the llm_calls table (Phase-0 migration, ADR-001).

Python port of lib/llm-dispatch.sh's `_mo_llm_write_llm_calls_row`. Two faithful
behaviours carried over:

  - **Column introspection.** Only columns that actually exist in the target
    `llm_calls` table are inserted, so the writer works against any schema
    version (old DBs without the 0024 cache-aware columns still take the core
    row) — exactly like the bash `PRAGMA table_info` gate.
  - **Cache-aware cost split.** `cost_input_uncached/cached/cache_write` are
    derived from the token split at the same default rates as the bash writer.

Parameterized SQL throughout; a missing DB file is a no-op (returns None), never
a crash — telemetry must never break a dispatch.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping
from pathlib import Path

from .models import DispatchResult, TokenUsage

# Per-Mtok USD rates for the cache-aware input cost split — same defaults as the
# bash writer (lib/llm-dispatch.sh). Overridable per call for correct
# per-provider pricing.
DEFAULT_RATE_UNCACHED_IN = 15.0
DEFAULT_RATE_CACHED_IN = 1.5
DEFAULT_RATE_CACHE_WRITE = 18.75


def cache_aware_cost(
    usage: TokenUsage,
    *,
    rate_uncached_in: float = DEFAULT_RATE_UNCACHED_IN,
    rate_cached_in: float = DEFAULT_RATE_CACHED_IN,
    rate_cache_write: float = DEFAULT_RATE_CACHE_WRITE,
) -> tuple[float, float, float]:
    """(uncached_input, cached_input, cache_write) USD. input_tokens INCLUDES the
    cached + cache-creation tokens, so subtract them before the uncached rate."""
    uncached_in = max(
        usage.input_tokens - usage.cached_input_tokens - usage.cache_creation_tokens, 0
    )
    return (
        uncached_in * rate_uncached_in / 1_000_000,
        usage.cached_input_tokens * rate_cached_in / 1_000_000,
        usage.cache_creation_tokens * rate_cache_write / 1_000_000,
    )


def persist_call(
    db_path: str | Path,
    result: DispatchResult,
    *,
    provider: str,
    feature_name: str,
    tier: str = "default",
    actor: str | None = None,
    run_id: str | int | None = None,
    iter_: int | None = None,
    traceparent: str | None = None,
    metadata: Mapping[str, object] | None = None,
    session_id: str | None = None,
) -> int | None:
    """Write one llm_calls row for ``result``; return its rowid (or None if the
    DB file is absent). ``status`` is derived from ``result.ok`` to satisfy the
    table's CHECK(status IN ('success','failed')) constraint."""
    db = Path(db_path)
    if not db.is_file():
        return None

    usage = result.usage
    uncached_cost, cached_cost, cache_write_cost = cache_aware_cost(usage)
    md: dict[str, object] = dict(metadata or {})
    if session_id and "session_id" not in md:
        md["session_id"] = session_id

    candidate: dict[str, object] = {
        "provider": provider,
        "model_id": result.model or "",
        "tier": tier,
        "feature_name": feature_name,
        "actor": actor,
        "status": "success" if result.ok else "failed",
        "duration_ms": int(result.duration_ms),
        "cost_usd": float(result.cost_usd),
        "error_message": result.error or None,
        "input_tokens": usage.input_tokens,
        "output_tokens": usage.output_tokens,
        "total_tokens": usage.input_tokens + usage.output_tokens,
        "metadata_json": json.dumps(md),
        "run_id": run_id,
        "iter": iter_,
        "traceparent": traceparent,
        "session_id": session_id,
        "cached_input_tokens": usage.cached_input_tokens,
        "cache_creation_input_tokens": usage.cache_creation_tokens,
        "cost_input_uncached_usd": uncached_cost,
        "cost_input_cached_usd": cached_cost,
        "cost_cache_write_usd": cache_write_cost,
    }

    con = sqlite3.connect(str(db), timeout=5)
    try:
        con.execute("PRAGMA busy_timeout=5000")
        existing = {row[1] for row in con.execute("PRAGMA table_info(llm_calls)")}
        cols = [c for c in candidate if c in existing]
        placeholders = ",".join("?" for _ in cols)
        col_sql = ",".join(f'"{c}"' for c in cols)
        cur = con.execute(
            f"INSERT INTO llm_calls ({col_sql}) VALUES ({placeholders})",
            [candidate[c] for c in cols],
        )
        con.commit()
        return cur.lastrowid
    finally:
        con.close()
