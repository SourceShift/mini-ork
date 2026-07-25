"""Unit tests for the extracted mini_ork/review/* pure helpers + re-export parity.

Covers helpers that had no direct coverage before the SRP split of
``mini_ork/pre_push_review.py``: ``_truncate_issue``, ``_read_diff``,
``_resolve_check_path``, ``_resolve_root``, and the re-export contract
(public names must remain importable from ``mini_ork.pre_push_review``).
"""
from __future__ import annotations

import os
from pathlib import Path

from mini_ork import pre_push_review as ppr
from mini_ork.review import common, gitdiff, lenses, verdict


def test_truncate_issue_applies_bash_contract_lengths():
    issue = {
        "lens": "heuristic.x",
        "severity": "high",
        "file": "f.sh",
        "line": 3,
        "title": "t" * 400,
        "description": "d" * 3000,
        "suggested_fix": "s" * 1500,
    }
    out = common._truncate_issue(issue)
    assert len(out["title"]) == 300
    assert len(out["description"]) == 2000
    assert len(out["suggested_fix"]) == 1000
    assert out["lens"] == "heuristic.x"
    assert out["severity"] == "high"
    assert out["file"] == "f.sh"
    assert out["line"] == 3


def test_truncate_issue_defaults():
    out = common._truncate_issue({})
    assert out["lens"] == "?"
    assert out["severity"] == "medium"
    assert out["file"] == "?"
    assert out["line"] is None
    assert out["title"] == ""
    assert out["description"] == ""
    assert out["suggested_fix"] == ""


def test_truncate_issue_none_file_maps_to_question_mark():
    out = common._truncate_issue({"file": None, "title": None})
    assert out["file"] == "?"
    assert out["title"] == ""


def test_read_diff_accepts_inline_diff_text():
    diff = "diff --git a/x b/x\n+++ b/x\n+a\n"
    assert common._read_diff(diff) == diff


def test_read_diff_reads_from_path(tmp_path):
    p = tmp_path / "d.patch"
    p.write_text("+++ b/x\n+a\n")
    assert common._read_diff(str(p)) == "+++ b/x\n+a\n"


def test_read_diff_falls_back_to_literal_string():
    # A non-path, non-diff string is returned verbatim.
    assert common._read_diff("not-a-real-path-xyz") == "not-a-real-path-xyz"


def test_resolve_check_path_behaviour():
    assert common._resolve_check_path("lib/a.sh", None) == "lib/a.sh"
    assert common._resolve_check_path("lib/a.sh", "/repo") == os.path.join("/repo", "lib/a.sh")
    assert common._resolve_check_path("/abs/a.sh", "/repo") == "/abs/a.sh"


def test_resolve_root_defaults_to_repo_root(monkeypatch):
    monkeypatch.delenv("MINI_ORK_ROOT", raising=False)
    root = common._resolve_root()
    # mini_ork/review/common.py → three parents up is the repo root, which
    # must contain the mini_ork package itself.
    assert (Path(root) / "mini_ork" / "pre_push_review.py").is_file()


def test_reexport_parity_public_names():
    """Every moved public name resolves to the same object via pre_push_review."""
    assert ppr.check_bash_syntax is lenses.check_bash_syntax
    assert ppr.check_migration_safety is lenses.check_migration_safety
    assert ppr.check_added_todos is lenses.check_added_todos
    assert ppr.check_diff_size is lenses.check_diff_size
    assert ppr.check_test_pairing is lenses.check_test_pairing
    assert ppr.check_secret_patterns is lenses.check_secret_patterns
    assert ppr.compute_verdict is verdict.compute_verdict
    assert ppr._default_llm_panel is lenses._default_llm_panel
    assert ppr._REVIEW_PROMPT == lenses._REVIEW_PROMPT


def test_reexport_parity_private_helpers():
    assert ppr._resolve_db is common._resolve_db
    assert ppr._resolve_home is common._resolve_home
    assert ppr._resolve_root is common._resolve_root
    assert ppr._truncate_issue is common._truncate_issue
    assert ppr._read_diff is common._read_diff
    assert ppr._compute_base is gitdiff._compute_base
    assert ppr._git_diff is gitdiff._git_diff
    assert ppr._git_shortstat is gitdiff._git_shortstat
    assert ppr._count_diff_lines is gitdiff._count_diff_lines
    assert ppr._run_heuristic_lenses is lenses._run_heuristic_lenses
    assert ppr._apply_verdict is verdict._apply_verdict
    assert ppr._TITLE_MAX == 300
    assert ppr._DESCRIPTION_MAX == 2000
    assert ppr._SUGGESTED_FIX_MAX == 1000


def test_all_exports_resolve():
    for name in ppr.__all__:
        assert getattr(ppr, name) is not None
