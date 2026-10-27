"""``mini_ork.verify.check_log`` — verdict-first selection of failed sections.

The static-check verifier writes one ``[<check-id>] <description>`` section per
check, ending in ``FAIL`` or ``ok``. Selecting feedback lines by their text
alone pulls in a *passing* check's by-design failures (the forward apply-check
fails when the patch is already applied), which is how three revise/repair
rounds went looking at ``error: patch failed`` instead of the real finding.

Hermetic: tmp logs only.
"""
from __future__ import annotations

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
    assert failing_sections(log, names=["other"]) == []
    assert len(failing_sections(log, names=["no-test-gaming"])) == 3


def test_per_section_cap_includes_header(tmp_path):
    lines = ["[big] fifty body lines"]
    lines += [f"  body-{i:03d}" for i in range(50)]
    lines.append("  FAIL")
    result = failing_sections(_write(tmp_path, "\n".join(lines) + "\n"))
    assert result == ["[big] fifty body lines"] + [
        f"  body-{i:03d}" for i in range(19)]


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
