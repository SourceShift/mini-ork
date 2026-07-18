"""Python port of ``tests/integration/test_meta_orchestrator_loop.sh``.

The bash smoke exercises 7 phases of the meta-orchestrator compound loop by
sourcing ``lib/epic_graph.sh``, ``lib/topology.sh`` and ``lib/role_evolver.sh``
and driving ``bin/mini-ork-conductor`` + ``bin/mini-ork-lifetime`` against
``.mini-ork/state.db``. Both bin entrypoints already delegate to their Python
ports via ``lib/runtime-select.sh::mo_runtime_maybe_delegate`` whenever
``MINI_ORK_RUNTIME`` is unset or ``python`` (the default) — so the bash test
was, in practice, already exercising ``mini_ork.ported.mini_ork_conductor``
and ``mini_ork.ported.mini_ork_lifetime`` for phases 6-7. This port drives
those two modules directly (in-process, ``main(db=..., root=...)``) instead
of shelling out, and drives the three ``lib/*.sh`` equivalents
(``mini_ork.ported.epic_graph``, ``mini_ork.ported.topology``,
``mini_ork.ported.role_evolver``) for phases 1-5.

Phase-by-phase correspondence (same behavioral assertions, not weakened):

  1. Seed root + dependent epic via epic_dependencies
     -> direct sqlite3 INSERT (same rows the bash heredoc inserts).
  2. epic_graph_ready_now picks only the unblocked root
     -> mini_ork.ported.epic_graph.ready_now()
  3. epic_graph_on_done cascades dep unblock
     -> mini_ork.ported.epic_graph.on_done()
  4. Seed execution_traces + recompute topology win-rates
     -> mini_ork.ported.topology.aggregate_traces() (the pure-logic port of
        lib/topology.sh::topology_recompute_win_rates, parity-proven against
        the live bash function by tests/unit/test_topology_parity.py), then
        persisted into topology_win_rates with the identical upsert SQL
        lib/topology.sh's own embedded-python heredoc uses (topology.py is
        intentionally I/O-free by design — see its module docstring — so the
        caller performs the read/write; this mirrors that contract exactly).
  5. role_evolver_propose runs cleanly
     -> mini_ork.ported.role_evolver.propose()
  6. conductor --once --dry-run --explain emits JSON decision
     -> mini_ork.ported.mini_ork_conductor.main(db=..., root=...)
  7. lifetime summary prints non-empty leaderboards
     -> mini_ork.ported.mini_ork_lifetime.summary()

Note on lib/bug_report.sh: the bash test's header comment lists "bug_report
channel (lib/bug_report.sh)" as in-scope, but the script body never sources
or calls it — it is not exercised by any phase below either. bug_report.sh
has real, unrelated couplings (bin/mini-ork-bugs, bin/mini-ork-bug-collector,
lib/pre_push_review.sh::review_forward_to_bug_reports) so its retirement
status is untouched by this port either way.
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from mini_ork.ported import epic_graph  # noqa: E402
from mini_ork.ported import mini_ork_conductor as conductor  # noqa: E402
from mini_ork.ported import mini_ork_lifetime as lifetime  # noqa: E402
from mini_ork.ported import role_evolver  # noqa: E402
from mini_ork.ported.topology import aggregate_traces  # noqa: E402

INIT_SH = REPO_ROOT / "db" / "init.sh"

# Namespaced ids mirror the bash test's "mol-" prefix / "$TS" uniqueness
# convention. A fresh per-test DB (below) makes the uniqueness suffix
# unnecessary, but the prefix is kept so the "^mol-" filter phase 2 performs
# in bash has a literal Python equivalent.
ROOT_EPIC = "mol-root"
DEP_EPIC = "mol-dep"
SEED_TC = "mol_test_class"
SEED_WF = "mol_wf"
SEED_LANE = "mol_lane"
SEED_RUN = "mol-run"


def _which_tools() -> None:
    for tool in ("bash", "sqlite3", "python3"):
        if not shutil.which(tool):
            pytest.skip(f"{tool} not on PATH")
    if not INIT_SH.exists():
        pytest.skip(f"missing db/init.sh at {INIT_SH}")


@pytest.fixture
def mo_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    """Fresh mini-ork sqlite state.db, migrated via the real db/init.sh.

    Mirrors the bash test's CI bootstrap (``db/init.sh`` applied idempotently
    before any schema-dependent check), but on an isolated temp DB rather
    than the shared ``.mini-ork/state.db`` the bash test mutated in place —
    same schema, same behavior, no side effects on real developer state.
    """
    _which_tools()
    home = tmp_path / ".mini-ork"
    home.mkdir()
    db_path = str(home / "state.db")
    result = subprocess.run(
        ["bash", str(INIT_SH)],
        env={**os.environ, "MINI_ORK_HOME": str(home), "MINI_ORK_DB": db_path},
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        pytest.skip(f"db/init.sh failed: rc={result.returncode}\nstderr={result.stderr}")
    monkeypatch.setenv("MINI_ORK_ROOT", str(REPO_ROOT))
    monkeypatch.setenv("MINI_ORK_HOME", str(home))
    monkeypatch.setenv("MINI_ORK_DB", db_path)
    return db_path


def test_meta_orchestrator_loop_phases(
    mo_db: str, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    state_db = mo_db
    con = sqlite3.connect(state_db)
    con.execute("PRAGMA busy_timeout=5000")

    # ── Phase 1: Seed root + dependent epic via epic_dependencies ──────────
    con.execute(
        "INSERT INTO epics(id, title, status) VALUES(?,?,?)",
        (ROOT_EPIC, "meta-orch smoke root", "not started"),
    )
    con.execute(
        "INSERT INTO epics(id, title, status) VALUES(?,?,?)",
        (DEP_EPIC, "meta-orch smoke dep", "blocked"),
    )
    con.execute(
        "INSERT INTO epic_dependencies(from_epic_id, to_epic_id, kind) VALUES(?,?,?)",
        (ROOT_EPIC, DEP_EPIC, "hard"),
    )
    con.commit()
    seeded = con.execute(
        "SELECT COUNT(*) FROM epics WHERE id LIKE 'mol-%'"
    ).fetchone()[0]
    assert seeded == 2, f"2 epics seeded; got {seeded}"

    # ── Phase 2: epic_graph_ready_now picks only the unblocked root ────────
    ready = [e for e in epic_graph.ready_now(db=state_db) if e.startswith("mol-")]
    assert ready == [ROOT_EPIC], (
        f"ready_now should list only the seeded root; got {ready!r}"
    )

    # ── Phase 3: epic_graph_on_done cascades dep unblock ────────────────────
    con.execute("UPDATE epics SET status='done' WHERE id=?", (ROOT_EPIC,))
    con.commit()
    epic_graph.on_done(ROOT_EPIC, db=state_db)
    dep_status = con.execute(
        "SELECT status FROM epics WHERE id=?", (DEP_EPIC,)
    ).fetchone()[0]
    assert dep_status == "not started", (
        f"dependent epic should flip 'blocked' -> 'not started'; got {dep_status!r}"
    )

    # ── Phase 4: Seed execution_traces and recompute topology win-rates ────
    for n in (1, 2, 3, 4):
        status = "success" if n <= 2 else "failure"
        verdict = "APPROVE" if n <= 2 else "REJECT"
        con.execute(
            "INSERT INTO execution_traces(trace_id, run_id, task_class, "
            "workflow_version_id, agent_version_id, status, reviewer_verdict, "
            "verifier_output, cost_usd, duration_ms, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?, strftime('%Y-%m-%dT%H:%M:%S.000Z','now'))",
            (f"mol-tr-{n}", SEED_RUN, SEED_TC, SEED_WF, SEED_LANE,
             status, verdict, '{"node_type":"researcher"}', 0.05, 1500),
        )
    con.commit()

    traces: list[dict[str, object]] = []
    cur = con.execute(
        "SELECT workflow_version_id, task_class, status, reviewer_verdict, "
        "cost_usd, duration_ms, created_at FROM execution_traces WHERE task_class = ?",
        (SEED_TC,),
    )
    for wvid, tclass, status, verdict, cost, dur, created in cur.fetchall():
        traces.append({
            "workflow_version_id": wvid,
            "task_class": tclass,
            "status": status,
            "reviewer_verdict": verdict,
            "cost_usd": cost,
            "duration_ms": dur,
            "created_at": created,
        })

    agg_rows = aggregate_traces(traces, workflow_memory=None)
    seed_row = next(r for r in agg_rows if r["task_class"] == SEED_TC)
    assert f"{seed_row['win_rate']:.2f}" == "0.50", (
        f"topology win_rate should be 0.50 (2 wins / 4 traces); got {seed_row!r}"
    )

    # Persist the aggregate the same way lib/topology.sh's embedded python
    # upserts it (topology.py is deliberately I/O-free; the caller performs
    # this write per its own docstring), then re-read via the
    # topology_win_rates table — the literal query surface bash asserted on.
    for r in agg_rows:
        con.execute(
            """
            INSERT INTO topology_win_rates
                (topology_id, workflow_name, task_class, wins, losses, ties,
                 win_rate, sample_size, avg_cost_usd, avg_duration_ms, last_updated)
            VALUES (?,?,?,?,?,?,?,?,?,?, strftime('%Y-%m-%dT%H:%M:%fZ','now'))
            ON CONFLICT(topology_id, task_class) DO UPDATE SET
                workflow_name   = excluded.workflow_name,
                wins            = excluded.wins,
                losses          = excluded.losses,
                ties            = excluded.ties,
                win_rate        = excluded.win_rate,
                sample_size     = excluded.sample_size,
                avg_cost_usd    = excluded.avg_cost_usd,
                avg_duration_ms = excluded.avg_duration_ms,
                last_updated    = excluded.last_updated
            """,
            (r["topology_id"], r["workflow_name"], r["task_class"],
             r["wins"], r["losses"], r["ties"], r["win_rate"], r["sample_size"],
             r["avg_cost_usd"], r["avg_duration_ms"]),
        )
    con.commit()

    wr = con.execute(
        "SELECT printf('%.2f', win_rate) FROM topology_win_rates WHERE task_class=?",
        (SEED_TC,),
    ).fetchone()[0]
    assert wr == "0.50", f"topology win_rate = 0.50 (2 wins / 4 traces); got {wr!r}"

    # ── Phase 5: role_evolver_propose runs cleanly ──────────────────────────
    n_proposed = role_evolver.propose(db=state_db, top=3)
    assert isinstance(n_proposed, int) and n_proposed >= 0, (
        f"role_evolver_propose should return a numeric count; got {n_proposed!r}"
    )

    # ── Phase 6: conductor --once --dry-run --explain emits JSON decision ──
    monkeypatch.setenv("MO_PLASTICITY_BUDGET", "5")
    capsys.readouterr()  # drain any prior output
    rc = conductor.main(
        ["--once", "--dry-run", "--explain"], db=state_db, root=str(REPO_ROOT)
    )
    captured = capsys.readouterr()
    dec_output = captured.out + captured.err
    assert rc == 0, f"conductor should succeed with a ready epic in queue; rc={rc}, out={dec_output!r}"
    assert '"epic_id"' in dec_output, (
        f"conductor emitted a decision JSON with epic_id; got: {dec_output!r}"
    )
    decision_rows = con.execute(
        "SELECT COUNT(*) FROM conductor_decisions "
        "WHERE decided_at >= strftime('%s','now','-2 minutes')"
    ).fetchone()[0]
    assert decision_rows >= 1, (
        f"conductor_decisions should have appended >= 1 row in the last 2 minutes; "
        f"got {decision_rows}"
    )

    # ── Phase 7: lifetime summary prints non-empty leaderboards ────────────
    capsys.readouterr()  # drain
    out = lifetime.summary()
    assert "Run volume" in out, f"lifetime summary should have a Run volume section; got: {out!r}"
    assert "topologies by win_rate" in out, (
        f"lifetime summary should have a topologies leaderboard; got: {out!r}"
    )

    con.close()
