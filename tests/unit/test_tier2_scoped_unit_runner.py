"""tier2 must route test files through the repo's own multi-project jest config.

The verifier's rule was "if ``server/jest.config.js`` exists, send EVERY test
file to ``test:server``". A repo that keeps two jest projects (frontend specs on
jsdom, server specs on node) also ships a root ``jest.config.js`` whose whole
purpose is to route each path to the project whose ``testMatch`` claims it, and
that rule sent ``src/**`` specs to the server project. Jest answers

    No tests found, exiting with code 1
    No files found in <repo root>.

for them — a TOOLCHAIN failure the node reads as a CODE failure.

run-1791821930-72687 (2026-10-12): tier1 passed after an eslint fix, then tier2
failed both frontend specs this way while
``jest --config jest.config.js --listTests`` claimed both.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "recipes" / "recursive-validate-impl" / "verifiers" / "tier2-scoped-unit.py"

_TEST_FILE = "src/hooks/__tests__/useChapterRewrite.test.ts"


def _stub(path: Path, record_to: str) -> None:
    """A fake tool that records the argv it was handed and succeeds."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f'#!/bin/sh\nprintf \'%s\\n\' "$@" > {record_to}\nexit 0\n',
                    encoding="utf-8")
    path.chmod(0o755)


def _fixture(tmp_path: Path, *, root_config: bool) -> Path:
    (tmp_path / "package.json").write_text(
        json.dumps({"name": "fixture", "scripts": {
            "test:server": "bash scripts/jest-guard.sh raw -- --config server/jest.config.js"}}),
        encoding="utf-8")
    (tmp_path / "server").mkdir()
    (tmp_path / "server" / "jest.config.js").write_text("module.exports = {};\n", encoding="utf-8")
    if root_config:
        (tmp_path / "jest.config.js").write_text(
            "module.exports = { projects: ['<rootDir>/tests/jest.config.js',\n"
            "                        '<rootDir>/server/jest.config.js'] };\n", encoding="utf-8")
    test_file = tmp_path / _TEST_FILE
    test_file.parent.mkdir(parents=True)
    test_file.write_text("it('x', () => {});\n", encoding="utf-8")
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "implementer-summary.json").write_text(
        json.dumps({"ready_for_tier1": True, "touched_files": [_TEST_FILE]}), encoding="utf-8")
    return run_dir


def _run(tmp_path: Path, run_dir: Path, *, path_prefix: Path | None = None):
    env = {**os.environ, "MINI_ORK_RUN_DIR": str(run_dir)}
    if path_prefix is not None:
        env["PATH"] = f"{path_prefix}{os.pathsep}{env.get('PATH', '')}"
    return subprocess.run([sys.executable, str(SCRIPT)], cwd=tmp_path, env=env,
                          capture_output=True, text=True)


def _recorded(name: str = "jest-argv.txt") -> Path:
    return Path.cwd() / name


def test_a_multi_project_root_config_is_the_runner_that_is_used(tmp_path, monkeypatch):
    """The repo's own router claims every handed file; picking a side does not."""
    run_dir = _fixture(tmp_path, root_config=True)
    _stub(tmp_path / "node_modules" / ".bin" / "jest", "jest-argv.txt")
    monkeypatch.chdir(tmp_path)

    result = _run(tmp_path, run_dir)

    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(result.stdout)["pass"] is True
    argv = _recorded().read_text(encoding="utf-8")
    assert "--config" in argv
    assert "jest.config.js" in argv
    assert "server/jest.config.js" not in argv
    assert _TEST_FILE in argv


def test_without_a_root_config_the_server_runner_is_still_used(tmp_path, monkeypatch):
    """The old heuristic is unchanged for repos that really are server-only."""
    run_dir = _fixture(tmp_path, root_config=False)
    script = tmp_path / "stubs" / "pnpm"
    _stub(script, "pnpm-argv.txt")
    monkeypatch.chdir(tmp_path)

    result = _run(tmp_path, run_dir, path_prefix=script.parent)

    assert result.returncode == 0, result.stdout + result.stderr
    argv = _recorded("pnpm-argv.txt").read_text(encoding="utf-8")
    assert "test:server" in argv
    assert _TEST_FILE in argv
