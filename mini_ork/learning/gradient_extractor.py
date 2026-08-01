"""Native textual-gradient extraction and persistence.

The deterministic SQLite and parsing contracts are kept in process, and the
default agentic boundary calls :mod:`mini_ork.dispatch.llm_dispatch` directly so
reflection no longer depends on or silently skips a Bash dispatcher.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import re
import sqlite3
import sys
import time
import uuid
from collections.abc import Callable, Iterable
from typing import Any, NoReturn, cast


_GRADIENT_EXTRACTOR_PROMPT_TEMPLATE = """You are a recipe-design improvement analyst.

You are given ONE node's execution trace, plus a RUN CONTEXT summarising the
whole run this node belongs to. Extract 0 to 5 textual gradients — specific,
actionable improvement signals — that are DIRECTLY SUPPORTED by the evidence
below. A gradient with no supporting evidence is a fabrication; do not emit it.

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
                                        itself, not any single node

RUN CONTEXT (facts about the whole run — trust these over inferences):
<<<RUN_CONTEXT>>>

THIS NODE'S TRACE:
<<<TRACE_JSON>>>

Evidence discipline (read before writing anything):
  - Ground every "signal" in a concrete field you can see (a duration, a cost,
    a reviewer_verdict, a verifier_output value, a named file, a status). Quote
    or name it. If you cannot point to the evidence, do not raise the claim.
  - You see ONE node, not the recipe graph. Do NOT claim topology facts ("the
    recipe produced no synthesis", "there is no publisher", "the run is empty")
    unless the RUN CONTEXT above states them. The RUN CONTEXT is authoritative
    about what other nodes did; your single trace is not.
  - Missing tool_calls / files_read / files_written are a TELEMETRY CAPTURE gap
    in this runtime, NOT proof the node did no work. Never infer "did nothing"
    from empty capture fields — check duration_ms / cost_usd / final_artifact_ref
    in the RUN CONTEXT instead.
  - Abstention is correct and expected. If this node did real work but the trace
    carries no concrete defect signal, respond with []. An empty array is a valid,
    honest answer — it is far better than a manufactured gradient.

Respond ONLY with a JSON array of gradient objects. Each object must have:
  "target"          : string — one of the 5 target families above
  "signal"          : string — what was observed, citing the evidence (1-2 sent.)
  "suggested_change": string — concrete recommendation (1-2 sentences)
  "confidence"      : number — 0.0 to 1.0, honest to how strong the evidence is

No prose, no markdown fences, only the JSON array (which may be [])."""

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


def is_framework_agent(task_class: str | None) -> bool:
    """Return whether ``task_class`` is reserved for framework self-work.

    Framework traces use the ``__name__`` namespace and are operational
    telemetry rather than evidence from a user task.  Reflection must exclude
    them before paying for gradient extraction.
    """
    return bool(task_class and task_class.startswith("__"))


def has_watermark(trace_id: str, db: str | None = None) -> bool:
    """Return whether a gradient already names ``trace_id`` as evidence.

    A missing ``gradient_records`` table is the fresh-database case and means
    the trace has not been processed yet.
    """
    con = _connect(db)
    try:
        try:
            row = con.execute(
                "SELECT 1 FROM gradient_records WHERE evidence=? LIMIT 1",
                (trace_id,),
            ).fetchone()
        except sqlite3.OperationalError:
            row = None
    finally:
        con.close()
    return row is not None


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


def _fail(msg: str) -> NoReturn:
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
                "SELECT gradient_id, target, signal, suggested_change"
                " FROM gradient_records WHERE task_class = ?",
                (task_class,),
            ).fetchall()
        except sqlite3.OperationalError:
            rows = []
        for gid_e, target_e, sig_e, chg_e in rows:
            if target_e == p["target"]:
                continue
            old_tok = _tokens(f"{sig_e} {chg_e}")
            if not new_tok or not old_tok:
                continue
            sim = len(new_tok & old_tok) / min(len(new_tok), len(old_tok))
            if sim >= dedup_sim:
                # BUG5: absorb the near-duplicate WITHOUT inflating confidence.
                # These matches cross DIFFERENT targets (same-target is skipped
                # above), so a reworded near-duplicate is not corroboration — it
                # is the same claim restated. Raising confidence to max(old,new)
                # let restated confabulations ratchet over the 0.6 injection bar.
                con.close()
                print(
                    f"gradient_store: dedup sim={sim:.2f} target={p['target']}"
                    f" absorbed into {target_e} [{gid_e}] (confidence unchanged)",
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


def _parse_llm_output(raw: str, trace_id: str) -> list[dict[str, Any]]:
    """Recover gradient objects from complete, fenced, or truncated arrays."""
    cleaned = re.sub(r"^```[a-z]*\n?", "", raw.strip(), flags=re.MULTILINE)
    cleaned = re.sub(r"\n?```$", "", cleaned, flags=re.MULTILINE).strip()
    try:
        parsed = json.loads(cleaned)
        items = parsed if isinstance(parsed, list) else []
    except json.JSONDecodeError:
        match = re.search(r"\[.*?\]", cleaned, re.DOTALL)
        if match:
            try:
                candidate = json.loads(match.group())
                items = candidate if isinstance(candidate, list) else []
            except (json.JSONDecodeError, TypeError):
                items = _extract_objects_balanced(cleaned)
        else:
            items = _extract_objects_balanced(cleaned)

    normalized: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        value = dict(item)
        value.setdefault("evidence", trace_id)
        value.setdefault("confidence", 0.5)
        normalized.append(value)
    return normalized


def _default_dispatch(
    prompt: str,
    *,
    repo_root: str | os.PathLike | None = None,
    dispatch_fn: Callable[..., int] | None = None,
    model: str | None = None,
) -> tuple[int, str]:
    """Call the native telemetry-aware dispatcher and isolate diagnostics."""
    from mini_ork.dispatch import llm_dispatch as native_dispatch

    stdout = io.StringIO()
    stderr = io.StringIO()
    argv = [
        "--model", model or os.environ.get("MINI_ORK_GRADIENT_MODEL", "codex"),
        "--node-type", "gradient-extract",
        "--prompt-text", prompt,
        "--timeout", "120",
        "--max-turns", "5",
    ]
    try:
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            rc = native_dispatch.llm_dispatch(
                argv,
                root=str(repo_root or os.environ.get("MINI_ORK_ROOT") or os.getcwd()),
                dispatch_fn=dispatch_fn,
            )
    except Exception:
        return 1, ""
    return rc, stdout.getvalue()


def _json_field_nonempty(raw: Any) -> bool:
    """True when a JSON-encoded list/dict column actually carries content."""
    if raw is None:
        return False
    if isinstance(raw, (list, dict)):
        return bool(raw)
    s = str(raw).strip()
    return s not in ("", "[]", "{}", "null", "None")


def _row_has_captured_evidence(row: dict[str, Any]) -> bool:
    """Whether the trace row carries any concrete, node-observed signal a
    gradient could legitimately cite.

    Missing ``tool_calls`` / ``files_read`` / ``files_written`` are the known
    telemetry-capture gap in the multi-model runtime (the single largest cause
    of confabulated gradients), so a row whose only non-empty fields are
    metadata (cost/duration) has NO evidence to ground a high-confidence claim.
    A ``reviewer_verdict`` of ``unknown`` is the reviewer-extraction failure
    mode, not a real verdict, and does not count as evidence.
    """
    for col in ("tool_calls", "files_read", "files_written", "verifier_output"):
        if _json_field_nonempty(row.get(col)):
            return True
    rv = str(row.get("reviewer_verdict") or "").strip().lower()
    return bool(rv) and rv != "unknown"


def _is_degenerate_node(row: dict[str, Any]) -> bool:
    """A zero-work control node: no time, no cost, no captured evidence.

    Mining a gradient from such a row can only confabulate — this is exactly the
    ``tr-verify`` / ``tr-plan`` control node that produced the "verdict=partial,
    duration_ms=0, zero tool_calls" fabrications. Skip it entirely.
    """
    try:
        dur = int(row.get("duration_ms") or 0)
    except (TypeError, ValueError):
        dur = 0
    try:
        cost = float(row.get("cost_usd") or 0.0)
    except (TypeError, ValueError):
        cost = 0.0
    if dur > 0 or cost > 0:
        return False
    return not _row_has_captured_evidence(row)


def _run_context(con: sqlite3.Connection, row: dict[str, Any]) -> str:
    """Compact, factual summary of the whole run this node belongs to (BUG2).

    A single node's row cannot witness recipe topology, so the extractor used to
    hallucinate "the run produced no synthesis / is empty" from one blind row.
    Feeding it the real cross-node facts (how many nodes ran, which roles, total
    cost/duration, whether any node produced an artifact) grounds — or refutes —
    those topology claims before they are made.
    """
    run_id = row.get("run_id")
    if not run_id:
        return json.dumps({"note": "no run_id on this trace; single-node view only"})
    prev_factory = con.row_factory
    con.row_factory = sqlite3.Row
    try:
        sib = con.execute(
            "SELECT trace_id, task_class, duration_ms, cost_usd, reviewer_verdict, "
            "final_artifact_ref, status FROM execution_traces WHERE run_id=? "
            "ORDER BY created_at",
            (run_id,),
        ).fetchall()
    finally:
        con.row_factory = prev_factory
    nodes, roles = [], []
    total_cost, total_dur, produced = 0.0, 0, False
    for s in sib:
        tid = s["trace_id"] or ""
        parts = tid.split("-")
        role = parts[1] if len(parts) > 1 else (s["task_class"] or "?")
        roles.append(role)
        dur = int(s["duration_ms"] or 0)
        cost = float(s["cost_usd"] or 0.0)
        total_dur += dur
        total_cost += cost
        produced = produced or bool(s["final_artifact_ref"])
        nodes.append({
            "trace_id": tid,
            "role_hint": role,
            "duration_ms": dur,
            "cost_usd": round(cost, 4),
            "reviewer_verdict": s["reviewer_verdict"],
            "produced_artifact": bool(s["final_artifact_ref"]),
            "status": s["status"],
        })
    return json.dumps({
        "run_id": run_id,
        "node_count": len(nodes),
        "roles_present": sorted(set(roles)),
        "total_cost_usd": round(total_cost, 4),
        "total_duration_ms": total_dur,
        "any_node_produced_artifact": produced,
        "nodes": nodes,
    }, indent=2)


def extract(
    trace_id: str,
    db: str | None = None,
    override_fn: Callable[[str, str], Iterable[dict[str, Any]]] | None = None,
    *,
    dispatch_fn: Callable[..., int] | None = None,
    repo_root: str | os.PathLike | None = None,
    emit: bool = True,
) -> list[dict[str, Any]]:
    """Extract gradients for a trace, print one JSON object per line."""
    con = _connect(db)
    con.row_factory = sqlite3.Row
    row = con.execute("SELECT * FROM execution_traces WHERE trace_id=?", (trace_id,)).fetchone()
    if not row:
        con.close()
        _fail(f"gradient_extract: trace_id {trace_id} not found")
    row_d = dict(row)

    # BUG4a: never mine a zero-work control node — it has no defect signal, so any
    # gradient extracted from it is a confabulation. Opt-out: MO_GRADIENT_SKIP_DEGENERATE=0.
    if (
        os.environ.get("MO_GRADIENT_SKIP_DEGENERATE", "1") == "1"
        and _is_degenerate_node(row_d)
    ):
        con.close()
        print(
            f"gradient_extract: trace_id {trace_id} is a zero-work control node "
            "(no duration/cost/evidence) — skipping (no gradients)",
            file=sys.stderr,
        )
        return []

    trace_json = json.dumps(row_d)
    run_context = _run_context(con, row_d)  # BUG2: whole-run facts for topology
    has_evidence = _row_has_captured_evidence(row_d)  # BUG4b/BUG1 grounding
    con.close()

    if override_fn is None:
        fn_name = os.environ.get("MINI_ORK_GRADIENT_EXTRACTOR_FN", "")
        candidate = globals().get(fn_name) if fn_name else None
        if callable(candidate):
            override_fn = cast(
                Callable[[str, str], Iterable[dict[str, Any]]], candidate
            )
        elif fn_name:
            _fail(f"gradient_extract: override fn {fn_name} not defined")

    if override_fn is None:
        prompt = _GRADIENT_EXTRACTOR_PROMPT_TEMPLATE.replace(
            "<<<RUN_CONTEXT>>>", run_context
        ).replace("<<<TRACE_JSON>>>", trace_json)
        rc, raw = _default_dispatch(
            prompt,
            repo_root=repo_root,
            dispatch_fn=dispatch_fn,
        )
        if rc != 0:
            _fail("gradient_extract: LLM dispatch failed")
        items = _parse_llm_output(raw, trace_id)
    else:
        items = list(override_fn(trace_id, trace_json))

    # BUG4b / BUG1 mitigation: a trace with no captured evidence (the systemic
    # telemetry gap) cannot support a high-confidence recipe lesson. Cap such
    # gradients BELOW context_assembler's 0.6 injection bar so unfounded claims
    # can never be injected as "Learned failure modes". Grounded rows (real
    # verifier_output / reviewer_verdict / files) keep their confidence.
    # Opt-out: MO_GRADIENT_GROUND_CONFIDENCE=0.
    if not has_evidence and os.environ.get("MO_GRADIENT_GROUND_CONFIDENCE", "1") == "1":
        try:
            cap = float(os.environ.get("MO_GRADIENT_UNGROUNDED_CAP", "0.55"))
        except (TypeError, ValueError):
            cap = 0.55
        for item in items:
            try:
                c = float(item.get("confidence", 0.5))
            except (TypeError, ValueError):
                c = 0.5
            if c > cap:
                item["confidence"] = cap
                item["grounding"] = "ungrounded_capture_gap"

    if emit:
        for item in items:
            print(json.dumps(item))
    return items
