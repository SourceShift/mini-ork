"""framework-edit blind spec-tests verifier (`verifiers/spec-tests.py`).

A framework-edit run passes today when the implementer's OWN tests pass — tests
that encode the implementer's reading of the kickoff, not the kickoff. The
`spec_test_author` node (a different model lane) writes pytest files from the
kickoff ALONE before any implementation exists; this verifier is what makes them
count, and what stops the implementer from quietly rewriting one to make a
failing clause pass.

Every case here is hermetic: a temp run dir with a temp state.db carrying the
author node's `node_end` event, a temp target dir, and the verifier run as a
subprocess exactly as the executor runs it (cf. test_framework_edit_kickoff_contract).
`MINI_ORK_*` / `MO_*` are scrubbed from the child env so a leaked harness
variable cannot make a case pass spuriously.

The node_end fence is a FIXED epoch rather than `now`, so "before the author
ended" and "after the author ended" are set with `os.utime` and no sleeps.
"""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[2]
VERIFIER = REPO / "recipes" / "framework-edit" / "verifiers" / "spec-tests.py"
WORKFLOW = REPO / "recipes" / "framework-edit" / "workflow.yaml"

RUN_ID = "run-spec-tests"
AUTHOR_NODE = "spec_test_author"
# The author node's node_end `created_at`, in epoch seconds as the engine writes
# it. Spec files are stamped 10 s before it; a tampered one 30 s after.
FENCE = 1_700_000_000

# A spec test that declares its kickoff clause and holds.
GOOD = '''\
def test_clause_holds():
    """Kickoff: 'the workflow rejects an edge that references an unknown node'."""
    assert 1 + 1 == 2
'''

# A spec test whose clause the change did NOT deliver.
BAD = '''\
def test_clause_holds():
    """Kickoff: 'a rejected input must fail closed, never return an empty value'."""
    assert False, "the kickoff clause was not delivered"
'''

# One marked test (runs) and one unmarked test whose body WOULD fail. If the
# unmarked one ever runs, the suite fails — which is how the deselection is
# proved rather than merely asserted.
MIXED = '''\
def test_marked():
    """Kickoff: 'the marked clause holds'."""
    assert True


def test_unmarked():
    assert False, "a test without a Kickoff: docstring must never run"
'''

# A marked test that hangs, for the timeout path.
SLOW = '''\
import time


def test_slow():
    """Kickoff: 'the suite finishes within the configured timeout'."""
    time.sleep(30)
'''


# ── harness ─────────────────────────────────────────────────────────────────

def _env(run_dir: Path, target: Path | None, extra: dict[str, str] | None = None) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith(("MINI_ORK_", "MO_"))}
    env["MINI_ORK_RUN_DIR"] = str(run_dir)
    env["MINI_ORK_RUN_ID"] = RUN_ID
    env["MINI_ORK_DB"] = str(run_dir / "state.db")
    if target is not None:
        env["MO_TARGET_CWD"] = str(target)
    if extra:
        env.update(extra)
    return env


def _seed_state_db(db: Path, *, author_end: int = FENCE, node_end: bool = True) -> None:
    """A minimal state.db with the author node's node_end row."""
    con = sqlite3.connect(str(db))
    con.execute(
        "CREATE TABLE run_events (event_id TEXT PRIMARY KEY, run_id TEXT, "
        "parent_run_id TEXT, event_type TEXT, payload_json TEXT, created_at INTEGER, "
        "finish_reason TEXT, last_heartbeat_at INTEGER)")
    if node_end:
        con.execute(
            "INSERT INTO run_events VALUES (?,?,?,?,?,?,?,?)",
            (f"evt-node_end-{AUTHOR_NODE}-1", RUN_ID, None, "node_end",
             json.dumps({"node_id": AUTHOR_NODE, "node_type": "researcher",
                         "duration_ms": 12, "finish_reason": "done"}),
             author_end, "done", None))
    con.commit()
    con.close()


def _run_dir(
    tmp_path: Path,
    *,
    files: dict[str, str] | None = None,
    mtimes: dict[str, float] | None = None,
    seed_db: bool = True,
    node_end: bool = True,
    spec_dir: bool = True,
    reply: str | None = None,
    reply_mtime: float = FENCE - 5,
) -> Path:
    """A run dir whose spec tests all predate the author's node_end unless a
    per-file mtime says otherwise. ``reply`` is the author's captured reply, as
    the researcher handler writes it (``context-spec_test_author.json``)."""
    run = tmp_path / "run"
    run.mkdir(parents=True)
    if files:
        spec = run / "spec-tests"
        spec.mkdir()
        for name, body in files.items():
            (spec / name).write_text(body, encoding="utf-8")
        for name in files:
            stamp = (mtimes or {}).get(name, FENCE - 10)
            os.utime(spec / name, (stamp, stamp))
    elif spec_dir:
        (run / "spec-tests").mkdir()
    if reply is not None:
        out = run / f"context-{AUTHOR_NODE}.json"
        out.write_text(reply, encoding="utf-8")
        os.utime(out, (reply_mtime, reply_mtime))
    if seed_db:
        _seed_state_db(run / "state.db", node_end=node_end)
    return run


def _target(tmp_path: Path) -> Path:
    target = tmp_path / "target"
    target.mkdir(exist_ok=True)
    return target


def _invoke(run_dir: Path, target: Path, *, extra=None, timeout: int = 120):
    return subprocess.run(
        [sys.executable, str(VERIFIER)], cwd=str(target),
        env=_env(run_dir, target, extra), capture_output=True, text=True, timeout=timeout)


def _payload(run_dir: Path) -> dict:
    return json.loads((run_dir / "verifier_spec-tests.json").read_text())


# ── the passing and failing paths ───────────────────────────────────────────

def test_passing_spec_tests_pass(tmp_path) -> None:
    run = _run_dir(tmp_path, files={"test_spec_a.py": GOOD})
    proc = _invoke(run, _target(tmp_path))
    assert proc.returncode == 0, proc.stdout + proc.stderr
    payload = _payload(run)
    assert payload["pass"] is True
    assert payload["verdict"] == "pass"
    assert payload["files"] == ["test_spec_a.py"]
    assert payload["selected"] == ["test_spec_a.py::test_clause_holds"]
    assert payload["deselected"] == []
    assert payload["tampered"] == []
    assert payload["rc"] == 0
    assert payload["author_node_end"] == FENCE
    assert "note" not in payload
    # `levels._contract_from_recipe_verifiers` needs a bool `pass` plus a list
    # `checks`; a payload without `checks` reads as unparsable.
    assert isinstance(payload["checks"], list)
    assert all(c["pass"] for c in payload["checks"])
    # The engine reads stdout back (`_handle_verifier` copies it over the JSON),
    # so the two copies must be the same envelope.
    assert json.loads(proc.stdout)["pass"] is True


def test_failing_spec_test_reports_the_test_and_its_kickoff_quote(tmp_path) -> None:
    run = _run_dir(tmp_path, files={"test_spec_a.py": BAD})
    proc = _invoke(run, _target(tmp_path))
    assert proc.returncode == 1
    payload = _payload(run)
    assert payload["pass"] is False
    assert payload["rc"] == 1
    assert payload["failed_checks"] == ["spec-tests-pass"]

    failures = {f["test"]: f["kickoff"] for f in payload["failures"]}
    assert "test_spec_a.py::test_clause_holds" in failures
    # The clause, verbatim — this is what the implementer's revise round reads.
    assert "a rejected input must fail closed, never return an empty value" \
        in failures["test_spec_a.py::test_clause_holds"]

    reason = payload["reason"]
    assert "spec-tests-pass failed" in reason
    assert "a rejected input must fail closed, never return an empty value" in reason

    log = (run / "verifier-spec-tests.log").read_text()
    assert "a rejected input must fail closed, never return an empty value" in log

    # …and the revise round the `retries` edge fires is built from this payload:
    # the clause must reach the implementer's feedback, not just the run dir.
    from mini_ork.cli import execute as ex
    section = ex._revise_failure_section(
        str(run), ("spec_test_verifier", "verifier", "verifier", "", "serial",
                   "verifiers/spec-tests.py"), "verdict_fail")
    assert "verifier_spec-tests.json" in section
    assert "a rejected input must fail closed, never return an empty value" in section


def test_a_module_that_fails_to_import_reports_each_of_its_clauses(tmp_path) -> None:
    """The likeliest failure of a blind test is an import of the module the
    kickoff names and the change never added. Every test in that module fails
    with its own clause, and the other modules still run."""
    broken = (
        "import module_the_change_never_added\n"
        "\n"
        "def test_one():\n"
        "    \"\"\"Kickoff: 'module_the_change_never_added exposes one'.\"\"\"\n"
        "\n"
        "def test_two():\n"
        "    \"\"\"Kickoff: 'module_the_change_never_added exposes two'.\"\"\"\n"
    )
    run = _run_dir(tmp_path, files={"test_spec_a.py": broken, "test_spec_b.py": GOOD})
    proc = _invoke(run, _target(tmp_path))
    assert proc.returncode == 1
    payload = _payload(run)
    assert payload["failed_checks"] == ["spec-tests-pass"]
    failures = {f["test"]: f["kickoff"] for f in payload["failures"]}
    assert set(failures) == {"test_spec_a.py::test_one", "test_spec_a.py::test_two"}
    assert "exposes one" in failures["test_spec_a.py::test_one"]
    assert "exposes two" in payload["reason"]
    log = (run / "verifier-spec-tests.log").read_text()
    assert "PASSED" in log and "test_spec_b.py::test_clause_holds" in log


# ── tamper detection ────────────────────────────────────────────────────────

def test_spec_file_touched_after_the_author_node_fails_and_never_runs(tmp_path) -> None:
    """The implementer "fixing" a failing clause by editing the test must not
    pass: the file's mtime is later than the author node's node_end. The suite is
    not even run — the tampered file's own failing body is never executed."""
    run = _run_dir(tmp_path, files={"test_spec_a.py": BAD},
                   mtimes={"test_spec_a.py": FENCE + 30})
    proc = _invoke(run, _target(tmp_path))
    assert proc.returncode == 1
    payload = _payload(run)
    assert payload["pass"] is False
    assert payload["tampered"] == ["test_spec_a.py"]
    assert payload["failed_checks"] == ["tamper-detection"]
    assert "spec tests modified after authoring: test_spec_a.py" in payload["reason"]
    assert payload["rc"] is None          # pytest never ran
    assert payload["failures"] == []


def test_file_written_before_the_author_node_ended_is_not_tampered(tmp_path) -> None:
    run = _run_dir(tmp_path, files={"test_spec_a.py": GOOD},
                   mtimes={"test_spec_a.py": FENCE - 1})
    assert _invoke(run, _target(tmp_path)).returncode == 0
    assert _payload(run)["tampered"] == []


def test_a_conftest_added_after_authoring_is_tamper(tmp_path) -> None:
    """A late `conftest.py` that flips failures to passes changes the verdict as
    surely as an edited test: every file in the spec dir is fenced."""
    hook = (
        "import pytest\n"
        "\n"
        "@pytest.hookimpl(hookwrapper=True)\n"
        "def pytest_runtest_makereport(item, call):\n"
        "    outcome = yield\n"
        "    outcome.get_result().outcome = 'passed'\n"
    )
    run = _run_dir(tmp_path, files={"test_spec_a.py": BAD, "conftest.py": hook},
                   mtimes={"conftest.py": FENCE + 30})
    proc = _invoke(run, _target(tmp_path))
    assert proc.returncode == 1
    payload = _payload(run)
    assert payload["tampered"] == ["conftest.py"]
    assert payload["failed_checks"] == ["tamper-detection"]
    assert payload["rc"] is None


def test_a_spec_test_deleted_after_authoring_fails(tmp_path) -> None:
    """Deletion leaves no mtime to compare; the author's own manifest is what
    names the file that is gone."""
    run = _run_dir(tmp_path, files={"test_spec_a.py": GOOD},
                   reply="Wrote two files.\n\nSpec tests written: test_spec_a.py, test_spec_b.py\n")
    proc = _invoke(run, _target(tmp_path))
    assert proc.returncode == 1
    payload = _payload(run)
    assert payload["manifest"] == ["test_spec_a.py", "test_spec_b.py"]
    assert payload["missing"] == ["test_spec_b.py"]
    assert payload["failed_checks"] == ["tamper-detection"]
    assert "spec tests deleted after authoring: test_spec_b.py" in payload["reason"]
    assert payload["rc"] is None


def test_deleting_the_whole_spec_dir_is_not_no_spec_tests(tmp_path) -> None:
    run = _run_dir(tmp_path, spec_dir=False, reply="Spec tests written: test_spec_a.py\n")
    proc = _invoke(run, _target(tmp_path))
    assert proc.returncode == 1
    payload = _payload(run)
    assert payload["missing"] == ["test_spec_a.py"]
    assert "note" not in payload


def test_a_manifest_in_a_json_reply_stops_at_its_own_line(tmp_path) -> None:
    """The researcher's output file is `context-<node>.json`, so the reply may be
    JSON: the manifest must not run on into the rest of the document and pick
    up a test file it merely mentions."""
    reply = json.dumps({"summary": "Done.\nSpec tests written: test_spec_a.py\n",
                        "notes": "did not read tests/unit/test_revise_loop.py"})
    run = _run_dir(tmp_path, files={"test_spec_a.py": GOOD}, reply=reply)
    proc = _invoke(run, _target(tmp_path))
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert _payload(run)["manifest"] == ["test_spec_a.py"]


def test_a_reply_rewritten_after_authoring_is_tamper(tmp_path) -> None:
    """Deleting the tests AND rewriting the manifest to `none` must not read as
    an honest "no spec tests authored"."""
    run = _run_dir(tmp_path, spec_dir=False, reply="Spec tests written: none\n",
                   reply_mtime=FENCE + 30)
    proc = _invoke(run, _target(tmp_path))
    assert proc.returncode == 1
    payload = _payload(run)
    assert payload["tampered"] == [f"context-{AUTHOR_NODE}.json"]
    assert payload["failed_checks"] == ["tamper-detection"]


def test_running_the_suite_again_is_not_tamper(tmp_path) -> None:
    """The verifier — and an implementer checking its change against the suite —
    makes pytest write `__pycache__` / `.pytest_cache` into spec-tests/. That is
    pytest, not a rewrite: the revise round's re-run must still pass."""
    run = _run_dir(tmp_path, files={"test_spec_a.py": GOOD})
    target = _target(tmp_path)
    assert _invoke(run, target).returncode == 0
    spec = run / "spec-tests"
    hand_env = {k: v for k, v in os.environ.items() if not k.startswith(("MINI_ORK_", "MO_"))}
    subprocess.run([sys.executable, "-m", "pytest", "-q", str(spec)], cwd=str(spec),
                   env=hand_env, capture_output=True, text=True, timeout=120)
    assert (spec / "__pycache__").is_dir()
    proc = _invoke(run, target)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert _payload(run)["tampered"] == []


# ── Kickoff: docstring gating (hard constraint c:0) ─────────────────────────

def test_test_without_kickoff_docstring_is_deselected_and_never_run(tmp_path) -> None:
    """`test_unmarked` asserts False: the run passing is the proof that it was
    deselected rather than executed."""
    run = _run_dir(tmp_path, files={"test_spec_a.py": MIXED})
    proc = _invoke(run, _target(tmp_path))
    assert proc.returncode == 0, proc.stdout + proc.stderr
    payload = _payload(run)
    assert payload["pass"] is True
    assert payload["selected"] == ["test_spec_a.py::test_marked"]
    assert payload["deselected"] == ["test_spec_a.py::test_unmarked"]
    assert payload["failures"] == []
    assert payload["deselected_reported"] >= 1     # pytest confirmed the deselect
    assert all(c["pass"] for c in payload["checks"])


def test_a_class_method_without_a_docstring_is_deselected_too(tmp_path) -> None:
    """pytest collects `Test*` class methods; the gate must cover that shape as
    well, or a docstring-less method would run."""
    body = (
        "class TestThing:\n"
        "    def test_marked(self):\n"
        "        \"\"\"Kickoff: 'the class clause holds'.\"\"\"\n"
        "        assert True\n"
        "\n"
        "    def test_unmarked(self):\n"
        "        assert False, 'a method without a Kickoff: docstring must never run'\n"
    )
    run = _run_dir(tmp_path, files={"test_spec_a.py": body})
    assert _invoke(run, _target(tmp_path)).returncode == 0
    payload = _payload(run)
    assert payload["selected"] == ["test_spec_a.py::TestThing::test_marked"]
    assert payload["deselected"] == ["test_spec_a.py::TestThing::test_unmarked"]


def test_a_deselect_that_would_shadow_a_marked_test_fails(tmp_path) -> None:
    """`--deselect` is a PREFIX match: deselecting unmarked `test_x` would also
    silence marked `test_x_extra`. Silently running less than reported is not a
    pass, so the run fails and names both."""
    body = (
        "def test_x_extra():\n"
        "    \"\"\"Kickoff: 'the marked clause holds'.\"\"\"\n"
        "    assert True\n"
        "\n"
        "def test_x():\n"
        "    assert True\n"
    )
    run = _run_dir(tmp_path, files={"test_spec_a.py": body})
    proc = _invoke(run, _target(tmp_path))
    assert proc.returncode == 1
    payload = _payload(run)
    assert payload["failed_checks"] == ["deselect-precision"]
    assert "prefix of a selected one" in payload["reason"]
    assert payload["rc"] is None          # the suite never ran


def test_a_skipping_marked_test_is_not_read_as_an_unmarked_one(tmp_path) -> None:
    """`-rA` reports a skip as `SKIPPED [1] file.py:N: reason` — no nodeid. That
    line must not be mistaken for an unmarked test that ran."""
    body = (
        "import pytest\n"
        "\n"
        "def test_skips():\n"
        "    \"\"\"Kickoff: 'a clause the platform cannot check'.\"\"\"\n"
        "    pytest.skip('not on this platform')\n"
        "\n"
        "def test_holds():\n"
        "    \"\"\"Kickoff: 'the clause holds'.\"\"\"\n"
        "    assert True\n"
    )
    run = _run_dir(tmp_path, files={"test_spec_a.py": body})
    proc = _invoke(run, _target(tmp_path))
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert _payload(run)["failed_checks"] == []


def test_file_with_no_kickoff_docstring_at_all_fails(tmp_path) -> None:
    """Files were authored but assert nothing about the kickoff: nothing is
    verifiable, so the run must not be certified."""
    run = _run_dir(tmp_path, files={"test_spec_a.py": "def test_x():\n    assert True\n"})
    proc = _invoke(run, _target(tmp_path))
    assert proc.returncode == 1
    payload = _payload(run)
    assert payload["failed_checks"] == ["kickoff-docstrings"]
    assert "nothing to verify" in payload["reason"]
    assert payload["rc"] is None


def test_unparsable_spec_file_fails_closed(tmp_path) -> None:
    run = _run_dir(tmp_path, files={"test_spec_a.py": "def test_x(:\n    pass\n"})
    proc = _invoke(run, _target(tmp_path))
    assert proc.returncode == 1
    payload = _payload(run)
    assert payload["failed_checks"] == ["spec-tests-parse"]
    assert "do not parse" in payload["reason"]


# ── no spec tests: an honest pass with a note ───────────────────────────────

def test_no_spec_tests_dir_passes_with_note(tmp_path) -> None:
    run = tmp_path / "run"
    run.mkdir(parents=True)
    _seed_state_db(run / "state.db")
    proc = _invoke(run, _target(tmp_path))
    assert proc.returncode == 0, proc.stdout + proc.stderr
    payload = _payload(run)
    assert payload["pass"] is True
    assert payload["note"] == "no spec tests authored"
    assert payload["files"] == []
    assert all(c["pass"] for c in payload["checks"])


def test_empty_spec_tests_dir_passes_with_note(tmp_path) -> None:
    run = _run_dir(tmp_path)          # the dir exists but holds no test_*.py
    proc = _invoke(run, _target(tmp_path))
    assert proc.returncode == 0
    assert _payload(run)["note"] == "no spec tests authored"


def test_author_reporting_none_passes_with_note(tmp_path) -> None:
    run = _run_dir(tmp_path, spec_dir=False,
                   reply="The kickoff declares no testable behaviour.\n\nSpec tests written: none\n")
    proc = _invoke(run, _target(tmp_path))
    assert proc.returncode == 0, proc.stdout + proc.stderr
    payload = _payload(run)
    assert payload["note"] == "no spec tests authored"
    assert payload["manifest"] == []


# ── timeout / fail-closed knobs ─────────────────────────────────────────────

def test_timeout_fails(tmp_path) -> None:
    run = _run_dir(tmp_path, files={"test_spec_a.py": SLOW})
    proc = _invoke(run, _target(tmp_path), extra={"MO_SPEC_TESTS_TIMEOUT_S": "2"})
    assert proc.returncode == 1
    payload = _payload(run)
    assert payload["pass"] is False
    assert payload["timed_out"] is True
    assert payload["failed_checks"] == ["spec-tests-run"]
    assert "timed out after 2s" in payload["reason"]


def test_malformed_timeout_fails_closed(tmp_path) -> None:
    """A knob that is PRESENT but not a positive integer is a broken knob, not
    permission to run the suite under some default."""
    run = _run_dir(tmp_path, files={"test_spec_a.py": GOOD})
    proc = _invoke(run, _target(tmp_path), extra={"MO_SPEC_TESTS_TIMEOUT_S": "soon"})
    assert proc.returncode == 1
    payload = _payload(run)
    assert payload["rc"] is None          # pytest never started under the broken knob
    assert "not an integer" in payload["reason"]


def test_missing_state_db_fails_closed(tmp_path) -> None:
    """A suite whose tamper check cannot run must not be certified: a skipped
    check is not a pass."""
    run = _run_dir(tmp_path, files={"test_spec_a.py": GOOD}, seed_db=False)
    proc = _invoke(run, _target(tmp_path))
    assert proc.returncode == 1
    payload = _payload(run)
    assert payload["failed_checks"] == ["tamper-detection"]
    assert "cannot establish spec_test_author node_end" in payload["reason"]
    assert payload["rc"] is None


def test_absent_author_node_end_event_fails_closed(tmp_path) -> None:
    run = _run_dir(tmp_path, files={"test_spec_a.py": GOOD}, node_end=False)
    proc = _invoke(run, _target(tmp_path))
    assert proc.returncode == 1
    payload = _payload(run)
    assert payload["failed_checks"] == ["tamper-detection"]
    assert f"no {AUTHOR_NODE} node_end" in payload["reason"]


# ── secrets never reach the spec suite ──────────────────────────────────────

def test_spec_suite_runs_under_a_scrubbed_env(tmp_path) -> None:
    """The suite is a target-repo test run: provider credentials must not reach
    it, or a spec test could charge the operator instead of asserting."""
    body = (
        "import os\n"
        "\n"
        "def test_env_is_scrubbed():\n"
        "    \"\"\"Kickoff: 'a spec test never sees provider credentials'.\"\"\"\n"
        "    for name in ('OPENAI_API_KEY', 'MINI_ORK_SECRETS', 'MINI_ORK_DB'):\n"
        "        assert name not in os.environ, name\n"
    )
    run = _run_dir(tmp_path, files={"test_spec_a.py": body})
    proc = _invoke(run, _target(tmp_path),
                   extra={"OPENAI_API_KEY": "sk-must-not-leak",
                          "MINI_ORK_SECRETS": "/secrets.local.sh"})
    assert proc.returncode == 0, proc.stdout + proc.stderr


# ── the change under test, not the checkout around the run dir ──────────────

def test_suite_imports_the_target_not_the_checkout_around_the_run_dir(tmp_path) -> None:
    """Production lays the run dir under the operator's checkout
    (`<checkout>/.mini-ork/runs/<id>`), whose pyproject sets `pythonpath = ["."]`.
    A plain `pytest <run_dir>/spec-tests` adopts that config and imports the
    CHECKOUT's module ahead of the target's — testing the old code. An ancestor
    conftest must not load either."""
    checkout = tmp_path / "checkout"
    (checkout / "specmod").mkdir(parents=True)
    (checkout / "specmod" / "__init__.py").write_text('WHO = "checkout"\n')
    (checkout / "pyproject.toml").write_text('[tool.pytest.ini_options]\npythonpath = ["."]\n')
    (checkout / "conftest.py").write_text("raise RuntimeError('an ancestor conftest loaded')\n")
    target = _target(tmp_path)
    (target / "specmod").mkdir()
    (target / "specmod" / "__init__.py").write_text('WHO = "target"\n')
    body = (
        "import subprocess\n"
        "import sys\n"
        "\n"
        "import specmod\n"
        "\n"
        "def test_in_process_import_is_the_target():\n"
        "    \"\"\"Kickoff: 'cwd = `MO_TARGET_CWD`'.\"\"\"\n"
        "    assert specmod.WHO == 'target', specmod.__file__\n"
        "\n"
        "def test_subprocess_import_is_the_target(tmp_path):\n"
        "    \"\"\"Kickoff: '`PYTHONPATH` = that dir'.\"\"\"\n"
        "    out = subprocess.run([sys.executable, '-c', 'import specmod; print(specmod.WHO)'],\n"
        "                         cwd=str(tmp_path), capture_output=True, text=True)\n"
        "    assert out.stdout.strip() == 'target', out.stderr\n"
    )
    run = _run_dir(checkout / ".mini-ork" / "runs", files={"test_spec_a.py": body})
    proc = _invoke(run, target)
    assert proc.returncode == 0, (run / "verifier-spec-tests.log").read_text()
    assert _payload(run)["selected"] == [
        "test_spec_a.py::test_in_process_import_is_the_target",
        "test_spec_a.py::test_subprocess_import_is_the_target"]


# ── engine contract ─────────────────────────────────────────────────────────

def test_payload_is_a_parseable_verifier_envelope(tmp_path) -> None:
    from mini_ork.verify import levels

    ok_run = _run_dir(tmp_path / "ok", files={"test_spec_a.py": GOOD})
    assert _invoke(ok_run, _target(tmp_path)).returncode == 0
    assert levels._contract_from_recipe_verifiers(str(ok_run))[0] == levels.PROVEN

    bad_run = _run_dir(tmp_path / "bad", files={"test_spec_a.py": BAD})
    assert _invoke(bad_run, _target(tmp_path)).returncode == 1
    assert levels._contract_from_recipe_verifiers(str(bad_run))[0] == levels.REFUTED


# ── workflow wiring ─────────────────────────────────────────────────────────

def test_workflow_compiles_with_the_blind_spec_test_nodes() -> None:
    from mini_ork.workflow.compiler import compile_workflow

    compiled = compile_workflow(WORKFLOW)

    author = compiled.nodes["spec_test_author"]
    assert author.type == "researcher"
    assert author.model_lane == "glm_lens"
    assert author.prompt_ref == "prompts/spec-test-author.md"
    assert author.dispatch_mode == "serial"
    # Blindness is structural: a different model from the implementer, and the
    # author finishes before the implementer starts.
    assert author.model_lane != compiled.nodes["implementer"].model_lane
    order = compiled.topological_order
    assert order.index("spec_test_author") < order.index("implementer")
    assert "planner" in compiled.control_parents["spec_test_author"]
    assert "spec_test_author" in compiled.control_parents["implementer"]

    verifier = compiled.nodes["spec_test_verifier"]
    assert verifier.type == "verifier"
    assert verifier.verifier_ref == "verifiers/spec-tests.py"
    assert verifier.model_lane == "verifier"
    assert verifier.prompt_ref == ""
    assert verifier.dispatch_mode == "serial"
    # verifies: implementer -> spec_test_verifier
    assert "implementer" in compiled.control_parents["spec_test_verifier"]
    # depends_on: spec_test_verifier -> reviewer
    assert "spec_test_verifier" in compiled.control_parents["reviewer"]
    # retries: spec_test_verifier -> implementer, max_rounds 2
    retry = compiled.retry_edges["spec_test_verifier"]
    assert retry.target == "implementer"
    assert retry.max_rounds == 2

    # escalates_to -> rollback. framework-edit declares no `recursion:` block, so
    # the compiler keeps escalation edges out of the compiled graph; assert the
    # declared edge in the YAML itself.
    edges = yaml.safe_load(WORKFLOW.read_text())["edges"]
    assert {"from": "spec_test_verifier", "to": "rollback",
            "edge_type": "escalates_to"} in edges


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
