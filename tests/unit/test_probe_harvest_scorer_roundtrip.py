"""Harvested probes round-trip through the apply loop's probe scorer.

Pins the contract between kickoff rsi-t1-probe-harvest-b (the writer) and -a
(the git-oracle loader): gate probes load, holdout probes never do, and the
oracle sidecar's test ids use the `<module>::<name>` form the manifest uses and
the scorer validates. Review addition — the run's blind spec fixture used a bare
`test_a` id, which the scorer correctly rejects.
"""
import os

from mini_ork.learning import probe_harvest, probe_scorer

NOW = "2026-10-27T00:00:00Z"


def _cand(task_id, fix_sha, holdout):
    return {
        "task_id": task_id, "source": "heldout-mined",
        "base_sha": "c" * 40, "fix_sha": fix_sha,
        "test_files": ["tests/unit/test_x.py"],
        "fail_to_pass": ["tests.unit.test_x::test_a"],
        "pass_to_pass": ["tests.unit.test_x::test_b"],
        "src_files": ["mini_ork/foo.py"], "problem_statement": "fix the thing",
        "cluster": "change/R4/verdict", "baseline_outcome": 0.0,
        "baseline_source": "evals/heldout/results/2026-09-29-baseline-dev.json",
        "holdout": holdout, "run_ids": [],
    }


def test_gate_probes_load_and_holdout_never_does(tmp_path, monkeypatch):
    root = tmp_path / "root"
    probes = root / "recipes" / "code-fix" / "probes"
    probes.mkdir(parents=True)
    stems = probe_harvest.write_probes(
        [_cand("mo-1", "1" * 40, False), _cand("mo-2", "2" * 40, False),
         _cand("mo-3", "3" * 40, True)],
        str(probes), recipe="code-fix", harvested_at=NOW)
    assert stems == ["probe-h01", "probe-h02", "probe-h03"]
    monkeypatch.setattr(probe_scorer, "_ROOT", str(root))
    monkeypatch.setenv("MO_APPLY_PROBE_GIT_ORACLE", "1")
    monkeypatch.setenv("MO_APPLY_PROBE_MAX_TASKS", "20")
    loaded = [os.path.basename(p[0]) for p in probe_scorer._load_probes("code-fix")]
    assert loaded == ["probe-h01.md", "probe-h02.md"]
    oracle = probe_scorer._probe_oracle(str(probes / "probe-h01.md"))
    assert oracle["fix_sha"] == "1" * 40


def test_git_oracle_probes_stay_off_without_the_flag(tmp_path, monkeypatch):
    root = tmp_path / "root"
    probes = root / "recipes" / "code-fix" / "probes"
    probes.mkdir(parents=True)
    probe_harvest.write_probes([_cand("mo-1", "1" * 40, False)], str(probes),
                               recipe="code-fix", harvested_at=NOW)
    monkeypatch.setattr(probe_scorer, "_ROOT", str(root))
    monkeypatch.delenv("MO_APPLY_PROBE_GIT_ORACLE", raising=False)
    assert probe_scorer._load_probes("code-fix") == []
