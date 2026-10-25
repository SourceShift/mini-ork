"""The ``db:<RULE>`` success check — the predicate a gap is judged against.

A candidate's ``success_command`` has to be something the cycle can actually run,
and for a gap found by re-reading the DB that command is "run the same rule
again". These tests pin the three properties that make it usable:

* **It is the harvest's own predicate.** The check re-runs one rule, so "fixed"
  means exactly "stops firing" — no second, drifting definition of success.
* **It fails closed where harvest fails open.** Harvest treats a missing table as
  "this home cannot answer" and moves on; a *success check* that did the same
  would report ``ok`` for a table that was never there, and G8 would merge a fix
  against a data path that never existed. Skip, drift and crash are all
  ``error`` here.
* **It reads its DB explicitly when the caller has one.** Only the probe-runner
  path goes through ``MINI_ORK_DB``, because the runner gives a probe no other
  channel.
"""
from __future__ import annotations

import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest

from mini_ork.rsi.gates import GateEvidence, evaluate
from mini_ork.rsi.harvest import rule_ids
from mini_ork.rsi.probes import (
    SYNTHETIC_PREFIX,
    run_probe,
    run_synthetic,
    synthetic_spec,
)

NOW = 1_792_000_000
CLOSED = "UPDATE lesson_injections SET held_out = 1 WHERE id = 1"


def _db(tmp_path: Path, sql: str) -> Path:
    path = tmp_path / "state.db"
    con = sqlite3.connect(path)
    con.executescript(sql)
    con.commit()
    con.close()
    return path


OPEN_GAP = (
    "CREATE TABLE lesson_injections (id INTEGER, held_out INTEGER);"
    "INSERT INTO lesson_injections VALUES (1,0),(2,0);"
)


# ── the registry ───────────────────────────────────────────────────────────


#: The DB rules. L8, V10 and V11 are probe-derived (verifier audit, the tests
#: probe, garden/validate findings) and have no ``db:`` success state, so they
#: are correctly absent here.
EXPECTED_RULES = (
    *(f"L{i}" for i in (1, 2, 3, 4, 5, 6, 7, 9, 10, 11, 12)),
    *(f"R{i}" for i in range(1, 9)),
    *(f"V{i}" for i in (1, 2, 3, 4, 5, 6, 7, 8, 9, 12)),
)


def test_every_db_rule_has_a_synthetic_check():
    ids = rule_ids()
    assert len(set(ids)) == len(ids), "duplicate rule id"
    assert set(ids) == set(EXPECTED_RULES), set(ids) ^ set(EXPECTED_RULES)
    for rule_id in ids:
        spec = synthetic_spec(rule_id)
        assert spec.probe_id == f"{SYNTHETIC_PREFIX}{rule_id}"


def test_an_unknown_rule_has_no_success_state():
    with pytest.raises(LookupError, match="nope"):
        synthetic_spec("nope")


def test_the_spec_is_an_in_process_probe_with_no_argv():
    spec = synthetic_spec("L3")
    assert spec.fn is not None and spec.argv == ()
    assert spec.policy == "ok_only"


# ── running it ─────────────────────────────────────────────────────────────


def test_a_still_open_gap_is_a_finding_not_a_success(tmp_path):
    db = _db(tmp_path, OPEN_GAP)
    result = run_synthetic("L3", root=tmp_path, db=db, now=NOW)
    assert result.status == "findings"
    assert result.payload["remaining"] == 1
    assert len(result.findings) == 1
    assert "L3" in result.findings[0].message


def test_closing_the_gap_flips_the_check_to_ok(tmp_path):
    db = _db(tmp_path, OPEN_GAP)
    con = sqlite3.connect(db)
    con.execute(CLOSED)
    con.commit()
    con.close()
    result = run_synthetic("L3", root=tmp_path, db=db, now=NOW)
    assert result.status == "ok"
    assert result.payload["remaining"] == 0
    assert result.findings == ()


def test_an_absent_table_is_an_error_though_harvest_would_skip_it(tmp_path):
    # The opposite of harvest's semantics, on purpose: G8 must not read
    # "cannot answer" as "fixed".
    db = _db(tmp_path, "CREATE TABLE unrelated (id INTEGER);")
    result = run_synthetic("L3", root=tmp_path, db=db, now=NOW)
    assert result.status == "error"
    assert "cannot answer" in result.payload["error"]


def test_schema_drift_is_an_error(tmp_path):
    db = _db(tmp_path, "CREATE TABLE lesson_injections (id INTEGER);")
    result = run_synthetic("L3", root=tmp_path, db=db, now=NOW)
    assert result.status == "error"


def test_an_unknown_rule_is_a_caller_bug_not_a_probe_status(tmp_path):
    # select() rejects such a candidate pre-dispatch (missing_success_command),
    # so reaching this is a programming error and must not look like a probe
    # that merely failed to run.
    with pytest.raises(LookupError):
        run_synthetic("nope", root=tmp_path, db=_db(tmp_path, OPEN_GAP), now=NOW)


def test_gap_closed_problems_are_a_skip_subclass_so_harvest_is_unchanged():
    from mini_ork.rsi.harvest import _GapClosed, _SkipRule

    assert issubclass(_GapClosed, _SkipRule)
    assert not issubclass(_SkipRule, _GapClosed)


def test_an_explicit_db_wins_over_the_environment(tmp_path, monkeypatch):
    db = _db(tmp_path, OPEN_GAP)
    monkeypatch.setenv("MINI_ORK_DB", str(tmp_path / "does-not-exist.db"))
    assert run_synthetic("L3", root=tmp_path, db=db, now=NOW).status == "findings"


def test_the_runner_path_reads_the_db_from_the_environment(tmp_path, monkeypatch):
    # What `cycle` does: the ordinary runner, one spec, no explicit db argument.
    db = _db(tmp_path, OPEN_GAP)
    monkeypatch.setenv("MINI_ORK_DB", str(db))
    monkeypatch.setenv("MINI_ORK_ROOT", str(tmp_path))
    result = run_probe(synthetic_spec("L3"), root=tmp_path)
    assert result.status == "findings"
    assert result.payload["remaining"] == 1


def test_the_runner_path_errors_without_a_db(tmp_path, monkeypatch):
    monkeypatch.delenv("MINI_ORK_DB", raising=False)
    assert run_probe(synthetic_spec("L3"), root=tmp_path).status == "error"


# ── why it exists: G8 can judge it ─────────────────────────────────────────


def test_g8_passes_on_evidence_the_synthetic_check_produced(tmp_path):
    db = _db(tmp_path, OPEN_GAP)
    before = run_synthetic("L3", root=tmp_path, db=db, now=NOW)
    con = sqlite3.connect(db)
    con.execute(CLOSED)
    con.commit()
    con.close()
    after = run_synthetic("L3", root=tmp_path, db=db, now=NOW)

    ev = GateEvidence(
        harvest_ok=True,
        child_verdict={"pass": True, "files_changed": 1},
        changed_files=("mini_ork/learning/ledger.py",),
        scope_files=("mini_ork/learning/ledger.py",),
        lint_ok=True, pytest_ok=True,
        static_ran=True, probes_ran=True,
        gap_before=bool(before.findings),
        gap_after=bool(after.findings),
        metric_command="db:L3",
        critics=(),
        daily_cap=50.0, per_cycle_cap=20.0, spend_24h=1.0, cycle_cost=1.0,
    )
    g8 = {r.gate: r for r in evaluate(ev).results}["G8"]
    assert g8.ok, g8.detail

    # and the same evidence with the fix not applied must fail
    stuck = replace(ev, gap_after=True)
    assert {r.gate: r for r in evaluate(stuck).results}["G8"].ok is False
