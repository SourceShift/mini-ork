"""The RSI candidate record — the unit of work one cycle reasons about.

A candidate is a *live predicate* over the repo and its state DB, not a
paragraph in an audit doc. Three properties make that work, and everything in
this module exists to protect them.

1. **Identity is the evidence's shape, never its size.** ``evidence_key`` says
   *what* is wrong (``table:learning_pass_stats:empty``,
   ``file:mini_ork/dispatch/routing.py:decide:segment-not-passed``) and never
   carries a row count or a timestamp. A table that grows 10k rows must not mint
   a new candidate, or the ledger's "already fixed" / "already failed" lookups
   never hit. Conversely a moved file or a differently-shaped finding *does*
   change the key — that is the desired behaviour, because it is a new fact.

2. **Severity is integer arithmetic.** no prose and no model. ``severity()``
   takes a kind, a reach, an age, a domain and a blast radius and returns a
   pinned 0..100 integer; the unit tests assert exact values, so a tuning change
   cannot silently reorder the queue.

3. **No I/O.** Building a candidate touches nothing. ``harvest`` supplies
   ``now`` explicitly, so a test can pin severity and the first-seen timestamp
   without freezing the clock.
"""
from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, field, replace
from typing import Any, Iterable, Mapping

#: The three subsystems one cycle harvests. Order is meaningful: it is the
#: tiebreak used by the queue when two candidates share severity and blast.
DOMAINS: tuple[str, ...] = ("learning", "routing", "verification")

#: Base severity by kind. The ordering encodes a judgement worth stating:
#: a producer that was never wired (70) starves a signal more thoroughly than a
#: signal that is produced but dropped (58), which in turn matters more than a
#: metric that merely stopped being refreshed (38).
KIND_BASE: Mapping[str, int] = {
    "producer_missing": 70,
    "data_path_dead": 68,
    "regression": 66,
    "silent_swallow": 62,
    "probe_broken": 60,
    "signal_not_propagated": 58,
    "temporal_corruption": 54,
    "unused_capability": 46,
    # a ranked research technique not yet adopted
    "technique_unadopted": 44,
    "stale_signal": 38,
    "quality_red": 36,
    "coverage_gap": 30,
}

#: Domain criticality bonus. Verification sits highest because the verifier is
#: the only part of the loop that can tell a good patch from a plausible one —
#: a gap there degrades every other domain's evidence.
DOMAIN_CRITICALITY: Mapping[str, int] = {
    "learning": 4,
    "routing": 3,
    "verification": 5,
}

MAX_REACH_BONUS = 15
MAX_PERSISTENCE_BONUS = 10
MAX_BLAST_COST = 8


def severity(
    kind: str,
    *,
    reach: int = 1,
    age_days: int = 0,
    domain: str = "learning",
    blast_radius: int = 1,
) -> int:
    """Return the pinned 0..100 severity for a candidate.

    The terms, in order:

    * ``base`` — ``KIND_BASE[kind]``. An unknown kind raises ``KeyError`` on
      purpose: a typo must fail at harvest, not silently rank at zero.
    * ``reach`` — ``min(15, isqrt(reach - 1))``. Undoing the square root caps
      the term at 15 for 226+ affected rows, so a big table cannot drown the
      base ordering, while 10 rows still outranks 2.
    * ``persistence`` — ``+2`` per full week of age, capped at ``+10``. A gap
      that has survived five weeks is more likely to be structural.
    * ``blast_radius`` — a *cost*, capped at 8, charged per file past the first.
      A six-file fix is riskier to auto-merge than a one-file fix, so it ranks
      lower even though the underlying gap may be worse.
    """
    base = KIND_BASE[kind]
    reach_bonus = min(MAX_REACH_BONUS, math.isqrt(max(0, int(reach) - 1)))
    persistence_bonus = min(MAX_PERSISTENCE_BONUS, 2 * (max(0, int(age_days)) // 7))
    blast_cost = min(MAX_BLAST_COST, 2 * max(0, int(blast_radius) - 1))
    total = base + reach_bonus + persistence_bonus + DOMAIN_CRITICALITY[domain] - blast_cost
    return max(0, min(100, total))


def normalize_path(path: str) -> str:
    """Return a repo-relative POSIX path, or raise on an absolute one.

    The fix scope is matched against ``git diff`` output and against the
    ownership registry, both of which speak repo-relative paths. An absolute
    path here would silently never match, so it is rejected rather than
    rewritten.
    """
    p = path.strip().replace("\\", "/")
    if not p:
        raise ValueError("candidate path is empty")
    if p.startswith("/"):
        raise ValueError(f"candidate path must be repo-relative: {path!r}")
    while p.startswith("./"):
        p = p[2:]
    return p


def signature_for(domain: str, kind: str, evidence_key: str, files: Iterable[str]) -> str:
    """16-hex identity for a candidate. Excludes counts, timestamps and titles."""
    payload = "|".join(
        (domain, kind, evidence_key, ",".join(sorted(normalize_path(f) for f in files)))
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


@dataclass(frozen=True, slots=True)
class Candidate:
    """One gap, with enough context to dispatch a fix and to re-check it later."""

    candidate_id: str
    domain: str
    source: str
    kind: str
    title: str
    detail: str
    severity: int
    files: tuple[str, ...]
    fix_hint: str
    success_metric: str
    success_command: str
    evidence_key: str
    signature: str
    first_seen_at: int
    times_seen: int = 1
    # Excluded from equality: two harvests that observe the same gap with a
    # different row sample are the same candidate, not a new one.
    evidence: dict[str, Any] = field(default_factory=dict, compare=False, repr=False)

    def __post_init__(self) -> None:
        if self.domain not in DOMAINS:
            raise ValueError(f"unknown domain: {self.domain!r}")
        if self.kind not in KIND_BASE:
            raise ValueError(f"unknown kind: {self.kind!r}")
        if not 0 <= self.severity <= 100:
            raise ValueError(f"severity out of range: {self.severity}")
        if self.times_seen < 1:
            raise ValueError("times_seen must be >= 1")

    @property
    def blast_radius(self) -> int:
        return max(1, len(self.files))

    @classmethod
    def build(
        cls,
        *,
        domain: str,
        source: str,
        kind: str,
        title: str,
        evidence_key: str,
        now: int,
        detail: str = "",
        files: Iterable[str] = (),
        evidence: Mapping[str, Any] | None = None,
        fix_hint: str = "",
        success_metric: str = "",
        success_command: str = "",
        reach: int = 1,
        age_days: int = 0,
    ) -> "Candidate":
        """Construct a candidate, deriving its signature, id and severity.

        ``now`` is required rather than defaulted so a test can pin both the
        id (via ``first_seen_at``-independent inputs) and the severity without
        monkeypatching the clock.

        Unlike the bare ``severity()`` helper — which lets a ``KeyError``
        escape so a typo in a harvest rule fails at the lookup — ``build``
        validates first and raises ``ValueError``, because its caller is
        constructing a record and deserves the field name in the message.
        """
        if domain not in DOMAINS:
            raise ValueError(f"unknown domain: {domain!r}")
        if kind not in KIND_BASE:
            raise ValueError(f"unknown kind: {kind!r}")
        scope = tuple(sorted({normalize_path(f) for f in files}))
        sig = signature_for(domain, kind, evidence_key, scope)
        return cls(
            candidate_id="rsi-" + sig,
            domain=domain,
            source=source,
            kind=kind,
            title=title,
            detail=detail,
            severity=severity(
                kind,
                reach=reach,
                age_days=age_days,
                domain=domain,
                blast_radius=max(1, len(scope)),
            ),
            files=scope,
            fix_hint=fix_hint,
            success_metric=success_metric,
            success_command=success_command,
            evidence_key=evidence_key,
            signature=sig,
            first_seen_at=int(now),
            evidence=dict(evidence or {}),
        )

    def seen(self) -> "Candidate":
        """Return a copy with ``times_seen`` advanced. ``first_seen_at`` is kept."""
        return replace(self, times_seen=self.times_seen + 1)

    def to_dict(self) -> dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "domain": self.domain,
            "source": self.source,
            "kind": self.kind,
            "title": self.title,
            "detail": self.detail,
            "severity": self.severity,
            "files": list(self.files),
            "fix_hint": self.fix_hint,
            "success_metric": self.success_metric,
            "success_command": self.success_command,
            "evidence_key": self.evidence_key,
            "signature": self.signature,
            "first_seen_at": self.first_seen_at,
            "times_seen": self.times_seen,
            "evidence": self.evidence,
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "Candidate":
        return cls(
            candidate_id=str(raw["candidate_id"]),
            domain=str(raw["domain"]),
            source=str(raw["source"]),
            kind=str(raw["kind"]),
            title=str(raw.get("title", "")),
            detail=str(raw.get("detail", "")),
            severity=int(raw["severity"]),
            files=tuple(str(f) for f in raw.get("files", ())),
            fix_hint=str(raw.get("fix_hint", "")),
            success_metric=str(raw.get("success_metric", "")),
            success_command=str(raw.get("success_command", "")),
            evidence_key=str(raw["evidence_key"]),
            signature=str(raw["signature"]),
            first_seen_at=int(raw["first_seen_at"]),
            times_seen=int(raw.get("times_seen", 1)),
            evidence=dict(raw.get("evidence") or {}),
        )

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), sort_keys=True)

    @classmethod
    def from_json(cls, text: str) -> "Candidate":
        return cls.from_dict(json.loads(text))


def dedupe(candidates: Iterable[Candidate]) -> list[Candidate]:
    """Collapse same-signature candidates, keeping the most severe and the union of evidence.

    Two harvest rules can describe one gap — ``decide()`` called without
    ``segment=`` and 83% of rows having ``code_region IS NULL`` are the same
    defect seen from the source side and the data side. They share an
    ``evidence_key`` by construction, so they share a signature and merge here
    rather than occupying two queue slots.
    """
    by_sig: dict[str, Candidate] = {}
    for cand in candidates:
        prev = by_sig.get(cand.signature)
        if prev is None:
            by_sig[cand.signature] = cand
            continue
        keep, drop = (cand, prev) if cand.severity > prev.severity else (prev, cand)
        merged_evidence = {**drop.evidence, **keep.evidence}
        by_sig[cand.signature] = replace(keep, evidence=merged_evidence)
    return list(by_sig.values())
