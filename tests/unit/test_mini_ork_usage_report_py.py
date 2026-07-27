"""Unit tests: mini_ork.observability.usage_report (bash parity halves removed; formerly vs bin/mini-ork-usage-report).

Each test invokes the Python port against a temp DB seeded by
``db/init.sh`` (and optionally data tables) and asserts the resulting
region_expertise.json payload semantically (floats within 1e-6,
``generated_at`` ignored). No mocks.

db/init.sh applies the live migration graph. This checkout has
``defect_attributions`` in that graph but not ``lane_region_advantage``,
so the fixture creates the report-specific region table only when the
live schema does not already provide it.

Cases (8):

  (a) missing DB → ``note`` field present, entry_count=0, entries=[].
  (b) lane_region_advantage only → entries with advantage + sample_size;
      outstanding_blame_penalty=0.0.
  (c) defect_attributions seeded → outstanding_blame_penalty in [-1, 0];
      age-weighted decay (penalty × 0.5**(age/halflife)).
  (d) --since filter excludes old ``last_updated`` rows.
  (e) multi-region/lane → entries sorted lexicographically by
      (code_region, lane) — load-bearing for ``sort_keys=True``.
  (f) --smoke synthetic → rc=0 + codex_lens/lib entry shape.
  (g) --help rc=0 + "Usage:" in stdout.
  (h) unknown flag rc=2 + "unknown flag" in stderr.

Tolerance notes:

  * Float columns (``advantage``, ``outstanding_blame_penalty``) at 1e-6.
  * Integer columns (``sample_size``, ``entry_count``, ``since``) exact.
  * The ``note`` field appears ONLY in the missing-DB branch; production
    + smoke paths MUST NOT include it.
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
from mini_ork.observability import usage_report as py

INIT_SH = REPO / "db" / "init.sh"


# ─────────────────────────────────────────────────────────────────────────────
# Fixtures / helpers
# ─────────────────────────────────────────────────────────────────────────────
@pytest.fixture
def temp_db(tmp_path, monkeypatch):
    """Spin up a real mini-ork SQLite DB via db/init.sh.

    Each test gets a fresh DB. The fixture sets ``MINI_ORK_DB`` /
    ``MINI_ORK_HOME`` / ``TZ`` in the parent process env.
    """
    for tool in ("bash", "sqlite3"):
        if not shutil.which(tool):
            pytest.skip(f"{tool} not on PATH")
    if not INIT_SH.exists():
        pytest.skip(f"missing db/init.sh at {INIT_SH}")
    home = tmp_path / "home"
    home.mkdir()
    dbp = str(home / "state.db")
    r = subprocess.run(
        ["bash", str(INIT_SH)],
        env={**os.environ, "TZ": "UTC", "MINI_ORK_HOME": str(home),
             "MINI_ORK_DB": dbp},
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        pytest.skip(f"db/init.sh failed: rc={r.returncode}\nstderr={r.stderr}")
    monkeypatch.setenv("MINI_ORK_DB", dbp)
    monkeypatch.setenv("MINI_ORK_HOME", str(home))
    monkeypatch.setenv("TZ", "UTC")
    return {"home": str(home), "db": dbp, "tmp_path": tmp_path}


def _py_main(args: list[str], *, db: str,
             env_extra: dict | None = None) -> int:
    """Invoke the Python port's ``main()`` with the given args."""
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


# ─────────────────────────────────────────────────────────────────────────────
# (a) Missing DB — the note field path.
# ─────────────────────────────────────────────────────────────────────────────
def test_missing_db_emits_note_field(tmp_path):
    """When state.db does not exist, the port emits a report with a
    ``note`` field + entry_count=0 + entries=[]."""
    db = str(tmp_path / "nonexistent" / "state.db")
    out_py = str(tmp_path / "py_region.json")

    py_rc = _py_main(["--db", db, "--out", out_py], db=db)
    assert py_rc == 0, f"py failed: rc={py_rc}"

    py_d = _read_json(out_py)
    assert "note" in py_d, "missing-DB branch lost 'note' field"
    assert "not found" in py_d["note"].lower()
    assert py_d["entry_count"] == 0
    assert py_d["entries"] == []


# ─────────────────────────────────────────────────────────────────────────────
# (b) lane_region_advantage only — verify advantage + sample_size.
# ─────────────────────────────────────────────────────────────────────────────
def test_lane_region_advantage_only(temp_db, tmp_path):
    """Seed 2 lane_region_advantage rows for codex_lens/lib and
    kimi_lens/lib. The port must:
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

    out_py = str(tmp_path / "py_region.json")
    py_rc = _py_main(["--out", out_py], db=temp_db["db"])
    assert py_rc == 0

    py_d = _read_json(out_py)
    assert py_d["entry_count"] == 2
    assert "note" not in py_d, "production path leaked 'note' field"

    codex_p = next(e for e in py_d["entries"] if e["lane"] == "codex_lens")
    kimi_p = next(e for e in py_d["entries"] if e["lane"] == "kimi_lens")
    assert abs(codex_p["advantage"] - 0.45) < 1e-6
    assert codex_p["sample_size"] == 2
    assert codex_p["outstanding_blame_penalty"] == 0.0
    assert kimi_p["advantage"] < 0
    assert abs(kimi_p["advantage"] - (-0.45)) < 1e-6


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

    out_py = str(tmp_path / "py_region.json")
    py_rc = _py_main(["--out", out_py], db=temp_db["db"])
    assert py_rc == 0

    py_d = _read_json(out_py)
    codex_p = next(e for e in py_d["entries"] if e["lane"] == "codex_lens")
    assert -1.0 <= codex_p["outstanding_blame_penalty"] <= 0.0
    # Age 0 → decay 1.0 → outstanding_blame_penalty == -0.6 exactly.
    assert abs(codex_p["outstanding_blame_penalty"] - (-0.6)) < 1e-6

    # Now seed a 30-day-old attribution for a fresh region; its penalty
    # should be halved.
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

    py_rc2 = _py_main(["--out", out_py], db=temp_db["db"])
    assert py_rc2 == 0

    py_d2 = _read_json(out_py)
    kimi_p = next(e for e in py_d2["entries"]
                  if e["lane"] == "kimi_lens")
    # 30 days ago, halflife=30 → decay=0.5 → outstanding ≈ -0.4.
    assert -0.5 <= kimi_p["outstanding_blame_penalty"] <= -0.3


# ─────────────────────────────────────────────────────────────────────────────
# (d) --since filter excludes old last_updated rows.
# ─────────────────────────────────────────────────────────────────────────────
def test_since_filter_excludes_old_rows(temp_db, tmp_path):
    """Seed two rows: one fresh (now), one ancient (2020-01-01). With
    --since=N (current epoch), only the fresh row is visible."""
    # 60s buffer: the "fresh" row is stamped at _now_iso_ms() (≈now). A --since
    # threshold of exactly int(time.time()) sits on the same-second boundary, so
    # ms rounding + scheduling skew can push the fresh row just under the
    # cutoff → entry_count 0 (a CI-timing flake). Backdating the threshold 60s
    # keeps the fresh row unambiguously in-window and the 2020 row out.
    now_epoch = int(time.time()) - 60
    _seed_lane_region_advantage(temp_db["db"], [
        ("codex_lens", "code-fix", "implementer", "code-delivery",
         "lib", 0.45, 2, 2, _now_iso_ms()),
        ("codex_lens", "code-fix", "implementer", "code-delivery",
         "ancient", 0.99, 99, 99, "2020-01-01T00:00:00.000Z"),
    ])

    out_py = str(tmp_path / "py_region.json")
    py_rc = _py_main(
        ["--since", str(now_epoch), "--out", out_py],
        db=temp_db["db"],
    )
    assert py_rc == 0

    py_d = _read_json(out_py)
    assert py_d["entry_count"] == 1
    assert py_d["entries"][0]["code_region"] == "lib"


# ─────────────────────────────────────────────────────────────────────────────
# (e) Multi-region/lane — entries sorted by (code_region, lane).
# ─────────────────────────────────────────────────────────────────────────────
def test_multi_region_sorted_output(temp_db, tmp_path):
    """Seed rows across 3 regions × 2 lanes; verify the entries list is
    sorted lexicographically by (code_region, lane).

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

    out_py = str(tmp_path / "py_region.json")
    py_rc = _py_main(["--out", out_py], db=temp_db["db"])
    assert py_rc == 0

    py_d = _read_json(out_py)
    py_keys = [(e["code_region"], e["lane"]) for e in py_d["entries"]]
    assert py_keys == sorted(py_keys), (
        f"entries not sorted: {py_keys}"
    )
    assert py_keys == [
        ("alpha", "codex_lens"), ("alpha", "kimi_lens"),
        ("middle", "kimi_lens"), ("zebra", "codex_lens"),
    ]


# ─────────────────────────────────────────────────────────────────────────────
# (f) --smoke synthetic — Python run_smoke rc=0; entry shape verified.
# ─────────────────────────────────────────────────────────────────────────────
def test_smoke_synthetic_db(temp_db, tmp_path):
    """The port's run_smoke() builds a synthetic DB and asserts entry
    shape; rc=0 + PASS on stdout. The synthetic-DB shape is additionally
    verified directly: codex_lens/lib with the expected numeric values.
    """
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

    # Independently verify the synthetic-DB shape.
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
    """help_text() prints the usage block."""
    assert "Usage:" in py.help_text()
    assert "--since" in py.help_text()
    assert "--out" in py.help_text()


# ─────────────────────────────────────────────────────────────────────────────
# (h) Unknown flag — rc=2 + 'unknown flag' on stderr.
# ─────────────────────────────────────────────────────────────────────────────
def test_unknown_flag(temp_db):
    """The port exits 2 with 'unknown flag' on stderr when an unrecognized
    flag is passed."""
    import io
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
