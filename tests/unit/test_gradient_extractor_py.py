"""Parity gate: mini_ork.ported.gradient_extractor vs lib/gradient_extractor.sh."""

from __future__ import annotations

import contextlib
import io
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
from mini_ork import trace_store  # noqa: E402
from mini_ork.ported import gradient_extractor as ge  # noqa: E402

GE_SH = REPO / "lib" / "gradient_extractor.sh"
TS_SH = REPO / "lib" / "trace_store.sh"


@pytest.fixture
def db(tmp_path):
    home = tmp_path / "home"
    dbp = str(home / "state.db")
    subprocess.run(
        ["bash", str(REPO / "db" / "init.sh")],
        env={**os.environ, "MINI_ORK_HOME": str(home), "MINI_ORK_DB": dbp},
        capture_output=True,
        text=True,
        check=True,
    )
    return dbp


def _bash(db: str, snippet: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    bash_env = {
        **os.environ,
        "MINI_ORK_ROOT": str(REPO),
        "MINI_ORK_DB": db,
        "MO_GRADIENT_DEDUP_SIM": "0",
    }
    if env:
        bash_env.update(env)
    return subprocess.run(
        ["bash", "-c", f'. "{TS_SH}" && . "{GE_SH}" && {snippet}'],
        env=bash_env,
        capture_output=True,
        text=True,
    )


def _py_store(payload: dict | str, db: str) -> tuple[int, str, str]:
    out = io.StringIO()
    err = io.StringIO()
    old = os.environ.get("MO_GRADIENT_DEDUP_SIM")
    os.environ["MO_GRADIENT_DEDUP_SIM"] = "0"
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            ge.store(payload, db=db)
        rc = 0
    except SystemExit as e:
        rc = int(e.code or 0)
    finally:
        if old is None:
            os.environ.pop("MO_GRADIENT_DEDUP_SIM", None)
        else:
            os.environ["MO_GRADIENT_DEDUP_SIM"] = old
    return rc, out.getvalue(), err.getvalue()


def _py_extract(trace_id: str, db: str, override_fn=None, env: dict[str, str] | None = None) -> tuple[int, str, str]:
    out = io.StringIO()
    err = io.StringIO()
    old = os.environ.get("MINI_ORK_GRADIENT_EXTRACTOR_FN")
    try:
        if env and "MINI_ORK_GRADIENT_EXTRACTOR_FN" in env:
            os.environ["MINI_ORK_GRADIENT_EXTRACTOR_FN"] = env["MINI_ORK_GRADIENT_EXTRACTOR_FN"]
        elif old is not None:
            os.environ.pop("MINI_ORK_GRADIENT_EXTRACTOR_FN", None)
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            ge.extract(trace_id, db=db, override_fn=override_fn)
        rc = 0
    except SystemExit as e:
        rc = int(e.code or 0)
    finally:
        if old is None:
            os.environ.pop("MINI_ORK_GRADIENT_EXTRACTOR_FN", None)
        else:
            os.environ["MINI_ORK_GRADIENT_EXTRACTOR_FN"] = old
    return rc, out.getvalue(), err.getvalue()


def _row(db: str, gid: str) -> dict:
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    row = con.execute("SELECT * FROM gradient_records WHERE gradient_id=?", (gid,)).fetchone()
    con.close()
    assert row is not None
    return dict(row)


def _all_rows(db: str) -> list[dict]:
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        "SELECT gradient_id, target, signal, suggested_change, evidence, confidence, task_class "
        "FROM gradient_records ORDER BY gradient_id"
    ).fetchall()
    con.close()
    return [dict(r) for r in rows]


def _assert_rows_match(left: list[dict], right: list[dict]) -> None:
    assert len(left) == len(right)
    for lrow, rrow in zip(left, right):
        assert set(lrow) == set(rrow)
        for key in lrow:
            if key == "confidence":
                assert abs(float(lrow[key]) - float(rrow[key])) <= 1e-6
            else:
                assert lrow[key] == rrow[key]


def test_store_happy_path_round_trip(db):
    payload = {
        "gradient_id": "gr-roundtrip",
        "target": "workflow.node.planner",
        "signal": "slow",
        "suggested_change": "add cache",
        "evidence": "tr-abc",
        "confidence": 0.8,
    }
    b = _bash(db, "gradient_store \"$1\"", env=None)
    assert b.returncode != 0

    b = _bash(db, 'gradient_store "$PAYLOAD"', {"PAYLOAD": json.dumps(payload)})
    assert b.returncode == 0 and b.stdout.strip() == payload["gradient_id"]
    py_payload = {**payload, "gradient_id": "gr-roundtrip-py"}
    rc, out, _ = _py_store(py_payload, db)
    assert rc == 0 and out.strip() == py_payload["gradient_id"]

    brow = _row(db, payload["gradient_id"])
    prow = _row(db, py_payload["gradient_id"])
    for key in ("target", "signal", "suggested_change", "evidence"):
        assert brow[key] == prow[key] == payload[key]
    assert abs(float(brow["confidence"]) - float(prow["confidence"])) <= 1e-6


def test_store_upsert_updates_confidence(db):
    base = {
        "gradient_id": "gr-upsert",
        "target": "wf.node.A",
        "signal": "signal1",
        "suggested_change": "change1",
        "evidence": "tr-x",
        "confidence": 0.3,
    }
    _bash(db, 'gradient_store "$PAYLOAD"', {"PAYLOAD": json.dumps(base)})
    _bash(db, 'gradient_store "$PAYLOAD"', {"PAYLOAD": json.dumps({**base, "confidence": 0.7})})
    rc, _, _ = _py_store({**base, "gradient_id": "gr-upsert-py"}, db)
    assert rc == 0
    rc, _, _ = _py_store({**base, "gradient_id": "gr-upsert-py", "confidence": 0.7}, db)
    assert rc == 0
    assert abs(float(_row(db, "gr-upsert")["confidence"]) - 0.7) <= 1e-6
    assert abs(float(_row(db, "gr-upsert-py")["confidence"]) - 0.7) <= 1e-6


def test_store_invalid_json_exits_nonzero(db):
    b = _bash(db, "gradient_store not-json")
    rc, _, _ = _py_store("not-json", db)
    assert b.returncode != 0
    assert rc != 0


def test_store_missing_field_exits_nonzero(db):
    payload = {"target": "wf.node.X", "signal": "s"}
    b = _bash(db, 'gradient_store "$PAYLOAD"', {"PAYLOAD": json.dumps(payload)})
    rc, _, _ = _py_store(payload, db)
    assert b.returncode != 0
    assert rc != 0


def test_extract_via_override(db):
    trace_id = trace_store.trace_write({"trace_id": "tr-override", "task_class": "grad-test"}, db=db)

    snippet = """
_stub_emit_one() {
  echo '{"target":"workflow.node.test","signal":"test signal","suggested_change":"test change","confidence":0.9}'
}
export MINI_ORK_GRADIENT_EXTRACTOR_FN=_stub_emit_one
gradient_extract "$1"
"""
    b = subprocess.run(
        ["bash", "-c", f'. "{TS_SH}" && . "{GE_SH}" && {snippet}', "_", trace_id],
        env={**os.environ, "MINI_ORK_ROOT": str(REPO), "MINI_ORK_DB": db},
        capture_output=True,
        text=True,
    )

    def stub(_trace_id: str, _trace_json: str):
        return [{
            "target": "workflow.node.test",
            "signal": "test signal",
            "suggested_change": "test change",
            "confidence": 0.9,
        }]

    rc, out, _ = _py_extract(trace_id, db, override_fn=stub, env={"MINI_ORK_GRADIENT_EXTRACTOR_FN": "_stub_emit_one"})
    assert b.returncode == 0 and rc == 0
    b_item = json.loads(b.stdout.strip().splitlines()[-1])
    p_item = json.loads(out.strip().splitlines()[-1])
    assert b_item["target"] == p_item["target"] == "workflow.node.test"


def test_extract_missing_trace_exits_nonzero(db):
    b = _bash(db, "unset MINI_ORK_GRADIENT_EXTRACTOR_FN; gradient_extract tr-doesnotexist")
    rc, _, _ = _py_extract("tr-doesnotexist", db)
    assert b.returncode != 0
    assert rc != 0


def test_db_row_diff_bash_vs_python(tmp_path):
    bash_db = str(tmp_path / "bash" / "state.db")
    py_db = str(tmp_path / "py" / "state.db")
    for dbp in (bash_db, py_db):
        subprocess.run(
            ["bash", str(REPO / "db" / "init.sh")],
            env={**os.environ, "MINI_ORK_HOME": str(Path(dbp).parent), "MINI_ORK_DB": dbp},
            capture_output=True,
            text=True,
            check=True,
        )
        trace_store.trace_write({"trace_id": "tr-shared", "task_class": "code_fix"}, db=dbp)

    payloads = [
        {
            "gradient_id": "gr-a",
            "target": "workflow.node.planner",
            "signal": "slow",
            "suggested_change": "add cache",
            "evidence": "tr-shared",
            "confidence": 0.3,
        },
        {
            "gradient_id": "gr-a",
            "target": "workflow.node.planner",
            "signal": "slow",
            "suggested_change": "add cache now",
            "evidence": "tr-shared",
            "confidence": 0.7,
        },
        {
            "gradient_id": "gr-b",
            "target": "workflow.edge.plan_to_review",
            "signal": "handoff omitted context",
            "suggested_change": "include reviewer contract",
            "evidence": "tr-shared",
            "confidence": 0.55,
        },
    ]
    for payload in payloads:
        b = _bash(bash_db, 'gradient_store "$PAYLOAD"', {"PAYLOAD": json.dumps(payload)})
        assert b.returncode == 0
        rc, _, _ = _py_store(payload, py_db)
        assert rc == 0

    _assert_rows_match(_all_rows(bash_db), _all_rows(py_db))
