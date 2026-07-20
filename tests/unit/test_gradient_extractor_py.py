"""Standalone contracts for the native gradient extractor."""

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
from mini_ork.learning import gradient_extractor as ge
from mini_ork.learning import reflection_pipeline as rp

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


def test_store_happy_path_round_trip(db):
    payload = {
        "gradient_id": "gr-roundtrip",
        "target": "workflow.node.planner",
        "signal": "slow",
        "suggested_change": "add cache",
        "evidence": "tr-abc",
        "confidence": 0.8,
    }
    rc, out, _ = _py_store(payload, db)
    assert rc == 0 and out.strip() == payload["gradient_id"]

    row = _row(db, payload["gradient_id"])
    for key in ("target", "signal", "suggested_change", "evidence"):
        assert row[key] == payload[key]
    assert abs(float(row["confidence"]) - float(payload["confidence"])) <= 1e-6


def test_store_upsert_updates_confidence(db):
    base = {
        "gradient_id": "gr-upsert",
        "target": "wf.node.A",
        "signal": "signal1",
        "suggested_change": "change1",
        "evidence": "tr-x",
        "confidence": 0.3,
    }
    rc, _, _ = _py_store(base, db)
    assert rc == 0
    rc, _, _ = _py_store({**base, "confidence": 0.7}, db)
    assert rc == 0
    assert abs(float(_row(db, "gr-upsert")["confidence"]) - 0.7) <= 1e-6


def test_store_semantic_dedup_and_explicit_id_contract(db, monkeypatch):
    trace_store.trace_write(
        {"trace_id": "tr-dedup", "task_class": "obs_smoke", "status": "success"},
        db=db,
    )
    first = {
        "target": "workflow.node.execute",
        "signal": (
            "run_id, workflow_version_id, prompt_version_hash, and "
            "context_bundle_hash are all empty on the execute trace"
        ),
        "suggested_change": "Stamp run lineage fields when writing the trace",
        "evidence": "tr-dedup",
        "confidence": 0.8,
    }
    second = {
        **first,
        "target": "workflow.node.obs_smoke",
        "signal": first["signal"].replace("execute trace", "obs_smoke trace"),
        "confidence": 0.9,
    }
    monkeypatch.setenv("MO_GRADIENT_DEDUP_SIM", "0.72")
    first_id = ge.store(first, db=db)
    second_id = ge.store(second, db=db)
    assert second_id == first_id
    assert float(_row(db, first_id)["confidence"]) == 0.9

    monkeypatch.setenv("MO_GRADIENT_DEDUP_SIM", "0")
    third_id = ge.store({**second, "confidence": 0.6}, db=db)
    assert third_id != first_id
    explicit = ge.store(
        {**second, "gradient_id": third_id, "confidence": 0.95}, db=db
    )
    assert explicit == third_id
    assert float(_row(db, third_id)["confidence"]) == 0.95


def test_store_invalid_json_exits_nonzero(db):
    rc, _, _ = _py_store("not-json", db)
    assert rc != 0


def test_store_missing_field_exits_nonzero(db):
    payload = {"target": "wf.node.X", "signal": "s"}
    rc, _, _ = _py_store(payload, db)
    assert rc != 0


def test_extract_via_override(db):
    trace_id = trace_store.trace_write({"trace_id": "tr-override", "task_class": "grad-test"}, db=db)

    def stub(_trace_id: str, _trace_json: str):
        return [{
            "target": "workflow.node.test",
            "signal": "test signal",
            "suggested_change": "test change",
            "confidence": 0.9,
        }]

    rc, out, _ = _py_extract(trace_id, db, override_fn=stub, env={"MINI_ORK_GRADIENT_EXTRACTOR_FN": "_stub_emit_one"})
    assert rc == 0
    p_item = json.loads(out.strip().splitlines()[-1])
    assert p_item["target"] == "workflow.node.test"


def test_extract_missing_trace_exits_nonzero(db):
    rc, _, _ = _py_extract("tr-doesnotexist", db)
    assert rc != 0


def test_framework_agent_policy():
    assert ge.is_framework_agent("__reflect__")
    assert ge.is_framework_agent("__future_agent__")
    assert not ge.is_framework_agent("framework_edit")
    assert not ge.is_framework_agent("")
    assert not ge.is_framework_agent(None)


def test_watermark_detects_evidence_link(db):
    ge.init_schema(db)
    assert not ge.has_watermark("tr-watermarked", db)
    ge.store({
        "gradient_id": "gr-watermarked",
        "target": "workflow.node.verify",
        "signal": "missed boundary",
        "suggested_change": "add a boundary assertion",
        "evidence": "tr-watermarked",
        "confidence": 0.8,
    }, db=db)
    assert ge.has_watermark("tr-watermarked", db)
    assert not ge.has_watermark("tr-fresh", db)


def test_parse_llm_output_recovers_fenced_and_truncated_arrays():
    fenced = """```json
[{"target":"workflow.node.plan","signal":"s","suggested_change":"c"}]
```"""
    truncated = (
        '[{"target":"workflow.node.plan","signal":"s1","suggested_change":"c1"},'
        '{"target":"workflow.node.verify","signal":"s2","suggested_change":"c2"}'
    )

    assert ge._parse_llm_output(fenced, "tr-fenced") == [{
        "target": "workflow.node.plan",
        "signal": "s",
        "suggested_change": "c",
        "evidence": "tr-fenced",
        "confidence": 0.5,
    }]
    recovered = ge._parse_llm_output(truncated, "tr-truncated")
    assert [item["target"] for item in recovered] == [
        "workflow.node.plan", "workflow.node.verify"
    ]
    assert {item["evidence"] for item in recovered} == {"tr-truncated"}


def test_extract_default_uses_native_dispatch(db, monkeypatch):
    trace_id = trace_store.trace_write(
        {"trace_id": "tr-native", "task_class": "grad-native"}, db=db
    )
    from mini_ork.dispatch import llm_dispatch as native_dispatch

    calls = []

    def fake(argv, *, root, dispatch_fn):
        calls.append((argv, root, dispatch_fn))
        print('[{"target":"workflow.node.verify","signal":"missed edge",'
              '"suggested_change":"add assertion","confidence":0.8}]', end="")
        return 0

    marker = lambda *args: 0
    monkeypatch.setattr(native_dispatch, "llm_dispatch", fake)
    monkeypatch.setenv("MINI_ORK_GRADIENT_MODEL", "glm_current")

    items = ge.extract(
        trace_id,
        db=db,
        dispatch_fn=marker,
        repo_root="/engine",
        emit=False,
    )

    assert items == [{
        "target": "workflow.node.verify",
        "signal": "missed edge",
        "suggested_change": "add assertion",
        "confidence": 0.8,
        "evidence": trace_id,
    }]
    argv, root, seen_marker = calls[0]
    assert root == "/engine" and seen_marker is marker
    assert argv[:4] == ["--model", "glm_current", "--node-type", "gradient-extract"]
    assert argv[-4:] == ["--timeout", "120", "--max-turns", "5"]
    assert "<<<TRACE_JSON>>>" not in argv[argv.index("--prompt-text") + 1]


def test_reflection_defaults_use_native_gradient_owner(monkeypatch):
    extracted = [{
        "target": "workflow.node.plan",
        "signal": "s",
        "suggested_change": "c",
        "evidence": "tr-1",
        "confidence": 0.7,
    }]
    calls = []

    monkeypatch.setattr(ge, "extract", lambda trace_id, emit: (
        calls.append(("extract", trace_id, emit)) or extracted
    ))
    monkeypatch.setattr(ge, "store", lambda payload: calls.append(("store", payload)))
    monkeypatch.setattr(ge, "init_schema", lambda: calls.append(("schema",)))

    assert rp._default_gradient_extract("tr-1") == [json.dumps(extracted[0])]
    rp._default_gradient_store(json.dumps(extracted[0]))
    rp._default_gradient_ensure_table()

    assert calls[0] == ("extract", "tr-1", False)
    assert calls[1][0] == "store"
    assert calls[2] == ("schema",)
