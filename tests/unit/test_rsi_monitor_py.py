"""The post-merge canary and the research-refresh launcher, pinned.

Two policies matter here and both are safety properties, not conveniences:

* a sustained canary decline writes the adoption-halt marker and queues a
  *fix-forward* candidate — it never reverts ``main``; and a later canary back
  at the peak clears the marker so the loop can resume on its own;
* a research refresh launches at most once a day per domain, and never at all
  unless ``research.auto_refresh`` is on (or the operator forces it).

Every test builds a throw-away git repo and a throw-away RSI home. The fake
benchmark is registered under the real ``verifier-replay`` name and restored in
``finally``, so no test leaks a runner into another.
"""
from __future__ import annotations

import contextlib
import os
import subprocess
from pathlib import Path

import pytest

from mini_ork.rsi import benchmarks, config, impact_state
from mini_ork.rsi import monitor

NOW = 1_700_000_000


# --------------------------------------------------------------------------
# Fixtures and fakes
# --------------------------------------------------------------------------


def _git(root: Path, *args: str) -> str:
    proc = subprocess.run(["git", "-C", str(root), *args],
                          capture_output=True, text=True, check=False)
    assert proc.returncode == 0, proc.stderr
    return proc.stdout.strip()


@pytest.fixture
def repo(tmp_path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "test@example.invalid")
    _git(root, "config", "user.name", "Monitor Test")
    (root / "SCORE").write_text("0.9\n", encoding="utf-8")
    _git(root, "add", "SCORE")
    _git(root, "commit", "-q", "-m", "seed")
    return root


def _commit_score(root: Path, score: float) -> str:
    """Commit a new canary score and return the resulting HEAD sha.

    ``COUNTER`` changes on every call so committing the same score twice still
    produces a distinct tree (and therefore a distinct cache key).
    """
    counter = root / "COUNTER"
    n = int(counter.read_text(encoding="utf-8")) + 1 if counter.exists() else 1
    counter.write_text(f"{n}\n", encoding="utf-8")
    (root / "SCORE").write_text(f"{score}\n", encoding="utf-8")
    _git(root, "add", "SCORE", "COUNTER")
    _git(root, "commit", "-q", "-m", f"score {score}")
    return _git(root, "rev-parse", "HEAD")


def _score_file_runner(root, items, ctx):
    """A benchmark whose score is the SCORE file of the tree it is handed."""
    score = float((Path(root) / "SCORE").read_text(encoding="utf-8"))
    return benchmarks.BenchResult(
        benchmark="verifier-replay", tree=str(root), scores={"i1": score},
        strata={"i1": "correct"}, guards={}, meta={})


def _one_item(ctx):
    return {"i1": "correct"}


@contextlib.contextmanager
def _fake_benchmark(runner=None, items=None):
    """Replace the real ``verifier-replay`` and restore it afterwards."""
    original = benchmarks._REGISTRY.get("verifier-replay")
    benchmarks.register_benchmark(
        benchmarks.Benchmark("verifier-replay", runner or _score_file_runner,
                             items or _one_item),
        replace=True)
    try:
        yield
    finally:
        if original is None:
            benchmarks._REGISTRY.pop("verifier-replay", None)
        else:
            benchmarks._REGISTRY["verifier-replay"] = original


def _cfg(**monitor_kw):
    return config.CycleConfig(
        budget=config.BudgetConfig(daily_cap_usd=10.0, per_cycle_cap_usd=5.0),
        decide=config.DecideConfig(auto_merge=False),
        research=config.ResearchConfig(enabled=True, domains=("verification",)),
        monitor=config.MonitorConfig(**monitor_kw),
    )


def _refresh_cfg(**research_kw):
    return config.CycleConfig(
        budget=config.BudgetConfig(daily_cap_usd=10.0, per_cycle_cap_usd=5.0),
        decide=config.DecideConfig(auto_merge=False),
        research=config.ResearchConfig(
            enabled=True, domains=("verification",), **research_kw),
    )


def _launch_script(root: Path) -> Path:
    path = root / "recipes" / "verification-technique-review" / "launch.sh"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    return path


def _review(home: Path, *, age_days: int) -> Path:
    path = home / "runs" / "verify-test" / "merged-review.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{}", encoding="utf-8")
    at = NOW - age_days * 86400
    os.utime(path, (at, at))
    return path


# --------------------------------------------------------------------------
# The halt marker
# --------------------------------------------------------------------------


def test_halt_path_lives_under_the_rsi_home(tmp_path):
    assert monitor.halt_path(tmp_path) == tmp_path / "rsi" / monitor.HALT_MARKER


def test_adoption_halted_is_none_without_a_marker(tmp_path):
    assert monitor.adoption_halted(tmp_path) is None


def test_adoption_halted_returns_the_parsed_marker(tmp_path):
    path = monitor.halt_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"bench": "verifier-replay", "peak": 0.9}', encoding="utf-8")
    assert monitor.adoption_halted(tmp_path) == {
        "bench": "verifier-replay", "peak": 0.9}


@pytest.mark.parametrize("body", ["{ not json", "[]", '"a string"'])
def test_a_corrupt_or_shapeless_marker_still_halts(tmp_path, body):
    """Fail closed: an unreadable marker must never read as "clear"."""
    path = monitor.halt_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    assert monitor.adoption_halted(tmp_path) == {"reason": "unreadable marker"}


# --------------------------------------------------------------------------
# bench -> domain
# --------------------------------------------------------------------------


def test_bench_domain_maps_a_known_benchmark():
    assert monitor.bench_domain("verifier-replay") == "verification"


def test_bench_domain_falls_back_to_verification():
    assert monitor.bench_domain("no-such-bench") == "verification"


# --------------------------------------------------------------------------
# The canary tree
# --------------------------------------------------------------------------


def test_canary_tree_is_a_worktree_at_the_sha_and_is_removable(repo, tmp_path):
    sha = _git(repo, "rev-parse", "HEAD")
    home = tmp_path / "home"
    tree = monitor.canary_tree(repo, home, sha)
    try:
        assert tree.is_dir()
        assert tree.name == f"canary-{sha[:12]}"
        assert sha in _git(repo, "worktree", "list", "--porcelain")
        assert (tree / "SCORE").read_text(encoding="utf-8").strip() == "0.9"
    finally:
        monitor.remove_canary_tree(repo, tree)
    assert not tree.exists()


def test_remove_canary_tree_ignores_an_unknown_path(repo, tmp_path):
    """Cleanup must never mask the canary result it is cleaning up after."""
    monitor.remove_canary_tree(repo, tmp_path / "not-a-worktree")


# --------------------------------------------------------------------------
# run_canary
# --------------------------------------------------------------------------


def test_run_canary_scores_head_records_it_and_removes_the_tree(repo, tmp_path):
    sha = _commit_score(repo, 0.75)
    home = tmp_path / "home"
    with _fake_benchmark():
        out = monitor.run_canary(repo, home, _cfg(), "verifier-replay", now=NOW)

    assert out["status"] == "ok"
    assert out["bench"] == "verifier-replay"
    assert out["sha"] == sha
    assert out["score"] == pytest.approx(0.75)
    assert out["peak"] == pytest.approx(0.75)
    assert out["peak_sha"] == sha
    assert out["declines"] == 0 and out["revert"] is False

    state = impact_state.load_state(home)
    canary = state["benchmarks"]["verifier-replay"]["canary"]
    assert canary[-1] == {"sha": sha, "score": pytest.approx(0.75), "at": NOW}
    assert not (home / "rsi" / f"canary-{sha[:12]}").exists()


def test_run_canary_prefers_accuracy_over_the_mean(repo, tmp_path):
    def runner(root, items, ctx):
        return benchmarks.BenchResult(
            benchmark="verifier-replay", tree=str(root),
            scores={"i1": 0.1, "i2": 0.9},
            strata={"i1": "correct", "i2": "correct"}, guards={},
            meta={"accuracy": 0.6})

    home = tmp_path / "home"
    with _fake_benchmark(runner=runner):
        out = monitor.run_canary(repo, home, _cfg(), "verifier-replay", now=NOW)
    assert out["score"] == pytest.approx(0.6)


def test_run_canary_with_no_scores_is_an_error_and_records_nothing(repo, tmp_path):
    def runner(root, items, ctx):
        return benchmarks.BenchResult(
            benchmark="verifier-replay", tree=str(root), scores={},
            strata={}, guards={}, meta={})

    home = tmp_path / "home"
    with _fake_benchmark(runner=runner):
        out = monitor.run_canary(repo, home, _cfg(), "verifier-replay", now=NOW)
    assert out == {"status": "error", "bench": "verifier-replay",
                   "sha": _git(repo, "rev-parse", "HEAD"), "reason": "no scores"}
    assert not impact_state.state_path(home).exists()


def test_a_sustained_decline_halts_adoption_and_never_reverts_main(repo, tmp_path):
    home = tmp_path / "home"
    cfg = _cfg(drop=0.02, consecutive=3)
    shas: list[str] = []
    with _fake_benchmark():
        for score in (0.90, 0.90, 0.85, 0.85, 0.85):
            shas.append(_commit_score(repo, score))
            out = monitor.run_canary(repo, home, cfg, "verifier-replay", now=NOW)

        assert out["status"] == "regressed"
        assert out["declines"] == 3 and out["revert"] is True

        halt = monitor.adoption_halted(home)
        assert halt is not None
        assert halt["bench"] == "verifier-replay"
        assert halt["peak"] == pytest.approx(0.90)
        assert halt["peak_sha"] == shas[0]
        assert halt["latest"] == pytest.approx(0.85)
        assert halt["latest_sha"] == shas[-1]
        assert halt["declines"] == 3

    # Fix forward: the halt is a marker and a candidate, not a revert.
    assert _git(repo, "rev-parse", "HEAD") == shas[-1]


def test_a_canary_back_at_the_peak_clears_the_halt(repo, tmp_path):
    home = tmp_path / "home"
    state = impact_state.new_state()
    impact_state.bench_state(state, "verifier-replay", "seed-corpus")
    for index, score in enumerate((0.90, 0.85, 0.85, 0.85)):
        impact_state.record_canary(
            state, "verifier-replay", sha=f"seed-{index}", score=score, at=NOW)
    impact_state.save_state(home, state)
    marker = monitor.halt_path(home)
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text('{"bench": "verifier-replay", "peak": 0.90}',
                      encoding="utf-8")

    _commit_score(repo, 0.90)
    with _fake_benchmark():
        out = monitor.run_canary(repo, home, _cfg(), "verifier-replay", now=NOW)

    assert out["status"] == "recovered"
    assert monitor.adoption_halted(home) is None


def test_a_short_dip_does_not_write_the_marker(repo, tmp_path):
    home = tmp_path / "home"
    cfg = _cfg(drop=0.02, consecutive=3)
    with _fake_benchmark():
        for score in (0.90, 0.85):
            _commit_score(repo, score)
            out = monitor.run_canary(repo, home, cfg, "verifier-replay", now=NOW)
    assert out["status"] == "ok"
    assert out["declines"] == 1 and out["revert"] is False
    assert monitor.adoption_halted(home) is None


# --------------------------------------------------------------------------
# The fix-forward candidate
# --------------------------------------------------------------------------


def test_regression_candidate_is_fix_forward_against_the_spec_file():
    halt = {
        "bench": "verifier-replay", "peak": 0.90, "peak_sha": "a" * 40,
        "latest": 0.85, "latest_sha": "b" * 40, "declines": 3,
    }
    cand = monitor.regression_candidate(halt, now=NOW)

    assert cand.kind == "regression"
    assert cand.domain == "verification"
    assert cand.source == "monitor:verifier-replay"
    assert "verifier-replay" in cand.title
    assert cand.files == ("recipes/code-fix/verifiers/test.py",)
    assert cand.evidence_key == "canary:verifier-replay:aaaaaaaaaaaa"
    assert "aaaaaaaaaaaa" in cand.fix_hint and "bbbbbbbbbbbb" in cand.fix_hint
    assert "Fix forward; do not revert main." in cand.fix_hint
    assert cand.success_command == "bench:verifier-replay"
    assert cand.evidence == halt
    assert 0 <= cand.severity <= 100


# --------------------------------------------------------------------------
# review age
# --------------------------------------------------------------------------


def test_review_age_days_is_none_without_a_review(tmp_path):
    from mini_ork.rsi import research

    spec = research.DEFAULT_SPECS["verification"]
    assert monitor.review_age_days(tmp_path, spec, now=NOW) is None


def test_review_age_days_counts_whole_days(tmp_path):
    from mini_ork.rsi import research

    _review(tmp_path, age_days=5)
    spec = research.DEFAULT_SPECS["verification"]
    assert monitor.review_age_days(tmp_path, spec, now=NOW) == 5


# --------------------------------------------------------------------------
# maybe_refresh
# --------------------------------------------------------------------------


def test_auto_refresh_off_does_not_launch(repo, tmp_path):
    results = monitor.maybe_refresh(
        repo, tmp_path / "home", _refresh_cfg(auto_refresh=False), now=NOW)
    assert results == [{"domain": "verification", "action": "skipped",
                        "reason": "auto_refresh is off", "age_days": None,
                        "log": None}]


def test_a_current_review_is_not_refreshed(repo, tmp_path):
    home = tmp_path / "home"
    _review(home, age_days=1)
    results = monitor.maybe_refresh(
        repo, home, _refresh_cfg(auto_refresh=True, refresh_days=30), now=NOW)
    assert results[0]["action"] == "skipped"
    assert results[0]["reason"] == "review is current"
    assert results[0]["age_days"] == 1


def test_a_stale_review_launches_once_a_day(repo, tmp_path):
    home = tmp_path / "home"
    _review(home, age_days=40)
    _launch_script(repo)
    cfg = _refresh_cfg(auto_refresh=True, refresh_days=30)

    first = monitor.maybe_refresh(repo, home, cfg, now=NOW)
    assert first[0]["action"] == "launched"
    assert first[0]["age_days"] == 40
    assert Path(first[0]["log"]).parent == home / "rsi"

    record = home / "rsi" / "research-refresh-verification.json"
    assert record.is_file()
    assert "verify-rrl-" in record.read_text(encoding="utf-8")

    second = monitor.maybe_refresh(repo, home, cfg, now=NOW + 3600)
    assert second[0]["action"] == "skipped"
    assert second[0]["reason"] == "launched within the last day"


def test_force_launches_even_with_auto_refresh_off_and_a_fresh_review(repo, tmp_path):
    home = tmp_path / "home"
    _review(home, age_days=0)
    _launch_script(repo)

    results = monitor.maybe_refresh(
        repo, home, _refresh_cfg(auto_refresh=False), now=NOW, force=True)
    assert results[0]["action"] == "launched"


def test_the_domain_filter_limits_the_work(repo, tmp_path):
    results = monitor.maybe_refresh(
        repo, tmp_path / "home", _refresh_cfg(auto_refresh=True),
        now=NOW, force=True, domains=["learning"])
    assert results == []


def test_a_domain_without_a_review_spec_is_skipped_silently(repo, tmp_path):
    cfg = config.CycleConfig(
        budget=config.BudgetConfig(daily_cap_usd=10.0, per_cycle_cap_usd=5.0),
        decide=config.DecideConfig(auto_merge=False),
        research=config.ResearchConfig(enabled=True, domains=("learning",)))
    assert monitor.maybe_refresh(repo, tmp_path / "home", cfg,
                                 now=NOW, force=True) == []
