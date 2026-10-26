"""Harness-defect detection in `recipes/framework-edit/verifiers/spec-tests.py`.

A blind spec test can be unsatisfiable: calling a decorated fixture as a plain
function, requesting a fixture that does not exist, or asking for one from the
wrong scope are pytest USAGE errors inside the test code. No implementation can
make them pass, so the verifier labels them and — when every failure is one —
files `<run_dir>/spec-test-defect.md` so RB-18 stops the revise rounds instead
of spending them on an impossible clause.

Hermetic: a temp run dir with a temp state.db carrying a future-dated
`spec_test_author` node_end (so the tamper fence never fires), and the verifier
loaded with `importlib.util.spec_from_file_location` so `harness_defects` can be
exercised directly.
"""
from __future__ import annotations

import importlib.util
import sqlite3
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
VERIFIER = REPO / "recipes" / "framework-edit" / "verifiers" / "spec-tests.py"

RUN_ID = "spec-harness-defects"
AUTHOR_NODE = "spec_test_author"
SPEC_FILE = "test_spec_subject.py"
REPORT = "spec-test-defect.md"

FIXTURE_TEST = "test_calls_fixture_directly"
PLAIN_TEST = "test_fails_plainly"


@pytest.fixture(scope="module")
def verifier():
    spec = importlib.util.spec_from_file_location("spec_tests_verifier_harness", VERIFIER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# Every suite below quotes the kickoff in each test's docstring, which is what
# the verifier reads as the failed clause.
SUITE_FIXTURE_DEFECT = (
    "import pytest\n"
    "\n"
    "\n"
    "@pytest.fixture\n"
    "def driver(tmp_path):\n"
    "    return tmp_path\n"
    "\n"
    "\n"
    "def test_calls_fixture_directly():\n"
    '    """Kickoff: a fixture is not a plain function."""\n'
    "    driver(1)\n"
    "\n"
    "\n"
    "def test_passes():\n"
    '    """Kickoff: the rest of the suite still runs."""\n'
    "    assert True\n"
)

SUITE_MIXED = (
    "import pytest\n"
    "\n"
    "\n"
    "@pytest.fixture\n"
    "def driver(tmp_path):\n"
    "    return tmp_path\n"
    "\n"
    "\n"
    "def test_calls_fixture_directly():\n"
    '    """Kickoff: a fixture is not a plain function."""\n'
    "    driver(1)\n"
    "\n"
    "\n"
    "def test_fails_plainly():\n"
    '    """Kickoff: a real refutation still gates the run."""\n'
    "    assert False\n"
    "\n"
    "\n"
    "def test_passes():\n"
    '    """Kickoff: the rest of the suite still runs."""\n'
    "    assert True\n"
)

SUITE_ALL_PASSING = (
    "def test_passes():\n"
    '    """Kickoff: a suite that holds passes."""\n'
    "    assert True\n"
)


# ── harness_defects on captured pytest output ───────────────────────────────

def test_fixture_called_directly_records_the_bare_line(verifier):
    """Kickoff: 'the `Fixture "driver" called directly` line sits under
    `___ test_a ___` with no `E` line'."""
    output = "\n".join([
        "=================================== FAILURES ===================================",
        "___________________________ test_a ____________________________",
        "",
        "spec-tests/test_spec_x.py:11: in <module>",
        'Fixture "driver" called directly. Fixtures are not meant to be called directly,',
        "but are created automatically when test functions request them as parameters.",
        "=========================== short test summary info ============================",
        "FAILED test_spec_x.py::test_a - Failed: Fixture called directly",
    ])
    found = verifier.harness_defects(output)
    assert found == {
        "test_a": 'Fixture "driver" called directly. Fixtures are not meant to be called directly,'}


def test_error_at_setup_of_unknown_fixture_is_keyed_to_the_unit(verifier):
    """Kickoff: 'An `ERROR at setup of test_b` block containing
    `fixture 'nope' not found` → `{"test_b": ...}`.'"""
    output = "\n".join([
        "==================================== ERRORS ====================================",
        "_____________________ ERROR at setup of test_b _______________________",
        "",
        "file /nowhere/spec-tests/test_spec_x.py, line 8",
        "  def test_b(nope):",
        "E   fixture 'nope' not found",
        ">       available fixtures: capfd, tmp_path",
    ])
    found = verifier.harness_defects(output)
    assert found == {"test_b": "E   fixture 'nope' not found"}


def test_ordinary_assertion_error_is_not_a_harness_defect(verifier):
    """Kickoff: 'An ordinary `E   AssertionError: assert 1 == 2` block → not recorded.'"""
    output = "\n".join([
        "=================================== FAILURES ===================================",
        "___________________________ test_plain ____________________________",
        "",
        "    assert 1 == 2",
        "E   AssertionError: assert 1 == 2",
        "=========================== short test summary info ============================",
        "FAILED test_spec_x.py::test_plain - AssertionError: assert 1 == 2",
    ])
    assert verifier.harness_defects(output) == {}


def test_short_summary_section_is_never_attributed_to_a_block(verifier):
    """Kickoff: '===== section lines (e.g. `short test summary info`) end the
    current block.'"""
    output = "\n".join([
        "___________________________ test_block ____________________________",
        "",
        'Fixture "driver" called directly. Fixtures are not meant to be called directly,',
        "=========================== short test summary info ============================",
        'FAILED test_spec_x.py::test_sum - Failed: Fixture "driver" called directly.',
        "E   fixture 'ghost' not found",
    ])
    found = verifier.harness_defects(output)
    assert set(found) == {"test_block"}, found


# ── evaluate end to end ─────────────────────────────────────────────────────

def _make_run_dir(tmp_path: Path, monkeypatch, suite: str) -> Path:
    run_dir = tmp_path / "run"
    spec_dir = run_dir / "spec-tests"
    spec_dir.mkdir(parents=True)
    (spec_dir / SPEC_FILE).write_text(suite, encoding="utf-8")
    (run_dir / f"context-{AUTHOR_NODE}.json").write_text(
        f"Spec tests written: {SPEC_FILE}\n", encoding="utf-8")
    db = tmp_path / "state.db"
    con = sqlite3.connect(str(db))
    con.execute("CREATE TABLE run_events ("
                "run_id TEXT, event_type TEXT, created_at INTEGER, payload_json TEXT)")
    # Far future: no spec file can be "modified after authoring".
    con.execute("INSERT INTO run_events VALUES (?, ?, ?, ?)",
                (RUN_ID, "node_end", 4_000_000_000,
                 '{"node_id": "spec_test_author"}'))
    con.commit()
    con.close()
    monkeypatch.setenv("MINI_ORK_RUN_ID", RUN_ID)
    monkeypatch.setenv("MINI_ORK_DB", str(db))
    monkeypatch.setenv("MO_SPEC_TESTS_TIMEOUT_S", "300")
    return run_dir


def _evaluate(verifier, run_dir: Path, tmp_path: Path) -> dict:
    log_path = tmp_path / "evaluate.log"
    with open(log_path, "w", encoding="utf-8") as fh:
        return verifier.evaluate(str(run_dir), fh)


def _pass_check(result: dict) -> dict:
    for check in result["checks"]:
        if check.get("name") == "spec-tests-pass":
            return check
    raise AssertionError("no spec-tests-pass check in " + repr(result["checks"]))


def test_every_failure_marked_files_the_defect_report(verifier, tmp_path, monkeypatch):
    """Kickoff: 'When every failure is marked (at least one failure exists) …
    write it.' A run never passes on broken tests."""
    run_dir = _make_run_dir(tmp_path, monkeypatch, SUITE_FIXTURE_DEFECT)
    result = _evaluate(verifier, run_dir, tmp_path)

    assert result["pass"] is False
    assert result["rc"] != 0
    assert result["harness_defects"] == [f"{SPEC_FILE}::{FIXTURE_TEST}"]

    report = run_dir / REPORT
    assert report.is_file()
    assert result["harness_defect_report"] == str(report)
    text = report.read_text(encoding="utf-8")
    assert text.splitlines()[0] == "# Spec-test defect (detected by the spec-tests verifier)"
    assert f"`{SPEC_FILE}::{FIXTURE_TEST}`" in text
    assert 'Fixture "driver" called directly' in text

    check = _pass_check(result)
    assert not check["pass"] and check["gating"]
    assert "harness defect:" in check["actual"]


def test_mixed_failures_are_labelled_but_no_report_is_filed(verifier, tmp_path, monkeypatch):
    """Kickoff: 'Mixed failures are labelled, so the implementer knows which
    ones not to chase.' The report is filed only when EVERY failure is one."""
    run_dir = _make_run_dir(tmp_path, monkeypatch, SUITE_MIXED)
    result = _evaluate(verifier, run_dir, tmp_path)

    assert not (run_dir / REPORT).exists()
    assert result["harness_defects"] == [f"{SPEC_FILE}::{FIXTURE_TEST}"]

    by_test = {failure["test"]: failure for failure in result["failures"]}
    assert "harness_defect" in by_test[f"{SPEC_FILE}::{FIXTURE_TEST}"]
    assert "harness_defect" not in by_test[f"{SPEC_FILE}::{PLAIN_TEST}"]

    actual = _pass_check(result)["actual"]
    assert "harness defect:" in actual
    assert "AssertionError: assert False" in actual


def test_implementer_filed_report_is_left_byte_for_byte(verifier, tmp_path, monkeypatch):
    """Kickoff: 'if `<run_dir>/spec-test-defect.md` does not exist (never
    overwrite an implementer-filed one)'."""
    sentinel = b"# Spec-test defect (filed by the implementer)\n\nDo not overwrite me.\n"
    run_dir = _make_run_dir(tmp_path, monkeypatch, SUITE_FIXTURE_DEFECT)
    (run_dir / REPORT).write_bytes(sentinel)
    result = _evaluate(verifier, run_dir, tmp_path)

    assert (run_dir / REPORT).read_bytes() == sentinel
    assert result["harness_defects"] == [f"{SPEC_FILE}::{FIXTURE_TEST}"]


def test_all_passing_suite_has_no_harness_defects_and_no_report(verifier, tmp_path, monkeypatch):
    """Kickoff: '`result["harness_defects"]` is the sorted list of marked test
    ids, always present (`[]` when none).'"""
    run_dir = _make_run_dir(tmp_path, monkeypatch, SUITE_ALL_PASSING)
    result = _evaluate(verifier, run_dir, tmp_path)

    assert result["pass"] is True
    assert result["rc"] == 0
    assert result["harness_defects"] == []
    assert not (run_dir / REPORT).exists()
