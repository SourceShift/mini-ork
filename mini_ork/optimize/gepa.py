"""GEPA-style reflective prompt optimizer (R4a standalone). Standalone — no
wiring into the GRPO loop, ``lib/reflection_pipeline.sh``, or
``bin/mini-ork-reflect``. R4b will integrate.

Budget semantic: ``budget`` = max OPTIMIZATION ITERATIONS. Each accepted
mutation costs one full eval; rejected mutations cost only the two minibatch
evals. With ``budget=4`` and most mutations rejected you spend ~8 minibatch
evals + 0-4 full evals — the 35x rollout savings claim vs. evaluating every
mutation on the full set.
"""

from __future__ import annotations

import json
import random
from dataclasses import dataclass
from typing import Any, Callable, Protocol, runtime_checkable

from mini_ork.dispatch import DispatchRequest, DispatchResult, dispatch_model


@runtime_checkable
class GepaAdapter(Protocol):
    """User-supplied scorer. ``full_eval_count`` and ``iteration_count`` are
    counters that ``optimize()`` resets at entry and increments during the run
    so callers can assert rollout cost is bounded and the loop halts.
    """

    full_eval_count: int
    iteration_count: int
    full_batch: list[Any]

    def evaluate(
        self, batch: list[Any], candidate: dict[str, str]
    ) -> tuple[list[float], list[Any]]:
        """Score ``candidate`` on ``batch``; return ``(per_example_scores, traces)``."""
        ...

    def make_reflective_dataset(
        self,
        candidate: dict[str, str],
        eval_batch: list[tuple[Any, float, Any]],
    ) -> list[dict[str, Any]]:
        """Turn ``(example, score, trace)`` triples into feedback dicts (opaque)."""
        ...


@dataclass
class _FrontEntry:
    candidate: dict[str, str]
    full_score: float


class ParetoFront:
    """Non-dominated front over ``(candidate, full_score)``. Single-objective
    degenerates to "keep the highest-scoring entry"; data structure is in
    place for R4b multi-objective extension.
    """

    def __init__(self) -> None:
        self._entries: list[_FrontEntry] = []

    def add(self, candidate: dict[str, str], full_score: float) -> None:
        # Drop entries strictly worse than the newcomer.
        self._entries = [
            e for e in self._entries if not (e.full_score < full_score)
        ]
        # If any remaining entry strictly beats the newcomer, it's dominated.
        if any(e.full_score > full_score for e in self._entries):
            return
        # Dedup ties (same score, different candidate).
        self._entries = [
            e for e in self._entries if e.full_score != full_score
        ]
        self._entries.append(_FrontEntry(dict(candidate), full_score))

    def select(self) -> tuple[dict[str, str], float]:
        best = max(self._entries, key=lambda e: e.full_score)
        return dict(best.candidate), best.full_score

    def best(self) -> tuple[dict[str, str], float]:
        return self.select()


class _NoProposal(Exception):
    """Reflection call returned no usable mutation. Loop skips the iteration."""


def _strip_code_fence(raw: str) -> str:
    if not raw.startswith("```"):
        return raw
    parts = raw.split("```")
    if len(parts) < 3:
        return raw
    inner = parts[1]
    if inner.startswith("json"):
        inner = inner[4:]
    return inner.strip()


def reflect_on_component(
    candidate: dict[str, str],
    reflective_dataset: list[dict[str, Any]],
    component_key: str,
    *,
    model: str = "stub",
) -> dict[str, str]:
    """Call ``dispatch_model`` to propose a rewrite of one component.

    Returns a NEW candidate dict (input not mutated). Raises ``_NoProposal``
    on unparseable response or missing target key.
    """
    prompt = json.dumps({
        "candidate": candidate,
        "component_to_rewrite": component_key,
        "feedback": reflective_dataset,
        "instruction": (
            f"Propose a rewrite of the '{component_key}' field that "
            f"addresses the failures above. Respond with JSON only: "
            f'{{"{component_key}": "<new value>"}}'
        ),
    }, indent=2)
    result: DispatchResult = dispatch_model(
        DispatchRequest(model=model, prompt=prompt)
    )
    if not result.ok or not result.text.strip():
        raise _NoProposal(f"dispatch not ok: rc={result.rc} err={result.error!r}")
    try:
        parsed = json.loads(_strip_code_fence(result.text.strip()))
    except json.JSONDecodeError as e:
        raise _NoProposal(f"reflection response not JSON: {e}") from e
    if not isinstance(parsed, dict) or component_key not in parsed:
        raise _NoProposal(f"reflection missing {component_key!r}: {parsed!r}")
    new_value = parsed[component_key]
    if not isinstance(new_value, str):
        new_value = str(new_value)
    mutated = dict(candidate)
    mutated[component_key] = new_value
    return mutated


def optimize(
    seed_candidate: dict[str, str],
    adapter: GepaAdapter,
    *,
    minibatch: int = 8,
    budget: int = 4,
    model: str = "stub",
    rng: random.Random | None = None,
    component_selector: Callable[[dict[str, str]], str] | None = None,
) -> tuple[dict[str, str], list[dict[str, Any]]]:
    """Run the reflective-mutation loop.

    ``budget`` = max iterations. ``adapter.full_eval_count`` is guaranteed
    ``<= budget`` at return because only accepted mutations consume a full
    eval. Returns ``(best_candidate, accepted_mutations_trace)``.
    """
    rng = rng or random.Random()
    component_selector = component_selector or (lambda c: next(iter(c)))

    adapter.full_eval_count = 0
    adapter.iteration_count = 0

    # Seed: full eval so the front has a baseline. NOT counted in
    # ``full_eval_count`` — that counter is "mutation-triggered full evals",
    # the rollout cost that ``budget`` bounds.
    front = ParetoFront()
    seed_scores, _ = adapter.evaluate(adapter.full_batch, seed_candidate)
    seed_full = sum(seed_scores) / max(len(seed_scores), 1)
    front.add(seed_candidate, seed_full)

    accepted: list[dict[str, Any]] = []

    while adapter.iteration_count < budget:
        adapter.iteration_count += 1
        parent, parent_full = front.select()
        component_key = component_selector(parent)

        # Draw minibatch (full batch if k >= len).
        if minibatch >= len(adapter.full_batch):
            minibatch_data = list(adapter.full_batch)
        else:
            minibatch_data = rng.sample(adapter.full_batch, minibatch)

        # Parent minibatch eval — NOT a full eval; rollout stays bounded.
        parent_minibatch_scores, parent_traces = adapter.evaluate(
            minibatch_data, parent
        )
        eval_batch = list(
            zip(minibatch_data, parent_minibatch_scores, parent_traces)
        )

        # Reflect + mutate. Skip iteration on no proposal.
        reflective = adapter.make_reflective_dataset(parent, eval_batch)
        try:
            mutated = reflect_on_component(
                parent, reflective, component_key, model=model
            )
        except _NoProposal:
            continue

        # Mutation minibatch eval. Gate: strict improvement.
        new_scores, _ = adapter.evaluate(minibatch_data, mutated)
        if sum(new_scores) <= sum(parent_minibatch_scores):
            continue  # rejected — no full eval consumed

        # Accepted: full eval + front update.
        new_full_scores, _ = adapter.evaluate(adapter.full_batch, mutated)
        new_full = sum(new_full_scores) / max(len(new_full_scores), 1)
        front.add(mutated, new_full)
        adapter.full_eval_count += 1
        accepted.append({
            "iteration": len(accepted),
            "component": component_key,
            "parent_minibatch_score": sum(parent_minibatch_scores),
            "new_minibatch_score": sum(new_scores),
            "parent_full_score": parent_full,
            "new_full_score": new_full,
        })

    best_candidate, _ = front.best()
    return best_candidate, accepted