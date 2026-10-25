"""Unit tests: mini_ork.rsi.ledger against a real migrated temp DB.

The migration is applied through ``mini_ork.stores.migrate.migrate_apply`` — the
same runner `mini-ork update` uses — rather than hand-written DDL, so a drift
between ``0084_rsi_cycles.sql`` and what the ledger module expects fails here
instead of in a live cycle.
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from mini_ork.rsi import ledger as L  # noqa: E402
from mini_ork.rsi.candidates import Candidate  # noqa: E402
from mini_ork.stores import migrate  # noqa: E402

MIGRATION = REPO / "db" / "migrations" / "0084_rsi_cycles.sql"
NOW = 1_760_000_000


def _apply_0084(db_path: Path, tmp_path: Path) -> None:
    migdir = tmp_path / "migrations"
    migdir.mkdir(exist_ok=True)
    shutil.copy(MIGRATION, migdir / MIGRATION.name)
    rc, out = migrate.migrate_apply(str(migdir), dry_run=False, db=str(db_path),
                                    root=str(REPO))
    assert rc == 0, out


@pytest.fixture
def db(tmp_path):
    db_path = tmp_path / "state.db"
    _apply_0084(db_path, tmp_path)
    con = L.connect(str(db_path))
    yield con
    con.close()


def _cand(**overrides) -> Candidate:
    kwargs = dict(
        domain="verification",
        source="db:V1",
        kind="data_path_dead",
        title="confidence is never persisted",
        evidence_key="table:verifier_results:confidence-null",
        files=("mini_ork/verify/persist.py",),
        now=NOW,
    )
    kwargs.update(overrides)
    return Candidate.build(**kwargs)


# ── schema guard ────────────────────────────────────────────────────────────


def test_migration_creates_all_four_tables(db):
    assert L.tables_present(db)


def test_migration_is_idempotent(tmp_path):
    db_path = tmp_path / "state.db"
    _apply_0084(db_path, tmp_path)
    _apply_0084(db_path, tmp_path)  # second apply must be a no-op, not an error
    con = L.connect(str(db_path))
    try:
        assert L.tables_present(con)
        rows = con.execute(
            "SELECT COUNT(*) FROM schema_migrations WHERE filename = ?",
            ("0084_rsi_cycles.sql",),
        ).fetchone()[0]
        assert rows == 1
    finally:
        con.close()


def test_sorted_after_0083():
    names = sorted(p.name for p in (REPO / "db" / "migrations").glob("*.sql"))
    assert names.index("0084_rsi_cycles.sql") == len(names) - 1


def test_require_tables_fails_closed_on_a_bare_db(tmp_path):
    con = L.connect(str(tmp_path / "empty.db"))
    try:
        assert not L.tables_present(con)
        with pytest.raises(L.LedgerUnavailable) as exc:
            L.require_tables(con)
        assert "mini-ork update" in str(exc.value)
    finally:
        con.close()


# ── cycles ──────────────────────────────────────────────────────────────────


def test_start_and_finish_cycle_round_trip(db):
    L.start_cycle(db, cycle_id="c1", now=NOW, mode="once", dry_run=False)
    running = L.get_cycle(db, "c1")
    assert running["status"] == "running"
    assert running["dry_run"] == 0
    assert running["finished_at"] is None

    L.finish_cycle(db, "c1", now=NOW + 60, status="merged", cost_usd=9.69,
                   merged_sha="abc123", gates_json={"G1": True})
    row = L.get_cycle(db, "c1")
    assert row["status"] == "merged"
    assert row["finished_at"] == NOW + 60
    assert row["cost_usd"] == pytest.approx(9.69)
    assert row["merged_sha"] == "abc123"
    assert row["gates_json"] == '{"G1": true}'


def test_finish_cycle_rejects_unknown_status_and_column(db):
    L.start_cycle(db, cycle_id="c1", now=NOW, mode="once", dry_run=False)
    with pytest.raises(ValueError):
        L.finish_cycle(db, "c1", now=NOW, status="done-ish")
    with pytest.raises(ValueError):
        L.finish_cycle(db, "c1", now=NOW, status="merged", not_a_column=1)


def test_list_cycles_orders_by_epoch_not_text(db):
    L.start_cycle(db, cycle_id="old", now=1_600_000_000, mode="once", dry_run=False)
    L.start_cycle(db, cycle_id="new", now=1_760_000_000, mode="once", dry_run=False)
    assert [r["cycle_id"] for r in L.list_cycles(db)] == ["new", "old"]


# ── candidates ──────────────────────────────────────────────────────────────


def test_upsert_reports_new_then_seen(db):
    cand = _cand()
    assert L.upsert_candidate(db, cand, now=NOW) == "new"
    assert L.upsert_candidate(db, cand, now=NOW + 10) == "seen"
    row = L.get_candidate(db, cand.candidate_id)
    assert row["times_seen"] == 2
    assert row["first_seen_at"] == NOW
    assert row["last_seen_at"] == NOW + 10
    assert row["status"] == "open"


def test_upsert_bumps_updated_at_only_while_open(db):
    cand = _cand()
    L.upsert_candidate(db, cand, now=NOW)
    L.set_candidate_status(db, cand.candidate_id, "quarantined", now=NOW + 100,
                           reason="G2 child_verdict_failed")
    L.upsert_candidate(db, cand, now=NOW + 500)
    row = L.get_candidate(db, cand.candidate_id)
    # The failure TTL must measure when the outcome was decided, not the last
    # time the harvest noticed the gap still exists.
    assert row["updated_at"] == NOW + 100
    assert row["times_seen"] == 2
    assert row["status"] == "quarantined"
    assert row["outcome_reason"] == "G2 child_verdict_failed"


def test_candidate_by_signature_is_the_avoidance_lookup(db):
    cand = _cand()
    L.upsert_candidate(db, cand, now=NOW)
    row = L.candidate_by_signature(db, cand.signature)
    assert row is not None
    assert row["candidate_id"] == cand.candidate_id
    assert L.candidate_by_signature(db, "0" * 16) is None


def test_set_candidate_status_records_sha_and_rejects_unknown(db):
    cand = _cand()
    L.upsert_candidate(db, cand, now=NOW)
    L.set_candidate_status(db, cand.candidate_id, "merged", now=NOW + 5,
                           reason="gates green", merged_sha="deadbeef")
    row = L.get_candidate(db, cand.candidate_id)
    assert row["status"] == "merged"
    assert row["merged_sha"] == "deadbeef"
    with pytest.raises(ValueError):
        L.set_candidate_status(db, cand.candidate_id, "sort-of-done", now=NOW)


def test_reconcile_closes_unreproduced_open_candidates_only(db):
    kept = _cand(evidence_key="table:a:empty")
    gone = _cand(evidence_key="table:b:empty")
    merged = _cand(evidence_key="table:c:empty")
    for cand in (kept, gone, merged):
        L.upsert_candidate(db, cand, now=NOW)
    L.set_candidate_status(db, merged.candidate_id, "merged", now=NOW, merged_sha="s")

    closed = L.reconcile(db, [kept.candidate_id], now=NOW + 60)

    assert closed == 1
    assert L.get_candidate(db, kept.candidate_id)["status"] == "open"
    assert L.get_candidate(db, gone.candidate_id)["status"] == "fixed_externally"
    assert L.get_candidate(db, gone.candidate_id)["outcome_reason"] == (
        "not reproduced by harvest"
    )
    assert L.get_candidate(db, merged.candidate_id)["status"] == "merged"


def test_reconcile_with_empty_harvest_closes_every_open_candidate(db):
    for key in ("a", "b"):
        L.upsert_candidate(db, _cand(evidence_key=f"table:{key}:empty"), now=NOW)
    assert L.reconcile(db, [], now=NOW + 1) == 2
    assert L.list_candidates(db, status="open") == []


def test_list_candidates_filters_and_orders(db):
    low = _cand(evidence_key="table:low:empty", kind="coverage_gap")
    high = _cand(evidence_key="table:high:empty", kind="producer_missing",
                 domain="learning")
    L.upsert_candidate(db, low, now=NOW)
    L.upsert_candidate(db, high, now=NOW)
    rows = L.list_candidates(db, limit=10)
    assert [r["candidate_id"] for r in rows] == [high.candidate_id, low.candidate_id]
    assert [r["candidate_id"] for r in L.list_candidates(db, domain="learning")] == [
        high.candidate_id
    ]
    with pytest.raises(ValueError):
        L.list_candidates(db, status="nope")


# ── probes and credit ───────────────────────────────────────────────────────


def test_probe_runs_are_unique_per_cycle_and_probe(db):
    for status in ("findings", "ok"):
        L.record_probe_run(db, cycle_id="c1", now=NOW, probe_id="garden", cmd="garden",
                           status=status, rc=0, duration_ms=120, findings_count=3)
    rows = L.list_probe_runs(db, "c1")
    assert len(rows) == 1
    assert rows[0]["status"] == "ok"


def test_credit_stats_need_min_observations(db):
    for i in range(2):
        L.record_credit(db, cycle_id=f"c{i}", now=NOW, candidate_id=None,
                        source="db:V1", lens="glm_lens", domain="verification",
                        outcome="quarantined")
    assert L.credit_stats(db, min_n=3) == {}

    L.record_credit(db, cycle_id="c9", now=NOW, candidate_id=None, source="db:V1",
                    lens="glm_lens", domain="verification", outcome="merged")
    stats = L.credit_stats(db, min_n=3)
    assert stats[("db:V1", "glm_lens")]["n"] == 3
    assert stats[("db:V1", "glm_lens")]["merged"] == 1
    assert stats[("db:V1", "glm_lens")]["rate"] == pytest.approx(1 / 3)


def test_credit_groups_by_source_and_lens(db):
    for lens in ("glm_lens", "opus_lens"):
        for i in range(3):
            L.record_credit(db, cycle_id=f"{lens}{i}", now=NOW, candidate_id=None,
                            source="db:V1", lens=lens, domain="verification",
                            outcome="merged")
    stats = L.credit_stats(db, min_n=3)
    assert set(stats) == {("db:V1", "glm_lens"), ("db:V1", "opus_lens")}
    assert all(v["rate"] == 1.0 for v in stats.values())
