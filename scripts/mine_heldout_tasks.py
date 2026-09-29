#!/usr/bin/env python3
"""Mine a SWE-bench-style held-out task set from this repo's own git history.

Each ``fix:`` commit that changes both code and tests is a candidate task:

    base      = the fix commit's parent (the buggy code)
    problem   = the fix commit's message
    grader    = the tests the fix commit added or changed

A candidate is kept only if it is actually gradable: with the fix's tests
dropped onto the base, at least one test FAILS, and on the fix every one of
those tests PASSES. Those are the task's ``fail_to_pass`` ids; tests that pass
on both are ``pass_to_pass`` (regression guards). Everything else is rejected
with a reason in ``rejects.jsonl`` so the filter stays auditable.

Tasks are split dev/test by a hash of the fix sha (stable across re-runs), and
the manifest is written with a sha256 lock beside it: any edit to the frozen
set shows up as a lock mismatch.

Usage:
    scripts/mine_heldout_tasks.py --since 2026-06-01 --until 2026-09-01 --limit 20
    scripts/mine_heldout_tasks.py --dry-run          # list candidates, run nothing
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
FIX_SUBJECT = re.compile(r"^fix(\([^)]*\))?!?:")
TEST_FILE = re.compile(r"(^|/)tests/(.*/)?test_[^/]*\.py$")
DOC_SUFFIXES = (".md", ".txt", ".rst")
TRAILER = re.compile(r"^(Co-Authored-By|Signed-off-by|Reviewed-by):", re.I)


def _git(repo: Path | str, *args: str, check: bool = True) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True,
                          text=True, check=check).stdout


# ── candidates ───────────────────────────────────────────────────────────────


def candidate_commits(repo: Path, rev: str, since: str = "", until: str = "",
                      limit: int = 0) -> list[dict]:
    """``fix:`` commits with one parent that touch at least one test file and
    at least one non-test, non-doc file. Oldest first.

    The same fix often exists under several shas (cherry-picks, re-landed
    rebases). Duplicates are dropped by patch-id AND by subject+test files, so
    one fix can never sit in both dev and test and leak the answer.
    """
    fmt = "%H%x1f%P%x1f%cI%x1f%s%x1f%b%x1e"
    args = ["log", "--no-merges", "--reverse", f"--format={fmt}"]
    if since:
        args.append(f"--since={since}")
    if until:
        args.append(f"--until={until}")
    out = []
    seen_keys: set = set()
    for rec in _git(repo, *args, rev).split("\x1e"):
        rec = rec.strip("\n")
        if not rec:
            continue
        sha, parents, date, subject, body = (rec.split("\x1f") + [""] * 5)[:5]
        parents = parents.split()
        if len(parents) != 1 or not FIX_SUBJECT.match(subject):
            continue
        files = [f for f in _git(repo, "diff", "--name-only", "--no-renames",
                                 parents[0], sha).splitlines() if f]
        # A test file the fix deleted cannot grade anything (and cannot be
        # checked out at the fix), so tests come from the non-deleted set only.
        alive = set(_git(repo, "diff", "--name-only", "--no-renames",
                         "--diff-filter=d", parents[0], sha).splitlines())
        tests = [f for f in files if TEST_FILE.search(f) and f in alive]
        src = [f for f in files if not TEST_FILE.search(f)
               and not f.endswith(DOC_SUFFIXES)]
        if not tests or not src:
            continue
        keys = {("patch", _patch_id(repo, sha)), ("subject", subject, tuple(sorted(tests)))}
        if keys & seen_keys:
            continue
        seen_keys |= keys
        stat = _git(repo, "diff", "--shortstat", parents[0], sha, "--", *src)
        m = re.search(r"(\d+) insertion", stat)
        d = re.search(r"(\d+) deletion", stat)
        out.append({
            "fix_sha": sha, "base_sha": parents[0], "committed_at": date,
            "subject": subject,
            "problem_statement": _problem_statement(subject, body),
            "src_files": src, "test_files": tests,
            "src_lines_changed": (int(m.group(1)) if m else 0) + (int(d.group(1)) if d else 0),
        })
        if limit and len(out) >= limit:
            break
    return out


def _patch_id(repo: Path, sha: str) -> str:
    diff = _git(repo, "show", "--format=", "--no-renames", sha)
    out = subprocess.run(["git", "-C", str(repo), "patch-id", "--stable"],
                         input=diff, capture_output=True, text=True).stdout
    return out.split()[0] if out.strip() else sha


def _problem_statement(subject: str, body: str) -> str:
    lines = [ln for ln in body.splitlines() if not TRAILER.match(ln.strip())]
    return (subject + "\n\n" + "\n".join(lines).strip()).strip()


# ── validation ───────────────────────────────────────────────────────────────


def run_tests(workdir: Path, test_files: list[str], python: str,
              timeout: int) -> dict[str, str] | None:
    """Run ``test_files`` in ``workdir``; return {test id: passed|failed|error|skipped}.
    ``None`` when pytest produced no report at all (timeout, crash)."""
    present = [f for f in test_files if (workdir / f).is_file()]
    if not present:
        return {}
    with tempfile.TemporaryDirectory() as tmp:
        report = Path(tmp) / "junit.xml"
        env = {**os.environ, "MINI_ORK_ROOT": str(workdir),
               "MINI_ORK_HOME": str(Path(tmp) / "home"),
               "MINI_ORK_DB": str(Path(tmp) / "home" / "state.db"),
               "PYTHONDONTWRITEBYTECODE": "1"}
        try:
            subprocess.run([python, "-m", "pytest", "-q", "-p", "no:cacheprovider",
                            f"--junitxml={report}", *present],
                           cwd=workdir, env=env, capture_output=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            return None
        if not report.is_file():
            return None
        results = {}
        for case in ET.parse(report).getroot().iter("testcase"):
            tid = f"{case.get('classname', '')}::{case.get('name', '')}"
            outcome = "passed"
            for child in case:
                if child.tag in ("failure", "error", "skipped"):
                    outcome = {"failure": "failed"}.get(child.tag, child.tag)
            results[tid] = outcome
        return results


def _checkout(scratch: Path, sha: str) -> None:
    _git(scratch, "checkout", "-q", "-f", "--detach", sha)
    _git(scratch, "clean", "-q", "-fd")


def validate(scratch: Path, cand: dict, python: str, timeout: int) -> tuple[dict | None, str]:
    """(task, "") when the candidate is gradable, else (None, reason)."""
    _checkout(scratch, cand["base_sha"])
    _git(scratch, "checkout", "-q", cand["fix_sha"], "--", *cand["test_files"])
    base = run_tests(scratch, cand["test_files"], python, timeout)
    if base is None:
        return None, "base: pytest timed out or produced no report"
    _checkout(scratch, cand["fix_sha"])
    gold = run_tests(scratch, cand["test_files"], python, timeout)
    if gold is None:
        return None, "fix: pytest timed out or produced no report"
    broken = sorted(t for t, o in gold.items() if o in ("failed", "error"))
    if broken:
        return None, f"fix: {len(broken)} test(s) not passing on the fix itself"
    f2p = sorted(t for t, o in gold.items() if o == "passed" and base.get(t) != "passed")
    if not f2p:
        return None, "no fail_to_pass test: the tests pass on the buggy base too"
    p2p = sorted(t for t, o in gold.items() if o == "passed" and base.get(t) == "passed")
    return {**cand, "fail_to_pass": f2p, "pass_to_pass": p2p}, ""


# ── split, difficulty, manifest ──────────────────────────────────────────────


def assign_split(fix_sha: str, test_fraction: float) -> str:
    """Stable per-task split: the same commit lands in the same split on every
    re-run, so growing the set never moves an existing task across the line."""
    bucket = int(hashlib.sha256(fix_sha.encode()).hexdigest()[:8], 16) / 0x100000000
    return "test" if bucket < test_fraction else "dev"


def classify_difficulty(task: dict) -> str:
    """Return "easy", "medium", or "hard" for a validated task.

    Available signals on ``task``:
      len(task["src_files"])      files the fix touched (excluding tests/docs)
      task["src_lines_changed"]   insertions + deletions in those files
      len(task["fail_to_pass"])   tests the fix turned green
      len(task["pass_to_pass"])   tests that must stay green
      task["problem_statement"]   how much the commit message explains

    TODO(you): decide what "hard" means for mini-ork. This label is how the eval
    report gets sliced ("the new recipe wins on easy, loses on hard"), so it
    should track what actually makes a fix hard for an agent here.
    """
    return "unrated"


def write_manifest(tasks: list[dict], out: Path) -> str:
    """Write the manifest plus a ``.sha256`` lock; return the digest."""
    out.parent.mkdir(parents=True, exist_ok=True)
    body = json.dumps(tasks, indent=2, sort_keys=True) + "\n"
    out.write_text(body)
    digest = hashlib.sha256(body.encode()).hexdigest()
    out.with_name(out.name + ".sha256").write_text(f"{digest}  {out.name}\n")
    return digest


def _task_id(task: dict) -> str:
    return f"mo-{task['fix_sha'][:10]}"


# ── entrypoint ───────────────────────────────────────────────────────────────


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="mine_heldout_tasks", description=__doc__.split("\n")[0])
    p.add_argument("--repo", type=Path, default=REPO)
    p.add_argument("--rev", default="origin/main")
    p.add_argument("--since", default="", help="oldest commit date to consider")
    p.add_argument("--until", default="",
                   help="cutoff: tasks after this date stay out of the frozen set")
    p.add_argument("--limit", type=int, default=0, help="max candidates (0 = all)")
    p.add_argument("--python", default=sys.executable)
    p.add_argument("--timeout", type=int, default=300, help="seconds per pytest run")
    p.add_argument("--test-fraction", type=float, default=0.5)
    p.add_argument("--out", type=Path, default=REPO / "evals" / "heldout" / "mined" / "manifest.json")
    p.add_argument("--dry-run", action="store_true", help="list candidates only")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    a = _parse_args(argv)
    cands = candidate_commits(a.repo, a.rev, a.since, a.until, a.limit)
    print(f"[miner] {len(cands)} candidate fix commits", file=sys.stderr)
    if a.dry_run:
        for c in cands:
            print(f"{c['fix_sha'][:10]}  src={len(c['src_files'])} "
                  f"tests={len(c['test_files'])}  {c['subject']}")
        return 0

    # Resume: keep already-validated tasks, skip already-rejected shas.
    kept = {t["fix_sha"]: t for t in (json.loads(a.out.read_text()) if a.out.is_file() else [])}
    rejects_path = a.out.with_name("rejects.jsonl")
    seen = set(kept)
    if rejects_path.is_file():
        seen |= {json.loads(ln)["fix_sha"] for ln in rejects_path.read_text().splitlines() if ln}

    scratch = Path(tempfile.mkdtemp(prefix="mo-miner-"))
    _git(a.repo, "worktree", "add", "-q", "--detach", str(scratch), a.rev)
    try:
        for i, cand in enumerate(c for c in cands if c["fix_sha"] not in seen):
            task, reason = validate(scratch, cand, a.python, a.timeout)
            tag = cand["fix_sha"][:10]
            if task is None:
                print(f"[miner] reject {tag}: {reason}", file=sys.stderr)
                rejects_path.parent.mkdir(parents=True, exist_ok=True)
                with rejects_path.open("a") as fh:
                    fh.write(json.dumps({"fix_sha": cand["fix_sha"],
                                         "subject": cand["subject"], "reason": reason}) + "\n")
                continue
            task["id"] = _task_id(task)
            task["split"] = assign_split(task["fix_sha"], a.test_fraction)
            task["difficulty"] = classify_difficulty(task)
            kept[task["fix_sha"]] = task
            print(f"[miner] keep   {tag}: {len(task['fail_to_pass'])} fail_to_pass "
                  f"({task['split']}, {task['difficulty']})", file=sys.stderr)
            # Checkpoint after every keep so a long run survives interruption.
            write_manifest(sorted(kept.values(), key=lambda t: t["committed_at"]), a.out)
    finally:
        _git(a.repo, "worktree", "remove", "--force", str(scratch), check=False)

    tasks = sorted(kept.values(), key=lambda t: t["committed_at"])
    # Relabel every task, not only new keeps: resumed runs skip validation, and
    # a changed classify_difficulty must still reach tasks mined earlier.
    for t in tasks:
        t["difficulty"] = classify_difficulty(t)
    digest = write_manifest(tasks, a.out)
    n_test = sum(t["split"] == "test" for t in tasks)
    print(f"[miner] {len(tasks)} tasks ({n_test} test / {len(tasks) - n_test} dev) "
          f"-> {a.out}  sha256={digest[:12]}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
