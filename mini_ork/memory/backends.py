"""Memory backend seam — ContextNest primary, SQLite for every install.

Design of record: ``docs/architecture/memory-backend.md``.

S1 moves one store — ``semantic_memory`` / ``semantic_memory_uses`` — behind
one protocol with two implementations:

  * :class:`SqlMemoryBackend` — today's ``memory.semantic`` / ``memory.retirement``
    code path, unchanged in behaviour. It is the contract the other backend is
    written against, not a degraded stub.
  * :class:`CnMemoryBackend` — the same protocol over ``mini_ork.cn_client``
    (``POST /api/v1/tools/store`` for upsert, ``POST /api/v1/agent/outcome`` for
    record_use, ``POST /api/v1/tools/retrieve`` for recall).

``resolve_backend()`` picks one per call from ``MO_MEMORY_BACKEND=auto|cn|sql``.
``auto`` selects CN only when ``cn_client.available()`` is true and otherwise
falls back to SQL, logging one line naming the chosen backend and the reason.
``cn`` is strict: with CN unreachable, ``recall`` raises
:class:`MemoryBackendUnavailable` — it never returns ``[]``, because a silent
empty answer fakes "no memories" and corrupts the learning loop.

Everything CN-bound goes through ``mini_ork.cn_client``; this module owns no
socket and no second availability probe.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from collections.abc import Callable, Sequence
from typing import Any, Protocol, runtime_checkable

from mini_ork import cn_client
from mini_ork.memory import semantic

logger = logging.getLogger(__name__)


class MemoryBackendUnavailable(RuntimeError):
    """The selected backend cannot serve memory right now.

    Raised by the strict CN backend — a caller that asked for CN must never get
    a silent ``[]`` that looks like "nothing relevant was remembered".
    """


def _content_key(text: str) -> str:
    """Content-hash identity for a write that supplied no explicit key.

    Mirrors the ``stable_fragment_id`` dedupe CN applies to ``tools/store``, so a
    replay of the same fact is a no-op on either backend.
    """
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


@runtime_checkable
class MemoryBackend(Protocol):
    """The one seam both stores implement (design doc §2)."""

    def recall(self, query: str, *, scope: str = "semantic", k: int = 5,
               **kwargs: Any) -> list[dict]:
        ...

    def record_use(self, hit_ids: Sequence[Any], outcome: str = "win", *,
                   run_id: str = "", scope: str = "semantic",
                   **kwargs: Any) -> int:
        ...

    def upsert(self, text: str, *, scope: str = "semantic",
               key: str | None = None, meta: dict | None = None,
               **kwargs: Any) -> Any:
        ...

    def list_kind(self, kind: str = "semantic", *, scope: str | None = None,
                  limit: int = 50, **kwargs: Any) -> list[dict]:
        ...

    def retire(self, ids: Sequence[Any], reason: str,
               **kwargs: Any) -> Any:
        ...


# ── SQLite backend (today's code path, unchanged) ──────────────────────────


class SqlMemoryBackend:
    """The SQLite implementation — the contract every backend is written against."""

    name = "sql"

    def __init__(self, *, db_path: str | os.PathLike[str] | None = None,
                 pending_cn: bool = False, fallback_reason: str = "",
                 **_: Any) -> None:
        self._db_path = db_path
        # True when resolve_backend() chose SQL because CN was unreachable:
        # every write is then marked pending_cn=1 for `mini-ork memory sync`.
        self.pending_cn = bool(pending_cn)
        self.fallback_reason = fallback_reason

    def _db(self, db_path: str | os.PathLike[str] | None = None):
        return semantic._resolve_db_path(
            db_path if db_path is not None else self._db_path
        )

    def recall(self, query: str, *, scope: str = "semantic", k: int = 5,
               db_path: str | os.PathLike[str] | None = None,
               **_: Any) -> list[dict]:
        """Rank active memories in ``scope`` for ``query`` (SimUtil-UCB)."""
        return semantic.search(query, scope=scope, top_k=k, db_path=self._db(db_path))

    def upsert(self, text: str, *, scope: str = "semantic",
               key: str | None = None, meta: dict | None = None,
               pending_cn: bool | None = None,
               db_path: str | os.PathLike[str] | None = None,
               **_: Any) -> int:
        """Insert/update the keyed memory; mark it pending when CN is down.

        ``key`` defaults to a content hash so the protocol's one-argument form
        (used by the contract tests and by ``memory sync`` for unkeyed facts)
        is idempotent.
        """
        identity = key if key is not None else _content_key(text)
        mark = self.pending_cn if pending_cn is None else bool(pending_cn)
        return semantic.upsert(
            text, scope=scope, key=identity, db_path=self._db(db_path),
            pending_cn=mark,
        )

    def record_use(self, hit_ids: Sequence[Any], outcome: str = "win", *,
                   run_id: str = "", scope: str = "semantic",
                   task_class: str = "", lane: str = "", node_id: str = "",
                   db_path: str | os.PathLike[str] | None = None,
                   **_: Any) -> int:
        """Record that these memories were used and how the run ended.

        With ``run_id`` the ledger path is used (``record_retrievals`` then
        ``record_outcome``), which is the production shape. Without one — the
        contract test, or an ad-hoc caller — the denormalized counters are bumped
        directly so a single ``outcome="win"`` moves utility off neutral. Only
        ``uses`` and ``wins`` move; the ranking weights are untouched (c:3).
        """
        ids = [int(i) for i in hit_ids]
        if not ids:
            return 0
        db = self._db(db_path)
        if run_id:
            recorded = semantic.record_retrievals(
                ids, scope=scope, run_id=run_id, task_class=task_class,
                lane=lane, node_id=node_id, db_path=db,
            )
            if outcome in ("win", "loss"):
                semantic.record_outcome(run_id, outcome == "win", db_path=db)
            return recorded
        win = 1 if outcome == "win" else 0
        conn = semantic._connect(db)
        try:
            placeholders = ",".join("?" * len(ids))
            conn.execute(
                f"UPDATE semantic_memory SET uses = uses + 1, wins = wins + ? "
                f"WHERE scope = ? AND id IN ({placeholders})",
                (win, scope, *ids),
            )
            conn.commit()
        finally:
            conn.close()
        return len(ids)

    def list_kind(self, kind: str = "semantic", *, scope: str | None = None,
                  limit: int = 50,
                  db_path: str | os.PathLike[str] | None = None,
                  **_: Any) -> list[dict]:
        """Active records of ``kind``, newest first, filtered by ``scope``.

        ``semantic_memory`` has no separate ``kind`` column: the kind IS the
        table, and ``scope`` is the namespace filter.
        """
        if kind not in ("semantic", "semantic_memory"):
            raise ValueError(
                f"unknown memory kind {kind!r}: the SQL backend serves "
                f"'semantic' in S1 (docs/architecture/memory-backend.md §7)"
            )
        db = self._db(db_path)
        conn = semantic._connect(db)
        try:
            if scope is None:
                rows = conn.execute(
                    "SELECT id, scope, text, uses, wins, created_at "
                    "FROM semantic_memory WHERE retired_at = 0 "
                    "ORDER BY created_at DESC, id DESC LIMIT ?",
                    (int(limit),),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT id, scope, text, uses, wins, created_at "
                    "FROM semantic_memory WHERE scope = ? AND retired_at = 0 "
                    "ORDER BY created_at DESC, id DESC LIMIT ?",
                    (scope, int(limit)),
                ).fetchall()
        finally:
            conn.close()
        return [dict(r) for r in rows]

    def retire(self, ids: Sequence[Any], reason: str, *,
               evidence_ref: str = "",
               db_path: str | os.PathLike[str] | None = None,
               **_: Any) -> dict[int, bool]:
        """Flag each id retired with a mandatory reason. Never a DELETE."""
        from mini_ork.memory import retirement

        db = self._db(db_path)
        return {
            int(mid): retirement.retire(
                int(mid), reason, evidence_ref, db_path=db
            )
            for mid in ids
        }


# ── ContextNest backend (HTTP via cn_client only) ──────────────────────────


class CnMemoryBackend:
    """The ContextNest implementation of :class:`MemoryBackend`."""

    name = "cn"

    def __init__(self, *, db_path: str | os.PathLike[str] | None = None,
                 pending_cn: bool = False, fallback_reason: str = "",
                 **_: Any) -> None:
        self._db_path = db_path
        self.fallback_reason = fallback_reason

    def _require(self, operation: str) -> None:
        if not cn_client.available():
            raise MemoryBackendUnavailable(
                f"memory backend 'cn' cannot {operation}: the ContextNest daemon "
                "is unreachable. Strict MO_MEMORY_BACKEND=cn refuses to invent "
                "an empty answer; start ContextNest or use "
                "MO_MEMORY_BACKEND=auto|sql."
            )

    def recall(self, query: str, *, scope: str = "semantic", k: int = 5,
               **_: Any) -> list[dict]:
        """``POST /api/v1/tools/retrieve`` — nearest fragments for ``query``."""
        self._require("recall")
        raw = cn_client.retrieve(query, limit=k)
        if not raw or raw.strip() == "{}":
            # available() said up, but the retrieve POST produced no body. That
            # is a failed read, not "no memories" — never hand back [].
            raise MemoryBackendUnavailable(
                "memory backend 'cn' recall failed: POST /api/v1/tools/retrieve "
                "returned no body"
            )
        try:
            payload = json.loads(raw)
        except ValueError as exc:
            raise MemoryBackendUnavailable(
                "memory backend 'cn' recall failed: ContextNest returned "
                "non-JSON from /api/v1/tools/retrieve"
            ) from exc
        hits: list[dict] = []
        items = payload.get("hits") if isinstance(payload, dict) else None
        for item in items or []:
            if not isinstance(item, dict):
                continue
            hits.append({
                "memory_id": (
                    item.get("id") or item.get("atom_id")
                    or item.get("memory_id") or ""
                ),
                "text": item.get("content") or item.get("text") or "",
                "score": float(item.get("similarity") or item.get("score") or 0.0),
            })
        return hits

    def upsert(self, text: str, *, scope: str = "semantic",
               key: str | None = None, meta: dict | None = None,
               importance: float | None = None, **_: Any) -> str:
        """``POST /api/v1/tools/store`` — content-hash dedupe handles replay."""
        self._require("upsert")
        identity = key if key is not None else _content_key(text)
        metadata: dict = {"kind": "semantic_memory", "scope": scope, "key": identity}
        if meta:
            metadata.update(meta)
        raw = cn_client.store(text, metadata=metadata, importance=importance)
        if not raw or raw.strip() == "{}":
            raise MemoryBackendUnavailable(
                "memory backend 'cn' upsert failed: POST /api/v1/tools/store "
                "returned no body"
            )
        try:
            payload = json.loads(raw)
        except ValueError:
            return identity
        if isinstance(payload, dict):
            return str(
                payload.get("atom_id") or payload.get("id")
                or payload.get("stable_fragment_id") or identity
            )
        return identity

    def record_use(self, hit_ids: Sequence[Any], outcome: str = "win", *,
                   run_id: str = "", scope: str = "semantic",
                   **_: Any) -> int:
        """``POST /api/v1/agent/outcome`` — the substrate's utility signal."""
        self._require("record_use")
        ids = [str(i) for i in hit_ids if str(i)]
        if not ids:
            return 0
        # The wire vocabulary is success/failure; the protocol's is win/loss.
        wire = {"win": "success", "loss": "failure"}.get(outcome, outcome)
        result = cn_client.settle_outcome_batch(wire, ids, session_id=run_id)
        if result.get("status") == "pending":
            raise MemoryBackendUnavailable(
                "memory backend 'cn' record_use failed: "
                f"{result.get('reason', 'agent/outcome did not settle')}"
            )
        return len(ids)

    def list_kind(self, kind: str = "semantic", *, scope: str | None = None,
                  limit: int = 50, **_: Any) -> list[dict]:
        """Honest gap: CN has no metadata-filtered list endpoint yet.

        ``GET /api/v1/memory/list?kind=&namespace=&since=&limit=`` is the
        ContextNest-side work in design §4.1.1. Returning ``[]`` here would
        claim "no records" for a question CN cannot answer, so this raises.
        """
        raise NotImplementedError(
            "CN listing: docs/architecture/memory-backend.md §4.1.1"
        )

    def retire(self, ids: Sequence[Any], reason: str, **_: Any) -> dict[str, bool]:
        """``POST /api/v1/tools/discard`` — remove ids from the active set."""
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("reason must be a non-empty string")
        self._require("retire")
        atom_ids = [str(i) for i in ids if str(i)]
        if not atom_ids:
            return {}
        raw = cn_client.discard(atom_ids, reason=reason)
        if not raw or raw.strip() == "{}":
            raise MemoryBackendUnavailable(
                "memory backend 'cn' retire failed: POST /api/v1/tools/discard "
                "returned no body"
            )
        return {aid: True for aid in atom_ids}


# ── registry + selection ───────────────────────────────────────────────────

#: name -> zero-or-kwarg factory. Follows the "Python runtime registries"
#: idiom: new backends plug in here without editing resolve_backend().
BACKENDS: dict[str, Callable[..., MemoryBackend]] = {}


def register_backend(name: str, factory: Callable[..., MemoryBackend]) -> None:
    """Register (or replace) a memory backend factory."""
    BACKENDS[name] = factory


class FallbackMemoryBackend:
    """``auto`` selection: ContextNest first, SQLite when a CN operation fails.

    ``available()`` says the daemon answers a health probe; it does not promise
    that a *retrieve* will. Measured on this box (2026-11-25): a retrieve that
    embeds a query took 17.5 s against an 8 s read budget, so a healthy daemon
    still failed every recall. Raising there would fail the run; returning ``[]``
    would claim nothing was remembered. So ``auto`` falls back to SQLite, logs
    which store actually served the answer, and marks writes ``pending_cn`` for
    ``mini-ork memory sync``. Strict ``MO_MEMORY_BACKEND=cn`` keeps raising —
    that mode exists to refuse a local answer, not to hide a broken daemon.
    """

    name = "auto"

    def __init__(self, primary: MemoryBackend, fallback: SqlMemoryBackend,
                 reason: str = "") -> None:
        self._primary = primary
        self._fallback = fallback
        self.fallback_reason = reason

    def _delegate(self, operation: str, *args: Any, **kwargs: Any) -> Any:
        try:
            return getattr(self._primary, operation)(*args, **kwargs)
        except (MemoryBackendUnavailable, NotImplementedError) as exc:
            logger.warning(
                "memory backend: sql (cn %s failed, falling back: %s)",
                operation, exc,
            )
            return getattr(self._fallback, operation)(*args, **kwargs)

    def recall(self, query: str, **kwargs: Any) -> list[dict]:
        return self._delegate("recall", query, **kwargs)

    def record_use(self, hit_ids: Sequence[Any], outcome: str = "win",
                   **kwargs: Any) -> int:
        return self._delegate("record_use", hit_ids, outcome, **kwargs)

    def upsert(self, text: str, **kwargs: Any) -> Any:
        return self._delegate("upsert", text, **kwargs)

    def list_kind(self, kind: str = "semantic", **kwargs: Any) -> list[dict]:
        return self._delegate("list_kind", kind, **kwargs)

    def retire(self, ids: Sequence[Any], reason: str, **kwargs: Any) -> Any:
        return self._delegate("retire", ids, reason, **kwargs)


def resolve_backend(*, db_path: str | os.PathLike[str] | None = None,
                    **kwargs: Any) -> MemoryBackend:
    """Select the backend for one call from ``MO_MEMORY_BACKEND``.

    ``auto`` (the default) selects CN only when ``cn_client.available()`` is
    true; otherwise it falls back to SQL and logs the reason. ``cn`` is strict —
    it never falls back, so ``recall`` raises when CN is down rather than
    returning a misleading ``[]``. Exactly one line is logged naming the chosen
    backend and, on fallback, the reason.
    """
    choice = (os.environ.get("MO_MEMORY_BACKEND") or "auto").strip().lower()
    if choice not in ("auto", "cn", "sql"):
        raise ValueError(
            f"MO_MEMORY_BACKEND={choice!r} is not one of auto|cn|sql"
        )

    fallback_reason = ""
    if choice == "auto":
        if cn_client.available():
            name = "cn"
        else:
            name = "sql"
            fallback_reason = (
                "cn_disabled" if cn_client._disabled() else "cn_unavailable"
            )
    else:
        name = choice

    factory = BACKENDS.get(name)
    if factory is None:  # pragma: no cover - built-ins are always registered
        raise ValueError(f"MO_MEMORY_BACKEND={name!r} has no registered backend")

    options: dict = dict(kwargs)
    if db_path is not None:
        options["db_path"] = db_path
    if name == "sql" and fallback_reason:
        options["pending_cn"] = True
        options["fallback_reason"] = fallback_reason
    backend = factory(**options)

    if choice == "auto" and name == "cn":
        # A reachable daemon is not a working retrieve (see FallbackMemoryBackend
        # docstring: 17.5 s retrieve against an 8 s budget on 2026-11-25). In
        # ``auto`` a failed CN operation must degrade to SQLite, never fail the
        # run and never return an empty answer; the SQL half marks writes
        # pending_cn so `mini-ork memory sync` replays them.
        backend = FallbackMemoryBackend(
            backend,
            SqlMemoryBackend(**{**options, "pending_cn": True,
                                "fallback_reason": "cn_op_failed"}),
        )

    if fallback_reason:
        logger.info(
            "memory backend: %s (auto fallback: %s)", name, fallback_reason
        )
    else:
        logger.info("memory backend: %s (MO_MEMORY_BACKEND=%s)", name, choice)
    return backend


register_backend("sql", SqlMemoryBackend)
register_backend("cn", CnMemoryBackend)
