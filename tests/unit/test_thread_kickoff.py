"""Tests for ``mini_ork.threads.kickoff`` — pure markdown composition.

No database, no disk, no run: the composer takes plain values and returns a
string, so every branch is asserted directly. What is pinned:

* a first attempt carries the ticket verbatim and no history sections
* a continuation carries the transcript and the prior run's outcome — the only
  way a fresh, detached run learns what came before
* the task is never truncated, even when it is the whole budget
* the newest turn survives a tight budget, with a note for what was dropped
* per-turn truncation stops one pasted log from consuming the kickoff
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from mini_ork.threads.kickoff import (  # noqa: E402
    Kickoff,
    PriorRun,
    Turn,
    compose,
)

TICKET = "**github:o/r#12** — Add a retry budget\n\nState: Todo\n\nBack off on 429."


def test_a_first_attempt_carries_the_ticket_and_nothing_else():
    text = compose(
        Kickoff(title="Add a retry budget", task="Implement it.", ticket=TICKET)
    )
    assert text.startswith("# Add a retry budget")
    assert "## Ticket" in text
    assert TICKET in text
    assert text.rstrip().endswith("Implement it.")
    assert "## Your task" in text
    # No prior run, no transcript — the sections must be absent, not empty.
    assert "## Thread so far" not in text
    assert "## Prior run" not in text


def test_a_continuation_carries_the_transcript_and_the_prior_outcome():
    text = compose(
        Kickoff(
            title="Add a retry budget",
            task="Also cover the 503 case.",
            history=(
                Turn(role="user", text="Implement it."),
                Turn(role="assistant", text="Added exponential backoff on 429."),
            ),
            prior=PriorRun(
                attempt=1,
                run_id="t1",
                status="finished",
                verdict="tests passed",
                artifacts=("src/retry.py", "tests/test_retry.py"),
            ),
        )
    )
    assert "## Thread so far" in text
    assert "**User:** Implement it." in text
    assert "**Agent:** Added exponential backoff on 429." in text
    assert "## Prior run" in text
    assert "- Outcome: finished" in text
    assert "- Verdict: tests passed" in text
    assert "`src/retry.py`" in text
    # The new instruction is last, so the run reads the context then the ask.
    assert text.rstrip().endswith("Also cover the 503 case.")


def test_the_task_is_never_truncated_even_when_it_is_the_whole_budget():
    long_task = "Do the thing. " * 400          # ~5.6k chars, over the cap
    text = compose(
        Kickoff(
            title="Big",
            task=long_task,
            history=(Turn(role="user", text="earlier"),),
            prior=None,
            max_total_chars=200,                 # a budget far below the task
        )
    )
    assert long_task.strip() in text            # the ask survives intact
    # The newest turn is still kept, so the follow-up is not contextless.
    assert "earlier" in text


def test_history_is_bounded_to_max_history_keeping_the_newest():
    history = tuple(Turn(role="user", text=f"turn {i}") for i in range(20))
    text = compose(Kickoff(title="T", task="next", history=history, max_history=3))
    assert "turn 19" in text
    assert "turn 17" in text
    assert "turn 16" not in text                 # older than the window
    assert "earlier turn(s) omitted" in text
    assert "turn 0" not in text


def test_one_pasted_log_cannot_crowd_out_the_rest():
    text = compose(
        Kickoff(
            title="T",
            task="next",
            history=(
                Turn(role="user", text="x" * 10_000),
                Turn(role="user", text="short one"),
            ),
            max_turn_chars=50,
        )
    )
    # The long turn is capped; the short one is untouched.
    assert "x" * 10_000 not in text
    assert " …" in text
    assert "short one" in text


def test_a_file_turn_renders_as_an_attachment():
    text = compose(
        Kickoff(
            title="T",
            task="next",
            history=(Turn(role="file", text="deck.pptx"),),
        )
    )
    assert "**Attachment:** deck.pptx" in text


def test_no_title_or_ticket_leaves_no_empty_heading():
    text = compose(Kickoff(title="", task="just do it"))
    assert text == "## Your task\n\njust do it\n"


def test_composition_is_deterministic():
    spec = Kickoff(
        title="T",
        task="t",
        history=(Turn(role="user", text="a"), Turn(role="assistant", text="b")),
        prior=PriorRun(attempt=1, run_id="t1", status="finished"),
    )
    assert compose(spec) == compose(spec)
