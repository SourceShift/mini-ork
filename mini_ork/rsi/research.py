"""Turn a ranked technique review into RSI candidates.

The verification-technique-review recipe already does the expensive part: it
collects the literature for a domain, clusters the techniques into families
and writes a ranked ``merged-review.json`` into a run directory. Nothing read
that file. This module is the reader: it finds the newest review for a domain
and turns its top ranked techniques into ``rsi.candidates.Candidate`` records
of a new kind, ``technique_unadopted``.

Nothing here touches the DB, the network, a subprocess or a model — a review
file and a clock in are enough. The candidate's *identity* is the technique
name (``technique:<slug>``), never the review's row id, so a re-review that
renumbers or regroups the same technique does not mint a new candidate and the
queue's "already fixed" lookup keeps hitting.

The candidate's severity is therefore a statement about the technique's
priority, not about the review: ``44 + isqrt(reach - 1) + 5`` for the
verification domain, where the reach term squares the row's priority so a
high-priority technique outranks the noise without drowning the base order.
"""
from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from .candidates import Candidate

__all__ = [
    "DEFAULT_SPECS",
    "DEFAULT_VERDICTS",
    "DomainSpec",
    "KIND",
    "MAX_TEXT",
    "ResearchHarvest",
    "eligible",
    "fix_hint",
    "harvest_all",
    "harvest_domain",
    "latest_review",
    "load_ranked",
    "slug",
    "technique_candidates",
]

#: The candidate kind this module produces. Registered in
#: ``candidates.KIND_BASE`` so the severity arithmetic stays in one place.
KIND = "technique_unadopted"

#: The verdicts worth acting on. ``watch`` and ``skip`` are a reviewer's
#: parking space, not a work order.
DEFAULT_VERDICTS = ("implement-now", "next")

#: The longest free-text field this module will quote back.
MAX_TEXT = 1200

#: How many evidence ids a candidate quotes.
_MAX_PAPERS = 12

#: The slug a technique name is reduced to.
_SLUG_MAX = 64

#: Characters outside this set become a single dash in a slug.
_NON_SLUG = re.compile(r"[^a-z0-9]+")


@dataclass(frozen=True, slots=True)
class DomainSpec:
    """Where one domain's reviews live and what its techniques are measured against.

    ``files`` is the one-file blast radius a candidate is charged for;
    ``surface`` is the wider set of paths a technique may legitimately touch —
    the fix hint quotes it so an implementer does not "fix" a technique
    somewhere the benchmark cannot see it.
    """

    domain: str
    review_glob: str
    benchmark: str
    files: tuple[str, ...]
    surface: tuple[str, ...]
    recipe: str = ""


#: The domains this module knows out of the box. Exactly one for now: the
#: verification review recipe is the only one that writes a ranked review.
DEFAULT_SPECS: Mapping[str, DomainSpec] = {
    "verification": DomainSpec(
        domain="verification",
        review_glob="runs/verify-*/merged-review.json",
        benchmark="verifier-replay",
        files=("recipes/code-fix/verifiers/test.py",),
        surface=(
            "recipes/code-fix/verifiers",
            "mini_ork/certify",
            "mini_ork/verify",
            "mini_ork/gates/suite_adequacy.py",
            "mini_ork/gates/suite_strengthen.py",
        ),
        recipe="verification-technique-review",
    ),
}


def latest_review(home: Path, review_glob: str) -> Path | None:
    """Return the newest review file under ``home`` matching ``review_glob``.

    Only files count: a directory that happens to be named ``merged-review.json``
    is not a review. Ties on mtime break to the lexicographically greatest
    path, so the answer does not depend on the filesystem's directory order.
    """
    found = [p for p in sorted(Path(home).glob(review_glob)) if p.is_file()]
    if not found:
        return None
    return max(found, key=lambda p: (p.stat().st_mtime, str(p)))


def load_ranked(path: Path) -> list[dict]:
    """Return the ``ranked`` rows of a review file, keeping only the dict ones.

    A review is written by another recipe and may be malformed; a payload that
    is not an object, or an object without a ranked list, is a hard error the
    caller reports rather than an empty harvest that looks like "nothing new".
    """
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"review payload is not an object: {path}")
    rows = payload.get("ranked")
    if not isinstance(rows, list):
        raise ValueError(f"review payload has no ranked list: {path}")
    return [row for row in rows if isinstance(row, dict)]


def slug(text: str) -> str:
    """Reduce a technique name to a short, stable, filename-safe handle.

    The handle is part of the candidate's identity, so the same technique must
    always reduce to the same slug: lowercase, every run of punctuation
    collapsed to one dash, and never longer than 64 characters.
    """
    out = _NON_SLUG.sub("-", str(text or "").lower()).strip("-")
    if len(out) > _SLUG_MAX:
        out = out[:_SLUG_MAX]
        if out.endswith("-"):
            out = out[:-1]
    return out or "technique"


def _clean(text: Any, limit: int = MAX_TEXT) -> str:
    """Trim, unquote and bound one field of a review row.

    The review writes a quoted phrase as ``'"Does a thing."'``; the quotes are
    the writer's punctuation, not the technique's, so one leading and one
    trailing quote come off.
    """
    out = str(text or "").strip()
    if out.startswith('"'):
        out = out[1:]
    if out.endswith('"'):
        out = out[:-1]
    out = out.strip()
    if len(out) > limit:
        out = out[:limit] + "…"
    return out


def _number(value: Any) -> float:
    """A finite float for a possibly malformed review number.

    A row's ``priority`` may be a string, ``None`` or a float that is not a
    number at all. None of those may raise out of the harvest: an unreadable
    priority counts as zero, which ranks the row last instead of failing the
    whole domain.
    """
    try:
        num = float(value or 0)
    except (TypeError, ValueError):
        return 0.0
    if not math.isfinite(num):
        return 0.0
    return num


def _rank(row: Mapping) -> int:
    """The row's rank, with a missing or malformed one sorting last."""
    raw = row.get("rank")
    if not raw:
        return 10 ** 6
    try:
        return int(raw)
    except (TypeError, ValueError):
        return 10 ** 6


def _papers(row: Mapping) -> list[str]:
    """The evidence ids of a row, capped at the twelve a hint may quote."""
    raw = row.get("evidence")
    if not isinstance(raw, (list, tuple)):
        return []
    return [str(paper) for paper in raw[:_MAX_PAPERS]]


def fix_hint(row: Mapping, spec: DomainSpec) -> str:
    """The task text an implementer receives for one technique.

    It names the technique, its mechanism and its evidence, and it states the
    benchmark and the surface the change is measured inside — a technique
    applied outside the benchmark's surface cannot show a gain, and the
    implementer is the only one who can act on that.
    """
    technique = _clean(row.get("technique"), 200)
    evidence = ", ".join(_papers(row)) or "none"
    surface = ", ".join(spec.surface)
    return "\n".join(
        [
            f"Adopt research technique {_clean(row.get('id'), 40)} — {technique}.",
            f"Mechanism: {_clean(row.get('mechanism'))}",
            f"Adoption sketch: {_clean(row.get('adoption_sketch'))}",
            f"Evidence: {evidence}",
            f"This change is measured by the `{spec.benchmark}` benchmark, which exercises only: "
            f"{surface}. Put the change inside that surface, or it cannot show a gain.",
            "Implement the mechanism as described. Do not edit tests that pin existing behaviour, "
            "`evals/`, or the benchmark.",
        ]
    )


def eligible(
    row: Mapping,
    *,
    verdicts: tuple[str, ...] = DEFAULT_VERDICTS,
    skip_disagreement: bool = True,
    skip_single_rater: bool = True,
) -> bool:
    """Whether one ranked row is worth turning into a candidate.

    A row is skipped when the reviewers did not agree it should be done now,
    when only one rater scored it, when the evidence is weak, or when it has no
    technique or no id to name it by.
    """
    if row.get("verdict") not in verdicts:
        return False
    if skip_disagreement and row.get("disagreement"):
        return False
    if skip_single_rater and row.get("single_rater"):
        return False
    if str(row.get("evidence_strength") or "").lower() == "weak":
        return False
    if not _clean(row.get("technique")):
        return False
    if not _clean(row.get("id")):
        return False
    return True


def technique_candidates(
    rows: list[dict],
    *,
    spec: DomainSpec,
    review_id: str,
    now: int,
    max_n: int = 5,
    verdicts: tuple[str, ...] = DEFAULT_VERDICTS,
    skip_disagreement: bool = True,
    skip_single_rater: bool = True,
) -> list[Candidate]:
    """Turn ranked review rows into candidates, the best technique first.

    The order is the review's own: priority descending, then the review's
    rank, then the row id — so two rows the reviewers scored identically are
    still ordered the same way on every harvest.
    """
    if max_n < 1:
        return []
    kept = [
        row
        for row in rows
        if eligible(
            row,
            verdicts=verdicts,
            skip_disagreement=skip_disagreement,
            skip_single_rater=skip_single_rater,
        )
    ]
    kept.sort(key=lambda row: (-_number(row.get("priority")), _rank(row), _clean(row.get("id"), 40)))
    return [
        _candidate(row, spec=spec, review_id=review_id, now=now) for row in kept[:max_n]
    ]


def _candidate(row: Mapping, *, spec: DomainSpec, review_id: str, now: int) -> Candidate:
    """Build one candidate from one ranked row."""
    technique = _clean(row.get("technique"), 200)
    group = _clean(row.get("group_id"), 20)
    priority = _number(row.get("priority"))
    reach = 1 + max(0, round(priority)) ** 2
    built = Candidate.build(
        domain=spec.domain,
        source=f"research:{group or 'G00'}",
        kind=KIND,
        title=f"adopt {_clean(row.get('id'), 40)}: {technique}"[:160],
        evidence_key=f"technique:{slug(technique)}",
        now=now,
        detail=_clean(row.get("mechanism"), 400),
        files=spec.files,
        evidence={
            "review": review_id,
            "technique_id": _clean(row.get("id"), 40),
            "group_id": group,
            "technique": technique,
            "priority": priority,
            "verdict": row.get("verdict"),
            "papers": _papers(row),
            "benchmark": spec.benchmark,
            "surface": list(spec.surface),
            "miniork_surface": _clean(row.get("miniork_surface"), 300),
        },
        fix_hint=fix_hint(row, spec),
        success_metric=f"{spec.benchmark}: sealed paired gain (G12)",
        success_command=f"bench:{spec.benchmark}",
        reach=reach,
    )
    return built


@dataclass(frozen=True, slots=True)
class ResearchHarvest:
    """What one domain's research harvest produced.

    ``review``/``review_id``/``age_days`` describe the review the candidates
    came from — a candidate older than the review it was read from is a
    different fact, and a stale review is a reason to look again.
    """

    domain: str
    review: str | None
    review_id: str | None
    age_days: int | None
    candidates: tuple[Candidate, ...]
    notes: tuple[str, ...] = ()
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """A summary of the harvest — enough to record, not to replay."""
        return {
            "domain": self.domain,
            "review": self.review,
            "review_id": self.review_id,
            "age_days": self.age_days,
            "candidates": [
                {
                    "candidate_id": candidate.candidate_id,
                    "title": candidate.title,
                    "source": candidate.source,
                    "severity": candidate.severity,
                }
                for candidate in self.candidates
            ],
            "notes": list(self.notes),
            "error": self.error,
        }


def harvest_domain(
    home: Path,
    spec: DomainSpec,
    *,
    now: int,
    max_n: int = 5,
    verdicts: tuple[str, ...] = DEFAULT_VERDICTS,
    skip_disagreement: bool = True,
    skip_single_rater: bool = True,
) -> ResearchHarvest:
    """Harvest one domain's newest review into candidates.

    A missing review is not an error — a domain nobody has reviewed yet is a
    domain with nothing to do. A review that exists but cannot be read *is*
    an error, and it is reported as one rather than as an empty harvest.
    """
    path = latest_review(home, spec.review_glob)
    if path is None:
        return ResearchHarvest(
            spec.domain,
            None,
            None,
            None,
            (),
            notes=(f"no technique review for {spec.domain} matching {spec.review_glob}",),
        )
    review_id = path.parent.name
    age_days = max(0, (now - int(path.stat().st_mtime)) // 86400)
    try:
        rows = load_ranked(path)
    except (ValueError, OSError, json.JSONDecodeError) as exc:
        return ResearchHarvest(
            spec.domain,
            str(path),
            review_id,
            age_days,
            (),
            error=f"unreadable review {path}: {exc}",
        )
    candidates = tuple(
        technique_candidates(
            rows,
            spec=spec,
            review_id=review_id,
            now=now,
            max_n=max_n,
            verdicts=verdicts,
            skip_disagreement=skip_disagreement,
            skip_single_rater=skip_single_rater,
        )
    )
    eligible_count = sum(
        1
        for row in rows
        if eligible(
            row,
            verdicts=verdicts,
            skip_disagreement=skip_disagreement,
            skip_single_rater=skip_single_rater,
        )
    )
    notes = (f"{eligible_count} eligible of {len(rows)} ranked; emitted {len(candidates)}",)
    return ResearchHarvest(spec.domain, str(path), review_id, age_days, candidates, notes=notes)


def harvest_all(
    home: Path,
    specs: Mapping[str, DomainSpec] | None = None,
    *,
    now: int,
    domains: list[str] | None = None,
    **kw: Any,
) -> list[ResearchHarvest]:
    """Harvest every domain in ``specs`` (the default set when omitted).

    Domains are visited in name order so two harvests of the same tree report
    their domains the same way. ``domains`` narrows the set; a domain that is
    not in ``specs`` is simply not harvested.
    """
    chosen = specs or DEFAULT_SPECS
    harvests: list[ResearchHarvest] = []
    for name in sorted(chosen):
        if domains is not None and name not in domains:
            continue
        harvests.append(harvest_domain(home, chosen[name], now=now, **kw))
    return harvests
