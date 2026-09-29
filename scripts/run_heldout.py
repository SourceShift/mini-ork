#!/usr/bin/env python3
"""Run mined held-out tasks through a solver and grade with hidden tests.

For each selected task:
  1. Materialise the task's BASE commit in a throwaway ``git worktree``.
  2. Write a kickoff markdown into the scratch dir from the task's
     ``problem_statement`` (with no hint about the hidden ``test_files``).
  3. Run the solver — by default ``bin/mini-ork run --json <recipe> <kickoff>``
     inside the scratch, or a caller-supplied ``--solver-cmd`` shell command.
  4. Restore the task's ``test_files`` from the fix commit and run pytest
     (``mine_heldout_tasks.run_tests``) — the tests are the grader.
  5. Write a row per task to ``--out``; skip task ids already there on resume.

No network outside the inner solver. The unit tests exercise the runner with
``--solver-cmd`` so no LLM is ever called from the test suite.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
DEFAULT_MANIFEST = REPO / "evals" / "heldout" / "mined" / "manifest.json"
DEFAULT_RECIPE = "code-fix"
DEFAULT_TIMEOUT = 3600
# `mini-ork run` exits 75 (EX_TEMPFAIL) when it refused to start, e.g. the
# daily budget is spent. That is not an attempt; it must not score as a fail.
RC_BLOCKED = 75

# Make ``mine_heldout_tasks.run_tests`` importable as ``grade``'s grader.
sys.path.insert(0, str(REPO / "scripts"))
import mine_heldout_tasks as mht  # noqa: E402


# ── git helper ───────────────────────────────────────────────────────────────


def _git(repo: Path | str, *args: str, check: bool = True) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=check
    ).stdout


# ── task selection ───────────────────────────────────────────────────────────


def _select_tasks(
    manifest: list[dict],
    *,
    split: str,
    difficulty: str | None,
    exclude_weak: bool,
    limit: int,
) -> list[dict]:
    """Return tasks ordered as in the manifest, filtered by the CLI knobs."""
    out: list[dict] = []
    for t in manifest:
        if t.get("split") != split:
            continue
        if difficulty is not None and t.get("difficulty") != difficulty:
            continue
        if exclude_weak and t.get("weak_signal"):
            continue
        out.append(t)
        if limit and len(out) >= limit:
            break
    return out


# ── kickoff rendering ────────────────────────────────────────────────────────


def _render_kickoff(task: dict, scratch: Path) -> str:
    """Build the kickoff markdown written to the scratch dir for the solver.

    Must NOT mention any ``test_files`` path — the tests are hidden, and
    surfacing them would let the solver cheat by editing the grader.
    """
    src_lines = "\n".join(f"- {Path(scratch / p).as_posix()}" for p in task["src_files"])
    return (
        f"# Task {task['id']}\n\n"
        f"{task['problem_statement']}\n\n"
        f"## Files in scope\n\n{src_lines}\n\n"
        f"## Definition of Done\n\n"
        f"Fix the behaviour described above so the repository state at "
        f"HEAD satisfies the spec.\n"
    )


# ── solver invocation ────────────────────────────────────────────────────────


def _run_cost_usd(run_id: str) -> float:
    """Summed ``cost_usd`` of the inner run's traces in state.db.

    ``mini_ork_result=`` carries no cost field, so the ledger is the only
    place the solve's spend is recorded. 0.0 when the db is unreadable.
    """
    db = os.environ.get("MINI_ORK_DB") or os.path.join(
        os.environ.get("MINI_ORK_HOME") or str(REPO / ".mini-ork"), "state.db")
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        try:
            row = con.execute("SELECT COALESCE(SUM(cost_usd), 0) FROM execution_traces "
                              "WHERE run_id = ?", (run_id,)).fetchone()
        finally:
            con.close()
        return float(row[0] or 0.0)
    except sqlite3.Error:
        return 0.0


def _cost_usd_from_sink(line: str) -> float:
    """Parse ``mini_ork_result={...}`` from the inner solver's stdout."""
    prefix = "mini_ork_result="
    for raw in line.splitlines():
        if not raw.startswith(prefix):
            continue
        try:
            obj = json.loads(raw[len(prefix):])
        except json.JSONDecodeError:
            return 0.0
        try:
            return float(obj.get("cost_usd", 0.0) or 0.0)
        except (TypeError, ValueError):
            return 0.0
    return 0.0


def _run_default_solver(
    scratch: Path, kickoff_path: Path, task_id: str, *, timeout: int,
    recipe: str = DEFAULT_RECIPE,
) -> tuple[int, float]:
    """Default solver: ``bin/mini-ork run --json <recipe> <kickoff>`` in scratch.

    Sets ``MO_TARGET_CWD`` so the inner run lands inside the scratch worktree,
    ``MO_ALLOW_FRAMEWORK_CWD=1`` to bypass the framework-cwd guard that
    normally refuses to write under ``mini_ork/``, and ``MINI_ORK_RUN_ID`` so
    the run's logs land in a predictable dir keyed by task id.
    """
    run_id = f"heldout-{task_id}-{int(time.time())}"
    inner_env = {
        **os.environ,
        "MO_TARGET_CWD": str(scratch),
        "MO_ALLOW_FRAMEWORK_CWD": "1",
        "MINI_ORK_RUN_ID": run_id,
        # This runner is the OUTER grader. Without this, a run whose internal
        # verifier doubts the fix rolls the edit back before we grade, and the
        # task is scored on the untouched base (pilot: mo-9a0cf68ccf).
        "MINI_ORK_ROLLBACK_KEEP_WORKTREE": "1",
    }
    # cwd is the ENGINE root, never the scratch: the scratch is itself a
    # mini-ork checkout at an older commit, and `python -m` puts cwd first on
    # sys.path, so the engine imported the TASK's old mini_ork and crashed
    # before planning. MO_TARGET_CWD is what points the lanes at the scratch.
    proc = subprocess.run(
        [str(REPO / "bin" / "mini-ork"), "run", "--json", recipe, str(kickoff_path)],
        cwd=REPO,
        env=inner_env,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    return proc.returncode, (_cost_usd_from_sink(proc.stdout) or _run_cost_usd(run_id))


def _run_solver_cmd(
    cmd: list[str], scratch: Path, task_id: str, kickoff_path: Path, *, timeout: int
) -> tuple[int, float]:
    """Custom solver: a shell command run with cwd=scratch and three env vars.

    Used by the unit tests so no LLM is ever invoked from the test suite.
    """
    env = {
        **os.environ,
        "TASK_ID": task_id,
        "KICKOFF": str(kickoff_path),
        "SCRATCH": str(scratch),
    }
    proc = subprocess.run(cmd, cwd=scratch, env=env, capture_output=True, text=True, timeout=timeout)
    # Custom cmds have no JSON sink — assume $0 cost.
    return proc.returncode, 0.0


# ── grader ───────────────────────────────────────────────────────────────────


def grade(scratch: Path, task: dict, python: str, timeout: int) -> dict[str, Any]:
    """Restore hidden ``test_files`` from ``fix_sha`` and run pytest on them.

    The solver's source changes stay intact: we only overlay the test files
    so a solver that tampered with the grader sees fresh tests. Returns a
    result row shaped per the kickoff contract; ``solver_rc`` and ``seconds``
    are filled by ``run_task`` — this function owns ``passed``,
    ``failed_ids``, ``cost_usd``, ``difficulty``, ``weak_signal``.
    """
    _git(scratch, "checkout", "-q", task["fix_sha"], "--", *task["test_files"])

    outcomes = mht.run_tests(scratch, task["test_files"], python, timeout)
    if outcomes is None:
        passed = False
        failed_ids = sorted(task["fail_to_pass"] + task["pass_to_pass"])
    else:
        want = set(task["fail_to_pass"]) | set(task["pass_to_pass"])
        passed = all(outcomes.get(t) == "passed" for t in want)
        failed_ids = sorted(t for t in want if outcomes.get(t) != "passed")

    return {
        "passed": passed,
        "cost_usd": 0.0,  # filled by run_task after solver returns
        "difficulty": task.get("difficulty", ""),
        "weak_signal": bool(task.get("weak_signal", False)),
        "failed_ids": failed_ids,
        "solver_rc": -1,  # filled by run_task
        "seconds": 0.0,   # filled by run_task
    }


# ── per-task orchestration ──────────────────────────────────────────────────


def _run_task(
    task: dict,
    *,
    python: str,
    timeout: int,
    solver_cmd: list[str] | None,
    repo: Path,
    recipe: str = DEFAULT_RECIPE,
) -> dict[str, Any]:
    """Materialise scratch → kickoff → solve → grade, always removing scratch.

    The scratch lives BESIDE the repo (not under $TMPDIR — colima hides it
    from the docker mount that backs /Volumes/docker-ssd/ — and not inside the
    repo, where it would show up untracked). The kickoff sits next to the
    scratch, never in it: a file in the target tree would be harvested as part
    of the solver's diff.
    """
    scratch_parent = repo.parent / f"{repo.name}-heldout-scratch"
    scratch_parent.mkdir(exist_ok=True)
    scratch = Path(tempfile.mkdtemp(prefix="mo-runner-", dir=str(scratch_parent)))
    _git(repo, "worktree", "add", "-q", "--detach", str(scratch), task["base_sha"])
    kickoff_path = scratch.with_name(scratch.name + ".KICKOFF.md")
    try:
        kickoff_path.write_text(_render_kickoff(task, scratch))
        t0 = time.monotonic()
        if solver_cmd is not None:
            rc, cost = _run_solver_cmd(solver_cmd, scratch, task["id"], kickoff_path, timeout=timeout)
        else:
            rc, cost = _run_default_solver(scratch, kickoff_path, task["id"],
                                           timeout=timeout, recipe=recipe)
        seconds = time.monotonic() - t0

        row = grade(scratch, task, python, timeout)
        row["cost_usd"] = cost
        row["solver_rc"] = rc
        row["seconds"] = seconds
        return row
    finally:
        _git(repo, "worktree", "remove", "--force", str(scratch), check=False)
        # Belt-and-braces: if `git worktree remove` somehow didn't, drop it.
        if scratch.exists():
            shutil.rmtree(scratch, ignore_errors=True)
        kickoff_path.unlink(missing_ok=True)


# ── results IO ───────────────────────────────────────────────────────────────


def _load_results(out: Path) -> dict[str, dict[str, Any]]:
    if not out.is_file():
        return {}
    try:
        obj = json.loads(out.read_text())
    except json.JSONDecodeError:
        return {}
    return obj if isinstance(obj, dict) else {}


def _write_results(out: Path, results: dict[str, dict[str, Any]]) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(results, indent=2, sort_keys=True) + "\n")


# ── entrypoint ───────────────────────────────────────────────────────────────


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="run_heldout", description=__doc__.split("\n")[0])
    p.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST,
                   help="Path to manifest.json (list of task dicts).")
    p.add_argument("--repo", type=Path, default=REPO,
                   help="Source repo whose shas appear in the manifest "
                        "(default: this script's repo). Tests override this.")
    p.add_argument("--split", choices=["dev", "test"], default="dev",
                   help="Manifest split to run (default dev).")
    p.add_argument("--allow-test-split", action="store_true",
                   help="Required when --split test is set.")
    p.add_argument("--difficulty", choices=["easy", "medium", "hard"], default=None,
                   help="Optional difficulty filter.")
    p.add_argument("--exclude-weak", action="store_true",
                   help="Skip tasks whose only signal is a self-hinting test.")
    p.add_argument("--limit", type=int, default=0,
                   help="Max tasks to attempt (0 = all selected).")
    p.add_argument("--recipe", default=DEFAULT_RECIPE,
                   help="Recipe name passed to ``bin/mini-ork run``.")
    p.add_argument("--solver-cmd", default=None,
                   help="Override the solver with a shell command "
                        "(used by the unit tests so no LLM is called).")
    p.add_argument("--out", type=Path, default=Path("heldout_results.json"),
                   help="Where to write the per-task results JSON.")
    p.add_argument("--python", default=sys.executable)
    p.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT,
                   help="Seconds per task (default 3600).")
    p.add_argument("--dry-run", action="store_true",
                   help="Print selected task ids and exit.")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    a = _parse_args(argv)

    if a.split == "test" and not a.allow_test_split:
        sys.stderr.write(
            "refusing --split test without --allow-test-split "
            "(test tasks would leak the answer into dev)\n"
        )
        return 2

    if not a.manifest.is_file():
        sys.stderr.write(f"manifest not found: {a.manifest}\n")
        return 2

    manifest = json.loads(a.manifest.read_text())
    if not isinstance(manifest, list):
        sys.stderr.write("manifest must be a JSON list of task dicts\n")
        return 2

    selected = _select_tasks(
        manifest,
        split=a.split,
        difficulty=a.difficulty,
        exclude_weak=a.exclude_weak,
        limit=a.limit,
    )

    if a.dry_run:
        for t in selected:
            print(t["id"])
        return 0

    solver_cmd = None
    if a.solver_cmd is not None:
        # shlex-split so callers can quote arguments containing whitespace
        # (e.g. a ``python -c "..."`` one-liner with inner spaces).
        import shlex
        solver_cmd = shlex.split(a.solver_cmd)

    results = _load_results(a.out)
    attempted = resolved = 0
    total_cost = 0.0
    # Ensure --out exists on a successful run, even when zero tasks were
    # selected — the next invocation can then resume against it without
    # special-casing "file doesn't exist yet".
    _write_results(a.out, results)
    for task in selected:
        tid = task["id"]
        if tid in results:
            continue
        try:
            row = _run_task(
                task,
                python=a.python,
                timeout=a.timeout,
                solver_cmd=solver_cmd,
                repo=a.repo,
                recipe=a.recipe,
            )
        except subprocess.TimeoutExpired:
            row = {
                "passed": False,
                "cost_usd": 0.0,
                "difficulty": task.get("difficulty", ""),
                "weak_signal": bool(task.get("weak_signal", False)),
                "failed_ids": sorted(task["fail_to_pass"] + task["pass_to_pass"]),
                "solver_rc": -1,
                "seconds": float(a.timeout),
            }
        if row.get("solver_rc") == RC_BLOCKED:
            # Stop the whole eval: every later task would be blocked too, and
            # recording them as failures would silently deflate the score.
            # The task stays unrecorded, so a resume re-attempts it.
            print(f"[heldout-runner] {tid}: solver refused to start (rc={RC_BLOCKED}, "
                  "e.g. daily budget spent) — stopping; re-run to resume.",
                  file=sys.stderr)
            break
        results[tid] = row
        _write_results(a.out, results)
        attempted += 1
        resolved += bool(row.get("passed"))
        total_cost += float(row.get("cost_usd", 0.0) or 0.0)

    print(
        f"[heldout-runner] resolved {resolved} / attempted {attempted} "
        f"(selected={len(selected)})  total_cost=${total_cost:.4f}",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())