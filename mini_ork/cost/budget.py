"""Per-run USD budget — the ceiling a run may not cross, not the meter.

Three cost controls already exist, and none of them answers "may THIS run
spend another node?":

* :mod:`mini_ork.dispatch.cost_pause` is **reactive** — every
  ``MO_PAUSE_EVERY_USD`` it parks the run for an approval, forever.
* ``dispatch.llm_dispatch.cost_circuit_open`` is **global** — one rolling-24h
  cap shared by every run on the machine (``MO_DAILY_BUDGET_USD``), so a
  single runaway run can starve the fleet, and a cheap run cannot borrow a
  headroom it was given.
* ``MO_RECURSION_BUDGET_CAP_TOTAL_USD`` caps one recipe's recursion loop only.

This module adds the missing axis: a **declared ceiling on one run**, checked
before every dispatch. The run parks (``budget_exhausted``) instead of
failing, so raising the ceiling and resuming continues the same run with the
same worktree and the same plan.

Two tiers, one knob (see the repo's "feature delivery: two tiers" rule):

**Simple** — nothing to stand up. Declare the ceiling in the environment and
the local dispatch gate enforces it against the SQLite ledger::

    MO_RUN_BUDGET_USD=12 mini-ork run code-fix kickoff.md

or record it in the run's profile (``run_profile.json`` → ``budget_usd``) so
the ceiling travels with the run into recovery and the IDE. A declared value
that does not parse is a **refusal**, never a silent "unlimited".

**Secure / cloud-scalable** — register a policy that asks your real cost
service, and select it by name::

    from mini_ork.cost.budget import register_budget_policy, Verdict
    register_budget_policy("myorg", lambda db, run_id, run_dir: ...)

    MO_BUDGET_POLICY=myorg

An out-of-tree deployment reaches the same registry through the plugin seam
(``PluginContext.budget_policy`` in :mod:`mini_ork.plugins`) rather than
patching core.

The policy owns where the number comes from (a quota API, a billing export);
this module owns when it is asked and what a refusal looks like. An unknown
policy name fails closed.

Fail-open boundary, stated once: a missing or unreadable ledger reads as
``$0.00`` spent — the same choice ``cost_ledger.spent_last_24h`` makes — so a
fresh home is never blocked by a database that does not exist yet. A ceiling
you *declared* but the engine cannot parse is the opposite case and fails
closed, because the operator asked for a limit and silence would remove it.
"""
from __future__ import annotations

import json
import os
import time
from collections.abc import Callable
from dataclasses import dataclass

#: Env var carrying the declared ceiling for the current run.
BUDGET_ENV = "MO_RUN_BUDGET_USD"

#: Env var selecting a registered policy (default: the built-in declared one).
POLICY_ENV = "MO_BUDGET_POLICY"

#: Durable per-run marker: :mod:`mini_ork.attention` reads it to put the run
#: in front of a person.
MARKER_NAME = ".budget-exhausted"

#: The name of the built-in policy that reads ``BUDGET_ENV`` / the run profile.
DECLARED_POLICY = "declared"


@dataclass(frozen=True, slots=True)
class Verdict:
    """The answer to "may this run spend another node?".

    ``budget`` is ``None`` when no ceiling is declared — unlimited, the
    pre-existing behaviour. ``exhausted`` is the gate's whole input: it is
    ``True`` both when the ceiling is crossed and when the ceiling itself
    cannot be read, so a typo can never buy a run more spending room.
    """

    spent: float
    budget: float | None
    exhausted: bool
    detail: str


def _budget_from_env() -> float:
    """``MO_RUN_BUDGET_USD`` as a finite, positive float.

    Raises :class:`ValueError` for a present-but-unusable value (a typo, a
    negative, ``nan``/``inf``) so :func:`declared` can fail closed instead of
    silently removing the ceiling the operator asked for.
    """
    raw = os.environ.get(BUDGET_ENV, "").strip()
    try:
        value = float(raw)
    except ValueError:
        raise ValueError(f"{BUDGET_ENV}={raw!r} is not a number") from None
    if not (value == value) or value in (float("inf"), float("-inf")):
        raise ValueError(f"{BUDGET_ENV}={raw!r} is not a finite number")
    if value <= 0:
        raise ValueError(f"{BUDGET_ENV}={raw!r} must be > 0")
    return value


def _budget_from_profile(run_dir: str | os.PathLike | None) -> float | None:
    """``run_profile.json`` → ``budget_usd``, when the profile carries one.

    Same strictness as the env path: a present-but-unusable value raises.
    """
    if not run_dir:
        return None
    path = os.path.join(os.fspath(run_dir), "run_profile.json")
    if not os.path.isfile(path):
        # No profile yet is a fresh run (classify writes it before dispatch) —
        # it declares no ceiling, and that is not an error.
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            profile = json.load(fh)
    except (OSError, ValueError) as exc:
        # A profile that EXISTS but cannot be read is not "no ceiling": it may
        # well carry one, and reading it as unlimited would let a corrupted
        # file quietly raise the run's spend. Fail closed, and say which file.
        raise ValueError(f"{path} is unreadable: {exc}") from None
    if not isinstance(profile, dict):
        raise ValueError(f"{path} is not a JSON object")
    if "budget_usd" not in profile:
        return None
    raw = profile["budget_usd"]
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        raise ValueError(f"run_profile.json budget_usd={raw!r} is not a number") from None
    # ``nan`` and ``inf`` parse as floats and pass every ``<=`` comparison as
    # False, so ``spent >= budget`` would never fire: a profile carrying
    # ``Infinity`` would be a ceiling that silently never applies. Same
    # strictness as the env path, or the fail-closed rule is only half true.
    if not (value == value) or value in (float("inf"), float("-inf")):
        raise ValueError(f"run_profile.json budget_usd={raw!r} is not a finite number")
    if value <= 0:
        raise ValueError(f"run_profile.json budget_usd={raw!r} must be > 0")
    return value


def declared(run_dir: str | os.PathLike | None = None) -> float | None:
    """The ceiling this run declares, or ``None`` for "no ceiling".

    Precedence: ``MO_RUN_BUDGET_USD`` wins over the run profile, so a single
    retry can be given a different ceiling without rewriting the profile.

    Raises :class:`ValueError` when a declared value cannot be used — callers
    that gate spending must treat that as a refusal (see :func:`verdict`).
    """
    if os.environ.get(BUDGET_ENV, "").strip():
        return _budget_from_env()
    return _budget_from_profile(run_dir)


def _spent(db: str | os.PathLike | None, run_id: str) -> float:
    """The run's ledger total; an unreadable/absent ledger reads as ``$0.00``."""
    from mini_ork import cost_ledger  # noqa: PLC0415

    total = cost_ledger.run_cost(db, run_id)
    return 0.0 if total is None else float(total)


def _declared_verdict(db, run_id, run_dir) -> Verdict:
    spent = _spent(db, run_id)
    try:
        budget = declared(run_dir)
    except ValueError as exc:
        return Verdict(spent=spent, budget=None, exhausted=True,
                       detail=f"per-run budget is unusable: {exc}")
    if budget is None:
        # No ceiling NOW is not the same as no ceiling WAS. A run parked while
        # ``MO_RUN_BUDGET_USD`` was set, restarted by a process that no longer
        # has it, would otherwise read as unlimited and have its marker erased
        # by ``record`` — the park evaporates because a variable was lost
        # between processes. The marker is the only durable evidence that a
        # ceiling applied, so while it exists this run stays parked: re-declare
        # a ceiling above the spend, or remove the marker deliberately.
        if read_marker(run_dir) is not None:
            return Verdict(
                spent=spent, budget=None, exhausted=True,
                detail=(f"this run was parked by a ceiling that is no longer "
                        f"declared (spent ${spent:.2f}); re-declare "
                        f"{BUDGET_ENV} above that, or remove {MARKER_NAME} "
                        f"deliberately"))
        return Verdict(spent=spent, budget=None, exhausted=False,
                       detail=f"no per-run budget declared (spent ${spent:.2f})")
    if spent >= budget:
        return Verdict(spent=spent, budget=budget, exhausted=True,
                       detail=f"spent ${spent:.2f} of a ${budget:.2f} budget")
    return Verdict(spent=spent, budget=budget, exhausted=False,
                   detail=f"spent ${spent:.2f} of a ${budget:.2f} budget")


#: policy name → ``fn(db, run_id, run_dir) -> Verdict``.
BUDGET_POLICIES: dict[str, Callable[..., Verdict]] = {DECLARED_POLICY: _declared_verdict}


def register_budget_policy(name: str, fn: Callable[..., Verdict]) -> None:
    """Announce a budget policy a deployment may select with ``MO_BUDGET_POLICY``.

    Same registration shape as the runtime's other extension points (gate
    evaluators, routing policies, provider kinds): a policy that is not
    registered cannot be selected, and a selected-but-unknown policy fails
    closed rather than falling back to "unlimited".
    """
    if not name:
        raise ValueError("budget policy name required")
    BUDGET_POLICIES[name] = fn


def verdict(db: str | os.PathLike | None, run_id: str,
            run_dir: str | os.PathLike | None = None) -> Verdict:
    """Ask the active policy whether ``run_id`` may spend another node.

    Never raises: an unknown policy, a policy that raises, or an unusable
    declared ceiling all come back as ``exhausted=True`` with a ``detail``
    naming the problem. A gate that swallowed the error would silently turn a
    ceiling into no ceiling, which is the one outcome this module exists to
    prevent.
    """
    name = (os.environ.get(POLICY_ENV, "") or DECLARED_POLICY).strip() or DECLARED_POLICY
    fn = BUDGET_POLICIES.get(name)
    if fn is None:
        return Verdict(spent=_spent(db, run_id), budget=None, exhausted=True,
                       detail=f"unknown budget policy {name!r} "
                              f"(registered: {', '.join(sorted(BUDGET_POLICIES))})")
    try:
        return fn(db, run_id, run_dir)
    except Exception as exc:  # noqa: BLE001 — a broken policy must fail closed
        return Verdict(spent=_spent(db, run_id), budget=None, exhausted=True,
                       detail=f"budget policy {name!r} failed: {exc}")


def marker_path(run_dir: str | os.PathLike | None) -> str | None:
    if not run_dir:
        return None
    return os.path.join(os.fspath(run_dir), MARKER_NAME)


def record(run_dir: str | os.PathLike | None, v: Verdict) -> None:
    """Keep ``<run_dir>/.budget-exhausted`` truthful, or remove it.

    The marker is what the attention inbox reads; it must not outlive the
    condition. Called once per dispatch, so the not-exhausted path
    is a single ``os.path.exists`` when the marker was never written.
    """
    path = marker_path(run_dir)
    if not path:
        return
    if not v.exhausted:
        try:
            os.remove(path)
        except OSError:
            pass
        return
    # Write-then-rename: a reader (the IDE page, ``mini-ork resume``, the
    # attention inbox) must never see a half-written marker, because an empty
    # file reads as a park with no numbers and a truncated one as garbage.
    # ``os.replace`` is atomic on the same filesystem.
    tmp = f"{path}.{os.getpid()}.tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({
                "spent_usd": round(v.spent, 6),
                "budget_usd": v.budget,
                "detail": v.detail,
                "recorded_at": int(time.time()),
            }, fh)
            fh.write("\n")
        os.replace(tmp, path)
    except OSError:
        try:
            os.remove(tmp)
        except OSError:
            pass
        # the gate's refusal does not depend on the marker landing


def read_marker(run_dir: str | os.PathLike | None) -> dict | None:
    """The marker's payload, or ``None`` when there is no marker file.

    A marker that exists but cannot be parsed comes back as ``{}`` — *present
    but unreadable*, which is not the same as absent. Every caller here only
    asks "is there a park?", so ``{}`` keeps answering yes; the numbers in the
    payload are a convenience for the page, never the gate's input.
    """
    path = marker_path(run_dir)
    if not path or not os.path.isfile(path):
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}
