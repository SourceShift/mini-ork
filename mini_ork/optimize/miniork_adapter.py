"""Concrete ``GepaAdapter`` for mini-ork's offline execution_traces store.

R4b: wires R4a's reflective prompt optimizer (a Protocol + ``optimize()``
loop in ``gepa.py``) into a real data source — the ``execution_traces`` and
``process_reward`` rows the project already writes — so the suggestion a
reflect hook emits is grounded in observed, scored, prior runs rather than
fresh LLM evals.

The adapter is offline: ``evaluate()`` reads cached ``reward_value`` rows
keyed by ``prompt_version_hash``; it never dispatches a model. This is the
hard constraint from the task brief. For hash mismatches we fall back to
the parent's cached mean score — i.e. "we have no evidence this candidate
is different from its parent, so treat it as parity" — instead of silently
defaulting to a fabricated number.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any

from .gepa import optimize


class MiniOrkGepaAdapter:
    """Protocol-conforming adapter over ``execution_traces`` + ``process_reward``.

    ``__init__`` loads the most recent ``n`` execution_traces rows for a
    given ``task_class`` from the configured mini-ork ``state.db`` and treats
    each row as one example in ``full_batch``. Per-row fields exposed to
    ``evaluate()``: ``prompt_version_hash`` (the candidate fingerprint in
    mini-ork's score cache) and ``reward_value`` (defaulting to 0.0 when
    unpopulated). The PRM is the source of truth — ``reward_value`` is the
    offline reward signal.

    ``evaluate(batch, candidate)`` scores the candidate by matching
    ``candidate.get('prompt_version_hash')`` to cached rows; rows that
    don't match fall back to the parent's cached mean (so a brand-new
    prompt_hash is treated as parity rather than inventing a number).

    ``make_reflective_dataset()`` surfaces each example's
    ``verifier_output`` and ``reviewer_verdict`` strings as feedback dicts
    so ``reflect_on_component`` can propose a mutation grounded in real
    failure signals.
    """

    def __init__(
        self,
        db_path: str | os.PathLike[str],
        task_class: str,
        recipe: str = "",
        n: int = 20,
    ) -> None:
        self.task_class = task_class
        self.recipe = recipe
        self.n = n
        self.full_eval_count = 0
        self.iteration_count = 0
        self._db_path = str(db_path)
        self.full_batch: list[dict[str, Any]] = self._load_full_batch()

        # Cache of {prompt_version_hash: [reward_value, ...]} for the
        # rows we loaded. Build once at __init__ so evaluate() is O(1).
        self._score_cache: dict[str, list[float]] = {}
        for row in self.full_batch:
            self._score_cache.setdefault(row["prompt_version_hash"], []).append(
                row["reward_value"]
            )
        # Default score to fall back to when a candidate has no cached
        # rows: the parent's mean across whatever rows ARE cached, so
        # unknown candidates inherit parent evidence instead of being
        # scored as 0.0 (which would silently drag the optimizer toward
        # "ignore new candidates").
        self._default_score = self._compute_default_score()

    def _load_full_batch(self) -> list[dict[str, Any]]:
        if not Path(self._db_path).exists():
            return []
        con = sqlite3.connect(self._db_path)
        con.row_factory = sqlite3.Row
        try:
            rows = con.execute(
                """
                SELECT trace_id, prompt_version_hash,
                       COALESCE(reward_value, 0.0) AS reward_value,
                       verifier_output, reviewer_verdict
                  FROM execution_traces
                 WHERE task_class = ?
                   AND prompt_version_hash IS NOT NULL
                   AND prompt_version_hash <> ''
                 ORDER BY created_at DESC
                 LIMIT ?
                """,
                (self.task_class, self.n),
            ).fetchall()
        finally:
            con.close()
        out: list[dict[str, Any]] = []
        for r in rows:
            out.append(
                {
                    "trace_id": r["trace_id"],
                    "prompt_version_hash": r["prompt_version_hash"],
                    "reward_value": float(r["reward_value"]),
                    "verifier_output": _safe_str(r["verifier_output"]),
                    "reviewer_verdict": _safe_str(r["reviewer_verdict"]),
                }
            )
        return out

    def _compute_default_score(self) -> float:
        if not self.full_batch:
            return 0.0
        return sum(r["reward_value"] for r in self.full_batch) / len(self.full_batch)

    def evaluate(
        self, batch: list[Any], candidate: dict[str, str]
    ) -> tuple[list[float], list[Any]]:
        """Score ``candidate`` against each example in ``batch``.

        Match by ``candidate['prompt_version_hash']`` against the cached
        ``reward_value`` rows. Examples whose row doesn't match the
        candidate hash fall back to ``_default_score`` (parent mean) so
        we never fabricate per-example scores.

        Returns ``(per_example_scores, traces)`` where each trace is the
        row dict — the same shape ``reflect_on_component`` consumes via
        ``make_reflective_dataset``.
        """
        cand_hash = candidate.get("prompt_version_hash", "")
        cached = self._score_cache.get(cand_hash, [])
        # Build a per-row fallback: if the row's own hash doesn't match
        # the candidate, use parent mean (cached).
        scores: list[float] = []
        traces: list[Any] = []
        for ex in batch:
            row_hash = ex.get("prompt_version_hash", "") if isinstance(ex, dict) else ""
            row_score = ex.get("reward_value") if isinstance(ex, dict) else None
            if cand_hash and row_hash == cand_hash and row_score is not None:
                scores.append(float(row_score))
            else:
                scores.append(float(self._default_score))
            traces.append(ex)
        # Rank-based assignment: if we have MORE cached examples than
        # the batch asks for (rare path; batch slice is normal), use
        # the first len(batch) cached scores; if we have FEWER, fill the
        # rest with default.
        if cached and len(cached) >= len(batch):
            for i in range(len(batch)):
                scores[i] = float(cached[i])
        return scores, traces

    def make_reflective_dataset(
        self,
        candidate: dict[str, str],
        eval_batch: list[tuple[Any, float, Any]],
    ) -> list[dict[str, Any]]:
        """Turn scored rows into feedback dicts the reflector can act on.

        Each feedback dict carries enough context for ``reflect_on_component``
        to propose a meaningful edit: the candidate hash we attempted, the
        score the example earned, the verifier output snippet that
        characterized it, and the reviewer verdict (if any).
        """
        cand_hash = candidate.get("prompt_version_hash", "")
        out: list[dict[str, Any]] = []
        for ex, score, _ in eval_batch:
            row_hash = ""
            verifier = ""
            reviewer = ""
            if isinstance(ex, dict):
                row_hash = ex.get("prompt_version_hash", "")
                verifier = ex.get("verifier_output", "")
                reviewer = ex.get("reviewer_verdict", "")
            # Snip large verifier strings so the reflect prompt stays
            # bounded — the optimizer never needs the full payload, only
            # signal. 240 chars covers most "verifier: ok / fail / reason"
            # outputs.
            verifier_snip = verifier[:240] if verifier else ""
            reviewer_snip = reviewer[:240] if reviewer else ""
            out.append(
                {
                    "example": {"prompt_version_hash": row_hash},
                    "score": float(score),
                    "candidate_hash": cand_hash,
                    "feedback": {
                        "verifier_output": verifier_snip,
                        "reviewer_verdict": reviewer_snip,
                    },
                }
            )
        return out


def _safe_str(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, (dict, list)):
        try:
            return json.dumps(v)
        except (TypeError, ValueError):
            return str(v)
    return str(v)


def load_recipe_prompt_fields(
    recipe: str,
    *,
    prompts_root: str | os.PathLike[str] | None = None,
) -> dict[str, str]:
    """Load ``recipes/<recipe>/prompts/*.md`` as seed candidate fields.

    Falls back to a single empty ``"instruction"`` field when the recipe
    directory is missing or empty. This is the seed for the optimizer —
    it's the on-disk prompt text the reflect hook is trying to improve,
    so a suggestion is directly comparable to "what's currently shipped".
    """
    if prompts_root is None:
        prompts_root = Path(__file__).resolve().parents[2] / "recipes" / recipe / "prompts"
    else:
        prompts_root = Path(prompts_root)
    if not prompts_root.exists() or not prompts_root.is_dir():
        return {"instruction": f"(missing prompts dir: {prompts_root})"}
    fields: dict[str, str] = {}
    for p in sorted(prompts_root.glob("*.md")):
        fields[p.stem] = p.read_text(encoding="utf-8")
    if not fields:
        return {"instruction": f"(no .md prompts under {prompts_root})"}
    return fields


def run_suggestion(
    *,
    db_path: str | os.PathLike[str],
    task_class: str,
    recipe: str = "",
    budget: int | None = None,
    minibatch: int = 4,
    prompt_version_hash: str | None = None,
    seed_candidate: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Build a suggestion dict the reflect hook can emit.

    Equivalent to ``optimize(seed, adapter, ...)`` but bundled with the
    ``pattern_records`` row shape so the bash hook can persist it
    directly. Pure-Python so a bash side can call this via
    ``python3 -c "import mini_ork.optimize.miniork_adapter as m; ..."``
    without bringing in another dependency.

    Returns a suggestion JSON dict with the shape used by
    ``reflection_suggest_promotions`` consumers:

        {
            "suggested_promotion_type": "prompt_change",
            "pattern_id": "gepa-<recipe>-<ts>",
            "description": "...",
            "evidence_trace_ids": [...],
            "candidate": <best_candidate>,
            "parent_seed": <seed_candidate>,
            "full_eval_count": N,
            "iteration_count": M,
            "validity": "valid" | "insufficient_evidence",
            "task_class": "...",
            "recipe": "...",
        }
    """
    if budget is None:
        budget = int(os.environ.get("MO_OPTIMIZER_BUDGET", "4"))
    budget = max(1, min(int(budget), 8))  # hard cap

    adapter = MiniOrkGepaAdapter(
        db_path=db_path, task_class=task_class, recipe=recipe
    )

    if not adapter.full_batch:
        # No evidence — emit an insufficient_evidence suggestion rather
        # than running a vacuous optimize() and reporting success.
        ts = int(time.time())
        return {
            "suggested_promotion_type": "prompt_change",
            "pattern_id": f"gepa-{recipe or 'unknown'}-{ts}-{uuid.uuid4().hex[:6]}",
            "description": (
                f"insufficient_evidence: no execution_traces rows for "
                f"task_class={task_class} in {db_path}"
            ),
            "evidence_trace_ids": [],
            "candidate": seed_candidate or {},
            "parent_seed": seed_candidate or {},
            "full_eval_count": 0,
            "iteration_count": 0,
            "validity": "insufficient_evidence",
            "task_class": task_class,
            "recipe": recipe,
            # pattern_store-shaped fields (so the bash reflect hook can
            # pattern_store the same dict without reshaping):
            "output_type": "prompt_change",
            "frequency": 1,
        }

    if seed_candidate is None:
        seed_candidate = load_recipe_prompt_fields(recipe)
    if prompt_version_hash is not None:
        seed_candidate = dict(seed_candidate)
        seed_candidate["prompt_version_hash"] = prompt_version_hash

    best, _ = optimize(
        seed_candidate, adapter, minibatch=minibatch, budget=budget
    )
    # Evidence: the trace_ids in full_batch — what the optimizer actually
    # scored against. Cap to the most recent 50 to keep the row bounded.
    evidence = [r["trace_id"] for r in adapter.full_batch[-50:]]
    ts = int(time.time())
    return {
        "suggested_promotion_type": "prompt_change",
        "pattern_id": f"gepa-{recipe or 'unknown'}-{ts}-{uuid.uuid4().hex[:6]}",
        "description": (
            f"gepa suggestion: optimized {len(adapter.full_batch)} "
            f"trace(s) for task_class={task_class}, "
            f"{adapter.iteration_count} iters, "
            f"{adapter.full_eval_count} full eval(s)"
        ),
        "evidence_trace_ids": evidence,
        "candidate": best,
        "parent_seed": seed_candidate,
        "full_eval_count": adapter.full_eval_count,
        "iteration_count": adapter.iteration_count,
        "validity": "valid",
        "task_class": task_class,
        "recipe": recipe,
        # pattern_store-shaped fields (so the bash reflect hook can
        # pattern_store the same dict without reshaping):
        "output_type": "prompt_change",
        "frequency": 1,
    }


__all__ = [
    "MiniOrkGepaAdapter",
    "load_recipe_prompt_fields",
    "run_suggestion",
]
