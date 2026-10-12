r"""The auto-repair round must read the verifier's own declared evidence log.

``_failing_verifier_sections`` resolved a failing verifier's log by deriving a
stem (``evidence/<stem>.log``) and never consulted the ``evidence_path`` the
verifier records about itself. ``tier1-compile-typecheck`` writes its tool
output to ``tier1-evidence.log`` and leaves only a one-line JSON summary at
``evidence/<stem>.log``, so the derived path *existed* — it just contained no
error text — and the round told the implementer "the build/test output was not
captured; re-run the verification command first". That is a claim the code never
checked, and an instruction the implementer cannot carry out against a toolchain
wall.

The regex was never the obstacle: ``_ERROR_LINE_RE``'s ``\w*Error\b`` under
``re.IGNORECASE`` matches the word "error" anywhere, so the npm output would
have matched had anything read it.

run-1791821930-72687 (2026-10-12) spent its whole repair budget that way and
gave up with "no progress — the same failure signature repeats", while the npm
``EBADDEVENGINES`` refusal that actually stopped the run sat unread in
``evidence_path``.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from mini_ork.recovery import auto_repair as ar  # noqa: E402

# Verbatim shape of the run's evidence log: the repo's husky hook, then npm
# refusing to start under an ambient node the target repo's devEngines rejects.
_NPM_WALL = "\n".join([
    "$ bash .husky/_typecheck-touched.sh src/hooks/useChapterRewrite.ts",
    "npm error code EBADDEVENGINES",
    "npm error EBADDEVENGINES Invalid semver version "
    '">=22.13 <25" does not match "v25.5.0" for "runtime"',
    "npm error A complete log of this run can be found in: /tmp/x-debug-0.log",
])


def _failing_run(tmp_path, *, evidence_path=None, summary=True, log_text=_NPM_WALL):
    """A run dir shaped like the real one: a failing verifier whose JSON declares
    ``evidence_path``, a stem-derived ``evidence/<stem>.log`` sibling, and the
    tool output at the declared path."""
    run_dir = tmp_path / "runs" / "run-1"
    (run_dir / "evidence").mkdir(parents=True)
    detail = run_dir / "tier1-evidence.log"
    detail.write_text(log_text, encoding="utf-8")

    payload = {"verifier": "tier1-compile-typecheck", "pass": False,
               "tsc_rc": 0, "lint_rc": 1, "shell_rc": 0}
    if evidence_path is None:
        payload["evidence_path"] = str(detail)
    elif evidence_path is not False:
        payload["evidence_path"] = evidence_path
    (run_dir / "verifier_tier1-compile-typecheck.json").write_text(
        json.dumps(payload), encoding="utf-8")
    if summary:
        # The real run left the verifier's own JSON blob at the stem-derived path.
        (run_dir / "evidence" / "tier1-compile-typecheck.log").write_text(
            json.dumps(payload) + "\n", encoding="utf-8")
    return run_dir


def test_the_declared_evidence_path_is_read(tmp_path):
    section = ar._failing_verifier_sections(_failing_run(tmp_path))[0]
    assert "## Verifier (tier1-compile-typecheck)" in section
    assert "EBADDEVENGINES" in section
    assert "was not captured" not in section


def test_a_log_with_no_error_lines_is_shown_rather_than_denied(tmp_path):
    r"""The old message asserted the output was missing; here it plainly is not.

    Log content with no error-shaped line at all (``\w*Error\b``, ``FAILED``,
    ``-->``): the round must still show what the tool printed.
    """
    run_dir = _failing_run(
        tmp_path, log_text="$ bash .husky/_typecheck-touched.sh src/x.ts\n"
                           "husky - hook exited 1\n(node v25.5.0)")
    section = ar._failing_verifier_sections(run_dir)[0]
    assert "raw tail of tier1-evidence.log" in section
    assert "no source-level" in section
    assert "hook exited 1" in section
    assert "was not captured" not in section


def test_error_shaped_lines_still_win_over_the_raw_tail(tmp_path):
    run_dir = _failing_run(
        tmp_path, log_text="TypeError: cannot read properties of null\n    at foo")
    section = ar._failing_verifier_sections(run_dir)[0]
    assert "verifier errors (tier1-evidence.log):" in section
    assert "TypeError" in section
    assert "raw tail of" not in section


def test_a_missing_log_still_reports_not_captured(tmp_path):
    run_dir = _failing_run(tmp_path)
    (run_dir / "tier1-evidence.log").unlink()
    (run_dir / "evidence" / "tier1-compile-typecheck.log").unlink()
    section = ar._failing_verifier_sections(run_dir)[0]
    assert "the build/test output was not captured" in section


def test_a_declared_path_outside_the_run_dir_is_never_read(tmp_path):
    """``evidence_path`` is run-controlled data, not a licence to read any file."""
    secret = tmp_path / "outside.log"
    secret.write_text("SECRET-MARKER\n", encoding="utf-8")
    section = ar._failing_verifier_sections(
        _failing_run(tmp_path, evidence_path=str(secret)))[0]
    assert "SECRET-MARKER" not in section


def test_declared_evidence_path_rejects_escapes(tmp_path):
    run_dir = tmp_path / "runs" / "run-1"
    run_dir.mkdir(parents=True)
    outside = tmp_path / "outside.log"
    outside.write_text("x\n", encoding="utf-8")
    assert ar._declared_evidence_path({"evidence_path": str(outside)}, run_dir) == ""
    assert ar._declared_evidence_path({}, run_dir) == ""
    assert ar._declared_evidence_path({"evidence_path": ""}, run_dir) == ""


def test_declared_evidence_path_accepts_one_inside(tmp_path):
    run_dir = tmp_path / "runs" / "run-1"
    run_dir.mkdir(parents=True)
    inside = run_dir / "tier1-evidence.log"
    inside.write_text("x\n", encoding="utf-8")
    assert ar._declared_evidence_path({"evidence_path": str(inside)}, run_dir) == str(inside)
    assert ar._declared_evidence_path(
        {"evidence_path": str(run_dir / "absent.log")}, run_dir) == ""


def test_log_tail_reads_the_first_readable_file(tmp_path):
    assert ar._log_tail([]) == ("", [])
    assert ar._log_tail([str(tmp_path / "absent.log")]) == ("", [])
    log = tmp_path / "a.log"
    log.write_text("\n".join(f"line{i}" for i in range(30)) + "\n", encoding="utf-8")
    path, tail = ar._log_tail([str(log)], max_lines=3)
    assert path == str(log)
    assert tail == ["line27", "line28", "line29"]
