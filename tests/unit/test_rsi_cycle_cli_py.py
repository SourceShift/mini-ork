"""Unit tests: the ``rsi-cycle`` operator surface.

What is pinned here is the *contract* the later stages build on:

* ``plan`` spends nothing and writes nothing — it opens the DB, harvests and
  prints. Running it twice against the same DB must leave the ledger unchanged.
* every failure that means "this checkout cannot run the loop" exits **2**
  with a remedy on stderr (missing DB, missing ``rsi_*`` tables, unknown flag),
  while a *finding* — a harvest that could not look — exits 1.
* ``--json`` prints exactly one object on stdout, so `jq` never sees prose.
* the run verb owns no policy of its own: it parses flags, loads the config,
  hands a request to ``cycle.run_cycle`` and renders the outcome. Those tests
  replace ``run_cycle`` for exactly that reason.
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
        ["run"],
        ["run", "--once", "--config", "x.yaml", "--dry-run", "--suggest",
         "--probe-run", "--max-cycles", "2", "--budget", "5",
         "--candidate", "rsi-abc", "--domain", "learning", "--json"],
        ["plan"],
        ["plan", "--json", "--no-probes", "--domain", "routing", "--max", "2"],
        ["list"],
        ["list", "--status", "open", "--limit", "5", "--cycles"],
        ["show", "rsi-abc"],
        ["quarantine"],
        ["quarantine", "show", "rsi-c1"],
        ["quarantine", "release", "rsi-c1"],
        ["revert", "rsi-c1"],
        ["halt"],
        ["resume"],
        ["doctor", "--json"],
    ):
        assert parser.parse_args(argv).verb == argv[0]


def test_the_default_verb_is_run_and_the_defaults_are_conservative():
    args = rsi_cycle.build_parser().parse_args([])
    assert args.verb == "run"
    assert args.dry_run is False and args.suggest is False
    assert args.probe_run is False and args.max_cycles is None


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
    _write_config(root)
    rc = rsi_cycle.main(["doctor", "--json"], root=root)
    payload = json.loads(capsys.readouterr().out)
    assert rc == 0, payload
    assert payload["ok"] is True
    names = [c["name"] for c in payload["checks"]]
    assert names == [
        "config", "database", "migrations", "rsi_tables", "lease_tables",
        "binaries", "origin_remote", "harvest_rules", "critic_families",
        "worktrees", "budget", "stop_switch",
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


def _readied(root: Path, monkeypatch) -> None:
    """The temp DB only has 0052 + 0084; the real repo has ~84 migrations."""
    monkeypatch.setattr(
        "mini_ork.stores.migrate.migrate_status", lambda *a, **kw: (84, 0, 0, 84))


def _checks(capsys) -> dict:
    return {c["name"]: c for c in json.loads(capsys.readouterr().out)["checks"]}


def test_doctor_flags_a_missing_config_first(root, monkeypatch, capsys):
    # The config check is the first one for a reason: everything after it that
    # needs a budget or a lane family cannot be judged without it.
    _readied(root, monkeypatch)
    assert rsi_cycle.main(["doctor", "--json"], root=root) == 1
    check = _checks(capsys)["config"]
    assert check["ok"] is False
    assert "rsi-cycle" in check["detail"]


def test_doctor_flags_the_stop_switch(root, db, monkeypatch, capsys):
    _readied(root, monkeypatch)
    _write_config(root)
    stop = db.parent / "rsi" / "DISABLED"
    stop.parent.mkdir(parents=True)
    stop.write_text("collapse: score_rise=0.2 anchor_drop=0.3\n", encoding="utf-8")
    rsi_cycle.main(["doctor", "--json"], root=root)
    check = _checks(capsys)["stop_switch"]
    assert check["ok"] is False
    assert "collapse: score_rise=0.2" in check["detail"]


def test_doctor_without_an_origin_fails_when_require_remote_is_true(root, db, monkeypatch,
                                                                   capsys):
    _readied(root, monkeypatch)
    _write_config(root)
    subprocess.run(["git", "-C", str(root), "remote", "remove", "origin"], check=True)
    rsi_cycle.main(["doctor", "--json"], root=root)
    check = _checks(capsys)["origin_remote"]
    assert check["ok"] is False
    assert "quarantine" in check["detail"]


def test_doctor_tolerates_a_missing_origin_when_remote_is_not_required(root, db, monkeypatch,
                                                                      capsys):
    _readied(root, monkeypatch)
    _write_config(root, CONFIG + "merge: {require_remote: false}\n")
    subprocess.run(["git", "-C", str(root), "remote", "remove", "origin"], check=True)
    rsi_cycle.main(["doctor", "--json"], root=root)
    check = _checks(capsys)["origin_remote"]
    assert check["ok"] is True
    assert "require_remote is false" in check["detail"]


def test_require_origin_is_a_setup_error_not_a_late_quarantine(root, tmp_path):
    from mini_ork.cli.rsi_cycle import _SetupError, _require_origin

    _require_origin(root)  # the fixture repo has an origin
    bare = _fake_root(tmp_path / "bare")
    subprocess.run(["git", "-C", str(bare), "remote", "remove", "origin"], check=True)
    with pytest.raises(_SetupError, match="no 'origin' remote"):
        _require_origin(bare)


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


# ── the run verb ────────────────────────────────────────────────────────────

CONFIG = """\
version: 1
mode: once
interval_s: 3600
max_cycles: 1
budget: {daily_cap_usd: 50, per_cycle_cap_usd: 20}
queue: {max_candidates: 3, min_severity: 40}
probes: {error_policy: halt_before_spend, allow_live: false}
gates: {pytest_paths: [tests/unit/test_x.py]}
critic: {lanes: [glm_lens, opus_lens]}
decide: {auto_merge: true}
"""


def _write_config(root: Path, text: str = CONFIG) -> Path:
    path = root / "config" / "rsi-cycle.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _outcome(**kw):
    from mini_ork.rsi.cycle import CycleOutcome

    base = {"cycle_id": "rsi-ctest", "status": "merged", "rc": 0,
            "candidate_id": "rsi-abc", "merged_sha": "deadbeef", "detail": "ok"}
    base.update(kw)
    return CycleOutcome(**base)


def _fake_cycle(monkeypatch, outcomes):
    """Replace the state machine; record every request it was handed."""
    seen: list = []
    queue = list(outcomes)

    def fake_run_cycle(req, *, runtime=None):
        seen.append(req)
        return queue.pop(0) if len(queue) > 1 else queue[0]

    monkeypatch.setattr("mini_ork.rsi.cycle.run_cycle", fake_run_cycle)
    return seen


def test_run_dispatches_one_cycle_and_renders_it(root, db, monkeypatch, capsys):
    seen = _fake_cycle(monkeypatch, [_outcome()])
    assert rsi_cycle.main(["--config", str(_write_config(root))], root=root) == 0
    out = capsys.readouterr().out
    assert len(seen) == 1
    assert "— merged" in out and "deadbeef" in out


def test_run_json_is_exactly_one_object(root, db, monkeypatch, capsys):
    _fake_cycle(monkeypatch, [_outcome()])
    assert rsi_cycle.main(["--config", str(_write_config(root)), "--json"], root=root) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["cycle_id"] == "rsi-ctest"
    assert payload["status"] == "merged"
    assert payload["merged_sha"] == "deadbeef"


def test_run_carries_the_flags_into_the_request(root, db, monkeypatch, capsys):
    seen = _fake_cycle(monkeypatch, [_outcome(status="halted", detail="dry run")])
    rc = rsi_cycle.main(
        ["--config", str(_write_config(root)), "--dry-run", "--suggest",
         "--candidate", "rsi-abc", "--max", "1", "--domain", "verification"],
        root=root)
    capsys.readouterr()
    assert rc == 0
    req = seen[0]
    assert req.dry_run is True and req.propose_only is True
    assert req.candidate_id == "rsi-abc" and req.max_candidates == 1
    assert list(req.domains) == ["verification"]
    assert req.config.mode == "once", "a dry run never loops"


def test_a_loop_runs_until_max_cycles(root, db, monkeypatch, capsys):
    monkeypatch.setattr(rsi_cycle.time, "sleep", lambda _s: None)
    seen = _fake_cycle(monkeypatch, [_outcome(status="merged"),
                                     _outcome(status="no_candidate", merged_sha="")])
    rc = rsi_cycle.main(
        ["--config", str(_write_config(root)), "--interval", "1", "--max-cycles", "2",
         "--json"], root=root)
    payload = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert len(seen) == 2
    assert [c["status"] for c in payload["cycles"]] == ["merged", "no_candidate"]


def test_a_halt_stops_the_loop_immediately(root, db, monkeypatch, capsys):
    monkeypatch.setattr(rsi_cycle.time, "sleep", lambda _s: None)
    seen = _fake_cycle(monkeypatch, [_outcome(status="halted", halt_reason="daily_cap")])
    assert rsi_cycle.main(
        ["--config", str(_write_config(root)), "--interval", "1"], root=root) == 0
    capsys.readouterr()
    assert len(seen) == 1


def test_once_beats_a_config_that_says_loop(root, db, monkeypatch, capsys):
    monkeypatch.setattr(rsi_cycle.time, "sleep", lambda _s: None)
    _write_config(root, CONFIG.replace("mode: once", "mode: loop").replace(
        "max_cycles: 1", "max_cycles: 0"))
    seen = _fake_cycle(monkeypatch, [_outcome()])
    assert rsi_cycle.main(["--once"], root=root) == 0
    capsys.readouterr()
    assert len(seen) == 1
    assert seen[0].config.mode == "once" and seen[0].config.max_cycles == 1


def test_budget_lowers_the_per_cycle_cap(root, db, monkeypatch, capsys):
    seen = _fake_cycle(monkeypatch, [_outcome()])
    assert rsi_cycle.main(
        ["--config", str(_write_config(root)), "--budget", "5"], root=root) == 0
    capsys.readouterr()
    assert seen[0].config.budget.per_cycle_cap_usd == 5.0
    assert seen[0].config.budget.daily_cap_usd == 50


def test_budget_may_not_raise_the_daily_cap(root, db, capsys):
    assert rsi_cycle.main(
        ["--config", str(_write_config(root)), "--budget", "500"], root=root) == 2
    assert "exceeds the daily cap" in capsys.readouterr().err


def test_a_missing_config_is_rc2_with_a_remedy(root, db, capsys):
    assert rsi_cycle.main(["--once"], root=root) == 2
    err = capsys.readouterr().err
    assert "no rsi-cycle config" in err and "rsi-cycle.example.yaml" in err


def test_a_cycle_failure_is_rc1(root, db, monkeypatch, capsys):
    def boom(req, *, runtime=None):
        from mini_ork.rsi.cycle import CycleFailure

        raise CycleFailure("the child could not be started")

    monkeypatch.setattr("mini_ork.rsi.cycle.run_cycle", boom)
    assert rsi_cycle.main(["--config", str(_write_config(root))], root=root) == 1
    assert "could not be started" in capsys.readouterr().err


def test_a_setup_failure_from_the_cycle_is_rc2(root, db, monkeypatch, capsys):
    def boom(req, *, runtime=None):
        from mini_ork.rsi.cycle import CycleSetupError

        raise CycleSetupError("lease tables are missing")

    monkeypatch.setattr("mini_ork.rsi.cycle.run_cycle", boom)
    assert rsi_cycle.main(["--config", str(_write_config(root))], root=root) == 2
    assert "lease tables" in capsys.readouterr().err


# ── halt / resume ───────────────────────────────────────────────────────────


def test_halt_writes_the_stop_file_and_resume_removes_it(root, db, capsys):
    assert rsi_cycle.main(["halt"], root=root) == 0
    stop = db.parent / "rsi" / "DISABLED"
    assert stop.is_file()
    capsys.readouterr()

    assert rsi_cycle.main(["resume"], root=root) == 0
    assert not stop.exists()
    assert "removed" in capsys.readouterr().out

    assert rsi_cycle.main(["resume"], root=root) == 0
    assert "not present" in capsys.readouterr().out


def test_halt_does_not_need_a_parsable_config(root, db, capsys):
    """Stopping a loop whose config is broken is the whole point of `halt`."""
    _write_config(root, "budget: {daily_cap_usd: 'not a number'}\n")
    assert rsi_cycle.main(["halt"], root=root) == 0
    assert (db.parent / "rsi" / "DISABLED").is_file()


def test_halt_releases_a_dead_cycles_lease(root, db, capsys):
    """A SIGKILLed cycle leaves its lease live for a full TTL.

    Releasing it here is what makes halt→resume a recovery path instead of an
    operator waiting an hour for an expired row. It is safe to release another
    process's lease *because the stop file is already on disk*: a new cycle
    refuses to start, and a cycle that is still alive halts at its next check.
    """
    from mini_ork.stores import lease as lease_mod

    token = lease_mod.acquire_lease(str(db), "__rsi_cycle__", ttl_s=3600)
    assert token
    assert lease_mod.current_lease(str(db), "__rsi_cycle__") is not None

    assert rsi_cycle.main(["halt"], root=root) == 0
    out = capsys.readouterr().out
    assert "released lease __rsi_cycle__" in out
    assert token in out
    assert lease_mod.current_lease(str(db), "__rsi_cycle__") is None


def test_halt_leaves_no_lease_of_its_own_behind(root, db, capsys):
    """Halt on an idle checkout must not print a release it did not do."""
    assert rsi_cycle.main(["halt"], root=root) == 0
    out = capsys.readouterr().out
    assert "halted: wrote" in out
    assert "released lease" not in out


def test_halt_says_so_when_the_release_does_not_take(root, db, capsys, monkeypatch):
    """A release that fails is reported, never silently swallowed."""
    from mini_ork.stores import lease as lease_mod

    lease_mod.acquire_lease(str(db), "__rsi_cycle__", ttl_s=3600)
    monkeypatch.setattr(lease_mod, "release_lease", lambda *a, **k: False)

    assert rsi_cycle.main(["halt"], root=root) == 0
    out = capsys.readouterr().out
    assert "was not released here" in out
    assert "expires on its own TTL" in out


# ── quarantine ──────────────────────────────────────────────────────────────


def test_quarantine_list_is_empty_on_a_fresh_db(root, db, capsys):
    assert rsi_cycle.main(["quarantine"], root=root) == 0
    assert "no quarantined cycles" in capsys.readouterr().out


def test_quarantine_rejects_an_unknown_action(root, db, capsys):
    assert rsi_cycle.main(["quarantine", "delete", "rsi-c1"], root=root) == 2
    assert "quarantine" in capsys.readouterr().err


def test_quarantine_show_unknown_id_is_rc2(root, db, capsys):
    assert rsi_cycle.main(["quarantine", "show", "rsi-nope"], root=root) == 2
    assert "no such cycle" in capsys.readouterr().err


def test_quarantine_show_without_a_record_is_rc2(root, db, capsys):
    con = ledger.connect(str(db))
    try:
        ledger.start_cycle(con, cycle_id="rsi-c1", now=1, mode="once", dry_run=False)
        ledger.finish_cycle(con, "rsi-c1", now=2, status="quarantined",
                            halt_reason="gate_failed",
                            quarantine_path=str(root / "missing.json"))
    finally:
        con.close()
    assert rsi_cycle.main(["quarantine", "show", "rsi-c1"], root=root) == 2
    assert "quarantine record missing" in capsys.readouterr().err


def test_quarantine_release_drops_the_claims_and_reopens_the_candidate(
        root, db, capsys, monkeypatch):
    """Release means "keep the tree, hand the files back" — not just a status flip.

    Two halves, both load-bearing:

    * the file claims go, or the next cycle rejects the same paths with
      `worktree_claim_conflict` *after* paying for SUGGEST;
    * the candidate reopens, or the queue keeps filtering it as a failure.

    It must also survive a broken config: cleaning up after a quarantine is
    exactly when the config that caused it may be unparsable.
    """
    from mini_ork.rsi import worktree as wt_mod
    from mini_ork.rsi.candidates import Candidate

    released: list[str] = []
    monkeypatch.setattr(wt_mod, "release",
                        lambda primary, slug, **kw: released.append(slug))

    _write_config(root, "budget: {daily_cap_usd: 'not a number'}\n")
    cand = Candidate.build(domain="verification", source="db:V1",
                           kind="data_path_dead", title="t",
                           evidence_key="table:x:y", now=1)
    con = ledger.connect(str(db))
    try:
        ledger.upsert_candidate(con, cand, now=1)
        ledger.set_candidate_status(con, cand.candidate_id, "quarantined", now=2,
                                    reason="gate_failed: lint_failed")
        ledger.start_cycle(con, cycle_id="rsi-c1", now=1, mode="once", dry_run=False)
        ledger.finish_cycle(con, "rsi-c1", now=2, status="quarantined",
                            halt_reason="gate_failed",
                            candidate_id=cand.candidate_id,
                            quarantine_path=str(root / "q.json"))
    finally:
        con.close()

    assert rsi_cycle.main(["quarantine", "release", "rsi-c1"], root=root) == 0
    out = capsys.readouterr().out
    assert "released claims for rsi-c1" in out and "wt/rsi-c1" in out
    assert released == ["rsi-c1"]
    con = ledger.connect(str(db))
    try:
        assert ledger.get_candidate(con, cand.candidate_id)["status"] == "open"
    finally:
        con.close()


# ── revert ──────────────────────────────────────────────────────────────────


def test_revert_without_a_merged_sha_is_rc2(root, db, capsys):
    con = ledger.connect(str(db))
    try:
        ledger.start_cycle(con, cycle_id="rsi-c1", now=1, mode="once", dry_run=False)
        ledger.finish_cycle(con, "rsi-c1", now=2, status="no_candidate")
    finally:
        con.close()
    assert rsi_cycle.main(["revert", "rsi-c1"], root=root) == 2
    assert "nothing to revert" in capsys.readouterr().err


def test_revert_without_an_id_is_rc2(tmp_path, capsys):
    assert rsi_cycle.main(["revert"], root=tmp_path) == 2
    assert "revert <cycle_id>" in capsys.readouterr().err


# ── show: the critic verdicts ───────────────────────────────────────────────


def test_show_renders_each_critic_and_its_first_finding():
    """A split must be legible in the default (non-JSON) output.

    The stored column is a JSON string; a cycle whose panel split is the only
    thing standing between it and a merge should say so on one line per critic,
    not require ``--json`` and a jq pipe.
    """
    raw = json.dumps({"critics": [
        {"alias": "glm", "family": "zai", "approve": True, "blocking_findings": []},
        {"alias": "minimax", "family": "minimax", "approve": False,
         "blocking_findings": ["no producer emits the field", "second finding"]},
    ]})
    lines = rsi_cycle._render_critics(raw)
    assert len(lines) == 2
    assert "approve" in lines[0] and "(zai)" in lines[0]
    assert "REJECT" in lines[1] and "no producer emits the field" in lines[1]
    assert "second finding" not in lines[1], "one line per critic, not a dump"


@pytest.mark.parametrize("raw", [None, "", "{}", "not json", {"critics": None}])
def test_show_survives_a_missing_or_shapeless_panel(raw):
    """``show`` is an operator tool: a bad column must not raise."""
    assert rsi_cycle._render_critics(raw) == []
