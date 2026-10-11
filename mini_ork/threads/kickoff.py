"""Markdown for a thread's next kickoff — pure, so it is fully testable.

When a run ends and the user follows up, the next attempt is a *new* run in the
same thread (that is what replaced the agent-server's 409). It cannot inherit
the previous run's context, because a run is a fresh detached process, so the
carry-over has to travel in the kickoff text itself: what the thread has said so
far, and how the previous run ended.

This module composes that text and nothing else — no database, no disk, no
clock. The caller (:mod:`mini_ork.threads.service`) reads the transcript and the
prior run's outcome and hands them here as plain values. Keeping the composer
pure is what lets every branch of the composition be asserted without launching
anything, and it keeps a later LLM summarizer a drop-in behind the same
signature.

Bounding is deliberate. A thread can grow without limit; a kickoff cannot. The
task instruction is never truncated — it is the reason the attempt exists — so
the *history* is what gives way, newest turn kept first, with an explicit note
naming how many earlier turns were dropped rather than a silent cut. The newest
turn is kept even when the budget is already spent, so a follow-up is never
contextless; ``max_total_chars`` is therefore a target the head, the task and
one capped turn may together exceed, not a hard ceiling.
"""

from __future__ import annotations

from dataclasses import dataclass

SEP = "\n\n"

#: How many of the most recent turns an unconfigured kickoff carries.
DEFAULT_MAX_HISTORY = 12
#: Per-turn character cap, so one pasted log cannot crowd out the task.
DEFAULT_MAX_TURN_CHARS = 2000
#: Whole-kickoff cap; the task and a little history always fit under it.
DEFAULT_MAX_TOTAL_CHARS = 24000


@dataclass(frozen=True, slots=True)
class Turn:
    """One message to carry forward. ``role`` is a plain string, not the store's
    enum, so the composer stays usable without importing the model."""

    role: str
    text: str


@dataclass(frozen=True, slots=True)
class PriorRun:
    """How the previous attempt ended — the one thing a fresh run cannot see."""

    attempt: int
    run_id: str
    status: str
    verdict: str | None = None
    artifacts: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class Kickoff:
    """Everything the composer needs, gathered by the caller.

    ``ticket`` is a pre-rendered block (the work-intake provenance header plus
    the issue body); this module does not know what a ticket is. ``history`` is
    oldest-first. ``prior`` is ``None`` on the first attempt.
    """

    title: str
    task: str
    ticket: str | None = None
    history: tuple[Turn, ...] = ()
    prior: PriorRun | None = None
    max_history: int = DEFAULT_MAX_HISTORY
    max_turn_chars: int = DEFAULT_MAX_TURN_CHARS
    max_total_chars: int = DEFAULT_MAX_TOTAL_CHARS


#: A role's label in the transcript. ``file`` is an attachment — no prose.
_LABELS = {
    "user": "User",
    "assistant": "Agent",
    "system": "System",
    "file": "Attachment",
}


def compose(spec: Kickoff) -> str:
    """The kickoff markdown for one attempt. Deterministic and bounded."""
    title = (spec.title or "").strip()
    task = (spec.task or "").strip()

    head_blocks: list[str] = []
    if title:
        head_blocks.append(f"# {title}")
    if spec.ticket and spec.ticket.strip():
        head_blocks.append("## Ticket" + SEP + spec.ticket.strip())
    if spec.prior is not None:
        head_blocks.append(_prior_section(spec.prior))

    task_block = "## Your task" + SEP + task
    head = SEP.join(head_blocks)

    # The task and the head are fixed; the history gets whatever is left.
    reserved = len(head) + (len(SEP) if head else 0) + len(SEP) + len(task_block)
    budget = max(0, spec.max_total_chars - reserved)
    history_block = _history_section(
        spec.history, budget, spec.max_history, spec.max_turn_chars
    )

    blocks = [block for block in (*head_blocks, history_block, task_block) if block]
    return SEP.join(blocks) + "\n"


def _prior_section(prior: PriorRun) -> str:
    lines = [
        "## Prior run",
        "",
        f"- Attempt: {prior.attempt}",
        f"- Run id: `{prior.run_id}`",
        f"- Outcome: {prior.status}",
    ]
    if prior.verdict and prior.verdict.strip():
        lines.append(f"- Verdict: {prior.verdict.strip()}")
    if prior.artifacts:
        lines.append("- Artifacts:")
        lines.extend(f"  - `{path}`" for path in prior.artifacts)
    return "\n".join(lines)


def _history_section(
    history: tuple[Turn, ...], budget: int, max_history: int, max_turn_chars: int
) -> str:
    if not history:
        return ""
    header = "## Thread so far"
    turns = [turn for turn in history if (turn.text or "").strip()]
    if not turns:
        return ""

    kept: list[str] = []
    used = len(header)
    # Newest first, so the most recent exchange is what survives a tight budget.
    for turn in reversed(turns):
        if len(kept) >= max(1, max_history):
            break
        rendered = _render_turn(turn, max_turn_chars)
        if used + len(rendered) + len(SEP) > budget and kept:
            break
        kept.append(rendered)
        used += len(rendered) + len(SEP)

    kept.reverse()
    omitted = len(turns) - len(kept)
    body = SEP.join(kept)
    if omitted > 0:
        body = f"_({omitted} earlier turn(s) omitted.)_" + SEP + body
    return header + SEP + body


def _render_turn(turn: Turn, max_turn_chars: int) -> str:
    label = _LABELS.get(str(turn.role), str(turn.role).title())
    text = (turn.text or "").strip()
    if len(text) > max_turn_chars:
        text = text[:max_turn_chars].rstrip() + " …"
    return f"**{label}:** {text}"
