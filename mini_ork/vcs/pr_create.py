"""Python port of lib/pr-create.sh — open a GitHub PR for an epic's branch and
persist the URL.

Strangler-fig parity port of mo_open_pr + helpers. `gh` and `git` are shelled
out (network ops); the ported logic is the MO_OPEN_PR gate, idempotence
(existing epics.pr_url short-circuit), the soft-skip pre-flight ladder, the
title/body builders, and the create → view-fallback → persist flow.

    open_pr(epic_id, branch, kickoff_path="", *, repo_root, state_db)
        -> (rc, url)   rc: 0 ok / 1 permanent failure / 2 not-configured
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

_PR_URL_RE = re.compile(r"^https://github\.com/[^/]+/[^/]+/pull/[0-9]+", re.MULTILINE)


def _sql(db, stmt) -> str:
    return subprocess.run(["sqlite3", db, stmt], capture_output=True, text=True).stdout.strip()


def get_existing_url(epic_id, state_db) -> str:
    return _sql(state_db,
                f"SELECT pr_url FROM epics WHERE id='{epic_id}' AND pr_url IS NOT NULL LIMIT 1;")


def write_url(epic_id, url, branch, state_db) -> None:
    _sql(state_db, f"UPDATE epics SET pr_url='{url}', branch='{branch}' WHERE id='{epic_id}';")


def build_title(epic_id, kickoff_path, state_db) -> str:
    title = ""
    if kickoff_path and os.path.isfile(kickoff_path):
        for line in Path(kickoff_path).read_text(errors="ignore").splitlines():
            if line.startswith("# "):
                title = line[2:]
                break
    if not title:
        title = _sql(state_db, f"SELECT title FROM epics WHERE id='{epic_id}';")
    if not title:
        title = f"epic {epic_id}"
    return title[:200]


def build_body(epic_id, kickoff_path) -> str:
    body = f"Auto-opened by mini-ork epic delivery for **{epic_id}**.\n\n"
    if kickoff_path and os.path.isfile(kickoff_path):
        body += "## Kickoff\n\n" + Path(kickoff_path).read_text(errors="ignore")
    else:
        body += f"_No kickoff document found at `{kickoff_path}`._\n"
    return body


def open_pr(epic_id: str, branch: str, kickoff_path: str = "", *,
            repo_root: str, state_db: str) -> tuple[int, str]:
    if os.environ.get("MO_OPEN_PR", "0") != "1":
        return 2, ""

    existing = get_existing_url(epic_id, state_db)
    if existing:
        return 0, existing

    if shutil.which("gh") is None:
        return 2, ""
    if not (os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")):
        if subprocess.run(["gh", "auth", "status"], capture_output=True).returncode != 0:
            return 2, ""
    if subprocess.run(["git", "-C", repo_root, "remote", "get-url", "origin"],
                      capture_output=True).returncode != 0:
        return 2, ""

    if subprocess.run(["git", "-C", repo_root, "rev-parse", "--verify", branch],
                      capture_output=True).returncode != 0:
        return 1, ""
    if subprocess.run(["git", "-C", repo_root, "ls-remote", "--exit-code", "--heads",
                       "origin", branch], capture_output=True).returncode != 0:
        if subprocess.run(["git", "-C", repo_root, "push", "-u", "origin", branch],
                          capture_output=True).returncode != 0:
            return 1, ""

    title = build_title(epic_id, kickoff_path, state_db)
    base = os.environ.get("MO_PR_BASE", "main")
    draft = ["--draft"] if os.environ.get("MO_PR_DRAFT", "0") == "1" else []

    fd, body_file = tempfile.mkstemp(prefix="mo-pr-body-", suffix=".md")
    os.close(fd)
    Path(body_file).write_text(build_body(epic_id, kickoff_path))
    try:
        out = subprocess.run(
            ["gh", "pr", "create", "--base", base, "--head", branch, "--title", title,
             "--body-file", body_file, *draft], capture_output=True, text=True).stdout
        m = _PR_URL_RE.search(out)
        url = m.group(0) if m else ""
        if not url:
            url = subprocess.run(["gh", "pr", "view", branch, "--json", "url", "--jq", ".url"],
                                 capture_output=True, text=True).stdout.strip()
    finally:
        os.unlink(body_file)

    if not url:
        return 1, ""
    write_url(epic_id, url, branch, state_db)
    return 0, url
