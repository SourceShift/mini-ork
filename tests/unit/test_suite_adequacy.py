"""Tests for the code-fix suite-adequacy gate (VT5, G03-T04).

Two sections:

1. Hermetic module tests (T1-T8) — each stands up a throwaway package in
   ``tmp_path`` (``calc.py`` + ``tests/test_calc.py`` + an empty ``conftest.py``)
   and drives ``mini_ork.gates.suite_adequacy`` directly. No LLM, no network.

2. Real-verifier tests (T9-T11) — drive the production entrypoint
   ``recipes/code-fix/verifiers/test.py`` as a subprocess against a throwaway
   git repo, the same harness shape as ``test_codefix_replay.py``.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from mini_ork.gates import suite_adequacy
from mini_ork.gates.suite_adequacy import OPERATORS

REPO_ROOT = Path(__file__).resolve().parents[2]
VERIFIER = REPO_ROOT / "recipes" / "code-fix" / "verifiers" / "test.py"

# Hermetic: quiet is fine — the audit only reads exit codes.
CMD = f"{sys.executable} -m pytest -q -p no:cacheprovider"

# Real verifier: NO `-q` — replay_check parses per-test lines (and pytest
# resolves conflicting verbosity flags by taking the LAST one).
MINI_ORK_TEST_CMD = f"{sys.executable} -m pytest -p no:cacheprovider"


# ─────────────────────────────────────────────────────────────────────────────
# Section 1 — hermetic module tests
# ─────────────────────────────────────────────────────────────────────────────

CALC = '''\
def add(a, b):
    return a + b


def sub(a, b):
    return a - b


def mul(a, b):
    return a * b


def div(a, b):
    if b == 0:
        return None
    return a / b


def positive(x):
    if x > 0:
        return True
    return False


def classify(n):
    if n < 10 and n > 0:
        return "small"
    return "big"


def inc(n):
    return n + 1
'''

STRONG_TEST = '''\
from calc import add, sub, mul, div, positive, classify, inc


def test_add():
    assert add(2, 3) == 5


def test_sub():
    assert sub(5, 2) == 3


def test_mul():
    assert mul(3, 4) == 12


def test_div():
    assert div(6, 2) == 3
    assert div(1, 0) is None


def test_positive():
    assert positive(5) is True
    assert positive(-1) is False


def test_classify():
    assert classify(5) == "small"
    assert classify(20) == "big"


def test_inc():
    assert inc(1) == 2
'''

WEAK_TEST = '''\
from calc import add, sub, mul, div, positive, classify, inc


def test_calls():
    add(2, 3)
    sub(5, 2)
    mul(3, 4)
    div(6, 2)
    div(1, 0)
    positive(5)
    positive(-1)
    classify(5)
    classify(20)
    inc(1)
'''

GUARD_CALC = '''\
_LIMIT = 10

if _LIMIT < 0:
    raise ImportError("mini-ork guard canary")


def double(x):
    return x * 2
'''

GUARD_TEST = '''\
from calc import double


def test_double():
    assert double(3) == 6
'''

RED_TEST = '''\
from calc import add


def test_add_wrong():
    assert add(2, 3) == 999
'''

NEVER_IMPORTS_TEST = '''\


def test_nothing():
    assert 1 + 1 == 2
'''


def _make_package(tmp_path: Path, calc_src: str, test_src: str) -> Path:
    pkg = tmp_path / "pkg"
    (pkg / "tests").mkdir(parents=True)
    (pkg / "calc.py").write_text(calc_src)
    (pkg / "tests" / "test_calc.py").write_text(test_src)
    (pkg / "conftest.py").write_text("")
    return pkg


def _snap_files(root: Path) -> dict:
    return {
        str(p.relative_to(root)): p.read_bytes()
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


def test_strong_suite_adequate(tmp_path):
    pkg = _make_package(tmp_path, CALC, STRONG_TEST)
    res = suite_adequacy.audit_suite(str(pkg), CMD, ["calc.py"])
    assert res["verdict"] == "ADEQUATE", res
    assert res["score"] >= 0.6, res
    assert res["killed"] + res["survived"] >= 3, res


def test_weak_suite_inadequate(tmp_path):
    pkg = _make_package(tmp_path, CALC, WEAK_TEST)
    res = suite_adequacy.audit_suite(str(pkg), CMD, ["calc.py"])
    assert res["verdict"] == "INADEQUATE", res
    assert res["survivors"], res
    for s in res["survivors"]:
        assert s["file"] == "calc.py", s
        assert isinstance(s["line"], int) and s["line"] >= 1, s
        assert s["operator"] in OPERATORS, s


def test_guard_mutants_invalid_and_math_consistent(tmp_path):
    pkg = _make_package(tmp_path, GUARD_CALC, GUARD_TEST)
    res = suite_adequacy.audit_suite(str(pkg), CMD, ["calc.py"], max_mutants=50)
    ms = res["mutants"]
    assert res["total"] == res["killed"] + res["survived"] + res["invalid"], res
    assert res["killed"] + res["survived"] >= 1, res
    assert res["score"] == round(res["killed"] / (res["killed"] + res["survived"]), 3), res
    assert [m for m in ms if m["operator"] == "cmp_flip" and m["outcome"] == "invalid"], res
    assert [m for m in ms if m["operator"] == "cond_true" and m["outcome"] == "invalid"], res


def test_baseline_red_unverified(tmp_path):
    pkg = _make_package(tmp_path, CALC, RED_TEST)
    res = suite_adequacy.audit_suite(str(pkg), CMD, ["calc.py"])
    assert res["verdict"] == "UNVERIFIED", res
    assert res["reason"].startswith("baseline-red"), res
    assert res["mutants"] == [], res


def test_canary_undetected_unverified(tmp_path):
    pkg = _make_package(tmp_path, CALC, NEVER_IMPORTS_TEST)
    res = suite_adequacy.audit_suite(str(pkg), CMD, ["calc.py"])
    assert res["verdict"] == "UNVERIFIED", res
    assert res["reason"].startswith("canary-undetected"), res


def test_live_tree_untouched(tmp_path, monkeypatch):
    pkg = _make_package(tmp_path, CALC, STRONG_TEST)
    before = _snap_files(pkg)

    made = []
    real_mkdtemp = tempfile.mkdtemp

    def _spy_mkdtemp(*a, **k):
        d = real_mkdtemp(*a, **k)
        made.append(d)
        return d

    monkeypatch.setattr(tempfile, "mkdtemp", _spy_mkdtemp)
    res = suite_adequacy.audit_suite(str(pkg), CMD, ["calc.py"])
    assert res["verdict"] == "ADEQUATE", res

    assert _snap_files(pkg) == before, "live tree file set or contents changed"
    assert not [p for p in pkg.rglob("__pycache__")], "audit wrote __pycache__ into the live tree"
    assert made, "audit never created a temp dir"
    assert not os.path.exists(made[0]), "audit temp dir was not removed"


def test_generate_mutants_deterministic_and_capped(tmp_path):
    pkg = _make_package(tmp_path, CALC, STRONG_TEST)
    a = suite_adequacy.generate_mutants(str(pkg), ["calc.py"], max_mutants=4, seed=0)
    b = suite_adequacy.generate_mutants(str(pkg), ["calc.py"], max_mutants=4, seed=0)
    fa = [(m.id, m.file, m.line, m.col, m.operator, m.source) for m in a]
    fb = [(m.id, m.file, m.line, m.col, m.operator, m.source) for m in b]
    assert fa == fb
    assert len(a) == 4
    assert len({m.operator for m in a}) >= 3


def test_enabled_knob():
    assert suite_adequacy.enabled({}) is True
    assert suite_adequacy.enabled({"MO_SUITE_ADEQUACY": "0"}) is False
    assert suite_adequacy.enabled({"MO_SUITE_ADEQUACY": "1"}) is True
    assert suite_adequacy.enabled({"MO_SUITE_ADEQUACY": "true"}) is False


def test_settings_defaults():
    s = suite_adequacy.settings({})
    assert s["max_mutants"] == 12
    assert s["min_score"] == 0.6
    assert s["timeout_s"] == 300.0


def test_settings_clamp_and_fallback():
    s = suite_adequacy.settings({
        "MO_SUITE_ADEQUACY_MAX_MUTANTS": "1000",
        "MO_SUITE_ADEQUACY_MIN_SCORE": "-5",
        "MO_SUITE_ADEQUACY_TIMEOUT_S": "0",
    })
    assert s["max_mutants"] == 50
    assert s["min_score"] == 0.0
    assert s["timeout_s"] == 1.0

    s2 = suite_adequacy.settings({
        "MO_SUITE_ADEQUACY_MAX_MUTANTS": "garbage",
        "MO_SUITE_ADEQUACY_MIN_SCORE": "nope",
        "MO_SUITE_ADEQUACY_TIMEOUT_S": "also-nope",
    })
    assert s2["max_mutants"] == 12
    assert s2["min_score"] == 0.6
    assert s2["timeout_s"] == 300.0


# ─────────────────────────────────────────────────────────────────────────────
# Section 2 — real verifier subprocess tests (harness copied from
# test_codefix_replay.py)
# ─────────────────────────────────────────────────────────────────────────────

MOD_HEAD = '''\
def add(a, b):
    return a - b


def mul(a, b):
    return a * b


def sub(a, b):
    return a - b
'''

MOD_FIXED = '''\
def add(a, b):
    return a + b


def mul(a, b):
    return a * b


def sub(a, b):
    return a - b
'''

TEST_STRONG = '''\
from mod import add, mul, sub


def test_add():
    assert add(2, 3) == 5


def test_mul():
    assert mul(3, 4) == 12


def test_sub():
    assert sub(5, 2) == 3
'''

TEST_WEAK = '''\
from mod import add


def test_add():
    assert add(2, 3) == 5
'''

# Reads a plain-text config file so a fix that changes ONLY config.yaml is the
# whole delta between a green candidate and a red base (replay overlap).
CONFIG_TEST = '''\
import os


def test_threshold():
    path = os.path.join(os.path.dirname(__file__), "config.yaml")
    with open(path) as fh:
        value = int(fh.read().strip().split("=")[1])
    assert value == 2
'''


def _git(cwd: Path, *args: str) -> None:
    subprocess.check_call(
        ["git", *args], cwd=cwd,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )


def _git_text(cwd: Path, *args: str) -> str:
    return subprocess.check_output(["git", *args], cwd=cwd, text=True)


def _make_repo(parent: Path, *, mod_src: str, test_src: str) -> Path:
    """Stand up a throwaway git repo with the given module + test (initial commit)."""
    repo = parent / "r"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    _git(repo, "config", "core.autocrlf", "false")
    (repo / "mod.py").write_text(mod_src)
    (repo / "test_mod.py").write_text(test_src)
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "init")
    return repo


def _invoke(repo: Path, tmp_path: Path, *, replay: str, run_id: str,
            adequacy: str | None = None, timeout: int = 300):
    mini_home = tmp_path / "mo-home"
    mini_home.mkdir(exist_ok=True)
    env = os.environ.copy()
    env["MINI_ORK_HOME"] = str(mini_home)
    env["MINI_ORK_RUN_ID"] = run_id
    env["MO_CODEFIX_REPLAY"] = replay
    env.pop("MO_SUITE_ADEQUACY", None)  # DEFAULT ON now; only set when pinned
    if adequacy is not None:
        env["MO_SUITE_ADEQUACY"] = adequacy
    env["MINI_ORK_TEST_CMD"] = MINI_ORK_TEST_CMD
    env["PYTHONPATH"] = str(REPO_ROOT)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return subprocess.run(
        [sys.executable, str(VERIFIER)], cwd=repo, env=env,
        capture_output=True, text=True, timeout=timeout,
    )


def _run_verifier(repo: Path, tmp_path: Path, *, replay: str, run_id: str,
                  adequacy: str = "0") -> tuple[dict, int]:
    """Invoke the verifier on ``repo``. Returns ``(parsed JSON envelope, rc)``."""
    proc = _invoke(repo, tmp_path, replay=replay, run_id=run_id, adequacy=adequacy)
    last = proc.stdout.strip().splitlines()
    assert last, (
        f"verifier produced no JSON: rc={proc.returncode} "
        f"stdout={proc.stdout!r} stderr={proc.stderr!r}"
    )
    return json.loads(last[-1]), proc.returncode


def _snap_worktree(repo: Path) -> tuple[dict, str]:
    """Snapshot (relative-path -> bytes) for files OUTSIDE .git, plus porcelain status."""
    files = {
        p.relative_to(repo): p.read_bytes()
        for p in repo.rglob("*")
        if p.is_file() and not str(p.relative_to(repo)).startswith(".git")
    }
    status = _git_text(repo, "status", "--porcelain")
    return files, status


def test_knob_off_byte_identical(tmp_path):
    repo = _make_repo(tmp_path, mod_src=MOD_HEAD, test_src=TEST_STRONG)
    (repo / "mod.py").write_text(MOD_FIXED)

    proc_unset = _invoke(repo, tmp_path, replay="1", run_id="knob-off", adequacy="0")
    proc_zero = _invoke(repo, tmp_path, replay="1", run_id="knob-off", adequacy="0")

    last_unset = proc_unset.stdout.strip().splitlines()[-1]
    last_zero = proc_zero.stdout.strip().splitlines()[-1]
    assert last_unset == last_zero

    out = json.loads(last_unset)
    assert set(out.keys()) == {"verifier", "pass", "evidence_path",
                               "error_summary", "post_rc", "base_rc", "replay"}
    assert out["error_summary"] == "post-patch suite green; replay: tests exercise the change"


def test_knob_on_strong_suite_adequate(tmp_path):
    repo = _make_repo(tmp_path, mod_src=MOD_HEAD, test_src=TEST_STRONG)
    (repo / "mod.py").write_text(MOD_FIXED)
    tests_dir = repo / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_extra.py").write_text(
        "from mod import add\n\ndef test_extra():\n    assert add(0, 0) == 0\n"
    )

    before_files, before_status = _snap_worktree(repo)
    run_id = "adequacy-strong"
    out, _ = _run_verifier(repo, tmp_path, replay="1", run_id=run_id, adequacy="1")

    assert out["pass"] is True, out
    sa = out["suite_adequacy"]
    assert sa["verdict"] == "ADEQUATE", sa
    assert sa["files"] == ["mod.py"], sa
    report = tmp_path / "mo-home" / "runs" / run_id / "suite_adequacy.json"
    assert report.is_file(), "suite_adequacy.json was not written to LOG_DIR"

    after_files, after_status = _snap_worktree(repo)
    assert before_files == after_files, "file set or contents changed"
    assert before_status == after_status, (
        f"git status changed\nbefore={before_status!r}\nafter={after_status!r}"
    )


def test_knob_on_weak_suite_inadequate(tmp_path):
    repo = _make_repo(tmp_path, mod_src=MOD_HEAD, test_src=TEST_WEAK)
    (repo / "mod.py").write_text(MOD_FIXED)

    out, rc = _run_verifier(repo, tmp_path, replay="1", run_id="adequacy-weak", adequacy="1")

    assert rc == 0, out
    assert out["pass"] is False, out
    assert out["adequacy_unverified"] is True, out
    assert "replay_unverified" not in out, out
    assert out["error_summary"].startswith("unverified: suite-inadequate"), out
    assert out["replay"]["overlap"] != [], out["replay"]
    assert out["suite_adequacy"]["survivors"] != [], out["suite_adequacy"]


def test_no_sources_not_applicable(tmp_path):
    # Only non-.py files in scope -> the instrument does not apply.
    pkg = _make_package(tmp_path, CALC, STRONG_TEST)
    res = suite_adequacy.audit_suite(str(pkg), CMD, ["readme.md", "config.yaml"])
    assert res["verdict"] == "NOT_APPLICABLE", res
    assert res["reason"].startswith("no-sources:"), res
    assert res["mutants"] == [], res


def test_too_few_sites_not_applicable(tmp_path):
    # A one-line change (`X = 1`) has one mutation site, fewer than MIN_VALID.
    pkg = _make_package(tmp_path, "X = 1\n", "def test_x():\n    assert True\n")
    res = suite_adequacy.audit_suite(str(pkg), CMD, ["calc.py"])
    assert res["verdict"] == "NOT_APPLICABLE", res
    assert res["reason"] == "too-few-sites: 1", res


def test_mutant_cap_below_min_valid_is_not_not_applicable(tmp_path, monkeypatch):
    # A MAX_MUTANTS cap below MIN_VALID must not masquerade as "too few sites":
    # the code has plenty of sites, so the audit cannot measure -> UNVERIFIED,
    # never NOT_APPLICABLE (which would silently keep every green).
    pkg = _make_package(tmp_path, CALC, STRONG_TEST)
    monkeypatch.setenv("MO_SUITE_ADEQUACY_MAX_MUTANTS", "2")
    res = suite_adequacy.audit_suite(str(pkg), CMD, ["calc.py"])
    assert res["verdict"] == "UNVERIFIED", res


def test_yaml_only_fix_not_applicable(tmp_path):
    # Drive the REAL verifier with the knob UNSET: a fix that touches only a
    # .yaml file has no .py in scope for the audit, so the green is kept via
    # NOT_APPLICABLE and `adequacy_unverified` must stay absent.
    repo = _make_repo(
        tmp_path,
        mod_src="def f():\n    return 1\n",
        test_src=CONFIG_TEST,
    )
    (repo / "config.yaml").write_text("threshold=1\n")
    _git(repo, "add", "config.yaml")
    _git(repo, "commit", "-q", "-m", "add config")
    (repo / "config.yaml").write_text("threshold=2\n")  # uncommitted non-.py fix

    proc = _invoke(repo, tmp_path, replay="1", run_id="yaml-only", adequacy=None)
    last = proc.stdout.strip().splitlines()
    assert last, (
        f"verifier produced no JSON: rc={proc.returncode} "
        f"stdout={proc.stdout!r} stderr={proc.stderr!r}"
    )
    out = json.loads(last[-1])

    assert out["pass"] is True, out
    assert out["suite_adequacy"]["verdict"] == "NOT_APPLICABLE", out
    assert "adequacy_unverified" not in out, out
