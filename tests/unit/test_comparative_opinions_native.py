"""Acceptance test for comparative-opinions' native dispatcher boundary."""
from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from collections import Counter
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "comparative-opinions.sh"


def test_comparative_panel_uses_native_module_without_bash_library(tmp_path: Path) -> None:
    root = tmp_path / "engine"
    docs = root / "docs" / "research"
    docs.mkdir(parents=True)
    (docs / "omnigent-vs-mini-ork-comparison.md").write_text("comparison\n")
    (docs / "omnigent-mini-ork-improvement-plan.md").write_text("plan\n")

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    call_log = tmp_path / "calls.log"
    python_wrapper = fake_bin / "python3"
    python_wrapper.write_text(
        """#!/usr/bin/env bash
if [ "$1" = "-m" ] && [ "$2" = "mini_ork.ported.llm_dispatch" ]; then
  shift 2
  model=""
  node_type=""
  while [ "$#" -gt 0 ]; do
    case "$1" in
      --model) model="$2"; shift 2 ;;
      --node-type) node_type="$2"; shift 2 ;;
      *) shift ;;
    esac
  done
  printf '%s|%s\n' "$model" "$node_type" >> "$CALL_LOG"
  printf '## Opinion: %s native result\n' "$model"
  exit 0
fi
exec "$REAL_PYTHON" "$@"
"""
    )
    python_wrapper.chmod(python_wrapper.stat().st_mode | stat.S_IXUSR)

    result = subprocess.run(
        ["bash", str(SCRIPT)],
        env={
            **os.environ,
            "MINI_ORK_ROOT": str(root),
            "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
            "REAL_PYTHON": sys.executable,
            "CALL_LOG": str(call_log),
        },
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 0, result.stderr
    assert not (root / "lib" / "llm-dispatch.sh").exists()
    run_dirs = list((root / ".mini-ork" / "runs").glob("comparative-opinions-*"))
    assert len(run_dirs) == 1
    manifest = json.loads((run_dirs[0] / "manifest.json").read_text())
    assert len(manifest["opinions"]) == 10
    assert all(item["headline"].startswith("Opinion:") for item in manifest["opinions"])

    calls = [line.split("|", 1) for line in call_log.read_text().splitlines()]
    assert Counter(model for model, _ in calls) == Counter(
        {"codex": 2, "minimax": 2, "glm": 2, "kimi": 2, "opus": 2}
    )
    assert all(node_type == f"{model}_lens" for model, node_type in calls)
