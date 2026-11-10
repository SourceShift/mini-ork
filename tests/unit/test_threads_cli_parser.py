"""The ``mini-ork threads`` parser tree is the command's definition.

``main`` dispatches through :func:`mini_ork.threads.cli.build_parser`, so these
tests are about one property: nothing else may describe the same command
differently. The verb list, the handler table and the usage page all have to
agree, and the argv the IDE emits has to be the argv the parser accepts.

Why it matters beyond tidiness: ``tests/unit/test_ide_pages_actions.py``
resolves a page action's parser by the module a subcommand dispatches into. That
check was blind to ``threads`` — the one subcommand with no ``build_parser`` —
so a thread page that emitted an argv its CLI rejects would have shipped green.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from mini_ork.threads import cli  # noqa: E402

#: The smallest argv that reaches each verb's handler. A verb missing here is a
#: verb these tests never exercise, so the dict is asserted against ``VERBS``.
MINIMAL = {
    "list": [],
    "show": ["th-1"],
    "send": ["th-1"],
    "continue": ["th-1"],
    "attach": ["th-1"],
    "serve": [],
}


def test_the_verb_list_the_handlers_and_the_usage_agree() -> None:
    """Three descriptions of one command; a fourth must not appear in one only."""
    assert set(cli.VERBS) == set(MINIMAL), "a test-side verb list drifted"
    assert set(cli._HANDLERS) == set(cli.VERBS)
    for verb in cli.VERBS:
        assert f"mini-ork threads {verb}" in cli._USAGE, (
            f"{verb!r} is a verb but the usage page never shows it"
        )


@pytest.mark.parametrize("verb", cli.VERBS)
def test_every_verb_parses_its_minimal_argv(verb: str) -> None:
    args = cli.build_parser().parse_args([verb, *MINIMAL[verb]])
    assert args.verb == verb


@pytest.mark.parametrize("verb", cli.VERBS)
def test_every_verb_accepts_the_home_the_ide_appends(verb: str) -> None:
    """``board.rs run_action`` appends ``--home`` to every action it runs."""
    args = cli.build_parser().parse_args(
        [verb, *MINIMAL[verb], "--home", "/tmp/some-home"]
    )
    assert args.home == "/tmp/some-home"


def test_the_ide_composer_and_picker_argv_parse() -> None:
    """The two actions the thread page emits (``ide_pages/thread.py``).

    A composer's ``cli`` is a prefix the typed text is appended to, so the
    parser sees the message as ``--text <value>``; the picker appends one path
    per selected file. Both are complete argv by the time the IDE runs them.
    """
    parser = cli.build_parser()
    send = parser.parse_args(
        ["send", "th-1", "--text", "steer this", "--home", "/tmp/h"]
    )
    assert (send.verb, send.id, send.text) == ("send", "th-1", "steer this")

    attach = parser.parse_args(["attach", "th-1", "/tmp/a.pdf", "/tmp/b.png"])
    assert attach.paths == ["/tmp/a.pdf", "/tmp/b.png"]


def test_bare_invocation_is_a_usage_error_not_a_traceback() -> None:
    with pytest.raises(SystemExit) as exc:
        cli.build_parser().parse_args([])
    assert exc.value.code == 2


def test_an_unknown_verb_is_rejected_by_the_parser_too() -> None:
    with pytest.raises(SystemExit) as exc:
        cli.build_parser().parse_args(["steer", "th-1"])
    assert exc.value.code == 2


def test_a_missing_text_is_still_the_handler_s_usage_error() -> None:
    """``--text`` stays optional in the parser: ``send th-1`` with no text is a
    usage error the handler writes (rc 2), not an argparse rejection, so
    ``--text -`` and stdin share one path."""
    args = cli.build_parser().parse_args(["send", "th-1"])
    assert args.text is None


def test_the_ide_action_check_can_resolve_this_parser() -> None:
    """Mirror ``test_ide_pages_actions._parser_for_action``'s lookup.

    That test imports the module from ``_NATIVE_MODULE_SUBS`` and calls its
    ``build_parser``. This is the step it could not take for ``threads``.
    """
    from mini_ork.cli import main as cli_main

    module_name = cli_main._NATIVE_MODULE_SUBS["threads"]
    module = __import__(module_name, fromlist=["build_parser"])
    assert callable(getattr(module, "build_parser", None))
