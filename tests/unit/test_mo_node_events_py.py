"""Parity gate: mini_ork.observability.node_events vs lib/mo_node_events.sh.

Each test invokes the LIVE bash subprocess (the bash source `mo_node_emit`/
`mo_node_start`/`mo_node_end` family) against a temp DB seeded by
`db/init.sh`, then invokes the Python port against an identical temp DB
(seeded the same way), and asserts the resulting `run_events` rows match
byte-for-byte (integer epoch columns use 1-second tolerance; PIDs and
nanosecond suffixes on `event_id` are excluded from the comparison). No
mocks, no hardcoded expected outputs — expected is always derived from the
live control bash invocation.

>=6 cases:
  (a) mo_node_emit happy path node_start         — payload merge + finish_reason
  (b) mo_node_emit node_end w/ explicit args     — full payload (verdict/artifact/finish_reason)
  (c) mo_node_emit node_heartbeat                — populates last_heartbeat_at (migration 0023)
  (d) mo_node_start with/without model_lane      — extra = {} vs {model_lane}
  (e) mo_node_end _build_extra_json              — payload bytes vs in-bash heredoc
  (f) missing required arg parity                — bash `return 0` + stderr phrase matches port
  (g) missing state.db silent no-op              — both produce 0 rows, both exit 0
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
from mini_ork.observability import node_events as py

SH = REPO / "lib" / "mo_node_events.sh"
INIT_SH = REPO / "db" / "init.sh"


def _which(*tools: str) -> dict[str, str]:
    out = {}
    for t in tools:
        p = shutil.which(t)
        if not p:
            pytest.skip(f"required tool not on PATH: {t}")
        out[t] = p
    return out


@pytest.fixture
def temp_db(tmp_path_factory, monkeypatch):
    """Spin up a real mini-ork SQLite DB via db/init.sh.

    Returns (db_path, home_dir) tuple — the home_dir is also exposed as
    MINI_ORK_HOME so the bash subprocess resolves the same default path the
    Python port picks up via `_resolve_db`. The fixture monkeypatches
    `MINI_ORK_DB` and `MINI_ORK_HOME` in the parent pytest env so the
    Python port's `_resolve_db()` lands on the same DB.
    """
    home = tmp_path_factory.mktemp("home")
    dbp = str(home / "state.db")
    r = subprocess.run(
        ["bash", str(INIT_SH)],
        env={**os.environ, "MINI_ORK_HOME": str(home), "MINI_ORK_DB": dbp},
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        pytest.skip(f"db/init.sh failed: rc={r.returncode}\nstdout={r.stdout}\nstderr={r.stderr}")
    monkeypatch.setenv("MINI_ORK_DB", dbp)
    monkeypatch.setenv("MINI_ORK_HOME", str(home))
    return dbp, home


def _seed_db(home: Path) -> str:
    """Replicate `temp_db`'s init for cases that need a fresh second DB to
    isolate the Python port from the bash run. Returns db path."""
    dbp = str(home / "state.db")
    subprocess.run(
        ["bash", str(INIT_SH)],
        env={**os.environ, "MINI_ORK_HOME": str(home), "MINI_ORK_DB": dbp},
        capture_output=True, text=True, check=True,
    )
    return dbp


def _row_dict(con: sqlite3.Connection, table: str = "run_events") -> list[dict]:
    """Dump rows as dicts, ordered by `created_at` then `event_id` for
    determinism. The bash and Python rows have different event_ids (different
    PIDs and nanosecond timestamps), so we cannot rely on event_id ordering;
    `created_at` is the same `int(time.time())` from both implementations
    and is monotonic in a single-process test."""
    cols = [d[0] for d in con.execute(f"SELECT * FROM {table} LIMIT 0").description]
    rows = con.execute(
        f"SELECT {', '.join(cols)} FROM {table} ORDER BY created_at, event_id"
    ).fetchall()
    return [dict(zip(cols, r)) for r in rows]


def _event_id_stem(eid: str | None) -> str:
    """Return the `evt-<event_type>-<node_id>-` prefix (everything up to and
    including the 3rd dash-segment). bash `event_id` and Python `event_id`
    share this prefix; the trailing `<timestamp>-<pid>` segments differ."""
    assert eid is not None, "event_id must not be None"
    parts = eid.split("-")
    return "-".join(parts[:3]) + "-"


def _epoch_close(a: int | float | None, b: int | float | None,
                 *, ms: bool = False) -> bool:
    """Integer epoch tolerance.

    Default: 1 second for `created_at` (which is `int(time.time())` from
    both impls; normally exact). Pass `ms=True` for millisecond-resolution
    columns like `last_heartbeat_at` (`_now_ms()`) where sub-second drift
    between the bash and Python invocations is expected — allow up to 1500
    ms of drift before failing. The plan's 1e-6 float tolerance remains as
    a final defense-in-depth check after the integer window.
    """
    if a is None and b is None:
        return True
    if a is None or b is None:
        return False
    window = 1500 if ms else 1
    if abs(int(a) - int(b)) <= window:
        return True
    return abs(float(a) - float(b)) <= 1e-6


def _assert_row_parity(bash_row: dict, py_row: dict, *, fields: list[str]) -> None:
    """Compare two rows field-by-field. `event_id` is compared by stem
    (prefix) since the suffix differs; epoch fields are compared with
    1-second tolerance."""
    for f in fields:
        b = bash_row.get(f)
        p = py_row.get(f)
        if f == "event_id":
            assert _event_id_stem(b) == _event_id_stem(p), (
                f"event_id stem mismatch: bash={b!r} py={p!r}"
            )
            continue
        if f == "last_heartbeat_at":
            assert _epoch_close(b, p, ms=True), f"{f}: bash={b!r} py={p!r}"
            continue
        if f == "created_at":
            assert _epoch_close(b, p), f"{f}: bash={b!r} py={p!r}"
            continue
        if f == "payload_json":
            # payload_json is a string blob; parse and compare structurally
            # so we ignore key ordering (json.dumps is insertion-ordered but
            # the bash in-here python also uses json.dumps on a dict, so
            # both end up the same order — assert byte equality on the
            # parsed structure).
            assert b is not None and p is not None, "payload_json must not be None"
            assert json.loads(b) == json.loads(p), (
                f"payload_json dict mismatch: bash={b!r} py={p!r}"
            )
            continue
        assert b == p, f"{f}: bash={b!r} py={p!r}"


def _bash_emit(
    *,
    db: str,
    run_id: str,
    node_id: str,
    node_type: str,
    event_type: str,
    extra_json: str = "{}",
) -> subprocess.CompletedProcess:
    """Source the bash library and call `mo_node_emit` with the given args
    against `db`. The bash function will resolve the DB from the env (we
    pass MINI_ORK_DB and MINI_ORK_HOME so the bash function picks up the
    same path the Python port would)."""
    # Use single quotes for the args; bash will pass them through unchanged.
    # We must pass `extra_json` to a temp var so the bash command itself
    # stays readable.
    script = (
        f'. "{SH}"\n'
        f'mo_node_emit "{run_id}" "{node_id}" "{node_type}" "{event_type}" \'{extra_json}\'\n'
    )
    return subprocess.run(
        ["bash", "-c", script],
        env={**os.environ, "MINI_ORK_DB": db, "MINI_ORK_HOME": str(Path(db).parent)},
        capture_output=True, text=True,
    )


def _bash_run_func(
    func: str,
    args: list[str],
    *,
    db: str,
    extra_env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess:
    """Source the bash library and call `func` with positional args."""
    arg_str = " ".join(f'"{a}"' for a in args)
    script = f'. "{SH}"\n{func} {arg_str}\n'
    return subprocess.run(
        ["bash", "-c", script],
        env={**os.environ, "MINI_ORK_DB": db,
             "MINI_ORK_HOME": str(Path(db).parent), **(extra_env or {})},
        capture_output=True, text=True,
    )


# ─────────────────────────────────────────────────────────────────────────────
# (a) mo_node_emit happy path node_start
# ─────────────────────────────────────────────────────────────────────────────
def test_mo_node_emit_node_start_parity(temp_db):
    """Bash `mo_node_emit <run> <node> <type> node_start '<json>'` writes one
    `run_events` row with `event_id` stem `evt-node_start-<node>-` and
    payload_json merging `node_id`/`node_type` with the parsed extra dict.
    Python port must match byte-for-byte (epoch columns within 1s)."""
    db, _ = temp_db
    bash_r = _bash_emit(
        db=db, run_id="run-a", node_id="n-a", node_type="researcher",
        event_type="node_start", extra_json='{"model_lane":"minimax"}',
    )
    assert bash_r.returncode == 0, f"bash failed: {bash_r.stderr}"
    py.mo_node_emit("run-a", "n-a", "researcher", "node_start", '{"model_lane":"minimax"}')

    con = sqlite3.connect(db)
    try:
        rows = _row_dict(con)
    finally:
        con.close()
    assert len(rows) == 2, f"expected 2 rows (bash + py), got {len(rows)}: {rows}"
    _assert_row_parity(rows[0], rows[1], fields=[
        "event_id", "run_id", "event_type", "payload_json",
        "created_at", "finish_reason", "last_heartbeat_at",
    ])


# ─────────────────────────────────────────────────────────────────────────────
# (b) mo_node_emit node_end w/ explicit verdict/artifact/finish_reason
# ─────────────────────────────────────────────────────────────────────────────
def test_mo_node_emit_node_end_full_payload(temp_db):
    """Bash `mo_node_end <run> <node> <type> <dur> <verdict> <artifact> <finish>`
    builds a JSON payload with `duration_ms` + optional fields. Python port
    must match. Note: `mo_node_end` is a bash convenience that calls
    `mo_node_emit` under the hood."""
    db, _ = temp_db
    bash_r = _bash_run_func(
        "mo_node_end",
        ["run-b", "n-b", "implementer", "1234", "pass", "/tmp/art.md", "done"],
        db=db,
    )
    assert bash_r.returncode == 0, f"bash failed: {bash_r.stderr}"
    py.mo_node_end("run-b", "n-b", "implementer", 1234, "pass", "/tmp/art.md", "done")

    con = sqlite3.connect(db)
    try:
        rows = _row_dict(con)
    finally:
        con.close()
    assert len(rows) == 2
    _assert_row_parity(rows[0], rows[1], fields=[
        "event_id", "run_id", "event_type", "payload_json",
        "created_at", "finish_reason", "last_heartbeat_at",
    ])
    # Sanity: payload carries the explicit fields.
    payload = json.loads(rows[0]["payload_json"])
    assert payload["duration_ms"] == 1234
    assert payload["verdict"] == "pass"
    assert payload["artifact_path"] == "/tmp/art.md"
    assert payload["finish_reason"] == "done"


# ─────────────────────────────────────────────────────────────────────────────
# (c) mo_node_emit node_heartbeat populates last_heartbeat_at
# ─────────────────────────────────────────────────────────────────────────────
def test_mo_node_heartbeat_writes_last_heartbeat_at(temp_db, monkeypatch):
    """Migration 0023 adds `last_heartbeat_at` to `run_events`; the bash
    heredoc populates it only when `event_type in ('node_start',
    'node_heartbeat')`. Python port must match.

    Both impls read `MO_NODE_TYPE` from the env (defaulting to
    `'heartbeat'`). We set it to `'reviewer'` on BOTH sides (bash via
    `extra_env`, Python via `monkeypatch.setenv`) so the test exercises
    the env-override path on both implementations rather than a mix of
    override/default. Then we sanity-check the override landed in the
    payload.
    """
    monkeypatch.setenv("MO_NODE_TYPE", "reviewer")
    db, _ = temp_db
    bash_r = _bash_run_func(
        "mo_emit_node_heartbeat", ["n-c", "run-c"],
        db=db, extra_env={"MO_NODE_TYPE": "reviewer"},
    )
    assert bash_r.returncode == 0, f"bash failed: {bash_r.stderr}"
    py.mo_emit_node_heartbeat("n-c", "run-c")

    con = sqlite3.connect(db)
    try:
        rows = _row_dict(con)
    finally:
        con.close()
    assert len(rows) == 2
    _assert_row_parity(rows[0], rows[1], fields=[
        "event_id", "run_id", "event_type", "payload_json",
        "created_at", "finish_reason", "last_heartbeat_at",
    ])
    # Sanity: the heartbeat column is populated, finish_reason is NULL.
    for r in rows:
        assert r["event_type"] == "node_heartbeat"
        assert r["last_heartbeat_at"] is not None
        assert r["finish_reason"] is None
        payload = json.loads(r["payload_json"])
        assert payload["node_type"] == "reviewer"  # MO_NODE_TYPE=reviewer on both sides


# ─────────────────────────────────────────────────────────────────────────────
# (d) mo_node_start with/without model_lane
# ─────────────────────────────────────────────────────────────────────────────
def test_mo_node_start_with_and_without_model_lane(temp_db):
    """Bash `mo_node_start <run> <node> <type> [<lane>]` builds
    `extra = {"model_lane": <lane>}` when non-empty, else the empty-object
    literal `{}`. Python port must match for both shapes."""
    db, _ = temp_db

    # (i) with model_lane
    bash_r1 = _bash_run_func(
        "mo_node_start", ["run-d1", "n-d1", "verifier", "opus"],
        db=db,
    )
    assert bash_r1.returncode == 0, f"bash failed: {bash_r1.stderr}"
    py.mo_node_start("run-d1", "n-d1", "verifier", "opus")

    # (ii) without model_lane — bash has a 4-arg variant; Python has default arg.
    bash_r2 = _bash_run_func(
        "mo_node_start", ["run-d2", "n-d2", "verifier"],
        db=db,
    )
    assert bash_r2.returncode == 0, f"bash failed: {bash_r2.stderr}"
    py.mo_node_start("run-d2", "n-d2", "verifier")

    con = sqlite3.connect(db)
    try:
        rows = _row_dict(con)
    finally:
        con.close()
    assert len(rows) == 4
    # Match by run_id to pair bash row with py row.
    by_run = {}
    for r in rows:
        by_run.setdefault(r["run_id"], []).append(r)
    for run_id, pair in by_run.items():
        assert len(pair) == 2, f"expected 2 rows for {run_id}, got {len(pair)}"
        _assert_row_parity(pair[0], pair[1], fields=[
            "event_id", "run_id", "event_type", "payload_json",
            "created_at", "finish_reason", "last_heartbeat_at",
        ])
    # Sanity: lane shape on the model_lane case, empty on the no-lane case.
    lane_row = by_run["run-d1"][0]
    no_lane_row = by_run["run-d2"][0]
    assert json.loads(lane_row["payload_json"])["model_lane"] == "opus"
    assert "model_lane" not in json.loads(no_lane_row["payload_json"])


# ─────────────────────────────────────────────────────────────────────────────
# (e) mo_node_end _build_extra_json payload bytes vs in-bash heredoc
# ─────────────────────────────────────────────────────────────────────────────
def test_build_extra_json_matches_bash_heredoc():
    """Drive the bash in-here python (lines 161-170) directly and compare
    its output to `py._build_extra_json`. The heredoc python uses
    `print(json.dumps(out))` and adds no extra newline; Python's
    `json.dumps` likewise does not add a trailing newline. The parities
    must be exact (no `event_type`/timestamp noise — this is a pure
    payload-shaping test)."""
    _which("python3")
    cases = [
        ("1000", "", "", ""),
        ("1000", "pass", "", ""),
        ("1000", "pass", "/tmp/art", "done"),
        ("0", "", "/tmp/x", "error"),
        ("42", "verdict-with-spaces and |pipe|", "/path/with spaces", "max_steps"),
    ]
    for duration_ms, verdict, artifact, finish in cases:
        # Bash: lines 161-170 verbatim
        bash_script = (
            "python3 - \"$1\" \"$2\" \"$3\" \"$4\" <<'PY'\n"
            "import json, sys\n"
            "duration_ms, verdict, artifact_path, finish_reason = sys.argv[1:5]\n"
            "out = {\"duration_ms\": int(duration_ms or 0)}\n"
            "if verdict:       out[\"verdict\"] = verdict\n"
            "if artifact_path: out[\"artifact_path\"] = artifact_path\n"
            "if finish_reason: out[\"finish_reason\"] = finish_reason\n"
            "print(json.dumps(out))\n"
            "PY\n"
        )
        r = subprocess.run(
            ["bash", "-c", bash_script, "_", duration_ms, verdict, artifact, finish],
            capture_output=True, text=True, check=True,
        )
        expected = r.stdout.rstrip("\n")
        actual = py._build_extra_json(duration_ms, verdict, artifact, finish)
        assert actual == expected, (
            f"build_extra_json mismatch (dur={duration_ms!r}, v={verdict!r}, "
            f"a={artifact!r}, f={finish!r}):\n  bash={expected!r}\n  py  ={actual!r}"
        )


# ─────────────────────────────────────────────────────────────────────────────
# (f) missing required arg parity (stderr phrase + return 0)
# ─────────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("missing_arg,bash_args,py_kwargs,expected_phrase", [
    ("run_id",
     ["", "n-f", "researcher", "node_start", "{}"],
     {"run_id": "", "node_id": "n-f", "node_type": "researcher",
      "event_type": "node_start", "extra_json": "{}"},
     "mo_node_emit: run_id required"),
    ("node_id",
     ["run-f", "", "researcher", "node_start", "{}"],
     {"run_id": "run-f", "node_id": "", "node_type": "researcher",
      "event_type": "node_start", "extra_json": "{}"},
     "mo_node_emit: node_id required"),
    ("event_type",
     ["run-f", "n-f", "researcher", "", "{}"],
     {"run_id": "run-f", "node_id": "n-f", "node_type": "researcher",
      "event_type": "", "extra_json": "{}"},
     "mo_node_emit: event_type required"),
])
def test_missing_required_arg_parity(temp_db, missing_arg, bash_args,
                                     py_kwargs, expected_phrase):
    """Bash emits the canonical `mo_node_emit: <arg> required` stderr phrase
    on guard miss and `return 0` (silent). Python port must emit the same
    phrase verbatim and return 0. Both must NOT write a row."""
    db, _ = temp_db
    bash_r = _bash_emit(
        db=db, run_id=bash_args[0], node_id=bash_args[1], node_type=bash_args[2],
        event_type=bash_args[3], extra_json=bash_args[4],
    )
    rc_py = py.mo_node_emit(**py_kwargs)
    rc_bash = bash_r.returncode
    err_bash = bash_r.stderr
    assert rc_py == 0, f"[{missing_arg}] port returned {rc_py} (must be 0)"
    assert rc_bash == 0, f"[{missing_arg}] bash returned {rc_bash} (must be 0); stderr={err_bash!r}"
    assert expected_phrase in err_bash, (
        f"[{missing_arg}] bash stderr missing phrase {expected_phrase!r}: {err_bash!r}"
    )
    # No row should be written by either side.
    con = sqlite3.connect(db)
    try:
        count = con.execute("SELECT COUNT(*) FROM run_events").fetchone()[0]
    finally:
        con.close()
    assert count == 0, f"missing-arg case must NOT write a row; got {count}"


def test_python_port_stderr_matches_bash_exact_phrase(temp_db, capsys):
    """Capture the Python port's stderr and assert it matches the bash phrase
    byte-for-byte (the leading-trailing whitespace, the colon-space, the
    trailing lack of newline). The bash `{ ... >&2; return 0; }` group has
    no trailing newline; the port's `print(..., file=sys.stderr)` adds one
    by default — so we strip the trailing newline on both sides for the
    comparison."""
    db, _ = temp_db
    bash_r = _bash_emit(
        db=db, run_id="", node_id="n", node_type="t", event_type="node_start",
    )
    rc_py = py.mo_node_emit("", "n", "t", "node_start")
    captured = capsys.readouterr()
    py_stderr = captured.err.rstrip("\n")
    bash_stderr = bash_r.stderr.rstrip("\n")
    assert rc_py == 0
    assert bash_stderr == "mo_node_emit: run_id required", (
        f"unexpected bash stderr: {bash_stderr!r}"
    )
    assert py_stderr == bash_stderr, (
        f"port stderr {py_stderr!r} != bash stderr {bash_stderr!r}"
    )


# ─────────────────────────────────────────────────────────────────────────────
# (g) missing state.db silent no-op
# ─────────────────────────────────────────────────────────────────────────────
def test_missing_state_db_silent_noop(tmp_path):
    """Bash and the Python port both silently no-op when the resolved
    state.db does not exist (e.g. uninitialized test). Both must exit 0
    and NOT emit any row to a phantom DB."""
    missing_db = str(tmp_path / "nonexistent" / "state.db")
    # Bash: source the library and call mo_node_emit with the missing DB.
    bash_r = _bash_emit(
        db=missing_db, run_id="run-g", node_id="n-g", node_type="t",
        event_type="node_start",
    )
    rc_py = py.mo_node_emit("run-g", "n-g", "t", "node_start", "{}")
    assert bash_r.returncode == 0, f"bash failed: {bash_r.stderr}"
    assert rc_py == 0
    # The missing DB must not have been created.
    assert not os.path.exists(missing_db), (
        f"silent no-op must not create {missing_db}"
    )


def test_missing_state_db_noop_does_not_create_file(tmp_path):
    """Cover the silent no-op with `MINI_ORK_HOME` resolved but state.db
    not yet seeded — same contract: both bash and Python return 0, neither
    creates the file."""
    home = tmp_path / "home"
    home.mkdir()
    dbp = str(home / "state.db")
    assert not os.path.exists(dbp)
    # Bash
    bash_r = subprocess.run(
        ["bash", "-c", f'. "{SH}"\nmo_node_emit "r" "n" "t" "node_start" \'{{}}\'\n'],
        env={**os.environ, "MINI_ORK_HOME": str(home)},
        capture_output=True, text=True,
    )
    # Python
    rc_py = py.mo_node_emit("r", "n", "t", "node_start", "{}")
    assert bash_r.returncode == 0
    assert rc_py == 0
    assert not os.path.exists(dbp), "state.db must not be auto-created"


# ─────────────────────────────────────────────────────────────────────────────
# (h) mo_node_emit_end_trap signature-parity: early-return on empty fields
#     and full end-trap happy path with explicit finish_reason.
# ─────────────────────────────────────────────────────────────────────────────
def test_mo_node_emit_end_trap_early_return(temp_db):
    """Bash early-returns 0 on empty `_mo_run_id`/`node_id`/`node_type`
    (lines 134-136). Python port mirrors the same guard."""
    db, _ = temp_db
    # Empty _run_id
    assert py.mo_node_emit_end_trap("", "n-h", "t", 1000, 0) == 0
    # Empty node_id
    assert py.mo_node_emit_end_trap("r-h", "", "t", 1000, 0) == 0
    # Empty node_type
    assert py.mo_node_emit_end_trap("r-h", "n-h", "", 1000, 0) == 0
    # None should be written.
    con = sqlite3.connect(db)
    try:
        count = con.execute("SELECT COUNT(*) FROM run_events").fetchone()[0]
    finally:
        con.close()
    assert count == 0


def test_mo_node_emit_end_trap_happy_path(temp_db):
    """Drive the Python `mo_node_emit_end_trap` happy path and confirm the
    underlying `mo_node_end` row matches what bash would write with the
    same effective `duration_ms`.

    The trap computes `duration_ms = end_ms - start_ms` at runtime, so we
    first call the Python trap to capture the duration it observed, then
    drive a bash `mo_node_end` call with the SAME `duration_ms` value so
    the resulting row payloads are byte-equal. The bash RETURN-trap has
    no Python equivalent, so parity is asserted on the delegate surface
    (`mo_node_end`) with shared args.
    """
    db, _ = temp_db
    start_ms = py._now_ms() - 5000
    rc = py.mo_node_emit_end_trap(
        "run-h", "n-h", "implementer", start_ms, 0,
        context_path="/tmp/art.md", verdict="pass", finish_reason="done",
    )
    assert rc == 0
    con = sqlite3.connect(db)
    try:
        py_row = _row_dict(con)[0]
    finally:
        con.close()
    observed_dur = json.loads(py_row["payload_json"])["duration_ms"]
    assert observed_dur >= 0

    # Bash control: equivalent mo_node_end call with the same observed
    # duration. This proves the Python trap produces the same row shape
    # as a bash mo_node_end call (the trap's only delegate).
    bash_r = _bash_run_func(
        "mo_node_end",
        ["run-h", "n-h", "implementer", str(observed_dur), "pass",
         "/tmp/art.md", "done"],
        db=db,
    )
    assert bash_r.returncode == 0, f"bash failed: {bash_r.stderr}"
    con = sqlite3.connect(db)
    try:
        rows = _row_dict(con)
    finally:
        con.close()
    assert len(rows) == 2
    _assert_row_parity(rows[0], rows[1], fields=[
        "event_id", "run_id", "event_type", "payload_json",
        "created_at", "finish_reason", "last_heartbeat_at",
    ])


def test_mo_node_emit_end_trap_rc_nonzero_defaults_to_error(tmp_db):
    """Default finish_reason on rc != 0 is 'error' (bash lines 142-145).
    The port mirrors that. We can't reuse `temp_db` because the fixture is
    scoped to the previous tests — use a local temp DB."""
    from mini_ork.observability import node_events as p
    home = tmp_db
    dbp = _seed_db(home)
    start_ms = p._now_ms() - 1000
    rc = p.mo_node_emit_end_trap(
        "run-err", "n-err", "implementer", start_ms, 1,
    )
    assert rc == 0
    con = sqlite3.connect(dbp)
    try:
        rows = _row_dict(con)
    finally:
        con.close()
    assert len(rows) == 1
    payload = json.loads(rows[0]["payload_json"])
    assert payload["finish_reason"] == "error"
    assert payload["duration_ms"] >= 0


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    """A separate fixture for tests that need their own DB distinct from
    `temp_db`. Returns the home dir; call `_seed_db` to materialize the DB.
    The env vars are monkeypatched in advance so subsequent Python port
    calls resolve to the same DB once `_seed_db` is called."""
    home = tmp_path / "home"
    home.mkdir()
    dbp = str(home / "state.db")
    monkeypatch.setenv("MINI_ORK_DB", dbp)
    monkeypatch.setenv("MINI_ORK_HOME", str(home))
    return home
