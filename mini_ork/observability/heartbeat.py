"""Periodic node heartbeats — the only sign of life between start and end.

A node is a *blocking in-process* call (verifiers park in ``run_check``'s Popen,
agent lanes park in ``dispatch.core``), so for the whole window that matters
nothing else in the run process is awake to say it is still alive. This module
is that ticker, and it is deliberately dumb: one ``node_heartbeat`` row per
interval, no model call, no file writes, no reads.

Three readers already consume the row this writes and currently starve, because
nothing emitted it:

* ``run_reaper._last_activity`` — ``MAX(last_heartbeat_at)``, the reaper's
  newest sign of life (``mini_ork/orchestration/run_reaper.py``);
* the run card's ``beat`` (``mini_ork/ide_pages/runs.py``), rendered as "current
  node, last seen";
* ``fleet``'s active-run filter (``mini_ork/web/routes/fleet.py``).

It is also the only signal a *hung* node produces, which is what makes a stall
observable at all now that the run buddy is gone.

``mo_node_emit`` writes ``last_heartbeat_at`` only for ``node_start`` and
``node_heartbeat`` (migration 0023), so this is exactly the emitter to use. The
node's own handle is passed through rather than re-resolved from the
environment: ``os.environ`` is not a reliable carrier mid-run under env
isolation, and a heartbeat that cannot find the DB is worse than none at all.
"""
from __future__ import annotations

import os
import sys
import threading
from typing import Callable

from mini_ork.observability.node_events import mo_node_emit

__all__ = ["Heartbeat", "heartbeat_seconds"]

# One INSERT per interval is the entire cost. 60s keeps a six-hour stale window
# (``mini-ork reap --stale-after 6h``) covered with room to spare without
# touching the DB often enough to contend with the node's own writer.
_DEFAULT_INTERVAL_S = 60.0


def heartbeat_seconds() -> float:
    """The tick interval, from ``MO_NODE_HEARTBEAT_SECONDS``.

    A missing, unparseable or non-positive value falls back to
    :data:`_DEFAULT_INTERVAL_S` rather than disabling the heartbeat — an
    interval nobody can accidentally set to zero.
    """
    try:
        value = float(os.environ.get("MO_NODE_HEARTBEAT_SECONDS", ""))
    except (TypeError, ValueError):
        return _DEFAULT_INTERVAL_S
    return value if value > 0 else _DEFAULT_INTERVAL_S


class Heartbeat:
    """One daemon thread emitting ``node_heartbeat`` for one node.

    Owns no state the run depends on: a failed tick, a dropped thread or a
    locked DB changes nothing for the run. ``sleep`` is injectable so tests can
    drive :meth:`tick` directly with no thread at all.
    """

    def __init__(
        self,
        run_id: str,
        node_id: str,
        node_type: str = "",
        *,
        db: str | None = None,
        interval_s: float | None = None,
        sleep: Callable[[float], bool] | None = None,
    ) -> None:
        self.run_id = run_id
        self.node_id = node_id
        self.node_type = node_type
        self._db = db
        # ``sleep`` returns False when the wait was cut short (the stop event).
        self._sleep = sleep or self._event_sleep
        self._interval = interval_s if interval_s and interval_s > 0 else heartbeat_seconds()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def _event_sleep(self, seconds: float) -> bool:
        return not self._stop.wait(seconds)

    def tick(self) -> None:
        """Emit one heartbeat. Public so tests drive it directly, with no thread."""
        try:
            mo_node_emit(self.run_id, self.node_id, self.node_type,
                         "node_heartbeat", db=self._db)
        except Exception as exc:  # noqa: BLE001 — observability must never fail a node
            print(f"  [heartbeat] {self.node_id}: {exc}", file=sys.stderr)

    def _loop(self) -> None:
        while self._sleep(self._interval):
            self.tick()

    def start(self) -> None:
        """Begin ticking. Idempotent."""
        if self._thread is not None:
            return
        try:
            self._thread = threading.Thread(
                target=self._loop, name=f"node-heartbeat-{self.node_id}", daemon=True)
            self._thread.start()
        except RuntimeError:  # pragma: no cover - can only fail with no threads left
            self._thread = None

    def stop(self) -> None:
        """Stop ticking and wait briefly for the thread to notice."""
        self._stop.set()
        thread, self._thread = self._thread, None
        if thread is not None and thread.is_alive():
            thread.join(timeout=2.0)
