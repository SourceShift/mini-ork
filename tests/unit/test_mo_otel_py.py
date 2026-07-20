"""Parity gate: mini_ork.observability.otel vs lib/mo_otel.sh.

Each test invokes the LIVE bash subprocess (the bash source `mo_otel_*`
family) against a per-case `MINI_ORK_RUN_DIR` temp dir, then invokes
the Python port against an identical-separation temp dir, and asserts
the resulting `.otel-spans.jsonl` buffers match line-by-line
(structural compare via json.loads; internally-generated timestamps
within a 1500ms tolerance window; explicit-arg timestamps exact). The
flush case uses `MO_OTEL_DRY_RUN=1` against a `db/init.sh`-seeded
state.db and diffs the printed OTLP payload structurally. No mocks,
no hardcoded expected outputs — expected is always derived from the
live control bash invocation.

>=6 cases (a-h):
  (a) mo_otel_emit raw-JSON append              — exact structural match
  (b) mo_otel_root_begin                        — {type, task_run_id} exact, start_ms tolerance
  (c) mo_otel_root_end parametrized rc=0/1      — {type, status} exact, end_ms tolerance
  (d) mo_otel_agent w/ explicit args            — full exact match (deterministic ms args)
  (e) disabled no-op (MO_OTEL unset)            — neither side creates buffer, both rc 0
  (f) enabled-gate-half no-op (MO_OTEL=1, no RUN_DIR) — neither side creates buffer
  (g) mo_otel_buf() path-string parity          — bash echo vs py return, exact
  (h) mo_otel_flush dry-run                     — OTLP payload parity via db/init.sh + MO_OTEL_DRY_RUN=1
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
from mini_ork.observability import otel as py

SH = REPO / "lib" / "mo_otel.sh"
INIT_SH = REPO / "db" / "init.sh"


def _which(*tools: str) -> dict[str, str]:
    out = {}
    for t in tools:
        p = shutil.which(t)
        if not p:
            pytest.skip(f"required tool not on PATH: {t}")
        out[t] = p
    return out


def _bash_env(bash_dir: Path, **extra: str) -> dict[str, str]:
    """Env for invoking lib/mo_otel.sh against a per-case temp dir."""
    return {
        **os.environ,
        "MO_OTEL": "1",
        "MINI_ORK_RUN_DIR": str(bash_dir),
        **extra,
    }


def _bash_run_func(
    func: str,
    args: list[str],
    bash_dir: Path,
    **extra: str,
) -> subprocess.CompletedProcess:
    """Source lib/mo_otel.sh and call `func` with positional args.

    Uses single-quote shell escaping (not double) so JSON args with inner
    double quotes survive intact — bash `"..."` would word-split on the
    inner `"`s. Test inputs in this module are single-quote-free, so the
    simple `'...'` quoting is safe; production callers would need to use
    the same convention or a `printf %q` escape for any arg containing a
    single quote.
    """
    arg_str = " ".join(f"'{a}'" for a in args)
    script = f'. "{SH}"\n{func} {arg_str}\n'
    return subprocess.run(
        ["bash", "-c", script],
        env=_bash_env(bash_dir, **extra),
        capture_output=True,
        text=True,
    )


def _read_buf_lines(buf_path: Path) -> list[dict]:
    """Parse JSONL buffer into a list of dicts. Missing file → empty."""
    if not buf_path.exists():
        return []
    return [
        json.loads(line)
        for line in buf_path.read_text().splitlines()
        if line.strip()
    ]


def _epoch_close(a: int | float | None, b: int | float | None,
                 *, ms: bool = False) -> bool:
    """Integer-epoch tolerance, mirroring the peer parity test.

    Default window: 1s (sufficient for `int(time.time())` which both
    impls use as `created_at`). Pass `ms=True` for millisecond-resolution
    fields (root_begin.start_ms, root_end.end_ms) where the bash and
    Python invocations have sub-second drift. The 1500ms window absorbs
    wall-clock drift between the bash subprocess start and the in-process
    Python call. The 1e-6 float check is a defense-in-depth fallback.
    """
    if a is None and b is None:
        return True
    if a is None or b is None:
        return False
    window = 1500 if ms else 1
    if abs(int(a) - int(b)) <= window:
        return True
    return abs(float(a) - float(b)) <= 1e-6


# ─────────────────────────────────────────────────────────────────────────────
# (a) mo_otel_emit raw-JSON append
# ─────────────────────────────────────────────────────────────────────────────
def test_mo_otel_emit_raw_json_parity(tmp_path, monkeypatch):
    """`mo_otel_emit` takes a pre-formed JSON line; both sides append verbatim.
    The bash and Python invocations are called with the SAME input string, so
    the resulting buffer lines are byte-equal (compared structurally to avoid
    any whitespace/key-order brittleness)."""
    bash_dir = tmp_path / "bash"
    py_dir = tmp_path / "py"
    bash_dir.mkdir()
    py_dir.mkdir()

    raw = '{"type":"custom","foo":"bar","n":42}'
    bash_r = _bash_run_func("mo_otel_emit", [raw], bash_dir)
    assert bash_r.returncode == 0, f"bash failed: {bash_r.stderr}"

    monkeypatch.setenv("MO_OTEL", "1")
    monkeypatch.setenv("MINI_ORK_RUN_DIR", str(py_dir))
    rc_py = py.mo_otel_emit(raw)
    assert rc_py == 0

    bash_lines = _read_buf_lines(bash_dir / ".otel-spans.jsonl")
    py_lines = _read_buf_lines(py_dir / ".otel-spans.jsonl")
    assert len(bash_lines) == 1, f"expected 1 bash line, got {bash_lines}"
    assert len(py_lines) == 1, f"expected 1 py line, got {py_lines}"
    assert bash_lines == py_lines, (
        f"raw-emit buffer mismatch:\n  bash={bash_lines}\n  py  ={py_lines}"
    )


# ─────────────────────────────────────────────────────────────────────────────
# (b) mo_otel_root_begin
# ─────────────────────────────────────────────────────────────────────────────
def test_mo_otel_root_begin_parity(tmp_path, monkeypatch):
    """`mo_otel_root_begin` writes `{type, task_run_id, start_ms}` to the
    buffer. `type` and `task_run_id` are caller-supplied (exact); `start_ms`
    is internally generated (`_now_ms`) and compared within a 1500ms
    tolerance window."""
    bash_dir = tmp_path / "bash"
    py_dir = tmp_path / "py"
    bash_dir.mkdir()
    py_dir.mkdir()

    bash_r = _bash_run_func("mo_otel_root_begin", ["task-b"], bash_dir)
    assert bash_r.returncode == 0, f"bash failed: {bash_r.stderr}"

    monkeypatch.setenv("MO_OTEL", "1")
    monkeypatch.setenv("MINI_ORK_RUN_DIR", str(py_dir))
    rc_py = py.mo_otel_root_begin("task-b")
    assert rc_py == 0

    bash_lines = _read_buf_lines(bash_dir / ".otel-spans.jsonl")
    py_lines = _read_buf_lines(py_dir / ".otel-spans.jsonl")
    assert len(bash_lines) == 1 and len(py_lines) == 1
    b, p = bash_lines[0], py_lines[0]
    assert b["type"] == "root_begin" == p["type"]
    assert b["task_run_id"] == "task-b" == p["task_run_id"]
    assert _epoch_close(b["start_ms"], p["start_ms"], ms=True), (
        f"start_ms drift > 1500ms: bash={b['start_ms']} py={p['start_ms']}"
    )


# ─────────────────────────────────────────────────────────────────────────────
# (c) mo_otel_root_end parametrized rc=0/1
# ─────────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("rc,expected_status", [
    ("0", "success"),
    ("1", "failure"),
])
def test_mo_otel_root_end_parity(tmp_path, monkeypatch, rc, expected_status):
    """`mo_otel_root_end <rc>` writes `{type, end_ms, status}` where status
    maps rc=='0'→'success' and rc!= '0'→'failure'. `end_ms` is internally
    generated (tolerance); `status` is caller-derived (exact)."""
    bash_dir = tmp_path / "bash"
    py_dir = tmp_path / "py"
    bash_dir.mkdir()
    py_dir.mkdir()

    bash_r = _bash_run_func("mo_otel_root_end", [rc], bash_dir)
    assert bash_r.returncode == 0, f"bash failed: {bash_r.stderr}"

    monkeypatch.setenv("MO_OTEL", "1")
    monkeypatch.setenv("MINI_ORK_RUN_DIR", str(py_dir))
    rc_py = py.mo_otel_root_end(rc)
    assert rc_py == 0

    bash_lines = _read_buf_lines(bash_dir / ".otel-spans.jsonl")
    py_lines = _read_buf_lines(py_dir / ".otel-spans.jsonl")
    assert len(bash_lines) == 1 and len(py_lines) == 1
    b, p = bash_lines[0], py_lines[0]
    assert b["type"] == "root_end" == p["type"]
    assert b["status"] == expected_status == p["status"]
    assert _epoch_close(b["end_ms"], p["end_ms"], ms=True), (
        f"end_ms drift > 1500ms: bash={b['end_ms']} py={p['end_ms']}"
    )


# ─────────────────────────────────────────────────────────────────────────────
# (d) mo_otel_agent full-exact match (deterministic explicit args)
# ─────────────────────────────────────────────────────────────────────────────
def test_mo_otel_agent_parity_full_exact(tmp_path, monkeypatch):
    """`mo_otel_agent` writes a 6-key JSON line. All 6 fields are caller-
    supplied (deterministic across subprocess invocations), so the compare
    is byte-equal — no tolerance needed."""
    bash_dir = tmp_path / "bash"
    py_dir = tmp_path / "py"
    bash_dir.mkdir()
    py_dir.mkdir()

    bash_r = _bash_run_func(
        "mo_otel_agent",
        ["n1", "implementer", "1000", "2000", "pass"],
        bash_dir,
    )
    assert bash_r.returncode == 0, f"bash failed: {bash_r.stderr}"

    monkeypatch.setenv("MO_OTEL", "1")
    monkeypatch.setenv("MINI_ORK_RUN_DIR", str(py_dir))
    rc_py = py.mo_otel_agent("n1", "implementer", 1000, 2000, "pass")
    assert rc_py == 0

    bash_lines = _read_buf_lines(bash_dir / ".otel-spans.jsonl")
    py_lines = _read_buf_lines(py_dir / ".otel-spans.jsonl")
    assert len(bash_lines) == 1 and len(py_lines) == 1
    assert bash_lines[0] == py_lines[0], (
        f"agent buffer mismatch:\n  bash={bash_lines[0]}\n  py  ={py_lines[0]}"
    )
    # Sanity: the explicit-arg fields landed verbatim.
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
    silent no-op. The buffer file must NOT be created by either side.
    Verified for both `mo_otel_root_begin` and `mo_otel_emit`."""
    bash_dir = tmp_path / "bash"
    py_dir = tmp_path / "py"
    bash_dir.mkdir()
    py_dir.mkdir()

    bash_env = {**os.environ, "MINI_ORK_RUN_DIR": str(bash_dir)}
    bash_env.pop("MO_OTEL", None)
    script = (
        f'. "{SH}"\n'
        f'mo_otel_root_begin "task-e"\n'
        f'mo_otel_emit \'{{"a":1}}\'\n'
    )
    bash_r = subprocess.run(
        ["bash", "-c", script],
        env=bash_env, capture_output=True, text=True,
    )
    assert bash_r.returncode == 0, f"bash failed: {bash_r.stderr}"

    monkeypatch.delenv("MO_OTEL", raising=False)
    monkeypatch.setenv("MINI_ORK_RUN_DIR", str(py_dir))
    rc1 = py.mo_otel_root_begin("task-e")
    rc2 = py.mo_otel_emit('{"a":1}')
    assert rc1 == 0
    assert rc2 == 0

    assert not (bash_dir / ".otel-spans.jsonl").exists(), (
        "disabled bash must not create the buffer"
    )
    assert not (py_dir / ".otel-spans.jsonl").exists(), (
        "disabled port must not create the buffer"
    )


# ─────────────────────────────────────────────────────────────────────────────
# (f) enabled-gate-half no-op (MO_OTEL=1 but MINI_ORK_RUN_DIR unset)
# ─────────────────────────────────────────────────────────────────────────────
def test_mo_otel_enabled_half_noop(tmp_path, monkeypatch):
    """`MO_OTEL=1` alone is not enough — `MINI_ORK_RUN_DIR` must also be set.
    Both sides' `mo_otel_enabled()` returns False, so every entry point is a
    silent no-op. Buffer file must NOT be created by either side."""
    bash_dir = tmp_path / "bash"
    py_dir = tmp_path / "py"
    bash_dir.mkdir()
    py_dir.mkdir()

    bash_env = {**os.environ, "MO_OTEL": "1"}
    bash_env.pop("MINI_ORK_RUN_DIR", None)
    script = f'. "{SH}"\nmo_otel_root_begin "task-f"\n'
    bash_r = subprocess.run(
        ["bash", "-c", script],
        env=bash_env, capture_output=True, text=True,
    )
    assert bash_r.returncode == 0, f"bash failed: {bash_r.stderr}"

    monkeypatch.setenv("MO_OTEL", "1")
    monkeypatch.delenv("MINI_ORK_RUN_DIR", raising=False)
    rc_py = py.mo_otel_root_begin("task-f")
    assert rc_py == 0

    assert not (bash_dir / ".otel-spans.jsonl").exists(), (
        "half-gated bash must not create the buffer"
    )
    assert not (py_dir / ".otel-spans.jsonl").exists(), (
        "half-gated port must not create the buffer"
    )


# ─────────────────────────────────────────────────────────────────────────────
# (g) mo_otel_buf() path-string parity
# ─────────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("run_dir,expected_buf", [
    ("/tmp/run-x", "/tmp/run-x/.otel-spans.jsonl"),
    ("", "/.otel-spans.jsonl"),  # bash ${VAR:-/} fallback when unset/empty
])
def test_mo_otel_buf_path_parity(run_dir, expected_buf, monkeypatch):
    """`mo_otel_buf` is a string-returning helper. Bash `echo`es the path;
    the Python port returns it. They must agree exactly. The unset/empty
    case exercises the `${VAR:-/}` bash fallback, which the port matches
    via `os.environ.get(...) or "/"`.

    Sanity check: `expected_buf` is asserted BEFORE we delegate to bash so
    the parity test itself never bakes in a wrong expected value — the bash
    subprocess is the live oracle."""
    bash_env = {**os.environ}
    if run_dir:
        bash_env["MINI_ORK_RUN_DIR"] = run_dir
        monkeypatch.setenv("MINI_ORK_RUN_DIR", run_dir)
    else:
        bash_env.pop("MINI_ORK_RUN_DIR", None)
        monkeypatch.delenv("MINI_ORK_RUN_DIR", raising=False)

    bash_script = f'. "{SH}"\nmo_otel_buf\n'
    bash_r = subprocess.run(
        ["bash", "-c", bash_script],
        env=bash_env, capture_output=True, text=True,
    )
    assert bash_r.returncode == 0, f"bash failed: {bash_r.stderr}"
    bash_buf = bash_r.stdout.rstrip("\n")
    py_buf = py.mo_otel_buf()

    # Sanity: bash output matches the documented expected path shape.
    assert bash_buf == expected_buf, (
        f"bash oracle drifted from documented shape: {bash_buf!r} != {expected_buf!r}"
    )
    assert py_buf == bash_buf, (
        f"buf path parity: bash={bash_buf!r} py={py_buf!r}"
    )


# ─────────────────────────────────────────────────────────────────────────────
# (h) mo_otel_flush dry-run
# ─────────────────────────────────────────────────────────────────────────────
def test_mo_otel_flush_dryrun_parity(tmp_path, monkeypatch, capfd):
    """`mo_otel_flush` with `MO_OTEL_DRY_RUN=1` shells out to
    `python3 -m mini_ork.otel_export --from-jsonl ... --dry-run` and prints
    the OTLP payload to stdout. Both sides seed an IDENTICAL buffer
    (deterministic timestamps), so the resulting payload must be
    structurally identical.

    Requires: bash, python3 on PATH, `db/init.sh` succeeds against the
    temp `MINI_ORK_HOME`, and `mini_ork.otel_export` is importable (cwd =
    repo root).

    Bash subprocess output is captured via `subprocess.run(capture_output=
    True).stdout`. The Python port's subprocess output inherits the port's
    stdout, which `capfd` captures at the fd level (the exporter writes to
    its own fd 1, which the port inherits and which the test framework's
    fd-level capture intercepts).
    """
    _which("bash", "python3")

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

    bash_dir = tmp_path / "bash_run"
    py_dir = tmp_path / "py_run"
    bash_dir.mkdir()
    py_dir.mkdir()
    buf_content = (
        '{"type":"root_begin","task_run_id":"task-flush","start_ms":1000000}\n'
        '{"type":"root_end","end_ms":1001000,"status":"success"}\n'
        '{"type":"agent","node_id":"n1","node_type":"implementer",'
        '"start_ms":1000000,"end_ms":1000500,"verdict":"pass"}\n'
    )
    (bash_dir / ".otel-spans.jsonl").write_text(buf_content)
    (py_dir / ".otel-spans.jsonl").write_text(buf_content)

    bash_env = {
        **os.environ,
        "MO_OTEL": "1",
        "MINI_ORK_RUN_DIR": str(bash_dir),
        "MINI_ORK_ROOT": str(REPO),
        "MINI_ORK_HOME": str(home),
        "MINI_ORK_DB": dbp,
        "MO_OTEL_DRY_RUN": "1",
    }
    bash_script = f'. "{SH}"\nmo_otel_flush\n'
    bash_r = subprocess.run(
        ["bash", "-c", bash_script],
        env=bash_env, capture_output=True, text=True,
    )
    assert bash_r.returncode == 0, (
        f"bash flush failed: stderr={bash_r.stderr!r}"
    )
    bash_payload_str = bash_r.stdout.strip()
    assert bash_payload_str, "bash dry-run produced empty stdout"
    bash_payload = json.loads(bash_payload_str)

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

    assert bash_payload == py_payload, (
        "OTLP payload mismatch:\n"
        f"  bash={json.dumps(bash_payload, indent=2)}\n"
        f"  py  ={json.dumps(py_payload, indent=2)}"
    )

    # Sanity: the deterministic-buffer inputs map to a known payload shape.
    spans = bash_payload["resourceSpans"][0]["scopeSpans"][0]["spans"]
    assert len(spans) == 2, f"expected 2 spans (root + 1 agent), got {len(spans)}"
    root = next(s for s in spans if "task_run" in s["name"])
    agent = next(s for s in spans if "agent" in s["name"])
    assert root["startTimeUnixNano"] == "1000000000000"
    assert root["endTimeUnixNano"] == "1001000000000"
    assert root["status"]["code"] == 1  # OK
    assert agent["startTimeUnixNano"] == "1000000000000"
    assert agent["endTimeUnixNano"] == "1000500000000"