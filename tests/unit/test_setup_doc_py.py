"""SETUP.md is a protocol an agent can follow — so its rules are checkable.

A setup guide written "to a coding agent" is executable text: the agent will do
what it says, including the mistakes. The three ways this one goes wrong are all
mechanical, which is why they are tests rather than prose:

* it names a command that does not exist (the reader believes it, runs it, gets
  a usage error, and improvises from there);
* it claims an attention budget it then exceeds, so "five minutes" is a lie and
  the operator stops answering;
* it lets a secret into the conversation.

The last one is checked structurally: the only credential step must be one the
operator runs, so the doc may not hand the agent a way to feed a key through a
tool call -- including the `--from-stdin` the command really does accept.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from mini_ork.cli.main import SUBCOMMAND_REGISTRY  # noqa: E402
from mini_ork.cli.onboard import _USAGE as ONBOARD_USAGE  # noqa: E402

SETUP = REPO / "SETUP.md"

_CODE_SPAN_RE = re.compile(r"`mini-ork ([a-z][a-z0-9-]{1,24})\b")
_SHELL_LINE_RE = re.compile(r"(?m)^\s*\$?\s*mini-ork ([a-z][a-z0-9-]{1,24})\b")
#: An onboard invocation with the flags a reader would copy.
_ONBOARD_LINE_RE = re.compile(r"mini-ork onboard([^\n`]*)")
_FLAG_RE = re.compile(r"--[a-z][a-z0-9-]*")


def _text() -> str:
    return SETUP.read_text(encoding="utf-8")


def test_the_file_is_at_the_root_where_a_reader_looks_first():
    assert SETUP.is_file()


def test_every_cited_command_exists():
    verbs = set(_CODE_SPAN_RE.findall(_text())) | set(_SHELL_LINE_RE.findall(_text()))
    unknown = sorted(v for v in verbs if v not in SUBCOMMAND_REGISTRY)
    assert not unknown, unknown


def test_every_onboard_flag_it_cites_is_a_real_flag():
    """The doc must mirror the wizard, or the two disagree and the reader
    follows whichever they read first."""
    cited: set[str] = set()
    for line in _text().splitlines():
        for tail in _ONBOARD_LINE_RE.findall(line):
            cited.update(_FLAG_RE.findall(tail))
    unknown = sorted(f for f in cited if f not in ONBOARD_USAGE)
    assert not unknown, unknown
    assert cited, "the guide stopped showing an onboard invocation"


def test_it_states_an_attention_budget_and_keeps_it():
    text = _text()
    cap = re.search(r"\|\s*Questions\s*\|\s*≤\s*(\d+)", text)
    assert cap, "no stated question cap"
    limit = int(cap.group(1))
    section = text.split("## The questions", 1)[1].split("## Secrets", 1)[0]
    asked = re.findall(r"(?m)^\d+\.\s+\*\*", section)
    assert len(asked) <= limit, (len(asked), limit)
    assert limit <= 9, limit  # beyond nine, a person stops answering


def test_it_fixes_plan_and_confirmation_count():
    text = _text()
    assert "exactly 1, after all answers" in text
    assert "exactly 1, before you execute" in text


def test_the_secret_rule_has_no_escape_hatch():
    text = _text()
    assert "Never ask for an API key" in text
    # `providers configure` accepts --from-stdin; the doc must name it and still
    # forbid it, otherwise a reader who finds the flag concludes the rule is out
    # of date.
    assert "--from-stdin" in text
    assert "lands in\nthe transcript too" in text
    # The credential step is the operator's, in their own terminal.
    assert "their own terminal" in text


def test_it_ends_with_a_handover_and_one_first_command():
    text = _text()
    tail = text.split("## Handover", 1)
    assert len(tail) == 2, "no handover section"
    rules = re.findall(r"(?m)^\d+\.\s+\*\*", tail[1])
    assert len(rules) == 3, rules
    assert tail[1].rstrip().endswith("`mini-ork doctor`")


def test_the_readme_points_at_it():
    readme = (REPO / "README.md").read_text(encoding="utf-8")
    assert "SETUP.md" in readme
