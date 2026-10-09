"""The code-fix verifier decides replay applicability BEFORE any base build.

Two live incidents (2026-10-08) motivated this: a build-only command
(``MINI_ORK_TEST_CMD="script/mini-ork-build"``) made the delta-gate replay run a
COLD base build in a temp worktree (7 GB / 20 min, disk at 92%), and the
``MO_CODEFIX_REPLAY=0`` workaround returned before applicability was recorded,
so ``verifier_test.json`` lacked ``replay_applicable`` and ``levels.py`` mapped
the run to ``target: not proven`` → the publisher abstained → the run ``failed``.

Applicability is a property of the command and of what its post-patch run
produced; it never needs a base run. The verifier now decides it right after a
green post-patch run, before any ``git worktree add`` and before the opt-out
check, and emits exactly the payload the late twin in ``mini_ork/certify/
oracle.py`` emitted — so ``mini_ork.verify.levels`` still yields ``target: n/a``.

Like the sibling suites, each test stands up a throwaway git repo and drives
the REAL verifier as a subprocess (the payload is the contract; a stubbed
function return would not exercise the emission). The verifier late-imports
``mini_ork.certify``, so ``PYTHONPATH`` points at the worktree root.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
VERIFIER = REPO_ROOT / "recipes" / "code-fix" / "verifiers" / "test.py"
sys.path.insert(0, str(REPO_ROOT))

from mini_ork.verify import levels as L
from mini_ork.verify.behavioral import PROVEN, UNVERIFIED

# A command that exits 0 and writes nothing: no pytest/jest/vitest runner and
# no results file, so the replay instrument does not apply.
BUILD_ONLY_CMD = "true"

# A gate script that satisfies the results-file contract: it writes JUnit XML
# into ``$MINI_ORK_TEST_RESULTS_DIR`` and reports the ``add.js`` bug. This is
# the same contract `tests/unit/test_replay_structured_runners.py` exercises.
GATE_SH = """#!/bin/bash
dir="$MINI_ORK_TEST_RESULTS_DIR"
if grep -q 'a - b' add.js; then
  body='<failure message="bug"/>'
else
  body=''
fi
printf '<testsuites><testsuite name="s"><testcase classname="add.test.js" name="add works">%s</testcase></testsuite></testsuites>\\n' "$body" > "$dir/results.xml"
exit 0
"""

PYTEST_CMD = f"{sys.executable} -m pytest -p no:cacheprovider"


def _git(cwd: Path, *args: str) -> None:
    subprocess.check_call(
        ["git", *args], cwd=cwd,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )


def _make_repo(parent: Path, files: dict[str, str]) -> Path:
    """Stand up a throwaway git repo with ``files`` committed at HEAD."""
    repo = parent / "r"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    _git(repo, "config", "core.autocrlf", "false")
    for rel, content in files.items():
        path = repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "init")
    return repo


def _worktrees(repo: Path) -> list[str]:
    """The `git worktree list --porcelain` lines (one worktree = one entry)."""
    out = subprocess.check_output(
        ["git", "worktree", "list", "--porcelain"], cwd=repo, text=True,
    )
    return [ln for ln in out.splitlines() if ln.startswith("worktree ")]


def _run_verifier(
    repo: Path, tmp_path: Path, *, cmd: str, replay: str = "1", run_id: str,
) -> tuple[int, dict]:
    """Invoke the verifier on `repo`. Returns (exit_code, parsed JSON envelope)."""
    mini_home = tmp_path / "mo-home"
    mini_home.mkdir(exist_ok=True)
    env = os.environ.copy()
    env["MINI_ORK_HOME"] = str(mini_home)
    env["MINI_ORK_RUN_ID"] = run_id
    env["MO_CODEFIX_REPLAY"] = replay
    env["MO_SUITE_ADEQUACY"] = "0"  # these tests are about applicability, not adequacy
    env["MINI_ORK_TEST_CMD"] = cmd
    # The verifier late-imports `mini_ork.certify`; run as a script it needs the
    # worktree root on sys.path.
    env["PYTHONPATH"] = str(REPO_ROOT)
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
    return proc.returncode, json.loads(last[-1])


def _write_evidence_and_levels(tmp_path: Path, run_id: str, payload: dict) -> dict:
    """Persist `payload` as `verifier_test.json` and derive the level vector."""
    run_dir = tmp_path / "mo-home" / "runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "verifier_test.json").write_text(json.dumps(payload))
    vector, _ = L.derive_levels(str(run_dir))
    return vector


# ── 1. build-only command → n/a, no base build ──────────────────────────────
def test_build_only_command_is_not_applicable_without_a_base_build(tmp_path):
    """A green build-only command writes no results file: the replay does not
    apply, so the verifier returns the not-applicable abstention BEFORE any
    base run (the disk incident) — and `levels.py` still yields `target: n/a`
    so publishing is not blocked.

    The disk incident is locked in by the *invocation count*, not by
    `git worktree list`: the verifier removes its base worktree in a `finally`
    block, so that list is identical before and after either way. A build-only
    command that appends its cwd to a file is run ONCE, in the candidate tree,
    by this verifier; the late-deciding twin runs it three times (post-patch,
    replay candidate, cold base build in a temp worktree). The replay logs are
    asserted absent for the same reason.
    """
    repo = _make_repo(tmp_path, {"mod.py": "def add(a, b):\n    return a + b\n"})
    # Green, no runner, no results file, and it records each invocation by
    # appending its cwd. The path is RELATIVE on purpose: an absolute path under
    # `tmp_path` embeds pytest's own `pytest-of-admin` segment, which
    # `detect_runners`' substring check would misread as a pytest command.
    calls = repo / "calls.txt"
    cmd = "pwd >> calls.txt"

    before = _worktrees(repo)
    rc, out = _run_verifier(repo, tmp_path, cmd=cmd, run_id="early-na")
    after = _worktrees(repo)

    assert rc == 0, out
    assert out["pass"] is False, out
    assert out["status"] == "unverified", out
    assert out["suite_green"] is True, out
    assert out["replay_unverified"] is True, out
    assert out["replay_applicable"] is False, out
    assert "replay" not in out, out

    # The command ran exactly once, in the candidate tree: the early decision
    # short-circuited before the replay ran it again as its candidate, let alone
    # on a rebuilt base. A late-deciding verifier appends twice here (post-patch
    # + replay candidate) and once more in a throwaway base worktree.
    ran_in = calls.read_text().splitlines()
    assert len(ran_in) == 1, (
        f"the command ran {len(ran_in)} times {ran_in!r}: the replay ran on a "
        f"rebuilt base (the disk incident)"
    )
    assert os.path.realpath(ran_in[0]) == os.path.realpath(str(repo)), ran_in

    # No replay artifacts: the audit trail of a base build that never happened.
    run_dir = tmp_path / "mo-home" / "runs" / "early-na"
    for name in ("verifier_replay_candidate.log", "verifier_replay_base.log"):
        assert not (run_dir / name).exists(), f"a replay ran: {name} was written"

    assert before == after, f"a base worktree was created: {before} -> {after}"

    vector = _write_evidence_and_levels(tmp_path, "early-na", out)
    assert vector["target"] == L.NA
    assert vector["executes"] == PROVEN, vector


# ── 2. build-only command + opt-out → still n/a ─────────────────────────────
def test_build_only_command_opt_out_is_still_not_applicable(tmp_path):
    """`MO_CODEFIX_REPLAY=0` no longer hides applicability: the early decision
    sits BEFORE the opt-out check, so a non-applicable command still records
    `replay_applicable: false` (the false-withhold incident)."""
    repo = _make_repo(tmp_path, {"mod.py": "def add(a, b):\n    return a + b\n"})

    before = _worktrees(repo)
    rc, out = _run_verifier(
        repo, tmp_path, cmd=BUILD_ONLY_CMD, replay="0", run_id="early-na-optout",
    )
    after = _worktrees(repo)

    assert rc == 0, out
    assert out["pass"] is False, out
    assert out["replay_applicable"] is False, out
    assert "replay" not in out, out
    assert before == after, f"a base worktree was created: {before} -> {after}"

    vector = _write_evidence_and_levels(tmp_path, "early-na-optout", out)
    assert vector["target"] == L.NA


# ── 3. results file written → applicable, replay path entered ───────────────
def test_results_file_makes_the_command_applicable(tmp_path):
    """A non-runner command that writes into `$MINI_ORK_TEST_RESULTS_DIR` is
    treated as applicable: the verifier enters the replay path (the `replay`
    payload proves `_run_replay_check` ran) instead of short-circuiting to n/a.
    A buggy base + fixed candidate gate yields a real fail-to-pass."""
    repo = _make_repo(tmp_path, {
        "add.js": "function add(a, b) { return a - b; }\n",
        "gate.sh": GATE_SH,
    })
    # The fix lives in the working tree; HEAD keeps the buggy base.
    (repo / "add.js").write_text("function add(a, b) { return a + b; }\n")

    rc, out = _run_verifier(repo, tmp_path, cmd="bash gate.sh", run_id="results-applicable")

    assert rc == 0, out
    assert out["pass"] is True, out
    assert "replay" in out, out
    assert out["replay"]["runner"] == "results-file", out["replay"]
    assert out["replay"]["overlap"] == ["add.test.js::add works"], out["replay"]


def test_stray_log_in_results_dir_is_not_a_results_file(tmp_path):
    """Only a file the oracle can PARSE counts: a log dropped into
    `$MINI_ORK_TEST_RESULTS_DIR` must not trigger the base build (it would end
    n/a anyway after paying for it)."""
    repo = _make_repo(tmp_path, {"mod.py": "def add(a, b):\n    return a + b\n"})
    cmd = 'echo build ok > "$MINI_ORK_TEST_RESULTS_DIR/build.log"'

    before = _worktrees(repo)
    rc, out = _run_verifier(repo, tmp_path, cmd=cmd, run_id="stray-log")
    after = _worktrees(repo)

    assert rc == 0, out
    assert out["replay_applicable"] is False, out
    assert before == after, f"a base worktree was created: {before} -> {after}"


# ── 4. pytest + opt-out → today's green pass, no applicability recorded ──────
def test_pytest_opt_out_keeps_the_green_pass(tmp_path):
    """The opt-out keeps its meaning where the replay applies: a pytest suite
    with `MO_CODEFIX_REPLAY=0` certifies green as today, emits no
    `replay_applicable` key, and leaves `target` UNVERIFIED (an honest abstain,
    not a publish)."""
    repo = _make_repo(tmp_path, {
        "mod.py": "def add(a, b):\n    return a - b\n",
        "test_mod.py": "from mod import add\n\ndef test_add():\n    assert add(2, 3) == 5\n",
    })
    (repo / "mod.py").write_text("def add(a, b):\n    return a + b\n")

    rc, out = _run_verifier(
        repo, tmp_path, cmd=PYTEST_CMD, replay="0", run_id="pytest-optout",
    )

    assert rc == 0, out
    assert out["pass"] is True, out
    assert "opt-out" in out["error_summary"], out
    assert "replay" not in out, out
    assert "replay_applicable" not in out, out

    vector = _write_evidence_and_levels(tmp_path, "pytest-optout", out)
    assert vector["target"] == UNVERIFIED


def test_opaque_runner_with_test_markers_stays_applicable(tmp_path):
    """An adapter-less command whose output proves tests ran (`Ran 1 test`)
    is the oracle's opaque exit-code instrument (ad507150): applicable, so the
    early check must NOT short-circuit it to n/a."""
    import importlib.util

    spec = importlib.util.spec_from_file_location("cf_test_verifier", VERIFIER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    log = tmp_path / "post.log"
    log.write_text("....\n----------------------------------------------------------------------\nRan 4 tests in 0.01s\n\nOK\n")
    assert mod._replay_applies("python -m unittest", None, str(log)) is True
    build = tmp_path / "build.log"
    build.write_text("   Compiling mini_ork_ui v0.1.0\n    Finished `release-fast` profile in 6m 19s\n")
    assert mod._replay_applies("script/mini-ork-build", None, str(build)) is False


def test_go_and_cargo_test_are_applicable_without_a_base_build():
    """`go test` / `cargo test` now have structured adapters, so applicability
    is decidable from the command alone (before any base run) — while a
    build-only invocation stays n/a and never triggers a cold base build."""
    import importlib.util

    spec = importlib.util.spec_from_file_location("cf_test_verifier", VERIFIER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    assert mod._replay_applies("go test ./...", None, None) is True
    assert mod._replay_applies("cargo test --all", None, None) is True
    # Build tools first: these are NOT test runs.
    assert mod._replay_applies("go build ./...", None, None) is False
    assert mod._replay_applies("cargo build --release", None, None) is False
