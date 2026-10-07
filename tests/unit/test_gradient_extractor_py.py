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
    # BUG5: a near-duplicate is ABSORBED into the existing gradient without
    # ratcheting its confidence up. The old contract raised 0.8 -> 0.9 on every
    # re-sighting, which let a repeated confabulation climb past the 0.6
    # injection bar purely by being re-emitted. Confidence must stay 0.8.
    assert float(_row(db, first_id)["confidence"]) == 0.8

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
    # duration_ms>0 keeps this out of the degenerate-node skip (BUG4a): a
    # zero-cost, zero-duration, no-evidence trace is a control node the
    # extractor now abstains on. This test exercises the override wiring, so
    # give it a real work signal.
    trace_id = trace_store.trace_write(
        {"trace_id": "tr-override", "task_class": "grad-test", "duration_ms": 1200},
        db=db,
    )

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
    # verifier_output makes this a grounded node so neither the degenerate-node
    # skip (BUG4a) nor the ungrounded-confidence cap (BUG4b) fires — this test
    # asserts the native-dispatch argv contract, not the grounding gates, so the
    # extracted item must pass through with its original 0.8 confidence.
    trace_id = trace_store.trace_write(
        {
            "trace_id": "tr-native",
            "task_class": "grad-native",
            "duration_ms": 1200,
            "verifier_output": {"verdict": "pass"},
        },
        db=db,
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


def test_extract_failure_surfaces_lane_and_provider_stderr(db, monkeypatch):
    """A failed native dispatch raises SystemExit; stderr names the lane AND the provider message.

    Pre-fix, `_default_dispatch` dropped stderr and `extract` died with the bare
    string "gradient_extract: LLM dispatch failed", masking the actual provider
    error (e.g. sibling induction was silently dead for weeks on a 'model is
    not supported' 400 from `codex`). The fix captures lane + last 300 chars of
    stderr into `_last_dispatch_error` under a lock; `extract`'s `_fail` now
    surfaces them. This test is the regression guard for that fix.
    """
    # Reset the module-level error buffer — earlier tests may have left state.
    monkeypatch.setattr(ge, "_last_dispatch_error", "")

    # Real work signal so the degenerate-node skip does not fire (BUG4a).
    trace_id = trace_store.trace_write(
        {
            "trace_id": "tr-fail-dispatch",
            "task_class": "grad-fail",
            "duration_ms": 1200,
            "verifier_output": {"verdict": "fail"},
        },
        db=db,
    )
    from mini_ork.dispatch import llm_dispatch as native_dispatch

    def fake(argv, *, root, dispatch_fn):
        # The provider writes its real failure to stderr.
        print("boom 400: 'gpt-6-astra' model is not supported", file=sys.stderr)
        return 1

    monkeypatch.setattr(native_dispatch, "llm_dispatch", fake)
    monkeypatch.setenv("MINI_ORK_GRADIENT_MODEL", "codex")

    rc, _, err = _py_extract(trace_id, db)
    assert rc != 0, "extract must raise SystemExit on rc != 0"
    # Both the provider message AND the lane reach stderr.
    assert "boom 400" in err, f"stderr missing provider message: {err!r}"
    assert "codex" in err, f"stderr missing lane name: {err!r}"
    # And it does NOT regress to the bare pre-fix text alone.
    assert "LLM dispatch failed" in err
    assert err.rstrip().endswith(
        "gradient_extract: LLM dispatch failed on lane codex: boom 400: 'gpt-6-astra' model is not supported"
    ), f"unexpected stderr: {err!r}"


def test_extract_failure_empty_stderr_still_names_lane(db, monkeypatch):
    """A failed native dispatch with NO stderr still names the lane (F2).

    Pre-fix, `_last_dispatch_error` was set to `""` when stderr was empty, so
    `extract` fell back to the bare `LLM dispatch failed` text with no lane.
    That re-blinds the blind-failure class this kickoff exists to close
    (e.g. provider dies before logging anything). The fix records a synthetic
    `lane <x>: rc=N (no stderr captured)` instead.
    """
    monkeypatch.setattr(ge, "_last_dispatch_error", "")

    trace_id = trace_store.trace_write(
        {
            "trace_id": "tr-fail-empty-stderr",
            "task_class": "grad-fail-empty",
            "duration_ms": 1200,
            "verifier_output": {"verdict": "fail"},
        },
        db=db,
    )
    from mini_ork.dispatch import llm_dispatch as native_dispatch

    def fake(argv, *, root, dispatch_fn):
        # Provider dies without writing anything to stderr.
        return 1

    monkeypatch.setattr(native_dispatch, "llm_dispatch", fake)
    monkeypatch.setenv("MINI_ORK_GRADIENT_MODEL", "codex")

    rc, _, err = _py_extract(trace_id, db)
    assert rc != 0
    assert "codex" in err, f"stderr missing lane name: {err!r}"
    assert "no stderr captured" in err, f"stderr missing fallback marker: {err!r}"
    assert "LLM dispatch failed" in err
    assert err.rstrip().endswith(
        "gradient_extract: LLM dispatch failed on lane codex: rc=1 (no stderr captured)"
    ), f"unexpected stderr: {err!r}"


def test_default_dispatch_resets_stale_error_on_success(db, monkeypatch):
    """A successful `_default_dispatch` MUST clear any prior `_last_dispatch_error`.

    The kickoff's stated test (failed → failed with empty stderr) is vacuous:
    the rc!=0 path already overwrites the buffer with the lane marker, so
    "first 400" cannot leak across two failed calls. The real hole is a
    FAILED call followed by a SUCCESSFUL call: the rc==0 path leaves the
    buffer alone, so a later failed call could echo the previous lane's
    error. The fix resets `_last_dispatch_error = ""` at entry under the
    lock, so a prior failure cannot survive a successful call.

    Without the reset, the pre-fix reproduction is:
        call 1 → rc=1, stderr "first 400"  → buffer = "lane codex: first 400"
        call 2 → rc=0                       → buffer still = "lane codex: first 400"
    """
    # Pre-seed the buffer to a recognisable prior failure — simulating state
    # left by an earlier call. The production reset must overwrite this.
    monkeypatch.setattr(ge, "_last_dispatch_error", "lane codex: first 400")
    from mini_ork.dispatch import llm_dispatch as native_dispatch

    def fake(argv, *, root, dispatch_fn):
        # Successful dispatch — does NOT write to _last_dispatch_error.
        print('[{"target":"workflow.node.test","signal":"s",'
              '"suggested_change":"c","confidence":0.5}]', end="")
        return 0

    monkeypatch.setattr(native_dispatch, "llm_dispatch", fake)
    monkeypatch.setenv("MINI_ORK_GRADIENT_MODEL", "codex")

    # Direct call — the reset is in `_default_dispatch`, not in `extract`.
    rc, _ = ge._default_dispatch(
        "stub prompt",
        repo_root="/engine",
        dispatch_fn=lambda *args: 0,
    )
    assert rc == 0
    # The buffer MUST be cleared — a later failure must not echo "first 400".
    assert ge._last_dispatch_error == "", (
        f"stale error leaked across successful call: {ge._last_dispatch_error!r}"
    )


def test_default_dispatch_reset_runs_before_rc_zero_path(db, monkeypatch):
    """The reset at entry runs BEFORE the success path leaves the buffer alone.

    Companion to the previous test: the buffer is reset even if the call
    itself never reaches a write site (rc==0 has no write site). This is the
    direct regression guard for the fix.
    """
    monkeypatch.setattr(ge, "_last_dispatch_error", "stale lane codex: prior failure")
    from mini_ork.dispatch import llm_dispatch as native_dispatch

    monkeypatch.setattr(native_dispatch, "llm_dispatch", lambda *a, **kw: 0)
    monkeypatch.setenv("MINI_ORK_GRADIENT_MODEL", "codex")

    rc, _ = ge._default_dispatch(
        "stub",
        repo_root="/engine",
        dispatch_fn=lambda *a: 0,
    )
    assert rc == 0
    assert ge._last_dispatch_error == ""
