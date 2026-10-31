"""Unit tests: the cycle's research preflight wires the monitor into a cycle.

``monitor`` (RRL-6a) can score ``main``, ratchet its history, write the
adoption-halt marker and queue a fix-forward regression. These tests pin the
four properties of the *cycle* side of that wiring:

* with ``research.enabled: false`` — the default — the cycle never runs a
  canary, never launches a refresh and never reads the halt marker;
* a dry run calls nothing even when research is on;
* a refresh or canary failure is reported and ignored, never halting a cycle;
* a halt marker drops the technique candidates and queues the regression
  candidate the monitor built, so this cycle's queue can act on it.

Every monitor call is monkeypatched on the ``mini_ork.rsi.monitor`` module —
the cycle calls through the module, never a bound function, so no subprocess,
git or benchmark runs here.
"""
from __future__ import annotations

import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO = Path(__file__).resolve().parents[2]

from mini_ork.rsi import cycle as cycle_mod  # noqa: E402
from mini_ork.rsi import ledger, monitor  # noqa: E402
from mini_ork.rsi.candidates import Candidate  # noqa: E402
from mini_ork.rsi.config import from_mapping  # noqa: E402
from mini_ork.rsi.cycle import CycleRequest, Runtime  # noqa: E402
from mini_ork.rsi.harvest import HarvestReport  # noqa: E402
from mini_ork.stores import migrate  # noqa: E402

NOW = 1_760_000_000
GAP_FILE = "mini_ork/verify/persist.py"
HALT = {
    "bench": "verifier-replay",
    "peak": 0.6,
    "peak_sha": "a" * 40,
    "latest": 0.5,
    "latest_sha": "b" * 40,
    "declines": 3,
    "at": 1,
}


# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------


def _migrate(db_path: Path, tmp_path: Path) -> None:
    migdir = tmp_path / "migrations"
    migdir.mkdir(exist_ok=True)
    shutil.copy(REPO / "db" / "migrations" / "0084_rsi_cycles.sql",
                migdir / "0084_rsi_cycles.sql")
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
    """Nothing resolves to the real home or a real kill switch."""
    monkeypatch.delenv("MO_RSI_CYCLE_DISABLED", raising=False)
    monkeypatch.setenv("MINI_ORK_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("MINI_ORK_DB", raising=False)


@pytest.fixture
def world(tmp_path):
    db = tmp_path / "state.db"
    _migrate(db, tmp_path)
    root = tmp_path / "repo"
    (root / "mini_ork" / "verify").mkdir(parents=True)
    (root / GAP_FILE).write_text("# target\n", encoding="utf-8")
    return SimpleNamespace(db=db, root=root, tmp=tmp_path)


def _req(world, *, cfg=None, domains=None, dry_run=False) -> CycleRequest:
    return CycleRequest(root=world.root, db=world.db,
                        config=cfg if cfg is not None else _cfg(),
                        cycle_id="rsi-c-test", domains=domains, dry_run=dry_run)


def _rt() -> Runtime:
    return Runtime(now=lambda: NOW)


def _gap() -> Candidate:
    return Candidate.build(
        domain="verification",
        source="db:V1",
        kind="data_path_dead",
        title="confidence is never persisted",
        evidence_key="table:verifier_results:confidence-null",
        files=(GAP_FILE,),
        success_command="db:V1",
        now=NOW,
    )


def _technique() -> Candidate:
    return Candidate.build(
        domain="verification",
        source="research:verification:distillation",
        kind="technique_unadopted",
        title="adopt distillation",
        evidence_key="technique:distillation",
        now=NOW,
    )


def _report(*cands: Candidate) -> HarvestReport:
    return HarvestReport(candidates=tuple(cands), probes=(), harvest_ok=True,
                         skipped=(), errors=())


def _record(monkeypatch, attr: str, result):
    """Replace a monitor entry point and record every call, in order."""
    calls: list[tuple] = []

    def fake(*args, **kwargs):
        calls.append((args, kwargs))
        return result

    monkeypatch.setattr(monitor, attr, fake)
    return calls


# --------------------------------------------------------------------------
# Default off
# --------------------------------------------------------------------------


def test_research_disabled_calls_nothing(world, monkeypatch):
    refresh = _record(monkeypatch, "maybe_refresh", [])
    canary = _record(monkeypatch, "run_canary", {"status": "ok"})
    halted = _record(monkeypatch, "adoption_halted", HALT)
    report = _report(_gap())

    out = cycle_mod._research_preflight(_req(world), _rt(), None, report)

    assert out == report
    assert refresh == [] and canary == []
    # The halt marker is not even read: with research off the marker's verdict
    # is not this cycle's business.
    assert halted == []


def test_dry_run_calls_nothing(world, monkeypatch):
    cfg = _cfg(research={"enabled": True})
    refresh = _record(monkeypatch, "maybe_refresh", [])
    canary = _record(monkeypatch, "run_canary", {"status": "ok"})
    halted = _record(monkeypatch, "adoption_halted", HALT)
    report = _report(_gap())

    out = cycle_mod._research_preflight(_req(world, cfg=cfg, dry_run=True),
                                        _rt(), None, report)

    assert out == report
    assert refresh == [] and canary == []
    assert halted == []


# --------------------------------------------------------------------------
# Enabled, healthy
# --------------------------------------------------------------------------


def test_enabled_scores_the_domain_benchmark_and_refreshes(world, monkeypatch):
    cfg = _cfg(research={"enabled": True})
    refresh = _record(monkeypatch, "maybe_refresh", [
        {"domain": "verification", "action": "launched", "log": "/tmp/r.log"}])
    canary = _record(monkeypatch, "run_canary",
                     {"status": "ok", "score": 0.9, "peak": 0.9})
    monkeypatch.setattr(monitor, "adoption_halted", lambda home: None)
    report = _report(_gap())

    out = cycle_mod._research_preflight(_req(world, cfg=cfg), _rt(), None, report)

    assert out == report
    assert len(refresh) == 1
    assert refresh[0][1]["domains"] is None
    assert len(canary) == 1
    assert canary[0][0][3] == "verifier-replay"


def test_domain_filter_limits_the_canary(world, monkeypatch):
    """A domain the operator left out of ``--domain`` is not scored."""
    cfg = _cfg(research={"enabled": True, "domains": ["verification", "learning"]})
    _record(monkeypatch, "maybe_refresh", [])
    canary = _record(monkeypatch, "run_canary",
                     {"status": "ok", "score": 0.9, "peak": 0.9})
    monkeypatch.setattr(monitor, "adoption_halted", lambda home: None)

    cycle_mod._research_preflight(
        _req(world, cfg=cfg, domains=("learning",)), _rt(), None, _report(_gap()))

    # Only ``verification`` has a DEFAULT_SPECS entry with a benchmark, and it
    # is filtered out by --domain, so nothing is scored.
    assert canary == []


# --------------------------------------------------------------------------
# Failures never halt a cycle
# --------------------------------------------------------------------------


def test_canary_failure_is_swallowed(world, monkeypatch, capsys):
    cfg = _cfg(research={"enabled": True})
    _record(monkeypatch, "maybe_refresh", [])

    def boom(*args, **kwargs):
        raise RuntimeError("benchmark died")

    monkeypatch.setattr(monitor, "run_canary", boom)
    monkeypatch.setattr(monitor, "adoption_halted", lambda home: None)
    report = _report(_gap())

    out = cycle_mod._research_preflight(_req(world, cfg=cfg), _rt(), None, report)

    assert out == report
    assert "canary verifier-replay failed: RuntimeError: benchmark died" in capsys.readouterr().err


def test_refresh_failure_is_swallowed(world, monkeypatch, capsys):
    cfg = _cfg(research={"enabled": True})
    _record(monkeypatch, "run_canary", {"status": "ok"})

    def boom(*args, **kwargs):
        raise RuntimeError("launcher died")

    monkeypatch.setattr(monitor, "maybe_refresh", boom)
    monkeypatch.setattr(monitor, "adoption_halted", lambda home: None)
    report = _report(_gap())

    out = cycle_mod._research_preflight(_req(world, cfg=cfg), _rt(), None, report)

    assert out == report
    assert "research refresh failed: RuntimeError: launcher died" in capsys.readouterr().err


# --------------------------------------------------------------------------
# A halt drops techniques and queues the regression
# --------------------------------------------------------------------------


def test_halt_drops_techniques_and_queues_the_regression(world, monkeypatch):
    cfg = _cfg(research={"enabled": True})
    _record(monkeypatch, "maybe_refresh", [])
    _record(monkeypatch, "run_canary", {"status": "regressed"})
    monkeypatch.setattr(monitor, "adoption_halted", lambda home: dict(HALT))
    gap, technique = _gap(), _technique()
    report = _report(gap, technique)
    assert len(report.candidates) == 2

    con = ledger.connect(str(world.db))
    try:
        out = cycle_mod._research_preflight(_req(world, cfg=cfg), _rt(), con, report)
        rows = con.execute(
            "SELECT candidate_id, source, kind FROM rsi_candidates").fetchall()
    finally:
        con.close()

    kinds = [c.kind for c in out.candidates]
    assert kinds.count("technique_unadopted") == 0
    assert kinds.count("data_path_dead") == 1
    assert kinds.count("regression") == 1
    [reg] = [c for c in out.candidates if c.kind == "regression"]
    assert reg.source == "monitor:verifier-replay"
    # The gap candidate survives untouched; the regression is appended.
    assert gap in out.candidates

    stored = {(row["candidate_id"], row["source"]) for row in rows}
    assert (reg.candidate_id, "monitor:verifier-replay") in stored


def test_halt_without_a_bench_drops_techniques_only(world, monkeypatch):
    """A marker that cannot name a benchmark still halts adoption."""
    cfg = _cfg(research={"enabled": True})
    _record(monkeypatch, "maybe_refresh", [])
    _record(monkeypatch, "run_canary", {"status": "ok"})
    monkeypatch.setattr(monitor, "adoption_halted",
                        lambda home: {"reason": "unreadable marker"})
    gap, technique = _gap(), _technique()

    con = ledger.connect(str(world.db))
    try:
        out = cycle_mod._research_preflight(
            _req(world, cfg=cfg), _rt(), con, _report(gap, technique))
        rows = con.execute("SELECT candidate_id FROM rsi_candidates").fetchall()
    finally:
        con.close()

    assert [c.kind for c in out.candidates] == ["data_path_dead"]
    assert rows == []
