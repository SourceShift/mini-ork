"""framework-edit static-check: the `no-test-gaming` row.

eco6 K10 and K12 passed every static check while their production code gamed
the blind spec suite (K10) or patched another module at import time (K12). The
row runs mini_ork.verify.gaming_scan over the diff and the patched copy.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
VERIFIER = REPO / "recipes" / "framework-edit" / "verifiers" / "static-check.py"


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(cwd), *args], check=True,
                          capture_output=True, text=True).stdout


def _repo(tmp: Path) -> Path:
    root = tmp / "target"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "t")
    (root / "mod.py").write_text("VALUE = 1\n")
    _git(root, "add", "mod.py")
    _git(root, "commit", "-q", "-m", "base")
    return root


def _diff_for(root: Path, rel: str, new_text: str) -> str:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    existed = path.exists()
    path.write_text(new_text)
    if not existed:
        _git(root, "add", "-N", rel)
    diff = _git(root, "diff")
    if existed:
        _git(root, "checkout", "-q", "--", rel)
    else:
        _git(root, "rm", "-q", "--cached", rel)
        path.unlink()
    return diff


def _run(tmp: Path, root: Path, diff: str, kickoff: str = "") -> tuple[str, str]:
    run_dir = tmp / "run"
    run_dir.mkdir()
    (run_dir / "framework-edit.diff").write_text(diff)
    if kickoff:
        (run_dir / "kickoff.md").write_text(kickoff)
    env = {k: v for k, v in os.environ.items() if not k.startswith(("MINI_ORK_", "MO_"))}
    env.update(MINI_ORK_RUN_DIR=str(run_dir), MINI_ORK_ROOT=str(root))
    subprocess.run([sys.executable, str(VERIFIER)], cwd=root, env=env,
                   capture_output=True, text=True, timeout=180)
    rows = (run_dir / "verifier-static-check.checks.tsv").read_text().splitlines()
    row = next(r.split("\t")[2] for r in rows if r.startswith("no-test-gaming\t"))
    return row, (run_dir / "verifier-static-check.log").read_text()


K12_SHAPE = (
    "VALUE = 1\n\n\n"
    "def _register():\n"
    "    import mod as target\n"
    "    target.VALUE = 2\n\n\n"
    "_register()\n"
)


def test_clean_edit_passes(tmp_path):
    root = _repo(tmp_path)
    row, _ = _run(tmp_path, root, _diff_for(root, "mod.py", "VALUE = 2\n"))
    assert row == "true"


def test_pytest_env_probe_in_production_fails(tmp_path):
    root = _repo(tmp_path)
    diff = _diff_for(root, "mod.py",
                     'import os\nVALUE = 1 if os.environ.get("PYTEST_CURRENT_TEST") else 2\n')
    row, log = _run(tmp_path, root, diff)
    assert row == "false"
    assert "pytest-env-probe" in log


def test_import_time_cross_module_write_fails(tmp_path):
    root = _repo(tmp_path)
    row, log = _run(tmp_path, root, _diff_for(root, "pkg/patcher.py", K12_SHAPE))
    assert row == "false"
    assert "cross-module-write" in log


def test_same_probe_in_a_test_file_passes(tmp_path):
    root = _repo(tmp_path)
    diff = _diff_for(root, "tests/test_mod.py",
                     'import os\n\n\ndef test_x():\n    assert os.environ.get("PYTEST_CURRENT_TEST")\n')
    row, _ = _run(tmp_path, root, diff)
    assert row == "true"


def test_kickoff_allowance_exempts_the_named_path(tmp_path):
    root = _repo(tmp_path)
    diff = _diff_for(root, "mod.py",
                     'import os\nVALUE = 1 if os.environ.get("PYTEST_CURRENT_TEST") else 2\n')
    row, log = _run(tmp_path, root, diff, kickoff="# k\n\nAllow-test-probe: mod.py\n")
    assert row == "true"
    assert "kickoff allows: mod.py" in log
