"""Layer 2 of the truth-grounded eval stack — metamorphic / gold-free testing.

Where Layer 0 asks "did the given tests pass?", Layer 2 asks "does the patched
code hold invariances the given tests never checked?" — catching the failure a
single extensional test misses: a patch that passes the exact test inputs but is
wrong on nearby inputs (overfitting to the test / coverage-gap gaming). No
ground-truth label is needed: a metamorphic relation is a property that must
hold *between* related executions of the same program.

The verdict is ALWAYS execution-grounded. A relation is violated iff running it
produces a concrete counterexample, so an LLM that merely *proposes* relations
can never corrupt the verdict — a bad proposal simply fails to reproduce a real
violation. This is what keeps Layer 2 truth-grounded while escaping the
"no oracle" wall.

Grounding: arXiv 2603.24774 (LLM proposes metamorphic relations, execution is
the oracle), arXiv 2510.26423 (execution-grounded oracle synthesis); and this
repo's own finding that a single extensional test is hackable through coverage
gaps while metamorphic relations over amplified inputs are not.

Pure engine — no IO, no dispatch. Callers (a recipe verifier, an offline grader,
or an LLM-proposer wrapper) supply the function under test, seed inputs, and
relations; this module runs them and reports an execution-grounded verdict whose
``to_verifier_json()`` drops straight into Layer 0's execution reward (it is read
as one more ``verifier_*.json`` pass signal).
"""

from __future__ import annotations

import copy
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any


class _Raised:
    """Sentinel wrapping an exception raised by the function under test, so a
    relation can compare outcomes uniformly: two calls that both raise the same
    exception type are 'equal', which is the correct metamorphic outcome for
    determinism (a function that deterministically errors is still deterministic).
    """

    __slots__ = ("kind",)

    def __init__(self, exc: BaseException):
        self.kind = type(exc).__name__

    def __eq__(self, other):
        return isinstance(other, _Raised) and other.kind == self.kind

    def __repr__(self):
        return f"<raised {self.kind}>"


def _safe_call(fn: Callable, x: Any) -> Any:
    """Call fn(x) tolerantly. A single-argument call by default; if x is a tuple
    tagged via ``spread=True`` semantics the caller pre-arranges it. Returns the
    result or a ``_Raised`` sentinel — never propagates, so one bad input can't
    sink the whole metamorphic run."""
    try:
        return fn(x)
    except Exception as exc:  # noqa: BLE001 — the function under test is untrusted
        return _Raised(exc)


@dataclass(frozen=True)
class MetamorphicRelation:
    """One invariance between a source execution and a follow-up execution.

    ``transform`` maps a source input to a follow-up input; ``relation`` returns
    True iff the source/follow-up outputs satisfy the invariance. Example
    (commutativity): transform swaps a 2-tuple, relation is equality.
    """

    name: str
    transform: Callable[[Any], Any]
    relation: Callable[[Any, Any], bool]
    description: str = ""


@dataclass
class MetamorphicResult:
    checks: int = 0
    violations: int = 0
    per_relation: dict = field(default_factory=dict)
    counterexamples: list = field(default_factory=list)
    skipped: int = 0

    @property
    def pass_fraction(self) -> float:
        """Fraction of relation checks that held. 1.0 when nothing was checked
        (vacuous → the caller decides whether that counts; the verifier envelope
        marks it vacuous so Layer 0 excludes it rather than crediting a pass)."""
        return 1.0 if self.checks == 0 else (self.checks - self.violations) / self.checks

    @property
    def passed(self) -> bool:
        return self.violations == 0

    def to_verifier_json(self) -> dict:
        """Layer-0-compatible envelope. When no relation ever ran, emit a
        ``vacuous`` verdict (not ``pass``) so ``execution_reward`` excludes it
        instead of crediting a free pass — a metamorphic check that examined
        nothing must never inflate the reward."""
        if self.checks == 0:
            return {"verdict": "vacuous", "checks": 0,
                    "note": "no metamorphic relation ran"}
        return {
            "pass": self.passed,
            "pass_fraction": round(self.pass_fraction, 4),
            "checks": self.checks,
            "violations": self.violations,
            "skipped": self.skipped,
            "per_relation": self.per_relation,
            "counterexamples": self.counterexamples[:20],  # bound the payload
        }


def check(
    fn: Callable,
    seed_inputs: Iterable[Any],
    relations: Sequence[MetamorphicRelation],
    *,
    check_immutability: bool = True,
    max_counterexamples: int = 50,
) -> MetamorphicResult:
    """Run every relation over every seed input against ``fn`` and report an
    execution-grounded metamorphic verdict.

    Each seed input is deep-copied before use so a mutating function cannot
    corrupt later checks. When ``check_immutability`` is set, a function that
    mutates its own input argument is itself a violation (a universal
    correctness property, gold-free) recorded under the pseudo-relation
    ``input_immutability``.
    """
    res = MetamorphicResult()
    for x in seed_inputs:
        snapshot = copy.deepcopy(x)
        # Pass the ORIGINAL x (not a copy) so a mutating function reveals itself
        # against the pristine snapshot; relations below run off the snapshot.
        base_out = _safe_call(fn, x)

        if check_immutability:
            res.checks += 1
            _bump(res.per_relation, "input_immutability")
            if _differs(x, snapshot):
                res.violations += 1
                res.per_relation["input_immutability"]["violations"] += 1
                if len(res.counterexamples) < max_counterexamples:
                    res.counterexamples.append(
                        {"relation": "input_immutability", "input": repr(snapshot)[:200]})

        for rel in relations:
            try:
                follow_in = rel.transform(copy.deepcopy(snapshot))
            except Exception:  # noqa: BLE001 — a bad transform is a bad proposal, not a code bug
                res.skipped += 1
                continue
            follow_out = _safe_call(fn, follow_in)
            res.checks += 1
            _bump(res.per_relation, rel.name)
            try:
                held = bool(rel.relation(base_out, follow_out))
            except Exception:  # noqa: BLE001 — a relation that errors can't certify anything
                res.skipped += 1
                res.checks -= 1
                res.per_relation[rel.name]["checks"] -= 1
                continue
            if not held:
                res.violations += 1
                res.per_relation[rel.name]["violations"] += 1
                if len(res.counterexamples) < max_counterexamples:
                    res.counterexamples.append({
                        "relation": rel.name,
                        "source_input": repr(snapshot)[:200],
                        "follow_input": repr(follow_in)[:200],
                        "source_output": repr(base_out)[:200],
                        "follow_output": repr(follow_out)[:200],
                    })
    return res


def _bump(per_relation: dict, name: str) -> None:
    slot = per_relation.setdefault(name, {"checks": 0, "violations": 0})
    slot["checks"] += 1


def _differs(a: Any, b: Any) -> bool:
    try:
        return a != b
    except Exception:  # noqa: BLE001 — uncomparable → treat as unchanged (can't prove mutation)
        return False


# ── Universal, gold-free relations (safe built-ins) ──────────────────────────
def determinism() -> MetamorphicRelation:
    """fn(x) called again on the same input must return the same output. Catches
    hidden state: mutable default args, module globals, RNG without a seed,
    dict/set-ordering leaks. Universal — needs no spec."""
    return MetamorphicRelation(
        "determinism", lambda x: x, lambda a, b: a == b,
        "same input twice yields the same output")


def commutativity() -> MetamorphicRelation:
    """For a function whose input is a 2-element sequence, swapping the operands
    must not change the output. Applies only where commutativity is expected;
    supply it via the relation set for such functions."""
    return MetamorphicRelation(
        "commutativity", lambda x: type(x)((x[1], x[0])) if _is_pair(x) else x,
        lambda a, b: a == b, "f(a, b) == f(b, a)")


def _is_pair(x: Any) -> bool:
    return isinstance(x, (tuple, list)) and len(x) == 2


def negation_invariance() -> MetamorphicRelation:
    """f(-x) == f(x) — for even / magnitude functions (abs, x², distance).
    Applies only where expected; supply it via the relation set."""
    return MetamorphicRelation(
        "negation_invariance",
        lambda x: -x if isinstance(x, (int, float)) and not isinstance(x, bool) else x,
        lambda a, b: a == b, "f(-x) == f(x)")


def order_invariance() -> MetamorphicRelation:
    """f(reversed(seq)) == f(seq) — for order-independent aggregates (sum, max,
    membership). Applies only where expected."""
    return MetamorphicRelation(
        "order_invariance",
        lambda x: type(x)(reversed(x)) if isinstance(x, (list, tuple)) else x,
        lambda a, b: a == b, "f(reversed(x)) == f(x)")


UNIVERSAL_RELATIONS: tuple[MetamorphicRelation, ...] = (determinism(),)
"""Relations that hold for ANY correct pure function and need no task spec.
Immutability is enforced separately via ``check(..., check_immutability=True)``."""


# ── Vetted relation library — the safety boundary for auto-proposal ──────────
# An LLM proposer may only SELECT relations from this whitelist by NAME (data),
# never author executable predicates. resolve_relations drops unknown names, so
# no model-supplied code ever runs — only these audited, deterministic relations.
RELATION_LIBRARY: dict = {
    "determinism": determinism,
    "commutativity": commutativity,
    "negation_invariance": negation_invariance,
    "order_invariance": order_invariance,
}


def resolve_relations(names) -> list:
    """Map relation NAMES to MetamorphicRelation objects from the vetted library.
    Unknown names are silently dropped — this is the boundary that keeps
    LLM-proposed relations to a safe whitelist (no arbitrary code)."""
    out = []
    for n in names or []:
        factory = RELATION_LIBRARY.get(n)
        if factory is not None:
            out.append(factory())
    return out


def library_catalog() -> list:
    """[{name, description}] for every library relation — fed to the proposer
    prompt so the LLM knows what it may select."""
    return [{"name": name, "description": factory().description}
            for name, factory in RELATION_LIBRARY.items()]
