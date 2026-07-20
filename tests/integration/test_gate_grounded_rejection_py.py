"""tests/integration/test_gate_grounded_rejection_py.py

Python port of ``tests/integration/test_gate_grounded_rejection.sh``.

That bash script sources all 6 gate libs (``coalition_gate.sh``,
``krippendorff_alpha_gate.sh``, ``citation_verifier_mechanical.sh``,
``refute_or_promote_gate.sh``, ``honest_ci_gate.sh``, ``gates_common.sh``)
into one shell and drives each gate's hard-block + pass path, asserting the
shared ``mo_grounded_rejection`` side-effect: exactly one row lands in
``grounded_rejections`` on a hard-block verdict, and zero rows land on a
pass/indeterminate verdict.

Wiring note (read before extending this file): none of the 5 ported gate
functions (``coalition_gate.check_panel_coalition``,
``krippendorff_alpha_gate.check_panel_alpha``,
``citation_verifier_mechanical.check_citations``,
``refute_or_promote_gate.check_fabrication_survival``,
``honest_ci_gate.check_ci_widths``) call ``gates_common.emit`` themselves —
each port's own docstring says so explicitly ("Python callers wire rejection
through their own substrate"). That wiring is NOT yet implemented anywhere
else in the Python side of this repo (no caller module invokes it either).
This test therefore performs that wiring itself, verbatim-matched against
each bash gate's inline glue that calls ``mo_grounded_rejection`` after
printing its verdict JSON:

  lib/coalition_gate.sh:246-256               (gate="coalition")
  lib/krippendorff_alpha_gate.sh:230-238       (gate="krippendorff_alpha")
  lib/citation_verifier_mechanical.sh:232-240  (gate="citation_verifier")
  lib/refute_or_promote_gate.sh:242-250        (gate="refute_or_promote")
  lib/honest_ci_gate.sh:280-290                (gate="honest_ci")

This proves gates_common.py's write path + each port's verdict-generation
reproduce the SAME grounded_rejections outcomes the bash integration test
asserts, given the same fixture data — but it does NOT prove a production
caller wires them today. See the migration audit for the precise couplings
that still block deleting the bash gate cluster.
"""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from mini_ork.gates import common as gc
from mini_ork.gates import coalition_gate as coalition
from mini_ork.gates import krippendorff_alpha_gate as krippendorff
from mini_ork.gates import citation_verifier_mechanical as citation
from mini_ork.gates import refute_or_promote_gate as refute
from mini_ork.gates import honest_ci_gate as honest_ci

MIGRATION_0037 = REPO / "db" / "migrations" / "0037_grounded_rejection.sql"
AGENTS_YAML = str(REPO / "config" / "agents.yaml")


# ─────────────────────────────────────────────────────────────────────────────
# Fixture: a temp sqlite db carrying grounded_rejections (via the real
# migration, run through the sqlite3 CLI so its `.read "|sh -c ...'"` guard
# trick works) plus the minimal execution_traces / panel_topology_telemetry
# schema the bash integration test builds ad hoc (NOT db/init.sh's full
# production schema — this mirrors the bash fixture byte-for-byte).
# ─────────────────────────────────────────────────────────────────────────────
@pytest.fixture
def db_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    dbp = str(tmp_path / "state.db")
    subprocess.run(
        ["sqlite3", dbp],
        input=MIGRATION_0037.read_text(encoding="utf-8"),
        text=True,
        capture_output=True,
        check=True,
        env={**os.environ, "MINI_ORK_DB": dbp},
    )
    con = sqlite3.connect(dbp)
    try:
        con.executescript(
            """
            CREATE TABLE IF NOT EXISTS execution_traces (
              trace_id           TEXT PRIMARY KEY,
              agent_version_id   TEXT,
              reviewer_verdict   TEXT,
              verifier_output    TEXT
            );
            CREATE TABLE IF NOT EXISTS panel_topology_telemetry (
              telemetry_id        TEXT PRIMARY KEY,
              panel_run_id        TEXT,
              recipe               TEXT,
              rho                  REAL,
              context_distance     REAL,
              inductive_distance   REAL,
              agent_count          INTEGER,
              n_traces             INTEGER,
              quadrant             TEXT
            );
            """
        )
        con.commit()
    finally:
        con.close()
    monkeypatch.setenv("MINI_ORK_DB", dbp)
    monkeypatch.setenv("MINI_ORK_ROOT", str(REPO))
    monkeypatch.delenv("MINI_ORK_RUN_ID", raising=False)
    return dbp


def _count_rows(db: str) -> int:
    con = sqlite3.connect(db)
    try:
        row = con.execute("SELECT COUNT(*) FROM grounded_rejections").fetchone()
        return int(row[0])
    finally:
        con.close()


def _last_row(db: str) -> dict[str, Any]:
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    try:
        r = con.execute(
            "SELECT gate_name, verdict, concern, evidence_summary, suggestion "
            "FROM grounded_rejections ORDER BY rowid DESC LIMIT 1"
        ).fetchone()
        return dict(r) if r is not None else {}
    finally:
        con.close()


def _seed_traces(db: str, panel: str, lanes: list[str]) -> None:
    con = sqlite3.connect(db)
    try:
        con.execute("DELETE FROM execution_traces")
        for i, lane in enumerate(lanes):
            con.execute(
                "INSERT INTO execution_traces "
                "(trace_id, agent_version_id, reviewer_verdict) VALUES (?, ?, ?)",
                (f"tr-{i}-{panel}", lane, "APPROVE: looks good"),
            )
        con.commit()
    finally:
        con.close()


# ─────────────────────────────────────────────────────────────────────────────
# Wiring helpers — one per gate, mirroring the bash gate's own inline glue
# from verdict JSON -> mo_grounded_rejection args (see module docstring for
# the exact bash line numbers each of these reproduces).
# ─────────────────────────────────────────────────────────────────────────────
def _wire_coalition(verdict: dict[str, Any], run_id: str) -> str | int | None:
    if verdict.get("verdict") != "COALITION_ABORT":
        return None
    remediation = verdict.get("remediation") or (
        "widen family diversity in config/agents.yaml, or accept lens "
        "reports without synthesis"
    )
    return gc.emit(
        "coalition", "fail", verdict.get("reason", ""),
        verdict.get("rationale", ""), remediation, "[]", run_id,
    )


def _wire_krippendorff(verdict: dict[str, Any], run_id: str) -> str | int | None:
    if verdict.get("verdict") != "ALPHA_ESCALATE":
        return None
    return gc.emit(
        "krippendorff_alpha", "needs_revision", verdict.get("reason", ""),
        verdict.get("rationale", ""),
        "re-run lenses or widen the panel; alpha below floor means the "
        "panel cannot defend a single point-verdict",
        "[]", run_id,
    )


def _wire_citation(verdict: dict[str, Any], run_id: str) -> str | int | None:
    if verdict.get("verdict") != "CITATION_UNDERCOVERED":
        return None
    return gc.emit(
        "citation_verifier", "fail", verdict.get("reason", ""),
        verdict.get("rationale", ""),
        "add path/file.ext:LINE anchors for the uncited claims, or remove "
        "the unsupported claims",
        "[]", run_id,
    )


def _wire_refute(verdict: dict[str, Any], run_id: str) -> str | int | None:
    if verdict.get("verdict") != "REFUTE_FAILED":
        return None
    return gc.emit(
        "refute_or_promote", "fail", verdict.get("reason", ""),
        verdict.get("rationale", ""),
        "strengthen the validator: the surviving fabrications are false "
        "positives that must be refuted before promotion",
        "[]", run_id,
    )


def _wire_honest_ci(verdict: dict[str, Any], run_id: str) -> str | int | None:
    if verdict.get("verdict") != "CI_TOO_WIDE":
        return None
    return gc.emit(
        "honest_ci", "needs_revision", verdict.get("reason", ""),
        verdict.get("rationale", ""),
        "gather more samples or widen the band consciously; the current "
        "CIs are too wide to act on",
        "[]", run_id,
    )


# ─────────────────────────────────────────────────────────────────────────────
# 1. COALITION — 4 single-family lenses -> COALITION_ABORT / fail
#               4 distinct families    -> panel_diverse / no row
# ─────────────────────────────────────────────────────────────────────────────
def test_coalition_block_writes_fail_row(db_path: str) -> None:
    _seed_traces(db_path, "run-fixture-12345", ["sonnet", "opus", "sonnet", "opus"])
    before = _count_rows(db_path)
    verdict, rc = coalition.check_panel_coalition(
        "run-fixture-12345", "refactor-audit", db=db_path, agents_yaml=AGENTS_YAML,
    )
    assert verdict["verdict"] == "COALITION_ABORT"
    assert rc == 1
    _wire_coalition(verdict, "run-fixture-12345")
    after = _count_rows(db_path)
    assert after - before == 1
    row = _last_row(db_path)
    assert row["gate_name"] == "coalition"
    assert row["verdict"] == "fail"
    assert row["concern"] and row["evidence_summary"] and row["suggestion"]


def test_coalition_pass_writes_no_row(db_path: str) -> None:
    _seed_traces(db_path, "run-fixture-diverse", ["glm", "kimi", "codex", "minimax"])
    before = _count_rows(db_path)
    verdict, rc = coalition.check_panel_coalition(
        "run-fixture-diverse", "refactor-audit", db=db_path, agents_yaml=AGENTS_YAML,
    )
    assert verdict["verdict"] == "panel_diverse"
    assert rc == 0
    _wire_coalition(verdict, "run-fixture-diverse")
    after = _count_rows(db_path)
    assert after - before == 0


# ─────────────────────────────────────────────────────────────────────────────
# 2. KRIPPENDORFF ALPHA — low agreement -> ALPHA_ESCALATE / needs_revision
#                         high agreement -> panel_calibrated / no row
# ─────────────────────────────────────────────────────────────────────────────
def test_krippendorff_block_writes_needs_revision_row(
    db_path: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MINI_ORK_RUN_ID", "run-itest-krip")
    kdir = tmp_path / "krip"
    kdir.mkdir()
    (kdir / "panel-verdict.json").write_text(json.dumps({
        "lens_scores": {
            "glm":     [1, 9, 2, 8, 3],
            "kimi":    [9, 1, 8, 2, 9],
            "codex":   [2, 8, 3, 7, 2],
            "minimax": [8, 2, 9, 3, 8],
        }
    }), encoding="utf-8")
    before = _count_rows(db_path)
    verdict, rc = krippendorff.check_panel_alpha(str(kdir))
    assert verdict["verdict"] == "ALPHA_ESCALATE"
    assert rc == 1
    _wire_krippendorff(verdict, os.environ.get("MINI_ORK_RUN_ID", ""))
    after = _count_rows(db_path)
    assert after - before == 1
    row = _last_row(db_path)
    assert row["gate_name"] == "krippendorff_alpha"
    assert row["verdict"] == "needs_revision"
    assert row["concern"] and row["evidence_summary"] and row["suggestion"]


def test_krippendorff_pass_writes_no_row(
    db_path: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MINI_ORK_RUN_ID", "run-itest-krip")
    kdir = tmp_path / "krip"
    kdir.mkdir()
    (kdir / "panel-verdict.json").write_text(json.dumps({
        "lens_scores": {
            "glm":     [8, 7, 9, 8, 7],
            "kimi":    [8, 7, 9, 8, 7],
            "codex":   [8, 7, 9, 8, 7],
            "minimax": [8, 7, 9, 8, 7],
        }
    }), encoding="utf-8")
    before = _count_rows(db_path)
    verdict, rc = krippendorff.check_panel_alpha(str(kdir))
    assert verdict["verdict"] == "panel_calibrated"
    assert rc == 0
    _wire_krippendorff(verdict, os.environ.get("MINI_ORK_RUN_ID", ""))
    after = _count_rows(db_path)
    assert after - before == 0


# ─────────────────────────────────────────────────────────────────────────────
# 3. CITATION VERIFIER — half invalid citations -> CITATION_UNDERCOVERED / fail
#                        all valid citations    -> citations_covered / no row
# ─────────────────────────────────────────────────────────────────────────────
def test_citation_block_writes_fail_row(
    db_path: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MINI_ORK_RUN_ID", "run-itest-citation")
    crepo = tmp_path / "crepo"
    (crepo / "src").mkdir(parents=True)
    (crepo / "docs").mkdir(parents=True)
    (crepo / "src" / "foo.ts").write_text("line1\nline2\nline3\nline4\nline5\n")
    (crepo / "src" / "bar.py").write_text("a\nb\nc\nd\ne\nf\ng\nh\ni\nj\n")
    synthesis = crepo / "docs" / "synthesis.md"
    synthesis.write_text(
        "Mixed: real src/foo.ts:2 and ghost src/missing.ts:5 and\n"
        "out-of-bounds src/foo.ts:9999 and real src/bar.py:1.\n"
    )
    before = _count_rows(db_path)
    verdict, rc = citation.check_citations(
        str(synthesis), report_dir=str(tmp_path), root=str(crepo),
    )
    assert verdict["verdict"] == "CITATION_UNDERCOVERED"
    assert rc == 1
    _wire_citation(verdict, os.environ.get("MINI_ORK_RUN_ID", ""))
    after = _count_rows(db_path)
    assert after - before == 1
    row = _last_row(db_path)
    assert row["gate_name"] == "citation_verifier"
    assert row["verdict"] == "fail"
    assert row["concern"] and row["evidence_summary"] and row["suggestion"]


def test_citation_pass_writes_no_row(
    db_path: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MINI_ORK_RUN_ID", "run-itest-citation")
    crepo = tmp_path / "crepo"
    (crepo / "src").mkdir(parents=True)
    (crepo / "docs").mkdir(parents=True)
    (crepo / "src" / "foo.ts").write_text("line1\nline2\nline3\nline4\nline5\n")
    (crepo / "src" / "bar.py").write_text("a\nb\nc\nd\ne\nf\ng\nh\ni\nj\n")
    synthesis = crepo / "docs" / "synthesis.md"
    synthesis.write_text(
        "Three valid anchors. See src/foo.ts:2 for the first claim and\n"
        "src/foo.ts:3-4 for the second, and src/bar.py:7 for the third.\n"
    )
    before = _count_rows(db_path)
    verdict, rc = citation.check_citations(
        str(synthesis), report_dir=str(tmp_path), root=str(crepo),
    )
    assert verdict["verdict"] == "citations_covered"
    assert rc == 0
    _wire_citation(verdict, os.environ.get("MINI_ORK_RUN_ID", ""))
    after = _count_rows(db_path)
    assert after - before == 0


# ─────────────────────────────────────────────────────────────────────────────
# 4. REFUTE OR PROMOTE — 3/5 fabrications survive -> REFUTE_FAILED / fail
#                        0/5 fabrications survive -> validator_grounded / no row
# ─────────────────────────────────────────────────────────────────────────────
def test_refute_block_writes_fail_row(
    db_path: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MINI_ORK_RUN_ID", "run-itest-refute")
    rdir = tmp_path / "refute"
    rdir.mkdir()
    fab_path = rdir / "fab.json"
    refute.generate_fabrications(5, str(fab_path))
    fabs = json.loads(fab_path.read_text(encoding="utf-8"))

    findings_bad = rdir / "findings-bad.md"
    lines = ["## Findings", ""]
    for x in fabs[:3]:
        lines.append(f"- {x['path']}:{x['line']} — {x['claim']}")
    lines.append("- src/legitimate/foo.ts:10 — real finding")
    findings_bad.write_text("\n".join(lines) + "\n", encoding="utf-8")

    before = _count_rows(db_path)
    verdict, rc = refute.check_fabrication_survival(
        str(findings_bad), str(fab_path), report_dir=str(rdir),
    )
    assert verdict["verdict"] == "REFUTE_FAILED"
    assert rc == 1
    _wire_refute(verdict, os.environ.get("MINI_ORK_RUN_ID", ""))
    after = _count_rows(db_path)
    assert after - before == 1
    row = _last_row(db_path)
    assert row["gate_name"] == "refute_or_promote"
    assert row["verdict"] == "fail"
    assert row["concern"] and row["evidence_summary"] and row["suggestion"]


def test_refute_pass_writes_no_row(
    db_path: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MINI_ORK_RUN_ID", "run-itest-refute")
    rdir = tmp_path / "refute"
    rdir.mkdir()
    fab_path = rdir / "fab.json"
    refute.generate_fabrications(5, str(fab_path))

    findings_clean = rdir / "findings-clean.md"
    findings_clean.write_text(
        "## Findings\n\n"
        "1. Real issue in src/auth/login.ts:42 — token expiration not validated.\n"
        "2. Race condition in src/db/pool.ts:118 — connection reuse before reset.\n"
        "3. Missing null check in src/api/handler.ts:7.\n",
        encoding="utf-8",
    )

    before = _count_rows(db_path)
    verdict, rc = refute.check_fabrication_survival(
        str(findings_clean), str(fab_path), report_dir=str(rdir),
    )
    assert verdict["verdict"] == "validator_grounded"
    assert rc == 0
    _wire_refute(verdict, os.environ.get("MINI_ORK_RUN_ID", ""))
    after = _count_rows(db_path)
    assert after - before == 0


# ─────────────────────────────────────────────────────────────────────────────
# 5. HONEST CI — split panel (wide CIs) -> CI_TOO_WIDE / needs_revision
#               tight panel             -> ci_within_band / no row
# ─────────────────────────────────────────────────────────────────────────────
def test_honest_ci_block_writes_needs_revision_row(
    db_path: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MINI_ORK_RUN_ID", "run-itest-honestci")
    hdir = tmp_path / "honest"
    hdir.mkdir()
    findings_json = hdir / "findings.json"
    findings_json.write_text(json.dumps({"findings": [
        {"id": "F-001", "title": "Race condition",
         "lens_votes": {"glm": 0, "kimi": 3, "codex": 0, "minimax": 3}},
        {"id": "F-002", "title": "Stale cache read",
         "lens_votes": {"glm": 1, "kimi": 4, "codex": 0, "minimax": 5}},
        {"id": "F-003", "title": "Missing escape",
         "lens_votes": {"glm": 2, "kimi": 2, "codex": 2, "minimax": 2}},
    ]}), encoding="utf-8")

    before = _count_rows(db_path)
    verdict, rc = honest_ci.check_ci_widths(str(findings_json), report_dir=str(hdir))
    assert verdict["verdict"] == "CI_TOO_WIDE"
    assert rc == 1
    _wire_honest_ci(verdict, os.environ.get("MINI_ORK_RUN_ID", ""))
    after = _count_rows(db_path)
    assert after - before == 1
    row = _last_row(db_path)
    assert row["gate_name"] == "honest_ci"
    assert row["verdict"] == "needs_revision"
    assert row["concern"] and row["evidence_summary"] and row["suggestion"]


def test_honest_ci_pass_writes_no_row(
    db_path: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MINI_ORK_RUN_ID", "run-itest-honestci")
    hdir = tmp_path / "honest"
    hdir.mkdir()
    findings_json = hdir / "findings.json"
    findings_json.write_text(json.dumps({"findings": [
        {"id": "F-001", "title": "Auth retry storm",
         "lens_votes": {"glm": 2, "kimi": 2, "codex": 2, "minimax": 2}},
        {"id": "F-002", "title": "Cache key collision",
         "lens_votes": {"glm": 3, "kimi": 3, "codex": 3, "minimax": 3}},
        {"id": "F-003", "title": "Null cursor crash",
         "lens_votes": {"glm": 1, "kimi": 1, "codex": 1, "minimax": 1}},
    ]}), encoding="utf-8")

    before = _count_rows(db_path)
    verdict, rc = honest_ci.check_ci_widths(str(findings_json), report_dir=str(hdir))
    assert verdict["verdict"] == "ci_within_band"
    assert rc == 0
    _wire_honest_ci(verdict, os.environ.get("MINI_ORK_RUN_ID", ""))
    after = _count_rows(db_path)
    assert after - before == 0


# ─────────────────────────────────────────────────────────────────────────────
# 6. Aggregate — mirrors the bash script's closing "total rows == 5" check
#    when all 5 block paths run against one shared db.
# ─────────────────────────────────────────────────────────────────────────────
def test_all_five_blocks_together_yield_five_rows(
    db_path: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MINI_ORK_RUN_ID", "run-itest-aggregate")

    _seed_traces(db_path, "run-agg-coalition", ["sonnet", "opus", "sonnet", "opus"])
    v, _ = coalition.check_panel_coalition(
        "run-agg-coalition", "refactor-audit", db=db_path, agents_yaml=AGENTS_YAML,
    )
    _wire_coalition(v, "run-agg-coalition")

    kdir = tmp_path / "krip"
    kdir.mkdir()
    (kdir / "panel-verdict.json").write_text(json.dumps({
        "lens_scores": {
            "glm": [1, 9, 2, 8, 3], "kimi": [9, 1, 8, 2, 9],
            "codex": [2, 8, 3, 7, 2], "minimax": [8, 2, 9, 3, 8],
        }
    }), encoding="utf-8")
    v, _ = krippendorff.check_panel_alpha(str(kdir))
    _wire_krippendorff(v, os.environ.get("MINI_ORK_RUN_ID", ""))

    crepo = tmp_path / "crepo"
    (crepo / "src").mkdir(parents=True)
    (crepo / "docs").mkdir(parents=True)
    (crepo / "src" / "foo.ts").write_text("line1\nline2\nline3\nline4\nline5\n")
    (crepo / "src" / "bar.py").write_text("a\nb\nc\nd\ne\nf\ng\nh\ni\nj\n")
    synthesis = crepo / "docs" / "synthesis.md"
    synthesis.write_text(
        "Mixed: real src/foo.ts:2 and ghost src/missing.ts:5 and\n"
        "out-of-bounds src/foo.ts:9999 and real src/bar.py:1.\n"
    )
    v, _ = citation.check_citations(
        str(synthesis), report_dir=str(tmp_path / "creport"), root=str(crepo),
    )
    _wire_citation(v, os.environ.get("MINI_ORK_RUN_ID", ""))

    rdir = tmp_path / "refute"
    rdir.mkdir()
    fab_path = rdir / "fab.json"
    refute.generate_fabrications(5, str(fab_path))
    fabs = json.loads(fab_path.read_text(encoding="utf-8"))
    findings_bad = rdir / "findings-bad.md"
    lines = ["## Findings", ""]
    for x in fabs[:3]:
        lines.append(f"- {x['path']}:{x['line']} — {x['claim']}")
    lines.append("- src/legitimate/foo.ts:10 — real finding")
    findings_bad.write_text("\n".join(lines) + "\n", encoding="utf-8")
    v, _ = refute.check_fabrication_survival(
        str(findings_bad), str(fab_path), report_dir=str(rdir),
    )
    _wire_refute(v, os.environ.get("MINI_ORK_RUN_ID", ""))

    hdir = tmp_path / "honest"
    hdir.mkdir()
    findings_json = hdir / "findings.json"
    findings_json.write_text(json.dumps({"findings": [
        {"id": "F-001", "title": "Race condition",
         "lens_votes": {"glm": 0, "kimi": 3, "codex": 0, "minimax": 3}},
        {"id": "F-002", "title": "Stale cache read",
         "lens_votes": {"glm": 1, "kimi": 4, "codex": 0, "minimax": 5}},
        {"id": "F-003", "title": "Missing escape",
         "lens_votes": {"glm": 2, "kimi": 2, "codex": 2, "minimax": 2}},
    ]}), encoding="utf-8")
    v, _ = honest_ci.check_ci_widths(str(findings_json), report_dir=str(hdir))
    _wire_honest_ci(v, os.environ.get("MINI_ORK_RUN_ID", ""))

    assert _count_rows(db_path) == 5
    gate_names = set()
    con = sqlite3.connect(db_path)
    try:
        for r in con.execute("SELECT gate_name FROM grounded_rejections"):
            gate_names.add(r[0])
    finally:
        con.close()
    assert gate_names == {
        "coalition", "krippendorff_alpha", "citation_verifier",
        "refute_or_promote", "honest_ci",
    }
