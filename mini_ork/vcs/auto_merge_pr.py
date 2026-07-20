"""Python port of lib/auto-merge-pr.sh — PR-based auto-merge gate.

Strangler-fig parity port of mo_auto_merge_pr_one / _sweep + the gh helpers.
`gh` is shelled out (network); the ported logic is the gate ladder (MO_AUTO_MERGE
→ gh-ready → pr_url → checks → approval → soak → merge) with the exact bash
return codes (0 merged, 1 permanent-fail, 2 not-ready/not-configured).

    auto_merge_pr_one(epic_id, *, state_db)  -> rc
    auto_merge_pr_sweep(state_db)            -> (merged, skipped)
"""
from __future__ import annotations

import datetime
import os
import shutil
import subprocess


def _sql(db, stmt) -> str:
    return subprocess.run(["sqlite3", db, stmt], capture_output=True, text=True).stdout.strip()


def gh_ready() -> int:
    if shutil.which("gh") is None:
        return 2
    if not (os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")):
        if subprocess.run(["gh", "auth", "status"], capture_output=True).returncode != 0:
            return 2
    return 0


def checks_passing(pr_url: str) -> int:
    """0 pass, 1 fail, 2 pending/unavailable (retry)."""
    r = subprocess.run(["gh", "pr", "checks", pr_url, "--json", "bucket", "--jq", ".[] | .bucket"],
                       capture_output=True, text=True)
    if r.returncode != 0:
        return 2
    out = r.stdout.strip()
    if not out:
        return 0  # no checks configured → pass
    buckets = out.splitlines()
    if any(b.startswith(("fail", "cancel")) for b in buckets):
        return 1
    if any(b.startswith("pending") for b in buckets):
        return 2
    return 0


def has_approval(pr_url: str) -> bool:
    if os.environ.get("MO_REQUIRE_REVIEWER", "1") != "1":
        return True
    n = subprocess.run(["gh", "pr", "view", pr_url, "--json", "reviewDecision", "--jq",
                        ".reviewDecision"], capture_output=True, text=True).stdout.strip()
    return n == "APPROVED"


def age_ok(pr_url: str) -> bool:
    soak = float(os.environ.get("MO_PR_SOAK_HOURS", "24"))
    r = subprocess.run(["gh", "pr", "view", pr_url, "--json", "createdAt", "--jq", ".createdAt"],
                       capture_output=True, text=True)
    if r.returncode != 0:
        return False
    created = r.stdout.strip()
    if not created:
        return False
    dt = datetime.datetime.fromisoformat(created.replace("Z", "+00:00"))
    now = datetime.datetime.now(dt.tzinfo)
    return (now - dt).total_seconds() / 3600.0 >= soak


def auto_merge_pr_one(epic_id: str, *, state_db: str) -> int:
    if os.environ.get("MO_AUTO_MERGE", "0") != "1":
        return 2
    if gh_ready() != 0:
        return 2
    pr_url = _sql(state_db,
                  f"SELECT pr_url FROM epics WHERE id='{epic_id}' AND pr_url IS NOT NULL LIMIT 1;")
    if not pr_url:
        return 2

    rc = checks_passing(pr_url)
    if rc == 1:
        return 1
    if rc == 2:
        return 2
    if not has_approval(pr_url):
        return 2
    if not age_ok(pr_url):
        return 2

    if subprocess.run(["gh", "pr", "merge", pr_url, "--squash", "--delete-branch", "--auto"],
                      capture_output=True).returncode == 0:
        _sql(state_db, f"UPDATE epics SET status='done' WHERE id='{epic_id}' AND status != 'done';")
        return 0
    return 1


def auto_merge_pr_sweep(state_db: str) -> tuple[int, int]:
    ids = _sql(state_db,
               "SELECT id FROM epics WHERE pr_url IS NOT NULL AND archived_at IS NULL "
               "AND status IN ('in review','in progress','not started') ORDER BY updated_at ASC;")
    merged = skipped = 0
    for ep in ids.splitlines():
        if not ep:
            continue
        if auto_merge_pr_one(ep, state_db=state_db) == 0:
            merged += 1
        else:
            skipped += 1
    return merged, skipped
