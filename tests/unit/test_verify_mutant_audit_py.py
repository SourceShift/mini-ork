"""Hermetic unit tests for ``mini_ork.verify.mutant_audit``.

The audit injects mutants into a throwaway ``git worktree`` of a temporary
repository, runs each gate, and scores security-kill TPR against benign-kill
FPR. Every fixture here is created under ``tmp_path``: no network, no model
calls, no dependence on the operator's checkout.
"""

import difflib
import json
import math
import shutil
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path

import pytest

import mini_ork.verify.mutant_audit as ma

requires_git = pytest.mark.skipif(
    shutil.which("git") is None, reason="git is required for worktree staging"
)

BASE_CALC = (
    "def add(a, b):\n"
    "    return a + b\n"
    "\n"
    "\n"
    "def label():\n"
    '    return "base"\n'
)
SEC_CALC = BASE_CALC.replace("return a + b", "return a - b")
BEN_CALC = BASE_CALC.replace('return "base"', 'return "mutated"')

SEC_WITNESS = (
    "def test_add():\n"
    "    from app.calc import add\n"
    "    assert add(2, 3) == 5\n"
)
BEN_WITNESS = (
    "def test_label():\n"
    "    from app.calc import label\n"
    '    assert label() == "base"\n'
)
PASS_ANYWHERE_WITNESS = "def test_ok():\n    assert True\n"

BLANKET_GATE = "import sys\nsys.exit(1)\n"
ABSTAIN_GATE = (
    "import sys\n"
    "print('{\"pass\": false, \"status\": \"unverified\"}')\n"
    "sys.exit(0)\n"
)
DISCRIMINATING_GATE = textwrap.dedent(
    """\
    import sys
    from pathlib import Path
    src = Path('app/calc.py').read_text()
    if 'return a - b' in src:
        sys.exit(1)
    print('{"pass": true}')
    sys.exit(0)
    """
)


def _run_git(cwd, *args):
    subprocess.run(
        ["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True
    )


def _make_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _run_git(repo, "init", "-q")
    _run_git(repo, "config", "user.email", "unit@example.invalid")
    _run_git(repo, "config", "user.name", "Unit Test")
    (repo / "app").mkdir()
    (repo / "app" / "calc.py").write_text(BASE_CALC)
    _run_git(repo, "add", "-A")
    _run_git(repo, "commit", "-q", "-m", "base")
    return repo


def _make_diff(rel, old, new):
    return "".join(
        difflib.unified_diff(
            old.splitlines(keepends=True),
            new.splitlines(keepends=True),
            fromfile=f"a/{rel}",
            tofile=f"b/{rel}",
        )
    )


def _write_mutant(directory, name, kind, diff_text, witness_text):
    entry = directory / name
    entry.mkdir(parents=True)
    (entry / "mutant.diff").write_text(diff_text)
    (entry / "witness.py").write_text(witness_text)
    if kind is not None:
        (entry / "kind").write_text(kind + "\n")
    return entry


def _write_gate(directory, name, source):
    directory.mkdir(parents=True, exist_ok=True)
    gate = directory / name
    gate.write_text(source)
    return gate


def _score_for(report, gate):
    matches = [item for item in report["gates"] if item["gate"] == gate]
    assert len(matches) == 1, f"expected exactly one score for {gate}"
    return matches[0]


def _outcome(gate, mutant, kind, status):
    return ma.GateOutcome(gate=gate, mutant=mutant, kind=kind, status=status, detail="")


# ------------------------------------------------------------------ wilson


def test_wilson_interval_bounds_and_clamping():
    assert ma.wilson_interval(0, 0) == (0.0, 0.0)

    lo, hi = ma.wilson_interval(5, 5)
    assert lo > 0.5
    assert hi == 1.0

    lo, hi = ma.wilson_interval(0, 5)
    assert lo == 0.0
    assert hi < 0.5

    assert ma.wilson_interval(0, 500)[1] < 0.05

    for n in range(1, 12):
        for k in range(0, n + 1):
            lo, hi = ma.wilson_interval(k, n)
            assert 0.0 <= lo <= hi <= 1.0, (k, n, lo, hi)


def test_wilson_interval_matches_reference_formula():
    for k, n in [(1, 1), (3, 7), (10, 40), (7, 8)]:
        p = k / n
        denom = 1 + ma.Z * ma.Z / n
        center = (p + ma.Z * ma.Z / (2 * n)) / denom
        half = (
            ma.Z
            * math.sqrt(p * (1 - p) / n + ma.Z * ma.Z / (4 * n * n))
            / denom
        )
        lo, hi = ma.wilson_interval(k, n)
        assert lo == pytest.approx(max(0.0, center - half))
        assert hi == pytest.approx(min(1.0, center + half))


# --------------------------------------------------------------- load_mutants


def test_load_mutants_sorted_and_default_kind(tmp_path):
    root = tmp_path / "mutants"
    for name in ("zeta", "alpha"):
        _write_mutant(root, name, None, "diff\n", PASS_ANYWHERE_WITNESS)
    _write_mutant(root, "beta", "benign", "diff\n", PASS_ANYWHERE_WITNESS)

    mutants, problems = ma.load_mutants(str(root))
    assert problems == []
    assert [mutant.name for mutant in mutants] == ["alpha", "beta", "zeta"]
    assert mutants[0].kind == ma.KIND_SECURITY
    assert mutants[1].kind == ma.KIND_BENIGN
    assert mutants[0].patch == root / "alpha" / "mutant.diff"


def test_load_mutants_reports_missing_files_and_unknown_kind(tmp_path):
    root = tmp_path / "mutants"
    _write_mutant(root, "good", None, "diff\n", PASS_ANYWHERE_WITNESS)

    nowitness = root / "nowitness"
    nowitness.mkdir()
    (nowitness / "mutant.diff").write_text("diff\n")

    weird = _write_mutant(root, "weird", "weird", "diff\n", PASS_ANYWHERE_WITNESS)

    mutants, problems = ma.load_mutants(str(root))
    assert [mutant.name for mutant in mutants] == ["good"]
    assert len(problems) == 2
    assert any(problem.startswith("nowitness: ") for problem in problems)
    assert any(problem.startswith("weird: ") for problem in problems)
    assert weird.is_dir()  # the fixture really did carry the bad kind


def test_load_mutants_missing_directory(tmp_path):
    missing = tmp_path / "does-not-exist"
    mutants, problems = ma.load_mutants(str(missing))
    assert mutants == []
    assert problems == [f"mutants directory not found: {missing}"]


# --------------------------------------------------------------- touched_paths


def test_touched_paths_dedup_sorted_skips_dev_null(tmp_path):
    patch = tmp_path / "mutant.diff"
    patch.write_text(
        "--- a/app/calc.py\n"
        "+++ b/app/calc.py\n"
        "@@ -1 +1 @@\n"
        "-old\n"
        "+new\n"
        "--- a/src/zed.py\n"
        "+++ b/src/zed.py\n"
        "@@ -1 +1 @@\n"
        "-old\n"
        "+new\n"
        "--- a/gone.py\n"
        "+++ /dev/null\n"
        "@@ -1 +0,0 @@\n"
        "-old\n"
        "+++ b/app/calc.py\n"
    )
    assert ma.touched_paths(patch) == ["app/calc.py", "src/zed.py"]


# ---------------------------------------------------------------------- score


def test_score_blanket_gate_is_advisory():
    outcomes = (
        [_outcome("blanket", f"s{i}", "security", ma.KILLED) for i in range(4)]
        + [_outcome("blanket", f"b{i}", "benign", ma.KILLED) for i in range(4)]
    )
    (score,) = ma.score(outcomes)
    assert score.tpr == 1.0 and score.fpr == 1.0
    assert score.separation == 0.0
    assert score.demonstrated is False
    assert score.promotion == "advisory"


def test_score_discriminating_gate_is_eligible():
    outcomes = (
        [_outcome("disc", f"s{i}", "security", ma.KILLED) for i in range(4)]
        + [_outcome("disc", f"b{i}", "benign", ma.SURVIVED) for i in range(4)]
    )
    (score,) = ma.score(outcomes)
    assert score.security_kills == 4 and score.security_total == 4
    assert score.benign_kills == 0 and score.benign_total == 4
    assert score.tpr == 1.0 and score.fpr == 0.0
    assert score.separation == 1.0
    assert score.demonstrated is True
    assert score.promotion == "eligible"


def test_score_gate_error_counts_to_neither_total():
    outcomes = [
        _outcome("g", "s1", "security", ma.KILLED),
        _outcome("g", "s2", "security", ma.GATE_ERROR),
        _outcome("g", "b1", "benign", ma.SURVIVED),
        _outcome("g", "b2", "benign", ma.GATE_ERROR),
    ]
    (score,) = ma.score(outcomes)
    assert score.security_total == 1 and score.security_kills == 1
    assert score.benign_total == 1 and score.benign_kills == 0
    assert score.tpr == 1.0 and score.fpr == 0.0

    (broken,) = ma.score(
        [
            _outcome("broken", "s1", "security", ma.GATE_ERROR),
            _outcome("broken", "b1", "benign", ma.GATE_ERROR),
        ]
    )
    assert broken.security_total == 0 and broken.benign_total == 0
    assert broken.promotion == "advisory"


def test_score_is_sorted_and_needs_both_kinds():
    scores = ma.score(
        [
            _outcome("z_gate", "s1", "security", ma.KILLED),
            _outcome("a_gate", "s1", "security", ma.KILLED),
        ]
    )
    assert [score.gate for score in scores] == ["a_gate", "z_gate"]
    # A perfect security record with no benign control cannot demonstrate.
    assert all(score.demonstrated is False for score in scores)

    # One mutant per kind is not enough: the Wilson bounds still overlap.
    (pair,) = ma.score(
        [
            _outcome("g", "s1", "security", ma.KILLED),
            _outcome("g", "b1", "benign", ma.SURVIVED),
        ]
    )
    assert pair.demonstrated is False


def test_score_of_no_outcomes_is_an_empty_list():
    """No outcomes means no gate to score — the shape never changes."""
    assert ma.score([]) == []


# ------------------------------------------------------------------ _child_env


def test_child_env_strips_mini_ork_and_mo_prefixes(monkeypatch):
    monkeypatch.setenv("MO_SPEC_MARKER", "1")
    monkeypatch.setenv("MINI_ORK_SPEC_MARKER", "1")
    env = ma._child_env()
    assert isinstance(env, dict)
    assert not any(
        key.startswith(("MO_", "MINI_ORK_")) for key in env
    ), sorted(key for key in env if key.startswith(("MO_", "MINI_ORK_")))


# ----------------------------------------------------------------------- _run


def test_run_returns_rc_and_400_char_tail(tmp_path):
    rc, tail = ma._run(
        [sys.executable, "-c", "print('x' * 600)"], str(tmp_path), 30
    )
    assert rc == 0
    assert tail == "x" * 400

    rc, tail = ma._run(
        [sys.executable, "-c", "import sys; print('AAA'); sys.stderr.write('BBB')"],
        str(tmp_path),
        30,
    )
    assert rc == 0
    assert "AAA" in tail and "BBB" in tail
    assert len(tail) <= 400


def test_run_timeout_and_missing_binary(tmp_path):
    rc, tail = ma._run(
        [sys.executable, "-c", "import time; time.sleep(10)"], str(tmp_path), 1
    )
    assert rc is None
    assert tail == "timed out after 1s"

    missing = str(tmp_path / "no-such-binary")
    rc, tail = ma._run([missing], str(tmp_path), 30)
    assert rc is None
    assert tail.startswith("could not run")
    assert missing in tail


# ------------------------------------------------------------------- staging


@requires_git
def test_stage_and_reset_tree(tmp_path):
    repo = _make_repo(tmp_path)
    tree, parent = ma.stage(str(repo), "HEAD")
    try:
        assert Path(tree, "app", "calc.py").read_text() == BASE_CALC
        Path(tree, "app", "calc.py").write_text("junk\n")
        Path(tree, "junk.txt").write_text("junk\n")
        ma.reset_tree(tree)
        assert Path(tree, "app", "calc.py").read_text() == BASE_CALC
        assert not Path(tree, "junk.txt").exists()
    finally:
        subprocess.run(
            ["git", "-C", str(repo), "worktree", "remove", "--force", tree],
            capture_output=True,
        )
        shutil.rmtree(parent, ignore_errors=True)


@requires_git
def test_stage_bad_base_raises(tmp_path):
    repo = _make_repo(tmp_path)
    with pytest.raises(RuntimeError) as excinfo:
        ma.stage(str(repo), "deadbeefdeadbeef")
    assert "deadbeefdeadbeef" in str(excinfo.value)


# ------------------------------------------------------------------ validate


@requires_git
def test_validate_admits_triggering_mutant_and_leaves_mutation(tmp_path):
    repo = _make_repo(tmp_path)
    tree, parent = ma.stage(str(repo), "HEAD")
    try:
        fixtures = tmp_path / "fixtures"
        fixtures.mkdir()
        patch = fixtures / "sec.diff"
        patch.write_text(_make_diff("app/calc.py", BASE_CALC, SEC_CALC))
        witness = fixtures / "witness.py"
        witness.write_text(SEC_WITNESS)

        mutant = ma.Mutant("sec.01-x", "security", patch, witness)
        assert ma.validate(tree, mutant, 60) == ""

        safe_copy = Path(tree, "test_mutant_sec_01_x.py")
        assert safe_copy.is_file()
        assert safe_copy.read_text() == SEC_WITNESS
        assert "return a - b" in Path(tree, "app", "calc.py").read_text()
    finally:
        subprocess.run(
            ["git", "-C", str(repo), "worktree", "remove", "--force", tree],
            capture_output=True,
        )
        shutil.rmtree(parent, ignore_errors=True)


@requires_git
def test_validate_rejects_non_triggering_mutant(tmp_path):
    repo = _make_repo(tmp_path)
    tree, parent = ma.stage(str(repo), "HEAD")
    try:
        fixtures = tmp_path / "fixtures"
        fixtures.mkdir()
        patch = fixtures / "sec.diff"
        patch.write_text(_make_diff("app/calc.py", BASE_CALC, SEC_CALC))
        witness = fixtures / "witness.py"
        witness.write_text(PASS_ANYWHERE_WITNESS)

        reason = ma.validate(
            tree, ma.Mutant("nontrigger", "security", patch, witness), 60
        )
        assert reason.startswith("witness does not trigger on the mutant")
        ma.reset_tree(tree)

        failing = fixtures / "failing.py"
        failing.write_text("def test_x():\n    assert False\n")
        reason = ma.validate(
            tree, ma.Mutant("basefail", "security", patch, failing), 60
        )
        assert reason.startswith("witness does not pass on the base tree")
    finally:
        subprocess.run(
            ["git", "-C", str(repo), "worktree", "remove", "--force", tree],
            capture_output=True,
        )
        shutil.rmtree(parent, ignore_errors=True)


# ------------------------------------------------------------------- run_gate


@requires_git
def test_run_gate_classifies_killed_survived_and_errors(tmp_path):
    gates = tmp_path / "gates"
    tree = tmp_path / "tree"
    tree.mkdir()

    killer = _write_gate(
        gates,
        "killer.py",
        "import sys\nprint('{\"status\": \"unverified\"}')\nsys.exit(1)\n",
    )
    assert ma.run_gate(str(killer), str(tree), [], 60)[0] == ma.KILLED

    sparer = _write_gate(
        gates, "sparer.py", "import sys\nprint('{\"pass\": true}')\nsys.exit(0)\n"
    )
    assert ma.run_gate(str(sparer), str(tree), [], 60)[0] == ma.SURVIVED

    abstain = _write_gate(
        gates,
        "abstain.py",
        "import sys\n"
        "print('{\"pass\": false, \"status\": \"unverified\"}')\n"
        "sys.exit(0)\n",
    )
    status, detail = ma.run_gate(str(abstain), str(tree), [], 60)
    assert status == ma.GATE_ERROR
    assert "abstention is not a kill" in detail

    silent = _write_gate(gates, "silent.py", "import sys\nsys.exit(0)\n")
    status, detail = ma.run_gate(str(silent), str(tree), [], 60)
    assert status == ma.GATE_ERROR
    assert "no JSON envelope" in detail

    crashed = _write_gate(gates, "crashed.py", "import sys\nsys.exit(2)\n")
    assert ma.run_gate(str(crashed), str(tree), [], 60)[0] == ma.GATE_ERROR


@requires_git
def test_run_gate_sets_env_contract(tmp_path):
    dump = tmp_path / "env.json"
    gate = _write_gate(
        tmp_path / "gates",
        "probe.py",
        "import json, os, sys\n"
        "with open(" + repr(str(dump)) + ", 'w') as handle:\n"
        "    json.dump(dict(os.environ), handle)\n"
        "print('{\"pass\": true}')\n"
        "sys.exit(0)\n",
    )
    tree = tmp_path / "tree"
    tree.mkdir()
    status, _ = ma.run_gate(str(gate), str(tree), ["app/a.py", "src/b.py"], 60)
    assert status == ma.SURVIVED
    env = json.loads(dump.read_text())
    assert env["MO_TARGET_CWD"] == str(tree)
    assert env["MINI_ORK_TOUCHED_FILES"] == "app/a.py\nsrc/b.py"
    assert Path(env["MINI_ORK_RUN_DIR"]).is_dir()


# ---------------------------------------------------------------------- audit


def _separation_fixtures(tmp_path):
    repo = _make_repo(tmp_path)
    mutants = tmp_path / "mutants"
    sec_diff = _make_diff("app/calc.py", BASE_CALC, SEC_CALC)
    ben_diff = _make_diff("app/calc.py", BASE_CALC, BEN_CALC)
    for index in range(1, 5):
        _write_mutant(
            mutants, f"sec-{index:02d}", "security", sec_diff, SEC_WITNESS
        )
        _write_mutant(mutants, f"ben-{index:02d}", "benign", ben_diff, BEN_WITNESS)
    gates = tmp_path / "gates"
    blanket = _write_gate(gates, "blanket.py", BLANKET_GATE)
    discriminating = _write_gate(
        gates, "discriminating.py", DISCRIMINATING_GATE
    )
    return repo, mutants, blanket, discriminating


@requires_git
def test_audit_separates_blanket_from_discriminating(tmp_path):
    repo, mutants, blanket, discriminating = _separation_fixtures(tmp_path)

    report = ma.audit(
        str(repo),
        "HEAD",
        str(mutants),
        [str(blanket), str(discriminating)],
        timeout=120,
    )

    assert report["problems"] == []
    assert len(report["mutants"]) == 8

    blanket_score = _score_for(report, str(blanket))
    assert blanket_score["tpr"] == 1.0 and blanket_score["fpr"] == 1.0
    assert blanket_score["separation"] == 0.0
    assert blanket_score["demonstrated"] is False
    assert blanket_score["promotion"] == "advisory"

    discriminating_score = _score_for(report, str(discriminating))
    assert discriminating_score["tpr"] == 1.0
    assert discriminating_score["fpr"] == 0.0
    assert discriminating_score["demonstrated"] is True
    assert discriminating_score["promotion"] == "eligible"

    assert report["demonstrated_gates"] == [str(discriminating)]


@requires_git
def test_audit_excludes_non_triggering_mutant_from_every_rate(tmp_path):
    repo = _make_repo(tmp_path)
    mutants = tmp_path / "mutants"
    _write_mutant(
        mutants, "nontrigger", "security", _make_diff(
            "app/calc.py", BASE_CALC, SEC_CALC
        ), PASS_ANYWHERE_WITNESS
    )
    gates = tmp_path / "gates"
    blanket = _write_gate(gates, "blanket.py", BLANKET_GATE)
    discriminating = _write_gate(
        gates, "discriminating.py", DISCRIMINATING_GATE
    )

    report = ma.audit(
        str(repo),
        "HEAD",
        str(mutants),
        [str(blanket), str(discriminating)],
        timeout=120,
    )

    assert len(report["outcomes"]) == 2
    assert all(outcome["status"] == ma.INVALID for outcome in report["outcomes"])
    assert any("nontrigger" in problem for problem in report["problems"])
    for gate in (str(blanket), str(discriminating)):
        score = _score_for(report, gate)
        assert score["security_total"] == 0 and score["benign_total"] == 0
        assert score["demonstrated"] is False
        assert score["promotion"] == "advisory"
    assert report["demonstrated_gates"] == []


@requires_git
def test_audit_abstaining_gate_is_never_a_kill(tmp_path):
    repo = _make_repo(tmp_path)
    mutants = tmp_path / "mutants"
    _write_mutant(
        mutants, "sec-01", "security", _make_diff(
            "app/calc.py", BASE_CALC, SEC_CALC
        ), SEC_WITNESS
    )
    _write_mutant(
        mutants, "ben-01", "benign", _make_diff(
            "app/calc.py", BASE_CALC, BEN_CALC
        ), BEN_WITNESS
    )
    abstain = _write_gate(tmp_path / "gates", "abstain.py", ABSTAIN_GATE)

    report = ma.audit(str(repo), "HEAD", str(mutants), [str(abstain)], timeout=120)

    assert [outcome["status"] for outcome in report["outcomes"]] == [
        ma.GATE_ERROR,
        ma.GATE_ERROR,
    ]
    assert all(
        "abstention is not a kill" in outcome["detail"]
        for outcome in report["outcomes"]
    )
    score = _score_for(report, str(abstain))
    assert score["security_total"] == 0 and score["benign_total"] == 0
    assert score["demonstrated"] is False
    assert report["demonstrated_gates"] == []


@requires_git
def test_audit_removes_throwaway_worktree(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path)
    mutants = tmp_path / "mutants"
    _write_mutant(
        mutants, "sec-01", "security", _make_diff(
            "app/calc.py", BASE_CALC, SEC_CALC
        ), SEC_WITNESS
    )
    _write_mutant(
        mutants, "ben-01", "benign", _make_diff(
            "app/calc.py", BASE_CALC, BEN_CALC
        ), BEN_WITNESS
    )
    blanket = _write_gate(tmp_path / "gates", "blanket.py", BLANKET_GATE)

    sandbox = tmp_path / "tempbox"
    sandbox.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(sandbox))

    before = subprocess.run(
        ["git", "-C", str(repo), "worktree", "list", "--porcelain"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout

    report = ma.audit(str(repo), "HEAD", str(mutants), [str(blanket)], timeout=120)
    assert report["demonstrated_gates"] == []

    after = subprocess.run(
        ["git", "-C", str(repo), "worktree", "list", "--porcelain"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert after == before
    leftovers = [
        entry
        for entry in sandbox.iterdir()
        if entry.is_dir() and (entry / ".git").is_file()
    ]
    assert leftovers == []


# ------------------------------------------------------------------------ CLI


def _run_main(argv):
    try:
        return ma.main(argv)
    except SystemExit as exc:
        return 0 if exc.code is None else int(exc.code)


@requires_git
def test_cli_json_report_and_require_separation(tmp_path, capsys):
    repo = _make_repo(tmp_path)
    mutants = tmp_path / "mutants"
    _write_mutant(
        mutants, "sec-01", "security", _make_diff(
            "app/calc.py", BASE_CALC, SEC_CALC
        ), SEC_WITNESS
    )
    _write_mutant(
        mutants, "ben-01", "benign", _make_diff(
            "app/calc.py", BASE_CALC, BEN_CALC
        ), BEN_WITNESS
    )
    blanket = _write_gate(tmp_path / "gates", "blanket.py", BLANKET_GATE)

    argv = [
        "--repo",
        str(repo),
        "--base",
        "HEAD",
        "--mutants",
        str(mutants),
        "--gate",
        str(blanket),
        "--json",
        "--timeout",
        "120",
    ]
    assert _run_main(argv) == 0
    report = json.loads(capsys.readouterr().out)
    assert {
        "gates",
        "mutants",
        "outcomes",
        "problems",
        "demonstrated_gates",
    } <= set(report)
    assert report["demonstrated_gates"] == []

    assert _run_main(argv + ["--require-separation"]) == 1


def test_cli_usage_error_exits_two(tmp_path):
    assert _run_main(["--repo", str(tmp_path), "--mutants", str(tmp_path)]) == 2
    assert _run_main(["--repo", str(tmp_path), "--base", "HEAD"]) == 2


@requires_git
def test_cli_text_mode_prints_gate_line_and_summary(tmp_path, capsys):
    repo = _make_repo(tmp_path)
    mutants = tmp_path / "mutants"
    _write_mutant(
        mutants, "sec-01", "security", _make_diff(
            "app/calc.py", BASE_CALC, SEC_CALC
        ), SEC_WITNESS
    )
    _write_mutant(
        mutants, "ben-01", "benign", _make_diff(
            "app/calc.py", BASE_CALC, BEN_CALC
        ), BEN_WITNESS
    )
    blanket = _write_gate(tmp_path / "gates", "blanket.py", BLANKET_GATE)

    rc = _run_main(
        [
            "--repo",
            str(repo),
            "--base",
            "HEAD",
            "--mutants",
            str(mutants),
            "--gate",
            str(blanket),
            "--timeout",
            "120",
        ]
    )
    assert rc == 0
    lines = [line for line in capsys.readouterr().out.splitlines() if line.strip()]
    assert len(lines) == 2
    gate_line, summary = lines
    assert gate_line.startswith(f"{blanket}: ")
    assert "tpr=" in gate_line and "fpr=" in gate_line
    assert "separation=" in gate_line
    assert gate_line.rstrip().endswith("advisory")
    assert summary == "mutant-audit: 0 of 1 gate(s) demonstrated"
