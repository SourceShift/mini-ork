"""One poll pass, and the loop that repeats it — intake tier A.

The simple tier, and it is simple for a structural reason rather than a
convenient one: polling is **outbound HTTPS**, so it needs no public URL, no
TLS, no reverse proxy and no server. ``mini-ork serve`` is not involved at all.
That is what makes this the tier that works on a laptop, and what the webhook
tier (``intake/webhooks.py``) trades away for freshness.

A pass is three steps, in this order because each one constrains the next:

1. **Sync** — ``pm.store.sync`` pulls from the source into the mirror. It fails
   loudly and records the failure in ``pm_sync``, so a stale board is a *visible*
   stale board rather than an empty column.
2. **Filter the mirror** — candidates come from ``pm.store.query_rows``, the
   open work in the mirror, and the trigger rules run against those rows. This
   is the load-bearing choice: the rules read only ``state``/``labels``/
   ``assignee``, all of which a mirror row already carries, so filtering a
   hundred-ticket board costs **zero** extra requests. Only a ticket that is
   about to be dispatched is read in full.
3. **Dispatch** — one :func:`~mini_ork.intake.dispatch.dispatch_issue` per
   survivor, which is where the ledger row is written.

A ticket is therefore only ever dispatched after it exists in the mirror, which
is also what puts it on the board: the two features read the same rows.

**``max_runs``** bounds a pass, because the first poll against a populated board
is exactly the moment a mistake is most expensive. It counts *dispatches*, not
launches, so a pass that only steers live runs still stops; a ticket already in
the ledger does not consume the budget, since it was never going to act.

**A sync failure does not kill the loop.** :func:`poll_once` raises — a one-shot
``mini-ork intake once`` should exit non-zero and say why — but
:func:`poll_loop` catches it per pass and carries on to the next interval. A
daemon that dies on the first flaky network is a daemon nobody leaves running.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from mini_ork.pm import config as pm_config
from mini_ork.pm import store as pm_store
from mini_ork.pm.model import CLOSED_STATES, OPEN_STATES, Issue, IssueState
from mini_ork.pm.provider import Provider, ProviderError, Query
from mini_ork.threads import store as thread_store

from mini_ork.intake.dispatch import dispatch_issue, delivery_id_for
from mini_ork.intake.trigger import Trigger, matches_trigger

#: One page per pass. The adapters clamp their own ceiling (GitHub's is 100);
#: a source with more open work than this shows its most recently updated page
#: until paging lands here. Stated rather than discovered.
SYNC_LIMIT = 100

#: How much of the mirror a pass looks at. Reading a row is free; the ceiling
#: exists so a pathological mirror cannot make one pass unbounded.
CANDIDATE_LIMIT = 200

#: Every state a ticket can be *worked* in, plus ``done`` — everything but
#: ``canceled``.
#:
#: The sync must ask for closed work as well as open, and this matters more than
#: it looks: with an open-only sync, a ticket closed between two polls keeps its
#: stale ``todo`` row in the mirror and the trigger starts work on a ticket that
#: is already finished. Asking for ``done`` too lets the close land, and the
#: candidate query then drops the ticket on its own.
#:
#: ``canceled`` is left out for two reasons. Canceled work is not work — intake
#: wants it neither in the mirror nor, ever, started. And the state is not a
#: status in every source: ``local`` spells it as the ``archived_at`` flag, so
#: asking for it alongside a status filter composes into a clause that returns
#: only archived rows. See the note on ``_where`` in ``pm/providers/local.py``.
_EVERY_STATE: tuple[IssueState, ...] = tuple(
    sorted((OPEN_STATES | CLOSED_STATES) - {IssueState.CANCELED}, key=str)
)


@dataclass(frozen=True, slots=True)
class Outcome:
    """What one mirror row's arrival did — or why it was passed over."""

    issue_uid: str
    action: str
    reason: str
    thread_id: str | None = None
    run_id: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "issue_uid": self.issue_uid,
            "action": self.action,
            "reason": self.reason,
            "thread_id": self.thread_id,
            "run_id": self.run_id,
        }


@dataclass(frozen=True, slots=True)
class PollReport:
    """One pass, in full. ``error`` is set only by the loop's catch."""

    source: str
    mirrored: int = 0
    considered: int = 0
    outcomes: tuple[Outcome, ...] = ()
    deferred: int = 0
    dry_run: bool = False
    error: str | None = None

    @property
    def started(self) -> int:
        """Runs begun — the number that grows the machine's load."""
        return sum(1 for outcome in self.outcomes if outcome.action in ("launched", "continued"))

    @property
    def previewed(self) -> int:
        """Tickets a dry run would have started. Always 0 on a real pass."""
        return sum(1 for outcome in self.outcomes if outcome.action == "would-start")

    @property
    def changed(self) -> bool:
        """Whether a person would want to be told about this pass.

        Two exclusions, for two different reasons. ``skipped`` is the normal
        state of a board — most tickets do not match a rule, and a daemon that
        announced them every interval would be unreadable. ``duplicate`` is
        quieter still: it means a ticket was edited again, which on a busy
        board happens constantly.

        A dry run's ``would-start`` **does** count. "This would have started
        two runs" is the entire product of ``--dry-run``, so reporting it as
        "nothing to start" would be exactly backwards.
        """
        return bool(self.error) or any(
            outcome.action not in ("skipped", "duplicate") for outcome in self.outcomes
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "source": self.source,
            "mirrored": self.mirrored,
            "considered": self.considered,
            "deferred": self.deferred,
            "dry_run": self.dry_run,
            "started": self.started,
            "error": self.error,
            "outcomes": [outcome.to_dict() for outcome in self.outcomes],
        }


def trigger_for(source_id: str, *, config: pm_config.PmConfig | None = None) -> Trigger:
    """A source's rules, or the inert default when it declares none.

    A source id that appears in no config is not an error here: the registry
    already declined to build it and ``intake status`` reports why. A *malformed*
    config propagates, and so does a ``trigger_states`` naming something that is
    not a state — those are rules the author believes are in force, and the one
    direction this must never fail is "silently wider".
    """
    configuration = config if config is not None else pm_config.load_config()
    declared = configuration.sources.get(source_id)
    if declared is None:
        return Trigger()
    return Trigger.from_settings(declared.settings)


def poll_once(
    home: Path | str,
    source: str,
    *,
    db: str | None = None,
    dry_run: bool = False,
    max_runs: int = 1,
    provider: Provider | None = None,
    trigger: Trigger | None = None,
    query: Query | None = None,
) -> PollReport:
    """Sync one source and dispatch what its rules select. Raises on a failed sync.

    ``provider`` and ``trigger`` are injectable so a test drives the whole path
    against a stub source with no registry, no network and no ``config/pm.yaml``.

    A dry run decides from the mirror alone and does not re-read the ticket, so
    it is *nearly* what the real pass would do rather than exactly it: a ticket
    closed since the sync reads as a candidate here and is passed over for real
    (see the ``is_open`` check below). The difference is one network call per
    dispatched ticket, which a preview exists to avoid, and it can only ever
    make the preview say "would start" where the real pass says "already done".
    """
    adapter = provider if provider is not None else _adapter(source)
    source_id = adapter.id
    rules = trigger if trigger is not None else trigger_for(source_id)

    mirrored = pm_store.sync(adapter, query or Query(limit=SYNC_LIMIT, states=_EVERY_STATE), db)
    rows = pm_store.query_rows(
        provider=source_id,
        states=tuple(sorted(OPEN_STATES, key=str)),
        limit=CANDIDATE_LIMIT,
        db=db,
    )

    outcomes: list[Outcome] = []
    acted = 0
    deferred = 0
    for row in rows:
        matched, reason = matches_trigger(row, rules)
        if not matched:
            outcomes.append(Outcome(row.uid, "skipped", reason))
            continue

        delivery_id = delivery_id_for(row)
        seen = thread_store.intake_event(source_id, delivery_id, db=db)
        if seen is not None:
            # Folded, not re-dispatched: the ticket's text changed (or a webhook
            # replayed) and the ledger remembers what was done with this arrival.
            outcomes.append(
                Outcome(
                    row.uid,
                    "duplicate",
                    f"already {seen.action or 'seen'} on this arrival",
                    thread_id=seen.thread_id,
                )
            )
            continue

        if acted >= max_runs:
            deferred += 1
            continue

        if dry_run:
            acted += 1
            outcomes.append(Outcome(row.uid, "would-start", reason))
            continue

        issue = _read(adapter, row.uid)
        if isinstance(issue, str):
            outcomes.append(Outcome(row.uid, "failed", issue))
            continue
        if not issue.is_open:
            # The mirror said open; the source says otherwise. A row can be
            # stale — synced before the ticket closed, or a state the sync's
            # filter could not see — and the source is the authority on whether
            # a ticket is still work. Checked here, on the one read that was
            # already being made, so it costs nothing.
            outcomes.append(
                Outcome(row.uid, "skipped", f"the source now reports it as {issue.state}")
            )
            continue

        acted += 1
        dispatch = dispatch_issue(home, adapter, issue, recipe=rules.recipe, db=db)
        outcomes.append(
            Outcome(
                row.uid,
                dispatch.action,
                dispatch.detail or f"{issue.uid} → {dispatch.thread_id}",
                thread_id=dispatch.thread_id,
                run_id=dispatch.run_id,
            )
        )

    return PollReport(
        source=source_id,
        mirrored=mirrored,
        considered=len(rows),
        outcomes=tuple(outcomes),
        deferred=deferred,
        dry_run=dry_run,
    )


def poll_loop(
    home: Path | str,
    source: str,
    *,
    interval: float,
    db: str | None = None,
    dry_run: bool = False,
    max_runs: int = 1,
    provider: Provider | None = None,
    trigger: Trigger | None = None,
    passes: int | None = None,
    sleep: Callable[[float], None] = time.sleep,
    on_pass: Callable[[PollReport], bool | None] | None = None,
) -> list[PollReport]:
    """``poll_once`` on a timer. Returns every pass, in order.

    ``passes`` bounds the loop — ``None`` runs until interrupted, which is what
    the CLI does and what a test would otherwise have to fake with an exception.

    ``sleep`` is a parameter because a test that waits five minutes is a test
    nobody runs. ``on_pass`` returns ``False`` to stop, so a caller can be
    finished without reaching into the loop.
    """
    reports: list[PollReport] = []
    completed = 0
    while passes is None or completed < passes:
        try:
            report = poll_once(
                home, source, db=db, dry_run=dry_run, max_runs=max_runs,
                provider=provider, trigger=trigger,
            )
        except (ProviderError, pm_config.PmConfigError) as exc:
            # One bad pass is not a reason to stop polling: a network gone away
            # for a minute, a token rotated. The reason is reported and the next
            # interval tries again.
            report = PollReport(source=source, error=str(exc))
        reports.append(report)
        completed += 1
        if on_pass is not None and on_pass(report) is False:
            break
        if passes is not None and completed >= passes:
            break
        sleep(interval)
    return reports


@dataclass(frozen=True, slots=True)
class SourceStatus:
    """One source as ``intake status`` shows it — the answer to "why nothing?"."""

    source: str
    registered: bool = False
    enabled: bool = False
    trigger: Trigger = field(default_factory=Trigger)
    skipped_reason: str | None = None
    synced_at: str | None = None
    mirrored: int = 0
    sync_error: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "source": self.source,
            "registered": self.registered,
            "enabled": self.enabled,
            "trigger": self.trigger.to_dict(),
            "skipped_reason": self.skipped_reason,
            "synced_at": self.synced_at,
            "mirrored": self.mirrored,
            "sync_error": self.sync_error,
        }


def source_statuses(
    db: str | None = None, *, config: pm_config.PmConfig | None = None
) -> list[SourceStatus]:
    """Every source this build knows about, declared or not.

    The union of four lists that can disagree — what the config declares, what
    the config enables, what the registry built, and what it skipped — because
    the question a person asks is "why is my source not doing anything", and
    each of the four is a different answer.
    """
    from mini_ork.pm import registry
    from mini_ork.pm.providers import skipped_sources

    configuration = config if config is not None else pm_config.load_config()
    skipped = dict(skipped_sources())
    registered = set(registry.provider_ids())
    mirrors = {row["provider"]: row for row in pm_store.mirror_state(db=db)}

    statuses: list[SourceStatus] = []
    for source_id in sorted(set(configuration.sources) | registered | set(skipped)):
        declared = configuration.sources.get(source_id)
        mirror = mirrors.get(source_id) or {}
        statuses.append(
            SourceStatus(
                source=source_id,
                registered=source_id in registered,
                enabled=bool(declared is not None and declared.enabled),
                trigger=Trigger.from_settings(declared.settings) if declared else Trigger(),
                skipped_reason=skipped.get(source_id),
                synced_at=mirror.get("synced_at"),
                mirrored=int(mirror.get("n") or 0),
                sync_error=mirror.get("error"),
            )
        )
    return statuses


def _adapter(source: str) -> Provider:
    """The registered adapter for a source id, with the registry's own error."""
    from mini_ork.pm import registry

    return registry.get_provider(source)


def _read(adapter: Provider, uid: str) -> Issue | str:
    """The full issue for a mirror row, or the reason it could not be read.

    Returns a ``str`` where a caller would rather have an :class:`Issue` — an
    explicit, if unlovely, way to carry "this row failed" through one expression
    without either raising out of the whole pass or losing the reason.
    """
    try:
        return adapter.get(uid)
    except (ProviderError, KeyError) as exc:
        return f"could not read {uid}: {exc}"
