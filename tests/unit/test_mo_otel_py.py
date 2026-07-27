"""Unit tests: mini_ork.observability.otel (bash parity halves removed; formerly vs lib/mo_otel.sh).

Each test invokes the Python port against a per-case `MINI_ORK_RUN_DIR`
temp dir and asserts the resulting `.otel-spans.jsonl` buffer semantically
(structural compare via json.loads; internally-generated timestamps checked
against wall-clock bounds; explicit-arg timestamps exact). The flush case
uses `MO_OTEL_DRY_RUN=1` against a `db/init.sh`-seeded state.db and checks
the printed OTLP payload structurally. No mocks.

Cases (a-h):
  (a) mo_otel_emit raw-JSON append              — exact structural match
  (b) mo_otel_root_begin                        — {type, task_run_id} exact, start_ms sane
  (c) mo_otel_root_end parametrized rc=0/1      — {type, status} exact, end_ms sane
  (d) mo_otel_agent w/ explicit args            — full exact match (deterministic ms args)
  (e) disabled no-op (MO_OTEL unset)            — no buffer created, rc 0
  (f) enabled-gate-half no-op (MO_OTEL=1, no RUN_DIR) — no buffer created
  (g) mo_otel_buf() path-string                 — exact path contract
  (h) mo_otel_flush dry-run                     — OTLP payload shape via db/init.sh + MO_OTEL_DRY_RUN=1
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
from mini_ork.observability import otel as py

INIT_SH = REPO / "db" / "init.sh"


def _read_buf_lines(buf_path: Path) -> list[dict]:
    """Parse JSONL buffer into a list of dicts. Missing file → empty."""
    if not buf_path.exists():
        return []
    return [
        json.loads(line)
        for line in buf_path.read_text().splitlines()
        if line.strip()
    ]


# ─────────────────────────────────────────────────────────────────────────────
# (a) mo_otel_emit raw-JSON append
# ─────────────────────────────────────────────────────────────────────────────
def test_mo_otel_emit_raw_json(tmp_path, monkeypatch):
    """`mo_otel_emit` takes a pre-formed JSON line and appends it verbatim."""
    py_dir = tmp_path / "py"
    py_dir.mkdir()

    raw = '{"type":"custom","foo":"bar","n":42}'
    monkeypatch.setenv("MO_OTEL", "1")
    monkeypatch.setenv("MINI_ORK_RUN_DIR", str(py_dir))
    rc_py = py.mo_otel_emit(raw)
    assert rc_py == 0

    py_lines = _read_buf_lines(py_dir / ".otel-spans.jsonl")
    assert py_lines == [{"type": "custom", "foo": "bar", "n": 42}]


# ─────────────────────────────────────────────────────────────────────────────
# (b) mo_otel_root_begin
# ─────────────────────────────────────────────────────────────────────────────
def test_mo_otel_root_begin(tmp_path, monkeypatch):
    """`mo_otel_root_begin` writes `{type, task_run_id, start_ms}` to the
    buffer. `type` and `task_run_id` are caller-supplied (exact); `start_ms`
    is internally generated (`_now_ms`) and checked against wall-clock."""
    py_dir = tmp_path / "py"
    py_dir.mkdir()

    monkeypatch.setenv("MO_OTEL", "1")
    monkeypatch.setenv("MINI_ORK_RUN_DIR", str(py_dir))
    t0 = int(time.time() * 1000)
    rc_py = py.mo_otel_root_begin("task-b")
    t1 = int(time.time() * 1000)
    assert rc_py == 0

    py_lines = _read_buf_lines(py_dir / ".otel-spans.jsonl")
    assert len(py_lines) == 1
    p = py_lines[0]
    assert p["type"] == "root_begin"
    assert p["task_run_id"] == "task-b"
    assert t0 - 100 <= p["start_ms"] <= t1 + 100


# ─────────────────────────────────────────────────────────────────────────────
# (c) mo_otel_root_end parametrized rc=0/1
# ─────────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("rc,expected_status", [
    ("0", "success"),
    ("1", "failure"),
])
def test_mo_otel_root_end(tmp_path, monkeypatch, rc, expected_status):
    """`mo_otel_root_end <rc>` writes `{type, end_ms, status}` where status
    maps rc=='0'→'success' and rc!= '0'→'failure'."""
    py_dir = tmp_path / "py"
    py_dir.mkdir()

    monkeypatch.setenv("MO_OTEL", "1")
    monkeypatch.setenv("MINI_ORK_RUN_DIR", str(py_dir))
    t0 = int(time.time() * 1000)
    rc_py = py.mo_otel_root_end(rc)
    t1 = int(time.time() * 1000)
    assert rc_py == 0

    py_lines = _read_buf_lines(py_dir / ".otel-spans.jsonl")
    assert len(py_lines) == 1
    p = py_lines[0]
    assert p["type"] == "root_end"
    assert p["status"] == expected_status
    assert t0 - 100 <= p["end_ms"] <= t1 + 100


# ─────────────────────────────────────────────────────────────────────────────
# (d) mo_otel_agent full-exact match (deterministic explicit args)
# ─────────────────────────────────────────────────────────────────────────────
def test_mo_otel_agent_full_exact(tmp_path, monkeypatch):
    """`mo_otel_agent` writes a 6-key JSON line. All 6 fields are caller-
    supplied (deterministic), so the compare is exact."""
    py_dir = tmp_path / "py"
    py_dir.mkdir()

    monkeypatch.setenv("MO_OTEL", "1")
    monkeypatch.setenv("MINI_ORK_RUN_DIR", str(py_dir))
    rc_py = py.mo_otel_agent("n1", "implementer", 1000, 2000, "pass")
    assert rc_py == 0

    py_lines = _read_buf_lines(py_dir / ".otel-spans.jsonl")
    assert len(py_lines) == 1
    a = py_lines[0]
    assert a["type"] == "agent"
    assert a["node_id"] == "n1"
    assert a["node_type"] == "implementer"
    assert a["start_ms"] == 1000
    assert a["end_ms"] == 2000
    assert a["verdict"] == "pass"


# ─────────────────────────────────────────────────────────────────────────────
# (e) disabled no-op (MO_OTEL unset)
# ─────────────────────────────────────────────────────────────────────────────
def test_mo_otel_disabled_noop(tmp_path, monkeypatch):
    """When `MO_OTEL` is unset (defaults to "0"), every entry point is a
    silent no-op. The buffer file must NOT be created."""
    py_dir = tmp_path / "py"
    py_dir.mkdir()

    monkeypatch.delenv("MO_OTEL", raising=False)
    monkeypatch.setenv("MINI_ORK_RUN_DIR", str(py_dir))
    rc1 = py.mo_otel_root_begin("task-e")
    rc2 = py.mo_otel_emit('{"a":1}')
    assert rc1 == 0
    assert rc2 == 0

    assert not (py_dir / ".otel-spans.jsonl").exists(), (
        "disabled port must not create the buffer"
    )


# ─────────────────────────────────────────────────────────────────────────────
# (f) enabled-gate-half no-op (MO_OTEL=1 but MINI_ORK_RUN_DIR unset)
# ─────────────────────────────────────────────────────────────────────────────
def test_mo_otel_enabled_half_noop(tmp_path, monkeypatch):
    """`MO_OTEL=1` alone is not enough — `MINI_ORK_RUN_DIR` must also be set.
    `mo_otel_enabled()` returns False, so every entry point is a silent
    no-op. Buffer file must NOT be created."""
    py_dir = tmp_path / "py"
    py_dir.mkdir()

    monkeypatch.setenv("MO_OTEL", "1")
    monkeypatch.delenv("MINI_ORK_RUN_DIR", raising=False)
    rc_py = py.mo_otel_root_begin("task-f")
    assert rc_py == 0

    assert not (py_dir / ".otel-spans.jsonl").exists(), (
        "half-gated port must not create the buffer"
    )


# ─────────────────────────────────────────────────────────────────────────────
# (g) mo_otel_buf() path-string contract
# ─────────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("run_dir,expected_buf", [
    ("/tmp/run-x", "/tmp/run-x/.otel-spans.jsonl"),
    ("", "/.otel-spans.jsonl"),  # ${VAR:-/} fallback when unset/empty
])
def test_mo_otel_buf_path(run_dir, expected_buf, monkeypatch):
    """`mo_otel_buf` returns the buffer path; the unset/empty case exercises
    the `/` fallback via `os.environ.get(...) or "/"`."""
    if run_dir:
        monkeypatch.setenv("MINI_ORK_RUN_DIR", run_dir)
    else:
        monkeypatch.delenv("MINI_ORK_RUN_DIR", raising=False)

    assert py.mo_otel_buf() == expected_buf


# ─────────────────────────────────────────────────────────────────────────────
# (h) mo_otel_flush dry-run
# ─────────────────────────────────────────────────────────────────────────────
def test_mo_otel_flush_dryrun(tmp_path, monkeypatch, capfd):
    """`mo_otel_flush` with `MO_OTEL_DRY_RUN=1` shells out to
    `python3 -m mini_ork.otel_export --from-jsonl ... --dry-run` and prints
    the OTLP payload to stdout. With a deterministic seeded buffer the
    payload shape is fully checkable.

    The Python port's subprocess output inherits the port's stdout, which
    `capfd` captures at the fd level.
    """
    home = tmp_path / "home"
    home.mkdir()
    dbp = str(home / "state.db")
    r = subprocess.run(
        ["bash", str(INIT_SH)],
        env={**os.environ, "MINI_ORK_HOME": str(home), "MINI_ORK_DB": dbp},
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        pytest.skip(f"db/init.sh failed: rc={r.returncode}\nstderr={r.stderr}")

    py_dir = tmp_path / "py_run"
    py_dir.mkdir()
    buf_content = (
        '{"type":"root_begin","task_run_id":"task-flush","start_ms":1000000}\n'
        '{"type":"root_end","end_ms":1001000,"status":"success"}\n'
        '{"type":"agent","node_id":"n1","node_type":"implementer",'
        '"start_ms":1000000,"end_ms":1000500,"verdict":"pass"}\n'
    )
    (py_dir / ".otel-spans.jsonl").write_text(buf_content)

    monkeypatch.setenv("MO_OTEL", "1")
    monkeypatch.setenv("MINI_ORK_RUN_DIR", str(py_dir))
    monkeypatch.setenv("MINI_ORK_ROOT", str(REPO))
    monkeypatch.setenv("MINI_ORK_HOME", str(home))
    monkeypatch.setenv("MINI_ORK_DB", dbp)
    monkeypatch.setenv("MO_OTEL_DRY_RUN", "1")
    rc_py = py.mo_otel_flush()
    assert rc_py == 0
    captured = capfd.readouterr()
    py_payload_str = captured.out.strip()
    assert py_payload_str, (
        f"port dry-run produced empty stdout (stderr={captured.err!r})"
    )
    py_payload = json.loads(py_payload_str)

    # The deterministic-buffer inputs map to a known payload shape.
    spans = py_payload["resourceSpans"][0]["scopeSpans"][0]["spans"]
    assert len(spans) == 2, f"expected 2 spans (root + 1 agent), got {len(spans)}"
    root = next(s for s in spans if "task_run" in s["name"])
    agent = next(s for s in spans if "agent" in s["name"])
    assert root["startTimeUnixNano"] == "1000000000000"
    assert root["endTimeUnixNano"] == "1001000000000"
    assert root["status"]["code"] == 1  # OK
    assert agent["startTimeUnixNano"] == "1000000000000"
    assert agent["endTimeUnixNano"] == "1000500000000"
