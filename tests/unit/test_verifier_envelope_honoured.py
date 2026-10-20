"""RB-02a: a verifier's own ``pass: false`` must fail its node.

``_run_verifier_ref`` merges a verifier's stdout+stderr into one evidence file.
A warning line before the JSON envelope (tarfile's DeprecationWarning on 3.13)
made the strict ``json.load`` fail, and because recipe verifiers always exit 0
the failed gate came back as a pass. These tests drive the real seam against
hermetic script bodies written under ``tmp_path``.
"""
import ast
import textwrap
from pathlib import Path

import pytest

import mini_ork
from mini_ork.cli.execute import _run_verifier_ref
from mini_ork.verify import levels

REPO_ROOT = Path(mini_ork.__file__).resolve().parents[1]
FRAMEWORK_EDIT_VERIFIERS = (
    "recipes/framework-edit/verifiers/test.py",
    "recipes/framework-edit/verifiers/static-check.py",
)
LANE_ENV_KEYS = (
    "MINI_ORK_RUN_ID",
    "MINI_ORK_RUN_DIR",
    "MINI_ORK_DB",
    "MINI_ORK_HOME",
    "MO_PLACEMENT",
    "MO_SANDBOX_SCOPE",
    "MO_SANDBOX_BACKEND",
    "MO_TARGET_CWD",
)
WARNING_LINE = (
    "recipes/framework-edit/verifiers/test.py:154: DeprecationWarning: "
    "the default extraction filter will change in a future Python\n"
)


@pytest.fixture
def local_lane(monkeypatch):
    """Keep ``run_check`` on the local branch — no lane identity leaks in."""
    for key in LANE_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)


def _run(tmp_path, body):
    script = tmp_path / "verifier_under_test.py"
    script.write_text(textwrap.dedent(body), encoding="utf-8")
    evidence = tmp_path / "verifier_under_test.json"
    rc = _run_verifier_ref(str(script), str(evidence), cwd=str(tmp_path),
                           run_dir=str(tmp_path))
    return rc, evidence


def test_pass_false_envelope_after_warning_line_is_refuted(local_lane, tmp_path):
    rc, _ = _run(tmp_path, """
        import sys
        sys.stderr.write(%r)
        print('{"verifier": "test", "pass": false, "checks": []}')
        sys.exit(0)
    """ % WARNING_LINE)
    assert rc == 1


def test_pass_true_envelope_after_warning_line_is_accepted(local_lane, tmp_path):
    rc, _ = _run(tmp_path, """
        import sys
        sys.stderr.write(%r)
        print('{"verifier": "test", "pass": true, "checks": []}')
        sys.exit(0)
    """ % WARNING_LINE)
    assert rc == 0


def test_pure_json_pass_false_is_still_refuted(local_lane, tmp_path):
    rc, _ = _run(tmp_path, """
        import sys
        print('{"verifier": "test", "pass": false, "checks": []}')
        sys.exit(0)
    """)
    assert rc == 1


def test_prose_only_exit_zero_still_propagates_zero(local_lane, tmp_path):
    rc, _ = _run(tmp_path, """
        import sys
        print("no checks ran, nothing to see here")
        sys.exit(0)
    """)
    assert rc == 0


def test_prose_only_exit_two_still_propagates_two(local_lane, tmp_path):
    rc, _ = _run(tmp_path, """
        import sys
        print("could not start the tool: no such file or directory")
        sys.exit(2)
    """)
    assert rc == 2


def test_abstain_envelope_after_warning_line_abstains(local_lane, tmp_path):
    rc, _ = _run(tmp_path, """
        import sys
        sys.stderr.write(%r)
        print('{"verifier": "test", "pass": false, "failure_kind": "toolchain"}')
        sys.exit(0)
    """ % WARNING_LINE)
    assert rc == levels.ABSTAIN_RC


def test_last_pass_carrying_envelope_wins(local_lane, tmp_path):
    """A nested check log's ``pass: true`` must not mask the final ``pass: false``."""
    rc, _ = _run(tmp_path, """
        import sys
        print('{"pass": true, "note": "inner check log"}')
        print('{"verifier": "test", "pass": false, "checks": []}')
        sys.exit(0)
    """)
    assert rc == 1


def _extractall_calls(path):
    tree = ast.parse(Path(path).read_text(encoding="utf-8"))
    calls = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if (isinstance(func, ast.Attribute) and func.attr == "extractall") or (
                isinstance(func, ast.Name) and func.id == "extractall"):
            calls.append(node)
    return calls


@pytest.mark.parametrize("rel", FRAMEWORK_EDIT_VERIFIERS)
def test_extractall_calls_carry_the_data_filter(rel):
    calls = _extractall_calls(REPO_ROOT / rel)
    assert calls, "no extractall calls in %s — the scan would be vacuous" % rel
    for call in calls:
        assert "filter" in [kw.arg for kw in call.keywords], (
            "%s: extractall at line %s has no filter= keyword"
            % (rel, getattr(call, "lineno", "?"))
        )


@pytest.mark.parametrize("rel", FRAMEWORK_EDIT_VERIFIERS)
def test_verifier_scripts_still_exit_zero(rel):
    tree = ast.parse((REPO_ROOT / rel).read_text(encoding="utf-8"))
    exits = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Call) and node.args
        and isinstance(node.args[0], ast.Constant) and node.args[0].value == 0
        and getattr(node.func, "attr", getattr(node.func, "id", "")) in ("exit", "SystemExit")
    ]
    assert exits, "%s no longer exits 0" % rel
