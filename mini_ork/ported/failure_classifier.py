"""Failure-class state machine for durable-DAG recovery (E3).

Design source: ``internal-docs/architecture/2026-07-15-durable-dag-resume-design.md`` §5.

Every stopped node attempt is classified into exactly one of five classes.
The class decides recovery policy — crucially, whether the whole run is marked
failed and whether an automatic (unattended) recovery is allowed:

    class            leaves recovery record?   auto-recover?   marks run failed?
    ---------------  ------------------------   -------------   -----------------
    infra_interrupt  yes                        YES (re-run)    no
    provider_limit   yes                        no (explicit)   no
    output_invalid   yes                        no (repair)     no
    input_required   yes                        no (human)      no
    terminal         no                         no              YES

The load-bearing invariants (design §5, kickoff acceptance):

  * A ``max-turns`` stop is ``provider_limit`` — NOT ``infra_interrupt`` — so
    it never triggers an unbounded auto-retry loop. Any new LLM attempt for a
    ``provider_limit``/``output_invalid`` stop is explicit and budget-bounded
    (see ``lease.can_dispatch``).
  * ONLY ``terminal`` marks the run failed. The other four leave a durable
    recovery record so an operator (or the auto-policy, for infra) can resume.
  * Unknown/unrecognized stops default to ``terminal`` — fail-closed. The
    node's E1 checkpoints still persist, so a terminal run remains *manually*
    recoverable via the E2 planner; it simply won't auto-recover or burn spend
    on a stop we could not classify.

This module is pure (no I/O) so it is trivially testable and callable from
both the checkpoint writer and the recovery planner.
"""
from __future__ import annotations

from typing import Optional

__all__ = [
    "INFRA_INTERRUPT",
    "PROVIDER_LIMIT",
    "OUTPUT_INVALID",
    "INPUT_REQUIRED",
    "TERMINAL",
    "ALL_CLASSES",
    "classify",
    "recovery_policy",
    "is_terminal",
    "leaves_recovery_record",
    "marks_run_failed",
    "auto_recoverable",
]

INFRA_INTERRUPT = "infra_interrupt"
PROVIDER_LIMIT = "provider_limit"
OUTPUT_INVALID = "output_invalid"
INPUT_REQUIRED = "input_required"
TERMINAL = "terminal"

ALL_CLASSES = (INFRA_INTERRUPT, PROVIDER_LIMIT, OUTPUT_INVALID, INPUT_REQUIRED, TERMINAL)

# POSIX signal exit codes (128 + signal) that mean "the process was killed by
# infrastructure", not "the model produced a bad answer".
_INFRA_EXIT_CODES = {
    137,  # 128+9  SIGKILL (OOM-killer, docker kill, colima OOM)
    143,  # 128+15 SIGTERM (scheduler reap, sandbox teardown)
    129,  # 128+1  SIGHUP  (terminal/ssh dropped)
    124,  # GNU timeout(1) killed the command
}

# Substring probes (lowercased). Order matters: the classify() function checks
# the most specific/dangerous conditions first so an ambiguous message lands on
# the safer class.
_MAX_TURNS = ("max-turns", "max_turns", "maxturns", "turn limit", "max turns", "turn budget exhausted")
_PROVIDER_LIMIT = (
    "rate limit", "rate-limit", "429", "quota", "fair usage", "fair-usage",
    "overloaded", "capacity", "529", "too many requests", "insufficient_quota",
    "context length", "context_length", "maximum context",
)
_INFRA = (
    "oom", "out of memory", "killed", "sigkill", "sigterm", "timed out", "timeout",
    "connection reset", "connection refused", "broken pipe", "network", "econnreset",
    "sandbox", "worker died", "container", "socket", "temporarily unavailable", "eof",
)
_OUTPUT_INVALID = (
    "json", "parse", "unparseable", "unparsable", "schema", "malformed", "invalid output",
    "empty output", "no output", "could not decode", "expecting value", "invalid json",
    "tool call", "did not return",
)
_INPUT_REQUIRED = (
    "needs_answers", "needs answers", "input required", "input_required", "awaiting input",
    "profile gate", "profile-gate", "clarif", "needs input", "user input", "blocked on question",
)
_TERMINAL = (
    "terminal", "unrecoverable", "fatal", "permanently", "giving up", "non-retryable",
    "not recoverable", "hard fail", "poisoned", "corrupt state",
)


def _hit(text: str, needles) -> bool:
    return any(n in text for n in needles)


def classify(
    *,
    reason: str = "",
    exit_code: Optional[int] = None,
    signal: Optional[int] = None,
    stderr: str = "",
    max_turns_hit: bool = False,
    provider_status: Optional[int] = None,
) -> str:
    """Classify a stopped attempt into one of ``ALL_CLASSES``.

    Inputs are all optional signals; pass whatever the runtime observed:
      * ``reason``          — a short label the runtime attaches to the stop.
      * ``exit_code``       — process exit code (137/143/… → infra).
      * ``signal``          — POSIX signal number if killed by one (9/15 → infra).
      * ``stderr``          — captured stderr tail (substring-probed).
      * ``max_turns_hit``   — the agent stopped because it hit its turn cap.
      * ``provider_status`` — HTTP status from the provider (429/529 → limit).

    Precedence (first match wins): explicit-terminal → input-required →
    max-turns/provider-limit → output-invalid → infra-interrupt →
    default terminal. Terminal is checked first (an explicitly terminal stop
    must never be downgraded to auto-recoverable) and used as the fail-closed
    default (an unclassifiable stop must never auto-retry).
    """
    text = f"{reason}\n{stderr}".lower()

    # 1. explicit terminal wins outright — never auto-recover something the
    #    runtime already declared unrecoverable.
    if _hit(text, _TERMINAL):
        return TERMINAL

    # 2. a node waiting on human/planner input is not a failure to retry.
    if _hit(text, _INPUT_REQUIRED):
        return INPUT_REQUIRED

    # 3. max-turns is a provider/budget limit, NOT an infra blip. This is the
    #    load-bearing rule: it must not become an unbounded auto-retry.
    if max_turns_hit or _hit(text, _MAX_TURNS):
        return PROVIDER_LIMIT
    if provider_status in (429, 529) or _hit(text, _PROVIDER_LIMIT):
        return PROVIDER_LIMIT

    # 4. a malformed/empty model answer → repair (explicit LLM re-attempt).
    if _hit(text, _OUTPUT_INVALID):
        return OUTPUT_INVALID

    # 5. killed-by-infra signals / messages → auto-recoverable re-run.
    if signal in (9, 15, 1) or exit_code in _INFRA_EXIT_CODES or _hit(text, _INFRA):
        return INFRA_INTERRUPT

    # 6. fail-closed default: anything we can't classify is terminal. The run's
    #    E1 checkpoints persist, so an operator can still manually recover.
    return TERMINAL


def recovery_policy(failure_class: str) -> dict:
    """Policy for a class. Keys:
      * ``auto_recoverable``  — the auto-policy may re-run WITHOUT operator/LLM
                                (only ``infra_interrupt``).
      * ``needs_llm``         — a recovery consumes a fresh LLM attempt (must be
                                explicit + budget-bounded).
      * ``marks_run_failed``  — this class fails the whole run (only ``terminal``).
      * ``leaves_record``     — leaves a durable ``recovery_requests`` record.
    """
    table = {
        INFRA_INTERRUPT: dict(auto_recoverable=True, needs_llm=False, marks_run_failed=False, leaves_record=True),
        PROVIDER_LIMIT:  dict(auto_recoverable=False, needs_llm=True, marks_run_failed=False, leaves_record=True),
        OUTPUT_INVALID:  dict(auto_recoverable=False, needs_llm=True, marks_run_failed=False, leaves_record=True),
        INPUT_REQUIRED:  dict(auto_recoverable=False, needs_llm=False, marks_run_failed=False, leaves_record=True),
        TERMINAL:        dict(auto_recoverable=False, needs_llm=False, marks_run_failed=True, leaves_record=False),
    }
    # Unknown class → treat as terminal (fail-closed), same default as classify.
    return table.get(failure_class, table[TERMINAL])


def is_terminal(failure_class: str) -> bool:
    return recovery_policy(failure_class)["marks_run_failed"]


def marks_run_failed(failure_class: str) -> bool:
    return recovery_policy(failure_class)["marks_run_failed"]


def leaves_recovery_record(failure_class: str) -> bool:
    return recovery_policy(failure_class)["leaves_record"]


def auto_recoverable(failure_class: str) -> bool:
    return recovery_policy(failure_class)["auto_recoverable"]
