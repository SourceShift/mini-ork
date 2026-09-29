#!/usr/bin/env python3
"""Replay stored held-out solves through a verifier, for $0, and score it.

Every held-out solve keeps its candidate patch: the runner writes
``<results-stem>.patches/<task>.patch`` for EVERY solve (use --patches-dir);
older runs only have ``<run_dir>/review-diff.patch``, written when the run's
reviewer ran, i.e. never for solves a verifier rejected (--runs-glob). For
each one this script:

  1. checks out the task's base commit in a scratch worktree and applies the
     patch (the candidate fix);
  2. runs the VERIFIER under test on that tree (default: the code-fix test
     verifier) and records its pass/fail;
  3. only then restores the task's hidden tests from the fix commit and grades
     the same tree with them — the ground truth.

Order matters: the verifier must never see the hidden tests.

The result is the verifier's confusion matrix against ground truth:

  accepted & correct   → true accept
  rejected & correct   → FALSE REJECT (a correct fix the run would roll back)
  accepted & wrong     → FALSE ACCEPT (a wrong fix the run would ship)
  rejected & wrong     → true reject

No model is called, so a verifier change can be measured on the whole stored
set in minutes instead of re-running the solves.

Usage:
    scripts/verifier_replay.py --out /tmp/replay.jsonl
    scripts/verifier_replay.py --verifier recipes/code-fix/verifiers/test.py --limit 5
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))
import run_heldout as rh  # noqa: E402

RUN_NAME = re.compile(r"^heldout-(mo-[0-9a-f]+)-\d+$")


def _git(repo: Path | str, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True,
                          text=True, check=check)


def stored_solves(runs_glob: str, tasks: dict[str, dict]) -> list[tuple[Path, dict]]:
    """(run_dir, task) for every run dir that names a known task and kept a
    non-empty review-diff.patch. Oldest first."""
    out = []
    for d in sorted(glob.glob(runs_glob), key=os.path.getmtime):
        m = RUN_NAME.match(Path(d).name)
        patch = Path(d) / "review-diff.patch"
        if m and m.group(1) in tasks and patch.is_file() and patch.stat().st_size > 0:
            out.append((Path(d), tasks[m.group(1)]))
    return out


def stored_patches(patches_dir: Path, tasks: dict[str, dict]) -> list[tuple[Path, dict]]:
    """(patch file, task) for every non-empty <task>.patch the runner kept."""
    out = []
    for f in sorted(patches_dir.glob("*.patch")):
        if f.stem in tasks and f.stat().st_size > 0:
            out.append((f, tasks[f.stem]))
    return out


def run_verifier(verifier: Path, scratch: Path, python: str, timeout: int) -> tuple[bool | None, str]:
    """(pass, reason) from the verifier's JSON line; pass is None when it
    produced no verdict (crash, timeout, non-JSON)."""
    with tempfile.TemporaryDirectory() as tmp:
        env = {**os.environ, "MO_TARGET_CWD": str(scratch), "MINI_ORK_HOME": tmp,
               "MINI_ORK_RUN_DIR": tmp, "MINI_ORK_RUN_ID": "verifier-replay",
               "PYTHONDONTWRITEBYTECODE": "1"}
        try:
            proc = subprocess.run([python, str(verifier)], cwd=scratch, env=env,
                                  capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            return None, "verifier timed out"
    for line in reversed(proc.stdout.splitlines()):
        line = line.strip()
        if line.startswith("{") and '"pass"' in line:
            try:
                payload = json.loads(line)
            except ValueError:
                continue
            reason = str(payload.get("error_summary") or payload.get("reason") or "")
            return payload.get("pass") is True, reason[:300]
    tail = (proc.stderr or proc.stdout).strip().splitlines()[-1:] or [""]
    return None, f"no verdict (rc={proc.returncode}): {tail[0][:200]}"


def replay_one(source: Path, task: dict, *, repo: Path, verifier: Path,
               python: str, timeout: int) -> dict:
    """``source`` is a runner-kept patch file or a run dir holding review-diff.patch."""
    patch = source if source.is_file() else source / "review-diff.patch"
    scratch_parent = repo.parent / f"{repo.name}-replay-scratch"
    scratch_parent.mkdir(exist_ok=True)
    scratch = Path(tempfile.mkdtemp(prefix="replay-", dir=str(scratch_parent)))
    _git(repo, "worktree", "add", "-q", "--detach", str(scratch), task["base_sha"])
    row = {"run": source.name, "task": task["id"], "difficulty": task.get("difficulty", ""),
           "weak_signal": bool(task.get("weak_signal", False))}
    try:
        applied = _git(scratch, "apply", "--whitespace=nowarn", str(patch), check=False)
        if applied.returncode != 0:
            row.update(skipped="patch does not apply to base: "
                       + (applied.stderr.strip().splitlines() or [""])[0][:200])
            return row
        # 1) verifier FIRST — before the hidden tests are restored.
        row["verifier_pass"], row["verifier_reason"] = run_verifier(verifier, scratch, python, timeout)
        # 2) ground truth: restore hidden tests from the fix and grade.
        graded = rh.grade(scratch, task, python, timeout)
        row["hidden_pass"] = graded["passed"]
        row["failed_ids"] = graded["failed_ids"]
        return row
    finally:
        _git(repo, "worktree", "remove", "--force", str(scratch), check=False)


def confusion(rows: list[dict]) -> dict:
    scored = [r for r in rows if "hidden_pass" in r and r.get("verifier_pass") is not None]
    c = {"true_accept": 0, "false_reject": 0, "false_accept": 0, "true_reject": 0}
    for r in scored:
        key = (("true_accept" if r["verifier_pass"] else "false_reject") if r["hidden_pass"]
               else ("false_accept" if r["verifier_pass"] else "true_reject"))
        c[key] += 1
    c["scored"] = len(scored)
    c["no_verdict"] = sum(1 for r in rows if "hidden_pass" in r and r.get("verifier_pass") is None)
    c["skipped"] = sum(1 for r in rows if "skipped" in r)
    return c


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="verifier_replay", description=__doc__.split("\n")[0])
    p.add_argument("--repo", type=Path, default=REPO)
    p.add_argument("--manifest", type=Path, default=rh.DEFAULT_MANIFEST)
    p.add_argument("--runs-glob", default=str(Path(os.environ.get("MINI_ORK_HOME", REPO / ".mini-ork"))
                                              / "runs" / "heldout-mo-*"))
    p.add_argument("--patches-dir", type=Path, default=None,
                   help="runner-kept patches (<results-stem>.patches); preferred over --runs-glob")
    p.add_argument("--verifier", type=Path, default=Path("recipes/code-fix/verifiers/test.py"),
                   help="repo-relative (resolved against --repo) or absolute")
    p.add_argument("--python", default=sys.executable)
    p.add_argument("--timeout", type=int, default=900)
    p.add_argument("--limit", type=int, default=0)
    p.add_argument("--out", type=Path, required=True, help="JSONL, one row per replayed solve")
    a = p.parse_args(argv)

    verifier = a.verifier if a.verifier.is_absolute() else a.repo / a.verifier
    tasks = {t["id"]: t for t in json.loads(a.manifest.read_text())}
    solves = (stored_patches(a.patches_dir, tasks) if a.patches_dir
              else stored_solves(a.runs_glob, tasks))
    if a.limit:
        solves = solves[:a.limit]
    print(f"[replay] {len(solves)} stored solves, verifier={verifier}", file=sys.stderr)
    rows = []
    a.out.parent.mkdir(parents=True, exist_ok=True)
    with a.out.open("w") as fh:
        for source, task in solves:
            row = replay_one(source, task, repo=a.repo, verifier=verifier,
                             python=a.python, timeout=a.timeout)
            rows.append(row)
            fh.write(json.dumps(row) + "\n")
            fh.flush()
            print(f"[replay] {row['run']}: verifier={row.get('verifier_pass')} "
                  f"hidden={row.get('hidden_pass')} {row.get('skipped', '')}", file=sys.stderr)
    print(json.dumps(confusion(rows)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
