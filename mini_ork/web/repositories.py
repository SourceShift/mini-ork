"""Repository layer for the web routes — SQL lives here, handlers shape JSON (M9, SRP/DIP).

The route handlers keep classification + response shaping; every SELECT against
the learning-loop tables (task_runs / execution_traces / gradient_records /
pattern_records / learning_record) moves here VERBATIM so the JSON responses
stay byte-identical for the same db state.

Each fetch method guards its own ``has_table`` probe and returns an empty
list/dict when the table is absent — the fresh-state.db case yields empty
structures, never a 500, exactly as the inline probes did.
"""
from __future__ import annotations

import json
import sqlite3
from typing import Any, Sequence

from .db import StateDB


class LearningRepository:
    """Read queries for the run-scoped learning endpoint (+ shared counts).

    Wraps a ``StateDB`` (the read-only, per-thread pooled handle) rather than
    opening its own connection, so the web app's concurrency + WAL semantics
    are unchanged.
    """

    def __init__(self, db: StateDB):
        self._db = db

    def has_table(self, name: str) -> bool:
        return self._db.has_table(name)

    # ── task_runs ────────────────────────────────────────────────────────────

    def fetch_task_run(self, task_run_id: str) -> dict[str, Any] | None:
        return self._db.row(
            """
            SELECT id, task_class, recipe, status, trace_id, created_at, ended_at
            FROM task_runs WHERE id = ?
            """,
            (task_run_id,),
        )

    # ── execution_traces ─────────────────────────────────────────────────────

    def fetch_execution_traces(self, task_run_id: str) -> list[str]:
        """trace_ids of all node traces belonging to this run.

        Gradients cite per-node trace ids (tr-researcher-*, tr-rubric-*, ...) as
        evidence, not the run's canonical task_runs.trace_id (the classify
        trace). Matching only the canonical id undercounted produced gradients
        ~7x.
        """
        if not self._db.has_table("execution_traces"):
            return []
        return [
            r["trace_id"]
            for r in self._db.rows(
                "SELECT trace_id FROM execution_traces WHERE run_id = ?",
                (task_run_id,),
            )
        ]

    def fetch_prior_similar_runs(
        self,
        task_class: str,
        exclude: Sequence[str],
        task_run_id: str,
    ) -> list[dict[str, Any]]:
        """Same-task_class traces excluding ALL of this run's own node traces —
        excluding only the canonical trace_id made the panel list the current
        run's own rubric/verify/researcher traces as "prior runs"."""
        if not self._db.has_table("execution_traces"):
            return []
        placeholders = ",".join("?" * len(exclude))
        return self._db.rows(
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

    def fetch_trace_summaries(self, trace_ids: Sequence[str]) -> dict[str, dict[str, Any]]:
        """Attribution lookup: trace_id → the columns agent-attribution infers from."""
        if not trace_ids or not self._db.has_table("execution_traces"):
            return {}
        out: dict[str, dict[str, Any]] = {}
        for trace_id in sorted(set(t for t in trace_ids if t)):
            row = self._db.row(
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

    # ── gradient_records ─────────────────────────────────────────────────────

    def fetch_gradient_records(self, trace_ids: Sequence[str]) -> list[dict[str, Any]]:
        """Gradients whose evidence cites one of this run's trace ids (produced)."""
        if not trace_ids or not self._db.has_table("gradient_records"):
            return []
        placeholders = ",".join("?" * len(trace_ids))
        return self._db.rows(
            f"""
            SELECT gradient_id, target, signal, suggested_change,
                   evidence, confidence, created_at
            FROM gradient_records
            WHERE evidence IN ({placeholders})
            ORDER BY created_at DESC
            LIMIT 25
            """,
            tuple(trace_ids),
        )

    def fetch_failure_mode_gradients(self, task_class: str) -> list[dict[str, Any]]:
        """What context_assemble would inject as known failure modes.

        Filter MUST mirror mini_ork.context_assembler.failure_modes_md.
        (task_class = ? OR target LIKE ?) — this panel claims to show what
        gets injected, so the queries have to agree. target-LIKE alone
        missed rows whose task_class matches but whose target doesn't
        embed the class name (e.g. target=workflow.node.verify).
        """
        if not self._db.has_table("gradient_records"):
            return []
        return self._db.rows(
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

    def gradient_count(self) -> int:
        if not self._db.has_table("gradient_records"):
            return 0
        rows = self._db.rows("SELECT COUNT(*) AS n FROM gradient_records")
        return int(rows[0]["n"]) if rows else 0

    # ── pattern_records ──────────────────────────────────────────────────────

    def fetch_pattern_candidates(self, trace_id: str) -> list[dict[str, Any]]:
        """LIKE pre-filter for patterns evidencing this run's canonical trace.

        The LIKE can over-match (substring); callers re-check membership against
        the parsed evidence_trace_ids array before showing a row.
        """
        if not self._db.has_table("pattern_records"):
            return []
        return self._db.rows(
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

    # ── learning_record ──────────────────────────────────────────────────────

    def fetch_learning_records(self, task_run_id: str) -> list[dict[str, Any]]:
        """Explicit cross-iteration self-improve rows for this run."""
        if not self._db.has_table("learning_record"):
            return []
        return self._db.rows(
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


class RunDetailRepository:
    """Read queries for the run-detail route handlers (M9 follow-up, SRP/DIP).

    Covers the four concerns that remained inline in routes/run_detail.py:
    the task_run row fetches, run events (run_events + mo_events bridges),
    llm_calls correlation, and the DAG node-lifecycle events. SQL is moved
    VERBATIM; has_table guards are preserved inside each fetch so a fresh
    state.db yields ``[]`` / ``None``, never a 500 — exactly as the inline
    probes did. Handlers keep param validation, bridge classification, and
    response shaping.
    """

    def __init__(self, db: StateDB):
        self._db = db

    def has_table(self, name: str) -> bool:
        return self._db.has_table(name)

    # ── task_runs ────────────────────────────────────────────────────────────

    def fetch_task_run_row(self, task_run_id: str) -> dict[str, Any] | None:
        """The full task_runs row (the detail endpoint returns it wholesale)."""
        return self._db.row("SELECT * FROM task_runs WHERE id = ?", (task_run_id,))

    def fetch_input_paths(self, task_run_id: str) -> dict[str, Any] | None:
        """kickoff/plan/recipe columns the inputs endpoint resolves to files."""
        return self._db.row(
            "SELECT kickoff_path, plan_path, recipe FROM task_runs WHERE id = ?",
            (task_run_id,),
        )

    def fetch_correlation_row(self, task_run_id: str) -> dict[str, Any] | None:
        """Columns the correlation diagnostic reports on."""
        return self._db.row(
            "SELECT id, trace_id, created_at, ended_at, kickoff_path FROM task_runs WHERE id = ?",
            (task_run_id,),
        )

    def fetch_trace_window(self, task_run_id: str) -> dict[str, Any] | None:
        """trace_id + [created_at, ended_at] — the events/llm-calls bridge input."""
        return self._db.row(
            "SELECT trace_id, created_at, ended_at FROM task_runs WHERE id = ?",
            (task_run_id,),
        )

    def fetch_run_recipe(self, task_run_id: str) -> dict[str, Any] | None:
        """Just the recipe name, for the DAG endpoint's fingerprint lookup."""
        return self._db.row(
            "SELECT recipe FROM task_runs WHERE id = ?", (task_run_id,)
        )

    # ── run_events ───────────────────────────────────────────────────────────

    def fetch_last_run_event_ts(self, task_run_id: str) -> Any:
        """Most recent run_events.created_at for staleness detection (None if
        the table is absent or the run has no events)."""
        if not self._db.has_table("run_events"):
            return None
        ev = self._db.row(
            "SELECT MAX(created_at) AS ts FROM run_events WHERE run_id = ?",
            (task_run_id,),
        )
        return ev.get("ts") if ev else None

    def fetch_run_events(self, task_run_id: str, limit: int) -> list[dict[str, Any]]:
        """run_events rows reshaped to the mo_events column layout, oldest first."""
        if not self._db.has_table("run_events"):
            return []
        return self._db.rows(
            """
            SELECT event_id AS id, created_at AS ts, event_type,
                   NULL AS actor, NULL AS status, NULL AS duration_ms,
                   NULL AS cost_usd, NULL AS artifact_path, payload_json
            FROM run_events WHERE run_id = ?
            ORDER BY created_at ASC LIMIT ?
            """,
            (task_run_id, limit),
        )

    def fetch_node_lifecycle_events(self, task_run_id: str) -> list[dict[str, Any]]:
        """node_start / node_end events the DAG status derivation consumes."""
        if not self._db.has_table("run_events"):
            return []
        return self._db.rows(
            """
            SELECT event_type, created_at, payload_json
            FROM run_events
            WHERE run_id = ? AND event_type IN ('node_start', 'node_end')
            ORDER BY created_at ASC
            """,
            (task_run_id,),
        )

    def fetch_run_recipes(
        self, task_run_ids: Sequence[str] | None = None,
        *, limit: int = 50,
    ) -> dict[str, str]:
        """Bulk sibling of :meth:`fetch_run_recipe` — one ``SELECT id, recipe``
        round-trip across many runs.

        ``task_run_ids=None`` falls back to the ``limit`` most recent runs so
        the IDE's ``dags`` page is usable from the CLI without an explicit
        ``--arg ids=...``; the default 50 mirrors the page's
        ``DEFAULT_PAGE_SIZE`` so the board's two batched calls agree on the
        window. An empty list yields an empty dict (no rows match).

        NULL ``recipe`` values (per-run column nullable; see
        ``tests/unit/test_run_detail_repo_py.py:177``) are dropped — the
        caller treats their run as "recipe not found" via the empty mapping.
        """
        if task_run_ids is None:
            rows = self._db.rows(
                "SELECT id, recipe FROM task_runs "
                f"ORDER BY created_at DESC LIMIT {int(limit)}"
            )
        elif not task_run_ids:
            return {}
        else:
            placeholders = ",".join("?" * len(task_run_ids))
            rows = self._db.rows(
                f"SELECT id, recipe FROM task_runs WHERE id IN ({placeholders})",
                tuple(task_run_ids),
            )
        out: dict[str, str] = {}
        for r in rows:
            recipe = r.get("recipe")
            if recipe:
                out[str(r["id"])] = str(recipe)
        return out

    def fetch_node_lifecycle_events_bulk(
        self, task_run_ids: Sequence[str]
    ) -> dict[str, list[dict[str, Any]]]:
        """Bulk sibling of :meth:`fetch_node_lifecycle_events` — one round-trip.

        Returns ``{run_id: [rows…]}`` with rows in ``created_at ASC`` order,
        matching the per-run method's ordering so the dags page can merge
        them straight into :func:`derive_node_statuses`. The same
        ``event_type IN ('node_start', 'node_end')`` filter is preserved
        verbatim — ``mini_ork/web/routes/run_detail.py::_node_status_map``
        depends on it.
        """
        if not task_run_ids or not self._db.has_table("run_events"):
            return {rid: [] for rid in task_run_ids}
        placeholders = ",".join("?" * len(task_run_ids))
        rows = self._db.rows(
            f"""
            SELECT run_id, event_type, created_at, payload_json
            FROM run_events
            WHERE run_id IN ({placeholders})
              AND event_type IN ('node_start', 'node_end')
            ORDER BY created_at ASC
            """,
            tuple(task_run_ids),
        )
        out: dict[str, list[dict[str, Any]]] = {rid: [] for rid in task_run_ids}
        for r in rows:
            out.setdefault(str(r["run_id"]), []).append(r)
        return out

    # ── mo_events ────────────────────────────────────────────────────────────

    def fetch_mo_events_by_trace_id(
        self, trace_id: str, limit: int
    ) -> list[dict[str, Any]]:
        """Strict trace_id bridge for the events endpoint."""
        if not self._db.has_table("mo_events"):
            return []
        return self._db.rows(
            """
            SELECT id, ts, event_type, actor, status, duration_ms, cost_usd,
                   artifact_path, payload_json
            FROM mo_events WHERE trace_id = ?
            ORDER BY ts ASC LIMIT ?
            """,
            (trace_id, limit),
        )

    def fetch_mo_events_in_window(
        self, start: int, upper: int, limit: int
    ) -> list[dict[str, Any]]:
        """Best-effort time-window bridge for the events endpoint."""
        if not self._db.has_table("mo_events"):
            return []
        return self._db.rows(
            """
            SELECT id, ts, event_type, actor, status, duration_ms, cost_usd,
                   artifact_path, payload_json
            FROM mo_events
            -- CAST is load-bearing: strftime returns TEXT, and TEXT BETWEEN
            -- INTEGER params is always false in SQLite (ints sort before text)
            WHERE CAST(strftime('%s', ts) AS INTEGER) BETWEEN ? AND ?
            ORDER BY ts ASC LIMIT ?
            """,
            (start, upper, limit),
        )

    # ── llm_calls ────────────────────────────────────────────────────────────

    def _llm_calls_select(self) -> str:
        """SELECT column list with the cached_input_tokens compat shim.

        Older state.db files lack the cached_input_tokens column; the inline
        code probed PRAGMA table_info and substituted a literal 0. That probe
        moves here with the query.
        """
        llm_cols = {r["name"] for r in self._db.rows("PRAGMA table_info(llm_calls)")}
        cached_input_expr = (
            "cached_input_tokens"
            if "cached_input_tokens" in llm_cols
            else "0 AS cached_input_tokens"
        )
        return f"""
            SELECT id, provider, model_id, tier, feature_name, actor,
                   input_tokens, output_tokens, total_tokens, cost_usd,
                   {cached_input_expr},
                   duration_ms, status, finish_reason, ts
            FROM llm_calls
            """

    def fetch_llm_calls_by_trace_id(self, trace_id: str) -> list[dict[str, Any]]:
        """Strict traceparent-substring bridge for the llm-calls endpoint."""
        if not self._db.has_table("llm_calls"):
            return []
        return self._db.rows(
            self._llm_calls_select()
            + """
            WHERE traceparent LIKE ?
            ORDER BY ts ASC
            """,
            (f"%{trace_id}%",),
        )

    def fetch_llm_calls_by_run_id(self, run_id: str) -> list[dict[str, Any]]:
        """Strict run-id bridge for the llm-calls endpoint.

        llm_calls rows stamped with the run's id are the run's calls; trace_id
        and time-window bridges can pick up sibling runs in the same window.
        Returns ``[]`` when the table or the ``run_id`` column is missing so
        the legacy bridge still applies for state.db files predating the
        migration that added ``run_id``."""
        if not self._db.has_table("llm_calls"):
            return []
        try:
            return self._db.rows(
                self._llm_calls_select()
                + """
                WHERE run_id = ?
                ORDER BY ts ASC
                """,
                (run_id,),
            )
        except sqlite3.OperationalError:
            return []

    def fetch_llm_calls_in_window(self, start: int, upper: int) -> list[dict[str, Any]]:
        """Best-effort time-window bridge for the llm-calls endpoint."""
        if not self._db.has_table("llm_calls"):
            return []
        return self._db.rows(
            self._llm_calls_select()
            + """
            -- CAST is load-bearing: strftime returns TEXT, and TEXT BETWEEN
            -- INTEGER params is always false in SQLite (ints sort before text)
            WHERE CAST(strftime('%s', ts) AS INTEGER) BETWEEN ? AND ?
            ORDER BY ts ASC
            """,
            (start, upper),
        )


# Statuses the DAG view can show. Mirrors the vocabulary at
# ``mini_ork/web/routes/run_detail.py:658-664`` exactly — do not add a fifth.
_NEVER_SEEN = "never_seen"
_RUNNING = "running"
_DONE = "done"
_FAILED = "failed"
# Verdict values that flip a ``node_end`` to ``failed``. From
# ``run_detail.py:705``. Mirrored here so this helper can stand alone.
_FAILED_VERDICTS = frozenset({"REQUEST_CHANGES", "ESCALATE", "CRASH"})


def derive_node_statuses(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Aggregate ``node_start`` / ``node_end`` events into per-node statuses.

    Pure module-level helper. Takes the rows already fetched by
    :meth:`RunDetailRepository.fetch_node_lifecycle_events_bulk` (or its
    single-run counterpart) and returns ``{node_id: {status, …}}`` with one
    entry per node that emitted at least one lifecycle event. Run-level
    "no events at all" → ``{}``; the merge default of ``never_seen`` is the
    caller's responsibility (mirroring ``run_detail.py:673-674``).

    Rules, verbatim from ``mini_ork/web/routes/run_detail.py:658-664``:

    - ``never_seen`` : no ``node_start`` event for this node  (caller applies)
    - ``running``    : ``node_start`` present, no ``node_end`` yet
    - ``done``       : ``node_end`` present, no verdict failure
    - ``failed``     : ``node_end`` present with verdict in
                       {``REQUEST_CHANGES``, ``ESCALATE``, ``CRASH``}

    ``rows[i]`` is a dict with at least ``event_type`` and ``payload_json``
    keys; ``payload_json`` may be ``None`` or unparseable — both yield an
    empty payload and the row is skipped (no ``node_id`` to key on).
    """
    out: dict[str, dict[str, Any]] = {}
    for r in rows:
        payload_raw = r.get("payload_json")
        try:
            payload = json.loads(payload_raw) if payload_raw else {}
        except json.JSONDecodeError:
            payload = {}
        node_id = payload.get("node_id")
        if not node_id:
            continue
        entry = out.setdefault(str(node_id), {"status": _NEVER_SEEN})
        if r.get("event_type") == "node_start":
            entry["status"] = _RUNNING
            entry["started_at"] = r.get("created_at")
        elif r.get("event_type") == "node_end":
            verdict = payload.get("verdict")
            entry["status"] = _FAILED if verdict in _FAILED_VERDICTS else _DONE
            entry["duration_ms"] = payload.get("duration_ms")
            entry["verdict"] = verdict
            entry["artifact_path"] = payload.get("artifact_path")
            entry["ended_at"] = r.get("created_at")
    return out


class ArtifactsRepository:
    """Read queries for the run_artifacts trajectory store (migration 0047).

    Same StateDB-wrapping, has_table-guarded style as LearningRepository: an
    old state.db without run_artifacts yields ``[]`` / ``None``, never a 500.
    """

    def __init__(self, db: StateDB):
        self._db = db

    def list_artifacts(
        self, run_id: str, kind: str | None = None
    ) -> list[dict[str, Any]]:
        """All run_artifacts rows for a run, oldest first; optional kind filter."""
        if not self._db.has_table("run_artifacts"):
            return []
        if kind:
            return self._db.rows(
                """
                SELECT id, run_id, node_id, call_id, kind, rel_path,
                       bytes, sha256, created_at
                FROM run_artifacts
                WHERE run_id = ? AND kind = ?
                ORDER BY created_at ASC, id ASC
                """,
                (run_id, kind),
            )
        return self._db.rows(
            """
            SELECT id, run_id, node_id, call_id, kind, rel_path,
                   bytes, sha256, created_at
            FROM run_artifacts
            WHERE run_id = ?
            ORDER BY created_at ASC, id ASC
            """,
            (run_id,),
        )

    def fetch_artifact(self, run_id: str, artifact_id: int) -> dict[str, Any] | None:
        """One row by primary key, scoped to the run (no cross-run reads)."""
        if not self._db.has_table("run_artifacts"):
            return None
        return self._db.row(
            """
            SELECT id, run_id, node_id, call_id, kind, rel_path,
                   bytes, sha256, created_at
            FROM run_artifacts
            WHERE id = ? AND run_id = ?
            """,
            (artifact_id, run_id),
        )
