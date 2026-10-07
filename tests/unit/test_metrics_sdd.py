"""K1 — ``mini-ork metrics sdd``: the SDD plan's baselines, read-only."""
from __future__ import annotations

import hashlib
import io
import json
import sqlite3
import time
from pathlib import Path

import jsonschema
import pytest

from mini_ork.cli import metrics, metrics_sdd
from mini_ork.stores import migrate as mig

REPO = Path(__file__).resolve().parents[2]
SCHEMA = json.loads((REPO / "schemas" / "metrics_sdd.schema.json").read_text(encoding="utf-8"))
K0_SNAPSHOT = Path("/Volumes/docker-ssd/Migration/Development/backups/k0-baseline-20261007-104547.db")


@pytest.fixture
def home(tmp_path: Path) -> Path:
    h = tmp_path / ".mini-ork"
    h.mkdir()
    rc, _out, err = mig.init_db(db=str(h / "state.db"), root=str(REPO))
    assert rc == 0, err
    con = sqlite3.connect(h / "state.db")
    now = int(time.time())
    runs = [  # id, recipe, status, verdict, notes, cost
        ("r-pub", "code-fix", "published", None, None, 2.0),
        ("r-pub-vac", "verified-artifact", "published", None, None, 1.0),
        ("r-fail", "code-fix", "failed", None, None, 3.0),
        ("r-crash", "code-fix", "failed", "CRASH", "reaped: dispatcher pid 1 gone", 4.0),
        ("r-open", "framework-edit", "executing", None, None, 10.0),
    ]
    for rid, recipe, status, verdict, notes, cost in runs:
        con.execute("INSERT INTO task_runs (id, recipe, status, verdict, notes, cost_usd, created_at, updated_at, "
                    "task_class, kickoff_path, workflow_version) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (rid, recipe, status, verdict, notes, cost, now, now, recipe.replace("-", "_"), "", "latest"))
    traces = [  # trace_id, run_id, status, created_at
        ("tr-verify-1", "r-pub", "success", "2026-09-10T00:00:00Z"),
        ("tr-verify-2", "r-pub-vac", "vacuous", "2026-09-11T00:00:00Z"),
        ("tr-verify-3", "r-fail", "success", "2026-10-01T00:00:00Z"),   # verify passed, run failed
        ("tr-verify-4", "r-crash", "success", "2026-10-02T00:00:00Z"),  # crashed: NOT verify-passed-run-failed
        ("tr-reviewer-1", "r-fail", "failure", "2026-10-01T00:00:00Z"),
        ("tr-reviewer-2", "r-pub", "success", "2026-09-10T00:00:00Z"),
        ("tr-plan-1", "r-open", "failure", "2026-10-03T00:00:00Z"),     # neither verify nor reviewer
    ]
    for tid, rid, status, created in traces:
        con.execute("INSERT INTO execution_traces (trace_id, run_id, status, created_at, task_class) VALUES (?,?,?,?,?)",
                    (tid, rid, status, created, "code_fix"))
    con.commit()
    con.close()
    runs_dir = h / "runs"
    for rid, files in {
        "r-pub": {"review-diff.patch": "diff --git a b\n", "implementer-summary.json": json.dumps(
            {"status": "implemented", "files_changed": ["a.py"]}), "run_profile.json": json.dumps({"profile_status": "ready"})},
        "r-fail": {"review-diff.patch": "", "implementer-summary.json": json.dumps(
            {"status": "implemented", "files_changed": []}), "run_profile.json": json.dumps({"profile_status": "ready"})},
        "r-open": {"plan-failure-parse_error.raw.txt": "x", "run_profile.json": json.dumps({"profile_status": "needs_answers"})},
        "r-orphan-dir": {},
    }.items():
        d = runs_dir / rid
        d.mkdir(parents=True)
        for name, body in files.items():
            (d / name).write_text(body, encoding="utf-8")
    return h


def _run(argv: list[str]) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    rc = metrics.main(argv, stdout=out, stderr=err)
    return rc, out.getvalue(), err.getvalue()


def test_db_baselines_are_exact(home: Path) -> None:
    rc, out, _ = _run(["sdd", "--db", str(home / "state.db"), "--json"])
    assert rc == 0
    m = json.loads(out)
    assert m["runs"] == {"total": 5, "published": 2, "publish_rate": 0.4, "non_terminal": 1, "cost_usd": 20.0,
                         "unpublished_cost_usd": 17.0, "unpublished_cost_share": 0.85, "cost_per_published": 1.5}
    assert m["crashed"] == {"runs": 1, "cost_usd": 4.0}
    assert (m["verify"]["traces"], m["verify"]["vacuous"], m["verify"]["vacuous_rate"]) == (4, 1, 0.25)
    assert [x["month"] for x in m["verify"]["monthly"]] == ["2026-09", "2026-10"]
    assert m["reviewer"] == {"traces": 2, "failures": 1, "failure_rate": 0.5}
    assert m["verify"]["published_with_vacuous"] == {"runs": 1, "by_recipe": {"verified-artifact": 1}}


def test_crashed_runs_never_count_as_verify_passed_run_failed(home: Path) -> None:
    # AC4: r-crash passed verify and is `failed`, but the reaper failed it — its own bucket.
    m = json.loads(_run(["sdd", "--db", str(home / "state.db"), "--json"])[1])
    assert m["verify"]["passed_run_failed"] == {"runs": 1, "by_recipe": {"code-fix": 1}}


def test_run_dir_metrics_from_live_dirs(home: Path) -> None:
    m = json.loads(_run(["sdd", "--db", str(home / "state.db"), "--json"])[1])
    rd = m["run_dirs"]
    assert rd["source"].startswith("live:")
    assert (rd["dirs"], rd["plan_failure"], rd["needs_answers"], rd["run_profiles"]) == (4, 1, 1, 3)
    assert (rd["empty_diffs"], rd["review_diffs"], rd["implemented_zero_files"], rd["implementer_summaries"]) == (1, 2, 1, 2)
    assert rd["needs_answers_rate"] == round(1 / 3, 4)


def test_frozen_run_dir_baseline_survives_dir_gc(home: Path, tmp_path: Path) -> None:
    frozen = tmp_path / "run-dirs.json"
    rc, _, err = _run(["sdd", "--db", str(home / "state.db"), "--write-run-dir-baseline", str(frozen), "--json"])
    assert rc == 0 and "froze 4 run dirs" in err
    saved = json.loads(frozen.read_text(encoding="utf-8"))
    assert saved["ids"]["plan_failure"] == ["r-open"] and saved["ids"]["implemented_zero_files"] == ["r-fail"]
    # Old dirs get garbage-collected; deltas must be computed against the frozen file.
    for d in (home / "runs").iterdir():
        for f in d.iterdir():
            f.unlink()
        d.rmdir()
    m = json.loads(_run(["sdd", "--db", str(home / "state.db"), "--run-dir-baseline", str(frozen), "--json"])[1])
    assert m["run_dirs"]["source"] == f"frozen:{frozen}" and m["run_dirs"]["dirs"] == 4


def test_json_is_schema_valid_and_stable(home: Path) -> None:
    out1 = _run(["sdd", "--db", str(home / "state.db"), "--json"])[1]
    out2 = _run(["sdd", "--db", str(home / "state.db"), "--json"])[1]
    assert out1 == out2  # stable: sorted keys, deterministic ordering
    jsonschema.validate(json.loads(out1), SCHEMA)
    jsonschema.validate(json.loads(_run(["sdd", "--db", str(home / "state.db"), "--no-run-dirs", "--json"])[1]), SCHEMA)


def test_never_writes_the_db(home: Path) -> None:
    db = home / "state.db"
    before = hashlib.sha256(db.read_bytes()).hexdigest()
    _run(["sdd", "--db", str(db), "--json"])
    _run(["sdd", "--db", str(db)])
    assert hashlib.sha256(db.read_bytes()).hexdigest() == before
    with pytest.raises(sqlite3.OperationalError):
        metrics_sdd.connect_readonly(str(db)).execute("DELETE FROM task_runs")


def test_markdown_view_and_missing_db(home: Path, tmp_path: Path) -> None:
    rc, out, _ = _run(["sdd", "--db", str(home / "state.db")])
    assert rc == 0 and "| Runs published | 2/5 (40.0%) |" in out and "Crashed / reaped" in out
    assert _run(["sdd", "--db", str(tmp_path / "nope.db")])[0] == 1


@pytest.mark.skipif(not K0_SNAPSHOT.is_file(), reason="K0 frozen snapshot only exists on the author's machine")
def test_reproduces_the_k0_snapshot_baselines() -> None:
    # AC1: the frozen K0 snapshot (2026-10-07 10:45).
    m = json.loads(_run(["sdd", "--db", str(K0_SNAPSHOT), "--no-run-dirs", "--json"])[1])
    r = m["runs"]
    assert (r["total"], r["published"], r["non_terminal"], r["cost_usd"]) == (1342, 467, 388, 3151.17)
    assert round(100 * r["unpublished_cost_share"], 1) == 67.0
    assert (m["verify"]["vacuous"], m["verify"]["traces"]) == (343, 994)
    assert (m["reviewer"]["failures"], m["reviewer"]["traces"]) == (200, 550)
    assert m["crashed"] == {"runs": 37, "cost_usd": 64.67}
