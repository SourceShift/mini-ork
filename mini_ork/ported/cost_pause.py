"""Reactive per-run cost pause — Python port of lib/cost_pause.sh.

Pause every MO_PAUSE_EVERY_USD spent (default $25): cumulative spend tracked
in a .cost-spent sidecar; when the spend crosses into a new threshold window,
a .cost-pause sentinel is written and rc=2 is returned so the dispatch halts
until `bin/mini-ork-resume` clears it. Window math matches bash exactly:
crossed = int(new//thr) > int(prev//thr).
"""
from __future__ import annotations

import datetime
import json
import os


def _sentinel_path(run_id: str) -> str:
    run_dir = os.environ.get("MINI_ORK_RUN_DIR", "")
    if run_dir and os.path.isdir(run_dir):
        return os.path.join(run_dir, ".cost-pause")
    home = os.environ.get("MINI_ORK_HOME", ".mini-ork")
    return os.path.join(home, "runs", run_id, ".cost-pause")


def check(run_id: str, delta_usd: float) -> int:
    """rc semantics of mo_cost_pause_check: 0 = proceed, 2 = paused (sentinel
    written when cumulative spend crosses into a new threshold window)."""
    if not run_id:
        return 2
    threshold = float(os.environ.get("MO_PAUSE_EVERY_USD", "25"))
    sentinel = _sentinel_path(run_id)
    spend_file = sentinel[: -len(".cost-pause")] + ".cost-spent"

    prev_spent = 0.0
    if os.path.isfile(spend_file):
        try:
            prev_spent = float(open(spend_file).read().strip() or 0)
        except (ValueError, OSError):
            prev_spent = 0.0
    try:
        new_spent = prev_spent + float(delta_usd)
    except (TypeError, ValueError):
        new_spent = prev_spent
    os.makedirs(os.path.dirname(spend_file) or ".", exist_ok=True)
    with open(spend_file, "w") as fh:
        fh.write(f"{new_spent}\n")

    crossed = int(new_spent // threshold) > int(prev_spent // threshold)
    if crossed:
        with open(sentinel, "w") as fh:
            fh.write(json.dumps({
                "threshold_usd": threshold, "spent_usd": new_spent,
                "created_at": datetime.datetime.now(datetime.timezone.utc)
                    .strftime("%Y-%m-%dT%H:%M:%SZ"),
                "run_id": run_id,
            }) + "\n")
        return 2
    return 0


def status(run_id: str) -> dict:
    """mo_cost_pause_status: {paused, threshold_usd, spent_usd, sentinel_path}."""
    threshold = float(os.environ.get("MO_PAUSE_EVERY_USD", "25"))
    sentinel = _sentinel_path(run_id)
    spend_file = sentinel[: -len(".cost-pause")] + ".cost-spent"
    spent = 0.0
    if os.path.isfile(spend_file):
        try:
            spent = float(open(spend_file).read().strip() or 0)
        except (ValueError, OSError):
            spent = 0.0
    return {"paused": os.path.isfile(sentinel), "threshold_usd": threshold,
            "spent_usd": spent, "sentinel_path": sentinel}
