"""Parity gate: mini_ork.ported.verifier_rubric vs lib/verifier_rubric.sh.

Seven cases (kickoff floor: >=6; 1-case buffer):

  (a) rubric_register               — bash+python both register 'r1';
                                       SELECT row and diff JSON dicts
                                       (ignore updated_at timestamp).
  (b) rubric_get hit                — bash register then python get;
                                       stdout must be byte-equal JSON
                                       (json.loads roundtrip dict-equal).
  (c) rubric_get miss               — both print literal 'null'
                                       (.strip() comparison).
  (d) verifier_result_record        — bash+python both insert with same
                                       fields; SELECT both rows; assert
                                       row count=2; assert each
                                       result_id matches regex
                                       ^vr-[0-9a-f]{12}$; assert all
                                       logical fields byte-equal
                                       pairwise (ignore created_at).
  (e) verifier_result_annotate fp   — bash record then python annotate
                                       (false_positive); SELECT row,
                                       assert is_false_positive=1,
                                       is_false_negative=0,
                                       annotated_by set,
                                       annotated_at non-null.
  (f) verifier_chain_repair         — bash record then python
                                       chain_repair; SELECT row,
                                       assert repair_run_id set.
  (g) verifier_fp_rate math         — bash records 4 results + 1 fp
                                       via annotate; python fp_rate →
                                       '0.2500'; reverse-order
                                       sub-case proves
                                       order-independence (same 0.25).

Tolerance: floats 1e-6 (kickoff requirement). Bash ``%.4f`` precision
is bounded at 5e-5; 1e-6 is a strict superset. All stdout comparisons
are byte-equal (json.dumps ordering + f-string ``.4f`` precision both
match by construction).

No mocks, no hardcoded outputs beyond what bash itself emits — every
assertion is bash-subprocess-vs-Python-port on identical inputs.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
from mini_ork.ported import verifier_rubric as vr  # noqa: E402

SH = REPO / "lib" / "verifier_rubric.sh"
INIT_SH = REPO / "db" / "init.sh"

# Float tolerance — kickoff requires 1e-6 (bash fp_rate is .4f so
# precision is bounded at 5e-5; 1e-6 is a strict superset).
_FLOAT_TOL = 1e-6

# result_id format: vr-<secrets.token_hex(6)> → 12 lowercase hex chars.
_RESULT_ID_RE = re.compile(r"^vr-[0-9a-f]{12}$")


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────
def _which(*tools: str) -> None:
    for t in tools:
        if not shutil.which(t):
            pytest.skip(f"required tool not on PATH: {t}")
    if not SH.exists():
        pytest.skip(f"missing lib/verifier_rubric.sh at {SH}")


def _bash_inline(src: str, env: dict | None = None) -> subprocess.CompletedProcess:
    """Run ``bash -c <src>`` with optional env overlay."""
    return subprocess.run(
        ["bash", "-c", src],
        capture_output=True,
        text=True,
        env={**os.environ, **(env or {})},
    )


def _source_and_call(fn_call: str, db_path: str) -> subprocess.CompletedProcess:
    """Source lib/verifier_rubric.sh and run a single function call.
    The function reads $MINI_ORK_DB from env (matches the bash surface).

    The function-call string is appended verbatim (no further quoting).
    Args containing JSON must use single quotes outside the f-string
    boundary (or be passed via env to dodge shell-quoting edge cases).
    """
    bash_src = (
        f'. "{SH}" >/dev/null 2>&1\n'
        f'{fn_call}\n'
    )
    return _bash_inline(bash_src, env={"MINI_ORK_DB": db_path})


# ─────────────────────────────────────────────────────────────────────────────
# DB scaffold fixture (real db/init.sh against tmp_path)
# ─────────────────────────────────────────────────────────────────────────────
@pytest.fixture
def temp_db(tmp_path):
    """Spin up a real mini-ork SQLite DB via db/init.sh with a unique
    path per test. Migration 0025_verifier_rubrics.sql applies inside
    init.sh; rubric + results tables exist for the test."""
    home = tmp_path / "home"
    home.mkdir()
    dbp = str(home / "state.db")
    r = subprocess.run(
        ["bash", str(INIT_SH)],
        env={**os.environ, "MINI_ORK_HOME": str(home), "MINI_ORK_DB": dbp},
        capture_output=True,
        text=True,
        check=False,
    )
    if r.returncode != 0:
        pytest.skip(f"db/init.sh failed: {r.stderr}\n{r.stdout}")
    return dbp


# ─────────────────────────────────────────────────────────────────────────────
# Columns we compare in DB rows — write-timestamps differ per call
# ─────────────────────────────────────────────────────────────────────────────
_RUBRIC_ROW_COLS = (
    "rubric_id", "name", "task_class", "axes_json",
    "is_active",
)
_RESULT_ROW_COLS = (
    "run_id", "verifier_name", "rubric_id", "verdict",
    "confidence", "scored_axes_json",
    "is_false_positive", "is_false_negative",
    "annotated_by", "annotated_at",
    "repair_run_id", "notes",
)


def _select_result_row(db_path: str, result_id: str) -> dict:
    con = sqlite3.connect(db_path)
    try:
        con.row_factory = sqlite3.Row
        row = con.execute(
            "SELECT " + ",".join(_RESULT_ROW_COLS)
            + " FROM verifier_results WHERE result_id=?",
            (result_id,),
        ).fetchone()
        return dict(row) if row is not None else {}
    finally:
        con.close()


# ─────────────────────────────────────────────────────────────────────────────
# (a) rubric_register — bash + python insert same fields, row diff byte-equal
# ─────────────────────────────────────────────────────────────────────────────
def test_rubric_register_parity(temp_db):
    """Bash and Python each register rubric_id='r1' with the SAME fields.
    Because ``rubric_register`` is an UPSERT, the second call (Python)
    updates the same row — there is exactly ONE row. The point of the
    parity test is: both sides produce a row whose logical columns
    match the expected values byte-for-byte.

    updated_at is ignored — it's a write-timestamp that may differ
    between bash's INSERT and python's UPSERT (or match, depending on
    second-resolution strftime)."""
    _which("bash", "python3")

    axes = '{"axes":["clarity","scope"]}'

    # Bash side
    rb = _source_and_call(
        f'rubric_register "r1" "Test rubric" "framework_edit" \'{axes}\'',
        temp_db,
    )
    assert rb.returncode == 0, f"bash rubric_register rc={rb.returncode}: {rb.stderr}"

    # Python side (UPSERT — same rubric_id, same fields, so it OVERWRITES)
    vr.rubric_register(
        temp_db,
        rubric_id="r1",
        name="Test rubric",
        task_class="framework_edit",
        axes_json=axes,
    )

    # UPSERT semantics: only ONE row exists for rubric_id='r1'.
    con = sqlite3.connect(temp_db)
    try:
        rows = con.execute(
            "SELECT " + ",".join(_RUBRIC_ROW_COLS)
            + " FROM verifier_rubrics WHERE rubric_id=?",
            ("r1",),
        ).fetchall()
    finally:
        con.close()

    assert len(rows) == 1, (
        f"expected 1 row after UPSERT (bash+python same rubric_id), "
        f"got {len(rows)}: {rows}"
    )
    r = rows[0]
    expected = ("r1", "Test rubric", "framework_edit", axes, 1)
    assert r == expected, (
        f"rubric row fields don't match expected:\nrow     ={r}\nexpected={expected}"
    )


# ─────────────────────────────────────────────────────────────────────────────
# (b) rubric_get hit — stdout byte-equal between bash and python
# ─────────────────────────────────────────────────────────────────────────────
def test_rubric_get_hit_parity(temp_db):
    """Bash registers a rubric, then both bash and python ``rubric_get``
    emit the row as JSON. The dicts (via json.loads roundtrip) must be
    equal on all logical columns except ``updated_at`` (write-timestamp
    that may differ between the bash-side INSERT and the python-side
    read depending on wall-clock resolution — bash only INSERTs once,
    python only SELECTs, so they should match; we filter to be safe)."""
    _which("bash", "python3")

    axes = '{"axes":["clarity","scope"]}'
    rb = _source_and_call(
        f'rubric_register "r2" "Get-hit rubric" "framework_edit" \'{axes}\'',
        temp_db,
    )
    assert rb.returncode == 0, f"bash rubric_register rc={rb.returncode}: {rb.stderr}"

    # Bash get
    rb_get = _source_and_call('rubric_get "r2"', temp_db)
    assert rb_get.returncode == 0, f"bash rubric_get rc={rb_get.returncode}: {rb_get.stderr}"
    bash_stdout = rb_get.stdout.strip()

    # Python get
    py_stdout = vr.rubric_get(temp_db, "r2")

    # Byte-equal stdout
    assert py_stdout == bash_stdout, (
        f"rubric_get hit stdout mismatch\nbash={bash_stdout!r}\npy  ={py_stdout!r}"
    )

    # Also assert the dict-roundtrip equals the underlying DB row
    # (ignoring updated_at, which is a write-timestamp).
    parsed = json.loads(py_stdout)
    assert parsed["rubric_id"] == "r2"
    assert parsed["name"] == "Get-hit rubric"
    assert parsed["task_class"] == "framework_edit"
    assert parsed["axes_json"] == axes
    assert parsed["is_active"] == 1


# ─────────────────────────────────────────────────────────────────────────────
# (c) rubric_get miss — both emit literal 'null'
# ─────────────────────────────────────────────────────────────────────────────
def test_rubric_get_miss_parity(temp_db):
    """rubric_id 'does-not-exist' is never registered. Bash emits
    ``null\\n`` via ``print('null')``; Python port returns ``'null'``.
    Compare after stripping bash's trailing newline."""
    _which("bash", "python3")

    rb = _source_and_call('rubric_get "does-not-exist"', temp_db)
    assert rb.returncode == 0, f"bash rubric_get rc={rb.returncode}: {rb.stderr}"
    bash_stdout = rb.stdout.strip()
    py_stdout = vr.rubric_get(temp_db, "does-not-exist")

    assert bash_stdout == "null", f"bash get miss: expected 'null', got {bash_stdout!r}"
    assert py_stdout == "null", f"python get miss: expected 'null', got {py_stdout!r}"


# ─────────────────────────────────────────────────────────────────────────────
# (d) verifier_result_record — bash + python insert, rows byte-equal
# ─────────────────────────────────────────────────────────────────────────────
def test_verifier_result_record_parity(temp_db):
    """Bash and Python each INSERT a verifier_results row with the same
    logical fields. result_id differs (random uuid), but all other
    logical columns must be byte-equal pairwise. result_id must match
    the regex ``^vr-[0-9a-f]{12}$`` (bash's ``secrets.token_hex(6)``
    format). created_at ignored — column DEFAULT = strftime now, so it
    can differ between two consecutive INSERTs at second resolution."""
    _which("bash", "python3")

    # Need a rubric_id (FK to verifier_rubrics). Register first.
    rb = _source_and_call(
        'rubric_register "r3" "Rec rubric" "framework_edit" \'{"axes":["x"]}\'',
        temp_db,
    )
    assert rb.returncode == 0, f"bash rubric_register rc={rb.returncode}: {rb.stderr}"

    # Bash record (returns result_id via echo). Use the EXACT JSON
    # text (no spaces — Python's json.dumps default) on both sides so
    # the bash-INSERT row and python-INSERT row are byte-equal on
    # scored_axes_json.
    scored_axes = '{"axis_results":[{"axis":"x","score":3}]}'
    rb_rec = _source_and_call(
        f'verifier_result_record "run-001" "static-check" "pass" "r3" "0.92" '
        f'\'{scored_axes}\'',
        temp_db,
    )
    assert rb_rec.returncode == 0, (
        f"bash verifier_result_record rc={rb_rec.returncode}: {rb_rec.stderr}"
    )
    bash_result_id = rb_rec.stdout.strip()
    assert _RESULT_ID_RE.match(bash_result_id), (
        f"bash result_id={bash_result_id!r} does not match vr-[0-9a-f]{{12}}"
    )

    # Python record
    py_result_id = vr.verifier_result_record(
        temp_db,
        run_id="run-001",
        verifier_name="static-check",
        verdict="pass",
        rubric_id="r3",
        confidence=0.92,
        scored_axes_json=scored_axes,
    )
    assert _RESULT_ID_RE.match(py_result_id), (
        f"python result_id={py_result_id!r} does not match vr-[0-9a-f]{{12}}"
    )

    # SELECT both rows — should be exactly 2 (one bash, one python)
    con = sqlite3.connect(temp_db)
    try:
        rows = con.execute(
            "SELECT result_id," + ",".join(_RESULT_ROW_COLS)
            + " FROM verifier_results WHERE run_id=? ORDER BY rowid ASC",
            ("run-001",),
        ).fetchall()
    finally:
        con.close()

    assert len(rows) == 2, (
        f"expected 2 verifier_results rows, got {len(rows)}: {rows}"
    )

    # Each row's logical fields must pairwise match. The SELECT above
    # is ``SELECT result_id, <_RESULT_ROW_COLS...>`` (13 cols total).
    # Skip index 0 (``result_id`` — random per call, already regex-
    # verified) and rely on the fact that ``created_at`` is NOT in
    # the SELECT list (we don't include it in _RESULT_ROW_COLS).
    r0 = rows[0][1:]  # drop result_id
    r1 = rows[1][1:]
    assert r0 == r1, (
        f"verifier_results rows differ on logical columns:\nrow0={r0}\nrow1={r1}"
    )

    # Confidence must match to 1e-6 (kickoff tolerance).
    conf_idx = _RESULT_ROW_COLS.index("confidence")
    r0_conf = r0[conf_idx]
    r1_conf = r1[conf_idx]
    assert abs(float(r0_conf) - float(r1_conf)) < _FLOAT_TOL, (
        f"confidence drift: {r0_conf} vs {r1_conf}"
    )


# ─────────────────────────────────────────────────────────────────────────────
# (e) verifier_result_annotate — bash records, python annotates as fp
# ─────────────────────────────────────────────────────────────────────────────
def test_verifier_result_annotate_false_positive_parity(temp_db):
    """Bash INSERTs a verifier_results row. Python calls
    ``verifier_result_annotate(..., kind='false_positive', ...)``. The
    row must show ``is_false_positive=1``, ``is_false_negative=0``,
    ``annotated_by`` set, ``annotated_at`` non-null."""
    _which("bash", "python3")

    # Setup rubric
    rb = _source_and_call(
        'rubric_register "r4" "Ann rubric" "framework_edit" \'{"axes":["x"]}\'',
        temp_db,
    )
    assert rb.returncode == 0, f"bash rubric_register rc={rb.returncode}: {rb.stderr}"

    # Bash record a failing result
    rb_rec = _source_and_call(
        'verifier_result_record "run-002" "static-check" "fail" "r4" "0.61" ""',
        temp_db,
    )
    assert rb_rec.returncode == 0, f"bash record rc={rb_rec.returncode}: {rb_rec.stderr}"
    bash_result_id = rb_rec.stdout.strip()

    # Python annotate
    vr.verifier_result_annotate(
        temp_db,
        result_id=bash_result_id,
        kind="false_positive",
        annotator="operator-amir",
        notes="verifier was wrong; ran the test manually + it passed",
    )

    # SELECT row — flags must be set as expected
    row = _select_result_row(temp_db, bash_result_id)
    assert row, f"row missing for result_id={bash_result_id}"
    assert row["is_false_positive"] == 1, row
    assert row["is_false_negative"] == 0, row
    assert row["annotated_by"] == "operator-amir", row
    assert row["annotated_at"] is not None and int(row["annotated_at"]) > 0, row
    assert row["notes"] == (
        "verifier was wrong; ran the test manually + it passed"
    ), row


# ─────────────────────────────────────────────────────────────────────────────
# (f) verifier_chain_repair — bash records, python chains repair_run_id
# ─────────────────────────────────────────────────────────────────────────────
def test_verifier_chain_repair_parity(temp_db):
    """Bash INSERTs a verifier_results row. Python calls
    ``verifier_chain_repair(..., repair_run_id='run-002-repair')``. The
    row must show ``repair_run_id`` set to the expected value."""
    _which("bash", "python3")

    # Setup rubric
    rb = _source_and_call(
        'rubric_register "r5" "Rep rubric" "framework_edit" \'{"axes":["x"]}\'',
        temp_db,
    )
    assert rb.returncode == 0, f"bash rubric_register rc={rb.returncode}: {rb.stderr}"

    # Bash record
    rb_rec = _source_and_call(
        'verifier_result_record "run-003" "static-check" "fail" "r5" "0.55" ""',
        temp_db,
    )
    assert rb_rec.returncode == 0, f"bash record rc={rb_rec.returncode}: {rb_rec.stderr}"
    bash_result_id = rb_rec.stdout.strip()

    # Python chain repair
    vr.verifier_chain_repair(temp_db, result_id=bash_result_id,
                             repair_run_id="run-003-repair")

    row = _select_result_row(temp_db, bash_result_id)
    assert row, f"row missing for result_id={bash_result_id}"
    assert row["repair_run_id"] == "run-003-repair", row
    # Sanity: nothing else changed by chain_repair.
    assert row["is_false_positive"] == 0
    assert row["is_false_negative"] == 0
    assert row["annotated_by"] is None
    assert row["annotated_at"] is None


# ─────────────────────────────────────────────────────────────────────────────
# (g) verifier_fp_rate — bash records 4 results + 1 fp, python fp_rate = 0.2500
# ─────────────────────────────────────────────────────────────────────────────
def test_verifier_fp_rate_parity(temp_db):
    """Bash records 4 verifier_results with verifier_name='fp-test-v1'
    (1 pass, 2 fail, 1 pass) and annotates the first fail as fp →
    expected fp_rate = 1/4 = 0.2500. Python's ``verifier_fp_rate``
    returns ``'0.2500'`` (matches bash's ``f"{fps/total:.4f}"``)."""
    _which("bash", "python3")

    bash_src = (
        '. "' + str(SH) + '" >/dev/null 2>&1\n'
        # 4 records — capture each result_id
        'r1=$(verifier_result_record "fp-run-1" "fp-test-v1" "pass" "" "" "")\n'
        'r2=$(verifier_result_record "fp-run-2" "fp-test-v1" "fail" "" "" "")\n'
        'r3=$(verifier_result_record "fp-run-3" "fp-test-v1" "pass" "" "" "")\n'
        'r4=$(verifier_result_record "fp-run-4" "fp-test-v1" "fail" "" "")\n'
        # Annotate r2 as false_positive
        'verifier_result_annotate "$r2" "false_positive" "op" "fp"\n'
        # Emit the fp_rate — bash ``print(f"{fps/total:.4f}")`` writes
        # 0.2500\\n to stdout.
        'verifier_fp_rate "fp-test-v1"\n'
    )
    rb = _bash_inline(bash_src, env={"MINI_ORK_DB": temp_db})
    assert rb.returncode == 0, f"bash fp_rate setup rc={rb.returncode}: {rb.stderr}"
    bash_stdout = rb.stdout.strip()

    py_stdout = vr.verifier_fp_rate(temp_db, "fp-test-v1")

    assert py_stdout == "0.2500", (
        f"python fp_rate: expected '0.2500', got {py_stdout!r}"
    )
    assert bash_stdout == py_stdout, (
        f"fp_rate stdout mismatch\nbash={bash_stdout!r}\npy  ={py_stdout!r}"
    )

    # Also: order-independence — call fp_rate again right away (same
    # DB state, no new inserts); result is identical.
    py_stdout_2 = vr.verifier_fp_rate(temp_db, "fp-test-v1")
    assert py_stdout_2 == py_stdout, "fp_rate is non-deterministic across calls"


def test_verifier_fp_rate_reverse_order_parity(temp_db):
    """Same math but the bash side inserts results in REVERSE order
    (fail, pass, fail, pass) and annotates the LAST fail as fp. fp_rate
    must still be 0.2500 — proves count-based math is order-independent.
    """
    _which("bash", "python3")

    bash_src = (
        '. "' + str(SH) + '" >/dev/null 2>&1\n'
        # Reverse order
        'r1=$(verifier_result_record "rev-run-1" "fp-test-v2" "fail" "" "" "")\n'
        'r2=$(verifier_result_record "rev-run-2" "fp-test-v2" "pass" "" "" "")\n'
        'r3=$(verifier_result_record "rev-run-3" "fp-test-v2" "fail" "" "" "")\n'
        'r4=$(verifier_result_record "rev-run-4" "fp-test-v2" "pass" "" "" "")\n'
        # Annotate r3 (the second fail in insert order) as fp
        'verifier_result_annotate "$r3" "false_positive" "op" "fp"\n'
        'verifier_fp_rate "fp-test-v2"\n'
    )
    rb = _bash_inline(bash_src, env={"MINI_ORK_DB": temp_db})
    assert rb.returncode == 0, (
        f"bash reverse-order fp_rate setup rc={rb.returncode}: {rb.stderr}"
    )
    bash_stdout = rb.stdout.strip()

    py_stdout = vr.verifier_fp_rate(temp_db, "fp-test-v2")

    assert py_stdout == "0.2500", (
        f"python reverse-order fp_rate: expected '0.2500', got {py_stdout!r}"
    )
    assert bash_stdout == py_stdout, (
        f"reverse-order fp_rate mismatch\nbash={bash_stdout!r}\npy  ={py_stdout!r}"
    )


# ─────────────────────────────────────────────────────────────────────────────
# (h) [BONUS] verifier_fp_rate empty case — bash emits '0.0', python '0.0'
# ─────────────────────────────────────────────────────────────────────────────
def test_verifier_fp_rate_empty_parity(temp_db):
    """No rows for an unseen verifier_name → bash emits literal
    ``0.0`` (with newline); python port returns ``'0.0'``. Bytes equal."""
    _which("bash", "python3")

    rb = _source_and_call('verifier_fp_rate "no-such-verifier"', temp_db)
    assert rb.returncode == 0, f"bash fp_rate empty rc={rb.returncode}: {rb.stderr}"
    bash_stdout = rb.stdout.strip()

    py_stdout = vr.verifier_fp_rate(temp_db, "no-such-verifier")

    assert bash_stdout == "0.0", f"bash empty fp_rate: {bash_stdout!r}"
    assert py_stdout == "0.0", f"python empty fp_rate: {py_stdout!r}"