"""Crucible — mini-ork's verified-execution runtime seam.

Crucible is the layer where a candidate change is put under fire: executed in an
isolated runtime and judged by what the *code actually did*, not by what a model
said about it.

It is a thin seam over the MIT-licensed `verifiers` engine (Prime Intellect),
which supplies the runtime primitives (Docker/sandbox execution, environments,
trace DAGs). mini-ork supplies what sits above:

  * the delivery loop (plan -> execute -> verify -> publish),
  * the EXECUTION-ANCHORED reward (a judge-approve on a failing test scores 0.0),
  * multi-node prompt evolution over {planner, implementer, reviewer},
  * per-customer verified-outcome memory.

Why the seam exists (and why we do not fork): `verifiers` is developed daily and
its runtime is better than anything we would hand-roll. We depend on it, pin it,
and keep our value where it actually is -- the loop and the verified outcomes on
a customer's private repo. See THIRD_PARTY.md for attribution.

Deliberate divergence: `verifiers` scores with rubric/judge Criteria. Crucible
does NOT. Verdicts here are anchored on execution status; a judge may only veto,
never approve (see mini_ork.ported.mini_ork_execute.reward_from_status, PR #168).
"""
from mini_ork.runtime.engine import Crucible, ExecOutcome, RuntimeSpec

__all__ = ["Crucible", "ExecOutcome", "RuntimeSpec"]
