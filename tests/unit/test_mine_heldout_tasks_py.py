"""Unit tests for scripts/mine_heldout_tasks.py against a real temp git repo.

The repo carries three commits on top of a base:
  - ``fix: add`` fixes a bug AND adds a test that fails on the base  → kept
  - ``fix: tidy`` changes code and a test that already passed         → rejected
  - ``feat: mul`` is not a fix commit                                 → never a candidate
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "scripts"))
import mine_heldout_tasks as m  # noqa: E402


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True,
                          text=True, check=True).stdout.strip()


def _commit(repo: Path, msg: str, files: dict[str, str], date: str = "") -> str:
    for rel, body in files.items():
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        (repo / rel).write_text(body)
    _git(repo, "add", "-A")
    env = ({**os.environ, "GIT_AUTHOR_DATE": date, "GIT_COMMITTER_DATE": date}
           if date else None)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", msg], check=True,
                   capture_output=True, text=True, env=env)
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
    }, date="2026-01-01T00:00:00+00:00")
    shas["fix"] = _commit(repo, "fix: add subtracted instead of adding\n\nadd(2, 3) returned -1.\n\n"
                          "Co-Authored-By: someone <x@y>", {
        "calc.py": "def add(a, b):\n    return a + b\n\ndef neg(a):\n    return -a\n",
        "tests/test_calc.py": ("from calc import add, neg\n\ndef test_neg():\n    assert neg(2) == -2\n\n"
                               "def test_add():\n    assert add(2, 3) == 5\n"),
    }, date="2026-01-02T00:00:00+00:00")
    shas["tidy"] = _commit(repo, "fix(calc): tidy neg", {
        "calc.py": "def add(a, b):\n    return a + b\n\ndef neg(a):\n    return 0 - a\n",
        "tests/test_calc.py": ("from calc import add, neg\n\ndef test_neg():\n    assert neg(3) == -3\n\n"
                               "def test_add():\n    assert add(2, 3) == 5\n"),
    }, date="2026-01-03T00:00:00+00:00")
    shas["feat"] = _commit(repo, "feat: mul", {
        "calc.py": ("def add(a, b):\n    return a + b\n\ndef neg(a):\n    return 0 - a\n\n"
                    "def mul(a, b):\n    return a * b\n"),
        "tests/test_mul.py": "from calc import mul\n\ndef test_mul():\n    assert mul(2, 3) == 6\n",
    }, date="2026-01-04T00:00:00+00:00")
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


# ── jest/vitest test-file detection ──────────────────────────────────────────


def test_test_family_classification():
    # pytest keeps its tests/test_*.py convention.
    assert m.test_family("tests/test_calc.py") == "pytest"
    assert m.test_family("pkg/tests/test_x.py") == "pytest"
    # jest/vitest basenames and __tests__ dirs — the default testMatch shapes.
    assert m.test_family("src/calc.test.js") == "jest"
    assert m.test_family("src/calc.spec.ts") == "jest"
    assert m.test_family("src/calc.test.tsx") == "jest"
    assert m.test_family("src/calc.spec.mjs") == "jest"
    assert m.test_family("src/__tests__/calc.js") == "jest"
    assert m.test_family("a/b/__tests__/deep/nested.tsx") == "jest"
    # Plain source is not a test.
    assert m.test_family("src/calc.js") is None
    assert m.test_family("src/jest.config.js") is None
    assert m.test_family("README.md") is None


def _mk_js_history(tmp_path: Path) -> dict[str, str]:
    """base (buggy src) → fix (gold src + a hidden js test) in one repo."""
    repo = tmp_path / "jsrepo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    shas = {}
    shas["base"] = _commit(repo, "chore: base", {
        "src/calc.js": "export function add(a, b) {\n  return a - b;\n}\n",
        "src/calc.test.js": "// placeholder\n",
    })
    shas["fix"] = _commit(repo, "fix: add subtracted instead of adding", {
        "src/calc.js": "export function add(a, b) {\n  return a + b;\n}\n",
        "src/calc.test.js": "import { add } from './calc.js';\ntest('add works', () => {});\n",
    })
    return {"repo": str(repo), **shas}


def _write_fake_jest(tmp_path: Path) -> str:
    """A stand-in runner that writes a jest-JSON results file, its outcome
    keyed on whether the checked-out src carries the fix. No node needed."""
    script = tmp_path / "fake_jest.py"
    script.write_text(
        "import json, os, sys\n"
        "out = next(a.split('=', 1)[1] for a in sys.argv if a.startswith('--outputFile='))\n"
        "cwd = os.getcwd()\n"
        "suite = os.path.join(cwd, 'src', 'calc.test.js')\n"
        "try:\n"
        "    src = open(os.path.join(cwd, 'src', 'calc.js')).read()\n"
        "except OSError:\n"
        "    src = ''\n"
        "fixed = 'a + b' in src\n"
        "res = {'testResults': [{'name': suite, 'status': 'passed', 'assertionResults': [\n"
        "    {'fullName': 'add works', 'status': 'passed' if fixed else 'failed'},\n"
        "    {'fullName': 'neg works', 'status': 'passed'}]}]}\n"
        "open(out, 'w').write(json.dumps(res))\n"
    )
    return str(script)


def _fake_runner(tmp_path: Path):
    script = _write_fake_jest(tmp_path)
    return lambda workdir: ("jest", [sys.executable, script])


def test_jest_test_files_are_candidates(tmp_path):
    h = _mk_js_history(tmp_path)

    cands = m.candidate_commits(Path(h["repo"]), "HEAD")

    assert [c["fix_sha"] for c in cands] == [h["fix"]]
    assert cands[0]["test_files"] == ["src/calc.test.js"]
    assert cands[0]["src_files"] == ["src/calc.js"]


def test_js_results_file_grades_to_pass_fail_ids(tmp_path, monkeypatch):
    """run_tests dispatches a JS file to the repo runner and parses its
    jest-JSON report into {id: outcome} using the certify id normalization."""
    monkeypatch.setattr(m, "_locate_js_runner", _fake_runner(tmp_path))
    work = tmp_path / "work"
    (work / "src").mkdir(parents=True)
    (work / "src" / "calc.js").write_text("export function add(a, b) {\n  return a + b;\n}\n")
    (work / "src" / "calc.test.js").write_text("// js\n")

    out = m.run_tests(work, ["src/calc.test.js"], sys.executable, 60)

    assert out == {"src/calc.test.js::add works": "passed",
                   "src/calc.test.js::neg works": "passed"}


def test_js_ids_are_stable_across_two_roots(tmp_path, monkeypatch):
    """base and fix are different roots; only a cwd-relative id matches them."""
    monkeypatch.setattr(m, "_locate_js_runner", _fake_runner(tmp_path))
    ids = []
    for name in ("base", "fix"):
        work = tmp_path / name
        (work / "src").mkdir(parents=True)
        (work / "src" / "calc.js").write_text("export function add(a, b) {\n  return a + b;\n}\n")
        (work / "src" / "calc.test.js").write_text("// js\n")
        ids.append(set(m.run_tests(work, ["src/calc.test.js"], sys.executable, 60)))
    assert ids[0] == ids[1] == {"src/calc.test.js::add works", "src/calc.test.js::neg works"}


def test_validate_mines_a_jest_fix(tmp_path, monkeypatch):
    """End to end: a js fix whose test fails on base and passes on fix yields
    the correct fail_to_pass / pass_to_pass id sets."""
    h = _mk_js_history(tmp_path)
    cand = m.candidate_commits(Path(h["repo"]), "HEAD")[0]
    scratch = tmp_path / "scratch"
    _git(Path(h["repo"]), "worktree", "add", "-q", "--detach", str(scratch), "HEAD")
    monkeypatch.setattr(m, "_locate_js_runner", _fake_runner(tmp_path))

    task, reason = m.validate(scratch, cand, sys.executable, 120)

    assert reason == ""
    assert task["fail_to_pass"] == ["src/calc.test.js::add works"]
    assert task["pass_to_pass"] == ["src/calc.test.js::neg works"]


def test_mixed_pytest_and_jest_files_are_unioned(tmp_path, monkeypatch):
    monkeypatch.setattr(m, "_locate_js_runner", _fake_runner(tmp_path))
    work = tmp_path / "work"
    (work / "src").mkdir(parents=True)
    (work / "tests").mkdir()
    (work / "src" / "calc.js").write_text("export function add(a, b) {\n  return a + b;\n}\n")
    (work / "src" / "calc.test.js").write_text("// js\n")
    (work / "tests" / "test_py.py").write_text("def test_ok():\n    assert True\n")

    out = m.run_tests(work, ["src/calc.test.js", "tests/test_py.py"], sys.executable, 120)

    assert "src/calc.test.js::add works" in out
    assert "tests.test_py::test_ok" in out


def test_missing_js_runner_yields_no_report(tmp_path, monkeypatch):
    """A JS file with no runner must produce None (→ an auditable reject), not
    a silent empty pass."""
    monkeypatch.setattr(m, "_locate_js_runner", lambda workdir: None)
    work = tmp_path / "work"
    (work / "src").mkdir(parents=True)
    (work / "src" / "calc.test.js").write_text("// js\n")

    assert m.run_tests(work, ["src/calc.test.js"], sys.executable, 30) is None


def test_locate_js_runner_prefers_the_vendored_binary(tmp_path):
    work = tmp_path / "w"
    bin_dir = work / "node_modules" / ".bin"
    bin_dir.mkdir(parents=True)
    jest_bin = bin_dir / "jest"
    jest_bin.write_text("#!/bin/sh\n")
    assert m._locate_js_runner(work) == ("jest", [str(jest_bin)])


def test_locate_js_runner_falls_back_to_a_vendored_vitest(tmp_path):
    work = tmp_path / "w"
    bin_dir = work / "node_modules" / ".bin"
    bin_dir.mkdir(parents=True)
    (bin_dir / "vitest").write_text("#!/bin/sh\n")
    assert m._locate_js_runner(work)[0] == "vitest"


def test_js_run_cmd_shapes():
    assert m._js_run_cmd("jest", ["npx", "jest"], "/tmp/r.json", ["a.test.js"]) == [
        "npx", "jest", "--json", "--outputFile=/tmp/r.json", "a.test.js"]
    assert m._js_run_cmd("vitest", ["npx", "vitest"], "/tmp/r.json", ["a.test.ts"]) == [
        "npx", "vitest", "run", "--reporter=json", "--outputFile=/tmp/r.json", "a.test.ts"]


# ── high-water mark / incremental mining ─────────────────────────────────────


def test_resolve_since_explicit_flag_wins_over_the_mark():
    mark = {"fix_sha": "abc", "committed_at": "2026-01-03T00:00:00+00:00"}
    assert m.resolve_since("2025-01-01", mark) == "2025-01-01"
    assert m.resolve_since("", mark) == "2026-01-03T00:00:00+00:00"
    assert m.resolve_since("", {}) == ""


def test_highwater_round_trips_and_never_rewinds(tmp_path):
    out = tmp_path / "m.json"
    assert m.read_highwater(out) == {}
    older = {"fix_sha": "a", "committed_at": "2026-01-01T00:00:00+00:00"}
    newer = {"fix_sha": "b", "committed_at": "2026-01-05T00:00:00+00:00"}

    iso = m.advance_highwater(out, newer, "")
    assert iso == newer["committed_at"]
    assert m.read_highwater(out)["fix_sha"] == "b"

    # An older commit (e.g. from a manually-older --since) must not rewind it.
    assert m.advance_highwater(out, older, iso) == newer["committed_at"]
    assert m.read_highwater(out)["fix_sha"] == "b"


def test_high_water_mark_mines_only_new_commits_on_rerun(tmp_path):
    h = _mk_history(tmp_path)
    out = tmp_path / "out" / "manifest.json"
    argv = ["--repo", h["repo"], "--rev", "HEAD", "--out", str(out),
            "--python", sys.executable, "--timeout", "120"]

    assert m.main(argv) == 0
    mark = m.read_highwater(out)
    # The newest commit *considered* is tidy (rejected) — not merely kept.
    assert mark["fix_sha"] == h["tidy"]
    assert mark["committed_at"].startswith("2026-01-03")

    # A new fix lands on top; a re-run without --since must pick only it up.
    repo = Path(h["repo"])
    new_fix = _commit(repo, "fix: sub never existed", {
        "calc.py": ("def add(a, b):\n    return a + b\n\ndef neg(a):\n    return 0 - a\n\n"
                    "def sub(a, b):\n    return a - b\n"),
        "tests/test_calc.py": ("from calc import add, neg, sub\n\ndef test_neg():\n    assert neg(3) == -3\n\n"
                               "def test_add():\n    assert add(2, 3) == 5\n\n"
                               "def test_sub():\n    assert sub(5, 3) == 2\n"),
    }, date="2026-01-05T00:00:00+00:00")

    assert m.main(argv) == 0
    tasks = json.loads(out.read_text())
    assert [t["fix_sha"] for t in tasks] == [h["fix"], new_fix]
    assert m.read_highwater(out)["fix_sha"] == new_fix


def test_high_water_mark_advances_only_over_considered_commits(tmp_path):
    """With --limit 1 only the first candidate is considered, so the mark must
    stop there — advancing to the end would silently skip unmined history."""
    h = _mk_history(tmp_path)
    out = tmp_path / "out" / "manifest.json"
    argv = ["--repo", h["repo"], "--rev", "HEAD", "--out", str(out),
            "--python", sys.executable, "--timeout", "120", "--limit", "1"]

    assert m.main(argv) == 0
    assert m.read_highwater(out)["fix_sha"] == h["fix"]
    assert [t["fix_sha"] for t in json.loads(out.read_text())] == [h["fix"]]

    # Resume: the next candidate (tidy) is now the only one in the window.
    assert m.main(argv) == 0
    assert [t["fix_sha"] for t in json.loads(out.read_text())] == [h["fix"]]
    rejects = [json.loads(ln) for ln in out.with_name("rejects.jsonl").read_text().splitlines()]
    assert [r["fix_sha"] for r in rejects] == [h["tidy"]]
    assert m.read_highwater(out)["fix_sha"] == h["tidy"]
