"""Pure-logic port of ``lib/anchor_corpus.sh``'s held-out anchor corpus loader + recall scorer.

Faithful port of ``lib/anchor_corpus.sh::anchor_corpus_load`` (lines 61-102) and
``lib/anchor_corpus.sh::anchor_corpus_recall`` (lines 104-222). The bash source
is a thin shell wrapper around embedded ``python3`` heredocs that handle parse,
shape validation, scoring, and TSV reporting. This module lifts those heredocs
out into native Python with identical semantics:

* ``load_corpus(path)`` mirrors ``anchor_corpus_load`` — validates the corpus
  JSON shape and returns the parsed dict. Raises :class:`AnchorCorpusShapeError`
  with the bash-identical stderr message on each failure mode.
* ``score_recall(findings_path, corpus_path, report_dir=None, floor=...)``
  mirrors ``anchor_corpus_recall`` — same indeterminate branches, same
  ``round(recall, 4)``, same TSV layout, same rc semantics; returns a
  ``(dict, rc)`` tuple so callers don't have to inspect stdout for rc.

Env knobs honored (also accepted as explicit kwargs):
  * ``MO_CORPUS_RECALL_FLOOR`` (default ``0.8``) — recall floor.
  * ``MINI_ORK_RUN_DIR`` (default ``"."``) — default ``report_dir`` when
    ``score_recall`` is called with ``report_dir=None``.

Public API::

    from mini_ork.stores.anchor_corpus import (
        load_corpus, score_recall, AnchorCorpusShapeError,
    )

    corpus = load_corpus("corpus.json")              # raises on bad shape
    result, rc = score_recall("findings.md", "corpus.json")
    # rc=0 recall_meets_floor OR indeterminate; rc=1 RECALL_BELOW_FLOOR.

``tests/unit/test_anchor_corpus_py.py`` enforces exact parity (floats within
1e-6, strings exact, JSON byte-stable) against the LIVE bash function over
nine cases spanning every bash branch.
"""

from __future__ import annotations

import json
import os
from typing import Any

__all__ = ["load_corpus", "score_recall", "AnchorCorpusShapeError"]


class AnchorCorpusShapeError(Exception):
    """Raised when a corpus file fails the bash ``anchor_corpus_load`` shape checks.

    The ``str(exc)`` matches the bash stderr message verbatim so parity tests
    can assert substring presence against the live bash invocation.
    """


_REQUIRED_ANCHOR_FIELDS = {"id", "file", "line", "claim"}


def _matched(anchor: dict[str, Any], findings_text: str) -> bool:
    """True if ``anchor`` is "found" in ``findings_text`` per bash's matched().

    Mirrors ``lib/anchor_corpus.sh:174-184`` — id token substring OR any of
    ``{file}:{line}`` / ``{file}#L{line}`` / ``{file}:{line}-`` substrings.
    """
    aid = anchor.get("id") or ""
    if aid and aid in findings_text:
        return True
    file_ = anchor.get("file") or ""
    line = anchor.get("line")
    if file_ and line is not None:
        for needle in (f"{file_}:{line}", f"{file_}#L{line}", f"{file_}:{line}-"):
            if needle in findings_text:
                return True
    return False


def _write_report(rows: list[str], report_path: str) -> None:
    """Write the TSV corpus-recall report. Mirrors ``lib/anchor_corpus.sh:203-207``.

    bash swallows write failures (``except Exception: pass``); the port mirrors
    that silently. A single ``\\n``-joined string terminating with ``\\n`` is the
    exact form bash emits.
    """
    try:
        with open(report_path, "w", encoding="utf-8") as f:
            f.write("\n".join(rows) + "\n")
    except Exception:
        pass


def load_corpus(path: str) -> dict[str, Any]:
    """Load and shape-validate a corpus file. Mirrors ``anchor_corpus_load`` (lines 72-101).

    Raises :class:`AnchorCorpusShapeError` with the bash-identical stderr message
    on each failure mode: missing path, parse error, non-dict root, empty anchors
    list, non-object anchor element, missing required fields ``{id, file, line,
    claim}``. Returns the parsed ``dict`` on success.
    """
    if not path:
        raise AnchorCorpusShapeError("anchor_corpus_load: corpus path missing")
    if not os.path.isfile(path):
        raise AnchorCorpusShapeError("anchor_corpus_load: corpus path missing")

    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except Exception as exc:
        raise AnchorCorpusShapeError(f"anchor_corpus_load: parse error: {exc}") from None

    if not isinstance(data, dict):
        raise AnchorCorpusShapeError("anchor_corpus_load: corpus must be a JSON object")

    anchors = data.get("anchors")
    if not isinstance(anchors, list) or not anchors:
        raise AnchorCorpusShapeError(
            "anchor_corpus_load: anchors[] must be a non-empty list"
        )

    for i, a in enumerate(anchors):
        if not isinstance(a, dict):
            raise AnchorCorpusShapeError(
                f"anchor_corpus_load: anchors[{i}] must be an object"
            )
        missing = _REQUIRED_ANCHOR_FIELDS - a.keys()
        if missing:
            raise AnchorCorpusShapeError(
                f"anchor_corpus_load: anchors[{i}] missing {sorted(missing)}"
            )

    return data


def score_recall(
    findings_path: str,
    corpus_path: str,
    report_dir: str | None = None,
    floor: float | None = None,
) -> tuple[dict[str, Any], int]:
    """Score findings against a corpus. Mirrors ``anchor_corpus_recall`` (lines 104-220).

    Returns ``(result_dict, rc)``. ``rc=0`` on ``recall_meets_floor`` OR any
    ``indeterminate`` branch; ``rc=1`` on ``RECALL_BELOW_FLOOR``.

    Env fallback knobs honored when the corresponding kwarg is ``None``:
      * ``MO_CORPUS_RECALL_FLOOR`` -> ``floor``
      * ``MINI_ORK_RUN_DIR`` or ``"."`` -> ``report_dir``
    """
    if floor is None:
        floor = float(os.environ.get("MO_CORPUS_RECALL_FLOOR", "0.8"))

    if report_dir is None:
        report_dir = os.environ.get("MINI_ORK_RUN_DIR") or "."

    report_path = os.path.join(report_dir, "corpus-recall.tsv")

    # missing_inputs branch — bash: rc=0 with synthesized JSON.
    if (
        not findings_path
        or not os.path.isfile(findings_path)
        or not corpus_path
        or not os.path.isfile(corpus_path)
    ):
        return (
            {
                "verdict": "indeterminate",
                "reason": "missing_inputs",
                "found": 0,
                "must_be_found": 0,
                "recall": None,
                "recall_floor": floor,
                "missed_anchor_ids": [],
                "rationale": "findings_path or corpus_path missing; cannot measure",
            },
            0,
        )

    # bash mkdir -p silently — mirror that to keep parity.
    try:
        os.makedirs(report_dir, exist_ok=True)
    except Exception:
        pass

    try:
        with open(findings_path, encoding="utf-8") as f:
            findings_text = f.read()
    except Exception as exc:
        return (
            {
                "verdict": "indeterminate",
                "reason": "missing_inputs",
                "found": 0,
                "must_be_found": 0,
                "recall": None,
                "recall_floor": floor,
                "missed_anchor_ids": [],
                "report_path": report_path,
                "rationale": f"findings unreadable: {exc}",
            },
            0,
        )

    try:
        with open(corpus_path, encoding="utf-8") as f:
            corpus = json.load(f)
    except Exception as exc:
        return (
            {
                "verdict": "indeterminate",
                "reason": "missing_inputs",
                "found": 0,
                "must_be_found": 0,
                "recall": None,
                "recall_floor": floor,
                "missed_anchor_ids": [],
                "report_path": report_path,
                "rationale": f"corpus unreadable: {exc}",
            },
            0,
        )

    anchors = corpus.get("anchors") or []
    must_be_found = [a for a in anchors if a.get("must_be_found")]
    must_total = len(must_be_found)

    if must_total == 0:
        # bash emits the indeterminate dict then sys.exit(0).
        return (
            {
                "verdict": "indeterminate",
                "reason": "no_must_be_found",
                "found": 0,
                "must_be_found": 0,
                "recall": None,
                "recall_floor": floor,
                "missed_anchor_ids": [],
                "report_path": report_path,
                "rationale": "corpus has no must_be_found anchors; nothing to score",
            },
            0,
        )

    found_count = 0
    missed_ids: list[str] = []
    report_rows = ["anchor_id\tfile\tline\tseverity\tfound"]
    for a in anchors:
        aid = a.get("id") or ""
        file_ = a.get("file") or ""
        line = a.get("line") or 0
        sev = a.get("severity") or ""
        ok = _matched(a, findings_text)
        report_rows.append(f"{aid}\t{file_}\t{line}\t{sev}\t{'yes' if ok else 'no'}")
        if a.get("must_be_found"):
            if ok:
                found_count += 1
            else:
                missed_ids.append(aid)

    _write_report(report_rows, report_path)

    recall = found_count / must_total

    if recall < floor:
        cap = ", ".join(missed_ids[:5]) + ("..." if len(missed_ids) > 5 else "")
        rationale = (
            f"recall {recall:.1%} < floor {floor:.0%} "
            f"({len(missed_ids)} must-find anchors missed: {cap})"
        )
        return (
            {
                "verdict": "RECALL_BELOW_FLOOR",
                "reason": "low_recall",
                "found": found_count,
                "must_be_found": must_total,
                "recall": round(recall, 4),
                "recall_floor": floor,
                "missed_anchor_ids": missed_ids,
                "report_path": report_path,
                "rationale": rationale,
            },
            1,
        )

    rationale = (
        f"recall {recall:.1%} >= floor {floor:.0%} across {must_total} "
        f"must-find anchors"
    )
    return (
        {
            "verdict": "recall_meets_floor",
            "reason": "ok",
            "found": found_count,
            "must_be_found": must_total,
            "recall": round(recall, 4),
            "recall_floor": floor,
            "missed_anchor_ids": missed_ids,
            "report_path": report_path,
            "rationale": rationale,
        },
        0,
    )
