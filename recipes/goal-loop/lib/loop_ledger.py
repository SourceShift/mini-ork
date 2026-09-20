"""Append-only decision ledger for the goal-loop's outer driver.

``goal-loop-state.json`` records the loop's OUTCOME per wave (the failing set,
the wave signature, the headroom it closed). It does not record the loop's
DECISION: which signals it held when it chose, what it chose, and what the
chosen action was allowed to do. Those are different questions, and only the
second one is learnable — an outcome without its decision context is a label
with no features.

This module owns that second record. One JSON object per line at
``<state_dir>/decisions.jsonl``, appended and never rewritten:

    context   every signal the driver held at decision time — the freshly
              probed failing set, the quarantine set, spend and projection,
              the evidence fingerprints, the detector flags.
    action    what it did with them — kind, recipe, units, and whether the
              action was destructive.
    outcome   what came back — the child's self-verdict, the diff size, and
              the two measures of whether the predicate actually moved.
    shield    the assurance verdict for that action (see ``assurance.py``).

Append-only is the point. The run directory is reused across waves and its
artifacts (``sweep-plan.json``, ``sweep-result.json``, ``goal-state.json``)
are overwritten every wave, so any history kept there is lost by construction.
A ledger that is only ever opened in ``"a"`` mode cannot lose a row to a later
wave, and a crashed write costs at most the trailing partial line — which
``read_decisions`` drops rather than failing the whole read.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

LEDGER_FILENAME = "decisions.jsonl"


def ledger_path(state_dir: str | Path) -> Path:
    """Return the absolute path to the ledger for ``state_dir``."""
    return Path(state_dir) / LEDGER_FILENAME


def append_decision(
    state_dir: str | Path,
    record: dict[str, Any],
    *,
    ts: int | None = None,
) -> Path:
    """Append one decision as a single JSON line; return the path written.

    ``ts`` is stamped here rather than by the caller so every row carries a
    comparable wall-clock even when a caller forgets, and so a caller that
    wants a deterministic timestamp (a test) can still supply one.
    """
    target = ledger_path(state_dir)
    target.parent.mkdir(parents=True, exist_ok=True)
    row: dict[str, Any] = {"ts": int(time.time() if ts is None else ts)}
    row.update(record)
    line = json.dumps(row, sort_keys=True, separators=(",", ":"))
    with open(target, "a", encoding="utf-8") as handle:
        handle.write(line + "\n")
        handle.flush()
    return target


def read_decisions(state_dir: str | Path) -> list[dict[str, Any]]:
    """Read every record back, in append order.

    A missing ledger is an empty history, not an error — the driver starts
    wave 1 with nothing recorded. A malformed or truncated line is skipped
    rather than raised: the ledger is a log, and a log that cannot be read
    because of its last line is worse than one that returns the rest.
    """
    path = ledger_path(state_dir)
    if not path.is_file():
        return []
    out: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict):
            out.append(row)
    return out
