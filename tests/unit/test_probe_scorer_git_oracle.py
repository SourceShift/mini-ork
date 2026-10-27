"""Git-oracle probe kind for the probe scorer (``MO_APPLY_PROBE_GIT_ORACLE``).

A git-oracle probe is a probe kickoff with ``oracle: git`` frontmatter and a
sidecar at ``probes/oracles/<stem>.json``. Its target is a history-free
snapshot of ``base_sha``; its outcome is 1.0 only when the run published AND
the hidden tests from ``fix_sha`` pass. The whole kind is default-off, so a
probe directory without ``oracle:``/``holdout:`` frontmatter behaves exactly
as it did before.

These tests are hermetic: a temp git repo is built per test and the launch /
outcome seams are stubbed. No network, no provider credentials.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

import mini_ork.learning.probe_scorer as ps

FLAG = "MO_APPLY_PROBE_GIT_ORACLE"
UNKNOWN_SHA = "0" * 40

BUGGY_CALC = "def add(a, b):\n    return a - b\n"
FIXED_CALC = "def add(a, b):\n    return a + b\n"

TEST_CALC_SRC = (
    "import os, sys\n"
    'sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))\n'
    "from calc import add\n"
    "\n"
    "def test_add():\n"
    "    assert add(2, 3) == 5\n"
    "\n"
    "def test_add_zero():\n"
    "    assert add(0, 0) == 0\n"
)

TEST_DEEP_SRC = (
    "import os, sys\n"
    "sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))\n"
    "from calc import add\n"
    "\n"
    "def test_deep():\n"
    "    assert add(1, 1) == 2\n"
)

OUTCOMES_SRC = (
    "import pytest\n"
    "\n"
    "@pytest.fixture\n"
    "def boom():\n"
    '    raise RuntimeError("boom")\n'
    "\n"
    "def test_error(boom):\n"
    "    assert True\n"
    "\n"
    "def test_failed():\n"
    "    assert False\n"
    "\n"
    "def test_skipped():\n"
    '    pytest.skip("no")\n'
    "\n"
    "def test_passed():\n"
    "    assert True\n"
)

SLOW_SRC = "import time\n\n\ndef test_slow():\n    time.sleep(60)\n"


def _git(root, *args):
    proc = subprocess.run(
        ["git", "-C", str(root), *args], capture_output=True, text=True
    )
    assert proc.returncode == 0, f"git {args} failed: {proc.stderr}"
    return proc.stdout.strip()


@pytest.fixture
def git_root(tmp_path, monkeypatch):
    """A temp repo with a base and a fix commit, plus probe scaffolding."""
    root = tmp_path / "fw"
    root.mkdir()
    _git(root, "init")
    _git(root, "config", "user.email", "spec@example.com")
    _git(root, "config", "user.name", "Spec Author")

    (root / "calc.py").write_text(BUGGY_CALC)
    _git(root, "add", "calc.py")
    _git(root, "commit", "-m", "base")
    base_sha = _git(root, "rev-parse", "HEAD")

    (root / "calc.py").write_text(FIXED_CALC)
    (root / "tests").mkdir()
    (root / "tests" / "test_calc.py").write_text(TEST_CALC_SRC)
    (root / "tests" / "nested").mkdir(parents=True)
    (root / "tests" / "nested" / "test_deep.py").write_text(TEST_DEEP_SRC)
    _git(root, "add", "calc.py", "tests")
    _git(root, "commit", "-m", "fix")
    fix_sha = _git(root, "rev-parse", "HEAD")

    probes = root / "recipes" / "code-fix" / "probes"
    probes.mkdir(parents=True)
    (root / "recipes" / "code-fix" / "prompts").mkdir(parents=True)
    (root / "recipes" / "code-fix" / "prompts" / "implementer.md").write_text("base\n")
    (probes / "probe-1.md").write_text("# plain probe\n")
    (probes / "probe-h01.md").write_text("---\noracle: git\n---\n# git probe\n")
    (probes / "probe-h02.md").write_text(
        "---\noracle: git\nholdout: true\n---\n# held out\n"
    )
    sidecar = {
        "schema": 1,
        "base_sha": base_sha,
        "fix_sha": fix_sha,
        "test_files": ["tests/test_calc.py"],
        "fail_to_pass": ["tests.test_calc::test_add"],
        "pass_to_pass": ["tests.test_calc::test_add_zero"],
    }
    (probes / "oracles").mkdir()
    (probes / "oracles" / "probe-h01.json").write_text(json.dumps(sidecar))
    (probes / "oracles" / "probe-h02.json").write_text(json.dumps(sidecar))

    monkeypatch.setattr(ps, "_ROOT", str(root))
    for var in (
        "MINI_ORK_RUN_DIR",
        "MINI_ORK_ROOT",
        "MINI_ORK_HOME",
        "MINI_ORK_DB",
        FLAG,
        "MO_APPLY_PROBE_SCRATCH_DIR",
        "MO_APPLY_PROBE_GRADE_TIMEOUT_S",
    ):
        monkeypatch.delenv(var, raising=False)
    return root, base_sha, fix_sha


def _probes_dir(root):
    return root / "recipes" / "code-fix" / "probes"


def _sidecar(root):
    return json.loads(
        (_probes_dir(root) / "oracles" / "probe-h01.json").read_text()
    )


def _write_probe(root, recipe, name, body, sidecar=None):
    d = root / "recipes" / recipe / "probes"
    d.mkdir(parents=True, exist_ok=True)
    (d / name).write_text(body)
    if sidecar is not None:
        (d / "oracles").mkdir(exist_ok=True)
        (d / "oracles" / (name[: -len(".md")] + ".json")).write_text(
            json.dumps(sidecar)
        )
    return d / name


def _valid_sidecar(base_sha, fix_sha):
    return {
        "base_sha": base_sha,
        "fix_sha": fix_sha,
        "test_files": ["tests/test_calc.py"],
        "fail_to_pass": ["tests.test_calc::test_add"],
        "pass_to_pass": ["tests.test_calc::test_add_zero"],
    }


def _snapshot(tmp_path, monkeypatch, sha, root=None):
    monkeypatch.setenv("MO_APPLY_PROBE_SCRATCH_DIR", str(tmp_path / "scratch"))
    if root is None:
        return ps.materialize_git_tree(sha)
    return ps.materialize_git_tree(sha, root=str(root))


def _basenames(paths):
    return [os.path.basename(str(p)) for p in paths]


def _collect_dicts(obj, out=None):
    out = [] if out is None else out
    if isinstance(obj, dict):
        out.append(obj)
        for v in obj.values():
            _collect_dicts(v, out)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            _collect_dicts(v, out)
    return out


def _run_dicts(res):
    if isinstance(res, dict):
        runs = res.get("runs")
        if isinstance(runs, list) and runs and all(
            isinstance(e, dict) for e in runs
        ):
            return list(runs)
    return _collect_dicts(res)


def _drive_probe_score(monkeypatch, tmp_path, root, *, flag_on, recipe="code-fix"):
    scratch = tmp_path / "scratch"
    monkeypatch.setenv("MO_APPLY_PROBE_SCRATCH_DIR", str(scratch))
    monkeypatch.setenv("MO_APPLY_PROBE_MAX_TASKS", "5")
    monkeypatch.setenv("MO_APPLY_PROBE_CONTROL_N", "1")
    monkeypatch.setenv("MO_APPLY_PROBE_BUDGET_USD", "100")
    if flag_on:
        monkeypatch.setenv(FLAG, "1")
    else:
        monkeypatch.delenv(FLAG, raising=False)

    calls = []

    def fake_launch(recipe_arg, kickoff, target_cwd=None, root=None):
        calls.append(recipe_arg)
        if (
            "__probe_" in recipe_arg
            and recipe_arg.endswith("_1")
            and target_cwd is not None
        ):
            (Path(target_cwd) / "calc.py").write_text(FIXED_CALC)
        return f"stdout-{len(calls)}", f"run-{len(calls)}", 0.0

    monkeypatch.setattr(ps, "_launch_run", fake_launch)
    monkeypatch.setattr(ps, "_run_outcome", lambda run_id, db=None: 1.0)
    res = ps.probe_score(recipe, "prompts/implementer.md", "a directive")
    return res, calls, scratch


# ── constants ──────────────────────────────────────────────────────────────


def test_module_constants():
    assert ps.GIT_ORACLE_FLAG == "MO_APPLY_PROBE_GIT_ORACLE"
    assert ps.ORACLE_DIR == "oracles"
    assert ps._SHA_RE.match("0" * 40)
    assert not ps._SHA_RE.match("A" * 40)
    assert not ps._SHA_RE.match("0" * 39)
    assert not ps._SHA_RE.match("0" * 41)
    assert ps._PYTEST_FILE_RE.search("tests/test_calc.py")
    assert ps._PYTEST_FILE_RE.search("pkg/tests/sub/dir/test_b.py")
    assert not ps._PYTEST_FILE_RE.search("tests/calc.py")
    assert not ps._PYTEST_FILE_RE.search("src/test_a.py")


# ── _probe_oracle ──────────────────────────────────────────────────────────


def test_probe_oracle_returns_parsed_sidecar(git_root):
    root, base_sha, fix_sha = git_root
    got = ps._probe_oracle(str(_probes_dir(root) / "probe-h01.md"))
    assert got == _sidecar(root)
    assert got["base_sha"] == base_sha
    assert got["fix_sha"] == fix_sha


def test_probe_oracle_none_for_plain_probe(git_root):
    root, _, _ = git_root
    assert ps._probe_oracle(str(_probes_dir(root) / "probe-1.md")) is None


def test_probe_oracle_none_for_other_oracle_kind(git_root):
    root, _, _ = git_root
    path = _write_probe(
        root, "svnrec", "probe-s.md", "---\noracle: svn\n---\n# svn\n"
    )
    assert ps._probe_oracle(str(path)) is None


def test_probe_oracle_reads_holdout_probe_sidecar(git_root):
    root, base_sha, _ = git_root
    got = ps._probe_oracle(str(_probes_dir(root) / "probe-h02.md"))
    assert isinstance(got, dict)
    assert got["base_sha"] == base_sha


def test_probe_oracle_ignores_extra_keys(git_root):
    root, base_sha, fix_sha = git_root
    path = _write_probe(
        root,
        "extrarec",
        "probe-x.md",
        "---\noracle: git\n---\n# x\n",
        sidecar={
            **_valid_sidecar(base_sha, fix_sha),
            "schema": 2,
            "provenance": {"mined": True, "run": "rsi-x"},
        },
    )
    got = ps._probe_oracle(str(path))
    assert got is not None
    assert got["base_sha"] == base_sha
    assert got.get("provenance") == {"mined": True, "run": "rsi-x"}


def test_probe_oracle_missing_sidecar(git_root):
    root, _, _ = git_root
    (_probes_dir(root) / "oracles" / "probe-h01.json").unlink()
    assert ps._probe_oracle(str(_probes_dir(root) / "probe-h01.md")) is None


def test_probe_oracle_misplaced_sidecar(git_root):
    root, _, _ = git_root
    sidecar = _probes_dir(root) / "oracles" / "probe-h01.json"
    (sidecar.parent.parent / "probe-h01.json").write_text(sidecar.read_text())
    sidecar.unlink()
    assert ps._probe_oracle(str(_probes_dir(root) / "probe-h01.md")) is None


_INVALID_SIDECAR_CASES = [
    ("fix-sha-abc", lambda d: {**d, "fix_sha": "abc"}),
    ("base-sha-uppercase", lambda d: {**d, "base_sha": "A" * 40}),
    ("base-sha-short", lambda d: {**d, "base_sha": "0" * 39}),
    ("fix-sha-long", lambda d: {**d, "fix_sha": "0" * 41}),
    ("test-files-non-pytest-path", lambda d: {**d, "test_files": ["calc.py"]}),
    ("test-files-empty", lambda d: {**d, "test_files": []}),
    ("fail-to-pass-empty", lambda d: {**d, "fail_to_pass": []}),
    ("fail-to-pass-bad-form", lambda d: {**d, "fail_to_pass": ["no-separator"]}),
    ("pass-to-pass-bad-form", lambda d: {**d, "pass_to_pass": ["no-separator"]}),
    ("not-json", lambda d: "{not json at all"),
    ("json-array", lambda d: "[1, 2, 3]"),
    ("json-string", lambda d: '"a json string"'),
]


def _apply_sidecar_case(root, mutate):
    path = _probes_dir(root) / "oracles" / "probe-h01.json"
    replacement = mutate(json.loads(path.read_text()))
    if isinstance(replacement, str):
        path.write_text(replacement)
    else:
        path.write_text(json.dumps(replacement))


@pytest.mark.parametrize(
    "mutate",
    [pytest.param(fn, id=cid) for cid, fn in _INVALID_SIDECAR_CASES],
)
def test_probe_oracle_invalid_sidecar(git_root, mutate):
    root, _, _ = git_root
    _apply_sidecar_case(root, mutate)
    assert ps._probe_oracle(str(_probes_dir(root) / "probe-h01.md")) is None


# ── _load_probes gating ────────────────────────────────────────────────────


def test_load_probes_flag_off_skips_git_and_holdout(git_root, monkeypatch):
    root, _, _ = git_root
    monkeypatch.delenv(FLAG, raising=False)
    monkeypatch.setenv("MO_APPLY_PROBE_MAX_TASKS", "5")
    assert _basenames(ps._load_probes("code-fix")) == ["probe-1.md"]


def test_load_probes_flag_on_loads_git_never_holdout(git_root, monkeypatch):
    root, _, _ = git_root
    monkeypatch.setenv(FLAG, "1")
    monkeypatch.setenv("MO_APPLY_PROBE_MAX_TASKS", "5")
    pairs = ps._load_probes("code-fix")
    assert _basenames(pairs) == ["probe-1.md", "probe-h01.md"]
    assert [anchor for _, anchor in pairs] == [False, False]


@pytest.mark.parametrize("flag", ["off", "on"])
def test_load_probes_holdout_never_loaded(git_root, monkeypatch, flag):
    root, _, _ = git_root
    _write_probe(root, "holdrec", "p1.md", "# p\n")
    _write_probe(root, "holdrec", "h.md", "---\nholdout: true\n---\n# h\n")
    monkeypatch.setenv("MO_APPLY_PROBE_MAX_TASKS", "5")
    if flag == "on":
        monkeypatch.setenv(FLAG, "1")
    else:
        monkeypatch.delenv(FLAG, raising=False)
    assert _basenames(ps._load_probes("holdrec")) == ["p1.md"]


@pytest.mark.parametrize(
    "mutate",
    [
        pytest.param(lambda d: {**d, "fix_sha": "abc"}, id="fix-sha-abc"),
        pytest.param(lambda d: {**d, "test_files": ["calc.py"]}, id="test-files-calc"),
    ],
)
def test_load_probes_invalid_sidecar_skipped(git_root, monkeypatch, mutate):
    root, _, _ = git_root
    _apply_sidecar_case(root, mutate)
    monkeypatch.setenv(FLAG, "1")
    monkeypatch.setenv("MO_APPLY_PROBE_MAX_TASKS", "5")
    assert _basenames(ps._load_probes("code-fix")) == ["probe-1.md"]


def test_load_probes_deleted_sidecar_skipped(git_root, monkeypatch):
    root, _, _ = git_root
    (_probes_dir(root) / "oracles" / "probe-h01.json").unlink()
    monkeypatch.setenv(FLAG, "1")
    monkeypatch.setenv("MO_APPLY_PROBE_MAX_TASKS", "5")
    assert _basenames(ps._load_probes("code-fix")) == ["probe-1.md"]


def test_load_probes_other_oracle_value_skipped(git_root, monkeypatch):
    root, _, _ = git_root
    _write_probe(root, "svnrec", "p1.md", "# p\n")
    _write_probe(root, "svnrec", "s.md", "---\noracle: svn\n---\n# s\n")
    monkeypatch.setenv(FLAG, "1")
    monkeypatch.setenv("MO_APPLY_PROBE_MAX_TASKS", "5")
    assert _basenames(ps._load_probes("svnrec")) == ["p1.md"]


@pytest.mark.parametrize("value", ["0", "true"])
def test_load_probes_flag_must_be_exactly_one(git_root, monkeypatch, value):
    root, _, _ = git_root
    monkeypatch.setenv(FLAG, value)
    monkeypatch.setenv("MO_APPLY_PROBE_MAX_TASKS", "5")
    assert _basenames(ps._load_probes("code-fix")) == ["probe-1.md"]


def test_load_probes_holdout_rule_precedes_anchor(git_root, monkeypatch):
    root, _, _ = git_root
    _write_probe(root, "hoanchor", "b.md", "# b\n")
    _write_probe(
        root, "hoanchor", "a.md", "---\nanchor: true\nholdout: true\n---\n# a\n"
    )
    monkeypatch.setenv("MO_APPLY_PROBE_MAX_TASKS", "5")
    assert _basenames(ps._load_probes("hoanchor")) == ["b.md"]


def test_load_probes_git_anchor_needs_flag_and_sidecar(git_root, monkeypatch):
    root, base_sha, fix_sha = git_root
    _write_probe(
        root,
        "garec",
        "g.md",
        "---\noracle: git\nanchor: true\n---\n# g\n",
        sidecar=_valid_sidecar(base_sha, fix_sha),
    )
    monkeypatch.setenv("MO_APPLY_PROBE_MAX_TASKS", "5")
    monkeypatch.delenv(FLAG, raising=False)
    assert _basenames(ps._load_probes("garec")) == []
    monkeypatch.setenv(FLAG, "1")
    assert _basenames(ps._load_probes("garec")) == ["g.md"]


def test_load_probes_cap_and_anchors_unchanged(git_root, monkeypatch):
    root, _, _ = git_root
    _write_probe(root, "caprec", "gate-a.md", "# a\n")
    _write_probe(root, "caprec", "gate-b.md", "# b\n")
    _write_probe(root, "caprec", "anchor.md", "---\nanchor: true\n---\n# c\n")
    monkeypatch.delenv(FLAG, raising=False)
    monkeypatch.setenv("MO_APPLY_PROBE_MAX_TASKS", "1")
    names = _basenames(ps._load_probes("caprec"))
    assert "anchor.md" in names
    assert len(names) == 2
    assert set(names) <= {"gate-a.md", "gate-b.md", "anchor.md"}


# ── probe_scratch_parent ───────────────────────────────────────────────────


def test_probe_scratch_parent_env_override(git_root, monkeypatch, tmp_path):
    root, _, _ = git_root
    want = str(tmp_path / "scratch2")
    monkeypatch.setenv("MO_APPLY_PROBE_SCRATCH_DIR", want)
    assert ps.probe_scratch_parent(str(root)) == want
    assert os.path.isdir(want)


def test_probe_scratch_parent_default(git_root, monkeypatch, tmp_path):
    root, _, _ = git_root
    monkeypatch.delenv("MO_APPLY_PROBE_SCRATCH_DIR", raising=False)
    want = str(tmp_path / "fw-probe-scratch")
    assert ps.probe_scratch_parent(str(root)) == want
    assert os.path.isdir(want)
    assert ps.probe_scratch_parent() == want


def test_probe_scratch_parent_empty_env_is_default(git_root, monkeypatch, tmp_path):
    root, _, _ = git_root
    monkeypatch.setenv("MO_APPLY_PROBE_SCRATCH_DIR", "")
    assert ps.probe_scratch_parent(str(root)) == str(tmp_path / "fw-probe-scratch")


# ── materialize_git_tree / _materialize_target ─────────────────────────────


def test_materialize_snapshot_has_no_history(git_root, monkeypatch, tmp_path):
    root, base_sha, fix_sha = git_root
    t = _snapshot(tmp_path, monkeypatch, base_sha)
    assert os.path.dirname(t) == str(tmp_path / "scratch")
    assert os.path.basename(t).startswith("mo-probe-git-")
    assert (Path(t) / "calc.py").read_text() == BUGGY_CALC
    assert not (Path(t) / "tests" / "test_calc.py").exists()
    count = subprocess.run(
        ["git", "-C", t, "rev-list", "--count", "HEAD"],
        capture_output=True,
        text=True,
    )
    assert count.returncode == 0, count.stderr
    assert count.stdout.strip() == "1"
    unreachable = subprocess.run(
        ["git", "-C", t, "cat-file", "-e", fix_sha], capture_output=True
    )
    assert unreachable.returncode != 0


def test_materialize_explicit_root(git_root, monkeypatch, tmp_path):
    root, base_sha, _ = git_root
    t = _snapshot(tmp_path, monkeypatch, base_sha, root=root)
    assert os.path.dirname(t) == str(tmp_path / "scratch")
    assert (Path(t) / "calc.py").read_text() == BUGGY_CALC


def test_materialize_unknown_sha_fails_loud_and_clean(git_root, monkeypatch, tmp_path):
    root, _, _ = git_root
    scratch = tmp_path / "scratch"
    with pytest.raises(RuntimeError) as excinfo:
        _snapshot(tmp_path, monkeypatch, UNKNOWN_SHA)
    msg = str(excinfo.value)
    assert "git archive" in msg
    assert "failed" in msg
    assert repr(UNKNOWN_SHA) in msg
    expected_tail = subprocess.run(
        ["git", "-C", str(root), "archive", "--format=tar", UNKNOWN_SHA],
        capture_output=True,
    ).stderr[-300:].decode("utf-8", "replace")
    assert msg.endswith(expected_tail)
    assert os.listdir(str(scratch)) == []


def test_materialize_target_uses_git_oracle(git_root, monkeypatch, tmp_path):
    root, base_sha, _ = git_root
    scratch = tmp_path / "scratch"
    monkeypatch.setenv("MO_APPLY_PROBE_SCRATCH_DIR", str(scratch))
    monkeypatch.delenv(FLAG, raising=False)
    t = ps._materialize_target(str(_probes_dir(root) / "probe-h01.md"))
    assert isinstance(t, str)
    assert os.path.dirname(t) == str(scratch)
    assert (Path(t) / "calc.py").read_text() == BUGGY_CALC
    assert not (Path(t) / "tests" / "test_calc.py").exists()


# ── run_pytest_junit ───────────────────────────────────────────────────────


def test_run_pytest_junit_ids(git_root, monkeypatch, tmp_path):
    root, base_sha, fix_sha = git_root
    t = Path(_snapshot(tmp_path, monkeypatch, base_sha))
    (t / "calc.py").write_text(FIXED_CALC)
    blob = subprocess.run(
        ["git", "-C", str(root), "show", f"{fix_sha}:tests/test_calc.py"],
        capture_output=True,
        check=True,
    ).stdout
    (t / "tests").mkdir()
    (t / "tests" / "test_calc.py").write_bytes(blob)
    got = ps.run_pytest_junit(str(t), ["tests/test_calc.py"], timeout=240)
    assert got == {
        "tests.test_calc::test_add": "passed",
        "tests.test_calc::test_add_zero": "passed",
    }


def test_run_pytest_junit_outcomes(tmp_path):
    tgt = tmp_path / "outcomes"
    (tgt / "tests").mkdir(parents=True)
    (tgt / "tests" / "test_outcomes.py").write_text(OUTCOMES_SRC)
    got = ps.run_pytest_junit(str(tgt), ["tests/test_outcomes.py"], timeout=240)
    assert got == {
        "tests.test_outcomes::test_error": "error",
        "tests.test_outcomes::test_failed": "failed",
        "tests.test_outcomes::test_skipped": "skipped",
        "tests.test_outcomes::test_passed": "passed",
    }


def test_run_pytest_junit_timeout_returns_none(tmp_path, monkeypatch):
    # Some operator environments auto-load a plugin that skips tests named
    # "slow" unless an opt-in flag is passed; the grading run must measure the
    # timeout itself, so keep third-party plugins out of this nested pytest.
    monkeypatch.setenv("PYTEST_DISABLE_PLUGIN_AUTOLOAD", "1")
    tgt = tmp_path / "slow"
    (tgt / "tests").mkdir(parents=True)
    (tgt / "tests" / "test_slow.py").write_text(SLOW_SRC)
    assert ps.run_pytest_junit(str(tgt), ["tests/test_slow.py"], timeout=5) is None


def test_run_pytest_junit_invocation_contract(tmp_path, monkeypatch):
    tgt = tmp_path / "argv"
    (tgt / "tests").mkdir(parents=True)
    (tgt / "tests" / "a.py").write_text("")
    monkeypatch.setenv("PYTHONSAFEPATH", "1")
    monkeypatch.setenv("MO_SPEC_SENTINEL", "yes")

    captured = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = list(cmd)
        captured["kwargs"] = kwargs
        return subprocess.CompletedProcess(cmd, returncode=0)

    monkeypatch.setattr(subprocess, "run", fake_run)
    test_files = ["tests/a.py", "tests/b.py"]
    got = ps.run_pytest_junit(str(tgt), test_files, timeout=42)

    assert got is None  # no junit.xml was ever written
    cmd = captured["cmd"]
    assert cmd[0] == sys.executable
    assert cmd[1:6] == ["-m", "pytest", "-q", "-p", "no:cacheprovider"]
    assert cmd[6].startswith("--junitxml=")
    assert cmd[7:] == test_files
    kwargs = captured["kwargs"]
    assert kwargs.get("cwd") == str(tgt)
    assert kwargs.get("capture_output") is True
    assert kwargs.get("timeout") == 42
    env = kwargs.get("env")
    assert isinstance(env, dict)
    assert "PYTHONSAFEPATH" not in env
    assert env.get("MO_SPEC_SENTINEL") == "yes"
    assert env.get("MINI_ORK_ROOT") == str(tgt)
    assert env.get("PYTHONDONTWRITEBYTECODE") == "1"
    assert os.path.basename(env.get("MINI_ORK_HOME", "")) == "home"
    assert env.get("MINI_ORK_DB") == os.path.join(
        env.get("MINI_ORK_HOME", ""), "state.db"
    )


# ── _grade_git_oracle ──────────────────────────────────────────────────────


def test_grade_false_then_true_after_fix(git_root, monkeypatch, tmp_path):
    root, base_sha, fix_sha = git_root
    t = Path(_snapshot(tmp_path, monkeypatch, base_sha))
    oracle = _sidecar(root)
    assert ps._grade_git_oracle(str(t), oracle) is False
    blob = subprocess.run(
        ["git", "-C", str(root), "show", f"{fix_sha}:tests/test_calc.py"],
        capture_output=True,
        check=True,
    ).stdout
    assert (t / "tests" / "test_calc.py").read_bytes() == blob
    (t / "calc.py").write_text(FIXED_CALC)
    assert ps._grade_git_oracle(str(t), oracle) is True


def test_grade_overlay_beats_tampered_test(git_root, monkeypatch, tmp_path):
    root, base_sha, _ = git_root
    t = Path(_snapshot(tmp_path, monkeypatch, base_sha))
    (t / "tests").mkdir()
    (t / "tests" / "test_calc.py").write_text("def test_add():\n    assert True\n")
    assert ps._grade_git_oracle(str(t), _sidecar(root)) is False
    assert (t / "tests" / "test_calc.py").read_text() == TEST_CALC_SRC


def test_grade_overlay_creates_parent_dirs(git_root, monkeypatch, tmp_path):
    root, base_sha, fix_sha = git_root
    t = Path(_snapshot(tmp_path, monkeypatch, base_sha))
    (t / "calc.py").write_text(FIXED_CALC)
    nested = {
        "base_sha": base_sha,
        "fix_sha": fix_sha,
        "test_files": ["tests/nested/test_deep.py"],
        "fail_to_pass": ["tests.nested.test_deep::test_deep"],
        "pass_to_pass": [],
    }
    assert ps._grade_git_oracle(str(t), nested) is True
    assert (t / "tests" / "nested" / "test_deep.py").read_text() == TEST_DEEP_SRC


def test_grade_missing_test_file_returns_false(git_root, monkeypatch, tmp_path):
    root, base_sha, _ = git_root
    t = Path(_snapshot(tmp_path, monkeypatch, base_sha))
    called = []

    def fake_rpj(target, test_files, *, timeout):
        called.append((target, test_files, timeout))
        return {"tests.missing::test_x": "passed"}

    monkeypatch.setattr(ps, "run_pytest_junit", fake_rpj)
    oracle = {**_sidecar(root), "test_files": ["tests/missing.py"]}
    assert ps._grade_git_oracle(str(t), oracle) is False
    assert called == []
    assert not (t / "tests" / "missing.py").exists()


def test_grade_timeout_source_and_none_results(git_root, monkeypatch, tmp_path):
    root, base_sha, _ = git_root
    t = Path(_snapshot(tmp_path, monkeypatch, base_sha))
    seen = {}

    def fake_rpj(target, test_files, *, timeout):
        seen["timeout"] = timeout
        seen["overlay"] = os.path.exists(
            os.path.join(target, "tests", "test_calc.py")
        )
        return None

    monkeypatch.setattr(ps, "run_pytest_junit", fake_rpj)
    oracle = _sidecar(root)

    monkeypatch.setenv("MO_APPLY_PROBE_GRADE_TIMEOUT_S", "12.5")
    assert ps._grade_git_oracle(str(t), oracle) is False
    assert seen["timeout"] == 12.5
    assert seen["overlay"] is True

    monkeypatch.delenv("MO_APPLY_PROBE_GRADE_TIMEOUT_S", raising=False)
    assert ps._grade_git_oracle(str(t), oracle) is False
    assert seen["timeout"] == 600.0

    assert ps._grade_git_oracle(str(t), oracle, timeout=7) is False
    assert seen["timeout"] == 7


@pytest.mark.parametrize(
    "results,expected",
    [
        pytest.param(
            {
                "tests.test_calc::test_add": "passed",
                "tests.test_calc::test_add_zero": "passed",
            },
            True,
            id="all-passed",
        ),
        pytest.param(
            {
                "tests.test_calc::test_add": "passed",
                "tests.test_calc::test_add_zero": "failed",
            },
            False,
            id="pass-to-pass-failed",
        ),
        pytest.param(
            {"tests.test_calc::test_add": "failed"}, False, id="fail-to-pass-failed"
        ),
        pytest.param(
            {"tests.test_calc::test_add": "passed"}, False, id="id-absent"
        ),
    ],
)
def test_grade_final_predicate(git_root, monkeypatch, tmp_path, results, expected):
    root, base_sha, _ = git_root
    t = Path(_snapshot(tmp_path, monkeypatch, base_sha))
    monkeypatch.setattr(
        ps, "run_pytest_junit", lambda target, test_files, *, timeout: results
    )
    assert ps._grade_git_oracle(str(t), _sidecar(root)) is expected


# ── _probe_outcome contracts ───────────────────────────────────────────────


def test_probe_outcome_plain_probe(git_root, monkeypatch):
    root, _, _ = git_root
    monkeypatch.setattr(ps, "_run_outcome", lambda run_id: 1.0)
    assert ps._probe_outcome(str(_probes_dir(root) / "probe-1.md"), "r1", None) == (
        1.0,
        None,
    )
    assert ps._probe_outcome(
        str(_probes_dir(root) / "probe-1.md"), "r1", None, db=None
    ) == (1.0, None)


def _grader_must_not_run(monkeypatch):
    def boom(*args, **kwargs):
        raise AssertionError("_grade_git_oracle must not be called")

    monkeypatch.setattr(ps, "_grade_git_oracle", boom)


def test_probe_outcome_unpublished_skips_grader(git_root, monkeypatch, tmp_path):
    root, _, _ = git_root
    monkeypatch.setattr(ps, "_run_outcome", lambda run_id, db=None: 0.0)
    _grader_must_not_run(monkeypatch)
    assert ps._probe_outcome(
        str(_probes_dir(root) / "probe-h01.md"), "r1", str(tmp_path)
    ) == (0.0, False)


def test_probe_outcome_falsy_target_skips_grader(git_root, monkeypatch):
    root, _, _ = git_root
    monkeypatch.setattr(ps, "_run_outcome", lambda run_id, db=None: 1.0)
    _grader_must_not_run(monkeypatch)
    assert ps._probe_outcome(
        str(_probes_dir(root) / "probe-h01.md"), "r1", None
    ) == (0.0, False)


def test_probe_outcome_hidden_results(git_root, monkeypatch, tmp_path):
    root, _, _ = git_root
    monkeypatch.setattr(ps, "_run_outcome", lambda run_id, db=None: 1.0)
    monkeypatch.setattr(ps, "_grade_git_oracle", lambda target, oracle: True)
    assert ps._probe_outcome(
        str(_probes_dir(root) / "probe-h01.md"), "r1", str(tmp_path)
    ) == (1.0, True)
    monkeypatch.setattr(ps, "_grade_git_oracle", lambda target, oracle: False)
    assert ps._probe_outcome(
        str(_probes_dir(root) / "probe-h01.md"), "r1", str(tmp_path)
    ) == (0.0, False)


def test_probe_outcome_db_plumbing(git_root, monkeypatch, tmp_path):
    root, _, _ = git_root
    seen = {}

    def fake_outcome(run_id, db=None):
        seen["db"] = db
        return 1.0

    monkeypatch.setattr(ps, "_run_outcome", fake_outcome)
    monkeypatch.setattr(ps, "_grade_git_oracle", lambda target, oracle: True)
    assert ps._probe_outcome(
        str(_probes_dir(root) / "probe-h01.md"), "r1", str(tmp_path), db="x.db"
    ) == (1.0, True)
    assert seen["db"] == "x.db"


# ── end to end through probe_score ─────────────────────────────────────────


def test_probe_score_end_to_end_flag_on(git_root, monkeypatch, tmp_path):
    root, _, _ = git_root
    res, _calls, scratch = _drive_probe_score(
        monkeypatch, tmp_path, root, flag_on=True
    )
    assert json.loads(res["pertask_json"]) == {
        "before": [1, 0],
        "after": [1, 1],
        "ids": ["probe-1.md", "probe-h01.md"],
    }
    entries = _run_dicts(res)
    h01 = [e for e in entries if "probe-h01.md" in str(e) and "hidden_pass" in e]
    assert sorted(e["hidden_pass"] for e in h01) == [False, True]
    plain = [e for e in entries if "probe-1.md" in str(e)]
    assert plain, "expected run entries for probe-1.md"
    assert all("hidden_pass" not in e for e in plain)
    assert os.listdir(str(scratch)) == []


def test_probe_score_end_to_end_flag_off(git_root, monkeypatch, tmp_path):
    root, _, _ = git_root
    res, _calls, scratch = _drive_probe_score(
        monkeypatch, tmp_path, root, flag_on=False
    )
    assert json.loads(res["pertask_json"])["ids"] == ["probe-1.md"]
    assert all("hidden_pass" not in e for e in _collect_dicts(res))
    assert os.listdir(str(scratch)) == []


def test_probe_score_plain_directory_unchanged_flag_off(
    git_root, monkeypatch, tmp_path
):
    root, _, _ = git_root
    plain = root / "recipes" / "plain"
    (plain / "prompts").mkdir(parents=True)
    (plain / "prompts" / "implementer.md").write_text("base\n")
    (plain / "probes").mkdir()
    (plain / "probes" / "p1.md").write_text("# p\n")
    res, _calls, scratch = _drive_probe_score(
        monkeypatch, tmp_path, root, flag_on=False, recipe="plain"
    )
    assert json.loads(res["pertask_json"])["ids"] == ["p1.md"]
    assert json.loads(res["pertask_json"])["before"] == [1]
    assert json.loads(res["pertask_json"])["after"] == [1]
    assert all("hidden_pass" not in e for e in _collect_dicts(res))
    assert os.listdir(str(scratch)) == []
