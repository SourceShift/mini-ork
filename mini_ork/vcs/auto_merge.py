"""Python port of lib/auto-merge.sh — squash-merges APPROVED epic branches into
main.

Strangler-fig parity port of mo_auto_merge + its mutex and untracked-stash
helpers. Git and sqlite3 operations shell out (the function IS a git+DB
orchestrator); the ported logic is the epic-collection + verdict gating + branch
resolution + squash-merge-under-mutex + DB status transition + worktree cleanup.

    auto_merge(repo_root, mini_orch_dir, job_id, *, mini_ork_home, state_db,
               now_iso=None) -> (merged, skipped, failed)

The mutex is the same mkdir-based cross-job lock (stale-PID takeover); the
critical section serialises `git merge --squash` + commit across parallel jobs.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path


def _git(cwd, *args, env=None) -> tuple[str, int]:
    r = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True, env=env)
    return r.stdout.strip(), r.returncode


def _identity_env(cwd) -> dict:
    """Ambient env + a fallback git identity when the repo/global config (and
    GIT_*_EMAIL env) provide none — so the squash `git commit` never dies with
    'Author identity unknown' in identity-less environments (CI runners, fresh
    repos). Configured identity is preserved (setdefault + only-when-unconfigured)."""
    e = dict(os.environ)
    if e.get("GIT_AUTHOR_EMAIL") and e.get("GIT_COMMITTER_EMAIL"):
        return e
    cfg = subprocess.run(["git", "-C", str(cwd), "config", "user.email"],
                         capture_output=True, text=True).stdout.strip()
    if not cfg:
        e.setdefault("GIT_AUTHOR_NAME", "mini-ork")
        e.setdefault("GIT_AUTHOR_EMAIL", "mini-ork@localhost")
        e.setdefault("GIT_COMMITTER_NAME", "mini-ork")
        e.setdefault("GIT_COMMITTER_EMAIL", "mini-ork@localhost")
    return e


def _sql(db, stmt) -> str:
    r = subprocess.run(["sqlite3", db, stmt], capture_output=True, text=True)
    return r.stdout.strip()


def _lock_dir(mini_ork_home): return os.path.join(mini_ork_home, "locks")
def _lock_path(mini_ork_home): return os.path.join(_lock_dir(mini_ork_home), "main-merge.lock")


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False


def acquire_main_mutex(mini_ork_home, job_id, timeout_s=300) -> bool:
    lock_path = _lock_path(mini_ork_home)
    os.makedirs(_lock_dir(mini_ork_home), exist_ok=True)
    waited = 0
    while True:
        try:
            os.mkdir(lock_path)
            break
        except FileExistsError:
            holder = ""
            try:
                holder = Path(lock_path, "pid").read_text().strip()
            except OSError:
                pass
            if holder and holder.isdigit() and not _pid_alive(int(holder)):
                shutil.rmtree(lock_path, ignore_errors=True)
                continue
            if waited >= timeout_s:
                return False
            import time as _t  # local: real sleep, mirrors bash `sleep 1`
            _t.sleep(1)
            waited += 1
    Path(lock_path, "pid").write_text(f"{os.getpid()}\n")
    Path(lock_path, "job").write_text(f"{job_id}\n")
    return True


def release_main_mutex(mini_ork_home) -> None:
    lock_path = _lock_path(mini_ork_home)
    pidf = Path(lock_path, "pid")
    if pidf.is_file() and pidf.read_text().strip() == str(os.getpid()):
        shutil.rmtree(lock_path, ignore_errors=True)


def stash_colliding_untracked(repo_root, branch, epic, job_id, mini_ork_home, now_stamp) -> int:
    if os.environ.get("MO_AUTO_MERGE_STASH_UNTRACKED", "1") != "1":
        return 0
    added, _ = _git(repo_root, "diff", "--name-only", "--diff-filter=A", f"main..{branch}")
    if not added:
        return 0
    stash_dir = os.path.join(mini_ork_home, "auto-merge-stash", job_id, f"{epic}-{now_stamp}")
    moved = 0
    for path in added.splitlines():
        if not path:
            continue
        abs_p = os.path.join(repo_root, path)
        if not os.path.exists(abs_p):
            continue
        porc, _ = _git(repo_root, "status", "--porcelain", "--", path)
        if porc[:2] != "??":
            continue
        if moved == 0:
            os.makedirs(stash_dir, exist_ok=True)
        rel_dir = os.path.join(stash_dir, os.path.dirname(path))
        os.makedirs(rel_dir, exist_ok=True)
        try:
            shutil.move(abs_p, os.path.join(rel_dir, os.path.basename(path)))
            moved += 1
        except OSError:
            pass
    return moved


_BRANCH_LINE = re.compile(r"^>?\s*\*\*Branch:\*\*")


def _resolve_branch(repo_root, kickoff_path) -> str:
    try:
        text = Path(repo_root, kickoff_path).read_text(errors="ignore")
    except OSError:
        return ""
    for line in text.splitlines():
        if _BRANCH_LINE.match(line):
            m = re.search(r"`([^`]+)`", line)
            return m.group(1) if m else ""
    return ""


def _worktree_for(repo_root, branch) -> str:
    out, _ = _git(repo_root, "worktree", "list", "--porcelain")
    cur = None
    for line in out.splitlines():
        if line.startswith("worktree "):
            cur = line[len("worktree "):]
        elif line.startswith("branch ") and line.split()[1] == f"refs/heads/{branch}":
            return cur or ""
    return ""


def auto_merge(repo_root, mini_orch_dir, job_id, *, mini_ork_home, state_db,
               now_iso=None, now_stamp="00000000-000000") -> tuple[int, int, int]:
    job_run_dir = os.path.join(mini_orch_dir, "runs", job_id)
    merged = skipped = failed = 0
    now = now_iso or subprocess.run(
        ["date", "-u", "+%Y-%m-%dT%H:%M:%S.000Z"], capture_output=True, text=True).stdout.strip()

    # ── collect approved epics ──────────────────────────────────────────────
    approved: list[tuple[str, str]] = []
    for epic_dir in sorted(Path(job_run_dir).glob("*/")):
        epic = epic_dir.name
        if epic == os.path.basename(job_run_dir):
            continue
        last_iter = None
        for d in sorted(epic_dir.glob("iter-*/"), key=lambda p: p.name, reverse=True):
            if (d / "verdict.json").is_file():
                last_iter = d
                break
        if last_iter is None:
            continue
        try:
            verdict = json.loads((last_iter / "verdict.json").read_text()).get("verdict", "UNKNOWN")
        except Exception:
            verdict = "UNKNOWN"
        if verdict != "APPROVE":
            skipped += 1
            continue
        if _sql(state_db, f"SELECT status FROM epics WHERE id='{epic}';") == "done":
            skipped += 1
            continue
        kickoff = _sql(state_db, f"SELECT kickoff_path FROM epics WHERE id='{epic}';")
        branch = _resolve_branch(repo_root, kickoff)
        if not branch:
            failed += 1
            continue
        approved.append((epic, branch))

    if not approved:
        return 0, skipped, failed

    for epic, branch in approved:
        # 1. conflict pre-flight (rebase fallback omitted-from-happy-path; on
        #    conflict without a rebaseable worktree the epic fails, as in bash)
        _, rc = _git(repo_root, "merge-tree", "--write-tree", "main", branch)
        if rc != 0:
            wt = _worktree_for(repo_root, branch)
            if wt and os.path.isdir(wt):
                _, rebase_rc = _git(wt, "rebase", "main")
                if rebase_rc != 0:
                    _git(wt, "rebase", "--abort")
                    failed += 1
                    continue
                _, rc2 = _git(repo_root, "merge-tree", "--write-tree", "main", branch)
                if rc2 != 0:
                    failed += 1
                    continue
            else:
                failed += 1
                continue

        commit_count_s, _ = _git(repo_root, "rev-list", "--count", f"main..{branch}")
        commit_count = int(commit_count_s) if commit_count_s.isdigit() else 0
        commit_log, _ = _git(repo_root, "log", "--oneline", f"main..{branch}")
        if commit_count == 0:
            skipped += 1
            continue

        squash_msg = (f"feat({job_id}): merge {epic} ({branch})\n\n"
                      f"Squash-merge of {commit_count} commit(s) from mini-ork job '{job_id}'.\n"
                      f"Reviewer verdict: APPROVE (evidence-based DoD).\n\nCommits:\n{commit_log}")

        if not acquire_main_mutex(mini_ork_home, job_id):
            failed += 1
            continue
        try:
            head_pre, _ = _git(repo_root, "symbolic-ref", "--short", "HEAD")
            if head_pre != "main":
                _, rc = _git(repo_root, "checkout", "main")
                if rc != 0:
                    failed += 1
                    continue
            stash_colliding_untracked(repo_root, branch, epic, job_id, mini_ork_home, now_stamp)
            _, rc = _git(repo_root, "merge", "--squash", branch)
            if rc != 0:
                _git(repo_root, "merge", "--abort")
                _git(repo_root, "checkout", "--", ".")
                failed += 1
                continue
            _, rc = _git(repo_root, "commit", "--no-verify", "-m", squash_msg,
                         env=_identity_env(repo_root))
            if rc != 0:
                _git(repo_root, "checkout", "--", ".")
                failed += 1
                continue
            merged_sha, _ = _git(repo_root, "rev-parse", "HEAD")
            main_after, _ = _git(repo_root, "rev-parse", "main")
            if main_after != merged_sha:
                failed += 1
                continue
        finally:
            release_main_mutex(mini_ork_home)

        # 4. DB updates
        latest_run = _sql(state_db, f"SELECT id FROM runs WHERE epic_id='{epic}' ORDER BY id DESC LIMIT 1;")
        if latest_run:
            _sql(state_db, f"UPDATE runs SET merged_sha='{merged_sha}', final_verdict='MERGED', "
                           f"ended_at=COALESCE(ended_at, '{now}') WHERE id={latest_run};")
        else:
            base, _ = _git(repo_root, "rev-parse", "main~1")
            _sql(state_db, "INSERT INTO runs (epic_id, run_dir, branch, baseline_sha, agent, "
                 f"final_verdict, merged_sha, ended_at) VALUES ('{epic}', 'mini-ork/{job_id}/{epic}', "
                 f"'{branch}', '{base}', 'mini-ork', 'MERGED', '{merged_sha}', '{now}');")
        _sql(state_db, f"UPDATE epics SET status='done', updated_at='{now}' WHERE id='{epic}';")

        wt = _worktree_for(repo_root, branch)
        if wt:
            _git(repo_root, "worktree", "remove", "--force", wt)
        _git(repo_root, "branch", "-D", branch)
        merged += 1

    return merged, skipped, failed
