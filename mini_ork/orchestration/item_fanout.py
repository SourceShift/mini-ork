"""Shared, checkpointed item fan-out — one bounded pool over a data-driven work list.

Four recipes grew their own copy of this loop independently
(``recipes/epic-runner/lib/epic_dispatcher.py``,
``recipes/doc-to-features-loop/lib/per_feature_dispatcher.py``,
``recipes/goal-loop/lib/drive.py``, ``recipes/bdd-first-delivery/lib/dispatch.sh``).
All four share two defects this module fixes:

* **No worker pool.** ``goal-loop``'s ``sweep_run`` is a plain ``for entry in
  plan:`` loop, and each ``spawn()`` blocks for the child's whole lifetime, so
  the pool never exceeds 1 no matter what ``MINI_ORK_RECURSIVE_MAX_PARALLEL``
  says. The work list scales; the worker pool does not.
* **No per-item checkpoint.** A sweep node holds every item in one node's
  lifetime, so any failure — or the revise loop's retry edge — re-runs the
  whole list. On a 61-row audit that is 61x the spend to fix one row.

This is a pure library: it never dispatches a model, never opens the state DB,
and never writes outside ``results_dir``. The caller supplies ``worker``, so
the same module drives ``spawn()`` children, subprocesses, or an in-process
fake in tests. Item state lives in JSON, never in a DB row — many concurrent
writers on one ``state.db`` serialize on a single write lock and now hard-fail
since ``execute.set_status`` raises instead of warning (b79263d9).
"""
from __future__ import annotations

import json
import re
import time
from collections.abc import Callable
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

# Outcome vocabulary. ``deferred`` is deliberately distinct from ``failed``: a
# deferred item was never attempted (budget or capacity), so a resume may run it.
STATUS_OK = "ok"
STATUS_FAILED = "failed"
STATUS_TIMEOUT = "timeout"
STATUS_CACHED = "skipped_cached"
STATUS_DEFERRED = "deferred"

Worker = Callable[[dict[str, Any]], dict[str, Any]]

_SLUG_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")


def item_id(item: dict[str, Any]) -> str:
    """The item's stable identity, from ``id`` or ``item_id``.

    Raises rather than inventing one: a synthesized id would make the per-item
    checkpoint collide across items and silently mark work as already done.
    """
    for key in ("id", "item_id"):
        value = item.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()
    raise ValueError(f"item has no 'id' or 'item_id': {item!r}")


def _slug(item_id_value: str) -> str:
    """Filesystem-safe form of an item id.

    Item ids are often relative file paths, so a raw f-string filename would
    create nested dirs or (for ``a/../b``) escape ``results_dir`` entirely.
    """
    slug = _SLUG_UNSAFE.sub("-", item_id_value).strip("-")
    return slug or "item"


def result_path(results_dir: str | Path, item_id_value: str) -> Path:
    """``<results_dir>/<slug>.json`` — the per-item checkpoint."""
    return Path(results_dir) / f"{_slug(item_id_value)}.json"


@dataclass
class ItemOutcome:
    """One item's terminal state. ``error`` is kept separate from ``status`` so
    a caller can distinguish "ran and failed" from "never ran"."""

    item_id: str
    status: str
    result_path: str = ""
    error: str = ""
    duration_s: float = 0.0
    cost_usd: float = 0.0

    @property
    def ran(self) -> bool:
        """True only for items the worker actually executed."""
        return self.status in (STATUS_OK, STATUS_FAILED, STATUS_TIMEOUT)


@dataclass
class FanoutResult:
    """The manifest. ``items`` preserves input order for a stable diff."""

    items: list[ItemOutcome] = field(default_factory=list)
    spent_usd: float = 0.0
    budget_usd: float | None = None
    max_workers: int = 1

    def count(self, status: str) -> int:
        return sum(1 for outcome in self.items if outcome.status == status)

    @property
    def ok(self) -> bool:
        """True when nothing ran and failed. Deferred items are not failures —
        they were never attempted, and the quarantine/retry logic owns them."""
        return self.count(STATUS_FAILED) == 0 and self.count(STATUS_TIMEOUT) == 0

    @property
    def verdict(self) -> str:
        """``pass`` only when every item was actually accounted for.

        A run where every item was deferred is NOT a pass: reporting success
        while nothing ran is the vacuous-verdict failure the probe-validity
        rules (``mini_ork.verify.probe_validity``) exist to catch.
        """
        if self.count(STATUS_FAILED) or self.count(STATUS_TIMEOUT):
            return "fail"
        if self.count(STATUS_DEFERRED) and not self.count(STATUS_OK):
            return "inconclusive"
        return "pass"

    def to_dict(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict,
            "total": len(self.items),
            "ok": self.count(STATUS_OK),
            "failed": self.count(STATUS_FAILED),
            "timeout": self.count(STATUS_TIMEOUT),
            "skipped_cached": self.count(STATUS_CACHED),
            "deferred": self.count(STATUS_DEFERRED),
            "spent_usd": round(self.spent_usd, 6),
            "budget_usd": self.budget_usd,
            "max_workers": self.max_workers,
            "items": [asdict(outcome) for outcome in self.items],
        }


def _read_cached(path: Path) -> dict[str, Any] | None:
    """A cached result is only reusable if it parses AND records a status —
    a truncated file from a killed process must not read as "already done"."""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if isinstance(payload, dict) and payload.get("status"):
        return payload
    return None


def _persist(path: Path, payload: dict[str, Any]) -> None:
    """Write atomically — a crash mid-write must not leave a half-parsed
    checkpoint that a later resume mistakes for a completed item."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)


def _cost_of(payload: dict[str, Any]) -> float:
    try:
        return float(payload.get("cost_usd") or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _invoke(worker: Worker, item: dict[str, Any]) -> tuple[dict[str, Any], str]:
    """Run the worker, converting a raise into a recorded failure.

    Swallowing is deliberate and narrow: one item's crash must not abort the
    wave and lose the siblings already completed. The error text is kept in
    the result so the failure stays diagnosable.
    """
    try:
        payload = worker(item)
    except Exception as exc:  # noqa: BLE001 — recorded, never silently dropped
        return {"status": STATUS_FAILED, "error": f"{type(exc).__name__}: {exc}"}, f"{type(exc).__name__}: {exc}"
    if not isinstance(payload, dict):
        msg = f"worker returned {type(payload).__name__}, not a dict"
        return {"status": STATUS_FAILED, "error": msg}, msg
    return payload, str(payload.get("error") or "")


def _run_one(item: dict[str, Any], iid: str, path: Path, worker: Worker) -> ItemOutcome:
    """Execute one item and checkpoint it. Runs inside a worker thread when
    ``max_workers > 1``; touches only its own ``path``, so no lock is needed."""
    started = time.monotonic()
    payload, error = _invoke(worker, item)
    outcome_status = str(payload.get("status") or STATUS_OK)
    payload.setdefault("item_id", iid)
    payload["status"] = outcome_status
    _persist(path, payload)
    return ItemOutcome(
        item_id=iid,
        status=outcome_status,
        result_path=str(path),
        error=error,
        duration_s=round(time.monotonic() - started, 4),
        cost_usd=_cost_of(payload),
    )


def run_items(
    items: list[dict[str, Any]],
    *,
    results_dir: str | Path,
    worker: Worker,
    max_workers: int = 1,
    per_item_timeout: float | None = None,
    budget_usd: float | None = None,
    resume: bool = True,
    manifest_path: str | Path | None = None,
) -> FanoutResult:
    """Fan out ``worker`` over ``items`` with a bounded pool and per-item checkpoints.

    ``max_workers=1`` is the default and the safe path: it reproduces today's
    sequential behaviour exactly, so adopting this module is never a
    concurrency change on its own.

    ``per_item_timeout`` is **best-effort and does not kill the worker**. Python
    threads cannot be preempted, so a worker that overruns is recorded as
    ``timeout`` and the fan-out moves on, but the thread keeps running until it
    returns on its own. Pass a timeout to workers that spawn subprocesses and
    let them enforce it there (``subprocess.run(..., timeout=)``) — that is the
    only version that actually stops work.

    ``budget_usd`` stops *admitting* new items once accumulated ``cost_usd``
    crosses it; the remainder are ``deferred``, never ``failed``.
    """
    results_dir = Path(results_dir)
    results_dir.mkdir(parents=True, exist_ok=True)
    workers = max(1, int(max_workers))
    manifest = FanoutResult(spent_usd=0.0, budget_usd=budget_usd, max_workers=workers)

    ordered: list[ItemOutcome | None] = [None] * len(items)
    todo: list[tuple[int, dict[str, Any], str, Path]] = []

    for index, item in enumerate(items):
        iid = item_id(item)
        path = result_path(results_dir, iid)
        if resume and _read_cached(path) is not None:
            ordered[index] = ItemOutcome(
                item_id=iid,
                status=STATUS_CACHED,
                result_path=str(path),
                cost_usd=0.0,  # already counted in a prior run's manifest
            )
            continue
        todo.append((index, item, iid, path))

    budget_spent = budget_usd is not None and manifest.spent_usd >= budget_usd

    if workers == 1:
        for index, item, iid, path in todo:
            if budget_spent:
                ordered[index] = ItemOutcome(item_id=iid, status=STATUS_DEFERRED, error="budget_exhausted")
                continue
            outcome = _run_one(item, iid, path, worker)
            ordered[index] = outcome
            manifest.spent_usd += outcome.cost_usd
            budget_spent = budget_usd is not None and manifest.spent_usd >= budget_usd
    else:
        pool = ThreadPoolExecutor(max_workers=workers)
        in_flight: dict[Future[ItemOutcome], tuple[int, str, float | None]] = {}
        queue = list(todo)

        def _submit_next() -> None:
            nonlocal budget_spent
            # ``len(in_flight) < workers`` is load-bearing for the budget, not
            # for concurrency: ThreadPoolExecutor already caps live threads, but
            # without this guard every item is submitted in the first pass while
            # ``spent`` is still 0, so ``budget_usd`` can never take effect in
            # the pool path. Admitting only as slots free up re-reads spend
            # between items. See test_budget_defers_remainder_in_the_pool_path.
            while queue and len(in_flight) < workers:
                index, item, iid, path = queue.pop(0)
                if budget_spent:
                    ordered[index] = ItemOutcome(
                        item_id=iid, status=STATUS_DEFERRED, error="budget_exhausted"
                    )
                    continue
                deadline = None if per_item_timeout is None else time.monotonic() + per_item_timeout
                in_flight[pool.submit(_run_one, item, iid, path, worker)] = (index, iid, deadline)

        try:
            _submit_next()
            while in_flight:
                if per_item_timeout is None:
                    wait_timeout = None
                else:
                    now = time.monotonic()
                    wait_timeout = max(0.0, min(d for _, _, d in in_flight.values() if d is not None) - now)
                done, _ = wait(list(in_flight), timeout=wait_timeout, return_when=FIRST_COMPLETED)

                # Deadline sweep: only reachable when per_item_timeout is set and
                # nothing finished in the window, so this is the one path that
                # records STATUS_TIMEOUT.
                if not done and per_item_timeout is not None:
                    now = time.monotonic()
                    for future, (index, iid, deadline) in list(in_flight.items()):
                        if deadline is not None and deadline <= now:
                            in_flight.pop(future)
                            ordered[index] = ItemOutcome(
                                item_id=iid, status=STATUS_TIMEOUT, error="per_item_timeout"
                            )

                for future in done:
                    index = in_flight.pop(future)[0]
                    outcome = future.result()
                    ordered[index] = outcome
                    manifest.spent_usd += outcome.cost_usd
                    budget_spent = budget_usd is not None and manifest.spent_usd >= budget_usd
                _submit_next()
        finally:
            # wait=False: a timed-out worker thread is still running and cannot
            # be killed; joining here would block past the deadline we just
            # honoured, making per_item_timeout a lie about wall-clock.
            pool.shutdown(wait=False)

    manifest.items = [outcome for outcome in ordered if outcome is not None]

    target = Path(manifest_path) if manifest_path else results_dir / "manifest.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(manifest.to_dict(), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return manifest
