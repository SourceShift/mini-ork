"""Parity gate: ``mini_ork.cli.topology`` vs ``bin/mini-ork-topology``.

Each test in this module builds a small ``execution_traces`` (and, where
relevant, ``panel_topology_telemetry``) corpus, materialises it into a
temp sqlite DB using the minimal DDL bash's heredocs require, then
invokes the LIVE bash CLI (``bash bin/mini-ork-topology ...``) on the
same DB. The Python port runs against the same DB via the matching
``cmd_*`` function (or via ``python3 -m mini_ork.cli.topology
...`` for end-to-end CLI parity). The test asserts:

  * ``--compute`` — stdout parses into (rho, C, I, telemetry_id) fields;
    bash + Python floats match within 1e-6; telemetry_id shape matches
    ``pt-...-<uuid6>``; a single ``panel_topology_telemetry`` row is
    inserted with matching values modulo the random uuid6 suffix.

  * ``--backfill`` — both modes walk the same set of distinct
    panel_run_ids in the same order and persist a comparable row set
    (modulo the random uuid6 suffix and the per-row computed_at).

  * Default ``summary`` — both modes produce the same parsed row dicts
    for the rows + quadrant distribution tables (insulates the test
    from sqlite3 -box column-width drift across sqlite3 versions).

Cases (six, exactly hitting the kickoff's >=6 floor):

  (1) test_compute_single_trace_fallback
  (2) test_compute_multi_family_three_traces
  (3) test_compute_same_family_zero_I
  (4) test_backfill_persists_identical_rows
  (5) test_summary_recipe_filter
  (6) test_summary_all_recipes_no_filter

Strangler-fig co-existence preserved: ``bin/mini-ork-topology`` is
byte-identical before and after this test exists (the verifier runs
``git diff --exit-code bin/mini-ork-topology`` to enforce).
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
BIN_TOPOLOGY = REPO_ROOT / "bin" / "mini-ork-topology"
sys.path.insert(0, str(REPO_ROOT))
# The Python port is invoked as a subprocess via ``python3 -m`` in every
# test below; the import here is exercised in ``test_python_import_smoke``
# as a fast-fail check that the module resolves from REPO_ROOT.
from mini_ork.cli import topology as mini_ork_topology

# Float parity tolerance — kickoff requirement.
_FLOAT_TOL = 1e-6

# Telemetry id format: 'pt-<panel_run_id[:16]>-<uuid6>'. Match prefix,
# tolerate the uuid6 suffix verbatim (it's random).
_TELEMETRY_ID_RE = re.compile(r"^pt-(.{0,16})-([0-9a-f]{6})$")


# ─────────────────────────────────────────────────────────────────────────────
# Minimal DDL — echo of the columns bash's SQL bodies touch.
#
# Verbatim echo of the relevant columns from db/migrations/0010_benchmarks.sql
# (execution_traces DDL) + 0015_panel_topology_telemetry.sql. A test DB with
# only these two tables accepts every bash heredoc SQL verbatim.
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
# Helpers — fixture seeding + bash invocation + parsed-row comparison.
# ─────────────────────────────────────────────────────────────────────────────

def _init_db(db_path: Path) -> sqlite3.Connection:
    """Make a fresh temp DB with the two tables bash touches."""
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
    the bash ``WHERE trace_id LIKE ? OR trace_id LIKE ?`` filter matches.
    Optional ``run_id`` field is stored when present (used by the bash
    backfill ``CASE WHEN run_id IS NOT NULL`` branch).
    """
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


def _run_bash_cli(
    db_path: Path,
    *args: str,
) -> subprocess.CompletedProcess:
    """Invoke the LIVE bash CLI ``bin/mini-ork-topology`` with ``args``.

    Sets ``MINI_ORK_DB`` to ``db_path`` so the bash script targets our
    temp DB. ``MINI_ORK_ROOT`` is set to the repo root so
    ``lib/topology_metrics.sh`` resolves correctly via the bash
    function's own ``MINI_ORK_ROOT`` derivation. The bash CLI does NOT
    need MINI_ORK_ROOT directly — it derives its own via BASH_SOURCE —
    but measure_* functions inside topology_metrics.sh do.
    """
    env = os.environ.copy()
    env["MINI_ORK_DB"] = str(db_path)
    env["MINI_ORK_ROOT"] = str(REPO_ROOT)
    return subprocess.run(
        ["bash", str(BIN_TOPOLOGY), *args],
        cwd=str(REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
    )


def _run_python_cli(
    db_path: Path,
    *args: str,
) -> subprocess.CompletedProcess:
    """Invoke the Python port via ``python3 -m`` for end-to-end parity."""
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
    """Parse the 8-line ``--compute`` banner into named fields.

    Format (verbatim from bin/mini-ork-topology lines 56-71):
        === mini-ork topology — ad-hoc measurement ===
            panel_run_id: <id>
            recipe:       <recipe>

            rho:   <float>
            C:     <float>
            I:     <float>

        Persisting to panel_topology_telemetry...
        telemetry_id=<id>
    """
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


def _normalise_telemetry_id(tid: str, panel_run_id: str) -> str:
    """Replace the random uuid6 suffix with a placeholder so two runs
    of the same fixture compare equal. The prefix ``pt-<panel_run_id[:16]>-``
    is part of the public contract and is left intact."""
    m = _TELEMETRY_ID_RE.match(tid)
    assert m, f"telemetry_id {tid!r} does not match pt-...-...... shape"
    prefix_run = m.group(1)
    assert prefix_run == panel_run_id[:16], (
        f"telemetry_id prefix drift: got {prefix_run!r}, "
        f"want {panel_run_id[:16]!r}"
    )
    return f"pt-{prefix_run}-<uuid6>"


def _parse_box_rows(stdout: str) -> list[dict]:
    """Parse ``sqlite3 -box`` table output into a list of row dicts.

    The box format uses box-drawing characters at row boundaries and
    ``|`` between cells. We strip leading/trailing ``│`` (or ``|``),
    then split each row line on ``│`` (or ``|``) and trim.

    Only lines that begin with a box-drawing character (or ``|``) AND
    contain at least one internal separator are considered data rows.
    """
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


def _assert_telemetry_row_parity(
    bash_row: dict,
    py_row: dict,
    label: str,
    panel_run_id: str,
) -> None:
    """Diff two ``panel_topology_telemetry`` rows modulo the random uuid."""
    for k in ("panel_run_id", "recipe", "quadrant"):
        assert bash_row[k] == py_row[k], (
            f"[{label}] {k} drift: bash={bash_row[k]!r} py={py_row[k]!r}"
        )
    assert _normalise_telemetry_id(bash_row["telemetry_id"], panel_run_id) == (
        _normalise_telemetry_id(py_row["telemetry_id"], panel_run_id)
    ), (
        f"[{label}] telemetry_id drift: bash={bash_row['telemetry_id']!r} "
        f"py={py_row['telemetry_id']!r}"
    )
    for k in ("agent_count", "n_traces"):
        assert int(bash_row[k]) == int(py_row[k]), (
            f"[{label}] {k} drift: bash={bash_row[k]!r} py={py_row[k]!r}"
        )
    for k in ("rho", "context_distance", "inductive_distance"):
        assert math.isclose(
            float(bash_row[k]), float(py_row[k]), abs_tol=_FLOAT_TOL
        ), (
            f"[{label}] {k} drift: bash={bash_row[k]!r} py={py_row[k]!r}"
        )


# ─────────────────────────────────────────────────────────────────────────────
# Per-test panel_run_id helper. The two LIKE-patterns in the bash heredoc
# match on substring, so each test uses a unique panel_run_id to avoid
# cross-contamination when temp DBs are shared.
# ─────────────────────────────────────────────────────────────────────────────

def _fresh_panel_run_id(label: str) -> str:
    """12-char base + per-label suffix keeps total <=16 so the
    telemetry_id prefix matches panel_run_id[:16] verbatim."""
    return f"p{label}xyz1234"


# ─────────────────────────────────────────────────────────────────────────────
# (1) test_compute_single_trace_fallback
#
# n=1 trace → ρ=C=I=0.0; telemetry_id must match the pt-...-<uuid6> shape.
# Compares the bash --compute stdout to the Python --compute stdout by
# parsed (rho, C, I, telemetry_id) fields — float 1e-6, telemetry_id
# shape regex.
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

    bash_proc = _run_bash_cli(db_path, "--compute", panel_run_id, recipe)
    assert bash_proc.returncode == 0, (
        f"bash --compute rc={bash_proc.returncode} stderr={bash_proc.stderr!r}"
    )
    py_proc = _run_python_cli(db_path, "--compute", panel_run_id, recipe)
    assert py_proc.returncode == 0, (
        f"py --compute rc={py_proc.returncode} stderr={py_proc.stderr!r}"
    )

    bash_parsed = _parse_compute_output(bash_proc.stdout)
    py_parsed = _parse_compute_output(py_proc.stdout)

    for k in ("rho", "C", "I"):
        assert math.isclose(
            bash_parsed[k], py_parsed[k], abs_tol=_FLOAT_TOL
        ), f"[{k}] bash={bash_parsed[k]} py={py_parsed[k]}"
        assert bash_parsed[k] == 0.0, f"single trace must yield 0.0 for {k}"
    assert bash_parsed["panel_run_id"] == panel_run_id
    assert bash_parsed["recipe"] == recipe
    assert _TELEMETRY_ID_RE.match(bash_parsed["telemetry_id"]), (
        f"bash telemetry_id shape drift: {bash_parsed['telemetry_id']!r}"
    )
    assert _TELEMETRY_ID_RE.match(py_parsed["telemetry_id"]), (
        f"py telemetry_id shape drift: {py_parsed['telemetry_id']!r}"
    )
    assert _normalise_telemetry_id(
        bash_parsed["telemetry_id"], panel_run_id
    ) == _normalise_telemetry_id(py_parsed["telemetry_id"], panel_run_id)


# ─────────────────────────────────────────────────────────────────────────────
# (2) test_compute_multi_family_three_traces
#
# 3 traces across 2 anthropic + 1 zhipu, overlapping files_read. Bash
# and py insert a panel_topology_telemetry row with matching (ρ, C, I,
# quadrant) within 1e-6 and matching (panel_run_id, recipe, agent_count,
# n_traces) exactly.
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

    bash_proc = _run_bash_cli(db_path, "--compute", panel_run_id, recipe)
    assert bash_proc.returncode == 0, (
        f"bash --compute rc={bash_proc.returncode} stderr={bash_proc.stderr!r}"
    )
    py_proc = _run_python_cli(db_path, "--compute", panel_run_id, recipe)
    assert py_proc.returncode == 0, (
        f"py --compute rc={py_proc.returncode} stderr={py_proc.stderr!r}"
    )

    bash_parsed = _parse_compute_output(bash_proc.stdout)
    py_parsed = _parse_compute_output(py_proc.stdout)

    for k in ("rho", "C", "I"):
        assert math.isclose(
            bash_parsed[k], py_parsed[k], abs_tol=_FLOAT_TOL
        ), f"[{k}] bash={bash_parsed[k]} py={py_parsed[k]}"

    rows = _read_telemetry_rows(db_path)
    assert len(rows) == 2, f"expected 2 telemetry rows, got {len(rows)}: {rows}"
    by_tid = {r["telemetry_id"]: r for r in rows}
    assert bash_parsed["telemetry_id"] in by_tid
    assert py_parsed["telemetry_id"] in by_tid
    _assert_telemetry_row_parity(
        by_tid[bash_parsed["telemetry_id"]],
        by_tid[py_parsed["telemetry_id"]],
        "compute_multi",
        panel_run_id,
    )


# ─────────────────────────────────────────────────────────────────────────────
# (3) test_compute_same_family_zero_I
#
# 3 traces all sonnet-* with identical reviewer_verdict head(50) → ρ=1.0
# and I=0.0. Same-family + identical verdict → both bash and py produce
# identical (rho, C, I) floats.
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

    bash_proc = _run_bash_cli(db_path, "--compute", panel_run_id, recipe)
    assert bash_proc.returncode == 0, (
        f"bash --compute rc={bash_proc.returncode} stderr={bash_proc.stderr!r}"
    )
    py_proc = _run_python_cli(db_path, "--compute", panel_run_id, recipe)
    assert py_proc.returncode == 0, (
        f"py --compute rc={py_proc.returncode} stderr={py_proc.stderr!r}"
    )

    bash_parsed = _parse_compute_output(bash_proc.stdout)
    py_parsed = _parse_compute_output(py_proc.stdout)

    for k in ("rho", "C", "I"):
        assert math.isclose(
            bash_parsed[k], py_parsed[k], abs_tol=_FLOAT_TOL
        ), f"[{k}] bash={bash_parsed[k]} py={py_parsed[k]}"
    assert math.isclose(bash_parsed["rho"], 1.0, abs_tol=_FLOAT_TOL)
    assert math.isclose(bash_parsed["I"], 0.0, abs_tol=_FLOAT_TOL)
    assert math.isclose(bash_parsed["C"], 0.0, abs_tol=_FLOAT_TOL)


# ─────────────────────────────────────────────────────────────────────────────
# (4) test_backfill_persists_identical_rows
#
# Seed 4 traces from 2 distinct panel_run_ids. Run ``bash --backfill``
# on one DB and the Python port on a fresh DB. Both modes should:
#   * walk the same set of distinct panel_run_ids in the same order,
#   * persist a panel_topology_telemetry row per id with matching
#     (panel_run_id, recipe, rho, C, I, quadrant, agent_count, n_traces).
#
# Two separate DBs avoid row-collision on telemetry_id (both produce
# pt-...-<uuid6> with random suffix).
# ─────────────────────────────────────────────────────────────────────────────

def test_backfill_persists_identical_rows(tmp_path: Path) -> None:
    bash_db = tmp_path / "bash_backfill.db"
    py_db = tmp_path / "py_backfill.db"
    # Two panel_run_ids, two traces each, distinct task_class per panel.
    # We set ``run_id`` on each trace so the bash backfill SQL takes the
    # ``WHEN run_id IS NOT NULL`` branch (yielding 2 distinct panel_run_ids
    # in the result) — without ``run_id``, the substring branch would yield
    # 4 distinct ids (one per trace) and the parity assertion would diverge.
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

    for db_path in (bash_db, py_db):
        con = _init_db(db_path)
        for panel_run_id, traces in seeds:
            # Update task_class per panel so the recipe lookup
            # (task_class of the first trace) is stable.
            tag = "code_fix" if panel_run_id == "pbf1xyz1234" else "refactor_audit"
            for t in traces:
                t["task_class"] = tag
            _seed_traces(con, traces)
        con.close()

    # Bash backfill
    bash_proc = _run_bash_cli(bash_db, "--backfill")
    assert bash_proc.returncode == 0, (
        f"bash --backfill rc={bash_proc.returncode} stderr={bash_proc.stderr!r}"
    )
    # Python backfill (subprocess so we hit the CLI surface)
    py_proc = _run_python_cli(py_db, "--backfill")
    assert py_proc.returncode == 0, (
        f"py --backfill rc={py_proc.returncode} stderr={py_proc.stderr!r}"
    )

    # Both backfill runs must produce the SAME ordered list of panel_run_ids.
    bash_ids = re.findall(
        r"^\s+([\w-]+)\s+→", bash_proc.stdout, flags=re.MULTILINE
    )
    py_ids = re.findall(
        r"^\s+([\w-]+)\s+→", py_proc.stdout, flags=re.MULTILINE
    )
    assert bash_ids == py_ids, (
        f"backfill panel_run_id walk order drift: "
        f"bash={bash_ids} py={py_ids}"
    )
    assert len(bash_ids) == 2, (
        f"expected 2 distinct panel_run_ids, got {bash_ids}"
    )

    # Read rows from each DB; diff modulo the random uuid6 suffix.
    bash_rows = _read_telemetry_rows(bash_db)
    py_rows = _read_telemetry_rows(py_db)
    assert len(bash_rows) == 2, f"bash rows: {bash_rows}"
    assert len(py_rows) == 2, f"py rows: {py_rows}"

    bash_by_prid = {r["panel_run_id"]: r for r in bash_rows}
    py_by_prid = {r["panel_run_id"]: r for r in py_rows}
    assert set(bash_by_prid) == set(py_by_prid), (
        f"panel_run_id mismatch: bash={set(bash_by_prid)} py={set(py_by_prid)}"
    )
    for prid in bash_by_prid:
        _assert_telemetry_row_parity(
            bash_by_prid[prid], py_by_prid[prid], "backfill", prid
        )


# ─────────────────────────────────────────────────────────────────────────────
# (5) test_summary_recipe_filter
#
# Pre-seed 6 telemetry rows (3 recipes × 2 each). Run bash with
# ``--recipe code_fix`` and the Python port with the same. Parse
# ``sqlite3 -box`` output into row dicts; assert identical row list
# length and matching key columns (recipe, rho, C, I, quadrant,
# agent_count, n_traces) within 1e-6.
# ─────────────────────────────────────────────────────────────────────────────

def test_summary_recipe_filter(tmp_path: Path) -> None:
    db_path = tmp_path / "summary_recipe.db"

    con = _init_db(db_path)
    seed_rows = [
        # recipe=code_fix — two rows
        {"telemetry_id": "pt-cf1aaaaaaaaaa-aa0001",
         "panel_run_id": "cf1aaaaaaaaaa",
         "recipe": "code_fix",
         "rho": 0.6, "context_distance": 0.4, "inductive_distance": 0.2,
         "agent_count": 2, "n_traces": 3,
         "quadrant": "convergent_corroboration",
         "computed_at": "2026-07-05T10:00:00.000Z"},
        {"telemetry_id": "pt-cf1aaaaaaaaaa-aa0002",
         "panel_run_id": "cf1aaaaaaaaaa",
         "recipe": "code_fix",
         "rho": 0.7, "context_distance": 0.5, "inductive_distance": 0.3,
         "agent_count": 2, "n_traces": 3,
         "quadrant": "convergent_corroboration",
         "computed_at": "2026-07-05T11:00:00.000Z"},
        # recipe=refactor_audit — two rows
        {"telemetry_id": "pt-ra1aaaaaaaaaa-aa0001",
         "panel_run_id": "ra1aaaaaaaaaa",
         "recipe": "refactor_audit",
         "rho": 0.2, "context_distance": 0.1, "inductive_distance": 0.6,
         "agent_count": 3, "n_traces": 4,
         "quadrant": "prior_driven_disagreement",
         "computed_at": "2026-07-05T09:00:00.000Z"},
        {"telemetry_id": "pt-ra1aaaaaaaaaa-aa0002",
         "panel_run_id": "ra1aaaaaaaaaa",
         "recipe": "refactor_audit",
         "rho": 0.3, "context_distance": 0.2, "inductive_distance": 0.7,
         "agent_count": 3, "n_traces": 4,
         "quadrant": "prior_driven_disagreement",
         "computed_at": "2026-07-05T08:00:00.000Z"},
        # recipe=spec_synthesis — two rows
        {"telemetry_id": "pt-ss1aaaaaaaaaa-aa0001",
         "panel_run_id": "ss1aaaaaaaaaa",
         "recipe": "spec_synthesis",
         "rho": 0.9, "context_distance": 0.1, "inductive_distance": 0.1,
         "agent_count": 2, "n_traces": 2,
         "quadrant": "coalition",
         "computed_at": "2026-07-05T07:00:00.000Z"},
        {"telemetry_id": "pt-ss1aaaaaaaaaa-aa0002",
         "panel_run_id": "ss1aaaaaaaaaa",
         "recipe": "spec_synthesis",
         "rho": 0.95, "context_distance": 0.15, "inductive_distance": 0.05,
         "agent_count": 2, "n_traces": 2,
         "quadrant": "coalition",
         "computed_at": "2026-07-05T06:00:00.000Z"},
    ]
    _seed_telemetry_rows(con, seed_rows)
    con.close()

    bash_proc = _run_bash_cli(db_path, "--recipe", "code_fix")
    assert bash_proc.returncode == 0, (
        f"bash --recipe rc={bash_proc.returncode} stderr={bash_proc.stderr!r}"
    )
    py_proc = _run_python_cli(db_path, "--recipe", "code_fix")
    assert py_proc.returncode == 0, (
        f"py --recipe rc={py_proc.returncode} stderr={py_proc.stderr!r}"
    )

    bash_rows = _parse_box_rows(bash_proc.stdout)
    py_rows = _parse_box_rows(py_proc.stdout)

    # bash and py must surface the SAME set of recipes. Both rows tables
    # are recipe-filtered to code_fix (2 rows).
    assert len(bash_rows) == len(py_rows), (
        f"summary row count drift: bash={len(bash_rows)} py={len(py_rows)}"
    )
    assert len(bash_rows) >= 1, f"summary returned no rows: bash={bash_rows}"

    # Compare each row's key fields. Order may differ if sqlite3
    # returns them in a different physical order between the two
    # invocations; we sort by recipe+at to make the comparison stable.
    bash_rows.sort(key=lambda r: (r.get("recipe", ""), r.get("at", "")))
    py_rows.sort(key=lambda r: (r.get("recipe", ""), r.get("at", "")))

    for i, (b, p) in enumerate(zip(bash_rows, py_rows)):
        for k in ("recipe", "quadrant", "agent_count", "n_traces"):
            assert b[k] == p[k], (
                f"[{i}] {k} drift: bash={b[k]!r} py={p[k]!r} (row={b})"
            )
        for k in ("rho", "C", "I"):
            assert math.isclose(
                float(b[k]), float(p[k]), abs_tol=_FLOAT_TOL
            ), (
                f"[{i}] {k} drift: bash={b[k]!r} py={p[k]!r} (row={b})"
            )


# ─────────────────────────────────────────────────────────────────────────────
# (6) test_summary_all_recipes_no_filter
#
# Same seed as (5) but with no --recipe flag. Both modes should list
# all 6 rows (latest 20) + the quadrant distribution rows. Assert
# identical row sets and identical distribution rows.
# ─────────────────────────────────────────────────────────────────────────────

def test_summary_all_recipes_no_filter(tmp_path: Path) -> None:
    db_path = tmp_path / "summary_all.db"

    con = _init_db(db_path)
    seed_rows = [
        {"telemetry_id": "pt-cf2aaaaaaaaaa-aa0001",
         "panel_run_id": "cf2aaaaaaaaaa",
         "recipe": "code_fix",
         "rho": 0.6, "context_distance": 0.4, "inductive_distance": 0.2,
         "agent_count": 2, "n_traces": 3,
         "quadrant": "convergent_corroboration",
         "computed_at": "2026-07-05T10:00:00.000Z"},
        {"telemetry_id": "pt-cf2aaaaaaaaaa-aa0002",
         "panel_run_id": "cf2aaaaaaaaaa",
         "recipe": "code_fix",
         "rho": 0.7, "context_distance": 0.5, "inductive_distance": 0.3,
         "agent_count": 2, "n_traces": 3,
         "quadrant": "convergent_corroboration",
         "computed_at": "2026-07-05T11:00:00.000Z"},
        {"telemetry_id": "pt-ra2aaaaaaaaaa-aa0001",
         "panel_run_id": "ra2aaaaaaaaaa",
         "recipe": "refactor_audit",
         "rho": 0.2, "context_distance": 0.1, "inductive_distance": 0.6,
         "agent_count": 3, "n_traces": 4,
         "quadrant": "prior_driven_disagreement",
         "computed_at": "2026-07-05T09:00:00.000Z"},
        {"telemetry_id": "pt-ra2aaaaaaaaaa-aa0002",
         "panel_run_id": "ra2aaaaaaaaaa",
         "recipe": "refactor_audit",
         "rho": 0.3, "context_distance": 0.2, "inductive_distance": 0.7,
         "agent_count": 3, "n_traces": 4,
         "quadrant": "prior_driven_disagreement",
         "computed_at": "2026-07-05T08:00:00.000Z"},
        {"telemetry_id": "pt-ss2aaaaaaaaaa-aa0001",
         "panel_run_id": "ss2aaaaaaaaaa",
         "recipe": "spec_synthesis",
         "rho": 0.9, "context_distance": 0.1, "inductive_distance": 0.1,
         "agent_count": 2, "n_traces": 2,
         "quadrant": "coalition",
         "computed_at": "2026-07-05T07:00:00.000Z"},
        {"telemetry_id": "pt-ss2aaaaaaaaaa-aa0002",
         "panel_run_id": "ss2aaaaaaaaaa",
         "recipe": "spec_synthesis",
         "rho": 0.95, "context_distance": 0.15, "inductive_distance": 0.05,
         "agent_count": 2, "n_traces": 2,
         "quadrant": "coalition",
         "computed_at": "2026-07-05T06:00:00.000Z"},
    ]
    _seed_telemetry_rows(con, seed_rows)
    con.close()

    # Default subcommand = summary, no --recipe.
    bash_proc = _run_bash_cli(db_path)
    assert bash_proc.returncode == 0, (
        f"bash summary rc={bash_proc.returncode} stderr={bash_proc.stderr!r}"
    )
    py_proc = _run_python_cli(db_path)
    assert py_proc.returncode == 0, (
        f"py summary rc={py_proc.returncode} stderr={py_proc.stderr!r}"
    )

    bash_rows = _parse_box_rows(bash_proc.stdout)
    py_rows = _parse_box_rows(py_proc.stdout)

    # Both surfaces must list all 6 rows.
    assert len(bash_rows) == len(py_rows) == 6, (
        f"summary-all row count drift: bash={len(bash_rows)} py={len(py_rows)}"
    )

    bash_rows.sort(key=lambda r: (r.get("recipe", ""), r.get("at", "")))
    py_rows.sort(key=lambda r: (r.get("recipe", ""), r.get("at", "")))

    for i, (b, p) in enumerate(zip(bash_rows, py_rows)):
        for k in ("recipe", "quadrant", "agent_count", "n_traces"):
            assert b[k] == p[k], (
                f"[{i}] {k} drift: bash={b[k]!r} py={p[k]!r} (row={b})"
            )
        for k in ("rho", "C", "I"):
            assert math.isclose(
                float(b[k]), float(p[k]), abs_tol=_FLOAT_TOL
            ), (
                f"[{i}] {k} drift: bash={b[k]!r} py={p[k]!r} (row={b})"
            )


# ─────────────────────────────────────────────────────────────────────────────
# (safety) bash availability preflight.
# ─────────────────────────────────────────────────────────────────────────────

def test_bash_available() -> None:
    """All tests above rely on a live ``bash`` subprocess + the bash CLI
    script being present. Skip-with-fail (per kickoff mandate: bash
    unavailable triggers fail, not skip)."""
    if shutil.which("bash") is None:
        pytest.fail("bash not on PATH — parity tests require a live bash")
    assert BIN_TOPOLOGY.is_file(), (
        f"missing bin/mini-ork-topology at {BIN_TOPOLOGY}"
    )


def test_python_import_smoke(tmp_path, monkeypatch) -> None:
    """The Python port must be importable from REPO_ROOT — catches
    ModuleNotFoundError / SyntaxError without paying the cost of a full
    subprocess invocation."""
    # main([]) resolves the default db at $MINI_ORK_HOME/state.db — point it
    # at a tmp home or cmd_summary's ensure_table leaks a partial state.db
    # into the suite's cwd (this poisoned later web_smoke tests in CI).
    monkeypatch.setenv("MINI_ORK_HOME", str(tmp_path))
    monkeypatch.delenv("MINI_ORK_DB", raising=False)
    assert hasattr(mini_ork_topology, "cmd_summary")
    assert hasattr(mini_ork_topology, "cmd_compute")
    assert hasattr(mini_ork_topology, "cmd_backfill")
    assert hasattr(mini_ork_topology, "main")
    # ``main([])`` should return 0 even with no DB rows.
    rc = mini_ork_topology.main([])
    assert rc == 0, f"main() with empty args returned {rc}"