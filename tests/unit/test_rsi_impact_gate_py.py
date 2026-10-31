"""Unit tests: G12's sealed impact measurement and research blindness.

Every case is hermetic — a fake benchmark registered only for the test, the
state home under ``tmp_path``, and two plain directories standing in for the
base and head trees. Nothing here runs git, a benchmark's real runner, a model
or the network.
"""
from __future__ import annotations

import json
import sqlite3
import stat
from pathlib import Path

import pytest

from mini_ork.rsi import benchmarks, cycle, harvest, impact, impact_state
from mini_ork.rsi.candidates import Candidate
from mini_ork.rsi.config import from_mapping

BENCH = "spec-impact-bench"

STRATA: dict[str, str] = {}
for _i in range(16):
    STRATA[f"h{_i:02d}"] = "hard_negative"
for _i in range(24):
    STRATA[f"c{_i:02d}"] = "correct"
ITEM_IDS = tuple(sorted(STRATA))

BENCH_COMMAND = f"bench:{BENCH}"


def _cfg(**overrides):
    raw = {
        "budget": {"daily_cap_usd": 50, "per_cycle_cap_usd": 20},
        "critic": {"lanes": ["glm_lens", "opus_lens"]},
        "gates": {"pytest_paths": ["tests/unit/test_rsi_impact_gate_py.py"]},
        "decide": {"auto_merge": False},
    }
    raw.update(overrides)
    return from_mapping(raw, env={}, strict=True)


def _request(tmp_path: Path, *, cycle_id: str = "rsi-spec-impact",
             **sections) -> cycle.CycleRequest:
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    repo = tmp_path / "repo"
    repo.mkdir(parents=True, exist_ok=True)
    return cycle.CycleRequest(
        root=repo, db=home / "state.db", config=_cfg(**sections),
        cycle_id=cycle_id)


class _Worktree:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)


def _impl(head: Path) -> cycle.Implementation:
    return cycle.Implementation(
        worktree=_Worktree(head),
        changed_files=("recipes/code-fix/verifiers/test.py",))


def _trees(tmp_path: Path) -> tuple[Path, Path]:
    base = tmp_path / "base"
    head = tmp_path / "head"
    base.mkdir(parents=True, exist_ok=True)
    head.mkdir(parents=True, exist_ok=True)
    (base / "marker.txt").write_text("base\n")
    (head / "marker.txt").write_text("head\n")
    return base, head


def _candidate(success_command: str) -> Candidate:
    return Candidate.build(
        domain="verification", source="research:G01",
        kind="technique_unadopted", title="adopt T1: replay the verifier",
        evidence_key="technique:replay-the-verifier", now=1700000000,
        files=("recipes/code-fix/verifiers/test.py",),
        success_command=success_command)


def _base_scores() -> dict[str, float]:
    return {i: (0.0 if STRATA[i] == "hard_negative" else 1.0) for i in ITEM_IDS}


def _head_scores(broken: tuple[str, ...] = ()) -> dict[str, float]:
    scores = dict(_base_scores())
    for i in ITEM_IDS:
        if STRATA[i] == "hard_negative":
            scores[i] = 1.0
    for i in broken:
        scores[i] = 0.0
    return scores


_ZERO_GUARDS = {"false_reject": 0.0, "no_verdict": 0.0}


def _runner(scores_by_root, guards_by_root):
    def runner(root, items, ctx):
        root = Path(root)
        ids = tuple(items) if items is not None else ITEM_IDS
        return benchmarks.BenchResult(
            benchmark=BENCH, tree=root.name,
            scores={i: float(scores_by_root[root.name].get(i, 0.0)) for i in ids},
            strata={i: STRATA[i] for i in ids},
            guards=dict(guards_by_root[root.name]),
            meta={"reasons": {}})
    return runner


def _register(scores_by_root, guards_by_root=None) -> None:
    if guards_by_root is None:
        guards_by_root = {"base": dict(_ZERO_GUARDS), "head": dict(_ZERO_GUARDS)}
    benchmarks.register_benchmark(
        benchmarks.Benchmark(
            name=BENCH, runner=_runner(scores_by_root, guards_by_root),
            items=lambda ctx: dict(STRATA),
            surface=("recipes/code-fix/verifiers",),
            lower_is_better=("false_reject", "no_verdict")),
        replace=True)


@pytest.fixture
def registry_restored():
    saved = dict(benchmarks._REGISTRY)
    try:
        yield
    finally:
        benchmarks._REGISTRY.clear()
        benchmarks._REGISTRY.update(saved)


def _split(req: cycle.CycleRequest) -> dict[str, list[str]]:
    secret = impact_state.load_secret(req.home)
    items = dict(STRATA)
    state = impact_state.load_state(req.home)
    entry = impact_state.bench_state(state, BENCH, impact_state.corpus_hash(items))
    return impact.split_items(
        items, secret=secret, epoch=entry["epoch"],
        dev_fraction=req.config.impact.dev_fraction, strata=items)


def _state_entry(req: cycle.CycleRequest) -> dict:
    state = json.loads(
        (req.home / "rsi" / "impact" / "state.json").read_text(encoding="utf-8"))
    return state["benchmarks"][BENCH]


# ── _impact ────────────────────────────────────────────────────────────────


def test_impact_is_none_for_a_non_bench_candidate(tmp_path, registry_restored):
    req = _request(tmp_path)
    base, head = _trees(tmp_path)
    assert cycle._impact(req, _candidate("db:V1"), _impl(head), base=base) is None


def test_impact_reports_an_unknown_benchmark(tmp_path, registry_restored):
    req = _request(tmp_path)
    base, head = _trees(tmp_path)
    out = cycle._impact(
        req, _candidate("bench:no-such-benchmark"), _impl(head), base=base)
    assert out is not None
    assert out["status"] == "error"
    assert out["reason"] == "unknown_benchmark"


def test_identical_trees_stop_at_the_dev_stage(tmp_path, registry_restored):
    req = _request(tmp_path)
    base, head = _trees(tmp_path)
    _register({"base": _base_scores(), "head": _base_scores()})
    out = cycle._impact(req, _candidate(BENCH_COMMAND), _impl(head), base=base)
    assert out["status"] == "rejected"
    assert out["stage"] == "dev"
    assert out["reason"] == "dev_no_discordant_pairs"
    assert out["look"] is None
    state_file = req.home / "rsi" / "impact" / "state.json"
    looks = 0
    if state_file.exists():
        looks = int(_state_entry(req)["looks"])
    assert looks == 0


def test_a_real_gain_is_accepted_and_costs_one_sealed_look(
        tmp_path, registry_restored):
    req = _request(tmp_path)
    base, head = _trees(tmp_path)
    _register({"base": _base_scores(), "head": _head_scores()})
    out = cycle._impact(req, _candidate(BENCH_COMMAND), _impl(head), base=base)
    assert out["status"] == "accepted"
    assert out["stage"] == "sealed"
    assert out["look"] == 1
    assert out["alpha"] == pytest.approx(round(0.05 / 3.39, 6), abs=1e-6)
    assert out["reason"] == "accepted"
    written = json.loads(
        (req.home / "rsi" / req.cycle_id / "impact.json").read_text(
            encoding="utf-8"))
    assert written["status"] == "accepted"
    entry = _state_entry(req)
    assert entry["looks"] == 1
    assert entry["accepted"] == 1


def test_a_broken_sealed_item_hits_the_regression_budget(
        tmp_path, registry_restored):
    req = _request(tmp_path)
    base, head = _trees(tmp_path)
    split = _split(req)
    broken = next(i for i in split["sealed"] if STRATA[i] == "correct")
    _register({"base": _base_scores(), "head": _head_scores((broken,))})
    out = cycle._impact(req, _candidate(BENCH_COMMAND), _impl(head), base=base)
    assert out["status"] == "rejected"
    assert out["stage"] == "sealed"
    assert out["reason"] == "regression_budget"


def test_a_guard_violation_is_rejected_despite_better_scores(
        tmp_path, registry_restored):
    req = _request(tmp_path)
    base, head = _trees(tmp_path)
    _register(
        {"base": _base_scores(), "head": _head_scores()},
        {"base": dict(_ZERO_GUARDS),
         "head": {"false_reject": 1.0, "no_verdict": 0.0}})
    out = cycle._impact(req, _candidate(BENCH_COMMAND), _impl(head), base=base)
    assert out["status"] == "rejected"
    assert out["reason"] == "guard_violation"


def test_an_exhausted_budget_stops_before_the_sealed_look(
        tmp_path, registry_restored):
    req = _request(tmp_path)
    base, head = _trees(tmp_path)
    _register({"base": _base_scores(), "head": _head_scores()})
    state_dir = req.home / "rsi" / "impact"
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "state.json").write_text(json.dumps({
        "version": 1,
        "benchmarks": {BENCH: {
            "epoch": 1,
            "corpus": impact_state.corpus_hash(dict(STRATA)),
            "looks": 30, "accepted": 0, "rejected": 0, "canary": []}},
    }), encoding="utf-8")
    out = cycle._impact(req, _candidate(BENCH_COMMAND), _impl(head), base=base)
    assert out["status"] == "rejected"
    assert out["stage"] == "sealed"
    assert out["reason"] == "sealed_budget_exhausted"


def test_public_impact_hides_the_sealed_counts(tmp_path, registry_restored):
    assert cycle.public_impact(None) is None
    req = _request(tmp_path)
    base, head = _trees(tmp_path)
    _register({"base": _base_scores(), "head": _head_scores()})
    out = cycle._impact(req, _candidate(BENCH_COMMAND), _impl(head), base=base)
    public = cycle.public_impact(out)
    assert public is not None
    assert public["sealed"] is None
    assert public["guards"] is None
    assert "flaky" not in public
    assert public["status"] == "accepted"
    assert out["sealed"] is not None
    assert "flaky" in out


def test_the_secret_file_is_owner_only(tmp_path, registry_restored):
    req = _request(tmp_path)
    base, head = _trees(tmp_path)
    _register({"base": _base_scores(), "head": _head_scores()})
    cycle._impact(req, _candidate(BENCH_COMMAND), _impl(head), base=base)
    secret = req.home / "rsi" / "impact" / "secret"
    assert secret.is_file()
    assert stat.S_IMODE(secret.stat().st_mode) == 0o600


# ── research harvest + blindness ───────────────────────────────────────────

REVIEW_ROW = {
    "id": "T1", "technique": "differential replay",
    "mechanism": "score both trees", "adoption_sketch": "run it twice",
    "group_id": "G01", "priority": 5, "verdict": "implement-now",
    "evidence": ["paper-1"], "evidence_strength": "strong", "rank": 1,
}


def test_the_harvest_appends_the_research_candidates(
        tmp_path, monkeypatch, capsys):
    req = _request(tmp_path, research={"enabled": True,
                                       "domains": ["verification"]})
    review_dir = req.home / "runs" / "verify-x"
    review_dir.mkdir(parents=True, exist_ok=True)
    (review_dir / "merged-review.json").write_text(
        json.dumps({"ranked": [REVIEW_ROW]}), encoding="utf-8")

    def fake_harvest(root, con, *, now, cfg=None, probes=()):
        return harvest.HarvestReport(
            candidates=(), probes=tuple(probes), harvest_ok=True,
            skipped=(), errors=())

    monkeypatch.setattr(harvest, "harvest", fake_harvest)
    report = cycle._default_harvest(req, None, 1700000000, ())
    assert report.harvest_ok is True
    techniques = [c for c in report.candidates
                  if c.kind == "technique_unadopted"]
    assert len(techniques) == 1
    assert techniques[0].source.startswith("research:")
    assert techniques[0].success_command == "bench:verifier-replay"
    assert "[rsi] research verification:" in capsys.readouterr().err


def _blind_db(path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(str(path))
    con.execute(
        "CREATE TABLE rsi_candidates ("
        "  candidate_id TEXT PRIMARY KEY, source TEXT NOT NULL,"
        "  kind TEXT NOT NULL, status TEXT NOT NULL)")
    con.executemany(
        "INSERT INTO rsi_candidates VALUES (?, ?, ?, ?)",
        [("rsi-1", "research:G01", "technique_unadopted", "open"),
         ("rsi-2", "research:G02", "technique_unadopted", "merged"),
         ("rsi-3", "db:V1", "producer_missing", "open")])
    con.commit()
    return con


def _empty_report(candidates=()) -> harvest.HarvestReport:
    return harvest.HarvestReport(
        candidates=tuple(candidates), probes=(), harvest_ok=True,
        skipped=(), errors=())


def test_blind_sources_keep_open_research_rows_when_none_were_harvested(
        tmp_path):
    con = _blind_db(tmp_path / "blind.db")
    blind = cycle._blind_sources(_empty_report(), con)
    assert "research:G01" in blind
    assert "research:G02" not in blind
    assert "db:V1" not in blind


def test_a_harvested_technique_makes_the_research_source_visible(tmp_path):
    con = _blind_db(tmp_path / "blind.db")
    blind = cycle._blind_sources(
        _empty_report([_candidate(BENCH_COMMAND)]), con)
    assert "research:G01" not in blind


def test_a_broken_ledger_query_adds_nothing(tmp_path):
    con = sqlite3.connect(str(tmp_path / "empty.db"))
    report = harvest.HarvestReport(
        candidates=(), probes=(), harvest_ok=True, skipped=("V1",), errors=())
    blind = cycle._blind_sources(report, con)
    assert "db:V1" in blind
