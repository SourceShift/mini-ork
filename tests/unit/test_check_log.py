"""``mini_ork.verify.check_log`` — verdict-first selection of failed sections.

The static-check verifier writes one ``[<check-id>] <description>`` section per
check, ending in ``FAIL`` or ``ok``. Selecting feedback lines by their text
alone pulls in a *passing* check's by-design failures (the forward apply-check
fails when the patch is already applied), which is how three revise/repair
rounds went looking at ``error: patch failed`` instead of the real finding.

Hermetic: tmp logs only.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from mini_ork.verify.check_log import SECTION_RE, failing_sections  # noqa: E402

LOG_TEXT = "\n".join([
    "[ok-lint] files under mini_ork/ pass ruff F,E9",
    "  0 issues",
    "  ok",
    "[no-test-gaming] diff adds no test-harness detection or "
    "cross-module patching to production code",
    "  mini_ork/cli/execute_handlers.py:1715: pytest-env-probe: "
    "'that detects a test run (PYTEST_CURRENT_TEST, ...'",
    "  mini_ork/cli/execute_handlers.py:1742: cross-module patch: "
    "monkeypatches mini_ork.gates.gate_registry  ",
    "  FAIL",
    "[diff-apply-check-clean] reverse apply of the diff is clean",
    "  error: patch failed: mini_ork/cli/execute_handlers.py: "
    "No such file or directory",
    "  ok",
]) + "\n"


def _write(tmp_path: Path, text: str = LOG_TEXT) -> str:
    log = tmp_path / "static-check.log"
    log.write_text(text, encoding="utf-8")
    return str(log)


def test_returns_only_the_failed_section(tmp_path):
    """The FAIL section's header and body; no passing check's lines."""
    assert failing_sections(_write(tmp_path)) == [
        "[no-test-gaming] diff adds no test-harness detection or "
        "cross-module patching to production code",
        "  mini_ork/cli/execute_handlers.py:1715: pytest-env-probe: "
        "'that detects a test run (PYTEST_CURRENT_TEST, ...'",
        "  mini_ork/cli/execute_handlers.py:1742: cross-module patch: "
        "monkeypatches mini_ork.gates.gate_registry",
    ]


def test_names_filter(tmp_path):
    log = _write(tmp_path)
    # A filter that matches no failed id falls back to the unfiltered failed
    # sections, so a verifier naming its checks differently does not lose them.
    assert failing_sections(log, names=["other"]) == failing_sections(log)
    assert len(failing_sections(log, names=["no-test-gaming"])) == 3


def test_per_section_cap_includes_header(tmp_path):
    lines = ["[big] fifty body lines"]
    lines += [f"  body-{i:03d}" for i in range(50)]
    lines.append("  FAIL")
    result = failing_sections(_write(tmp_path, "\n".join(lines) + "\n"))
    # The tail survives: header + marker + the last 18 body lines = 20.
    assert result == ["[big] fifty body lines",
                      "  … (32 earlier lines omitted)"] + [
        f"  body-{i:03d}" for i in range(32, 50)]


def test_total_cap_across_sections(tmp_path):
    lines = ["[a] fifty body lines"]
    lines += [f"  a-{i:03d}" for i in range(50)]
    lines.append("  FAIL")
    lines += ["[b] fifty more body lines"]
    lines += [f"  b-{i:03d}" for i in range(50)]
    lines.append("  FAIL")
    result = failing_sections(_write(tmp_path, "\n".join(lines) + "\n"),
                              max_lines=30)
    assert len(result) == 30
    assert result[0] == "[a] fifty body lines"
    assert result[20] == "[b] fifty more body lines"


def test_missing_and_unreadable_are_empty(tmp_path):
    assert failing_sections(str(tmp_path / "absent.log")) == []
    assert failing_sections(str(tmp_path)) == []


def test_no_failed_section_is_empty(tmp_path):
    text = "[ok] fine\n  ok\n"
    assert failing_sections(_write(tmp_path, text)) == []


def test_section_re_contract():
    assert SECTION_RE.match("[no-test-gaming] x").group(1) == "no-test-gaming"
    assert SECTION_RE.match("[py311.lint:e9] x") is not None
    assert SECTION_RE.match("  [indented] x") is None


# ── F1: verdict-first, tail-keeping, byte caps, name fallback ──────────────

_OMIT_RE = re.compile(r"… \(\d+ earlier lines omitted\)")


def test_bare_fail_mid_body_is_not_a_verdict(tmp_path):
    """Only the section's last non-blank line decides pass/fail."""
    text = "\n".join([
        "[alpha] check alpha",
        "  tool summary: 3 files checked",
        "  FAIL",
        "  more tool output",
        "  ok",
        "[beta] check beta",
        "  mini_ork/foo.py:12:1: F401 imported but unused",
        "  FAIL",
    ]) + "\n"
    log = _write(tmp_path, text)
    headers = [line for line in failing_sections(log) if line.startswith("[")]
    assert headers == ["[beta] check beta"]


def test_failed_section_keeps_the_finding_at_the_tail(tmp_path):
    """The finding (last body line) survives the per-section cap."""
    body = [f"  line-{i:02d}" for i in range(1, 40)] + ["  FINDING: boom"]
    log = _write(tmp_path, "\n".join(["[tail] long"] + body + ["  FAIL"]) + "\n")
    result = failing_sections(log, max_lines_per_check=20)

    assert len(result) <= 20
    assert result[0] == "[tail] long"
    assert _OMIT_RE.search(result[1]), result
    assert "  FINDING: boom" in result
    assert not any(line.strip() in ("line-01", "line-10") for line in result)


def test_long_line_is_truncated_to_401(tmp_path):
    """A 1,000-char body line comes back as 401 chars ending in ``…``."""
    long_line = "  " + "k" * 998
    log = _write(tmp_path, "\n".join(
        ["[wide] wide", long_line, "  FAIL"]) + "\n")
    result = failing_sections(log)

    assert all(len(line) <= 401 for line in result)
    assert "  " + "k" * 398 + "…" in result


def test_name_filter_matching_nothing_falls_back(tmp_path):
    """An unmatched name filter returns the unfiltered failed sections."""
    text = "\n".join([
        "[alpha] a", "  boom", "  FAIL",
        "[gamma] g", "  fine", "  ok",
    ]) + "\n"
    log = _write(tmp_path, text)

    fallback = failing_sections(log, names=["not-in-the-log"])
    assert any(line.startswith("[alpha]") for line in fallback)
    assert not any(line.startswith("[gamma]") for line in fallback)
    assert failing_sections(log, names=None) == fallback
