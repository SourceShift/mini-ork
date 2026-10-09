"""mini_ork.learning.themes — group gradient_records into themes.

``gradient_records`` holds ~10k rows today. A large share describe mini-ork's
own trace/telemetry fields (``verifier_output``, ``tool_calls``,
``files_read``, ``duration_ms``, ``cost_usd``, ``prompt_version_hash``,
``context_bundle_hash``, ``reward_*``, …). Those are bugs in mini-ork's
tracing, not guidance for the task — yet every one of them can be injected
into a task prompt. Nobody can read 10k rows. The IDE needs one row per idea.

This module is P2 of the learning-memory refactor (D1 + D2). It BUILDS themes
and rolls up framework-bug candidates. It does NOT change what gets injected
into prompts (a later phase adds the filter to ``context_assembler``).

Identity contract
=================

A theme's id is ``"th-" + sha256(kind + first member gradient_id)[:12]``.
``kind`` is ``"task"`` or ``"framework"``. The same gradient re-assigned to a
theme of the same kind on a re-run yields the SAME row — the centroid and
member count grow in place. Identity is therefore stable across runs (the
determinism the kickoff's test asserts).

Clustering
==========

Greedy leader, in ``created_at, gradient_id`` order. A gradient's signal is
tokenized (``normalize()`` + stopword/``<3``-char filtering) into a sparse
TF-IDF vector. Candidates are the existing themes of the SAME kind that share
any of the gradient's 8 highest-IDF tokens (an in-memory inverted index
``token -> {theme_id}`` built once per call from stored centroids), capped at
50 by shared-token count. The gradient joins the best-cosine candidate at or
above ``MO_THEME_SIM``, else starts a new theme. A theme's vector is the top
40 TF-IDF tokens of its members' mean vector, L2-normalized, stored as JSON in
``centroid``.

Why TF-IDF instead of hashed embeddings? A hash embedder counts every token
equally, so stopwords dominate and two paraphrases of the same trace complaint
join only at ~0.3 cosine. TF-IDF down-weights tokens that appear in many
signals (``verifier_output``, ``node_type``, …) and up-weights the
discriminating prose, so two paraphrases of the same observation share their
high-IDF tokens and join at a sane default threshold. ``normalize()`` still
collapses trace ids, hex strings, numbers, paths and quoted JSON values to
placeholders FIRST so the paraphrase pair is not split by an incidental id.

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
import random
import re
import sqlite3
import sys
import time

__all__ = [
    "FRAMEWORK_FIELDS",
    "MO_THEME_SIM_DEFAULT",
    "assign_new",
    "backfill",
    "classify_kind",
    "ensure_schema",
    "normalize",
    "promote",
    "role_of",
    "rollup_framework_bugs",
    "stats",
]

# Default cosine threshold for theme joining. Set from the three-sim dry-run
# proof over the live DB (the kickoff's search starts at 0.35); the unit
# tests run at this default, not at a hand-picked low threshold.
MO_THEME_SIM_DEFAULT = 0.35

# TF-IDF clustering knobs (kickoff fix 3).
_TOP_K = 40              # stored centroid keeps the top-40 TF-IDF tokens
_QUERY_TOKENS = 8        # gradient's highest-IDF tokens drive candidate lookup
_CANDIDATE_CAP = 50      # cosine is computed against at most this many themes
_IDF_GROWTH = 1.25       # rebuild IDF when the gradient count grew >= 25%
_COMMIT_EVERY = 1000     # commit the assignment loop in batches (kickoff fix 5)

# Sentinel row in ``theme_idf`` holding the total gradient count N. ``\x00``
# can never be emitted by ``normalize()`` (its output is printable ASCII), so
# it cannot collide with a genuine token. See migration 0063.
_META_KEY = "\x00N"

# One regex per kind-classifier — keeps the regex compiled once, and keeps the
# trace-field vocabulary in one obvious place for a future reviewer to extend.
# The list is the kickoff's enumerated framework-field vocabulary (fix 1):
# ``target`` and ``gradient_id`` are deliberately ABSENT — "target" is
# ordinary English (it made ~160 live rows classify ``framework`` by
# accident), and ``gradient_id`` never appears in prose. ``reward_\w+``
# replaces the old ``reward(_|s)?`` whose trailing ``\b`` failed before a
# word char (so ``reward_score`` / ``reward_g`` never matched).
FRAMEWORK_FIELDS = re.compile(
    r"\b("
    r"verifier_output|tool_calls|files_read|files_written|duration_ms|"
    r"cost_usd|prompt_version_hash|context_bundle_hash|reward_\w+|"
    r"process_reward|reviewer_verdict|recipe_fallback|trace_id|"
    r"execution_traces|run[_\s]?context|finish_reason|node_type"
    r")\b",
    re.IGNORECASE,
)

# Trace identifiers (``tr-XXXX``), hex strings ≥ 6 chars, bare numbers, file
# paths and quoted JSON values all collapse to placeholders. The point is to
# make two paraphrases of the same observation produce the same token set.
_RE_TRACE_ID = re.compile(r"\btr-[A-Za-z0-9_-]+\b")
_RE_HEX_LONG = re.compile(r"\b[a-f0-9]{6,}\b", re.IGNORECASE)
_RE_NUMBER = re.compile(r"\b\d+\b")
_RE_PATH = re.compile(r"(?:/|\.{1,2}/)[A-Za-z0-9_./-]+\.[A-Za-z0-9]+")
_RE_QUOTED_JSON = re.compile(r'"[^"\n]{1,200}"')
_WHITESPACE = re.compile(r"\s+")

# English stopwords (kickoff fix 3: a ~120-word module constant). Tokens are
# lowercased and split on non-word before this filter, so the contraction
# forms (``isn't``) never match verbatim — the pieces (``isn``, ``t``) do, or
# are dropped for being < 3 chars. Domain-significant words (``node``,
# ``run``, ``file``, ``trace``, ``signal``, ``task``) are deliberately NOT
# here.
_STOPWORDS = frozenset(
    """
    a about above after again against all am an and any are as at be because
    been before being below between both but by can cannot could did do does
    doing down during each few for from further had has have having he her
    here hers herself him himself his how i if in into is it its itself just
    me more most my myself no nor not of off on once only or other ought our
    ours ourselves out over own same she should so some such than that the
    their theirs them themselves then there these they this those through to
    too under until up very was we were what when where which while who whom
    why will with would you your yours yourself yourselves
    """.split()
)


def normalize(text: str) -> str:
    """Collapse identifiers/hex/numbers/paths/quoted JSON to placeholders.

    Lowercase, then replace:
      * ``tr-XXXX…`` trace ids → ``<trace>``
      * 6+ char hex runs → ``X``
      * bare numbers → ``N``
      * file paths → ``<path>``
      * quoted JSON-ish strings → ``Q``

    Then collapse whitespace. The output is the text the tokenizer sees, so two
    paraphrases of the same observation share the same discriminating tokens.

    Examples (kickoff-mandated cases)::

        "verifier_output for this node is only {node_type: researcher}"
            -> "verifier_output for this node is only {node_type: researcher}"

        "this node's verifier_output records only {node_type: planner}"
            -> "this node's verifier_output records only {node_type: planner}"

    These normalize to DIFFERENT strings — paraphrases are the same idea, not
    the same sentence. They share the discriminating tokens (``verifier_output``,
    ``node_type``) and differ only in prose, which is exactly what lets TF-IDF
    give them a high cosine without pretending they are identical.
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
    generic). Pure function; unit-tested with 6 verbatim live-DB signals plus
    two regression probes.
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
    """Idempotent DDL for the theme tables. Called from every entry point.

    A database that predates migration ``0063_lesson_themes.sql`` (i.e. one
    that has not run ``mini_ork/stores/migrate``) still works after this call.
    ``CREATE TABLE IF NOT EXISTS`` and the indexes are all the migration does
    — kept verbatim so the live migration and this in-process path agree.
    """
    con = _connect(db_path)
    try:
        _ensure_schema_con(con)
        con.commit()
    finally:
        con.close()


def _ensure_schema_con(con: sqlite3.Connection) -> None:
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
          n_at_refresh  INTEGER NOT NULL DEFAULT 0,
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
        CREATE TABLE IF NOT EXISTS theme_idf (
          token TEXT PRIMARY KEY,
          df    INTEGER NOT NULL
        );
        """
    )


# ── TF-IDF primitives ───────────────────────────────────────────────────────


def _tokenize(text: str) -> list[str]:
    """``normalize`` + split on non-word, lowercase, drop stopwords and <3.

    ``normalize`` already lowercases and collapses identifiers; the split then
    yields ``[a-z0-9_]+`` tokens (the ``{node_type: …}`` / ``<trace>``
    placeholders break on the non-word brackets, which is what we want).
    """
    norm = normalize(text)
    if not norm:
        return []
    return [
        t for t in re.split(r"[^\w]+", norm)
        if len(t) >= 3 and t not in _STOPWORDS
    ]


def _l2norm(vec: dict[str, float]) -> dict[str, float]:
    norm = math.sqrt(sum(v * v for v in vec.values()))
    if norm <= 0.0:
        return {}
    return {t: v / norm for t, v in vec.items()}


def _top_k(vec: dict[str, float], k: int) -> dict[str, float]:
    """Top-``k`` entries by weight (tie-break by token), L2-normalized."""
    if len(vec) > k:
        vec = dict(sorted(vec.items(), key=lambda kv: (-kv[1], kv[0]))[:k])
    return _l2norm(vec)


def _df_to_idf(df: dict[str, int], n: int) -> dict[str, float]:
    """Smoothed IDF, matching ``mini_ork.similarity``: log(1 + n / (1 + df))."""
    if n <= 0:
        return {}
    return {t: math.log(1.0 + n / (1.0 + c)) for t, c in df.items()}


def _idf_unseen(n_total: int) -> float:
    """IDF for a token with df=0 — the smoothed value ``log(1 + n)``.

    A token absent from the persisted ``theme_idf`` is NEW vocabulary, not
    meaningless: it should be up-weighted, never zeroed. ``_df_to_idf`` at
    ``df=0`` gives exactly this value, which is higher than any seen token's
    (``df>=1`` gives ``log(1 + n/(1+df)) < log(1 + n)``).
    """
    return math.log(1.0 + n_total) if n_total > 0 else 0.0


def _vector(
    signal: str, idf: dict[str, float], unseen_idf: float
) -> dict[str, float]:
    """Sparse TF-IDF vector (L2-normalized) over the signal's tokens.

    A token missing from ``idf`` (vocabulary that arrived after the IDF was
    persisted) gets ``unseen_idf`` — the df=0 smoothed weight — so fresh
    vocabulary is up-weighted instead of silently zeroed.
    """
    toks = _tokenize(signal)
    if not toks:
        return {}
    tf: dict[str, float] = {}
    for t in toks:
        tf[t] = tf.get(t, 0) + 1
    total = len(toks)
    vec = {t: (c / total) * idf.get(t, unseen_idf) for t, c in tf.items()}
    return _l2norm(vec)


def _query_tokens(
    signal: str, idf: dict[str, float], k: int, unseen_idf: float
) -> list[str]:
    """The signal's ``k`` highest-IDF tokens, for inverted-index lookup.

    Unseen tokens score ``unseen_idf`` (the df=0 value), so fresh vocabulary
    ranks FIRST for candidate lookup instead of being skipped.
    """
    toks = set(_tokenize(signal))
    return sorted(toks, key=lambda t: (-idf.get(t, unseen_idf), t))[:k]


def _dot(a: dict[str, float], b: dict[str, float]) -> float:
    """Cosine of two L2-normalized sparse vectors (dot == cosine)."""
    if len(a) > len(b):
        a, b = b, a
    return sum(w * b.get(t, 0.0) for t, w in a.items())


def _mean_add(
    centroid: dict[str, float], n: int, vec: dict[str, float]
) -> dict[str, float]:
    """Running-mean a sparse centroid with a new vector, then top-k + L2-norm.

    ``n`` is the member count BEFORE adding ``vec``. The stored centroid is
    already top-k-truncated, so this is an approximation of the true mean;
    ``_recompute_centroid`` re-derives it exactly when a theme grows ≥ 25%.
    """
    if n <= 0:
        return _top_k(vec, _TOP_K)
    new_n = n + 1
    merged: dict[str, float] = {}
    for t, w in centroid.items():
        merged[t] = w * n
    for t, w in vec.items():
        merged[t] = merged.get(t, 0.0) + w
    for t in merged:
        merged[t] /= new_n
    return _top_k(merged, _TOP_K)


# ── IDF persistence ─────────────────────────────────────────────────────────


def _compute_df(con: sqlite3.Connection) -> tuple[dict[str, int], int]:
    """Document frequency of every token across all gradient signals."""
    try:
        rows = con.execute("SELECT signal FROM gradient_records").fetchall()
    except sqlite3.OperationalError:
        return {}, 0
    df: dict[str, int] = {}
    n = 0
    for r in rows:
        n += 1
        for tok in set(_tokenize(r["signal"] or "")):
            df[tok] = df.get(tok, 0) + 1
    return df, n


def _load_df(con: sqlite3.Connection) -> tuple[dict[str, int], int]:
    """Load persisted (df, N) from ``theme_idf`` (``_META_KEY`` row holds N)."""
    try:
        rows = con.execute("SELECT token, df FROM theme_idf").fetchall()
    except sqlite3.OperationalError:
        return {}, 0
    df: dict[str, int] = {}
    n = 0
    for r in rows:
        if r["token"] == _META_KEY:
            n = int(r["df"] or 0)
        else:
            df[r["token"]] = int(r["df"] or 0)
    return df, n


def _persist_df(con: sqlite3.Connection, df: dict[str, int], n: int) -> None:
    con.execute("DELETE FROM theme_idf")
    con.executemany(
        "INSERT INTO theme_idf(token, df) VALUES (?, ?)", df.items()
    )
    con.execute(
        "INSERT OR REPLACE INTO theme_idf(token, df) VALUES (?, ?)",
        (_META_KEY, n),
    )


# ── Centroid JSON (de)serialization ─────────────────────────────────────────


def _centroid_to_blob(vec: dict[str, float]) -> bytes:
    return json.dumps(vec, sort_keys=True).encode("utf-8")


def _centroid_from_blob(blob: bytes | str | None) -> dict[str, float]:
    if blob is None:
        return {}
    if isinstance(blob, bytes):
        blob = blob.decode("utf-8")
    try:
        return json.loads(blob)
    except (ValueError, TypeError):
        return {}


# ── Theme id derivation ─────────────────────────────────────────────────────


def _theme_id(kind: str, first_gradient_id: str) -> str:
    digest = hashlib.sha256(
        f"{kind}|{first_gradient_id}".encode("utf-8")
    ).hexdigest()[:12]
    return f"th-{digest}"


# ── Connection-scoped readers ───────────────────────────────────────────────


def _members_of(con: sqlite3.Connection, kind: str) -> list[sqlite3.Row]:
    """Existing themes of one kind, ordered by id for stable iteration."""
    return list(
        con.execute(
            "SELECT theme_id, centroid, n_gradients, n_at_refresh, first_seen,"
            "       last_seen, role, task_class, representative"
            "  FROM lesson_themes WHERE kind = ? ORDER BY theme_id",
            (kind,),
        )
    )


def _unassigned(con: sqlite3.Connection) -> list[sqlite3.Row]:
    """Gradients with no gradient_theme row, in deterministic order."""
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


def _distinct_runs_con(
    con: sqlite3.Connection, gradient_ids: list[str]
) -> int:
    """Distinct ``execution_traces.run_id`` over the member gradients.

    ``gradient_records.evidence`` holds ONE trace_id (text), not a run id, so
    the count is the JOIN — see gradient_extractor.py:198-201.
    """
    if not gradient_ids:
        return 0
    placeholders = ",".join("?" for _ in gradient_ids)
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


def _member_classes_con(
    con: sqlite3.Connection, theme_id: str
) -> set[str]:
    """Distinct task_classes on the theme."""
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
    return {r["task_class"] for r in rows if r["task_class"]}


def _recompute_centroid(
    con: sqlite3.Connection,
    theme_id: str,
    idf: dict[str, float],
    unseen_idf: float,
) -> dict[str, float]:
    """Exact centroid: mean of all member vectors, then top-k + L2-norm."""
    rows = list(
        con.execute(
            "SELECT g.signal FROM gradient_records g"
            "  JOIN gradient_theme gt ON gt.gradient_id = g.gradient_id"
            " WHERE gt.theme_id = ?",
            (theme_id,),
        )
    )
    if not rows:
        return {}
    total: dict[str, float] = {}
    for r in rows:
        for t, w in _vector(r["signal"] or "", idf, unseen_idf).items():
            total[t] = total.get(t, 0.0) + w
    n = len(rows)
    mean = {t: w / n for t, w in total.items()}
    return _top_k(mean, _TOP_K)


def _representative_con(
    con: sqlite3.Connection,
    theme_id: str,
    centroid: dict[str, float],
    idf: dict[str, float],
    unseen_idf: float,
) -> str:
    """The member signal closest to the centroid. Refresh after the cluster grows."""
    rows = list(
        con.execute(
            "SELECT g.gradient_id, g.signal FROM gradient_records g"
            "  JOIN gradient_theme gt ON gt.gradient_id = g.gradient_id"
            " WHERE gt.theme_id = ?",
            (theme_id,),
        )
    )
    if not rows:
        return ""
    if len(rows) == 1:
        return rows[0]["signal"]
    best_id, best_sim = rows[0]["gradient_id"], -1.0
    for r in rows:
        v = _vector(r["signal"] or "", idf, unseen_idf)
        s = _dot(centroid, v)
        if s > best_sim:
            best_sim = s
            best_id = r["gradient_id"]
    row = con.execute(
        "SELECT signal FROM gradient_records WHERE gradient_id = ?",
        (best_id,),
    ).fetchone()
    return row["signal"] if row else ""


# ── Core assignment ─────────────────────────────────────────────────────────


def assign_new(db_path: str | None = None, sim: float | None = None) -> dict:
    """Assign every unassigned gradient to a theme (or start a new one).

    Returns ``{"assigned": int, "themes_new": int, "themes_total": int}``.
    Deterministic: re-running on the same DB assigns 0 gradients.
    """
    db_path = _resolve_db_path(db_path)
    con = _connect(db_path)
    try:
        return _assign_new(con, sim)
    finally:
        con.close()


def _assign_new(con: sqlite3.Connection, sim: float | None = None) -> dict:
    _ensure_schema_con(con)
    con.commit()
    threshold = (
        sim if sim is not None
        else float(os.environ.get("MO_THEME_SIM", str(MO_THEME_SIM_DEFAULT)))
    )

    # Empty-call fast path (kickoff fix 4): the reflect hook calls this after
    # every run, so with nothing to assign it must return in < 0.5 s. Fetch the
    # unassigned set FIRST and bail before loading themes, decoding centroids or
    # building the inverted index — that is O(themes) work that only matters
    # when there is something to assign.
    unassigned = _unassigned(con)
    if not unassigned:
        try:
            total = int(
                con.execute("SELECT COUNT(*) FROM lesson_themes").fetchone()[0]
            )
        except sqlite3.OperationalError:
            total = 0
        return {"assigned": 0, "themes_new": 0, "themes_total": total}

    # IDF: reuse the persisted df, rebuilding only on first run or when the
    # gradient count has grown by >= 25% since it was computed.
    df, n_total = _load_df(con)
    try:
        n_cur = int(
            con.execute("SELECT COUNT(*) FROM gradient_records").fetchone()[0]
        )
    except sqlite3.OperationalError:
        n_cur = 0
    if not df or n_cur >= n_total * _IDF_GROWTH:
        df, n_cur = _compute_df(con)
        _persist_df(con, df, n_cur)
        con.commit()
        n_total = n_cur
    idf = _df_to_idf(df, n_total)
    # Weight for vocabulary absent from the persisted IDF (the df=0 smoothed
    # value) — new tokens are up-weighted, never silently zeroed.
    unseen_idf = _idf_unseen(n_total)

    # Load existing themes and build the inverted index once per call.
    themes_by_kind: dict[str, list[dict]] = {"task": [], "framework": []}
    index_by_kind: dict[str, dict[str, set[str]]] = {
        "task": {},
        "framework": {},
    }
    theme_lookup: dict[str, dict] = {}
    for kind in ("task", "framework"):
        for r in _members_of(con, kind):
            centroid = _centroid_from_blob(r["centroid"])
            t = {
                "theme_id": r["theme_id"],
                "kind": kind,
                "centroid": centroid,
                "n": r["n_gradients"] or 0,
                "n_at_refresh": r["n_at_refresh"] or 0,
                "representative": r["representative"],
                "first_seen": r["first_seen"],
                "last_seen": r["last_seen"],
                "role": r["role"],
                "task_class": r["task_class"],
            }
            themes_by_kind[kind].append(t)
            theme_lookup[r["theme_id"]] = t
            for token in centroid:
                index_by_kind[kind].setdefault(token, set()).add(r["theme_id"])

    touched: set[str] = set()
    assigned = 0
    new_themes = 0
    now = int(time.time())

    for g in unassigned:
        kind = classify_kind(
            g["target"] or "", g["signal"] or "", g["suggested_change"] or ""
        )
        vec = _vector(g["signal"] or "", idf, unseen_idf)
        gid = g["gradient_id"]

        q_tokens = _query_tokens(g["signal"] or "", idf, _QUERY_TOKENS, unseen_idf)
        cand_ids = _candidates(index_by_kind[kind], q_tokens, _CANDIDATE_CAP)

        best_theme: dict | None = None
        best_sim = -1.0
        for tid in cand_ids:
            t = theme_lookup.get(tid)
            if t is None:
                continue
            s = _dot(vec, t["centroid"])
            if s > best_sim:
                best_sim = s
                best_theme = t

        if best_theme is not None and best_sim >= threshold:
            # Join: incremental centroid update; the exact re-derive happens
            # in the post-pass only when membership grew >= 25%.
            new_n = best_theme["n"] + 1
            new_centroid = _mean_add(best_theme["centroid"], best_theme["n"], vec)
            new_first = best_theme["first_seen"] or g["created_at"] or now
            new_last = max(best_theme["last_seen"] or 0, g["created_at"] or 0)
            con.execute(
                "UPDATE lesson_themes SET centroid = ?, n_gradients = ?,"
                "  first_seen = ?, last_seen = ?, updated_at = ?"
                "  WHERE theme_id = ?",
                (
                    _centroid_to_blob(new_centroid),
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
            best_theme["n"] = new_n
            best_theme["centroid"] = new_centroid
            best_theme["first_seen"] = new_first
            best_theme["last_seen"] = new_last
            _index_update(index_by_kind[kind], best_theme["theme_id"], new_centroid)
            touched.add(best_theme["theme_id"])
        else:
            # Start a new theme. Its single member is already the centroid.
            tid = _theme_id(kind, gid)
            centroid = _top_k(vec, _TOP_K)
            con.execute(
                "INSERT INTO lesson_themes ("
                "  theme_id, kind, representative, centroid, n_gradients,"
                "  n_at_refresh, first_seen, last_seen, updated_at"
                ") VALUES (?, ?, ?, ?, 1, 1, ?, ?, ?)",
                (
                    tid,
                    kind,
                    g["signal"] or "",
                    _centroid_to_blob(centroid),
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
            t = {
                "theme_id": tid,
                "kind": kind,
                "centroid": centroid,
                "n": 1,
                "n_at_refresh": 1,
                "representative": g["signal"] or "",
                "first_seen": g["created_at"] or now,
                "last_seen": g["created_at"] or now,
                "role": None,
                "task_class": None,
            }
            themes_by_kind[kind].append(t)
            theme_lookup[tid] = t
            _index_update(index_by_kind[kind], tid, centroid)
            touched.add(tid)
            new_themes += 1
        assigned += 1
        # Batch-commit (kickoff fix 5): release the write lock every 1,000
        # assignments so other mini-ork writers to state.db do not hit
        # "database is locked" during a long backfill.
        if assigned % _COMMIT_EVERY == 0:
            con.commit()

    con.commit()

    # Post-pass: refresh role / task_class / n_runs for every theme touched in
    # this call, and re-derive representative + centroid only for those whose
    # membership grew by >= 25% since the last refresh (the speed fix — the
    # empty-call path touches nothing and therefore does no member scans).
    for tid in touched:
        _refresh_theme(con, theme_lookup[tid], idf, now, unseen_idf)
    con.commit()

    total = sum(len(v) for v in themes_by_kind.values())
    return {
        "assigned": assigned,
        "themes_new": new_themes,
        "themes_total": total,
    }


def _candidates(
    index: dict[str, set[str]], q_tokens: list[str], cap: int
) -> list[str]:
    """Theme ids sharing any query token, ranked by shared-token count."""
    counts: dict[str, int] = {}
    for tok in q_tokens:
        for tid in index.get(tok, ()):
            counts[tid] = counts.get(tid, 0) + 1
    return sorted(counts, key=lambda tid: (-counts[tid], tid))[:cap]


def _index_update(
    index: dict[str, set[str]], tid: str, centroid: dict[str, float]
) -> None:
    for token in centroid:
        index.setdefault(token, set()).add(tid)


def _refresh_theme(
    con: sqlite3.Connection,
    t: dict,
    idf: dict[str, float],
    now: int,
    unseen_idf: float,
) -> None:
    tid = t["theme_id"]
    members = list(
        con.execute(
            "SELECT g.gradient_id, g.target, g.signal FROM gradient_records g"
            "  JOIN gradient_theme gt ON gt.gradient_id = g.gradient_id"
            " WHERE gt.theme_id = ?",
            (tid,),
        )
    )
    member_ids = [m["gradient_id"] for m in members]
    role_counts: dict[str, int] = {}
    for m in members:
        r = role_of(m["target"] or "")
        role_counts[r] = role_counts.get(r, 0) + 1
    role = (
        max(role_counts.items(), key=lambda kv: kv[1])[0]
        if role_counts else None
    )
    classes = _member_classes_con(con, tid)
    n_runs = _distinct_runs_con(con, member_ids)
    task_class = (
        next(iter(classes)) if len(classes) == 1
        else ("*" if len(classes) >= 3 else None)
    )

    n = t["n"]
    at_refresh = t["n_at_refresh"] or 0
    if n >= at_refresh * _IDF_GROWTH and n > 0:
        centroid = _recompute_centroid(con, tid, idf, unseen_idf)
        representative = _representative_con(con, tid, centroid, idf, unseen_idf)
        at_refresh = n
    else:
        centroid = t["centroid"]
        representative = t["representative"]

    con.execute(
        "UPDATE lesson_themes SET role = ?, task_class = ?, n_runs = ?,"
        "  representative = ?, centroid = ?, n_at_refresh = ?, updated_at = ?"
        "  WHERE theme_id = ?",
        (
            role,
            task_class,
            n_runs,
            representative,
            _centroid_to_blob(centroid),
            at_refresh,
            now,
            tid,
        ),
    )
    t["role"] = role
    t["task_class"] = task_class
    t["n_runs"] = n_runs
    t["representative"] = representative
    t["centroid"] = centroid
    t["n_at_refresh"] = at_refresh


# ── Promotion (candidate → verified lesson) ─────────────────────────────────


def promote(
    db_path: str | None = None,
    *,
    min_runs: int | None = None,
    limit: int = 20,
    dispatch_fn=None,
    model: str | None = None,
) -> dict:
    """Author + verify ``lesson_text`` for candidate themes.

    A ``lesson_themes`` row is a cluster statistic until an authored lesson is
    written. The v1/v2 lesson blocks (``context_assembler``'s
    ``_verified_theme_lessons`` and ``context_v2.verified_themes``) read only
    ``status='verified'`` rows with a non-blank ``lesson_text`` — so a table of
    candidates with no author is a consumer with no producer, and both blocks
    render nothing. This is that producer.

    A theme's member traces come from ``gradient_theme`` ⋈ ``gradient_records``
    (``evidence`` is the trace a gradient was extracted from). The lesson is
    authored by the SAME grounded induction path ``pattern_records`` uses
    (``induce_cluster`` → propose → guardrails → merge), so ``'verified'`` here
    means exactly what it means for a pattern lesson: authored from the evidence
    and surviving the guardrails, never invented. Gated by ``MO_PATTERN_INDUCE``
    (the same switch as pattern induction). Only rows whose ``lesson_text`` is
    blank are touched, so a re-run is cheap and an authored lesson is never
    overwritten.

    ``min_runs`` (default ``$MO_THEME_PROMOTE_MIN_RUNS`` or 3) is the support
    bar: a theme seen on fewer runs carries too little evidence to generalise.
    """
    from mini_ork.learning import pattern_induction as pi

    report: dict = {"promoted": 0, "skipped": [], "enabled": pi._induct_enabled()}
    if not report["enabled"]:
        return report

    db_path = _resolve_db_path(db_path)
    if not os.path.isfile(db_path):
        return report
    con = _connect(db_path)
    try:
        _ensure_schema_con(con)
        con.commit()
        if min_runs is None:
            try:
                min_runs = int(os.environ.get("MO_THEME_PROMOTE_MIN_RUNS", "3"))
            except ValueError:
                min_runs = 3
        try:
            min_members = int(os.environ.get("MO_THEME_PROMOTE_MIN_MEMBERS", "3"))
        except ValueError:
            min_members = 3
        rows = con.execute(
            """
            SELECT theme_id, representative, task_class, n_runs
              FROM lesson_themes
             WHERE kind = 'task' AND status = 'candidate'
               AND COALESCE(lesson_text, '') = ''
               AND n_runs >= ?
             ORDER BY n_runs DESC, last_seen DESC
             LIMIT ?
            """,
            (max(1, int(min_runs)), max(1, int(limit))),
        ).fetchall()
        now = int(time.time())
        for r in rows:
            tid = r["theme_id"]
            members = [
                str(x[0]) for x in con.execute(
                    "SELECT DISTINCT g.evidence FROM gradient_theme gt "
                    "JOIN gradient_records g ON g.gradient_id = gt.gradient_id "
                    "WHERE gt.theme_id = ? AND COALESCE(g.evidence, '') <> ''",
                    (tid,),
                ).fetchall()
            ]
            members = [m for m in (members or []) if m.strip()]
            if len(set(members)) < max(1, min_members):
                report["skipped"].append(
                    {"theme_id": tid, "reason": "too few member traces"})
                continue
            text, detail = pi.induce_cluster(
                con, target=r["representative"] or tid,
                member_trace_ids=members, dispatch_fn=dispatch_fn, model=model,
            )
            if not text:
                reason = detail.get("reason") if isinstance(detail, dict) else None
                report["skipped"].append(
                    {"theme_id": tid, "reason": reason or "no lesson"})
                continue
            con.execute(
                "UPDATE lesson_themes SET lesson_text = ?, status = 'verified',"
                " updated_at = ? WHERE theme_id = ?",
                (text, now, tid),
            )
            con.commit()
            report["promoted"] += 1
        return report
    finally:
        con.close()


# ── Backfill + stats ────────────────────────────────────────────────────────


def backfill(
    db_path: str | None = None,
    *,
    sim: float | None = None,
    dry_run: bool = False,
) -> dict:
    """Run ``assign_new`` over everything.

    With ``dry_run=True`` the source DB is snapshotted into an in-memory DB via
    ``sqlite3.Connection.backup`` (which reads through the WAL) and the run
    happens against that copy — never the user's source, and nothing is left
    in ``$TMPDIR``. The ``":memory:"`` marker is returned in the stats block.

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
        con = _memory_copy(src)
        scratch = ":memory:"
    else:
        con = _connect(src)
        scratch = src
    try:
        report = _assign_new(con, sim)
        stats_block = _stats_con(con)
        stats_block["wall_seconds"] = round(time.time() - started, 3)
        stats_block["assigned_in_run"] = report
        if dry_run:
            stats_block["dry_run_source"] = src
            stats_block["dry_run_scratch"] = scratch
            stats_block["member_pairs"] = _sample_member_pairs(con, limit=5)
        return stats_block
    finally:
        con.close()


def _memory_copy(src: str) -> sqlite3.Connection:
    """Snapshot ``src`` into an in-memory DB via the backup API.

    ``Connection.backup`` reads through the WAL, so the copy is a consistent
    snapshot even if the source is mid-write, and nothing is written to disk.
    """
    mem = sqlite3.connect(":memory:")
    mem.row_factory = sqlite3.Row
    try:
        # Open the source read-only so the snapshot never takes a write lock
        # or checkpoints the live WAL; ``backup`` still reads through the WAL.
        src_con = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
        try:
            src_con.backup(mem)
        finally:
            src_con.close()
    except Exception:
        mem.close()
        raise
    return mem


def _sample_member_pairs(con: sqlite3.Connection, limit: int = 5) -> list:
    """``limit`` member-signal pairs from the largest theme (seeded, so the
    proof block is reproducible)."""
    row = con.execute(
        "SELECT theme_id FROM lesson_themes"
        "  ORDER BY n_gradients DESC, theme_id LIMIT 1"
    ).fetchone()
    if not row:
        return []
    tid = row["theme_id"]
    members = [
        r["signal"] or ""
        for r in con.execute(
            "SELECT g.signal FROM gradient_records g"
            "  JOIN gradient_theme gt ON gt.gradient_id = g.gradient_id"
            " WHERE gt.theme_id = ? ORDER BY g.gradient_id",
            (tid,),
        )
    ]
    if len(members) < 2:
        return []
    rng = random.Random(0)
    return [
        [rng.choice(members)[:160], rng.choice(members)[:160]]
        for _ in range(limit)
    ]


def stats(db_path: str | None = None) -> dict:
    """Aggregate counts the kickoff and the human-readable Summary set print.

    A ``db_path`` without the migration still works because ``ensure_schema``
    is called first.
    """
    db_path = _resolve_db_path(db_path)
    con = _connect(db_path)
    try:
        _ensure_schema_con(con)
        con.commit()
        return _stats_con(con)
    finally:
        con.close()


def _stats_con(con: sqlite3.Connection) -> dict:
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
                "representative": (r["representative"] or "")[:100],
            }
            for r in top10
        ],
    }


# ── Framework-bug rollup ────────────────────────────────────────────────────


def rollup_framework_bugs(
    db_path: str | None = None, min_members: int | None = None
) -> int:
    """Upsert one ``bug_reports`` row per qualifying framework theme.

    Returns the number of bug rows written or updated. Idempotent: a second
    call updates frequency/description/suggested_fix/last_seen_at but never
    overwrites ``status``/``severity``/``confidence`` — that's how a human
    flipping to ``wontfix`` survives re-rollup.

    ``min_members`` defaults to ``MO_THEME_BUG_MIN`` (20). Measured on the live
    DB (2026-10-07): ≥5 would file 262 reports, ≥20 files 50, each a defect
    seen at least 20 times — the flood threshold matters more than recall here.
    """
    if min_members is None:
        try:
            min_members = int(os.environ.get("MO_THEME_BUG_MIN", "20"))
        except ValueError:
            min_members = 20
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
                     help="run against an in-memory backup of the DB; writes nothing to disk")
    back.add_argument("--db", default=None, help="override DB path")

    st = sub.add_parser("stats", help="aggregate counts only")
    st.add_argument("--db", default=None, help="override DB path")

    rl = sub.add_parser("rollup", help="framework-bug rollup only")
    rl.add_argument("--min-members", type=int, default=None,
                   help="minimum theme size to roll up (default $MO_THEME_BUG_MIN or 20)")
    rl.add_argument("--db", default=None, help="override DB path")

    pr = sub.add_parser("promote", help="author + verify lessons for candidate themes")
    pr.add_argument("--min-runs", type=int, default=None,
                   help="support bar: minimum runs a theme must span "
                        "(default $MO_THEME_PROMOTE_MIN_RUNS or 3)")
    pr.add_argument("--limit", type=int, default=20,
                   help="max themes to promote per call (default 20)")
    pr.add_argument("--db", default=None, help="override DB path")

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
    elif args.cmd == "promote":
        out = promote(db_path=args.db, min_runs=args.min_runs, limit=args.limit)
    else:  # pragma: no cover — argparse required=True
        return 2
    sys.stdout.write(json.dumps(out, indent=2, sort_keys=True) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
