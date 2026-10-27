"""Git-backed tests for ``mini_ork.learning.probe_harvest``.

Hermetic: a throwaway git repo, a throwaway home, no network, no real
``diagnose``. Every test pins a clause from kickoff rsi-t1-probe-harvest-c:
candidates mined by an older interpreter are re-validated in git, and failed
framework-edit runs become candidates from the commit their named test landed
on.
"""
import json
import shutil
import sqlite3
import subprocess
import types
from pathlib import Path

import pytest

from mini_ork.learning import probe_harvest

K1_TEXT = "Fix add.\n\n## Files in scope\n- calc.py\n- tests/test_calc.py (new)\n"

# The fixture test file from kickoff -a: test_add fails on base (a - b) and on
# the side commit (a * b), passes on the landing (a + b). test_add_zero passes
# everywhere, which is what the "fix-not-green:1" case requires.
TEST_CALC = (
    "import os, sys\n"
    "sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))\n"
    "from calc import add\n"
    "\n"
    "def test_add():\n"
    "    assert add(2, 3) == 5\n"
    "\n"
    "def test_add_zero():\n"
    "    assert add(0, 0) == 0\n"
)


@pytest.fixture(autouse=True)
def harness(tmp_path, monkeypatch):
    """Set the scratch dir and fake ``diagnose`` for every test."""
    monkeypatch.setenv("MO_APPLY_PROBE_SCRATCH_DIR", str(tmp_path / "scratch"))
    layers: dict[str, str] = {}

    def fake_diagnose(home, run_id, *, write=True, allow_model=False):
        assert write is False, "harvest must call diagnose with write=False"
        return {
            "layer": layers.get(run_id, "change"),
            "rule_id": "R5",
            "node": "test_verifier",
        }

    monkeypatch.setattr("mini_ork.recovery.diagnose.diagnose", fake_diagnose)
    return types.SimpleNamespace(layers=layers)


def _git(root, *args):
    subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        capture_output=True,
        text=True,
    )


def _git_out(root, *args) -> str:
    proc = subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return proc.stdout.strip()


def _commit(root, message) -> str:
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", message)
    return _git_out(root, "rev-parse", "HEAD")


def _build_repo(tmp_path, *, with_kickoff=False):
    """``(root, c1, c2, c3)``: base, landing on main, side branch, HEAD == c2."""
    root = tmp_path / "fw"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "checkout", "-q", "-b", "main")
    _git(root, "config", "user.email", "spec@example.invalid")
    _git(root, "config", "user.name", "Spec Tests")

    (root / "README").write_text("x\n")
    _commit(root, "init")

    if with_kickoff:
        (root / "kickoffs" / "auto").mkdir(parents=True)
        (root / "kickoffs" / "auto" / "k.md").write_text(
            "v1 tests/test_calc.py\n"
        )
    (root / "calc.py").write_text("def add(a, b):\n    return a - b\n")
    c1 = _commit(root, "base")

    (root / "calc.py").write_text("def add(a, b):\n    return a + b\n")
    (root / "tests").mkdir()
    (root / "tests" / "test_calc.py").write_text(TEST_CALC)
    if with_kickoff:
        (root / "kickoffs" / "auto" / "k.md").write_text(
            "v2 tests/test_calc.py\n"
        )
    c2 = _commit(root, "landing")

    _git(root, "checkout", "-q", "-b", "side")
    (root / "calc.py").write_text("def add(a, b):\n    return a * b\n")
    c3 = _commit(root, "side")
    _git(root, "checkout", "-q", "main")
    assert _git_out(root, "rev-parse", "HEAD") == c2
    return str(root), c1, c2, c3


def _row(
    rid,
    *,
    kickoff,
    created,
    recipe="framework-edit",
    status="failed",
    baseline_ref=None,
):
    return {
        "id": rid,
        "recipe": recipe,
        "status": status,
        "kickoff_path": kickoff,
        "created_at": created,
        "baseline_ref": baseline_ref,
        "fixture": baseline_ref is not None,
    }


def _home(tmp_path, rows) -> str:
    """A temp home with ``state.db`` and one pre-impl fixture per flagged row."""
    home = tmp_path / "home"
    (home / "runs").mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(home / "state.db")
    try:
        conn.execute(
            "CREATE TABLE task_runs (id TEXT PRIMARY KEY, recipe TEXT,"
            " status TEXT, kickoff_path TEXT, created_at INTEGER)"
        )
        for row in rows:
            conn.execute(
                "INSERT INTO task_runs VALUES (?, ?, ?, ?, ?)",
                (
                    row["id"],
                    row["recipe"],
                    row["status"],
                    row["kickoff_path"],
                    row["created_at"],
                ),
            )
            if row.get("fixture"):
                fixture_dir = home / "runs" / row["id"] / "pre-impl-fixture"
                fixture_dir.mkdir(parents=True, exist_ok=True)
                (fixture_dir / "MANIFEST.json").write_text(
                    json.dumps(
                        {
                            "run_id": row["id"],
                            "baseline_ref": row["baseline_ref"],
                        }
                    )
                )
        conn.commit()
    finally:
        conn.close()
    return str(home)


def _scratch_is_empty(tmp_path) -> bool:
    scratch = tmp_path / "scratch"
    return not scratch.exists() or not any(scratch.iterdir())


def _candidate(c1, c2, text, **overrides):
    base = {
        "task_id": f"fe-{c2[:10]}",
        "source": "failed-run",
        "cluster": "change/R5/test_verifier",
        "baseline_outcome": 0.0,
        "baseline_source": "task_runs.status=failed",
        "holdout": probe_harvest.is_holdout(c2),
        "run_ids": ["fe-ok-1"],
        "problem_statement": text,
        "base_sha": c1,
        "fix_sha": c2,
        "test_files": ["tests/test_calc.py"],
        "fail_to_pass": ["tests.test_calc::test_add"],
        "pass_to_pass": ["tests.test_calc::test_add_zero"],
        "src_files": ["calc.py"],
    }
    base.update(overrides)
    return base


# ── validate_candidate ───────────────────────────────────────────────────


def test_validate_candidate_passes(tmp_path):
    root, c1, c2, _ = _build_repo(tmp_path)
    ok, reason, updated = probe_harvest.validate_candidate(
        {"base_sha": c1, "fix_sha": c2, "test_files": ["tests/test_calc.py"]},
        root=root,
    )
    assert ok is True
    assert reason == ""
    assert updated["fail_to_pass"] == ["tests.test_calc::test_add"]
    assert updated["pass_to_pass"] == ["tests.test_calc::test_add_zero"]
    assert _scratch_is_empty(tmp_path)


def test_validate_candidate_no_fail_to_pass(tmp_path):
    root, _c1, c2, _ = _build_repo(tmp_path)
    ok, reason, _ = probe_harvest.validate_candidate(
        {"base_sha": c2, "fix_sha": c2, "test_files": ["tests/test_calc.py"]},
        root=root,
    )
    assert (ok, reason) == (False, "no-fail-to-pass")
    assert _scratch_is_empty(tmp_path)


def test_validate_candidate_fix_not_green(tmp_path):
    root, c1, _c2, c3 = _build_repo(tmp_path)
    ok, reason, _ = probe_harvest.validate_candidate(
        {"base_sha": c1, "fix_sha": c3, "test_files": ["tests/test_calc.py"]},
        root=root,
    )
    assert (ok, reason) == (False, "fix-not-green:1")
    assert _scratch_is_empty(tmp_path)


def test_validate_candidate_archive_failed(tmp_path):
    root, _c1, c2, _ = _build_repo(tmp_path)
    ok, reason, _ = probe_harvest.validate_candidate(
        {"base_sha": "0" * 40, "fix_sha": c2, "test_files": ["tests/test_calc.py"]},
        root=root,
    )
    assert (ok, reason) == (False, "archive-failed")
    assert _scratch_is_empty(tmp_path)


def test_validate_candidate_fix_missing_test(tmp_path):
    root, c1, c2, _ = _build_repo(tmp_path)
    ok, reason, _ = probe_harvest.validate_candidate(
        {
            "base_sha": c1,
            "fix_sha": c2,
            "test_files": ["tests/test_missing.py"],
        },
        root=root,
    )
    assert (ok, reason) == (False, "fix-missing-test")
    assert _scratch_is_empty(tmp_path)


@pytest.mark.parametrize(
    "path", ["../escape/test_x.py", "tests/../../test_x.py", "/tmp/test_x.py"]
)
def test_validate_candidate_unsafe_test_path(tmp_path, path):
    root, c1, c2, _ = _build_repo(tmp_path)
    ok, reason, _ = probe_harvest.validate_candidate(
        {"base_sha": c1, "fix_sha": c2, "test_files": [path]},
        root=root,
    )
    assert (ok, reason) == (False, "unsafe-test-path")
    assert not (tmp_path / "escape").exists()
    assert _scratch_is_empty(tmp_path)


# ── failed_run_candidates ────────────────────────────────────────────────


def test_failed_run_becomes_candidate(tmp_path):
    root, c1, c2, _ = _build_repo(tmp_path)
    k1 = tmp_path / "k1.md"
    k1.write_text(K1_TEXT)
    home = _home(
        tmp_path, [_row("fe-ok-1", kickoff=str(k1), created=1, baseline_ref=c1)]
    )

    candidates, rejections = probe_harvest.failed_run_candidates(
        home, root, landing_ref="HEAD"
    )

    assert rejections == []
    assert len(candidates) == 1
    cand = candidates[0]
    assert cand["source"] == "failed-run"
    assert cand["task_id"] == "fe-" + c2[:10]
    assert cand["fix_sha"] == c2
    assert cand["base_sha"] == c1
    assert cand["test_files"] == ["tests/test_calc.py"]
    assert cand["src_files"] == ["calc.py"]
    assert cand["cluster"] == "change/R5/test_verifier"
    assert cand["run_ids"] == ["fe-ok-1"]
    assert cand["baseline_outcome"] == 0.0
    assert cand["baseline_source"] == "task_runs.status=failed"
    assert cand["problem_statement"] == K1_TEXT
    assert cand["fail_to_pass"] == []
    assert cand["pass_to_pass"] == []
    assert cand["holdout"] == probe_harvest.is_holdout(c2)


def test_two_runs_same_landing_merge(tmp_path):
    root, c1, c2, _ = _build_repo(tmp_path)
    k1 = tmp_path / "k1.md"
    k1.write_text(K1_TEXT)
    home = _home(
        tmp_path,
        [
            _row("fe-ok-1", kickoff=str(k1), created=1, baseline_ref=c1),
            _row("fe-ok-2", kickoff=str(k1), created=2, baseline_ref=c1),
        ],
    )

    candidates, rejections = probe_harvest.failed_run_candidates(
        home, root, landing_ref="HEAD"
    )

    assert rejections == []
    assert len(candidates) == 1
    cand = candidates[0]
    assert cand["task_id"] == "fe-" + c2[:10]
    assert cand["fix_sha"] == c2
    assert cand["run_ids"] == ["fe-ok-1", "fe-ok-2"]


def test_rejection_reasons_in_query_order(tmp_path, harness, monkeypatch):
    root, c1, _c2, _ = _build_repo(tmp_path)
    k5 = tmp_path / "k5.md"
    k5.write_text("Nothing to see here.\n")
    k6 = tmp_path / "k6.md"
    k6.write_text("See tests/unit/test_never.py.\n")
    k7 = tmp_path / "k7.md"
    k7.write_text(K1_TEXT)
    harness.layers["r3"] = "provider"
    monkeypatch.setattr(probe_harvest, "MAX_SRC_FILES", 0)

    home = _home(
        tmp_path,
        [
            _row("r1", kickoff=str(k7), created=1, baseline_ref=None),
            _row("r2", kickoff=str(k7), created=2, baseline_ref="0" * 40),
            _row("r3", kickoff=str(k7), created=3, baseline_ref=c1),
            _row("r4", kickoff="/nonexistent/k.md", created=4, baseline_ref=c1),
            _row("r5", kickoff=str(k5), created=5, baseline_ref=c1),
            _row("r6", kickoff=str(k6), created=6, baseline_ref=c1),
            _row("r7", kickoff=str(k7), created=7, baseline_ref=c1),
        ],
    )

    candidates, rejections = probe_harvest.failed_run_candidates(
        home, root, landing_ref="HEAD"
    )

    assert candidates == []
    assert [(r["task_id"], r["reason"]) for r in rejections] == [
        ("r1", "no-pre-impl-fixture"),
        ("r2", "baseline-unreachable"),
        ("r3", "layer:provider"),
        ("r4", "kickoff-unrecoverable"),
        ("r5", "no-test-named"),
        ("r6", "test-not-landed"),
        ("r7", "landing-too-broad"),
    ]


def test_ignored_rows(tmp_path):
    root, c1, _c2, _ = _build_repo(tmp_path)
    k7 = tmp_path / "k7.md"
    k7.write_text(K1_TEXT)
    home = _home(
        tmp_path,
        [
            _row(
                "r8",
                kickoff=str(k7),
                created=8,
                status="published",
                baseline_ref=c1,
            ),
            _row(
                "r9",
                kickoff=str(k7),
                created=9,
                recipe="code-fix",
                baseline_ref=c1,
            ),
        ],
    )

    candidates, rejections = probe_harvest.failed_run_candidates(
        home, root, landing_ref="HEAD"
    )

    assert candidates == []
    assert rejections == []


def test_kickoff_that_landed_wins(tmp_path):
    root, c1, c2, _ = _build_repo(tmp_path, with_kickoff=True)
    home = _home(
        tmp_path,
        [
            _row(
                "fe-k1",
                kickoff="/gone/kickoffs/auto/k.md",
                created=1,
                baseline_ref=c1,
            )
        ],
    )

    candidates, rejections = probe_harvest.failed_run_candidates(
        home, root, landing_ref="HEAD"
    )

    assert rejections == []
    assert len(candidates) == 1
    cand = candidates[0]
    assert cand["problem_statement"] == "v2 tests/test_calc.py\n"
    assert cand["src_files"] == ["calc.py", "kickoffs/auto/k.md"]
    assert cand["fix_sha"] == c2
    assert cand["base_sha"] == c1


def test_frontmatter_stripped(tmp_path):
    root, c1, _c2, _ = _build_repo(tmp_path)
    kfm = tmp_path / "kfm.md"
    kfm.write_text("---\ntitle: spec\nrun: rsi-1\n---\nFix add tests/test_calc.py.\n")
    home = _home(
        tmp_path, [_row("fe-fm", kickoff=str(kfm), created=1, baseline_ref=c1)]
    )

    candidates, rejections = probe_harvest.failed_run_candidates(
        home, root, landing_ref="HEAD"
    )

    assert rejections == []
    assert len(candidates) == 1
    statement = candidates[0]["problem_statement"]
    assert not statement.lstrip().startswith("---")
    assert "title:" not in statement
    assert "Fix add tests/test_calc.py." in statement


# ── render_probe ─────────────────────────────────────────────────────────


def test_render_failed_run_candidate(tmp_path):
    root, c1, c2, _ = _build_repo(tmp_path)
    task_id = f"fe-{c2[:10]}"
    text = (
        f"Fix {c2} starting at {c1} (short {c2[:10]} / {c1[:10]}),"
        f" task {task_id}.\n\n## Files in scope\n- calc.py\n"
        "- tests/test_calc.py (new)\n"
    )
    cand = _candidate(c1, c2, text)

    markdown, oracle = probe_harvest.render_probe(
        cand,
        recipe="framework-edit",
        stem="probe-h01",
        harvested_at="2026-10-27T00:00:00Z",
    )

    assert "tests/test_calc.py" in markdown
    assert "## Files in scope" in markdown
    for needle in (c2, c1, c2[:10], c1[:10], task_id):
        assert needle not in markdown
    assert "## Problem" not in markdown
    assert "## Definition of Done" not in markdown
    assert oracle["provenance"]["source"] == "failed-run"


def test_render_mined_candidate_unchanged(tmp_path):
    root, c1, c2, _ = _build_repo(tmp_path)
    cand = _candidate(
        c1, c2, "see tests/test_calc.py and " + "mo-" + c2[:10] + "\n"
    )
    cand.update(
        {
            "source": "heldout-mined",
            "task_id": "mo-" + c2[:10],
            "src_files": ["calc.py"],
        }
    )

    markdown, oracle = probe_harvest.render_probe(
        cand,
        recipe="code-fix",
        stem="probe-h01",
        harvested_at="2026-10-27T00:00:00Z",
    )

    assert "## Problem" in markdown
    assert "## Files in scope" in markdown
    assert "## Definition of Done" in markdown
    assert "tests/test_calc.py" not in markdown
    assert "test_add" not in markdown
    assert "[hidden]" in markdown
    assert oracle["provenance"]["source"] == "heldout-mined"


def test_render_mined_candidate_keeps_words_that_contain_a_test_name(tmp_path):
    # Needles are the test ids, paths and names — never a test's bare
    # behaviour word (``test_add`` -> ``add``), which would also eat
    # ``address`` and ``added`` in ordinary prose.
    root, c1, c2, _ = _build_repo(tmp_path)
    cand = _candidate(c1, c2, "Fix the add function; it added the address.\n")
    cand.update({"source": "heldout-mined", "task_id": "mo-" + c2[:10]})

    markdown, _ = probe_harvest.render_probe(
        cand,
        recipe="code-fix",
        stem="probe-h01",
        harvested_at="2026-10-27T00:00:00Z",
    )

    assert "Fix the add function; it added the address." in markdown


# ── harvest ──────────────────────────────────────────────────────────────


def test_harvest_framework_edit(tmp_path):
    root, c1, c2, _ = _build_repo(tmp_path)
    k1 = tmp_path / "k1.md"
    k1.write_text(K1_TEXT)
    home = _home(
        tmp_path, [_row("fe-ok-1", kickoff=str(k1), created=1, baseline_ref=c1)]
    )

    report = probe_harvest.harvest(
        "framework-edit",
        root=root,
        home=home,
        write=True,
        landing_ref="HEAD",
        validate_fn=lambda c: (True, "", {**c, "fail_to_pass": ["x::y"]}),
        now="2026-10-27T00:00:00Z",
    )

    probes = Path(root) / "recipes" / "framework-edit" / "probes"
    assert (probes / "probe-h01.md").is_file()
    sidecar = probes / "oracles" / "probe-h01.json"
    assert sidecar.is_file()
    data = json.loads(sidecar.read_text())
    assert data["provenance"]["source"] == "failed-run"
    assert data["fail_to_pass"] == ["x::y"]
    assert report["written"] is True
    assert report["diagnoses_available"] is True


# ── CLI ──────────────────────────────────────────────────────────────────


def _write_code_fix_inputs(root, c1, c2):
    task_id = "mo-" + c2[:10]
    manifest = [
        {
            "id": task_id,
            "split": "dev",
            "weak_signal": False,
            "base_sha": c1,
            "fix_sha": c2,
            "test_files": ["tests/test_calc.py"],
            "fail_to_pass": ["stale::id"],
            "pass_to_pass": [],
            "src_files": ["calc.py"],
            "problem_statement": "Fix add.",
        }
    ]
    evals = Path(root) / "evals" / "heldout"
    (evals / "mined").mkdir(parents=True)
    (evals / "mined" / "manifest.json").write_text(json.dumps(manifest))
    (evals / "results").mkdir(parents=True)
    (evals / "results" / "2026-09-29-baseline-dev.json").write_text(
        json.dumps({task_id: {"passed": False}})
    )


def test_cli_validates_by_default(tmp_path):
    root, c1, c2, _ = _build_repo(tmp_path)
    _write_code_fix_inputs(root, c1, c2)
    home = _home(tmp_path, [])

    root2 = tmp_path / "fw2"
    shutil.copytree(root, root2)

    rc = probe_harvest.main(
        ["--recipe", "code-fix", "--root", root, "--home", home, "--write"]
    )
    assert rc == 0
    sidecars = sorted((Path(root) / "recipes" / "code-fix").rglob("*.json"))
    assert len(sidecars) == 1, f"expected one written sidecar, got {sidecars}"
    data = json.loads(sidecars[0].read_text())
    assert data["fail_to_pass"] == ["tests.test_calc::test_add"]
    assert data["pass_to_pass"] == ["tests.test_calc::test_add_zero"]
    assert "stale::id" not in data["fail_to_pass"]

    rc2 = probe_harvest.main(
        [
            "--recipe",
            "code-fix",
            "--root",
            str(root2),
            "--home",
            home,
            "--write",
            "--no-validate",
        ]
    )
    assert rc2 == 0
    sidecars2 = sorted((Path(root2) / "recipes" / "code-fix").rglob("*.json"))
    assert len(sidecars2) == 1, f"expected one written sidecar, got {sidecars2}"
    data2 = json.loads(sidecars2[0].read_text())
    assert data2["fail_to_pass"] == ["stale::id"]


# ── constants ────────────────────────────────────────────────────────────


def test_constants():
    assert probe_harvest.SUPPORTED_RECIPES == ("code-fix", "framework-edit")
    assert probe_harvest.MAX_SRC_FILES == 12
    assert probe_harvest.DEFAULT_LANDING_REF == "origin/main"


def test_kickoff_test_re():
    pattern = probe_harvest.KICKOFF_TEST_RE
    assert pattern.findall("Fix add. See tests/test_calc.py (new).\n") == [
        "tests/test_calc.py"
    ]
    assert pattern.findall(
        "See tests/test_calc.py and tests/unit/test_never.py.\n"
    ) == ["tests/test_calc.py", "tests/unit/test_never.py"]
    assert pattern.findall(
        "docs/test_calc.md tests/test_calc.txt tests/README.py\n"
    ) == []
