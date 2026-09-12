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

import hashlib
import json
import os
import sqlite3
import sys
import time
from collections.abc import Mapping
from pathlib import Path

from .models import DispatchResult, TokenUsage

# Per-Mtok USD rates for the cache-aware input cost split, keyed by provider
# FAMILY (audit F2). The old code applied the bash writer's Anthropic list
# prices (15/1.5/18.75) to every provider and subtracted cached+creation from
# input unconditionally — right for neither family: Anthropic's envelope
# reports input EXCLUDING cache (live receipt: rows with input=44k /
# cached=261k — the inclusive reading would be negative), so the subtraction
# zeroed real uncached cost ($0.0017 total across 2,856 rows), while codex
# rows got a 10x-overstated breakdown at Anthropic rates ($1,160 phantom vs
# $115.73 real). Families WITHOUT a known table (gateway proxies serving
# minimax/glm/kimi/deepseek, google) resolve to None → a zero breakdown: the
# real billed cost still lands in cost_usd from the provider envelope, and a
# zero is honest where a fabricated number is not.
PROVIDER_FAMILY_RATES: dict[str, tuple[float, float, float] | None] = {
    "anthropic": (15.0, 1.5, 18.75),   # sonnet-class list prices
    "openai": (1.25, 0.125, 0.0),      # gpt-5 list (codex lanes); no cache-write bill
    "gateway": None,                   # per-model prices; envelope carries the real cost
    "google": None,
    "unknown": None,
}

# Families whose reported input_tokens INCLUDE cached tokens (OpenAI chat
# usage semantics: prompt_tokens ⊇ cached_tokens). Anthropic-style envelopes
# report input EXCLUDING cache read and creation, so uncached input is
# input_tokens as-is for every family not listed here.
INPUT_INCLUDES_CACHE: frozenset[str] = frozenset({"openai"})

_LANE_FAMILY = {
    "anthropic": "anthropic", "opus": "anthropic", "sonnet": "anthropic",
    "openai": "openai", "codex": "openai", "gpt": "openai",
    "gateway": "gateway", "minimax": "gateway", "glm": "gateway",
    "kimi": "gateway", "deepseek": "gateway",
    "google": "google", "gemini": "google",
}


def family_of(provider: str) -> str:
    """Collapse a lane alias or llm_calls.provider value to its rate family."""
    return _LANE_FAMILY.get((provider or "").strip().lower(), "unknown")


def rates_for(provider: str) -> tuple[float, float, float] | None:
    """(uncached_in, cached_in, cache_write) USD/MTok for a provider/lane, or
    None when the family has no known table (breakdown must stay zero)."""
    return PROVIDER_FAMILY_RATES.get(family_of(provider))


def cache_aware_cost(
    usage: TokenUsage,
    *,
    provider: str = "anthropic",
    rate_uncached_in: float | None = None,
    rate_cached_in: float | None = None,
    rate_cache_write: float | None = None,
) -> tuple[float, float, float]:
    """(uncached_input, cached_input, cache_write) USD at the provider family's
    rates. Token semantics are family-aware: for openai-style streams
    input_tokens INCLUDES cached+creation (subtract before the uncached rate);
    for anthropic-style envelopes input EXCLUDES them (use as-is). Families
    without a known rate table — and unknown providers — price the breakdown at
    zero rather than fabricating precision. Explicit rate kwargs override the
    family table."""
    rates = rates_for(provider) or (0.0, 0.0, 0.0)
    r_in = rate_uncached_in if rate_uncached_in is not None else rates[0]
    r_cached = rate_cached_in if rate_cached_in is not None else rates[1]
    r_write = rate_cache_write if rate_cache_write is not None else rates[2]
    if family_of(provider) in INPUT_INCLUDES_CACHE:
        uncached_in = max(
            usage.input_tokens - usage.cached_input_tokens - usage.cache_creation_tokens, 0
        )
    else:
        uncached_in = max(usage.input_tokens, 0)
    return (
        uncached_in * r_in / 1_000_000,
        usage.cached_input_tokens * r_cached / 1_000_000,
        usage.cache_creation_tokens * r_write / 1_000_000,
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
    uncached_cost, cached_cost, cache_write_cost = cache_aware_cost(
        usage, provider=provider
    )
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


def _validate_rel_path(rel_path: str) -> str | None:
    """Reject rel_paths that aren't portable across vendor / foreign-home /
    cloud-exec run dirs. Returns the cleaned rel_path on success, or None on
    rejection (we don't raise — telemetry must never break a dispatch)."""
    if not rel_path:
        return None
    if rel_path.startswith("/"):
        return None
    parts = rel_path.split("/")
    if any(p == ".." for p in parts):
        return None
    return rel_path


def persist_artifact(
    db_path: str | Path,
    *,
    run_id: str,
    node_id: str | None,
    call_id: int | None,
    kind: str,
    rel_path: str,
    abs_path: str | Path,
) -> int | None:
    """Register one run_artifacts row for ``abs_path``. PRAGMA-table_info-gated:
    no-op (returns None) if the table is absent (old DBs). rel_path MUST be a
    portable relative path — absolute paths and ``..`` components are rejected
    with a soft stderr warning instead of crashing. Returns the inserted rowid
    on success."""
    db = Path(db_path)
    if not db.is_file():
        return None
    cleaned = _validate_rel_path(rel_path)
    if cleaned is None:
        sys.stderr.write(
            f"[telemetry] reject run_artifacts rel_path={rel_path!r}: "
            "must be relative, no leading '/', no '..' component\n"
        )
        return None
    abs_p = Path(abs_path)
    if not abs_p.is_file():
        return None
    try:
        size = abs_p.stat().st_size
        h = hashlib.sha256()
        with open(abs_p, "rb") as fh:
            for chunk in iter(lambda: fh.read(65536), b""):
                h.update(chunk)
        digest = h.hexdigest()
    except OSError as exc:
        sys.stderr.write(f"[telemetry] hash failed for {abs_p}: {exc}\n")
        return None

    con = sqlite3.connect(str(db), timeout=5)
    try:
        con.execute("PRAGMA busy_timeout=5000")
        tables = {row[0] for row in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )}
        if "run_artifacts" not in tables:
            return None
        existing = {row[1] for row in con.execute("PRAGMA table_info(run_artifacts)")}
        cols = [c for c in (
            "run_id", "node_id", "call_id", "kind", "rel_path",
            "bytes", "sha256", "created_at",
        ) if c in existing]
        placeholders = ",".join("?" for _ in cols)
        col_sql = ",".join(f'"{c}"' for c in cols)
        values = {
            "run_id": run_id,
            "node_id": node_id,
            "call_id": call_id,
            "kind": kind,
            "rel_path": cleaned,
            "bytes": size,
            "sha256": digest,
            "created_at": int(time.time()),
        }
        cur = con.execute(
            f"INSERT INTO run_artifacts ({col_sql}) VALUES ({placeholders})",
            [values[c] for c in cols],
        )
        con.commit()
        return cur.lastrowid
    except sqlite3.IntegrityError:
        # UNIQUE(run_id, node_id, kind, rel_path) — already registered; no-op.
        return None
    finally:
        con.close()


def resolve_artifact_abs(
    run_dir_or_db: str | Path,
    run_id: str,
    node_id: str | None,
    kind: str,
    *,
    run_dir: str | Path | None = None,
) -> Path | None:
    """Return the absolute path for the most recent run_artifacts row matching
    ``(run_id, node_id, kind)``. First positional arg accepts either a DB path
    OR a run dir; if a run dir is passed, ``$MINI_ORK_DB`` is used as the DB.
    Returns ``None`` if no row exists or the joined path doesn't resolve to a
    real file."""
    if run_dir is not None:
        anchor = Path(run_dir)
        db_path = Path(run_dir_or_db)
    else:
        anchor = Path(run_dir_or_db)
        candidate_db = Path(run_dir_or_db)
        db_path = candidate_db if candidate_db.suffix == ".db" else Path(
            os.environ.get("MINI_ORK_DB", str(candidate_db / "state.db"))
        )
    if not db_path.is_file():
        return None
    con = sqlite3.connect(str(db_path), timeout=5)
    try:
        existing = {row[1] for row in con.execute("PRAGMA table_info(run_artifacts)")}
        if not existing:
            return None
        row = con.execute(
            "SELECT rel_path FROM run_artifacts "
            "WHERE run_id=? AND kind=? "
            "  AND (node_id IS ? OR node_id=?) "
            "ORDER BY created_at DESC, id DESC LIMIT 1",
            (run_id, kind, node_id, node_id),
        ).fetchone()
    finally:
        con.close()
    if not row:
        return None
    joined = (anchor / row[0]).resolve()
    return joined if joined.is_file() else None
