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

Test files are graded per family: pytest files (``tests/test_*.py``) run under
pytest with a JUnit XML report; jest/vitest files (``*.test.*`` / ``*.spec.*``
/ ``**/__tests__/**``) run under the checked-out repo's own runner and are
parsed from its jest-JSON report, reusing ``mini_ork.certify.test_results`` id
normalization so base and fix ids line up. A mixed commit runs both and the id
sets are unioned.

Tasks are split dev/test by a hash of the fix sha (stable across re-runs), and
the manifest is written with a sha256 lock beside it: any edit to the frozen
set shows up as a lock mismatch. A high-water mark (``<manifest>.hwm``) records
the newest commit *considered* so a re-run defaults ``--since`` to it and mines
only newer history; only commits actually iterated advance the mark, so an
interrupted or ``--limit``ed run never skips unmined commits.

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
import shutil
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
# Make ``mini_ork.certify.test_results`` importable when this runs as a script
# (``python scripts/mine_heldout_tasks.py`` puts ``scripts/`` — not the repo
# root — on sys.path). The JS grader reuses its results parsing and id
# normalization so base/fix ids line up exactly as the certify oracle's do.
sys.path.insert(0, str(REPO))
from mini_ork.certify.test_results import parse_results_dir  # noqa: E402

FIX_SUBJECT = re.compile(r"^fix(\([^)]*\))?!?:")
# A pytest test file: the existing ``tests/test_*.py`` convention.
PYTEST_FILE = re.compile(r"(^|/)tests/(.*/)?test_[^/]*\.py$")
# A jest/vitest test file. Their default ``testMatch`` is
# ``**/__tests__/**/*.[jt]s?(x)`` and ``**/?(*.)+(spec|test).[jt]s?(x)``; the
# extension class folds in the JS/TS variants (js, ts, jsx, tsx, mjs, cjs, …).
JS_TEST_FILE = re.compile(
    r"(^|/)(?:"
    r"__tests__/.*\.[cm]?[jt]sx?"
    r"|[^/]+\.(?:test|spec)\.[cm]?[jt]sx?"
    r")$"
)
#: Every file that is a test for *some* family — the union used to split a
#: fix's files into tests vs. non-tests.
TEST_FILE = re.compile(f"(?:{PYTEST_FILE.pattern})|(?:{JS_TEST_FILE.pattern})")
DOC_SUFFIXES = (".md", ".txt", ".rst")
TRAILER = re.compile(r"^(Co-Authored-By|Signed-off-by|Reviewed-by):", re.I)

#: Grader family keys. pytest and the JS runners are dispatched separately:
#: pytest reports JUnit XML to a file, jest/vitest report jest-JSON.
FAMILY_PYTEST = "pytest"
FAMILY_JS = "jest"


def test_family(path: str) -> str | None:
    """Which grader family ``path`` belongs to, or ``None`` if it is not a test.

    pytest keeps its ``tests/test_*.py`` shape; any ``__tests__/`` file or a
    ``*.test.*`` / ``*.spec.*`` basename is a JS test (jest and vitest share the
    jest-JSON results format, so one dispatch key covers both).
    """
    if PYTEST_FILE.search(path):
        return FAMILY_PYTEST
    if JS_TEST_FILE.search(path):
        return FAMILY_JS
    return None


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
        tests = [f for f in files if test_family(f) and f in alive]
        src = [f for f in files if not test_family(f)
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


def _run_pytest(workdir: Path, present: list[str], python: str,
                timeout: int) -> dict[str, str] | None:
    """pytest path, byte-identical to the original ``run_tests``: JUnit XML to
    a temp file, ids ``<classname>::<name>``. ``None`` when pytest produced no
    report at all (timeout, crash)."""
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


def _locate_js_runner(workdir: Path) -> tuple[str, list[str]] | None:
    """Locate the checked-out repo's own JS runner: ``(runner, argv_prefix)``.

    ``node_modules/.bin/jest`` (or ``vitest``) is preferred — the commit pins
    its own runner, so base and fix use the same version. With no vendored
    binary, fall back to ``npx`` (jest unless package.json only declares
    vitest). ``None`` when neither node nor a vendored binary is present, so
    the caller records an auditable reject rather than silently passing.
    """
    bin_dir = workdir / "node_modules" / ".bin"
    for runner in ("jest", "vitest"):
        cand = bin_dir / runner
        if cand.is_file():
            return runner, [str(cand)]
    if shutil.which("npx"):
        runner = "jest"
        try:
            pkg = (workdir / "package.json").read_text()
        except OSError:
            pkg = ""
        if "vitest" in pkg and "jest" not in pkg:
            runner = "vitest"
        return runner, ["npx", runner]
    return None


def _js_run_cmd(runner: str, prefix: list[str], report: Path,
                present: list[str]) -> list[str]:
    """The runner command that writes machine-readable results to ``report``.

    Mirrors ``mini_ork.certify.test_results.augment_for_results``: jest takes
    ``--json --outputFile=<f>``; vitest takes ``--reporter=json
    --outputFile=<f>`` (plus ``run`` so it never enters watch mode).
    """
    if runner == "vitest":
        return [*prefix, "run", "--reporter=json", f"--outputFile={report}", *present]
    return [*prefix, "--json", f"--outputFile={report}", *present]


def _run_js_tests(workdir: Path, present: list[str], timeout: int,
                  ) -> dict[str, str] | None:
    """Run ``present`` JS test files with the repo's own jest/vitest and parse
    the jest-JSON results file into ``{id: passed|failed}`` via the certify
    adapter (ids are cwd-relative, so base and fix trees agree). ``None`` when
    no runner is available or it produced no parsable report."""
    located = _locate_js_runner(workdir)
    if located is None:
        return None
    runner, prefix = located
    with tempfile.TemporaryDirectory() as tmp:
        report = Path(tmp) / "js-results.json"
        cmd = _js_run_cmd(runner, prefix, report, present)
        env = {**os.environ, "CI": "1"}
        try:
            subprocess.run(cmd, cwd=workdir, env=env, capture_output=True,
                           timeout=timeout)
        except (subprocess.TimeoutExpired, OSError):
            return None
        parsed = parse_results_dir(tmp, str(workdir))
        if parsed is None:
            return None
        passed, failed = parsed
        results = {tid: "passed" for tid in passed}
        results.update({tid: "failed" for tid in failed})
        return results


def run_tests(workdir: Path, test_files: list[str], python: str,
              timeout: int) -> dict[str, str] | None:
    """Grade ``test_files`` in ``workdir``; return {test id: passed|failed|...}.

    dispatch per test-file family: pytest files run under pytest (JUnit XML),
    jest/vitest files run under the repo's own JS runner (jest JSON). A mixed
    commit runs both and the id sets are unioned so ``fail_to_pass`` never
    undercounts. ``None`` when a family that had files produced no report at
    all (timeout, crash, runner absent) — the caller rejects the candidate.
    """
    present = [f for f in test_files if (workdir / f).is_file()]
    if not present:
        return {}
    py = [f for f in present if test_family(f) == FAMILY_PYTEST]
    js = [f for f in present if test_family(f) == FAMILY_JS]
    results: dict[str, str] = {}
    ran = False
    if py:
        res = _run_pytest(workdir, py, python, timeout)
        if res is None:
            return None
        results.update(res)
        ran = True
    if js:
        res = _run_js_tests(workdir, js, timeout)
        if res is None:
            return None
        results.update(res)
        ran = True
    return results if ran else None


def _checkout(scratch: Path, sha: str) -> None:
    _git(scratch, "checkout", "-q", "-f", "--detach", sha)
    _git(scratch, "clean", "-q", "-fd")


def validate(scratch: Path, cand: dict, python: str, timeout: int) -> tuple[dict | None, str]:
    """(task, "") when the candidate is gradable, else (None, reason)."""
    _checkout(scratch, cand["base_sha"])
    _git(scratch, "checkout", "-q", cand["fix_sha"], "--", *cand["test_files"])
    base = run_tests(scratch, cand["test_files"], python, timeout)
    if base is None:
        return None, "base: grader timed out or produced no report"
    _checkout(scratch, cand["fix_sha"])
    gold = run_tests(scratch, cand["test_files"], python, timeout)
    if gold is None:
        return None, "fix: grader timed out or produced no report"
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

    This label is how eval reports get sliced ("wins on easy, loses on hard").
    Files touched leads: a multi-module fix means the agent must first LOCATE
    the bug, which is where agents fail most. Thresholds sit near the mined
    set's quartiles (files p75=2; lines p50=46, p75=103) so no bucket is tiny.
    """
    n_files = len(task["src_files"])
    lines = task["src_lines_changed"]
    if n_files >= 3 or lines > 150:
        return "hard"
    if n_files == 1 and lines <= 50:
        return "easy"
    return "medium"


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


# ── incremental mining: the high-water mark ──────────────────────────────────


def highwater_path(out: Path) -> Path:
    """The high-water-mark file beside the manifest (``manifest.json.hwm``)."""
    return out.with_name(out.name + ".hwm")


def read_highwater(out: Path) -> dict:
    """The last mined mark ``{fix_sha, committed_at}``, or ``{}`` when absent.

    A missing or corrupt mark is not an error: the next run falls back to a
    full scan (the manifest/rejects resume logic still skips done work).
    """
    p = highwater_path(out)
    if not p.is_file():
        return {}
    try:
        obj = json.loads(p.read_text())
    except json.JSONDecodeError:
        return {}
    return obj if isinstance(obj, dict) else {}


def write_highwater(out: Path, cand: dict) -> None:
    """Record the newest commit the miner has considered (kept or rejected).

    The mark advances only over commits actually iterated, so an interrupted
    or ``--limit``ed run never claims to have covered history it skipped.
    """
    p = highwater_path(out)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(
        {"fix_sha": cand["fix_sha"], "committed_at": cand["committed_at"]},
        indent=2, sort_keys=True) + "\n")


def resolve_since(arg_since: str, mark: dict) -> str:
    """The window lower bound: an explicit ``--since`` always wins; otherwise
    the high-water mark's ``committed_at`` so a re-run only validates commits
    newer than the last mined mark."""
    return arg_since or str(mark.get("committed_at") or "")


def _iso_key(text: str):
    """A comparable datetime for an ISO-8601 commit date, or ``None``."""
    try:
        return datetime.fromisoformat(str(text).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def newer_than_mark(cand: dict, mark: dict) -> bool:
    """Is ``cand`` newer than the high-water mark (or is there no mark)?

    ``git log --since`` is inclusive of the boundary commit, so deriving the
    window from the mark re-surfaces the marked commit itself; dropping it here
    keeps the ``--limit`` budget for genuinely new commits. A new commit that
    merely shares the mark's second is still new (only the exact marked sha is
    'already considered').
    """
    marked_iso = str(mark.get("committed_at") or "")
    if not marked_iso:
        return True
    cand_iso = str(cand["committed_at"])
    new, cur = _iso_key(cand_iso), _iso_key(marked_iso)
    if new is not None and cur is not None:
        return new > cur or (new == cur and cand["fix_sha"] != mark.get("fix_sha"))
    return cand_iso > marked_iso or (
        cand_iso == marked_iso and cand["fix_sha"] != mark.get("fix_sha"))


def advance_highwater(out: Path, cand: dict, current_iso: str) -> str:
    """Advance the mark to ``cand`` unless it is older than the current one.

    Oldest-first iteration makes this monotonic within a run; the guard keeps a
    later invocation with a manually-older ``--since`` from rewinding it.
    Returns the (possibly unchanged) current mark iso string.
    """
    iso = str(cand["committed_at"])
    if current_iso:
        cur, new = _iso_key(current_iso), _iso_key(iso)
        if cur is not None and new is not None:
            if new <= cur:
                return current_iso
        elif iso <= current_iso:
            return current_iso
    write_highwater(out, cand)
    return iso


# ── entrypoint ───────────────────────────────────────────────────────────────


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="mine_heldout_tasks", description=__doc__.split("\n")[0])
    p.add_argument("--repo", type=Path, default=REPO)
    p.add_argument("--rev", default="origin/main")
    p.add_argument("--since", default="",
                   help="oldest commit date to consider (default: the "
                        "high-water mark beside --out, so re-runs only mine "
                        "commits newer than the last run)")
    p.add_argument("--until", default="",
                   help="cutoff: tasks after this date stay out of the frozen set")
    p.add_argument("--limit", type=int, default=0, help="max candidates (0 = all)")
    p.add_argument("--python", default=sys.executable)
    p.add_argument("--timeout", type=int, default=300, help="seconds per grader run")
    p.add_argument("--test-fraction", type=float, default=0.5)
    p.add_argument("--out", type=Path, default=REPO / "evals" / "heldout" / "mined" / "manifest.json")
    p.add_argument("--dry-run", action="store_true", help="list candidates only")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    a = _parse_args(argv)
    # Incremental mining: an explicit --since wins, else resume from the mark
    # left by the previous run so only newer commits are candidates.
    mark = read_highwater(a.out)
    since = resolve_since(a.since, mark)
    cands = candidate_commits(a.repo, a.rev, since, a.until, 0)
    # When the window itself came from the mark, drop the boundary commit the
    # (inclusive) --since re-surfaces — and only then apply --limit, so a
    # resumed run spends its budget on new commits rather than re-seeing the
    # one already mined. An explicit --since is trusted as written.
    if not a.since:
        cands = [c for c in cands if newer_than_mark(c, mark)]
    if a.limit:
        cands = cands[:a.limit]
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

    mark_iso = str(mark.get("committed_at") or "")
    scratch = Path(tempfile.mkdtemp(prefix="mo-miner-"))
    _git(a.repo, "worktree", "add", "-q", "--detach", str(scratch), a.rev)
    try:
        for cand in cands:
            if cand["fix_sha"] not in seen:
                task, reason = validate(scratch, cand, a.python, a.timeout)
                tag = cand["fix_sha"][:10]
                if task is None:
                    print(f"[miner] reject {tag}: {reason}", file=sys.stderr)
                    rejects_path.parent.mkdir(parents=True, exist_ok=True)
                    with rejects_path.open("a") as fh:
                        fh.write(json.dumps({"fix_sha": cand["fix_sha"],
                                             "subject": cand["subject"], "reason": reason}) + "\n")
                else:
                    task["id"] = _task_id(task)
                    task["split"] = assign_split(task["fix_sha"], a.test_fraction)
                    task["difficulty"] = classify_difficulty(task)
                    kept[task["fix_sha"]] = task
                    print(f"[miner] keep   {tag}: {len(task['fail_to_pass'])} fail_to_pass "
                          f"({task['split']}, {task['difficulty']})", file=sys.stderr)
                    # Checkpoint after every keep so a long run survives interruption.
                    write_manifest(sorted(kept.values(), key=lambda t: t["committed_at"]), a.out)
            # The commit is considered now (validated, rejected, or skipped as
            # already-seen): advance the mark so an interrupted or --limit'ed
            # run never claims to have covered history it never reached.
            mark_iso = advance_highwater(a.out, cand, mark_iso)
    finally:
        _git(a.repo, "worktree", "remove", "--force", str(scratch), check=False)

    tasks = sorted(kept.values(), key=lambda t: t["committed_at"])
    # Relabel every task, not only new keeps: resumed runs skip validation, and
    # a changed classify_difficulty must still reach tasks mined earlier.
    for t in tasks:
        t["difficulty"] = classify_difficulty(t)
        # No pass_to_pass = the WHOLE test file was red on the base (usually it
        # imports a symbol the fix adds), so the test itself hints at the
        # interface to build. Valid, but reports should be able to exclude it.
        t["weak_signal"] = not t["pass_to_pass"]
    digest = write_manifest(tasks, a.out)
    n_test = sum(t["split"] == "test" for t in tasks)
    print(f"[miner] {len(tasks)} tasks ({n_test} test / {len(tasks) - n_test} dev) "
          f"-> {a.out}  sha256={digest[:12]}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
