"""mini_ork.certify — the solve-time oracle.

Repo-agnostic, importable layer that ports the solve-time oracle engine into
the package. Downstream slice C2 (`mini-ork certify`) will call `judge` from
here. Slice C1 (this slice) adds NO CLI surface.

    from mini_ork.certify import judge, Verdict, PROVEN, REFUTED, UNVERIFIED

Pattern mirrors `mini_ork.runtime.__init__`: explicit re-exports + `__all__`,
never `from .oracle import *`.
"""
from __future__ import annotations

from mini_ork.certify.oracle import judge
from mini_ork.certify.verdict import (
    PROVEN,
    REFUTED,
    UNVERIFIED,
    Verdict,
)

__all__ = ["judge", "Verdict", "PROVEN", "REFUTED", "UNVERIFIED"]