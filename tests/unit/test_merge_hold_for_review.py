"""Unit tests: the content id that binds a merge approval to exact content.

``content_ids`` returns ``(merge_base, head_tree)`` so an approval is held to
the content the human reviewed; any git failure returns ``("", "")`` so the
gate fails closed. All git work happens in throwaway ``tmp_path`` repos.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from mini_ork.vcs.content_ids import content_ids  # noqa: E402


# ── git helper ─────────────────────────────────────────────────────────────


def _g(cwd, *args, date=None, check=True):
    env = {**os.environ,
           "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@e",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@e"}
    if date:
        env["GIT_AUTHOR_DATE"] = env["GIT_COMMITTER_DATE"] = date
    r = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True, env=env)
    if check and r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)}: {r.stderr}")
    return r.stdout.strip()


# ── content_ids ────────────────────────────────────────────────────────────


def test_1_content_ids_returns_merge_base_and_tree(tmp_path):
    p = tmp_path / "r"; p.mkdir()
    _g(p, "init", "-q", "-b", "main")
    (p / "base.txt").write_text("base\n")
    _g(p, "add", "-A"); _g(p, "commit", "-qm", "base", date="2026-01-01T00:00:00Z")
    _g(p, "checkout", "-q", "-b", "feat/x")
    (p / "feature.txt").write_text("the feature\n")
    _g(p, "add", "-A"); _g(p, "commit", "-qm", "feat", date="2026-02-01T00:00:00Z")
    _g(p, "checkout", "-q", "main")

    expected_base = _g(p, "merge-base", "main", "feat/x")
    expected_tree = _g(p, "rev-parse", "feat/x^{tree}")
    base, tree = content_ids(str(p), "feat/x", "main")
    assert base == expected_base
    assert tree == expected_tree
    assert base and tree


def test_1_content_ids_missing_branch_returns_empty_tuple(tmp_path):
    p = tmp_path / "r"; p.mkdir()
    _g(p, "init", "-q", "-b", "main")
    (p / "base.txt").write_text("base\n")
    _g(p, "add", "-A"); _g(p, "commit", "-qm", "base", date="2026-01-01T00:00:00Z")

    base, tree = content_ids(str(p), "does/not/exist", "main")
    assert base == "" and tree == ""
