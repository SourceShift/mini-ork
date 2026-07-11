"""Parity gate: mini_ork.ported.langfuse_score_mapper vs lib/langfuse_score_mapper.sh.

Each test invokes the LIVE bash functions from ``lib/langfuse_score_mapper.sh``
via ``bash -c 'source lib/langfuse_score_mapper.sh; <func> <args>'`` on
identical inputs as the Python port and asserts equivalent outputs
(stdout lines parsed as one-JSON-per-line, exit codes / raised
exceptions, byte-equal table text). No mocks, no hardcoded outputs
beyond what bash itself emits.

Seven cases (above the kickoff's >=6 floor):
  (a) reviewer APPROVE via stdin           — bash emits 1 line
                                               {{name:'reviewer_approve',
                                                 value:1.0, ...}};
                                             python port returns
                                             [same dict].
  (b) verifier fail via stdin              — bash emits 1 line value=-0.5
                                             comment 'deterministic fail'.
  (c) oracle CITATION_UNDERCOVERED         — bash emits 1 line value=-0.5
                                             comment 'citations missing'
                                             (verdict→ORACLE_MAP).
  (d) rollback event via stdin             — bash emits 1 line value=-1.0
                                             comment 'publish reverted'.
  (e) promotion promoted via stdin        — bash emits 1 line value=1.0
                                             comment 'candidate promoted'
                                             (decision→PROMOTION_MAP).
  (f) combined reviewer+verifier (2 evts)  — bash emits 2 lines in
                                             reviewer→verifier order;
                                             python returns the same
                                             2-element list.
  (g) langfuse_score_table byte-equal      — bash stdout of the table
                                             function equals Python
                                             langfuse_score_table()
                                             string verbatim.

Bash stdin pipe: bash's ``[ -t 0 ]`` returns False when subprocess
pipes stdin, so the bash function's usage-error branch is unreachable
from these tests — it requires a real tty that pytest cannot easily
provide. Python port raises ``ValueError('usage: ...')`` for the
equivalent condition (``input_json`` None AND ``stdin_text`` None);
see test_lsm.usage_error_raises for the in-process equivalent.

Environment isolation: each test points subprocess at a per-test
stdin payload so bash's ``cat`` and Python's ``stdin_text`` both
land in the same JSON string with no filesystem pollution.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
from mini_ork.ported import langfuse_score_mapper as lsm  # noqa: E402

SH = REPO / "lib" / "langfuse_score_mapper.sh"

# Float tolerance from the kickoff: 'floats 1e-6'. Strict superset of
# bash's own repr precision (Python's ``json.dumps(0.5)`` → ``0.5``
# exactly; ``json.dumps(-0.25)`` → ``-0.25`` exactly).
_FLOAT_TOL = 1e-6


def _which_bash() -> None:
    if not shutil.which("bash"):
        pytest.skip("bash not on PATH")
    if not shutil.which("python3"):
        pytest.skip("python3 not on PATH (lib/langfuse_score_mapper.sh uses a python3 heredoc)")
    if not SH.exists():
        pytest.skip(f"missing lib/langfuse_score_mapper.sh at {SH}")


def _bash_score_for_verdict(stdin_payload: str) -> subprocess.CompletedProcess:
    """Invoke ``bash -c 'source lib/langfuse_score_mapper.sh; langfuse_score_for_verdict'``
    with ``stdin_payload`` piped on stdin (so bash's ``[ -t 0 ]`` is False
    and the function reads stdin instead of erroring on usage)."""
    src = (
        f'source "{SH}"\n'
        f'langfuse_score_for_verdict\n'
    )
    return subprocess.run(
        ["bash", "-c", src],
        input=stdin_payload,
        capture_output=True, text=True,
    )


def _bash_score_table() -> subprocess.CompletedProcess:
    """Invoke ``bash -c 'source lib/langfuse_score_mapper.sh; langfuse_score_table'``."""
    src = (
        f'source "{SH}"\n'
        f'langfuse_score_table\n'
    )
    return subprocess.run(
        ["bash", "-c", src],
        capture_output=True, text=True,
    )


def _bash_json_lines(cp: subprocess.CompletedProcess) -> list[dict]:
    """Parse bash stdout as one JSON object per line.

    bash emits via Python's ``print(json.dumps(...))`` so each non-empty
    stdout line is a JSON object. Empty / trailing lines are skipped
    (bash sometimes leaves a final newline).
    """
    out: list[dict] = []
    for line in cp.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        out.append(json.loads(line))
    return out


def _assert_score_eq(py_score: dict, bash_score: dict, label: str) -> None:
    """Field-by-field equality for one emitted score dict."""
    assert py_score["name"] == bash_score["name"], (
        f"{label}: name mismatch py={py_score['name']!r} bash={bash_score['name']!r}"
    )
    assert py_score["data_type"] == bash_score["data_type"], (
        f"{label}: data_type mismatch py={py_score['data_type']!r} bash={bash_score['data_type']!r}"
    )
    assert py_score["comment"] == bash_score["comment"], (
        f"{label}: comment mismatch py={py_score['comment']!r} bash={bash_score['comment']!r}"
    )
    assert abs(float(py_score["value"]) - float(bash_score["value"])) < _FLOAT_TOL, (
        f"{label}: value mismatch py={py_score['value']!r} bash={bash_score['value']!r}"
    )


# ─────────────────────────────────────────────────────────────────────────────
# (a) reviewer APPROVE via stdin
# ─────────────────────────────────────────────────────────────────────────────
def test_reviewer_approve_via_stdin_parity():
    """echo '{"decision":"APPROVE"}' | langfuse_score_for_verdict
    → 1 line: reviewer_approve value=1.0 'explicit approval'."""
    _which_bash()
    payload = '{"decision":"APPROVE"}'

    r_bash = _bash_score_for_verdict(payload)
    assert r_bash.returncode == 0, (
        f"bash rc={r_bash.returncode}\nstderr={r_bash.stderr}"
    )
    bash_scores = _bash_json_lines(r_bash)
    assert len(bash_scores) == 1, (
        f"bash expected 1 line for reviewer APPROVE, got {len(bash_scores)}: "
        f"{r_bash.stdout!r}"
    )

    py_scores = lsm.langfuse_score_for_verdict(payload)
    assert len(py_scores) == 1
    _assert_score_eq(py_scores[0], bash_scores[0], "reviewer_approve")
    assert bash_scores[0]["name"] == "reviewer_approve"
    assert bash_scores[0]["value"] == 1.0


# ─────────────────────────────────────────────────────────────────────────────
# (b) verifier fail via stdin
# ─────────────────────────────────────────────────────────────────────────────
def test_verifier_fail_via_stdin_parity():
    """echo '{"verdict":"fail"}' | langfuse_score_for_verdict
    → 1 line: verifier_fail value=-0.5 'deterministic fail'."""
    _which_bash()
    payload = '{"verdict":"fail"}'

    r_bash = _bash_score_for_verdict(payload)
    assert r_bash.returncode == 0, (
        f"bash rc={r_bash.returncode}\nstderr={r_bash.stderr}"
    )
    bash_scores = _bash_json_lines(r_bash)
    assert len(bash_scores) == 1, (
        f"bash expected 1 line for verifier fail, got {len(bash_scores)}: "
        f"{r_bash.stdout!r}"
    )

    py_scores = lsm.langfuse_score_for_verdict(payload)
    assert len(py_scores) == 1
    _assert_score_eq(py_scores[0], bash_scores[0], "verifier_fail")
    assert bash_scores[0]["name"] == "verifier_fail"
    assert bash_scores[0]["value"] == -0.5


# ─────────────────────────────────────────────────────────────────────────────
# (c) oracle CITATION_UNDERCOVERED via stdin
# ─────────────────────────────────────────────────────────────────────────────
def test_oracle_citation_undercovered_via_stdin_parity():
    """echo '{"verdict":"CITATION_UNDERCOVERED"}' | langfuse_score_for_verdict
    → 1 line: citation_undercovered value=-0.5 'citations missing'.

    Note: the bash Python heredoc emits the JSON-style rationale
    'citations missing' (NOT the operator-facing table text 'citations
    failed to resolve' shown in langfuse_score_table)."""
    _which_bash()
    payload = '{"verdict":"CITATION_UNDERCOVERED"}'

    r_bash = _bash_score_for_verdict(payload)
    assert r_bash.returncode == 0, (
        f"bash rc={r_bash.returncode}\nstderr={r_bash.stderr}"
    )
    bash_scores = _bash_json_lines(r_bash)
    assert len(bash_scores) == 1, (
        f"bash expected 1 line for oracle CITATION_UNDERCOVERED, got "
        f"{len(bash_scores)}: {r_bash.stdout!r}"
    )

    py_scores = lsm.langfuse_score_for_verdict(payload)
    assert len(py_scores) == 1
    _assert_score_eq(py_scores[0], bash_scores[0], "citation_undercovered")
    assert bash_scores[0]["name"] == "citation_undercovered"
    assert bash_scores[0]["value"] == -0.5


# ─────────────────────────────────────────────────────────────────────────────
# (d) rollback event via stdin
# ─────────────────────────────────────────────────────────────────────────────
def test_rollback_event_via_stdin_parity():
    """echo '{"event":"rollback_fired"}' | langfuse_score_for_verdict
    → 1 line: rollback_fired value=-1.0 'publish reverted'."""
    _which_bash()
    payload = '{"event":"rollback_fired"}'

    r_bash = _bash_score_for_verdict(payload)
    assert r_bash.returncode == 0, (
        f"bash rc={r_bash.returncode}\nstderr={r_bash.stderr}"
    )
    bash_scores = _bash_json_lines(r_bash)
    assert len(bash_scores) == 1, (
        f"bash expected 1 line for rollback event, got {len(bash_scores)}: "
        f"{r_bash.stdout!r}"
    )

    py_scores = lsm.langfuse_score_for_verdict(payload)
    assert len(py_scores) == 1
    _assert_score_eq(py_scores[0], bash_scores[0], "rollback_fired")
    assert bash_scores[0]["name"] == "rollback_fired"
    assert bash_scores[0]["value"] == -1.0


# ─────────────────────────────────────────────────────────────────────────────
# (e) promotion promoted via stdin (decision → PROMOTION_MAP)
# ─────────────────────────────────────────────────────────────────────────────
def test_promotion_promoted_via_stdin_parity():
    """echo '{"decision":"promoted"}' | langfuse_score_for_verdict
    → 1 line: promoted value=1.0 'candidate promoted'.

    'promoted' is in PROMOTION_MAP but NOT in REVIEWER_MAP, so only the
    promotion emit fires."""
    _which_bash()
    payload = '{"decision":"promoted"}'

    r_bash = _bash_score_for_verdict(payload)
    assert r_bash.returncode == 0, (
        f"bash rc={r_bash.returncode}\nstderr={r_bash.stderr}"
    )
    bash_scores = _bash_json_lines(r_bash)
    assert len(bash_scores) == 1, (
        f"bash expected 1 line for promotion promoted, got {len(bash_scores)}: "
        f"{r_bash.stdout!r}"
    )

    py_scores = lsm.langfuse_score_for_verdict(payload)
    assert len(py_scores) == 1
    _assert_score_eq(py_scores[0], bash_scores[0], "promoted")
    assert bash_scores[0]["name"] == "promoted"
    assert bash_scores[0]["value"] == 1.0


# ─────────────────────────────────────────────────────────────────────────────
# (f) combined reviewer APPROVE + verifier pass (2 events, in emit order)
# ─────────────────────────────────────────────────────────────────────────────
def test_combined_reviewer_and_verifier_parity():
    """echo '{"decision":"APPROVE","verdict":"pass"}' | langfuse_score_for_verdict
    → 2 lines in bash-emit order: reviewer_approve then verifier_pass.

    Bash heredoc emits reviewer_map(decision) FIRST then verifier_map(verdict);
    the Python port mirrors that order so positionally-indexed comparison
    holds."""
    _which_bash()
    payload = '{"decision":"APPROVE","verdict":"pass"}'

    r_bash = _bash_score_for_verdict(payload)
    assert r_bash.returncode == 0, (
        f"bash rc={r_bash.returncode}\nstderr={r_bash.stderr}"
    )
    bash_scores = _bash_json_lines(r_bash)
    assert len(bash_scores) == 2, (
        f"bash expected 2 lines for combined input, got {len(bash_scores)}: "
        f"{r_bash.stdout!r}"
    )

    py_scores = lsm.langfuse_score_for_verdict(payload)
    assert len(py_scores) == 2
    # Both sides must agree on order AND content.
    for i, (py_s, bash_s) in enumerate(zip(py_scores, bash_scores)):
        _assert_score_eq(py_s, bash_s, f"combined[{i}]")
    assert py_scores[0]["name"] == "reviewer_approve"
    assert py_scores[1]["name"] == "verifier_pass"


# ─────────────────────────────────────────────────────────────────────────────
# (g) langfuse_score_table byte-equal
# ─────────────────────────────────────────────────────────────────────────────
def test_score_table_byte_equal_parity():
    """langfuse_score_table() returns the same heredoc text bash ``cat
    <<EOF`` prints. Static literal — no input — so byte-equality holds."""
    _which_bash()
    r_bash = _bash_score_table()
    assert r_bash.returncode == 0, (
        f"bash rc={r_bash.returncode}\nstderr={r_bash.stderr}"
    )
    py_table = lsm.langfuse_score_table()
    assert py_table == r_bash.stdout, (
        f"score table text mismatch.\n--- python ---\n{py_table!r}\n"
        f"--- bash ---\n{r_bash.stdout!r}\n--- diff ---\n"
        f"{[i for i, (a, b) in enumerate(zip(py_table, r_bash.stdout)) if a != b]}"
    )


# ─────────────────────────────────────────────────────────────────────────────
# Empty-match input (no rule fires) — bonus case for safety
# ─────────────────────────────────────────────────────────────────────────────
def test_empty_match_returns_empty_list_parity():
    """dict with no decision/verdict/event keys → 0 lines on bash, []
    on python. Mirrors bash heredoc behavior of 'if isinstance(data,
    dict): [emit rules]' — when none match, nothing is printed."""
    _which_bash()
    payload = '{"foo":"bar"}'

    r_bash = _bash_score_for_verdict(payload)
    assert r_bash.returncode == 0, (
        f"bash rc={r_bash.returncode}\nstderr={r_bash.stderr}"
    )
    bash_lines = [ln for ln in r_bash.stdout.splitlines() if ln.strip()]
    assert bash_lines == [], (
        f"bash expected 0 stdout lines for unmatched dict, got "
        f"{bash_lines!r}"
    )

    py_scores = lsm.langfuse_score_for_verdict(payload)
    assert py_scores == [], f"python expected [] for unmatched dict, got {py_scores}"


def test_usage_error_raises_value_error():
    """Calling langfuse_score_for_verdict with NO input_json and NO
    stdin_text raises ValueError with the usage message — the Python
    port equivalent of bash's ``[ -t 0 ]`` usage-error branch (which
    is unreachable from subprocess because pytest cannot provide a
    real tty)."""
    with pytest.raises(ValueError) as exc:
        lsm.langfuse_score_for_verdict()
    msg = str(exc.value)
    assert "usage" in msg.lower(), f"usage message missing 'usage': {msg!r}"
    assert "langfuse_score_for_verdict" in msg, f"usage message missing function name: {msg!r}"


def test_parse_error_raises_value_error(tmp_path):
    """Malformed JSON → ValueError with 'parse error' prefix; bash
    returns rc=2 with the same prefix on stderr."""
    _which_bash()
    bad = "{not valid json"

    r_bash = _bash_score_for_verdict(bad)
    assert r_bash.returncode == 2, (
        f"bash expected rc=2 on parse error, got {r_bash.returncode}; "
        f"stderr={r_bash.stderr!r}"
    )
    assert "parse error" in r_bash.stderr.lower(), (
        f"bash stderr should contain 'parse error': {r_bash.stderr!r}"
    )

    # Python: stdin_text path with malformed JSON
    with pytest.raises(ValueError) as exc:
        lsm.langfuse_score_for_verdict(stdin_text=bad)
    assert "parse error" in str(exc.value).lower()

    # Python: input_json path with malformed JSON
    with pytest.raises(ValueError) as exc:
        lsm.langfuse_score_for_verdict(bad)
    assert "parse error" in str(exc.value).lower()

    # Python: file-path mode with malformed-JSON file. Bash does NOT
    # echo the path on parse error (only the JSON exception message),
    # so we just assert the prefix matches.
    bad_file = tmp_path / "bad.json"
    bad_file.write_text(bad)
    with pytest.raises(ValueError) as exc:
        lsm.langfuse_score_for_verdict(str(bad_file))
    assert "parse error" in str(exc.value).lower()


def test_input_json_file_path_parity(tmp_path):
    """input_json pointing at an existing file is read (mirror bash
    lines 156-161). Otherwise it's treated as inline JSON."""
    _which_bash()
    payload_file = tmp_path / "verdict.json"
    payload_file.write_text('{"verdict":"pass"}')

    # Bash: only stdin path is reachable from subprocess (positional
    # arg path requires non-empty stdin? — bash lines 92-112: if $1
    # is non-empty AND stdin is also non-empty, bash takes $1 as the
    # input. With our test we pipe the same content on stdin for
    # parity; the file-vs-inline distinction is only testable on the
    # Python side.)
    same_payload = '{"verdict":"pass"}'
    r_bash = _bash_score_for_verdict(same_payload)
    assert r_bash.returncode == 0
    bash_scores = _bash_json_lines(r_bash)
    assert len(bash_scores) == 1
    assert bash_scores[0]["name"] == "verifier_pass"

    # Python file-path mode
    py_scores = lsm.langfuse_score_for_verdict(str(payload_file))
    assert len(py_scores) == 1
    _assert_score_eq(py_scores[0], bash_scores[0], "file_path_mode")