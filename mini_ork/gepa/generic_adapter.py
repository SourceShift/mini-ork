"""A GEPA adapter that optimizes an ARBITRARY prompt against ANY ``RunBackend``.

``MiniOrkGEPAAdapter`` (sibling module) is the recipe/DB specialization: it runs
real mini-ork recipes and scores from ``execution_traces``. This adapter removes
those assumptions — it composes a :class:`~mini_ork.gepa.backends.RunBackend`, so
the "how do I run + score a candidate" concern is pluggable:

    GEPARunBackendAdapter(SubprocessRunBackend("node dist/eval.js"))  # external critic
    GEPARunBackendAdapter(CallbackRunBackend(my_python_fn))           # in-process

The candidate is ``{component_name: prompt_text}`` (usually a single component,
e.g. ``{"prompt": "..."}``). Each trainset item is an opaque ``task`` handed to
the backend verbatim. Scoring and the reflective feedback come entirely from the
backend, so GEPA's reflection LM sees the critic's own natural-language
diagnosis unmodified — which is exactly the signal GEPA turns into targeted
prompt mutations.
"""
from __future__ import annotations

from typing import Any

try:
    from gepa.core.adapter import EvaluationBatch, GEPAAdapter
except ImportError as exc:  # pragma: no cover
    raise ImportError("GEPA not installed. Run: pip install gepa") from exc

from mini_ork.gepa.backends import ExternalTrace, RunBackend


class GEPARunBackendAdapter(GEPAAdapter[Any, ExternalTrace, ExternalTrace]):
    """Compose any ``RunBackend`` into GEPA's adapter interface.

    ``components`` optionally restricts which candidate keys are optimizable
    (defaults to every key in the seed candidate at first evaluate()).
    """

    def __init__(self, backend: RunBackend, components: list[str] | None = None):
        self.backend = backend
        self.components = components

    # ---- GEPA interface -------------------------------------------------

    def evaluate(
        self, batch: list[Any], candidate: dict[str, str], capture_traces: bool = False
    ) -> EvaluationBatch[ExternalTrace, ExternalTrace]:
        outputs: list[ExternalTrace] = []
        scores: list[float] = []
        traces: list[ExternalTrace] | None = [] if capture_traces else None
        for task in batch:
            trace = self.backend.run(candidate, task)
            score = self.backend.score(trace)
            outputs.append(trace)
            scores.append(score)
            if traces is not None:
                traces.append(trace)
        return EvaluationBatch(outputs=outputs, scores=scores, trajectories=traces)

    def make_reflective_dataset(
        self,
        candidate: dict[str, str],
        eval_batch: EvaluationBatch[ExternalTrace, ExternalTrace],
        components_to_update: list[str],
    ) -> dict[str, list[dict[str, Any]]]:
        """Forward the backend's NL feedback verbatim into the reflection prompt.

        The critic's own diagnosis is the highest-signal input GEPA has; we do
        not summarize or reshape it.
        """
        dataset: dict[str, list[dict[str, Any]]] = {c: [] for c in components_to_update}
        for i, trace in enumerate(eval_batch.trajectories or []):
            score = eval_batch.scores[i]
            feedback = self.backend.feedback(trace, score)
            record = {
                "Inputs": {"candidate_components": sorted(candidate)},
                "Generated Outputs": {
                    "score": score,
                    "subscores": trace.subscores or {},
                },
                "Feedback": feedback,
            }
            for comp in components_to_update:
                dataset[comp].append(record)
        return dataset
