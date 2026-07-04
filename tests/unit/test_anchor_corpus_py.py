"""Parity gate: mini_ork.ported.anchor_corpus vs lib/anchor_corpus.sh.

Each test drives the LIVE bash functions ``anchor_corpus_load`` and
``anchor_corpus_recall`` via ``bash -c 'source lib/anchor_corpus.sh; ...'``
against the SAME JSON fixtures as the Python port, then deep-compares the
two outputs: floats within 1e-6, strings exact, JSON byte-stable. No mocks,
no hardcoded outputs beyond what bash itself emits.

Bash reads its knobs from the environment (``MO_CORPUS_RECALL_FLOOR``,
``MINI_ORK_RUN_DIR``); the Python port takes them as explicit kwargs AND
honors the env as fallback, so ``_compare`` exports the same values to the
bash subprocess that it passes to the Python call — both sides honour
identical knobs.

Nine cases (above the kickoff's >=6 floor):
  (a) load_valid_corpus                       → dict deep-equal
  (b) load_missing_required_field             → rc=2 + stderr "missing"; py raises
  (c) load_non_object_corpus                  → rc=2 + stderr "JSON object"; py raises
  (d) recall_hits_all_must_be_found           → rc=0 recall_meets_floor; py parity
  (e) recall_below_floor                      → rc=1 BELOW_FLOOR + recall=0.6667 missed=['A-003']; py parity
  (f) recall_missing_findings_path            → rc=0 indeterminate missing_inputs; py parity
  (g) recall_no_must_be_found                 → rc=0 indeterminate no_must_be_found; py parity
  (h) recall_tunable_floor (floor=0.5)        → fixture (e) flips to meets_floor; py parity
  (i) recall_file_line_only_match             → id-less findings matches via file:line substring; TSV row parity
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
from mini_ork.ported import anchor_corpus as ac  # noqa: E402

SH = REPO / "lib" / "anchor_corpus.sh"
_FLOAT_TOL = 1e-6


def _which_bash() -> None:
    if not shutil.which("bash"):
        pytest.skip("bash not on PATH")
    if not shutil.which("python3"):
        pytest.skip("python3 not on PATH (required by lib/anchor_corpus.sh)")
    if not SH.exists():
        pytest.skip(f"missing lib/anchor_corpus.sh at {SH}")


_BASH_HEADER = 'set -uo pipefail\nsource "{sh}"\n'


def _bash_load(path: Path, env: dict | None = None) -> subprocess.CompletedProcess:
    src = _BASH_HEADER.format(sh=SH) + f'anchor_corpus_load "{path}"\n'
    return subprocess.run(
        ["bash", "-c", src],
        env={**os.environ, **(env or {})},
        capture_output=True, text=True,
    )


def _bash_recall(
    findings: Path, corpus: Path, report_dir: Path,
    env: dict | None = None,
) -> subprocess.CompletedProcess:
    src = (
        _BASH_HEADER.format(sh=SH)
        + f'anchor_corpus_recall "{findings}" "{corpus}" "{report_dir}"\n'
    )
    return subprocess.run(
        ["bash", "-c", src],
        env={**os.environ, **(env or {})},
        capture_output=True, text=True,
    )


def _assert_parity(bash_obj, py_obj):
    """Deep-compare: same keys; floats within 1e-6; lists element-wise float-eq; else exact."""
    assert isinstance(bash_obj, dict) and isinstance(py_obj, dict), (
        f"not both dicts: bash={type(bash_obj).__name__} py={type(py_obj).__name__}"
    )
    assert set(bash_obj.keys()) == set(py_obj.keys()), (
        f"key mismatch\nbash={sorted(bash_obj)}\npy  ={sorted(py_obj)}"
    )
    for k in bash_obj:
        b, p = bash_obj[k], py_obj[k]
        if isinstance(b, bool) or isinstance(p, bool):
            assert b == p, f"key {k!r}: bash={b!r} py={p!r}"
        elif isinstance(b, (int, float)) and isinstance(p, (int, float)):
            assert abs(float(b) - float(p)) <= _FLOAT_TOL, (
                f"key {k!r}: bash={b!r} py={p!r} (diff > {_FLOAT_TOL})"
            )
        elif isinstance(b, list) and isinstance(p, list):
            assert len(b) == len(p), f"key {k!r}: length {len(b)} vs {len(p)}"
            for i, (bi, pi) in enumerate(zip(b, p)):
                if isinstance(bi, (int, float)) and isinstance(pi, (int, float)):
                    assert abs(float(bi) - float(pi)) <= _FLOAT_TOL, (
                        f"key {k!r}[{i}]: bash={bi!r} py={pi!r}"
                    )
                else:
                    assert bi == pi, f"key {k!r}[{i}]: bash={bi!r} py={pi!r}"
        else:
            assert b == p, f"key {k!r}: bash={b!r} py={p!r}"


# ─────────────────────────────────────────────────────────────────────────────
# Fixtures shared across recall tests (mirrors lib/anchor_corpus.sh:229-249).
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
# (a) load_valid_corpus — bash stdout JSON deep-equals parsed py dict.
# ─────────────────────────────────────────────────────────────────────────────
def test_load_valid_corpus(tmp_path):
    _which_bash()
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
    r = _bash_load(cp)
    assert r.returncode == 0, f"bash load rc={r.returncode}\nstderr={r.stderr}"
    bash_obj = json.loads(r.stdout)
    py_obj = ac.load_corpus(str(cp))
    _assert_parity(bash_obj, py_obj)
    assert py_obj["anchors"][0]["id"] == "A-1"


# ─────────────────────────────────────────────────────────────────────────────
# (b) load_missing_required_field — bash rc=2 + 'missing' on stderr; py raises.
# ─────────────────────────────────────────────────────────────────────────────
def test_load_missing_required_field(tmp_path):
    _which_bash()
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
    r = _bash_load(cp)
    assert r.returncode == 2
    assert "missing" in r.stderr
    with pytest.raises(ac.AnchorCorpusShapeError):
        ac.load_corpus(str(cp))


# ─────────────────────────────────────────────────────────────────────────────
# (c) load_non_object_corpus — bash rc=2 + 'must be a JSON object'; py raises.
# ─────────────────────────────────────────────────────────────────────────────
def test_load_non_object_corpus(tmp_path):
    _which_bash()
    cp = tmp_path / "bad-array.json"
    cp.write_text(json.dumps([{"id": "A-1"}]))  # root is list, not dict
    r = _bash_load(cp)
    assert r.returncode == 2
    assert "JSON object" in r.stderr
    with pytest.raises(ac.AnchorCorpusShapeError):
        ac.load_corpus(str(cp))


# ─────────────────────────────────────────────────────────────────────────────
# (d) recall_hits_all_must_be_found — rc=0 meets_floor; deep-equal JSON dict.
# ─────────────────────────────────────────────────────────────────────────────
def test_recall_hits_all_must_be_found(tmp_path, corpus_path, findings_good):
    _which_bash()
    report_dir = tmp_path / "rpt-d"
    r = _bash_recall(findings_good, corpus_path, report_dir)
    assert r.returncode == 0, f"bash recall rc={r.returncode}\nstderr={r.stderr}"
    bash_obj = json.loads(r.stdout)
    assert bash_obj["verdict"] == "recall_meets_floor"
    assert bash_obj["reason"] == "ok"
    assert bash_obj["found"] == 3
    assert bash_obj["must_be_found"] == 3
    assert bash_obj["missed_anchor_ids"] == []

    py_obj, py_rc = ac.score_recall(
        str(findings_good), str(corpus_path), report_dir=str(report_dir),
    )
    assert py_rc == 0
    _assert_parity(bash_obj, py_obj)


# ─────────────────────────────────────────────────────────────────────────────
# (e) recall_below_floor — rc=1 BELOW_FLOOR + recall=0.6667 missed=['A-003'].
# ─────────────────────────────────────────────────────────────────────────────
def test_recall_below_floor(tmp_path, corpus_path, findings_bad):
    _which_bash()
    report_dir = tmp_path / "rpt-e"
    r = _bash_recall(findings_bad, corpus_path, report_dir)
    assert r.returncode == 1
    bash_obj = json.loads(r.stdout)
    assert bash_obj["verdict"] == "RECALL_BELOW_FLOOR"
    assert bash_obj["reason"] == "low_recall"
    assert bash_obj["found"] == 2
    assert bash_obj["must_be_found"] == 3
    assert abs(bash_obj["recall"] - 0.6667) <= _FLOAT_TOL
    assert bash_obj["missed_anchor_ids"] == ["A-003"]

    py_obj, py_rc = ac.score_recall(
        str(findings_bad), str(corpus_path), report_dir=str(report_dir),
    )
    assert py_rc == 1
    _assert_parity(bash_obj, py_obj)


# ─────────────────────────────────────────────────────────────────────────────
# (f) recall_missing_findings_path — rc=0 indeterminate missing_inputs.
# ─────────────────────────────────────────────────────────────────────────────
def test_recall_missing_findings_path(tmp_path, corpus_path):
    _which_bash()
    bogus = tmp_path / "does-not-exist.md"
    report_dir = tmp_path / "rpt-f"
    r = _bash_recall(bogus, corpus_path, report_dir)
    assert r.returncode == 0
    bash_obj = json.loads(r.stdout)
    assert bash_obj["verdict"] == "indeterminate"
    assert bash_obj["reason"] == "missing_inputs"

    py_obj, py_rc = ac.score_recall(
        str(bogus), str(corpus_path), report_dir=str(report_dir),
    )
    assert py_rc == 0
    _assert_parity(bash_obj, py_obj)


# ─────────────────────────────────────────────────────────────────────────────
# (g) recall_no_must_be_found — rc=0 indeterminate no_must_be_found.
# ─────────────────────────────────────────────────────────────────────────────
def test_recall_no_must_be_found(tmp_path, findings_good):
    _which_bash()
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
    r = _bash_recall(findings_good, cp, report_dir)
    assert r.returncode == 0
    bash_obj = json.loads(r.stdout)
    assert bash_obj["verdict"] == "indeterminate"
    assert bash_obj["reason"] == "no_must_be_found"
    assert bash_obj["must_be_found"] == 0

    py_obj, py_rc = ac.score_recall(
        str(findings_good), str(cp), report_dir=str(report_dir),
    )
    assert py_rc == 0
    _assert_parity(bash_obj, py_obj)


# ─────────────────────────────────────────────────────────────────────────────
# (h) recall_tunable_floor — floor=0.5 flips fixture (e) to meets_floor.
# ─────────────────────────────────────────────────────────────────────────────
def test_recall_tunable_floor(tmp_path, corpus_path, findings_bad):
    _which_bash()
    report_dir = tmp_path / "rpt-h"
    r = _bash_recall(
        findings_bad, corpus_path, report_dir,
        env={"MO_CORPUS_RECALL_FLOOR": "0.5"},
    )
    assert r.returncode == 0
    bash_obj = json.loads(r.stdout)
    assert bash_obj["verdict"] == "recall_meets_floor"
    assert bash_obj["recall_floor"] == 0.5
    # 2/3 recall at floor=0.5 satisfies the gate.
    assert bash_obj["found"] == 2 and bash_obj["must_be_found"] == 3

    py_obj, py_rc = ac.score_recall(
        str(findings_bad), str(corpus_path),
        report_dir=str(report_dir), floor=0.5,
    )
    assert py_rc == 0
    _assert_parity(bash_obj, py_obj)


# ─────────────────────────────────────────────────────────────────────────────
# (i) recall_file_line_only_match — id-less findings match via file:line substring;
#     BOTH bash and py write the TSV with file=src/auth.ts line=42 found=yes.
#     1-of-3 found → recall 0.3333 < 0.8 → rc=1 BELOW_FLOOR (recall is below
#     floor, but the file-line match itself is verified via the TSV row).
# ─────────────────────────────────────────────────────────────────────────────
def test_recall_file_line_only_match(tmp_path, corpus_path, findings_idless):
    _which_bash()
    report_dir = tmp_path / "rpt-i"
    r = _bash_recall(findings_idless, corpus_path, report_dir)
    assert r.returncode == 1
    bash_obj = json.loads(r.stdout)
    assert bash_obj["verdict"] == "RECALL_BELOW_FLOOR"
    assert bash_obj["reason"] == "low_recall"
    assert bash_obj["found"] == 1
    assert bash_obj["missed_anchor_ids"] == ["A-002", "A-003"]

    # Verify bash's TSV row for the file-line match is correct.
    bash_tsv = (report_dir / "corpus-recall.tsv").read_text().splitlines()
    assert bash_tsv[0] == "anchor_id\tfile\tline\tseverity\tfound"
    row_a001 = next(r for r in bash_tsv[1:] if r.startswith("A-001\t"))
    assert row_a001 == "A-001\tsrc/auth.ts\t42\tP1\tyes"
    # A-002/A-003 had no citation in idless findings → found=no.
    row_a002 = next(r for r in bash_tsv[1:] if r.startswith("A-002\t"))
    assert row_a002.endswith("\tno")

    py_obj, py_rc = ac.score_recall(
        str(findings_idless), str(corpus_path),
        report_dir=str(report_dir),
    )
    assert py_rc == 1
    _assert_parity(bash_obj, py_obj)

    # Verify py's TSV matches bash byte-for-byte.
    py_tsv = (report_dir / "corpus-recall.tsv").read_text().splitlines()
    assert py_tsv == bash_tsv
