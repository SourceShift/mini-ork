"""End-to-end tests for ``replay_check``'s structured (non-pytest) runners.

No node and no real jest/vitest install required. A fake ``jest`` executable
is dropped onto a tmp PATH; it reads ``--outputFile=``, inspects ``add.js`` in
its cwd, and writes jest-JSON — the same contract a real jest satisfies. A
bash gate script exercises the results-file contract by writing JUnit XML to
``$MINI_ORK_TEST_RESULTS_DIR``.
"""
from __future__ import annotations

import os
from pathlib import Path

from mini_ork.certify import replay_check


# A buggy `add.js` contains `a - b`; the fake jest reports the test failed.
# The fixed tree contains `a + b`; the fake jest reports it passed.
FAKE_JEST = """#!/usr/bin/env python3
import json
import os
import sys

out = None
for arg in sys.argv[1:]:
    if arg.startswith("--outputFile="):
        out = arg.split("=", 1)[1]

src = open(os.path.join(os.getcwd(), "add.js")).read()
status = "failed" if "a - b" in src else "passed"
result = {
    "testResults": [
        {
            "name": os.path.join(os.getcwd(), "add.test.js"),
            "status": status,
            "assertionResults": [{"fullName": "add works", "status": status}],
        }
    ]
}
if out:
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w") as fh:
        fh.write(json.dumps(result))
else:
    print(json.dumps(result))
"""

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


def _install_fake_jest(tmp_path: Path) -> Path:
    """Write an executable `jest` shim into a fresh bin dir and return it."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "jest").write_text(FAKE_JEST)
    (bindir / "jest").chmod(0o755)
    return bindir


def _patch_path(monkeypatch, bindir: Path) -> None:
    monkeypatch.setenv("PATH", str(bindir) + os.pathsep + os.environ.get("PATH", ""))


def _make_trees(tmp_path: Path, *, base_buggy: bool) -> tuple[Path, Path]:
    """Two worktrees: base (buggy or fixed) and candidate (always fixed)."""
    base = tmp_path / "base"
    cand = tmp_path / "cand"
    base.mkdir()
    cand.mkdir()
    (base / "add.js").write_text(
        "function add(a, b) { return a - b; }\n" if base_buggy
        else "function add(a, b) { return a + b; }\n"
    )
    (cand / "add.js").write_text("function add(a, b) { return a + b; }\n")
    return base, cand


# ── 4. fail-to-pass proven ───────────────────────────────────────────────────


def test_jest_fail_to_pass_is_proven(tmp_path, monkeypatch):
    base, cand = _make_trees(tmp_path, base_buggy=True)
    _patch_path(monkeypatch, _install_fake_jest(tmp_path))

    result = replay_check("jest add.test.js", base_cwd=str(base), candidate_cwd=str(cand))

    assert result["passed"] is True, result
    assert result["unverified"] is False
    assert result["replay"]["runner"] == "jest"
    assert result["replay"]["overlap"] == ["add.test.js::add works"]


# ── 5. not exercised ─────────────────────────────────────────────────────────


def test_jest_not_exercised(tmp_path, monkeypatch):
    base, cand = _make_trees(tmp_path, base_buggy=False)
    _patch_path(monkeypatch, _install_fake_jest(tmp_path))

    result = replay_check("jest add.test.js", base_cwd=str(base), candidate_cwd=str(cand))

    assert result["passed"] is False, result
    assert result["reason"] == "tests-do-not-exercise-change"
    assert result["unverified"] is False
    assert result["replay"]["runner"] == "jest"
    assert result["replay"]["overlap"] == []


# ── 6. results-file contract ─────────────────────────────────────────────────


def test_results_file_contract(tmp_path, monkeypatch):
    base, cand = _make_trees(tmp_path, base_buggy=True)
    for tree in (base, cand):
        gate = tree / "gate.sh"
        gate.write_text(GATE_SH)
        gate.chmod(0o755)

    result = replay_check("bash gate.sh", base_cwd=str(base), candidate_cwd=str(cand))

    assert result["passed"] is True, result
    assert result["unverified"] is False
    assert result["replay"]["runner"] == "results-file"
    assert result["replay"]["overlap"] == ["add.test.js::add works"]


# ── 7. not applicable ────────────────────────────────────────────────────────


def test_no_adapter_no_results_is_not_applicable(tmp_path):
    base, cand = _make_trees(tmp_path, base_buggy=True)

    result = replay_check("bash -c 'exit 0'", base_cwd=str(base), candidate_cwd=str(cand))

    assert result["unverified"] is True
    assert result["applicable"] is False
    assert result["replay"] is None
    assert "pytest" in result["reason"]


# ── 8. opaque fallback (no adapter, no results file) ─────────────────────────
#
# A wrapper script / `make test` / `go test ./...` has no per-test adapter and
# writes no results file. Refusing every such command made the instrument
# unusable outside pytest and jest (the reported defect: a jest wrapper exited 0
# on the candidate and the replay still said "unverified"). The fallback judges
# by exit code, and only when BOTH sides prove tests actually ran.

OPAQUE_SH = """#!/bin/bash
if grep -q 'a - b' add.js; then
  echo "1 failed, 1 passed"
  exit 1
fi
echo "1 passed"
exit 0
"""


def _install_opaque_runner(tree: Path, name: str, body: str) -> None:
    script = tree / name
    script.write_text(body)
    script.chmod(0o755)


def test_opaque_command_rc_delta_is_proven(tmp_path):
    base, cand = _make_trees(tmp_path, base_buggy=True)
    for tree in (base, cand):
        _install_opaque_runner(tree, "run-tests.sh", OPAQUE_SH)

    result = replay_check("bash run-tests.sh", base_cwd=str(base), candidate_cwd=str(cand))

    assert result["passed"] is True, result
    assert result["unverified"] is False
    assert result["replay"]["runner"] == "opaque"
    assert result["replay"]["candidate_rc"] == 0
    assert result["replay"]["base_rc"] != 0


def test_opaque_command_without_a_test_marker_is_never_a_pass(tmp_path):
    """A silent exit-0 stub must not beat a silent exit-0 base into a PASS."""
    base, cand = _make_trees(tmp_path, base_buggy=True)
    for tree in (base, cand):
        _install_opaque_runner(tree, "noop.sh", "#!/bin/bash\nexit 0\n")

    result = replay_check("bash noop.sh", base_cwd=str(base), candidate_cwd=str(cand))

    assert result["passed"] is False, result
    assert result["unverified"] is True
    assert result["applicable"] is False


def test_opaque_unrunnable_baseline_is_not_a_delta(tmp_path):
    """The base worktree has no node_modules: rc 127 is "never ran", not "red".

    Candidate passes (marker printed, rc 0), base cannot run (rc 127). Without
    the runnability guard that pair looks exactly like a legitimate delta.
    """
    base, cand = _make_trees(tmp_path, base_buggy=True)
    body = "#!/bin/bash\necho '1 passed'\nexit 0\n"
    _install_opaque_runner(cand, "run-tests.sh", body)

    result = replay_check("bash run-tests.sh", base_cwd=str(base), candidate_cwd=str(cand))

    assert result["passed"] is False, result
    assert result["unverified"] is True
    assert "could not run" in result["reason"]


# ── 9. go test -json adapter (no go toolchain required) ──────────────────────
#
# A repo with no pytest entrypoint may still run `go test`. The adapter
# augments the command with `-json` and parses the event stream back from the
# run log (go has no output-file flag). The fake `go` shim prints the same
# NDJSON a real `go test -json` emits.

FAKE_GO = """#!/usr/bin/env python3
import json
import os
import sys

args = sys.argv[1:]
if "test" not in args:
    sys.exit(0)  # only `go test` is exercised here

src = open(os.path.join(os.getcwd(), "add.js")).read()
buggy = "a - b" in src
pkg = "example.com/m"


def emit(action, test=None):
    ev = {"Action": action, "Package": pkg, "Elapsed": 0}
    if test:
        ev["Test"] = test
    print(json.dumps(ev))


emit("run", "TestAdd")
if buggy:
    emit("fail", "TestAdd")
    emit("fail")
    sys.exit(1)
emit("pass", "TestAdd")
emit("pass")
sys.exit(0)
"""

# Base fails to BUILD (no per-test outcome) while the candidate passes: the
# only base signal is a build-fail, which is a load failure, not an assertion.
FAKE_GO_BUILD_FAIL = """#!/usr/bin/env python3
import json
import os
import sys

src = open(os.path.join(os.getcwd(), "add.js")).read()
buggy = "a - b" in src
pkg = "example.com/m"
if buggy:
    print(json.dumps({"Action": "build-fail", "ImportPath": pkg}))
    sys.exit(1)
print(json.dumps({"Action": "pass", "Package": pkg, "Test": "TestAdd"}))
print(json.dumps({"Action": "pass", "Package": pkg}))
sys.exit(0)
"""

FAKE_CARGO = """#!/usr/bin/env python3
import json
import os
import sys

src = open(os.path.join(os.getcwd(), "add.js")).read()
buggy = "a - b" in src
print(json.dumps({"type": "suite", "event": "started", "test_count": 1}))
print(json.dumps({
    "type": "test",
    "event": "failed" if buggy else "ok",
    "name": "tests::test_add",
}))
sys.exit(1 if buggy else 0)
"""


def _install_shim(bindir: Path, name: str, body: str) -> None:
    shim = bindir / name
    shim.write_text(body)
    shim.chmod(0o755)


def test_go_test_json_fail_to_pass_is_proven(tmp_path, monkeypatch):
    base, cand = _make_trees(tmp_path, base_buggy=True)
    bindir = tmp_path / "bin"
    bindir.mkdir()
    _install_shim(bindir, "go", FAKE_GO)
    _patch_path(monkeypatch, bindir)

    result = replay_check("go test ./...", base_cwd=str(base), candidate_cwd=str(cand))

    assert result["passed"] is True, result
    assert result["unverified"] is False
    assert result["weak"] is False, result  # a real failing assertion
    assert result["replay"]["runner"] == "go"
    assert result["replay"]["overlap"] == ["example.com/m::TestAdd"]


def test_go_build_failure_on_base_is_not_a_pass(tmp_path, monkeypatch):
    """A base that cannot build yields only a load-failure id, which never
    overlaps a per-test pass — the replay must NOT mint a pass from it."""
    base, cand = _make_trees(tmp_path, base_buggy=True)
    bindir = tmp_path / "bin"
    bindir.mkdir()
    _install_shim(bindir, "go", FAKE_GO_BUILD_FAIL)
    _patch_path(monkeypatch, bindir)

    result = replay_check("go test ./...", base_cwd=str(base), candidate_cwd=str(cand))

    assert result["passed"] is False, result
    assert result["replay"]["overlap"] == [], result["replay"]
    assert result["replay"]["base_failed"] == ["example.com/m::<suite load failure>"], result


def test_cargo_test_json_fail_to_pass_is_proven(tmp_path, monkeypatch):
    base, cand = _make_trees(tmp_path, base_buggy=True)
    bindir = tmp_path / "bin"
    bindir.mkdir()
    _install_shim(bindir, "cargo", FAKE_CARGO)
    _patch_path(monkeypatch, bindir)

    result = replay_check("cargo test", base_cwd=str(base), candidate_cwd=str(cand))

    assert result["passed"] is True, result
    assert result["unverified"] is False
    assert result["replay"]["runner"] == "cargo"
    assert result["replay"]["overlap"] == ["tests::test_add"]
