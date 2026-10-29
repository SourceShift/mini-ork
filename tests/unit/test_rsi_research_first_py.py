"""RRL-5b: ``research.first`` must queue techniques, not just reorder them.

``build_queue`` caps each domain's pool at ``queue.per_domain_max`` by severity
*before* anything re-orders the selected list. When the harvest also carries
verification gaps (severity 60–86), those gaps fill the cap and every
``technique_unadopted`` candidate is dropped with ``domain_cap`` — so
``_order_research`` never sees one and a research-first loop works on gaps.

``_queue_for`` fixes that by building the queue from the technique candidates
alone when ``research.first`` is on, falling back to the full candidate set
only when no technique is eligible. Every test here is hermetic: a temp state DB
migrated from the repo's own ``db/migrations``, a temp git repo whose
``origin/main`` resolves (so merged-sha avoidance answers), and faked runtime
edges. No model, network or credentials.

Each test names the failure it prevents.
"""
from __future__ import annotations

import sqlite3
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

import mini_ork
from mini_ork.rsi import cycle
from mini_ork.rsi.candidates import Candidate
from mini_ork.rsi.config import (
    BudgetConfig,
    CycleConfig,
    DecideConfig,
    GatesConfig,
    ResearchConfig,
)
from mini_ork.rsi.harvest import HarvestReport
from mini_ork.rsi.queue import QueuePlan
from mini_ork.stores.migrate import migrate_apply

REPO_ROOT = Path(mini_ork.__file__).resolve().parents[1]
NOW = 1_800_000_000
GAP_FILE = "mini_ork/rsi/harvest.py"
TECH_FILE = "recipes/code-fix/verifiers/test.py"


# --------------------------------------------------------------------------
# Hermetic scaffolding
# --------------------------------------------------------------------------


def _git(path: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(path),
         "-c", "user.email=spec@example.com", "-c", "user.name=spec", *args],
        capture_output=True, text=True, check=True,
    )
    return proc.stdout.strip()


def _git_repo(path: Path) -> None:
    """A repo whose ``origin/main`` resolves, so merged-sha ancestry answers."""
    _git(path, "init", "-q")
    _git(path, "commit", "--allow-empty", "-q", "-m", "test seed")
    _git(path, "update-ref", "refs/remotes/origin/main", _git(path, "rev-parse", "HEAD"))


def _migrated_db(tmp_path: Path) -> Path:
    db = tmp_path / "state" / "rsi.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    rc, lines = migrate_apply(str(REPO_ROOT / "db" / "migrations"),
                              dry_run=False, db=str(db), root=str(REPO_ROOT))
    assert rc == 0, ("migrations failed", lines)
    return db


def _cfg(*, first: bool) -> CycleConfig:
    return CycleConfig(
        budget=BudgetConfig(daily_cap_usd=50.0, per_cycle_cap_usd=20.0),
        decide=DecideConfig(auto_merge=False),
        gates=GatesConfig(pytest_paths=("tests/unit/test_x.py",)),
        research=ResearchConfig(first=first),
    )


def _gap_candidates() -> tuple[Candidate, ...]:
    """Six verification gaps: distinct evidence keys and high severity.

    Six, not five: ``per_domain_max`` defaults to 5, so one is dropped by the
    cap even before the techniques are considered — the condition the fix
    exists for.
    """
    return tuple(
        Candidate.build(
            domain="verification",
            source="db:V3",
            kind="data_path_dead",
            title=f"verification gap {i}",
            evidence_key=f"table:rows{i}:dead",
            now=NOW,
            reach=4,
            files=(GAP_FILE,),
        )
        for i in range(6)
    )


def _technique_candidates() -> tuple[Candidate, ...]:
    return (
        Candidate.build(
            domain="verification", source="research:G01",
            kind="technique_unadopted", title="technique G01",
            evidence_key="research:technique:g01", now=NOW,
            success_command="bench:verifier-replay", files=(TECH_FILE,),
        ),
        Candidate.build(
            domain="verification", source="research:G02",
            kind="technique_unadopted", title="technique G02",
            evidence_key="research:technique:g02", now=NOW,
            success_command="bench:verifier-replay", files=(TECH_FILE,),
        ),
    )


def _report(*cands: Candidate) -> HarvestReport:
    return HarvestReport(candidates=tuple(cands), probes=(), harvest_ok=True,
                         skipped=(), errors=(), closed=())


def _req(root: Path, db: Path, *, first: bool, dry_run: bool = False) -> cycle.CycleRequest:
    return cycle.CycleRequest(root=root, db=db, dry_run=dry_run,
                              config=_cfg(first=first))


def _seed_merged(con: sqlite3.Connection, root: Path, techs) -> None:
    """Ledger rows marking each technique's signature ``merged`` into origin/main.

    ``avoidance`` only drops a merged signature when it can prove the merge is
    an ancestor of ``origin/main``, so the row carries the seed commit's sha.
    """
    sha = _git(root, "rev-parse", "refs/remotes/origin/main")
    for cand in techs:
        con.execute(
            "INSERT INTO rsi_candidates ("
            "  candidate_id, signature, domain, source, kind, severity,"
            "  blast_radius, files_json, evidence_key, evidence_json, title,"
            "  first_seen_at, last_seen_at, updated_at, times_seen, status,"
            "  merged_sha)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'merged',?)",
            (cand.candidate_id, cand.signature, cand.domain, cand.source,
             cand.kind, cand.severity, 1, "[]", cand.evidence_key, "{}",
             cand.title, NOW, NOW, NOW, 1, sha),
        )
    con.commit()


@pytest.fixture
def world(tmp_path: Path):
    root = tmp_path / "repo"
    root.mkdir()
    _git_repo(root)
    db = _migrated_db(tmp_path)
    con = sqlite3.connect(str(db))
    con.row_factory = sqlite3.Row
    gaps = _gap_candidates()
    techs = _technique_candidates()
    yield SimpleNamespace(root=root, db=db, con=con, gaps=gaps, techs=techs,
                          report=_report(*gaps, *techs))
    con.close()


# --------------------------------------------------------------------------
# _queue_for
# --------------------------------------------------------------------------


def test_first_false_queue_is_exactly_todays(world):
    """Off is the default; the queue must be byte-for-byte ``_build_queue``."""
    req = _req(world.root, world.db, first=False)
    plan = cycle._queue_for(req, world.con, world.report, NOW)

    assert isinstance(plan, QueuePlan)
    assert plan.top is not None
    assert plan.top.kind == "data_path_dead"
    assert plan == cycle._build_queue(req, world.con, world.report, NOW)


def test_first_true_queues_techniques_alone(world):
    """The domain cap must not drop a technique when a gap floods the domain."""
    req = _req(world.root, world.db, first=True)
    plan = cycle._queue_for(req, world.con, world.report, NOW)

    assert plan.top is not None
    assert plan.top.kind == "technique_unadopted"
    assert {plan.top.candidate_id} <= {c.candidate_id for c in world.techs}
    assert plan.selected
    assert all(item.candidate.kind == "technique_unadopted"
               for item in plan.selected)

    # _order_research is still the next step and only permutes the techniques.
    ordered = cycle._order_research(req, world.con, plan)
    assert {i.candidate.candidate_id for i in ordered.selected} == {
        c.candidate_id for c in world.techs}


def test_first_true_without_techniques_falls_back_to_gaps(world):
    """A research-first queue with no techniques is not an empty cycle."""
    req = _req(world.root, world.db, first=True)
    gaps_only = _report(*world.gaps)
    plan = cycle._queue_for(req, world.con, gaps_only, NOW)

    assert plan.top is not None
    assert plan.top.kind == "data_path_dead"
    assert plan == cycle._build_queue(req, world.con, gaps_only, NOW)


def test_first_true_with_ineligible_techniques_falls_back(world):
    """Techniques that are already merged must hand the cycle back to the gaps.

    A technique-only pool whose every item is avoided yields an empty plan;
    returning that would silently end the cycle as ``no_candidate``.
    """
    _seed_merged(world.con, world.root, world.techs)
    req = _req(world.root, world.db, first=True)
    plan = cycle._queue_for(req, world.con, world.report, NOW)

    assert plan.top is not None
    assert plan.top.kind == "data_path_dead"
    assert plan == cycle._build_queue(req, world.con, world.report, NOW)


# --------------------------------------------------------------------------
# _body wiring
# --------------------------------------------------------------------------


def test_body_uses_queue_for_in_a_dry_run(world, monkeypatch, tmp_path: Path):
    """The QUEUE step must go through ``_queue_for``, not ``_build_queue``.

    A dry run with ``research.first`` on and six gaps in the harvest must pick
    a technique; if ``_body`` still called ``_build_queue`` directly, the cap
    would drop both techniques and the top would be a gap.
    """
    monkeypatch.delenv("MO_RSI_CYCLE_DISABLED", raising=False)
    monkeypatch.setenv("MINI_ORK_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("MINI_ORK_RUN_DIR", str(tmp_path / "run"))
    (tmp_path / "run").mkdir()
    (tmp_path / "worktrees").mkdir()
    monkeypatch.setenv("MINI_ORK_WORKTREES_DIR", str(tmp_path / "worktrees"))

    req = _req(world.root, world.db, first=True, dry_run=True)
    rt = cycle.Runtime(
        now=lambda: NOW,
        probes=lambda _req: (),
        harvest=lambda _req, _con, _now, _probes: world.report,
        spend_24h=lambda _db: 0.0,
    )
    outcome = cycle.run_cycle(req, runtime=rt)

    technique_ids = {c.candidate_id for c in world.techs}
    gap_ids = {c.candidate_id for c in world.gaps}
    assert outcome.candidate_id in technique_ids, (
        f"expected a technique, got {outcome.candidate_id!r} "
        f"(status={outcome.status!r}, detail={outcome.detail!r})")
    assert outcome.candidate_id not in gap_ids
