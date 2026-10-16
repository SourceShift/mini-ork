"""Hermetic tests for the LF-R5 harvest-hygiene surface.

``mini_ork/learning/code_findings.py`` used to store — and context v2 then
injected under "Problems reviewers already found in these files" — text that is
not a code problem: receipts ("diff applies cleanly"), harness notes ("not the
implementation"), process complaints ("pushed to origin/main … before the
reviewer node ran"), findings filed under run artifacts (``verdict.json``,
``lens-bottleneck.md``) and unlabelled prose that was auto-graded ``medium``.

This covers the classifier (``classify_note``), the filters it drives
(``parse_review`` / ``parse_verifier`` / ``normalize_file`` /
``normalize_severity``), the ``context_v2.prior_attempts`` gate and the
maintenance commands (``prune_receipts``, ``clear_artifact_files``,
``reharvest``) — all cold-safe, idempotent and hermetic.

Everything is hermetic: temp SQLite DBs and temp ``home`` dirs only; no network,
credentials or live run directory is touched.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

import mini_ork.context_v2 as context_v2
import mini_ork.learning.code_findings as code_findings


_TS = 1700000000

# ── classify_note (kickoff §1) ────────────────────────────────────────────────

# The three F12 strings the kickoff quotes, the two live rows it names (3791,
# 2286) and the passing static-check receipt.
NON_CODE_NOTES = [
    ("receipt", "Diff inspected and in scope: mini_ork/context_v2.py"),
    ("harness", "Root cause of the false is environmental, not the implementation"),
    ("harness", "To clear the gate: resolve the suite-adequacy module availability"),
    ("process", "This delivery was pushed to origin/main as commit 43740937 "
                "before the reviewer node ran"),
    ("process", "No approve is possible: files_changed=[] with 0-byte review-diff.patch"),
    ("receipt", "diff-apply-check-clean: diff applies cleanly to repo root"),
]

CODE_NOTES = [
    "The log proxy leaks into the transcript lower bound whenever impl-<node>.log exists",
    "Dead priority parse: _task_card never emits a prio segment",
]


@pytest.mark.parametrize("expected,text", NON_CODE_NOTES)
def test_non_code_notes_classify_as_their_declared_group(expected, text):
    assert code_findings.classify_note(text) == expected


@pytest.mark.parametrize("text", CODE_NOTES)
def test_genuine_code_notes_classify_as_code(text):
    assert code_findings.classify_note(text) == "code"


def test_classifier_defaults_to_code_for_an_empty_note():
    assert code_findings.classify_note("") == "code"
    assert code_findings.classify_note(None) == "code"


# ── parse_review: only code findings survive (§2) ─────────────────────────────


def test_structured_process_finding_is_not_stored():
    payload = json.dumps({
        "verdict": "fail",
        "findings": [{
            "file": "mini_ork/ide_pages/__init__.py",
            "line": 32,
            "issue": "This delivery was pushed to origin/main as commit 43740937 "
                     "before the reviewer node ran",
        }],
    })
    assert code_findings.parse_review(payload) == []


def test_structured_code_finding_is_stored_with_its_file():
    payload = json.dumps({
        "verdict": "fail",
        "findings": [{
            "file": "mini_ork/learning/code_findings.py",
            "line": 42,
            "issue": "the loop misses the last item",
        }],
    })
    found = code_findings.parse_review(payload)
    assert len(found) == 1
    assert found[0]["file"] == "mini_ork/learning/code_findings.py"
    assert found[0]["line"] == 42


def test_dict_note_that_is_a_process_complaint_is_not_stored():
    payload = json.dumps({
        "verdict": "fail",
        "notes": [{"file": "mini_ork/ide_pages/__init__.py",
                   "issue": "No approve is possible: files_changed=[]"}],
    })
    assert code_findings.parse_review(payload) == []


# ── parse_verifier: receipt/harness-only failures record nothing (§3) ─────────


def test_failed_verifier_with_only_a_receipt_note_records_nothing():
    payload = {
        "verifier": "static-check",
        "pass": False,
        "notes": ["diff-apply-check-clean: diff applies cleanly to repo root"],
    }
    assert code_findings.parse_verifier("static-check", payload) == []


def test_verifier_reason_is_none_for_receipt_and_harness_only_notes():
    assert code_findings._verifier_reason({
        "pass": False,
        "notes": [
            "diff-apply-check-clean: diff applies cleanly to repo root",
            "Root cause of the false is environmental, not the implementation",
        ],
    }) is None


def test_verifier_reason_keeps_a_real_note():
    reason = code_findings._verifier_reason({
        "pass": False,
        "notes": ["the guard is dropped on the cold path"],
    })
    assert reason and "guard is dropped" in reason


def test_failed_verifier_with_a_named_failed_check_records_one_high_row():
    payload = {
        "verifier": "static-check",
        "pass": False,
        "failed_checks": ["checksums"],
        "notes": ["diff-apply-check-clean: diff applies cleanly to repo root"],
    }
    found = code_findings.parse_verifier("static-check", payload)
    assert len(found) == 1
    assert found[0]["severity"] == "high"


def test_failed_verifier_with_missing_reads_the_missing_key():
    # No reasons/notes at all: the None path must not fire, and the ``missing``
    # key still names the failure.
    found = code_findings.parse_verifier("lens", {"pass": False,
                                                  "missing": ["bottleneck-scan.md"]})
    assert len(found) == 1
    assert "bottleneck-scan.md" in found[0]["issue"]


# ── normalize_file: run artifacts and absolute paths → None (§4) ──────────────

ARTIFACT_BASENAMES = [
    "verdict.json",
    "review-verdict.json",
    "panel-verdict.json",
    "plan.json",
    "run_profile.json",
    "synthesis.md",
    "implementer-summary.json",
    "implementer_summary.json",
    "verifier_test.json",
    "verifier-format.json",
    "review-round1.json",
    "context-pack.json",
    "lens-bottleneck.md",
    "framework-edit.diff",
    "harvest.log",
]


@pytest.mark.parametrize("name", ARTIFACT_BASENAMES)
def test_normalize_file_returns_none_for_run_artifacts(name):
    assert code_findings.normalize_file(name, known_paths=set()) is None


def test_normalize_file_returns_none_for_a_still_absolute_path():
    assert code_findings.normalize_file("/Users/admin/.py") is None


def test_normalize_file_keeps_repo_relative_paths():
    assert (code_findings.normalize_file("mini_ork/context_v2.py", known_paths=set())
            == "mini_ork/context_v2.py")
    # A worktree-prefixed absolute path strips to the repo-relative one.
    assert (code_findings.normalize_file(
        "/mini-ork-worktrees/lf-r5/mini_ork/context_v2.py", known_paths=set())
        == "mini_ork/context_v2.py")


# ── normalize_severity: unlabelled → unknown (§5) ─────────────────────────────


def test_unlabelled_finding_gets_unknown_severity():
    assert code_findings.normalize_severity(None, "fail") == "unknown"
    assert code_findings.normalize_severity(None, "approve") == "unknown"


def test_unlabelled_prose_under_fail_verdict_gets_unknown():
    found = code_findings.parse_review(
        '{"verdict": "fail", "notes": ["the loop misses the last item"]}'
    )
    assert len(found) == 1
    assert found[0]["severity"] == "unknown"


def test_blocker_prefix_still_gets_high():
    found = code_findings.parse_review(
        '{"verdict": "fail", "notes": '
        '["BLOCKER: mini_ork/widget.py:12 drops the guard on the cold path"]}'
    )
    assert len(found) == 1
    assert found[0]["severity"] == "high"


# ── maintenance commands ──────────────────────────────────────────────────────


def _make_db(tmp_path) -> str:
    """A cold temp DB carrying the code_findings schema (and nothing else)."""
    path = tmp_path / "state.db"
    sqlite3.connect(str(path)).close()
    code_findings.ensure_schema(str(path))
    return str(path)


def _insert(db, *, fingerprint, run_id, issue, file=None, line=None,
            severity="high", source="review:code", ts=_TS) -> None:
    con = sqlite3.connect(db)
    con.execute(
        "INSERT INTO code_findings (fingerprint, run_id, source, file, line, "
        "severity, category, issue, snippet, verdict, ts) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (fingerprint, run_id, source, file, line, severity, "other", issue,
         None, None, ts),
    )
    con.commit()
    con.close()


def _rows(db, run_id=None):
    con = sqlite3.connect(db)
    if run_id is None:
        rows = con.execute(
            "SELECT run_id, file, issue FROM code_findings ORDER BY id"
        ).fetchall()
    else:
        rows = con.execute(
            "SELECT run_id, file, issue FROM code_findings WHERE run_id = ? "
            "ORDER BY id",
            (run_id,),
        ).fetchall()
    con.close()
    return rows


def test_prune_receipts_drops_every_non_code_row(tmp_path):
    db = _make_db(tmp_path)
    _insert(db, fingerprint="a", run_id="r1", file="mini_ork/foo.py",
            issue="Diff inspected and in scope: mini_ork/foo.py")
    _insert(db, fingerprint="b", run_id="r1", file=None,
            issue="This delivery was pushed to origin/main before the reviewer node ran")
    _insert(db, fingerprint="c", run_id="r1", file="mini_ork/foo.py",
            issue="the loop misses the last item")

    assert code_findings.prune_receipts(db) == 2
    assert _rows(db) == [("r1", "mini_ork/foo.py", "the loop misses the last item")]
    # Idempotent.
    assert code_findings.prune_receipts(db) == 0


def test_prune_receipts_is_cold_safe(tmp_path):
    assert code_findings.prune_receipts(str(tmp_path / "absent.db")) == 0


def test_clear_artifact_files_nulls_artifacts_and_keeps_repo_paths(tmp_path):
    db = _make_db(tmp_path)
    _insert(db, fingerprint="a", run_id="r1", file="verdict.json",
            issue="the loop misses the last item")
    _insert(db, fingerprint="b", run_id="r1", file="/Users/admin/.py",
            issue="the loop misses the last item")
    _insert(db, fingerprint="c", run_id="r1", file="mini_ork/context_v2.py",
            issue="the loop misses the last item")

    first = code_findings.clear_artifact_files(db)
    assert isinstance(first, int)
    assert first == 2
    con = sqlite3.connect(db)
    got = dict(con.execute("SELECT fingerprint, file FROM code_findings").fetchall())
    con.close()
    assert got["a"] is None
    assert got["b"] is None
    assert got["c"] == "mini_ork/context_v2.py"
    # Idempotent.
    assert code_findings.clear_artifact_files(db) == 0


def test_reharvest_rewrites_existing_run_and_leaves_missing_run_rows(tmp_path):
    home = tmp_path / "home"
    run_dir = home / "runs" / "run-a"
    run_dir.mkdir(parents=True)
    (run_dir / "review-code.json").write_text(
        json.dumps({
            "verdict": "fail",
            "findings": [{
                "file": "mini_ork/widget.py",
                "line": 12,
                "issue": "the loop misses the last item",
                "severity": "high",
            }],
        }),
        encoding="utf-8",
    )

    db = _make_db(tmp_path)
    # A stale non-code row the old parser left for a run whose dir still exists.
    _insert(db, fingerprint="stale-a", run_id="run-a", file="mini_ork/old.py",
            issue="Diff inspected and in scope: mini_ork/old.py")
    # A row for a run whose dir is gone — it must survive untouched.
    _insert(db, fingerprint="orphan-b", run_id="run-b", file="mini_ork/gone.py",
            issue="the loop misses the last item")

    result = code_findings.reharvest(str(home), db=db)

    assert isinstance(result, dict)
    assert result["runs"] == 1 and result["findings"] == 1
    assert _rows(db, "run-a") == [
        ("run-a", "mini_ork/widget.py", "the loop misses the last item")
    ]
    assert _rows(db, "run-b") == [
        ("run-b", "mini_ork/gone.py", "the loop misses the last item")
    ]


def test_reharvest_is_cold_safe(tmp_path):
    assert code_findings.reharvest("", db=str(tmp_path / "absent.db"))["reason"] == "no_home"


def test_maintenance_subcommands_are_exposed_on_the_module_cli(tmp_path, capsys):
    home = tmp_path / "home"
    (home / "runs").mkdir(parents=True)
    db = _make_db(tmp_path)

    assert code_findings.main(["prune", "--db", db, "--json"]) == 0
    assert code_findings.main(["clear-artifact-files", "--db", db, "--json"]) == 0
    assert code_findings.main(
        ["reharvest", "--home", str(home), "--db", db, "--json"]
    ) == 0
    capsys.readouterr()


# ── context_v2: severity rank + prior_attempts gate (§5, §6) ──────────────────


def test_severity_rank_puts_unknown_below_the_default_floor():
    assert context_v2._SEVERITY_RANK.get("unknown") == 0
    assert context_v2._SEVERITY_RANK["unknown"] < context_v2._SEVERITY_RANK["medium"]


def test_prior_attempts_drops_unknown_and_non_code_rows(tmp_path, monkeypatch):
    monkeypatch.setenv("MO_CONTEXT_V2_MIN_SEVERITY", "medium")
    db = _make_db(tmp_path)
    kickoff = str(tmp_path / "kickoffs" / "lf-r5-harvest-hygiene.md")

    con = sqlite3.connect(db)
    con.execute(
        "CREATE TABLE task_runs (id TEXT PRIMARY KEY, status TEXT, cost_usd REAL, "
        "created_at INTEGER, kickoff_path TEXT)"
    )
    con.execute(
        "INSERT INTO task_runs VALUES (?, ?, ?, ?, ?)",
        ("run-prev", "completed", 0.0, _TS, kickoff),
    )
    con.commit()
    con.close()

    _insert(db, fingerprint="k", run_id="run-prev", file="mini_ork/widget.py",
            line=1, severity="high", issue="the loop misses the last item")
    _insert(db, fingerprint="u", run_id="run-prev", file="mini_ork/widget.py",
            line=2, severity="unknown", issue="the loop misses the last item")
    _insert(db, fingerprint="n", run_id="run-prev", file="mini_ork/widget.py",
            line=3, severity="high",
            issue="Diff inspected and in scope: mini_ork/widget.py")

    out = context_v2.prior_attempts(kickoff, db=db, current_run="run-other")

    assert len(out) == 1
    findings = out[0]["findings"]
    assert len(findings) == 1
    assert findings[0]["file"] == "mini_ork/widget.py"
    assert findings[0]["line"] == 1
    assert findings[0]["severity"] == "high"


# ── reflect wiring (§7) ───────────────────────────────────────────────────────


def test_reflect_wires_the_hygiene_prunes_but_never_reharvest():
    reflect_py = (
        Path(code_findings.__file__).resolve().parent.parent / "cli" / "reflect.py"
    )
    src = reflect_py.read_text(encoding="utf-8")
    assert "prune_receipts" in src
    assert "clear_artifact_files" in src
    assert "reharvest(" not in src
