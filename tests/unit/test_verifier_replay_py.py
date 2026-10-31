"""Unit tests for scripts/verifier_replay.py against a real temp git repo.

History: base has a bug in calc.add; the fix commit fixes it and adds
tests/test_calc.py::test_add. Two stored solves: one applies the real fix
(correct), one changes an unrelated line (wrong).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "scripts"))
import verifier_replay as vr  # noqa: E402


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True,
                          text=True, check=True).stdout.strip()


def _commit(repo: Path, msg: str, files: dict[str, str]) -> str:
    for rel, body in files.items():
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        (repo / rel).write_text(body)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", msg)
    return _git(repo, "rev-parse", "HEAD")


def _setup(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    base = _commit(repo, "base", {
        "calc.py": "def add(a, b):\n    return a - b\n\n\ndef neg(a):\n    return -a\n",
        "tests/test_calc.py": "from calc import neg\n\n\ndef test_neg():\n    assert neg(2) == -2\n",
    })
    fix = _commit(repo, "fix: add", {
        "calc.py": "def add(a, b):\n    return a + b\n\n\ndef neg(a):\n    return -a\n",
        "tests/test_calc.py": ("from calc import add, neg\n\n\ndef test_neg():\n    assert neg(2) == -2\n"
                               "\n\ndef test_add_hidden_marker():\n    assert add(2, 3) == 5\n"),
    })
    task = {"id": "mo-aaaaaaaaaa", "base_sha": base, "fix_sha": fix,
            "src_files": ["calc.py"], "test_files": ["tests/test_calc.py"],
            "fail_to_pass": ["tests.test_calc::test_add_hidden_marker"],
            "pass_to_pass": ["tests.test_calc::test_neg"], "difficulty": "easy", "weak_signal": False}
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps([task]))
    runs = tmp_path / "runs"
    correct = runs / "heldout-mo-aaaaaaaaaa-1"
    wrong = runs / "heldout-mo-aaaaaaaaaa-2"
    correct.mkdir(parents=True)
    wrong.mkdir(parents=True)
    (correct / "review-diff.patch").write_text(_git(repo, "diff", base, fix, "--", "calc.py") + "\n")
    _git(repo, "checkout", "-q", base)
    (repo / "calc.py").write_text("def add(a, b):\n    return a - b  # looked at it\n\n\ndef neg(a):\n    return -a\n")
    (wrong / "review-diff.patch").write_text(_git(repo, "diff") + "\n")
    _git(repo, "checkout", "-q", "--", "calc.py")
    _git(repo, "checkout", "-q", fix)
    return repo, manifest, runs


def _verifier(tmp_path: Path, body: str) -> Path:
    v = tmp_path / "verifier.py"
    v.write_text(body)
    return v


def _main(repo, manifest, runs, verifier, out, capsys):
    vr.main(["--repo", str(repo), "--manifest", str(manifest), "--runs-glob", str(runs / "heldout-mo-*"),
             "--verifier", str(verifier), "--python", sys.executable, "--timeout", "120", "--out", str(out)])
    return json.loads(capsys.readouterr().out.strip().splitlines()[-1])


def test_an_accept_everything_verifier_scores_one_false_accept(tmp_path, capsys):
    repo, manifest, runs = _setup(tmp_path)
    v = _verifier(tmp_path, 'import json; print(json.dumps({"pass": True}))\n')

    c = _main(repo, manifest, runs, v, tmp_path / "out.jsonl", capsys)

    assert (c["true_accept"], c["false_accept"], c["false_reject"], c["true_reject"]) == (1, 1, 0, 0)


def test_a_reject_everything_verifier_scores_one_false_reject(tmp_path, capsys):
    repo, manifest, runs = _setup(tmp_path)
    v = _verifier(tmp_path, 'import json; print(json.dumps({"pass": False, "error_summary": "no"}))\n')

    c = _main(repo, manifest, runs, v, tmp_path / "out.jsonl", capsys)

    assert (c["true_accept"], c["false_accept"], c["false_reject"], c["true_reject"]) == (0, 0, 1, 1)


def test_the_verifier_never_sees_the_hidden_tests(tmp_path, capsys):
    """Hidden tests are restored only AFTER the verifier ran; a verifier that
    could read them would grade with the answer key."""
    repo, manifest, runs = _setup(tmp_path)
    v = _verifier(tmp_path, (
        "import json, pathlib\n"
        "seen = 'test_add_hidden_marker' in pathlib.Path('tests/test_calc.py').read_text()\n"
        "print(json.dumps({'pass': not seen}))\n"))

    c = _main(repo, manifest, runs, v, tmp_path / "out.jsonl", capsys)

    assert c["true_accept"] + c["false_accept"] == 2  # never saw the marker


def test_a_crashing_verifier_is_no_verdict_not_a_reject(tmp_path, capsys):
    repo, manifest, runs = _setup(tmp_path)
    v = _verifier(tmp_path, "raise SystemExit(3)\n")

    c = _main(repo, manifest, runs, v, tmp_path / "out.jsonl", capsys)

    assert c["no_verdict"] == 2 and c["scored"] == 0
    rows = [json.loads(ln) for ln in (tmp_path / "out.jsonl").read_text().splitlines()]
    assert all(r["hidden_pass"] is not None for r in rows)  # ground truth still graded


def test_runner_kept_patches_are_replayed(tmp_path, capsys):
    repo, manifest, runs = _setup(tmp_path)
    patches = tmp_path / "results.patches"
    patches.mkdir()
    (patches / "mo-aaaaaaaaaa.patch").write_text(
        (runs / "heldout-mo-aaaaaaaaaa-1" / "review-diff.patch").read_text())
    v = _verifier(tmp_path, 'import json; print(json.dumps({"pass": False}))\n')

    vr.main(["--repo", str(repo), "--manifest", str(manifest), "--patches-dir", str(patches),
             "--verifier", str(v), "--python", sys.executable, "--timeout", "120",
             "--out", str(tmp_path / "out.jsonl")])
    c = json.loads(capsys.readouterr().out.strip().splitlines()[-1])

    assert (c["scored"], c["false_reject"]) == (1, 1)  # the correct fix, rejected


# ── RRL-3b: extra_env, test-path scoping, patch_files ──────────────────────


def test_run_verifier_extra_env_and_fixed_keys_win(tmp_path, monkeypatch):
    monkeypatch.delenv("X_RRL", raising=False)
    script = tmp_path / "v_env.py"
    script.write_text(
        "import json, os\n"
        "ok = (os.environ.get('X_RRL') == '1'\n"
        "      and os.environ.get('MINI_ORK_RUN_ID') == 'verifier-replay')\n"
        "print(json.dumps({'pass': ok, 'reason': 'env'}))\n")
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    passed, reason = vr.run_verifier(
        script, scratch, sys.executable, 60,
        extra_env={"X_RRL": "1", "MINI_ORK_RUN_ID": "x"})
    assert (passed, reason) == (True, "env")
    assert "X_RRL" not in os.environ


def test_run_verifier_scrubs_inherited_test_cmd(tmp_path, monkeypatch):
    # The operator's shell may export MINI_ORK_TEST_CMD (this repo's own
    # tooling does to scope green gates).  An unscoped replay must not inherit
    # it: the command points at the operator's checkout, not the scratch tree.
    monkeypatch.setenv("MINI_ORK_TEST_CMD", "pytest -q pinned/from/shell")
    script = tmp_path / "v_cmd.py"
    script.write_text(
        "import json, os\n"
        "print(json.dumps({'pass': os.environ.get('MINI_ORK_TEST_CMD') == '',\n"
        "                  'reason': os.environ.get('MINI_ORK_TEST_CMD')}))\n")
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    passed, _ = vr.run_verifier(script, scratch, sys.executable, 60)
    assert passed is True


def test_run_verifier_extra_env_can_opt_into_test_cmd(tmp_path, monkeypatch):
    monkeypatch.setenv("MINI_ORK_TEST_CMD", "pytest -q pinned/from/shell")
    script = tmp_path / "v_cmd.py"
    script.write_text(
        "import json, os\n"
        "print(json.dumps({'pass': os.environ.get('MINI_ORK_TEST_CMD')\n"
        "                  == 'pytest -q tests/test_x.py'}))\n")
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    passed, _ = vr.run_verifier(
        script, scratch, sys.executable, 60,
        extra_env={"MINI_ORK_TEST_CMD": "pytest -q tests/test_x.py"})
    assert passed is True


def test_test_suffix_and_is_test_path():
    assert vr.TEST_SUFFIX == ".py"
    assert vr.is_test_path("tests/unit/test_a.py") is True
    assert vr.is_test_path("pkg/tests/x.py") is True
    assert vr.is_test_path("test_b.py") is True
    assert vr.is_test_path("mini_ork/a.py") is False
    assert vr.is_test_path("tests/data.json") is False


def test_patch_files_reads_numstat_third_column(tmp_path):
    repo, _manifest, _runs = _setup(tmp_path)
    patch = tmp_path / "x.patch"
    patch.write_text(_git(repo, "diff", "HEAD~1", "HEAD", "--",
                          "calc.py", "tests/test_calc.py") + "\n")
    assert set(vr.patch_files(patch, repo)) == {"calc.py", "tests/test_calc.py"}
    assert vr.patch_files(tmp_path / "missing.patch", repo) == []


class _VerifierSpy:
    def __init__(self):
        self.calls = []

    def __call__(self, verifier, scratch, python, timeout, **kwargs):
        self.calls.append(kwargs)
        return True, "stub"


def _scoped_repo(tmp_path: Path):
    """A repo whose base has src.py + tests/test_old.py; ``patch`` edits src.py
    and adds tests/test_new.py."""
    repo = tmp_path / "srepo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    (repo / "src.py").write_text("x = 1\n")
    (repo / "tests").mkdir()
    (repo / "tests" / "test_old.py").write_text("def test_old():\n    assert True\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "base")
    base = _git(repo, "rev-parse", "HEAD")
    (repo / "src.py").write_text("x = 2\n")
    (repo / "tests" / "test_new.py").write_text("def test_new():\n    assert True\n")
    _git(repo, "add", "-A")
    diff = _git(repo, "diff", "--cached")
    _git(repo, "reset", "-q", "--hard", "HEAD")
    _git(repo, "clean", "-fdq")
    patch = tmp_path / "item.patch"
    patch.write_text(diff + "\n")
    return repo, base, patch


def test_replay_one_scoped_selects_task_and_patch_tests(tmp_path, monkeypatch):
    repo, base, patch = _scoped_repo(tmp_path)
    task = {"id": "item", "base_sha": base,
            "test_files": ["tests/test_old.py", "tests/test_missing.py"]}
    spy = _VerifierSpy()
    monkeypatch.setattr(vr, "run_verifier", spy)
    row = vr.replay_one(patch, task, repo=repo, verifier=tmp_path / "v.py",
                        python=sys.executable, timeout=60, hidden=False,
                        import_root=repo, scoped_tests=True)
    assert row["scoped_tests"] == ["tests/test_new.py", "tests/test_old.py"]
    cmd = spy.calls[0]["extra_env"]["MINI_ORK_TEST_CMD"]
    assert "test_new.py" in cmd and "test_old.py" in cmd
    assert "test_missing" not in cmd
    assert spy.calls[0].get("import_root") == repo


def test_replay_one_scoped_no_tests_skips(tmp_path, monkeypatch):
    repo, base, patch = _scoped_repo(tmp_path)
    task = {"id": "item", "base_sha": base, "test_files": []}
    # A patch that touches no test: rebuild it from src.py alone.
    (repo / "src.py").write_text("x = 3\n")
    _git(repo, "add", "src.py")
    patch.write_text(_git(repo, "diff", "--cached") + "\n")
    _git(repo, "reset", "-q", "--hard", "HEAD")
    spy = _VerifierSpy()
    monkeypatch.setattr(vr, "run_verifier", spy)
    row = vr.replay_one(patch, task, repo=repo, verifier=tmp_path / "v.py",
                        python=sys.executable, timeout=60, hidden=False,
                        scoped_tests=True)
    assert row["skipped"] == "no runnable tests for a scoped verifier run"
    assert spy.calls == []


def test_replay_one_unscoped_passes_no_extra_env(tmp_path, monkeypatch):
    repo, base, patch = _scoped_repo(tmp_path)
    task = {"id": "item", "base_sha": base,
            "test_files": ["tests/test_old.py"]}
    spy = _VerifierSpy()
    monkeypatch.setattr(vr, "run_verifier", spy)
    row = vr.replay_one(patch, task, repo=repo, verifier=tmp_path / "v.py",
                        python=sys.executable, timeout=60, hidden=False)
    assert spy.calls[0].get("extra_env") is None
    assert "scoped_tests" not in row
