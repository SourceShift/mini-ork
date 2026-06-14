"""Typed contracts for policy callables.

Mirrors the shape of ``omnigent/policies/schema.py`` (Apache 2.0) so
operators familiar with Omnigent's policy authoring can port logic
without learning a new vocabulary. The event taxonomy is trimmed to
events mini-ork actually emits; the response codes are identical to
Omnigent's.

Defines the shapes of the ``event`` dict passed TO a policy callable
and the ``response`` dict returned FROM it. These are ``TypedDict``
definitions — they are not enforced at runtime; the actual coercion
lives in :func:`mini_ork.policies.engine._coerce_to_policy_result`,
but they serve as the authoritative reference for authors implementing
policy callables.

Usage in a policy callable::

    from mini_ork.policies.schema import PolicyEvent, PolicyResponse

    def block_git_push_after_npm_install(event: PolicyEvent) -> PolicyResponse | None:
        if event["type"] != "tool_call":
            return None  # abstain
        if event["data"].get("tool") != "git_push":
            return None
        return {
            "result": "REQUIRE_APPROVAL",
            "reason": "git push requires approval after npm install",
            "policy_name": "block_git_push_after_npm_install",
        }
"""

from __future__ import annotations

from typing import Any, Literal, Protocol, TypedDict


PolicyEventType = Literal[
    "tool_call",          # agent is about to invoke a tool
    "network_request",    # agent is about to make an outbound HTTP call
    "file_write",         # agent is about to write to a file
    "cost_threshold",     # cumulative cost crossed a threshold
    "verifier_result",    # a verifier emitted pass/fail
]


PolicyResult = Literal[
    "ALLOW",              # pass through (default)
    "DENY",               # block this action
    "REQUIRE_APPROVAL",   # write a sentinel; wait for operator approval
    "LOG_ONLY",           # let it through but record an audit row
]


class PolicyEvent(TypedDict, total=False):
    """Event passed to every registered policy callable.

    Total=False so policies can read whatever fields they care about
    without forcing every dispatcher to populate the whole shape. The
    canonical fields are documented here; consumers may add their own.
    """

    type: PolicyEventType
    data: dict[str, Any]
    session_id: str
    run_id: str
    timestamp: int  # unix seconds


class PolicyResponse(TypedDict, total=False):
    """Response returned by a policy callable, or None to abstain.

    A policy that returns ``None`` is treated as abstaining; the next
    registered policy is consulted. The first non-``None`` response
    wins. If every policy abstains, the engine returns the default
    ALLOW response.
    """

    result: PolicyResult
    reason: str
    policy_name: str
    # Optional advisory fields read by specific callers.
    require_approval_message: str | None
    log_payload: dict[str, Any] | None


class PolicyCallable(Protocol):
    """Protocol for policy functions.

    Two shapes are accepted: a single-argument callable that only
    consults the event, or a two-argument callable that also consults
    operator-provided config (e.g. thresholds from agents.yaml).
    """

    def __call__(
        self, event: PolicyEvent, config: dict[str, Any] | None = None
    ) -> PolicyResponse | None: ...
