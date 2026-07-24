"""Standalone, opt-in semantic long-term memory (mem-a).

Mem0-style ADD/UPDATE/DELETE reconcile over a per-scope SQLite index, with a
default stdlib-only HashEmbedder so the module imports and runs with zero new
pip dependency. A real Embedder is wired behind a thin provider stub that
activates only when ``MO_EMBED_PROVIDER`` is set (no third-party import path
on the default branch).

Test-monkeypatch contract: tests patch ``mini_ork.memory.semantic.dispatch_model``
(imported here from ``mini_ork.dispatch``). The two names refer to the same
object — Python imports are by-reference — so the patch correctly substitutes
the dispatch call inside ``add(..., infer=True)`` without the real provider
being invoked.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import sqlite3
import struct
from collections.abc import Callable
import time
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

# Pinned at the import site the kickoff contract specifies. Tests patch
# `mini_ork.memory.semantic.dispatch_model`; that attribute resolves to this
# same object because Python imports are by-reference.
from mini_ork.dispatch import DispatchRequest, dispatch_model


# ── Reconcile thresholds (deterministic, unit-testable) ────────────────────
#
# These are the design call the planner handed to the implementer. They are
# fixed constants (not env-tunable) so behavior is reproducible and the
# no-unbounded-growth DoD bullet is provable on the same input across runs.
#
#   UPDATE_THRESHOLD  ≥ : same fact, rephrased (cosine ~ 0.90+). Update in place.
#   DELETE_ADD_THRESH ≥ : same topic, contradicting replacement. Requires the
#                        model to also tag the fact as op="delete_replace";
#                        cosine alone is not enough to delete — the model is
#                        the source of truth for "this contradicts X".
UPDATE_THRESHOLD = 0.90
DELETE_ADD_THRESHOLD = 0.80

# HashEmbedder dim. 256 is small enough to pack as a 1KB blob per row and big
# enough that 4096-token text hashes give distinct-enough projections for the
# reconcile thresholds above to discriminate paraphrase vs contradiction.
HASH_DIM = 256


# ── Embedder protocol + default impl ───────────────────────────────────────


@runtime_checkable
class Embedder(Protocol):
    """A vector embedder. ``embed`` returns unit-normalized vectors so cosine
    similarity reduces to a dot product — cheap at search time."""

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        ...


# Token splitter: lowercase, then contiguous runs of [a-z0-9]. Strips
# punctuation but keeps digits, so "user-42" and "user 42" collide
# intentionally (they're the same fact).
_TOKEN_RE = re.compile(r"[a-z0-9]+")


def _tokens(text: str) -> list[str]:
    return _TOKEN_RE.findall(text.lower())


class HashEmbedder:
    """Deterministic, pure-stdlib embedder using the signed hashing trick.

    Each token contributes ±1 to a fixed-dim vector at positions derived from
    ``sha256(token)`` chunks. The final vector is L2-normalized so cosine
    similarity between two unit vectors equals their dot product — no per-call
    magnitude math at search time. Two identical inputs always produce the
    same vector (deterministic), and the dim is fixed at 256.
    """

    def __init__(self, dim: int = HASH_DIM) -> None:
        if dim <= 0:
            raise ValueError("dim must be positive")
        self.dim = dim

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        out: list[list[float]] = []
        for t in texts:
            v = [0.0] * self.dim
            for tok in _tokens(t):
                if not tok:
                    continue
                digest = hashlib.sha256(tok.encode("utf-8")).digest()
                # 8 int32 chunks per sha256 = 32 bytes. Spread each chunk over
                # the vector at a position derived from its value.
                for i in range(0, 32, 4):
                    chunk = struct.unpack_from("<i", digest, i)[0]
                    pos = chunk % self.dim
                    sign = 1.0 if chunk >= 0 else -1.0
                    v[pos] += sign
            # L2 normalize. Skip the divide if the vector is empty (no tokens)
            # — caller will see a zero vector, which is a legitimate "unknown".
            norm = math.sqrt(sum(x * x for x in v))
            if norm > 0.0:
                inv = 1.0 / norm
                v = [x * inv for x in v]
            out.append(v)
        return out


# ── Embedder provider registry (OCP/LSP) ────────────────────────────────────
# A provider is a zero-arg factory returning a REAL Embedder — registered
# implementations must satisfy the Embedder protocol (no NotImplementedError
# stubs; an unregistered provider fails fast at the factory, before any
# contract-breaking object can exist).

EMBEDDER_PROVIDERS: dict[str, Callable[[], "Embedder"]] = {}


def register_embedder_provider(name: str, factory: Callable[[], "Embedder"]) -> None:
    """Register an Embedder factory selectable via MO_EMBED_PROVIDER=<name>.

    Real impls (sentence-transformers, OpenAI, Cohere, …) plug in here from
    downstream code, keeping third-party imports off the default path."""
    EMBEDDER_PROVIDERS[name] = factory


def get_embedder() -> Embedder:
    """Factory: returns HashEmbedder unless ``MO_EMBED_PROVIDER`` names a
    registered provider. An unregistered provider raises immediately (fail
    fast at configuration time — never return an Embedder that cannot embed)."""
    provider = os.environ.get("MO_EMBED_PROVIDER", "").strip()
    if not provider:
        return HashEmbedder()
    factory = EMBEDDER_PROVIDERS.get(provider)
    if factory is None:
        raise ValueError(
            f"MO_EMBED_PROVIDER={provider!r} has no registered embedder. "
            "Register one via register_embedder_provider(), pass an Embedder "
            "to add(..., embedder=...), or unset MO_EMBED_PROVIDER to use the "
            "default HashEmbedder."
        )
    return factory()


# ── Storage ────────────────────────────────────────────────────────────────


def _resolve_db_path(db_path: str | os.PathLike[str] | None) -> str:
    """Resolve the semantic-memory db path lazily (DIP): explicit argument
    wins, else the MINI_ORK_DB env contract is read AT CALL TIME — never
    frozen at import, so tests and long-running processes that repoint the
    env see the current value."""
    if db_path is None:
        return os.environ.get("MINI_ORK_DB") or ".mini-ork/state.db"
    return os.fspath(db_path)


# Idempotent migration SQL — a slim copy of the canonical
# db/migrations/0046_semantic_memory.sql so the module bootstraps a tmp DB
# without requiring the migration loader to have run. Kept in lock-step with
# the .sql file by hand (one table, one index, additive). Re-running this
# block on an existing DB is a no-op (IF NOT EXISTS).
_BOOTSTRAP_SQL = """
CREATE TABLE IF NOT EXISTS semantic_memory (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  scope      TEXT    NOT NULL,
  text       TEXT    NOT NULL,
  embedding  BLOB    NOT NULL,
  created_at REAL    NOT NULL,
  meta       TEXT
);
CREATE INDEX IF NOT EXISTS idx_semantic_memory_scope
  ON semantic_memory(scope);
"""


def _connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    # Self-bootstrap. Idempotent — the SQL uses IF NOT EXISTS, so applying it
    # to a DB that already has the table is a no-op. The migration loader is
    # not on the test path; this is how the module guarantees the schema
    # exists when called from a fresh tmp DB.
    conn.executescript(_BOOTSTRAP_SQL)
    conn.commit()
    return conn


def _pack_embedding(vec: Sequence[float]) -> bytes:
    return struct.pack(f"<{len(vec)}f", *vec)


def _unpack_embedding(blob: bytes) -> list[float]:
    n = len(blob) // 4
    return list(struct.unpack(f"<{n}f", blob))


# ── Reconcile ──────────────────────────────────────────────────────────────


# Result event shape. Tests assert on `op`; `memory_id` and `text` are
# surfaced so callers can audit what happened.
@dataclass(frozen=True)
class MemoryEvent:
    op: str           # one of: "ADD", "UPDATE", "DELETE", "NOOP"
    memory_id: int
    text: str
    score: float = 0.0  # cosine vs the closest existing memory; 0.0 if ADD


def _cosine(a: Sequence[float], b: Sequence[float]) -> float:
    # Both are unit-normalized (caller guarantee — embed() normalizes), so
    # cosine reduces to a dot product. No magnitude math.
    n = min(len(a), len(b))
    return sum(a[i] * b[i] for i in range(n))


def _last_id(cur: sqlite3.Cursor) -> int:
    """lastrowid is `int | None` by typing; INSERTs always set it. Coerce
    to 0 on the (unreachable here) None branch so the event payload stays a
    plain int and tests don't have to handle a non-existent memory_id."""
    return int(cur.lastrowid or 0)


def _search_in_scope(
    emb: Sequence[float], scope: str, db_path: str,
) -> list[tuple[float, sqlite3.Row]]:
    conn = _connect(db_path)
    try:
        rows = conn.execute(
            "SELECT id, text, embedding FROM semantic_memory WHERE scope = ?",
            (scope,),
        ).fetchall()
    finally:
        conn.close()
    scored: list[tuple[float, sqlite3.Row]] = []
    for r in rows:
        v = _unpack_embedding(r["embedding"])
        scored.append((_cosine(emb, v), r))
    scored.sort(key=lambda t: t[0], reverse=True)
    return scored


# ── Fact extraction (inference) ────────────────────────────────────────────

# Prompt asks for a JSON list of objects with explicit `op` so the reconcile
# step can distinguish ADD / UPDATE / DELETE+ADD without guessing. The
# `supersedes` field is the model's free-form pointer to the fact being
# replaced (only meaningful for op="delete_replace"). A pure-string list
# (legacy/test format) is also accepted and treated as a flat ADD.
#
# Brace doubling: the JSON examples below contain literal `{` / `}`, which
# str.format() would otherwise try to interpret as format placeholders. We
# use `{{` / `}}` to escape them; `{text}` is the single real placeholder.
_EXTRACT_PROMPT = """\
Extract durable, persistent facts from the text below. Return ONLY a JSON
list — no prose, no fences, no commentary. Each element is one of:

  {{"text": "<durable fact>", "op": "add"}}            — new fact to record
  {{"text": "<durable fact>", "op": "update"}}         — refines an existing fact
  {{"text": "<durable fact>", "op": "delete_replace",
   "supersedes": "<text of fact being replaced>"}}    — contradicts + replaces

If the text contains no durable facts, return [].

Text:
\"\"\"
{text}
\"\"\"
"""


def _coerce_facts(parsed: object) -> list[dict]:
    """Normalize the model's response to a list[dict{text, op, supersedes?}]."""
    if not isinstance(parsed, list):
        raise ValueError(
            f"fact extraction: expected a JSON list, got {type(parsed).__name__}"
        )
    out: list[dict] = []
    for item in parsed:
        if isinstance(item, str):
            out.append({"text": item, "op": "add", "supersedes": None})
            continue
        if not isinstance(item, dict):
            raise ValueError(
                f"fact extraction: list element must be str or dict, got {type(item).__name__}"
            )
        if "text" not in item or not isinstance(item["text"], str):
            raise ValueError("fact extraction: each item needs a string 'text' field")
        op = item.get("op", "add")
        if op not in ("add", "update", "delete_replace"):
            raise ValueError(f"fact extraction: unknown op {op!r}")
        out.append({
            "text": item["text"],
            "op": op,
            "supersedes": item.get("supersedes"),
        })
    return out


def _extract_facts(text: str, model: str) -> list[dict]:
    """Call the model and parse the response. Raises on failure (fail-loudly
    policy — see forbidden_fallbacks in the planner contract)."""
    prompt = _EXTRACT_PROMPT.format(text=text)
    result = dispatch_model(DispatchRequest(model=model, prompt=prompt))
    if not result.ok:
        raise RuntimeError(
            f"fact extraction: dispatch failed (rc={result.rc}): {result.error}"
        )
    raw = result.text.strip()
    # Strip optional code fences — some providers wrap JSON in ```json ... ```.
    if raw.startswith("```"):
        raw = raw.strip("`")
        if raw.startswith("json"):
            raw = raw[4:]
        raw = raw.strip()
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"fact extraction: model returned non-JSON (len={len(raw)}): {raw[:200]!r}"
        ) from exc
    return _coerce_facts(parsed)


# ── Public API ────────────────────────────────────────────────────────────


# Default model for inference. The test stub monkeypatches dispatch_model so
# this string never reaches a real provider in tests. Production callers can
# override per-call via `model=` or globally via MO_SEMANTIC_MODEL.
_DEFAULT_MODEL = os.environ.get("MO_SEMANTIC_MODEL", "haiku")


def add(
    text: str,
    *,
    scope: str,
    infer: bool = True,
    db_path: str | os.PathLike[str] | None = None,
    embedder: Embedder | None = None,
    model: str | None = None,
) -> list[dict]:
    """Reconcile ``text`` into the semantic index under ``scope``.

    With ``infer=True`` (default), the text is sent to the model to extract
    durable facts; each fact is embedded, compared to existing memories in
    the scope, and emitted as ADD / UPDATE / DELETE+ADD / NOOP. With
    ``infer=False``, the raw text is stored as a single ADD. Returns the
    list of events so callers can audit what changed.

    The scope filter is mandatory; empty/whitespace raises ValueError.
    """
    if not isinstance(scope, str) or not scope.strip():
        raise ValueError("scope must be a non-empty string")
    if not isinstance(text, str) or not text.strip():
        raise ValueError("text must be a non-empty string")

    db = _resolve_db_path(db_path)
    emb_obj = embedder or get_embedder()

    if infer:
        facts = _extract_facts(text, model or _DEFAULT_MODEL)
    else:
        facts = [{"text": text, "op": "add", "supersedes": None}]

    events: list[dict] = []
    conn = _connect(db)
    try:
        for fact in facts:
            vec = emb_obj.embed([fact["text"]])[0]
            existing = _search_in_scope(vec, scope, db)
            best_score, best_row = (existing[0] if existing else (0.0, None))

            if (
                fact["op"] == "delete_replace"
                and best_row is not None
                and best_score >= DELETE_ADD_THRESHOLD
            ):
                # DELETE old + ADD new in the same transaction.
                conn.execute("DELETE FROM semantic_memory WHERE id = ?", (best_row["id"],))
                cur = conn.execute(
                    "INSERT INTO semantic_memory(scope, text, embedding, created_at) "
                    "VALUES (?, ?, ?, ?)",
                    (scope, fact["text"], _pack_embedding(vec), time.time()),
                )
                events.append({
                    "op": "DELETE", "memory_id": int(best_row["id"]),
                    "text": best_row["text"], "score": best_score,
                })
                events.append({
                    "op": "ADD", "memory_id": _last_id(cur),
                    "text": fact["text"], "score": 0.0,
                })
            elif (
                best_row is not None
                and best_score >= UPDATE_THRESHOLD
            ):
                # Update in place. Same memory_id, refreshed text + embedding.
                conn.execute(
                    "UPDATE semantic_memory SET text = ?, embedding = ?, created_at = ? "
                    "WHERE id = ?",
                    (fact["text"], _pack_embedding(vec), time.time(), best_row["id"]),
                )
                events.append({
                    "op": "UPDATE", "memory_id": int(best_row["id"]),
                    "text": fact["text"], "score": best_score,
                })
            else:
                cur = conn.execute(
                    "INSERT INTO semantic_memory(scope, text, embedding, created_at) "
                    "VALUES (?, ?, ?, ?)",
                    (scope, fact["text"], _pack_embedding(vec), time.time()),
                )
                events.append({
                    "op": "ADD", "memory_id": _last_id(cur),
                    "text": fact["text"], "score": 0.0,
                })
        # One transaction per add() call: commit all fact ops atomically so a
        # mid-loop failure rolls back the whole add rather than leaving a
        # partial write.
        conn.commit()
    finally:
        conn.close()
    return events


def search(
    query: str,
    *,
    scope: str,
    top_k: int = 5,
    db_path: str | os.PathLike[str] | None = None,
    embedder: Embedder | None = None,
) -> list[dict]:
    """Cosine-rank memories in ``scope`` against ``query``. The scope filter
    is mandatory — empty/whitespace raises ValueError."""
    if not isinstance(scope, str) or not scope.strip():
        raise ValueError("scope must be a non-empty string")
    if not isinstance(query, str) or not query.strip():
        raise ValueError("query must be a non-empty string")
    if top_k <= 0:
        raise ValueError("top_k must be positive")

    db = _resolve_db_path(db_path)
    emb_obj = embedder or get_embedder()
    qv = emb_obj.embed([query])[0]

    conn = _connect(db)
    try:
        rows = conn.execute(
            "SELECT id, text, embedding FROM semantic_memory WHERE scope = ?",
            (scope,),
        ).fetchall()
    finally:
        conn.close()

    scored: list[tuple[float, sqlite3.Row]] = []
    for r in rows:
        v = _unpack_embedding(r["embedding"])
        scored.append((_cosine(qv, v), r))
    scored.sort(key=lambda t: t[0], reverse=True)
    return [
        {"memory_id": int(r["id"]), "text": r["text"], "score": float(s)}
        for s, r in scored[:top_k]
    ]
