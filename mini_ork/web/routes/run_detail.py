"""Run detail endpoints: task_run row + events + llm_calls + artifacts + DAG."""

from __future__ import annotations

from typing import Any

import json
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Path as PathParam

from .. import agents as agent_mod, artifacts, recipes, why
from ..db import StateDB
from ..deps import get_db, get_home
from ..recipes import mini_ork_root

router = APIRouter(prefix="/api/v1/task-runs", tags=["run-detail"])


@router.get("/{task_run_id}")
def get_task_run(
    task_run_id: str = PathParam(..., description="task_runs.id"),
    db: StateDB = Depends(get_db),
) -> dict[str, Any]:
    tr = db.row("SELECT * FROM task_runs WHERE id = ?", (task_run_id,))
    if not tr:
        raise HTTPException(status_code=404, detail=f"task_run {task_run_id} not found")
    return tr


@router.get("/{task_run_id}/agents")
def list_agents(
    task_run_id: str = PathParam(...),
    db: StateDB = Depends(get_db),
    home=Depends(get_home),
) -> dict[str, Any]:
    """List every agent this task_run dispatched with rolled-up metrics."""
    return agent_mod.list_agents(db, home, mini_ork_root(), task_run_id)


@router.get("/{task_run_id}/agents/{node_id}")
def agent_detail(
    task_run_id: str = PathParam(...),
    node_id: str = PathParam(...),
    db: StateDB = Depends(get_db),
    home=Depends(get_home),
) -> dict[str, Any]:
    """Full per-agent detail: prompt, output artifact, LLM calls, child spawns."""
    out = agent_mod.agent_detail(db, home, mini_ork_root(), task_run_id, node_id)
    if "error" in out:
        raise HTTPException(status_code=404, detail=out["error"])
    return out


@router.get("/{task_run_id}/inputs")
def list_inputs(
    task_run_id: str = PathParam(...),
    db: StateDB = Depends(get_db),
    home=Depends(get_home),
) -> list[dict[str, Any]]:
    """List source documents that shaped the run.

    These are not output artifacts. They are the operator/kickoff/profile/plan
    inputs the UI should show before the DAG.
    """
    tr = db.row(
        "SELECT kickoff_path, plan_path FROM task_runs WHERE id = ?",
        (task_run_id,),
    )
    if not tr:
        raise HTTPException(status_code=404, detail="task_run not found")

    candidates = [
        ("kickoff", "Kickoff", tr.get("kickoff_path"), "markdown"),
        ("plan", "Plan", tr.get("plan_path"), "plan"),
        ("run_profile", "Run profile", str(home / "runs" / task_run_id / "run_profile.json"), "json"),
        (
            "profile_answers",
            "Profile answers",
            str(home / "runs" / task_run_id / "profile-answers.json"),
            "json",
        ),
    ]
    out: list[dict[str, Any]] = []
    for key, label, raw_path, kind in candidates:
        if not raw_path:
            continue
        path = _resolve_input_path(raw_path)
        if not path.exists() or not path.is_file():
            continue
        try:
            st = path.stat()
        except OSError:
            continue
        out.append(
            {
                "key": key,
                "label": label,
                "path": str(path),
                "name": path.name,
                "kind": kind,
                "size": st.st_size,
                "mtime": int(st.st_mtime),
            }
        )
    return out


@router.get("/{task_run_id}/inputs/{input_key}")
def read_input(
    task_run_id: str = PathParam(...),
    input_key: str = PathParam(...),
    db: StateDB = Depends(get_db),
    home=Depends(get_home),
) -> dict[str, Any]:
    """Read one source document for markdown/structured rendering."""
    items = list_inputs(task_run_id=task_run_id, db=db, home=home)
    item = next((i for i in items if i["key"] == input_key), None)
    if not item:
        raise HTTPException(status_code=404, detail=f"input not found: {input_key}")

    path = Path(item["path"]).resolve()
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        raise HTTPException(status_code=500, detail=str(e))
    return {**item, "content": text}


def _resolve_input_path(raw_path: str) -> Path:
    path = Path(raw_path)
    if not path.is_absolute():
        path = Path.cwd() / path
    return path.resolve()


@router.get("/{task_run_id}/why")
def get_why(
    task_run_id: str = PathParam(...),
    db: StateDB = Depends(get_db),
    home=Depends(get_home),
) -> dict[str, Any]:
    """Aggregate every failure signal scattered across disk + DB.

    Answers the UI's 'why did this fail?' question by reading execute.log,
    parsing verifier-result-*.json, pulling self_improve notes, and joining
    execution_traces with non-trivial verdicts.
    """
    return why.aggregate(home, db, task_run_id)


@router.get("/{task_run_id}/evidence")
def get_evidence(
    task_run_id: str = PathParam(...),
    path: str = "",
    home=Depends(get_home),
) -> dict[str, Any]:
    """Read an evidence log file by absolute path (must be under .mini-ork/)."""
    if not path:
        raise HTTPException(status_code=400, detail="?path=<absolute> required")
    try:
        return why.read_evidence_log(home, path)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail=f"evidence not found: {path}")
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e))


@router.get("/{task_run_id}/correlation")
def get_correlation(
    task_run_id: str = PathParam(...),
    db: StateDB = Depends(get_db),
) -> dict[str, Any]:
    """Diagnose how event/llm-call correlation works for this task_run.

    Reports trace_id, available bridge methods, and remediation hints —
    answers the UI's "why are some panels empty?" question.
    """
    tr = db.row(
        "SELECT id, trace_id, created_at, ended_at, kickoff_path FROM task_runs WHERE id = ?",
        (task_run_id,),
    )
    if not tr:
        raise HTTPException(status_code=404, detail="task_run not found")
    methods: list[str] = ["run_events.run_id"]  # always works for our node events
    issues: list[str] = []
    if tr.get("trace_id"):
        methods.append("mo_events.trace_id")
        methods.append("llm_calls.traceparent")
    else:
        issues.append(
            "task_runs.trace_id is NULL — mo_events + llm_calls cannot be correlated by trace_id. "
            "Falling back to time-window query (best-effort, may include events from concurrent runs)."
        )
        methods.append("mo_events.ts ∈ [created_at, ended_at] (best-effort)")
        methods.append("llm_calls.ts ∈ [created_at, ended_at] (best-effort)")
    if tr.get("ended_at") is None:
        issues.append(
            "task_runs.ended_at is NULL — the time-window fallback uses 'now' as the upper bound."
        )
    return {
        "task_run_id": tr["id"],
        "trace_id": tr.get("trace_id"),
        "bridge_methods": methods,
        "issues": issues,
        "remediation": (
            "Re-run with the updated bin/mini-ork-classify + bin/mini-ork-execute "
            "which write trace_id to task_runs. Legacy rows can be backfilled with: "
            "UPDATE task_runs SET trace_id = 'tr-backfill-' || id WHERE trace_id IS NULL;"
            if not tr.get("trace_id")
            else None
        ),
    }


@router.get("/{task_run_id}/learning")
def get_learning(
    task_run_id: str = PathParam(...),
    db: StateDB = Depends(get_db),
) -> dict[str, Any]:
    """Run-scoped view of mini-ork's persistent learning loop.

    This endpoint intentionally separates three concepts the UI should not
    conflate:
      - produced: records this run directly generated or evidenced
      - injected: prior memory that context_assemble would make available
      - self_improve: explicit cross-iteration learning rows, when present
    """
    tr = db.row(
        """
        SELECT id, task_class, recipe, status, trace_id, created_at, ended_at
        FROM task_runs WHERE id = ?
        """,
        (task_run_id,),
    )
    if not tr:
        raise HTTPException(status_code=404, detail="task_run not found")

    trace_id = tr.get("trace_id")
    task_class = tr.get("task_class") or ""
    recipe_name = tr.get("recipe")
    recipe_nodes = _recipe_node_names(recipe_name)

    # All node traces belonging to THIS run — gradients cite per-node trace
    # ids (tr-researcher-*, tr-rubric-*, ...) as evidence, not the run's
    # canonical task_runs.trace_id (the classify trace). Matching only the
    # canonical id undercounted produced gradients ~7x.
    run_trace_ids: list[str] = []
    if db.has_table("execution_traces"):
        run_trace_ids = [
            r["trace_id"]
            for r in db.rows(
                "SELECT trace_id FROM execution_traces WHERE run_id = ?",
                (task_run_id,),
            )
        ]
    if trace_id and trace_id not in run_trace_ids:
        run_trace_ids.append(trace_id)

    gradients_produced: list[dict[str, Any]] = []
    if run_trace_ids and db.has_table("gradient_records"):
        placeholders = ",".join("?" * len(run_trace_ids))
        gradients_produced = db.rows(
            f"""
            SELECT gradient_id, target, signal, suggested_change,
                   evidence, confidence, created_at
            FROM gradient_records
            WHERE evidence IN ({placeholders})
            ORDER BY created_at DESC
            LIMIT 25
            """,
            tuple(run_trace_ids),
        )
        _attach_gradient_attribution(db, gradients_produced, recipe_nodes)

    patterns_evidenced: list[dict[str, Any]] = []
    if trace_id and db.has_table("pattern_records"):
        candidates = db.rows(
            """
            SELECT pattern_id, description, evidence_trace_ids, frequency,
                   first_seen, last_seen, output_type, promoted_to, status
            FROM pattern_records
            WHERE evidence_trace_ids LIKE ?
            ORDER BY frequency DESC, last_seen DESC
            LIMIT 50
            """,
            (f"%{trace_id}%",),
        )
        patterns_evidenced = [
            {**row, "evidence_trace_ids": _parse_json_array(row.get("evidence_trace_ids"))}
            for row in candidates
            if trace_id in _parse_json_array(row.get("evidence_trace_ids"))
        ][:25]
        _attach_pattern_attribution(db, patterns_evidenced, recipe_nodes)

    learning_records: list[dict[str, Any]] = []
    if db.has_table("learning_record"):
        learning_records = db.rows(
            """
            SELECT id, run_id, iter, rank, category, title,
                   evidence_paths, arxiv_refs, patch_summary, outcome,
                   severity, confidence, benchmark_delta, created_at, updated_at
            FROM learning_record
            WHERE run_id = ?
            ORDER BY rank ASC, updated_at DESC
            LIMIT 25
            """,
            (task_run_id,),
        )
        for row in learning_records:
            row["evidence_paths"] = _parse_json_array(row.get("evidence_paths"))
            row["arxiv_refs"] = _parse_json_array(row.get("arxiv_refs"))
        _attach_learning_record_attribution(learning_records, recipe_nodes)

    prior_similar_runs: list[dict[str, Any]] = []
    if task_class and db.has_table("execution_traces"):
        # Exclude ALL of this run's own node traces — excluding only the
        # canonical trace_id made the panel list the current run's own
        # rubric/verify/researcher traces as "prior runs".
        exclude = run_trace_ids or [""]
        placeholders = ",".join("?" * len(exclude))
        prior_similar_runs = db.rows(
            f"""
            SELECT trace_id, task_class, status, cost_usd, duration_ms,
                   reviewer_verdict, final_artifact_ref, created_at
            FROM execution_traces
            WHERE task_class = ?
              AND trace_id NOT IN ({placeholders})
              AND (run_id IS NULL OR run_id != ?)
            ORDER BY created_at DESC
            LIMIT 10
            """,
            (task_class, *exclude, task_run_id),
        )

    known_failure_modes: list[dict[str, Any]] = []
    if task_class and db.has_table("gradient_records"):
        # Filter MUST mirror lib/context_assembler.sh::context_failure_modes_md
        # (task_class = ? OR target LIKE ?) — this panel claims to show what
        # gets injected, so the queries have to agree. target-LIKE alone
        # missed rows whose task_class matches but whose target doesn't
        # embed the class name (e.g. target=workflow.node.verify).
        known_failure_modes = db.rows(
            """
            SELECT gradient_id, target, signal, suggested_change,
                   evidence, confidence, created_at
            FROM gradient_records
            WHERE (task_class = ? OR target LIKE ?) AND confidence >= 0.6
            ORDER BY confidence DESC, created_at DESC
            LIMIT 10
            """,
            (task_class, f"%{task_class}%"),
        )
        _attach_gradient_attribution(db, known_failure_modes, recipe_nodes)

    return {
        "task_run_id": task_run_id,
        "task_class": task_class,
        "trace_id": trace_id,
        "summary": {
            "gradients_produced": len(gradients_produced),
            "patterns_evidenced": len(patterns_evidenced),
            "learning_records": len(learning_records),
            "prior_similar_runs_available": len(prior_similar_runs),
            "known_failure_modes_available": len(known_failure_modes),
        },
        "produced": {
            "gradients": gradients_produced,
            "patterns": patterns_evidenced,
        },
        "self_improve": {
            "records": learning_records,
        },
        "injected_candidates": {
            "prior_similar_runs": prior_similar_runs,
            "known_failure_modes": known_failure_modes,
            "source": "lib/context_assembler.sh",
            # "wired" = the injection actually happens in the live run
            # pipeline today. context_assemble() (which would inject
            # prior_similar_runs) exists but nothing in bin/ calls it —
            # claiming injection for it was misleading.
            "injection_points": [
                {
                    "name": "known_failure_modes",
                    "where": "bin/mini-ork-plan + bin/mini-ork-execute (context_failure_modes_md)",
                    "how": "gradient_records matching the task_class with confidence >= 0.6 are appended as a 'Learned failure modes' block to planner/researcher/implementer/reviewer prompts.",
                    "wired": True,
                },
                {
                    "name": "cross_iteration_learnings",
                    "where": "recipes/recursive-self-improve/prompts/*.md",
                    "how": "recursive-self-improve prompts explicitly ask agents to consume pattern_records and learning_record rows (that recipe only).",
                    "wired": True,
                },
                {
                    "name": "prior_similar_runs",
                    "where": "lib/context_assembler.sh (context_assemble)",
                    "how": "would insert same-task_class execution_traces into a ContextPack — implemented but NOT yet called by the run pipeline.",
                    "wired": False,
                },
            ],
        },
    }


def _recipe_node_names(recipe_name: str | None) -> list[str]:
    if not recipe_name:
        return []
    try:
        fp = recipes.fingerprint(recipe_name)
    except Exception:
        return []
    return [str(n.get("name")) for n in fp.get("nodes", []) if n.get("name")]


def _attach_gradient_attribution(
    db: StateDB,
    rows: list[dict[str, Any]],
    recipe_nodes: list[str],
) -> None:
    trace_ids = [str(r.get("evidence")) for r in rows if r.get("evidence")]
    traces = _trace_lookup(db, trace_ids)
    for row in rows:
        row["agent_attribution"] = _infer_agent_attribution(
            recipe_nodes,
            trace=traces.get(str(row.get("evidence"))),
            text_parts=[
                row.get("target"),
                row.get("signal"),
                row.get("suggested_change"),
                row.get("evidence"),
            ],
        )


def _attach_pattern_attribution(
    db: StateDB,
    rows: list[dict[str, Any]],
    recipe_nodes: list[str],
) -> None:
    trace_ids: list[str] = []
    for row in rows:
        trace_ids.extend(str(t) for t in row.get("evidence_trace_ids") or [])
    traces = _trace_lookup(db, trace_ids)
    for row in rows:
        attributions = [
            _infer_agent_attribution(
                recipe_nodes,
                trace=traces.get(str(tid)),
                text_parts=[row.get("description"), row.get("output_type"), tid],
            )
            for tid in row.get("evidence_trace_ids") or []
        ]
        concrete = [a for a in attributions if a.get("node_id")]
        unique = sorted({str(a["node_id"]) for a in concrete})
        row["agent_attribution"] = (
            concrete[0]
            if len(unique) == 1
            else {
                "node_id": None,
                "source": "mixed-evidence" if unique else "unknown",
                "confidence": "mixed" if unique else "none",
                "candidate_node_ids": unique,
            }
        )


def _attach_learning_record_attribution(
    rows: list[dict[str, Any]],
    recipe_nodes: list[str],
) -> None:
    for row in rows:
        row["agent_attribution"] = _infer_agent_attribution(
            recipe_nodes,
            text_parts=[
                row.get("title"),
                row.get("patch_summary"),
                " ".join(str(p) for p in row.get("evidence_paths") or []),
            ],
        )


def _trace_lookup(db: StateDB, trace_ids: list[str]) -> dict[str, dict[str, Any]]:
    if not trace_ids or not db.has_table("execution_traces"):
        return {}
    out: dict[str, dict[str, Any]] = {}
    for trace_id in sorted(set(t for t in trace_ids if t)):
        row = db.row(
            """
            SELECT trace_id, agent_version_id, final_artifact_ref,
                   verifier_output, reviewer_verdict, task_class
            FROM execution_traces
            WHERE trace_id = ?
            """,
            (trace_id,),
        )
        if row:
            out[trace_id] = row
    return out


def _infer_agent_attribution(
    recipe_nodes: list[str],
    trace: dict[str, Any] | None = None,
    text_parts: list[Any] | None = None,
) -> dict[str, Any]:
    texts = [str(p) for p in (text_parts or []) if p]
    if trace:
        texts.extend(
            str(trace.get(k) or "")
            for k in ("agent_version_id", "final_artifact_ref", "verifier_output", "reviewer_verdict")
        )

    haystack = "\n".join(texts)
    for node in recipe_nodes:
        if node and node in haystack:
            return {"node_id": node, "source": "explicit-node-reference", "confidence": "high"}

    artifact = str(trace.get("final_artifact_ref") or "") if trace else ""
    node_from_artifact = _node_from_artifact_name(artifact, recipe_nodes)
    if node_from_artifact:
        return {"node_id": node_from_artifact, "source": "final_artifact_ref", "confidence": "medium"}

    agent_version = str(trace.get("agent_version_id") or "") if trace else ""
    if agent_version:
        return {"node_id": None, "agent_version_id": agent_version, "source": "agent_version_id", "confidence": "low"}

    return {"node_id": None, "source": "unknown", "confidence": "none"}


def _node_from_artifact_name(path: str, recipe_nodes: list[str]) -> str | None:
    name = Path(path).name
    candidates: list[str] = []
    for prefix, suffix in (
        ("context-", ".json"),
        ("review-", ".json"),
        ("impl-", ".log"),
        ("agent-", ".transcript.json"),
        ("agent-", ".stream.jsonl"),
    ):
        if name.startswith(prefix) and name.endswith(suffix):
            candidates.append(name[len(prefix) : -len(suffix)])
    if name.startswith("lens-") and name.endswith(".md"):
        stem = name[len("lens-") : -len(".md")]
        candidates.extend([stem, f"{stem}_lens", f"{stem}-lens"])
    for candidate in candidates:
        if candidate in recipe_nodes:
            return candidate
    return None


def _parse_json_array(raw: Any) -> list[Any]:
    if isinstance(raw, list):
        return raw
    if raw in (None, ""):
        return []
    try:
        value = json.loads(str(raw))
    except json.JSONDecodeError:
        return []
    return value if isinstance(value, list) else []


@router.get("/{task_run_id}/events")
def get_events(
    task_run_id: str = PathParam(...),
    db: StateDB = Depends(get_db),
    limit: int = 500,
) -> list[dict[str, Any]]:
    """mo_events + run_events scoped to this task_run.

    Bridge methods, in priority order:
      1. run_events.run_id = task_run_id (always works for node_start/end + recursive emits)
      2. mo_events.trace_id = task_runs.trace_id (strict, requires trace_id populated)
      3. mo_events.ts ∈ [task_run.created_at, ended_at] (fallback when trace_id NULL)

    Each row carries `bridge` field telling the UI which method matched it.
    """
    tr = db.row(
        "SELECT trace_id, created_at, ended_at FROM task_runs WHERE id = ?",
        (task_run_id,),
    )
    if not tr:
        raise HTTPException(status_code=404, detail="task_run not found")

    out: list[dict[str, Any]] = []
    seen_ids: set[tuple[str, str]] = set()  # dedup by (source, id)

    def _add(row: dict[str, Any], source: str, bridge: str) -> None:
        key = (source, str(row.get("id")))
        if key in seen_ids:
            return
        seen_ids.add(key)
        row["source"] = source
        row["bridge"] = bridge
        out.append(row)

    # 1. mo_events by trace_id (strict)
    if db.has_table("mo_events") and tr.get("trace_id"):
        for r in db.rows(
            """
            SELECT id, ts, event_type, actor, status, duration_ms, cost_usd,
                   artifact_path, payload_json
            FROM mo_events WHERE trace_id = ?
            ORDER BY ts ASC LIMIT ?
            """,
            (tr["trace_id"], limit),
        ):
            _add(r, "mo_events", "trace_id")

    # 2. mo_events by time window (best-effort fallback when trace_id is missing
    #    OR when this run also had nested emits with a different trace_id)
    if db.has_table("mo_events") and tr.get("created_at"):
        upper = tr.get("ended_at") or int(__import__("time").time())
        for r in db.rows(
            """
            SELECT id, ts, event_type, actor, status, duration_ms, cost_usd,
                   artifact_path, payload_json
            FROM mo_events
            WHERE strftime('%s', ts) BETWEEN ? AND ?
            ORDER BY ts ASC LIMIT ?
            """,
            (int(tr["created_at"]), int(upper), limit),
        ):
            _add(r, "mo_events", "time-window")

    # 3. run_events scoped by run_id (always works for our node lifecycle emits)
    if db.has_table("run_events"):
        for r in db.rows(
            """
            SELECT event_id AS id, created_at AS ts, event_type,
                   NULL AS actor, NULL AS status, NULL AS duration_ms,
                   NULL AS cost_usd, NULL AS artifact_path, payload_json
            FROM run_events WHERE run_id = ?
            ORDER BY created_at ASC LIMIT ?
            """,
            (task_run_id, limit),
        ):
            _add(r, "run_events", "run_id")

    out.sort(key=lambda e: str(e.get("ts") or ""))
    return out


@router.get("/{task_run_id}/llm-calls")
def get_llm_calls(
    task_run_id: str = PathParam(...),
    db: StateDB = Depends(get_db),
) -> list[dict[str, Any]]:
    """LLM calls correlated to a task_run via trace_id (strict) or time window (fallback)."""
    tr = db.row(
        "SELECT trace_id, created_at, ended_at FROM task_runs WHERE id = ?",
        (task_run_id,),
    )
    if not tr or not db.has_table("llm_calls"):
        return []

    out: list[dict[str, Any]] = []
    seen: set[int] = set()

    def _add(row: dict[str, Any], bridge: str) -> None:
        if row["id"] in seen:
            return
        seen.add(row["id"])
        row["bridge"] = bridge
        out.append(row)

    # 1. trace_id match (strict)
    trace_id = tr.get("trace_id")
    if trace_id:
        for r in db.rows(
            """
            SELECT id, provider, model_id, tier, feature_name, actor,
                   input_tokens, output_tokens, total_tokens, cost_usd,
                   duration_ms, status, finish_reason, ts
            FROM llm_calls
            WHERE traceparent LIKE ?
            ORDER BY ts ASC
            """,
            (f"%{trace_id}%",),
        ):
            _add(r, "trace_id")

    # 2. time-window fallback
    if tr.get("created_at"):
        upper = tr.get("ended_at") or int(__import__("time").time())
        for r in db.rows(
            """
            SELECT id, provider, model_id, tier, feature_name, actor,
                   input_tokens, output_tokens, total_tokens, cost_usd,
                   duration_ms, status, finish_reason, ts
            FROM llm_calls
            WHERE strftime('%s', ts) BETWEEN ? AND ?
            ORDER BY ts ASC
            """,
            (int(tr["created_at"]), int(upper)),
        ):
            _add(r, "time-window")

    return out


@router.get("/{task_run_id}/artifacts")
def get_artifacts(task_run_id: str = PathParam(...), home=Depends(get_home)) -> list[dict[str, Any]]:
    return artifacts.list_artifacts(home, task_run_id)


@router.get("/{task_run_id}/artifacts/{relpath:path}")
def read_artifact(
    task_run_id: str = PathParam(...),
    relpath: str = PathParam(...),
    home=Depends(get_home),
) -> dict[str, Any]:
    try:
        return artifacts.read_artifact(home, task_run_id, relpath)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail=f"artifact not found: {relpath}")
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e))


@router.get("/{task_run_id}/dag")
def get_dag(task_run_id: str = PathParam(...), db: StateDB = Depends(get_db)) -> dict[str, Any]:
    """Return the recipe DAG with node statuses derived from run_events.

    Status rules (derived from node_start/node_end events emitted by
    bin/mini-ork-execute:_dispatch_node via lib/mo_node_events.sh):
      - never_seen : no node_start event for this node
      - running    : node_start present, no node_end yet
      - done       : node_end present, no verdict failure
      - failed     : node_end present with verdict in {REQUEST_CHANGES, ESCALATE, CRASH}
    """
    tr = db.row("SELECT recipe FROM task_runs WHERE id = ?", (task_run_id,))
    if not tr or not tr.get("recipe"):
        raise HTTPException(status_code=404, detail="task_run or recipe not found")
    fp = recipes.fingerprint(tr["recipe"])

    node_status = _node_status_map(db, task_run_id)
    # Merge status into each node, preserving family attribution
    for n in fp["nodes"]:
        s = node_status.get(n["name"], {})
        n["status"] = s.get("status", "never_seen")
        n["started_at"] = s.get("started_at")
        n["ended_at"] = s.get("ended_at")
        n["duration_ms"] = s.get("duration_ms")
        n["verdict"] = s.get("verdict")
        n["artifact_path"] = s.get("artifact_path")

    return {"task_run_id": task_run_id, "recipe": tr["recipe"], **fp}


def _node_status_map(db: StateDB, task_run_id: str) -> dict[str, dict[str, Any]]:
    """Aggregate node_start / node_end events per node_id."""
    if not db.has_table("run_events"):
        return {}
    rows = db.rows(
        """
        SELECT event_type, created_at, payload_json
        FROM run_events
        WHERE run_id = ? AND event_type IN ('node_start', 'node_end')
        ORDER BY created_at ASC
        """,
        (task_run_id,),
    )
    import json

    out: dict[str, dict[str, Any]] = {}
    for r in rows:
        try:
            payload = json.loads(r["payload_json"]) if r["payload_json"] else {}
        except json.JSONDecodeError:
            payload = {}
        node_id = payload.get("node_id")
        if not node_id:
            continue
        entry = out.setdefault(node_id, {"status": "never_seen"})
        if r["event_type"] == "node_start":
            entry["status"] = "running"
            entry["started_at"] = r["created_at"]
        elif r["event_type"] == "node_end":
            verdict = payload.get("verdict")
            entry["status"] = (
                "failed"
                if verdict in ("REQUEST_CHANGES", "ESCALATE", "CRASH")
                else "done"
            )
            entry["duration_ms"] = payload.get("duration_ms")
            entry["verdict"] = verdict
            entry["artifact_path"] = payload.get("artifact_path")
            entry["ended_at"] = r["created_at"]
    return out
