"""Parity gate: mini_ork.ported.profile_gate vs lib/profile_gate.sh.

Each case invokes the LIVE bash function via subprocess (no mocking) and
compares stdout + file side-effect against the Python port.
Bash source at lib/profile_gate.sh is unmodified (strangler-fig coexistence).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
from mini_ork.ported import profile_gate as pg  # noqa: E402

PG_SH = REPO / "lib" / "profile_gate.sh"


def _bash_echo(profile_path: str) -> str:
    """Invoke the live bash function on a file; return stripped stdout."""
    r = subprocess.run(
        ["bash", "-c",
         f'. "{PG_SH}" && mo_profile_normalize_zero_questions "$1"',
         "_", profile_path],
        env={**os.environ, "MINI_ORK_ROOT": str(REPO)},
        capture_output=True, text=True, check=True)
    return r.stdout.strip()


def _bytes(path: str) -> bytes:
    with open(path, "rb") as f:
        return f.read()


def _write(path: Path, body: dict | str) -> str:
    if isinstance(body, str):
        path.write_text(body)
    else:
        path.write_text(json.dumps(body))
    return str(path)


def _run_pair(input_body: dict | str, py_dest: Path, bash_dest: Path) -> tuple[str, str, bytes, bytes]:
    """Write the same `input_body` to two fresh paths and run Python on one,
    bash on the other. Returns (out_py, out_bash, py_bytes, bash_bytes)."""
    _write(py_dest, input_body)
    _write(bash_dest, input_body)
    out_py = pg.normalize_zero_questions(str(py_dest))
    out_bash = _bash_echo(str(bash_dest))
    return out_py, out_bash, _bytes(str(py_dest)), _bytes(str(bash_dest))


# ---------- parity cases ----------

def test_needs_answers_zero_questions_normalizes_parity(tmp_path):
    """Case 1: needs_answers + [] → echo 'ready', file rewritten with marker."""
    body = {"profile_status": "needs_answers", "human_questions": [],
            "confidence": 0.9, "recipe": "code_fix"}
    out_py, out_bash, py_b, bash_b = _run_pair(
        body, tmp_path / "p1_py.json", tmp_path / "p1_bash.json")

    assert out_py == out_bash == "ready"
    assert py_b == bash_b  # both rewrites byte-identical
    j = json.loads(py_b)
    assert j["profile_status"] == "ready"
    assert j["profile_status_normalized"] == "needs_answers->ready (0 questions: nothing to answer)"


def test_needs_answers_real_questions_unchanged_parity(tmp_path):
    """Case 2: needs_answers + non-empty questions → echo 'needs_answers'; file untouched."""
    body = {"profile_status": "needs_answers",
            "human_questions": ["What is the target repo?"], "confidence": 0.4}
    out_py, out_bash, py_b, bash_b = _run_pair(
        body, tmp_path / "p2_py.json", tmp_path / "p2_bash.json")

    assert out_py == out_bash == "needs_answers"
    assert py_b == bash_b == json.dumps(body).encode()  # both sides wrote nothing


def test_already_ready_unchanged_parity(tmp_path):
    """Case 3: already ready → echo 'ready', file untouched."""
    body = {"profile_status": "ready", "human_questions": [], "confidence": 1.0}
    out_py, out_bash, py_b, bash_b = _run_pair(
        body, tmp_path / "p3_py.json", tmp_path / "p3_bash.json")

    assert out_py == out_bash == "ready"
    assert py_b == bash_b == json.dumps(body).encode()


def test_missing_path_empty_echo_parity(tmp_path):
    """Case 4: missing path → both echo '', no file written."""
    bogus = str(tmp_path / "does-not-exist.json")
    assert not os.path.exists(bogus)

    out_py = pg.normalize_zero_questions(bogus)
    out_bash = _bash_echo(bogus)

    assert out_py == out_bash == ""
    assert not os.path.exists(bogus)


def test_malformed_json_empty_echo_untouched_parity(tmp_path):
    """Case 5: malformed JSON → both echo '', file untouched."""
    bad = "{not json"

    py_path = _write(tmp_path / "mal_py.json", bad)
    py_bytes_before = _bytes(py_path)
    out_py = pg.normalize_zero_questions(py_path)

    bash_path = _write(tmp_path / "mal_bash.json", bad)
    out_bash = _bash_echo(bash_path)

    assert out_py == out_bash == ""
    assert _bytes(py_path) == py_bytes_before == bad.encode()
    assert _bytes(bash_path) == bad.encode()


def test_needs_answers_with_confidence_and_extras_preserved_parity(tmp_path):
    """Case 6: needs_answers + many keys → echo 'ready'; confidence + extras preserved.
    Both sides get a fresh input; both rewrite; bytes must be identical."""
    body = {"profile_status": "needs_answers", "human_questions": [],
            "confidence": 0.42, "recipe": "code_fix", "task_class": "code_fix",
            "target_repo": "mo-wt-profile_gate", "success_criteria": ["parity"],
            "scope_allow": ["*.py"], "scope_deny": ["lib/profile_gate.sh"]}
    out_py, out_bash, py_b, bash_b = _run_pair(
        body, tmp_path / "p6_py.json", tmp_path / "p6_bash.json")

    assert out_py == out_bash == "ready"
    assert py_b == bash_b  # byte-identical rewrite is the strongest key-preservation proof
    j = json.loads(py_b)
    assert j["confidence"] == 0.42
    assert j["recipe"] == "code_fix"
    assert j["target_repo"] == "mo-wt-profile_gate"


def test_ready_with_nonempty_questions_passthrough_parity(tmp_path):
    """Case 7: ready + non-empty questions → echo 'ready', file untouched (passthrough)."""
    body = {"profile_status": "ready",
            "human_questions": ["documentation-only flag remains?"], "confidence": 0.7}
    out_py, out_bash, py_b, bash_b = _run_pair(
        body, tmp_path / "p7_py.json", tmp_path / "p7_bash.json")

    assert out_py == out_bash == "ready"
    assert py_b == bash_b == json.dumps(body).encode()


def test_empty_path_string_empty_echo_parity():
    """Case 8 (edge): empty string path → both echo ''."""
    out_py = pg.normalize_zero_questions("")
    out_bash = _bash_echo("")
    assert out_py == out_bash == ""
