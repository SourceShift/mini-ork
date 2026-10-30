"""Unit tests for the code-fix verifiers' ``failure_kind`` provenance.

When ``recipes/code-fix/verifiers/test.py`` or ``typecheck.py`` cannot start its
tool (a missing binary, ``command not found``, an npm ``EBADDEVENGINES`` refusal,
or a suite killed by a signal) the engine must read the failure as an
abstention, not a refutation. The verifiers now emit ``failure_kind=
"toolchain"`` for exactly those states, built on the suite-strict
``classify_suite_spawn`` in ``mini_ork.verify.pm``.

A real test failure — including one whose output says "No such file or
directory" while the runner ran fine — must stay a refutation. ``classify_spawn``
is deliberately NOT reused on suite logs: its needle list would turn that real
failure into an abstention.

Hermetic: both verifier scripts are loaded with ``importlib.util`` and every
run-local / host-derived input is pinned (``MINI_ORK_TEST_CMD``,
``MINI_ORK_TYPECHECK_CMD``, a tmp ``MINI_ORK_HOME``/``MINI_ORK_RUN_ID``).
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path

import pytest

import mini_ork

REPO_ROOT = Path(mini_ork.__file__).resolve().parents[1]
TEST_VERIFIER = REPO_ROOT / "recipes" / "code-fix" / "verifiers" / "test.py"
TYPECHECK_VERIFIER = REPO_ROOT / "recipes" / "code-fix" / "verifiers" / "typecheck.py"

# A binary that cannot exist: the shell reports rc 127 + "command not found".
MISSING_TOOL = "mini-ork-spec-no-such-binary-xyz --noEmit"

_FILE_NOT_FOUND_LINE = (
    "E   FileNotFoundError: [Errno 2] No such file or directory: 'x.json'"
)


def _last_json(text: str) -> dict:
    """The last non-empty stdout line as JSON — the verifier's envelope."""
    lines = [line for line in text.splitlines() if line.strip()]
    assert lines, "verifier printed no payload"
    return json.loads(lines[-1])


def _load_module(name: str, path: Path, home: Path, extra_env: dict[str, str]):
    """Exec a verifier script with its log dir rooted at ``home``.

    ``extra_env`` pins module-level detection (the test command / typecheck
    command) so import neither probes the host for runners nor reads any
    run-local profile.
    """
    keys = ("MINI_ORK_HOME", "MINI_ORK_RUN_ID", *extra_env)
    saved = {k: os.environ.get(k) for k in keys}
    os.environ["MINI_ORK_HOME"] = str(home)
    os.environ["MINI_ORK_RUN_ID"] = "failure-kind-tests"
    os.environ.update(extra_env)
    try:
        spec = importlib.util.spec_from_file_location(name, path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _load_test_verifier(home: Path):
    return _load_module(
        "codefix_test_verifier_failure_kind", TEST_VERIFIER, home,
        {"MINI_ORK_TEST_CMD": "python3 -c pass"},
    )


def _load_typecheck_verifier(home: Path):
    return _load_module(
        "codefix_typecheck_verifier_failure_kind", TYPECHECK_VERIFIER, home, {},
    )


@pytest.fixture(scope="module")
def test_verifier(tmp_path_factory):
    return _load_test_verifier(tmp_path_factory.mktemp("codefix-test-home"))


@pytest.fixture(scope="module")
def typecheck_verifier(tmp_path_factory):
    return _load_typecheck_verifier(tmp_path_factory.mktemp("codefix-typecheck-home"))


# ── classify_suite_spawn (mini_ork/verify/pm.py) ─────────────────────────────


def test_classify_rc_127_is_toolchain():
    from mini_ork.verify.pm import classify_suite_spawn

    assert classify_suite_spawn(["pytest", "-q"], 127, "") == "toolchain"


def test_classify_signal_killed_is_toolchain():
    from mini_ork.verify.pm import classify_suite_spawn

    assert classify_suite_spawn(["npm", "test"], -15, "") == "toolchain"


def test_classify_ebaddevengines_on_line_2_is_toolchain():
    from mini_ork.verify.pm import classify_suite_spawn

    out = "> repo@1.0.0 test\nnpm ERR! code EBADDEVENGINES\n"
    assert classify_suite_spawn(["npm", "run", "test"], 1, out) == "toolchain"


def test_classify_command_not_found_on_line_1_is_toolchain():
    from mini_ork.verify.pm import classify_suite_spawn

    assert classify_suite_spawn(
        ["pytest", "-q"], 1, "sh: 1: pytest: command not found\n"
    ) == "toolchain"


def test_classify_file_not_found_error_is_not_a_spawn_failure():
    """The trap: a real test failure saying "No such file or directory" refutes."""
    from mini_ork.verify.pm import classify_suite_spawn

    log = "\n".join([
        "============================= test session starts ==============================",
        "platform darwin -- Python 3.11.0",
        "collected 3 items",
        "",
        _FILE_NOT_FOUND_LINE,
        "=================================== FAILURES ===================================",
    ])
    assert classify_suite_spawn(["pytest"], 1, log) == ""


def test_classify_command_not_found_only_on_line_25_is_empty():
    """Only the 20-line head counts: a hit past it is real test output."""
    from mini_ork.verify.pm import classify_suite_spawn

    lines = [f"tests/test_demo.py::test_case_{i:02d} PASSED [ 50%]" for i in range(1, 25)]
    lines.append("sh: 1: pytest: command not found")  # line 25
    assert classify_suite_spawn(["pytest"], 1, "\n".join(lines)) == ""


def test_classify_rc_0_is_empty():
    from mini_ork.verify.pm import classify_suite_spawn

    assert classify_suite_spawn(["pytest", "-q"], 0, "3 passed in 0.01s\n") == ""


# ── verifiers/test.py envelopes ──────────────────────────────────────────────


def _emit_with_log(verifier, monkeypatch, tmp_path, capsys, log_text, *emit_args):
    log = tmp_path / "verifier_test.log"
    log.write_text(log_text, encoding="utf-8")
    monkeypatch.setattr(verifier, "LOG_PATH", str(log))
    rc = verifier.emit(*emit_args)
    return rc, _last_json(capsys.readouterr().out)


def test_test_emit_127_with_spawn_log_carries_toolchain(
    test_verifier, monkeypatch, tmp_path, capsys
):
    from mini_ork.verify.levels import abstain_kind

    rc, payload = _emit_with_log(
        test_verifier, monkeypatch, tmp_path, capsys,
        "sh: 1: pytest: command not found\n", False, "x", 127,
    )
    assert payload.get("failure_kind") == "toolchain"
    assert abstain_kind(payload) == "toolchain"


def test_test_emit_1_with_filenotfound_log_has_no_failure_kind(
    test_verifier, monkeypatch, tmp_path, capsys
):
    rc, payload = _emit_with_log(
        test_verifier, monkeypatch, tmp_path, capsys,
        _FILE_NOT_FOUND_LINE + "\n", False, "x", 1,
    )
    assert rc == 1
    assert payload["pass"] is False
    assert "failure_kind" not in payload


def test_test_emit_true_has_no_failure_kind(test_verifier, monkeypatch, tmp_path, capsys):
    rc, payload = _emit_with_log(
        test_verifier, monkeypatch, tmp_path, capsys,
        "3 passed in 0.01s\n", True, "ok", 0,
    )
    assert payload["pass"] is True
    assert "failure_kind" not in payload


def test_test_emit_non_int_post_rc_has_no_failure_kind(
    test_verifier, monkeypatch, tmp_path, capsys
):
    rc, payload = _emit_with_log(
        test_verifier, monkeypatch, tmp_path, capsys,
        "sh: 1: pytest: command not found\n", False, "tests weakened: y", "",
    )
    assert "failure_kind" not in payload


def test_test_emit_fails_closed_without_classifier(tmp_path, monkeypatch, capsys):
    """A missing classifier must never invent a failure_kind."""
    saved = sys.modules.get("mini_ork.verify.pm")
    sys.modules["mini_ork.verify.pm"] = None  # halts the submodule import
    try:
        mod = _load_test_verifier(tmp_path)
    finally:
        if saved is None:
            sys.modules.pop("mini_ork.verify.pm", None)
        else:
            sys.modules["mini_ork.verify.pm"] = saved

    assert getattr(mod, "_classify_suite_spawn", "missing") is None

    log = tmp_path / "spawn.log"
    log.write_text("sh: 1: pytest: command not found\n", encoding="utf-8")
    monkeypatch.setattr(mod, "LOG_PATH", str(log))
    mod.emit(False, "x", 127)
    payload = _last_json(capsys.readouterr().out)
    assert "failure_kind" not in payload


# ── verifiers/typecheck.py envelopes ─────────────────────────────────────────


def test_typecheck_emit_false_carries_failure_kind(typecheck_verifier, capsys):
    typecheck_verifier._emit(
        passed=False, reason="see log", evidence=None, gating=True,
        failure_kind="toolchain",
    )
    payload = _last_json(capsys.readouterr().out)
    assert payload.get("failure_kind") == "toolchain"


def test_typecheck_emit_true_does_not_carry_failure_kind(typecheck_verifier, capsys):
    typecheck_verifier._emit(
        passed=True, reason="", evidence=None, gating=True,
        failure_kind="toolchain",
    )
    payload = _last_json(capsys.readouterr().out)
    assert "failure_kind" not in payload


def test_typecheck_main_emits_toolchain_and_returns_rc(
    typecheck_verifier, monkeypatch, capsys
):
    monkeypatch.setenv("MINI_ORK_TYPECHECK_CMD", MISSING_TOOL)
    rc = typecheck_verifier.main()
    payload = _last_json(capsys.readouterr().out)
    assert rc == 127
    assert payload["pass"] is False
    assert payload.get("failure_kind") == "toolchain"


def test_typecheck_main_real_diagnostic_is_not_toolchain(
    typecheck_verifier, monkeypatch, capsys
):
    cmd = ('echo "src/types.ts: error TS2307: FileNotFoundError [Errno 2] '
           "No such file or directory: 'x.d.ts'\"; exit 1")
    monkeypatch.setenv("MINI_ORK_TYPECHECK_CMD", cmd)
    rc = typecheck_verifier.main()
    payload = _last_json(capsys.readouterr().out)
    assert rc == 1
    assert payload["pass"] is False
    assert "failure_kind" not in payload


def test_typecheck_fallback_fails_closed(tmp_path, monkeypatch, capsys):
    """Without mini_ork.verify.pm the fallback returns "" — main must not NameError."""
    saved = sys.modules.get("mini_ork.verify.pm")
    sys.modules["mini_ork.verify.pm"] = None  # halts the submodule import
    try:
        mod = _load_typecheck_verifier(tmp_path)
    finally:
        if saved is None:
            sys.modules.pop("mini_ork.verify.pm", None)
        else:
            sys.modules["mini_ork.verify.pm"] = saved

    monkeypatch.setenv("MINI_ORK_TYPECHECK_CMD", MISSING_TOOL)
    rc = mod.main()
    payload = _last_json(capsys.readouterr().out)
    assert rc == 127
    assert "failure_kind" not in payload
