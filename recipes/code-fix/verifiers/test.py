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
#   post red,  base ALSO red                 -> per-test decision (see below)
#   post red,  base green                    -> fail  (the patch introduced a regression)
#   post red,  base indeterminate            -> fail  (fall back to absolute)
#
# Red base, per test (post red AND base red):
#   a test passed on base but not on post        -> fail  (regression on a red base)
#   no regression, strict improvement + knob     -> pass  (MO_TEST_CERTIFY_ON_IMPROVEMENT=1)
#   no regression, improvement or equal          -> unverified (red_base_unverified)
#   no per-test outcomes on either side          -> unverified (never a blanket pass)
#
# Exit codes:  0 pass   1 fail   (unverified also exits 0 — abstention, not failure)
#
# Env vars:
#   MINI_ORK_TEST_CMD    explicit command to run (skips auto-detect)
#   MINI_ORK_HOME        path to .mini-ork/ dir (default: .mini-ork)
#   MINI_ORK_RUN_ID      current run id (used in log path)
#   MO_TEST_BASELINE     set to 0 to disable baseline (revert to absolute gating)
#   MO_CODEFIX_REPLAY    set to 0 to disable the delta-gate replay (default ON)
#   MO_SUITE_ADEQUACY               DEFAULT ON; set to 0 to skip the mutant-kill audit
#   MO_SUITE_ADEQUACY_MAX_MUTANTS   cap on generated mutants (default 12, 1..50)
#   MO_SUITE_ADEQUACY_MIN_SCORE     adequacy threshold (default 0.6, 0..1)
#   MO_SUITE_ADEQUACY_TIMEOUT_S     per-run suite timeout (default 300, >=1)
#   MO_ALLOW_TEST_CHANGES           set to 1 to disable the test-weakening guard
#   MO_TEST_LEGACY_RED_BASE         set to 1 to restore the old red-base blanket pass
#   MO_TEST_CERTIFY_ON_IMPROVEMENT  set to 1 to certify a strict red-base improvement

from __future__ import annotations
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

try:
    # Late import — same seam as replay_check above; the verifier must keep
    # working in environments that do not have mini_ork installed.
    from mini_ork.gates import suite_adequacy as _suite_adequacy
except Exception:                       # pragma: no cover — defensive only
    _suite_adequacy = None              # type: ignore[assignment]

try:
    # Late import — same seam as the two above. `scrubbed_test_env` lives
    # outside `mini_ork.verify.__all__`, so it is imported by full subpath.
    from mini_ork.verify.test_env import scrubbed_test_env
except Exception:                       # pragma: no cover — defensive only
    def scrubbed_test_env(environ=None):
        import os as _os
        env = dict(_os.environ if environ is None else environ)
        for k in list(env):
            if (k in {"MINI_ORK_SECRETS", "MINI_ORK_DB", "MINI_ORK_HOME",
                      "MINI_ORK_PROJECT_HOME", "MINI_ORK_RUN_ID", "MINI_ORK_RUN_DIR",
                      "MINI_ORK_PLAN_PATH", "MINI_ORK_AGENTS"}
                    or k.endswith(("_API_KEY", "_AUTH_TOKEN", "_ACCESS_TOKEN",
                                   "_SECRET", "_SECRET_KEY"))
                    or k.startswith("ANTHROPIC_")
                    or k in {"OPENAI_API_BASE", "OPENAI_BASE_URL"}):
                env.pop(k)
        return env

try:
    # Late import — the jest/vitest/results-file leg of the red-base per-test
    # parse; only used when the test command is not pytest.
    from mini_ork.certify.test_results import augment_for_results, parse_results_dir
    _TEST_RESULTS_AVAILABLE = True
except Exception:                       # pragma: no cover — defensive only
    augment_for_results = None          # type: ignore[assignment]
    parse_results_dir = None            # type: ignore[assignment]
    _TEST_RESULTS_AVAILABLE = False


def _child_env(environ=None):
    """Environment for the target repo's test command.

    `scrubbed_test_env()` strips provider credentials and live mini-ork state
    pointers, but PRESERVES every ``MO_*`` knob by contract. The kickoff's
    leak list includes the ``MO_*`` lane keys, so drop those too — a
    target-repo test must not see the operator's lane configuration.
    """
    env = scrubbed_test_env(environ)
    for k in list(env):
        if k.startswith("MO_"):
            env.pop(k)
    return env


MINI_ORK_HOME = os.environ.get("MINI_ORK_HOME", ".mini-ork")
MINI_ORK_RUN_ID = os.environ.get("MINI_ORK_RUN_ID", "unknown-run")
LOG_DIR = os.path.join(MINI_ORK_HOME, "runs", MINI_ORK_RUN_ID)
os.makedirs(LOG_DIR, exist_ok=True)
LOG_PATH = os.path.join(LOG_DIR, "verifier_test.log")
BASE_LOG = os.path.join(LOG_DIR, "verifier_test_baseline.log")
REPLAY_CANDIDATE_LOG = os.path.join(LOG_DIR, "verifier_replay_candidate.log")
REPLAY_BASE_LOG = os.path.join(LOG_DIR, "verifier_replay_base.log")
RED_CANDIDATE_LOG = os.path.join(LOG_DIR, "verifier_red_base_candidate.log")
RED_BASE_LOG = os.path.join(LOG_DIR, "verifier_red_base_base.log")

# Exit codes that mean the process never reached the test runner: 126 (found
# but not executable), 127 (command not found), 77 (the jest-guard convention
# for "refused to start under load"). A baseline with one of these did NOT
# fail — it never ran, so no pass and no regression may be attributed to it.
_UNRUNNABLE_RC = frozenset({"126", "127", "77"})
# The same states, as the output reports them (the rc is often masked by a
# wrapper script that swallows the inner exit code).
_UNRUNNABLE_LOG_RE = re.compile(
    r"command not found|No such file or directory|not executable"
    r"|\brefus(?:e|ed)\b|REFUSE:",
    re.IGNORECASE,
)


def _baseline_unrunnable(base_rc: str, base_log: str) -> str | None:
    """A reason string when the baseline never executed its tests.

    ``BASE_RC != 0`` cannot distinguish "the suite ran and was already red"
    from "the suite never started". Both land in the same bucket, and treating
    the second as a red baseline is how a run with ZERO tests executed got
    certified green (a candidate whose ``node_modules`` exists but whose base
    worktree has none: candidate rc=0, base rc=127).
    """
    if str(base_rc) in _UNRUNNABLE_RC:
        return f"baseline did not execute tests (rc={base_rc})"
    try:
        with open(base_log, encoding="utf-8", errors="replace") as fh:
            text = fh.read()
    except OSError:
        return None
    if _UNRUNNABLE_LOG_RE.search(text):
        return (
            f"baseline did not execute tests (rc={base_rc}; "
            "its output reports a launch failure)"
        )
    return None


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


def _changed_source_files(candidate_cwd: str) -> list[str]:
    """Changed non-test ``.py`` files from ``git status --porcelain``.

    Mirrors ``_overlay_candidate_tests``: renames resolve to the new path,
    ignored (``!!``) and deleted (status contains ``D``) paths are skipped,
    and quoted paths are skipped (they carry git's ``"`` escaping). Only
    ``.py`` paths that are NOT test paths are kept. Returns a sorted, deduped
    list; ``[]`` on any git error.
    """
    proc = subprocess.run(
        ["git", "status", "--porcelain", "--untracked-files=all"],
        cwd=candidate_cwd,
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        return []

    changed: list[str] = []
    for line in proc.stdout.splitlines():
        if not line or len(line) < 3:
            continue
        if " -> " in line:
            prefix, _, new_part = line.partition(" -> ")
            status = prefix[:2]
            rel_path = new_part.strip()
        else:
            status = line[:2]
            rel_path = line[3:].strip()
        if not rel_path:
            continue
        if status == "!!":
            continue
        if "D" in status:
            continue
        if rel_path.startswith('"'):
            continue
        if not rel_path.endswith(".py"):
            continue
        if _is_test_path(rel_path):
            continue
        changed.append(rel_path)
    return sorted(set(changed))


# ── Tamper guard (kickoff antigaming-a §3) ──────────────────────────────────
# A patch that deletes a test file, renames it to a non-test path, or adds a
# skip marker to an EXISTING test file weakens the suite. The guard reads
# `git diff` against HEAD only, never mutates the candidate, and fires before
# the suite runs.

_SKIP_MARKER_RE = re.compile("|".join((
    r"@pytest\.mark\.skip",
    r"@pytest\.mark\.xfail",
    r"pytest\.skip\(",
    r"@unittest\.skip",
    r"\bxit\(",
    r"\bxdescribe\(",
    r"\b(it|test|describe)\.skip\(",
    r"\b(it|test|describe)\.todo\(",
)))


def _is_guard_test_path(rel_path: str) -> bool:
    """Test-file matcher for the tamper guard (broader than ``_is_test_path``).

    A test file is any path under a ``tests/`` / ``__tests__/`` dir segment,
    or a basename matching ``test_*.py`` / ``*_test.py`` /
    ``*.test.[cm]?[jt]sx?`` / ``*.spec.[cm]?[jt]sx?``. Deliberately distinct
    from the replay overlay's narrower ``_is_test_path`` per the kickoff.
    """
    norm = rel_path.replace("\\", "/")
    parts = norm.split("/")
    for seg in parts[:-1]:
        if seg in ("tests", "__tests__"):
            return True
    base = parts[-1]
    if base.startswith("test_") and base.endswith(".py"):
        return True
    if base.endswith("_test.py"):
        return True
    if re.search(r"\.(?:test|spec)\.[cm]?[jt]sx?$", base):
        return True
    return False


def _check_tests_tamper(cwd: str) -> tuple[bool, str]:
    """Return ``(violated, reason)`` for test-weakening changes vs HEAD.

    Deletions (``D``) and renames to a non-test path (``R``) are violations,
    as are newly added skip markers in files that exist in HEAD (``git diff
    -U0`` naturally excludes untracked files, and a new file's ``---`` side is
    ``/dev/null`` so its markers are ignored). Returns ``(False, "")`` on any
    git error or when not inside a work tree.
    """
    in_git = subprocess.run(
        ["git", "rev-parse", "--is-inside-work-tree"],
        cwd=cwd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    ).returncode == 0
    if not in_git:
        return False, ""

    violations: list[str] = []

    name_status = subprocess.run(
        ["git", "diff", "-M", "--name-status", "HEAD"],
        cwd=cwd, capture_output=True, text=True,
    )
    if name_status.returncode == 0:
        for line in name_status.stdout.splitlines():
            if not line.strip():
                continue
            fields = line.split("\t")
            status = fields[0]
            if status.startswith("R"):
                if len(fields) >= 3:
                    old, new = fields[1], fields[2]
                    if _is_guard_test_path(old) and not _is_guard_test_path(new):
                        violations.append(f"{old}: renamed to non-test path {new}")
            elif status == "D":
                path = fields[1] if len(fields) > 1 else ""
                if path and _is_guard_test_path(path):
                    violations.append(f"{path}: deleted")

    diff = subprocess.run(
        ["git", "diff", "-U0", "HEAD"],
        cwd=cwd, capture_output=True, text=True,
    )
    if diff.returncode == 0:
        current: str | None = None
        is_new = False
        for line in diff.stdout.splitlines():
            if line.startswith("--- "):
                rest = line[4:]
                if rest.startswith("a/"):
                    rest = rest[2:]
                is_new = rest == "/dev/null"
                continue
            if line.startswith("+++ "):
                rest = line[4:]
                if rest.startswith("b/"):
                    rest = rest[2:]
                current = rest
                continue
            if line.startswith("+") and not line.startswith("+++"):
                if (current and not is_new and _is_guard_test_path(current)
                        and _SKIP_MARKER_RE.search(line[1:])):
                    marker = line[1:].strip()[:40]
                    violations.append(f"{current}: added skip marker {marker}")

    if not violations:
        return False, ""
    return True, ", ".join(violations)


# ── Red-base per-test parsing (kickoff antigaming-a §1) ─────────────────────
# Local twin of oracle._TEST_RESULT_RE / _ensure_pytest_verbose: the verifier
# must keep working in a target repo with no mini_ork on path, and [c:0]
# forbids editing oracle.py.

_RED_TEST_RESULT_RE = re.compile(
    r"(?P<id>(?:\S+::\S+|\S+\.py))\s+(?P<status>PASSED|FAILED|ERROR|SKIPPED)\b"
)


def _ensure_pytest_verbose(cmd: str) -> str:
    """Insert ``-v --tb=no`` into a pytest command lacking a verbose flag."""
    if "pytest" not in cmd:
        return cmd
    if re.search(r"(?:^|\s)(?:-v\b|--verbose\b)", cmd):
        return cmd
    return re.sub(r"\bpytest\b", "pytest -v --tb=no", cmd, count=1)


def _per_test_outcomes(cmd: str, cwd: str, log: str) -> tuple[int, set[str], set[str]]:
    """Run ``cmd`` in ``cwd`` and return ``(rc, passed_ids, failed_ids)``.

    pytest → augment with ``-v`` and parse the console text. Anything else →
    augment jest/vitest to write a results file and parse
    ``MINI_ORK_TEST_RESULTS_DIR`` via the structured adapters. Returns empty
    sets when no per-test outcomes are produced.
    """
    if "pytest" in cmd:
        augmented = _ensure_pytest_verbose(cmd)
        env = _child_env()
        try:
            with open(log, "wb") as fh:
                rc = subprocess.run(
                    augmented, shell=True, cwd=cwd, env=env,
                    stdout=fh, stderr=subprocess.STDOUT,
                ).returncode
        except OSError:
            return -1, set(), set()
        passed: set[str] = set()
        failed: set[str] = set()
        try:
            with open(log, encoding="utf-8", errors="replace") as fh:
                text = fh.read()
        except OSError:
            return rc, passed, failed
        for m in _RED_TEST_RESULT_RE.finditer(text):
            tid = m.group("id")
            st = m.group("status")
            if st == "PASSED":
                passed.add(tid)
            elif st in ("FAILED", "ERROR"):
                failed.add(tid)
        return rc, passed, failed

    if not _TEST_RESULTS_AVAILABLE:
        return -1, set(), set()
    with tempfile.TemporaryDirectory(prefix="redbase-results-") as tmp:
        augmented, _ = augment_for_results(cmd, tmp)
        env = _child_env()
        env["MINI_ORK_TEST_RESULTS_DIR"] = tmp
        try:
            with open(log, "wb") as fh:
                rc = subprocess.run(
                    augmented, shell=True, cwd=cwd, env=env,
                    stdout=fh, stderr=subprocess.STDOUT,
                ).returncode
        except OSError:
            return -1, set(), set()
        res = parse_results_dir(tmp, cwd)
    if res is None:
        return rc, set(), set()
    return rc, res[0], res[1]


def _judge_red_base(post_rc, base_wt):
    """Per-test decision for a red post-patch suite on a red baseline.

    Replaces the old ``BASE_RC != 0`` blanket pass. Runs both sides with
    per-test outcomes and compares the passing ids:

      - a test that passed on the base but not on the candidate is a
        regression → fail;
      - no regression plus a strict improvement → pass only under
        ``MO_TEST_CERTIFY_ON_IMPROVEMENT=1``, else abstain;
      - equal outcomes, or no per-test outcomes on either side → abstain.
    """
    if not base_wt:
        return emit_unverified(post_rc, "no per-test results on a red base",
                               flag="red_base_unverified")

    _, cand_pass, cand_fail = _per_test_outcomes(CMD, os.getcwd(), RED_CANDIDATE_LOG)
    _, base_pass, base_fail = _per_test_outcomes(CMD, base_wt, RED_BASE_LOG)

    if not (cand_pass or cand_fail or base_pass or base_fail):
        return emit_unverified(post_rc, "no per-test results on a red base",
                               flag="red_base_unverified")

    regressions = sorted(base_pass - cand_pass)
    if regressions:
        return emit(False, f"regression on a red base: {', '.join(regressions[:5])}",
                    post_rc)

    improvements = sorted(cand_pass & base_fail)
    if improvements and os.environ.get("MO_TEST_CERTIFY_ON_IMPROVEMENT", "0") == "1":
        return emit(True, "red base: strict improvement with no regression", post_rc)

    detail = " (strict improvement)" if improvements else ""
    return emit_unverified(post_rc, f"red base: no regression{detail}",
                           flag="red_base_unverified")


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


def run_suite(log, env=None):  # returns the exit code, never raises
    with open(log, "wb") as fh:
        return subprocess.run(CMD, shell=True, stdout=fh, stderr=subprocess.STDOUT,
                              env=env).returncode


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


def emit(passed, reason, post_rc, replay=None, adequacy=None):
    summary = reason if passed else f"{reason}: {first_fail_line(LOG_PATH)}"
    payload = {
        "verifier": "test", "pass": passed, "evidence_path": LOG_PATH,
        "error_summary": summary, "post_rc": post_rc,
        "base_rc": str(BASE_RC) if BASE_RC != "" else "",
    }
    if replay is not None:
        payload["replay"] = replay
    if adequacy is not None:
        payload["suite_adequacy"] = adequacy
    print(json.dumps(payload, separators=(",", ":"), ensure_ascii=False))
    return 0 if passed else 1


def emit_unverified(post_rc, reason, replay=None, adequacy=None, flag="replay_unverified",
                    replay_applicable=None):
    """Emit an abstention (gate treats as 'no roll-back, no certify'). Exits 0.

    The payload carries `pass: False` so any caller that only reads `pass`
    does not silently certify a non-verified patch, plus
    `<flag>: True` so a gate that knows about abstention can route it cleanly.
    The reason is prefixed with `unverified:` so an operator scanning the
    verifier log sees the abstention immediately. `status: "unverified"` is the
    field to branch on; `pass` stays the conservative certify bit, and
    `suite_green` records whether the post-patch suite itself was green
    (`post_rc == 0`). `status` and `suite_green` are appended LAST so readers
    that slice the JSON by byte position are unaffected. `replay_applicable`
    is emitted only when False (the replay instrument does not apply to this
    test runner), and it is appended after `suite_green`.
    """
    payload = {
        "verifier": "test", "pass": False, "evidence_path": LOG_PATH,
        "error_summary": f"unverified: {reason}",
        "post_rc": post_rc,
        "base_rc": str(BASE_RC) if BASE_RC != "" else "",
    }
    payload[flag] = True
    if replay is not None:
        payload["replay"] = replay
    if adequacy is not None:
        payload["suite_adequacy"] = adequacy
    payload["status"] = "unverified"
    payload["suite_green"] = post_rc == 0
    if replay_applicable is False:
        payload["replay_applicable"] = False
    print(json.dumps(payload, separators=(",", ":"), ensure_ascii=False))
    return 0


def _revise_rounds_remain():
    """True when the revise loop has at least one round left.

    Mirrors the runtime's revise-round bookkeeping: rounds remain when there is
    no ``<LOG_DIR>/revise/current.json`` and ``MO_REVISE_ROUNDS`` is enabled, or
    when the recorded ``round`` is below ``max_rounds``. A missing, unreadable,
    or malformed ``current.json`` is treated as "no revise state yet".
    """
    try:
        cap = int(os.environ.get("MO_REVISE_ROUNDS", "2"))
    except ValueError:
        cap = 2
    if cap <= 0:
        return False
    current_path = os.path.join(LOG_DIR, "revise", "current.json")
    try:
        with open(current_path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return True
    if not isinstance(data, dict):
        return True
    try:
        n = int(data.get("round"))
        m = int(data.get("max_rounds", cap))
    except (TypeError, ValueError):
        return True
    # The runtime grants min(edge max_rounds, MO_REVISE_ROUNDS) rounds; trusting
    # the edge max alone would hard-fail on the last granted round.
    return n < min(m, cap)


def _green_pass(reason, post_rc, replay=None, require_adequate=False):
    """Route a green post-patch suite through the adequacy audit (default ON).

    Knob off (``MO_SUITE_ADEQUACY=0``): byte-identical to the pre-audit
    ``emit(True, …)`` — unless ``require_adequate`` (weak fail-to-pass), which
    can never pass without an ``ADEQUATE`` verdict. Knob on: run ``audit_suite``
    against the changed source files.

    Normal (strong) path: ``ADEQUATE`` and ``NOT_APPLICABLE`` keep the pass
    (the latter means the instrument does not apply to the change); anything
    else downgrades a green that cannot kill its mutants to UNVERIFIED
    (pass:false, exit 0) — the existing abstention contract, never a rollback.

    Weak path (``require_adequate=True``): only ``ADEQUATE`` keeps the pass.
    ``NOT_APPLICABLE`` and every other non-ADEQUATE verdict become
    ``weak_f2p_unverified`` — except ``INADEQUATE`` with surviving mutants,
    which feeds the revise loop: with rounds remaining it fails (exit 1) naming
    the survivors so the implementer can strengthen the assertions; with rounds
    exhausted it falls back to today's ``adequacy_unverified`` abstention.
    """
    if os.environ.get("MO_SUITE_ADEQUACY", "1") != "1":
        if require_adequate:
            return emit_unverified(
                post_rc,
                "weak fail-to-pass requires an ADEQUATE suite; adequacy audit disabled",
                replay=replay, flag="weak_f2p_unverified",
            )
        return emit(True, reason, post_rc, replay=replay)

    if _suite_adequacy is None:
        a = {"verdict": "UNVERIFIED", "reason": "module-unavailable"}
    else:
        a = _suite_adequacy.audit_suite(
            os.getcwd(), CMD, _changed_source_files(os.getcwd()),
            report_path=os.path.join(LOG_DIR, "suite_adequacy.json"),
        )
    if a["verdict"] == "ADEQUATE":
        return emit(True, f"{reason}; suite adequacy ADEQUATE (score {a['score']:.3f})",
                    post_rc, replay=replay, adequacy=a)
    if a["verdict"] == "NOT_APPLICABLE":
        if require_adequate:
            return emit_unverified(post_rc, f"weak fail-to-pass: {a['reason']}",
                                   replay=replay, adequacy=a, flag="weak_f2p_unverified")
        return emit(True, f"{reason}; suite adequacy n/a ({a['reason']})",
                    post_rc, replay=replay, adequacy=a)
    if require_adequate and a["verdict"] == "INADEQUATE" and a.get("survivors"):
        survivors = a["survivors"][:5]
        mutant_text = "; ".join(
            f"{s['file']}:{s['line']} {s['operator']}: {s['original']} -> {s['mutated']}"
            for s in survivors
        )
        if _revise_rounds_remain():
            return emit(
                False,
                f"weak fail-to-pass: {mutant_text}; "
                "strengthen the assertions so these mutants fail",
                post_rc, replay=replay, adequacy=a,
            )
        return emit_unverified(
            post_rc, f"suite-{a['verdict'].lower()}: {a['reason']}",
            replay=replay, adequacy=a, flag="adequacy_unverified",
        )
    if require_adequate:
        return emit_unverified(
            post_rc, f"weak fail-to-pass: suite-{a['verdict'].lower()}: {a['reason']}",
            replay=replay, adequacy=a, flag="weak_f2p_unverified",
        )
    return emit_unverified(
        post_rc, f"suite-{a['verdict'].lower()}: {a['reason']}",
        replay=replay, adequacy=a, flag="adequacy_unverified",
    )


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

    # ── Tamper guard (BEFORE the suite) ─────────────────────────────────
    # A patch that deletes/renames a test file or adds a skip marker to an
    # existing one weakens the suite. Refuse before running anything so the
    # revise loop sees the refusal as immediate feedback.
    if os.environ.get("MO_ALLOW_TEST_CHANGES", "0") != "1":
        violated, reason = _check_tests_tamper(os.getcwd())
        if violated:
            return emit(False, f"tests weakened: {reason}", "")

    # ── Post-patch run (current working tree = patched) ──────────────────
    sys.stderr.write(f"[test] running: {CMD}\n")
    post_rc = run_suite(LOG_PATH, env=_child_env())

    if post_rc == 0:
        # ── Delta-gate replay: a green suite that doesn't exercise the bug is theatre ──
        if os.environ.get("MO_CODEFIX_REPLAY", "1") == "0":
            return _green_pass("post-patch suite green (replay opt-out: MO_CODEFIX_REPLAY=0)", post_rc)
        replay_result = _run_replay_check()
        if replay_result is None:
            return _green_pass("post-patch suite green (replay skipped: certify unavailable or not a git repo)", post_rc)
        if replay_result.get("unverified"):
            return emit_unverified(post_rc, replay_result["reason"],
                                   replay=replay_result.get("replay"),
                                   replay_applicable=replay_result.get("applicable"))
        if replay_result["passed"]:
            if replay_result.get("weak"):
                return _green_pass("post-patch suite green; replay: tests exercise the change",
                                   post_rc, replay=replay_result["replay"], require_adequate=True)
            return _green_pass("post-patch suite green; replay: tests exercise the change",
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
    base_wt = None
    if os.environ.get("MO_TEST_BASELINE", "1") != "0" and in_git:
        base_wt, _ = _attach_overlaid_worktree(os.getcwd())

    try:
        if base_wt:
            with open(BASE_LOG, "wb") as fh:
                BASE_RC = subprocess.run(CMD, shell=True, cwd=base_wt, stdout=fh,
                                         stderr=subprocess.STDOUT,
                                         env=_child_env()).returncode

        if BASE_RC == "":
            # Could not establish a baseline → fall back to absolute gating (do not hide a regression).
            return emit(False, "post-patch failing; no baseline established (absolute gate)", post_rc)
        unrunnable = _baseline_unrunnable(BASE_RC, BASE_LOG)
        if unrunnable:
            # NOT a red baseline. Checked BEFORE the legacy hatch so that hatch
            # can never blanket-pass a baseline that never ran.
            sys.stderr.write(f"[test] {unrunnable} — cannot attribute; needs rerun\n")
            return emit(False, f"{unrunnable} — cannot attribute the failure; rerun needed", post_rc)
        if BASE_RC != 0:
            # Baseline ALSO fails. Decide per test instead of blanket-passing
            # (a patch that breaks MORE tests in an already-red suite must not
            # be certified). `MO_TEST_LEGACY_RED_BASE=1` restores the old pass
            # as an explicit escape hatch.
            if os.environ.get("MO_TEST_LEGACY_RED_BASE", "0") == "1":
                sys.stderr.write(f"[test] baseline (HEAD) also fails rc={BASE_RC} — legacy red-base escape hatch\n")
                return emit(True, "pre-existing failure: baseline (HEAD) also fails — uninformative test env, not caused by this patch", post_rc)
            return _judge_red_base(post_rc, base_wt)
        # Baseline green, post red → the patch broke something.
        return emit(False, "regression: baseline (HEAD) passed but post-patch fails", post_rc)
    finally:
        _detach_git_worktree(base_wt)


if __name__ == "__main__":
    sys.exit(main())
