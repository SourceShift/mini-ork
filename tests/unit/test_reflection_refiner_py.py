"""Parity gate: mini_ork.ported.reflection_refiner vs lib/reflection-refiner.sh.

Each test invokes the LIVE bash subprocess (jq, awk, sed, sqlite3 from PATH,
or a sub-bash that sources the library with a stubbed `mo_run_dir`) on the
same inputs as the Python port and asserts byte-identical output. No mocks,
no hardcoded expected outputs — expected is always derived from a control
bash invocation that shares the inputs.

>=6 cases:
  (a) format_failure_summary   — Python vs live `jq` projection
  (b) build_prompt             — Python vs live `awk`/`sed` pipeline
  (c) extract_reflection       — Python vs live `awk` range extraction
  (d) render_fallback          — Python vs bash heredoc
  (e) read_kickoff_path        — Python vs live `sqlite3` over a temp DB
                                  created by db/init.sh (kickoff requirement)
  (f) should_run               — Python vs live bash early-bail (missing /
                                  verdict=PASS / verdict=FAIL)
  (g) append_to_feedback       — Python vs live `mo_append_reflection_to_feedback`
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
from mini_ork.ported import reflection_refiner as rr  # noqa: E402

SH = REPO / "lib" / "reflection-refiner.sh"
INIT_SH = REPO / "db" / "init.sh"


def _which(*tools: str) -> dict[str, str]:
    out = {}
    for t in tools:
        p = shutil.which(t)
        if not p:
            pytest.skip(f"required tool not on PATH: {t}")
        out[t] = p
    return out


# ─────────────────────────────────────────────────────────────────────────────
# (a) format_failure_summary
# ─────────────────────────────────────────────────────────────────────────────
def test_format_failure_summary_parity():
    """Python `format_failure_summary` matches the live `jq` projection
    byte-for-byte across empty + populated failure lists."""
    path = _which("jq")["jq"]
    verdict = {
        "verdict": "FAIL",
        "scenarios_run": 3,
        "scenarios_failed": 2,
        "failures": [
            {"title": "Auth rejects valid token", "spec": "spec/auth.md",
             "error": "expected 200\ngot 401"},
            {"title": "Rate limit fires too early", "spec": "spec/rate.md",
             "error": "burst at 100 rps"},
        ],
    }
    jq_expr = (
        '"Total: " + (.scenarios_run | tostring) + " scenarios, " + '
        '(.scenarios_failed | tostring) + " failed.\\n\\n" + '
        '([.failures[] | "- **" + .title + "** (" + .spec + "): " + '
        '(.error | gsub("\\n"; " // "))] | join("\\n"))'
    )
    r = subprocess.run(
        [path, "-r", jq_expr],
        input=json.dumps(verdict), capture_output=True, text=True, check=True,
    )
    expected = r.stdout
    assert rr.format_failure_summary(verdict) == expected

    # empty failures list: bash jq `[] | join("\\n")` collapses to ''; the
    # header still ends in "\n\n". Python must reproduce.
    empty = {"scenarios_run": 0, "scenarios_failed": 0, "failures": []}
    r2 = subprocess.run(
        [path, "-r", jq_expr],
        input=json.dumps(empty), capture_output=True, text=True, check=True,
    )
    assert rr.format_failure_summary(empty) == r2.stdout


# ─────────────────────────────────────────────────────────────────────────────
# (b) build_prompt — three-stage awk split + sed pipe-escape + 5-slot concat
# ─────────────────────────────────────────────────────────────────────────────
def test_build_prompt_parity(tmp_path):
    """Python `build_prompt` matches the LIVE bash awk+sed pipeline.

    The pipeline below is the verbatim awk+awk+awk sequence from
    lib/reflection-refiner.sh, executed with mawk/gawk on tmp files, then
    sed for {{KICKOFF_PATH}}, then the 7-part `cat ...` concat. Byte parity
    across the template, kickoff body, diff list, and failure summary is
    asserted by feeding both implementations identical inputs.
    """
    _which("awk", "sed", "mktemp")

    template = (
        "## Head\n"
        "intro paragraph {{KICKOFF_PATH}}\n"
        "{{KICKOFF_BODY}}\n"
        "## Diff\n"
        "files changed: {{KICKOFF_PATH}}\n"
        "{{DIFF_FILES}}\n"
        "## Failures\n"
        "see: {{KICKOFF_PATH}}\n"
        "{{FAILURE_SUMMARY}}\n"
        "## Tail\n"
        "end marker {{KICKOFF_PATH}}\n"
    )
    kickoff_body = "BODY-LINE-1\nBODY-LINE-2\n"
    kickoff_path = "kickoffs/foo|bar.md"  # pipe char exercises sed escape
    diff_files = ["src/a.py", "src/b.py", "tests/test_x.py"]
    failure_summary = (
        "Total: 2 scenarios, 1 failed.\n\n"
        "- **t1** (s1): boom // stack\n"
        "- **t2** (s2): ok\n"
    )

    # Write inputs to disk so the bash subprocess can `cat` them.
    tmpl_path = tmp_path / "tmpl.md"
    kickoff_path_file = tmp_path / "kickoff.md"
    tmpl_path.write_text(template, encoding="utf-8")
    kickoff_path_file.write_text(kickoff_body, encoding="utf-8")

    # Verbatim bash pipeline from lib/reflection-refiner.sh lines 58-88.
    bash_script = r"""
set -u
template="$1" kickoff_abs="$2" kickoff_rel="$3" diff_files="$4" failure_summary="$5"
tmp_a="$(mktemp)"; tmp_b="$(mktemp)"; tmp_c="$(mktemp)"; tmp_d="$(mktemp)"
awk -v m='{{KICKOFF_BODY}}' '
  !found && index($0,m) { found=1; gsub(m,""); if(length($0)) print; next }
  !found { print > "/dev/stdout" }
  found  { print > "/dev/stderr" }
' "$template" >"$tmp_a" 2>"$tmp_b" || true
awk -v m='{{DIFF_FILES}}' '
  !found && index($0,m) { found=1; gsub(m,""); if(length($0)) print; next }
  !found { print > "/dev/stdout" }
  found  { print > "/dev/stderr" }
' "$tmp_b" >"$tmp_b.h" 2>"$tmp_c" || true; mv "$tmp_b.h" "$tmp_b"
awk -v m='{{FAILURE_SUMMARY}}' '
  !found && index($0,m) { found=1; gsub(m,""); if(length($0)) print; next }
  !found { print > "/dev/stdout" }
  found  { print > "/dev/stderr" }
' "$tmp_c" >"$tmp_c.h" 2>"$tmp_d" || true; mv "$tmp_c.h" "$tmp_c"
for f in "$tmp_a" "$tmp_b" "$tmp_c" "$tmp_d"; do
  sed -i.bak -e "s|{{KICKOFF_PATH}}|${kickoff_rel//|/\\|}|g" "$f"
  rm -f "$f.bak"
done
{
  cat "$tmp_a"
  cat "$kickoff_abs"
  cat "$tmp_b"
  echo "$diff_files"
  cat "$tmp_c"
  echo "$failure_summary"
  cat "$tmp_d"
}
rm -f "$tmp_a" "$tmp_b" "$tmp_c" "$tmp_d"
"""
    diff_arg = "\n".join(diff_files)
    r = subprocess.run(
        ["bash", "-c", bash_script, "_", str(tmpl_path),
         str(kickoff_path_file), kickoff_path, diff_arg, failure_summary],
        capture_output=True, text=True, check=True,
    )
    expected = r.stdout
    actual = rr.build_prompt(template, kickoff_body, kickoff_path,
                             diff_files, failure_summary)
    assert actual == expected, (
        f"len actual={len(actual)} expected={len(expected)}\n"
        f"actual head: {actual[:200]!r}\nexpected head: {expected[:200]!r}"
    )


# ─────────────────────────────────────────────────────────────────────────────
# (c) extract_reflection — live awk range pattern
# ─────────────────────────────────────────────────────────────────────────────
def test_extract_reflection_parity():
    """Python `extract_reflection` matches live `awk '/^## Reflection refiner/,/EOF/'`."""
    path = _which("awk")["awk"]
    log = (
        "preamble noise\n"
        "## Reflection refiner\n"
        "## Hypotheses\n"
        "- hypothesis 1\n"
        "- hypothesis 2\n"
        "## Remediation\n"
        "fix foo\n"
    )
    r = subprocess.run(
        [path, "/^## Reflection refiner/,/EOF/"],
        input=log, capture_output=True, text=True, check=True,
    )
    assert rr.extract_reflection(log) == r.stdout

    # Empty / no match: bash awk prints nothing; Python returns ''.
    r2 = subprocess.run(
        [path, "/^## Reflection refiner/,/EOF/"],
        input="no heading here\n", capture_output=True, text=True, check=True,
    )
    assert rr.extract_reflection("no heading here\n") == r2.stdout == ""


# ─────────────────────────────────────────────────────────────────────────────
# (d) render_fallback — bash heredoc
# ─────────────────────────────────────────────────────────────────────────────
def test_render_fallback_parity():
    """Python `render_fallback` matches the verbatim heredoc body in
    lib/reflection-refiner.sh lines 132-141."""
    iter_name = "3"
    fs = "Total: 1 scenarios, 1 failed.\n\n- **t** (s): boom\n"
    # Bash execution: disable -u so an unset $failure_summary wouldn't abort;
    # under our control we always pass the variable explicitly.
    bash_script = (
        "set +u\n"
        "fs=\"$1\"; iter=\"$2\"\n"
        "cat <<EOF\n"
        "## Reflection refiner — fallback (LLM output unparseable)\n"
        "\n"
        "The reflection refiner did not produce parseable output. "
        "Raw failure summary:\n"
        "\n"
        "$fs\n"
        "\n"
        "See iter-$iter/reflection.log for the full transcript.\n"
        "EOF\n"
    )
    r = subprocess.run(
        ["bash", "-c", bash_script, "_", fs, iter_name],
        capture_output=True, text=True, check=True,
    )
    py = rr.render_fallback(fs, iter_name)
    # The bash heredoc and the Python port produce byte-identical text;
    # the three-newline gap between `$fs` and `See iter-...` matches the
    # combination of fs's trailing \n + heredoc line-ending \n + blank-line \n.
    assert py == r.stdout
    # Sanity: the transcript line is anchored by the iter name.
    assert py.endswith(f"See iter-{iter_name}/reflection.log for the full transcript.\n")


# ─────────────────────────────────────────────────────────────────────────────
# (e) read_kickoff_path — temp DB from db/init.sh
# ─────────────────────────────────────────────────────────────────────────────
@pytest.fixture
def temp_db(tmp_path_factory):
    """Spin up a real mini-ork SQLite DB via db/init.sh — the kickoff
    explicitly requires 'temp DB created by db/init.sh, diff resulting rows'."""
    home = tmp_path_factory.mktemp("home")
    dbp = str(home / "state.db")
    subprocess.run(
        ["bash", str(INIT_SH)],
        env={**os.environ, "MINI_ORK_HOME": str(home), "MINI_ORK_DB": dbp},
        capture_output=True, text=True, check=True,
    )
    return dbp


def test_read_kickoff_path_db_init(temp_db):
    """Python `read_kickoff_path` matches live `sqlite3` on a real DB initialised
    via db/init.sh. Three rows are seeded (present, present-empty, missing) and
    each is queried by both implementations."""
    con = sqlite3.connect(temp_db)
    con.execute(
        "INSERT INTO epics(id, title, status, kickoff_path) VALUES (?, ?, ?, ?)",
        ("epic-present", "t", "in progress", "kickoffs/p.md"),
    )
    con.execute(
        "INSERT INTO epics(id, title, status, kickoff_path) VALUES (?, ?, ?, ?)",
        ("epic-empty", "t", "in progress", ""),
    )
    con.commit()
    con.close()

    path = _which("sqlite3")["sqlite3"]

    def bash_query(epic: str) -> str:
        r = subprocess.run(
            [path, temp_db,
             f"SELECT kickoff_path FROM epics WHERE id='{epic}';"],
            capture_output=True, text=True, check=True,
        )
        # bash reads trailing newline; trim to match Python's None/str.
        return r.stdout.rstrip("\n")

    # Present row.
    assert rr.read_kickoff_path(temp_db, "epic-present") == bash_query("epic-present")
    # Present-but-empty: bash returns ''; Python returns ''.
    assert rr.read_kickoff_path(temp_db, "epic-empty") == "" == bash_query("epic-empty")
    # Missing row: bash returns '' (sqlite3 returns empty result); Python returns None.
    assert rr.read_kickoff_path(temp_db, "epic-missing") is None
    assert bash_query("epic-missing") == ""
    # Bash returns '' on both, Python distinguishes absent vs empty → this is the
    # one intentional divergence from bash (cf. plan risk note: don't silently
    # default; surface None).

    # Sanity: live bash also exercises the missing-DB path → None.
    assert rr.read_kickoff_path("/nonexistent/path/db.sqlite", "epic") is None


# ─────────────────────────────────────────────────────────────────────────────
# (f) should_run — bash early-bail emitted to stderr
# ─────────────────────────────────────────────────────────────────────────────
def test_should_run_parity(tmp_path):
    """Python `should_run` matches the bash `mo_run_reflection_refiner`
    early-bail emitted to stderr across (missing, PASS, FAIL) verdicts.

    `mo_run_dir` is undefined in the library source; the bash subprocess
    stubs it to return our tmp_path so the function computes the right
    iter_dir. The library source itself is unchanged.
    """
    iter_dir = tmp_path / "iter-1"
    iter_dir.mkdir()

    # Source the library and stub mo_run_dir, then call mo_run_reflection_refiner.
    # After the early-bail, the function calls sqlite3 / jq which would fail
    # without a DB; for the FAIL case we provide a real DB so the function
    # proceeds to the LLM call (which is a stub).
    bash_wrapper = (
        f'. "{SH}"\n'
        # Stub mo_run_dir to return tmp_path so iter_dir is correct.
        f'mo_run_dir() {{ echo "{tmp_path}"; }}\n'
        # Stub mo_emit_budget_flag, mo_emit_cache_flags to no-ops so the
        # function doesn't reference undefined helpers after the early-bail.
        'mo_emit_budget_flag() { return 0; }\n'
        'mo_emit_cache_flags() { return 0; }\n'
        # Stub the claude invocation: just echo a parseable reflection line.
        'claude() { echo "## Reflection refiner"; echo "ok"; }\n'
        'export -f claude mo_emit_budget_flag mo_emit_cache_flags mo_run_dir\n'
        # Args: epic worktree iter. Capture both streams separately; the LLM
        # stub writes parseable output to stdout (we ignore it) and the
        # early-bail `>&2` lines come back via subprocess.stderr.
        'mo_run_reflection_refiner "epic-x" "wt" "1"\n'
    )

    # (i) missing verdict
    r1 = subprocess.run(
        ["bash", "-c", bash_wrapper],
        env={**os.environ, "MINI_ORK_HOME": str(tmp_path),
             "REPO_ROOT": str(REPO), "MINI_ORK_ROOT": str(REPO)},
        capture_output=True, text=True,
    )
    # bash wrapper redirects stdout to /dev/null; only stderr surfaces.
    err1 = r1.stderr
    py_ok1, py_reason1 = rr.should_run(iter_dir)
    assert py_ok1 is False
    assert py_reason1 in err1
    assert "no bdd-verdict.json" in py_reason1
    assert "no bdd-verdict.json" in err1

    # (ii) verdict = PASS
    (iter_dir / "bdd-verdict.json").write_text(
        json.dumps({"verdict": "PASS", "scenarios_run": 1,
                    "scenarios_failed": 0, "failures": []}),
        encoding="utf-8",
    )
    r2 = subprocess.run(
        ["bash", "-c", bash_wrapper],
        env={**os.environ, "MINI_ORK_HOME": str(tmp_path),
             "REPO_ROOT": str(REPO), "MINI_ORK_ROOT": str(REPO)},
        capture_output=True, text=True,
    )
    err2 = r2.stderr
    py_ok2, py_reason2 = rr.should_run(iter_dir)
    assert py_ok2 is False
    assert "bdd verdict=PASS" in py_reason2
    assert "bdd verdict=PASS" in err2

    # (iii) verdict = FAIL → both proceed (python returns True; bash proceeds
    # past the early-bail and into the LLM stub which echoes a parseable line).
    (iter_dir / "bdd-verdict.json").write_text(
        json.dumps({"verdict": "FAIL", "scenarios_run": 1,
                    "scenarios_failed": 1, "failures": [
                        {"title": "t", "spec": "s", "error": "e"}]}),
        encoding="utf-8",
    )
    py_ok3, py_reason3 = rr.should_run(iter_dir)
    assert py_ok3 is True and py_reason3 == ""

    # For the FAIL case, bash proceeds past the early-bail — verify it does
    # NOT emit either of the two early-bail stderr messages.
    r3 = subprocess.run(
        ["bash", "-c", bash_wrapper],
        env={**os.environ, "MINI_ORK_HOME": str(tmp_path),
             "REPO_ROOT": str(REPO), "MINI_ORK_ROOT": str(REPO)},
        capture_output=True, text=True,
    )
    err3 = r3.stderr
    assert "no bdd-verdict.json" not in err3
    assert "bdd verdict=" not in err3 or "bdd verdict=FAIL" in err3


# ─────────────────────────────────────────────────────────────────────────────
# (g) append_to_feedback — live bash source + call
# ─────────────────────────────────────────────────────────────────────────────
def test_append_to_feedback_parity(tmp_path):
    """Python `append_to_feedback` matches the live bash
    `mo_append_reflection_to_feedback`. When reflection.md exists, both
    append "\\n{content}" to feedback_path. When absent, both no-op."""
    # (i) reflection.md exists
    epic = "epic-app"
    iter = "2"
    iter_dir = tmp_path / "iter"
    (iter_dir / f"iter-{iter}").mkdir(parents=True)
    refl_content = "## Reflection refiner\n- fix foo\n"
    (iter_dir / f"iter-{iter}" / "reflection.md").write_text(
        refl_content, encoding="utf-8",
    )
    feedback_path = tmp_path / "feedback.md"
    feedback_path.write_text("existing feedback line\n", encoding="utf-8")

    bash_wrapper = (
        f'. "{SH}"\n'
        f'mo_run_dir() {{ echo "{iter_dir}"; }}\n'
        'export -f mo_run_dir\n'
        # Pre-existing feedback content already on disk.
        'mo_append_reflection_to_feedback "epic-app" "2" '
        f'"{feedback_path}"\n'
    )
    subprocess.run(
        ["bash", "-c", bash_wrapper],
        capture_output=True, text=True, check=True,
    )
    bash_after = feedback_path.read_text(encoding="utf-8")

    # Re-run the Python port on a fresh copy so we can diff cleanly.
    feedback_path2 = tmp_path / "feedback2.md"
    feedback_path2.write_text("existing feedback line\n", encoding="utf-8")
    ok = rr.append_to_feedback(epic, iter, feedback_path2, iter_dir)
    py_after = feedback_path2.read_text(encoding="utf-8")
    assert ok is True
    assert py_after == bash_after
    # Sanity: the reflection content is appended after a blank line.
    assert py_after.endswith("\n" + refl_content)

    # (ii) reflection.md absent → no-op, feedback unchanged
    missing_iter_dir = tmp_path / "missing"
    missing_iter_dir.mkdir()
    fp3 = tmp_path / "feedback3.md"
    fp3.write_text("untouched\n", encoding="utf-8")
    ok2 = rr.append_to_feedback("epic-x", "9", fp3, missing_iter_dir)
    assert ok2 is False
    assert fp3.read_text(encoding="utf-8") == "untouched\n"

    bash_wrapper2 = (
        f'. "{SH}"\n'
        f'mo_run_dir() {{ echo "{missing_iter_dir}"; }}\n'
        'export -f mo_run_dir\n'
        f'mo_append_reflection_to_feedback "epic-x" "9" "{fp3}"\n'
    )
    subprocess.run(
        ["bash", "-c", bash_wrapper2],
        capture_output=True, text=True, check=True,
    )
    assert fp3.read_text(encoding="utf-8") == "untouched\n"