"""Unit tests: ``mini_ork.cli.topology`` (bash parity halves removed; formerly vs ``bin/mini-ork-topology``).

Each test builds a small ``execution_traces`` (and, where relevant,
``panel_topology_telemetry``) corpus in a temp sqlite DB, then invokes the
Python CLI via ``python3 -m mini_ork.cli.topology ...`` against the same
DB. The tests assert:

  * ``--compute`` — stdout parses into (rho, C, I, telemetry_id) fields;
    telemetry_id shape matches ``pt-...-<uuid6>``; a single
    ``panel_topology_telemetry`` row is inserted with consistent values.

  * ``--backfill`` — walks the distinct panel_run_ids in order and
    persists a row per id.

  * Default ``summary`` — parsed row dicts for the rows + quadrant
    distribution tables (insulates the test from sqlite3 -box
    column-width drift across sqlite3 versions).

Cases (six):

  (1) test_compute_single_trace_fallback
  (2) test_compute_multi_family_three_traces
  (3) test_compute_same_family_zero_I
  (4) test_backfill_persists_rows
  (5) test_summary_recipe_filter
  (6) test_summary_all_recipes_no_filter
"""

from __future__ import annotations

import json
import math
import os
import re
import sqlite3
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))
from mini_ork.cli import topology as mini_ork_topology
from mini_ork.observability import topology_metrics as tm

# Float tolerance for banner/table parsing.
_FLOAT_TOL = 1e-6

# Telemetry id format: 'pt-<panel_run_id[:16]>-<uuid6>'.
_TELEMETRY_ID_RE = re.compile(r"^pt-(.{0,16})-([0-9a-f]{6})$")


# ─────────────────────────────────────────────────────────────────────────────
# Minimal DDL — echo of the columns the port's SQL bodies touch.
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
  status                TEXT    NOT NULL DEFAULT 'success',
  run_id                TEXT
);
"""

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
# Helpers — fixture seeding + CLI invocation + parsed-row comparison.
# ─────────────────────────────────────────────────────────────────────────────

def _init_db(db_path: Path) -> sqlite3.Connection:
    """Make a fresh temp DB with the two tables the port touches."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(db_path))
    con.executescript(_EXEC_TRACES_DDL + _PANEL_TOPOLOGY_TELEMETRY_DDL)
    con.commit()
    return con


def _seed_traces(
    con: sqlite3.Connection,
    traces: list[dict],
) -> None:
    """Insert each trace row. ``trace_id`` must echo ``panel_run_id`` so
    the ``WHERE trace_id LIKE ? OR trace_id LIKE ?`` filter matches."""
    for t in traces:
        con.execute(
            "INSERT INTO execution_traces "
            "(trace_id, agent_version_id, reviewer_verdict, verifier_output, "
            " files_read, tool_calls, task_class, status, run_id) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (
                t["trace_id"],
                t.get("agent_version_id", ""),
                t.get("reviewer_verdict"),
                json.dumps(t.get("verifier_output", {})),
                json.dumps(t.get("files_read", [])),
                json.dumps(t.get("tool_calls", [])),
                t.get("task_class", "code_fix"),
                t.get("status", "success"),
                t.get("run_id"),
            ),
        )
    con.commit()


def _seed_telemetry_rows(
    con: sqlite3.Connection,
    rows: list[dict],
) -> None:
    """Pre-seed ``panel_topology_telemetry`` rows. Used by summary tests."""
    for r in rows:
        con.execute(
            "INSERT INTO panel_topology_telemetry "
            "(telemetry_id, panel_run_id, recipe, rho, context_distance, "
            " inductive_distance, agent_count, n_traces, quadrant, computed_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                r["telemetry_id"],
                r["panel_run_id"],
                r["recipe"],
                float(r["rho"]),
                float(r["context_distance"]),
                float(r["inductive_distance"]),
                int(r["agent_count"]),
                int(r["n_traces"]),
                r["quadrant"],
                r["computed_at"],
            ),
        )
    con.commit()


def _run_python_cli(
    db_path: Path,
    *args: str,
) -> subprocess.CompletedProcess:
    """Invoke the Python port via ``python3 -m``."""
    env = os.environ.copy()
    env["MINI_ORK_DB"] = str(db_path)
    env["MINI_ORK_ROOT"] = str(REPO_ROOT)
    return subprocess.run(
        [sys.executable, "-m", "mini_ork.cli.topology", *args],
        cwd=str(REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
    )


def _parse_compute_output(stdout: str) -> dict:
    """Parse the 8-line ``--compute`` banner into named fields."""
    lines = stdout.splitlines()
    out: dict = {}
    for ln in lines:
        s = ln.strip()
        if s.startswith("panel_run_id:"):
            out["panel_run_id"] = s.split(":", 1)[1].strip()
        elif s.startswith("recipe:"):
            out["recipe"] = s.split(":", 1)[1].strip()
        elif s.startswith("rho:"):
            out["rho"] = float(s.split(":", 1)[1].strip())
        elif s.startswith("C:"):
            out["C"] = float(s.split(":", 1)[1].strip())
        elif s.startswith("I:"):
            out["I"] = float(s.split(":", 1)[1].strip())
        elif s.startswith("telemetry_id="):
            out["telemetry_id"] = s.split("=", 1)[1].strip()
    return out


def _read_telemetry_rows(db_path: Path) -> list[dict]:
    """Read every row in ``panel_topology_telemetry`` keyed for diff."""
    con = sqlite3.connect(str(db_path))
    con.row_factory = sqlite3.Row
    rows = con.execute(
        "SELECT * FROM panel_topology_telemetry ORDER BY panel_run_id, telemetry_id"
    ).fetchall()
    out = [dict(r) for r in rows]
    con.close()
    return out


def _parse_box_rows(stdout: str) -> list[dict]:
    """Parse ``sqlite3 -box`` table output into a list of row dicts."""
    rows: list[dict] = []
    headers: list[str] = []
    for ln in stdout.splitlines():
        s = ln.strip()
        if not s or s.startswith(("┌", "├", "└", "+", "-")):
            continue
        # Detect separators: prefer │ (unicode box-drawing), fall back to |.
        if "│" in s:
            cells = [c.strip() for c in s.strip("│").split("│")]
        elif "|" in s:
            cells = [c.strip() for c in s.strip("|").split("|")]
        else:
            continue
        if not headers:
            headers = cells
            continue
        if len(cells) != len(headers):
            # Not a data row — skip.
            continue
        rows.append(dict(zip(headers, cells)))
    return rows


def _fresh_panel_run_id(label: str) -> str:
    """12-char base + per-label suffix keeps total <=16 so the
    telemetry_id prefix matches panel_run_id[:16] verbatim."""
    return f"p{label}xyz1234"


# ─────────────────────────────────────────────────────────────────────────────
# (1) test_compute_single_trace_fallback
#
# n=1 trace → ρ=C=I=0.0; telemetry_id must match the pt-...-<uuid6> shape.
# ─────────────────────────────────────────────────────────────────────────────

def test_compute_single_trace_fallback(tmp_path: Path) -> None:
    db_path = tmp_path / "compute_single.db"
    panel_run_id = _fresh_panel_run_id("cs")
    recipe = "code_fix_recipe"

    con = _init_db(db_path)
    _seed_traces(con, [
        {"trace_id": f"tr-op-001-{panel_run_id}",
         "agent_version_id": "sonnet-v1",
         "reviewer_verdict": "APPROVE — single agent, no pairwise distance",
         "files_read": ["a.py"], "tool_calls": []},
    ])
    con.close()

    py_proc = _run_python_cli(db_path, "--compute", panel_run_id, recipe)
    assert py_proc.returncode == 0, (
        f"py --compute rc={py_proc.returncode} stderr={py_proc.stderr!r}"
    )

    py_parsed = _parse_compute_output(py_proc.stdout)

    for k in ("rho", "C", "I"):
        assert py_parsed[k] == 0.0, f"single trace must yield 0.0 for {k}"
    assert py_parsed["panel_run_id"] == panel_run_id
    assert py_parsed["recipe"] == recipe
    assert _TELEMETRY_ID_RE.match(py_parsed["telemetry_id"]), (
        f"py telemetry_id shape drift: {py_parsed['telemetry_id']!r}"
    )
    assert py_parsed["telemetry_id"].startswith(f"pt-{panel_run_id[:16]}-")


# ─────────────────────────────────────────────────────────────────────────────
# (2) test_compute_multi_family_three_traces
#
# 3 traces across 2 anthropic + 1 zhipu, overlapping files_read.
# ─────────────────────────────────────────────────────────────────────────────

def test_compute_multi_family_three_traces(tmp_path: Path) -> None:
    db_path = tmp_path / "compute_multi.db"
    panel_run_id = _fresh_panel_run_id("cm")
    recipe = "refactor_audit"

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
         "files_read": ["zzz_glm_only_file.py"],
         "tool_calls": [
             {"tool": "Bash", "input": {"cmd": "ls -la"}},
         ]},
    ])
    con.close()

    py_proc = _run_python_cli(db_path, "--compute", panel_run_id, recipe)
    assert py_proc.returncode == 0, (
        f"py --compute rc={py_proc.returncode} stderr={py_proc.stderr!r}"
    )

    py_parsed = _parse_compute_output(py_proc.stdout)

    # all three verdicts share the same 50-char head → rho = 1.0
    assert math.isclose(py_parsed["rho"], 1.0, abs_tol=_FLOAT_TOL)
    # files overlap partially → 0 < C < 1; 2 anthropic + 1 zhipu → 0 < I < 1
    assert 0.0 < py_parsed["C"] < 1.0
    assert 0.0 < py_parsed["I"] < 1.0

    rows = _read_telemetry_rows(db_path)
    assert len(rows) == 1, f"expected 1 telemetry row, got {len(rows)}: {rows}"
    row = rows[0]
    assert row["telemetry_id"] == py_parsed["telemetry_id"]
    assert row["panel_run_id"] == panel_run_id
    assert row["recipe"] == recipe
    assert row["agent_count"] == 3
    assert row["n_traces"] == 3
    assert math.isclose(row["rho"], py_parsed["rho"], abs_tol=_FLOAT_TOL)
    assert math.isclose(row["context_distance"], py_parsed["C"], abs_tol=_FLOAT_TOL)
    assert math.isclose(row["inductive_distance"], py_parsed["I"], abs_tol=_FLOAT_TOL)
    assert row["quadrant"] == tm.classify_quadrant(
        row["rho"], row["context_distance"], row["inductive_distance"])


# ─────────────────────────────────────────────────────────────────────────────
# (3) test_compute_same_family_zero_I
#
# 3 traces all sonnet-* with identical reviewer_verdict head(50) → ρ=1.0,
# I=0.0, C=0.0.
# ─────────────────────────────────────────────────────────────────────────────

def test_compute_same_family_zero_I(tmp_path: Path) -> None:
    db_path = tmp_path / "compute_same.db"
    panel_run_id = _fresh_panel_run_id("cx")
    recipe = "code_fix_recipe"

    common_head = (
        "approval — code change passes all verifier gates; "
        "continue to merge as planned"
    )

    con = _init_db(db_path)
    _seed_traces(con, [
        {"trace_id": f"tr-op-001-{panel_run_id}",
         "agent_version_id": "sonnet-v1",
         "reviewer_verdict": common_head + " — runner-A",
         "files_read": ["shared.py"], "tool_calls": []},
        {"trace_id": f"tr-op-002-{panel_run_id}",
         "agent_version_id": "sonnet-v2",
         "reviewer_verdict": common_head + " — runner-B",
         "files_read": ["shared.py"], "tool_calls": []},
        {"trace_id": f"tr-op-003-{panel_run_id}",
         "agent_version_id": "sonnet-v3",
         "reviewer_verdict": common_head + " — runner-C",
         "files_read": ["shared.py"], "tool_calls": []},
    ])
    con.close()

    py_proc = _run_python_cli(db_path, "--compute", panel_run_id, recipe)
    assert py_proc.returncode == 0, (
        f"py --compute rc={py_proc.returncode} stderr={py_proc.stderr!r}"
    )

    py_parsed = _parse_compute_output(py_proc.stdout)

    assert math.isclose(py_parsed["rho"], 1.0, abs_tol=_FLOAT_TOL)
    assert math.isclose(py_parsed["I"], 0.0, abs_tol=_FLOAT_TOL)
    assert math.isclose(py_parsed["C"], 0.0, abs_tol=_FLOAT_TOL)


# ─────────────────────────────────────────────────────────────────────────────
# (4) test_backfill_persists_rows
#
# Seed 4 traces from 2 distinct panel_run_ids. The backfill must:
#   * walk the distinct panel_run_ids in order,
#   * persist a panel_topology_telemetry row per id with the recipe
#     resolved from the panel's task_class.
# ─────────────────────────────────────────────────────────────────────────────

def test_backfill_persists_rows(tmp_path: Path) -> None:
    py_db = tmp_path / "py_backfill.db"
    # Two panel_run_ids, two traces each, distinct task_class per panel.
    # We set ``run_id`` on each trace so the backfill SQL takes the
    # ``WHEN run_id IS NOT NULL`` branch (yielding 2 distinct panel_run_ids).
    seeds = [
        ("pbf1xyz1234", [
            {"trace_id": "tr-op-001-pbf1xyz1234",
             "run_id": "pbf1xyz1234",
             "agent_version_id": "sonnet-v1",
             "reviewer_verdict": (
                 "approval — code change passes all verifier gates; "
                 "ship as planned and continue with the next milestone"
             ),
             "files_read": ["a.py"], "tool_calls": []},
            {"trace_id": "tr-op-002-pbf1xyz1234",
             "run_id": "pbf1xyz1234",
             "agent_version_id": "opus-v3",
             "reviewer_verdict": (
                 "approval — code change passes all verifier gates; "
                 "reviewer concurs with the merge decision as documented"
             ),
             "files_read": ["b.py"], "tool_calls": []},
        ]),
        ("pbf2xyz1234", [
            {"trace_id": "tr-op-001-pbf2xyz1234",
             "run_id": "pbf2xyz1234",
             "agent_version_id": "kimi-v1",
             "reviewer_verdict": (
                 "rejection — verifier failed the lint gate; please "
                 "rerun after fixing the import order issue"
             ),
             "files_read": ["c.py"], "tool_calls": []},
            {"trace_id": "tr-op-002-pbf2xyz1234",
             "run_id": "pbf2xyz1234",
             "agent_version_id": "glm-v2",
             "reviewer_verdict": (
                 "rejection — verifier failed the type-check gate; "
                 "please rerun after fixing the annotation drift"
             ),
             "files_read": ["d.py"], "tool_calls": []},
        ]),
    ]

    con = _init_db(py_db)
    for panel_run_id, traces in seeds:
        # Update task_class per panel so the recipe lookup
        # (task_class of the first trace) is stable.
        tag = "code_fix" if panel_run_id == "pbf1xyz1234" else "refactor_audit"
        for t in traces:
            t["task_class"] = tag
        _seed_traces(con, traces)
    con.close()

    # Python backfill (subprocess so we hit the CLI surface)
    py_proc = _run_python_cli(py_db, "--backfill")
    assert py_proc.returncode == 0, (
        f"py --backfill rc={py_proc.returncode} stderr={py_proc.stderr!r}"
    )

    # The walk covers both panel_run_ids in sorted order.
    py_ids = re.findall(
        r"^\s+([\w-]+)\s+→", py_proc.stdout, flags=re.MULTILINE
    )
    assert py_ids == ["pbf1xyz1234", "pbf2xyz1234"], (
        f"backfill walk drift: {py_ids}"
    )

    py_rows = _read_telemetry_rows(py_db)
    assert len(py_rows) == 2, f"py rows: {py_rows}"

    by_prid = {r["panel_run_id"]: r for r in py_rows}
    assert set(by_prid) == {"pbf1xyz1234", "pbf2xyz1234"}
    for prid, row in by_prid.items():
        expected_recipe = "code_fix" if prid == "pbf1xyz1234" else "refactor_audit"
        assert row["recipe"] == expected_recipe
        assert row["agent_count"] == 2
        assert row["n_traces"] == 2
        assert _TELEMETRY_ID_RE.match(row["telemetry_id"])
        assert row["quadrant"] == tm.classify_quadrant(
            row["rho"], row["context_distance"], row["inductive_distance"])


# ─────────────────────────────────────────────────────────────────────────────
# (5) test_summary_recipe_filter
#
# Pre-seed 6 telemetry rows (3 recipes × 2 each). ``--recipe code_fix``
# lists only the 2 code_fix rows with the seeded values.
# ─────────────────────────────────────────────────────────────────────────────

def _seed_summary_rows(con, tag: str) -> list[dict]:
    seed_rows = [
        # recipe=code_fix — two rows
        {"telemetry_id": f"pt-cf{tag}aaaaaaaa-aa0001",
         "panel_run_id": f"cf{tag}aaaaaaaa",
         "recipe": "code_fix",
         "rho": 0.6, "context_distance": 0.4, "inductive_distance": 0.2,
         "agent_count": 2, "n_traces": 3,
         "quadrant": "convergent_corroboration",
         "computed_at": "2026-07-05T10:00:00.000Z"},
        {"telemetry_id": f"pt-cf{tag}aaaaaaaa-aa0002",
         "panel_run_id": f"cf{tag}aaaaaaaa",
         "recipe": "code_fix",
         "rho": 0.7, "context_distance": 0.5, "inductive_distance": 0.3,
         "agent_count": 2, "n_traces": 3,
         "quadrant": "convergent_corroboration",
         "computed_at": "2026-07-05T11:00:00.000Z"},
        # recipe=refactor_audit — two rows
        {"telemetry_id": f"pt-ra{tag}aaaaaaaa-aa0001",
         "panel_run_id": f"ra{tag}aaaaaaaa",
         "recipe": "refactor_audit",
         "rho": 0.2, "context_distance": 0.1, "inductive_distance": 0.6,
         "agent_count": 3, "n_traces": 4,
         "quadrant": "prior_driven_disagreement",
         "computed_at": "2026-07-05T09:00:00.000Z"},
        {"telemetry_id": f"pt-ra{tag}aaaaaaaa-aa0002",
         "panel_run_id": f"ra{tag}aaaaaaaa",
         "recipe": "refactor_audit",
         "rho": 0.3, "context_distance": 0.2, "inductive_distance": 0.7,
         "agent_count": 3, "n_traces": 4,
         "quadrant": "prior_driven_disagreement",
         "computed_at": "2026-07-05T08:00:00.000Z"},
        # recipe=spec_synthesis — two rows
        {"telemetry_id": f"pt-ss{tag}aaaaaaaa-aa0001",
         "panel_run_id": f"ss{tag}aaaaaaaa",
         "recipe": "spec_synthesis",
         "rho": 0.9, "context_distance": 0.1, "inductive_distance": 0.1,
         "agent_count": 2, "n_traces": 2,
         "quadrant": "coalition",
         "computed_at": "2026-07-05T07:00:00.000Z"},
        {"telemetry_id": f"pt-ss{tag}aaaaaaaa-aa0002",
         "panel_run_id": f"ss{tag}aaaaaaaa",
         "recipe": "spec_synthesis",
         "rho": 0.95, "context_distance": 0.15, "inductive_distance": 0.05,
         "agent_count": 2, "n_traces": 2,
         "quadrant": "coalition",
         "computed_at": "2026-07-05T06:00:00.000Z"},
    ]
    _seed_telemetry_rows(con, seed_rows)
    return seed_rows


def test_summary_recipe_filter(tmp_path: Path) -> None:
    db_path = tmp_path / "summary_recipe.db"

    con = _init_db(db_path)
    _seed_summary_rows(con, "1")
    con.close()

    py_proc = _run_python_cli(db_path, "--recipe", "code_fix")
    assert py_proc.returncode == 0, (
        f"py --recipe rc={py_proc.returncode} stderr={py_proc.stderr!r}"
    )

    py_rows = _parse_box_rows(py_proc.stdout)

    # Only the 2 code_fix rows surface, with the seeded values.
    data = [r for r in py_rows if r.get("recipe") == "code_fix"
            and "rho" in r]
    assert len(data) == 2, f"expected 2 code_fix rows: {py_rows}"
    got = sorted(float(r["rho"]) for r in data)
    assert got == [0.6, 0.7]
    for r in data:
        assert r["quadrant"] == "convergent_corroboration"
        assert r["agent_count"] == "2"
        assert r["n_traces"] == "3"


# ─────────────────────────────────────────────────────────────────────────────
# (6) test_summary_all_recipes_no_filter
#
# Same seed as (5) but with no --recipe flag → all 6 rows.
# ─────────────────────────────────────────────────────────────────────────────

def test_summary_all_recipes_no_filter(tmp_path: Path) -> None:
    db_path = tmp_path / "summary_all.db"

    con = _init_db(db_path)
    _seed_summary_rows(con, "2")
    con.close()

    # Default subcommand = summary, no --recipe.
    py_proc = _run_python_cli(db_path)
    assert py_proc.returncode == 0, (
        f"py summary rc={py_proc.returncode} stderr={py_proc.stderr!r}"
    )

    py_rows = _parse_box_rows(py_proc.stdout)

    # All 6 rows surface (rows with a rho cell).
    data = [r for r in py_rows if r.get("recipe") and "rho" in r]
    assert len(data) == 6, f"expected 6 rows: {py_rows}"
    recipes = sorted(r["recipe"] for r in data)
    assert recipes == ["code_fix", "code_fix", "refactor_audit",
                       "refactor_audit", "spec_synthesis", "spec_synthesis"]


def test_python_import_smoke(tmp_path, monkeypatch) -> None:
    """The Python port must be importable from REPO_ROOT — catches
    ModuleNotFoundError / SyntaxError without paying the cost of a full
    subprocess invocation."""
    # main([]) resolves the default db at $MINI_ORK_HOME/state.db — point it
    # at a tmp home or cmd_summary's ensure_table leaks a partial state.db
    # into the suite's cwd.
    monkeypatch.setenv("MINI_ORK_HOME", str(tmp_path))
    monkeypatch.delenv("MINI_ORK_DB", raising=False)
    assert hasattr(mini_ork_topology, "cmd_summary")
    assert hasattr(mini_ork_topology, "cmd_compute")
    assert hasattr(mini_ork_topology, "cmd_backfill")
    assert hasattr(mini_ork_topology, "main")
    # ``main([])`` should return 0 even with no DB rows.
    rc = mini_ork_topology.main([])
    assert rc == 0
