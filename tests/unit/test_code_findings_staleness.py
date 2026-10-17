"""LF-R11a — findings age out and go stale at HEAD.

Hermetic: temp git repositories (whose HEAD decides staleness) and temp SQLite
databases. The live DB under ``.mini-ork/`` is never opened.

Covers the kickoff's test list:

- ``normalize_for_match`` collapses whitespace;
- ``stale_reason``: a snippet still at HEAD (different indentation) is fresh; the
  same row after a rewrite is ``snippet-absent``; a deleted file is
  ``file-missing``; no snippet falls back to age-only; ``max_age_days=0`` keeps
  the old row;
- ``max_age_days_from_env`` maps unset/empty → 30, ``"0"`` → 0, malformed → 30
  with one stderr warning;
- ``file_texts_at_head`` reads HEAD, not the dirty worktree, falls back to the
  worktree for untracked paths, maps a missing path to None and returns ``{}``
  for a non-git root;
- ``scope_findings`` drops aged and snippet-stale rows (and brings the stale one
  back under ``MO_FINDINGS_STALENESS=0``), the pack records the drop, and no row
  is ever deleted;
- the "Your code" tab hides stale rows and prints the hidden-count line;
- ``_harvest_run`` stores no row for a failed verifier whose only reason is a
  process note.
"""
from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from mini_ork import context_v2  # noqa: E402
from mini_ork.ide_pages.learn import code as code_tab  # noqa: E402
from mini_ork.learning import code_findings as cf  # noqa: E402

NOW = int(time.time())
DAY = 86400
THING = "src/lib/core/thing.py"
THING_HEAD = "def total(xs):\n    return sum(xs)\n"


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for var in ("MO_CONTEXT_V2_MAX_AGE_DAYS", "MO_FINDINGS_STALENESS",
                "MO_TARGET_CWD", "MINI_ORK_DB", "MINI_ORK_HOME", "MINI_ORK_ROOT"):
        monkeypatch.delenv(var, raising=False)


def _git(repo, *args):
    subprocess.run(
        ["git", "-C", str(repo), "-c", "user.email=t@example.com",
         "-c", "user.name=T", *args],
        check=True, capture_output=True, text=True,
    )


def _git_repo(tmp_path: Path, name: str = "repo") -> Path:
    repo = tmp_path / name
    (repo / "src/lib/core").mkdir(parents=True)
    _git(repo, "init", "-q")
    (repo / THING).write_text(THING_HEAD, encoding="utf-8")
    (repo / "src/lib/core/other.py").write_text("OTHER = 1\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "init")
    return repo


# ── primitives ──────────────────────────────────────────────────────────────


def test_normalize_for_match_collapses_whitespace():
    assert cf.normalize_for_match("    return sum(xs)\n") == "return sum(xs)"
    assert cf.normalize_for_match("a\t b\n  c") == "a b c"
    assert cf.normalize_for_match("") == ""


def test_stale_reason_snippet_still_at_head_is_fresh(tmp_path):
    repo = _git_repo(tmp_path)
    row = {"file": THING, "snippet": "return sum(xs)", "ts": NOW - 3600}
    assert cf.stale_reason(row, repo_root=str(repo),
                           file_texts={THING: THING_HEAD},
                           now=NOW, max_age_days=30) is None


def test_stale_reason_rewritten_line_is_snippet_absent(tmp_path):
    repo = _git_repo(tmp_path)
    row = {"file": THING, "snippet": "return sum(xs)", "ts": NOW - 3600}
    rewritten = {THING: "def total(xs):\n    return math.fsum(xs)\n"}
    assert cf.stale_reason(row, repo_root=str(repo), file_texts=rewritten,
                           now=NOW, max_age_days=30) == "snippet-absent"


def test_stale_reason_deleted_file_is_file_missing(tmp_path):
    repo = _git_repo(tmp_path)
    row = {"file": "src/lib/core/gone.py", "snippet": "x = 1", "ts": NOW - 3600}
    assert cf.stale_reason(row, repo_root=str(repo),
                           file_texts={"src/lib/core/gone.py": None},
                           now=NOW, max_age_days=30) == "file-missing"


def test_stale_reason_no_snippet_is_age_only(tmp_path):
    repo = _git_repo(tmp_path)
    head = {THING: THING_HEAD}
    old = {"file": THING, "snippet": None, "ts": NOW - 40 * DAY}
    recent = {"file": THING, "snippet": None, "ts": NOW - 3600}
    assert cf.stale_reason(old, repo_root=str(repo), file_texts=head,
                           now=NOW, max_age_days=30) == "age"
    assert cf.stale_reason(recent, repo_root=str(repo), file_texts=head,
                           now=NOW, max_age_days=30) is None


def test_stale_reason_max_age_zero_keeps_the_old_row(tmp_path):
    repo = _git_repo(tmp_path)
    row = {"file": THING, "snippet": None, "ts": NOW - 400 * DAY}
    assert cf.stale_reason(row, repo_root=str(repo), file_texts={THING: THING_HEAD},
                           now=NOW, max_age_days=0) is None


def test_max_age_days_from_env_maps_values():
    var = "MO_CONTEXT_V2_MAX_AGE_DAYS"
    assert cf.max_age_days_from_env(environ={}) == 30
    assert cf.max_age_days_from_env(environ={var: ""}) == 30
    assert cf.max_age_days_from_env(environ={var: "0"}) == 0
    assert cf.max_age_days_from_env(environ={var: "7"}) == 7


def test_max_age_days_from_env_warns_once_for_malformed_values(capsys):
    var = "MO_CONTEXT_V2_MAX_AGE_DAYS"
    # First malformed call in this process emits the warning …
    assert cf.max_age_days_from_env(environ={var: "-3"}) == 30
    err = capsys.readouterr().err
    assert "[code_findings]" in err
    # … and later ones stay silent (once per process, not once per row).
    assert cf.max_age_days_from_env(environ={var: "soon"}) == 30
    assert capsys.readouterr().err == ""


def test_file_texts_at_head_reads_head_not_the_worktree(tmp_path):
    repo = _git_repo(tmp_path)
    (repo / THING).write_text("TOTALLY DIFFERENT\n", encoding="utf-8")  # dirty
    (repo / "src/lib/core/other.py").unlink()  # tracked at HEAD
    got = cf.file_texts_at_head(str(repo), [THING, "src/lib/core/other.py"])
    assert got == {THING: THING_HEAD, "src/lib/core/other.py": "OTHER = 1\n"}


def test_file_texts_at_head_untracked_fallback_and_missing(tmp_path):
    repo = _git_repo(tmp_path)
    untracked = "src/lib/core/untracked.py"
    (repo / untracked).write_text("UNTRACKED = True\n", encoding="utf-8")
    got = cf.file_texts_at_head(str(repo), [untracked, "src/lib/core/ghost.py", THING])
    assert got == {untracked: "UNTRACKED = True\n",
                   "src/lib/core/ghost.py": None,
                   THING: THING_HEAD}


def test_file_texts_at_head_non_git_root_returns_empty(tmp_path):
    plain = tmp_path / "plain"
    (plain / "src/lib/core").mkdir(parents=True)
    (plain / THING).write_text(THING_HEAD, encoding="utf-8")
    assert cf.file_texts_at_head(str(plain), [THING]) == {}


def test_annotate_staleness_copies_rows_and_marks_reasons(tmp_path):
    repo = _git_repo(tmp_path)
    rows = [
        {"file": THING, "snippet": "return sum(xs)", "ts": NOW - 3600, "issue": "fresh"},
        {"file": THING, "snippet": "return sum(xs) * 2", "ts": NOW - 3600,
         "issue": "snippet-stale"},
        {"file": THING, "snippet": None, "ts": NOW - 40 * DAY, "issue": "aged"},
    ]
    out = cf.annotate_staleness(rows, repo_root=str(repo), now=NOW, max_age_days=30)
    assert [r["stale_reason"] for r in out] == [None, "snippet-absent", "age"]
    assert [r["stale"] for r in out] == [False, True, True]
    assert all(a is not b for a, b in zip(rows, out))
    assert "stale" not in rows[0]


def test_annotate_staleness_flag_gates_only_the_head_check(tmp_path, monkeypatch):
    repo = _git_repo(tmp_path)
    monkeypatch.setenv("MO_FINDINGS_STALENESS", "0")
    rows = [
        {"file": THING, "snippet": "return sum(xs) * 2", "ts": NOW - 3600,
         "issue": "snippet-stale"},
        {"file": THING, "snippet": None, "ts": NOW - 40 * DAY, "issue": "aged"},
    ]
    out = {r["issue"]: r for r in cf.annotate_staleness(
        rows, repo_root=str(repo), now=NOW, max_age_days=30)}
    assert out["snippet-stale"]["stale"] is False
    assert out["aged"]["stale_reason"] == "age"


# ── scope_findings and the v2 pack ──────────────────────────────────────────

FRESH = "fresh row: total() drops the final element"
NO_SNIPPET = "no-snippet row: recent problem about totals"
AGED = "aged row: total() drops the final element"
SNIPPET_STALE = "stale row: cache clear happens before the write"


@pytest.fixture
def scoped(tmp_path):
    repo = _git_repo(tmp_path / "scoped")
    ident = cf.repo_identity(str(repo))
    db = tmp_path / "state.db"
    sqlite3.connect(str(db)).close()
    cf.ensure_schema(str(db))
    con = sqlite3.connect(str(db))
    for name, issue, snippet, ts in (
        ("r1", FRESH, "return sum(xs)", NOW - 3600),
        ("r1", NO_SNIPPET, None, NOW - 3600),
        ("r1", AGED, None, NOW - 40 * DAY),
        ("r1", SNIPPET_STALE, "return sum(xs) * 2", NOW - 3600),
    ):
        con.execute(
            "INSERT INTO code_findings (fingerprint, run_id, source, file, line,"
            " severity, category, issue, snippet, verdict, ts, target_repo)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (f"r1|{issue}", "r1", "review:critic", THING, 1, "high", "other",
             issue, snippet, "fail", ts, ident),
        )
    con.commit()
    con.close()
    return SimpleNamespace(repo=str(repo), ident=ident, db=str(db))


def _issues(scoped, **kwargs):
    return {r["issue"] for r in context_v2.scope_findings(
        [THING], db=scoped.db, **kwargs)}


def test_scope_findings_excludes_aged_and_stale_rows(scoped):
    assert _issues(scoped, target_repo=scoped.ident) == {FRESH, NO_SNIPPET}


def test_scope_findings_flag_returns_the_snippet_stale_row(scoped, monkeypatch):
    monkeypatch.setenv("MO_FINDINGS_STALENESS", "0")
    got = _issues(scoped, target_repo=scoped.ident)
    assert SNIPPET_STALE in got and AGED not in got


def test_scope_findings_with_stats_and_pack_counts(scoped, tmp_path):
    rows, stats = context_v2.scope_findings_with_stats(
        [THING], db=scoped.db, target_repo=scoped.ident)
    assert len(rows) == 2
    assert stats["n_findings_scanned"] == 4
    assert stats["n_findings_stale"] == 2
    assert stats["max_age_days"] == 30

    kickoff = tmp_path / "kickoff.md"
    kickoff.write_text("# Fixture\n\n## Files in scope\n\n- `%s`\n" % THING,
                       encoding="utf-8")
    pack = context_v2.build(str(kickoff), kickoff_text=kickoff.read_text(),
                            db=scoped.db, target_repo=scoped.ident)
    assert pack["n_findings_scanned"] == 4
    assert pack["n_findings_stale"] == 2
    assert pack["max_age_days"] == 30


def test_scope_findings_never_deletes_rows(scoped):
    context_v2.scope_findings([THING], db=scoped.db, target_repo=scoped.ident)
    con = sqlite3.connect(scoped.db)
    try:
        assert con.execute("SELECT COUNT(*) FROM code_findings").fetchone()[0] == 4
    finally:
        con.close()


# ── the "Your code" tab ────────────────────────────────────────────────────


@pytest.fixture
def tab_home(tmp_path, monkeypatch):
    repo = _git_repo(tmp_path / "tab")
    ident = cf.repo_identity(str(repo))
    home = tmp_path / "home"
    home.mkdir()
    db = home / "state.db"
    sqlite3.connect(str(db)).close()
    cf.ensure_schema(str(db))
    con = sqlite3.connect(str(db))
    for issue, snippet in ((FRESH, "return sum(xs)"), (SNIPPET_STALE,
                                                        "return sum(xs) * 2")):
        con.execute(
            "INSERT INTO code_findings (fingerprint, run_id, source, file, line,"
            " severity, category, issue, snippet, verdict, ts, target_repo)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (f"r1|{issue}", "r1", "review:critic", THING, 1, "high", "other",
             issue, snippet, "fail", NOW - 3600, ident),
        )
    con.commit()
    con.close()
    monkeypatch.setattr(code_tab, "engine_repo_identity", lambda root=None: ident)
    monkeypatch.setenv("MINI_ORK_ROOT", str(repo))
    return SimpleNamespace(home=home, db=str(db), repo=str(repo), ident=ident)


def test_tab_window_hides_stale_rows(tab_home):
    rows = code_tab._window_findings(tab_home.db, since_days=30,
                                     repo_root=tab_home.repo,
                                     target_repo=tab_home.ident)
    issues = [r["issue"] for r in rows]
    assert FRESH in issues and SNIPPET_STALE not in issues


def test_tab_shows_the_hidden_count_line(tab_home):
    errors: dict = {}
    out = code_tab.sections(tab_home.home, {"area": "src/lib/core", "days": "30"},
                            errors)
    assert not errors, errors
    dump = json.dumps(out, default=str)
    assert FRESH in dump
    assert SNIPPET_STALE not in dump
    assert dump.count("1 finding(s) hidden as stale at HEAD") == 1
    con = sqlite3.connect(tab_home.db)
    try:
        assert con.execute("SELECT COUNT(*) FROM code_findings").fetchone()[0] == 2
    finally:
        con.close()


# ── R5's residual: verifier process notes ──────────────────────────────────


def test_harvest_gates_a_verifier_process_note(tmp_path, monkeypatch):
    monkeypatch.setenv("MINI_ORK_ROOT", str(tmp_path))
    home = tmp_path / "home"
    note = "pushed to origin/main before the reviewer node ran"
    real = "mini_ork/widget.py computes the wrong total when the input is empty"
    for run_id, reason in (("procnote", note), ("realbug", real)):
        run_dir = home / "runs" / run_id
        run_dir.mkdir(parents=True)
        (run_dir / "verifier-static.json").write_text(
            json.dumps({"pass": False, "reasons": [reason]}), encoding="utf-8")
    db = tmp_path / "state.db"
    sqlite3.connect(str(db)).close()
    cf.harvest(str(home), db=str(db))
    con = sqlite3.connect(str(db))
    try:
        rows = con.execute("SELECT run_id FROM code_findings").fetchall()
    finally:
        con.close()
    assert rows == [("realbug",)]
