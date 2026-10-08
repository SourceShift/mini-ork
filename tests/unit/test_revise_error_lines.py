"""#13 — a revise round must carry the failing verifier's REAL errors.

Live defect (run ``ide-orca-b2b-story-20261008113340``, code-fix / Rust): both
revise rounds failed on a compile error (``post_rc=101``), but
``revise/round-N.md`` held only the verifier's JSON verdict line
("post-patch failing; see log"). The implementer fixed blind.

Two causes, both fixed in ``_revise_failure_section``:

1. The code-fix verifiers write ``verifier_<stem>.log`` (UNDERSCORE — see
   ``recipes/code-fix/verifiers/test.py``), while the round builder only looked
   for ``verifier-<stem>.log`` (hyphen) and ``evidence/<stem>.log`` — so the
   real log was never read.
2. Even when read, only a raw tail was appended. A failing test/typecheck
   verifier's signal is the error LINES, so those are now selected explicitly
   (``^error``, ``-->``, ``FAILED``, ``Traceback``, ``*Error``), capped to keep
   issue #9b's budget — findings, not the whole attempt.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
from mini_ork.cli import execute as ex  # noqa: E402

# A code-fix verifier node field: [node_id, node_type, ..., verifier_ref@5].
_FIELD = ("test", "verifier", "", None, None, "verifiers/test.py")

_CARGO_LOG = """\
   Compiling orca v0.1.0 (/target)
error[E0432]: unresolved import `crate::missing`
 --> src/main.rs:3:5
  |
3 | use crate::missing::Thing;
  |     ^^^^^^^ no `missing` in the crate root
error[E0308]: mismatched types
  --> src/lib.rs:12:20
   |
12 |     let x: u32 = "oops";
   |            ---   ^^^^^^ expected `u32`, found `&str`
error: could not compile `orca` (bin "orca") due to 2 previous errors
"""


def _run_dir(tmp_path: Path) -> Path:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    return run_dir


def test_a_round_file_carries_the_failing_cargo_errors(tmp_path):
    """Acceptance: the round file for a failing cargo-style log contains the
    error lines, not just the JSON verdict."""
    run_dir = _run_dir(tmp_path)
    # The real log is the UNDERSCORE name — the one the old code missed.
    (run_dir / "verifier_test.log").write_text(_CARGO_LOG)
    (run_dir / "verifier_test.json").write_text(
        json.dumps({"verdict": "fail", "reasons": ["post-patch failing; see log"]}))

    field = _FIELD
    path = ex._write_revise_feedback(str(run_dir), 1, 2, [(field, 1, "error")])
    text = Path(path).read_text()

    assert "error[E0432]: unresolved import" in text
    assert "--> src/main.rs:3:5" in text
    assert "error[E0308]: mismatched types" in text
    # The JSON summary is kept, but is no longer the ONLY thing the fixer sees.
    assert "see log" in text


def test_underscore_named_log_is_found(tmp_path):
    """Regression on the naming bug: only ``verifier_test.log`` exists, and the
    section must still surface its errors."""
    run_dir = _run_dir(tmp_path)
    (run_dir / "verifier_test.log").write_text("error[E0599]: no method named `foo`\n")

    section = ex._revise_failure_section(str(run_dir), _FIELD, "error")
    assert "no method named `foo`" in section


def test_pytest_failed_lines_are_captured(tmp_path):
    run_dir = _run_dir(tmp_path)
    (run_dir / "verifier_test.log").write_text(
        "collected 3 items\n"
        "test_a.py::test_one PASSED\n"
        "test_a.py::test_two FAILED\n"
        "AssertionError: 1 != 2\n"
    )
    section = ex._revise_failure_section(str(run_dir), _FIELD, "error")
    assert "test_two FAILED" in section
    assert "AssertionError: 1 != 2" in section


def test_error_lines_are_capped_for_the_budget(tmp_path):
    run_dir = _run_dir(tmp_path)
    (run_dir / "verifier_test.log").write_text(
        "".join(f"error[E{i:04d}]: failure number {i}\n" for i in range(500)))
    section = ex._revise_failure_section(str(run_dir), _FIELD, "error")
    # 40-line cap (issue #9b budget) — nowhere near all 500.
    assert section.count("failure number") <= 40
    assert section.count("failure number") >= 1


def test_falls_back_to_a_tail_when_no_error_lines(tmp_path):
    """A log with no error-looking line (unusual) still yields a bounded tail so
    a diagnostic is not silently dropped."""
    run_dir = _run_dir(tmp_path)
    (run_dir / "verifier_test.log").write_text(
        "".join(f"step {i} ok\n" for i in range(100)))
    section = ex._revise_failure_section(str(run_dir), _FIELD, "error")
    assert "evidence tail" in section
    assert "step 99 ok" in section            # the tail end
    assert "step 0 ok" not in section          # bounded, not the whole log
