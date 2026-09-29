"""Unit tests for scripts/verifier_replay.py against a real temp git repo.

History: base has a bug in calc.add; the fix commit fixes it and adds
tests/test_calc.py::test_add. Two stored solves: one applies the real fix
(correct), one changes an unrelated line (wrong).
"""
from __future__ import annotations

import json
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
