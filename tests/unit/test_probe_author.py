"""Blind probe authorship: the held-out split and `acceptance-probes.json` (I2b).

The spec-test author writes probes from the kickoff alone. Without a split, the
implementer's revise round reads every failing clause, so it can satisfy each
probe by reading the answers to the exam it is sitting. The split withholds the
CLAUSE of some probes while keeping them in the verdict — the implementation is
still graded on them, it just cannot read what they say.

Two things must be true together, and each has its own way of silently
breaking:

* a held-out probe is still RUN and still counted (`spec-tests-pass` reads it),
  otherwise "held out" quietly means "ignored";
* nothing about it reaches the revise round — not its clause, and not the
  assertion's expected values, which the pytest exception text carries;
* and it must reach EVERY consumer. A parametrized case reports a nodeid the
  manifest never listed, so a raw-nodeid lookup marks it visible.

`<run_dir>/acceptance-probes.json` is the audit record of the split. It is
deliberately NOT under `gates/`: `probe_validity.load_probes` globs
`gates/*.json`, treats each entry's `probe` as an executable command and runs it
against the base tree, so a pytest nodeid there fails the I1 gate outright.
"""
from __future__ import annotations

import importlib.util
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
VERIFIER = REPO / "recipes" / "framework-edit" / "verifiers" / "spec-tests.py"

RUN_ID = "run-probe-author"
AUTHOR_NODE = "spec_test_author"
FENCE = 1_700_000_000


def _load_verifier_module():
    """Import the verifier as a module (it is a script, not an installed pkg)."""
    spec = importlib.util.spec_from_file_location("_spec_tests_under_test", VERIFIER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


VT = _load_verifier_module()


# Two probes that BOTH fail. One is held out; its clause must not reach the
# revise evidence, while the visible one's must.
TWO_FAILING = '''\
def test_visible_fails():
    """Kickoff: 'a rejected input fails closed'."""
    assert False, "visible probe not delivered"


def test_held_out_fails():
    """Kickoff: 'the fail-closed knob never returns an empty value'.

    Held-out: yes
    """
    assert False, "held-out probe not delivered"
'''

TWO_PASSING = '''\
def test_visible_holds():
    """Kickoff: 'a rejected input fails closed'."""
    assert True


def test_held_out_holds():
    """Kickoff: 'the fail-closed knob never returns an empty value'.

    Held-out: yes
    """
    assert True
'''

# Both held out — over half of two, which the bound forbids.
BOTH_HELD_OUT = '''\
def test_one():
    """Kickoff: 'clause one'.

    Held-out: yes
    """
    assert True


def test_two():
    """Kickoff: 'clause two'.

    Held-out: yes
    """
    assert True
'''


# ── harness (mirrors test_framework_edit_spec_tests.py) ─────────────────────

def _env(run_dir: Path, target: Path) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith(("MINI_ORK_", "MO_"))}
    env["MINI_ORK_RUN_DIR"] = str(run_dir)
    env["MINI_ORK_RUN_ID"] = RUN_ID
    env["MINI_ORK_DB"] = str(run_dir / "state.db")
    env["MO_TARGET_CWD"] = str(target)
    return env


def _seed_state_db(db: Path, author_end: int = FENCE) -> None:
    con = sqlite3.connect(str(db))
    con.execute(
        "CREATE TABLE run_events (event_id TEXT PRIMARY KEY, run_id TEXT, "
        "parent_run_id TEXT, event_type TEXT, payload_json TEXT, created_at INTEGER, "
        "finish_reason TEXT, last_heartbeat_at INTEGER)")
    con.execute(
        "INSERT INTO run_events VALUES (?,?,?,?,?,?,?,?)",
        (f"evt-node_end-{AUTHOR_NODE}-1", RUN_ID, None, "node_end",
         json.dumps({"node_id": AUTHOR_NODE, "node_type": "researcher",
                     "duration_ms": 12, "finish_reason": "done"}),
         author_end, "done", None))
    con.commit()
    con.close()


def _run_dir(tmp_path: Path, files: dict[str, str]) -> Path:
    run = tmp_path / "run"
    run.mkdir(parents=True)
    spec = run / "spec-tests"
    spec.mkdir()
    for name, body in files.items():
        path = spec / name
        path.write_text(body, encoding="utf-8")
        os.utime(path, (FENCE - 10, FENCE - 10))
    _seed_state_db(run / "state.db")
    return run


def _target(tmp_path: Path) -> Path:
    target = tmp_path / "target"
    target.mkdir(exist_ok=True)
    return target


def _invoke(run: Path, target: Path, timeout: int = 120):
    return subprocess.run(
        [sys.executable, str(VERIFIER)], cwd=str(target),
        env=_env(run, target), capture_output=True, text=True, timeout=timeout)


def _payload(run: Path) -> dict:
    return json.loads((run / "verifier_spec-tests.json").read_text())


# ── the marker ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize("doc", [
    'Kickoff: \'clause\'.\n\n    Held-out: yes\n    ',
    'Kickoff: \'clause\'.\nHeld-out: YES',
    'Kickoff: \'clause\'.\nHeld-out: `true`',
])
def test_held_out_reads_an_affirmative_mark(doc: str) -> None:
    assert VT.held_out(doc) is True


@pytest.mark.parametrize("doc", [
    'Kickoff: \'clause\'.',                      # absent — the pre-split default
    'Kickoff: \'clause\'.\nHeld-out: no',
    'Kickoff: \'clause\'.\nHeld-out: false',
    'Kickoff: \'clause\'.\nHeld-out:',
    None,
])
def test_held_out_absent_or_negative_is_visible(doc) -> None:
    assert VT.held_out(doc) is False


def test_held_out_refuses_a_typo_instead_of_guessing() -> None:
    """A typo must not silently make a probe visible when its author withheld it."""
    with pytest.raises(ValueError, match="Held-out"):
        VT.held_out('Kickoff: \'clause\'.\nHeld-out: yess')


# ── AC2: a held-out clause never reaches the revise evidence ────────────────

def test_held_out_clause_is_withheld_from_revise_evidence(tmp_path) -> None:
    run = _run_dir(tmp_path, {"test_spec_split.py": TWO_FAILING})
    proc = _invoke(run, _target(tmp_path))
    assert proc.returncode == 1, proc.stdout + proc.stderr
    payload = _payload(run)

    by_test = {f["test"]: f for f in payload["failures"]}
    visible = by_test["test_spec_split.py::test_visible_fails"]
    hidden = by_test["test_spec_split.py::test_held_out_fails"]

    # The visible probe keeps its quote — that is the implementer's evidence.
    assert "fails closed" in visible["kickoff"]
    assert not visible.get("held_out")

    # The held-out probe is IN the verdict but its clause is gone.
    assert hidden.get("held_out") is True
    assert hidden["kickoff"] == ""
    assert "empty value" not in json.dumps(payload["failures"])

    # Counted, not ignored: both probes red the gate.
    assert payload["pass"] is False
    pass_check = next(c for c in payload["checks"] if c["name"] == "spec-tests-pass")
    assert pass_check["pass"] is False
    # BOTH probes are named in the failure evidence: holding a clause back must
    # never mean the probe is dropped from what the gate reports.
    assert "test_visible_fails" in pass_check["actual"]
    assert "test_held_out_fails" in pass_check["actual"]


def test_held_out_failure_still_reaches_needs_change_without_its_clause(tmp_path) -> None:
    """The revise-facing text names the probe and says the clause is withheld."""
    run = _run_dir(tmp_path, {"test_spec_split.py": TWO_FAILING})
    _invoke(run, _target(tmp_path))
    payload = _payload(run)
    hidden = next(f for f in payload["failures"] if f.get("held_out"))
    rendered = VT._failure_clause(hidden)
    assert "held-out clause withheld" in rendered
    assert "empty value" not in rendered


# ── the manifest: an audit record, deliberately outside gates/ ─────────────

def test_acceptance_probes_manifest_is_written_outside_the_gates_dir(tmp_path) -> None:
    run = _run_dir(tmp_path, {"test_spec_split.py": TWO_PASSING})
    proc = _invoke(run, _target(tmp_path))
    assert proc.returncode == 0, proc.stdout + proc.stderr

    manifest_path = run / "acceptance-probes.json"
    assert manifest_path.is_file()
    manifest = json.loads(manifest_path.read_text())
    assert manifest["spec_id"] == RUN_ID

    probes = manifest["probes"]
    assert [p["id"] for p in probes] == [
        "test_spec_split.py::test_visible_holds",
        "test_spec_split.py::test_held_out_holds",
    ]
    assert all(p["clause"] for p in probes)
    assert [p["held_out"] for p in probes] == [False, True]

    # The bug this guards: `load_probes` globs `gates/*.json`, treats each
    # entry's `probe` as an EXECUTABLE command with an `expect`, and runs it
    # against the base tree. A pytest nodeid there fails the I1 validity gate
    # for every run, so the manifest must stay out of `gates/`.
    assert not (run / "gates").exists()
    from mini_ork.verify.probe_validity import load_probes

    _acceptance, loaded = load_probes(str(run), {})
    assert loaded == []


def test_manifest_is_written_even_when_the_suite_fails(tmp_path) -> None:
    """A failing suite must still record its probes and the split it declared."""
    run = _run_dir(tmp_path, {"test_spec_split.py": TWO_FAILING})
    _invoke(run, _target(tmp_path))
    manifest = json.loads((run / "acceptance-probes.json").read_text())
    assert len(manifest["probes"]) == 2


# ── the payload the revise round is pointed at must not carry the answer ───

def test_payload_redacts_held_out_clauses_the_audit_record_keeps_them(tmp_path) -> None:
    run = _run_dir(tmp_path, {"test_spec_split.py": TWO_PASSING})
    _invoke(run, _target(tmp_path))
    payload = _payload(run)

    by_id = {p["id"]: p for p in payload["probes"]}
    assert by_id["test_spec_split.py::test_visible_holds"]["clause"].startswith("Kickoff:")
    withheld = by_id["test_spec_split.py::test_held_out_holds"]
    assert withheld["held_out"] is True
    assert withheld["clause"] == ""

    # `verifier_spec-tests.json` is NAMED by the revise evidence, so a clause
    # in it is a clause the implementer can read. The audit record is not.
    assert "empty value" not in (run / "verifier_spec-tests.json").read_text()
    assert "empty value" in (run / "acceptance-probes.json").read_text()
    # No pointer to the audit record either — see `write_probe_manifest`.
    assert "probe_manifest" not in payload


def test_held_out_failure_withholds_the_assertion_error(tmp_path) -> None:
    """The pytest exception text carries the expected values — the answer."""
    body = '''\
def test_visible_fails():
    """Kickoff: 'a rejected input fails closed'."""
    assert False, "visible detail"


def test_held_out_fails():
    """Kickoff: 'the fail-closed knob never returns an empty value'.

    Held-out: yes
    """
    assert 7 == 42, "secret expectation"
'''
    run = _run_dir(tmp_path, {"test_spec_split.py": body})
    _invoke(run, _target(tmp_path))
    payload = _payload(run)

    visible = next(f for f in payload["failures"] if not f.get("held_out"))
    hidden = next(f for f in payload["failures"] if f.get("held_out"))
    # The visible probe keeps its error text; the held-out one keeps nothing.
    assert "error" in visible
    assert "error" not in hidden
    assert hidden["kickoff"] == ""
    assert "secret expectation" not in json.dumps(payload["failures"])


# ── the evidence log is a file the revise round can open ───────────────────

SECRET_ASSERTION = '''\
def test_visible_fails():
    """Kickoff: 'a rejected input fails closed'."""
    assert False, "visible detail"


def test_held_out_fails():
    """Kickoff: 'the fail-closed knob never returns an empty value'.

    Held-out: yes
    """
    assert 7 == 42, "secret expectation"
'''


def test_evidence_log_withholds_the_held_out_block(tmp_path) -> None:
    """pytest prints the failing test's SOURCE: a raw log is the clause, again.

    The verifier names `verifier-spec-tests.log` as evidence, so whatever it
    holds is the handout. The block goes; the probe's name stays.
    """
    run = _run_dir(tmp_path, {"test_spec_split.py": SECRET_ASSERTION})
    _invoke(run, _target(tmp_path))
    log = (run / "verifier-spec-tests.log").read_text()

    # The visible probe's block is untouched — it is the actionable evidence.
    assert "visible detail" in log
    assert "fails closed" in log

    # The held-out one contributes its nodeid and nothing else.
    assert "test_held_out_fails" in log
    assert "empty value" not in log
    assert "secret expectation" not in log
    assert "assert 7 == 42" not in log
    assert "[held-out probe" in log


def test_redaction_hides_a_setup_error_and_spares_the_summary() -> None:
    """A held-out test that dies in a fixture is still that test.

    The `-rA` summary repeats the nodeid below a `=====` banner; it is what
    keeps the probe in the verdict, so the banner must end the hidden block.
    """
    output = (
        "____ ERROR at setup of test_hidden ____\n"
        "fixture 'the empty-value knob' not found\n"
        "=== short test summary info ===\n"
        "FAILED test_spec.py::test_hidden\n")
    redacted = VT.redact_held_out(output, {"test_hidden"})
    assert "empty-value knob" not in redacted
    assert "FAILED test_spec.py::test_hidden" in redacted
    assert "[held-out probe" in redacted


# ── parametrized probes inherit the mark ───────────────────────────────────

PARAMETRIZED = '''\
import pytest


def test_visible_holds():
    """Kickoff: 'a rejected input fails closed'."""
    assert True


@pytest.mark.parametrize("key", ["", "x"])
def test_held_out_cases(key):
    """Kickoff: 'the fail-closed knob never returns an empty value'.

    Held-out: yes
    """
    assert key
'''


def test_parametrized_cases_inherit_the_held_out_mark(tmp_path) -> None:
    """A parametrized probe is ONE probe: one manifest entry, one held-out mark.

    `classify` walks the AST, so the manifest lists the function while pytest
    reports a case. Both sides are compared through `_unit_key`, which drops
    the `[case-id]`, so a failing case inherits the mark — in the payload and
    in the redacted evidence log alike.
    """
    run = _run_dir(tmp_path, {"test_spec_param.py": PARAMETRIZED})
    proc = _invoke(run, _target(tmp_path))
    assert proc.returncode == 1, proc.stdout + proc.stderr
    payload = _payload(run)

    failing = [f for f in payload["failures"] if "test_held_out_cases" in f["test"]]
    assert failing, payload["failures"]
    for failure in failing:
        assert failure.get("held_out") is True, failure
        assert failure["kickoff"] == ""
        assert "error" not in failure
    assert "empty value" not in json.dumps(payload["failures"])
    # One manifest entry per FUNCTION, not per case: the bound counts probes.
    manifest = json.loads((run / "acceptance-probes.json").read_text())
    assert [p["id"] for p in manifest["probes"]] == [
        "test_spec_param.py::test_visible_holds",
        "test_spec_param.py::test_held_out_cases",
    ]
    assert [p["held_out"] for p in manifest["probes"]] == [False, True]


# ── a typo is a failing check, never a crashed verifier ────────────────────

def test_malformed_marker_reds_a_check_instead_of_crashing(tmp_path) -> None:
    """A verifier owes its caller one JSON verdict; a traceback is neither."""
    body = '''\
def test_typo():
    """Kickoff: 'the clause'.

    Held-out: yess
    """
    assert True
'''
    run = _run_dir(tmp_path, {"test_spec_split.py": body})
    proc = _invoke(run, _target(tmp_path))
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "Traceback" not in (proc.stderr or "")
    payload = _payload(run)
    assert payload["pass"] is False
    check = next(c for c in payload["checks"] if c["name"] == "held-out-marker")
    assert check["pass"] is False
    assert "yess" in check["actual"]


# ── the bound ───────────────────────────────────────────────────────────────

def test_more_than_half_held_out_reds_the_gate_before_the_suite_runs(tmp_path) -> None:
    run = _run_dir(tmp_path, {"test_spec_split.py": BOTH_HELD_OUT})
    proc = _invoke(run, _target(tmp_path))
    assert proc.returncode == 1
    payload = _payload(run)
    assert payload["pass"] is False
    check = next(c for c in payload["checks"] if c["name"] == "held-out-bounds")
    assert check["pass"] is False
    assert "at most half" in check["actual"]
    # Refused before pytest ran: the implementation was never measured.
    assert payload["rc"] is None
    assert payload["failures"] == []


def test_declaring_no_split_is_reported_not_punished(tmp_path) -> None:
    one = '''\
def test_only():
    """Kickoff: 'the only clause'."""
    assert True
'''
    run = _run_dir(tmp_path, {"test_spec_split.py": one})
    proc = _invoke(run, _target(tmp_path))
    assert proc.returncode == 0, proc.stdout + proc.stderr
    payload = _payload(run)
    assert payload["pass"] is True
    assert payload["held_out"] == "no split declared (1 probe(s), all visible)"
    check = next(c for c in payload["checks"] if c["name"] == "held-out-split")
    assert check["pass"] is True
    assert check["gating"] is False
