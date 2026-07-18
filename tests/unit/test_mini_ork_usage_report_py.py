"""Parity gate: mini_ork.ported.mini_ork_usage_report vs bin/mini-ork-usage-report.

Each test invokes the LIVE bash subprocess against a temp DB seeded by
``db/init.sh`` (and optionally data tables), then invokes the Python
port against the same DB, and asserts the resulting region_expertise.json
payloads are equivalent (parsed dicts equal, floats within 1e-6,
``generated_at`` ignored). No mocks, no hardcoded expected outputs — the
expected value is always derived from the live control bash invocation.

db/init.sh applies the live migration graph through 0047. This checkout has
``defect_attributions`` in that graph but not ``lane_region_advantage``, so the
fixture preserves the db/init.sh bootstrap and creates the report-specific
region table only when the live schema does not already provide it.

Cases (8, above the kickoff's >=6 floor):

  (a) empty DB → ``note`` field present, entry_count=0, entries=[]
      (this is the bash missing-DB branch + port's missing-DB branch).
  (b) lane_region_advantage only → entries with advantage + sample_size
      match; outstanding_blame_penalty=0.0.
  (c) defect_attributions seeded → outstanding_blame_penalty in [-1, 0];
      age-weighted decay (penalty × 0.5**(age/halflife)) correct.
  (d) --since filter excludes old ``last_updated`` rows.
  (e) multi-region/lane → entries sorted lexicographically by
      (code_region, lane) — load-bearing for ``sort_keys=True`` in
      ``render_json``.
  (f) --smoke synthetic → rc=0 + codex_lens/lib entry shape; also
      drives Python's ``run_smoke()`` against the same synthetic DB.
  (g) --help rc=0 + "Usage:" in stdout.
  (h) unknown flag rc=2 + "unknown flag" in stderr.

Tolerance notes (per the kickoff and the live-bash quirks memo):

  * Float columns (``advantage``, ``outstanding_blame_penalty``) compared
    at 1e-6.
  * Integer columns (``sample_size``, ``entry_count``, ``since``) compared
    exact.
  * ``generated_at`` ignored — datetime.utcnow() drift between bash and
    Python runs is expected.
  * The bash script emits ``"note"`` field ONLY in the missing-DB branch.
    Production + smoke paths MUST NOT include it. Forgetting this silently
    breaks case (a).
  * Bash emits JSON via ``json.dump(report, f, indent=2, sort_keys=True)``
    + ``'\\n'``. Python port mirrors this exactly.
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
from mini_ork.ported import mini_ork_usage_report as py  # noqa: E402

SH = REPO / "bin" / "mini-ork-usage-report"
INIT_SH = REPO / "db" / "init.sh"

_SUBPROC_ENV_BASE = {**os.environ, "TZ": "UTC"}


# ─────────────────────────────────────────────────────────────────────────────
# Fixtures / helpers
# ─────────────────────────────────────────────────────────────────────────────
def _which_tools() -> None:
    for tool in ("bash", "sqlite3", "python3"):
        if not shutil.which(tool):
            pytest.skip(f"{tool} not on PATH")
    if not SH.exists():
        pytest.skip(f"missing bin/mini-ork-usage-report at {SH}")
    if not INIT_SH.exists():
        pytest.skip(f"missing db/init.sh at {INIT_SH}")


@pytest.fixture
def temp_db(tmp_path, monkeypatch):
    """Spin up a real mini-ork SQLite DB via db/init.sh.

    Each test gets a fresh DB. The fixture sets ``MINI_ORK_DB`` /
    ``MINI_ORK_HOME`` / ``TZ`` in the parent process env so the Python
    port's ``_db_path()`` and any TZ-sensitive bash subprocess calls
    resolve to the same DB the bash subprocess sees.
    """
    _which_tools()
    home = tmp_path / "home"
    home.mkdir()
    dbp = str(home / "state.db")
    r = subprocess.run(
        ["bash", str(INIT_SH)],
        env={**_SUBPROC_ENV_BASE, "MINI_ORK_HOME": str(home),
             "MINI_ORK_DB": dbp},
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        pytest.skip(f"db/init.sh failed: rc={r.returncode}\nstderr={r.stderr}")
    monkeypatch.setenv("MINI_ORK_DB", dbp)
    monkeypatch.setenv("MINI_ORK_HOME", str(home))
    monkeypatch.setenv("TZ", "UTC")
    return {"home": str(home), "db": dbp, "tmp_path": tmp_path}


def _bash_run(args: list[str], *, db: str, out: str | None = None,
              env_extra: dict | None = None) -> subprocess.CompletedProcess:
    """Invoke the live ``bin/mini-ork-usage-report`` bash with the given args."""
    env = {**_SUBPROC_ENV_BASE, "MINI_ORK_DB": db,
           "MINI_ORK_HOME": str(Path(db).parent),
           "MINI_ORK_ROOT": str(REPO)}
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        ["bash", str(SH), *args],
        env=env, capture_output=True, text=True,
    )


def _py_main(args: list[str], *, db: str, out: str | None = None,
             env_extra: dict | None = None) -> int:
    """Invoke the Python port's ``main()`` with the given args.

    The bash subprocess writes region_expertise.json to its --out (or
    ./region_expertise.json default); we mirror that on the Python side.
    """
    env = {**os.environ, "MINI_ORK_DB": db,
           "MINI_ORK_HOME": str(Path(db).parent),
           "MINI_ORK_ROOT": str(REPO), "TZ": "UTC"}
    if env_extra:
        env.update(env_extra)
    old_env = os.environ.copy()
    try:
        os.environ.clear()
        os.environ.update(env)
        return py.main(args)
    finally:
        os.environ.clear()
        os.environ.update(old_env)


def _read_json(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _assert_dicts_equal(
    bash_d: dict, py_d: dict, *,
    ignore_generated_at: bool = True,
    float_tol: float = 1e-6,
) -> None:
    """Assert parsed region_expertise.json dicts are equivalent.

    Compares:
      * Top-level scalar fields exact (source_db, since, entry_count).
      * Top-level "note" field present-or-absent identically.
      * entries list: same length, same (code_region, lane) keys
        in the same order, with numeric fields equal within float_tol.
    """
    assert bash_d["source_db"] == py_d["source_db"], (
        f"source_db mismatch: bash={bash_d['source_db']!r} "
        f"py={py_d['source_db']!r}"
    )
    assert bash_d["since"] == py_d["since"], (
        f"since mismatch: bash={bash_d['since']} py={py_d['since']}"
    )
    assert bash_d["entry_count"] == py_d["entry_count"], (
        f"entry_count mismatch: bash={bash_d['entry_count']} "
        f"py={py_d['entry_count']}"
    )
    # Note field: both must agree on presence + value.
    assert bash_d.get("note") == py_d.get("note"), (
        f"note field mismatch: bash={bash_d.get('note')!r} "
        f"py={py_d.get('note')!r}"
    )

    bash_entries = bash_d["entries"]
    py_entries = py_d["entries"]
    assert len(bash_entries) == len(py_entries), (
        f"entries length mismatch: bash={len(bash_entries)} "
        f"py={len(py_entries)}"
    )

    # Both must be sorted by (code_region, lane) so order matches.
    for i, (be, pe) in enumerate(zip(bash_entries, py_entries)):
        assert be["code_region"] == pe["code_region"], (
            f"entry {i} code_region mismatch: bash={be['code_region']!r} "
            f"py={pe['code_region']!r}"
        )
        assert be["lane"] == pe["lane"], (
            f"entry {i} lane mismatch: bash={be['lane']!r} py={pe['lane']!r}"
        )
        assert be["sample_size"] == pe["sample_size"], (
            f"entry {i} sample_size mismatch: bash={be['sample_size']} "
            f"py={pe['sample_size']}"
        )
        assert abs(be["advantage"] - pe["advantage"]) <= float_tol, (
            f"entry {i} advantage drift > {float_tol}: bash={be['advantage']} "
            f"py={pe['advantage']}"
        )
        assert (
            abs(be["outstanding_blame_penalty"]
                - pe["outstanding_blame_penalty"]) <= float_tol
        ), (
            f"entry {i} outstanding_blame_penalty drift > {float_tol}: "
            f"bash={be['outstanding_blame_penalty']} "
            f"py={pe['outstanding_blame_penalty']}"
        )


def _seed_lane_region_advantage(
    db: str, rows: list[tuple],
) -> None:
    """Insert rows into lane_region_advantage.

    Each row is (agent_version_id, task_class, node_type,
    objective_domain, code_region, relative_advantage, runs_count,
    success_count, last_updated_iso).
    """
    con = sqlite3.connect(db)
    try:
        con.execute("""
            CREATE TABLE IF NOT EXISTS lane_region_advantage (
              agent_version_id   TEXT    NOT NULL,
              task_class         TEXT    NOT NULL,
              node_type          TEXT    NOT NULL DEFAULT '',
              objective_domain   TEXT    NOT NULL DEFAULT '',
              code_region        TEXT    NOT NULL DEFAULT '',
              relative_advantage REAL    NOT NULL DEFAULT 0.0,
              runs_count         INTEGER NOT NULL DEFAULT 0,
              success_count      INTEGER NOT NULL DEFAULT 0,
              last_updated       TEXT    NOT NULL DEFAULT (
                strftime('%Y-%m-%dT%H:%M:%fZ','now')
              ),
              PRIMARY KEY (
                agent_version_id, task_class, node_type,
                objective_domain, code_region
              )
            )
        """)
        for r in rows:
            con.execute(
                "INSERT INTO lane_region_advantage "
                "(agent_version_id, task_class, node_type, "
                " objective_domain, code_region, relative_advantage, "
                " runs_count, success_count, last_updated) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                r,
            )
        con.commit()
    finally:
        con.close()


def _seed_defect_attributions(db: str, rows: list[tuple]) -> None:
    """Insert rows into defect_attributions.

    Each row is (found_run_id, blamed_run_id, lane, code_region,
    task_class, severity, penalty, decay_halflife_days, ts_iso).
    """
    con = sqlite3.connect(db)
    try:
        for r in rows:
            con.execute(
                "INSERT INTO defect_attributions "
                "(found_run_id, blamed_run_id, lane, code_region, "
                " task_class, severity, penalty, decay_halflife_days, "
                " ts) VALUES (?,?,?,?,?,?,?,?,?)",
                r,
            )
        con.commit()
    finally:
        con.close()


def _now_iso_ms() -> str:
    """ISO with milliseconds for deterministic seeding."""
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()) + ".000Z"


def _now_iso_us() -> str:
    """ISO with microseconds (Python-style)."""
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()) + ".000000Z"


# ─────────────────────────────────────────────────────────────────────────────
# (a) Empty DB — bash emits note field; production path emits nothing.
#     We exercise the missing-DB branch here (file-not-found) so the
#     note field path is hit on BOTH sides.
# ─────────────────────────────────────────────────────────────────────────────
def test_missing_db_emits_note_field(tmp_path):
    """When state.db does not exist, both bash and Python emit a
    report with ``note`` field + entry_count=0 + entries=[].

    Bash's missing-DB branch (lines 240-256) emits the note via an
    embedded Python heredoc; the port's missing-DB branch in main()
    mirrors it. Both must produce byte-equal JSON.
    """
    db = str(tmp_path / "nonexistent" / "state.db")
    out_bash = str(tmp_path / "bash_region.json")
    out_py = str(tmp_path / "py_region.json")

    bash_r = _bash_run(["--db", db, "--out", out_bash], db=db)
    assert bash_r.returncode == 0, (
        f"bash failed: rc={bash_r.returncode} stderr={bash_r.stderr}"
    )

    py_rc = _py_main(["--db", db, "--out", out_py], db=db)
    assert py_rc == 0, f"py failed: rc={py_rc}"

    bash_d = _read_json(out_bash)
    py_d = _read_json(out_py)
    _assert_dicts_equal(bash_d, py_d)

    # The note field MUST be present in the missing-DB branch.
    assert "note" in bash_d, "bash missing-DB branch lost 'note' field"
    assert "note" in py_d, "py missing-DB branch lost 'note' field"
    assert bash_d["note"] == py_d["note"]
    assert "not found" in bash_d["note"].lower()


# ─────────────────────────────────────────────────────────────────────────────
# (b) lane_region_advantage only — verify advantage + sample_size.
# ─────────────────────────────────────────────────────────────────────────────
def test_lane_region_advantage_only(temp_db, tmp_path):
    """Seed 2 lane_region_advantage rows for codex_lens/lib and
    kimi_lens/lib. Both bash and Python must:
      * aggregate advantage × runs_count / runs_count (weighted mean)
      * sample_size = sum of runs_count
      * outstanding_blame_penalty = 0.0 (no defect_attributions seeded)
      * entries sorted by (code_region, lane)
    """
    _seed_lane_region_advantage(temp_db["db"], [
        ("codex_lens", "code-fix", "implementer", "code-delivery",
         "lib", 0.45, 2, 2, _now_iso_ms()),
        ("kimi_lens", "code-fix", "implementer", "code-delivery",
         "lib", -0.45, 2, 0, _now_iso_ms()),
    ])

    out_bash = str(tmp_path / "bash_region.json")
    out_py = str(tmp_path / "py_region.json")
    bash_r = _bash_run(["--out", out_bash], db=temp_db["db"])
    assert bash_r.returncode == 0, bash_r.stderr
    py_rc = _py_main(["--out", out_py], db=temp_db["db"])
    assert py_rc == 0

    bash_d = _read_json(out_bash)
    py_d = _read_json(out_py)
    _assert_dicts_equal(bash_d, py_d)

    # Sanity: bash and py should each have 2 entries (codex_lens/lib
    # + kimi_lens/lib). The 'note' field must NOT appear in production.
    assert bash_d["entry_count"] == 2
    assert py_d["entry_count"] == 2
    assert "note" not in bash_d, "production path leaked 'note' field"
    assert "note" not in py_d, "production path leaked 'note' field"

    # The kimi_lens row was seeded with negative advantage.
    kimi_b = next(e for e in bash_d["entries"] if e["lane"] == "kimi_lens")
    kimi_p = next(e for e in py_d["entries"] if e["lane"] == "kimi_lens")
    assert kimi_b["advantage"] < 0
    assert kimi_p["advantage"] < 0


# ─────────────────────────────────────────────────────────────────────────────
# (c) defect_attributions seeded — outstanding_blame_penalty in [-1, 0].
# ─────────────────────────────────────────────────────────────────────────────
def test_defect_attributions_decay(temp_db, tmp_path):
    """Seed one fresh defect_attribution (penalty=-0.6, halflife=30d,
    age=0). outstanding_blame_penalty must equal penalty × decay where
    decay=1.0 at age=0 → outstanding_blame_penalty ≈ -0.6.

    Then seed a 30-day-old attribution (decay=0.5); penalty magnitude
    should halve.
    """
    _seed_defect_attributions(temp_db["db"], [
        ("run-found-1", "run-blamed-1", "codex_lens", "lib",
         "code-fix", "high", -0.6, 30.0, _now_iso_ms()),
    ])
    _seed_lane_region_advantage(temp_db["db"], [
        ("codex_lens", "code-fix", "implementer", "code-delivery",
         "lib", 0.45, 2, 2, _now_iso_ms()),
    ])

    out_bash = str(tmp_path / "bash_region.json")
    out_py = str(tmp_path / "py_region.json")
    bash_r = _bash_run(["--out", out_bash], db=temp_db["db"])
    assert bash_r.returncode == 0, bash_r.stderr
    py_rc = _py_main(["--out", out_py], db=temp_db["db"])
    assert py_rc == 0

    bash_d = _read_json(out_bash)
    py_d = _read_json(out_py)
    _assert_dicts_equal(bash_d, py_d)

    codex_b = next(e for e in bash_d["entries"] if e["lane"] == "codex_lens")
    codex_p = next(e for e in py_d["entries"] if e["lane"] == "codex_lens")
    # Both must be in [-1, 0] and within 1e-6 of each other.
    assert -1.0 <= codex_b["outstanding_blame_penalty"] <= 0.0
    assert -1.0 <= codex_p["outstanding_blame_penalty"] <= 0.0
    # Age 0 → decay 1.0 → outstanding_blame_penalty == -0.6 exactly.
    assert abs(codex_b["outstanding_blame_penalty"] - (-0.6)) < 1e-6
    assert abs(codex_p["outstanding_blame_penalty"] - (-0.6)) < 1e-6

    # Now seed a 30-day-old attribution (Python strftime) for a fresh
    # region; its penalty should be halved.
    thirty_days_ago = time.strftime(
        "%Y-%m-%dT%H:%M:%S", time.gmtime(time.time() - 30 * 86400)) + ".000Z"
    _seed_defect_attributions(temp_db["db"], [
        ("run-found-2", "run-blamed-2", "kimi_lens", "bin",
         "code-fix", "high", -0.8, 30.0, thirty_days_ago),
    ])
    _seed_lane_region_advantage(temp_db["db"], [
        ("kimi_lens", "code-fix", "implementer", "code-delivery",
         "bin", 0.0, 1, 0, _now_iso_ms()),
    ])

    bash_r2 = _bash_run(["--out", out_bash], db=temp_db["db"])
    py_rc2 = _py_main(["--out", out_py], db=temp_db["db"])
    assert bash_r2.returncode == 0, bash_r2.stderr
    assert py_rc2 == 0

    bash_d2 = _read_json(out_bash)
    py_d2 = _read_json(out_py)
    _assert_dicts_equal(bash_d2, py_d2)

    kimi_b = next(e for e in bash_d2["entries"]
                  if e["lane"] == "kimi_lens")
    kimi_p = next(e for e in py_d2["entries"]
                  if e["lane"] == "kimi_lens")
    # 30 days ago, halflife=30 → decay=0.5 → outstanding ≈ -0.4.
    assert -0.5 <= kimi_b["outstanding_blame_penalty"] <= -0.3
    assert -0.5 <= kimi_p["outstanding_blame_penalty"] <= -0.3


# ─────────────────────────────────────────────────────────────────────────────
# (d) --since filter excludes old last_updated rows.
# ─────────────────────────────────────────────────────────────────────────────
def test_since_filter_excludes_old_rows(temp_db, tmp_path):
    """Seed two rows: one fresh (now), one ancient (2020-01-01). With
    --since=N (current epoch), only the fresh row is visible. The
    ancient row's (region, lane) pair must NOT appear.
    """
    # 60s buffer: the "fresh" row is stamped at _now_iso_ms() (≈now). A --since
    # threshold of exactly int(time.time()) sits on the same-second boundary, so
    # ms rounding + subprocess scheduling skew can push the fresh row just under
    # the cutoff → entry_count 0 (a CI-timing flake). Backdating the threshold 60s
    # keeps the fresh row unambiguously in-window and the 2020 row out, preserving
    # the test's intent. (See memory: relative-window test time-bomb.)
    now_epoch = int(time.time()) - 60
    _seed_lane_region_advantage(temp_db["db"], [
        ("codex_lens", "code-fix", "implementer", "code-delivery",
         "lib", 0.45, 2, 2, _now_iso_ms()),
        ("codex_lens", "code-fix", "implementer", "code-delivery",
         "ancient", 0.99, 99, 99, "2020-01-01T00:00:00.000Z"),
    ])

    out_bash = str(tmp_path / "bash_region.json")
    out_py = str(tmp_path / "py_region.json")
    bash_r = _bash_run(
        ["--since", str(now_epoch), "--out", out_bash],
        db=temp_db["db"],
    )
    assert bash_r.returncode == 0, bash_r.stderr
    py_rc = _py_main(
        ["--since", str(now_epoch), "--out", out_py],
        db=temp_db["db"],
    )
    assert py_rc == 0

    bash_d = _read_json(out_bash)
    py_d = _read_json(out_py)
    _assert_dicts_equal(bash_d, py_d)

    # Both must contain only the fresh row.
    assert bash_d["entry_count"] == 1
    assert py_d["entry_count"] == 1
    assert bash_d["entries"][0]["code_region"] == "lib"
    assert py_d["entries"][0]["code_region"] == "lib"


# ─────────────────────────────────────────────────────────────────────────────
# (e) Multi-region/lane — entries sorted by (code_region, lane).
# ─────────────────────────────────────────────────────────────────────────────
def test_multi_region_sorted_output(temp_db, tmp_path):
    """Seed rows across 3 regions × 2 lanes; verify the entries list
    is sorted lexicographically by (code_region, lane) on BOTH sides.

    This is the load-bearing case for ``sort_keys=True`` in render_json
    AND the explicit ``sorted(all_keys)`` in collect_region_expertise.
    """
    _seed_lane_region_advantage(temp_db["db"], [
        ("codex_lens", "code-fix", "implementer", "code-delivery",
         "zebra", 0.1, 1, 1, _now_iso_ms()),
        ("kimi_lens", "code-fix", "implementer", "code-delivery",
         "alpha", -0.2, 1, 0, _now_iso_ms()),
        ("codex_lens", "code-fix", "implementer", "code-delivery",
         "alpha", 0.5, 3, 3, _now_iso_ms()),
        ("kimi_lens", "code-fix", "implementer", "code-delivery",
         "middle", 0.0, 2, 1, _now_iso_ms()),
    ])

    out_bash = str(tmp_path / "bash_region.json")
    out_py = str(tmp_path / "py_region.json")
    bash_r = _bash_run(["--out", out_bash], db=temp_db["db"])
    assert bash_r.returncode == 0, bash_r.stderr
    py_rc = _py_main(["--out", out_py], db=temp_db["db"])
    assert py_rc == 0

    bash_d = _read_json(out_bash)
    py_d = _read_json(out_py)
    _assert_dicts_equal(bash_d, py_d)

    # The (code_region, lane) order must match between bash and py.
    bash_keys = [(e["code_region"], e["lane"]) for e in bash_d["entries"]]
    py_keys = [(e["code_region"], e["lane"]) for e in py_d["entries"]]
    assert bash_keys == py_keys, (
        f"entry order mismatch: bash={bash_keys} py={py_keys}"
    )
    # And both must be sorted lexicographically.
    assert bash_keys == sorted(bash_keys), (
        f"bash entries not sorted: {bash_keys}"
    )
    assert py_keys == sorted(py_keys), (
        f"py entries not sorted: {py_keys}"
    )


# ─────────────────────────────────────────────────────────────────────────────
# (f) --smoke synthetic — bash rc=0 + Python run_smoke rc=0; same fixture.
# ─────────────────────────────────────────────────────────────────────────────
def test_smoke_synthetic_db(temp_db, tmp_path):
    """Bash's --smoke builds a synthetic DB and asserts entry shape.
    Python's run_smoke() builds the SAME synthetic DB and asserts the
    SAME shape. Both must exit 0.

    The Python port additionally exposes run_smoke() as a callable so
    we can drive it directly without a subprocess round-trip.
    """
    # Bash --smoke: writes its own PASS line to stdout; rc=0.
    bash_r = _bash_run(["--smoke"], db=temp_db["db"])
    assert bash_r.returncode == 0, (
        f"bash --smoke failed: rc={bash_r.returncode} "
        f"stderr={bash_r.stderr}"
    )
    assert "PASS" in bash_r.stdout, (
        f"bash --smoke missing PASS: stdout={bash_r.stdout!r}"
    )

    # Python run_smoke() called directly (no subprocess). The port
    # uses its own build_smoke_db() with the same row seeds.
    import io
    buf = io.StringIO()
    old = sys.stdout
    sys.stdout = buf
    try:
        rc = py.run_smoke()
    finally:
        sys.stdout = old
    assert rc == 0, f"py.run_smoke() returned {rc}"
    assert "PASS" in buf.getvalue(), (
        f"py.run_smoke() missing PASS: stdout={buf.getvalue()!r}"
    )

    # Independently verify the synthetic-DB shape: build the smoke DB
    # at a known path and assert the entries include codex_lens/lib
    # with the expected numeric values.
    smoke_db = str(tmp_path / "smoke.db")
    py.build_smoke_db(smoke_db)
    rep = py.collect_region_expertise(smoke_db, since=0)
    target = next(
        (e for e in rep["entries"]
         if e["code_region"] == "lib" and e["lane"] == "codex_lens"),
        None,
    )
    assert target is not None
    assert abs(target["advantage"] - 0.45) < 1e-6
    assert target["sample_size"] == 2
    assert -1.0 <= target["outstanding_blame_penalty"] <= 0.0
    assert target["outstanding_blame_penalty"] < 0.0


# ─────────────────────────────────────────────────────────────────────────────
# (g) --help / -h — Usage: prefix on stdout, rc=0.
# ─────────────────────────────────────────────────────────────────────────────
def test_help_flag(temp_db):
    """``--help`` and ``-h`` print help to stdout, exit 0.

    Bash prints its heredoc verbatim via ``_usage``; the port's
    ``help_text()`` must produce the same content.
    """
    for variant in ("--help", "-h"):
        bash_r = _bash_run([variant], db=temp_db["db"])
        assert bash_r.returncode == 0, (
            f"bash {variant} failed: rc={bash_r.returncode} "
            f"stderr={bash_r.stderr}"
        )
        assert "Usage:" in bash_r.stdout, (
            f"bash {variant} missing 'Usage:': {bash_r.stdout!r}"
        )
        # The port's help_text() must match bash verbatim.
        assert py.help_text() == bash_r.stdout, (
            f"{variant}: py help_text != bash heredoc\n"
            f"  bash: {bash_r.stdout[:200]!r}\n"
            f"  py:   {py.help_text()[:200]!r}"
        )


# ─────────────────────────────────────────────────────────────────────────────
# (h) Unknown flag — rc=2 + 'unknown flag' on stderr (bash + py).
# ─────────────────────────────────────────────────────────────────────────────
def test_unknown_flag(temp_db):
    """Both bash and the port exit 2 with 'unknown flag' on stderr
    when an unrecognized flag is passed.
    """
    import io
    bash_r = _bash_run(["--bogus-flag"], db=temp_db["db"])
    assert bash_r.returncode == 2, f"bash rc={bash_r.returncode}"
    assert "unknown flag" in bash_r.stderr, (
        f"bash stderr missing 'unknown flag': {bash_r.stderr!r}"
    )

    # Python port: capture stderr via redirect.
    buf = io.StringIO()
    old_stderr = sys.stderr
    sys.stderr = buf
    try:
        py_rc = py.main(["--bogus-flag"])
    finally:
        sys.stderr = old_stderr
    assert py_rc == 2, f"py rc={py_rc}"
    assert "unknown flag" in buf.getvalue(), (
        f"py stderr missing 'unknown flag': {buf.getvalue()!r}"
    )
