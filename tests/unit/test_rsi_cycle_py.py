"""Unit tests: one cycle end to end, with every side effect faked.

The point of injecting a ``Runtime`` is that these tests can prove the *policy*
of an unattended loop without a model, a worktree or a merge:

* it merges only when every gate passes and the panel is unanimous;
* it quarantines rather than discards when a gate is red;
* it halts before spending, not after;
* it never leaves a ``running`` row behind;
* ``--dry-run`` writes nothing and spawns nothing.

Each test names the failure it prevents. A cycle is a spending machine; the
tests are its brakes.
"""
from __future__ import annotations

import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from mini_ork.rsi import cycle as cycle_mod  # noqa: E402
from mini_ork.rsi.candidates import Candidate  # noqa: E402
from mini_ork.rsi.config import from_mapping  # noqa: E402
from mini_ork.rsi.cycle import (  # noqa: E402
    CycleFailure,
    CycleRequest,
    CycleSetupError,
    Implementation,
    Runtime,
    run_cycle,
)
from mini_ork.rsi.gates import CriticVerdict, GateEvidence  # noqa: E402
from mini_ork.rsi.harvest import HarvestReport  # noqa: E402
from mini_ork.rsi.probes import ProbeResult  # noqa: E402
from mini_ork.rsi.select import Proposal, Selection  # noqa: E402
from mini_ork.rsi.worktree import MergeResult  # noqa: E402
from mini_ork.stores import lease, migrate  # noqa: E402

NOW = 1_760_000_000
FILE = "mini_ork/verify/persist.py"


# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------


def _migrate(db_path: Path, tmp_path: Path, names: tuple[str, ...]) -> None:
    migdir = tmp_path / "migrations"
    migdir.mkdir(exist_ok=True)
    for name in names:
        shutil.copy(REPO / "db" / "migrations" / name, migdir / name)
    rc, out = migrate.migrate_apply(str(migdir), dry_run=False, db=str(db_path),
                                    root=str(REPO))
    assert rc == 0, out


def _cfg(**overrides):
    raw = {
        "budget": {"daily_cap_usd": 50, "per_cycle_cap_usd": 20},
        "critic": {"lanes": ["glm_lens", "opus_lens"]},
        "gates": {"pytest_paths": ["tests/unit/test_x.py"]},
        "decide": {"auto_merge": True},
    }
    raw.update(overrides)
    return from_mapping(raw, env={}, strict=True)


@pytest.fixture(autouse=True)
def _isolated_env(tmp_path, monkeypatch):
    """Every kill switch and meter resolves under tmp_path, never the real home."""
    monkeypatch.delenv("MO_RSI_CYCLE_DISABLED", raising=False)
    monkeypatch.setenv("MINI_ORK_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("MINI_ORK_DB", raising=False)


@pytest.fixture
def world(tmp_path):
    db = tmp_path / "state.db"
    _migrate(db, tmp_path, ("0052_run_leases_recovery_requests.sql",
                            "0084_rsi_cycles.sql",
                            "0060_collapse_history.sql"))
    root = tmp_path / "repo"
    (root / "mini_ork" / "verify").mkdir(parents=True)
    (root / FILE).write_text("# target\n", encoding="utf-8")
    return SimpleNamespace(db=db, root=root, tmp=tmp_path, cfg=_cfg())


def _cand(**overrides) -> Candidate:
    kwargs = dict(
        domain="verification",
        source="db:V1",
        kind="data_path_dead",
        title="confidence is never persisted",
        evidence_key="table:verifier_results:confidence-null",
        files=(FILE,),
        success_command="db:V1",
        now=NOW,
    )
    kwargs.update(overrides)
    return Candidate.build(**kwargs)


def _report(cand: Candidate | None = None, *, ok: bool = True,
            skipped: tuple[str, ...] = (),
            closed: tuple[str, ...] = (),
            probes: tuple[ProbeResult, ...] = ()) -> HarvestReport:
    return HarvestReport(
        candidates=() if cand is None else (cand,),
        probes=probes,
        harvest_ok=ok,
        skipped=skipped,
        closed=closed,
        errors=() if ok else ("garden",),
    )


def _impl(root: Path, cand: Candidate, *, cost: float = 2.0) -> Implementation:
    return Implementation(
        worktree=SimpleNamespace(path=root),
        worktree_config=object(),
        changed_files=cand.files,
        diff="diff --git a/x b/x\n",
        verdict={"pass": True, "files_changed": 1},
        child_run_id="child-run",
        cost_usd=cost,
    )


def _evidence(**overrides) -> GateEvidence:
    base = dict(lint_ok=True, pytest_ok=True, static_ran=True, probes_ran=True,
                static_before=(), static_after=(),
                gap_before=True, gap_after=False,
                probe_failures_before=(), probe_failures_after=())
    base.update(overrides)
    return GateEvidence(**base)


class Harness:
    """A runtime where every call is recorded, so 'did not run' is assertable."""

    def __init__(self, world, cand: Candidate | None = None):
        self.world = world
        self.cand = cand if cand is not None else _cand()
        self.calls: list[str] = []
        self.quarantined: list[str] = []
        self.cleaned: list[str] = []
        self.lease_key = world.cfg.lease.key
        self.runtime = Runtime(
            now=lambda: NOW,
            probes=self._probes,
            harvest=self._harvest,
            propose=self._propose,
            implement=self._implement,
            measure=self._measure,
            critics=self._critics,
            spend_24h=self._spend,
            merge=self._merge,
            clean=self._clean,
            quarantine=self._quarantine,
        )

    # --- overridable behaviour
    spend: float = 0.0
    merge_status: str = "merged"
    critic_verdicts: tuple[CriticVerdict, ...] = (
        CriticVerdict("glm", lane="glm_lens", approve=True),
        CriticVerdict("opus", lane="opus_lens", approve=True),
    )
    measurement_overrides: dict = {}

    def _probes(self, req):
        self.calls.append("probes")
        return (ProbeResult("garden", "garden", "ok", 0, 1),)

    def _harvest(self, req, con, now, probes):
        self.calls.append("harvest")
        return _report(self.cand)

    def _propose(self, req, cand):
        self.calls.append("propose")
        return Proposal(candidate_id=cand.candidate_id, files=cand.files,
                        summary="persist the column", root_cause="the producer is unwired",
                        verification_command=cand.success_command)

    def _implement(self, req, cand, selection):
        self.calls.append("implement")
        return _impl(self.world.root, cand)

    def _measure(self, req, cand, impl, selection):
        self.calls.append("measure")
        return _evidence(**self.measurement_overrides)

    def _critics(self, req, cand, impl, selection, report):
        self.calls.append("critics")
        return self.critic_verdicts

    def _spend(self, db):
        self.calls.append("spend")
        return self.spend

    def _merge(self, req, impl):
        self.calls.append("merge")
        return MergeResult(self.merge_status, sha="deadbeef" if self.merge_status == "merged" else "",
                           detail="")

    def _clean(self, req, impl):
        self.calls.append("clean")
        self.cleaned.append(req.cycle_id)

    def _quarantine(self, req, cand, impl, report, reason):
        self.calls.append("quarantine")
        self.quarantined.append(reason)
        return self.world.tmp / f"{req.cycle_id}.json"

    # --- helpers
    def request(self, **overrides) -> CycleRequest:
        kwargs = dict(root=self.world.root, db=self.world.db, config=self.world.cfg,
                      cycle_id="rsi-ctest", propose_only=False, proposal=None)
        kwargs.update(overrides)
        return CycleRequest(**kwargs)

    def run(self, **overrides):
        return run_cycle(self.request(**overrides), runtime=self.runtime)


def _rows(db: Path, sql: str, *args):
    con = sqlite3.connect(str(db))
    con.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in con.execute(sql, args)]
    finally:
        con.close()


def _cycle(db: Path, cycle_id: str = "rsi-ctest") -> dict:
    rows = _rows(db, "SELECT * FROM rsi_cycles WHERE cycle_id = ?", cycle_id)
    assert rows, "the cycle wrote no ledger row"
    return rows[0]


def _candidate_row(db: Path) -> dict:
    rows = _rows(db, "SELECT * FROM rsi_candidates")
    assert len(rows) == 1, rows
    return rows[0]


# --------------------------------------------------------------------------
# The happy path
# --------------------------------------------------------------------------


def test_a_clean_cycle_merges_and_records_the_sha(world):
    h = Harness(world)
    out = h.run()

    assert out.rc == 0
    assert out.status == "merged"
    assert out.merged_sha == "deadbeef"
    row = _cycle(world.db)
    assert row["status"] == "merged"
    assert row["merged_sha"] == "deadbeef"
    assert row["finished_at"] == NOW
    cand = _candidate_row(world.db)
    assert cand["status"] == "merged"
    assert cand["merged_sha"] == "deadbeef"
    assert h.cleaned == [], "a merged worktree is cleaned by the merge script, not the cycle"


def test_the_merge_is_the_last_thing_that_happens(world):
    h = Harness(world)
    h.run()
    assert h.calls == ["spend", "probes", "harvest", "propose", "implement",
                       "measure", "critics", "merge"]
    assert h.calls.index("implement") < h.calls.index("measure") < h.calls.index("merge")


def test_probe_results_are_recorded_per_cycle(world):
    Harness(world).run()
    rows = _rows(world.db, "SELECT probe_id, status FROM rsi_probe_runs")
    assert rows == [{"probe_id": "garden", "status": "ok"}]


# --------------------------------------------------------------------------
# Red gates
# --------------------------------------------------------------------------


def test_a_red_gate_quarantines_instead_of_merging(world):
    h = Harness(world)
    h.measurement_overrides = {"lint_ok": False, "lint_detail": "E999 syntax error"}
    out = h.run()

    assert out.status == "quarantined"
    assert out.rc == 0
    assert "lint_failed" in out.detail
    assert h.quarantined == ["gate_failed"]
    assert "merge" not in h.calls
    assert _candidate_row(world.db)["status"] == "quarantined"
    row = _cycle(world.db)
    assert row["status"] == "quarantined"
    assert "lint_failed" in row["quarantine_reason"]
    assert row["quarantine_path"].endswith("rsi-ctest.json")


def test_quarantine_keeps_the_worktree_by_default(world):
    h = Harness(world)
    h.measurement_overrides = {"pytest_ok": False}
    h.run()
    assert h.cleaned == []
    assert world.cfg.decide.quarantine_keep_worktree is True


def test_quarantine_can_clean_a_throwaway_worktree(world):
    h = Harness(world)
    h.world = SimpleNamespace(**{**world.__dict__,
                                 "cfg": _cfg(decide={"auto_merge": True,
                                                     "quarantine_keep_worktree": False})})
    h.measurement_overrides = {"pytest_ok": False}
    h.run()
    assert h.cleaned == ["rsi-ctest"]


def test_a_split_panel_quarantines_even_when_every_other_gate_passes(world):
    h = Harness(world)
    h.critic_verdicts = (
        CriticVerdict("glm", lane="glm_lens", approve=True),
        CriticVerdict("opus", lane="opus_lens", approve=False, blocking=("no root cause",)),
    )
    out = h.run()
    assert out.status == "quarantined"
    assert "critic_split" in out.detail
    assert "merge" not in h.calls


def test_no_critics_at_all_fails_g10_closed(world):
    """With no panel configured a cycle must not merge on 'nobody objected'."""
    h = Harness(world)
    h.critic_verdicts = ()
    out = h.run()
    assert out.status == "quarantined"
    assert "critic_not_exogenous" in out.detail


def test_auto_merge_absent_quarantines_a_passing_change(world):
    h = Harness(world)
    h.world = SimpleNamespace(**{**world.__dict__,
                                 "cfg": from_mapping({
                                     "budget": {"daily_cap_usd": 50, "per_cycle_cap_usd": 20},
                                     "critic": {"lanes": ["glm_lens", "opus_lens"]},
                                     "gates": {"pytest_paths": ["tests/unit/test_x.py"]},
                                 }, env={}, strict=True)})
    out = h.run()
    assert out.status == "quarantined"
    assert out.halt_reason == "auto_merge_disabled"
    assert "merge" not in h.calls


def test_a_rejected_merge_is_a_quarantine_not_a_crash(world):
    h = Harness(world)
    h.merge_status = "rejected"
    out = h.run()
    assert out.status == "quarantined"
    assert out.halt_reason == "merge_rejected"
    assert _candidate_row(world.db)["status"] == "quarantined"


def test_a_diverged_merge_counts_as_landed(world):
    """``--ff-only`` refusing locally means the push already landed."""
    h = Harness(world)
    h.merge_status = "diverged"
    out = h.run()
    assert out.status == "merged"


# --------------------------------------------------------------------------
# Budget
# --------------------------------------------------------------------------


def test_the_daily_cap_halts_before_probes_or_models(world):
    h = Harness(world)
    h.spend = 50.0
    out = h.run()

    assert out.status == "halted"
    assert out.rc == 0
    assert out.halt_reason == "daily_cap"
    assert h.calls == ["spend"], "nothing may run once the daily cap is reached"
    assert _rows(world.db, "SELECT * FROM rsi_cycles") == []


def test_the_per_cycle_cap_blocks_the_merge(world):
    h = Harness(world)

    def expensive(req, cand, selection):
        return _impl(world.root, cand, cost=25.0)

    h.runtime.implement = expensive
    out = h.run()
    assert out.status == "quarantined"
    assert "budget_exceeded" in out.detail
    assert "merge" not in h.calls


def test_the_per_cycle_cap_is_judged_against_the_child_cost(world):
    """A tight cap still merges a child that fits under it — G11 grades the
    measured cost, not the cap itself."""
    h = Harness(world)
    h.world = SimpleNamespace(**{**world.__dict__, "cfg": _cfg(
        budget={"daily_cap_usd": 50, "per_cycle_cap_usd": 5})})
    out = h.run()
    assert out.status == "merged"


# --------------------------------------------------------------------------
# Lease and kill switches
# --------------------------------------------------------------------------


def test_a_held_lease_is_rc_zero_and_spends_nothing(world):
    lease.acquire_lease(str(world.db), world.cfg.lease.key, owner_token="someone-else")
    h = Harness(world)
    out = h.run()

    assert out.status == "halted"
    assert out.rc == 0
    assert out.halt_reason == "lease_held"
    # The budget check runs before the lease, by design: it costs one query and
    # a halted cycle must never even read a model bill.
    assert h.calls == ["spend"]
    assert _rows(world.db, "SELECT * FROM rsi_cycles") == []


def test_the_lease_is_released_after_the_cycle(world):
    Harness(world).run()
    assert lease.current_lease(str(world.db), world.cfg.lease.key) is None


def test_missing_lease_tables_is_a_setup_error(tmp_path):
    db = tmp_path / "old.db"
    _migrate(db, tmp_path, ("0084_rsi_cycles.sql",))
    root = tmp_path / "repo"
    root.mkdir()
    with pytest.raises(CycleSetupError, match="lease tables"):
        run_cycle(CycleRequest(root=root, db=db, config=_cfg()))


def test_no_database_is_a_setup_error(tmp_path):
    with pytest.raises(CycleSetupError, match="state DB not found"):
        run_cycle(CycleRequest(root=tmp_path, db=tmp_path / "nope.db", config=_cfg()))


def test_the_env_kill_switch_stops_everything(world, monkeypatch):
    monkeypatch.setenv("MO_RSI_CYCLE_DISABLED", "1")
    h = Harness(world)
    out = h.run()
    assert out.halt_reason == "MO_RSI_CYCLE_DISABLED=1"
    assert h.calls == []


def test_the_disabled_file_stops_everything(world):
    (world.tmp / "rsi").mkdir()
    (world.tmp / "rsi" / "DISABLED").write_text("operator stop\n", encoding="utf-8")
    h = Harness(world)
    out = h.run()
    assert out.halt_reason == "rsi/DISABLED file present"
    assert h.calls == []


def test_the_config_switch_stops_everything(world):
    (world.tmp / "rsi").mkdir()
    h = Harness(world)
    h.world = SimpleNamespace(**{**world.__dict__, "cfg": from_mapping({
        "enabled": False,
        "budget": {"daily_cap_usd": 50, "per_cycle_cap_usd": 20},
        "critic": {"lanes": ["glm_lens", "opus_lens"]},
        "gates": {"pytest_paths": ["tests/unit/test_x.py"]},
    }, env={}, strict=True)})
    out = h.run()
    assert out.halt_reason == "enabled: false"
    assert h.calls == []


# --------------------------------------------------------------------------
# Dry run
# --------------------------------------------------------------------------


def test_a_dry_run_writes_zero_rows_and_spawns_nothing(world):
    """The default dry run must not spend: with no SUGGEST recipe wired it
    previews from the candidate's own evidence and stops there."""
    h = Harness(world)
    h.runtime.propose = None
    h.runtime.implement = None
    out = h.run(dry_run=True)

    assert out.rc == 0
    assert out.status == "halted"
    assert out.kickoff
    assert h.cand.candidate_id in out.kickoff or "confidence is never persisted" in out.kickoff
    assert h.calls == ["spend", "probes", "harvest"]
    assert _rows(world.db, "SELECT * FROM rsi_cycles") == []
    assert _rows(world.db, "SELECT * FROM rsi_candidates") == []
    assert _rows(world.db, "SELECT * FROM rsi_probe_runs") == []


def test_a_dry_run_with_suggest_runs_the_panel_but_still_writes_nothing(world):
    """``--dry-run --suggest`` may pay for SUGGEST; it may never implement."""
    h = Harness(world)
    out = h.run(dry_run=True)

    assert out.rc == 0
    assert "propose" in h.calls
    for forbidden in ("implement", "measure", "critics", "merge"):
        assert forbidden not in h.calls
    assert _rows(world.db, "SELECT * FROM rsi_cycles") == []
    assert _rows(world.db, "SELECT * FROM rsi_candidates") == []


# --------------------------------------------------------------------------
# Halts before spend
# --------------------------------------------------------------------------


def test_a_probe_error_halts_before_suggest(world):
    h = Harness(world)

    def broken(req, con, now, probes):
        return _report(None, ok=False)

    h.runtime.harvest = broken
    out = h.run()

    assert out.status == "halted"
    assert out.rc == 1
    assert out.halt_reason == "harvest_incomplete"
    assert "propose" not in h.calls
    assert "implement" not in h.calls
    assert _cycle(world.db)["status"] == "halted"


def test_quarantine_only_policy_continues_and_lets_g1_decide(world):
    h = Harness(world)
    h.world = SimpleNamespace(**{**world.__dict__, "cfg": _cfg(
        probes={"error_policy": "quarantine_only"})})
    h.runtime.harvest = lambda req, con, now, probes: _report(h.cand, ok=False)
    out = h.run()
    assert out.status == "quarantined"
    assert "harvest_incomplete" in out.detail


def test_no_proposal_halts_without_touching_git(world):
    h = Harness(world)
    h.runtime.propose = lambda req, cand: None
    out = h.run()
    assert out.status == "halted"
    assert out.halt_reason == "no_proposal"
    assert out.rc == 0
    assert "implement" not in h.calls


def test_an_empty_diff_is_a_rejection_that_cleans_its_worktree(world):
    h = Harness(world)
    h.runtime.implement = lambda req, cand, sel: Implementation(
        worktree=SimpleNamespace(path=world.root), detail="the child produced no diff")
    out = h.run()
    assert out.status == "rejected"
    assert "no diff" in out.detail
    assert h.cleaned == ["rsi-ctest"]


def test_a_rejected_selection_never_reaches_implement(world):
    h = Harness(world)
    h.runtime.propose = lambda req, cand: Proposal(
        candidate_id=cand.candidate_id, files=("mini_ork/nope.py",),
        summary="invented", root_cause="invented", verification_command=cand.success_command)
    out = h.run()
    assert out.status == "rejected"
    assert "panel_invented_file" in out.detail
    assert "implement" not in h.calls
    assert _candidate_row(world.db)["status"] == "open"


def test_a_crash_finishes_the_row_as_error_and_re_raises(world):
    h = Harness(world)

    def explode(req, cand, selection):
        raise CycleFailure("the child could not be started")

    h.runtime.implement = explode
    with pytest.raises(CycleFailure):
        h.run()
    assert _cycle(world.db)["status"] == "error"
    assert "CycleFailure" in _cycle(world.db)["halt_reason"]


# --------------------------------------------------------------------------
# Harvest bookkeeping
# --------------------------------------------------------------------------


def test_an_empty_harvest_is_rc_zero(world):
    h = Harness(world)
    h.runtime.harvest = lambda req, con, now, probes: _report(None)
    out = h.run()
    assert out.rc == 0
    assert out.status == "no_candidate"
    assert _cycle(world.db)["status"] == "no_candidate"


def test_a_repeated_candidate_increments_times_seen(world):
    h = Harness(world)
    h.run()
    # Second cycle sees the same signature but the row is merged now; the
    # harvest still records the observation.
    h.run(cycle_id="rsi-ctest-2")
    row = _candidate_row(world.db)
    assert row["times_seen"] >= 2


def test_a_skipped_rule_protects_its_own_candidates(world):
    """A gap a DB rule cannot measure is not a gap that was fixed."""
    h = Harness(world, _cand(source="db:L7"))
    h.runtime.propose = lambda req, cand: None  # leaves the row open
    h.run(cycle_id="rsi-ctest-1")
    assert _candidate_row(world.db)["status"] == "open"

    # The second harvest cannot run L7 (its table is absent) and reports nothing.
    h.runtime.harvest = lambda req, con, now, probes: _report(None, skipped=("L7",))
    h.run(cycle_id="rsi-ctest-2")
    assert _candidate_row(world.db)["status"] == "open", (
        "reconcile must not mark an L7 candidate fixed_externally while L7 is blind")


def test_a_skipped_rule_does_not_veto_another_rules_candidates(world):
    """One unreadable rule must not freeze reconciliation for the whole board.

    A home missing a single legacy table would otherwise be stuck forever: every
    harvest skips that rule, and no candidate from any other source could ever
    reach ``fixed_externally``.
    """
    h = Harness(world)  # candidate source is db:V1
    h.runtime.propose = lambda req, cand: None
    h.run(cycle_id="rsi-ctest-1")
    assert _candidate_row(world.db)["status"] == "open"

    h.runtime.harvest = lambda req, con, now, probes: _report(None, skipped=("L7",))
    h.run(cycle_id="rsi-ctest-2")
    assert _candidate_row(world.db)["status"] == "fixed_externally"


def test_an_errored_probe_protects_its_own_candidates(world):
    """A probe that did not reach a verdict carries no evidence either way."""
    h = Harness(world, _cand(source="probe:tests", success_command="probe:tests"))
    h.runtime.propose = lambda req, cand: None
    h.run(cycle_id="rsi-ctest-1")
    assert _candidate_row(world.db)["status"] == "open"

    h.runtime.harvest = lambda req, con, now, probes: _report(
        None, probes=(ProbeResult("tests", "pytest", "error", 1, 1),))
    h.run(cycle_id="rsi-ctest-2")
    assert _candidate_row(world.db)["status"] == "open"


def test_a_full_harvest_marks_a_vanished_candidate_fixed_externally(world):
    h = Harness(world)
    h.runtime.propose = lambda req, cand: None  # leaves the row open
    h.run(cycle_id="rsi-ctest-1")
    assert _candidate_row(world.db)["status"] == "open"

    h.runtime.harvest = lambda req, con, now, probes: _report(None)
    h.run(cycle_id="rsi-ctest-2")
    assert _candidate_row(world.db)["status"] == "fixed_externally"


def test_a_dead_cycles_row_stops_claiming_its_candidate(world):
    """Holding the lease proves no peer is alive; a 'running' row then is a corpse.

    ``view.claimed_ids`` reads ``status = 'running'``, so a killed process vetoes
    its own gap from every later queue. Cycle 5 died on a transient ledger error
    after ten green gates and would otherwise have blocked its candidate forever.
    """
    dead = _cand()
    L = cycle_mod.ledger
    con = sqlite3.connect(str(world.db))
    try:
        L.upsert_candidate(con, dead, now=NOW - 60)
        con.execute(
            "INSERT INTO rsi_cycles (cycle_id, started_at, mode, status, candidate_id,"
            " lease_token) VALUES ('rsi-dead', ?, 'once', 'running', ?, 'tok')",
            (NOW - 60, dead.candidate_id))
        con.commit()
    finally:
        con.close()

    h = Harness(world, dead)
    h.run(cycle_id="rsi-ctest-1")

    con = sqlite3.connect(str(world.db))
    try:
        row = con.execute("SELECT status, halt_reason FROM rsi_cycles"
                          " WHERE cycle_id = 'rsi-dead'").fetchone()
    finally:
        con.close()
    assert row == ("error", "stale_process")
    assert "propose" in h.calls, "the retired row must not keep the candidate out of the queue"


def test_a_closed_rule_still_reconciles(world):
    """A rule that answered "not here" measured its tables; it is not a blind spot.

    ``_GapClosed`` subclasses ``_SkipRule``, so before the split both landed in
    ``skipped``. Every healthy harvest therefore skipped something, reconciliation
    never ran, and no candidate could ever be marked ``fixed_externally`` — which
    is exactly the state the plan's definition of done needs.
    """
    h = Harness(world)
    h.runtime.propose = lambda req, cand: None  # leaves the row open
    h.run(cycle_id="rsi-ctest-1")
    assert _candidate_row(world.db)["status"] == "open"

    h.runtime.harvest = lambda req, con, now, probes: _report(
        None, closed=("L1", "L2", "L7"))
    h.run(cycle_id="rsi-ctest-2")
    assert _candidate_row(world.db)["status"] == "fixed_externally"


def test_a_pinned_candidate_that_is_not_eligible_is_not_a_crash(world):
    h = Harness(world)
    out = h.run(candidate_id="rsi-does-not-exist")
    assert out.rc == 0
    assert out.status == "no_candidate"


# --------------------------------------------------------------------------
# LEARN — what the cycle leaves for the next one
# --------------------------------------------------------------------------


def _seed_history(db: Path, rows: tuple[tuple[float, float], ...]) -> None:
    con = sqlite3.connect(str(db))
    try:
        for step, (score, anchor) in enumerate(rows):
            con.execute(
                "INSERT INTO collapse_history"
                " (task_class, step, score, anchor, directives, run_id, created_at)"
                " VALUES ('rsi_cycle', ?, ?, ?, 0, ?, ?)",
                (step, score, anchor, f"seed-{step}", NOW - 10 + step),
            )
        con.commit()
    finally:
        con.close()


def test_a_merged_cycle_records_credit_and_a_collapse_measurement(world):
    out = Harness(world).run()
    assert out.status == "merged"

    credit = _rows(world.db, "SELECT * FROM rsi_credit")
    assert len(credit) == 1
    assert credit[0]["cycle_id"] == "rsi-ctest"
    assert credit[0]["source"] == "db:V1"
    assert credit[0]["domain"] == "verification"
    assert credit[0]["outcome"] == "merged"
    assert credit[0]["utility"] == 1.0

    history = _rows(world.db, "SELECT * FROM collapse_history")
    assert len(history) == 1
    assert history[0]["task_class"] == "rsi_cycle"
    assert history[0]["run_id"] == "rsi-ctest"
    assert history[0]["step"] == 0
    assert history[0]["score"] == pytest.approx(1.0)
    assert history[0]["anchor"] == pytest.approx(1.0)


def test_a_quarantined_cycle_earns_no_utility(world):
    h = Harness(world)
    h.measurement_overrides = {"lint_ok": False}
    assert h.run().status == "quarantined"

    credit = _rows(world.db, "SELECT * FROM rsi_credit")
    assert [c["utility"] for c in credit] == [0.0]
    assert [c["outcome"] for c in credit] == ["quarantined"]


def test_learning_accumulates_across_cycles(world):
    h = Harness(world)
    h.run(cycle_id="rsi-one")
    h.run(cycle_id="rsi-two")

    steps = [r["step"] for r in _rows(
        world.db, "SELECT step FROM collapse_history ORDER BY step")]
    assert steps == [0, 1]
    assert len(_rows(world.db, "SELECT * FROM rsi_credit")) == 2

    scores = [r["score"] for r in _rows(
        world.db, "SELECT score FROM collapse_history ORDER BY step")]
    assert scores == [pytest.approx(1.0), pytest.approx(1.0)]


def test_a_dry_run_teaches_nothing(world):
    h = Harness(world)
    out = h.run(dry_run=True)

    assert out.rc == 0
    assert _rows(world.db, "SELECT * FROM rsi_credit") == []
    assert _rows(world.db, "SELECT * FROM collapse_history") == []
    assert _rows(world.db, "SELECT * FROM rsi_cycles") == []


def test_a_cycle_with_no_candidate_measures_the_anchor_but_earns_no_credit(world):
    h = Harness(world)
    h.runtime.harvest = lambda req, con, now, probes: _report(None)
    assert h.run().status == "no_candidate"

    assert _rows(world.db, "SELECT * FROM rsi_credit") == []
    assert len(_rows(world.db, "SELECT * FROM collapse_history")) == 1


def test_a_raising_learner_never_turns_a_merge_into_an_error(world, monkeypatch):
    """Bookkeeping is not allowed to change a decision already made."""
    import mini_ork.rsi.cycle as cycle_mod

    def boom(*args, **kwargs):
        raise RuntimeError("disk on fire")

    monkeypatch.setattr(cycle_mod.learn, "record", boom)
    out = Harness(world).run()

    assert out.status == "merged"
    assert out.rc == 0
    assert _cycle(world.db)["status"] == "merged"


def test_a_collapse_trend_arms_the_stop_and_the_next_cycle_says_why(world):
    _seed_history(world.db, ((0.0, 1.0), (0.0, 1.0), (0.0, 1.0), (0.0, 1.0)))
    h = Harness(world)

    def red_probes(req):
        h.calls.append("probes")
        return (ProbeResult("garden", "garden", "error", 1, 1),)

    h.runtime.probes = red_probes
    assert h.run().status == "merged"

    stop = world.tmp / "rsi" / "DISABLED"
    assert stop.exists(), "a rising score with a dying anchor must arm the stop"

    second = h.run(cycle_id="rsi-after-collapse")
    assert second.rc == 0
    assert second.status == "halted"
    assert second.halt_reason.startswith("collapse:"), second.halt_reason
    assert second.halt_reason == stop.read_text(encoding="utf-8").splitlines()[0]
    assert h.calls.count("probes") == 1, "the halted cycle did not even probe"


def test_the_collapse_reason_does_not_disguise_an_operator_stop(world):
    stop = world.tmp / "rsi"
    stop.mkdir()
    (stop / "DISABLED").write_text("halted by operator\n", encoding="utf-8")

    out = Harness(world).run()

    assert out.status == "halted"
    assert out.halt_reason == "rsi/DISABLED file present"


# --------------------------------------------------------------------------
# G8/G9 measure a frozen input, not a DB the implementer can write
# --------------------------------------------------------------------------


def _seed_l10_gap(db: Path) -> None:
    """The state L10 fires on: themes exist, conflicts never recorded."""
    con = sqlite3.connect(str(db))
    try:
        con.executescript(
            "CREATE TABLE lesson_themes (id TEXT);"
            "INSERT INTO lesson_themes VALUES ('th-1'), ('th-2');"
            "CREATE TABLE lesson_conflicts (id INTEGER);")
        con.commit()
    finally:
        con.close()


def _l10_candidate() -> Candidate:
    return Candidate(
        candidate_id="rsi-l10test", domain="learning", source="db:L10",
        kind="producer_missing", title="lesson_conflicts is empty while themes exist",
        detail="", severity=89, files=("mini_ork/learning/lesson_conflict.py",),
        evidence={"table": "lesson_conflicts", "rows": 0, "themes": 2},
        fix_hint="", success_metric="", success_command="db:L10",
        evidence_key="table:lesson_conflicts:empty",
        signature="sig-l10", first_seen_at=NOW)


def test_g8_reads_the_frozen_db_not_the_live_one(tmp_path):
    """The third live cycle's own fix wrote the rows G8 was measuring.

    The child runs with MINI_ORK_DB pointing at the state DB, so an implementer
    that makes the check *execute* changes the very table the gap is defined on.
    Measured live, both trees then read "already absent" and G8 could not credit
    a correct change. The freeze is taken before IMPLEMENT for exactly this.
    """
    db = tmp_path / "state.db"
    _seed_l10_gap(db)
    req = CycleRequest(root=tmp_path, db=db, config=_cfg(), cycle_id="rsi-ctest")
    frozen = cycle_mod._freeze_db(req)
    assert frozen is not None and frozen.is_file()

    # The child's run: the detector is now on the write path.
    con = sqlite3.connect(str(db))
    try:
        con.execute("INSERT INTO lesson_conflicts (id) VALUES (1)")
        con.commit()
    finally:
        con.close()

    cand = _l10_candidate()
    assert cycle_mod._measured_db(req) == frozen
    assert cycle_mod._gap_at(req, cand, tmp_path) is True, (
        "the gap must still be measurable after the implementer rewrote the "
        "table it is defined on — otherwise G8 credits nothing")
    # Without the freeze the same measurement sees the mutation.
    frozen.unlink()
    assert cycle_mod._gap_at(req, cand, tmp_path) is False


def test_no_freeze_falls_back_to_the_live_db(tmp_path):
    """A DB that cannot be copied must not be reported as a measured one."""
    db = tmp_path / "state.db"
    _seed_l10_gap(db)
    req = CycleRequest(root=tmp_path, db=db, config=_cfg(), cycle_id="rsi-nofreeze")

    assert cycle_mod._freeze_db(req) is not None
    assert cycle_mod._measured_db(req) != Path(db)
    (cycle_mod._baseline_path(req)).unlink()
    assert cycle_mod._measured_db(req) == Path(db)


def test_a_missing_db_freezes_to_none(tmp_path):
    req = CycleRequest(root=tmp_path, db=tmp_path / "absent.db", config=_cfg(),
                       cycle_id="rsi-absent")

    assert cycle_mod._freeze_db(req) is None


def _seed_llm_calls_without_lane(db: Path) -> None:
    """R8's shape: the table exists, the column that makes cost attributable does not."""
    con = sqlite3.connect(str(db))
    try:
        con.executescript("CREATE TABLE llm_calls (id INTEGER);"
                          "INSERT INTO llm_calls VALUES (1), (2);")
        con.commit()
    finally:
        con.close()


def _r8_candidate() -> Candidate:
    return Candidate(
        candidate_id="rsi-r8test", domain="routing", source="db:R8",
        kind="signal_not_propagated", title="llm_calls records no lane",
        detail="", severity=74,
        files=("mini_ork/dispatch/telemetry.py", "mini_ork/dispatch/llm_dispatch.py"),
        evidence={"table": "llm_calls", "column": "lane", "rows": 2},
        fix_hint="", success_metric="llm_calls has a lane column",
        success_command="db:R8",
        evidence_key="table:llm_calls:lane:absent",
        signature="sig-r8", first_seen_at=NOW)


def _post_tree(root: Path, sql: str) -> Path:
    """A tree whose only migration is the patch's."""
    mig = root / "db" / "migrations"
    mig.mkdir(parents=True, exist_ok=True)
    (mig / "0099_add_lane.sql").write_text(sql, encoding="utf-8")
    return root


def test_g8_sees_the_schema_its_patch_adds(tmp_path):
    """A missing column is fixed by a migration; measured on one DB it could not move.

    Before the post-DB, ``db:R8`` was evaluated against the same frozen baseline on
    both sides, so *every* DB-sourced candidate reported ``metric_unchanged`` and no
    cycle could ever merge. The baseline stays the thing nobody writes; the post
    side migrates a copy.
    """
    db = tmp_path / "state.db"
    _seed_llm_calls_without_lane(db)
    req = CycleRequest(root=tmp_path, db=db, config=_cfg(), cycle_id="rsi-ctest")
    assert cycle_mod._freeze_db(req) is not None

    post = _post_tree(tmp_path / "post", "ALTER TABLE llm_calls ADD COLUMN lane TEXT;")
    cand = _r8_candidate()

    assert cycle_mod._gap_at(req, cand, post) is True, "the baseline still has the gap"
    assert cycle_mod._gap_at(req, cand, post, post=True) is False, (
        "after the patch's migration the rule must stop firing")

    # …and the copy is the only thing that moved.
    con = sqlite3.connect(str(cycle_mod._measured_db(req)))
    try:
        columns = {row[1] for row in con.execute("PRAGMA table_info(llm_calls)")}
    finally:
        con.close()
    assert "lane" not in columns


def test_a_data_ratio_gap_stays_unchanged_after_the_post_migration(tmp_path):
    """History is not a schema: a copy with new migrations still has the old rows.

    The honest outcome for such a candidate is ``metric_unchanged`` — it needs a
    producer-side success metric, not a looser gate.
    """
    db = tmp_path / "state.db"
    _seed_l10_gap(db)
    req = CycleRequest(root=tmp_path, db=db, config=_cfg(), cycle_id="rsi-ctest")
    assert cycle_mod._freeze_db(req) is not None

    post = _post_tree(tmp_path / "post", "CREATE TABLE unrelated (id INTEGER);")
    cand = _l10_candidate()

    assert cycle_mod._gap_at(req, cand, post, post=True) is True


def test_the_post_db_falls_back_when_a_migration_fails(tmp_path):
    """A patch whose migration does not apply must not be credited with a fix."""
    db = tmp_path / "state.db"
    _seed_llm_calls_without_lane(db)
    req = CycleRequest(root=tmp_path, db=db, config=_cfg(), cycle_id="rsi-ctest")
    assert cycle_mod._freeze_db(req) is not None

    post = _post_tree(tmp_path / "post", "THIS IS NOT SQL;")
    assert cycle_mod._post_db(req, post) == cycle_mod._measured_db(req)
    assert cycle_mod._gap_at(req, _r8_candidate(), post, post=True) is True


# --------------------------------------------------------------------------
# VALIDATE measures the tree the patch landed, not the child's revert
# --------------------------------------------------------------------------


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(["git", "-C", str(repo), *args], capture_output=True,
                          text=True, timeout=60, check=True)
    return proc.stdout


def _init_repo(repo: Path) -> None:
    repo.mkdir(parents=True)
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "t")


def _landed_but_reverted_repo(repo: Path) -> None:
    """HEAD carries the fix; the working tree is back at ``HEAD~1``.

    This is the live shape between IMPLEMENT and VALIDATE: ``land_patch``
    commits through a private index without checking anything out, and a
    framework-edit child restores its own in-place diff on the way out
    (``[rollback] discard_worktree``). Cycle 12 (2026-10-26) measured exactly
    this tree.
    """
    _init_repo(repo)
    (repo / "a.py").write_text("pre\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "base")

    (repo / "a.py").write_text("post\n", encoding="utf-8")
    (repo / "db" / "migrations").mkdir(parents=True)
    (repo / "db" / "migrations" / "0085_lane.sql").write_text(
        "ALTER TABLE t ADD COLUMN lane TEXT;\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "fix")

    # the child's per-path revert: the bytes go back, the commit stays
    (repo / "a.py").write_text("pre\n", encoding="utf-8")
    (repo / "db" / "migrations" / "0085_lane.sql").unlink()


def test_the_landed_commit_is_written_back_into_the_worktree(tmp_path):
    """Lint and pytest read files. A commit nobody checked out is invisible to them."""
    repo = tmp_path / "wt"
    _landed_but_reverted_repo(repo)
    assert _git(repo, "status", "--porcelain").strip(), "the fixture must start dirty"

    assert cycle_mod._restore_landed(repo, ("a.py", "db/migrations/0085_lane.sql")) is None

    assert (repo / "a.py").read_text(encoding="utf-8") == "post\n"
    assert (repo / "db" / "migrations" / "0085_lane.sql").is_file(), (
        "a patch that adds a file is the case a revert deletes outright")
    assert _git(repo, "status", "--porcelain") == "", "the tree now matches HEAD"


def test_a_commit_that_names_no_files_is_not_measurable(tmp_path):
    """Measuring the unchanged tree would credit a patch that touched nothing."""
    problem = cycle_mod._restore_landed(tmp_path, ())
    assert problem and "nothing to measure" in problem


def test_a_restore_that_fails_is_reported_not_swallowed(tmp_path):
    """A git refusal must reach the ledger as a reason, not as a passing gate."""

    def _runner(argv, **kwargs):
        return subprocess.CompletedProcess(
            argv, 1, "", "error: path 'a.py' does not exist in HEAD\n")

    problem = cycle_mod._restore_landed(tmp_path, ("a.py",), runner=_runner)
    assert problem and "does not exist in HEAD" in problem


def test_a_restore_that_did_not_take_is_caught_by_the_verification(tmp_path):
    """``checkout`` exiting 0 is not proof; the dirty check is."""
    calls: list[list[str]] = []

    def _runner(argv, **kwargs):
        calls.append(list(argv))
        return subprocess.CompletedProcess(argv, 0, "" if len(calls) == 1 else "a.py\n", "")

    problem = cycle_mod._restore_landed(tmp_path, ("a.py",), runner=_runner)
    assert problem and "still differs from HEAD" in problem
    assert "diff" in calls[1], "the second git call must be the verification"


def test_implement_leaves_the_worktree_on_the_landed_commit(world, monkeypatch):
    """The defect a live cycle paid for: gates measured the child's revert.

    ``apply_patch`` here is a real commit followed by a real revert, which is
    what ``land_patch`` + ``[rollback]`` produce together. Without the restore,
    ``impl.changed_files`` is non-empty while the files the gates read are the
    pre-fix ones.
    """
    repo = world.tmp / "wt"
    _init_repo(repo)
    (repo / "a.py").write_text("pre\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "base")

    from mini_ork.rsi import worktree as wt_mod

    monkeypatch.setattr(wt_mod, "claim_and_create", lambda cfg: SimpleNamespace(path=repo))
    monkeypatch.setattr(wt_mod, "assert_worktree_root", lambda wt, runner=None: None)

    def _apply(wt, diff, message=""):
        (repo / "a.py").write_text("post\n", encoding="utf-8")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-qm", "fix")
        (repo / "a.py").write_text("pre\n", encoding="utf-8")  # the child's revert
        return "abc123"

    monkeypatch.setattr(wt_mod, "apply_patch", _apply)

    def _dispatch(req, wt, kickoff, child_home):
        run_dir = child_home / "runs" / "rid"
        run_dir.mkdir(parents=True)
        (run_dir / "framework-edit.diff").write_text(
            "diff --git a/a.py b/a.py\n", encoding="utf-8")
        (run_dir / "verdict.json").write_text(
            '{"pass": true, "files_changed": 1}', encoding="utf-8")
        return "rid", 0, ""

    monkeypatch.setattr(cycle_mod, "_dispatch_child", _dispatch)

    req = CycleRequest(root=world.root, db=world.db, config=world.cfg, cycle_id="rsi-ctest")
    cand = _cand(files=("a.py",))
    selection = Selection(candidate_id=cand.candidate_id, accepted=True, files=("a.py",),
                          verification_command=cand.success_command, kickoff="# k\n")

    impl = cycle_mod._default_implement(req, cand, selection)

    assert impl is not None and impl.landed_something, impl
    assert impl.changed_files == ("a.py",)
    assert (repo / "a.py").read_text(encoding="utf-8") == "post\n", (
        "the gates measure the working tree; it must hold the landed fix")


def test_a_failed_restore_is_an_implementation_detail_not_a_silent_pass(world, monkeypatch):
    """If the tree cannot be restored, the cycle must reject — never measure it."""
    repo = world.tmp / "wt"
    _init_repo(repo)
    (repo / "a.py").write_text("pre\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "base")

    from mini_ork.rsi import worktree as wt_mod

    monkeypatch.setattr(wt_mod, "claim_and_create", lambda cfg: SimpleNamespace(path=repo))
    monkeypatch.setattr(wt_mod, "assert_worktree_root", lambda wt, runner=None: None)

    def _apply(wt, diff, message=""):
        (repo / "a.py").write_text("post\n", encoding="utf-8")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-qm", "fix")
        return "abc123"

    monkeypatch.setattr(wt_mod, "apply_patch", _apply)
    monkeypatch.setattr(cycle_mod, "_restore_landed",
                        lambda wt, paths: "the landed tree could not be restored: boom")

    def _dispatch(req, wt, kickoff, child_home):
        run_dir = child_home / "runs" / "rid"
        run_dir.mkdir(parents=True)
        (run_dir / "framework-edit.diff").write_text(
            "diff --git a/a.py b/a.py\n", encoding="utf-8")
        (run_dir / "verdict.json").write_text('{"pass": true}', encoding="utf-8")
        return "rid", 0, ""

    monkeypatch.setattr(cycle_mod, "_dispatch_child", _dispatch)

    req = CycleRequest(root=world.root, db=world.db, config=world.cfg, cycle_id="rsi-ctest")
    cand = _cand(files=("a.py",))
    selection = Selection(candidate_id=cand.candidate_id, accepted=True, files=("a.py",),
                          verification_command=cand.success_command, kickoff="# k\n")

    impl = cycle_mod._default_implement(req, cand, selection)

    assert impl is not None and not impl.landed_something, impl
    assert "could not be restored" in impl.detail
