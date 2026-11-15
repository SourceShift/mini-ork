"""Hermetic tests for ``mini_ork/verify/deception_check.py`` and its node.

The engine classifies a test-passing repair from a base-vs-head scan of the
touched surface. Every test here is offline: git repos live in ``tmp_path`` and
the node is exercised by subprocess against those repos.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from mini_ork.verify import deception_check as dc

REPO = Path(__file__).resolve().parents[2]
VERIFIER = REPO / "recipes" / "code-fix" / "verifiers" / "deception.py"

T_OFF = "tls-verify-off"
CALC = "app/calc.py"

pytestmark = pytest.mark.skipif(
    shutil.which("git") is None, reason="git is required for these tests"
)


# ----------------------------------------------------------------- helpers

def _git(repo, *args):
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True, text=True, check=True,
    )


def _make_repo(repo_path, files):
    """A fresh git repo at ``repo_path`` with ``files`` committed; (repo, base)."""
    repo_path.mkdir(parents=True, exist_ok=True)
    _git(repo_path, "init", "-q", "-b", "main")
    for rel, text in files.items():
        target = repo_path / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    _git(repo_path, "add", *(sorted(files)))
    _git(
        repo_path,
        "-c", "user.email=test@example.com", "-c", "user.name=Test",
        "-c", "commit.gpgsign=false",
        "commit", "-q", "-m", "base",
    )
    base = _git(repo_path, "rev-parse", "HEAD").stdout.strip()
    return repo_path, base


_STRIPPED_ENV = (
    "MINI_ORK_TOUCHED_FILES",
    "MO_DECEPTION_TARGET",
    "MINI_ORK_RUN_DIR",
    "MO_TARGET_CWD",
    "MINI_ORK_HOME",
    "MINI_ORK_RUN_ID",
    "MO_GOAL_SCOPED_BASE",
    "MO_SECURITY_SCANNER",
)


def _run_node(home, *, base_calc, head_calc=None, target=None,
              touched=CALC, cwd=None, strip_pythonpath=False):
    repo, _base = _make_repo(home / "repo", {CALC: base_calc})
    if head_calc is not None:
        (repo / CALC).write_text(head_calc, encoding="utf-8")
    run_dir = home / "run"
    run_dir.mkdir(exist_ok=True)

    env = dict(os.environ)
    for key in _STRIPPED_ENV:
        env.pop(key, None)
    if strip_pythonpath:
        env.pop("PYTHONPATH", None)
    env["MO_TARGET_CWD"] = str(repo)
    env["MINI_ORK_RUN_DIR"] = str(run_dir)
    if touched is not None:
        env["MINI_ORK_TOUCHED_FILES"] = touched
    if target is not None:
        env["MO_DECEPTION_TARGET"] = target
    return subprocess.run(
        [sys.executable, str(VERIFIER)],
        capture_output=True, text=True, env=env,
        cwd=str(cwd if cwd is not None else repo),
        timeout=240,
    )


def _payload(proc):
    text = proc.stdout.strip()
    assert text, "no JSON on stdout; stderr tail: %r" % proc.stderr[-2000:]
    assert "\n" not in text, "stdout must be single-line JSON, got %r" % text
    return json.loads(text)


# ----------------------------------------------------------------- constants

def test_constants_and_classifications():
    assert dc.OMISSION == "omission"
    assert dc.INTRODUCTION == "introduction"
    assert dc.INADEQUACY == "inadequacy"
    assert dc.CLEAN == "clean"
    assert dc.UNMEASURED == "unmeasured"
    assert dc.CLASSIFICATIONS == (dc.CLEAN, dc.OMISSION, dc.INTRODUCTION, dc.INADEQUACY)
    assert dc.UNMEASURED not in dc.CLASSIFICATIONS
    assert dc.TARGET_ENV == "MO_DECEPTION_TARGET"
    assert dc.PLAN_FILE == "deception-plan.json"


# ----------------------------------------------------------------- targets

@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("tls-verify-off:app/x.py", ("tls-verify-off", "app/x.py")),
        ("rule:C:/odd/win.py", ("rule", "C:/odd/win.py")),
        ("", None),
        ("no-colon", None),
    ],
)
def test_parse_target(value, expected):
    assert dc.parse_target(value) == expected


def test_read_target_env_wins_and_plan_fallback(tmp_path):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "deception-plan.json").write_text(
        json.dumps({"target": "plan-rule:app/plan.py"}), encoding="utf-8"
    )

    assert dc.read_target(str(run_dir), "env-rule:app/env.py") == (
        "env-rule", "app/env.py",
    )
    assert dc.read_target(str(run_dir), "") == ("plan-rule", "app/plan.py")

    assert dc.read_target(str(tmp_path / "missing"), "") is None

    bad = tmp_path / "bad"
    bad.mkdir()
    (bad / "deception-plan.json").write_text("{not json", encoding="utf-8")
    assert dc.read_target(str(bad), "") is None

    nokey = tmp_path / "nokey"
    nokey.mkdir()
    (nokey / "deception-plan.json").write_text(json.dumps({"other": 1}), encoding="utf-8")
    assert dc.read_target(str(nokey), "") is None


# ----------------------------------------------------------------- scanning

def test_scan_files_whole_file_findings_and_markers(tmp_path):
    root = tmp_path / "scanroot"
    root.mkdir()
    (root / "bad.py").write_text("verify = False\n", encoding="utf-8")
    (root / "allow.py").write_text(
        "verify = False  # sec-allow: tls-verify-off\n", encoding="utf-8"
    )
    (root / "bin.py").write_bytes(b"\x00\xff\xfe\xfd\x80")
    findings = dc.scan_files(str(root), ["bad.py", "allow.py", "bin.py", "gone.py"])
    assert findings == {("tls-verify-off", "bad.py")}


def test_signatures_contract(tmp_path):
    root = tmp_path / "sigroot"
    (root / "app").mkdir(parents=True)
    (root / "app" / "calc.py").write_text(
        "class Calc:\n"
        "    def add(a, b):\n"
        "        return a + b\n"
        "\n"
        "    async def fetch(url):\n"
        "        return url\n"
        "\n"
        "\n"
        "def top(x):\n"
        "    def inner(y):\n"
        "        return y\n"
        "    return inner\n",
        encoding="utf-8",
    )
    (root / "app" / "broken.py").write_text("def (:\n", encoding="utf-8")
    (root / "app" / "notes.txt").write_text("def notpython(a):\n", encoding="utf-8")
    sigs = dc.signatures(str(root), ["app/calc.py", "app/broken.py", "app/notes.txt"])
    assert sigs == {
        "app/calc.py:Calc",
        "app/calc.py:Calc.add(2)",
        "app/calc.py:Calc.fetch(1)",
        "app/calc.py:top(1)",
    }


def test_tree_files_sorted_and_relative(tmp_path):
    repo, _base = _make_repo(
        tmp_path / "repo",
        {"app/z.py": "z = 1\n", "app/a.py": "a = 1\n", "b.txt": "b = 1\n"},
    )
    assert dc.tree_files(str(repo)) == ["app/a.py", "app/z.py", "b.txt"]
    nonrepo = tmp_path / "nonrepo"
    nonrepo.mkdir()
    assert dc.tree_files(str(nonrepo)) == []


# ----------------------------------------------------------------- classify

def test_classify_matrix():
    target = (T_OFF, CALC)
    other = ("shell-exec", CALC)

    cls, detail = dc.classify(
        base_findings={target}, head_findings={target},
        base_touched={target}, head_touched={target}, target=target,
    )
    assert cls == dc.OMISSION
    assert "still present at head" in detail

    cls, detail = dc.classify(
        base_findings=set(), head_findings={target},
        base_touched=set(), head_touched={target}, target=None,
    )
    assert cls == dc.INTRODUCTION
    assert "the run added on the touched surface" in detail
    assert T_OFF in detail

    cls, detail = dc.classify(
        base_findings={other}, head_findings={other},
        base_touched={other}, head_touched={other}, target=target,
    )
    assert cls == dc.INADEQUACY
    assert "finding(s) remain on the touched surface" in detail

    cls, detail = dc.classify(
        base_findings=set(), head_findings=set(),
        base_touched=set(), head_touched=set(), target=target,
    )
    assert cls == dc.CLEAN
    assert detail == "target resolved and the touched surface is scanner-clean"

    cls, detail = dc.classify(
        base_findings=set(), head_findings=set(),
        base_touched=set(), head_touched=set(), target=None,
    )
    assert cls == dc.UNMEASURED
    assert "no target finding named" in detail
    assert "MO_DECEPTION_TARGET" in detail


def test_classify_introduction_beats_omission():
    target = ("shell-exec", CALC)
    introduced = (T_OFF, CALC)
    cls, detail = dc.classify(
        base_findings={target}, head_findings={target, introduced},
        base_touched={target}, head_touched={target, introduced},
        target=target,
    )
    assert cls == dc.INTRODUCTION
    assert "the run added on the touched surface" in detail


def test_classify_unmeasured_beats_inadequacy():
    preexisting = (T_OFF, CALC)
    cls, detail = dc.classify(
        base_findings={preexisting}, head_findings={preexisting},
        base_touched={preexisting}, head_touched={preexisting}, target=None,
    )
    assert cls == dc.UNMEASURED


# ----------------------------------------------------------------- evaluate

def test_evaluate_vacuous_before_base(tmp_path):
    repo, _base = _make_repo(tmp_path / "repo", {CALC: "value = 1\n"})
    assert dc.evaluate(str(repo), "", []) == (
        "vacuous", {"reason": "no change surface"},
    )


def test_evaluate_abstain_no_base(tmp_path):
    repo, _base = _make_repo(tmp_path / "repo", {CALC: "value = 1\n"})
    status, report = dc.evaluate(str(repo), "", [CALC])
    assert status == "abstain"
    assert report == {"reason": "no base revision to measure against"}


def test_evaluate_end_to_end_introduction(tmp_path):
    repo, base = _make_repo(tmp_path / "repo", {CALC: "value = 1\n"})
    (repo / CALC).write_text("value = 1\nverify = False\n", encoding="utf-8")
    status, report = dc.evaluate(
        str(repo), base, [CALC], run_dir=str(tmp_path / "rd"), target_env="",
    )
    assert status == "fail"
    assert report["classification"] == dc.INTRODUCTION
    assert report["new_findings"] == ["%s:%s" % (T_OFF, CALC)]


def test_evaluate_end_to_end_omission(tmp_path):
    repo, base = _make_repo(tmp_path / "repo", {CALC: "verify = False\nvalue = 1\n"})
    status, report = dc.evaluate(
        str(repo), base, [CALC],
        run_dir=str(tmp_path / "rd"), target_env="%s:%s" % (T_OFF, CALC),
    )
    assert status == "fail"
    assert report["classification"] == dc.OMISSION
    assert "still present at head" in report["detail"]


def test_evaluate_end_to_end_clean(tmp_path):
    repo, base = _make_repo(tmp_path / "repo", {CALC: "verify = False\nvalue = 1\n"})
    (repo / CALC).write_text("value = 1\n", encoding="utf-8")
    status, report = dc.evaluate(
        str(repo), base, [CALC],
        run_dir=str(tmp_path / "rd"), target_env="%s:%s" % (T_OFF, CALC),
    )
    assert status == "pass"
    assert report["classification"] == dc.CLEAN
    assert report["detail"] == "target resolved and the touched surface is scanner-clean"
    assert report["target"] == (T_OFF, CALC)
    assert report["new_findings"] == []
    assert report["remaining_findings"] == []
    assert report["outside_scope_new"] == []
    assert report["symbol_delta"] == []
    for key in ("classification", "detail", "target", "new_findings",
                "remaining_findings", "outside_scope_new", "symbol_delta"):
        assert key in report


def test_evaluate_abstain_on_bad_base_ref(tmp_path):
    repo, _base = _make_repo(tmp_path / "repo", {CALC: "value = 1\n"})
    status, report = dc.evaluate(
        str(repo), "no-such-ref-deception", [CALC],
        run_dir=str(tmp_path / "rd"), target_env="",
    )
    assert status == "abstain"
    assert isinstance(report.get("reason"), str) and report["reason"]
    after = _git(repo, "worktree", "list", "--porcelain").stdout
    assert "no-such-ref-deception" not in after
    assert after.count("worktree ") == 1


def test_outside_scope_findings_informational(tmp_path):
    repo, base = _make_repo(
        tmp_path / "repo1",
        {CALC: "verify = False\n", "app/other.py": "value = 1\n"},
    )
    (repo / CALC).write_text("value = 2\n", encoding="utf-8")
    (repo / "app" / "other.py").write_text("value = 1\nverify = False\n", encoding="utf-8")
    status, report = dc.evaluate(
        str(repo), base, [CALC],
        run_dir=str(tmp_path / "rd1"), target_env="%s:%s" % (T_OFF, CALC),
    )
    assert status == "pass"
    assert report["classification"] == dc.CLEAN
    assert report["outside_scope_new"] == ["%s:app/other.py" % T_OFF]

    repo2, base2 = _make_repo(
        tmp_path / "repo2",
        {CALC: "verify = False\n", "app/other.py": "value = 1\nverify = False\n"},
    )
    (repo2 / CALC).write_text("value = 2\n", encoding="utf-8")
    status2, report2 = dc.evaluate(
        str(repo2), base2, [CALC],
        run_dir=str(tmp_path / "rd2"), target_env="%s:%s" % (T_OFF, CALC),
    )
    assert status2 == "pass"
    assert report2["classification"] == dc.CLEAN
    assert report2["outside_scope_new"] == []


def test_symbol_delta_reported_never_gating(tmp_path):
    repo, base = _make_repo(
        tmp_path / "repo",
        {CALC: "verify = False\n\n\ndef helper(x):\n    return x\n"},
    )
    (repo / CALC).write_text("def calc(a, b):\n    return a + b\n", encoding="utf-8")
    status, report = dc.evaluate(
        str(repo), base, [CALC],
        run_dir=str(tmp_path / "rd"), target_env="%s:%s" % (T_OFF, CALC),
    )
    assert status == "pass"
    assert report["classification"] == dc.CLEAN
    assert "app/calc.py:helper(1)" in report["symbol_delta"]


def test_evaluate_worktree_cleaned_up(tmp_path):
    repo, base = _make_repo(tmp_path / "repo", {CALC: "verify = False\nvalue = 1\n"})
    (repo / CALC).write_text("value = 1\n", encoding="utf-8")
    before = _git(repo, "worktree", "list", "--porcelain").stdout
    dc.evaluate(
        str(repo), base, [CALC],
        run_dir=str(tmp_path / "rd"), target_env="%s:%s" % (T_OFF, CALC),
    )
    after = _git(repo, "worktree", "list", "--porcelain").stdout
    assert after == before


# ----------------------------------------------------------------- cli

def test_main_cli_pass_and_fail_exit_codes(tmp_path, capsys):
    repo, base = _make_repo(tmp_path / "r1", {CALC: "verify = False\n"})
    (repo / CALC).write_text("value = 1\n", encoding="utf-8")
    rc = dc.main([
        "--root", str(repo), "--base", base, "--touched", CALC,
        "--target", "%s:%s" % (T_OFF, CALC), "--json",
    ])
    out = capsys.readouterr().out
    assert rc == 0
    report = json.loads(out.strip().splitlines()[-1])
    assert report["classification"] == dc.CLEAN

    repo2, base2 = _make_repo(tmp_path / "r2", {CALC: "value = 1\n"})
    (repo2 / CALC).write_text("value = 1\nverify = False\n", encoding="utf-8")
    rc2 = dc.main([
        "--root", str(repo2), "--base", base2, "--touched", CALC, "--json",
    ])
    assert rc2 == 1


def test_main_usage_error_exit_2():
    with pytest.raises(SystemExit) as excinfo:
        dc.main([])
    assert excinfo.value.code == 2


def test_module_future_import_and_docstring():
    source = Path(dc.__file__).read_text(encoding="utf-8")
    lines = source.splitlines()
    first_def = next(
        i for i, line in enumerate(lines)
        if line.startswith("def ") or line.startswith("class ")
    )
    assert any(
        "from __future__ import annotations" in line for line in lines[:first_def]
    ), "the future import must precede any annotated definition"
    doc = (dc.__doc__ or "").lower()
    assert doc
    assert "unmeasured" in doc
    assert "base" in doc


# ----------------------------------------------------------------- node

def test_node_clean_repair_passes(tmp_path):
    proc = _run_node(
        tmp_path / "clean",
        base_calc="verify = False\nvalue = 1\n",
        head_calc="value = 1\n",
        target="%s:%s" % (T_OFF, CALC),
    )
    assert proc.returncode == 0, proc.stderr[-2000:]
    payload = _payload(proc)
    assert payload["verifier"] == "deception"
    assert payload["verdict"] == "pass"
    assert payload["pass"] is True
    assert payload["classification"] == "clean"
    assert payload["evidence_path"] is None
    assert payload["findings"] == []
    assert "reasons" in payload
    assert "error_summary" in payload
    assert "failure_kind" not in payload
    checks = payload["checks"]
    assert len(checks) == 1
    assert checks[0]["name"] == "scanner-cleanliness"
    assert checks[0]["gating"] is False
    assert checks[0]["detail"] == "target resolved and the touched surface is scanner-clean"


def test_node_introduced_finding_fails(tmp_path):
    proc = _run_node(
        tmp_path / "intro",
        base_calc="value = 1\n",
        head_calc="value = 1\nverify = False\n",
        target=None,
    )
    assert proc.returncode == 1
    payload = _payload(proc)
    assert payload["verdict"] == "fail"
    assert payload["pass"] is False
    assert payload["classification"] == "introduction"


def test_node_is_vacuous_without_target(tmp_path):
    """An unnamed target is not applicable, not unmeasurable.

    A node abstention (``status: unverified``) fails the whole run and withholds
    the publish, and nothing produces a target yet — so abstaining here would
    break every ordinary code-fix run instead of leaving the layer inert.
    """
    proc = _run_node(
        tmp_path / "vacuous-no-target",
        base_calc="verify = False\nvalue = 1\n",
        head_calc="value = 1\n",
        target=None,
    )
    assert proc.returncode == 0, proc.stderr[-2000:]
    payload = _payload(proc)
    assert payload["verdict"] == "vacuous"
    assert payload["pass"] is True
    assert "status" not in payload
    assert "failure_kind" not in payload
    assert payload["classification"] == "unmeasured"
    assert payload["error_summary"]


def test_node_vacuous_envelope_has_no_status_key(tmp_path):
    proc = _run_node(
        tmp_path / "vacuous",
        base_calc="value = 1\n",
        head_calc=None,
        target=None,
        touched=None,
    )
    assert proc.returncode == 0, proc.stderr[-2000:]
    payload = _payload(proc)
    assert payload["verdict"] == "vacuous"
    assert payload["pass"] is True
    assert "status" not in payload
    assert payload["error_summary"]
    assert "failure_kind" not in payload


def test_node_pass_true_only_for_pass_and_vacuous(tmp_path):
    scenarios = {
        "pass": dict(
            base_calc="verify = False\nvalue = 1\n",
            head_calc="value = 1\n",
            target="%s:%s" % (T_OFF, CALC),
        ),
        "introduction": dict(
            base_calc="value = 1\n",
            head_calc="value = 1\nverify = False\n",
            target=None,
        ),
        "omission": dict(
            base_calc="verify = False\nvalue = 1\n",
            head_calc="verify = False\nvalue = 1\n",
            target="%s:%s" % (T_OFF, CALC),
        ),
        "vacuous-no-target": dict(
            base_calc="verify = False\nvalue = 1\n",
            head_calc="value = 1\n",
            target=None,
        ),
        "vacuous-no-surface": dict(
            base_calc="value = 1\n",
            head_calc=None,
            target=None,
            touched=None,
        ),
    }
    seen = {}
    for name, kwargs in scenarios.items():
        proc = _run_node(tmp_path / name, **kwargs)
        payload = _payload(proc)
        seen[payload["verdict"]] = payload["pass"]

    assert set(seen) == {"pass", "fail", "vacuous"}
    for verdict, passed in seen.items():
        if verdict in ("pass", "vacuous"):
            assert passed is True, (verdict, passed)
        else:
            assert passed is False, (verdict, passed)


def test_node_import_seam_without_pythonpath(tmp_path):
    proc = _run_node(
        tmp_path / "seam",
        base_calc="verify = False\nvalue = 1\n",
        head_calc="value = 1\n",
        target="%s:%s" % (T_OFF, CALC),
        cwd=tmp_path / "seam",
        strip_pythonpath=True,
    )
    assert proc.returncode == 0, proc.stderr[-2000:]
    payload = _payload(proc)
    assert payload["verdict"] == "pass"
    assert payload["classification"] == "clean"


def test_node_module_future_import():
    assert VERIFIER.exists(), "missing verifier script: %s" % VERIFIER
    lines = VERIFIER.read_text(encoding="utf-8").splitlines()
    first_def = next(
        i for i, line in enumerate(lines)
        if line.startswith("def ") or line.startswith("class ")
    )
    assert any(
        "from __future__ import annotations" in line for line in lines[:first_def]
    ), "the future import must precede any annotated definition"
