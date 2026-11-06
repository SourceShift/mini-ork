"""The no-silent-fallback detector: the shapes it catches, the ones it must not.

The failure this prevents is a run that reports green because an error was
swallowed — so the tests are as much about the *narrowness* of the rule as about
its recall. A check that flags best-effort cleanup gets turned off, and a check
that is turned off catches nothing.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from mini_ork.verify import fallback_guard as fg

ROOT = Path(__file__).resolve().parents[2]


def _scan(src: str):
    return fg.scan_text(src, "m.py")


def test_bare_except_pass_is_a_finding():
    findings = _scan("try:\n    x()\nexcept:\n    pass\n")
    assert len(findings) == 1
    # The finding points at the handler, which is the line a reader edits.
    assert findings[0].line == 3


def test_exception_handler_pass_is_a_finding():
    findings = _scan("try:\n    x()\nexcept Exception:\n    pass\n")
    assert len(findings) == 1
    assert findings[0].handler == "except Exception"


def test_return_none_on_error_is_a_finding_with_its_own_fix():
    findings = _scan("def f():\n    try:\n        return x()\n"
                     "    except Exception as exc:\n        return None\n")
    assert len(findings) == 1
    assert "returns None" in findings[0].shape
    assert "indistinguishable" in findings[0].fix


def test_bare_return_on_error_is_a_finding():
    findings = _scan("def f():\n    try:\n        x()\n    except Exception:\n        return\n")
    assert len(findings) == 1
    assert "returns None" in findings[0].shape


def test_a_named_cleanup_handler_is_not_a_finding():
    """`except OSError: pass` is a judgement about a specific failure."""
    assert _scan("try:\n    os.unlink(p)\nexcept OSError:\n    pass\n") == []


def test_a_handler_that_records_the_error_is_not_a_finding():
    src = ("try:\n    x()\nexcept Exception as exc:\n"
           "    log.debug('failed', exc_info=exc)\n")
    assert _scan(src) == []


def test_a_documented_silence_is_allowed():
    src = ("try:\n    x()\nexcept Exception:\n"
           "    # fallback-guard: allow: cache write is an optimisation\n"
           "    pass\n")
    assert _scan(src) == []


def test_the_allow_marker_does_not_leak_to_a_sibling_handler():
    src = ("try:\n    x()\nexcept ValueError:\n"
           "    # fallback-guard: allow: documented\n    pass\n"
           "try:\n    y()\nexcept Exception:\n    pass\n")
    assert len(_scan(src)) == 1


def test_a_parse_error_is_a_finding_never_a_pass(tmp_path):
    bad = tmp_path / "bad.py"
    bad.write_text("def f(:\n    pass\n")
    findings = fg.scan_file(bad)
    assert len(findings) == 1
    assert "does not parse" in findings[0].shape


def test_an_unreadable_file_is_a_finding_never_a_pass(tmp_path):
    missing = tmp_path / "gone.py"
    findings = fg.scan_file(missing)
    assert len(findings) == 1
    assert "cannot be read" in findings[0].shape


def _cli(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "mini_ork.cli.fallbacks", *args],
        cwd=ROOT, capture_output=True, text=True, timeout=120,
    )


def test_cli_exits_1_and_prints_a_fix_on_a_finding(tmp_path):
    target = tmp_path / "t.py"
    target.write_text("try:\n    x()\nexcept Exception:\n    pass\n")
    proc = _cli(str(target))
    assert proc.returncode == 1
    assert f"{target}:3" in proc.stderr
    assert "Fix:" in proc.stderr


def test_cli_json_shape(tmp_path):
    target = tmp_path / "clean.py"
    target.write_text("try:\n    x()\nexcept OSError:\n    pass\n")
    proc = _cli("--json", str(target))
    assert proc.returncode == 0
    payload = json.loads(proc.stdout)
    assert payload["pass"] is True
    assert payload["files_checked"] == 1


def test_the_shipped_framework_passes_its_own_check():
    """The layer that injects the rule must not break it."""
    findings = fg.scan_file(ROOT / "mini_ork" / "context_assembler.py")
    assert findings == []


# ── the code-fix verifier node ───────────────────────────────────────────────
VERIFIER = ROOT / "recipes" / "code-fix" / "verifiers" / "fallback_guard.py"


def _git_repo(path: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(["git", "add", "-A"], cwd=path, check=True)
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "base"],
        cwd=path, check=True,
    )


def _run_verifier(repo: Path, run_dir: Path) -> dict:
    env = {
        **os.environ,
        "MINI_ORK_RUN_DIR": str(run_dir),
        "MO_TARGET_CWD": str(repo),
    }
    env.pop("MINI_ORK_TOUCHED_FILES", None)
    proc = subprocess.run([sys.executable, str(VERIFIER)], capture_output=True,
                          text=True, env=env, timeout=120)
    return json.loads(proc.stdout)


def test_verifier_fails_a_patch_that_adds_a_swallow(tmp_path):
    repo, run = tmp_path / "repo", tmp_path / "run"
    repo.mkdir(); run.mkdir()
    (repo / "a.py").write_text("def f():\n    return 1\n")
    _git_repo(repo)
    (repo / "a.py").write_text(
        "def f():\n    try:\n        return g()\n    except Exception:\n        pass\n")
    payload = _run_verifier(repo, run)
    assert payload["pass"] is False
    assert payload["checks"][0]["gating"] is True
    assert payload["findings"][0]["line"] == 4
    assert Path(payload["evidence_path"]).is_file()


def test_verifier_passes_a_patch_with_no_swallow(tmp_path):
    repo, run = tmp_path / "repo", tmp_path / "run"
    repo.mkdir(); run.mkdir()
    (repo / "a.py").write_text("def f():\n    return 1\n")
    _git_repo(repo)
    (repo / "a.py").write_text("def f():\n    return 2\n")
    payload = _run_verifier(repo, run)
    assert payload["pass"] is True
    assert payload["checks"][0]["gating"] is True


def test_verifier_abstains_outside_git(tmp_path):
    """An unattributable tree is unmeasured, never a pass."""
    repo, run = tmp_path / "plain", tmp_path / "run"
    repo.mkdir(); run.mkdir()
    (repo / "a.py").write_text("try:\n    x()\nexcept Exception:\n    pass\n")
    payload = _run_verifier(repo, run)
    assert payload["status"] == "unverified"
    assert payload["checks"][0]["gating"] is False
