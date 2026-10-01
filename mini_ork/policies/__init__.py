"""Stateful policy engine for mini-ork dispatch decisions.

Adopts the TypedDict contract shape from Omnigent's
``omnigent/policies/schema.py`` (Apache-2.0). The
engine is Python (not bash) because three panel lenses independently
flagged shell as the wrong substrate for stateful guardrails.
"""

from mini_ork.policies.schema import (
    PolicyCallable,
    PolicyEvent,
    PolicyResponse,
)
from mini_ork.policies.engine import (
    evaluate_policies,
    record_decision,
    register_policy,
)

__all__ = [
    "PolicyCallable",
    "PolicyEvent",
    "PolicyResponse",
    "evaluate_policies",
    "record_decision",
    "register_policy",
]
