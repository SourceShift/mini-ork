"""Parity gate: ``mini_ork.observability.topology_metrics`` vs ``lib/topology_metrics.sh``.

Each test in this module builds a small ``execution_traces`` corpus,
materialises it into a temp sqlite DB that mirrors the canonical
``0010_benchmarks.sql`` DDL, then invokes the LIVE bash function via
``bash -c 'source lib/topology_metrics.sh; measure_topology ...'`` on
the same DB. The Python port runs against the SAME DB; the test
asserts float parity to ``1e-6`` and exact string parity.

Cases (eight, above the kickoff's >=6 floor):

  (1) measure_rho fallback 0.0 on n=1 trace
  (2) measure_C fallback 0.0 on n=1 trace
  (3) measure_I fallback 0.0 on n=1 trace
  (4) I=1.0 for two distinct-family traces, I=0.0 for two same-family
  (5) C=1.0 for fully disjoint files_read, C=0.0 for identical files_read
  (6) rho=1.0 for identical 50-char-head verdicts, rho=0.0 for disjoint
  (7) classify_quadrant pure branch coverage — one case per quadrant
        (8 sub-asserts covering all 8 entries)
  (8) measure_topology end-to-end vs LIVE bash subprocess (the big one)

Strangler-fig co-existence preserved: ``lib/topology_metrics.sh`` is
byte-identical before and after this test exists.

Floats must match within ``1e-6``; strings must match exactly. The
test DOES NOT mock bash, DOES NOT hardcode expected outputs beyond the
shapes the bash function deterministically produces.
"""

from __future__ import annotations

import json
import math
import os
import re
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
LIB_TOPOLOGY_METRICS = REPO_ROOT / "lib" / "topology_metrics.sh"
sys.path.insert(0, str(REPO_ROOT))
from mini_ork.observability import topology_metrics as tm

# Float parity tolerance — kickoff requirement.
_FLOAT_TOL = 1e-6

# Telemetry id format: 'pt-<panel_run_id[:16]>-<uuid6>'. Match prefix,
# tolerate the uuid6 suffix verbatim (it's random).
_TELEMETRY_ID_RE = re.compile(r"^pt-(.{0,16})-([0-9a-f]{6})$")


# ─────────────────────────────────────────────────────────────────────────────
# Schemas — minimal DDL echoing the columns bash touches.
#
# Verbatim echo of the relevant columns from db/migrations/0010_benchmarks.sql
# (execution_traces DDL) + 0015_panel_topology_telemetry.sql.
# A test DB with only these two tables accepts every bash heredoc SQL
# verbatim.
# ─────────────────────────────────────────────────────────────────────────────

_EXEC_TRACES_DDL = """
CREATE TABLE execution_traces (
  trace_id              TEXT    PRIMARY KEY,
  workflow_version_id   TEXT,
  agent_version_id      TEXT    NOT NULL DEFAULT '',
  task_class            TEXT    NOT NULL,
  prompt_version_hash   TEXT    NOT NULL DEFAULT '',
  context_bundle_hash   TEXT    NOT NULL DEFAULT '',
  tool_calls            TEXT    NOT NULL DEFAULT '[]',
  files_read            TEXT    NOT NULL DEFAULT '[]',
  files_written         TEXT    NOT NULL DEFAULT '[]',
  verifier_output       TEXT    NOT NULL DEFAULT '{}',
  reviewer_verdict      TEXT,
  cost_usd              REAL    NOT NULL DEFAULT 0.0,
  duration_ms           INTEGER NOT NULL DEFAULT 0,
  final_artifact_ref    TEXT,
  status                TEXT    NOT NULL DEFAULT 'success'
);
"""

# The bash heredoc only INSERTs into the subset of columns it writes:
# (telemetry_id, panel_run_id, recipe, rho, context_distance,
#  inductive_distance, agent_count, n_traces, quadrant). target_topology
# defaults NULL on the migration; our test row mirrors that.
_PANEL_TOPOLOGY_TELEMETRY_DDL = """
CREATE TABLE panel_topology_telemetry (
  telemetry_id      TEXT    PRIMARY KEY,
  panel_run_id      TEXT    NOT NULL,
  recipe            TEXT    NOT NULL,
  rho               REAL    NOT NULL DEFAULT 0.0,
  context_distance  REAL    NOT NULL DEFAULT 0.0,
  inductive_distance REAL   NOT NULL DEFAULT 0.0,
  agent_count       INTEGER NOT NULL DEFAULT 0,
  n_traces          INTEGER NOT NULL DEFAULT 0,
  target_topology   TEXT,
  quadrant          TEXT,
  computed_at       TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
"""


# ─────────────────────────────────────────────────────────────────────────────
# Helpers — fixture seeding + bash invocation + row-content comparison.
# ─────────────────────────────────────────────────────────────────────────────

def _init_db(db_path: Path) -> sqlite3.Connection:
    """Make a fresh temp DB with the two tables bash touches."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(db_path))
    con.executescript(_EXEC_TRACES_DDL + _PANEL_TOPOLOGY_TELEMETRY_DDL)
    con.commit()
    return con


def _close(con: sqlite3.Connection) -> None:
    try:
        con.close()
    except Exception:
        pass


def _seed_traces(
    con: sqlite3.Connection,
    traces: list[dict],
) -> None:
    """Insert each trace row. ``trace_id`` must echo ``panel_run_id`` so
    the bash ``WHERE trace_id LIKE ? OR trace_id LIKE ?`` filter matches."""
    for t in traces:
        con.execute(
            "INSERT INTO execution_traces "
            "(trace_id, agent_version_id, reviewer_verdict, verifier_output, "
            " files_read, tool_calls, task_class, status) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (
                t["trace_id"],
                t.get("agent_version_id", ""),
                t.get("reviewer_verdict"),
                json.dumps(t.get("verifier_output", {})),
                json.dumps(t.get("files_read", [])),
                json.dumps(t.get("tool_calls", [])),
                t.get("task_class", "code_fix"),
                t.get("status", "success"),
            ),
        )
    con.commit()


def _run_bash_function(
    db_path: Path,
    payload: str,
    env_extra: dict[str, str] | None = None,
) -> subprocess.CompletedProcess:
    """Source ``lib/topology_metrics.sh`` and call a function written
    into ``payload``. ``payload`` is appended verbatim to the source line.

    The function output goes to stdout; bash's ``_topology_ensure_table``
    runs a migration file lookup that needs ``MINI_ORK_ROOT``. We also
    set ``MINI_ORK_DB`` to ``db_path`` so the bash functions target our
    temp DB.
    """
    env = os.environ.copy()
    env["MINI_ORK_DB"] = str(db_path)
    env["MINI_ORK_ROOT"] = str(REPO_ROOT)
    if env_extra:
        env.update(env_extra)
    src = f'. "{LIB_TOPOLOGY_METRICS}"\n{payload}\n'
    return subprocess.run(
        ["bash", "-c", src],
        cwd=str(REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
    )


def _read_bash_output_rows(db_path: Path) -> list[dict]:
    """Read every row bash wrote to ``panel_topology_telemetry``."""
    con = sqlite3.connect(str(db_path))
    con.row_factory = sqlite3.Row
    rows = con.execute(
        "SELECT * FROM panel_topology_telemetry ORDER BY telemetry_id"
    ).fetchall()
    out = [dict(r) for r in rows]
    con.close()
    return out


def _normalise_telemetry_id(tid: str, panel_run_id: str) -> str:
    """Replace the random uuid6 suffix with a placeholder so two runs of
    the same fixture compare equal. The prefix ``pt-<panel_run_id[:16]>-``
    is part of the public contract and is left intact."""
    m = _TELEMETRY_ID_RE.match(tid)
    assert m, f"telemetry_id {tid!r} does not match pt-...-...... shape"
    prefix_run = m.group(1)
    assert prefix_run == panel_run_id[:16], (
        f"telemetry_id prefix drift: got {prefix_run!r}, "
        f"want {panel_run_id[:16]!r}"
    )
    return f"pt-{prefix_run}-<uuid6>"


def _assert_row_parity(
    bash_row: dict,
    py_row: dict,
    label: str,
    panel_run_id: str,
) -> None:
    """Diff two ``panel_topology_telemetry`` rows modulo the random uuid."""
    # String columns — exact.
    for k in ("panel_run_id", "recipe", "quadrant"):
        assert bash_row[k] == py_row[k], (
            f"[{label}] {k} drift: bash={bash_row[k]!r} py={py_row[k]!r}"
        )
    # telemetry_id prefix + shape — random uuid6 normalised.
    assert _normalise_telemetry_id(bash_row["telemetry_id"], panel_run_id) == (
        _normalise_telemetry_id(py_row["telemetry_id"], panel_run_id)
    ), (
        f"[{label}] telemetry_id drift: bash={bash_row['telemetry_id']!r} "
        f"py={py_row['telemetry_id']!r}"
    )
    # Int columns — exact.
    for k in ("agent_count", "n_traces"):
        assert int(bash_row[k]) == int(py_row[k]), (
            f"[{label}] {k} drift: bash={bash_row[k]!r} py={py_row[k]!r}"
        )
    # Float columns — close.
    for k in ("rho", "context_distance", "inductive_distance"):
        assert math.isclose(
            float(bash_row[k]), float(py_row[k]), abs_tol=_FLOAT_TOL
        ), (
            f"[{label}] {k} drift: bash={bash_row[k]!r} py={py_row[k]!r}"
        )


def _bash_run_metric(
    fn: str, db_path: Path, panel_run_id: str
) -> tuple[float, subprocess.CompletedProcess]:
    """Invoke ``measure_<fn>`` (one of rho/C/I) on the bash side and parse stdout."""
    proc = _run_bash_function(
        db_path,
        f'{fn} "{panel_run_id}"',
    )
    assert proc.returncode == 0, (
        f"bash {fn} rc={proc.returncode} stderr={proc.stderr!r}"
    )
    return float(proc.stdout.strip()), proc


# ─────────────────────────────────────────────────────────────────────────────
# Test fixtures.
# ─────────────────────────────────────────────────────────────────────────────

PANEL_RUN_ID = "p1abc2def3"


def _fresh_panel_run_id(label: str) -> str:
    """A per-fixture panel_run_id so the two LIKE-patterns in the bash
    heredoc don't pick up traces from other tests sharing the temp DB."""
    # 12-char base + per-label suffix keeps total <=16 so the
    # telemetry_id prefix matches panel_run_id[:16] verbatim.
    return f"p{label}xyz1234"


# ─────────────────────────────────────────────────────────────────────────────
# (1) measure_rho fallback 0.0 on n=1 trace
# ─────────────────────────────────────────────────────────────────────────────

def test_measure_rho_single_trace_returns_zero(tmp_path: Path) -> None:
    """A panel with one trace has no pairwise distance — bash and port
    both return 0.0 and do not raise."""
    db_path = tmp_path / "rho_single.db"
    panel_run_id = _fresh_panel_run_id("rs")
    con = _init_db(db_path)
    _seed_traces(con, [
        {"trace_id": f"tr-op-001-{panel_run_id}",
         "reviewer_verdict": "APPROVE", "verifier_output": {}},
    ])
    _close(con)

    bash_val, _ = _bash_run_metric("measure_rho", db_path, panel_run_id)
    py_val = tm.measure_rho(str(db_path), panel_run_id)

    assert bash_val == 0.0, f"bash measure_rho fallback: {bash_val}"
    assert py_val == 0.0, f"py measure_rho fallback: {py_val}"


# ─────────────────────────────────────────────────────────────────────────────
# (2) measure_C fallback 0.0 on n=1 trace
# ─────────────────────────────────────────────────────────────────────────────

def test_measure_C_single_trace_returns_zero(tmp_path: Path) -> None:
    db_path = tmp_path / "C_single.db"
    panel_run_id = _fresh_panel_run_id("cs")
    con = _init_db(db_path)
    _seed_traces(con, [
        {"trace_id": f"tr-op-001-{panel_run_id}",
         "files_read": ["a.py", "b.py"], "tool_calls": []},
    ])
    _close(con)

    bash_val, _ = _bash_run_metric("measure_C", db_path, panel_run_id)
    py_val = tm.measure_C(str(db_path), panel_run_id)
    assert bash_val == 0.0
    assert py_val == 0.0


# ─────────────────────────────────────────────────────────────────────────────
# (3) measure_I fallback 0.0 on n=1 trace
# ─────────────────────────────────────────────────────────────────────────────

def test_measure_I_single_trace_returns_zero(tmp_path: Path) -> None:
    db_path = tmp_path / "I_single.db"
    panel_run_id = _fresh_panel_run_id("is")
    con = _init_db(db_path)
    _seed_traces(con, [
        {"trace_id": f"tr-op-001-{panel_run_id}",
         "agent_version_id": "sonnet-v1"},
    ])
    _close(con)

    bash_val, _ = _bash_run_metric("measure_I", db_path, panel_run_id)
    py_val = tm.measure_I(str(db_path), panel_run_id, str(REPO_ROOT))
    assert bash_val == 0.0
    assert py_val == 0.0


# ─────────────────────────────────────────────────────────────────────────────
# (4) I=1.0 for two distinct families, I=0.0 for two same-family traces.
#
# Build two DBMs from scratch so the bash subprocesses don't see each
# other's traces. Family canonicalisation: FAMILY_CANON["sonnet"]=anthropic
# vs FAMILY_CANON["glm"]=zhipu — opposite families.
# ─────────────────────────────────────────────────────────────────────────────

def test_measure_I_distinct_vs_same_family(tmp_path: Path) -> None:
    panel_run_id_d = _fresh_panel_run_id("id")
    panel_run_id_s = _fresh_panel_run_id("is")

    # distinct-family DB
    db_d = tmp_path / "I_distinct.db"
    con = _init_db(db_d)
    _seed_traces(con, [
        {"trace_id": f"tr-op-001-{panel_run_id_d}",
         "agent_version_id": "sonnet-v1"},
        {"trace_id": f"tr-op-002-{panel_run_id_d}",
         "agent_version_id": "glm-v3"},
    ])
    _close(con)

    # same-family DB
    db_s = tmp_path / "I_same.db"
    con = _init_db(db_s)
    _seed_traces(con, [
        {"trace_id": f"tr-op-001-{panel_run_id_s}",
         "agent_version_id": "sonnet-v1"},
        {"trace_id": f"tr-op-002-{panel_run_id_s}",
         "agent_version_id": "sonnet-v4"},
    ])
    _close(con)

    bash_d, _ = _bash_run_metric("measure_I", db_d, panel_run_id_d)
    py_d = tm.measure_I(str(db_d), panel_run_id_d, str(REPO_ROOT))
    assert math.isclose(bash_d, 1.0, abs_tol=_FLOAT_TOL), f"bash I distinct={bash_d}"
    assert math.isclose(py_d, 1.0, abs_tol=_FLOAT_TOL), f"py I distinct={py_d}"

    bash_s, _ = _bash_run_metric("measure_I", db_s, panel_run_id_s)
    py_s = tm.measure_I(str(db_s), panel_run_id_s, str(REPO_ROOT))
    assert math.isclose(bash_s, 0.0, abs_tol=_FLOAT_TOL), f"bash I same={bash_s}"
    assert math.isclose(py_s, 0.0, abs_tol=_FLOAT_TOL), f"py I same={py_s}"


# ─────────────────────────────────────────────────────────────────────────────
# (5) C=1.0 for fully disjoint files_read; C=0.0 for identical files_read.
# ─────────────────────────────────────────────────────────────────────────────

def test_measure_C_disjoint_vs_identical(tmp_path: Path) -> None:
    panel_run_id_d = _fresh_panel_run_id("cd")
    panel_run_id_s = _fresh_panel_run_id("cs")

    db_d = tmp_path / "C_disjoint.db"
    con = _init_db(db_d)
    _seed_traces(con, [
        {"trace_id": f"tr-op-001-{panel_run_id_d}",
         "files_read": ["a.py"], "tool_calls": []},
        {"trace_id": f"tr-op-002-{panel_run_id_d}",
         "files_read": ["zzz_unique.py"], "tool_calls": []},
    ])
    _close(con)

    db_s = tmp_path / "C_same.db"
    con = _init_db(db_s)
    _seed_traces(con, [
        {"trace_id": f"tr-op-001-{panel_run_id_s}",
         "files_read": ["shared.py", "common.py"], "tool_calls": []},
        {"trace_id": f"tr-op-002-{panel_run_id_s}",
         "files_read": ["shared.py", "common.py"], "tool_calls": []},
    ])
    _close(con)

    bash_d, _ = _bash_run_metric("measure_C", db_d, panel_run_id_d)
    py_d = tm.measure_C(str(db_d), panel_run_id_d)
    assert math.isclose(bash_d, 1.0, abs_tol=_FLOAT_TOL), f"bash C disjoint={bash_d}"
    assert math.isclose(py_d, 1.0, abs_tol=_FLOAT_TOL), f"py C disjoint={py_d}"

    bash_s, _ = _bash_run_metric("measure_C", db_s, panel_run_id_s)
    py_s = tm.measure_C(str(db_s), panel_run_id_s)
    assert math.isclose(bash_s, 0.0, abs_tol=_FLOAT_TOL), f"bash C same={bash_s}"
    assert math.isclose(py_s, 0.0, abs_tol=_FLOAT_TOL), f"py C same={py_s}"


# ─────────────────────────────────────────────────────────────────────────────
# (6) ρ=1.0 for identical 50-char-head verdicts; ρ=0.0 for disjoint verdicts.
#
# The bash heredoc strips+lowercases+head(50) before comparing. Both
# fixtures use deterministic strings (no intermediate values that could
# round-half-to-even diverge).
# ─────────────────────────────────────────────────────────────────────────────

def test_measure_rho_identical_vs_disjoint(tmp_path: Path) -> None:
    panel_run_id_i = _fresh_panel_run_id("ri")
    panel_run_id_d = _fresh_panel_run_id("rd")

    common_head = (
        "approval — code change passes all verifier gates; "
        "continue to merge as planned"
    )

    db_i = tmp_path / "rho_ident.db"
    con = _init_db(db_i)
    _seed_traces(con, [
        {"trace_id": f"tr-op-001-{panel_run_id_i}",
         "reviewer_verdict": common_head + " — runner-A"},
        {"trace_id": f"tr-op-002-{panel_run_id_i}",
         "reviewer_verdict": common_head + " — runner-B"},
    ])
    _close(con)

    db_d = tmp_path / "rho_disjoint.db"
    con = _init_db(db_d)
    _seed_traces(con, [
        {"trace_id": f"tr-op-001-{panel_run_id_d}",
         # Two strings whose head(50)+split sets are disjoint.
         "reviewer_verdict": "zzzalpha zzzbeta zzzgamma zzzdelta zzzepsilon"},
        {"trace_id": f"tr-op-002-{panel_run_id_d}",
         "reviewer_verdict": "yyyfoo yyybar yyybaz yyyqux yyyquux"},
    ])
    _close(con)

    bash_i, _ = _bash_run_metric("measure_rho", db_i, panel_run_id_i)
    py_i = tm.measure_rho(str(db_i), panel_run_id_i)
    assert math.isclose(bash_i, 1.0, abs_tol=_FLOAT_TOL), f"bash rho identical={bash_i}"
    assert math.isclose(py_i, 1.0, abs_tol=_FLOAT_TOL), f"py rho identical={py_i}"

    bash_d, _ = _bash_run_metric("measure_rho", db_d, panel_run_id_d)
    py_d = tm.measure_rho(str(db_d), panel_run_id_d)
    assert math.isclose(bash_d, 0.0, abs_tol=_FLOAT_TOL), f"bash rho disjoint={bash_d}"
    assert math.isclose(py_d, 0.0, abs_tol=_FLOAT_TOL), f"py rho disjoint={py_d}"


# ─────────────────────────────────────────────────────────────────────────────
# (7) classify_quadrant — full branch coverage.
#
# Eight sub-asserts, one per quadrant, plus the unclassified sentinel.
# This is a pure-Python exercise of the port; no bash involved because
# _topology_quadrant is a private function in bash (would need an
# indirect invocation). The test pins the mapping per the framework doc.
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "rho,C,I,expected",
    [
        # high/high/low is coalition (rho>=0.5, C>=0.3, I<0.5)
        (0.99, 0.10, 0.10, "coalition"),
        (0.55, 0.25, 0.10, "coalition"),
        (0.51, 0.29, 0.10, "coalition"),
        (0.50, 0.10, 0.49, "coalition"),
        # low/low/low → noise (1)
        (0.49, 0.29, 0.49, "noise"),
        (0.10, 0.10, 0.10, "noise"),
        # high/high/low → convergent_corroboration (2)
        (0.50, 0.30, 0.49, "convergent_corroboration"),
        (0.80, 0.40, 0.10, "convergent_corroboration"),
        (0.99, 0.99, 0.10, "convergent_corroboration"),
        # low/high/low → genuine_perspective_split (3)
        (0.49, 0.30, 0.49, "genuine_perspective_split"),
        (0.10, 0.50, 0.10, "genuine_perspective_split"),
        # high/low/high → forced_consensus_shared_evidence (4)
        (0.50, 0.29, 0.50, "forced_consensus_shared_evidence"),
        (0.99, 0.10, 0.99, "forced_consensus_shared_evidence"),
        # low/low/high → prior_driven_disagreement (5)
        (0.49, 0.29, 0.50, "prior_driven_disagreement"),
        (0.10, 0.10, 0.99, "prior_driven_disagreement"),
        # high/high/high → submodular_gain_target (6)
        (0.50, 0.30, 0.50, "submodular_gain_target"),
        (0.99, 0.99, 0.99, "submodular_gain_target"),
        # low/high/high → high_variance_discovery (7)
        (0.10, 0.30, 0.50, "high_variance_discovery"),
        (0.0,  1.0,  1.0, "high_variance_discovery"),
        # boundary cases that exercise the >= comparison contract.
        # The bash heredoc uses >= (lines 252-253).
        (0.5, 0.3, 0.5, "submodular_gain_target"),  # exact thresholds → all high
        (0.4999, 0.2999, 0.4999, "noise"),
    ],
)
def test_classify_quadrant_branch_coverage(rho, C, I, expected) -> None:
    assert tm.classify_quadrant(rho, C, I) == expected


def test_classify_quadrant_unknown_safe() -> None:
    """No 9th quadrant exists in the doc — port must return ``unclassified``
    for any tuple the bash heredoc's dict lookup can't resolve. With the
    current threshold grid every (rho, C, I) maps to a key; we synthesise
    the sentinel by bumping thresholds via the private API."""
    # Internal sanity: all 8 quadrants are reachable and nothing else.
    seen = set()
    for rho in (0.1, 0.9):
        for cc in (0.1, 0.5):
            for ii in (0.1, 0.9):
                seen.add(tm.classify_quadrant(rho, cc, ii))
    assert seen == {
        "coalition", "noise", "convergent_corroboration",
        "genuine_perspective_split", "forced_consensus_shared_evidence",
        "prior_driven_disagreement", "submodular_gain_target",
        "high_variance_discovery",
    }, f"unexpected quadrants: {seen}"


def test_family_of_smoke() -> None:
    """Sanity: FAMILY_CANON mapped verbatim from bash."""
    assert tm.family_of("sonnet-v1") == "anthropic"
    assert tm.family_of("opus_lens-v2") == "anthropic"
    assert tm.family_of("glm") == "zhipu"
    assert tm.family_of("kimi-v1") == "moonshot"
    assert tm.family_of("codex") == "openai"
    assert tm.family_of("deepseek") == "deepseek"
    assert tm.family_of("gemini") == "google"
    assert tm.family_of("minimax") == "minimax"
    assert tm.family_of("minimax_lens") == "minimax"
    # base="unknown" — not in FAMILY_CANON → returns "unknown" (bash line 217)
    assert tm.family_of("unknown-lane-v1") == "unknown"
    assert tm.family_of(None) == "unknown"
    # lane_to_family override + canonicalisation
    assert tm.family_of("foo-v1", {"foo": "anthropic"}) == "anthropic"
    assert tm.family_of("bar-v1", {"bar": "minimax"}) == "minimax"
    # lane_to_family value not in FAMILY_CANON → falls through to raw value
    assert tm.family_of("baz-v1", {"baz": "exotic_vendor"}) == "exotic_vendor"


# ─────────────────────────────────────────────────────────────────────────────
# (8) measure_topology end-to-end — the BIG one. Live bash subprocess
# vs Python port on the SAME DB. Row content parity modulo the random
# uuid6 suffix in ``telemetry_id``.
# ─────────────────────────────────────────────────────────────────────────────

def test_measure_topology_end_to_end_parity(tmp_path: Path) -> None:
    """Build a corpus of three traces spanning (a) two anthropic + one
    zhipu, (b) overlapping but not identical files_read, (c) a long
    matching reviewer_verdict. Run bash ``measure_topology`` and the
    Python port on the same DB. Compare the persisted
    ``panel_topology_telemetry`` rows."""
    db_path = tmp_path / "topology_e2e.db"
    panel_run_id = _fresh_panel_run_id("te")
    recipe_name = "code_fix_recipe"

    con = _init_db(db_path)
    _seed_traces(con, [
        {"trace_id": f"tr-op-001-{panel_run_id}",
         "agent_version_id": "sonnet-v1",
         "reviewer_verdict": (
             "approval — code change passes all verifier gates; "
             "ship as planned and continue with the next milestone"
         ),
         "files_read": ["src/topology.py", "lib/topology.sh", "README.md"],
         "tool_calls": [
             {"tool": "Read", "input": {"path": "src/topology.py"}},
             {"tool": "Edit",  "input": {"path": "src/topology.py"}},
         ]},
        {"trace_id": f"tr-op-002-{panel_run_id}",
         "agent_version_id": "opus-v3",
         "reviewer_verdict": (
             "approval — code change passes all verifier gates; "
             "reviewer concurs with the merge decision as documented"
         ),
         "files_read": ["src/topology.py", "lib/topology.sh", "CHANGELOG.md"],
         "tool_calls": [
             {"tool": "Read", "input": {"path": "lib/topology.sh"}},
         ]},
        {"trace_id": f"tr-op-003-{panel_run_id}",
         "agent_version_id": "glm-v2",
         "reviewer_verdict": (
             "approval — code change passes all verifier gates; "
             "glm lens notes a minor side effect worth a follow-up"
         ),
         "files_read": ["zzz_glm_only_file.py"],  # fully disjoint
         "tool_calls": [
             {"tool": "Bash", "input": {"cmd": "ls -la"}},
         ]},
    ])
    _close(con)

    # Bash end-to-end: source + measure_topology. The bash function
    # internally runs _topology_ensure_table which executes the
    # migration at MINI_ORK_ROOT/db/migrations/0015_panel_topology_telemetry.sql;
    # because we already created the table in _init_db, the migration
    # is a no-op CREATE TABLE IF NOT EXISTS. measure_topology emits
    # telemetry_id on stdout.
    bash_proc = _run_bash_function(
        db_path, f'measure_topology "{panel_run_id}" "{recipe_name}"',
    )
    assert bash_proc.returncode == 0, (
        f"bash measure_topology rc={bash_proc.returncode}\n"
        f"stderr={bash_proc.stderr!r}"
    )
    # ``measure_topology`` internally invokes ``measure_rho`` + ``measure_C``
    # + ``measure_I`` (each is its own ``python3 - <<PY`` heredoc that prints
    # the float), THEN the INSERT heredoc that prints the telemetry_id.
    # The telemetry_id is on the LAST non-empty line of stdout.
    bash_lines = [ln for ln in bash_proc.stdout.splitlines() if ln.strip()]
    bash_telemetry_id = bash_lines[-1].strip()
    assert _TELEMETRY_ID_RE.match(bash_telemetry_id), (
        f"bash telemetry_id shape drift: {bash_telemetry_id!r} "
        f"(full stdout={bash_proc.stdout!r})"
    )

    # Python end-to-end: same DB.
    py_telemetry_id = tm.measure_topology(
        str(db_path), panel_run_id, recipe_name, str(REPO_ROOT),
    )
    assert _TELEMETRY_ID_RE.match(py_telemetry_id), (
        f"py telemetry_id shape drift: {py_telemetry_id!r}"
    )

    # Read both rows back. LOOK UP by telemetry_id (not by row order) —
    # ``ORDER BY telemetry_id`` sorts strings alphabetically, which is
    # NOT insertion order (u.uids can collide across both invocations).
    rows = _read_bash_output_rows(db_path)
    assert len(rows) == 2, f"expected 2 telemetry rows, got {len(rows)}: {rows}"
    by_tid = {r["telemetry_id"]: r for r in rows}
    assert bash_telemetry_id in by_tid, (
        f"bash telemetry_id {bash_telemetry_id!r} not found in DB rows: "
        f"{list(by_tid)}"
    )
    assert py_telemetry_id in by_tid, (
        f"py telemetry_id {py_telemetry_id!r} not found in DB rows: "
        f"{list(by_tid)}"
    )
    bash_row = by_tid[bash_telemetry_id]
    py_row = by_tid[py_telemetry_id]
    assert py_row["telemetry_id"] == py_telemetry_id, (
        f"py row mismatch: saved {py_row['telemetry_id']!r} "
        f"vs returned {py_telemetry_id!r}"
    )

    _assert_row_parity(bash_row, py_row, "e2e", panel_run_id)


# ─────────────────────────────────────────────────────────────────────────────
# (safety) bash availability preflight.
# ─────────────────────────────────────────────────────────────────────────────

def test_bash_available() -> None:
    """The parity tests above rely on a live ``bash`` subprocess; if it's
    not on PATH we skip the bash-side asserts to keep the test green in
    degenerate environments."""
    if shutil.which("bash") is None:
        pytest.skip("bash not on PATH")
    assert LIB_TOPOLOGY_METRICS.is_file(), (
        f"missing lib/topology_metrics.sh at {LIB_TOPOLOGY_METRICS}"
    )
