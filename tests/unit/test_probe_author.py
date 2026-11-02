"""Blind probe authorship: the held-out split and `acceptance-probes.json` (I2b).

The spec-test author writes probes from the kickoff alone. Without a split, the
implementer's revise round reads every failing clause, so it can satisfy each
probe by reading the answers to the exam it is sitting. The split withholds the
CLAUDE of some probes while keeping them in the verdict — the implementation is
still graded on them, it just cannot read what they say.

Two things must be true together, and each has its own way of silently
breaking:

* a held-out probe is still RUN and still counted (`spec-tests-pass` reads it),
  otherwise "held out" quietly means "ignored";
* its clause never reaches the revise round, otherwise the split is decorative.

`gates/acceptance-probes.json` is the machine-readable probe set. It is written
in the shape `mini_ork.verify.probe_validity.load_probes` already reads for
`gates/*.json`, so I1's validity gate binds to it with no new plumbing.
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


# ── AC3: acceptance-probes.json is readable by probe_validity ──────────────

def test_acceptance_probes_manifest_is_written_and_loadable(tmp_path) -> None:
    run = _run_dir(tmp_path, {"test_spec_split.py": TWO_PASSING})
    proc = _invoke(run, _target(tmp_path))
    assert proc.returncode == 0, proc.stdout + proc.stderr

    manifest_path = run / "gates" / "acceptance-probes.json"
    assert manifest_path.is_file()
    manifest = json.loads(manifest_path.read_text())
    assert manifest["spec_id"] == RUN_ID

    probes = manifest["probes"]
    assert [p["id"] for p in probes] == [
        "test_spec_split.py::test_visible_holds",
        "test_spec_split.py::test_held_out_holds",
    ]
    # Every probe quotes its kickoff clause: a probe with an empty clause is
    # not a probe of a criterion, and load_probes' own rule depends on it.
    assert all(p["clause"] for p in probes)
    assert [p["held_out"] for p in probes] == [False, True]

    # AC3 — I1's validity gate binds to the file with no new plumbing.
    from mini_ork.verify.probe_validity import load_probes

    _acceptance, loaded = load_probes(str(run), {})
    loaded_ids = {p.get("id") for p in loaded}
    assert loaded_ids == {p["id"] for p in probes}
    assert all(p["clause"] for p in loaded)


def test_manifest_is_written_even_when_the_suite_fails(tmp_path) -> None:
    """A failing suite must still declare its probes — I1 judges them either way."""
    run = _run_dir(tmp_path, {"test_spec_split.py": TWO_FAILING})
    _invoke(run, _target(tmp_path))
    manifest = json.loads((run / "gates" / "acceptance-probes.json").read_text())
    assert len(manifest["probes"]) == 2


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
