"""Parity gate: ``mini_ork.gates.citation_verifier_mechanical`` vs ``lib/citation_verifier_mechanical.sh``.

For each fixture we build a synthetic repo + synthesis doc, invoke the LIVE
``mo_check_citations`` bash function via subprocess (no mocking), then call
the Python port and compare the resulting dict shape. Coverage floats must
match within ``1e-6``; exit codes must match; every key/value must match
byte-stable (including ``report_path``, so both engines share a single
``report_dir`` per test — TSV content is deterministic, so sequential
write-overwrite is a no-op).

Strangler-fig invariant: ``lib/citation_verifier_mechanical.sh`` is NEVER
modified by this test (parity is enforced, not the bash itself). The bash
subprocess sources ONLY ``lib/citation_verifier_mechanical.sh`` — never
``gates_common.sh`` — so the comparison is free of the bash-only
``mo_grounded_rejection`` side-effect.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
from mini_ork.gates import citation_verifier_mechanical as cv

LIB_SH = REPO / "lib" / "citation_verifier_mechanical.sh"


# --------------------------------------------------------------------------- #
# Subprocess harness
# --------------------------------------------------------------------------- #

def _bash_check(doc: str, report_dir: str, env_extra: dict) -> tuple[dict | None, int]:
    """Run live bash mo_check_citations, return (parsed_json, rc)."""
    env = os.environ.copy()
    env.update(env_extra)
    proc = subprocess.run(
        ["bash", "-c",
         f'. "{LIB_SH}" && mo_check_citations "$1" "$2"',
         "_", doc, report_dir],
        env=env,
        capture_output=True,
        text=True,
    )
    raw = (proc.stdout or "").strip()
    parsed = json.loads(raw) if raw else None
    return parsed, proc.returncode


def _py_check(doc: str, report_dir: str, env_extra: dict) -> tuple[dict, int]:
    """Run Python port with the same env knobs bash sees."""
    saved: dict[str, str | None] = {}
    for k in ("MINI_ORK_ROOT", "MO_CITATION_COVERAGE_FLOOR", "MO_CITATION_MIN_COUNT"):
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


def _assert_parity(bash_dict: dict | None, py_dict: dict | None,
                   bash_rc: int, py_rc: int, label: str) -> None:
    assert bash_dict is not None and py_dict is not None, (
        f"{label}: empty output bash={bash_dict!r} py={py_dict!r}"
    )
    assert bash_rc == py_rc, f"{label}: rc drift bash={bash_rc} py={py_rc}"
    assert bash_dict == py_dict, (
        f"{label}: dict drift\n  bash={bash_dict!r}\n  py={py_dict!r}"
    )
    b_cov = bash_dict.get("coverage")
    p_cov = py_dict.get("coverage")
    if b_cov is not None and p_cov is not None:
        assert abs(b_cov - p_cov) < 1e-6, (
            f"{label}: coverage drift bash={b_cov} py={p_cov}"
        )


# --------------------------------------------------------------------------- #
# Fixture helpers
# --------------------------------------------------------------------------- #

def _make_repo(tmp_path: Path) -> Path:
    """Build a fake repo with two small source files (mirror bash self-test).
    Idempotent — safe to call twice."""
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True, exist_ok=True)
    (repo / "docs").mkdir(exist_ok=True)
    (repo / "src" / "foo.ts").write_text("line1\nline2\nline3\nline4\nline5\n")
    (repo / "src" / "bar.py").write_text("a\nb\nc\nd\ne\nf\ng\nh\ni\nj\n")
    return repo


def _run_pair(tmp_path: Path, doc_text: str, doc_relpath: str = "docs/synth.md",
              label: str = "") -> tuple[dict, int]:
    """Write a synth doc, run both engines sharing one report_dir.

    Asserts parity inside. Returns ``(bash_dict, bash_rc)`` for the caller's
    downstream value-level assertions — py_dict and py_rc are checked
    internally but discarded (bash's view is authoritative on naming).
    """
    repo = _make_repo(tmp_path)
    doc = repo / doc_relpath
    doc.parent.mkdir(parents=True, exist_ok=True)
    doc.write_text(doc_text)
    env_extra = {"MINI_ORK_ROOT": str(repo)}
    report_dir = tmp_path / "report"
    report_dir.mkdir()
    bd, brc = _bash_check(str(doc), str(report_dir), env_extra)
    pd, prc = _py_check(str(doc), str(report_dir), env_extra)
    _assert_parity(bd, pd, brc, prc, label)
    assert bd is not None  # parity assert would have raised otherwise
    return bd, brc


# --------------------------------------------------------------------------- #
# Fixtures (6 cases per kickoff)
# --------------------------------------------------------------------------- #

def test_01_all_valid_3_citations(tmp_path):
    bd, brc = _run_pair(tmp_path, (
        "Three valid anchors. See src/foo.ts:2 for the first claim and\n"
        "src/foo.ts:3-4 for the second, and src/bar.py:7 for the third.\n"
    ), label="01_all_valid")
    assert brc == 0
    assert bd["verdict"] == "citations_covered"
    assert bd["total_citations"] == 3
    assert bd["valid_citations"] == 3
    assert bd["invalid_citations"] == 0
    assert bd["unique_files"] == 2


def test_02_one_of_four_invalid_triggers_undercovered(tmp_path):
    bd, brc = _run_pair(tmp_path, (
        "Mixed: real src/foo.ts:2 and ghost src/missing.ts:5 and\n"
        "out-of-bounds src/foo.ts:9999 and real src/bar.py:1.\n"
    ), label="02_undercovered")
    assert brc == 1
    assert bd["verdict"] == "CITATION_UNDERCOVERED"
    assert bd["reason"] == "low_coverage"
    assert bd["total_citations"] == 4
    assert bd["valid_citations"] == 2
    assert bd["invalid_citations"] == 2


def test_03_zero_citations_indeterminate(tmp_path):
    bd, brc = _run_pair(tmp_path, (
        "A document with no file:line anchors at all, just narrative prose.\n"
    ), label="03_zero_cites")
    assert brc == 0
    assert bd["verdict"] == "indeterminate"
    assert bd["reason"] == "no_citations_found"
    assert bd["total_citations"] == 0


def test_04_missing_document(tmp_path):
    repo = _make_repo(tmp_path)
    env_extra = {"MINI_ORK_ROOT": str(repo)}
    report_dir = tmp_path / "report"
    report_dir.mkdir()
    missing = repo / "does-not-exist.md"
    bd, brc = _bash_check(str(missing), str(report_dir), env_extra)
    pd, prc = _py_check(str(missing), str(report_dir), env_extra)
    _assert_parity(bd, pd, brc, prc, "04_missing_doc")
    assert brc == 0
    assert bd is not None and bd["verdict"] == "indeterminate"
    assert bd is not None and bd["reason"] == "missing_document"
    # Shell-level early return OMITS report_path — both engines agree.
    assert "report_path" not in (bd or {})
    assert "report_path" not in (pd or {})


def test_05_absolute_path_accepted(tmp_path):
    """Absolute paths must resolve as-is (bash: ``os.path.isabs(path)``)."""
    repo = _make_repo(tmp_path)
    abs_foo = repo / "src" / "foo.ts"
    abs_bar = repo / "src" / "bar.py"
    text = (
        f"Abs anchor: {abs_foo}:2 and {abs_foo}:3-4 and {abs_bar}:5.\n"
    )
    bd, brc = _run_pair(tmp_path, text, label="05_abs_path")
    assert brc == 0
    assert bd["verdict"] == "citations_covered"
    assert bd["total_citations"] == 3
    assert bd["valid_citations"] == 3


def test_06_dedupe_identical(tmp_path):
    """Identical (path, start, end) tuples must collapse to one entry."""
    text = (
        "Repeated: src/foo.ts:2 src/foo.ts:2 src/foo.ts:2 "
        "and then src/foo.ts:3-4 src/foo.ts:3-4.\n"
        # Need at least one more unique citation so we hit the min_count=3 floor.
        "Plus src/bar.py:1.\n"
    )
    bd, brc = _run_pair(tmp_path, text, label="06_dedupe")
    assert brc == 0
    assert bd["verdict"] == "citations_covered"
    # 5 raw → 3 unique (foo.ts:2 once, foo.ts:3-4 once, bar.py:1)
    assert bd["total_citations"] == 3
    assert bd["valid_citations"] == 3
    assert bd["unique_files"] == 2


# --------------------------------------------------------------------------- #
# Strangler-fig + sibling-port smoke
# --------------------------------------------------------------------------- #

def test_bash_untouched():
    """Bash file must remain byte-identical — the port never edits it."""
    proc = subprocess.run(
        ["git", "diff", "--stat", "lib/citation_verifier_mechanical.sh"],
        cwd=str(REPO),
        capture_output=True, text=True,
    )
    assert proc.stdout.strip() == "", (
        f"bash was modified:\n{proc.stdout}\n{proc.stderr}"
    )


def test_import_and_keyshape():
    """Smoke: pure import + dict-shape sanity without subprocess."""
    d, rc = cv.check_citations("/no/such/file.md")
    assert rc == 0
    assert d["verdict"] == "indeterminate"
    assert d["reason"] == "missing_document"
    assert set(d.keys()) == {
        "verdict", "reason", "coverage", "coverage_floor",
        "total_citations", "valid_citations", "invalid_citations",
        "unique_files", "rationale",
    }