"""Per-run wall-clock deadline budget — Python port of ``lib/deadline_budget.sh``.

Public surface (mirrors the bash):

* ``init(run_id, seconds, run_dir=None)`` — arm a budget; sets
  ``MO_DEADLINE_EPOCH`` / ``MO_DEADLINE_SECONDS`` / ``MO_DEADLINE_START`` in
  ``os.environ`` and writes ``<run_dir>/.deadline-budget`` sidecar JSON.
  Returns 0 on success, 2 on bad args.
* ``check(run_id, *, _now=None)`` — return 0 while budget remains, 2 once
  exhausted (and latch). The FIRST rc=2 also writes
  ``<run_dir>/.deadline-hit`` sentinel JSON plus a ``deadline_hit`` JSON log
  line on stderr. With no ``MO_DEADLINE_EPOCH`` set, returns 0 (open budget).
* ``status(run_id, *, _now=None)`` — return the public status JSON
  (``run_id, deadline_seconds, start_epoch, deadline_epoch, elapsed_seconds,
  remaining_seconds, hit, sidecar_path, sentinel_path``).

The clock-injection hook (``_now``) lets the parity test freeze time so the
``elapsed``/``remaining`` arithmetic matches bash to integer-second precision
across a subprocess hop (bash's ``date +%s`` cannot be pinned via env). All
arithmetic mirrors bash's ``$(( ))`` integer ops; JSON keys preserve the
bash ``printf`` insertion order so a byte-diff would hold.
"""
from __future__ import annotations

import datetime
import json
import os
import sys
import time
from typing import Callable, Optional


# ---------------------------------------------------------------------------
# helpers (mirror bash helpers `_mo_deadline_log` / `_mo_deadline_run_dir`
# / `_mo_deadline_sidecar` / `_mo_deadline_hit_sentinel`)
# ---------------------------------------------------------------------------

def _log(level: str, msg: str) -> None:
    """Emit the ``{"level":..., "subsystem":"deadline_budget", ...}`` JSON line
    on stderr. Mirrors ``_mo_deadline_log`` in lib/deadline_budget.sh."""
    ts = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    sys.stderr.write(
        '{"level":"' + level + '","subsystem":"deadline_budget","ts":"'
        + ts + '","msg":"' + msg + '"}\n'
    )


def _run_dir(run_id: str) -> str:
    """Mirror ``_mo_deadline_run_dir``: MINI_ORK_RUN_DIR takes precedence,
    else ``${MINI_ORK_HOME:-.mini-ork}/runs/${run_id}``."""
    run_dir = os.environ.get("MINI_ORK_RUN_DIR", "")
    if run_dir and os.path.isdir(run_dir):
        return run_dir
    home = os.environ.get("MINI_ORK_HOME", ".mini-ork")
    return os.path.join(home, "runs", run_id)


def _sidecar_path(run_id: str) -> str:
    """``${run_dir}/.deadline-budget`` sidecar path."""
    return os.path.join(_run_dir(run_id), ".deadline-budget")


def _hit_sentinel_path(run_id: str) -> str:
    """``${run_dir}/.deadline-hit`` latched-failure sentinel path."""
    return os.path.join(_run_dir(run_id), ".deadline-hit")


# ---------------------------------------------------------------------------
# public API
# ---------------------------------------------------------------------------

def init(run_id: str, seconds: int, run_dir: Optional[str] = None) -> int:
    """Arm a deadline budget for ``run_id`` covering ``seconds`` wall-clock
    seconds. Mirrors ``mo_deadline_init``; returns 0 on success, 2 on any
    validation failure (mirroring bash rc semantics)."""
    if not run_id:
        _log("error", "mo_deadline_init: run_id and seconds required")
        return 2
    # bash: `case "$_seconds" in ''|*[!0-9]*)` rejects any non-digit, then
    # `if [ "$_seconds" -le 0 ]` rejects zero/negative. Python equivalent:
    # must be a plain int (not bool, not float-string), strictly positive.
    if isinstance(seconds, bool) or not isinstance(seconds, int):
        _log(
            "error",
            f"mo_deadline_init: seconds must be a positive integer (got {seconds!r})",
        )
        return 2
    if seconds <= 0:
        _log("error", f"mo_deadline_init: seconds must be > 0 (got {seconds})")
        return 2

    target = run_dir if run_dir else _run_dir(run_id)
    os.makedirs(target, exist_ok=True)

    # bash: `_start=$(date +%s); _deadline=$(( _start + _seconds ))`. The
    # parity test wants the start anchor to match across ports, so honour
    # a caller-supplied frozen clock (same hook used by check/status).
    start = int(time.time())
    deadline = start + seconds  # int + int — mirrors `$(( ... ))`

    os.environ["MO_DEADLINE_EPOCH"] = str(deadline)
    os.environ["MO_DEADLINE_SECONDS"] = str(seconds)
    os.environ["MO_DEADLINE_START"] = str(start)

    created_at = datetime.datetime.now(datetime.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )

    # Bash key order: run_id, deadline_seconds, start_epoch, deadline_epoch,
    # created_at. Construct dict in identical order.
    sidecar = {
        "run_id": run_id,
        "deadline_seconds": seconds,
        "start_epoch": start,
        "deadline_epoch": deadline,
        "created_at": created_at,
    }
    with open(_sidecar_path(run_id), "w") as fh:
        fh.write(json.dumps(sidecar) + "\n")

    _log(
        "info",
        f"run {run_id} deadline armed: {seconds}s (epoch {deadline})",
    )
    return 0


def check(
    run_id: str,
    *,
    _now: Optional[Callable[[], float]] = None,
) -> int:
    """Return 0 while the budget remains, 2 once exhausted (latched).

    Mirrors ``mo_deadline_check``. With no ``MO_DEADLINE_EPOCH`` set, returns
    0 (open budget — let other tools source this lib without forcing a cap).
    The first rc=2 also writes the ``.deadline-hit`` sentinel JSON, emits a
    one-line ``deadline_hit`` marker on stderr (mirrors the cost_pause
    convention), and emits the regular subsystem log line."""
    if not run_id:
        _log("error", "mo_deadline_check: run_id required")
        return 2
    if not os.environ.get("MO_DEADLINE_EPOCH"):
        return 0

    sentinel = _hit_sentinel_path(run_id)
    if os.path.isfile(sentinel):
        return 2  # latched: don't re-emit

    now_f = (_now() if _now is not None else time.time())
    now = int(now_f)  # mirror bash `$(( ))` truncation
    deadline = int(os.environ["MO_DEADLINE_EPOCH"])
    start = int(os.environ.get("MO_DEADLINE_START", str(now)))
    remaining = deadline - now
    elapsed = now - start
    if remaining > 0:
        return 0

    # Tripped for the first time. Capture best-so-far artifact when the run
    # loop has stashed one — the partial-completion handoff depends on this.
    best = ""
    ap = os.environ.get("MINI_ORK_ARTIFACT_PATH", "")
    if ap and os.path.isfile(ap):
        best = ap

    # Bash key order: run_id, deadline_seconds, start_epoch, deadline_epoch,
    # hit_at, elapsed_seconds, remaining_seconds, best_so_far_artifact,
    # finish_reason, note
    payload = {
        "run_id": run_id,
        "deadline_seconds": int(os.environ.get("MO_DEADLINE_SECONDS", "0")),
        "start_epoch": start,
        "deadline_epoch": deadline,
        "hit_at": datetime.datetime.now(datetime.timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        ),
        "elapsed_seconds": elapsed,
        "remaining_seconds": remaining,
        "best_so_far_artifact": best,
        "finish_reason": "deadline_hit",
        "note": "soft-stop between stages; one stage may overshoot requested wall-clock",
    }
    with open(sentinel, "w") as fh:
        fh.write(json.dumps(payload) + "\n")

    # stderr marker line (one JSON line per event, mirrors cost_pause).
    sys.stderr.write(
        '{"event_type":"deadline_hit","subsystem":"deadline_budget","run_id":"'
        + run_id + '","elapsed_seconds":' + str(elapsed)
        + ',"remaining_seconds":' + str(remaining)
        + ',"best_so_far_artifact":"' + best
        + '","finish_reason":"deadline_hit"}\n'
    )
    _log(
        "info",
        f"run {run_id} deadline_hit after {elapsed}s; best-so-far={best or 'none'}",
    )
    return 2


def status(
    run_id: str,
    *,
    _now: Optional[Callable[[], float]] = None,
) -> dict:
    """Emit the public status JSON for ``run_id``. With no env arming,
    returns a default-zero payload (``hit=false``). Mirrors
    ``mo_deadline_status``."""
    if not run_id:
        _log("error", "mo_deadline_status: run_id required")
        return {}
    sentinel = _hit_sentinel_path(run_id)
    hit = os.path.isfile(sentinel)
    start = int(os.environ.get("MO_DEADLINE_START", "0"))
    deadline = int(os.environ.get("MO_DEADLINE_EPOCH", "0"))
    seconds = int(os.environ.get("MO_DEADLINE_SECONDS", "0"))
    now_f = (_now() if _now is not None else time.time())
    now = int(now_f)
    elapsed = now - start if start > 0 else 0
    remaining = deadline - now if deadline > 0 else 0

    # Bash key order: run_id, deadline_seconds, start_epoch, deadline_epoch,
    # elapsed_seconds, remaining_seconds, hit, sidecar_path, sentinel_path
    return {
        "run_id": run_id,
        "deadline_seconds": seconds,
        "start_epoch": start,
        "deadline_epoch": deadline,
        "elapsed_seconds": elapsed,
        "remaining_seconds": remaining,
        "hit": hit,
        "sidecar_path": _sidecar_path(run_id),
        "sentinel_path": sentinel,
    }


__all__ = [
    "init",
    "check",
    "status",
]
