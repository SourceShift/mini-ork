"""Standalone unit tests for ``mini_ork.gates.citation_verifier_mechanical``.

Replaces the bash-parity gate (against
``lib/citation_verifier_mechanical.sh``) as part of the bash→Python
migration: the Python port is now the sole implementation, so its coverage
no longer runs ``mo_check_citations`` in a subprocess — it asserts the
port's behaviour directly. The expected values below are the semantic
contract the bash side used to pin (verdicts, reasons, citation counts,
rc semantics), now asserted on the port's output.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
from mini_ork.gates import citation_verifier_mechanical as cv

_ENV_KNOBS = ("MINI_ORK_ROOT", "MO_CITATION_COVERAGE_FLOOR", "MO_CITATION_MIN_COUNT")


# --------------------------------------------------------------------------- #
# Harness
# --------------------------------------------------------------------------- #

def _py_check(doc: str, report_dir: str, env_extra: dict) -> tuple[dict, int]:
    """Run the Python port with env knobs scoped to the call."""
    saved: dict[str, str | None] = {}
    for k in _ENV_KNOBS:
        if k in env_extra:
            saved[k] = os.environ.get(k)
            os.environ[k] = env_extra[k]
    try:
        d, rc = cv.check_citations(doc, report_dir)
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    return d, rc


# --------------------------------------------------------------------------- #
# Fixture helpers
# --------------------------------------------------------------------------- #

def _make_repo(tmp_path: Path) -> Path:
    """Build a fake repo with two small source files. Idempotent."""
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True, exist_ok=True)
    (repo / "docs").mkdir(exist_ok=True)
    (repo / "src" / "foo.ts").write_text("line1\nline2\nline3\nline4\nline5\n")
    (repo / "src" / "bar.py").write_text("a\nb\nc\nd\ne\nf\ng\nh\ni\nj\n")
    return repo


def _run(tmp_path: Path, doc_text: str, doc_relpath: str = "docs/synth.md") -> tuple[dict, int]:
    """Write a synth doc into the fake repo and run the port on it."""
    repo = _make_repo(tmp_path)
    doc = repo / doc_relpath
    doc.parent.mkdir(parents=True, exist_ok=True)
    doc.write_text(doc_text)
    env_extra = {"MINI_ORK_ROOT": str(repo)}
    report_dir = tmp_path / "report"
    report_dir.mkdir()
    return _py_check(str(doc), str(report_dir), env_extra)


# --------------------------------------------------------------------------- #
# Fixtures (6 cases)
# --------------------------------------------------------------------------- #

def test_01_all_valid_3_citations(tmp_path):
    d, rc = _run(tmp_path, (
        "Three valid anchors. See src/foo.ts:2 for the first claim and\n"
        "src/foo.ts:3-4 for the second, and src/bar.py:7 for the third.\n"
    ))
    assert rc == 0
    assert d["verdict"] == "citations_covered"
    assert d["total_citations"] == 3
    assert d["valid_citations"] == 3
    assert d["invalid_citations"] == 0
    assert d["unique_files"] == 2


def test_02_one_of_four_invalid_triggers_undercovered(tmp_path):
    d, rc = _run(tmp_path, (
        "Mixed: real src/foo.ts:2 and ghost src/missing.ts:5 and\n"
        "out-of-bounds src/foo.ts:9999 and real src/bar.py:1.\n"
    ))
    assert rc == 1
    assert d["verdict"] == "CITATION_UNDERCOVERED"
    assert d["reason"] == "low_coverage"
    assert d["total_citations"] == 4
    assert d["valid_citations"] == 2
    assert d["invalid_citations"] == 2


def test_03_zero_citations_indeterminate(tmp_path):
    d, rc = _run(tmp_path, (
        "A document with no file:line anchors at all, just narrative prose.\n"
    ))
    assert rc == 0
    assert d["verdict"] == "indeterminate"
    assert d["reason"] == "no_citations_found"
    assert d["total_citations"] == 0


def test_04_missing_document(tmp_path):
    repo = _make_repo(tmp_path)
    env_extra = {"MINI_ORK_ROOT": str(repo)}
    report_dir = tmp_path / "report"
    report_dir.mkdir()
    missing = repo / "does-not-exist.md"
    d, rc = _py_check(str(missing), str(report_dir), env_extra)
    assert rc == 0
    assert d["verdict"] == "indeterminate"
    assert d["reason"] == "missing_document"
    # Shell-level early return OMITS report_path.
    assert "report_path" not in d


def test_05_absolute_path_accepted(tmp_path):
    """Absolute paths must resolve as-is."""
    repo = _make_repo(tmp_path)
    abs_foo = repo / "src" / "foo.ts"
    abs_bar = repo / "src" / "bar.py"
    text = (
        f"Abs anchor: {abs_foo}:2 and {abs_foo}:3-4 and {abs_bar}:5.\n"
    )
    d, rc = _run(tmp_path, text)
    assert rc == 0
    assert d["verdict"] == "citations_covered"
    assert d["total_citations"] == 3
    assert d["valid_citations"] == 3


def test_06_dedupe_identical(tmp_path):
    """Identical (path, start, end) tuples must collapse to one entry."""
    text = (
        "Repeated: src/foo.ts:2 src/foo.ts:2 src/foo.ts:2 "
        "and then src/foo.ts:3-4 src/foo.ts:3-4.\n"
        # Need at least one more unique citation so we hit the min_count=3 floor.
        "Plus src/bar.py:1.\n"
    )
    d, rc = _run(tmp_path, text)
    assert rc == 0
    assert d["verdict"] == "citations_covered"
    # 5 raw → 3 unique (foo.ts:2 once, foo.ts:3-4 once, bar.py:1)
    assert d["total_citations"] == 3
    assert d["valid_citations"] == 3
    assert d["unique_files"] == 2


# --------------------------------------------------------------------------- #
# Import + keyshape smoke
# --------------------------------------------------------------------------- #

def test_import_and_keyshape():
    """Smoke: pure import + dict-shape sanity."""
    d, rc = cv.check_citations("/no/such/file.md")
    assert rc == 0
    assert d["verdict"] == "indeterminate"
    assert d["reason"] == "missing_document"
    assert set(d.keys()) == {
        "verdict", "reason", "coverage", "coverage_floor",
        "total_citations", "valid_citations", "invalid_citations",
        "unique_files", "rationale",
    }
