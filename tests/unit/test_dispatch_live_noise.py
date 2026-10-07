"""The live stream keeps the agent's work, not the harness's startup chatter.

The Claude CLI prints an auth-precedence notice and a model-registry warning to
stderr before it does anything. On a gateway lane that is the first — and, until
the stdout envelope lands, the *only* — thing a live view of the lane has to
show, which is what made the node inspector display warnings where the output
belongs. ``_drain_stream`` withholds them from the live sink while leaving the
diagnostic copy intact.
"""
from __future__ import annotations

import io

from mini_ork.dispatch.core import _drain_stream

CONNECTORS_BANNER = ("⚠ claude.ai connectors are disabled because ANTHROPIC_API_KEY or "
                     "another auth source is set and takes precedence over your "
                     "claude.ai login")
UNRECOGNIZED_MODEL = ('[claude-code:unrecognized_model] '
                      '{"model":"deepseek-v4-pro[1m]","query_source":"sdk"}')


class _Spy:
    """Stands in for LiveWriter — records what would have been written."""

    def __init__(self) -> None:
        self.lines: list[str] = []

    def write_line(self, line: str, stream: str = "stdout", *, partial: bool = False) -> None:
        self.lines.append(line.rstrip("\r\n"))


def _drain(text: str, name: str) -> tuple[list[str], list[str]]:
    spy = _Spy()
    sink: list[str] = []
    _drain_stream(io.StringIO(text), spy, name, sink)  # type: ignore[arg-type]
    return spy.lines, sink


def test_startup_noise_is_withheld_from_the_live_view() -> None:
    live, sink = _drain(f"{CONNECTORS_BANNER}\n{UNRECOGNIZED_MODEL}\nEdit calc.py\n", "stderr")
    assert live == ["Edit calc.py"]


def test_every_line_still_reaches_the_diagnostic_sink() -> None:
    # ``_failure_detail`` reads this sink, so a suppressed line must still be here.
    _live, sink = _drain(f"{CONNECTORS_BANNER}\n{UNRECOGNIZED_MODEL}\n", "stderr")
    assert "".join(sink).splitlines() == [CONNECTORS_BANNER, UNRECOGNIZED_MODEL]


def test_filter_is_stderr_only() -> None:
    # An agent's stdout may legitimately quote the banner — it must survive.
    live, _sink = _drain(f"{CONNECTORS_BANNER}\n", "stdout")
    assert live == [CONNECTORS_BANNER]


def test_unrelated_stderr_is_untouched() -> None:
    live, _sink = _drain("warn: retrying after 429\n", "stderr")
    assert live == ["warn: retrying after 429"]
