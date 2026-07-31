"""Reward write-back for the learning loop (P3).

:func:`verdict_reward` maps a verdict status to a scalar reward:

- ``PROVEN`` → ``1.0``
- ``REFUTED`` → ``0.0``
- ``UNVERIFIED`` → ``None``

The ``None`` is load-bearing: abstention supplies no training target.
Mapping it to ``0.0`` would teach the learning loop that an unreachable
surface is a refutation; mapping it to ``1.0`` would recreate false
completion.

:func:`record_reward` appends one compact JSON line to
``${MINI_ORK_HOME}/verify_rewards.jsonl`` (path is overridable for tests).
The parent directory is created on first append; write failures are *not*
silently swallowed — a missing learning record is part of the contract.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from mini_ork.verify.behavioral import (
    PROVEN,
    REFUTED,
    UNVERIFIED,
)

__all__ = ["verdict_reward", "record_reward", "default_reward_path"]


_VALID_STATUSES = (PROVEN, REFUTED, UNVERIFIED)


def verdict_reward(status: str) -> float | None:
    """Map a verdict status to a scalar reward.

    ``PROVEN`` → ``1.0``, ``REFUTED`` → ``0.0``, ``UNVERIFIED`` → ``None``.
    Unknown statuses raise :class:`ValueError`.
    """
    if status == PROVEN:
        return 1.0
    if status == REFUTED:
        return 0.0
    if status == UNVERIFIED:
        return None
    raise ValueError(f"unknown verdict status: {status!r}")


def default_reward_path() -> Path:
    """Resolve the default JSONL path from ``${MINI_ORK_HOME}``.

    Falls back to ``~/.mini-ork/verify_rewards.jsonl`` when ``MINI_ORK_HOME``
    is unset (matches the rest of the runtime's home-resolution convention).
    """
    home = os.environ.get("MINI_ORK_HOME") or os.path.expanduser("~/.mini-ork")
    return Path(home) / "verify_rewards.jsonl"


def _iso_ts(ts: str | datetime | None) -> str:
    """Coerce ``ts`` to an ISO-8601 UTC string with a ``Z`` suffix.

    ``None`` defaults to "now". ``datetime`` values are converted to UTC.
    Already-formatted strings pass through (caller-owned).
    """
    if ts is None:
        return datetime.now(timezone.utc).isoformat(timespec="seconds").replace(
            "+00:00", "Z"
        )
    if isinstance(ts, datetime):
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return ts.astimezone(timezone.utc).isoformat(timespec="seconds").replace(
            "+00:00", "Z"
        )
    return str(ts)


def record_reward(
    run_id: str,
    surface: str,
    target: str,
    status: str,
    ts: str | datetime | None = None,
    path: str | os.PathLike[str] | None = None,
) -> float | None:
    """Append one reward row to the JSONL sink and return the mapped reward.

    Row keys (verbatim, per the prior-art lens): ``run_id``, ``surface``,
    ``target``, ``status``, ``reward``, ``ts``. The parent directory is
    created on first append. Write errors propagate to the caller — silent
    swallowing would lose learning records, which the learning-loop
    contract depends on.
    """
    if status not in _VALID_STATUSES:
        raise ValueError(f"unknown verdict status: {status!r}")
    if not run_id:
        raise ValueError("run_id must be a non-empty string")

    reward = verdict_reward(status)

    row: dict[str, Any] = {
        "run_id": run_id,
        "surface": surface,
        "target": target,
        "status": status,
        "reward": reward,
        "ts": _iso_ts(ts),
    }

    sink = Path(path) if path is not None else default_reward_path()
    sink.parent.mkdir(parents=True, exist_ok=True)
    with open(sink, "a", encoding="utf-8") as f:
        f.write(json.dumps(row, separators=(",", ":")) + "\n")

    return reward