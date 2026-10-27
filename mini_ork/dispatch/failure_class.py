"""Typed dispatch failure classes + ONE action table.

The retry loop, the fallback walk and the lane fuse used to each read the raw
error text differently: ``classify_error`` (a regex chain over the text),
``dispatch_with_fallback`` (walk on ANY failure) and the fuse (its own category
vocabulary). 2,675 failed ``llm_calls`` rows, 1,881 of them ``unknown`` — a
recognisable timeout or spawn failure that bought neither a retry nor a
diagnosis — were the price.

This module is the single place that turns an error text into

* a **failure class** (the typed vocabulary the operator reads, stored in
  ``llm_calls.metadata_json.failure_class``; the column's CHECK constraint is
  unchanged), and
* an **Action**: whether the outer loop may re-run the same chain, whether the
  fallback walk tries the next lane, whether the walk stops, and how long a
  provider asked us to wait.

``classify_error`` stays the regex source of truth for the legacy categories it
already recognises — this module wraps it rather than forking it. The import is
function-local because ``llm_dispatch`` imports this module at top level and
``classify_error`` is not defined yet when that import is being resolved.
"""
from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta

CLASSES = ("transient", "rate_limit", "credential", "quota", "model_access",
           "caller_fault", "timeout", "environment", "shape", "unknown")

# Classes whose failure is worth re-running the SAME chain for. Everything else
# moves on (or halts) instead of burning the same wall-clock again.
RETRYABLE_CLASSES = frozenset({"transient", "rate_limit", "shape"})

# The only classes a stdout tail may PROMOTE an otherwise-unknown failure to.
# These are phrases a provider emits in a structured rejection envelope, so a
# tail carrying one is provider output rather than model prose. Everything else
# stays ``unknown``: RB-10 is model prose saying "401"/"forbidden" fusing a
# healthy lane for the run, and ``credential`` is fuse-terminal.
TAIL_PROMOTABLE = frozenset({"quota", "model_access", "rate_limit"})


@dataclass(frozen=True)
class Action:
    retry_chain: bool      # the outer loop may re-run the chain (with backoff)
    next_lane: bool        # the fallback walk continues to the next lane
    stop_walk: bool        # the walk stops here: no other lane can help
    wait_hint_s: float | None


# ── text rules (checked against the FULL text, case-insensitively) ──

_TIMEOUT_RE = re.compile(r"timeout after \d|timed out after")
_ENVIRONMENT_RE = re.compile(
    r"spawn failed|dispatcher crashed:\s*\[errno 2\]|likely cwd-confusion"
    r"|refusing codex dispatch")
# The environment faults that are run-wide: every lane spawns in the same cwd,
# so walking on spends a timeout per lane to learn the same fact. Everything
# else in ``environment`` (a missing CLI binary, a missing key, a lane absent
# from providers.yaml) belongs to one lane, and the next lane may well work.
_RUN_WIDE_RE = re.compile(
    r"cwd does not exist|likely cwd-confusion|refusing codex dispatch")
_SHAPE_RE = re.compile(r"failed shape check|shape reject")
# The "… does not exist" phrase is bounded to ONE line and 120 chars: over a
# multi-line error text, a bare ``[\s\S]*`` let the word "model" on one line and
# "does not exist" on a later line (a *file* or *path*) classify as
# ``model_access`` — a 1-strike, fuse-terminal mistake. The ``(?!s\b)`` guard
# additionally keeps a plural path fragment ("…/models.json does not exist")
# from being read as a model name.
_MODEL_ACCESS_RE = re.compile(
    r"model_not_found|model is not supported"
    r"|(model|deployment)(?!s\b)[^\n]{0,120}does not exist"
    r"|do(es)? not have access to (the )?model")

_RETRY_AFTER_RE = re.compile(r"retry-after[:=\s]+(\d+)", re.IGNORECASE)
_TRY_AGAIN_IN_RE = re.compile(
    r"try again in\s+(\d+)\s*(seconds?|secs?|minutes?|mins?|s|m)\b",
    re.IGNORECASE)
_TRY_AGAIN_AT_RE = re.compile(
    r"try again at\s+(\d{1,2}):(\d{2})\s*(am|pm)", re.IGNORECASE)


def wait_hint(text: str, *, now: datetime | None = None) -> float | None:
    """Seconds a provider asked the caller to wait, or ``None``.

    Understands the three wordings actually emitted by the lanes:
    ``Retry-After: 30``, ``try again in 2 minutes`` and ``try again at 10:50
    AM``. The dated Codex wording (``try again at Nov 8th, 2026 10:19 AM``) is
    deliberately NOT parsed — no clock arithmetic can safely guess its date.
    """
    body = text or ""
    match = _RETRY_AFTER_RE.search(body)
    if match:
        return float(match.group(1))
    match = _TRY_AGAIN_IN_RE.search(body)
    if match:
        amount = int(match.group(1))
        unit = match.group(2).lower()
        return float(amount * 60 if unit.startswith("m") else amount)
    match = _TRY_AGAIN_AT_RE.search(body)
    if match:
        base = now or datetime.now()
        hour = int(match.group(1)) % 12
        if match.group(3).lower() == "pm":
            hour += 12
        target = base.replace(hour=hour, minute=int(match.group(2)),
                              second=0, microsecond=0)
        if target <= base:
            target += timedelta(days=1)
        return (target - base).total_seconds()
    return None


def classify(text: str, rc: int | str = "") -> tuple[str, str]:
    """``(failure_class, db_category)`` for an error ``text`` and exit code.

    ``db_category`` is always one of the ten words the ``llm_calls`` CHECK
    constraint allows; the typed class only ever travels in ``metadata_json``.
    The first matching rule wins (a timeout outranks a spawn failure it also
    mentions). Anything the rules do not recognise is handed to the existing
    ``classify_error`` and mapped onto the typed vocabulary.
    """
    body = text or ""
    low = body.lower()
    rc_s = str(rc).strip()

    if rc_s == "124" or _TIMEOUT_RE.search(low):
        return ("timeout", "network")
    if rc_s == "127" or _ENVIRONMENT_RE.search(low):
        return ("environment", "config")
    if _SHAPE_RE.search(low):
        return ("shape", "request")
    if _MODEL_ACCESS_RE.search(low):
        return ("model_access", "config")
    if "empty output" in low and rc_s == "0":
        return ("transient", "provider")

    # Function-local: the module must be importable on its own (llm_dispatch
    # imports it at top level, before classify_error is defined).
    from mini_ork.dispatch.llm_dispatch import classify_error

    category = classify_error(body, rc)
    if category == "quota":
        return ("quota", "quota")
    if category == "auth":
        return ("credential", "auth")
    if category == "capacity":
        if "429" in low and "rate" in low:
            return ("rate_limit", "capacity")
        return ("transient", "capacity")
    if category in ("network", "stream", "provider"):
        return ("transient", category)
    if category in ("request", "safety"):
        return ("caller_fault", category)
    if category == "config":
        return ("environment", "config")
    return ("unknown", "unknown")


def retryable(failure_class_name: str) -> int:
    """1 when the class is worth retrying the same chain, else 0."""
    return 1 if failure_class_name in RETRYABLE_CLASSES else 0


def action(failure_class_name: str, text: str = "",
           env: Mapping[str, str] | None = None) -> Action:
    """The one behaviour table for a failure class."""
    if failure_class_name == "transient":
        return Action(True, True, False, wait_hint(text))
    if failure_class_name == "rate_limit":
        return Action(True, True, False, wait_hint(text))
    if failure_class_name == "timeout":
        return Action(False, True, False, None)
    if failure_class_name in ("credential", "quota", "model_access"):
        return Action(False, True, False, None)
    if failure_class_name == "environment":
        # A missing cwd or a confused target affects every lane alike: stop.
        # A missing binary or key affects this lane only: walk on.
        if _RUN_WIDE_RE.search((text or "").lower()):
            return Action(False, False, True, None)
        return Action(False, True, False, None)
    if failure_class_name == "shape":
        return Action(True, True, False, None)
    if failure_class_name == "caller_fault":
        # "prompt too long" on one lane can fit another, so the default walks
        # on; an operator who knows the fault is chain-wide can stop the walk.
        lookup = os.environ if env is None else env
        if lookup.get("MO_FALLBACK_STOP_ON_CALLER_FAULT") == "1":
            return Action(False, False, True, None)
        return Action(False, True, False, None)
    return Action(False, True, False, None)
