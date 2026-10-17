"""Unit tests for mini_ork.gates.rubric_prescreen (+ rubric_scoring helpers).

Seven cases:

  (a) extract_rubric_json      — brace-balanced scanner finds the LAST
                                 ``{"pass":`` marker and returns the
                                 matching json.loads-able substring.
  (b) substitute_template      — first-occurrence replacement of
                                 {{KICKOFF_BODY}} / {{DIFF_SUMMARY}};
                                 diff_summary and the result are
                                 rstripped of trailing newlines.
  (c) artifact_summary         — per-file ``### name (size bytes)`` headers,
                                 dotfiles skipped, sorted order; the text
                                 budget is split across files in proportion
                                 to size, and every cut is labelled with
                                 exact line/byte counts.
  (d) mo_append_rubric_to_feedback — appends the advisory section listing
                                     only non-PASS items when .pass != true;
                                     pass-through on missing / passing rubric.
  (e) cache_emit               — inserts a mini_orch_sessions row whose
                                 logical columns match the call args.
  (f) cache_lookup             — hit returns output_path; miss returns "".
  (g) build_parse_error_payload — exact payload dict, with and without
                                  log_path.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
from mini_ork.gates import rubric_prescreen as rp  # noqa: E402
from mini_ork.stores.migrate import init_db  # noqa: E402


# ─────────────────────────────────────────────────────────────────────────────
# DB scaffold fixture (native init_db against tmp_path — no bash twin)
# ─────────────────────────────────────────────────────────────────────────────
@pytest.fixture
def temp_db(tmp_path):
    """Spin up a real mini-ork SQLite DB via the native init_db port with a
    unique path per test. Migration 0001_core.sql + 0002_mini_orch_sessions.sql
    both apply, so the epics + mini_orch_sessions tables exist for the test."""
    home = tmp_path / "home"
    home.mkdir()
    dbp = str(home / "state.db")
    rc, out, err = init_db(db=dbp, root=str(REPO))
    assert rc == 0, f"init_db failed rc={rc}\nstdout={out}\nstderr={err}"
    return dbp


# ─────────────────────────────────────────────────────────────────────────────
# (a) extract_rubric_json — last-valid-marker extraction
# ─────────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("text,expected", [
    # simple {"pass": true, "score": 7}
    (
        'Some preamble text\n{"pass": true, "score": 7}\n',
        '{"pass": true, "score": 7}',
    ),
    # multiple {"pass" markers; the LAST valid one wins
    (
        'first attempt {"pass": false, "score": 0}\nfinal: {"pass": true, "score": 8, "items": []}\n',
        '{"pass": true, "score": 8, "items": []}',
    ),
    # nested braces in items
    (
        'wrapped {"pass": true, "score": 6, "items": [{"label": "x", "verdict": "PASS", "note": "ok"}]}\n',
        '{"pass": true, "score": 6, "items": [{"label": "x", "verdict": "PASS", "note": "ok"}]}',
    ),
])
def test_extract_rubric_json(text, expected):
    out = rp.extract_rubric_json(text)
    assert out == expected
    # and the extracted substring json.loads-roundtrips to the same object
    assert json.loads(out) == json.loads(expected)


def test_extract_rubric_json_no_marker_returns_none():
    assert rp.extract_rubric_json("no rubric here at all") is None


# ─────────────────────────────────────────────────────────────────────────────
# (b) substitute_template — first-occurrence replacement + rstrip semantics
# ─────────────────────────────────────────────────────────────────────────────
def test_substitute_template():
    template = (
        "# header\n\n"
        "## Kickoff\n{{KICKOFF_BODY}}\n\n"
        "## Diff\n{{DIFF_SUMMARY}}\n\n"
        "# footer\n"
    )
    kickoff = "the kickoff body line 1\nline 2\n"
    diff = "file.py | 2 +-\n1 file changed\n"

    out = rp.substitute_template(template, kickoff, diff)

    # kickoff body is inserted verbatim (keeping ITS trailing newline);
    # the diff is rstripped; the whole result is rstripped.
    expected = (
        "# header\n\n"
        "## Kickoff\nthe kickoff body line 1\nline 2\n\n\n"
        "## Diff\nfile.py | 2 +-\n1 file changed\n\n"
        "# footer"
    )
    assert out == expected


def test_substitute_template_first_occurrence_only():
    out = rp.substitute_template(
        "{{KICKOFF_BODY}} / {{KICKOFF_BODY}}", "BODY", "DIFF",
    )
    assert out == "BODY / {{KICKOFF_BODY}}"


# ─────────────────────────────────────────────────────────────────────────────
# (c) artifact_summary — headers + heads, dotfiles skipped, sorted order
# ─────────────────────────────────────────────────────────────────────────────
def test_artifact_summary(tmp_path):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "alpha.md").write_text("# alpha\nfirst line\nsecond\n", encoding="utf-8")
    (run_dir / "beta.json").write_text('{"x": 1}\n', encoding="utf-8")
    (run_dir / "gamma.txt").write_text("gamma body\n", encoding="utf-8")
    # Add a dotfile that should be skipped.
    (run_dir / ".hidden").write_text("hidden\n", encoding="utf-8")

    out = rp.artifact_summary(str(run_dir))

    alpha_size = len("# alpha\nfirst line\nsecond\n")
    beta_size = len('{"x": 1}\n')
    gamma_size = len("gamma body\n")
    expected = (
        f"### alpha.md ({alpha_size} bytes)\n"
        "# alpha\nfirst line\nsecond\n"
        "\n"
        f"### beta.json ({beta_size} bytes)\n"
        '{"x": 1}\n'
        "\n"
        f"### gamma.txt ({gamma_size} bytes)\n"
        "gamma body"
    )
    assert out == expected
    assert ".hidden" not in out


def test_artifact_summary_missing_dir_returns_empty(tmp_path):
    assert rp.artifact_summary(str(tmp_path / "nope")) == ""


def test_artifact_summary_does_not_cut_a_file_that_fits(tmp_path):
    """Regression: a 43-line artifact used to be clipped to 25 lines with no
    label, so a complete document appeared to end mid-section and the grader
    failed it on the "no truncation" checklist item."""
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    body = ["# Synthesis", ""]
    for i in range(1, 7):
        body += [f"## D-{i}", "", "rationale", ""]
    body += ["## Verdict", "", "THE CONCLUSION"]
    (run_dir / "synthesis.md").write_text("\n".join(body) + "\n", encoding="utf-8")

    out = rp.artifact_summary(str(run_dir))

    assert "THE CONCLUSION" in out
    assert "lines," not in out  # nothing was cut, so no label


def test_artifact_summary_budget_goes_to_the_biggest_artifact(tmp_path):
    """The headline artifact is normally the largest, and must not be starved
    by a crowd of small companions."""
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    synthesis = "\n".join(f"section {i}\n" + "filler line\n" * 30 for i in range(40))
    (run_dir / "synthesis.md").write_text(synthesis + "\nVERDICT\n", encoding="utf-8")
    for name in ("rubric.json", "panel-verdict.json", "run.log", "notes.txt"):
        (run_dir / name).write_text('{"small": true}\n', encoding="utf-8")

    out = rp.artifact_summary(str(run_dir))

    assert len(out) <= 12000
    # Every small file is shown whole; the big one takes the rest and is
    # labelled rather than silently clipped.
    assert out.count('{"small": true}') == 4
    assert "bytes shown]" in out
    body = out.split("### synthesis.md", 1)[1]
    assert len(body) > 9000


def test_artifact_summary_truncation_label_is_exact(tmp_path):
    """The label is the grader's only signal that it is looking at a sample,
    so its counts must match the file — not approximate it."""
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    path = run_dir / "synthesis.md"
    text = "\n".join(f"line {i}" for i in range(4000)) + "\n"
    path.write_text(text, encoding="utf-8")

    out = rp.artifact_summary(str(run_dir))
    assert len(out) <= 12000

    shown_lines, total_lines, shown_bytes, total_bytes = map(
        int,
        re.search(
            r"… \[(\d+) of (\d+) lines, (\d+) of (\d+) bytes shown\]", out
        ).groups(),
    )
    after_header = out.split("### synthesis.md", 1)[1].split("\n", 1)[1]
    content = after_header.rsplit("\n… [", 1)[0]

    assert total_lines == len(text.splitlines())
    assert total_bytes == path.stat().st_size
    assert shown_lines == len(content.splitlines())
    assert shown_bytes == len(content.rstrip())
    assert out.rstrip().endswith("bytes shown]")


def _marker_run(tmp_path, *, verdict: str, marker_after_verdict: bool = False) -> Path:
    """A run dir shaped like a revived one: escalation markers plus a verdict."""
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    marker = run_dir / "NEEDS-CHANGE.md"
    marker.write_text("the test check could not reach something it needs\n", encoding="utf-8")
    hint = run_dir / "retry-hint.json"
    hint.write_text('{"failed_node": "test"}\n', encoding="utf-8")
    (run_dir / "run-verdict.json").write_text(
        json.dumps({"verdict": verdict}) + "\n", encoding="utf-8"
    )
    verdict_at = (run_dir / "run-verdict.json").stat().st_mtime
    offset = 10 if marker_after_verdict else -10
    os.utime(marker, (verdict_at + offset, verdict_at + offset))
    os.utime(hint, (verdict_at + offset, verdict_at + offset))
    return run_dir


def test_artifact_summary_drops_markers_the_green_verdict_superseded(tmp_path):
    """Regression, measured on rp-e3-header-20261015-222115: the revive left the
    previous attempt's NEEDS-CHANGE.md and retry-hint.json in the run dir, the
    grader sampled them, and rubric.json scored a run that had published green
    as pass=false."""
    run_dir = _marker_run(tmp_path, verdict="pass")

    out = rp.artifact_summary(str(run_dir))

    assert "could not reach something it needs" not in out
    assert "failed_node" not in out
    assert "### NEEDS-CHANGE.md" not in out
    # The omission is named, so the grader knows the artifacts exist.
    assert "superseded, not shown" in out
    assert "NEEDS-CHANGE.md" in out
    assert "retry-hint.json" in out


def test_artifact_summary_keeps_markers_a_failed_run_stands_on(tmp_path):
    run_dir = _marker_run(tmp_path, verdict="fail")

    out = rp.artifact_summary(str(run_dir))

    assert "could not reach something it needs" in out
    assert "superseded, not shown" not in out


def test_artifact_summary_keeps_a_marker_written_after_the_green_verdict(tmp_path):
    """repair.json is rewritten by the revive itself, and a marker that a *later*
    event produced is this attempt's, not the superseded one's."""
    run_dir = _marker_run(tmp_path, verdict="pass", marker_after_verdict=True)

    out = rp.artifact_summary(str(run_dir))

    assert "could not reach something it needs" in out


def test_artifact_summary_without_a_verdict_is_unchanged(tmp_path):
    """No verdict file at all (epic iter dirs, pre-verdict runs): nothing is
    dropped, so the previous behaviour is preserved."""
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "NEEDS-CHANGE.md").write_text("still standing\n", encoding="utf-8")

    out = rp.artifact_summary(str(run_dir))

    assert "still standing" in out


# ─────────────────────────────────────────────────────────────────────────────
# (d) mo_append_rubric_to_feedback — FAIL items appended, PASS filtered out
# ─────────────────────────────────────────────────────────────────────────────
def test_append_rubric_to_feedback(tmp_path):
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

    home = tmp_path / "home"
    run_dir = home / "runs" / epic
    iter_dir = run_dir / f"iter-{iter_n}"
    iter_dir.mkdir(parents=True)
    (iter_dir / "rubric.json").write_text(json.dumps(rubric), encoding="utf-8")

    fb = tmp_path / "fb.md"
    fb.write_text("")

    rp.mo_append_rubric_to_feedback(
        epic, iter_n, str(fb),
        run_dir=str(run_dir), mini_ork_home=str(home),
    )

    text = fb.read_text(encoding="utf-8")
    expected = (
        "\n## Rubric pre-screen (advisory — Phase A.5)\n"
        "\nScore: 4/8 (need ≥6 to PASS)\n"
        "\n- **[FAIL]** c — bad c\n"
        "- **[FAIL]** d — bad d\n"
    )
    assert text == expected


def test_append_rubric_to_feedback_pass_through(tmp_path):
    epic = "FB-E2"
    run_dir = tmp_path / "runs" / epic
    (run_dir / "iter-1").mkdir(parents=True)
    fb = tmp_path / "fb.md"
    fb.write_text("original")

    # passing rubric → no append
    (run_dir / "iter-1" / "rubric.json").write_text(
        json.dumps({"pass": True, "score": 8, "items": []}), encoding="utf-8")
    rp.mo_append_rubric_to_feedback(epic, 1, str(fb), run_dir=str(run_dir))
    assert fb.read_text() == "original"

    # missing rubric → no append
    rp.mo_append_rubric_to_feedback(epic, 2, str(fb), run_dir=str(run_dir))
    assert fb.read_text() == "original"


# ─────────────────────────────────────────────────────────────────────────────
# (e) cache_emit — row lands with the logical columns matching the args
# ─────────────────────────────────────────────────────────────────────────────
_SESSION_ROW_COLS = (
    "epic_id", "iter", "stage", "input_hash", "status",
    "output_path", "log_path", "cost_usd", "turns", "duration_ms",
    "prompt_version",
)


def test_cache_emit_row_columns(temp_db):
    input_hash = "abc123" + "f" * 57  # 64 hex chars (sha256 length)
    rp.cache_emit(
        temp_db, "rubric", "E-CACHE", 1, input_hash, "success",
        "/tmp/rubric.json", "/tmp/rubric.log", 0.07, 3, 12000,
        job_id="test-job", prompt_version="v1",
    )

    con = sqlite3.connect(temp_db)
    try:
        rows = con.execute(
            "SELECT " + ",".join(_SESSION_ROW_COLS)
            + " FROM mini_orch_sessions WHERE epic_id=? AND input_hash=?",
            ("E-CACHE", input_hash),
        ).fetchall()
    finally:
        con.close()

    assert len(rows) == 1
    (epic_id, it, stage, ih, status, output_path, log_path,
     cost_usd, turns, duration_ms, prompt_version) = rows[0]
    assert (epic_id, it, stage, ih, status) == (
        "E-CACHE", 1, "rubric", input_hash, "success")
    assert (output_path, log_path) == ("/tmp/rubric.json", "/tmp/rubric.log")
    assert abs(float(cost_usd) - 0.07) < 1e-6
    assert (turns, duration_ms, prompt_version) == (3, 12000, "v1")


# ─────────────────────────────────────────────────────────────────────────────
# (f) cache_lookup — hit returns output_path, miss returns ""
# ─────────────────────────────────────────────────────────────────────────────
def test_cache_lookup_hit_and_miss(temp_db):
    input_hash = "deadbeef" * 8  # 64 hex chars

    rp.cache_emit(
        temp_db, "rubric", "E-LU", 1, input_hash, "success",
        "/out/hit.json", "/log/hit.log", 0.05, 2, 5000,
    )

    assert rp.cache_lookup(temp_db, "rubric", "E-LU", 1, input_hash) == "/out/hit.json"
    assert rp.cache_lookup(temp_db, "rubric", "E-LU", 1, "nonexistent" * 4) == ""


# ─────────────────────────────────────────────────────────────────────────────
# (g) build_parse_error_payload — exact payload shape
# ─────────────────────────────────────────────────────────────────────────────
def test_build_parse_error_payload():
    diag = "the last 800 chars of model output — e.g. truncated"
    log_path = "/tmp/iter-1/rubric.log"

    assert rp.build_parse_error_payload(diag=diag, log_path=log_path) == {
        "pass": False,
        "score": -1,
        "parse_error": True,
        "items": [],
        "parse_error_diagnostic": diag,
        "parse_error_log_hint": "inspect last 200 lines of /tmp/iter-1/rubric.log",
    }

    # No-log_path variant (the dispatch-failure branch): hint omitted.
    assert rp.build_parse_error_payload(diag=diag, log_path=None) == {
        "pass": False,
        "score": -1,
        "parse_error": True,
        "items": [],
        "parse_error_diagnostic": diag,
    }


# ─────────────────────────────────────────────────────────────────────────────
# panel-verdict.json merge (run-1791760519-72158 D1): the prescreen write used
# to clobber a panel-gate agent's verdict+reasons with its scoring shape; it
# must merge with existing keys authoritative + a namespaced "prescreen" key.
# ─────────────────────────────────────────────────────────────────────────────

def _fake_dispatch_emitting(payload):
    def fake(argv, *, root):
        print(payload)
        return 0
    return fake


def test_run_score_preserves_agent_panel_verdict(tmp_path, monkeypatch):
    kickoff = tmp_path / "kickoff.md"
    kickoff.write_text("# Kickoff\nbody\n")
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "panel-verdict.json").write_text(json.dumps({
        "verdict": "ESCALATE",
        "reasons": ["cross-lens contradiction on probe P2"],
        "dod_probe_results": [{"id": "P1", "panel_consensus": "pass"}],
    }))
    monkeypatch.setattr(
        "mini_ork.dispatch.llm_dispatch.llm_dispatch",
        _fake_dispatch_emitting(json.dumps({"pass": True, "score": 6, "items": []})))
    rp.mo_rubric_run_score(
        kickoff_path=str(kickoff), run_dir=str(run_dir),
        task_class="recursive_validate_impl",
        mini_ork_root=str(REPO), mini_ork_home=str(tmp_path))
    merged = json.loads((run_dir / "panel-verdict.json").read_text())
    assert merged["verdict"] == "ESCALATE"                    # agent keys authoritative
    assert merged["reasons"][0].startswith("cross-lens")
    assert merged["panel_score"] == 75.0                      # fills only absent keys
    assert merged["prescreen"]["pass"] is True                # namespaced, refreshable


def test_run_score_fresh_dir_keeps_prescreen_shape(tmp_path, monkeypatch):
    kickoff = tmp_path / "kickoff.md"
    kickoff.write_text("# Kickoff\nbody\n")
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    monkeypatch.setattr(
        "mini_ork.dispatch.llm_dispatch.llm_dispatch",
        _fake_dispatch_emitting(json.dumps({"pass": False, "score": 2, "items": []})))
    rp.mo_rubric_run_score(
        kickoff_path=str(kickoff), run_dir=str(run_dir),
        task_class="generic",
        mini_ork_root=str(REPO), mini_ork_home=str(tmp_path))
    doc = json.loads((run_dir / "panel-verdict.json").read_text())
    assert doc["panel_score"] == 25.0
    assert doc["pass"] is False
    assert doc["prescreen"]["panel_score"] == 25.0
