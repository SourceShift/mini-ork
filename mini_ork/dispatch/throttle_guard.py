"""Provider-throttle classification + per-lane backoff — Python port of
``lib/throttle-guard.sh``.

Faithful port of the bash functions backing the recursive-self-improve outer
loop: classify a provider error log into one of six categories, record the
failure in a per-provider flag file with an exponential backoff, check the
remaining cool-down, clear on success, scan a run dir for failed LLM calls,
escalate to a systemic halt when N+ providers are simultaneously throttled,
and sleep until every named provider's cool-down expires.

The bash source stays in place (strangler-fig co-existence). The Python
functions below are called by Python drivers and exercised by parity tests in
``tests/unit/test_throttle_guard_py.py`` that invoke the live bash subprocess
on identical inputs and assert byte-identical state.
"""
from __future__ import annotations

import os
import re
import sys
import time


BACKOFFS: tuple[int, ...] = (0, 300, 600, 1800, 3600, 3600, 3600)
SYSTEMIC_THRESHOLD: int = 3
SYSTEMIC_WINDOW_S: int = 600
EMPTY_ITER_THRESHOLD: int = 5
MAX_SLEEP_S: int = 1800

_CLASS_PATTERNS: tuple[tuple[str, "re.Pattern[str]"], ...] = (
    ("capacity",
     re.compile(r"Selected model is at capacity|model_overloaded|engine is overloaded")),
    ("throttled",
     re.compile(r"429 Too Many Requests|rate_limit_exceeded|rate limit reached"
                r"|insufficient_quota|Fair Usage Policy|Request rejected \(429\)"
                r"|api_error_status\"?:\s*\"?429")),
    ("overloaded",
     re.compile(r"529 |overloaded_error|Service Unavailable")),
    ("auth_failed",
     re.compile(r"401|authentication_error|invalid_api_key"
                r"|API Key appears to be invalid")),
    ("timed_out",
     re.compile(r"gtimeout|timed out|deadline_exceeded")),
)

_PROVIDER_FROM_FILENAME = re.compile(r"^[0-9]+-(.+)$")


def _state_dir() -> str:
    home = os.environ.get("MINI_ORK_HOME") or os.path.join(os.getcwd(), ".mini-ork")
    return os.path.join(home, "state")


def _now_epoch() -> int:
    return int(time.time())


def _read_int_field(path: str, field: str) -> int:
    if not os.path.isfile(path):
        return 0
    try:
        with open(path, "r", errors="replace") as fh:
            for line in fh:
                if line.startswith(field + "="):
                    raw = line.split("=", 1)[1].strip()
                    try:
                        return int(raw)
                    except ValueError:
                        return 0
    except OSError:
        return 0
    return 0


def classify_error(err_log: str) -> str:
    """Return the throttle classification of the error log at ``err_log``.

    Returns one of ``capacity``, ``throttled``, ``overloaded``, ``auth_failed``,
    ``timed_out``, or ``unknown``. Returns ``unknown`` if the file is missing
    or unreadable. Mirrors ``_throttle_classify_error`` in lib/throttle-guard.sh.
    """
    if not os.path.isfile(err_log):
        return "unknown"
    try:
        with open(err_log, "r", errors="replace") as fh:
            content = fh.read()
    except OSError:
        return "unknown"
    for name, pattern in _CLASS_PATTERNS:
        if pattern.search(content):
            return name
    return "unknown"


def flag_path(provider: str) -> str:
    """Return the flag-file path for ``provider`` (``<state>/throttle-<provider>.flag``)."""
    return os.path.join(_state_dir(), f"throttle-{provider}.flag")


def record_failure(provider: str, classification: str) -> None:
    """Record a classified failure for ``provider`` and emit a stderr announcement.

    Mirrors ``_throttle_record_failure``: bumps the consecutive-failure counter,
    selects a cool-down seconds from the backoff ladder (capacity / throttled /
    overloaded / timed_out), or 0 for auth_failed, or 60 for unknown, then
    writes the four-line flag file. Emits the stderr line::

        [throttle] <provider> classified=<c> consecutive=<n> cool_down_seconds=<s>
    """
    state = _state_dir()
    os.makedirs(state, exist_ok=True)
    flag = flag_path(provider)
    now = _now_epoch()
    prior = _read_int_field(flag, "consecutive_failures")
    prior_failures = prior + 1

    if classification in ("capacity", "throttled", "overloaded", "timed_out"):
        idx = prior_failures
        if idx >= len(BACKOFFS):
            idx = len(BACKOFFS) - 1
        cool_seconds = BACKOFFS[idx]
    elif classification == "auth_failed":
        cool_seconds = 0
    else:
        cool_seconds = 60

    cool_until = now + cool_seconds
    with open(flag, "w") as fh:
        fh.write(f"cool_down_until={cool_until}\n")
        fh.write(f"consecutive_failures={prior_failures}\n")
        fh.write(f"last_error={classification}\n")
        fh.write(f"last_seen={now}\n")

    sys.stderr.write(
        f"  [throttle] {provider} classified={classification} "
        f"consecutive={prior_failures} cool_down_seconds={cool_seconds}\n"
    )


def check_cooldown(provider: str) -> int:
    """Return seconds-until-resume for ``provider`` (0 if the flag is missing
    or the cool-down has already expired). Mirrors ``_throttle_check_cooldown``.
    """
    flag = flag_path(provider)
    if not os.path.isfile(flag):
        return 0
    cool_until = _read_int_field(flag, "cool_down_until")
    now = _now_epoch()
    if cool_until > now:
        return cool_until - now
    return 0


def clear_on_success(provider: str) -> None:
    """Remove the flag file for ``provider`` (idempotent — no error if absent).
    Mirrors ``_throttle_clear_on_success``."""
    flag = flag_path(provider)
    try:
        os.remove(flag)
    except FileNotFoundError:
        pass


def systemic_halt_check() -> bool:
    """Return ``True`` when >= ``MINI_ORK_THROTTLE_SYSTEMIC_THRESHOLD`` (default 3)
    distinct providers have a live cool-down AND were last seen within
    ``MINI_ORK_THROTTLE_SYSTEMIC_WINDOW_S`` (default 600s) of each other.
    Mirrors ``_throttle_systemic_halt_check``.
    """
    state = _state_dir()
    if not os.path.isdir(state):
        return False
    threshold = int(os.environ.get(
        "MINI_ORK_THROTTLE_SYSTEMIC_THRESHOLD", str(SYSTEMIC_THRESHOLD)))
    window = int(os.environ.get(
        "MINI_ORK_THROTTLE_SYSTEMIC_WINDOW_S", str(SYSTEMIC_WINDOW_S)))
    now = _now_epoch()
    count = 0
    for entry in sorted(os.listdir(state)):
        if not entry.startswith("throttle-") or not entry.endswith(".flag"):
            continue
        flag = os.path.join(state, entry)
        if not os.path.isfile(flag):
            continue
        cool_until = _read_int_field(flag, "cool_down_until")
        last_seen = _read_int_field(flag, "last_seen")
        if cool_until > now and (now - last_seen) < window:
            count += 1
    return count >= threshold


def classify_run_failures(run_dir: str) -> None:
    """Scan ``<run_dir>/llm-failures/*.err.log`` and record each failure.

    The provider name is extracted from the file basename ``<ts>-<provider>.err.log``
    (stripping the leading ``<digits>-`` prefix). Files that classify as
    ``unknown`` are skipped (consistent with bash). Mirrors
    ``_throttle_classify_run_failures``.
    """
    failures_dir = os.path.join(run_dir, "llm-failures")
    if not os.path.isdir(failures_dir):
        return
    for entry in sorted(os.listdir(failures_dir)):
        if not entry.endswith(".err.log"):
            continue
        path = os.path.join(failures_dir, entry)
        if not os.path.isfile(path):
            continue
        base = entry[: -len(".err.log")]
        m = _PROVIDER_FROM_FILENAME.match(base)
        if not m:
            continue
        provider = m.group(1)
        if not provider:
            continue
        cls = classify_error(path)
        if cls == "unknown":
            continue
        record_failure(provider, cls)


def wait_for_cooldowns(hard_deadline: int = 0, *providers: str) -> int:
    """Sleep until the longest cool-down across ``providers`` expires (or skip
    the sleep if none are throttled). Returns 0 if all providers cleared, 1 if
    the remaining budget was non-positive after capping.

    The cool-down is capped by both ``hard_deadline`` (when > 0) and
    ``MINI_ORK_THROTTLE_MAX_SLEEP_S`` (default 1800). Emits the stderr
    announcement ``[throttle] sleeping <N>s for provider cool-down to expire``
    before sleeping. Mirrors ``_throttle_wait_for_cooldowns``.
    """
    max_sleep = int(os.environ.get("MINI_ORK_THROTTLE_MAX_SLEEP_S", str(MAX_SLEEP_S)))
    longest = 0
    for p in providers:
        s = check_cooldown(p)
        if s > longest:
            longest = s
    if longest == 0:
        return 0
    if hard_deadline > 0:
        now = _now_epoch()
        budget = hard_deadline - now
        if budget < longest:
            longest = budget
    if longest > max_sleep:
        longest = max_sleep
    if longest <= 0:
        return 1
    sys.stderr.write(
        f"  [throttle] sleeping {longest}s for provider cool-down to expire\n"
    )
    time.sleep(longest)
    return 0


__all__ = [
    "BACKOFFS",
    "SYSTEMIC_THRESHOLD",
    "SYSTEMIC_WINDOW_S",
    "EMPTY_ITER_THRESHOLD",
    "MAX_SLEEP_S",
    "classify_error",
    "flag_path",
    "record_failure",
    "check_cooldown",
    "clear_on_success",
    "systemic_halt_check",
    "classify_run_failures",
    "wait_for_cooldowns",
]