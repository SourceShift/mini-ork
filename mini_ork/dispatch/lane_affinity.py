"""Run-local lane affinity: a node's later rounds start on the lane that served it.

Every revise round and every ``recover`` re-enters :func:`llm_dispatch`, which
re-runs routing and starts the chain from its lead again. When the lead is the
same lane as in the round that first succeeded, that means a fallback lane
which actually served the round (because the lead timed out) is walked past on
the next round — and provider prompt caches are per lane, so the work starts
cold. Affinity remembers the serving lane of a node's first successful round
and moves it to the front of the chain on later rounds.

The operator always wins: an entry is only honoured while the chain's lead is
unchanged, so an operator remap, ``recover --lane`` or an agents.yaml edit
silently disables affinity for that node. Affinity only reorders lanes already
in the chain, and the lane fuse still runs afterwards, so it can never add a
lane or resurrect a fused one.

State lives in ``<run_dir>/.lane-affinity.json``::

    {node_id: {"lead": <chain[0] when it first succeeded>,
               "served": <lane>, "at": <epoch>}}

A missing or corrupt file reads as ``{}``. Writes are atomic. Every function
swallows ``OSError`` / ``ValueError`` — affinity is an optimisation and must
never change a dispatch outcome.

stdlib only; no ``mini_ork`` imports.
"""
from __future__ import annotations

import json
import os
import tempfile
import time

AFFINITY_REL = ".lane-affinity.json"


def enabled(env=None) -> bool:
    """True unless ``MO_LANE_AFFINITY`` is exactly ``"0"`` (default on)."""
    source = os.environ if env is None else env
    value = source.get("MO_LANE_AFFINITY", "1")
    return str("" if value is None else value).strip() != "0"


def _path(run_dir: str) -> str:
    return os.path.join(str(run_dir), AFFINITY_REL)


def _load(run_dir: str) -> dict:
    """Read the affinity map; missing/unparseable/wrong-shaped reads as ``{}``."""
    try:
        with open(_path(run_dir), encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _store(run_dir: str, data: dict) -> None:
    """Atomically replace the affinity file (unique tmp + ``os.replace``).

    ``mkstemp`` gives each concurrent writer (lens nodes dispatch in parallel
    against one run dir) its own temp name, so two writers can never interleave
    content or race a shared fixed-name temp. The read-modify-write can still
    lose one node's entry; that is benign — the node simply records again on
    its next success, because it has no entry.
    """
    fd = None
    tmp = None
    try:
        fd, tmp = tempfile.mkstemp(
            dir=str(run_dir), prefix=".lane-affinity.", suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fd = None
            json.dump(data, fh)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, _path(run_dir))
        tmp = None
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        if tmp is not None:
            try:
                os.unlink(tmp)
            except OSError:
                pass


def preferred(run_dir: str, node_id: str, lead: str) -> str | None:
    """The lane to put first, or None when there is no usable affinity.

    ``served`` is returned only when an entry exists for ``node_id`` AND that
    entry was recorded for the same ``lead``. A different lead means someone
    changed the routing choice — affinity does nothing and the operator's lane
    stays first.
    """
    try:
        entry = _load(str(run_dir)).get(str(node_id))
        if not isinstance(entry, dict):
            return None
        if entry.get("lead") != lead:
            return None
        served = entry.get("served")
        if isinstance(served, str) and served:
            return served
        return None
    except (OSError, ValueError):
        return None


def record(run_dir: str, node_id: str, lead: str, served: str) -> None:
    """Record the first successful round's serving lane for ``node_id``.

    Only the FIRST success defines the affinity: a node that already has an
    entry keeps it, so later rounds cannot rewrite the lane that warmed the
    node's cache. The lead is stored alongside it so a remap can invalidate it.
    """
    try:
        data = _load(str(run_dir))
        node = str(node_id)
        if isinstance(data.get(node), dict):
            return
        data[node] = {"lead": lead, "served": served, "at": time.time()}
        _store(str(run_dir), data)
    except (OSError, ValueError):
        pass


def apply(chain: list[str], lane: str | None) -> list[str]:
    """Move ``lane`` to index 0, keeping the relative order of the rest.

    Returns a NEW list. When ``lane`` is None or not in the chain the chain is
    returned unchanged — affinity never adds a lane the routing did not choose.
    """
    lanes = list(chain)
    if lane is None or lane not in lanes:
        return lanes
    lanes.remove(lane)
    return [lane, *lanes]
