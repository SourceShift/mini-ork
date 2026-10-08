"""Unit tests for the code-fix verifier's delta-gate replay check.

Each test stands up a throwaway git repo in `tmp_path` with a function + a
test, runs `recipes/code-fix/verifiers/test.py` against it, and asserts both
the JSON verdict and that the candidate working tree is byte-identical
before and after.

The verifier auto-detects a pytest command via `MINI_ORK_TEST_CMD` (we
pass it explicitly to keep the test hermetic against the worker's PATH).
We disable pytest's on-disk cache (`-p no:cacheprovider`) so the replay
does not leave `.pytest_cache/` behind in the candidate working tree —
the "byte-identical" invariant covers only source files anyway, but a
cleaner diff is easier to read when a regression breaks it.
"""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
VERIFIER = REPO_ROOT / "recipes" / "code-fix" / "verifiers" / "test.py"

# `-p no:cacheprovider` keeps `.pytest_cache/` out of the candidate
# working tree; otherwise the verifier leaves pytest's cache behind and
# the "byte-identical" check on test 4 has to special-case it.
# We deliberately do NOT pass `-q`: pytest resolves conflicting verbosity
# flags by taking the LAST one, so a trailing `-q` would silence the
# per-test lines the replay helper needs to parse.
TEST_CMD = "python3 -m pytest -p no:cacheprovider"


def _git(cwd: Path, *args: str) -> None:
    subprocess.check_call(
        ["git", *args], cwd=cwd,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )


def _git_text(cwd: Path, *args: str) -> str:
    return subprocess.check_output(["git", *args], cwd=cwd, text=True)


def _make_repo(
    parent: Path,
    *,
    mod_src: str,
    test_src: str,
) -> Path:
    """Stand up a throwaway git repo with the given module + test (initial commit)."""
    repo = parent / "r"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    _git(repo, "config", "core.autocrlf", "false")
    (repo / "mod.py").write_text(mod_src)
    (repo / "test_mod.py").write_text(test_src)
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "init")
    return repo


def _run_verifier(repo: Path, tmp_path: Path, *, replay: str, run_id: str,
                  test_cmd: str = TEST_CMD) -> dict:
    """Invoke the verifier on `repo`. Returns the parsed JSON envelope (last stdout line)."""
    mini_home = tmp_path / "mo-home"
    mini_home.mkdir(exist_ok=True)
    env = os.environ.copy()
    env["MINI_ORK_HOME"] = str(mini_home)
    env["MINI_ORK_RUN_ID"] = run_id
    env["MO_CODEFIX_REPLAY"] = replay
    env["MO_SUITE_ADEQUACY"] = "0"  # these tests are about replay, not adequacy
    env["MINI_ORK_TEST_CMD"] = test_cmd
    # The verifier late-imports `mini_ork.certify`; that import needs the
    # worktree root on sys.path because the verifier is run as a script
    # (`python3 verifier/test.py`) not as `python3 -m`.
    env["PYTHONPATH"] = str(REPO_ROOT)
    # pytest otherwise writes `__pycache__/*.pyc` into the candidate
    # working tree, which violates the "byte-identical before and after"
    # invariant on test 4. The cleaner shape would be to special-case
    # `__pycache__` in the snapshot, but this is a one-line fix that
    # also keeps the source tree cleaner for human inspection.
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    proc = subprocess.run(
        [sys.executable, str(VERIFIER)], cwd=repo, env=env,
        capture_output=True, text=True, timeout=120,
    )
    last = proc.stdout.strip().splitlines()
    assert last, (
        f"verifier produced no JSON: rc={proc.returncode} "
        f"stdout={proc.stdout!r} stderr={proc.stderr!r}"
    )
    return json.loads(last[-1])


def _snap_worktree(repo: Path) -> tuple[dict, str]:
    """Snapshot (relative-path -> bytes) for files OUTSIDE .git, plus the porcelain status.

    `.git/` is excluded because the verifier creates a `git worktree add
    --detach HEAD <tmp>` whose metadata lands in `<repo>/.git/worktrees/`.
    That is git's internal storage, not the candidate working tree, so
    it does not count as a "mutation" of what we are protecting.
    """
    files = {
        p.relative_to(repo): p.read_bytes()
        for p in repo.rglob("*")
        if p.is_file() and not str(p.relative_to(repo)).startswith(".git")
    }
    status = _git_text(repo, "status", "--porcelain")
    return files, status


# ── 1. real fix + regression test → pass (overlap) ─────────────────────────
def test_replay_passes_for_real_fix(tmp_path):
    """Base = buggy. Candidate = fixed + regression test that fails on base."""
    repo = _make_repo(
        tmp_path,
        mod_src="def add(a, b):\n    return a - b\n",
        test_src="from mod import add\n\ndef test_add():\n    assert add(2, 3) == 5\n",
    )
    # The fix lives in the working tree (uncommitted) — HEAD stays buggy.
    (repo / "mod.py").write_text("def add(a, b):\n    return a + b\n")

    before_files, before_status = _snap_worktree(repo)

    out = _run_verifier(repo, tmp_path, replay="1", run_id="replay-pass")

    assert out["verifier"] == "test"
    assert out["pass"] is True, out
    assert "exercise" in out["error_summary"]
    assert "replay" in out, out
    overlap = out["replay"]["overlap"]
    assert any("test_add" in tid for tid in overlap), overlap

    after_files, after_status = _snap_worktree(repo)
    assert before_files == after_files, "file set or contents changed"
    assert before_status == after_status, (
        f"git status changed\nbefore={before_status!r}\nafter={after_status!r}"
    )


# ── 2. test passes on base too → fail (tests-do-not-exercise-change) ───────
def test_replay_fails_when_test_passes_on_base(tmp_path):
    """The "patch" is a no-op add of a test that ALSO passes on the base."""
    repo = _make_repo(
        tmp_path,
        # Code is already correct at HEAD; the "patch" adds a redundant test.
        mod_src="def add(a, b):\n    return a + b\n",
        test_src="from mod import add\n\ndef test_add():\n    assert add(2, 3) == 5\n",
    )
    (repo / "test_extra.py").write_text(
        "from mod import add\n\ndef test_extra():\n    assert add(0, 0) == 0\n"
    )

    out = _run_verifier(repo, tmp_path, replay="1", run_id="replay-fail")

    assert out["verifier"] == "test"
    assert out["pass"] is False, out
    assert "tests-do-not-exercise-change" in out["error_summary"]
    assert "replay" in out
    assert out["replay"]["overlap"] == [], out["replay"]


# ── 3. MO_CODEFIX_REPLAY=0 → replay skipped, legacy semantics ─────────────
def test_replay_skipped_when_opt_out(tmp_path):
    repo = _make_repo(
        tmp_path,
        mod_src="def add(a, b):\n    return a - b\n",
        test_src="from mod import add\n\ndef test_add():\n    assert add(2, 3) == 5\n",
    )
    (repo / "mod.py").write_text("def add(a, b):\n    return a + b\n")

    out = _run_verifier(repo, tmp_path, replay="0", run_id="replay-skip")

    assert out["verifier"] == "test"
    assert out["pass"] is True, out
    assert "opt-out" in out["error_summary"] or "MO_CODEFIX_REPLAY=0" in out["error_summary"]
    # Opt-out path emits no `replay` payload — the verifier has not done
    # any extra work to report.
    assert "replay" not in out


# ── 4. target working tree byte-identical before and after ─────────────────
def test_working_tree_untouched(tmp_path):
    repo = _make_repo(
        tmp_path,
        mod_src="def add(a, b):\n    return a - b\n",
        test_src="from mod import add\n\ndef test_add():\n    assert add(2, 3) == 5\n",
    )
    (repo / "mod.py").write_text("def add(a, b):\n    return a + b\n")

    before_files, before_status = _snap_worktree(repo)

    out = _run_verifier(repo, tmp_path, replay="1", run_id="wt-untouched")
    # Sanity: replay actually ran (the test exercises the worktree path).
    assert "replay" in out, f"replay did not run: {out}"

    after_files, after_status = _snap_worktree(repo)
    assert before_files == after_files, (
        f"file set or contents changed\nbefore={sorted(before_files)}\n"
        f"after={sorted(after_files)}"
    )
    assert before_status == after_status, (
        f"git status changed\nbefore={before_status!r}\nafter={after_status!r}"
    )


# ── 5. replay helper unit tests (no verifier subprocess) ───────────────────
# These exercise `mini_ork.certify.replay_check` directly. They keep the
# helper honest under hermetic conditions (no pytest collection error,
# no missing dependency) and let a regression in the regex or the
# unverified branches fail loudly even when the verifier path is happy.

from mini_ork.certify import replay_check


def _base_worktree(repo: Path, tmp_path: Path) -> Path:
    """Materialise a worktree at HEAD (the pre-change state) for the helper to consume."""
    wt = tmp_path / "base-wt"
    _git(repo, "worktree", "add", "-q", "--detach", str(wt), "HEAD")
    return wt


def _cleanup_worktree(repo: Path, wt: Path) -> None:
    _git(repo, "worktree", "remove", "--force", str(wt))


def test_replay_helper_passes_with_real_fix(tmp_path):
    repo = _make_repo(
        tmp_path,
        mod_src="def add(a, b):\n    return a - b\n",
        test_src="from mod import add\n\ndef test_add():\n    assert add(2, 3) == 5\n",
    )
    (repo / "mod.py").write_text("def add(a, b):\n    return a + b\n")
    wt = _base_worktree(repo, tmp_path)
    try:
        result = replay_check(TEST_CMD, base_cwd=str(wt), candidate_cwd=str(repo))
    finally:
        _cleanup_worktree(repo, wt)
    assert result["passed"] is True, result
    assert result["unverified"] is False
    assert any("test_add" in tid for tid in result["replay"]["overlap"])


def test_replay_helper_fails_with_passing_on_base(tmp_path):
    repo = _make_repo(
        tmp_path,
        mod_src="def add(a, b):\n    return a + b\n",
        test_src="from mod import add\n\ndef test_add():\n    assert add(2, 3) == 5\n",
    )
    (repo / "test_extra.py").write_text(
        "from mod import add\n\ndef test_extra():\n    assert add(0, 0) == 0\n"
    )
    wt = _base_worktree(repo, tmp_path)
    try:
        result = replay_check(TEST_CMD, base_cwd=str(wt), candidate_cwd=str(repo))
    finally:
        _cleanup_worktree(repo, wt)
    assert result["passed"] is False, result
    assert "tests-do-not-exercise-change" in result["reason"]
    assert result["replay"]["overlap"] == []


def test_replay_helper_unverified_for_non_pytest():
    # The literal abstention reason is host-dependent: with no cargo project in
    # /tmp the command yields no test-run marker (adapter-list reason) and, where
    # cargo is absent, an unrunnable-rc reason. Both are abstentions, which is
    # the contract this pins; the exact adapter wording is pinned by
    # test_replay_structured_runners.py::test_no_adapter_no_results_is_not_applicable.
    result = replay_check("cargo test", base_cwd="/tmp", candidate_cwd="/tmp")
    assert result["unverified"] is True
    assert result["passed"] is False
    assert result["reason"]


def test_replay_helper_unverified_for_missing_base(tmp_path):
    result = replay_check(TEST_CMD, base_cwd=str(tmp_path / "does-not-exist"),
                          candidate_cwd=str(tmp_path))
    assert result["unverified"] is True


# ── 6. test-file overlay onto the base worktree (rsi-i3-bsg-va-wiring-repair) ──
# The previous WIP pass (767c4037) added the replay infrastructure but left
# the base worktree empty of the candidate's test files. A "fix + new
# regression test" patch has nothing to fail on the base, so the verifier
# rejected every correct fix with `tests-do-not-exercise-change`. The overlay
# carries the candidate's test files (matched by `_is_test_path`) onto the
# base so the replay delta is honest. These four tests pin the contract.

def test_overlay_carries_new_untracked_test_for_real_fix(tmp_path):
    """Buggy HEAD. Candidate fixes mod.py + adds a NEW UNTRACKED regression test
    that fails on the buggy code. Verifier must PASS and stamp the new test
    file in `replay.overlaid_tests`. This is the case the previous WIP got wrong."""
    repo = _make_repo(
        tmp_path,
        mod_src="def add(a, b):\n    return a - b\n",
        # Trivial test that passes on both buggy and fixed code; does NOT exercise
        # the bug. Committed at HEAD so it is present on the base too.
        test_src="from mod import add\n\ndef test_add_trivial():\n    assert add(0, 0) == 0\n",
    )
    # Candidate fixes the bug.
    (repo / "mod.py").write_text("def add(a, b):\n    return a + b\n")
    # Candidate ADDS a NEW UNTRACKED regression test that fails on buggy code.
    # Path uses a `tests/` segment to exercise the segment-match branch of
    # `_is_test_path`.
    tests_dir = repo / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_bug_regression.py").write_text(
        "from mod import add\n\ndef test_bug_fixed():\n    assert add(2, 3) == 5\n"
    )
    # Sanity: the new test file is untracked (overlay must carry it; HEAD does not).
    # `-uall` so git reports files inside the untracked `tests/` directory
    # rather than collapsing to a single `?? tests/` entry.
    porcelain = _git_text(repo, "status", "--porcelain", "-uall")
    assert any("test_bug_regression.py" in ln for ln in porcelain.splitlines()), porcelain

    before_files, before_status = _snap_worktree(repo)

    out = _run_verifier(repo, tmp_path, replay="1", run_id="overlay-untracked-pass")

    assert out["verifier"] == "test"
    assert out["pass"] is True, out
    assert "exercise" in out["error_summary"]
    assert "replay" in out, out
    # The overlay audit trail must name the new test file.
    assert out["replay"]["overlaid_tests"] == ["tests/test_bug_regression.py"], out["replay"]
    # And that test must be in the overlap (passed on candidate, failed on base).
    assert any("test_bug_fixed" in tid for tid in out["replay"]["overlap"]), out["replay"]

    # Candidate tree unchanged.
    after_files, after_status = _snap_worktree(repo)
    assert before_files == after_files, "file set or contents changed"
    assert before_status == after_status, (
        f"git status changed\nbefore={before_status!r}\nafter={after_status!r}"
    )


def test_overlay_carries_untracked_test_passing_on_base(tmp_path):
    """Candidate adds a NEW UNTRACKED test that ALSO passes on buggy code.
    Verifier must FAIL with `tests-do-not-exercise-change`; overlay still
    stamps the file (audit trail is independent of pass/fail)."""
    repo = _make_repo(
        tmp_path,
        # Code is already correct at HEAD; the "patch" only adds a redundant test.
        mod_src="def add(a, b):\n    return a + b\n",
        test_src="from mod import add\n\ndef test_add():\n    assert add(2, 3) == 5\n",
    )
    # New UNTRACKED test in a `tests/` segment that passes on the correct code.
    tests_dir = repo / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_extra.py").write_text(
        "from mod import add\n\ndef test_extra():\n    assert add(0, 0) == 0\n"
    )

    out = _run_verifier(repo, tmp_path, replay="1", run_id="overlay-untracked-fail")

    assert out["verifier"] == "test"
    assert out["pass"] is False, out
    assert "tests-do-not-exercise-change" in out["error_summary"]
    # Overlay still records the file (it DID cross to the base).
    assert out["replay"]["overlaid_tests"] == ["tests/test_extra.py"], out["replay"]
    # No overlap because the new test passed on both sides.
    assert out["replay"]["overlap"] == [], out["replay"]


def test_overlay_skips_non_test_source_changes(tmp_path):
    """A non-test source file changed in the candidate is NOT carried onto the base.
    The overlay is selective — only test paths cross over."""
    repo = _make_repo(
        tmp_path,
        mod_src="def add(a, b):\n    return a - b\n",
        # Tracked regression test that fails on buggy code, passes on fixed code.
        test_src="from mod import add\n\ndef test_add():\n    assert add(2, 3) == 5\n",
    )
    # Only modify a non-test source file — no test changes.
    (repo / "mod.py").write_text("def add(a, b):\n    return a + b\n")

    before_files, before_status = _snap_worktree(repo)

    out = _run_verifier(repo, tmp_path, replay="1", run_id="overlay-non-test")

    assert out["verifier"] == "test"
    assert out["pass"] is True, out  # the tracked test exercises the fix
    assert "exercise" in out["error_summary"]
    # Non-test source change → overlay must be empty.
    assert out["replay"]["overlaid_tests"] == [], out["replay"]
    # And the existing tracked test must be in the overlap.
    assert any("test_add" in tid for tid in out["replay"]["overlap"]), out["replay"]

    after_files, after_status = _snap_worktree(repo)
    assert before_files == after_files, "file set or contents changed"
    assert before_status == after_status, (
        f"git status changed\nbefore={before_status!r}\nafter={after_status!r}"
    )


def test_overlay_preserves_candidate_tree_byte_identical(tmp_path):
    """The overlay writes into the BASE worktree, never the candidate. The
    candidate working tree must be byte-identical before and after a verifier
    run that exercises the overlay path."""
    repo = _make_repo(
        tmp_path,
        mod_src="def add(a, b):\n    return a - b\n",
        test_src="from mod import add\n\ndef test_add():\n    assert add(0, 0) == 0\n",
    )
    (repo / "mod.py").write_text("def add(a, b):\n    return a + b\n")
    # Add an untracked test file so the overlay has something to copy.
    tests_dir = repo / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_bug.py").write_text(
        "from mod import add\n\ndef test_bug():\n    assert add(2, 3) == 5\n"
    )

    before_files, before_status = _snap_worktree(repo)

    out = _run_verifier(repo, tmp_path, replay="1", run_id="overlay-tree-clean")

    # Sanity: the overlay path actually ran.
    assert "replay" in out, out
    assert out["replay"]["overlaid_tests"] == ["tests/test_bug.py"], out["replay"]

    after_files, after_status = _snap_worktree(repo)
    assert before_files == after_files, (
        f"file set or contents changed\nbefore={sorted(before_files)}\n"
        f"after={sorted(after_files)}"
    )
    assert before_status == after_status, (
        f"git status changed\nbefore={before_status!r}\nafter={after_status!r}"
    )


# ── 7. committed patch: base must be the run's pre-implementer-ref ─────────
# The implementer may COMMIT its change. Then HEAD already contains both the
# fix AND the new test and `git status` is clean — so a base built from HEAD is
# the candidate itself, and the overlay copies nothing. Every such patch was
# refuted with `tests-do-not-exercise-change`. The base must instead be the
# run's `pre-implementer-ref` (the commit `execute.py` records *before* the
# implementer edits), with the candidate's test delta carried onto it.

def _write_ref(tmp_path: Path, run_id: str, sha: str) -> None:
    """Drop a run's `pre-implementer-ref` where the verifier looks for it."""
    rd = tmp_path / "mo-home" / "runs" / run_id
    rd.mkdir(parents=True, exist_ok=True)
    (rd / "pre-implementer-ref").write_text(sha + "\n")


def test_replay_base_is_pre_implementer_ref_for_committed_patch(tmp_path):
    """Committed fix + committed regression test → base = pre-implementer-ref;
    the overlay carries the committed test onto it → target PROVEN.

    Regression: the base was built from HEAD, which already held the committed
    fix and test, so base == candidate → empty overlap → spurious refute."""
    repo = _make_repo(
        tmp_path,
        mod_src="def add(a, b):\n    return a - b\n",
        test_src="from mod import add\n\ndef test_add_trivial():\n    assert add(0, 0) == 0\n",
    )
    pre = _git_text(repo, "rev-parse", "HEAD").strip()

    # Candidate COMMITS the fix and a new regression test that fails on buggy code.
    (repo / "mod.py").write_text("def add(a, b):\n    return a + b\n")
    tests_dir = repo / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_fix.py").write_text(
        "from mod import add\n\ndef test_fixed():\n    assert add(2, 3) == 5\n"
    )
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "fix + regression test")
    assert _git_text(repo, "status", "--porcelain").strip() == "", (
        "worktree must be clean (the implementer committed)"
    )

    _write_ref(tmp_path, "replay-committed", pre)

    out = _run_verifier(repo, tmp_path, replay="1", run_id="replay-committed")

    assert out["verifier"] == "test"
    assert out["pass"] is True, out
    assert "exercise" in out["error_summary"], out
    # The committed test crossed onto the pre-implementer base...
    assert out["replay"]["overlaid_tests"] == ["tests/test_fix.py"], out["replay"]
    # ...and it failed there (buggy source) while passing on the candidate.
    assert any("test_fixed" in tid for tid in out["replay"]["overlap"]), out["replay"]


def test_replay_base_ref_when_committed_but_tree_dirty(tmp_path):
    """Committed fix + a LEFTOVER uncommitted in-scope file → base is still the
    pre-implementer-ref, not the (post-patch) HEAD.

    Regression (ask-k2, 2026-10-08): the run's work reached HEAD via a recovery
    commit while one in-scope file stayed modified, so the tree was DIRTY. The
    old cleanliness gate read "dirty ⇒ HEAD is pre-patch" and built the base
    from HEAD — which already held the committed fix + tests — refuting a
    correct patch with ``tests-do-not-exercise-change``. Dirty does not imply
    HEAD is pre-patch when the ref is a strict ancestor of HEAD."""
    repo = _make_repo(
        tmp_path,
        mod_src="def add(a, b):\n    return a - b\n",
        test_src="from mod import add\n\ndef test_add_trivial():\n    assert add(0, 0) == 0\n",
    )
    # A tracked, in-scope, NON-test file whose later edit dirties the tree.
    (repo / "notes.txt").write_text("v1\n")
    _git(repo, "add", "notes.txt")
    _git(repo, "commit", "-q", "-m", "notes")
    pre = _git_text(repo, "rev-parse", "HEAD").strip()

    # Candidate COMMITS the fix + a regression test that fails on buggy code...
    (repo / "mod.py").write_text("def add(a, b):\n    return a + b\n")
    tests_dir = repo / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_fix.py").write_text(
        "from mod import add\n\ndef test_fixed():\n    assert add(2, 3) == 5\n"
    )
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "fix + regression test")
    # ...then leaves an in-scope file COMMITTED-adjacent dirty (the mixed state).
    (repo / "notes.txt").write_text("v2\n")
    assert _git_text(repo, "status", "--porcelain").strip() != "", (
        "worktree must be dirty for this regression"
    )

    _write_ref(tmp_path, "replay-mixed", pre)

    out = _run_verifier(repo, tmp_path, replay="1", run_id="replay-mixed")

    assert out["verifier"] == "test"
    assert out["pass"] is True, out
    assert "exercise" in out["error_summary"], out
    assert out["replay"]["overlaid_tests"] == ["tests/test_fix.py"], out["replay"]
    assert any("test_fixed" in tid for tid in out["replay"]["overlap"]), out["replay"]


def test_replay_committed_test_passing_on_base_not_proven(tmp_path):
    """Committed no-op + committed redundant test that ALSO passes on the base:
    the overlay must still refuse to call it proof (anti-gaming guard holds
    under the pre-implementer base too)."""
    repo = _make_repo(
        tmp_path,
        # Code is already correct at HEAD; the "patch" only adds a redundant test.
        mod_src="def add(a, b):\n    return a + b\n",
        test_src="from mod import add\n\ndef test_add():\n    assert add(2, 3) == 5\n",
    )
    pre = _git_text(repo, "rev-parse", "HEAD").strip()
    tests_dir = repo / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_extra.py").write_text(
        "from mod import add\n\ndef test_extra():\n    assert add(0, 0) == 0\n"
    )
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "no-op + redundant test")

    _write_ref(tmp_path, "replay-committed-noop", pre)

    out = _run_verifier(repo, tmp_path, replay="1", run_id="replay-committed-noop")

    assert out["verifier"] == "test"
    assert out["pass"] is False, out
    assert "tests-do-not-exercise-change" in out["error_summary"], out
    assert out["replay"]["overlap"] == [], out["replay"]


def test_overlay_carries_js_ts_test_paths(tmp_path):
    """A `__tests__/*.test.ts` path IS a test file: the overlay must carry it.

    The code-fix recipe runs on JS/TS repos (jest/vitest results-file runner),
    whose tests live under `__tests__/` as `*.test.ts`. The old `_is_test_path`
    was Python-only, so a TS repo's new tests never reached the base."""
    repo = _make_repo(
        tmp_path,
        mod_src="def add(a, b):\n    return a - b\n",
        test_src="from mod import add\n\ndef test_add():\n    assert add(2, 3) == 5\n",
    )
    (repo / "mod.py").write_text("def add(a, b):\n    return a + b\n")
    d = repo / "server" / "routes" / "__tests__"
    d.mkdir(parents=True)
    (d / "authorQuestions.test.ts").write_text("// ts regression test\n")

    out = _run_verifier(repo, tmp_path, replay="1", run_id="overlay-ts")

    assert out["verifier"] == "test"
    assert "replay" in out, out
    assert "server/routes/__tests__/authorQuestions.test.ts" in out["replay"]["overlaid_tests"], out["replay"]


# ── 8. base load failure of a candidate-ADDED module (defect #16) ───────────
# The overlay carries a candidate's new test onto the base, but a test that
# imports a module the candidate ADDED cannot load there: jest reports
# "Cannot find module '../x'" with ZERO per-test ids, so the base records only
# `<file>::<suite load failure>` — an id that never intersects the candidate's
# `<file>::<test name>` ids. Overlap stays empty and a correct "add module +
# test" patch is refuted. When the unresolved module resolves to a path the
# candidate ADDED (absent at the base ref), the load failure IS the exercise
# proof: those ids become base-failed and the patch is proven.

# A jest stand-in: it walks `**/*.test.ts`, resolves each test's first RELATIVE
# import against the tree, writes a jest-JSON results file (an assertion result,
# or — on a load failure — a `status: "failed"` suite with none), prints a
# jest-like FAIL transcript, and exits non-zero when any suite failed to load.
# Driven through `MINI_ORK_TEST_CMD="python3 runner.py"` so the verifier takes
# its results-file leg with no jest/node toolchain required.
TS_RUNNER = r'''import json, os, re, sys
from pathlib import Path

cwd = Path(os.getcwd())
files = sorted(p for p in cwd.rglob("*.test.ts") if "node_modules" not in p.parts)
test_results = []
any_failed = False
for p in files:
    rel = p.relative_to(cwd).as_posix()
    m = re.search(r"(?:from|import)\s*['\"](?P<s>\.[^'\"]+)['\"]", p.read_text())
    missing = None
    if m:
        spec = m.group("s")
        target = p.parent / spec
        if not any(Path(str(target) + e).exists() for e in ("", ".ts", ".tsx", "/index.ts")):
            missing = spec
    if missing is not None:
        print("FAIL " + rel)
        print("  ● Test suite failed to run")
        print()
        print("    Cannot find module '" + missing + "' from '" + rel + "'")
        test_results.append({"name": str(p), "status": "failed"})
        any_failed = True
    else:
        test_results.append({
            "name": str(p), "status": "passed",
            "assertionResults": [{"status": "passed", "fullName": "works the change"}],
        })
print("Test Suites: %d total" % len(test_results))
print("Tests:       %d total" % len(test_results))
res = os.environ.get("MINI_ORK_TEST_RESULTS_DIR")
if res:
    os.makedirs(res, exist_ok=True)
    Path(res, "jest-1.json").write_text(json.dumps({"testResults": test_results}))
sys.exit(1 if any_failed else 0)
'''

TS_CMD = "python3 runner.py"


def _make_ts_repo(tmp_path: Path, *, head_files: dict[str, str] | None = None) -> Path:
    """A throwaway TS-ish git repo whose `runner.py` jest stand-in is committed
    at HEAD (so it is present in the base worktree, not only the candidate)."""
    repo = tmp_path / "r"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    _git(repo, "config", "core.autocrlf", "false")
    (repo / "runner.py").write_text(TS_RUNNER)
    for rel, src in (head_files or {}).items():
        p = repo / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(src)
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "init")
    return repo


def _verifier_module(tmp_path: Path, monkeypatch):
    """Load the verifier script as a module so its helpers can be unit-tested.

    `MINI_ORK_HOME`/`MINI_ORK_RUN_ID` are redirected to `tmp_path` first: the
    module creates its log dir at import time."""
    monkeypatch.setenv("MINI_ORK_HOME", str(tmp_path / "verifier-import-home"))
    monkeypatch.setenv("MINI_ORK_RUN_ID", "unit-import")
    spec = importlib.util.spec_from_file_location("mo_codefix_verifier_ut", VERIFIER)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_base_load_failure_of_candidate_added_module_is_proof(tmp_path):
    """(a) Candidate adds src/x.ts + src/__tests__/x.test.ts importing it. The
    overlay carries the test onto the base, where the new module does not exist
    → the suite fails to LOAD ("Cannot find module '../x'") with no per-test
    ids. That load failure is the exercise proof → target PROVEN."""
    repo = _make_ts_repo(tmp_path)
    (repo / "src" / "__tests__").mkdir(parents=True)
    (repo / "src" / "x.ts").write_text("export const x = 1\n")
    (repo / "src" / "__tests__" / "x.test.ts").write_text("import { x } from '../x'\n")

    out = _run_verifier(repo, tmp_path, replay="1", run_id="loadfail-proof",
                        test_cmd=TS_CMD)

    assert out["verifier"] == "test"
    assert out["pass"] is True, out
    assert "exercise" in out["error_summary"], out
    # The new test crossed onto the base (overlay) and failed to load there.
    assert out["replay"]["overlaid_tests"] == ["src/__tests__/x.test.ts"], out["replay"]
    # Its candidate-passing id now counts as base-failed → non-empty overlap.
    assert out["replay"]["overlap"] == ["src/__tests__/x.test.ts::works the change"], out["replay"]
    # The audit block names the unresolved module and the added path it resolves to.
    proof = out["replay"]["load_failure_proof"]["src/__tests__/x.test.ts"]
    assert proof["missing_module"] == "../x", proof
    assert proof["added_path"] == "src/x.ts", proof


def test_base_load_failure_module_not_added_by_candidate_not_proven(tmp_path, monkeypatch):
    """(b) A base load failure naming a module the candidate did NOT add proves
    nothing — neither a relative module absent from the candidate diff nor a
    bare package (the bare base worktree has no node_modules)."""
    mod = _verifier_module(tmp_path, monkeypatch)
    repo = _make_ts_repo(tmp_path)
    (repo / "src" / "__tests__").mkdir(parents=True)
    (repo / "src" / "__tests__" / "x.test.ts").write_text("import '../y'\n")

    replay = {
        "candidate_passed": ["src/__tests__/x.test.ts::works the change"],
        "base_failed": ["src/__tests__/x.test.ts::<suite load failure>"],
        "overlap": [],
        "overlaid_tests": ["src/__tests__/x.test.ts"],
    }
    result = {"passed": False, "reason": "tests-do-not-exercise-change", "replay": replay}

    # The named relative module ('../y' → src/__tests__/y) is not in the diff.
    log_rel = tmp_path / "base-rel.log"
    log_rel.write_text(
        "FAIL src/__tests__/x.test.ts\n"
        "  ● Test suite failed to run\n\n"
        "    Cannot find module '../y' from 'src/__tests__/x.test.ts'\n"
    )
    assert mod._promote_base_load_failure(
        result, candidate_cwd=str(repo), base_ref="", base_log=str(log_rel),
    ) is None

    # A bare package resolves to no repo path → still not proof.
    log_pkg = tmp_path / "base-pkg.log"
    log_pkg.write_text(
        "FAIL src/__tests__/x.test.ts\n"
        "  ● Test suite failed to run\n\n"
        "    Cannot find module 'react' from 'src/__tests__/x.test.ts'\n"
    )
    assert mod._promote_base_load_failure(
        result, candidate_cwd=str(repo), base_ref="", base_log=str(log_pkg),
    ) is None


def test_base_suite_loads_and_passes_not_proven(tmp_path):
    """(c) When the base CAN load and pass the overlaid test — it imports a
    module that already exists at the base ref — there is no load failure and
    the patch stays not-proven."""
    repo = _make_ts_repo(tmp_path, head_files={"src/mod.ts": "export const m = 1\n"})
    (repo / "src" / "__tests__").mkdir(parents=True)
    (repo / "src" / "__tests__" / "x.test.ts").write_text("import { m } from '../mod'\n")

    out = _run_verifier(repo, tmp_path, replay="1", run_id="loadfail-notproven",
                        test_cmd=TS_CMD)

    assert out["verifier"] == "test"
    assert out["pass"] is False, out
    assert "tests-do-not-exercise-change" in out["error_summary"], out
    assert out["replay"]["overlap"] == [], out["replay"]