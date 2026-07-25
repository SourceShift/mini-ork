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
