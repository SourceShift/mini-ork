"""Unit tests: the ``rsi-cycle`` operator surface at the read-only step.

What is pinned here is the *contract* the later stages build on:

* ``plan`` spends nothing and writes nothing — it opens the DB, harvests and
  prints. Running it twice against the same DB must leave the ledger unchanged.
* every failure that means "this checkout cannot run the loop" exits **2**
  with a remedy on stderr (missing DB, missing ``rsi_*`` tables, unknown flag),
  while a *finding* — a harvest that could not look — exits 1.
* ``--json`` prints exactly one object on stdout, so `jq` never sees prose.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from mini_ork.cli import rsi_cycle  # noqa: E402
from mini_ork.rsi import ledger  # noqa: E402
from mini_ork.rsi.harvest import HarvestReport  # noqa: E402
from mini_ork.stores import migrate  # noqa: E402

MIGRATIONS = ("0052_run_leases_recovery_requests.sql", "0084_rsi_cycles.sql")


def _fake_root(tmp_path: Path) -> Path:
    """A repo-shaped root: the two binaries doctor looks for, plus migrations."""
    root = tmp_path / "repo"
    (root / "bin").mkdir(parents=True)
    (root / "bin" / "mini-ork").write_text("#!/bin/sh\n", encoding="utf-8")
    (root / "scripts").mkdir()
    (root / "scripts" / "mini_ork_worktree.py").write_text("", encoding="utf-8")
    (root / "db" / "migrations").mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    subprocess.run(
        ["git", "-C", str(root), "remote", "add", "origin", "https://example.invalid/x.git"],
        check=True,
    )
    return root


def _db(root: Path, *, apply: bool = True) -> Path:
    db = root / ".mini-ork" / "state.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    if apply:
        migdir = root / "db" / "migrations"
        for name in MIGRATIONS:
            shutil.copy(REPO / "db" / "migrations" / name, migdir / name)
        rc, out = migrate.migrate_apply(str(migdir), db=str(db), root=str(REPO))
        assert rc == 0, out
    else:
        db.write_bytes(b"")
    return db


@pytest.fixture
def root(tmp_path):
    return _fake_root(tmp_path)


@pytest.fixture
def db(root):
    return _db(root)


# ── parser ──────────────────────────────────────────────────────────────────


def test_every_documented_verb_parses():
    parser = rsi_cycle.build_parser()
    for argv in (
        ["plan"],
        ["plan", "--json", "--no-probes", "--domain", "routing", "--max", "2"],
        ["list"],
        ["list", "--status", "open", "--limit", "5", "--cycles"],
        ["show", "rsi-abc"],
        ["doctor", "--json"],
    ):
        assert parser.parse_args(argv).verb == argv[0]


def test_unknown_flag_exits_2(capsys):
    assert rsi_cycle.main(["plan", "--yolo"], root="/tmp") == 2
    assert capsys.readouterr().err


def test_show_without_an_id_exits_2(capsys):
    assert rsi_cycle.main(["show"], root="/tmp") == 2
    assert "show <cycle_id" in capsys.readouterr().err


def test_help_prints_usage(capsys):
    assert rsi_cycle.main(["--help"]) == 0
    assert "rsi-cycle" in capsys.readouterr().out


# ── plan ────────────────────────────────────────────────────────────────────


def test_plan_on_an_empty_but_migrated_db_is_clean_rc0(root, db, capsys):
    rc = rsi_cycle.main(["plan", "--no-probes"], root=root)
    out = capsys.readouterr().out
    assert rc == 0
    assert "harvest ok" in out
    assert "no candidate is eligible" in out


def test_plan_json_is_exactly_one_object(root, db, capsys):
    assert rsi_cycle.main(["plan", "--no-probes", "--json"], root=root) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["harvest_ok"] is True
    assert set(payload["queue"]) == {"examined", "selected", "backlog", "rejected", "holes"}


def test_plan_writes_nothing_to_the_ledger(root, db, capsys):
    def snapshot() -> tuple[int, int]:
        con = ledger.connect(str(db))
        try:
            return (
                con.execute("SELECT COUNT(*) FROM rsi_candidates").fetchone()[0],
                con.execute("SELECT COUNT(*) FROM rsi_cycles").fetchone()[0],
            )
        finally:
            con.close()

    before = snapshot()
    rsi_cycle.main(["plan", "--no-probes", "--json"], root=root)
    capsys.readouterr()
    assert snapshot() == before


def test_probes_read_the_same_db_as_the_harvest(root, db, monkeypatch, capsys):
    """`--db` must reach the probe subprocesses. Without it, `mini-ork metrics`
    resolves `.mini-ork/state.db` from the cwd and the cycle reads two DBs."""
    seen: dict[str, str | None] = {}

    def fake_run_probes(specs, *, root):
        seen["db"] = os.environ.get("MINI_ORK_DB")
        return ()

    monkeypatch.setattr("mini_ork.rsi.probes.run_probes", fake_run_probes)
    assert rsi_cycle.main(["plan"], root=root) == 0  # probes enabled by default
    capsys.readouterr()
    assert seen["db"] == str(db)


def test_plan_reports_an_incomplete_harvest_as_rc1(root, db, monkeypatch, capsys):
    monkeypatch.setattr(
        "mini_ork.rsi.harvest.harvest",
        lambda *a, **kw: HarvestReport(
            candidates=(), probes=(), harvest_ok=False, skipped=(), errors=("L1",)
        ),
    )
    assert rsi_cycle.main(["plan", "--no-probes"], root=root) == 1
    assert "INCOMPLETE" in capsys.readouterr().out


def test_missing_db_is_a_setup_error_not_an_empty_queue(tmp_path, capsys):
    root = _fake_root(tmp_path)
    rc = rsi_cycle.main(["plan", "--no-probes"], root=root)
    err = capsys.readouterr().err
    assert rc == 2
    assert "state DB not found" in err


def test_unmigrated_db_points_at_update(root, capsys):
    _db(root, apply=False)
    assert rsi_cycle.main(["plan", "--no-probes"], root=root) == 2
    assert "mini-ork update" in capsys.readouterr().err


# ── list / show ─────────────────────────────────────────────────────────────


def test_list_is_empty_on_a_fresh_db(root, db, capsys):
    assert rsi_cycle.main(["list"], root=root) == 0
    assert "no candidates recorded" in capsys.readouterr().out


def test_list_json_is_an_array(root, db, capsys):
    assert rsi_cycle.main(["list", "--json"], root=root) == 0
    assert json.loads(capsys.readouterr().out) == []


def test_list_cycles_dispatches_to_the_cycle_table(root, db, capsys):
    assert rsi_cycle.main(["list", "--cycles"], root=root) == 0
    assert "no cycles recorded" in capsys.readouterr().out


def test_unknown_status_is_a_usage_error_rc2(root, db, capsys):
    # The valid statuses differ between the candidate and cycle tables, so
    # argparse cannot express them; the command turns the ledger's ValueError
    # into rc 2 instead of a traceback.
    assert rsi_cycle.main(["list", "--status", "nonsense"], root=root) == 2
    assert "unknown candidate status" in capsys.readouterr().err


def test_show_unknown_id_is_rc2(root, db, capsys):
    assert rsi_cycle.main(["show", "rsi-deadbeef"], root=root) == 2
    assert "no such candidate" in capsys.readouterr().err


# ── doctor ──────────────────────────────────────────────────────────────────


def test_doctor_reports_every_check_and_its_remedy(root, db, monkeypatch, capsys):
    # The temp DB only has 0052 + 0084; the real repo has ~84 migrations.
    monkeypatch.setattr(
        "mini_ork.stores.migrate.migrate_status",
        lambda *a, **kw: (84, 0, 0, 84),
    )
    rc = rsi_cycle.main(["doctor", "--json"], root=root)
    payload = json.loads(capsys.readouterr().out)
    assert rc == 0, payload
    assert payload["ok"] is True
    names = [c["name"] for c in payload["checks"]]
    assert names == [
        "database", "migrations", "rsi_tables", "lease_tables",
        "binaries", "origin_remote", "harvest_rules",
    ]


def test_doctor_fails_when_the_db_is_missing(root, capsys):
    assert rsi_cycle.main(["doctor"], root=root) == 1
    out = capsys.readouterr().out
    assert "FAIL" in out
    assert "not ready" in out


def test_doctor_flags_pending_migrations(root, db, monkeypatch, capsys):
    monkeypatch.setattr(
        "mini_ork.stores.migrate.migrate_status",
        lambda *a, **kw: (80, 4, 0, 84),
    )
    assert rsi_cycle.main(["doctor"], root=root) == 1
    assert "4 of 84 migration(s) pending" in capsys.readouterr().out


def test_doctor_flags_schema_drift_as_not_ready(root, db, monkeypatch, capsys):
    from dataclasses import replace

    from mini_ork.rsi.candidates import Candidate

    drift = Candidate.build(
        domain="verification", source="db:R8", kind="probe_broken",
        title="llm_calls has no lane column", evidence_key="table:llm_calls:lane",
        now=0,
    )
    real = __import__("mini_ork.rsi.harvest", fromlist=["harvest"]).harvest
    monkeypatch.setattr(
        "mini_ork.rsi.harvest.harvest",
        lambda *a, **kw: replace(
            real(*a, **kw), candidates=(drift,), harvest_ok=True, errors=()
        ),
    )
    monkeypatch.setattr(
        "mini_ork.stores.migrate.migrate_status", lambda *a, **kw: (84, 0, 0, 84)
    )
    assert rsi_cycle.main(["doctor"], root=root) == 1
    assert "schema drift" in capsys.readouterr().out
