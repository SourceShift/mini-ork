"""Default model dispatch for the certify oracle.

The oracle is repo-agnostic; model invocation is intentionally narrow so the
test surface stays small and sidecar files never leak into the long-running
verifier process. Anything more elaborate (lane routing, telemetry) lives in
`mini_ork.dispatch` and is reached via the in-process entrypoint here.

`mo_llm_dispatch` reports spend only through its `.cost` sidecar (the
`llm_calls` row is written by the CLI wrapper, which this in-process path does
not go through), so the sidecar is read here and summed. A certificate reports
what the verdict cost from `spent()`, never from a table this path never wrote.
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
import time

from mini_ork.context import scoped_environ
from mini_ork.dispatch.llm_dispatch import mo_llm_dispatch

# The code-writing lane: probes and invariants are pytest code, so the default is a
# code lane, not an analysis lane. Override per install with MO_CERTIFY_MODEL.
DEFAULT_MODEL = "minimax"

# Per-call bounds. The dispatcher's defaults (25 min, 60 turns) are sized for an
# implementer node; an oracle call writes one pytest block and must not pin a
# verify loop for half an hour when a gateway stalls. Measured 2026-10-25 on the
# certify demo: the PROVEN path made six sequential lane calls, one of which
# took 6m43s on a lane that answers the same prompt in 4 s when it does not
# stall, so `mini-ork certify` looked hung. Override with MO_CERTIFY_LANE_TIMEOUT_S,
# MO_CERTIFY_LANE_MAX_TURNS and MO_CERTIFY_LANE_RETRIES; a present-but-malformed
# value fails closed (no dispatch, the oracle abstains), like the other knobs.
DEFAULT_TIMEOUT_S = 180
DEFAULT_MAX_TURNS = 8
DEFAULT_RETRIES = 1

_spend = {"usd": 0.0, "calls": 0}


def spent() -> dict:
    """Cumulative spend of every `default_dispatch` call in this process."""
    return dict(_spend)


def reset_spend() -> None:
    _spend["usd"], _spend["calls"] = 0.0, 0


def _knob(name: str, default: int, *, allow_zero: bool = False) -> int:
    """Integer env knob. Unset/blank → default; present but malformed → ValueError."""
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = int(float(raw.strip()))
    except ValueError:
        raise ValueError(f"{name}={raw!r} is not a number") from None
    if value < 0 or (value == 0 and not allow_zero):
        raise ValueError(f"{name}={raw!r} must be {'>= 0' if allow_zero else '> 0'}")
    return value


def _note(msg: str) -> None:
    """One progress line per lane call, on a terminal only (or MO_CERTIFY_PROGRESS=1).

    `mini-ork certify` prints nothing until the verdict, so a slow lane is
    indistinguishable from a hang without this. Never on a non-TTY by default:
    tests and CI capture stderr and must not see it."""
    if os.environ.get("MO_CERTIFY_PROGRESS", "") == "1" or getattr(sys.stderr, "isatty", lambda: False)():
        try:
            sys.stderr.write(f"[certify] {msg}\n")
            sys.stderr.flush()
        except (OSError, ValueError):
            pass


def _call(model: str, prompt: str, timeout_s: int, max_turns: int) -> tuple[str, int]:
    """One bounded lane call. Returns (text, rc); text is "" unless rc == 0."""
    fd, out_file = tempfile.mkstemp(prefix="certify-oracle-", suffix=".out")
    # Agentic lanes have file tools and run in MO_TARGET_CWD (else the process cwd).
    # Measured: while writing a probe, a lane recreated the package under test in the
    # caller's cwd to try its test. `certify` runs inside the user's own repo, so every
    # call gets a throwaway directory — the model can scribble; the repo stays untouched.
    scratch = tempfile.mkdtemp(prefix="certify-lane-")
    try:
        os.close(fd)
        with scoped_environ({"MO_TARGET_CWD": scratch}):
            rc = mo_llm_dispatch(model, prompt, out_file, timeout_s=timeout_s, max_turns=max_turns)
        _spend["calls"] += 1
        try:
            with open(out_file + ".cost", encoding="utf-8") as f:
                _spend["usd"] += float(f.read().strip() or 0)
        except (OSError, ValueError):
            pass
        if rc != 0:
            return "", rc
        try:
            with open(out_file, encoding="utf-8") as f:
                return f.read(), 0
        except OSError:
            return "", 1
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
        for suffix in ("", ".cost", ".model", ".tokens", ".err.log"):
            try:
                os.remove(out_file + suffix)
            except OSError:
                pass


def default_dispatch(prompt: str) -> str:
    """Invoke the worker lane and return the text body. Empty string on failure.

    The oracle already treats empty output as "could not build a probe" →
    UNVERIFIED (see `oracle.judge`). Swallowing exceptions here is the contract:
    a dispatch crash must not propagate and break a long-running verify loop.

    Every call is bounded (DEFAULT_TIMEOUT_S / DEFAULT_MAX_TURNS) and a failed or
    timed-out call is retried DEFAULT_RETRIES times: gateway stalls are transient,
    and the same prompt usually answers in seconds on the next attempt.
    """
    model = os.environ.get("MO_CERTIFY_MODEL", DEFAULT_MODEL)
    try:
        timeout_s = _knob("MO_CERTIFY_LANE_TIMEOUT_S", DEFAULT_TIMEOUT_S)
        max_turns = _knob("MO_CERTIFY_LANE_MAX_TURNS", DEFAULT_MAX_TURNS)
        retries = _knob("MO_CERTIFY_LANE_RETRIES", DEFAULT_RETRIES, allow_zero=True)
    except ValueError as e:
        _note(f"{e}; abstaining")
        return ""
    attempts = 1 + retries
    for attempt in range(1, attempts + 1):
        started = time.monotonic()
        try:
            text, rc = _call(model, prompt, timeout_s, max_turns)
        except Exception as e:  # the contract: never propagate into the verify loop
            text, rc = "", -1
            _note(f"lane {model} crashed: {type(e).__name__}: {e}")
        elapsed = time.monotonic() - started
        if rc == 0:
            _note(f"lane {model} call {_spend['calls']} ok in {elapsed:.0f}s")
            return text
        tail = f"retrying ({attempt}/{attempts})" if attempt < attempts else "giving up; the oracle abstains"
        _note(f"lane {model} call {_spend['calls']} failed (rc={rc}) after {elapsed:.0f}s of {timeout_s}s; {tail}")
    return ""


__all__ = ["DEFAULT_MAX_TURNS", "DEFAULT_MODEL", "DEFAULT_RETRIES", "DEFAULT_TIMEOUT_S",
           "default_dispatch", "reset_spend", "spent"]
