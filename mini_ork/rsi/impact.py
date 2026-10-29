"""The statistics core of the G12 impact gate.

This module is deliberately pure: it performs no file, database, network,
subprocess or model access, reads no environment variable, and has no
module-level side effects. It only reasons over the mappings and sequences it
is handed, so the research-driven RSI loop can decide whether a candidate
change really improved a benchmark without touching the tree itself.

Scores are per-item floats; an item *passes* when ``score >= threshold``
(default ``0.5``). Item ids are strings.
"""

import hashlib
import hmac
import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

SPEND_Z = 3.39
SPENDING_MODES = ("horizon_free", "per_decision")
STRATA_DEFAULT = ""


@dataclass(frozen=True, slots=True)
class PairedTable:
    """Paired pass/fail counts between a base tree and a candidate head tree."""

    wins: int
    losses: int
    both_pass: int
    both_fail: int
    missing: int

    @property
    def n(self) -> int:
        """Items scored in both trees (``missing`` excluded)."""
        return self.wins + self.losses + self.both_pass + self.both_fail

    @property
    def base_passed(self) -> int:
        """Items the base tree passed."""
        return self.losses + self.both_pass

    @property
    def discordant(self) -> int:
        """Items where base and head disagree (the McNemar pairs)."""
        return self.wins + self.losses

    def to_dict(self) -> dict[str, int]:
        return {
            "wins": self.wins,
            "losses": self.losses,
            "both_pass": self.both_pass,
            "both_fail": self.both_fail,
            "missing": self.missing,
            "n": self.n,
            "base_passed": self.base_passed,
            "discordant": self.discordant,
        }


def paired_counts(
    base: Mapping[str, float],
    head: Mapping[str, float],
    *,
    items: Iterable[str] | None = None,
    threshold: float = 0.5,
) -> PairedTable:
    """Cross-tabulate pass/fail between ``base`` and ``head``.

    The universe is ``items`` (de-duplicated) when given, else the sorted union
    of both mappings' keys. An id missing from either side is counted in
    ``missing`` and otherwise skipped.
    """
    if items is None:
        universe: list[str] = sorted(set(base) | set(head))
    else:
        universe = list(dict.fromkeys(items))

    wins = losses = both_pass = both_fail = missing = 0
    for item in universe:
        if item not in base or item not in head:
            missing += 1
            continue
        base_ok = base[item] >= threshold
        head_ok = head[item] >= threshold
        if base_ok and head_ok:
            both_pass += 1
        elif base_ok and not head_ok:
            losses += 1
        elif not base_ok and head_ok:
            wins += 1
        else:
            both_fail += 1
    return PairedTable(wins, losses, both_pass, both_fail, missing)


def binom_tail(k: int, n: int, q: float) -> float:
    """Exact ``P[Bin(n, q) >= k]`` via ``math.comb``."""
    if n < 0 or not 0.0 <= q <= 1.0:
        raise ValueError(f"invalid binomial parameters: n={n!r}, q={q!r}")
    if k <= 0:
        return 1.0
    if k > n:
        return 0.0
    total = sum(
        math.comb(n, i) * q**i * (1.0 - q) ** (n - i) for i in range(k, n + 1)
    )
    return min(1.0, max(0.0, total))


def look_alpha(
    alpha0: float,
    look: int,
    *,
    spending: str = "horizon_free",
) -> float:
    """Per-look significance level under the requested alpha-spending mode."""
    if not 0 < alpha0 < 1:
        raise ValueError(f"alpha0 must be in (0, 1): {alpha0!r}")
    if look < 1:
        raise ValueError(f"look must be >= 1: {look!r}")
    if spending not in SPENDING_MODES:
        raise ValueError(f"unknown spending mode: {spending!r}")
    if spending == "per_decision":
        return alpha0
    return alpha0 / (SPEND_Z * look * math.log2(look + 1))


def sage_decision(
    table: PairedTable,
    *,
    alpha: float,
    lam: float = 1.0,
    max_regression_rate: float = 0.0,
) -> dict[str, Any]:
    """Decide whether a paired table is a real, budget-respecting improvement."""
    if lam <= 0:
        raise ValueError(f"lam must be > 0: {lam!r}")
    if not 0 < alpha < 1:
        raise ValueError(f"alpha must be in (0, 1): {alpha!r}")
    if max_regression_rate < 0:
        raise ValueError(
            f"max_regression_rate must be >= 0: {max_regression_rate!r}"
        )

    q = lam / (1.0 + lam)
    delta = table.wins - lam * table.losses
    p_value = (
        binom_tail(table.wins, table.discordant, q)
        if table.discordant > 0
        else 1.0
    )

    if table.base_passed > 0:
        regression_rate = table.losses / table.base_passed
    elif table.losses == 0:
        regression_rate = 0.0
    else:
        regression_rate = 1.0

    if table.discordant == 0:
        reason = "no_discordant_pairs"
    elif regression_rate > max_regression_rate:
        reason = "regression_budget"
    elif delta <= 0:
        reason = "net_loss"
    elif p_value >= alpha:
        reason = "not_significant"
    else:
        reason = "accepted"

    return {
        "accept": reason == "accepted",
        "reason": reason,
        "wins": table.wins,
        "losses": table.losses,
        "delta": round(delta, 6),
        "p_value": round(p_value, 6),
        "alpha": round(alpha, 6),
        "lam": lam,
        "q": round(q, 6),
        "regression_rate": round(regression_rate, 6),
        "n": table.n,
        "base_passed": table.base_passed,
    }


def stable_items(
    runs: Sequence[Mapping[str, float]],
    *,
    threshold: float = 0.5,
) -> tuple[frozenset[str], frozenset[str]]:
    """Partition ids present in every run into ``(stable, flaky)``.

    An id is stable when its pass/fail is identical across every run; ids
    absent from any run are in neither set.
    """
    runs = list(runs)
    if not runs:
        return frozenset(), frozenset()

    common = set(runs[0])
    for run in runs[1:]:
        common &= set(run)

    stable: set[str] = set()
    flaky: set[str] = set()
    for item in common:
        outcomes = [run[item] >= threshold for run in runs]
        if all(outcomes) or not any(outcomes):
            stable.add(item)
        else:
            flaky.add(item)
    return frozenset(stable), frozenset(flaky)


def _split_key(secret: bytes, epoch: int, item: str) -> int:
    digest = hmac.new(secret, f"{epoch}:{item}".encode(), hashlib.sha256).digest()
    return int.from_bytes(digest[:8], "big")


def split_items(
    items: Iterable[str],
    *,
    secret: bytes,
    epoch: int,
    dev_fraction: float = 0.5,
    strata: Mapping[str, str] | None = None,
) -> dict[str, list[str]]:
    """Deterministically split items into ``dev`` and ``sealed`` strata-aware."""
    if not secret:
        raise ValueError("secret must be non-empty")
    if not 0 < dev_fraction < 1:
        raise ValueError(f"dev_fraction must be in (0, 1): {dev_fraction!r}")

    groups: dict[str, list[str]] = {}
    seen: set[str] = set()
    for item in items:
        if item in seen:
            continue
        seen.add(item)
        stratum = STRATA_DEFAULT if strata is None else strata.get(item, STRATA_DEFAULT)
        groups.setdefault(stratum, []).append(item)

    dev: list[str] = []
    sealed: list[str] = []
    for group in groups.values():
        ordered = sorted(group, key=lambda x: (_split_key(secret, epoch, x), x))
        cut = round(len(ordered) * dev_fraction)
        dev.extend(ordered[:cut])
        sealed.extend(ordered[cut:])
    return {"dev": sorted(dev), "sealed": sorted(sealed)}


def guard_check(
    base: Mapping[str, float],
    head: Mapping[str, float],
    *,
    tolerances: Mapping[str, float],
    lower_is_better: Iterable[str] = (),
) -> dict[str, Any]:
    """Flag every guarded metric that got missing or worse than its tolerance."""
    for metric, tolerance in tolerances.items():
        if tolerance < 0:
            raise ValueError(f"tolerance for {metric!r} must be >= 0: {tolerance!r}")

    lower = set(lower_is_better)
    violations: list[dict[str, Any]] = []
    for metric in sorted(tolerances):
        if metric not in base or metric not in head:
            violations.append({"metric": metric, "reason": "missing"})
            continue
        if metric in lower:
            worse = head[metric] - base[metric]
        else:
            worse = base[metric] - head[metric]
        if worse > tolerances[metric]:
            violations.append(
                {
                    "metric": metric,
                    "reason": "worse",
                    "base": base[metric],
                    "head": head[metric],
                    "tolerance": tolerances[metric],
                }
            )
    return {"ok": not violations, "violations": violations}


def ratchet(
    history: Sequence[float],
    *,
    drop: float = 0.02,
    consecutive: int = 3,
) -> dict[str, Any]:
    """Detect a trailing decline below the peak of a canary-score history."""
    if consecutive < 1:
        raise ValueError(f"consecutive must be >= 1: {consecutive!r}")

    values = list(history)
    if not values:
        return {
            "peak": None,
            "peak_index": None,
            "declines": 0,
            "revert": False,
            "n": 0,
        }

    peak = max(values)
    peak_index = values.index(peak)
    declines = 0
    for index in range(len(values) - 1, peak_index, -1):
        if values[index] < peak - drop:
            declines += 1
        else:
            break
    return {
        "peak": peak,
        "peak_index": peak_index,
        "declines": declines,
        "revert": declines >= consecutive,
        "n": len(values),
    }


def effective_families(labels: Sequence[str]) -> float:
    """Effective number of families: ``exp`` of the label-frequency entropy."""
    labels = list(labels)
    if not labels:
        return 0.0
    counts: dict[str, int] = {}
    for label in labels:
        counts[label] = counts.get(label, 0) + 1
    total = len(labels)
    entropy = 0.0
    for count in counts.values():
        p = count / total
        entropy -= p * math.log(p)
    return round(math.exp(entropy), 6)


def family_collapse(
    labels: Sequence[str],
    accepted: Sequence[bool],
    *,
    window: int = 6,
    n_eff_max: float = 1.5,
) -> dict[str, Any]:
    """Detect a plateau of rejected, low-diversity family labels."""
    labels = list(labels)
    accepted = list(accepted)
    if len(labels) != len(accepted):
        raise ValueError(
            f"labels and accepted differ in length: {len(labels)} != {len(accepted)}"
        )

    recent_labels = labels[-window:]
    recent_accepted = accepted[-window:]
    plateau = len(labels) >= window and not any(recent_accepted)
    n_eff = effective_families(recent_labels)
    narrow = n_eff <= n_eff_max
    return {
        "collapse": plateau and narrow,
        "plateau": plateau,
        "narrow": narrow,
        "n_eff": n_eff,
        "window": window,
        "recent_families": sorted(set(recent_labels)),
    }


def thompson_order(
    options: Sequence[Mapping[str, Any]],
    attempts: Sequence[Mapping[str, Any]],
    *,
    rng: Any,
    tau: float = 1.0,
    prior_weight: float = 2.0,
) -> list[str]:
    """Order options by a clade-aware Thompson draw (descending draw, then id)."""
    if tau <= 0:
        raise ValueError(f"tau must be > 0: {tau!r}")
    if prior_weight < 0:
        raise ValueError(f"prior_weight must be >= 0: {prior_weight!r}")

    options = list(options)
    if not options:
        return []
    attempts = list(attempts)

    scored: list[tuple[str, float]] = []
    for option in options:
        prior = min(1.0, max(0.0, option["prior"]))
        successes = sum(
            1
            for attempt in attempts
            if attempt["family"] == option["family"] and attempt["success"]
        )
        successes += sum(
            1
            for attempt in attempts
            if attempt["technique"] == option["id"] and attempt["success"]
        )
        failures = sum(
            1
            for attempt in attempts
            if attempt["family"] == option["family"] and not attempt["success"]
        )
        failures += sum(
            1
            for attempt in attempts
            if attempt["technique"] == option["id"] and not attempt["success"]
        )
        a = tau * (1 + successes + prior_weight * prior)
        b = tau * (1 + failures + prior_weight * (1 - prior))
        scored.append((option["id"], rng.betavariate(a, b)))

    scored.sort(key=lambda pair: (-pair[1], pair[0]))
    return [option_id for option_id, _ in scored]


__all__ = [
    "PairedTable",
    "SPENDING_MODES",
    "SPEND_Z",
    "STRATA_DEFAULT",
    "binom_tail",
    "effective_families",
    "family_collapse",
    "guard_check",
    "look_alpha",
    "paired_counts",
    "ratchet",
    "sage_decision",
    "split_items",
    "stable_items",
    "thompson_order",
]
