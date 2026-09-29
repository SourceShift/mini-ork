"""Unit tests for scripts/mine_heldout_tasks.py against a real temp git repo.

The repo carries three commits on top of a base:
  - ``fix: add`` fixes a bug AND adds a test that fails on the base  → kept
  - ``fix: tidy`` changes code and a test that already passed         → rejected
  - ``feat: mul`` is not a fix commit                                 → never a candidate
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "scripts"))
import mine_heldout_tasks as m  # noqa: E402


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


def _mk_history(tmp_path: Path) -> dict[str, str]:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    shas = {}
    shas["base"] = _commit(repo, "chore: base", {
        "calc.py": "def add(a, b):\n    return a - b\n\ndef neg(a):\n    return -a\n",
        "tests/test_calc.py": "from calc import neg\n\ndef test_neg():\n    assert neg(2) == -2\n",
    })
    shas["fix"] = _commit(repo, "fix: add subtracted instead of adding\n\nadd(2, 3) returned -1.\n\n"
                          "Co-Authored-By: someone <x@y>", {
        "calc.py": "def add(a, b):\n    return a + b\n\ndef neg(a):\n    return -a\n",
        "tests/test_calc.py": ("from calc import add, neg\n\ndef test_neg():\n    assert neg(2) == -2\n\n"
                               "def test_add():\n    assert add(2, 3) == 5\n"),
    })
    shas["tidy"] = _commit(repo, "fix(calc): tidy neg", {
        "calc.py": "def add(a, b):\n    return a + b\n\ndef neg(a):\n    return 0 - a\n",
        "tests/test_calc.py": ("from calc import add, neg\n\ndef test_neg():\n    assert neg(3) == -3\n\n"
                               "def test_add():\n    assert add(2, 3) == 5\n"),
    })
    shas["feat"] = _commit(repo, "feat: mul", {
        "calc.py": ("def add(a, b):\n    return a + b\n\ndef neg(a):\n    return 0 - a\n\n"
                    "def mul(a, b):\n    return a * b\n"),
        "tests/test_mul.py": "from calc import mul\n\ndef test_mul():\n    assert mul(2, 3) == 6\n",
    })
    return {"repo": str(repo), **shas}


def test_only_fix_commits_touching_code_and_tests_are_candidates(tmp_path):
    h = _mk_history(tmp_path)

    cands = m.candidate_commits(Path(h["repo"]), "HEAD")

    assert [c["fix_sha"] for c in cands] == [h["fix"], h["tidy"]]
    first = cands[0]
    assert first["base_sha"] == h["base"]
    assert first["src_files"] == ["calc.py"] and first["test_files"] == ["tests/test_calc.py"]
    assert "Co-Authored-By" not in first["problem_statement"]
    assert first["problem_statement"].startswith("fix: add subtracted")


def test_a_real_fix_yields_its_fail_to_pass_test(tmp_path):
    h = _mk_history(tmp_path)
    cand = m.candidate_commits(Path(h["repo"]), "HEAD")[0]
    scratch = tmp_path / "scratch"
    _git(Path(h["repo"]), "worktree", "add", "-q", "--detach", str(scratch), "HEAD")

    task, reason = m.validate(scratch, cand, sys.executable, 120)

    assert reason == ""
    assert [t.split("::")[1] for t in task["fail_to_pass"]] == ["test_add"]
    assert [t.split("::")[1] for t in task["pass_to_pass"]] == ["test_neg"]


def test_tests_that_already_pass_on_the_base_are_rejected(tmp_path):
    """The tidy commit's tests are green before AND after: no signal."""
    h = _mk_history(tmp_path)
    cand = m.candidate_commits(Path(h["repo"]), "HEAD")[1]
    scratch = tmp_path / "scratch"
    _git(Path(h["repo"]), "worktree", "add", "-q", "--detach", str(scratch), "HEAD")

    task, reason = m.validate(scratch, cand, sys.executable, 120)

    assert task is None and "no fail_to_pass" in reason


def test_split_is_stable_and_roughly_proportional():
    shas = [f"{i:040x}" for i in range(2000)]
    first = [m.assign_split(s, 0.3) for s in shas]
    assert first == [m.assign_split(s, 0.3) for s in shas]  # same sha, same split
    assert 0.25 < first.count("test") / len(shas) < 0.35


def test_end_to_end_writes_a_locked_manifest_and_resumes(tmp_path):
    h = _mk_history(tmp_path)
    out = tmp_path / "out" / "manifest.json"
    argv = ["--repo", h["repo"], "--rev", "HEAD", "--out", str(out),
            "--python", sys.executable, "--timeout", "120"]

    assert m.main(argv) == 0

    tasks = json.loads(out.read_text())
    assert [t["fix_sha"] for t in tasks] == [h["fix"]]
    assert tasks[0]["id"] == f"mo-{h['fix'][:10]}"
    lock = out.with_name("manifest.json.sha256").read_text().split()[0]
    import hashlib
    assert lock == hashlib.sha256(out.read_bytes()).hexdigest()
    rejects = [json.loads(ln) for ln in out.with_name("rejects.jsonl").read_text().splitlines()]
    assert [r["fix_sha"] for r in rejects] == [h["tidy"]]

    # Re-running validates nothing new: kept and rejected shas are both skipped.
    assert m.main(argv) == 0
    assert len(out.with_name("rejects.jsonl").read_text().splitlines()) == 1
    assert json.loads(out.read_text()) == tasks


def test_a_cherry_picked_duplicate_is_mined_once(tmp_path):
    """The same fix re-landed under a new sha must not appear twice — two
    copies could land in different splits and leak test answers into dev."""
    h = _mk_history(tmp_path)
    repo = Path(h["repo"])
    _git(repo, "checkout", "-q", "-b", "side", h["base"])
    _git(repo, "cherry-pick", h["fix"])
    _git(repo, "checkout", "-q", "-")
    _git(repo, "merge", "-q", "--no-edit", "-s", "ours", "side")

    shas = [c["fix_sha"] for c in m.candidate_commits(repo, "HEAD")]

    assert len([s for s in shas if s != h["tidy"]]) == 1
