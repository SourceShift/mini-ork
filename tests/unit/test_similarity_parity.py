"""Parity gate: ``mini_ork.ported.similarity.rank`` vs ``bash lib/similarity.sh``.

For each fixture we seed a fresh sqlite DB under one whitelisted
``(table, text_col)`` pair, invoke the LIVE bash function via subprocess
(no mocking, exactly as the production runtime would), then call the
Python port with the fetched column values and compare the resulting JSON
shapes. Scores must match within ``1e-6`` (post-rounding); row payloads
must match exactly.

Strangler-fig co-existence is preserved: ``lib/similarity.sh`` is
byte-identical before and after this test exists. The test only WRITES to
its ``tmp_path`` and READS from ``lib/similarity.sh``.
"""

from __future__ import annotations

import json
import math
import os
import sqlite3
import subprocess
from pathlib import Path

import pytest

from mini_ork.ported.similarity import rank

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
LIB_SIMILARITY = REPO_ROOT / "lib" / "similarity.sh"

# Must mirror lib/similarity.sh::ALLOWED. We only seed against ``bug_reports``
# here (simplest whitelisted table); extending to the others is plumbing, not
# new parity surface.
_TABLE = "bug_reports"
_TEXT_COL = "title"


def _seed(db_path: Path, rows: list[dict]) -> None:
    con = sqlite3.connect(str(db_path))
    con.execute(f"CREATE TABLE {_TABLE} (id INTEGER PRIMARY KEY, {_TEXT_COL} TEXT)")
    for r in rows:
        con.execute(
            f"INSERT INTO {_TABLE} (id, {_TEXT_COL}) VALUES (?, ?)",
            (r["id"], r[_TEXT_COL]),
        )
    con.commit()
    con.close()


def _fetch_rows(db_path: Path) -> list[dict]:
    """Re-read rows in the SAME shape bash's heredoc sees (SELECT rowid AS rid, *)."""
    con = sqlite3.connect(str(db_path))
    con.row_factory = sqlite3.Row
    rows = con.execute(
        f"SELECT rowid AS rid, * FROM {_TABLE}"
    ).fetchall()
    out = [dict(r) for r in rows]
    con.close()
    return out


def _run_bash(db_path: Path, query: str, limit: int) -> list:
    env = os.environ.copy()
    env["MINI_ORK_DB"] = str(db_path)
    proc = subprocess.run(
        ["bash", "-c",
         f'. "{LIB_SIMILARITY}" && similarity_query "{_TABLE}" "{_TEXT_COL}" '
         f'"{query}" {limit}'],
        cwd=str(REPO_ROOT),
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
    raw = proc.stdout.strip()
    return json.loads(raw) if raw else []


def _py_match(db_path: Path, query: str, limit: int) -> list:
    rows = _fetch_rows(db_path)
    docs = [(r.get(_TEXT_COL) or "") for r in rows]
    scored = rank(query, docs, limit=limit)
    return [{"score": s, "row": rows[i]} for s, i in scored]


def _assert_parity(bash_out: list, py_out: list) -> None:
    assert len(bash_out) == len(py_out), (
        f"length drift: bash={len(bash_out)} py={len(py_out)} "
        f"bash={bash_out!r} py={py_out!r}"
    )
    for b, p in zip(bash_out, py_out):
        assert math.isclose(b["score"], p["score"], abs_tol=1e-6), (
            f"score drift: bash={b['score']!r} py={p['score']!r}"
        )
        assert b["row"] == p["row"], f"row drift: bash={b['row']!r} py={p['row']!r}"


# Fixtures cover the input surface that exercises TF-IDF + IDF + rank orderings.
# Each writes to its own tmp_path slot thanks to ``tmp_path`` being per-fixture.
def _f01_single_doc_only_match():
    return {
        "rows": [
            {"id": 1, _TEXT_COL: "fix bug in auth middleware"},
            {"id": 2, _TEXT_COL: "totally unrelated marketing draft"},
            {"id": 3, _TEXT_COL: "another unrelated item"},
        ],
        "query": "auth middleware",
        "limit": 5,
    }


def _f02_multi_doc_ranking():
    return {
        "rows": [
            {"id": 1, _TEXT_COL: "auth bug fix tokens expired"},
            {"id": 2, _TEXT_COL: "token refresh logic flow"},
            {"id": 3, _TEXT_COL: "completely unrelated thing"},
        ],
        "query": "auth tokens",
        "limit": 5,
    }


def _f03_empty_db():
    return {"rows": [], "query": "anything", "limit": 5}


def _f04_query_with_no_qualifying_tokens():
    return {
        "rows": [
            {"id": 1, _TEXT_COL: "fix bug in auth middleware"},
            {"id": 2, _TEXT_COL: "another item here"},
        ],
        "query": "a b c d",  # all len < 3 after tok; q_vec ends up empty
        "limit": 5,
    }


def _f05_limit_truncation():
    return {
        "rows": [
            {"id": i, _TEXT_COL: f"match entry number {i}"}
            for i in range(1, 6)
        ],
        "query": "match entry",
        "limit": 2,
    }


def _f06_idf_downweights_common_token():
    # word "fix" appears in every doc; only "race" or "condition" should
    # discriminate. Both ranks should match bash, verifying the IDF log.
    return {
        "rows": [
            {"id": 1, _TEXT_COL: "fix the race condition"},
            {"id": 2, _TEXT_COL: "fix the leak condition"},
            {"id": 3, _TEXT_COL: "fix the race detector"},
            {"id": 4, _TEXT_COL: "fix the leak detector"},
        ],
        "query": "race condition",
        "limit": 5,
    }


def _f07_special_chars_preserved():
    # dots, slashes, underscores, hyphens are KEPT by the tokenizer
    return {
        "rows": [
            {"id": 1, _TEXT_COL: "fix: error.foo/bar_v2 — retry!"},
            {"id": 2, _TEXT_COL: "another doc with no overlap"},
        ],
        "query": "foo/bar_v2",
        "limit": 5,
    }


def _f08_duplicates_exact_match():
    return {
        "rows": [
            {"id": 1, _TEXT_COL: "alpha beta gamma"},
            {"id": 2, _TEXT_COL: "alpha beta gamma"},
            {"id": 3, _TEXT_COL: "delta epsilon"},
        ],
        "query": "alpha beta",
        "limit": 5,
    }


FIXTURES = {
    "01_single_doc_only_match":            _f01_single_doc_only_match(),
    "02_multi_doc_ranking":                _f02_multi_doc_ranking(),
    "03_empty_db":                         _f03_empty_db(),
    "04_query_with_no_qualifying_tokens":  _f04_query_with_no_qualifying_tokens(),
    "05_limit_truncation":                 _f05_limit_truncation(),
    "06_idf_downweights_common_token":     _f06_idf_downweights_common_token(),
    "07_special_chars_preserved":          _f07_special_chars_preserved(),
    "08_duplicates_exact_match":           _f08_duplicates_exact_match(),
}


@pytest.mark.parametrize("fix", list(FIXTURES.values()), ids=list(FIXTURES.keys()))
def test_similarity_query_matches_bash(tmp_path, fix):
    db_path = tmp_path / "sim.sqlite"
    _seed(db_path, fix["rows"])

    bash_out = _run_bash(db_path, fix["query"], fix["limit"])
    py_out = _py_match(db_path, fix["query"], fix["limit"])

    _assert_parity(bash_out, py_out)


def test_smoke_import_and_rank_no_db():
    """Module imports and ranks without I/O — proves the pure path works in isolation."""
    out = rank("auth bug", ["auth middleware bug", "unrelated item"], limit=3)
    assert isinstance(out, list)
    assert all(isinstance(p, tuple) and len(p) == 2 for p in out)
    assert all(isinstance(p[0], float) and isinstance(p[1], int) for p in out)
    assert all(0.0 < p[0] <= 1.0 for p in out)
    # The auth-relevant doc (index 0) must rank above the unrelated one.
    assert out[0][1] == 0
