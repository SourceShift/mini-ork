"""Hermetic tests for ``mini_ork.learning.code_findings``.

Models after ``test_learning_ledger.py`` (temp sqlite, temp home, no
migrations, no live ``state.db``). Covers the kickoff's contract list:

- ``parse_review`` on all four live shapes (plain JSON findings, fenced JSON
  with prose around it, prose bullets, notes-as-strings with ``file.py:123``).
- ``parse_verifier`` on a failed and a passed payload.
- ``categorize``: one real issue string per category, plus an "other".
- Bare-filename resolution (one match → full path; two → stays bare).
- ``harvest`` is incremental (second call adds 0) and idempotent per fingerprint.
- ``areas`` groups by directory, honours ``since_days``, orders high first.
- ``findings_for`` returns the run titles (and survives a missing ``task_runs`` row).
"""
from __future__ import annotations

import json
import shutil
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from mini_ork.learning import code_findings  # noqa: E402


# ── helpers ────────────────────────────────────────────────────────────────────


def _bare_db(path: Path) -> Path:
    """An empty sqlite file. ensure_schema() must build the tables on demand."""
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    con.close()
    return path


def _run_dir(home: Path, run_id: str) -> Path:
    d = home / "runs" / run_id
    d.mkdir(parents=True)
    return d


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


# ── parse_review: the four live shapes ─────────────────────────────────────────


def test_parse_review_plain_json_findings() -> None:
    text = json.dumps({
        "verdict": "needs_revision",
        "findings": [
            {
                "file": "tests/unit/test_acp_agent_py.py",
                "line": 6215,
                "snippet": "assert f\"Attached image: {expected}\" in prompt",
                "severity": "medium",
                "issue": "new test passes on b069825e; kickoff requires it to fail there",
            }
        ],
    })
    findings = code_findings.parse_review(text)
    assert len(findings) == 1
    f = findings[0]
    assert f["file"] == "tests/unit/test_acp_agent_py.py"
    assert f["line"] == 6215
    assert f["severity"] == "medium"
    assert f["issue"] == "new test passes on b069825e; kickoff requires it to fail there"


def test_parse_review_fenced_json_with_prose() -> None:
    text = (
        "Here is the review.\n```json\n"
        + json.dumps({
            "verdict": "APPROVE",
            "notes": ["all good", "mini_ork/acp/agent.py:1529 fixed"],
        })
        + "\n```\nthanks"
    )
    findings = code_findings.parse_review(text)
    paths = [f["file"] for f in findings]
    assert "mini_ork/acp/agent.py" in paths
    f = next(f for f in findings if f["file"] == "mini_ork/acp/agent.py")
    assert f["line"] == 1529
    # APPROVE is a pass → the file-bearing note defaults to low severity.
    assert f["severity"] == "low"
    # The "all good" note names no file and the verdict is a pass → dropped.
    assert all(f["file"] is not None for f in findings)


def test_parse_review_prose_bullets() -> None:
    text = (
        "**Findings:**\n"
        "- `framework-edit.diff` absent at `$MINI_ORK_RUN_DIR` root.\n"
        "- mini_ork/acp/agent.py:1529 stale docstring."
    )
    findings = code_findings.parse_review(text)
    with_file = [f for f in findings if f["file"] is not None]
    no_file = [f for f in findings if f["file"] is None]
    assert with_file and with_file[0]["file"] == "mini_ork/acp/agent.py"
    assert with_file[0]["line"] == 1529
    # Prose has no verdict → non-pass, so the file-less bullet is kept.
    assert no_file and "absent" in no_file[0]["issue"]


def test_parse_review_json_fence_after_python_fence() -> None:
    # Reviewer findings[2]: a ```python block before the ```json fence used to
    # break the parse — the python block's *closing* fence was matched as an
    # opener and the real json fence was consumed, so the structured finding was
    # lost (line=None, verdict=None).
    text = (
        "```python\nx = 1\n```\n"
        "and here is the payload:\n"
        "```json\n"
        + json.dumps({
            "verdict": "needs_revision",
            "findings": [{"file": "mini_ork/a.py", "line": 7,
                          "issue": "wrong", "severity": "high"}],
        })
        + "\n```\n"
    )
    findings = code_findings.parse_review(text)
    assert len(findings) == 1
    assert findings[0]["file"] == "mini_ork/a.py"
    assert findings[0]["line"] == 7
    assert findings[0]["verdict"] == "needs_revision"


def test_parse_review_notes_as_strings_with_line() -> None:
    text = json.dumps({
        "verdict": "needs_revision",
        "notes": [
            "SUMMARY: scope respected.",
            "FINDING 1: tests/unit/file.py:123 the new test never asserts the claim",
        ],
    })
    findings = code_findings.parse_review(text)
    paths = [f["file"] for f in findings]
    assert "tests/unit/file.py" in paths
    f = next(f for f in findings if f["file"] == "tests/unit/file.py")
    assert f["line"] == 123
    # The SUMMARY note names no file and the verdict is not a pass → kept.
    assert any(f["file"] is None and "SUMMARY" in f["issue"] for f in findings)


def test_parse_review_dict_notes_under_pass_dropped() -> None:
    # §1: a dict note naming no file must not become a "(no issue)" row when
    # the review was approved; it survives only under a non-pass verdict.
    text = json.dumps({
        "verdict": "APPROVE",
        "notes": [
            {"issue": "looks good"},                        # no file, pass → dropped
            {"file": "mini_ork/x.py", "issue": "nit"},      # names a file → kept
            {},                                             # empty → dropped
        ],
    })
    findings = code_findings.parse_review(text)
    assert [f["file"] for f in findings] == ["mini_ork/x.py"]

    nonpass = json.dumps({
        "verdict": "needs_revision",
        "notes": [{"issue": "scope creep in the diff"}],
    })
    f2 = code_findings.parse_review(nonpass)
    assert any(f["file"] is None and "scope creep" in f["issue"] for f in f2)


def test_parse_review_coerces_malformed_field_types() -> None:
    # A machine-written review can carry the wrong types (issue as a list,
    # verdict as a dict). Parsing must not raise and every field must land as a
    # JSON-safe scalar (reviewer findings[0] — harvest used to AttributeError).
    text = json.dumps({
        "verdict": {"weird": True},
        "findings": [{"file": ["a.py"], "issue": ["b", "c"], "line": "1?2"}],
    })
    findings = code_findings.parse_review(text)
    assert findings
    # A container in a scalar field degrades to None, never a JSON blob.
    assert findings[0]["file"] is None
    assert findings[0]["verdict"] is None
    for f in findings:
        assert isinstance(f["issue"], str)
        assert f["file"] is None or isinstance(f["file"], str)
        assert f["verdict"] is None or isinstance(f["verdict"], str)
        assert f["line"] is None or isinstance(f["line"], int)


# ── parse_verifier ─────────────────────────────────────────────────────────────


def test_parse_verifier_failed() -> None:
    payload = {
        "verifier": "bottlenecks-found",
        "pass": False,
        "missing": ["bottleneck-scan.md", "synthesis.md"],
    }
    findings = code_findings.parse_verifier("bottlenecks-found", payload)
    assert len(findings) == 1
    f = findings[0]
    assert f["severity"] == "high"
    assert f["file"] == "bottleneck-scan.md"
    assert "bottleneck-scan.md" in f["issue"]


def test_parse_verifier_passed() -> None:
    payload = {"verifier": "lens-exists", "pass": True, "reasons": []}
    assert code_findings.parse_verifier("lens-exists", payload) == []


def test_parse_verifier_first_failed_check_from_checks_list() -> None:
    # The real framework-edit shape (recipes/framework-edit/verifiers/*.py):
    # failed_checks names the check, checks[] carries its expected/actual.
    payload = {
        "verifier": "static-check",
        "pass": False,
        "verdict": "fail",
        "failed_checks": ["diff-apply-check-clean"],
        "checks": [
            {"name": "artifact-diff-exists", "expected": "x", "actual": "y", "pass": True},
            {"name": "diff-apply-check-clean", "expected": "diff applies cleanly to repo root",
             "actual": "see evidence log", "pass": False},
        ],
    }
    findings = code_findings.parse_verifier("static-check", payload)
    assert len(findings) == 1
    f = findings[0]
    assert f["severity"] == "high"
    assert "diff-apply-check-clean" in f["issue"]
    assert "diff applies cleanly" in f["issue"]


def test_parse_verifier_checks_list_without_failed_checks() -> None:
    payload = {
        "pass": False,
        "checks": [
            {"name": "a", "expected": "e1", "pass": True},
            {"name": "b", "expected": "e2", "pass": False},
        ],
    }
    assert "b" in code_findings.parse_verifier("v", payload)[0]["issue"]


def test_parse_verifier_checks_map_shape() -> None:
    payload = {"pass": False, "checks": {"no-todo-markers": False, "lint": True}}
    assert code_findings.parse_verifier("v", payload)[0]["issue"] == "no-todo-markers"


def test_parse_verifier_warning_prefixed_json() -> None:
    # Live shape (reviewer findings[0]): the framework-edit verifiers write a
    # Python DeprecationWarning line to stderr before the JSON payload. Captured
    # together the file is neither valid JSON nor fenced, so before the fix the
    # payload degraded to {"raw": text}, _verifier_failed returned False, and
    # every framework-edit verifier failure was dropped silently.
    text = (
        "framework-edit-shape.py:87: DeprecationWarning: "
        "invalid escape sequence at position 3\n"
        "  matches = re.findall(pattern, body)\n"
        + json.dumps({
            "verifier": "static-check",
            "pass": False,
            "verdict": "fail",
            "failed_checks": ["diff-apply-check-clean"],
            "checks": [{"name": "diff-apply-check-clean",
                        "expected": "diff applies cleanly to repo root",
                        "actual": "already exists in working directory",
                        "pass": False}],
        })
    )
    findings = code_findings.parse_verifier("static-check", text)
    assert len(findings) == 1
    assert findings[0]["severity"] == "high"
    assert "diff-apply-check-clean" in findings[0]["issue"]


def test_scan_json_object_skips_invalid_braces() -> None:
    # Noise before the payload may itself contain '{' — the scan must step past
    # a brace that does not start a decodable object.
    text = 'cfg = {"a": } broken\n{"verifier": "v", "pass": false}\n'
    assert code_findings._scan_json_object(text) == {"verifier": "v", "pass": False}


# ── categorize ─────────────────────────────────────────────────────────────────


def test_categorize_each_category_and_other() -> None:
    cases = [
        ("the new test never asserts the claim", "test doesn't check the claim"),
        ("the test passes on base", "test doesn't check the claim"),
        ("docstring contradicts the fix", "comment or docstring contradicts code"),
        ("stale docstring on the helper", "comment or docstring contradicts code"),
        ("missing a guard, will raise on cold path", "missing guard or error handling"),
        ("raises KeyError when the row is missing", "missing guard or error handling"),
        ("this change is out of scope", "change outside the agreed scope"),
        ("drops the result, off by one", "wrong behaviour"),
        ("quadratic loop is slow", "performance"),
        ("leaks the secret token", "security"),
        ("framework-edit.diff absent", "missing artifact or output"),
        ("looks fine", "other"),
    ]
    for issue, expected in cases:
        assert code_findings.categorize(issue) == expected, issue


def test_normalize_severity() -> None:
    assert code_findings.normalize_severity("blocking") == "high"
    assert code_findings.normalize_severity("critical") == "high"
    assert code_findings.normalize_severity("medium") == "medium"
    assert code_findings.normalize_severity("minor") == "low"
    assert code_findings.normalize_severity("nit") == "low"
    # Missing severity defaults by verdict.
    assert code_findings.normalize_severity(None, "needs_revision") == "medium"
    assert code_findings.normalize_severity(None, "APPROVE") == "low"


# ── file normalization ─────────────────────────────────────────────────────────


def test_bare_filename_resolution_one_match_and_ambiguous() -> None:
    paths = {"mini_ork/ide_pages/node.py", "mini_ork/other.py"}
    assert code_findings.normalize_file("node.py", known_paths=paths) == "mini_ork/ide_pages/node.py"
    ambiguous = {"a/node.py", "b/node.py"}
    assert code_findings.normalize_file("node.py", known_paths=ambiguous) == "node.py"


def test_normalize_file_strips_prefixes() -> None:
    assert code_findings.normalize_file("./mini_ork/foo.py", known_paths=set()) == "mini_ork/foo.py"
    abs_worktree = "/Volumes/ps/mini-ork-worktrees/eng-code-findings/mini_ork/foo.py"
    assert code_findings.normalize_file(abs_worktree, known_paths=set()) == "mini_ork/foo.py"
    abs_home = "/Volumes/ps/mini-ork/.mini-ork/runs/r1/lens.md"
    assert code_findings.normalize_file(abs_home, known_paths=set()) == ".mini-ork/runs/r1/lens.md"


def test_normalize_file_strips_target_cwd() -> None:
    # §5: the run's MO_TARGET_CWD (from run_profile.json) is stripped so a
    # finding in another repo becomes a repo-relative path.
    tc = "/Users/dev/other-repo"
    assert code_findings.normalize_file(
        "/Users/dev/other-repo/src/app/main.py", known_paths=set(), target_cwd=tc
    ) == "src/app/main.py"
    assert code_findings.normalize_file(
        "/elsewhere/x.py", known_paths=set(), target_cwd=tc
    ) == "/elsewhere/x.py"


def test_bare_filename_resolves_via_git_ls_files(tmp_path: Path) -> None:
    if shutil.which("git") is None:
        import pytest  # noqa: F401
        pytest.skip("git not available")
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    (repo / "mini_ork").mkdir()
    (repo / "mini_ork" / "node.py").write_text("", encoding="utf-8")
    (repo / "other").mkdir()
    (repo / "other" / "x.py").write_text("", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    code_findings._git_cache.pop(str(repo), None)
    assert code_findings.normalize_file("node.py", repo_root=str(repo)) == "mini_ork/node.py"


# ── harvest ────────────────────────────────────────────────────────────────────


def _seed_run(home: Path, run_id: str) -> None:
    d = _run_dir(home, run_id)
    _write(d / "review-reviewer.json", json.dumps({
        "verdict": "needs_revision",
        "findings": [
            {"file": "mini_ork/ide_pages/node.py", "line": 42, "severity": "low",
             "issue": "docstring contradicts the fix"},
        ],
    }))
    _write(d / "verifier-result-no-regression.json", json.dumps({
        "verifier": "no-regression", "pass": False, "missing": ["bottleneck-scan.md"],
    }))


def test_harvest_is_incremental_and_idempotent(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(code_findings, "_git_ls_files", lambda *_: None)
    home = tmp_path / "home"
    db = _bare_db(tmp_path / "cf.db")
    _seed_run(home, "run-a")

    first = code_findings.harvest(str(home), db=str(db))
    assert first["runs"] == 1
    assert first["findings"] == 2  # one review + one verifier
    assert first["with_file"] == 2

    # Second call: run already recorded → skipped entirely.
    second = code_findings.harvest(str(home), db=str(db))
    assert second["runs"] == 0
    assert second["findings"] == 0

    # Forcing the same run re-reads files but INSERT OR IGNORE dedupes.
    forced = code_findings.harvest(str(home), db=str(db), run_ids=["run-a"])
    assert forced["runs"] == 1
    assert forced["findings"] == 0

    con = sqlite3.connect(db)
    try:
        n = con.execute("SELECT COUNT(*) FROM code_findings").fetchone()[0]
        runs = con.execute("SELECT COUNT(*) FROM code_findings_runs").fetchone()[0]
    finally:
        con.close()
    assert n == 2
    assert runs == 1


def test_harvest_marks_failed_run_and_does_not_stall(tmp_path: Path, monkeypatch) -> None:
    # A run that always fails must not block every run after it: each run is
    # marked harvested even on failure, and later runs are still read
    # (reviewer findings[0]).
    monkeypatch.setattr(code_findings, "_git_ls_files", lambda *_: None)
    home = tmp_path / "home"
    db = _bare_db(tmp_path / "cf.db")
    _seed_run(home, "a-bad")
    _seed_run(home, "b-good")

    real = code_findings._harvest_run

    def _boom(con, run_id, run_dir, repo_root):
        if run_id == "a-bad":
            raise RuntimeError("malformed review file")
        return real(con, run_id, run_dir, repo_root)

    monkeypatch.setattr(code_findings, "_harvest_run", _boom)
    stats = code_findings.harvest(str(home), db=str(db))
    assert stats["runs"] == 2
    assert stats["findings"] == 2  # from b-good only; the bad run added nothing

    con = sqlite3.connect(db)
    try:
        marked = {r[0] for r in con.execute("SELECT run_id FROM code_findings_runs")}
    finally:
        con.close()
    assert marked == {"a-bad", "b-good"}

    # Second pass does not re-hit the recorded bad run.
    monkeypatch.setattr(code_findings, "_harvest_run", real)
    again = code_findings.harvest(str(home), db=str(db))
    assert again["runs"] == 0


def test_harvest_leaves_a_locked_run_unmarked(tmp_path: Path, monkeypatch) -> None:
    # A transient lock must not record the run as done with n=0: its findings
    # would never be read. The next pass picks it up.
    monkeypatch.setattr(code_findings, "_git_ls_files", lambda *_: None)
    home = tmp_path / "home"
    db = _bare_db(tmp_path / "cf.db")
    _seed_run(home, "run-a")
    real = code_findings._harvest_run

    def _locked(con, run_id, run_dir, repo_root):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(code_findings, "_harvest_run", _locked)
    code_findings.harvest(str(home), db=str(db))
    con = sqlite3.connect(db)
    try:
        assert con.execute("SELECT COUNT(*) FROM code_findings_runs").fetchone()[0] == 0
    finally:
        con.close()

    monkeypatch.setattr(code_findings, "_harvest_run", real)
    again = code_findings.harvest(str(home), db=str(db))
    assert again["runs"] == 1 and again["findings"] == 2


def test_harvest_skips_runs_in_flight_but_reads_stuck_ones(tmp_path: Path, monkeypatch) -> None:
    # A run still executing has not written its reviews: harvesting it would
    # freeze it as done. A run "executing" for more than 24 h is stuck, not in
    # flight, and is read.
    import time as _time

    monkeypatch.setattr(code_findings, "_git_ls_files", lambda *_: None)
    home = tmp_path / "home"
    db = _bare_db(tmp_path / "cf.db")
    _seed_run(home, "run-live")
    _seed_run(home, "run-stuck")
    _seed_run(home, "run-done")
    con = sqlite3.connect(db)
    try:
        con.execute("CREATE TABLE IF NOT EXISTS task_runs "
                    "(id TEXT PRIMARY KEY, kickoff_path TEXT, status TEXT, created_at INTEGER)")
        now = int(_time.time())
        con.executemany("INSERT INTO task_runs (id, status, created_at) VALUES (?,?,?)", [
            ("run-live", "executing", now - 60),
            ("run-stuck", "executing", now - 3 * 86400),
            ("run-done", "published", now - 60),
        ])
        con.commit()
    finally:
        con.close()

    code_findings.harvest(str(home), db=str(db))
    con = sqlite3.connect(db)
    try:
        marked = {r[0] for r in con.execute("SELECT run_id FROM code_findings_runs")}
    finally:
        con.close()
    assert marked == {"run-stuck", "run-done"}


def test_harvest_reads_stdout_md_fallback(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(code_findings, "_git_ls_files", lambda *_: None)
    home = tmp_path / "home"
    db = _bare_db(tmp_path / "cf.db")
    d = _run_dir(home, "run-md")
    # An empty .json → the .stdout.md carries the content (kickoff shape #4).
    _write(d / "review-reviewer.json", "")
    _write(d / "review-reviewer.json.stdout.md", json.dumps({
        "verdict": "needs_revision",
        "findings": [{"file": "mini_ork/y.py", "issue": "broken", "severity": "high"}],
    }))
    stats = code_findings.harvest(str(home), db=str(db))
    assert stats["findings"] == 1


def test_harvest_reads_underscore_verifier_file(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(code_findings, "_git_ls_files", lambda *_: None)
    home = tmp_path / "home"
    db = _bare_db(tmp_path / "cf.db")
    d = _run_dir(home, "run-u")
    _write(d / "verifier_static-check.json", json.dumps({
        "verifier": "static-check",
        "pass": False,
        "failed_checks": ["diff-apply-check-clean"],
        "checks": [{"name": "diff-apply-check-clean", "expected": "applies cleanly",
                    "pass": False}],
    }))
    stats = code_findings.harvest(str(home), db=str(db))
    assert stats["findings"] == 1
    con = sqlite3.connect(db)
    try:
        src = con.execute("SELECT source FROM code_findings").fetchone()[0]
    finally:
        con.close()
    assert src == "verifier:static-check"


def test_harvest_reads_warning_prefixed_verifier_file(tmp_path: Path, monkeypatch) -> None:
    # The reviewer's exact repro: on this host a real framework-edit verifier
    # file is a DeprecationWarning line followed by the JSON. Before the fix it
    # yielded 0 findings and was not counted anywhere. The filename is the
    # underscore form the framework-edit recipe writes.
    monkeypatch.setattr(code_findings, "_git_ls_files", lambda *_: None)
    home = tmp_path / "home"
    db = _bare_db(tmp_path / "cf.db")
    d = _run_dir(home, "run-warn")
    _write(d / "verifier_static-check.json",
           "framework-edit-shape.py:87: DeprecationWarning: invalid escape "
           "sequence at position 3\n"
           "  matches = re.findall(pattern, body)\n"
           + json.dumps({
               "verifier": "static-check",
               "pass": False,
               "verdict": "fail",
               "failed_checks": ["diff-apply-check-clean"],
           }))
    stats = code_findings.harvest(str(home), db=str(db))
    assert stats["findings"] == 1
    con = sqlite3.connect(db)
    try:
        src = con.execute("SELECT source FROM code_findings").fetchone()[0]
    finally:
        con.close()
    assert src == "verifier:static-check"


def test_harvest_strips_run_target_cwd(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(code_findings, "_git_ls_files", lambda *_: None)
    home = tmp_path / "home"
    db = _bare_db(tmp_path / "cf.db")
    d = _run_dir(home, "run-t")
    _write(d / "run_profile.json", json.dumps({"roots": {"exec_cwd": "/srv/app"}}))
    _write(d / "review-reviewer.json", json.dumps({
        "verdict": "needs_revision",
        "findings": [{"file": "/srv/app/src/x.py", "issue": "wrong", "severity": "high"}],
    }))
    code_findings.harvest(str(home), db=str(db))
    con = sqlite3.connect(db)
    try:
        files = [r[0] for r in con.execute("SELECT file FROM code_findings")]
    finally:
        con.close()
    assert files == ["src/x.py"]


# ── areas ──────────────────────────────────────────────────────────────────────


def _insert_finding(db: Path, *, file, severity, category, run_id, ts, issue="x") -> None:
    con = sqlite3.connect(db)
    try:
        con.execute(
            "INSERT INTO code_findings (fingerprint, run_id, source, file, line, "
            "severity, category, issue, snippet, verdict, ts) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (f"fp-{file}-{run_id}-{ts}", run_id, "review:reviewer", file, None,
             severity, category, issue, None, None, ts),
        )
        con.commit()
    finally:
        con.close()


def test_areas_groups_orders_and_honours_since_days(tmp_path: Path) -> None:
    db = _bare_db(tmp_path / "cf.db")
    code_findings.ensure_schema(str(db))
    now = int(time.time())
    for i in range(3):
        _insert_finding(db, file="mini_ork/ide_pages/node.py", severity="high",
                        category="wrong behaviour", run_id=f"r{i}", ts=now)
    _insert_finding(db, file="mini_ork/ide_pages/board.py", severity="medium",
                    category="performance", run_id="r9", ts=now)
    _insert_finding(db, file="mini_ork/learning/foo.py", severity="low",
                    category="other", run_id="r9", ts=now)
    _insert_finding(db, file="mini_ork/learning/bar.py", severity="low",
                    category="other", run_id="r9", ts=now - 40 * 86400)

    result = code_findings.areas(db=str(db), since_days=30)

    # node.py dominates its area (3 of 4) → the area IS the file; high first.
    assert result[0]["area"] == "mini_ork/ide_pages/node.py"
    assert result[0]["n_findings"] == 4
    assert result[0]["worst_severity"] == "high"
    # The stale file is outside since_days → absent everywhere.
    all_files = [f for r in result for f, _ in r["files"]]
    assert "mini_ork/learning/bar.py" not in all_files


def test_areas_never_blank_label(tmp_path: Path) -> None:
    db = _bare_db(tmp_path / "cf.db")
    code_findings.ensure_schema(str(db))
    now = int(time.time())
    # A bare filename (unresolved) has no directory prefix — the area label
    # must still be non-empty.
    _insert_finding(db, file="node.py", severity="high", category="other",
                    run_id="r", ts=now)
    result = code_findings.areas(db=str(db), since_days=30)
    assert result and result[0]["area"] == "node.py"


def test_areas_bare_filenames_stay_separate(tmp_path: Path) -> None:
    # Reviewer findings[1]: unrelated unresolved filenames must not collapse
    # into one empty-prefix bucket labelled after whichever is most frequent.
    db = _bare_db(tmp_path / "cf.db")
    code_findings.ensure_schema(str(db))
    now = int(time.time())
    for i in range(3):
        _insert_finding(db, file="verdict.json", severity="medium", category="other",
                        run_id=f"r{i}", ts=now)
    _insert_finding(db, file="pyproject.toml", severity="low", category="other",
                    run_id="r9", ts=now)
    _insert_finding(db, file="CLAUDE.md", severity="low", category="other",
                    run_id="r9", ts=now)

    result = code_findings.areas(db=str(db), since_days=30)
    by_area = {r["area"]: r for r in result}
    assert set(by_area) == {"verdict.json", "pyproject.toml", "CLAUDE.md"}
    assert by_area["verdict.json"]["n_findings"] == 3
    assert by_area["verdict.json"]["n_runs"] == 3
    assert by_area["pyproject.toml"]["n_findings"] == 1


# ── findings_for ───────────────────────────────────────────────────────────────


def test_findings_for_returns_run_titles(tmp_path: Path) -> None:
    db = _bare_db(tmp_path / "cf.db")
    code_findings.ensure_schema(str(db))
    con = sqlite3.connect(db)
    try:
        con.execute(
            "CREATE TABLE task_runs (id TEXT PRIMARY KEY, kickoff_path TEXT, status TEXT)"
        )
        con.commit()
    finally:
        con.close()

    kickoff = tmp_path / "kickoff.md"
    _write(kickoff, "# Foo Kickoff\n\nbody\n")

    con = sqlite3.connect(db)
    try:
        con.execute(
            "INSERT INTO task_runs (id, kickoff_path, status) VALUES ('run-1', ?, 'published')",
            (str(kickoff),),
        )
        con.commit()
    finally:
        con.close()

    now = int(time.time())
    _insert_finding(db, file="mini_ork/foo.py", severity="high",
                    category="other", run_id="run-1", ts=now, issue="a")
    _insert_finding(db, file="mini_ork/foo.py", severity="low",
                    category="other", run_id="run-2", ts=now - 1, issue="b")

    result = code_findings.findings_for("mini_ork/foo", db=str(db))
    assert len(result) == 2
    # Newest first.
    assert result[0]["run_id"] == "run-1"
    assert result[0]["run_title"] == "Foo Kickoff"
    assert result[0]["run_status"] == "published"
    # run-2 has no task_runs row → LEFT JOIN keeps it, title falls back to id.
    assert result[1]["run_id"] == "run-2"
    assert result[1]["run_title"] == "run-2"


def test_prose_finding_takes_severity_from_its_leading_word() -> None:
    # Live 2026-10-07: string findings like "BLOCKER node.py:2440 …" came out
    # as medium because only the (absent) severity field was read.
    items = code_findings.parse_review(json.dumps({
        "verdict": "needs_revision",
        "notes": ["BLOCKER mini_ork/ide_pages/node.py:2440: pill uses the offset slice",
                  "nit: tests/unit/test_x.py:3 unused fixture",
                  "MAJOR mini_ork/acp/agent.py:10 drops the guard"],
    }))
    sev = {i["file"]: i["severity"] for i in items}
    assert sev["mini_ork/ide_pages/node.py"] == "high"
    assert sev["tests/unit/test_x.py"] == "low"
    assert sev["mini_ork/acp/agent.py"] == "high"
