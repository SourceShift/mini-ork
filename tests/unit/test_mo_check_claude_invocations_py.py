"""Parity gate: mini_ork.ported.mo_check_claude_invocations vs the bash lint.

A fixture repo (good/bad/comment/doc/provider invocations) is scanned by the
LIVE bash entrypoint and the port; the return code and the set of violations
must match.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
from mini_ork.ported import mo_check_claude_invocations as chk  # noqa: E402

BIN = REPO / "bin" / "mo-check-claude-invocations"


def _fixture(tmp_path: Path, clean: bool):
    (tmp_path / "lib").mkdir()
    (tmp_path / "lib" / "providers").mkdir()
    (tmp_path / "bin").mkdir()
    # clean invocation — flag on same line
    (tmp_path / "lib" / "good.sh").write_text(
        'run() {\n  claude --print --permission-mode bypassPermissions "$p"\n}\n')
    # clean — flag within window (next lines)
    (tmp_path / "lib" / "good2.sh").write_text(
        'x() {\n  claude -p "$prompt" \\\n    --output-format text \\\n    --dangerously-skip-permissions\n}\n')
    # comment + doc lines — skipped, not violations
    (tmp_path / "lib" / "docs.sh").write_text(
        '# claude --print example in a comment\n'
        'echo "hi"  # see: claude -p usage\n')
    # provider wrapper — skipped even without flag
    (tmp_path / "lib" / "providers" / "cl_x.sh").write_text('exec claude --print "$@"\n')
    if not clean:
        # real violations
        (tmp_path / "lib" / "bad.sh").write_text('do_it() {\n  claude --print "$prompt"\n}\n')
        (tmp_path / "bin" / "mini-ork-rogue").write_text('#!/bin/bash\nclaude -p "$q"\n')
    return tmp_path


def _bash(root):
    r = subprocess.run(["bash", str(BIN)], capture_output=True, text=True,
                       env={**os.environ, "MINI_ORK_ROOT": str(root)})
    viol = sorted(l.strip().lstrip("✗ ").strip() for l in r.stderr.splitlines()
                  if ": claude invocation without" in l)
    return r.returncode, viol


def _py(root):
    total, checked, violations = chk.check(str(root))
    rc = chk.main(root=str(root))
    return rc, sorted(violations)


def test_clean_repo_parity(tmp_path):
    root = _fixture(tmp_path, clean=True)
    rc_b, vb = _bash(root)
    rc_p, vp = _py(root)
    assert rc_b == rc_p == 0
    assert vb == vp == []


def test_violations_parity(tmp_path):
    root = _fixture(tmp_path, clean=False)
    rc_b, vb = _bash(root)
    rc_p, vp = _py(root)
    assert rc_b == rc_p == 1
    assert vb == vp                      # identical violation set
    assert len(vp) == 2                  # bad.sh + mini-ork-rogue
    assert any("bad.sh" in v for v in vp) and any("mini-ork-rogue" in v for v in vp)
    # comment/doc/provider lines never flagged
    assert not any("docs.sh" in v or "providers" in v for v in vp)
