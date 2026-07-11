"""Python port of lib/gradient_extractor.sh.

The bash implementation remains the source of truth; this module keeps the same
SQLite writes and error semantics for in-process callers.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import sys
import time
import uuid
from collections.abc import Callable, Iterable
from typing import Any


_GRADIENT_EXTRACTOR_PROMPT_TEMPLATE = """You are a recipe-design improvement analyst.

Given the execution trace below, extract 0 to 5 textual gradients — specific,
actionable improvement signals. The trace may describe an algorithmic
workflow (planner → implementer → verifier) OR a coordination workflow
(4 lenses → synthesizer → publisher). Find what about THIS RECIPE design
would have made the outcome better.

Five target families (pick the most specific that fits each gradient):

  1. "workflow.node.<name>"           — algorithmic improvement to one node
                                        (e.g. planner missed a step, verifier
                                        accepted a bad artifact)
  2. "agent.<role>.prompt"            — prompt-level improvement (e.g. the
                                        lens prompt produced shallow output;
                                        the synthesizer prompt missed an axis)
  3. "workflow.edge.<name>"           — dependency / sequencing refinement
                                        (e.g. depends_on should be
                                        supplies_context_to; this edge needs
                                        a retries policy)
  4. "verifier.<name>"                — verifier-script logic gap
                                        (e.g. the grep-assert missed a
                                        boundary condition)
  5. "workflow.recipe.<recipe_name>"  — RECIPE-LEVEL shape suggestion when
                                        the issue is the dispatch topology
                                        itself, not any single node (e.g.
                                        the 4-lens panel has 2 lenses in the
                                        same family; the synthesizer should
                                        be a different family from any lens;
                                        a missing publisher node lets
                                        artifacts vanish from the audit)

TRACE:
<<<TRACE_JSON>>>

Important: if the trace is from a coordination-shaped recipe (audit,
synthesis, multi-lens debate), prefer "workflow.recipe.<name>" or
"agent.<role>.prompt" gradients — those are where the leverage actually
lives. Do NOT respond with [] just because no algorithmic bug stands out;
coordination shapes ALWAYS have a recipe-design improvement to surface
(family-diversity, verifier contract, synthesis aggregation, etc).

Respond ONLY with a JSON array of gradient objects. Each object must have:
  "target"          : string — one of the 5 target families above
  "signal"          : string — what was observed (1-2 sentences)
  "suggested_change": string — concrete recommendation (1-2 sentences)
  "confidence"      : number — 0.0 to 1.0

If after honest analysis you genuinely find nothing to improve (rare —
audit yourself before defaulting), respond with []. No prose, no markdown
fences, only the JSON array."""

_STOP = set(
    "the a an of to in on for and or is are was were be been it its this"
    " that with not no when even though so as by from at into our we you"
    " their".split()
)


def _db_path(db: str | None) -> str:
    if db:
        return db
    env = os.environ.get("MINI_ORK_DB")
    if not env:
        print("MINI_ORK_DB unset", file=sys.stderr)
        raise SystemExit(1)
    return env


def _connect(db: str | None) -> sqlite3.Connection:
    con = sqlite3.connect(_db_path(db))
    con.execute("PRAGMA busy_timeout=5000")
    return con


def init_schema(db: str | None = None) -> None:
    """Ensure gradient_records exists, mirroring _gradient_ensure_table."""
    con = _connect(db)
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS gradient_records (
            gradient_id   TEXT PRIMARY KEY,
            target        TEXT NOT NULL,
            signal        TEXT NOT NULL,
            suggested_change TEXT NOT NULL,
            evidence      TEXT NOT NULL,
            confidence    REAL NOT NULL DEFAULT 0.0
                              CHECK(confidence BETWEEN 0.0 AND 1.0),
            created_at    INTEGER NOT NULL,
            task_class    TEXT
        )
        """
    )
    cols = [r[1] for r in con.execute("PRAGMA table_info(gradient_records)").fetchall()]
    if "task_class" not in cols:
        con.execute("ALTER TABLE gradient_records ADD COLUMN task_class TEXT")
    con.commit()
    con.close()


def _tokens(s: str) -> set[str]:
    return set(re.findall(r"[a-z0-9_]+", s.lower())) - _STOP


def _fail(msg: str) -> None:
    print(msg, file=sys.stderr)
    raise SystemExit(1)


def store(payload: dict[str, Any] | str, db: str | None = None, dedup_sim: float | None = None) -> str:
    """Store a gradient record, print and return its gradient_id."""
    init_schema(db)
    try:
        p = json.loads(payload) if isinstance(payload, str) else dict(payload)
    except json.JSONDecodeError as e:
        _fail(f"gradient_store: invalid JSON: {e}")

    gid = p.get("gradient_id") or f"gr-{uuid.uuid4().hex[:12]}"
    now = int(time.time())
    required = ("target", "signal", "suggested_change", "evidence")
    for field in required:
        if not p.get(field):
            _fail(f"gradient_store: missing required field '{field}'")

    con = _connect(db)
    task_class = p.get("task_class") or ""
    if not task_class:
        try:
            row = con.execute(
                "SELECT task_class FROM execution_traces WHERE trace_id = ?",
                (p["evidence"],),
            ).fetchone()
            task_class = row[0] if row and row[0] else ""
        except sqlite3.OperationalError:
            task_class = ""

    if dedup_sim is None:
        dedup_sim = float(os.environ.get("MO_GRADIENT_DEDUP_SIM", "0.65"))
    if dedup_sim > 0 and not p.get("gradient_id"):
        new_tok = _tokens(f"{p['signal']} {p['suggested_change']}")
        try:
            rows = con.execute(
                "SELECT gradient_id, target, signal, suggested_change, confidence"
                " FROM gradient_records WHERE task_class = ?",
                (task_class,),
            ).fetchall()
        except sqlite3.OperationalError:
            rows = []
        for gid_e, target_e, sig_e, chg_e, conf_e in rows:
            if target_e == p["target"]:
                continue
            old_tok = _tokens(f"{sig_e} {chg_e}")
            if not new_tok or not old_tok:
                continue
            sim = len(new_tok & old_tok) / min(len(new_tok), len(old_tok))
            if sim >= dedup_sim:
                con.execute(
                    "UPDATE gradient_records SET confidence = ? WHERE gradient_id = ?",
                    (max(float(conf_e or 0), float(p.get("confidence", 0.5))), gid_e),
                )
                con.commit()
                con.close()
                print(
                    f"gradient_store: dedup sim={sim:.2f} target={p['target']}"
                    f" absorbed into {target_e} [{gid_e}]",
                    file=sys.stderr,
                )
                print(gid_e)
                return str(gid_e)

    con.execute(
        """
        INSERT INTO gradient_records (
            gradient_id, target, signal, suggested_change, evidence, confidence, created_at, task_class
        ) VALUES (?,?,?,?,?,?,?,?)
        ON CONFLICT(gradient_id) DO UPDATE SET
            signal=excluded.signal,
            suggested_change=excluded.suggested_change,
            confidence=excluded.confidence,
            task_class=COALESCE(NULLIF(excluded.task_class,''), gradient_records.task_class)
        """,
        (
            gid,
            p["target"],
            p["signal"],
            p["suggested_change"],
            p["evidence"],
            float(p.get("confidence", 0.5)),
            now,
            task_class,
        ),
    )
    con.commit()
    con.close()
    print(gid)
    return str(gid)


def _extract_objects_balanced(text: str) -> list[dict[str, Any]]:
    """Brace-balanced extraction from possibly-truncated JSON array output."""
    decoder = json.JSONDecoder()
    objs = []
    i = text.find("[")
    if i < 0:
        i = text.find("{")
    if i < 0:
        return objs
    if text[i] == "[":
        i += 1
    n = len(text)
    while i < n:
        while i < n and text[i] in " \t\n\r,":
            i += 1
        if i >= n or text[i] == "]":
            break
        try:
            obj, end = decoder.raw_decode(text, i)
        except json.JSONDecodeError:
            break
        if isinstance(obj, dict):
            objs.append(obj)
        i = end
    return objs


def extract(
    trace_id: str,
    db: str | None = None,
    override_fn: Callable[[str, str], Iterable[dict[str, Any]]] | None = None,
) -> list[dict[str, Any]]:
    """Extract gradients for a trace, print one JSON object per line."""
    con = _connect(db)
    con.row_factory = sqlite3.Row
    row = con.execute("SELECT * FROM execution_traces WHERE trace_id=?", (trace_id,)).fetchone()
    con.close()
    if not row:
        _fail(f"gradient_extract: trace_id {trace_id} not found")
    trace_json = json.dumps(dict(row))

    if override_fn is None:
        fn_name = os.environ.get("MINI_ORK_GRADIENT_EXTRACTOR_FN", "")
        candidate = globals().get(fn_name) if fn_name else None
        if callable(candidate):
            override_fn = candidate
        elif fn_name:
            _fail(f"gradient_extract: override fn {fn_name} not defined")

    if override_fn is None:
        return []

    items = list(override_fn(trace_id, trace_json))
    for item in items:
        print(json.dumps(item))
    return items

