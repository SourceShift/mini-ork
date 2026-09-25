"""Standalone semantic long-term memory (mem-a) with a utility-aware
retrieval policy (mem-b).

Mem0-style ADD/UPDATE/DELETE reconcile over a per-scope SQLite index, with a
default stdlib-only HashEmbedder so the module imports and runs with zero new
pip dependency. A real Embedder is wired behind a thin provider stub that
activates only when ``MO_EMBED_PROVIDER`` is set (no third-party import path
on the default branch).

Retrieval ranks on relevance *plus* historical utility plus an exploration
bonus (RetroAgent SimUtil-UCB, arXiv 2603.08561) rather than similarity alone
— see the ``W_UTILITY`` / ``W_EXPLORE`` block below for the formula. The
utility signal comes from a retrieval ledger: ``record_retrievals()`` is
called when memories are injected into a prompt, and ``record_outcome()``
resolves those rows to win/loss once the run's result is known. Relevance
still gates — utility and exploration only reorder what similarity admitted.

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


# ── SimUtil-UCB retrieval weights (RetroAgent 2603.08561) ───────────────────
#
# Ranking by similarity alone cannot separate two memories that read alike but
# have different track records: a vague memory similar to everything is
# retrieved forever, while a narrow one that actually worked keeps losing on
# wording. These weights add the missing signal — how useful this memory has
# been, and how much is still unknown about it.
#
#   score = sim + W_UTILITY * (utility - 0.5) + W_EXPLORE * exploration
#
#   utility     = (wins + 1) / (uses + 2)     Laplace-smoothed pass rate; the
#               Beta(1,1) posterior mean, so an untried memory sits at 0.5 and
#               contributes nothing. Smoothing rather than a raw ratio is what
#               creates the "never fully buried" floor DeltaMem asks for: a
#               memory with 0 wins from 1 use scores 1/3, not 0, and recovers.
#               It also protects against the credit-assignment noise the survey
#               flags — one unlucky retrieval cannot condemn a good memory.
#   exploration = sqrt(ln(N_total + 1) / (uses + 1))   UCB1 bonus, largest for
#               memories never retrieved, decaying as evidence accumulates.
#
# The subtraction of 0.5 centres utility: a memory must do better than
# chance-and-neutral to gain, and the exploration term is what gives an unproven
# memory its first chance.
#
# CRITICAL: both terms only REORDER candidates that relevance already admitted.
# Exploration must not be able to drag an irrelevant memory into a prompt — the
# survey's own caution is that the bonus "deliberately surfaces untested
# memories", which is exactly what you must not let it surface. Relevance
# selects the candidate pool; utility and exploration order it.
#
# The pool is the top (top_k * POOL_FACTOR) by similarity. Using a relative pool
# rather than an absolute similarity floor is deliberate: HashEmbedder cosines
# are signed, so "sim > 0" would silently drop legitimate memories, and any
# fixed floor is a magic number that has to be re-tuned per embedder.
W_UTILITY = 0.30
W_EXPLORE = 0.10
UTILITY_NEUTRAL = 0.5
POOL_FACTOR = 4


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
# db/migrations/0046_semantic_memory.sql, 0055_semantic_memory_utility.sql and
# 0058_semantic_memory_attribution.sql, so the module bootstraps a tmp DB
# without requiring the migration loader to have run. Kept in lock-step with
# the .sql files by hand. Re-running this block on an existing DB is a no-op
# (IF NOT EXISTS).
_BOOTSTRAP_SQL = """
CREATE TABLE IF NOT EXISTS semantic_memory (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  scope      TEXT    NOT NULL,
  text       TEXT    NOT NULL,
  embedding  BLOB    NOT NULL,
  created_at REAL    NOT NULL,
  meta       TEXT,
  uses       INTEGER NOT NULL DEFAULT 0,
  wins       INTEGER NOT NULL DEFAULT 0,
  retired_at      REAL    NOT NULL DEFAULT 0,
  retire_reason   TEXT    NOT NULL DEFAULT '',
  retire_evidence TEXT    NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_semantic_memory_scope
  ON semantic_memory(scope);
CREATE TABLE IF NOT EXISTS semantic_memory_uses (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  memory_id    INTEGER NOT NULL,
  scope        TEXT    NOT NULL,
  run_id       TEXT    NOT NULL DEFAULT '',
  task_class   TEXT    NOT NULL DEFAULT '',
  lane         TEXT    NOT NULL DEFAULT '',
  node_id      TEXT    NOT NULL DEFAULT '',
  retrieved_at REAL    NOT NULL,
  outcome      TEXT    NOT NULL DEFAULT 'pending'
               CHECK (outcome IN ('pending','win','loss'))
);
CREATE INDEX IF NOT EXISTS idx_semantic_memory_uses_run
  ON semantic_memory_uses(run_id);
CREATE INDEX IF NOT EXISTS idx_semantic_memory_uses_memory
  ON semantic_memory_uses(memory_id);
"""

# Columns added after their table's CREATE shipped, keyed by table. A DB created
# at an earlier version — or by an earlier version of this module's bootstrap —
# has the table without them, and `CREATE TABLE IF NOT EXISTS` will not add
# them, so the presence of the table is not evidence the columns are there.
# Guarded ALTER, same shape as mini_ork/stores/migrate.py::_ensure_column.
#
# The ledger's ``lane`` / ``node_id`` are LIMBO's accounting half (arXiv
# 2609.14138): a retrieval injects tokens into a prompt, and until the decision
# that caused it is recorded, that spend cannot be attributed to a lane or
# weighed against what the memories bought. The table names here are module
# constants, never caller input.
_ADDED_COLUMNS: dict[str, tuple[tuple[str, str], ...]] = {
    "semantic_memory": (
        ("uses", "INTEGER NOT NULL DEFAULT 0"),
        ("wins", "INTEGER NOT NULL DEFAULT 0"),
        ("retired_at", "REAL NOT NULL DEFAULT 0"),
        ("retire_reason", "TEXT NOT NULL DEFAULT ''"),
        ("retire_evidence", "TEXT NOT NULL DEFAULT ''"),
    ),
    "semantic_memory_uses": (
        ("lane", "TEXT NOT NULL DEFAULT ''"),
        ("node_id", "TEXT NOT NULL DEFAULT ''"),
    ),
}


def _ensure_columns(conn: sqlite3.Connection) -> None:
    for table, columns in _ADDED_COLUMNS.items():
        have = {r[1] for r in conn.execute(f"PRAGMA table_info('{table}')")}
        if not have:
            # Table absent entirely: _BOOTSTRAP_SQL creates it with every
            # column already, so there is nothing to upgrade.
            continue
        for name, ddl in columns:
            if name not in have:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")


def _connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    # Self-bootstrap. Idempotent — the SQL uses IF NOT EXISTS, so applying it
    # to a DB that already has the tables is a no-op, and _ensure_columns
    # upgrades a table that predates the utility columns. The migration loader
    # is not on the test path; this is how the module guarantees the schema
    # exists when called from a fresh tmp DB.
    conn.executescript(_BOOTSTRAP_SQL)
    _ensure_columns(conn)
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
            "SELECT id, text, embedding FROM semantic_memory "
            "WHERE scope = ? AND retired_at = 0",
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


def upsert(
    text: str,
    *,
    scope: str,
    key: str,
    db_path: str | os.PathLike[str] | None = None,
    embedder: Embedder | None = None,
) -> int:
    """Insert or update the memory in ``scope`` identified by ``key``.

    Identity comes from ``key`` (kept in the ``meta`` column), *not* from
    similarity. That is the difference from ``add()``, and it matters for any
    caller mirroring a source table: two rows of the source that happen to read
    alike are still two rows, and ``add()``'s similarity reconcile
    (``UPDATE_THRESHOLD``) would silently collapse them into one. A mirror needs
    "one memory per source row", which only a real key can give it.

    A re-upsert refreshes the text and embedding and preserves ``uses`` /
    ``wins`` — the track record belongs to the key, not to the wording — so a
    mirror can re-sync on every read without eroding what it has learned.
    Returns the memory id.
    """
    if not isinstance(scope, str) or not scope.strip():
        raise ValueError("scope must be a non-empty string")
    if not isinstance(key, str) or not key.strip():
        raise ValueError("key must be a non-empty string")
    if not isinstance(text, str) or not text.strip():
        raise ValueError("text must be a non-empty string")

    db = _resolve_db_path(db_path)
    vec = (embedder or get_embedder()).embed([text])[0]
    meta = json.dumps({"key": key}, sort_keys=True)
    now = time.time()

    conn = _connect(db)
    try:
        row = conn.execute(
            "SELECT id FROM semantic_memory WHERE scope = ? AND meta = ?",
            (scope, meta),
        ).fetchone()
        if row is None:
            cur = conn.execute(
                "INSERT INTO semantic_memory"
                "(scope, text, embedding, created_at, meta) VALUES (?, ?, ?, ?, ?)",
                (scope, text, _pack_embedding(vec), now, meta),
            )
            mid = _last_id(cur)
        else:
            mid = int(row["id"])
            # Text and embedding only — uses/wins are untouched on purpose.
            conn.execute(
                "UPDATE semantic_memory SET text = ?, embedding = ?, created_at = ? "
                "WHERE id = ?",
                (text, _pack_embedding(vec), now, mid),
            )
        conn.commit()
    finally:
        conn.close()
    return mid


def _utility(uses: int, wins: int) -> float:
    """Laplace-smoothed win rate — the Beta(1,1) posterior mean.

    Smoothing is what gives the "never fully buried" floor for free: a memory
    with no wins from one use scores 1/3 rather than 0, so a single
    unattributed retrieval cannot consign it to the bottom forever. It also
    damps credit-assignment noise, which matters because attribution here is
    "retrieved during a run that passed" — evidence, not proof.
    """
    return (wins + 1.0) / (uses + 2.0)


def _exploration(uses: int, n_total: int) -> float:
    """UCB1 bonus: ``sqrt(ln N / (n + 1))`` over the scope's total retrievals.

    Largest for a memory never retrieved, shrinking as it is used and as the
    scope as a whole accumulates evidence. The ``+1`` mirrors the ``+2`` in
    ``_utility`` — it keeps the very first retrieval from dividing by zero and
    makes the untried case finite rather than infinite.
    """
    return math.sqrt(math.log(n_total + 1.0) / (uses + 1.0))


def search(
    query: str,
    *,
    scope: str,
    top_k: int = 5,
    db_path: str | os.PathLike[str] | None = None,
    embedder: Embedder | None = None,
) -> list[dict]:
    """Rank memories in ``scope`` against ``query`` by utility-aware
    similarity (SimUtil-UCB). The scope filter is mandatory —
    empty/whitespace raises ValueError.

    Relevance admits the candidates; utility and exploration reorder them.
    The candidate pool is the top ``top_k * POOL_FACTOR`` by cosine, and the
    returned list is that pool re-sorted by the composite and cut to
    ``top_k``. ``score`` is the composite (what the ranking used) —
    ``similarity``, ``utility``, ``uses`` and ``wins`` are returned alongside
    it so a caller can see why a memory placed where it did.
    """
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
            "SELECT id, text, embedding, uses, wins "
            "FROM semantic_memory WHERE scope = ? AND retired_at = 0",
            (scope,),
        ).fetchall()
        # The N in UCB1's ln(N) is the scope's total retrieval count, not the
        # row's — it is what makes the bonus shrink as the scope as a whole
        # accumulates evidence. Read once, outside the per-row loop.
        n_total = sum(int(r["uses"]) for r in rows)
    finally:
        conn.close()

    # Gate: relevance chooses the pool.
    by_sim: list[tuple[float, sqlite3.Row]] = []
    for r in rows:
        by_sim.append((_cosine(qv, _unpack_embedding(r["embedding"])), r))
    by_sim.sort(key=lambda t: t[0], reverse=True)
    pool = by_sim[: max(top_k * POOL_FACTOR, top_k)]

    # Reorder: utility and exploration act only inside the pool.
    ranked: list[tuple[float, float, float, int, int, sqlite3.Row]] = []
    for sim, r in pool:
        uses = int(r["uses"])
        wins = int(r["wins"])
        utility = _utility(uses, wins)
        exploration = _exploration(uses, n_total)
        composite = (
            sim
            + W_UTILITY * (utility - UTILITY_NEUTRAL)
            + W_EXPLORE * exploration
        )
        ranked.append((composite, sim, utility, uses, wins, r))
    ranked.sort(key=lambda t: t[0], reverse=True)

    return [
        {
            "memory_id": int(r["id"]),
            "text": r["text"],
            "score": float(composite),
            "similarity": float(sim),
            "utility": float(utility),
            "uses": uses,
            "wins": wins,
        }
        for composite, sim, utility, uses, wins, r in ranked[:top_k]
    ]


def rank_with_prior(
    candidates: Sequence[tuple[int, float]],
    *,
    scope: str,
    top_k: int = 5,
    db_path: str | os.PathLike[str] | None = None,
) -> list[dict]:
    """Rank keyed memories by an external prior, adjusted by utility and
    exploration (SimUtil-UCB without a query).

    ``search()`` answers "which of these is closest to the query"; this
    answers "which of these has the best claim to be shown", for callers whose
    candidate set is already decided — a mirrored source table, say — and
    whose relevance signal lives outside the store. Same policy, different
    relevance term: the prior stands in for similarity, and it keeps the same
    role — **the prior gates, utility and exploration only reorder inside the
    gate.** The pool is the top ``top_k * POOL_FACTOR`` candidates by prior,
    and the return is that pool re-sorted by the composite and cut to
    ``top_k``.

    Gate and reorder on the same scale is what makes the policy meaningful
    here. If utility could reach any candidate, a single lucky retrieval would
    outrank a pattern observed forty times more often; if it could reach none,
    the record would be decorative. Confining the record to reordering *near
    peers* is the middle it is meant to be, and it is exactly the rule
    ``search()`` applies to cosine similarity.

    The prior is min-max normalised over the pool, so:

      * **An untried scope reproduces the prior's order exactly.** With no
        retrievals anywhere, ``n_total`` is 0, so exploration is 0 and every
        utility is the neutral 0.5 — the composite collapses to the normalised
        prior and nothing moves. A caller introducing this ranking changes
        nothing until evidence exists to change it.
      * **Exploration is the only way an untried memory is ever tried.** Its
        bonus is identical for every unused candidate, so they keep their
        prior order among themselves while the group drifts up relative to
        used ones as the scope's evidence grows. Without that drift, whichever
        memories were sampled first would be sampled forever.

    ``candidates`` is ``(memory_id, prior)`` pairs; ids outside ``scope``, or
    that do not exist, are dropped. Returns the same dict shape as
    ``search()``, with ``prior`` in place of ``similarity``.
    """
    if not isinstance(scope, str) or not scope.strip():
        raise ValueError("scope must be a non-empty string")
    if top_k <= 0:
        raise ValueError("top_k must be positive")
    prior_of = {int(mid): float(prior) for mid, prior in candidates}
    if not prior_of:
        return []
    wanted = list(prior_of)

    db = _resolve_db_path(db_path)
    conn = _connect(db)
    try:
        placeholders = ",".join("?" * len(wanted))
        rows = conn.execute(
            f"SELECT id, text, uses, wins FROM semantic_memory "
            f"WHERE scope = ? AND id IN ({placeholders}) AND retired_at = 0",
            (scope, *wanted),
        ).fetchall()
        n_total = int(conn.execute(
            "SELECT COALESCE(SUM(uses), 0) FROM semantic_memory "
            "WHERE scope = ? AND retired_at = 0",
            (scope,),
        ).fetchone()[0] or 0)
    finally:
        conn.close()

    # Gate: the prior chooses the pool.
    rows.sort(key=lambda r: prior_of[int(r["id"])], reverse=True)
    pool = rows[: max(top_k * POOL_FACTOR, top_k)]

    lo = min(prior_of[int(r["id"])] for r in pool) if pool else 0.0
    hi = max(prior_of[int(r["id"])] for r in pool) if pool else 0.0
    span = hi - lo

    out = []
    for r in pool:
        mid = int(r["id"])
        uses, wins = int(r["uses"]), int(r["wins"])
        utility = _utility(uses, wins)
        # A pool with one distinct prior has no order to preserve; the flat
        # 0.0 leaves the ranking to utility and exploration alone.
        norm = (prior_of[mid] - lo) / span if span > 0 else 0.0
        out.append({
            "memory_id": mid,
            "text": r["text"],
            "prior": prior_of[mid],
            "score": norm
                     + W_UTILITY * (utility - UTILITY_NEUTRAL)
                     + W_EXPLORE * _exploration(uses, n_total),
            "utility": utility,
            "uses": uses,
            "wins": wins,
        })
    # Stable sort, so candidates the composite cannot separate keep the order
    # the gate gave them.
    out.sort(key=lambda hit: hit["score"], reverse=True)
    return out[:top_k]


# ── Retrieval ledger (the utility signal's source) ─────────────────────────


def record_retrievals(
    memory_ids: Sequence[int],
    *,
    scope: str,
    run_id: str = "",
    task_class: str = "",
    lane: str = "",
    node_id: str = "",
    db_path: str | os.PathLike[str] | None = None,
) -> int:
    """Log that these memories were injected into a prompt for ``run_id``.

    One pending ledger row per memory, plus a bump of each row's denormalized
    ``uses`` counter. The run's outcome is unknown at injection time, so the
    rows start 'pending' and are resolved at run end by ``record_outcome()``.

    A retrieval that is never resolved counts toward ``uses`` forever and
    never toward ``wins`` — an unattributed retrieval can only make a memory
    look worse, never better. That is deliberate: the failure mode worth
    guarding is a memory silently earning credit it never proved, and failing
    closed is the only way to make an unattributed retrieval harmless.

    ``lane`` and ``node_id`` name the decision that caused the injection. They
    are accounting, not ranking: nothing in ``search()`` or
    ``rank_with_prior()`` reads them. With them recorded, the retrieval spend a
    run incurred can be attributed to the lane and node that chose it
    (LIMBO, arXiv 2609.14138) instead of being invisible; without them the
    ledger says a memory was used but never by whom, so no lane can be held to
    its retrieval cost. Default ``''`` keeps every existing caller unchanged.

    Only ids that actually exist in ``scope`` are recorded — the ledger is the
    authoritative audit trail and should never assert a retrieval of something
    that was not there. Returns the number of retrievals recorded.
    """
    if not isinstance(scope, str) or not scope.strip():
        raise ValueError("scope must be a non-empty string")
    # Dedupe, preserve order: the same memory injected twice in one prompt is
    # one retrieval.
    ids = list(dict.fromkeys(int(m) for m in memory_ids))
    if not ids:
        return 0

    db = _resolve_db_path(db_path)
    now = time.time()
    conn = _connect(db)
    try:
        placeholders = ",".join("?" * len(ids))
        present = [
            int(r["id"]) for r in conn.execute(
                f"SELECT id FROM semantic_memory "
                f"WHERE scope = ? AND id IN ({placeholders}) ORDER BY id",
                (scope, *ids),
            )
        ]
        if not present:
            return 0
        conn.executemany(
            "INSERT INTO semantic_memory_uses"
            "(memory_id, scope, run_id, task_class, lane, node_id, "
            " retrieved_at, outcome) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, 'pending')",
            [(mid, scope, run_id, task_class, lane, node_id, now)
             for mid in present],
        )
        conn.executemany(
            "UPDATE semantic_memory SET uses = uses + 1 WHERE id = ?",
            [(mid,) for mid in present],
        )
        conn.commit()
    finally:
        conn.close()
    return len(present)


def record_outcome(
    run_id: str,
    passed: bool,
    *,
    db_path: str | os.PathLike[str] | None = None,
) -> int:
    """Resolve every pending retrieval for ``run_id`` to win/loss.

    Called at run end, once the run's result is known. A win bumps the
    memory's ``wins``; a loss only closes its ledger row, because ``uses`` was
    already counted at retrieval and a loss is precisely "used, did not help".

    Only 'pending' rows are touched, so this is idempotent — a resumed or
    re-stamped run cannot double-count a win. Returns the number of rows
    resolved.
    """
    if not isinstance(run_id, str) or not run_id.strip():
        raise ValueError("run_id must be a non-empty string")

    db = _resolve_db_path(db_path)
    outcome = "win" if passed else "loss"
    conn = _connect(db)
    try:
        rows = conn.execute(
            "SELECT id, memory_id FROM semantic_memory_uses "
            "WHERE run_id = ? AND outcome = 'pending'",
            (run_id,),
        ).fetchall()
        if not rows:
            return 0
        conn.execute(
            "UPDATE semantic_memory_uses SET outcome = ? "
            "WHERE run_id = ? AND outcome = 'pending'",
            (outcome, run_id),
        )
        if passed:
            # One bump per resolved row, in lock-step with the ledger.
            conn.executemany(
                "UPDATE semantic_memory SET wins = wins + 1 WHERE id = ?",
                [(int(r["memory_id"]),) for r in rows],
            )
        conn.commit()
    finally:
        conn.close()
    return len(rows)


def resolve_finished_runs(
    *,
    db_path: str | os.PathLike[str] | None = None,
) -> int:
    """Resolve pending retrievals for every run whose traces have finished.

    The outcome is already in the database: ``execution_traces`` records a
    status per node, and a run is over when none of its traces is still
    'running'. Sweeping that table at retrieval time closes the feedback loop
    for *every* recipe, not just the one with a ``type: eval`` node — which
    matters because an unresolved retrieval counts toward ``uses`` forever and
    therefore decays a memory's utility toward zero. Silence must not look like
    failure for a run that actually succeeded.

    A run resolves iff it has traces and none is running; it passes iff none
    failed. Runs with **no** traces at all are left pending: there is nothing
    recorded to judge them by, and guessing is the fabrication this whole
    design exists to avoid.

    Idempotent (``record_outcome`` touches only 'pending' rows) and cold-safe —
    a missing ``execution_traces`` table yields 0 rather than raising.

    Call this before reading memories, so a run's own outcome from a previous
    invocation is reflected in the ranking it is about to influence.
    """
    db = _resolve_db_path(db_path)
    conn = _connect(db)
    try:
        try:
            rows = conn.execute(
                """
                SELECT u.run_id AS run_id,
                       COUNT(t.trace_id) AS n,
                       SUM(CASE WHEN t.status = 'running' THEN 1 ELSE 0 END) AS running,
                       SUM(CASE WHEN t.status NOT IN ('success', 'running')
                                THEN 1 ELSE 0 END) AS failed
                FROM semantic_memory_uses u
                JOIN execution_traces t ON t.run_id = u.run_id
                WHERE u.outcome = 'pending' AND u.run_id != ''
                GROUP BY u.run_id
                """
            ).fetchall()
        except sqlite3.OperationalError:
            return 0
    finally:
        conn.close()

    resolved = 0
    for row in rows:
        if int(row["n"] or 0) == 0 or int(row["running"] or 0) > 0:
            continue
        resolved += record_outcome(
            str(row["run_id"]),
            int(row["failed"] or 0) == 0,
            db_path=db,
        )
    return resolved
