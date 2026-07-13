"""Crucible — the verified-execution seam (mini_ork/runtime/).

These tests pin down the two things Crucible exists to get right:

  1. The VERDICT TAXONOMY. `failed` (a real reproduction) and `error` (a broken
     environment) are different facts. Collapsing them is what made the verifier
     net-negative before PR #170: a patch was blamed for an environment it did not break.

  2. The BACKEND SEAM. `verifiers` is an optional dependency. With it we run through their
     Runtime protocol (docker/subprocess/prime/modal); without it we drive the docker CLI.
     Callers must not be able to tell the difference.

The end-to-end container test is opt-in (it needs a docker daemon and pulls an image), so
CI stays fast. Run it with:  MO_TEST_CRUCIBLE_E2E=1 pytest tests/unit/test_crucible_runtime.py
"""
import os
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from mini_ork.runtime import Crucible, ExecOutcome, RuntimeSpec  # noqa: E402
from mini_ork.runtime.engine import _have_verifiers, available_backends  # noqa: E402


# ── the verdict taxonomy ─────────────────────────────────────────────────────
# `_classify` is pure: it reads a container's combined output and decides what happened.
# It is the whole verdict, and no model is consulted anywhere in it.

def test_green_test_is_passed():
    o = Crucible._classify("1 passed in 0.42s\nCRUCIBLE_RC=0")
    assert o.status == "passed"
    assert o.passed and o.informative and o.ran


def test_assertion_failure_is_a_real_reproduction():
    """A failing assertion means the test RAN and saw the bug. That is a fact worth acting on."""
    o = Crucible._classify("E   assert add(2, 3) == 5\n1 failed in 0.31s\nCRUCIBLE_RC=1")
    assert o.status == "failed"
    assert o.ran
    assert o.informative       # we may act on this
    assert not o.passed


def test_a_test_with_a_nameerror_is_a_defect_not_a_reproduction():
    """THE false-reject that motivated `test_defect`, verbatim from a real run.

    A generated probe used `exp_polar` without importing it. pytest printed
    "FAILED ... - NameError", the word "failed" matched, and the oracle concluded
    "the bug is NOT fixed" — about a patch that fixed it perfectly.

    A test that dies on an undefined name never exercised the code at all.
    """
    o = Crucible._classify(
        "FAILED crucible_probe.py::test_polylog - NameError: name 'exp_polar' is not defined\n"
        "1 failed in 0.9s\nCRUCIBLE_RC=1"
    )
    assert o.status == "test_defect"
    assert o.test_is_broken
    assert not o.informative      # says NOTHING about the patch
    assert not o.ran


@pytest.mark.parametrize("exc", [
    "NameError: name 'foo' is not defined",
    "ImportError: cannot import name 'bar'",
    "ModuleNotFoundError: No module named 'baz'",
    "SyntaxError: invalid syntax",
    "IndentationError: unexpected indent",
    "fixture 'tmpdir_factory' not found",
])
def test_every_test_defect_is_uninformative(exc):
    o = Crucible._classify(f"E   {exc}\n1 failed in 0.1s\nCRUCIBLE_RC=1")
    assert o.status == "test_defect", exc
    assert not o.informative, exc


@pytest.mark.parametrize("exc", ["AttributeError", "TypeError", "ValueError", "KeyError"])
def test_ambiguous_exceptions_are_NOT_called_test_defects(exc):
    """Deliberately narrow. A library bug can legitimately raise AttributeError or
    TypeError, so a probe that catches one may be a TRUE reproduction. We only claim the
    cases where the test is unambiguously at fault — over-claiming here would silently
    discard real bug reports."""
    o = Crucible._classify(f"E   {exc}: something\n1 failed in 0.1s\nCRUCIBLE_RC=1")
    assert o.status == "failed"
    assert o.informative


def test_a_bare_assert_is_reported_as_an_AssertionError():
    """pytest renders a bare `assert x == y` as `FAILED t.py::t - assert 3 == 5` — the word
    "assert" sits exactly where the exception name goes.

    Reading that literally makes a genuine assertion failure look like an unknown exception.
    Measured cost: a caller keying on AssertionError to confirm "this probe really did
    reproduce the reported bug" discarded a perfectly good reproduction and abstained.
    """
    o = Crucible._classify("FAILED probe.py::test_polylog - assert 3 == 5\n1 failed\nCRUCIBLE_RC=1")
    assert o.status == "failed"
    assert o.exc == "AssertionError"


def test_the_exception_that_caused_the_failure_is_reported():
    """`status` alone cannot answer the question that matters: did the test fail for the
    reason it was WRITTEN for? A probe asserting an expected value should fail with an
    AssertionError. One that dies on a ValueError inside the library's own setup never
    reached its assertion — it is red, but it is not a reproduction."""
    assert Crucible._classify(
        "E   AssertionError: assert 3 == 5\n1 failed\nCRUCIBLE_RC=1").exc == "AssertionError"
    assert Crucible._classify(
        "E   ValueError: Could not determine celestial frame\n1 failed\nCRUCIBLE_RC=1").exc == "ValueError"
    assert Crucible._classify(
        "E   astropy.utils.iers.iers.IERSWarning: failed to download\n1 failed\nCRUCIBLE_RC=1"
    ).exc == "IERSWarning"           # dotted paths reduce to the bare name
    assert Crucible._classify("1 passed\nCRUCIBLE_RC=0").exc == ""


def test_broken_environment_is_error_not_failure():
    """THE distinction PR #170 turns on.

    A collection error means the test never ran. The patch is not to blame, and a gate that
    treats this as a failure will reject correct work — the false-reject that made the old
    absolute-green verifier net-negative.

    (Import errors are NOT here: they land in `test_defect`, because they are usually the
    test's own fault and are worth one repair attempt before abstaining. Either way the
    `informative` guard blocks them — see below.)
    """
    o = Crucible._classify("INTERNALERROR> pytest could not collect crucible_probe.py\nCRUCIBLE_RC=3")
    assert o.status == "error"
    assert not o.ran
    assert not o.informative   # we may NOT act on this — it says nothing about the patch


def test_no_uninformative_outcome_can_ever_be_read_as_a_verdict():
    """The whole taxonomy, stated as one invariant: only two of six statuses are evidence
    about the patch. Everything else is a fact about the harness."""
    for s in ("test_defect", "error", "apply_fail", "no_run"):
        assert not ExecOutcome(status=s).informative, s
    for s in ("passed", "failed"):
        assert ExecOutcome(status=s).informative, s


def test_unapplied_patch_is_apply_fail_not_failure():
    """77% of model-authored diffs are rejected by `git apply`. That is a diff-format
    problem, not a wrong-answer problem, and it must not be scored as one."""
    o = Crucible._classify("CRUCIBLE_APPLY_FAIL")
    assert o.status == "apply_fail"
    assert not o.ran and not o.informative


def test_missing_sentinel_is_no_run():
    """No RC sentinel means the script never got far enough to emit one."""
    o = Crucible._classify("docker: connection refused")
    assert o.status == "no_run"
    assert not o.informative


def test_only_passed_and_failed_are_informative():
    """The `informative` guard is the single place that decides whether an outcome may be
    used as evidence at all. Nothing else in the taxonomy may leak through it."""
    informative = {s for s in ("passed", "failed", "error", "apply_fail", "no_run")
                   if ExecOutcome(status=s).informative}
    assert informative == {"passed", "failed"}


def test_apply_fail_wins_over_a_stale_rc():
    """If the patch did not apply, nothing after it means anything — even if the container
    echoed an RC from a previous step."""
    o = Crucible._classify("CRUCIBLE_APPLY_FAIL\nCRUCIBLE_RC=0")
    assert o.status == "apply_fail"


# ── the backend seam ─────────────────────────────────────────────────────────

def test_auto_prefers_verifiers_and_falls_back_to_the_cli():
    """`auto` must never fail: it takes the verifiers runtime if installed, the docker CLI
    if not. mini-ork stays zero-dependency by default."""
    expected = "docker" if _have_verifiers() else "docker-cli"
    assert Crucible(RuntimeSpec(image="x")).backend == expected


def test_docker_cli_backend_never_needs_verifiers():
    """The zero-dependency path must be selectable explicitly and must not raise."""
    assert Crucible(RuntimeSpec(image="x", backend="docker-cli")).backend == "docker-cli"


def test_unknown_backend_is_rejected_loudly():
    with pytest.raises(ValueError, match="unknown backend"):
        Crucible(RuntimeSpec(image="x", backend="wishful"))


@pytest.mark.skipif(not _have_verifiers(), reason="verifiers not installed")
def test_cloud_backends_are_selectable():
    """The point of going through their Runtime protocol rather than shelling out: the same
    probe runs in the cloud by changing one field."""
    for b in ("prime", "modal", "subprocess"):
        assert Crucible(RuntimeSpec(image="x", backend=b)).backend == b
    assert set(available_backends()) >= {"docker", "prime", "modal", "subprocess"}


def test_a_runtime_that_never_started_reports_no_run_not_a_failure():
    """A dead runtime must not be mistaken for a dead patch."""
    c = Crucible(RuntimeSpec(image="x", backend="docker-cli"))
    assert not c.up
    o = c.run_test("def test_x(): assert True")
    assert o.status == "no_run"
    assert not o.informative


# ── end-to-end (opt-in: needs a docker daemon) ───────────────────────────────

@pytest.mark.skipif(
    os.environ.get("MO_TEST_CRUCIBLE_E2E") != "1",
    reason="set MO_TEST_CRUCIBLE_E2E=1 to run the container test",
)
def test_end_to_end_base_red_gold_green_garbage_rejected():
    """The whole point, in one test: a real container, a real bug, a real fix.

    base   -> failed      the bug reproduces
    gold   -> passed      the fix works
    junk   -> apply_fail  the diff is nonsense and we say so, rather than scoring it
    """
    TEST = "from calc import add\ndef test_add():\n    assert add(2, 3) == 5\n"
    GOLD = (
        "--- a/calc.py\n+++ b/calc.py\n@@ -1,2 +1,2 @@\n"
        " def add(a, b):\n-    return a - b\n+    return a + b\n"
    )
    GARBAGE = "--- a/nope.py\n+++ b/nope.py\n@@ -9,9 +9,9 @@\n-not real\n+nope\n"

    with Crucible(RuntimeSpec(image="python:3.11-slim", timeout_s=240)) as c:
        assert c.up, "runtime did not start"
        c._exec(
            "apt-get -qq update >/dev/null 2>&1; apt-get -qq install -y git >/dev/null 2>&1; "
            "mkdir -p /testbed && cd /testbed && git init -q . && "
            "printf 'def add(a, b):\\n    return a - b\\n' > calc.py && "
            "git add -A && git -c user.email=a@b -c user.name=c commit -qm base"
        )
        assert c.run_test(TEST).status == "failed"                  # reproduces
        assert c.run_test(TEST, patch=GOLD).status == "passed"      # fixed
        assert c.run_test(TEST, patch=GARBAGE).status == "apply_fail"
