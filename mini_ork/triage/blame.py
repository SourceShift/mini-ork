"""Blame attribution for a failed workflow node — pure, no I/O.

The failure classifier (``mini_ork.learning.failure_classifier``) answers
*can this be recovered?* This module answers a different question: *whose bug
is it?* — the framework's, or the caller's/consumer's? The distinction decides
whether an automatic fix run is warranted.

Three verdicts, fail-closed to ``unknown`` (which, like ``consumer``, means
"do not auto-fix"):

  * ``mini_ork``  — the failure raised inside mini-ork's own code (``mini_ork/``
    or a *shipped* ``recipes/*`` verifier). A ``framework-edit`` fix run is
    warranted.
  * ``consumer``  — a legitimate artifact verdict (``verdict_fail`` /
    ``verdict_revise``) or an error in code outside the framework tree.
  * ``unknown``   — infra (timeout / budget / provider error) or simply not
    localisable. Never fix blind.

Pure function: :func:`attribute` takes a :class:`NodeFailure` (already-loaded
fields) and returns ``(verdict, evidence)``. All file/DB reading lives in
:mod:`mini_ork.triage.failures`.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Literal

Blame = Literal["mini_ork", "consumer", "unknown"]

# finish_reason values from db/migrations/0021_error_taxonomy_finish_reasons.sql.
_VERDICT_REASONS = frozenset({"verdict_fail", "verdict_revise"})
_INFRA_REASONS = frozenset({"timeout", "cost_limit", "interrupted", "max_steps"})

# A provider/auth/transport failure is infra, never a mini-ork code bug. Kept
# conservative on purpose: a false ``unknown`` only skips an auto-fix, whereas a
# false ``mini_ork`` spends a fix run on the wrong thing.
_PROVIDER_SIGNATURES = (
    "401 unauthorized",
    "403 forbidden",
    "invalid api key",
    "invalid_api_key",
    "api key not",
    "authenticationerror",
    "rate limit",
    "429 too many requests",
    "connection refused",
    "connection reset",
    "no such host",
    "temporary failure in name resolution",
    "model: null",
    "model is null",
)

_FRAME_RE = re.compile(r'File "([^"]+)", line \d+')


@dataclass(frozen=True)
class NodeFailure:
    """One failed node, as observed at run finalize. Pure data — no I/O."""

    node_id: str
    node_type: str = ""
    finish_reason: str | None = None
    log_excerpt: str = ""
    artifact_path: str = ""


@dataclass(frozen=True)
class Evidence:
    """One reason contributing to a verdict — named so it can be surfaced."""

    rule: str
    detail: str


def _is_under(child: str, root: str) -> bool:
    try:
        return os.path.commonpath([os.path.abspath(child), os.path.abspath(root)]) == os.path.abspath(root)
    except (ValueError, TypeError):
        return False


def _segments(path: str) -> list[str]:
    return [s for s in re.split(r"[\\/]+", os.path.normpath(path)) if s]


def _is_framework_path(path: str, root: str | None = None) -> bool:
    """True when *path* is part of mini-ork's own editable surface.

    Keyed on path *shape*, not a checkout root: framework code ships as the
    ``mini_ork/`` package and as ``recipes/<name>/verifiers/*``. A failed run
    often executes from a worktree or a vendored copy whose root differs from
    the triager's — a root-relative check alone misses those real framework
    bugs (observed: a traceback under
    ``…/mini-ork-worktrees/heldout-baseline/recipes/code-fix/verifiers/test.py``
    with the triager rooted at the main checkout). The *root* argument, when
    given, is kept as an extra true-condition.
    """
    segs = _segments(path)
    if "mini_ork" in segs:
        return True
    if "recipes" in segs and "verifiers" in segs[segs.index("recipes") + 1:]:
        return True
    if root:
        return _is_under(path, os.path.join(root, "mini_ork")) or _is_under(
            path, os.path.join(root, "recipes")
        )
    return False


def frame_paths(log: str) -> list[str]:
    """Extract ``File "<path>", line N`` frames from a Python traceback."""
    return _FRAME_RE.findall(log or "")


def _provider_hit(log: str) -> str | None:
    low = (log or "").lower()
    for sig in _PROVIDER_SIGNATURES:
        if sig in low:
            return sig
    return None


def attribute(failed: NodeFailure, *, root: str | None = None) -> tuple[Blame, list[Evidence]]:
    """Classify a failed node as ``mini_ork`` / ``consumer`` / ``unknown``.

    ``root`` is the mini-ork repo root. It is *optional*: framework frames are
    recognised by path shape (the ``mini_ork/`` package, ``recipes/*/verifiers``)
    so a checkout rooted elsewhere (a worktree, a vendored copy) still resolves.
    ``root`` only adds an anchored true-condition on top.
    """
    ev: list[Evidence] = []
    reason = (failed.finish_reason or "").strip().lower()

    if reason in _VERDICT_REASONS:
        return "consumer", [
            Evidence("verdict", f"finish_reason={reason}: a legitimate artifact verdict, not a harness bug")
        ]

    if reason in _INFRA_REASONS:
        return "unknown", [
            Evidence("infra", f"finish_reason={reason}: environment/budget/step-limit, not a code defect")
        ]

    hit = _provider_hit(failed.log_excerpt)
    if hit:
        return "unknown", [Evidence("provider", f"provider/auth/transport signature: {hit!r}")]

    frames = frame_paths(failed.log_excerpt)
    fw = [p for p in frames if _is_framework_path(p, root)]
    if fw:
        ev = [Evidence("traceback", f"error inside mini-ork source: {fw[0]}")]
        if len(fw) > 1:
            ev.append(Evidence("traceback", f"{len(fw)} framework frames; deepest {fw[-1]}"))
        return "mini_ork", ev

    if frames:
        return "consumer", [
            Evidence("traceback", f"error in non-framework code: {frames[0]}")
        ]

    if reason == "error":
        return "unknown", [
            Evidence("error", "node errored with no localisable traceback (no provider signature)")
        ]

    return "unknown", [Evidence("default", f"no localising signal (finish_reason={reason or 'none'})")]
