"""Solve-time oracle verdicts.

Three string constants and a dataclass. The strings are uppercased, no
qualifiers — they are the load-bearing key for the framework-edit verdict
envelope (`recipes/framework-edit/verifiers/_verdict_merge.py` keys on these
exact literals; introducing synonyms silently breaks the merge).
"""
from __future__ import annotations

from dataclasses import dataclass, field

PROVEN = "PROVEN"
REFUTED = "REFUTED"
UNVERIFIED = "UNVERIFIED"


@dataclass
class Verdict:
    """What the oracle decided — and the trail that produced it.

    `detail` carries the per-invariant evidence so downstream tooling can audit
    the call without re-running anything.
    """

    verdict: str
    reason: str
    poc_plus: str | None = None
    mr_pass_rate: float | None = None
    mr_n: int = 0
    detail: dict = field(default_factory=dict)