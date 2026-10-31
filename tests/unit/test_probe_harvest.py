"""Tests for ``mini_ork.learning.probe_harvest``.

Pure inputs and temp dirs: no network, no git, no real ``diagnose``. The
synthetic shas and paths mirror the kickoff's stated vectors.
"""
import hashlib
import json
import os
import re
import sqlite3
from pathlib import Path

import pytest

from mini_ork.learning import probe_harvest
from mini_ork.learning import probe_scorer

NOT_HOLDOUT_SHA = "0123456789abcdef0123456789abcdef01234567"
HOLDOUT_SHA = "a" * 40
NOW = "2026-10-27T00:00:00Z"

FAILED_ID = "mo-aaaaaaaaaa"
SOLVED_ID = "mo-bbbbbbbbbb"
TEST_SPLIT_ID = "mo-cccccccccc"

CANDIDATE_KEYS = {
    "task_id",
    "source",
    "base_sha",
    "fix_sha",
    "test_files",
    "fail_to_pass",
    "pass_to_pass",
    "src_files",
    "problem_statement",
    "cluster",
    "baseline_outcome",
    "baseline_source",
    "holdout",
    "run_ids",
}


def _task(task_id, fix_sha, **over):
    task = {
        "id": task_id,
        "split": "dev",
        "weak_signal": False,
        "base_sha": "c" * 40,
        "fix_sha": fix_sha,
        "test_files": ["tests/unit/test_x.py"],
        "fail_to_pass": ["tests.unit.test_x::test_a"],
        "pass_to_pass": [],
        "src_files": ["mini_ork/foo.py"],
        "problem_statement": "fix the thing for " + task_id,
    }
    task.update(over)
    return task


def _candidate(task_id, fix_sha, **over):
    candidate = {
        "task_id": task_id,
        "source": "heldout-mined",
        "base_sha": "b" * 40,
        "fix_sha": fix_sha,
        "test_files": ["tests/unit/test_x.py"],
        "fail_to_pass": ["tests.unit.test_x::test_a"],
        "pass_to_pass": [],
        "src_files": ["mini_ork/foo.py"],
        "problem_statement": "fix the thing",
        "cluster": "change/R4/verdict",
        "baseline_outcome": 0.0,
        "baseline_source": probe_harvest.DEFAULT_BASELINE,
        "holdout": False,
        "run_ids": [],
    }
    candidate.update(over)
    return candidate


def _render_candidate(**over):
    candidate = {
        "task_id": "mo-aaaaaaaaaa",
        "source": "heldout-mined",
        "base_sha": "b" * 40,
        "fix_sha": "a" * 40,
        "test_files": ["tests/unit/test_x.py"],
        "fail_to_pass": ["test_hidden_f2p"],
        "pass_to_pass": ["test_hidden_p2p"],
        "src_files": ["mini_ork/qux.py"],
        "problem_statement": "see tests/unit/test_x.py and mo-aaaaaaaaaa",
        "cluster": "change/R4/verdict",
        "baseline_outcome": 0.0,
        "baseline_source": probe_harvest.DEFAULT_BASELINE,
        "holdout": False,
        "run_ids": ["r1"],
    }
    candidate.update(over)
    return candidate


RENDER_MD = (
    "---\n"
    "oracle: git\n"
    "---\n"
    "# Frozen probe probe-h01 — code-fix harvested task\n"
    "\n"
    "FROZEN probe kickoff harvested by mini_ork.learning.probe_harvest "
    "from mini-ork's failure history. Never edit it to fit a candidate. "
    "It is graded by hidden tests, not by this text.\n"
    "\n"
    "## Problem\n"
    "\n"
    "see [hidden] and [hidden]\n"
    "\n"
    "## Files in scope\n"
    "\n"
    "- mini_ork/qux.py\n"
    "\n"
    "## Definition of Done\n"
    "\n"
    "Fix the behaviour described above so the repository state at HEAD "
    "satisfies the spec.\n"
)


def _write_e2e_inputs(root: Path, home: Path, *, state_db: bool = True):
    manifest = [
        _task(FAILED_ID, NOT_HOLDOUT_SHA),
        _task(SOLVED_ID, HOLDOUT_SHA),
        _task(TEST_SPLIT_ID, "d" * 40, split="test"),
    ]
    (root / "evals/heldout/mined").mkdir(parents=True)
    (root / "evals/heldout/mined/manifest.json").write_text(json.dumps(manifest))
    (root / "evals/heldout/results").mkdir(parents=True)
    (root / "evals/heldout/results/2026-09-29-baseline-dev.json").write_text(
        json.dumps({FAILED_ID: {"passed": False}, SOLVED_ID: {"passed": True}})
    )
    if state_db:
        home.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(home / "state.db"))
        conn.execute(
            "CREATE TABLE task_runs (id TEXT PRIMARY KEY, status TEXT,"
            " created_at INTEGER)"
        )
        conn.executemany(
            "INSERT INTO task_runs (id, status, created_at)"
            " VALUES (?, 'failed', ?)",
            [
                ("heldout-%s-1" % FAILED_ID, 1),
                ("heldout-%s-2" % FAILED_ID, 2),
            ],
        )
        conn.commit()
        conn.close()


class FakeDiagnose:
    def __init__(self):
        self.calls = []

    def __call__(self, home, run_id, *, write=True, **kwargs):
        self.calls.append((run_id, write))
        return {"layer": "change", "rule_id": "R4", "node": "verdict"}


# ── constants and regex ───────────────────────────────────────────────────


def test_constants():
    assert probe_harvest.HARVESTER == "probe_harvest@1"
    assert probe_harvest.SUPPORTED_RECIPES == ("code-fix", "framework-edit")
    assert probe_harvest.DEFAULT_MAX_NEW == 12
    assert probe_harvest.MAX_PER_CLUSTER == 6
    assert probe_harvest.HOLDOUT_FRACTION == 0.25
    assert probe_harvest.HOLDOUT_SALT == "probe-holdout:"
    assert probe_harvest.PROBE_STEM_PREFIX == "probe-h"
    assert probe_harvest.MAX_STEM_INDEX == 99
    assert probe_harvest.SOLVED == "solved"
    assert probe_harvest.UNMEASURED == "unmeasured-failure"
    assert probe_harvest.DEFAULT_MANIFEST == "evals/heldout/mined/manifest.json"
    assert probe_harvest.DEFAULT_BASELINE == (
        "evals/heldout/results/2026-09-29-baseline-dev.json"
    )
    assert probe_harvest.PYTEST_FILE.pattern == (
        r"(^|/)tests/(.*/)?test_[^/]*\.py$"
    )


def test_pytest_file_regex():
    rx = probe_harvest.PYTEST_FILE
    for path in [
        "tests/test_x.py",
        "tests/unit/test_x.py",
        "pkg/tests/sub/test_x.py",
        "tests/a/b/test_any_thing.py",
    ]:
        assert rx.search(path), path
    for path in [
        "src/x.py",
        "tests/x.py",
        "tests/unit/helper.py",
        "tests/unit/x_test.py",
        "test_x.py",
        "atests/unit/test_x.py",
    ]:
        assert not rx.search(path), path


# ── is_holdout / cluster_key ──────────────────────────────────────────────


def test_is_holdout_vectors():
    assert probe_harvest.is_holdout("a" * 40) is True
    assert probe_harvest.is_holdout(
        "0123456789abcdef0123456789abcdef01234567"
    ) is False
    assert probe_harvest.is_holdout(
        "f487f5014286ff2a7d8c9fa8d129ac45e371720b"
    ) is True
    for sha, bucket in [
        ("a" * 40, 0.15188764198683202),
        ("0123456789abcdef0123456789abcdef01234567", 0.5020780412014574),
        ("f487f5014286ff2a7d8c9fa8d129ac45e371720b", 0.15689958119764924),
    ]:
        own = int(
            hashlib.sha256(("probe-holdout:" + sha).encode()).hexdigest()[:8],
            16,
        ) / 0x100000000
        assert own == pytest.approx(bucket)


def test_cluster_key():
    assert probe_harvest.cluster_key(
        {"layer": "change", "rule_id": "R4", "node": "verdict"}
    ) == "change/R4/verdict"
    assert probe_harvest.cluster_key(
        {"layer": "provider", "rule_id": "R6", "node": None}
    ) == "provider/R6/-"
    assert probe_harvest.cluster_key(None) == "unmeasured-failure"


# ── mined_candidates ──────────────────────────────────────────────────────


def test_mined_candidates_rejection_order_and_fields():
    manifest = [
        _task("task-split", "1" * 40, split="test"),
        _task("task-weak", "2" * 40, weak_signal=True),
        _task("task-src", "3" * 40, test_files=["src/x.py"]),
        _task("task-nof2p", "4" * 40, fail_to_pass=[]),
        _task("task-nobaseline", "5" * 40),
        _task("task-failed", NOT_HOLDOUT_SHA),
        _task("task-solved", HOLDOUT_SHA),
    ]
    baseline = {
        "task-failed": {"passed": False},
        "task-solved": {"passed": True},
    }
    diagnoses = {
        "task-failed": {
            "layer": "change",
            "rule_id": "R4",
            "node": "verdict",
            "run_ids": ["r1", "r2"],
        }
    }
    kept, rejected = probe_harvest.mined_candidates(
        manifest, baseline, diagnoses, baseline_source="my/baseline.json"
    )
    assert rejected == [
        {"task_id": "task-split", "reason": "heldout-test-split"},
        {"task_id": "task-weak", "reason": "weak-signal"},
        {"task_id": "task-src", "reason": "non-pytest-oracle"},
        {"task_id": "task-nof2p", "reason": "no-fail-to-pass"},
        {"task_id": "task-nobaseline", "reason": "no-baseline-measurement"},
    ]
    assert [c["task_id"] for c in kept] == ["task-failed", "task-solved"]
    failed, solved = kept
    assert failed["cluster"] == "change/R4/verdict"
    assert failed["baseline_outcome"] == 0.0
    assert failed["run_ids"] == ["r1", "r2"]
    assert failed["source"] == "heldout-mined"
    assert failed["baseline_source"] == "my/baseline.json"
    assert failed["holdout"] is False
    assert solved["cluster"] == "solved"
    assert solved["baseline_outcome"] == 1.0
    assert solved["holdout"] is True  # "a" * 40 bucket is 0.151...
    for candidate in kept:
        assert set(candidate) == CANDIDATE_KEYS


def test_mined_candidates_non_bool_baseline_and_missing_split():
    kept, rejected = probe_harvest.mined_candidates(
        [
            _task("t-nosplit", "1" * 40, split=None),
            _task("t-strpassed", "2" * 40),
            _task("t-nonepassed", "3" * 40),
        ],
        {
            "t-nosplit": {"passed": True},
            "t-strpassed": {"passed": "yes"},
            "t-nonepassed": {"passed": None},
        },
        {},
    )
    assert kept == []
    assert rejected == [
        {"task_id": "t-nosplit", "reason": "heldout-test-split"},
        {"task_id": "t-strpassed", "reason": "no-baseline-measurement"},
        {"task_id": "t-nonepassed", "reason": "no-baseline-measurement"},
    ]


# ── select ────────────────────────────────────────────────────────────────


def _seven_gate_candidates():
    return [
        _candidate("t-a1", "1" * 40, cluster="change/R4/verdict"),
        _candidate("t-a2", "2" * 40, cluster="change/R4/verdict"),
        _candidate("t-a3", "3" * 40, cluster="change/R4/verdict"),
        _candidate("t-b1", "4" * 40, cluster="change/R5/test"),
        _candidate("t-s1", "5" * 40, cluster="solved"),
        _candidate("t-s2", "6" * 40, cluster="solved"),
        _candidate("t-s3", "7" * 40, cluster="solved"),
    ]


def test_select_round_robin_cluster_cap_binds():
    admitted, rejected = probe_harvest.select(
        _seven_gate_candidates(),
        existing_fix_shas=set(),
        max_new=5,
        max_per_cluster=2,
    )
    assert [c["task_id"] for c in admitted] == [
        "t-a1", "t-b1", "t-s1", "t-a2", "t-s2",
    ]
    assert {r["task_id"]: r["reason"] for r in rejected} == {
        "t-a3": "cluster-cap",
        "t-s3": "cluster-cap",
    }


def test_select_round_robin_max_new_binds():
    admitted, rejected = probe_harvest.select(
        _seven_gate_candidates(),
        existing_fix_shas=set(),
        max_new=5,
        max_per_cluster=3,
    )
    assert [c["task_id"] for c in admitted] == [
        "t-a1", "t-b1", "t-s1", "t-a2", "t-s2",
    ]
    assert {r["task_id"]: r["reason"] for r in rejected} == {
        "t-a3": "max-new",
        "t-s3": "max-new",
    }


def test_select_dedup_and_holdout():
    candidates = [
        _candidate("t-e1", "e" * 40),
        _candidate("t-g1", "1" * 40),
        _candidate("t-g2", "1" * 40),
        _candidate("t-h1", "e" * 40, holdout=True),
        _candidate("t-h2", "9" * 40, holdout=True),
    ]
    admitted, rejected = probe_harvest.select(
        candidates, existing_fix_shas={"e" * 40}, max_new=12
    )
    assert [c["task_id"] for c in admitted] == ["t-g1", "t-h2"]
    assert {r["task_id"]: r["reason"] for r in rejected} == {
        "t-e1": "duplicate",
        "t-g2": "duplicate",
        "t-h1": "duplicate",
    }
    for entry in rejected:
        assert set(entry) == {"task_id", "reason"}


def test_select_holdout_admitted_after_gate_uncapped():
    candidates = [
        _candidate("t-h1", "9" * 40, holdout=True),
        _candidate("t-g1", "1" * 40),
        _candidate("t-h2", "8" * 40, holdout=True),
    ]
    admitted, rejected = probe_harvest.select(
        candidates, existing_fix_shas=set(), max_new=1
    )
    assert [c["task_id"] for c in admitted] == ["t-g1", "t-h1", "t-h2"]
    assert rejected == []


# ── existing_fix_shas / next_stems ────────────────────────────────────────


def test_existing_fix_shas(tmp_path):
    probes = tmp_path / "probes"
    assert probe_harvest.existing_fix_shas(str(probes)) == set()
    (probes / "oracles").mkdir(parents=True)
    (probes / "oracles" / "p1.json").write_text(json.dumps({"fix_sha": "a" * 40}))
    (probes / "oracles" / "p2.json").write_text(
        json.dumps({"fix_sha": "b" * 40, "other": 1})
    )
    (probes / "oracles" / "p3.json").write_text("{not json")
    assert probe_harvest.existing_fix_shas(str(probes)) == {
        "a" * 40, "b" * 40,
    }


def test_next_stems(tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    assert probe_harvest.next_stems(str(empty), 2) == ["probe-h01", "probe-h02"]

    mixed = tmp_path / "mixed"
    mixed.mkdir()
    for name in ("probe-1.md", "probe-h01.md", "probe-h07.md"):
        (mixed / name).write_text("x")
    assert probe_harvest.next_stems(str(mixed), 2) == ["probe-h08", "probe-h09"]

    edge = tmp_path / "edge"
    edge.mkdir()
    (edge / "probe-h98.md").write_text("x")
    assert probe_harvest.next_stems(str(edge), 1) == ["probe-h99"]
    with pytest.raises(ValueError):
        probe_harvest.next_stems(str(edge), 2)


# ── render_probe ──────────────────────────────────────────────────────────


def test_render_probe_frontmatter_gate_and_holdout():
    gate_md, _ = probe_harvest.render_probe(
        _render_candidate(), recipe="code-fix", stem="probe-h01", harvested_at=NOW
    )
    assert gate_md.startswith("---\noracle: git\n---\n")
    hold_md, _ = probe_harvest.render_probe(
        _render_candidate(holdout=True),
        recipe="code-fix",
        stem="probe-h01",
        harvested_at=NOW,
    )
    assert hold_md.startswith("---\noracle: git\nholdout: true\n---\n")


def test_render_probe_markdown_exact():
    markdown, _ = probe_harvest.render_probe(
        _render_candidate(), recipe="code-fix", stem="probe-h01", harvested_at=NOW
    )
    assert markdown == RENDER_MD


def test_render_probe_scrub_hides_secrets():
    markdown, _ = probe_harvest.render_probe(
        _render_candidate(), recipe="code-fix", stem="probe-h01", harvested_at=NOW
    )
    assert "[hidden]" in markdown
    for secret in (
        "tests/unit/test_x.py",
        "mo-aaaaaaaaaa",
        "a" * 40,
        "a" * 10,
        "b" * 40,
        "b" * 10,
        "test_hidden_f2p",
        "test_hidden_p2p",
    ):
        assert secret not in markdown, secret
    assert "## Files in scope" in markdown
    assert markdown.count("- mini_ork/qux.py\n") == 1


def test_render_probe_oracle_exact():
    candidate = _render_candidate()
    _, oracle = probe_harvest.render_probe(
        candidate, recipe="code-fix", stem="probe-h01", harvested_at=NOW
    )
    assert oracle == {
        "schema": 1,
        "base_sha": candidate["base_sha"],
        "fix_sha": candidate["fix_sha"],
        "test_files": candidate["test_files"],
        "fail_to_pass": candidate["fail_to_pass"],
        "pass_to_pass": candidate["pass_to_pass"],
        "provenance": {
            "source": candidate["source"],
            "task_id": candidate["task_id"],
            "run_ids": candidate["run_ids"],
            "cluster": candidate["cluster"],
            "baseline_outcome": candidate["baseline_outcome"],
            "baseline_source": candidate["baseline_source"],
            "recipe": "code-fix",
            "harvested_at": NOW,
            "harvester": "probe_harvest@1",
        },
    }


# ── write_probes ──────────────────────────────────────────────────────────


def test_write_probes_round_trip(tmp_path):
    probes = tmp_path / "probes"
    probes.mkdir()
    candidate = _render_candidate()
    markdown, oracle = probe_harvest.render_probe(
        candidate, recipe="code-fix", stem="probe-h01", harvested_at=NOW
    )
    stems = probe_harvest.write_probes(
        [candidate], str(probes), recipe="code-fix", harvested_at=NOW
    )
    assert stems == ["probe-h01"]
    assert (probes / "probe-h01.md").read_text() == markdown
    raw = (probes / "oracles" / "probe-h01.json").read_text()
    assert raw == json.dumps(oracle, indent=2, sort_keys=True) + "\n"
    assert json.loads(raw) == oracle


def test_write_probes_refuses_existing_target(tmp_path):
    probes = tmp_path / "probes"
    (probes / "oracles").mkdir(parents=True)
    (probes / "oracles" / "probe-h01.json").write_text("{}")
    with pytest.raises(FileExistsError):
        probe_harvest.write_probes(
            [_candidate("t-1", "1" * 40), _candidate("t-2", "2" * 40)],
            str(probes),
            recipe="code-fix",
            harvested_at=NOW,
        )
    assert list(probes.glob("*.md")) == []
    assert [p.name for p in (probes / "oracles").glob("*")] == [
        "probe-h01.json"
    ]


# ── scorer round trip ─────────────────────────────────────────────────────


def test_written_probes_load_in_scorer(tmp_path, monkeypatch):
    """The written probes are shaped exactly as the -a scorer consumes them.

    The full round trip in the kickoff (the apply loop drops the holdout probe
    and ``_probe_oracle`` reads each sidecar) is supplied by kickoff
    ``rsi-t1-probe-harvest-a``. That prerequisite is a separate change and this
    kickoff's file scope excludes ``probe_scorer.py``, so the harvester side of
    the round trip is asserted unconditionally and the -a-specific filtering
    and oracle reads run whenever that API is present.
    """
    root = tmp_path / "root"
    probes = root / "recipes" / "code-fix" / "probes"
    probes.mkdir(parents=True)
    gate1 = _candidate("mo-1111111111", "1" * 40, holdout=False)
    gate2 = _candidate("mo-2222222222", "2" * 40, holdout=False)
    hold = _candidate("mo-3333333333", "3" * 40, holdout=True)
    stems = probe_harvest.write_probes(
        [gate1, gate2, hold], str(probes), recipe="code-fix", harvested_at=NOW
    )
    assert stems == ["probe-h01", "probe-h02", "probe-h03"]

    # Harvester side: every written probe is a git-oracle probe, the holdout
    # marker tracks the candidate, and every sidecar carries its fix_sha.
    by_stem = {"probe-h01": gate1, "probe-h02": gate2, "probe-h03": hold}
    for stem, candidate in by_stem.items():
        frontmatter = probe_scorer._parse_probe_frontmatter(
            str(probes / f"{stem}.md")
        )
        assert frontmatter.get("oracle") == "git"
        assert bool(frontmatter.get("holdout")) is candidate["holdout"]
        oracle = json.loads((probes / "oracles" / f"{stem}.json").read_text())
        assert oracle["fix_sha"] == candidate["fix_sha"]

    # Scorer side: the gate probes are discoverable from the probe directory.
    monkeypatch.setattr(probe_scorer, "_ROOT", str(root))
    monkeypatch.setenv("MO_APPLY_PROBE_GIT_ORACLE", "1")
    monkeypatch.setenv("MO_APPLY_PROBE_MAX_TASKS", "20")
    loaded = probe_scorer._load_probes("code-fix")
    paths = [item[0] if isinstance(item, tuple) else item for item in loaded]
    names = {os.path.basename(path) for path in paths}
    assert {"probe-h01.md", "probe-h02.md"} <= names

    if hasattr(probe_scorer, "_probe_oracle"):
        # kickoff -a merged: assert the full round trip it promises. The
        # holdout probe exists on disk but the apply loop never loads it, and
        # without the flag not one git-oracle probe is scored.
        assert names == {"probe-h01.md", "probe-h02.md"}
        for path in paths:
            oracle = probe_scorer._probe_oracle(path)
            assert oracle["fix_sha"] == by_stem[Path(path).stem]["fix_sha"]
        monkeypatch.delenv("MO_APPLY_PROBE_GIT_ORACLE")
        assert probe_scorer._load_probes("code-fix") == []


# ── load_code_fix_inputs ──────────────────────────────────────────────────


def test_load_code_fix_inputs_missing_file_raises(tmp_path):
    root = tmp_path / "empty-root"
    root.mkdir()
    with pytest.raises(FileNotFoundError):
        probe_harvest.load_code_fix_inputs(str(root), str(tmp_path / "home"))
    (root / "evals" / "heldout" / "mined").mkdir(parents=True)
    (root / "evals" / "heldout" / "mined" / "manifest.json").write_text("[]")
    with pytest.raises(FileNotFoundError):
        probe_harvest.load_code_fix_inputs(str(root), str(tmp_path / "home"))


def test_load_code_fix_inputs_without_state_db(tmp_path):
    root, home = tmp_path / "root", tmp_path / "home"
    _write_e2e_inputs(root, home, state_db=False)
    manifest, baseline, diagnoses = probe_harvest.load_code_fix_inputs(
        str(root), str(home)
    )
    assert [t["id"] for t in manifest] == [
        FAILED_ID, SOLVED_ID, TEST_SPLIT_ID,
    ]
    assert baseline[FAILED_ID] == {"passed": False}
    assert diagnoses == {}


# ── harvest end to end ────────────────────────────────────────────────────


def test_harvest_dry_run(tmp_path, monkeypatch):
    root, home = tmp_path / "root", tmp_path / "home"
    _write_e2e_inputs(root, home)
    fake = FakeDiagnose()
    monkeypatch.setattr("mini_ork.recovery.diagnose.diagnose", fake)

    report = probe_harvest.harvest("code-fix", root=str(root), home=str(home))
    assert report["recipe"] == "code-fix"
    assert report["harvester"] == "probe_harvest@1"
    assert report["n_candidates"] == 2
    assert {"task_id": TEST_SPLIT_ID, "reason": "heldout-test-split"} in (
        report["rejected"]
    )
    assert report["written"] is False
    assert all(entry["stem"] is None for entry in report["admitted"])
    assert not (root / "recipes").exists()
    assert fake.calls == [("heldout-%s-2" % FAILED_ID, False)]
    failed = [e for e in report["admitted"] if e["task_id"] == FAILED_ID]
    assert [e["cluster"] for e in failed] == ["change/R4/verdict"]
    assert report["n_admitted_gate"] == 1
    assert report["n_admitted_holdout"] == 1
    assert report["diagnoses_available"] is True
    assert set(report) == {
        "recipe",
        "harvester",
        "n_existing_probes",
        "n_candidates",
        "admitted",
        "rejected",
        "mined_rejected",
        "n_admitted_gate",
        "n_admitted_holdout",
        "diagnoses_available",
        "written",
    }
    for entry in report["admitted"]:
        assert set(entry) == {
            "task_id", "cluster", "holdout", "baseline_outcome", "stem",
        }


def test_harvest_write_then_second_write_duplicates(tmp_path, monkeypatch):
    root, home = tmp_path / "root", tmp_path / "home"
    _write_e2e_inputs(root, home)
    monkeypatch.setattr("mini_ork.recovery.diagnose.diagnose", FakeDiagnose())

    report = probe_harvest.harvest(
        "code-fix", root=str(root), home=str(home), write=True, now=NOW
    )
    assert report["written"] is True
    probes = root / "recipes" / "code-fix" / "probes"
    assert sorted(p.name for p in probes.glob("*.md")) == [
        "probe-h01.md", "probe-h02.md",
    ]
    assert [entry["stem"] for entry in report["admitted"]] == [
        "probe-h01", "probe-h02",
    ]
    oracle = json.loads((probes / "oracles" / "probe-h01.json").read_text())
    assert oracle["provenance"]["harvested_at"] == NOW
    assert oracle["provenance"]["task_id"] == FAILED_ID
    assert oracle["provenance"]["run_ids"] == [
        "heldout-%s-1" % FAILED_ID, "heldout-%s-2" % FAILED_ID,
    ]
    assert (probes / "probe-h02.md").read_text().startswith(
        "---\noracle: git\nholdout: true\n---\n"
    )

    report2 = probe_harvest.harvest(
        "code-fix", root=str(root), home=str(home), write=True
    )
    assert report2["admitted"] == []
    # The top-up reports the duplicates it weighed, not the permanently
    # ineligible test-split task it will never take.
    assert {r["task_id"]: r["reason"] for r in report2["rejected"]} == {
        FAILED_ID: "duplicate",
        SOLVED_ID: "duplicate",
    }


def test_harvest_default_utc_timestamp(tmp_path):
    root, home = tmp_path / "root", tmp_path / "home"
    _write_e2e_inputs(root, home, state_db=False)
    report = probe_harvest.harvest(
        "code-fix", root=str(root), home=str(home), write=True
    )
    assert report["written"] is True
    oracle = json.loads(
        (
            root / "recipes" / "code-fix" / "probes" / "oracles" /
            "probe-h01.json"
        ).read_text()
    )
    assert re.fullmatch(
        r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z",
        oracle["provenance"]["harvested_at"],
    )


def test_harvest_unsupported_recipe(tmp_path):
    with pytest.raises(ValueError, match="unsupported recipe 'obs-smoke'"):
        probe_harvest.harvest(
            "obs-smoke", root=str(tmp_path), home=str(tmp_path / "home")
        )


def test_harvest_validate_fn_hook(tmp_path):
    root, home = tmp_path / "root", tmp_path / "home"
    _write_e2e_inputs(root, home, state_db=False)

    def validate(candidate):
        if candidate["task_id"] == FAILED_ID:
            return (False, "nope", candidate)
        return (True, "ok", {**candidate, "cluster": "change/R9/validate"})

    report = probe_harvest.harvest(
        "code-fix", root=str(root), home=str(home), validate_fn=validate
    )
    assert {"task_id": FAILED_ID, "reason": "validate:nope"} in report["rejected"]
    solved = [e for e in report["admitted"] if e["task_id"] == SOLVED_ID]
    assert [e["cluster"] for e in solved] == ["change/R9/validate"]


def test_harvest_validate_fn_accepts_two_tuple(tmp_path):
    """A validator with nothing to say on the happy path may return (ok, c)."""
    root, home = tmp_path / "root", tmp_path / "home"
    _write_e2e_inputs(root, home, state_db=False)

    def validate(candidate):
        if candidate["task_id"] == FAILED_ID:
            return (False, "nope", candidate)
        return (True, {**candidate, "cluster": "change/R9/two"})

    report = probe_harvest.harvest(
        "code-fix", root=str(root), home=str(home), validate_fn=validate
    )
    assert report["rejected"] == [
        {"task_id": FAILED_ID, "reason": "validate:nope"}
    ]
    solved = [e for e in report["admitted"] if e["task_id"] == SOLVED_ID]
    assert [e["cluster"] for e in solved] == ["change/R9/two"]


# ── CLI and module shape ──────────────────────────────────────────────────


def test_main_cli_report_matches_stdout(tmp_path, monkeypatch, capsys):
    root, home = tmp_path / "root", tmp_path / "home"
    _write_e2e_inputs(root, home)
    monkeypatch.setattr("mini_ork.recovery.diagnose.diagnose", FakeDiagnose())
    report_path = tmp_path / "report.json"
    rc = probe_harvest.main(
        [
            "--recipe", "code-fix",
            "--root", str(root),
            "--home", str(home),
            "--report", str(report_path),
        ]
    )
    assert rc == 0
    lines = [ln for ln in capsys.readouterr().out.splitlines() if ln.strip()]
    assert len(lines) == 1
    stdout_report = json.loads(lines[0])
    assert stdout_report == json.loads(report_path.read_text())
    assert stdout_report["written"] is False


def test_main_root_home_env_defaults(tmp_path, monkeypatch, capsys):
    root = tmp_path / "root"
    _write_e2e_inputs(root, root / ".mini-ork")
    monkeypatch.setenv("MINI_ORK_ROOT", str(root))
    monkeypatch.delenv("MINI_ORK_HOME", raising=False)
    monkeypatch.setattr("mini_ork.recovery.diagnose.diagnose", FakeDiagnose())
    assert probe_harvest.main(["--recipe", "code-fix"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["recipe"] == "code-fix"
    assert report["n_candidates"] == 2


def test_module_guard_and_no_cli_import():
    source = Path(probe_harvest.__file__).read_text()
    assert 'if __name__ == "__main__":' in source
    assert "sys.exit(main())" in source
    assert "mini_ork.cli" not in source
