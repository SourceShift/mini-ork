"""Operator settings — the env knobs that are *not* cost features.

:mod:`mini_ork.features.registry` covers features that are turned on or off and
scaled by a cost multiplier. This module covers everything else an operator
tunes: hard caps, timeouts, verification modes, retention. They have no cost
model, so they do not belong in the registry (whose ``default`` drives a cost
multiplier and whose tier grouping would mislabel them), but they must still be
*declared* somewhere rather than hand-listed in the IDE page.

Each entry names the module that reads it (``read_at``) so the page can show
where a setting is honoured, and so a test can assert the knob is still read
somewhere — a knob the engine ignores is worse than no knob at all.

Values are applied from ``.mini-ork/config/features.yaml``'s ``env:`` section via
``os.environ.setdefault`` (see :mod:`mini_ork.features.store`), so a real
environment variable always wins.
"""

from __future__ import annotations

from dataclasses import dataclass

#: Section headings the page groups by, in display order.
GROUPS: tuple[str, ...] = ("Cost & limits", "Verification", "Context & memory", "Git & PR")


@dataclass(frozen=True)
class Setting:
    """One operator knob: what it is, what it does, and where it is read."""

    name: str  # the env var
    label: str
    group: str  # one of GROUPS
    effect: str  # one sentence
    default: str  # the code default when unset — a hint, never enforced here
    kind: str  # money | seconds | count | onoff | enum | text
    read_at: str  # repo-relative module that reads it (checked by a test)
    tokens: tuple[str, ...] = ()  # accepted values when kind == "enum"
    note: str = ""  # a gotcha worth surfacing


ENGINE_SETTINGS: tuple[Setting, ...] = (
    # ── Cost & limits ────────────────────────────────────────────────────────
    Setting(
        name="MO_DAILY_BUDGET_USD",
        label="Daily budget cap",
        group="Cost & limits",
        effect="Hard cap on spend over a rolling 24 hours; a run that would cross it is refused.",
        default="50",
        kind="money",
        read_at="mini_ork/scheduler.py",
        note="The only cap that is actually enforced. See Lanes & cost for spend to date.",
    ),
    Setting(
        name="MO_PAUSE_EVERY_USD",
        label="Pause every",
        group="Cost & limits",
        effect="Ask for confirmation each time cumulative spend crosses this step.",
        default="25",
        kind="money",
        read_at="mini_ork/dispatch/cost_pause.py",
    ),
    Setting(
        name="MO_SCHED_MAX_PARALLEL",
        label="Max parallel runs",
        group="Cost & limits",
        effect="How many runs the scheduler may have in flight at once.",
        default="3",
        kind="count",
        read_at="mini_ork/scheduler.py",
    ),
    Setting(
        name="MO_NODE_TIMEOUT_S",
        label="Node timeout (s)",
        group="Cost & limits",
        effect="Abort a single node that runs longer than this.",
        default="1500",
        kind="seconds",
        read_at="mini_ork/dispatch/llm_dispatch.py",
    ),
    Setting(
        name="MO_KILL_REAP_TIMEOUT_S",
        label="Kill reap timeout (s)",
        group="Cost & limits",
        effect="Stop waiting for a killed lane process group to be reaped after this many seconds.",
        default="30",
        kind="seconds",
        read_at="mini_ork/dispatch/core.py",
    ),
    # ── Verification ─────────────────────────────────────────────────────────
    Setting(
        name="MO_REVISE_ROUNDS",
        label="Revise rounds",
        group="Verification",
        effect="How many times a failing node may be revised before the run gives up; 0 disables the loop.",
        default="2",
        kind="count",
        read_at="mini_ork/cli/execute.py",
    ),
    Setting(
        name="MO_KICKOFF_GUARD",
        label="Kickoff guard",
        group="Verification",
        effect="Evaluate a kickoff before the run starts.",
        default="shadow",
        kind="enum",
        tokens=("off", "shadow", "on"),
        read_at="mini_ork/verify/kickoff_guard.py",
    ),
    Setting(
        name="MO_PROBE_VALIDITY",
        label="Probe validity",
        group="Verification",
        effect="Require a probe's measurements to be valid before anything is committed.",
        default="off",
        kind="enum",
        tokens=("off", "shadow", "enforce"),
        read_at="mini_ork/verify/probe_validity.py",
        note="Accepts 1 for enforce.",
    ),
    Setting(
        name="MO_EVIDENCE_LEDGER",
        label="Evidence ledger",
        group="Verification",
        effect="Record a per-claim evidence ledger for each run.",
        default="off",
        kind="enum",
        tokens=("off", "shadow", "enforce"),
        read_at="mini_ork/verify/evidence_ledger.py",
        note="Accepts 1 for enforce.",
    ),
    Setting(
        name="MO_APPLY_ENABLED",
        label="Apply patch",
        group="Verification",
        effect="Allow a run's changes to be applied back to the project.",
        default="0",
        kind="onoff",
        read_at="mini_ork/cli/apply.py",
    ),
    # ── Context & memory ─────────────────────────────────────────────────────
    Setting(
        name="MO_CONTEXT_V2",
        label="Context v2",
        group="Context & memory",
        effect="Use the v2 context builder, which records what it built before switching over.",
        default="shadow",
        kind="enum",
        tokens=("off", "shadow", "on"),
        read_at="mini_ork/context_v2.py",
    ),
    Setting(
        name="MO_TRAJECTORY_TTL_DAYS",
        label="Trajectory retention (days)",
        group="Context & memory",
        effect="How long recorded run trajectories are kept before pruning.",
        default="30",
        kind="count",
        read_at="mini_ork/dispatch/retention.py",
    ),
    # ── Git & PR ─────────────────────────────────────────────────────────────
    Setting(
        name="MO_AUTO_MERGE",
        label="Auto-merge PR",
        group="Git & PR",
        effect="Merge a run's pull request once its gates pass.",
        default="0",
        kind="onoff",
        read_at="mini_ork/vcs/auto_merge_pr.py",
        note="Read with a different default (1) by the recovery path — set it explicitly if you rely on it.",
    ),
    Setting(
        name="MO_WORKSPACE_MODE",
        label="Workspace mode",
        group="Git & PR",
        effect="Where a run does its work: an isolated git worktree, or the checkout itself.",
        default="worktree",
        kind="enum",
        tokens=("worktree", "in-place"),
        read_at="mini_ork/mcp_context/server.py",
    ),
)


def settings_for(group: str | None = None) -> list[Setting]:
    """Every setting, or just those in ``group``."""
    if group is None:
        return list(ENGINE_SETTINGS)
    return [s for s in ENGINE_SETTINGS if s.group == group]
