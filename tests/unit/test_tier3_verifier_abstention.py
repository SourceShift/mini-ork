"""tier3 must not report a skipped check as a passed one.

The verifier emitted ``"pass": true, "mutation_rc": 0`` whenever the target repo
had no stryker config, while its own evidence log said ``no mutation tooling
configured - skipping``. Nothing in the JSON distinguished *mutation testing ran
and passed* from *mutation testing was never attempted*, so the tier-4 panel had
to adjudicate it by reading the log by hand — kimi opened a hard-rule violation
on exactly this in run-1791821930-72687 (2026-10-12), and the deterministic
tier-4 fallback in ``doc-to-features-loop`` gates on ``pass is True``.

An rc now belongs only to a check that ran; ``vacuous`` reports whether any did.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "recipes" / "recursive-validate-impl" / "verifiers" / "tier3-property-mutation.py"

_TOUCHED = "src/hooks/useChapterRewrite.ts"


def _fixture(tmp_path: Path) -> Path:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "implementer-summary.json").write_text(
        json.dumps({"ready_for_tier1": True, "touched_files": [_TOUCHED]}), encoding="utf-8")
    return run_dir


def _stub(path: Path, *, rc: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f'#!/bin/sh\nexit {rc}\n', encoding="utf-8")
    path.chmod(0o755)


def _run(tmp_path: Path, run_dir: Path):
    env = {**os.environ, "MINI_ORK_RUN_DIR": str(run_dir)}
    return subprocess.run([sys.executable, str(SCRIPT)], cwd=tmp_path, env=env,
                          capture_output=True, text=True)


def test_no_mutation_tooling_passes_vacuously_and_says_so(tmp_path):
    """A repo with no stryker config abstains — it does not verify."""
    run_dir = _fixture(tmp_path)

    result = _run(tmp_path, run_dir)

    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert payload["pass"] is True
    assert payload["mutation_rc"] is None, "a check that never ran must not report an rc"
    assert payload["property_rc"] is None
    assert payload["vacuous"] is True
    assert payload["checks_ran"] == []
    assert payload["checks_skipped"], "the abstention must name what was skipped"
    assert any("mutation" in reason for reason in payload["checks_skipped"])
    assert "skipping" in (run_dir / "tier3-evidence.log").read_text(encoding="utf-8")


def test_configured_mutation_run_reports_its_rc_and_is_not_vacuous(tmp_path):
    """With stryker configured the rc is real and the pass is earned."""
    run_dir = _fixture(tmp_path)
    (tmp_path / "stryker.conf.json").write_text("{}", encoding="utf-8")
    _stub(tmp_path / "node_modules" / ".bin" / "stryker", rc=0)

    result = _run(tmp_path, run_dir)

    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert payload["pass"] is True
    assert payload["mutation_rc"] == 0
    assert payload["vacuous"] is False
    assert payload["checks_ran"] == ["mutation"]


def test_a_failing_mutation_run_fails_the_node(tmp_path):
    """The rc that used to be hardcoded 0 must actually gate the verdict."""
    run_dir = _fixture(tmp_path)
    (tmp_path / "stryker.conf.json").write_text("{}", encoding="utf-8")
    _stub(tmp_path / "node_modules" / ".bin" / "stryker", rc=1)

    result = _run(tmp_path, run_dir)

    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert payload["pass"] is False
    assert payload["mutation_rc"] == 1
    assert payload["vacuous"] is False
