"""Verifier node → ``verifier_results`` wire.

Before this wire the ``verifier_results`` table (migration 0025) was dead
schema: the writer ``gates.verifier_rubric.verifier_result_record`` existed and
was unit-tested, the consumer ``gates.abstain_gate`` read it as the verifier's
labelled calibration history, and nothing on the run path ever wrote a row —
while 8k+ ``execution_traces`` claimed ``reward_source='verifier@v1'``. These
tests drive a real verifier node through ``execute.dispatch_node`` and assert
the row lands with the right ``verdict`` and identity.

Three outcomes:
  * pass  — the verifier script exits 0 → ``verdict='pass'``;
  * fail  — the verifier script exits non-zero → ``verdict='fail'``;
  * vacuous — the run-local required artifact is missing so the verifier never
    runs → ``verdict='vacuous'`` (the DDL's "produced nothing" value).
"""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
from mini_ork.cli import execute as ex  # noqa: E402

# G8's ``_exercise_once`` (mini_ork/rsi/cycle.py) runs this file's scoped tests
# against a *copy* of the measured DB and exports that copy as ``MINI_ORK_DB``.
# The autouse fixture in ``tests/conftest.py`` pops ``MINI_ORK_DB`` before every
# test (it must, or a stray test writes into the operator's live DB), so the
# value is only visible at import time. Capture it here; ``_is_exercise_copy``
# accepts only G8's copy, never an arbitrary inherited live DB.
_G8_DB = os.environ.get("MINI_ORK_DB") or ""

# Two rubric profiles with four decisive items each and opposite agreement with
# a pass row: profile A matches 3 of 4 (0.75), profile B matches 1 of 4 (0.25).
# Same items must replay to the same value; a constant, default or back-filled
# number cannot satisfy both.
PROFILE_A = [
    {"label": "build", "verdict": "PASS", "note": "clean"},
    {"label": "tests", "verdict": "pass", "note": "green"},
    {"label": "lint", "verdict": "Pass", "note": "clean"},
    {"label": "docs", "verdict": "FAIL", "note": "stale"},
]
PROFILE_B = [
    {"label": "build", "verdict": "PASS", "note": "clean"},
    {"label": "tests", "verdict": "FAIL", "note": "red"},
    {"label": "lint", "verdict": "fail", "note": "findings"},
    {"label": "docs", "verdict": "Fail", "note": "missing"},
]


def _is_exercise_copy(db: str) -> bool:
    """True only for G8's ``_exercise_once`` copy of the measured DB.

    Its shape (``mini_ork/rsi/cycle.py``) is
    ``<home>/rsi/<cycle_id>/exercise-{base,post}.db`` beside an
    ``exercise-{base,post}-home`` dir. A node lane also exports ``MINI_ORK_DB``
    pointing at the operator's live ``state.db``; the shape check keeps this
    file from writing there.
    """
    path = Path(db)
    return (path.name.startswith("exercise-") and path.suffix == ".db"
            and path.parent.parent.name == "rsi"
            and (path.parent / f"{path.stem}-home").is_dir())


def _seed(tmp: Path) -> str:
    home = tmp / "db" / ".mini-ork"
    home.mkdir(parents=True)
    db = str(home / "state.db")
    subprocess.run(["bash", str(REPO / "db" / "init.sh")],
                   env={**os.environ, "MINI_ORK_HOME": str(home), "MINI_ORK_DB": db},
                   capture_output=True, text=True, check=True)
    con = sqlite3.connect(db)
    con.execute("INSERT INTO task_runs (id,task_class,workflow_version,kickoff_path,status,cost_usd,created_at,"
                "updated_at) VALUES ('r1','x','v1','k.md','executing',0,strftime('%s','now'),strftime('%s','now'))")
    con.commit()
    con.close()
    return db


def _fields(node_id: str, vref: str = ""):
    return (node_id, "verifier", f"do {node_id}", "", "serial", vref, "verifier", "")


def _rows(db: str) -> list[tuple]:
    con = sqlite3.connect(db)
    try:
        return con.execute(
            "SELECT verdict, verifier_name, scored_axes_json FROM verifier_results "
            "WHERE run_id='r1' ORDER BY created_at"
        ).fetchall()
    finally:
        con.close()


def _pre_impl_run(tmp: Path, db: str, monkeypatch, node_id: str, vref: str,
                  run_verifier, *, rubric_items=None,
                  run_id: str = "r1") -> tuple[int, str]:
    """Drive a pre-implementation verifier node (its artifact guard is skipped).

    Mirrors ``test_verifier_nodes_run._self_migrate``: the self-migrate recipe's
    ``pre_retirement_parity`` node runs before any implementer, so the hollow-run
    guard is bypassed and the node reaches its verifier script.
    """
    rd = tmp / "run"
    rd.mkdir(exist_ok=True)
    # A scored rubric next to the run so scored_axes_json is populated and the
    # measured confidence has items to agree with.
    items = rubric_items if rubric_items is not None else [
        {"label": "L", "verdict": "PASS", "note": "n"}]
    (rd / "rubric.json").write_text(json.dumps({"pass": True, "score": 6, "items": items}))
    plan = tmp / "plan.json"
    plan.write_text(json.dumps(
        {"objective": "o", "artifact_contract": {"outputs": [str(rd / "self-migrate.diff")]}}))
    monkeypatch.setattr(ex, "_run_verifier_ref", run_verifier)
    return ex.dispatch_node(_fields(node_id, vref), root=str(REPO), run_dir=str(rd),
                            plan_path=str(plan), task_class="self_migrate", db=db, run_id=run_id,
                            dispatch_fn=lambda *a: (0, ""), recipe="self-migrate",
                            workflow=str(REPO / "recipes" / "self-migrate" / "workflow.yaml"))


def _confidence_rows(db: str, run_id: str) -> list[tuple[str, float | None]]:
    """``(verdict, confidence)`` for one run, in write order.

    ``rowid`` rather than ``created_at``: a tight loop writes several rows in
    the same second, and ``created_at`` ties are unordered.
    """
    con = sqlite3.connect(db)
    try:
        return con.execute(
            "SELECT verdict, confidence FROM verifier_results "
            "WHERE run_id=? ORDER BY rowid", (run_id,)).fetchall()
    finally:
        con.close()


def _null_ratio(db: str) -> tuple[int, int]:
    """``(total, null_confidence_rows)`` over the whole ``verifier_results`` table."""
    con = sqlite3.connect(db)
    try:
        total = con.execute("SELECT count(*) FROM verifier_results").fetchone()[0]
        nulls = con.execute(
            "SELECT count(*) FROM verifier_results WHERE confidence IS NULL").fetchone()[0]
    finally:
        con.close()
    return total, nulls


def test_passing_verifier_records_a_pass_row(tmp_path, monkeypatch) -> None:
    db = _seed(tmp_path)

    def passing(script, evidence_path, **_kw):
        Path(evidence_path).write_text('{"pass":true}\n')
        return 0

    rc, fr = _pre_impl_run(tmp_path, db, monkeypatch, "pre_retirement_parity",
                           "verifiers/pre-retirement-parity.py", passing)
    assert (rc, fr) == (0, "done")
    rows = _rows(db)
    assert len(rows) == 1
    verdict, name, axes = rows[0]
    assert verdict == "pass"
    # verifier_name is the verifier_ref stem — the identity the abstain gate groups by.
    assert name == "pre-retirement-parity"
    # The scored rubric axes ride along so the verdict is auditable.
    assert axes is not None and json.loads(axes)[0]["label"] == "L"


def test_failing_verifier_records_a_fail_row(tmp_path, monkeypatch) -> None:
    db = _seed(tmp_path)

    def failing(script, evidence_path, **_kw):
        Path(evidence_path).write_text('{"pass":false}\n')
        return 1

    rc, fr = _pre_impl_run(tmp_path, db, monkeypatch, "pre_retirement_parity",
                           "verifiers/pre-retirement-parity.py", failing)
    assert (rc, fr) == (1, "error")
    rows = _rows(db)
    assert len(rows) == 1 and rows[0][0] == "fail"


def test_verifier_that_never_runs_records_vacuous(tmp_path) -> None:
    db = _seed(tmp_path)
    rd = tmp_path / "run"
    rd.mkdir()
    plan = tmp_path / "plan.json"
    # framework_edit + a required run-local artifact that is absent → the
    # hollow-run guard fails the node before the verifier script runs.
    plan.write_text(json.dumps(
        {"objective": "o", "artifact_contract": {"required_artifacts": [str(rd / "x.diff")]}}))
    rc, fr = ex.dispatch_node(_fields("hollow"), root=str(REPO), run_dir=str(rd),
                              plan_path=str(plan), task_class="framework_edit", db=db,
                              run_id="r1", dispatch_fn=lambda *a: (0, ""))
    assert (rc, fr) == (1, "error")
    rows = _rows(db)
    assert len(rows) == 1 and rows[0][0] == "vacuous"


def test_recorded_confidence_is_the_rubric_agreement_fraction(tmp_path, monkeypatch) -> None:
    """The stored value is measured from the scored items, not defaulted.

    Two profiles over the same real ``dispatch_node`` path: A (3 of 4 decisive
    items agree with ``pass``) must store 0.75, B (1 of 4) must store 0.25.
    Replaying A stores the same value again, so a constant, default or
    back-filled number cannot pass.
    """
    db = _seed(tmp_path)

    def passing(script, evidence_path, **_kw):
        Path(evidence_path).write_text('{"pass":true}\n')
        return 0

    for items in (PROFILE_A, PROFILE_A, PROFILE_B):
        rc, fr = _pre_impl_run(tmp_path, db, monkeypatch, "pre_retirement_parity",
                               "verifiers/pre-retirement-parity.py", passing,
                               rubric_items=items, run_id="profile-run")
        assert (rc, fr) == (0, "done")

    rows = _confidence_rows(db, "profile-run")
    assert [verdict for verdict, _ in rows] == ["pass", "pass", "pass"]
    a1, a2, b = (confidence for _, confidence in rows)
    assert a1 is not None and a2 is not None and b is not None, (
        "a verifier run with a scored rubric must record a measured confidence")
    for value in (a1, a2, b):
        assert isinstance(value, float) and 0.0 <= value <= 1.0
    assert a1 == 0.75  # A: 3 of 4 decisive items agree with pass
    assert b == 0.25   # B: 1 of 4
    assert a1 == a2    # same items -> same value
    assert a1 != b     # different items -> different value


def test_g8_exercised_copy_crosses_v1s_null_ratio(tmp_path, monkeypatch) -> None:
    """``db:V1`` closes only when the *producer* writes non-NULL confidence.

    G8 (``rsi.cycle._exercise_once``) re-asks ``db:V1`` after running this
    file against a copy of the measured DB; the rule's success metric is a
    NULL-confidence ratio below 0.99. Inserting rows directly — or calling
    ``_record_verifier_result`` directly — would fake that measurement, so this
    drives the same ``dispatch_node`` path a real run takes, enough times to
    move the ratio on a corpus of that size. It skips cleanly unless
    ``MINI_ORK_DB`` is G8's exercise copy, so the ordinary suite stays
    hermetic and never touches a live state DB.
    """
    db = _G8_DB
    if not _is_exercise_copy(db):
        pytest.skip("G8 did not supply an exercised measured-corpus DB")

    total, _nulls = _null_ratio(db)
    if total <= 100:
        pytest.skip(f"exercised corpus too small for db:V1 ({total} rows)")

    # Least n with nulls / (total + n) < 0.99. floor(total/99) + 1 also clears
    # the boundary when total is an exact multiple of 99, where ceil lands on
    # the threshold itself.
    needed = total // 99 + 1

    def passing(script, evidence_path, **_kw):
        Path(evidence_path).write_text('{"pass":true}\n')
        return 0

    for i in range(needed):
        rc, fr = _pre_impl_run(
            tmp_path, db, monkeypatch, "pre_retirement_parity",
            "verifiers/pre-retirement-parity.py", passing,
            rubric_items=PROFILE_A if i % 2 == 0 else PROFILE_B,
            run_id="g8-confidence")
        assert (rc, fr) == (0, "done")

    total_after, nulls_after = _null_ratio(db)
    assert total_after == total + needed, "each producer call must add one row"
    assert nulls_after / total_after < 0.99, (
        f"db:V1 still fires: {nulls_after}/{total_after} rows have NULL confidence")
