"""Turn a harvest into one ranked, de-duplicated queue. Pure — no I/O.

Three policies meet here, and each one exists to stop a specific failure:

* **Avoidance.** A gap already merged, already failed, or already claimed must
  not be dispatched again. That decision needs the ledger, but the ledger is
  SQLite — so the caller reads it and hands over a :class:`LedgerView`. The
  queue never opens a connection, which is what lets the whole ordering be
  tested against a dict.
* **Seeding.** Ranking purely by severity lets one loud domain (verification
  produces the most candidates) starve the other two. The order is therefore
  ``[global max]`` then one candidate per domain, then the remainder by
  severity. The first element is still the single worst gap anywhere, so
  "one ranked queue" is honoured literally.
* **Credit.** A source whose fixes never merge should rank lower — but only
  after at least ``CREDIT_MIN_N`` cycles, and only by a bounded amount. Credit
  is evidence about a *source*, not about a gap; letting it dominate would let
  two unlucky cycles bury a real defect.

Ordering is a total order — ``(-effective_severity, blast_radius, domain_order,
candidate_id)`` — with no randomness and no ``now``-dependent inputs beyond the
caller-supplied timestamp, so two runs over the same inputs print the same
table.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Sequence

from .candidates import DOMAINS, Candidate, dedupe

#: Rejection codes. Stable strings because they are written to ``selection.json``
#: and read by operators and, later, by the lesson author.
REJECT_CODES: tuple[str, ...] = (
    "already_merged",
    "failed_signature",
    "already_claimed",
    "low_severity",
    "domain_cap",
    "domain_filtered",
)

DEFAULT_MAX_CANDIDATES = 3
DEFAULT_PER_DOMAIN_MAX = 5
DEFAULT_MIN_SEVERITY = 40
DEFAULT_FAILED_TTL_DAYS = 30
DEFAULT_SEVERITY_RISE = 15
CREDIT_MIN_N = 3
#: Points added at a 1.0 merge rate and subtracted at 0.0. Small on purpose.
CREDIT_SPAN = 4

_DAY = 86400


@dataclass(frozen=True, slots=True)
class QueueConfig:
    """The knobs from ``config/rsi-cycle.yaml``'s ``queue:`` block."""

    max_candidates: int = DEFAULT_MAX_CANDIDATES
    per_domain_max: int = DEFAULT_PER_DOMAIN_MAX
    min_severity: int = DEFAULT_MIN_SEVERITY
    failed_signature_ttl_days: int = DEFAULT_FAILED_TTL_DAYS
    severity_rise: int = DEFAULT_SEVERITY_RISE
    credit_min_n: int = CREDIT_MIN_N

    def __post_init__(self) -> None:
        if self.max_candidates < 1:
            raise ValueError("queue.max_candidates must be >= 1")
        if self.per_domain_max < 1:
            raise ValueError("queue.per_domain_max must be >= 1")
        if not 0 <= self.min_severity <= 100:
            raise ValueError("queue.min_severity must be 0..100")
        if self.failed_signature_ttl_days < 0:
            raise ValueError("queue.failed_signature_ttl_days must be >= 0")

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any] | None) -> "QueueConfig":
        """Build from a YAML mapping, ignoring unknown keys.

        Unknown keys are ignored rather than rejected here because the same
        mapping carries the rest of the cycle config; the *config* loader is
        where an unknown key is an error. This class only claims its own keys.
        """
        raw = dict(raw or {})
        known = {
            "max_candidates",
            "per_domain_max",
            "min_severity",
            "failed_signature_ttl_days",
            "severity_rise",
            "credit_min_n",
        }
        kwargs = {k: int(v) for k, v in raw.items() if k in known and v is not None}
        return cls(**kwargs)


@dataclass(frozen=True, slots=True)
class LedgerView:
    """What the queue needs to know about the ledger, read by the caller.

    ``by_signature`` is the avoidance lookup (``ledger.candidate_by_signature``
    for each signature, or one bulk read). ``merged_ancestor_signatures`` is
    already *filtered*: the caller has checked that each merged row's
    ``merged_sha`` is an ancestor of ``origin/main``. A merged-but-reverted gap
    legitimately returns to the queue, and that distinction needs git, which
    this module must not touch.
    """

    by_signature: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    claimed_ids: frozenset[str] = frozenset()
    merged_ancestor_signatures: frozenset[str] = frozenset()


@dataclass(frozen=True, slots=True)
class QueueItem:
    candidate: Candidate
    effective_severity: int
    rank: int
    seed: str = ""  # "global-max" | "seed:<domain>" | ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "candidate_id": self.candidate.candidate_id,
            "domain": self.candidate.domain,
            "kind": self.candidate.kind,
            "title": self.candidate.title,
            "severity": self.candidate.severity,
            "effective_severity": self.effective_severity,
            "rank": self.rank,
            "seed": self.seed,
            "files": list(self.candidate.files),
            "source": self.candidate.source,
        }


@dataclass(frozen=True, slots=True)
class Rejection:
    candidate_id: str
    signature: str
    code: str
    detail: str = ""

    def __post_init__(self) -> None:
        if self.code not in REJECT_CODES:
            raise ValueError(f"unknown reject code: {self.code!r}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "signature": self.signature,
            "code": self.code,
            "detail": self.detail,
        }


@dataclass(frozen=True, slots=True)
class QueuePlan:
    """The selected few, the ordered remainder, and every reason for a drop."""

    selected: tuple[QueueItem, ...] = ()
    backlog: tuple[QueueItem, ...] = ()
    rejected: tuple[Rejection, ...] = ()
    #: Domains that produced candidates but had none eligible afterwards.
    holes: tuple[str, ...] = ()
    #: Candidates the harvest produced, before any filter.
    examined: int = 0

    @property
    def top(self) -> Candidate | None:
        return self.selected[0].candidate if self.selected else None

    def find(self, candidate_id: str) -> QueueItem | None:
        for item in (*self.selected, *self.backlog):
            if item.candidate.candidate_id == candidate_id:
                return item
        return None

    def rejections_for(self, code: str) -> tuple[Rejection, ...]:
        return tuple(r for r in self.rejected if r.code == code)

    def to_dict(self) -> dict[str, Any]:
        return {
            "examined": self.examined,
            "selected": [i.to_dict() for i in self.selected],
            "backlog": [i.to_dict() for i in self.backlog],
            "rejected": [r.to_dict() for r in self.rejected],
            "holes": list(self.holes),
        }


# --------------------------------------------------------------------------
# Credit
# --------------------------------------------------------------------------


def credit_for_source(
    credits: Mapping[tuple[str, str], Mapping[str, Any]] | None,
    source: str,
) -> dict[str, Any]:
    """Aggregate ``ledger.credit_stats()`` across lenses for one source."""
    n = merged = 0
    for key, stats in (credits or {}).items():
        if key[0] != source:
            continue
        n += int(stats.get("n", 0))
        merged += int(stats.get("merged", 0))
    return {"n": n, "merged": merged, "rate": (merged / n) if n else 0.0}


def credit_adjustment(
    credits: Mapping[tuple[str, str], Mapping[str, Any]] | None,
    source: str,
    *,
    min_n: int = CREDIT_MIN_N,
) -> int:
    """Bounded severity bonus/penalty from a source's historical merge rate.

    ``+CREDIT_SPAN`` at a 1.0 merge rate, ``-CREDIT_SPAN`` at 0.0, zero before
    ``min_n`` observations. The threshold is what keeps the first two cycles
    from reordering the queue on noise.
    """
    agg = credit_for_source(credits, source)
    n, merged = int(agg["n"]), int(agg["merged"])
    if n < max(1, int(min_n)):
        return 0
    # rate - 0.5 == (2*merged - n) / (2n); scaled by CREDIT_SPAN it is this.
    return int(round(CREDIT_SPAN * (2 * merged - n) / n))


def effective_severity(
    cand: Candidate,
    credits: Mapping[tuple[str, str], Mapping[str, Any]] | None = None,
    *,
    min_n: int = CREDIT_MIN_N,
) -> int:
    """Severity after credit, clamped to the record's own 0..100 range."""
    return max(0, min(100, cand.severity + credit_adjustment(credits, cand.source, min_n=min_n)))


# --------------------------------------------------------------------------
# Filtering
# --------------------------------------------------------------------------


def _age_days(now: int, stamp: Any) -> float:
    try:
        return max(0.0, (int(now) - int(stamp)) / _DAY)
    except (TypeError, ValueError):
        return 0.0


def avoidance(
    cand: Candidate,
    view: LedgerView,
    cfg: QueueConfig,
    *,
    now: int,
) -> Rejection | None:
    """The "don't spend twice" filter. Order matters: cheapest reason wins."""
    if cand.candidate_id in view.claimed_ids:
        return Rejection(cand.candidate_id, cand.signature, "already_claimed",
                         "a live cycle holds this candidate")
    if cand.signature in view.merged_ancestor_signatures:
        return Rejection(cand.candidate_id, cand.signature, "already_merged",
                         "merged and present in origin/main")

    row = view.by_signature.get(cand.signature)
    if not row:
        return None
    status = str(row.get("status") or "")
    if status not in ("rejected", "quarantined"):
        return None
    age = _age_days(now, row.get("updated_at") or 0)
    if age >= cfg.failed_signature_ttl_days:
        return None
    previous = row.get("severity")
    rose = False
    if previous is not None:
        rose = cand.severity - int(previous) >= cfg.severity_rise
    if rose:
        return None
    return Rejection(
        cand.candidate_id,
        cand.signature,
        "failed_signature",
        f"{status} {age:.1f}d ago; severity rose "
        f"{cand.severity - int(previous or 0)} < {cfg.severity_rise}",
    )


def _order_key(item: QueueItem) -> tuple[int, int, int, str]:
    return (
        -item.effective_severity,
        item.candidate.blast_radius,
        DOMAINS.index(item.candidate.domain),
        item.candidate.candidate_id,
    )


def _seed_order(items: Sequence[QueueItem]) -> list[QueueItem]:
    """Global max first, then one per domain, then everything else by severity.

    The first slot is the single worst gap in any domain — "one ranked queue"
    read literally. The second pass exists only so a quiet domain still gets a
    slot before the backlog is drained by severity alone.
    """
    if not items:
        return []
    ordered = sorted(items, key=_order_key)
    seeded: list[tuple[QueueItem, str]] = [(ordered[0], "global-max")]
    used = {ordered[0].candidate.candidate_id}
    # The global max already represents its own domain, so the seed pass is one
    # slot per *unrepresented* domain. Taking a second candidate from the
    # loudest domain here would push a quiet domain out whenever
    # max_candidates equals the number of domains.
    represented = {ordered[0].candidate.domain}

    for domain in DOMAINS:
        if domain in represented:
            continue
        for item in ordered:
            cid = item.candidate.candidate_id
            if cid in used or item.candidate.domain != domain:
                continue
            seeded.append((item, f"seed:{domain}"))
            used.add(cid)
            represented.add(domain)
            break

    seeded.extend((i, "") for i in ordered if i.candidate.candidate_id not in used)
    return [
        QueueItem(candidate=item.candidate, effective_severity=item.effective_severity,
                  rank=rank, seed=seed)
        for rank, (item, seed) in enumerate(seeded)
    ]


def build_queue(
    candidates: Iterable[Candidate],
    *,
    now: int,
    view: LedgerView | None = None,
    cfg: QueueConfig | None = None,
    credits: Mapping[tuple[str, str], Mapping[str, Any]] | None = None,
    domains: Sequence[str] | None = None,
) -> QueuePlan:
    """Dedupe, filter, cap per domain, seed, and cut to ``max_candidates``."""
    view = view or LedgerView()
    cfg = cfg or QueueConfig()
    allowed = tuple(domains) if domains else DOMAINS
    unknown = [d for d in allowed if d not in DOMAINS]
    if unknown:
        raise ValueError(f"unknown domain filter: {unknown}")

    unique = dedupe(candidates)
    rejected: list[Rejection] = []
    eligible: list[QueueItem] = []
    produced: dict[str, int] = {d: 0 for d in DOMAINS}

    for cand in unique:
        produced[cand.domain] = produced.get(cand.domain, 0) + 1
        if cand.domain not in allowed:
            rejected.append(Rejection(cand.candidate_id, cand.signature,
                                      "domain_filtered", f"domain {cand.domain} not selected"))
            continue
        avoid = avoidance(cand, view, cfg, now=now)
        if avoid is not None:
            rejected.append(avoid)
            continue
        eff = effective_severity(cand, credits, min_n=cfg.credit_min_n)
        if eff < cfg.min_severity:
            rejected.append(Rejection(cand.candidate_id, cand.signature, "low_severity",
                                      f"effective severity {eff} < {cfg.min_severity}"))
            continue
        eligible.append(QueueItem(candidate=cand, effective_severity=eff, rank=0))

    # Per-domain cap: keep the best N of each domain, on the same total order.
    per_domain: dict[str, list[QueueItem]] = {d: [] for d in DOMAINS}
    for item in sorted(eligible, key=_order_key):
        bucket = per_domain[item.candidate.domain]
        if len(bucket) < cfg.per_domain_max:
            bucket.append(item)
        else:
            rejected.append(Rejection(
                item.candidate.candidate_id, item.candidate.signature, "domain_cap",
                f"{item.candidate.domain} already has {cfg.per_domain_max} of "
                f"{produced[item.candidate.domain]} candidates",
            ))

    ordered = _seed_order([i for bucket in per_domain.values() for i in bucket])
    selected = tuple(ordered[: cfg.max_candidates])
    backlog = tuple(ordered[cfg.max_candidates:])

    holes = tuple(
        d for d in DOMAINS
        if produced.get(d, 0) and not per_domain[d]
    )
    return QueuePlan(
        selected=selected,
        backlog=backlog,
        rejected=tuple(sorted(rejected, key=lambda r: (r.code, r.candidate_id))),
        holes=holes,
        examined=len(unique),
    )


__all__ = [
    "CREDIT_MIN_N",
    "CREDIT_SPAN",
    "DEFAULT_FAILED_TTL_DAYS",
    "DEFAULT_MAX_CANDIDATES",
    "DEFAULT_MIN_SEVERITY",
    "DEFAULT_PER_DOMAIN_MAX",
    "REJECT_CODES",
    "LedgerView",
    "QueueConfig",
    "QueueItem",
    "QueuePlan",
    "Rejection",
    "avoidance",
    "build_queue",
    "credit_adjustment",
    "credit_for_source",
    "effective_severity",
]
