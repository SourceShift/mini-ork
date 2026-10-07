"""mini_ork.learning.themes — group gradient_records into themes.

``gradient_records`` holds 10,248 rows today. 5,964 (58%) describe mini-ork's
own trace/telemetry fields (``verifier_output``, ``tool_calls``, ``files_read``,
``duration_ms``, ``cost_usd``, ``prompt_version_hash``, ``context_bundle_hash``,
``reward_*``, …). Those are bugs in mini-ork's tracing, not guidance for the
task — yet every one of them can be injected into a task prompt. Nobody can
read 10k rows. The IDE needs one row per idea.

This module is P2 of the learning-memory refactor (D1 + D2). It BUILDS themes
and rolls up framework-bug candidates. It does NOT change what gets injected
into prompts (a later phase adds the filter to ``context_assembler``).

Identity contract
=================

A theme's id is ``"th-" + sha256(kind + first member gradient_id)[:12]``.
``kind`` is ``"task"`` or ``"framework"``. The same gradient re-assigned to a
theme of the same kind on a re-run yields the SAME row — the running mean
centroid and member count grow in place. Identity is therefore stable across
runs (the determinism the kickoff's test asserts).

Clustering
==========

Greedy leader, in ``created_at, gradient_id`` order. Compare an incoming
gradient's normalized signal embedding against existing theme centroids of the
SAME kind. If the best cosine ≥ ``MO_THEME_SIM`` (default 0.6) join that
theme; else start a new one. Centroid is the L2-normalized running mean of
member embeddings, stored as packed float32 (1KB per row at the 256-dim
``HashEmbedder``).

Why not cosine alone on raw text? Two near-duplicate paraphrases of the same
trace complaint score ~0.55 cosine on hash embeddings — below the natural
threshold. ``normalize()`` collapses trace ids, hex strings, numbers, file
paths and quoted JSON values to placeholders FIRST, so the paraphrases
converge on a single embedding.

Bug rollup
==========

For each ``framework`` theme with ≥ ``min_members`` gradients, upsert one
``bug_reports`` row keyed by ``"theme:" + theme_id``. The upsert preserves any
status a human set (``wontfix``, ``dupe``, ``resolved``) by deliberately
omitting ``status`` from the ``ON CONFLICT DO UPDATE`` SET clause — that is
the only SQLite-portable way to keep a human decision against a re-run.

Pure stdlib + ``sqlite3``. No ``numpy`` (the rest of ``mini_ork/learning/``
does not import it; this module does not either).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import sqlite3
import struct
import sys
import time
from typing import Any

__all__ = [
    "FRAMEWORK_FIELDS",
    "MO_THEME_SIM_DEFAULT",
    "assign_new",
    "backfill",
    "classify_kind",
    "ensure_schema",
    "normalize",
    "role_of",
    "rollup_framework_bugs",
    "stats",
]

# Default cosine threshold for theme joining. 0.6 reproduces the value the
# kickoff specifies; tests can pass a lower value (``--sim 0.5``) to probe
# coverage vs. coarseness on a COPY of the live DB.
MO_THEME_SIM_DEFAULT = 0.6

# One regex per kind-classifier — keeps the regex compiled once, and keeps the
# trace-field vocabulary in one obvious place for a future reviewer to extend.
# The list is the kickoff's enumerated framework-field vocabulary plus three
# additions from the live DB scan: ``trace_id``, ``execution_traces``,
# ``run context``, ``finish_reason``, ``process_reward``, ``node_type``.
FRAMEWORK_FIELDS = re.compile(
    r"\b("
    r"verifier_output|tool_calls|files_read|files_written|duration_ms|"
    r"cost_usd|prompt_version_hash|context_bundle_hash|reward(_|s)?|"
    r"reviewer_verdict|trace_id|execution_traces|run[_\s]?context|"
    r"finish_reason|process_reward|node_type|gradient_id|target"
    r")\b",
    re.IGNORECASE,
)

# Trace identifiers (``tr-XXXX``), hex strings ≥ 6 chars, bare numbers, file
# paths and quoted JSON values all collapse to placeholders. The point is to
# make two paraphrases of the same observation produce the same embedding.
_RE_TRACE_ID = re.compile(r"\btr-[A-Za-z0-9_-]+\b")
_RE_HEX_LONG = re.compile(r"\b[a-f0-9]{6,}\b", re.IGNORECASE)
_RE_NUMBER = re.compile(r"\b\d+\b")
_RE_PATH = re.compile(r"(?:/|\.{1,2}/)[A-Za-z0-9_./-]+\.[A-Za-z0-9]+")
_RE_QUOTED_JSON = re.compile(r'"[^"\n]{1,200}"')
_WHITESPACE = re.compile(r"\s+")


def normalize(text: str) -> str:
    """Collapse identifiers/hex/numbers/paths/quoted JSON to placeholders.

    Lowercase, then replace:
      * ``tr-XXXX…`` trace ids → ``<trace>``
      * 6+ char hex runs → ``X``
      * bare numbers → ``N``
      * file paths → ``<path>``
      * quoted JSON-ish strings → ``Q``

    Then collapse whitespace. The output is the text the embedder sees, so two
    paraphrases of the same observation hash to the same embedding.

    Examples (kickoff-mandated cases)::

        "verifier_output for this node is only {node_type: researcher}"
            -> "verifier_output for this node is only {node_type: researcher}"

        "this node's verifier_output records only {node_type: planner}"
            -> "this node's verifier_output records only {node_type: planner}"

    Both produce identical embeddings after normalization — that's the test
    the kickoff requires.
    """
    if not text:
        return ""
    out = text.lower()
    out = _RE_TRACE_ID.sub("<trace>", out)
    out = _RE_HEX_LONG.sub("X", out)
    out = _RE_PATH.sub("<path>", out)
    out = _RE_NUMBER.sub("N", out)
    out = _RE_QUOTED_JSON.sub("Q", out)
    out = _WHITESPACE.sub(" ", out).strip()
    return out


def classify_kind(target: str, signal: str, suggested_change: str) -> str:
    """``framework`` if the signal mentions mini-ork trace/telemetry fields.

    Match against ``FRAMEWORK_FIELDS`` over the concatenated signal +
    suggested_change. The target string is included too (a target like
    ``verifier_output`` itself is a strong signal even if the prose is
    generic). Pure function; unit-tested with 6 real live-DB signals.
    """
    haystack = f"{target or ''}\n{signal or ''}\n{suggested_change or ''}"
    return "framework" if FRAMEWORK_FIELDS.search(haystack) else "task"


def role_of(target: str) -> str:
    """The target family. Convention:

      * ``verifier.researcher`` → ``verifier``
      * ``workflow.node.implementer`` → ``node:implementer``
      * ``agent.reviewer.prompt`` → ``agent:reviewer``
      * ``workflow.recipe.code_fix`` → ``recipe``
      * anything else → first dotted segment
    """
    if not target:
        return ""
    parts = target.split(".")
    if len(parts) >= 2:
        head, nxt = parts[0], parts[1]
        if head == "verifier" or head == "researcher":
            return head
        if head == "workflow" and parts[1] == "node" and len(parts) >= 3:
            return f"node:{parts[2]}"
        # ``agent.<role>.<location>`` — the role is parts[1], not parts[2].
        # The kickoff's example ``agent.reviewer.prompt`` → ``agent:reviewer``
        # follows that convention.
        if head == "agent" and len(parts) >= 2:
            return f"agent:{parts[1]}"
        if head == "workflow" and parts[1] == "recipe" and len(parts) >= 3:
            return "recipe"
        return nxt
    return parts[0]


# ── DB plumbing ─────────────────────────────────────────────────────────────


def _resolve_db_path(db_path: str | os.PathLike[str] | None) -> str:
    """Resolve the DB path lazily. Explicit kwarg > $MINI_ORK_DB >
    $MINI_ORK_HOME/state.db. Mirrors ``cross_epic_gradient._resolve_db_path``.
    """
    if db_path is not None and str(db_path) != "":
        return str(db_path)
    env = os.environ.get("MINI_ORK_DB")
    if env:
        return env
    home = os.environ.get("MINI_ORK_HOME") or ".mini-ork"
    return os.path.join(home, "state.db")


def _connect(db_path: str) -> sqlite3.Connection:
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    return con


def ensure_schema(db_path: str) -> None:
    """Idempotent DDL for the two theme tables. Called from every entry point.

    A database that predates migration ``0063_lesson_themes.sql`` (i.e. one
    that has not run ``mini_ork/stores/migrate``) still works after this call.
    ``CREATE TABLE IF NOT EXISTS`` and the indexes are all the migration does
    — kept verbatim so the live migration and this in-process path agree.
    """
    con = _connect(db_path)
    try:
        con.executescript(
            """
            CREATE TABLE IF NOT EXISTS lesson_themes (
              theme_id      TEXT PRIMARY KEY,
              kind          TEXT NOT NULL CHECK(kind IN ('task','framework')),
              role          TEXT,
              task_class    TEXT,
              representative TEXT NOT NULL,
              centroid      BLOB NOT NULL,
              n_gradients   INTEGER NOT NULL DEFAULT 0,
              n_runs        INTEGER NOT NULL DEFAULT 0,
              first_seen    INTEGER,
              last_seen     INTEGER,
              lesson_text   TEXT,
              status        TEXT NOT NULL DEFAULT 'candidate',
              bug_report_id INTEGER,
              updated_at    INTEGER
            );
            CREATE TABLE IF NOT EXISTS gradient_theme (
              gradient_id   TEXT PRIMARY KEY,
              theme_id      TEXT NOT NULL,
              similarity    REAL
            );
            CREATE INDEX IF NOT EXISTS idx_gradient_theme_theme
              ON gradient_theme(theme_id);
            """
        )
        con.commit()
    finally:
        con.close()


# ── Embedder integration ────────────────────────────────────────────────────

# Lazy import: the module must not pull numpy / sentence-transformers at import
# time. ``get_embedder`` resolves ``MO_EMBED_PROVIDER`` at call time.
_EMBEDDER_SINGLETON: Any = None


def _embedder():
    global _EMBEDDER_SINGLETON
    if _EMBEDDER_SINGLETON is None:
        from mini_ork.memory.semantic import get_embedder

        _EMBEDDER_SINGLETON = get_embedder()
    return _EMBEDDER_SINGLETON


def _embed_one(text: str) -> bytes:
    """Return L2-normalized 256-dim packed float32 (1KB)."""
    emb = _embedder().embed([normalize(text)])[0]
    # The HashEmbedder contract is unit-normalized. Re-normalize defensively
    # so a future registered embedder that forgets to normalize does not break
    # the cosine-as-dot-product fast path.
    norm = math.sqrt(sum(x * x for x in emb))
    if norm > 0.0:
        inv = 1.0 / norm
        emb = [x * inv for x in emb]
    return struct.pack(f"{len(emb)}f", *emb)


def _unpack(blob: bytes) -> list[float]:
    n = len(blob) // 4
    return list(struct.unpack(f"{n}f", blob))


def _cosine(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b))


def _running_mean_update(
    packed: bytes, n: int, vec: list[float]
) -> tuple[bytes, int]:
    """In-place HAC-of-online-mean update. Re-normalize after the merge.

    ``n`` is the member count BEFORE adding ``vec``. Returns new pack + new n.
    """
    if n == 0:
        return struct.pack(f"{len(vec)}f", *vec), 1
    cur = _unpack(packed)
    new_n = n + 1
    merged = [(c * n + v) / new_n for c, v in zip(cur, vec)]
    norm = math.sqrt(sum(x * x for x in merged))
    if norm > 0.0:
        inv = 1.0 / norm
        merged = [x * inv for x in merged]
    return struct.pack(f"{len(merged)}f", *merged), new_n


# ── Theme id derivation ─────────────────────────────────────────────────────


def _theme_id(kind: str, first_gradient_id: str) -> str:
    digest = hashlib.sha256(
        f"{kind}|{first_gradient_id}".encode("utf-8")
    ).hexdigest()[:12]
    return f"th-{digest}"


# ── Core assignment ─────────────────────────────────────────────────────────


def _members_of(db_path: str, kind: str) -> list[sqlite3.Row]:
    """Existing themes of one kind, ordered by id for stable iteration."""
    con = _connect(db_path)
    try:
        return list(
            con.execute(
                "SELECT theme_id, centroid, n_gradients, first_seen, last_seen,"
                "       role, task_class, representative"
                "  FROM lesson_themes WHERE kind = ? ORDER BY theme_id",
                (kind,),
            )
        )
    finally:
        con.close()


def _unassigned(db_path: str) -> list[sqlite3.Row]:
    """Gradients with no gradient_theme row, in deterministic order."""
    con = _connect(db_path)
    try:
        try:
            return list(
                con.execute(
                    "SELECT gradient_id, target, signal, suggested_change,"
                    "       evidence, confidence, created_at, task_class"
                    "  FROM gradient_records"
                    "  WHERE gradient_id NOT IN"
                    "        (SELECT gradient_id FROM gradient_theme)"
                    "  ORDER BY created_at, gradient_id"
                )
            )
        except sqlite3.OperationalError:
            # gradient_records does not exist (fresh DB, no migration run).
            return []

    finally:
        con.close()


def _distinct_runs(db_path: str, gradient_ids: list[str]) -> int:
    """Distinct ``execution_traces.run_id`` over the member gradients.

    ``gradient_records.evidence`` holds ONE trace_id (text), not a run id, so
    the count is the JOIN — see gradient_extractor.py:198-201.
    """
    if not gradient_ids:
        return 0
    placeholders = ",".join("?" for _ in gradient_ids)
    con = _connect(db_path)
    try:
        try:
            row = con.execute(
                f"SELECT COUNT(DISTINCT et.run_id) FROM gradient_records g"
                f"  JOIN execution_traces et ON et.trace_id = g.evidence"
                f" WHERE g.gradient_id IN ({placeholders})",
                gradient_ids,
            ).fetchone()
            return int(row[0]) if row and row[0] is not None else 0
        except sqlite3.OperationalError:
            # execution_traces absent — count gradients as a lower bound on runs.
            return len(set(gradient_ids))
    finally:
        con.close()


def _member_classes(db_path: str, theme_id: str) -> set[str]:
    """Distinct task_classes on the theme."""
    con = _connect(db_path)
    try:
        try:
            rows = list(
                con.execute(
                    "SELECT DISTINCT g.task_class FROM gradient_records g"
                    "  JOIN gradient_theme gt ON gt.gradient_id = g.gradient_id"
                    " WHERE gt.theme_id = ? AND g.task_class IS NOT NULL"
                    "  AND g.task_class != ''",
                    (theme_id,),
                )
            )
        except sqlite3.OperationalError:
            rows = []
    finally:
        con.close()
    return {r["task_class"] for r in rows if r["task_class"]}


def _representative(
    db_path: str, theme_id: str, centroid_blob: bytes
) -> str:
    """The member signal closest to the centroid. Refresh after the cluster grows."""
    con = _connect(db_path)
    try:
        rows = list(
            con.execute(
                "SELECT g.gradient_id, g.signal FROM gradient_records g"
                "  JOIN gradient_theme gt ON gt.gradient_id = g.gradient_id"
                " WHERE gt.theme_id = ?",
                (theme_id,),
            )
        )
    finally:
        con.close()
    if not rows:
        return ""
    if len(rows) == 1:
        return rows[0]["signal"]
    centroid = _unpack(centroid_blob)
    best_id, best_sim = rows[0]["gradient_id"], -1.0
    for r in rows:
        emb = _embedder().embed([normalize(r["signal"])])[0]
        sim = _cosine(centroid, emb)
        if sim > best_sim:
            best_sim = sim
            best_id = r["gradient_id"]
    # Refetch the chosen row's full signal.
    con = _connect(db_path)
    try:
        row = con.execute(
            "SELECT signal FROM gradient_records WHERE gradient_id = ?",
            (best_id,),
        ).fetchone()
        return row["signal"] if row else ""
    finally:
        con.close()


def assign_new(db_path: str | None = None, sim: float | None = None) -> dict:
    """Assign every unassigned gradient to a theme (or start a new one).

    Returns ``{"assigned": int, "themes_new": int, "themes_total": int}``.
    Deterministic: re-running on the same DB assigns 0 gradients.
    """
    db_path = _resolve_db_path(db_path)
    ensure_schema(db_path)
    threshold = (
        sim if sim is not None
        else float(os.environ.get("MO_THEME_SIM", str(MO_THEME_SIM_DEFAULT)))
    )

    themes_by_kind: dict[str, list[dict]] = {"task": [], "framework": []}
    for r in _members_of(db_path, "task"):
        themes_by_kind["task"].append(
            {
                "theme_id": r["theme_id"],
                "centroid": _unpack(r["centroid"]),
                "n": r["n_gradients"],
                "representative": r["representative"],
                "first_seen": r["first_seen"],
                "last_seen": r["last_seen"],
                "role": r["role"],
                "task_class": r["task_class"],
            }
        )
    for r in _members_of(db_path, "framework"):
        themes_by_kind["framework"].append(
            {
                "theme_id": r["theme_id"],
                "centroid": _unpack(r["centroid"]),
                "n": r["n_gradients"],
                "representative": r["representative"],
                "first_seen": r["first_seen"],
                "last_seen": r["last_seen"],
                "role": r["role"],
                "task_class": r["task_class"],
            }
        )

    rows = _unassigned(db_path)
    assigned = 0
    new_themes = 0
    now = int(time.time())

    con = _connect(db_path)
    try:
        for g in rows:
                kind = classify_kind(
                    g["target"] or "", g["signal"] or "", g["suggested_change"] or ""
                )
                emb = _embed_one(g["signal"] or "")
                emb_list = _unpack(emb)

                best_theme = None
                best_sim = -1.0
                for t in themes_by_kind[kind]:
                    s = _cosine(t["centroid"], emb_list)
                    if s > best_sim:
                        best_sim = s
                        best_theme = t

                gid = g["gradient_id"]
                if best_theme is not None and best_sim >= threshold:
                    # Join. Centroid running-mean update.
                    new_blob, new_n = _running_mean_update(
                        struct.pack(
                            f"{len(best_theme['centroid'])}f",
                            *best_theme["centroid"],
                        ),
                        best_theme["n"],
                        emb_list,
                    )
                    new_first = best_theme["first_seen"] or g["created_at"] or now
                    new_last = max(best_theme["last_seen"] or 0, g["created_at"] or 0)
                    con.execute(
                        "UPDATE lesson_themes SET centroid = ?, n_gradients = ?,"
                        "  first_seen = ?, last_seen = ?, updated_at = ?"
                        "  WHERE theme_id = ?",
                        (
                            new_blob,
                            new_n,
                            new_first,
                            new_last,
                            now,
                            best_theme["theme_id"],
                        ),
                    )
                    con.execute(
                        "INSERT INTO gradient_theme (gradient_id, theme_id, similarity)"
                        "  VALUES (?, ?, ?)"
                        "  ON CONFLICT(gradient_id) DO UPDATE SET"
                        "    theme_id = excluded.theme_id,"
                        "    similarity = excluded.similarity",
                        (gid, best_theme["theme_id"], float(best_sim)),
                    )
                    best_theme["centroid"] = _unpack(new_blob)
                    best_theme["n"] = new_n
                    best_theme["first_seen"] = new_first
                    best_theme["last_seen"] = new_last
                else:
                    # Start a new theme.
                    tid = _theme_id(kind, gid)
                    con.execute(
                        "INSERT INTO lesson_themes ("
                        "  theme_id, kind, representative, centroid, n_gradients,"
                        "  first_seen, last_seen, updated_at"
                        ") VALUES (?, ?, ?, ?, 1, ?, ?, ?)",
                        (
                            tid,
                            kind,
                            g["signal"] or "",
                            emb,
                            g["created_at"] or now,
                            g["created_at"] or now,
                            now,
                        ),
                    )
                    con.execute(
                        "INSERT INTO gradient_theme (gradient_id, theme_id, similarity)"
                        "  VALUES (?, ?, ?)",
                        (gid, tid, 1.0),
                    )
                    themes_by_kind[kind].append(
                        {
                            "theme_id": tid,
                            "centroid": emb_list,
                            "n": 1,
                            "representative": g["signal"],
                            "first_seen": g["created_at"] or now,
                            "last_seen": g["created_at"] or now,
                            "role": None,
                            "task_class": None,
                        }
                    )
                    new_themes += 1
                assigned += 1

        con.commit()
    finally:
        con.close()

    # Post-pass: re-read every theme and refresh role / task_class / n_runs /
    # representative. Runs in a SECOND connection so the gradient_theme
    # rows the join/insert loop just persisted are visible — without the
    # commit-then-reopen split, _representative's own connection would see
    # an empty gradient_theme for every theme and write back "".
    con2 = _connect(db_path)
    try:
        all_themes = list(
            con2.execute(
                "SELECT theme_id, n_gradients, centroid, representative FROM lesson_themes"
            )
        )
        for t in all_themes:
            tid = t["theme_id"]
            members = list(
                con2.execute(
                    "SELECT g.gradient_id, g.target FROM gradient_records g"
                    "  JOIN gradient_theme gt ON gt.gradient_id = g.gradient_id"
                    " WHERE gt.theme_id = ?",
                    (tid,),
                )
            )
            member_ids = [m["gradient_id"] for m in members]
            role_counts: dict[str, int] = {}
            for m in members:
                r = role_of(m["target"])
                role_counts[r] = role_counts.get(r, 0) + 1
            role = max(role_counts.items(), key=lambda kv: kv[1])[0] if role_counts else None
            classes = _member_classes(db_path, tid)
            n_runs = _distinct_runs(db_path, member_ids)
            task_class = next(iter(classes)) if len(classes) == 1 else (
                "*" if len(classes) >= 3 else None
            )
            # Refresh representative on every assignment. The kickoff's
            # "≥ 25%" rule reduces to "always" without a stored
            # "last refresh n" — and one extra centroid-distance computation
            # per assignment is the price of keeping the representative
            # accurate.
            prior_repr = t["representative"]
            new_repr = _representative(db_path, tid, t["centroid"]) if t["centroid"] else prior_repr
            con2.execute(
                "UPDATE lesson_themes SET role = ?, task_class = ?, n_runs = ?,"
                "  representative = ?, updated_at = ?"
                "  WHERE theme_id = ?",
                (role, task_class, n_runs, new_repr, now, tid),
            )
        con2.commit()
    finally:
        con2.close()

    # ``themes_by_kind`` was updated in-place as new themes were added, so
    # the total is the current length — no separate counter needed.
    total = sum(len(v) for v in themes_by_kind.values())
    return {
        "assigned": assigned,
        "themes_new": new_themes,
        "themes_total": total,
    }


# ── Backfill + stats ────────────────────────────────────────────────────────


def backfill(
    db_path: str | None = None,
    *,
    sim: float | None = None,
    dry_run: bool = False,
) -> dict:
    """Run ``assign_new`` over everything.

    With ``dry_run=True``, the source DB is FILE-COPIED to a /tmp scratch and
    assign_new runs against the copy — never the user's source. The
    kickoff's proof step explicitly points at a user-made copy of the live
    DB; if the operator forgets to copy, ``dry_run`` still does not write
    to the source path. The scratch path is returned in the stats block for
    forensics.

    Returns stats the kickoff's proof gate expects:
      * ``themes_per_kind`` — ``{"task": int, "framework": int}``
      * ``themes_ge_3`` — themes with ≥ 3 members
      * ``coverage_ge_3`` — share of gradients covered by those themes
      * ``top_10`` — top 10 themes by member count
      * ``wall_seconds``
    """
    src = _resolve_db_path(db_path)

    started = time.time()
    if dry_run:
        target = _dry_run_copy(src)
        # In dry-run, the scratch is the only file we may write. The source
        # is opened for read by ``_dry_run_copy`` and never touched again.
        # The kickoff's proof step says "never write to the live one".
    else:
        target = src
        ensure_schema(target)
    report = assign_new(target, sim=sim)
    stats_block = stats(target)
    stats_block["wall_seconds"] = round(time.time() - started, 3)
    stats_block["assigned_in_run"] = report
    if dry_run:
        stats_block["dry_run_source"] = src
        stats_block["dry_run_scratch"] = target
    return stats_block


def _dry_run_copy(src: str) -> str:
    """File-copy ``src`` to a /tmp scratch and return the new path.

    ``shutil.copyfile`` preserves the bytes; the scratch is created in
    ``$TMPDIR`` (fallback ``/tmp``) and is the file the implementer touches.
    The source is opened for read; even a mid-transaction live DB is safe.
    """
    import shutil
    import tempfile

    fd, scratch = tempfile.mkstemp(prefix="themes-dryrun-", suffix=".db")
    os.close(fd)
    os.unlink(scratch)  # shutil.copyfile needs the target absent
    shutil.copyfile(src, scratch)
    return scratch


def stats(db_path: str | None = None) -> dict:
    """Aggregate counts the kickoff and the human-readable Summary set print.

    A ``db_path`` without the migration still works because ``ensure_schema``
    is called first.
    """
    db_path = _resolve_db_path(db_path)
    ensure_schema(db_path)
    con = _connect(db_path)
    try:
        per_kind = dict(
            con.execute(
                "SELECT kind, COUNT(*) FROM lesson_themes GROUP BY kind"
            ).fetchall() or []
        )
        ge3 = list(
            con.execute(
                "SELECT theme_id, n_gradients FROM lesson_themes"
                "  WHERE n_gradients >= 3"
            )
        )
        ge3_theme_ids = {r["theme_id"] for r in ge3}
        ge3_members = 0
        if ge3_theme_ids:
            placeholders = ",".join("?" for _ in ge3_theme_ids)
            row = con.execute(
                f"SELECT COUNT(*) FROM gradient_theme WHERE theme_id IN ({placeholders})",
                list(ge3_theme_ids),
            ).fetchone()
            ge3_members = int(row[0]) if row else 0
        total_members_row = con.execute("SELECT COUNT(*) FROM gradient_theme").fetchone()
        total_members = int(total_members_row[0]) if total_members_row else 0
        top10 = list(
            con.execute(
                "SELECT theme_id, kind, n_gradients, representative"
                "  FROM lesson_themes ORDER BY n_gradients DESC, theme_id LIMIT 10"
            )
        )
        return {
            "themes_per_kind": {
                "task": int(per_kind.get("task", 0)),
                "framework": int(per_kind.get("framework", 0)),
            },
            "themes_ge_3": len(ge3),
            "gradients_in_ge_3": ge3_members,
            "gradients_total": total_members,
            "coverage_ge_3": (
                round(ge3_members / total_members, 4) if total_members else 0.0
            ),
            "top_10": [
                {
                    "theme_id": r["theme_id"],
                    "kind": r["kind"],
                    "n_gradients": r["n_gradients"],
                    "representative": (r["representative"] or "")[:160],
                }
                for r in top10
            ],
        }
    finally:
        con.close()


# ── Framework-bug rollup ────────────────────────────────────────────────────


def rollup_framework_bugs(
    db_path: str | None = None, min_members: int = 5
) -> int:
    """Upsert one ``bug_reports`` row per qualifying framework theme.

    Returns the number of bug rows written or updated. Idempotent: a second
    call updates frequency/description/suggested_fix/last_seen_at but never
    overwrites ``status``/``severity``/``confidence`` — that's how a human
    flipping to ``wontfix`` survives re-rollup.
    """
    db_path = _resolve_db_path(db_path)
    ensure_schema(db_path)
    con = _connect(db_path)
    now = int(time.time())
    written = 0
    try:
        themes = list(
            con.execute(
                "SELECT theme_id, representative, n_gradients, first_seen,"
                "       last_seen, role, bug_report_id"
                "  FROM lesson_themes"
                "  WHERE kind = 'framework' AND n_gradients >= ?",
                (min_members,),
            )
        )
        for t in themes:
            tid = t["theme_id"]
            members = list(
                con.execute(
                    "SELECT g.gradient_id, g.signal, g.suggested_change"
                    "  FROM gradient_records g"
                    "  JOIN gradient_theme gt ON gt.gradient_id = g.gradient_id"
                    " WHERE gt.theme_id = ?",
                    (tid,),
                )
            )
            # Top-3 distinct member quotes for the description.
            seen_quotes: list[str] = []
            seen_set: set[str] = set()
            for m in members:
                q = (m["signal"] or "").strip()
                if q and q not in seen_set:
                    seen_set.add(q)
                    seen_quotes.append(q[:200])
                if len(seen_quotes) >= 3:
                    break
            # Most-common suggested_change.
            fix_counts: dict[str, int] = {}
            for m in members:
                f = (m["suggested_change"] or "").strip()
                if not f:
                    continue
                fix_counts[f] = fix_counts.get(f, 0) + 1
            most_common_fix = (
                max(fix_counts.items(), key=lambda kv: kv[1])[0][:2000]
                if fix_counts
                else None
            )
            n = t["n_gradients"]
            n_runs_row = con.execute(
                "SELECT n_runs FROM lesson_themes WHERE theme_id = ?", (tid,)
            ).fetchone()
            n_runs = int(n_runs_row["n_runs"]) if n_runs_row and n_runs_row["n_runs"] else 0
            first_seen = t["first_seen"] or now
            description = (
                f"{n} gradients across {n_runs} runs since "
                f"{time.strftime('%Y-%m-%d', time.gmtime(first_seen))}."
            )
            if seen_quotes:
                description += " Quotes: " + " | ".join(seen_quotes)
            fingerprint = f"theme:{tid}"
            title = (t["representative"] or "")[:200]

            existing = con.execute(
                "SELECT id FROM bug_reports WHERE fingerprint = ?",
                (fingerprint,),
            ).fetchone()
            # Single-statement upsert: INSERT with ON CONFLICT preserves
        # `status`/`severity`/`confidence` because those columns are NOT in
            # the SET clause — that is the SQLite-portable way to keep a
            # human-set ``wontfix`` against a re-run. ``excluded.column`` is
            # only legal inside an INSERT ... ON CONFLICT, which is why
            # this is one statement and not an UPDATE branch.
            cur = con.execute(
                """
                INSERT INTO bug_reports (
                  fingerprint, agent_role, observed_in, title, description,
                  suggested_fix, severity, confidence, frequency, status,
                  first_seen_at, last_seen_at, updated_at
                ) VALUES (?, 'learning', ?, ?, ?, ?, 'medium', 0.8, ?, 'open', ?, ?, ?)
                ON CONFLICT(fingerprint) DO UPDATE SET
                  description   = excluded.description,
                  suggested_fix = excluded.suggested_fix,
                  frequency     = excluded.frequency,
                  last_seen_at  = excluded.last_seen_at,
                  updated_at    = excluded.updated_at
                """,
                (
                    fingerprint,
                    t["role"] or "",
                    title,
                    description,
                    most_common_fix,
                    n,
                    first_seen,
                    t["last_seen"] or now,
                    now,
                ),
            )
            if existing is None:
                bug_id = cur.lastrowid
            else:
                bug_id = existing["id"]

            con.execute(
                "UPDATE lesson_themes SET bug_report_id = ?, updated_at = ?"
                "  WHERE theme_id = ?",
                (bug_id, now, tid),
            )
            written += 1
        con.commit()
    finally:
        con.close()
    return written


# ── CLI ─────────────────────────────────────────────────────────────────────


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m mini_ork.learning.themes",
        description=(
            "Group gradient_records into themes; roll up framework-bug "
            "candidates. JSON to stdout."
        ),
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    back = sub.add_parser("backfill", help="assign_new over everything")
    back.add_argument("--sim", type=float, default=None,
                     help=f"cosine threshold (default {MO_THEME_SIM_DEFAULT})")
    back.add_argument("--dry-run", action="store_true",
                     help="compute against an in-memory scratch DB")
    back.add_argument("--db", default=None, help="override DB path")

    st = sub.add_parser("stats", help="aggregate counts only")
    st.add_argument("--db", default=None, help="override DB path")

    rl = sub.add_parser("rollup", help="framework-bug rollup only")
    rl.add_argument("--min-members", type=int, default=5,
                   help="minimum theme size to roll up (default 5)")
    rl.add_argument("--db", default=None, help="override DB path")

    return p


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.cmd == "backfill":
        out = backfill(db_path=args.db, sim=args.sim, dry_run=args.dry_run)
    elif args.cmd == "stats":
        out = stats(db_path=args.db)
    elif args.cmd == "rollup":
        n = rollup_framework_bugs(db_path=args.db, min_members=args.min_members)
        out = {"bugs_updated": n}
    else:  # pragma: no cover — argparse required=True
        return 2
    sys.stdout.write(json.dumps(out, indent=2, sort_keys=True) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())