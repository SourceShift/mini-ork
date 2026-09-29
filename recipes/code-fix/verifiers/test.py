#!/usr/bin/env python3
# verifiers/test.py — run the project's test suite and emit structured JSON.
#
# Python port of test.sh (bash-removal WS8). Same rc semantics, env vars, and
# output text.
#
# Gating is BASELINE-RELATIVE (delta), not absolute-green: a patch is judged on
# whether it makes the suite WORSE, not on whether the suite is perfectly green.
# Pre-existing failures and broken test environments (wrong interpreter, shadow
# installs, collection/import errors in untouched code) are NOT the patch's fault
# and must not roll back a correct fix. This mirrors SWE-bench's own
# FAIL_TO_PASS / PASS_TO_PASS semantics and fixes the false-reject that discarded
# correct cheap-model patches (see docs: verifier net-negative diagnosis).
#
# A GREEN SUITE THAT DOESN'T TOUCH THE BUG IS THEATRE (arXiv 2607.28871).
# After a green post-patch run we replay the same command on the pre-patch base
# and require at least one test that PASSED on the candidate to FAIL on the
# base. Reuses `mini_ork.certify.replay_check` so the LLM-judge delta gate
# and the shell-test delta gate share the same shape.
#
# Decision table (post = current patched tree, base = HEAD worktree without patch):
#   post green + overlap with base failures  -> pass  (real fix)
#   post green + nothing fails on base       -> fail  (tests-do-not-exercise-change)
#   post green + base unevaluable            -> unverified (abstain; gate softens)
#   post red,  base ALSO red                 -> pass  (pre-existing/env breakage)
#   post red,  base green                    -> fail  (the patch introduced a regression)
#   post red,  base indeterminate            -> fail  (fall back to absolute)
#
# Exit codes:  0 pass   1 fail   (unverified also exits 0 — abstention, not failure)
#
# Env vars:
#   MINI_ORK_TEST_CMD    explicit command to run (skips auto-detect)
#   MINI_ORK_HOME        path to .mini-ork/ dir (default: .mini-ork)
#   MINI_ORK_RUN_ID      current run id (used in log path)
#   MO_TEST_BASELINE     set to 0 to disable baseline (revert to absolute gating)
#   MO_CODEFIX_REPLAY    set to 0 to disable the delta-gate replay (default ON)

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

try:
    # Late import — the verifier must keep working in environments that do
    # not have mini_ork installed (a test fixture that copies just this
    # script, or a code-fix target repo where `mini_ork` is not on sys.path).
    from mini_ork.certify import replay_check as _replay_check
    _REPLAY_AVAILABLE = True
except Exception:                       # pragma: no cover — defensive only
    _replay_check = None                # type: ignore[assignment]
    _REPLAY_AVAILABLE = False

MINI_ORK_HOME = os.environ.get("MINI_ORK_HOME", ".mini-ork")
MINI_ORK_RUN_ID = os.environ.get("MINI_ORK_RUN_ID", "unknown-run")
LOG_DIR = os.path.join(MINI_ORK_HOME, "runs", MINI_ORK_RUN_ID)
os.makedirs(LOG_DIR, exist_ok=True)
LOG_PATH = os.path.join(LOG_DIR, "verifier_test.log")
BASE_LOG = os.path.join(LOG_DIR, "verifier_test_baseline.log")
REPLAY_CANDIDATE_LOG = os.path.join(LOG_DIR, "verifier_replay_candidate.log")
REPLAY_BASE_LOG = os.path.join(LOG_DIR, "verifier_replay_base.log")


def _is_test_path(rel_path: str) -> bool:
    """Test-file pattern per kickoff: segment 'tests'/'test' OR basename match.

    Matches paths like:
      - `tests/unit/foo.py`, `test/unit/foo.py`, `foo/tests/bar.py`
        (any path segment equals `tests` or `test`)
      - `test_foo.py`, `foo/test_foo.py`, `foo_test.py`, `conftest.py`
        (basename matches `test_*.py` / `*_test.py` / `conftest.py`)
    """
    parts = rel_path.replace("\\", "/").split("/")
    for seg in parts[:-1]:
        if seg in ("tests", "test"):
            return True
    base = parts[-1]
    if base == "conftest.py":
        return True
    if base.startswith("test_") and base.endswith(".py"):
        return True
    if base.endswith("_test.py"):
        return True
    return False


def _overlay_candidate_tests(base_wt: str, candidate_cwd: str) -> list[str]:
    """Copy the candidate's test files onto the base worktree.

    Reads `git status --porcelain --untracked-files=all` in the candidate,
    keeps every path whose status indicates modified/added/untracked/deleted
    AND that matches `_is_test_path`. Modified/added/untracked files are
    copied from candidate → base (with directory creation as needed).
    Deleted files are removed from the base too.

    Returns a sorted list of overlaid (or deleted) relative paths. The list
    is the `overlaid_tests` audit trail the kickoff requires.

    This helper is silent on failure: a missing path or a copy/delete error
    must NOT fail the verifier. The overlay augments the base; the replay
    oracle (replay_check) is the one that decides pass/fail.
    """
    proc = subprocess.run(
        ["git", "status", "--porcelain", "--untracked-files=all"],
        cwd=candidate_cwd,
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        return []

    overlaid: list[str] = []
    for line in proc.stdout.splitlines():
        if not line or len(line) < 3:
            continue
        # Renames print as `XY old -> new`; treat the new path as the candidate
        # path and ignore the old (the base still has it from HEAD).
        if " -> " in line:
            prefix, _, new_part = line.partition(" -> ")
            status = prefix[:2]
            rel_path = new_part.strip()
        else:
            status = line[:2]
            rel_path = line[3:].strip()
        if not rel_path:
            continue
        # Skip ignored files; we never want to copy an `.gitignore`-d test.
        if status == "!!":
            continue
        if not _is_test_path(rel_path):
            continue

        src = os.path.join(candidate_cwd, rel_path)
        dst = os.path.join(base_wt, rel_path)
        is_deleted = "D" in status

        try:
            if is_deleted:
                if os.path.lexists(dst):
                    os.remove(dst)
                    overlaid.append(rel_path)
            else:
                if os.path.isfile(src):
                    parent = os.path.dirname(dst)
                    if parent:
                        os.makedirs(parent, exist_ok=True)
                    shutil.copy2(src, dst)
                    overlaid.append(rel_path)
        except OSError:
            # Overlay is best-effort; one bad path must not fail the run.
            pass

    return sorted(overlaid)


def _package_scripts():
    try:
        with open("package.json", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return {}
    scripts = data.get("scripts") if isinstance(data, dict) else None
    return scripts if isinstance(scripts, dict) else {}


def detect_test_cmd():
    # Explicit override wins.
    if os.environ.get("MINI_ORK_TEST_CMD"):
        return os.environ["MINI_ORK_TEST_CMD"]

    # npm / pnpm / yarn — check package.json scripts first.
    if os.path.isfile("package.json"):
        scripts = _package_scripts()
        for candidate in ("test", "test:unit", "test:ci"):
            if candidate in scripts:
                if shutil.which("pnpm"):
                    return f"pnpm run {candidate}"
                if shutil.which("npm"):
                    return "npm test"
        # Fallback: npm test without package.json script parsing
        if shutil.which("pnpm"):
            return "pnpm test"
        if shutil.which("npm"):
            return "npm test"

    # Python pytest
    if shutil.which("pytest"):
        return "pytest"
    if shutil.which("python3"):
        if subprocess.run([sys.executable, "-m", "pytest", "--version"],
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0:
            return "python3 -m pytest"

    # Rust
    if shutil.which("cargo") and os.path.isfile("Cargo.toml"):
        return "cargo test"

    # Go
    if shutil.which("go") and os.path.isfile("go.mod"):
        return "go test ./..."

    # Ruby
    if shutil.which("bundle") and os.path.isfile("Gemfile"):
        return "bundle exec rake test"

    # Nothing found — skip and pass
    return ""


CMD = detect_test_cmd()
BASE_RC = ""


def run_suite(log):  # returns the exit code, never raises
    with open(log, "wb") as fh:
        return subprocess.run(CMD, shell=True, stdout=fh, stderr=subprocess.STDOUT).returncode


def first_fail_line(log):
    pat = re.compile(r"(FAIL|Error|failed|assert|ImportError|ModuleNotFound|Interrupted)")
    try:
        with open(log, encoding="utf-8", errors="replace") as f:
            for line in f:
                if pat.search(line):
                    return line.rstrip("\n").replace('"', '\\"')[:200]
    except OSError:
        pass
    return "see log"


def emit(passed, reason, post_rc, replay=None):
    summary = reason if passed else f"{reason}: {first_fail_line(LOG_PATH)}"
    payload = {
        "verifier": "test", "pass": passed, "evidence_path": LOG_PATH,
        "error_summary": summary, "post_rc": post_rc,
        "base_rc": str(BASE_RC) if BASE_RC != "" else "",
    }
    if replay is not None:
        payload["replay"] = replay
    print(json.dumps(payload, separators=(",", ":"), ensure_ascii=False))
    return 0 if passed else 1


def emit_unverified(post_rc, reason, replay=None):
    """Emit an abstention (gate treats as 'no roll-back, no certify'). Exits 0.

    The payload carries `pass: False` so any caller that only reads `pass`
    does not silently certify a non-verified patch, plus
    `replay_unverified: True` so a gate that knows about abstention can
    route it cleanly. The reason is prefixed with `unverified:` so an
    operator scanning the verifier log sees the abstention immediately.
    """
    payload = {
        "verifier": "test", "pass": False, "evidence_path": LOG_PATH,
        "error_summary": f"unverified: {reason}",
        "post_rc": post_rc,
        "base_rc": str(BASE_RC) if BASE_RC != "" else "",
        "replay_unverified": True,
    }
    if replay is not None:
        payload["replay"] = replay
    print(json.dumps(payload, separators=(",", ":"), ensure_ascii=False))
    return 0


def _attach_git_worktree_base():
    """Create a detached worktree at HEAD. Returns path or None on failure.

    The caller is responsible for `git worktree remove --force <path>`
    afterwards. We use `--detach` because we never want this worktree to
    advance HEAD — that would mutate the user's branch state in a way
    that has nothing to do with the verifier.

    Note: this helper returns the raw HEAD worktree. Use
    `_attach_overlaid_worktree()` for the replay/baseline paths that must
    carry the candidate's test files onto the base (otherwise the replay
    always sees `tests-do-not-exercise-change` for fixes that add an
    untracked regression test).
    """
    in_git = subprocess.run(
        ["git", "rev-parse", "--is-inside-work-tree"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    ).returncode == 0
    if not in_git:
        return None
    wt = tempfile.mkdtemp()
    added = subprocess.run(
        ["git", "worktree", "add", "-q", "--detach", wt, "HEAD"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    ).returncode == 0
    if not added:
        shutil.rmtree(wt, ignore_errors=True)
        return None
    return wt


def _attach_overlaid_worktree(candidate_cwd: str) -> tuple[str | None, list[str]]:
    """HEAD worktree + candidate test-file overlay. Both branches (green
    replay, red baseline) need the same overlay, so they share this helper.

    Returns (worktree_path_or_None, overlaid_paths). The caller is responsible
    for `_detach_git_worktree(wt)` afterwards.
    """
    wt = _attach_git_worktree_base()
    if wt is None:
        return None, []
    return wt, _overlay_candidate_tests(wt, candidate_cwd)


def _detach_git_worktree(wt):
    if not wt:
        return
    subprocess.run(
        ["git", "worktree", "remove", "--force", wt],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    shutil.rmtree(wt, ignore_errors=True)


def _run_replay_check():
    """Run the delta-gate replay. Returns the replay_check() dict, or None if skipped.

    Skip reasons: opt-out env var, certify unavailable, or no git repo.
    We never raise — the caller falls back to "post-patch green" (legacy
    semantics) so a missing dependency cannot wedge a verifier run.

    The base worktree carries the candidate's test files (overlay) so a
    fix that adds an untracked regression test still fails on the base —
    otherwise the delta gate would reject every "fix + new test" patch.
    The overlaid paths are stamped onto `replay.overlaid_tests` so the
    audit trail shows exactly what ran on the base.
    """
    if not _REPLAY_AVAILABLE or _replay_check is None:
        return None
    if os.environ.get("MO_CODEFIX_REPLAY", "1") == "0":
        return None
    candidate_cwd = os.getcwd()
    wt, overlaid = _attach_overlaid_worktree(candidate_cwd)
    if wt is None:
        return None
    try:
        result = _replay_check(
            CMD,
            base_cwd=wt,
            candidate_cwd=candidate_cwd,
            candidate_log=REPLAY_CANDIDATE_LOG,
            base_log=REPLAY_BASE_LOG,
        )
        if isinstance(result, dict) and isinstance(result.get("replay"), dict):
            result["replay"]["overlaid_tests"] = overlaid
        return result
    except Exception as exc:                # pragma: no cover — defensive only
        sys.stderr.write(f"[test] replay raised: {exc}\n")
        return None
    finally:
        _detach_git_worktree(wt)


def main():
    global BASE_RC

    if not CMD:
        sys.stderr.write("[test] no test command detected — skipping (pass)\n")
        print(json.dumps({
            "verifier": "test", "pass": True, "evidence_path": None,
            "error_summary": "no test runner detected — skipped",
        }, separators=(",", ":"), ensure_ascii=False))
        return 0

    # ── Post-patch run (current working tree = patched) ──────────────────
    sys.stderr.write(f"[test] running: {CMD}\n")
    post_rc = run_suite(LOG_PATH)

    if post_rc == 0:
        # ── Delta-gate replay: a green suite that doesn't exercise the bug is theatre ──
        if os.environ.get("MO_CODEFIX_REPLAY", "1") == "0":
            return emit(True, "post-patch suite green (replay opt-out: MO_CODEFIX_REPLAY=0)", post_rc)
        replay_result = _run_replay_check()
        if replay_result is None:
            return emit(True, "post-patch suite green (replay skipped: certify unavailable or not a git repo)", post_rc)
        if replay_result.get("unverified"):
            return emit_unverified(post_rc, replay_result["reason"],
                                   replay=replay_result.get("replay"))
        if replay_result["passed"]:
            return emit(True, "post-patch suite green; replay: tests exercise the change",
                        post_rc, replay=replay_result["replay"])
        return emit(False, replay_result["reason"], post_rc, replay=replay_result["replay"])

    # ── Post-patch failed → establish a baseline to attribute blame ───────
    # Baseline = HEAD (the pre-patch tree) run in a throwaway worktree so the
    # real working directory is never mutated. Only reached when post-patch is red.
    # The baseline worktree must also carry the candidate's test files: a
    # candidate that ADDS a new regression test will not "pre-exist" on HEAD,
    # so without the overlay the baseline would be misleadingly green for
    # tests that were already failing on the candidate.
    in_git = subprocess.run(["git", "rev-parse", "--is-inside-work-tree"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0
    if os.environ.get("MO_TEST_BASELINE", "1") != "0" and in_git:
        wt, _ = _attach_overlaid_worktree(os.getcwd())
        if wt:
            try:
                with open(BASE_LOG, "wb") as fh:
                    BASE_RC = subprocess.run(CMD, shell=True, cwd=wt, stdout=fh,
                                             stderr=subprocess.STDOUT).returncode
            finally:
                _detach_git_worktree(wt)

    if BASE_RC == "":
        # Could not establish a baseline → fall back to absolute gating (do not hide a regression).
        return emit(False, "post-patch failing; no baseline established (absolute gate)", post_rc)
    if BASE_RC != 0:
        # Baseline ALSO fails → pre-existing failure / broken test env in untouched code.
        sys.stderr.write(f"[test] baseline (HEAD) also fails rc={BASE_RC} — pre-existing/env, not a regression\n")
        return emit(True, "pre-existing failure: baseline (HEAD) also fails — uninformative test env, not caused by this patch", post_rc)
    # Baseline green, post red → the patch broke something.
    return emit(False, "regression: baseline (HEAD) passed but post-patch fails", post_rc)


if __name__ == "__main__":
    sys.exit(main())
