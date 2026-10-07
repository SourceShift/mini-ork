"""Unit tests for mini_ork.triage.blame — pure attribution, no I/O."""
from __future__ import annotations

from mini_ork.triage.blame import NodeFailure, attribute, frame_paths

ROOT = "/repo"

_FW_TRACE = """Traceback (most recent call last):
  File "/repo/mini_ork/cli/execute.py", line 10, in run
    boom()
RuntimeError: kablammo
"""

_CONSUMER_TRACE = """Traceback (most recent call last):
  File "/home/me/proj/verifier.py", line 3, in main
    x = 1 / 0
ZeroDivisionError: division by zero
"""


def test_verdict_fail_is_consumer():
    v, ev = attribute(NodeFailure("review", finish_reason="verdict_fail"), root=ROOT)
    assert v == "consumer"
    assert ev[0].rule == "verdict"


def test_verdict_revise_is_consumer():
    v, _ = attribute(NodeFailure("review", finish_reason="verdict_revise"), root=ROOT)
    assert v == "consumer"


def test_timeout_is_unknown():
    v, ev = attribute(NodeFailure("slow", finish_reason="timeout"), root=ROOT)
    assert v == "unknown"
    assert ev[0].rule == "infra"


def test_cost_limit_is_unknown():
    v, _ = attribute(NodeFailure("spendy", finish_reason="cost_limit"), root=ROOT)
    assert v == "unknown"


def test_provider_error_is_unknown_even_with_traceback():
    log = "HTTP 401 Unauthorized: invalid api key\n" + _FW_TRACE
    v, ev = attribute(NodeFailure("impl", finish_reason="error", log_excerpt=log), root=ROOT)
    assert v == "unknown"
    assert ev[0].rule == "provider"


def test_framework_traceback_is_mini_ork():
    v, ev = attribute(NodeFailure("cycle_gate", finish_reason="error", log_excerpt=_FW_TRACE), root=ROOT)
    assert v == "mini_ork"
    assert "mini_ork" in ev[0].detail


def test_shipped_recipe_verifier_traceback_is_mini_ork():
    log = 'File "/repo/recipes/framework-edit/verifiers/test.py", line 5, in check\n'
    v, _ = attribute(NodeFailure("test_verifier", finish_reason="error", log_excerpt=log), root=ROOT)
    assert v == "mini_ork"


def test_non_framework_traceback_is_consumer():
    v, ev = attribute(NodeFailure("impl", finish_reason="error", log_excerpt=_CONSUMER_TRACE), root=ROOT)
    assert v == "consumer"
    assert ev[0].rule == "traceback"


def test_error_without_traceback_is_unknown():
    v, ev = attribute(NodeFailure("impl", finish_reason="error", log_excerpt="boom\n"), root=ROOT)
    assert v == "unknown"
    assert ev[0].rule == "error"


def test_no_signal_is_unknown():
    v, _ = attribute(NodeFailure("impl"), root=ROOT)
    assert v == "unknown"


def test_framework_frames_win_over_consumer_frames():
    log = _CONSUMER_TRACE + _FW_TRACE
    v, _ = attribute(NodeFailure("impl", finish_reason="error", log_excerpt=log), root=ROOT)
    assert v == "mini_ork"


def test_frame_paths_parses_tracebacks():
    assert frame_paths(_FW_TRACE) == ["/repo/mini_ork/cli/execute.py"]
    assert frame_paths("no frames here") == []


def test_framework_frame_in_foreign_checkout_is_mini_ork():
    # Real case: the failed run executed a shipped recipe verifier out of a
    # *worktree* whose root differs from the triager's — shape must still win.
    log = (
        'Traceback (most recent call last):\n'
        '  File "/Volumes/x/mini-ork-worktrees/heldout-baseline/recipes/code-fix/'
        'verifiers/test.py", line 298, in <module>\n'
        "TypeError: unsupported operand type(s) for |: 'type' and 'NoneType'\n"
    )
    v, ev = attribute(NodeFailure("test_verifier", finish_reason="error", log_excerpt=log), root=ROOT)
    assert v == "mini_ork"
    assert "recipes" in ev[0].detail


def test_mini_ork_package_frame_without_root_is_mini_ork():
    # No root given — shape alone resolves the package.
    v, _ = attribute(NodeFailure("impl", finish_reason="error", log_excerpt=_FW_TRACE))
    assert v == "mini_ork"
