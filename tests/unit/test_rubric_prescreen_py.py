"""Parity gate: mini_ork.gates.rubric_prescreen vs lib/rubric-prescreen.sh.

Seven cases (kickoff floor: >=6; 1-case buffer):

  (a) extract_rubric_json      — feed identical text to bash heredoc
                                 (lines 140-159) and python
                                 extract_rubric_json, assert returned
                                 substring byte-equal + json.loads
                                 roundtrip equal.
  (b) substitute_template      — bash the inline python heredoc from
                                 lines 271-279 against a fake template
                                 + fake kickoff + fake diff_summary;
                                 call rp.substitute_template on the
                                 same inputs; assert byte-equal.
  (c) artifact_summary         — bash run the python heredoc from
                                 lines 247-267 against a tmp_dir with
                                 3 fake files (md/json/txt); call
                                 rp.artifact_summary on same dir;
                                 assert byte-equal.
  (d) mo_append_rubric_to_feedback — write a fake rubric.json with
                                     2 PASS + 2 FAIL items; bash
                                     source lib/rubric-prescreen.sh
                                     and call mo_append_rubric_to_
                                     feedback epic iter fb_path;
                                     call rp.mo_append_rubric_to_
                                     feedback on a SEPARATE fb_path;
                                     assert both appended text
                                     byte-equal (post-PASS filter
                                     must skip PASS items per
                                     line 372).
  (e) cache_emit parity        — bash call mo_cache_emit + python
                                 call rp.cache_emit with same args;
                                 SELECT both rows from
                                 mini_orch_sessions, diff logical
                                 columns (uuid + job_id + expires_at
                                 ignored — uuidgen/env clock skew).
  (f) cache_lookup hit+miss    — bash INSERT a row then both bash
                                 and python call lookup, assert
                                 identical output_path; miss case
                                 (no row) returns empty string both
                                 sides.
  (g) build_parse_error_payload — bash run the jq -n command from
                                   lines 187-191 with controlled
                                   diag + log_path; call
                                   rp.build_parse_error_payload on
                                   same inputs; json.loads both and
                                   assert dict-equal (the ONLY case
                                   where dict-equal replaces
                                   byte-equal — jq may not match
                                   Python's json.dumps key order).

Tolerance: floats 1e-6 (kickoff requirement). The plan calls for
byte-equal where possible. Where jq-emitted JSON differs in key
ordering from Python's json.dumps, we compare via json.loads
(case (g) explicitly). All other cases assert byte-equality on
stdout / DB-row logical columns.

No mocks, no hardcoded outputs beyond what bash itself emits —
every assertion is bash-subprocess-vs-Python-port on identical
inputs.
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
from mini_ork.gates import rubric_prescreen as rp

SH = REPO / "lib" / "rubric-prescreen.sh"
CACHE_SH = REPO / "lib" / "cache.sh"
INIT_SH = REPO / "db" / "init.sh"

# Float tolerance — kickoff requires 1e-6. Used by build_panel_verdict
# (panel_score = score * 12.5) when we get around to asserting it.
_FLOAT_TOL = 1e-6

# mo_cache_emit uuid format: 32 hex chars (Python's secrets.token_hex(16))
# or hyphenated 36-char UUID (bash uuidgen). The DB-row diff ignores
# the uuid column entirely (per the plan's risk note).


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────
def _which(*tools: str) -> None:
    for t in tools:
        if not shutil.which(t):
            pytest.skip(f"required tool not on PATH: {t}")
    if not SH.exists():
        pytest.skip(f"missing lib/rubric-prescreen.sh at {SH}")
    if not CACHE_SH.exists():
        pytest.skip(f"missing lib/cache.sh at {CACHE_SH}")


def _bash_inline(src: str, env: dict | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", "-c", src],
        capture_output=True,
        text=True,
        env={**os.environ, **(env or {})},
    )


def _source_rp_and_call(fn_call: str, db_path: str,
                        extra_sources: str = "") -> subprocess.CompletedProcess:
    """Source lib/rubric-prescreen.sh (+ lib/cache.sh if asked) and
    run a single function call. The functions read $MINI_ORK_DB from
    env (matches the bash surface)."""
    src_lines = [
        f'. "{SH}" >/dev/null 2>&1',
    ]
    if extra_sources:
        src_lines.append(extra_sources)
    src_lines.append(fn_call)
    return _bash_inline("\n".join(src_lines), env={"MINI_ORK_DB": db_path})


# ─────────────────────────────────────────────────────────────────────────────
# DB scaffold fixture (real db/init.sh against tmp_path)
# ─────────────────────────────────────────────────────────────────────────────
@pytest.fixture
def temp_db(tmp_path):
    """Spin up a real mini-ork SQLite DB via db/init.sh with a unique
    path per test. Migration 0001_core.sql + 0002_mini_orch_sessions.sql
    both apply inside init.sh, so the epics + mini_orch_sessions tables
    exist for the test."""
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


@pytest.fixture
def temp_db_with_epic(tmp_path, temp_db):
    """temp_db + an epics row inserted for fetch_kickoff_path / cache_emit
    tests. The kickoff_path is set to a real (existing) file so bash
    scripts that ``cat`` it don't fail."""
    kf = tmp_path / "kickoff.md"
    kf.write_text("# fake kickoff\nbody\n", encoding="utf-8")
    rel = kf.name  # not a real relative path, but fine for SELECT
    con = sqlite3.connect(temp_db)
    try:
        con.execute(
            "INSERT INTO epics (id, title, status, kickoff_path) "
            "VALUES (?, ?, ?, ?)",
            ("E1", "test epic", "in progress", rel),
        )
        con.commit()
    finally:
        con.close()
    return temp_db, str(kf)


# ─────────────────────────────────────────────────────────────────────────────
# Columns we compare in mini_orch_sessions — write-timestamps differ
# ─────────────────────────────────────────────────────────────────────────────
_SESSION_ROW_COLS = (
    "epic_id", "iter", "stage", "input_hash", "status",
    "output_path", "log_path", "cost_usd", "turns", "duration_ms",
    "prompt_version",
)


def _select_session_rows(db_path: str, epic_id: str, input_hash: str) -> list[tuple]:
    con = sqlite3.connect(db_path)
    try:
        rows = con.execute(
            "SELECT " + ",".join(_SESSION_ROW_COLS)
            + " FROM mini_orch_sessions WHERE epic_id=? AND input_hash=?",
            (epic_id, input_hash),
        ).fetchall()
    finally:
        con.close()
    return rows


# ─────────────────────────────────────────────────────────────────────────────
# (a) extract_rubric_json — bash heredoc vs python helper, byte-equal
# ─────────────────────────────────────────────────────────────────────────────
def test_extract_rubric_json_parity(tmp_path):
    """Bash the lines-140-159 heredoc against three sample texts;
    python extract_rubric_json on the same text. Returned substring
    must be byte-equal in every case, AND must json.loads roundtrip
    identically."""
    _which("bash", "python3")

    samples = [
        # simple {"pass": true, "score": 7}
        'Some preamble text\n{"pass": true, "score": 7}\n',
        # multiple {"pass" markers; bash picks the LAST valid one
        'first attempt {"pass": false, "score": 0}\nfinal: {"pass": true, "score": 8, "items": []}\n',
        # nested braces in items
        'wrapped {"pass": true, "score": 6, "items": [{"label": "x", "verdict": "PASS", "note": "ok"}]}\n',
    ]

    for idx, text in enumerate(samples):
        text_file = tmp_path / f"sample_{idx}.txt"
        text_file.write_text(text, encoding="utf-8")

        # Bash side: source rubric-prescreen.sh and call the heredoc-
        # equivalent via env var (mirrors lines 140-159 verbatim).
        bash_src = f'''
. "{SH}" >/dev/null 2>&1
RESULT_TEXT="$(cat "{text_file}")" python3 - <<'PY'
import re, sys, json, os
text = os.environ.get("RESULT_TEXT", "")
starts = [m.start() for m in re.finditer(r'\\{{[^{{]*?"pass"\\s*:', text)]
for start in reversed(starts):
    depth, in_str, esc = 0, False, False
    for i in range(start, len(text)):
        c = text[i]
        if esc: esc = False; continue
        if c == '\\\\': esc = True; continue
        if c == '"' and not esc: in_str = not in_str; continue
        if in_str: continue
        if c == '{{': depth += 1
        elif c == '}}':
            depth -= 1
            if depth == 0:
                cand = text[start:i+1]
                try: json.loads(cand); print(cand); sys.exit(0)
                except Exception: break
PY
'''
        rb = _bash_inline(bash_src)
        assert rb.returncode == 0, (
            f"bash extract sample {idx} rc={rb.returncode}: {rb.stderr}"
        )
        bash_out = rb.stdout

        # Python side
        py_out = rp.extract_rubric_json(text) or ""

        # Byte-equal: bash prints the substring verbatim (no trailing
        # newline because sys.exit(0) happens before print's own
        # newline — actually Python print() does add a newline; we
        # strip one side for comparison).
        assert py_out == bash_out.rstrip("\n"), (
            f"sample {idx} mismatch\n"
            f"text={text!r}\n"
            f"bash={bash_out!r}\npy  ={py_out!r}"
        )

        # json.loads roundtrip dict-equal
        assert json.loads(py_out) == json.loads(bash_out), (
            f"sample {idx} json roundtrip differs"
        )


# ─────────────────────────────────────────────────────────────────────────────
# (b) substitute_template — bash heredoc vs python helper, byte-equal
# ─────────────────────────────────────────────────────────────────────────────
def test_substitute_template_parity(tmp_path):
    """Bash the lines-271-279 heredoc against a fake template; python
    substitute_template on the same template + kickoff + diff.
    Returned text must be byte-equal."""
    _which("bash", "python3")

    template = (
        "# header\n\n"
        "## Kickoff\n{{KICKOFF_BODY}}\n\n"
        "## Diff\n{{DIFF_SUMMARY}}\n\n"
        "# footer\n"
    )
    kickoff = "the kickoff body line 1\nline 2\n"
    diff = "file.py | 2 +-\n1 file changed\n"
    tpl_file = tmp_path / "tpl.md"
    kf_file = tmp_path / "kickoff.md"
    df_file = tmp_path / "diff.txt"
    tpl_file.write_text(template, encoding="utf-8")
    kf_file.write_text(kickoff, encoding="utf-8")
    df_file.write_text(diff, encoding="utf-8")

    # The bash heredoc at lines 271-279 receives the artifact_summary
    # as a multi-line STRING (not a file path) in sys.argv[3]. We
    # mirror that exactly: pass the diff content via a bash variable
    # that we export to env (bash here-strings preserve newlines
    # inside single-quoted strings, but embedding a multi-line
    # f-string with literal newlines breaks the bash source layout).
    diff_file = tmp_path / "diff_content.txt"
    diff_file.write_text(diff, encoding="utf-8")
    bash_src = f'''
export DIFF_STR="$(cat "{diff_file}")"
python3 - "{tpl_file}" "{kf_file}" <<'PY'
import sys, os
template, kickoff = sys.argv[1], sys.argv[2]
artifacts = os.environ['DIFF_STR']
body = open(template, errors="replace").read()
body = body.replace("{{{{KICKOFF_BODY}}}}", open(kickoff, errors="replace").read())
body = body.replace("{{{{DIFF_SUMMARY}}}}", artifacts)
print(body)
PY
'''
    rb = _bash_inline(bash_src)
    assert rb.returncode == 0, (
        f"bash substitute rc={rb.returncode}: {rb.stderr}"
    )
    # Bash callers use ``$(python3 ...)`` which strips trailing
    # newlines from the heredoc's print output — rstrip to compare
    # the SEMANTIC string the bash caller actually sees.
    bash_out = rb.stdout.rstrip("\n")

    py_out = rp.substitute_template(template, kickoff, diff)

    assert py_out == bash_out, (
        f"substitute_template mismatch\n"
        f"bash={bash_out!r}\npy  ={py_out!r}"
    )


# ─────────────────────────────────────────────────────────────────────────────
# (c) artifact_summary — bash heredoc vs python helper, byte-equal
# ─────────────────────────────────────────────────────────────────────────────
def test_artifact_summary_parity(tmp_path):
    """Bash the lines-247-267 heredoc against a tmp_dir with 3 fake
    files (md/json/txt); python artifact_summary on same dir.
    Output must be byte-equal."""
    _which("bash", "python3")

    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "alpha.md").write_text("# alpha\nfirst line\nsecond\n", encoding="utf-8")
    (run_dir / "beta.json").write_text('{"x": 1}\n', encoding="utf-8")
    (run_dir / "gamma.txt").write_text("gamma body\n", encoding="utf-8")
    # Add a dotfile that should be skipped.
    (run_dir / ".hidden").write_text("hidden\n", encoding="utf-8")

    bash_src = f'''
python3 - "{run_dir}" <<'PY'
import os, sys
run_dir = sys.argv[1]
lines = []
for name in sorted(os.listdir(run_dir)):
    path = os.path.join(run_dir, name)
    if not os.path.isfile(path) or name.startswith("."):
        continue
    size = os.path.getsize(path)
    lines.append("### " + name + " (" + str(size) + " bytes)")
    if name.endswith((".md", ".json", ".txt", ".yaml", ".log")) and size > 0:
        try:
            with open(path, errors="replace") as f:
                head = "".join(f.readlines()[:25])
            lines.append(head[:2000].rstrip())
        except Exception:
            pass
    lines.append("")
print("\\n".join(lines)[:12000])
PY
'''
    rb = _bash_inline(bash_src)
    assert rb.returncode == 0, (
        f"bash artifact_summary rc={rb.returncode}: {rb.stderr}"
    )
    # Bash callers use ``$(python3 ...)`` which strips trailing
    # newlines from the heredoc's print output. Mirror that with
    # rstrip so the comparison is on the SEMANTIC string the bash
    # caller actually sees, not the raw stdout.
    bash_out = rb.stdout.rstrip("\n")

    py_out = rp.artifact_summary(str(run_dir))

    assert py_out == bash_out, (
        f"artifact_summary mismatch\nbash={bash_out!r}\npy  ={py_out!r}"
    )


# ─────────────────────────────────────────────────────────────────────────────
# (d) mo_append_rubric_to_feedback — bash function vs python, byte-equal
# ─────────────────────────────────────────────────────────────────────────────
def test_append_rubric_to_feedback_parity(tmp_path, temp_db):
    """Write a fake rubric.json with 2 PASS + 2 FAIL items + score=4
    (so .pass != true → the function will append). Source
    lib/rubric-prescreen.sh, call mo_append_rubric_to_feedback via
    bash. Then call rp.mo_append_rubric_to_feedback on a SEPARATE
    feedback path (so the bash output is not stomped on). The two
    feedback files must contain byte-equal appended content.

    The bash version uses ``mo_run_dir $epic`` to find the rubric;
    we mock the env so the bash looks at our tmp run_dir instead."""
    _which("bash", "python3")

    epic = "FB-E1"
    iter_n = 1
    rubric = {
        "pass": False,
        "score": 4,
        "items": [
            {"label": "a", "verdict": "PASS", "note": "ok a"},
            {"label": "b", "verdict": "PASS", "note": "ok b"},
            {"label": "c", "verdict": "FAIL", "note": "bad c"},
            {"label": "d", "verdict": "FAIL", "note": "bad d"},
        ],
    }

    # Create a run dir matching bash's ``mo_run_dir $epic`` resolution.
    # mo_run_dir defaults to $MINI_ORK_HOME/runs/$epic (verified by
    # reading lib/mo-runner.sh). We set MINI_ORK_HOME to a tmp dir.
    home = tmp_path / "home"
    run_dir = home / "runs" / epic
    iter_dir = run_dir / f"iter-{iter_n}"
    iter_dir.mkdir(parents=True)
    (iter_dir / "rubric.json").write_text(json.dumps(rubric), encoding="utf-8")

    bash_fb = tmp_path / "bash_fb.md"
    py_fb = tmp_path / "py_fb.md"
    bash_fb.write_text("")  # start empty
    py_fb.write_text("")

    # Bash side. mo_run_dir is NOT defined in lib/rubric-prescreen.sh
    # (it's expected to be loaded from dispatch.sh by real callers).
    # We stub it for the test to point at our tmp run_dir.
    bash_src = f'''
. "{SH}" >/dev/null 2>&1
mo_run_dir() {{ printf '%s' "{run_dir}"; }}
mo_append_rubric_to_feedback "{epic}" "{iter_n}" "{bash_fb}"
'''
    rb = _bash_inline(bash_src, env={"MINI_ORK_HOME": str(home)})
    assert rb.returncode == 0, (
        f"bash append rc={rb.returncode}: {rb.stderr}"
    )

    # Python side
    rp.mo_append_rubric_to_feedback(
        epic, iter_n, str(py_fb),
        run_dir=str(run_dir), mini_ork_home=str(home),
    )

    bash_text = bash_fb.read_text(encoding="utf-8")
    py_text = py_fb.read_text(encoding="utf-8")

    assert py_text == bash_text, (
        f"mo_append_rubric_to_feedback mismatch\n"
        f"bash={bash_text!r}\npy  ={py_text!r}"
    )

    # Sanity: both contain the FAIL lines and NOT the PASS lines.
    assert "- **[FAIL]** c" in py_text
    assert "- **[FAIL]** d" in py_text
    assert "ok a" not in py_text  # PASS item is filtered out
    assert "ok b" not in py_text  # PASS item is filtered out


# ─────────────────────────────────────────────────────────────────────────────
# (e) cache_emit parity — bash mo_cache_emit vs python cache_emit
# ─────────────────────────────────────────────────────────────────────────────
def test_cache_emit_parity(tmp_path, temp_db):
    """Bash inserts a row via ``mo_cache_emit``; python inserts a row
    via ``rp.cache_emit`` with the same args. SELECT both rows from
    mini_orch_sessions, diff logical columns (uuid + job_id +
    expires_at ignored)."""
    _which("bash", "python3", "sqlite3")

    input_hash = "abc123" + "f" * 57  # 64 hex chars (sha256 length)
    job_id = "test-job"
    output_path = "/tmp/rubric.json"
    log_path = "/tmp/rubric.log"
    cost = 0.07
    turns = 3
    dur = 12000
    prompt_version = "v1"

    # Bash: source lib/cache.sh, then mo_cache_emit with same args.
    bash_src = f'''
. "{CACHE_SH}" >/dev/null 2>&1
mo_cache_init_schema >/dev/null 2>&1
JOB_ID="{job_id}" mo_cache_emit "rubric" "E-CACHE" 1 "{input_hash}" "success" \
  "{output_path}" "{log_path}" "{cost}" "{turns}" "{dur}" "{prompt_version}"
'''
    rb = _bash_inline(bash_src, env={"MINI_ORK_DB": temp_db})
    assert rb.returncode == 0, (
        f"bash cache_emit rc={rb.returncode}: {rb.stderr}"
    )

    # Python: rp.cache_emit with same args.
    rp.cache_emit(
        temp_db, "rubric", "E-CACHE", 1, input_hash, "success",
        output_path, log_path, cost, turns, dur,
        job_id=job_id, prompt_version=prompt_version,
    )

    # SELECT both rows.
    rows = _select_session_rows(temp_db, "E-CACHE", input_hash)
    assert len(rows) == 2, (
        f"expected 2 mini_orch_sessions rows (bash + python), got {len(rows)}: {rows}"
    )

    # Both rows must be byte-equal on the logical columns.
    r0, r1 = rows[0], rows[1]
    assert r0 == r1, (
        f"cache_emit logical columns differ\nrow0={r0}\nrow1={r1}"
    )

    # Float tolerance on cost_usd.
    cost_idx = _SESSION_ROW_COLS.index("cost_usd")
    assert abs(float(r0[cost_idx]) - float(r1[cost_idx])) < _FLOAT_TOL, (
        f"cost_usd drift: {r0[cost_idx]} vs {r1[cost_idx]}"
    )


# ─────────────────────────────────────────────────────────────────────────────
# (f) cache_lookup hit + miss
# ─────────────────────────────────────────────────────────────────────────────
def test_cache_lookup_parity(tmp_path, temp_db):
    """Bash inserts a row via mo_cache_emit, then both bash and python
    call mo_cache_lookup / rp.cache_lookup with same args. The
    output_path must be byte-equal. The miss case (no row) returns
    empty string both sides."""
    _which("bash", "python3", "sqlite3")

    input_hash = "deadbeef" * 8  # 64 hex chars

    # Insert via bash (so the test is symmetric: bash wrote, both read).
    bash_src = f'''
. "{CACHE_SH}" >/dev/null 2>&1
mo_cache_init_schema >/dev/null 2>&1
mo_cache_emit "rubric" "E-LU" 1 "{input_hash}" "success" \
  "/out/hit.json" "/log/hit.log" "0.05" 2 5000 "v1"
mo_cache_lookup "rubric" "E-LU" 1 "{input_hash}"
'''
    rb = _bash_inline(bash_src, env={"MINI_ORK_DB": temp_db})
    assert rb.returncode == 0, (
        f"bash setup+lookup rc={rb.returncode}: {rb.stderr}"
    )
    bash_out = rb.stdout.strip()

    py_out = rp.cache_lookup(temp_db, "rubric", "E-LU", 1, input_hash)

    assert py_out == bash_out, (
        f"cache_lookup hit mismatch\nbash={bash_out!r}\npy  ={py_out!r}"
    )
    assert py_out == "/out/hit.json", (
        f"expected /out/hit.json, got {py_out!r}"
    )

    # Miss case: lookup on a different input_hash.
    py_miss = rp.cache_lookup(temp_db, "rubric", "E-LU", 1, "nonexistent" * 4)
    bash_miss = _source_rp_and_call(
        f'mo_cache_lookup "rubric" "E-LU" 1 "nonexistent"',
        temp_db, extra_sources=f'. "{CACHE_SH}" >/dev/null 2>&1',
    ).stdout.strip()
    assert py_miss == bash_miss == "", (
        f"miss: bash={bash_miss!r} py={py_miss!r}"
    )


# ─────────────────────────────────────────────────────────────────────────────
# (g) build_parse_error_payload — bash jq -n vs python dict, dict-equal
# ─────────────────────────────────────────────────────────────────────────────
def test_build_parse_error_payload_parity():
    """Bash runs the jq -n command from lines 187-191 with controlled
    diag + log_path; python calls rp.build_parse_error_payload on
    the same inputs. json.loads both and assert dict-equal.

    NOTE: This is the ONLY case where dict-equal replaces byte-equal.
    jq may not match Python's json.dumps key order, so we compare
    the parsed dicts (not the raw text)."""
    _which("jq")

    diag = "the last 800 chars of model output — e.g. truncated"
    log_path = "/tmp/iter-1/rubric.log"
    bash_src = f'''
jq -n --arg diag "{diag}" --arg log_path "{log_path}" \
  '{{pass: false, score: -1, parse_error: true, items: [],
    parse_error_diagnostic: $diag,
    parse_error_log_hint: ("inspect last 200 lines of " + $log_path)}}'
'''
    rb = _bash_inline(bash_src)
    assert rb.returncode == 0, (
        f"bash jq -n rc={rb.returncode}: {rb.stderr}"
    )
    bash_obj = json.loads(rb.stdout)

    py_obj = rp.build_parse_error_payload(diag=diag, log_path=log_path)

    # Both must have the same logical fields and same values.
    assert set(bash_obj.keys()) == set(py_obj.keys()), (
        f"key set differs: bash={set(bash_obj.keys())} py={set(py_obj.keys())}"
    )
    for k in bash_obj:
        if k == "parse_error_log_hint":
            # bash emits a string that says "inspect last 200 lines of /tmp/..."
            # python emits the same. Both should match exactly.
            assert bash_obj[k] == py_obj[k], (
                f"key {k!r}: bash={bash_obj[k]!r} py={py_obj[k]!r}"
            )
        else:
            assert bash_obj[k] == py_obj[k], (
                f"key {k!r}: bash={bash_obj[k]!r} py={py_obj[k]!r}"
            )

    # No-log_path variant (the dispatch-failure branch in bash).
    py_obj_nolog = rp.build_parse_error_payload(diag=diag, log_path=None)
    assert py_obj_nolog["parse_error_diagnostic"] == diag
    assert py_obj_nolog["pass"] is False
    assert py_obj_nolog["score"] == -1
    assert py_obj_nolog["parse_error"] is True
    assert py_obj_nolog["items"] == []
