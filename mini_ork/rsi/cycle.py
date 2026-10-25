"""One RSI cycle: harvest → queue → suggest → implement → validate → decide.

The state machine is deliberately boring. Every stage that spends money, touches
git, or calls a model sits behind one callable in :class:`Runtime`, so this module
decides *what happens next* and nothing else. The three properties that follow
from that are the reason for the shape:

* **Order is a safety property, not a convenience.** Budget, kill switches and
  the lease are all checked before the first paid call; a red probe halts before
  SUGGEST rather than after. A cycle that discovers it cannot afford itself has
  already spent the money.
* **Every exit writes a row.** ``running`` is never left behind: each terminal
  path finishes the ledger row with a status and, where one exists, the reason.
  An operator reading ``rsi-cycle list`` sees why the loop stopped, not a stale
  ``running``.
* **Failures are not exceptions.** A failed gate quarantines, a rejected
  proposal rejects, a held lease returns 0. Only an unusable setup (exit 2) and
  an internal error (exit 1) raise out of :func:`run_cycle`.

Nothing here merges on its own judgement: :func:`decide` reads a
:class:`~mini_ork.rsi.gates.ValidationReport` it did not compute, and the one
policy knob with a dangerous default — ``decide.auto_merge`` — has no default in
:mod:`mini_ork.rsi.config`.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import subprocess
import sys
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from . import learn, ledger
from .candidates import Candidate
from .config import CycleConfig
from .gates import CriticVerdict, GateEvidence, ValidationReport, evaluate
from .probes import ProbeResult
from .queue import QueueConfig, QueuePlan, build_queue
from .select import (
    DEFAULT_ALLOW_PREFIXES,
    Proposal,
    Selection,
    SelectConfig,
    SelectContext,
    select,
)
from .view import ledger_view

__all__ = [
    "CycleFailure",
    "CycleOutcome",
    "CycleRequest",
    "CycleSetupError",
    "Implementation",
    "Runtime",
    "disabled_reason",
    "new_cycle_id",
    "run_cycle",
]

#: Set to anything but ``0``/empty to stop new cycles without touching the DB.
DISABLED_ENV = "MO_RSI_CYCLE_DISABLED"

#: ``timeout``'s exit status when it killed the command.
_TIMEOUT_RC = 124

#: Anchors the child's result sink in its stdout (``mini_ork_result={...}``).
_RESULT_SINK = re.compile(r"mini_ork_result=(\{.*\})\s*$", re.MULTILINE)


# --------------------------------------------------------------------------
# Errors
# --------------------------------------------------------------------------


class CycleSetupError(RuntimeError):
    """The checkout cannot run a cycle at all. The CLI turns this into exit 2."""


class CycleFailure(RuntimeError):
    """A stage broke in a way that is not a verdict. The CLI turns this into exit 1."""


# --------------------------------------------------------------------------
# Values crossing the stage boundaries
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Implementation:
    """What IMPLEMENT produced.

    ``changed_files == ()`` is the "nothing to land" answer — an abstained patch
    or an empty diff — and the cycle treats it as a rejection that still cleans
    its worktree. A worktree with nothing in it is not evidence worth keeping.
    """

    worktree: Any  # mini_ork.rsi.worktree.Worktree; untyped to keep the cycle import-light
    worktree_config: Any = None
    changed_files: tuple[str, ...] = ()
    diff: str = ""
    verdict: Mapping[str, Any] | None = None
    child_run_id: str = ""
    cost_usd: float = 0.0
    detail: str = ""

    @property
    def landed_something(self) -> bool:
        return bool(self.changed_files)


@dataclass(frozen=True, slots=True)
class CycleRequest:
    """Everything a cycle needs that is not a callable."""

    root: Path
    db: Path
    config: CycleConfig
    cycle_id: str = ""
    dry_run: bool = False
    propose_only: bool = False
    proposal: Proposal | None = None
    domains: tuple[str, ...] | None = None
    candidate_id: str | None = None
    max_candidates: int | None = None
    probe_run: bool = False

    def __post_init__(self) -> None:
        if not self.cycle_id:
            object.__setattr__(self, "cycle_id", new_cycle_id())

    @property
    def home(self) -> Path:
        """The state home. The DB's directory is the only definition that holds
        for ``--db``, ``MINI_ORK_DB`` and a repo-local default alike."""
        return Path(self.db).parent


@dataclass(frozen=True, slots=True)
class CycleOutcome:
    cycle_id: str
    status: str
    rc: int = 0
    halt_reason: str = ""
    candidate_id: str = ""
    detail: str = ""
    kickoff: str = ""
    merged_sha: str = ""
    gates: ValidationReport | None = None


def new_cycle_id(now: int | None = None) -> str:
    """``rsi-c<epoch>-<hex>`` — sortable, safe as a worktree slug, unique."""
    stamp = int(time.time()) if now is None else int(now)
    return f"rsi-c{stamp}-{os.urandom(4).hex()}"


# --------------------------------------------------------------------------
# Runtime — every side effect a cycle performs
# --------------------------------------------------------------------------

ProbesFn = Callable[[CycleRequest], tuple[ProbeResult, ...]]
HarvestFn = Callable[[CycleRequest, sqlite3.Connection, int, Sequence[ProbeResult]],
                     Any]  # HarvestReport
ProposeFn = Callable[[CycleRequest, Candidate], "Proposal | None"]
ImplementFn = Callable[[CycleRequest, Candidate, Selection], "Implementation | None"]
MeasureFn = Callable[[CycleRequest, Candidate, Implementation, Selection], GateEvidence]
CriticsFn = Callable[[CycleRequest, Candidate, Implementation, Selection,
                      ValidationReport], tuple[CriticVerdict, ...]]
SpendFn = Callable[[Path], float]
MergeFn = Callable[[CycleRequest, Implementation], Any]  # MergeResult
CleanFn = Callable[[CycleRequest, Implementation], None]
QuarantineFn = Callable[[CycleRequest, Candidate, Implementation, ValidationReport,
                         str], Path | None]


@dataclass(slots=True)
class Runtime:
    """The injectable edge of a cycle. ``None`` means the real implementation.

    Tests replace exactly the callables a scenario needs; a fake child is a fake
    ``implement`` plus a fake ``measure``, and no git or model process is spawned.
    """

    now: Callable[[], int] | None = None
    probes: ProbesFn | None = None
    harvest: HarvestFn | None = None
    propose: ProposeFn | None = None
    implement: ImplementFn | None = None
    measure: MeasureFn | None = None
    critics: CriticsFn | None = None
    spend_24h: SpendFn | None = None
    merge: MergeFn | None = None
    clean: CleanFn | None = None
    quarantine: QuarantineFn | None = None

    def clock(self) -> int:
        return int((self.now or (lambda: int(time.time())))())


# --------------------------------------------------------------------------
# Kill switches and budget
# --------------------------------------------------------------------------


def disabled_reason(req: CycleRequest, runtime: Runtime | None = None) -> str:
    """Why new cycles must not start, or ``""``.

    Three switches, checked before anything else: the environment (a shell-wide
    stop), the DISABLED file (an operator's durable stop), and the reactive
    cost-pause sentinel (a run that already crossed its spend threshold).
    """
    env = os.environ.get(DISABLED_ENV, "")
    if env.strip() not in ("", "0"):
        return f"{DISABLED_ENV}={env.strip()}"
    stop = req.home / "rsi" / "DISABLED"
    if stop.exists():
        # A collapse trip writes its own reason as line 1 (see ``rsi.learn``).
        # Surfacing it here is the whole operator story: the run that stopped
        # says *why* it stopped, not just that a file exists.
        try:
            first = stop.read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            first = ""
        if first.startswith("collapse"):
            return first.splitlines()[0]
        return "rsi/DISABLED file present"
    if not req.config.enabled:
        return "enabled: false"
    try:
        from mini_ork.dispatch import cost_pause

        state = cost_pause.status(req.config.lease.key)
    except Exception:  # noqa: BLE001 - an unreadable pause meter is not a stop
        return ""
    if state.get("paused"):
        return f"cost-pause sentinel at {state.get('sentinel_path')}"
    return ""


def _budget(all_spend: float, cap: float) -> str:
    return "" if all_spend < cap else f"spent ${all_spend:.2f} of the ${cap:.2f} daily cap"


# --------------------------------------------------------------------------
# The cycle
# --------------------------------------------------------------------------


def run_cycle(req: CycleRequest, *, runtime: Runtime | None = None) -> CycleOutcome:
    """Run one cycle. Raises only :class:`CycleSetupError` / :class:`CycleFailure`."""
    rt = runtime or Runtime()

    reason = disabled_reason(req, rt)
    if reason:
        return CycleOutcome(req.cycle_id, "halted", 0, halt_reason=reason,
                            detail=f"cycle not started: {reason}")

    _require_lease_tables(req)

    # --- budget is checked *before* the lease so a halted cycle costs nothing
    spend = float((rt.spend_24h or _default_spend)(req.db))
    over = _budget(spend, req.config.budget.daily_cap_usd)
    if over:
        return CycleOutcome(req.cycle_id, "halted", 0, halt_reason="daily_cap",
                            detail=over)

    token = _acquire(req)
    if token is None:
        held = _current_lease(req)
        if held:
            return CycleOutcome(req.cycle_id, "halted", 0, halt_reason="lease_held",
                                detail="another cycle holds the lease")
        raise CycleFailure("the lease could not be acquired and no lease is recorded — "
                           "the lease store is broken")

    con = None
    try:
        con = ledger.connect(str(req.db))
        if not req.dry_run:
            ledger.start_cycle(con, cycle_id=req.cycle_id, now=rt.clock(),
                               mode=req.config.mode, dry_run=False)
        return _body(req, rt, con, token=token, spend=spend)
    except CycleSetupError:
        raise
    except BaseException as exc:  # noqa: BLE001 - a broken cycle must not stay 'running'
        if con is not None and not req.dry_run:
            try:
                row = ledger.get_cycle(con, req.cycle_id)
                # Only a still-running row is an error. A cycle that already
                # terminal-finished (a merge, a quarantine) must not have its
                # status rewritten because something raised *after* the ledger
                # write — a closed stderr pipe is enough to do it.
                if row is not None and row["status"] == "running":
                    ledger.finish_cycle(con, req.cycle_id, now=rt.clock(),
                                        status="error",
                                        halt_reason=f"{type(exc).__name__}: {exc}")
                    _learn(req, rt, con, "error", "")
            except Exception:  # noqa: BLE001 - the original error is the one to raise
                pass
        raise
    finally:
        if con is not None:
            con.close()
        _release(req, token)


def _body(req: CycleRequest, rt: Runtime, con, *, token: str, spend: float) -> CycleOutcome:
    cfg = req.config
    _refresh(req, token)

    # --- HARVEST -----------------------------------------------------------
    probes = tuple((rt.probes or _default_probes)(req))
    report = (rt.harvest or _default_harvest)(req, con, rt.clock(), probes)
    if not req.dry_run:
        _record_probes(con, req, probes, rt.clock())

    if not report.harvest_ok and cfg.probes.error_policy == "halt_before_spend":
        return _finish(req, rt, con, "halted", rc=1,
                       halt_reason="harvest_incomplete",
                       detail="harvest did not complete: " + ", ".join(report.errors))

    # Reconciling "what we did not see" only means something when every rule
    # ran. A skipped rule (an old home without that table) would otherwise mark
    # its candidates fixed_externally — the loop would forget a real gap
    # because this particular DB could not measure it.
    if not req.dry_run:
        _upsert(con, req, report, rt.clock())
        if report.harvest_ok and not report.skipped:
            # ``reconcile`` keeps the *ids* it was told about; any other open row
            # is closed. Passing signatures here would match nothing (ids carry
            # the ``rsi-`` prefix), so every gap would be marked
            # ``fixed_externally`` on the cycle that first saw it.
            ledger.reconcile(con, [c.candidate_id for c in report.candidates],
                             now=rt.clock())

    # --- QUEUE -------------------------------------------------------------
    plan = _build_queue(req, con, report, rt.clock())
    cand = _pick(req, plan)
    if cand is None:
        return _finish(req, rt, con, "no_candidate", rc=0,
                       detail=f"{plan.examined} candidate(s) examined, none eligible",
                       extra={"candidates_found": len(report.candidates),
                              "candidates_queued": len(plan.selected)})

    # --- SUGGEST -----------------------------------------------------------
    # A dry run must be able to show the would-be kickoff without a paid panel,
    # so it falls back to the candidate's own evidence. A live cycle never does:
    # there, a missing proposal is the recipe's job and it must say so.
    if req.proposal is not None:
        proposal = req.proposal
    elif req.dry_run and rt.propose is None:
        proposal = _preview_proposal(cand)
    else:
        proposal = (rt.propose or _default_propose)(req, cand)
    if proposal is None:
        return _finish(req, rt, con, "halted", rc=0, halt_reason="no_proposal",
                       candidate_id=cand.candidate_id,
                       detail="SUGGEST produced no proposal")

    selection = select({c.candidate_id: c for c in report.candidates}, proposal,
                       _select_context(req, con, report, cand))
    if not selection.accepted:
        if req.dry_run:
            return CycleOutcome(req.cycle_id, "rejected", 0, candidate_id=cand.candidate_id,
                                detail=f"{selection.code}: {selection.detail}")
        return _finish(req, rt, con, "rejected", rc=0, candidate_id=cand.candidate_id,
                       detail=f"{selection.code}: {selection.detail}",
                       extra=_candidate_fields(cand))

    if req.dry_run:
        return CycleOutcome(req.cycle_id, "halted", 0, candidate_id=cand.candidate_id,
                            detail="dry run: nothing written, nothing dispatched",
                            kickoff=selection.kickoff)
    if req.propose_only:
        return _finish(req, rt, con, "halted", rc=0, halt_reason="suggest_only",
                       candidate_id=cand.candidate_id, detail="proposal accepted; not dispatched",
                       extra=_candidate_fields(cand))

    ledger.set_candidate_status(con, cand.candidate_id, "claimed", now=rt.clock(),
                                reason=f"cycle {req.cycle_id}")
    _refresh(req, token)

    # --- IMPLEMENT ---------------------------------------------------------
    impl = (rt.implement or _default_implement)(req, cand, selection)
    if impl is None or not impl.landed_something:
        detail = (impl.detail if impl is not None else "the implementer produced nothing")
        if impl is not None:
            (rt.clean or _default_clean)(req, impl)
        return _finish(req, rt, con, "rejected", rc=0, candidate_id=cand.candidate_id,
                       detail=detail, extra={**_candidate_fields(cand),
                                             "child_run_id": getattr(impl, "child_run_id", "")})

    # --- VALIDATE ----------------------------------------------------------
    measurement = (rt.measure or _default_measure)(req, cand, impl, selection)
    evidence = _evidence(req, report, cand, impl, selection, measurement, spend)
    first = evaluate(evidence)

    raw = (rt.critics or _default_critics)(req, cand, impl, selection, first)
    evidence = _with_critics(evidence, raw)
    report_final = evaluate(evidence)

    # --- DECIDE ------------------------------------------------------------
    if not report_final.passed:
        return _quarantine(req, rt, con, cand, impl, report_final, "gate_failed")
    if not cfg.decide.auto_merge:
        return _quarantine(req, rt, con, cand, impl, report_final, "auto_merge_disabled")

    merged = (rt.merge or _default_merge)(req, impl)
    if not getattr(merged, "landed", False):
        return _quarantine(req, rt, con, cand, impl, report_final,
                           f"merge_{getattr(merged, 'status', 'failed')}",
                           detail=getattr(merged, "detail", ""))

    sha = str(getattr(merged, "sha", "") or "")
    ledger.set_candidate_status(con, cand.candidate_id, "merged", now=rt.clock(),
                                reason=f"cycle {req.cycle_id}", merged_sha=sha or None)
    return _finish(req, rt, con, "merged", rc=0, candidate_id=cand.candidate_id,
                   merged_sha=sha, gates=report_final,
                   extra={**_candidate_fields(cand),
                          "child_run_id": impl.child_run_id,
                          "cost_usd": impl.cost_usd})


# --------------------------------------------------------------------------
# Stage helpers
# --------------------------------------------------------------------------


def _pick(req: CycleRequest, plan: QueuePlan) -> Candidate | None:
    """The candidate this cycle works on: the pin, else the queue's top."""
    if req.candidate_id:
        item = plan.find(req.candidate_id)
        return item.candidate if item is not None else None
    return plan.top


def _queue_cfg(req: CycleRequest) -> QueueConfig:
    if req.max_candidates is None:
        return req.config.queue
    return replace(req.config.queue, max_candidates=req.max_candidates)


def _build_queue(req: CycleRequest, con, report, now: int) -> QueuePlan:
    view = ledger_view(con, report.candidates, root=req.root)
    return build_queue(
        report.candidates,
        now=now,
        view=view,
        cfg=_queue_cfg(req),
        credits=ledger.credit_stats(con, min_n=req.config.queue.credit_min_n),
        domains=req.domains,
    )


def _select_context(req: CycleRequest, con, report, cand: Candidate) -> SelectContext:
    from mini_ork.vcs.repo_integrity_guard import enforced_frozen_paths
    from .select import read_claims

    cfg = req.config
    worktrees_dir = _worktrees_dir(req.root)
    return SelectContext(
        root=req.root,
        config=SelectConfig(
            max_files=cfg.gates.max_files,
            allow_prefixes=cfg.gates.allow_prefixes or DEFAULT_ALLOW_PREFIXES,
            queue=_queue_cfg(req),
        ),
        claims=read_claims(worktrees_dir),
        frozen_paths=tuple(enforced_frozen_paths(str(req.root))),
        view=ledger_view(con, report.candidates, root=req.root),
        probe_status={p.probe_id: p.status for p in report.probes},
        now=int(time.time()),
    )


def _evidence(req: CycleRequest, report, cand: Candidate, impl: Implementation,
              selection: Selection, measured: GateEvidence, spend: float) -> GateEvidence:
    """Fill the facts the cycle owns; trust the measurer for the rest.

    The split matters: ``measure`` must not be able to state whether the harvest
    completed, what the kickoff scope was, or whether a path is frozen — those
    are the cycle's own record of what it asked for.
    """
    from mini_ork.vcs.repo_integrity_guard import enforced_frozen_paths, frozen_hits

    cfg = req.config
    root = getattr(impl.worktree, "path", req.root)
    return replace(
        measured,
        harvest_ok=report.harvest_ok,
        required_probe_failures=tuple(
            p.probe_id for p in report.probes if p.failed and p.required),
        child_verdict=impl.verdict,
        frozen_hits=tuple(frozen_hits(list(impl.changed_files),
                                      enforced_frozen_paths(str(root)))),
        changed_files=tuple(impl.changed_files),
        scope_files=tuple(selection.files),
        allow_prefixes=cfg.gates.allow_prefixes or DEFAULT_ALLOW_PREFIXES,
        max_files=cfg.gates.max_files,
        metric_command=cand.success_command,
        spend_24h=float(spend),
        daily_cap=float(cfg.budget.daily_cap_usd),
        cycle_cost=float(impl.cost_usd),
        per_cycle_cap=float(cfg.budget.per_cycle_cap_usd),
        implementer_family=cfg.implement.lane,
    )


def _candidate_fields(cand: Candidate) -> dict[str, Any]:
    return {"domain": cand.domain, "signature": cand.signature, "source": cand.source}


def _preview_proposal(cand: Candidate) -> Proposal:
    """The kickoff a dry run shows: the candidate's own scope, nothing invented."""
    return Proposal(
        candidate_id=cand.candidate_id,
        files=tuple(cand.files),
        summary=cand.title,
        root_cause=cand.detail or cand.fix_hint,
        verification_command=cand.success_command,
    )


def _upsert(con, req: CycleRequest, report, now: int) -> None:
    for cand in report.candidates:
        ledger.upsert_candidate(con, cand, now=now)


def _record_probes(con, req: CycleRequest, probes: Sequence[ProbeResult], now: int) -> None:
    for probe in probes:
        ledger.record_probe_run(
            con, cycle_id=req.cycle_id, now=now, probe_id=probe.probe_id,
            cmd=probe.cmd, status=probe.status, rc=probe.rc,
            duration_ms=probe.duration_ms, findings_count=len(probe.findings),
            payload=probe.payload, stderr_digest=probe.stderr_digest)


def _finish(req: CycleRequest, rt: Runtime, con, status: str, *, rc: int,
            halt_reason: str = "", detail: str = "", candidate_id: str = "",
            merged_sha: str = "", gates: ValidationReport | None = None,
            extra: Mapping[str, Any] | None = None) -> CycleOutcome:
    fields: dict[str, Any] = {"halt_reason": halt_reason}
    if candidate_id:
        fields["candidate_id"] = candidate_id
    if merged_sha:
        fields["merged_sha"] = merged_sha
    if gates is not None:
        fields["gates_json"] = gates.to_dict()
    fields.update(extra or {})
    if not req.dry_run:
        ledger.finish_cycle(con, req.cycle_id, now=rt.clock(), status=status, **fields)
        _learn(req, rt, con, status, candidate_id)
    return CycleOutcome(req.cycle_id, status, rc, halt_reason=halt_reason,
                        candidate_id=candidate_id, detail=detail, merged_sha=merged_sha,
                        gates=gates)


def _learn(req: CycleRequest, rt: Runtime, con, status: str,
           candidate_id: str) -> None:
    """Record credit + the collapse measurement for a finished cycle.

    Runs *after* ``finish_cycle`` so the merge rate it computes includes this
    cycle. Bookkeeping must never change a decision that was already made: a
    merged cycle whose credit row failed is still merged, so errors here are
    swallowed and reported on stderr. That includes a collapse trip that could
    not be written — the cycle is already finished by then, and rewriting a
    ``merged`` row to ``error`` would misreport work that landed. The message
    says the stop is NOT armed, because that is the part a human must act on.
    """
    try:
        report = learn.record(con, cycle_id=req.cycle_id, status=status,
                              candidate_id=candidate_id or None,
                              home=req.home, now=rt.clock())
    except Exception as exc:  # noqa: BLE001 - see the docstring
        print(f"[rsi] learn failed for {req.cycle_id}: {type(exc).__name__}: {exc}",
              file=sys.stderr)
        return
    if report.get("collapse"):
        if report.get("collapse_error"):
            print("[rsi] collapse detected but the DISABLED file could not be"
                  f" written ({report['collapse_error']}); the next cycle will"
                  " NOT halt — fix the home directory before resuming",
                  file=sys.stderr)
        else:
            print(f"[rsi] collapse detected: {report['disabled_path']} written;"
                  " the next cycle will halt", file=sys.stderr)


def _quarantine(req: CycleRequest, rt: Runtime, con, cand: Candidate,
                impl: Implementation, report: ValidationReport,
                reason: str, *, detail: str = "") -> CycleOutcome:
    """Keep the work; record why. A quarantined cycle is evidence, not garbage."""
    failing = ", ".join(report.codes) or reason
    path = (rt.quarantine or _default_quarantine)(req, cand, impl, report, reason)
    ledger.set_candidate_status(con, cand.candidate_id, "quarantined", now=rt.clock(),
                                reason=f"{reason}: {failing}")
    if not req.config.decide.quarantine_keep_worktree and impl is not None:
        (rt.clean or _default_clean)(req, impl)
    return _finish(
        req, rt, con, "quarantined", rc=0, halt_reason=reason,
        candidate_id=cand.candidate_id,
        detail=(detail + "; " if detail else "") + "failing gates: " + failing,
        gates=report,
        extra={**_candidate_fields(cand),
               "child_run_id": impl.child_run_id,
               "cost_usd": impl.cost_usd,
               "gates_json": report.to_dict(),
               "quarantine_reason": f"{reason}: {failing}",
               "quarantine_path": str(path) if path else ""},
    )


# --------------------------------------------------------------------------
# Lease
# --------------------------------------------------------------------------


def _require_lease_tables(req: CycleRequest) -> None:
    """Checked first: ``acquire_lease`` swallows ``sqlite3.Error`` and returns
    ``None``, which is indistinguishable from "held"."""
    from mini_ork.stores.lease import lease_tables_present

    if not Path(req.db).exists():
        raise CycleSetupError(f"state DB not found at {req.db} — run `mini-ork init`")
    if not lease_tables_present(str(req.db)):
        raise CycleSetupError("lease tables are missing — run `mini-ork update`")


def _acquire(req: CycleRequest) -> str | None:
    from mini_ork.stores.lease import acquire_lease

    return acquire_lease(str(req.db), req.config.lease.key,
                         ttl_s=req.config.lease.ttl_s)


def _current_lease(req: CycleRequest) -> Mapping[str, Any] | None:
    from mini_ork.stores.lease import current_lease

    return current_lease(str(req.db), req.config.lease.key)


def _refresh(req: CycleRequest, token: str) -> None:
    from mini_ork.stores.lease import refresh_lease

    if not refresh_lease(str(req.db), req.config.lease.key, token,
                         ttl_s=req.config.lease.ttl_s):
        raise CycleFailure("the cycle lost its lease mid-run (halt_reason=lease_lost)")


def _release(req: CycleRequest, token: str) -> None:
    from mini_ork.stores.lease import release_lease

    try:
        release_lease(str(req.db), req.config.lease.key, token)
    except Exception:  # noqa: BLE001 - a failed release expires on its own TTL
        pass


# --------------------------------------------------------------------------
# Default adapters — the real world
# --------------------------------------------------------------------------


def _worktrees_dir(root: Path) -> Path:
    explicit = os.environ.get("MINI_ORK_WORKTREES_DIR")
    if explicit:
        return Path(explicit)
    root = Path(root)
    return root.parent / f"{root.name}-worktrees"


def _default_spend(db: Path) -> float:
    from mini_ork.cost_ledger import spent_last_24h

    return float(spent_last_24h(str(db)))


def _default_probes(req: CycleRequest) -> tuple[ProbeResult, ...]:
    from . import probes as probe_mod

    cfg = req.config.probes
    # ``db`` pins the in-process ``verifier-audit`` probe: a cycle given
    # ``--db`` must audit that database, not whichever one the child env
    # happens to resolve.
    by_id = {s.probe_id: s for s in probe_mod.default_specs(
        root=req.root, pytest_paths=cfg.pytest_paths, timeout_s=cfg.timeout_s,
        db=req.db)}
    if not cfg.run:
        specs = list(by_id.values())
    else:
        specs = []
        for probe_id in cfg.run:
            if probe_id.startswith(probe_mod.SYNTHETIC_PREFIX):
                specs.append(probe_mod.synthetic_spec(
                    probe_id[len(probe_mod.SYNTHETIC_PREFIX):]))
                continue
            spec = by_id.get(probe_id)
            if spec is None:
                # Every id ``config`` accepts is built by ``default_specs`` (the
                # shipped example is pinned by a test against exactly this), so
                # this is a config bug, not a missing implementation.
                raise CycleSetupError(
                    f"probe {probe_id!r} is configured but has no runnable spec")
            specs.append(spec)

    return probe_mod.run_probes(specs, root=req.root, runner=_scoped_runner(req))


def _scoped_runner(req: CycleRequest):
    """A probe runner that pins the DB and root the cycle decided on.

    Probes are subprocesses; without this each would resolve its own DB from the
    ambient environment, and a cycle given ``--db`` would harvest one database
    while its probes report on another.
    """
    from .probes import subprocess_runner

    def run(argv, *, timeout_s: int, cwd: str):
        return subprocess_runner(argv, timeout_s=timeout_s, cwd=cwd,
                                 env={**os.environ,
                                      "MINI_ORK_DB": str(req.db),
                                      "MINI_ORK_ROOT": str(req.root)})

    return run


def _default_harvest(req: CycleRequest, con, now: int, probes):
    from .harvest import harvest

    return harvest(req.root, con, now=now,
                   cfg={"per_source_max": req.config.probes.per_source_max},
                   probes=tuple(probes))


#: How long one SUGGEST run may take. Five model nodes, four of them in
#: parallel; the critics get ``critic.model_timeout_s`` from config instead.
_SUGGEST_TIMEOUT_S = 1800


class CriticsResult(tuple):
    """Verdicts plus the record of which lane actually served each critic.

    ``Runtime.critics`` returns a plain tuple when nothing measured the panel
    (an injected fake in a test). The recipe path returns this subclass so G10
    also sees the served-lane report. Subclassing ``tuple`` keeps every existing
    caller — ``tuple(...)``, ``len()``, iteration — working unchanged.
    """

    panel: Mapping[str, Any] | None

    def __new__(cls, verdicts: Sequence[CriticVerdict] = (), panel=None):
        self = super().__new__(cls, tuple(verdicts))
        self.panel = panel
        return self


def _with_critics(evidence: GateEvidence, raw: Sequence[CriticVerdict]) -> GateEvidence:
    """Attach the panel's verdicts and, when the run measured it, the served lanes.

    G10 grades two things: the verdicts and — when the critic run reported it —
    which lane actually served each critic. A plain tuple (an injected fake)
    carries no measurement, so ``critic_panel`` stays ``None`` and the gate
    judges the families recorded on the verdicts alone. A panel without verdicts
    is dropped: it would describe a review that produced no answer.
    """
    critics = tuple(raw)
    if not critics:
        return evidence
    return replace(evidence, critics=critics, critic_panel=getattr(raw, "panel", None))


def _recipe_dir(req: CycleRequest, name: str) -> Path:
    return Path(req.root) / "recipes" / name


def _candidate_brief(cand: Candidate) -> dict[str, Any]:
    """The candidate as the panel sees it — model input, not the ledger row."""
    return {
        "candidate_id": cand.candidate_id,
        "domain": cand.domain,
        "kind": cand.kind,
        "severity": cand.severity,
        "title": cand.title,
        "detail": cand.detail,
        "files": list(cand.files),
        "evidence": dict(cand.evidence),
        "evidence_key": cand.evidence_key,
        "fix_hint": cand.fix_hint,
        "success_metric": cand.success_metric,
        "success_command": cand.success_command,
        "source": cand.source,
    }


def _run_recipe(req: CycleRequest, recipe: str, brief: Mapping[str, Any],
                kickoff: str, *, timeout_s: int, brief_env: str) -> tuple[int, Path, Path]:
    """Dispatch one recipe run for this cycle; return ``(rc, home, run_dir)``.

    Same shape as the implementer child: its own home (the runner rewrites
    ``agents.yaml`` there), the cycle's DB, and a run id derived from the cycle
    so every artifact of one cycle sorts together. The brief is handed over by
    environment variable because the run directory does not exist yet.
    """
    stage = req.home / "rsi" / req.cycle_id / recipe
    home = stage / "home"
    home.mkdir(parents=True, exist_ok=True)
    brief_path = stage / "brief.json"
    brief_path.write_text(json.dumps(brief, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    kickoff_path = stage / "kickoff.md"
    kickoff_path.write_text(kickoff, encoding="utf-8")
    run_id = f"{req.cycle_id}-{recipe}"

    argv = [
        "timeout", str(timeout_s),
        str(Path(req.root) / "bin" / "mini-ork"), "run",
        "--deadline", str(max(60, timeout_s - 120)), recipe, str(kickoff_path),
    ]
    env = {
        **os.environ,
        "MINI_ORK_ROOT": str(req.root),
        "MINI_ORK_DB": str(req.db),
        "MINI_ORK_HOME": str(home),
        "MINI_ORK_RUN_ID": run_id,
        "MO_RUNTIME_BACKEND": req.config.implement.runtime_backend,
        brief_env: str(brief_path),
    }
    try:
        proc = subprocess.run(argv, cwd=str(req.root), env=env, capture_output=True,
                              text=True, timeout=timeout_s + 60)
    except subprocess.TimeoutExpired:
        return _TIMEOUT_RC, home, home / "runs" / run_id
    except OSError as exc:
        raise CycleFailure(f"the {recipe} run could not be started: {exc}") from exc
    return proc.returncode, home, home / "runs" / run_id


def _run_artifact(home: Path, run_id: str, filename: str) -> Path | None:
    """The declared artifact of this run, or the newest one under its home.

    The sink's run id is authoritative when the engine honoured it; a run that
    renamed itself still leaves exactly one artifact in a fresh home.
    """
    direct = home / "runs" / run_id / filename
    if direct.is_file():
        return direct
    hits = sorted(home.glob(f"runs/*/{filename}"), key=lambda p: p.stat().st_mtime)
    return hits[-1] if hits else None


def _default_propose(req: CycleRequest, cand: Candidate) -> Proposal | None:
    """SUGGEST is the first model edge and lives in a recipe, not here.

    Failing loudly is the point: inventing a proposal from the candidate alone
    would dispatch an implementer with no diagnosis behind it.
    """
    if not _recipe_dir(req, "rsi-cycle").is_dir():
        raise CycleFailure(
            "SUGGEST has no recipe: recipes/rsi-cycle is not installed under "
            f"{req.root}, and this cycle has no injected proposal. Install the "
            "recipe (it ships with the repo) or inject one through the runtime.")
    return _recipe_propose(req, cand)


def _recipe_propose(req: CycleRequest, cand: Candidate) -> Proposal:
    cfg = req.config
    brief = {
        "version": 1,
        "cycle_id": req.cycle_id,
        "candidate": _candidate_brief(cand),
        "constraints": {
            "repo_root": str(req.root),
            "verification_command": cand.success_command,
            "max_files": cfg.gates.max_files,
            "allow_prefixes": list(cfg.gates.allow_prefixes),
        },
        # Context only: the panel answers exactly one candidate. Carrying the
        # neighbours lets a lens notice a duplicate without letting it re-rank.
        "candidates": [{"candidate_id": cand.candidate_id, "domain": cand.domain,
                        "kind": cand.kind, "severity": cand.severity,
                        "title": cand.title}],
    }
    kickoff = "\n".join([
        f"# RSI suggest: {cand.title}",
        "",
        "Produce one fix proposal for the gap described in the machine brief.",
        "",
        f"- The brief (authoritative): `{req.home / 'rsi' / req.cycle_id / 'rsi-cycle' / 'brief.json'}`",
        "- Recipe: rsi-cycle. Run every node; do not skip a lens because another one looks right.",
        "- Required output: `$MINI_ORK_RUN_DIR/proposal.json`, shaped as described in the "
        "synthesize prompt.",
        "- Do not edit the repository. This run proposes; the cycle decides.",
        "",
    ])
    rc, home, run_dir = _run_recipe(req, "rsi-cycle", brief, kickoff,
                                    timeout_s=_SUGGEST_TIMEOUT_S,
                                    brief_env="MO_RSI_SUGGEST_BRIEF")
    path = _run_artifact(home, run_dir.name, "proposal.json")
    if path is None:
        raise CycleFailure(
            f"the SUGGEST run exited {rc} without a proposal.json (see {run_dir})")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise CycleFailure(f"{path} is not JSON: {exc}") from exc
    if not isinstance(payload, Mapping):
        raise CycleFailure(f"{path} must be a JSON object")
    return Proposal.from_mapping(payload)


def worktree_config(req: CycleRequest, *, slug: str, files: Sequence[str],
                    test_cmd: str | None = None):
    """The one place a cycle's worktree policy is built.

    ``clean`` and ``revert`` in the CLI act on a worktree that an earlier process
    created, so they cannot reuse the object — they rebuild it from the ledger.
    Deriving it twice is how the operator path and the run path end up
    disagreeing about ``require_remote``, which is the difference between a
    quarantine and a commit to local ``main``.
    """
    from .worktree import WorktreeConfig

    return WorktreeConfig(
        slug=slug,
        files=tuple(files),
        worktrees_dir=_worktrees_dir(req.root),
        primary=req.root,
        test_cmd=test_cmd or req.config.merge_test_cmd,
        require_remote=req.config.merge.require_remote,
        clean_after_merge=req.config.merge.on_merge == "clean",
    )


def _default_implement(req: CycleRequest, cand: Candidate,
                       selection: Selection) -> Implementation | None:
    """Claim a worktree, dispatch the child inside it, land its diff."""
    from .worktree import (
        Abstain,
        apply_patch,
        assert_worktree_root,
        claim_and_create,
    )

    cfg = worktree_config(req, slug=req.cycle_id, files=selection.files)
    wt = claim_and_create(cfg)
    # A diff built against the wrong tree is the shared-engine-tree hazard: the
    # child would fix main while claiming to fix the worktree.
    from .worktree import subprocess_runner

    assert_worktree_root(wt, runner=subprocess_runner)

    child_home = req.home / "rsi" / req.cycle_id / "child-home"
    child_home.mkdir(parents=True, exist_ok=True)
    run_id, rc, out = _dispatch_child(req, wt, selection.kickoff, child_home)

    diff_path = child_home / "runs" / run_id / "framework-edit.diff"
    verdict_path = child_home / "runs" / run_id / "verdict.json"
    if not diff_path.is_file():
        diff_path, run_id = _newest_diff(child_home, run_id)
    diff = diff_path.read_text(encoding="utf-8") if diff_path.is_file() else ""
    verdict = _read_json(verdict_path)
    cost = _child_cost(req, run_id, rc)
    if not diff.strip():
        return Implementation(wt, cfg, detail=f"the child produced no diff (rc={rc})",
                              child_run_id=run_id, cost_usd=cost, verdict=verdict)

    landed = apply_patch(wt, diff, message=f"rsi: {cand.title} [{req.cycle_id}]")
    if isinstance(landed, Abstain):
        return Implementation(wt, cfg, detail=f"land_patch abstained: {landed.reason}",
                              child_run_id=run_id, cost_usd=cost, verdict=verdict)
    return Implementation(
        wt, cfg, changed_files=tuple(_changed_files(wt)), diff=diff,
        verdict=verdict, child_run_id=run_id, cost_usd=cost,
    )


def _dispatch_child(req: CycleRequest, wt, kickoff: str, child_home: Path) -> tuple[str, int, str]:
    """Run ``framework-edit`` inside the worktree, with its own home and DB."""
    run_id = f"{req.cycle_id}-fe"
    kickoff_path = child_home / "kickoff.md"
    kickoff_path.write_text(kickoff, encoding="utf-8")
    argv = [
        "timeout", str(req.config.implement.child_timeout_s),
        str(Path(wt.path) / "bin" / "mini-ork"), "run",
        "--deadline", str(max(60, req.config.implement.child_timeout_s - 120)),
        "framework-edit", str(kickoff_path),
    ]
    env = {
        **os.environ,
        "MINI_ORK_ROOT": str(wt.path),
        "MINI_ORK_DB": str(req.db),
        "MINI_ORK_HOME": str(child_home),
        "MINI_ORK_RUN_ID": run_id,
        "MO_ALLOW_FRAMEWORK_CWD": "1",
        "MO_RUNTIME_BACKEND": req.config.implement.runtime_backend,
    }
    if req.config.implement.static_recipe_plan:
        env["MO_STATIC_RECIPE_PLAN"] = "1"
    try:
        proc = subprocess.run(argv, cwd=str(wt.path), env=env, capture_output=True,
                              text=True, timeout=req.config.implement.child_timeout_s + 60)
    except subprocess.TimeoutExpired:
        return run_id, _TIMEOUT_RC, ""
    except OSError as exc:
        raise CycleFailure(f"the child could not be started: {exc}") from exc
    out = proc.stdout or ""
    sink = _RESULT_SINK.findall(out)
    if sink:
        try:
            reported = json.loads(sink[-1]).get("run_id")
            if reported:
                run_id = str(reported)
        except ValueError:
            pass
    return run_id, proc.returncode, out


def _newest_diff(child_home: Path, fallback: str) -> tuple[Path, str]:
    candidates = sorted(
        (p for p in (child_home / "runs").glob("*/framework-edit.diff") if p.is_file()),
        key=lambda p: p.stat().st_mtime, reverse=True)
    if not candidates:
        return child_home / "runs" / fallback / "framework-edit.diff", fallback
    return candidates[0], candidates[0].parent.name


def _read_json(path: Path) -> Mapping[str, Any] | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _changed_files(wt) -> list[str]:
    try:
        proc = subprocess.run(
            ["git", "-C", str(wt.path), "diff", "--name-only", "HEAD~1", "HEAD"],
            capture_output=True, text=True, timeout=60, check=False)
    except (OSError, subprocess.SubprocessError):
        return []
    return [line.strip() for line in proc.stdout.splitlines() if line.strip()]


def _child_cost(req: CycleRequest, run_id: str, rc: int) -> float:
    """The child's spend, from the ledger it wrote to. ``0.0`` when unreadable —
    a spend we cannot measure cannot be used to claim we stayed under budget,
    so G11 also compares the *cycle* cost against the per-cycle cap."""
    if not Path(req.db).exists():
        return 0.0
    try:
        con = sqlite3.connect(str(req.db), timeout=5)
        try:
            row = con.execute(
                "SELECT COALESCE(SUM(cost_usd), 0) FROM llm_calls WHERE run_id = ?",
                (run_id,)).fetchone()
        finally:
            con.close()
    except sqlite3.Error:
        return 0.0
    return float(row[0] or 0.0)


# --- measurement -----------------------------------------------------------


def _default_measure(req: CycleRequest, cand: Candidate, impl: Implementation,
                     selection: Selection) -> GateEvidence:
    """Run the checks the gates read. Nothing here decides pass/fail."""
    wt = Path(getattr(impl.worktree, "path", req.root))
    changed = tuple(impl.changed_files)
    lint_ok, lint_detail = _lint(wt, changed, req.config.gates.cmd_timeout_s)
    pytest_ok, pytest_detail = _pytest(wt, req, cand, changed)
    static_before, static_after, static_ran = _static_differential(req, wt)
    gap_before, gap_after = _gap(req, cand, wt)
    failures_before, failures_after, probes_ran = _probe_differential(req, wt)
    return GateEvidence(
        lint_ok=lint_ok, lint_detail=lint_detail,
        pytest_ok=pytest_ok, pytest_detail=pytest_detail,
        static_before=static_before, static_after=static_after, static_ran=static_ran,
        gap_before=gap_before, gap_after=gap_after,
        probe_failures_before=failures_before, probe_failures_after=failures_after,
        probes_ran=probes_ran,
    )


def _run(argv: Sequence[str], *, cwd: Path, timeout_s: int,
         env: Mapping[str, str] | None = None) -> tuple[int, str]:
    try:
        proc = subprocess.run(list(argv), cwd=str(cwd), env=dict(env or os.environ),
                              capture_output=True, text=True, timeout=timeout_s)
    except subprocess.TimeoutExpired:
        return _TIMEOUT_RC, "timed out"
    except OSError as exc:
        return 127, f"{type(exc).__name__}: {exc}"
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def _lint(wt: Path, changed: Sequence[str], timeout_s: int) -> tuple[bool, str]:
    files = [p for p in changed if p.endswith(".py")]
    if not files:
        return True, "no Python files changed"
    rc, out = _run([sys.executable, "-m", "ruff", "check", "--select", "F,E9",
                    "--line-length", "110", *files], cwd=wt, timeout_s=timeout_s)
    return rc == 0, "" if rc == 0 else out[-2000:]


def _pytest(wt: Path, req: CycleRequest, cand: Candidate,
            changed: Sequence[str]) -> tuple[bool, str]:
    """The kickoff's tests plus every ``tests/`` file importing a changed module."""
    tests = list(req.config.scoped_tests)
    tests.extend(_importing_tests(req.root, changed, existing=tests))
    if not tests:
        return False, ("no scoped tests: set gates.pytest_paths — an unscoped "
                       "pytest run is the whole suite")
    rc, out = _run([sys.executable, "-m", "pytest", "-q", "-p", "no:asyncio", *tests],
                   cwd=wt, timeout_s=req.config.gates.cmd_timeout_s)
    return rc == 0, "" if rc == 0 else out[-4000:]


def _importing_tests(root: Path, changed: Sequence[str], *,
                     existing: Sequence[str]) -> list[str]:
    """One-level reverse import search, by module stem.

    Structural, not line-based: a test file that imports the changed module by
    name is the pinning test for that module, whatever line it is on.
    """
    stems = {Path(p).stem for p in changed if p.endswith(".py")}
    if not stems:
        return []
    tests_dir = Path(root) / "tests"
    if not tests_dir.is_dir():
        return []
    out: list[str] = []
    for path in tests_dir.rglob("test_*.py"):
        rel = path.relative_to(root).as_posix()
        if rel in existing:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if any(f"import {stem}" in text or f"from {stem}" in text for stem in stems):
            out.append(rel)
    return out


def _static_differential(req: CycleRequest, wt: Path) -> tuple[tuple, tuple, bool]:
    """``validate --strict`` + ``garden`` findings at ``HEAD~1`` and ``HEAD``."""
    base = _detached_base(req, wt)
    if base is None:
        return (), (), False
    try:
        before = _static_findings(req, base)
        after = _static_findings(req, wt)
    finally:
        _remove_detached(req, base)
    return before, after, True


def _static_findings(req: CycleRequest, root: Path) -> tuple:
    from .probes import parse_garden, parse_validate

    bin_path = str(Path(root) / "bin" / "mini-ork")
    env = {**os.environ, "MINI_ORK_ROOT": str(root), "MINI_ORK_DB": str(req.db)}
    findings: list = []
    rc, out = _run([bin_path, "garden"], cwd=root,
                   timeout_s=req.config.gates.cmd_timeout_s, env=env)
    findings.extend(parse_garden(out))
    rc, out = _run([bin_path, "validate", "--strict"], cwd=root,
                   timeout_s=req.config.gates.cmd_timeout_s, env=env)
    findings.extend(parse_validate(out))
    return tuple(findings)


def _detached_base(req: CycleRequest, wt: Path) -> Path | None:
    tmp = _worktrees_dir(req.root) / f".rsi-base-{req.cycle_id}"
    rc, _ = _run(["git", "-C", str(wt), "worktree", "add", "--detach", str(tmp),
                  "HEAD~1"], cwd=wt, timeout_s=120)
    return tmp if rc == 0 else None


def _remove_detached(req: CycleRequest, base: Path) -> None:
    _run(["git", "-C", str(req.root), "worktree", "remove", "--force", str(base)],
         cwd=req.root, timeout_s=120)


def _gap(req: CycleRequest, cand: Candidate, wt: Path) -> tuple[bool | None, bool | None]:
    """G8's before/after: is this candidate's gap still measurable?

    A ``db:<RULE>`` success command re-runs the rule; anything else compares the
    candidate's own finding signature against the probe findings at each tree.
    """
    base = _detached_base(req, wt)
    if base is None:
        return None, None
    try:
        before = _gap_at(req, cand, base)
        after = _gap_at(req, cand, wt)
    finally:
        _remove_detached(req, base)
    return before, after


def _gap_at(req: CycleRequest, cand: Candidate, root: Path) -> bool | None:
    from .probes import SYNTHETIC_PREFIX, run_synthetic

    command = cand.success_command or ""
    if command.startswith(SYNTHETIC_PREFIX):
        rule = command[len(SYNTHETIC_PREFIX):]
        try:
            result = run_synthetic(rule, root=root, db=req.db, now=int(time.time()))
        except Exception:  # noqa: BLE001 - "cannot measure" is None, not True
            return None
        return result.status == "findings"
    # A real probe id: the gap is the finding whose signature produced this
    # candidate. It is present when the probe still reports that signature.
    from . import probes as probe_mod

    spec = {s.probe_id: s for s in probe_mod.default_specs(
        root=root, pytest_paths=req.config.probes.pytest_paths,
        timeout_s=req.config.probes.timeout_s, db=req.db)}.get(command)
    if spec is None:
        return None
    env = {**os.environ, "MINI_ORK_ROOT": str(root), "MINI_ORK_DB": str(req.db)}
    try:
        result = probe_mod.run_probe(spec, root=root,
                                     runner=lambda a, timeout_s, cwd: probe_mod.subprocess_runner(
                                         a, timeout_s=timeout_s, cwd=cwd, env=env))
    except Exception:  # noqa: BLE001
        return None
    wanted = cand.evidence_key.rsplit(":", 1)[-1]
    return any(f.signature() == wanted for f in result.findings)


def _probe_differential(req: CycleRequest, wt: Path) -> tuple[tuple, tuple, bool]:

    base = _detached_base(req, wt)
    if base is None:
        return (), (), False
    try:
        before = _probe_failures(req, base)
        after = _probe_failures(req, wt)
    finally:
        _remove_detached(req, base)
    return before, after, True


def _probe_failures(req: CycleRequest, root: Path) -> tuple[str, ...]:
    from . import probes as probe_mod

    # No ``pytest_paths`` on purpose: G9 compares cheap probe health at the two
    # trees, and G6 has already run the scoped tests once. ``verifier-audit``
    # reads the DB, not the tree, so it is identical at both ends and drops out
    # of the set difference — it costs one query, not a false failure.
    specs = probe_mod.default_specs(
        root=root, pytest_paths=(), timeout_s=req.config.probes.timeout_s, db=req.db)
    results = probe_mod.run_probes(
        specs, root=root,
        runner=lambda a, timeout_s, cwd: probe_mod.subprocess_runner(
            a, timeout_s=timeout_s, cwd=cwd,
            env={**os.environ, "MINI_ORK_ROOT": str(root), "MINI_ORK_DB": str(req.db)}))
    return tuple(r.probe_id for r in results if r.failed)


def _critic_brief(req: CycleRequest, cand: Candidate, impl: Implementation,
                  report: ValidationReport) -> dict[str, Any]:
    """What a critic is asked to judge: the gap, the diff, the gates, the metric."""
    gates = [{"id": r.gate, "name": r.name, "passed": r.ok,
              "code": r.code, "detail": r.detail} for r in report.results]
    g8 = next((g for g in gates if g["id"] == "G8"), None)
    return {
        "version": 1,
        "cycle_id": req.cycle_id,
        "candidate": _candidate_brief(cand),
        "implementer_lane": req.config.implement.lane,
        "diff": impl.diff,
        "changed_files": list(impl.changed_files),
        "gates": gates,
        "metric": {"command": cand.success_command,
                   "detail": (g8 or {}).get("detail", ""),
                   "passed": (g8 or {}).get("passed")},
        "child_verdict": dict(impl.verdict or {}),
    }


def _recipe_critics(req: CycleRequest, cand: Candidate, impl: Implementation,
                    selection: Selection, report: ValidationReport) -> tuple[CriticVerdict, ...]:
    """Ask the critic recipe for an independent, two-family review of this diff.

    A critic run that fails returns an *empty* verdict set rather than raising:
    G10 then fails closed and the cycle quarantines, keeping the worktree. An
    exception here would take down a cycle whose diff already landed, which
    loses the work in exchange for a cleaner stack trace.
    """
    cfg = req.config
    brief = _critic_brief(req, cand, impl, report)
    lanes = ", ".join(cfg.critic.lanes)
    kickoff = "\n".join([
        f"# RSI review: {cand.title}",
        "",
        "Review the diff in the machine brief for one question: does it fix the "
        "gap it claims to fix, without making any check easier to pass?",
        "",
        f"- The brief (authoritative): "
        f"`{req.home / 'rsi' / req.cycle_id / 'rsi-critic' / 'brief.json'}`",
        f"- Recipe: rsi-critic. Every critic answers independently; families: {lanes}.",
        "- Required output: `$MINI_ORK_RUN_DIR/critics.json` and "
        "`$MINI_ORK_RUN_DIR/panel-composition.json`.",
        "- Do not edit the repository. You judge; the cycle decides.",
        "",
    ])
    try:
        rc, home, run_dir = _run_recipe(req, "rsi-critic", brief, kickoff,
                                        timeout_s=cfg.critic.model_timeout_s,
                                        brief_env="MO_RSI_CRITIC_BRIEF")
    except CycleFailure as exc:
        sys.stderr.write(f"rsi-cycle: critic run failed: {exc}\n")
        return CriticsResult(())

    verdicts_path = _run_artifact(home, run_dir.name, "critics.json")
    if verdicts_path is None:
        sys.stderr.write(f"rsi-cycle: critic run exited {rc} without critics.json "
                         f"(see {run_dir}); G10 will quarantine\n")
        return CriticsResult(())
    try:
        payload = json.loads(verdicts_path.read_text(encoding="utf-8"))
    except ValueError as exc:
        sys.stderr.write(f"rsi-cycle: {verdicts_path} is not JSON: {exc}\n")
        return CriticsResult(())

    rows = payload.get("critics") if isinstance(payload, Mapping) else None
    verdicts = [CriticVerdict.from_mapping(row) for row in (rows or ())
                if isinstance(row, Mapping)]
    # The panel file is written by the recipe from `llm_calls`; its absence is
    # an unmeasured panel, which is `None`, not a fabricated {"ok": true}.
    panel_path = _run_artifact(home, run_dir.name, "panel-composition.json")
    panel = None
    if panel_path is not None:
        try:
            loaded = json.loads(panel_path.read_text(encoding="utf-8"))
            panel = loaded if isinstance(loaded, Mapping) else None
        except ValueError:
            panel = None
    return CriticsResult(verdicts, panel=panel)


def _default_critics(req: CycleRequest, cand: Candidate, impl: Implementation,
                     selection: Selection, report: ValidationReport) -> tuple[CriticVerdict, ...]:
    """The panel is a recipe. Without it, G10 fails closed and the cycle quarantines."""
    if not _recipe_dir(req, "rsi-critic").is_dir():
        return CriticsResult(())
    return _recipe_critics(req, cand, impl, selection, report)


def _default_merge(req: CycleRequest, impl: Implementation):
    from .worktree import merge

    return merge(impl.worktree_config)


def _default_clean(req: CycleRequest, impl: Implementation) -> None:
    from .worktree import clean

    clean(impl.worktree_config)


def _default_quarantine(req: CycleRequest, cand: Candidate, impl: Implementation,
                        report: ValidationReport, reason: str) -> Path | None:
    from .worktree import write_quarantine

    record = {
        "cycle_id": req.cycle_id,
        "candidate_id": cand.candidate_id,
        "title": cand.title,
        "reason": reason,
        "gates": report.to_dict() if report is not None else {},
        "changed_files": list(impl.changed_files) if impl is not None else [],
        "child_run_id": getattr(impl, "child_run_id", ""),
        "diff_sha": _diff_sha(getattr(impl, "diff", "")),
    }
    return write_quarantine(record, record_dir=req.home / "rsi" / "quarantine",
                            cycle_id=req.cycle_id)


def _diff_sha(diff: str) -> str:
    import hashlib

    return hashlib.sha256(diff.encode("utf-8")).hexdigest()[:16] if diff else ""
