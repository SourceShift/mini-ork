"""Unit tests: mini_ork.stores.anchor_corpus (bash parity halves removed; formerly vs lib/anchor_corpus.sh).

Each test drives the Python port against JSON fixtures and asserts the
result dicts semantically: floats within 1e-6, strings exact. No mocks.

The Python port takes its knobs as explicit kwargs AND honors the env as
fallback (``MO_CORPUS_RECALL_FLOOR``, ``MINI_ORK_RUN_DIR``).

Nine cases:
  (a) load_valid_corpus                       → parsed dict shape
  (b) load_missing_required_field             → raises AnchorCorpusShapeError
  (c) load_non_object_corpus                  → raises AnchorCorpusShapeError
  (d) recall_hits_all_must_be_found           → rc=0 recall_meets_floor
  (e) recall_below_floor                      → rc=1 BELOW_FLOOR + recall=0.6667 missed=['A-003']
  (f) recall_missing_findings_path            → rc=0 indeterminate missing_inputs
  (g) recall_no_must_be_found                 → rc=0 indeterminate no_must_be_found
  (h) recall_tunable_floor (floor=0.5)        → fixture (e) flips to meets_floor
  (i) recall_file_line_only_match             → id-less findings matches via file:line substring; TSV row
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
from mini_ork.stores import anchor_corpus as ac

_FLOAT_TOL = 1e-6


# ─────────────────────────────────────────────────────────────────────────────
# Fixtures shared across recall tests.
# ─────────────────────────────────────────────────────────────────────────────
@pytest.fixture
def corpus_path(tmp_path) -> Path:
    p = tmp_path / "corpus.json"
    p.write_text(
        json.dumps(
            {
                "name": "selftest-corpus",
                "task_class": "refactor_audit",
                "description": "Self-test corpus with 4 anchors, 3 must_be_found.",
                "anchors": [
                    {
                        "id": "A-001",
                        "file": "src/auth.ts",
                        "line": 42,
                        "claim": "Token expiration unchecked",
                        "severity": "P1",
                        "must_be_found": True,
                    },
                    {
                        "id": "A-002",
                        "file": "src/cache.ts",
                        "line": 88,
                        "claim": "Race on cache invalidation",
                        "severity": "P0",
                        "must_be_found": True,
                    },
                    {
                        "id": "A-003",
                        "file": "src/db.ts",
                        "line": 117,
                        "claim": "Connection leak under retry",
                        "severity": "P1",
                        "must_be_found": True,
                    },
                    {
                        "id": "A-004",
                        "file": "src/util.ts",
                        "line": 30,
                        "claim": "Minor: typo in error message",
                        "severity": "P3",
                        "must_be_found": False,
                    },
                ],
            }
        )
    )
    return p


@pytest.fixture
def findings_good(tmp_path) -> Path:
    """Hits A-001, A-002, A-003 by id token AND file:line."""
    p = tmp_path / "findings-good.md"
    p.write_text(
        "## Findings\n\n"
        "- A-001 src/auth.ts:42 — token expiration unchecked\n"
        "- A-002 src/cache.ts:88 — race on cache invalidation\n"
        "- A-003 src/db.ts:117 — connection leak under retry\n"
    )
    return p


@pytest.fixture
def findings_bad(tmp_path) -> Path:
    """Hits A-001, A-002 only — A-003 missed → 2/3 = 0.6667 < 0.8."""
    p = tmp_path / "findings-bad.md"
    p.write_text(
        "## Findings\n\n"
        "- A-001 src/auth.ts:42 — token expiration unchecked\n"
        "- A-002 src/cache.ts:88 — race on cache invalidation\n"
    )
    return p


@pytest.fixture
def findings_idless(tmp_path) -> Path:
    """No anchor id tokens; only the src/auth.ts:42 citation appears."""
    p = tmp_path / "findings-idless.md"
    p.write_text(
        "## Findings\n\n"
        "Reviewing auth module: src/auth.ts:42 has an unchecked token expiry.\n"
    )
    return p


# ─────────────────────────────────────────────────────────────────────────────
# (a) load_valid_corpus — parsed dict shape.
# ─────────────────────────────────────────────────────────────────────────────
def test_load_valid_corpus(tmp_path):
    cp = tmp_path / "ok.json"
    cp.write_text(
        json.dumps(
            {
                "name": "n", "description": "d", "task_class": "x",
                "anchors": [
                    {"id": "A-1", "file": "f", "line": 1, "claim": "c"},
                ],
            }
        )
    )
    py_obj = ac.load_corpus(str(cp))
    assert py_obj["name"] == "n"
    assert py_obj["task_class"] == "x"
    assert py_obj["anchors"][0]["id"] == "A-1"
    assert py_obj["anchors"][0]["line"] == 1


# ─────────────────────────────────────────────────────────────────────────────
# (b) load_missing_required_field — py raises AnchorCorpusShapeError.
# ─────────────────────────────────────────────────────────────────────────────
def test_load_missing_required_field(tmp_path):
    cp = tmp_path / "bad-missing.json"
    cp.write_text(
        json.dumps(
            {
                "name": "n", "description": "d", "task_class": "x",
                "anchors": [
                    {"id": "A-1", "file": "f"},  # missing claim + line
                ],
            }
        )
    )
    with pytest.raises(ac.AnchorCorpusShapeError):
        ac.load_corpus(str(cp))


# ─────────────────────────────────────────────────────────────────────────────
# (c) load_non_object_corpus — py raises AnchorCorpusShapeError.
# ─────────────────────────────────────────────────────────────────────────────
def test_load_non_object_corpus(tmp_path):
    cp = tmp_path / "bad-array.json"
    cp.write_text(json.dumps([{"id": "A-1"}]))  # root is list, not dict
    with pytest.raises(ac.AnchorCorpusShapeError):
        ac.load_corpus(str(cp))


# ─────────────────────────────────────────────────────────────────────────────
# (d) recall_hits_all_must_be_found — rc=0 meets_floor.
# ─────────────────────────────────────────────────────────────────────────────
def test_recall_hits_all_must_be_found(tmp_path, corpus_path, findings_good):
    report_dir = tmp_path / "rpt-d"
    py_obj, py_rc = ac.score_recall(
        str(findings_good), str(corpus_path), report_dir=str(report_dir),
    )
    assert py_rc == 0
    assert py_obj["verdict"] == "recall_meets_floor"
    assert py_obj["reason"] == "ok"
    assert py_obj["found"] == 3
    assert py_obj["must_be_found"] == 3
    assert py_obj["missed_anchor_ids"] == []


# ─────────────────────────────────────────────────────────────────────────────
# (e) recall_below_floor — rc=1 BELOW_FLOOR + recall=0.6667 missed=['A-003'].
# ─────────────────────────────────────────────────────────────────────────────
def test_recall_below_floor(tmp_path, corpus_path, findings_bad):
    report_dir = tmp_path / "rpt-e"
    py_obj, py_rc = ac.score_recall(
        str(findings_bad), str(corpus_path), report_dir=str(report_dir),
    )
    assert py_rc == 1
    assert py_obj["verdict"] == "RECALL_BELOW_FLOOR"
    assert py_obj["reason"] == "low_recall"
    assert py_obj["found"] == 2
    assert py_obj["must_be_found"] == 3
    assert abs(py_obj["recall"] - 0.6667) <= _FLOAT_TOL
    assert py_obj["missed_anchor_ids"] == ["A-003"]


# ─────────────────────────────────────────────────────────────────────────────
# (f) recall_missing_findings_path — rc=0 indeterminate missing_inputs.
# ─────────────────────────────────────────────────────────────────────────────
def test_recall_missing_findings_path(tmp_path, corpus_path):
    bogus = tmp_path / "does-not-exist.md"
    report_dir = tmp_path / "rpt-f"
    py_obj, py_rc = ac.score_recall(
        str(bogus), str(corpus_path), report_dir=str(report_dir),
    )
    assert py_rc == 0
    assert py_obj["verdict"] == "indeterminate"
    assert py_obj["reason"] == "missing_inputs"


# ─────────────────────────────────────────────────────────────────────────────
# (g) recall_no_must_be_found — rc=0 indeterminate no_must_be_found.
# ─────────────────────────────────────────────────────────────────────────────
def test_recall_no_must_be_found(tmp_path, findings_good):
    cp = tmp_path / "no-must.json"
    cp.write_text(
        json.dumps(
            {
                "name": "n", "description": "d", "task_class": "x",
                "anchors": [
                    {
                        "id": "A-9", "file": "f", "line": 1,
                        "claim": "c", "must_be_found": False,
                    },
                ],
            }
        )
    )
    report_dir = tmp_path / "rpt-g"
    py_obj, py_rc = ac.score_recall(
        str(findings_good), str(cp), report_dir=str(report_dir),
    )
    assert py_rc == 0
    assert py_obj["verdict"] == "indeterminate"
    assert py_obj["reason"] == "no_must_be_found"
    assert py_obj["must_be_found"] == 0


# ─────────────────────────────────────────────────────────────────────────────
# (h) recall_tunable_floor — floor=0.5 flips fixture (e) to meets_floor.
# ─────────────────────────────────────────────────────────────────────────────
def test_recall_tunable_floor(tmp_path, corpus_path, findings_bad):
    report_dir = tmp_path / "rpt-h"
    py_obj, py_rc = ac.score_recall(
        str(findings_bad), str(corpus_path),
        report_dir=str(report_dir), floor=0.5,
    )
    assert py_rc == 0
    assert py_obj["verdict"] == "recall_meets_floor"
    assert py_obj["recall_floor"] == 0.5
    # 2/3 recall at floor=0.5 satisfies the gate.
    assert py_obj["found"] == 2 and py_obj["must_be_found"] == 3


# ─────────────────────────────────────────────────────────────────────────────
# (i) recall_file_line_only_match — id-less findings match via file:line substring;
#     the TSV row for A-001 records file=src/auth.ts line=42 found=yes.
#     1-of-3 found → recall 0.3333 < 0.8 → rc=1 BELOW_FLOOR.
# ─────────────────────────────────────────────────────────────────────────────
def test_recall_file_line_only_match(tmp_path, corpus_path, findings_idless):
    report_dir = tmp_path / "rpt-i"
    py_obj, py_rc = ac.score_recall(
        str(findings_idless), str(corpus_path),
        report_dir=str(report_dir),
    )
    assert py_rc == 1
    assert py_obj["verdict"] == "RECALL_BELOW_FLOOR"
    assert py_obj["reason"] == "low_recall"
    assert py_obj["found"] == 1
    assert py_obj["missed_anchor_ids"] == ["A-002", "A-003"]

    # TSV row for the file-line match.
    py_tsv = (report_dir / "corpus-recall.tsv").read_text().splitlines()
    assert py_tsv[0] == "anchor_id\tfile\tline\tseverity\tfound"
    row_a001 = next(r for r in py_tsv[1:] if r.startswith("A-001\t"))
    assert row_a001 == "A-001\tsrc/auth.ts\t42\tP1\tyes"
    # A-002/A-003 had no citation in idless findings → found=no.
    row_a002 = next(r for r in py_tsv[1:] if r.startswith("A-002\t"))
    assert row_a002.endswith("\tno")
