"""Unit tests for the pre-push review finding lifecycle (rev6 R2b).

Hermetic: tmp git repos, tmp state DBs built from the repo's own migrations,
fake dispatch functions — no network, no LLM.  Every test asserts persisted
rows (findings, links, lens runs, verdicts), not just return values.
"""
from __future__ import annotations

import functools
import getpass
import hashlib
import json
import re
import shutil
import sqlite3
import subprocess
from pathlib import Path

import pytest

import mini_ork
import mini_ork.pre_push_review as ppr
from mini_ork.pre_push_review import review_run, review_verdict_for
from mini_ork.review import findings
from mini_ork.review import gate as gate_mod
from mini_ork.review.lenses import _REVIEW_PROMPT, _default_llm_panel
from mini_ork.review.verdict import _apply_verdict, compute_verdict
from mini_ork.stores.migrate import init_db, migrate_apply

REPO = Path(mini_ork.__file__).resolve().parents[1]
MIGRATIONS = str(REPO / "db" / "migrations")


# ── fixtures / helpers ─────────────────────────────────────────────────────


@pytest.fixture(scope="session")
def db_template(tmp_path_factory):
    d = tmp_path_factory.mktemp("dbtemplate")
    db = d / "state.db"
    rc, _out, err = init_db(db=str(db), root=str(REPO))
    assert rc == 0, f"init_db rc={rc}: {err}"
    con = sqlite3.connect(str(db))
    try:
        con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        con.close()
    return db


@pytest.fixture()
def db(tmp_path, db_template):
    p = tmp_path / "state.db"
    shutil.copyfile(db_template, p)
    return str(p)


@pytest.fixture()
def git_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "unit@example.com")
    _git(repo, "config", "user.name", "Unit Test")
    (repo / "a.py").write_text("x = 1\ny = 2\n")
    _git(repo, "add", "a.py")
    _git(repo, "commit", "-qm", "base", "--no-gpg-sign", "--no-verify")
    return repo


def _git(repo, *args):
    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True, text=True, check=False,
    )
    assert proc.returncode == 0, f"git {args} failed: {proc.stderr}"
    return proc.stdout.strip()


def git_commit(repo, path, content, msg):
    (repo / path).write_text(content)
    _git(repo, "add", path)
    _git(repo, "commit", "-qm", msg, "--no-gpg-sign", "--no-verify")
    return _git(repo, "rev-parse", "HEAD")


def rows(db, sql, params=()):
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in con.execute(sql, params).fetchall()]
    finally:
        con.close()


def one(db, sql, params=()):
    got = rows(db, sql, params)
    return got[0] if got else None


def seed_review(db, target="main", verdict="pending"):
    con = sqlite3.connect(db)
    try:
        cur = con.execute(
            "INSERT INTO pre_push_reviews (reviewed_at, source_sha, target_branch,"
            " reviewer_mode, files_changed, lines_added, lines_removed, verdict)"
            " VALUES (1, 'deadbeef', ?, 'hybrid', 1, 1, 0, ?)",
            (target, verdict),
        )
        con.commit()
        return int(cur.lastrowid)
    finally:
        con.close()


def seed_issue(db, rid, *, lens="llm.glm", severity="high", file="a.py",
               title="Seeded issue", status="open"):
    con = sqlite3.connect(db)
    try:
        cur = con.execute(
            "INSERT INTO pre_push_review_issues (review_id, lens, severity,"
            " file_path, line_no, title, description, suggested_fix, status)"
            " VALUES (?, ?, ?, ?, NULL, ?, '', '', ?)",
            (rid, lens, severity, file, title, status),
        )
        con.commit()
        return int(cur.lastrowid)
    finally:
        con.close()


def link(db, issue_id, review_id, issue, now=1000):
    con = sqlite3.connect(db)
    try:
        findings.ensure_schema(con)
        fp = findings.link_issue(con, issue_id, review_id, issue, now)
        con.commit()
        return fp
    finally:
        con.close()


def seed_lens_run(db, rid, lens, status, issues, elapsed_ms=1234):
    con = sqlite3.connect(db)
    try:
        findings.ensure_schema(con)
        con.execute(
            "INSERT INTO review_lens_runs (review_id, lens, status, issues,"
            " elapsed_ms, created_at) VALUES (?, ?, ?, ?, ?, 1)",
            (rid, lens, status, issues, elapsed_ms),
        )
        con.commit()
    finally:
        con.close()


def issue_dict(**over):
    d = {"lens": "llm.glm", "severity": "high", "file": "a.py",
         "title": "Cache is never invalidated", "description": "d",
         "suggested_fix": "f"}
    d.update(over)
    return d


def panel_returning(issues):
    def panel(_diff_text):
        return [dict(i) for i in issues]
    return panel


def make_dispatch(payload=None, rc=0, raise_exc=None):
    calls = []

    def dispatch(model, prompt, out_file, timeout, effort=4):
        calls.append({"model": model, "prompt": prompt})
        if raise_exc is not None:
            raise raise_exc
        if payload is not None:
            with open(out_file, "w") as fh:
                json.dump(payload, fh)
        return rc

    return dispatch, calls


def inject_default_panel(monkeypatch, dispatch):
    import mini_ork.review.lenses as lenses_mod

    real = lenses_mod._default_llm_panel
    bound = functools.partial(real, dispatch_fn=dispatch)
    monkeypatch.setattr(ppr, "_default_llm_panel", bound)
    monkeypatch.setattr(lenses_mod, "_default_llm_panel", bound)
    return bound


def cli_rc(fn, argv):
    try:
        return fn(argv)
    except SystemExit as exc:
        return int(exc.code or 0)


def run_review(repo, db, head, base, *, mode="hybrid", panel=None,
               target="feature/x"):
    return review_run(head, target, mode=mode, base=base, cwd=str(repo),
                      db=db, llm_panel=panel)


def parent_sha(repo):
    return _git(repo, "rev-parse", "HEAD~1")


# ── migration + schema ─────────────────────────────────────────────────────


def test_migration_file_exists_and_applies(db):
    assert (REPO / "db" / "migrations" / "0085_review_findings.sql").is_file()
    names = {r["name"] for r in rows(
        db, "SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"review_findings", "review_finding_links",
            "review_lens_runs"} <= names
    assert "0085_review_findings.sql" in {
        r["filename"] for r in rows(db, "SELECT filename FROM schema_migrations")}


def test_migration_applies_twice_around_ensure_schema(
        db_template, tmp_path, monkeypatch):
    copy = tmp_path / "copy.db"
    shutil.copyfile(db_template, copy)
    monkeypatch.setenv("MINI_ORK_DB", str(copy))
    assert migrate_apply(MIGRATIONS, db=str(copy), root=str(REPO))[0] == 0
    con = sqlite3.connect(str(copy))
    try:
        findings.ensure_schema(con)
    finally:
        con.close()
    assert migrate_apply(MIGRATIONS, db=str(copy), root=str(REPO))[0] == 0

    fresh = tmp_path / "fresh.db"
    con = sqlite3.connect(str(fresh))
    try:
        findings.ensure_schema(con)
    finally:
        con.close()
    assert migrate_apply(MIGRATIONS, db=str(fresh), root=str(REPO))[0] == 0


def test_ensure_schema_check_constraints():
    con = sqlite3.connect(":memory:")
    try:
        findings.ensure_schema(con)
        insert = ("INSERT INTO review_findings (fingerprint, lens_kind, title,"
                  " severity, status, first_review_id, last_review_id,"
                  " created_at, updated_at) VALUES (?, ?, 't', ?, ?, 1, 1, 0, 0)")
        con.execute(insert, ("fp00", "llm", "high", "open"))
        for fp, lk, sev, st in [("fp01", "ai", "high", "open"),
                                ("fp02", "llm", "bogus", "open"),
                                ("fp03", "llm", "high", "wontfix")]:
            with pytest.raises(sqlite3.IntegrityError):
                con.execute(insert, (fp, lk, sev, st))
        with pytest.raises(sqlite3.IntegrityError):
            con.execute("INSERT INTO review_lens_runs (review_id, lens, status,"
                        " issues, created_at) VALUES (1, 'l', 'crashed', 0, 0)")
    finally:
        con.close()


# ── fingerprint ────────────────────────────────────────────────────────────


def test_fingerprint_formulas_and_invariants():
    issue = {"lens": "heuristic.todo_marker", "file": "db/x.sql",
             "title": "DROP without IF EXISTS in migration"}
    assert findings.fingerprint(issue) == hashlib.sha1(
        b"heuristic.todo_marker|db/x.sql|drop without if exists in migration"
    ).hexdigest()[:16]

    plain = findings.fingerprint(
        {"lens": "llm.glm", "file": "a.py", "title": "sql injection"})
    noisy = findings.fingerprint(
        {"lens": "llm.glm", "file": "a.py", "title": "Sql Injection!"})
    assert plain == noisy
    assert findings.fingerprint(
        {"lens": "llm.deepseek_flash", "file": "a.py",
         "title": "sql injection"}) == plain
    assert findings.fingerprint(
        {"lens": "heuristic.secret_leak", "file": "a.py",
         "title": "sql injection"}) != plain
    assert findings.fingerprint(
        {"lens": "llm.glm", "file": "a.py", "title": "T",
         "same_as": "abcd1234abcd1234"}) == "abcd1234abcd1234"
    assert findings.fingerprint(
        {"lens": "llm.glm", "file": "a.py", "title": "T",
         "same_as": ""}) == findings.fingerprint(
        {"lens": "llm.glm", "file": "a.py", "title": "T"})


def test_prior_for_filters_orders_and_limits():
    con = sqlite3.connect(":memory:")
    try:
        findings.ensure_schema(con)

        def seed(fp, sev, status, file, upd, kind="llm"):
            con.execute(
                "INSERT INTO review_findings (fingerprint, lens_kind, file_path,"
                " title, severity, status, first_review_id, last_review_id,"
                " created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, 1, 1, 0, ?)",
                (fp, kind, file, f"title-{fp}", sev, status, upd))

        seed("aaa", "low", "open", "a.py", 100)
        seed("bbb", "critical", "open", "a.py", 50)
        seed("ccc", "high", "open", "a.py", 200)
        seed("ddd", "high", "open", "a.py", 300)
        seed("eee", "medium", "fixed", "a.py", 400)
        seed("fff", "critical", "open", "b.py", 900)
        seed("ggg", "critical", "dismissed", "a.py", 500)
        seed("hhh", "medium", "open", "a.py", 90)
        con.commit()
        got = findings.prior_for(con, ["a.py"])
        assert [g["fingerprint"] for g in got] == \
            ["ggg", "bbb", "ddd", "ccc", "hhh", "aaa"]
        assert set(got[0]) == {"fingerprint", "status", "severity",
                               "file_path", "title", "lens_kind"}
        assert [g["fingerprint"] for g in findings.prior_for(con, ["a.py"], limit=2)] \
            == ["ggg", "bbb"]
        assert findings.prior_for(con, []) == []
    finally:
        con.close()


def test_link_issue_transitions():
    con = sqlite3.connect(":memory:")
    try:
        findings.ensure_schema(con)
        issue = {"lens": "llm.glm", "severity": "high", "file": "a.py",
                 "title": "Cache bug"}
        fp = findings.link_issue(con, 11, 7, issue, 1000)
        assert fp == findings.fingerprint(issue)
        row = con.execute(
            "SELECT lens_kind, status, first_review_id, last_review_id,"
            " last_issue_id, times_seen, reopened FROM review_findings"
            " WHERE fingerprint=?", (fp,)).fetchone()
        assert row == ("llm", "open", 7, 7, 11, 1, 0)
        assert con.execute("SELECT issue_id, fingerprint, review_id,"
                           " pre_existing FROM review_finding_links").fetchone() \
            == (11, fp, 7, 0)

        # re-reported: counter update, severity follows the observation
        findings.link_issue(con, 12, 8, dict(issue, severity="critical"), 2000)
        assert con.execute(
            "SELECT severity, last_review_id, last_issue_id, times_seen,"
            " reopened, updated_at FROM review_findings WHERE fingerprint=?",
            (fp,)).fetchone() == ("critical", 8, 12, 2, 0, 2000)

        # fixed then reported again: reopened
        con.execute("UPDATE review_findings SET status='fixed' WHERE fingerprint=?",
                    (fp,))
        findings.link_issue(con, 13, 9, dict(issue), 3000)
        assert con.execute(
            "SELECT status, reopened, times_seen FROM review_findings"
            " WHERE fingerprint=?", (fp,)).fetchone() == ("open", 1, 3)

        # dismissed is sticky
        con.execute("UPDATE review_findings SET status='dismissed' WHERE fingerprint=?",
                    (fp,))
        findings.link_issue(con, 14, 10, dict(issue), 4000)
        assert con.execute(
            "SELECT status, times_seen FROM review_findings"
            " WHERE fingerprint=?", (fp,)).fetchone() == ("dismissed", 4)
        con.commit()
    finally:
        con.close()


def test_link_issue_pre_existing_and_same_as_fallback():
    con = sqlite3.connect(":memory:")
    try:
        findings.ensure_schema(con)
        fp = findings.link_issue(
            con, 12, 7,
            {"lens": "llm.glm", "severity": "critical", "file": "c.py",
             "title": "Old bug", "pre_existing": True}, 1000)
        assert con.execute("SELECT status FROM review_findings"
                           " WHERE fingerprint=?", (fp,)).fetchone()[0] \
            == "pre_existing"
        assert con.execute("SELECT pre_existing FROM review_finding_links"
                           " WHERE issue_id=12").fetchone()[0] == 1

        known = findings.link_issue(
            con, 13, 8,
            {"lens": "llm.glm", "severity": "high", "file": "a.py",
             "title": "Cache bug"}, 1001)
        assert findings.link_issue(
            con, 14, 8,
            {"lens": "llm.glm", "severity": "high", "file": "a.py",
             "title": "Totally different", "same_as": known}, 1002) == known
        unknown = findings.link_issue(
            con, 15, 8,
            {"lens": "llm.glm", "severity": "high", "file": "a.py",
             "title": "Fresh bug", "same_as": "zzzz0000zzzz0000"}, 1003)
        assert unknown == findings.fingerprint(
            {"lens": "llm.glm", "file": "a.py", "title": "Fresh bug"})
        con.commit()
    finally:
        con.close()


def test_settle_fixes_heuristic_silence_and_resolved():
    con = sqlite3.connect(":memory:")
    try:
        findings.ensure_schema(con)
        prior = [
            {"fingerprint": "heur000000000001", "status": "open",
             "severity": "low", "file_path": "a.py", "title": "t",
             "lens_kind": "heuristic"},
            {"fingerprint": "llm0000000000001", "status": "open",
             "severity": "high", "file_path": "a.py", "title": "t",
             "lens_kind": "llm"},
            {"fingerprint": "llm0000000000002", "status": "open",
             "severity": "high", "file_path": "a.py", "title": "t",
             "lens_kind": "llm"},
            {"fingerprint": "heur000000000003", "status": "dismissed",
             "severity": "low", "file_path": "a.py", "title": "t",
             "lens_kind": "heuristic"},
        ]
        for p in prior:
            con.execute(
                "INSERT INTO review_findings (fingerprint, lens_kind, file_path,"
                " title, severity, status, first_review_id, last_review_id,"
                " created_at, updated_at) VALUES (?, ?, ?, 't', ?, ?, 1, 1, 0, 1)",
                (p["fingerprint"], p["lens_kind"], p["file_path"],
                 p["severity"], p["status"]))
        con.commit()
        fixed = findings.settle(con, 5, prior, {"llm0000000000001"},
                                {"llm0000000000002"}, 999)
        assert sorted(fixed) == ["heur000000000001", "llm0000000000002"]
        got = {r[0]: r for r in con.execute(
            "SELECT fingerprint, status, resolved_by, resolved_at"
            " FROM review_findings").fetchall()}
        assert got["heur000000000001"] == \
            ("heur000000000001", "fixed", "review:5", 999)
        assert got["llm0000000000002"] == \
            ("llm0000000000002", "fixed", "review:5", 999)
        assert got["llm0000000000001"][1] == "open"
        assert got["heur000000000003"][1] == "dismissed"
    finally:
        con.close()


# ── resolve / list / CLI ───────────────────────────────────────────────────


def test_resolve_validation_and_persistence(db):
    fp = link(db, 601, 9, issue_dict())
    for status, reason in (("dismissed", ""), ("pre_existing", ""),
                           ("open", "r"), ("not_a_status", "r")):
        with pytest.raises(ValueError):
            findings.resolve(db, fp, status, reason=reason, by="human:t")
    with pytest.raises(ValueError):
        findings.resolve(db, "0123456789abcdef", "dismissed",
                         reason="r", by="human:t")
    assert one(db, "SELECT status, resolved_by FROM review_findings"
                   " WHERE fingerprint=?", (fp,)) == \
        {"status": "open", "resolved_by": None}

    findings.resolve(db, fp, "fixed", reason="", by="human:spec", now=777)
    assert one(db, "SELECT status, reason, resolved_by, resolved_at, updated_at"
                   " FROM review_findings WHERE fingerprint=?", (fp,)) == {
        "status": "fixed", "reason": "", "resolved_by": "human:spec",
        "resolved_at": 777, "updated_at": 777}


def test_list_findings_filters(db):
    fp_a = link(db, 601, 9, issue_dict())
    fp_c = link(db, 602, 9, issue_dict(severity="critical", file="c.py",
                                       title="Old bug", pre_existing=True))
    assert [d["fingerprint"] for d in findings.list_findings(db)] == [fp_a]
    assert findings.list_findings(db, status="dismissed") == []
    assert [d["fingerprint"] for d in
            findings.list_findings(db, status="pre_existing")] == [fp_c]
    assert [d["fingerprint"] for d in
            findings.list_findings(db, status="pre_existing",
                                   file="c.py")] == [fp_c]
    assert findings.list_findings(db, limit=0) == []


def test_cli_findings_lines_json_and_resolve_exit_codes(db, monkeypatch, capsys):
    monkeypatch.setenv("MINI_ORK_DB", db)
    fp = link(db, 601, 9, issue_dict())
    link(db, 602, 10, issue_dict())  # same fingerprint → seen 2×
    assert ppr.main(["findings"]) == 0
    out = capsys.readouterr().out
    expected = (f"{fp}  open  high  a.py  Cache is never invalidated"
                "  (seen 2×)")
    assert any(line.strip() == expected for line in out.splitlines()), out
    capsys.readouterr()
    assert ppr.main(["findings", "--status", "all", "--json"]) == 0
    assert fp in capsys.readouterr().out

    assert cli_rc(ppr.main, ["resolve", fp, "--as", "dismissed"]) == 1
    assert capsys.readouterr().err.strip()
    assert cli_rc(ppr.main, ["findings", "--status", "nope"]) == 2
    assert ppr.main(["resolve", fp, "--as", "dismissed", "--reason",
                     "not a blocker"]) == 0
    row = one(db, "SELECT status, reason, resolved_by FROM review_findings"
                  " WHERE fingerprint=?", (fp,))
    assert row["status"] == "dismissed"
    assert row["reason"] == "not a blocker"
    assert row["resolved_by"] == "human:" + getpass.getuser()


def test_usage_mentions_findings_and_resolve(capsys, db, monkeypatch):
    monkeypatch.setenv("MINI_ORK_DB", db)
    assert ppr.main([]) == 0
    out = capsys.readouterr().out
    assert "findings" in out and "resolve" in out


# ── prompt / panel ─────────────────────────────────────────────────────────


def test_build_review_prompt_empty_and_with_prior():
    from mini_ork.review.lenses import build_review_prompt

    marker = "--- DIFF FOLLOWS ---"
    assert _REVIEW_PROMPT.count(marker) == 1
    empty = build_review_prompt("SOME DIFF", [])
    assert empty.count(marker) == 1
    assert "pre_existing" in empty
    assert "For each OPEN earlier finding" not in empty
    assert empty.startswith(_REVIEW_PROMPT[: _REVIEW_PROMPT.index(marker)])
    assert empty.endswith(marker + "\n")

    prior = [{"fingerprint": "abcd1234abcd1234", "status": "open",
              "severity": "high", "file_path": "mini_ork/x.py",
              "title": "Cache bug", "lens_kind": "llm"}]
    with_prior = build_review_prompt("SOME DIFF", prior)
    assert re.search(r"^- \[abcd1234abcd1234\] (?:OPEN|open) high "
                     r"mini_ork/x\.py — Cache bug$", with_prior, re.M)
    instr = ('For each OPEN earlier finding: if this diff fixes it, put its '
             'id in "resolved"; if it is still present, report it again '
             'with "same_as": "<id>".')
    assert instr in with_prior
    assert "Never report a DISMISSED or PRE_EXISTING finding again." in with_prior
    assert with_prior.index(instr) < with_prior.index(marker)


def test_panel_no_prior_prompt_is_canonical(monkeypatch):
    monkeypatch.setenv("MO_REVIEW_PANEL", "glm")
    dispatch, calls = make_dispatch(payload={"issues": []})
    assert _default_llm_panel("d", dispatch_fn=dispatch) == []
    assert calls[0]["prompt"] == _REVIEW_PROMPT + "d"


def test_panel_prior_same_as_resolved_and_lens_sink(monkeypatch):
    monkeypatch.setenv("MO_REVIEW_PANEL", "glm")
    prior_fp = "abcd1234abcd1234"
    prior = [{"fingerprint": prior_fp, "status": "open", "severity": "high",
              "file_path": "a.py", "title": "Cache bug", "lens_kind": "llm"}]
    payload = {"issues": [
        {"severity": "high", "file": "a.py", "title": "Same",
         "same_as": prior_fp},
        {"severity": "low", "file": "a.py", "title": "Other",
         "same_as": "ffffffffffffffff"},
        {"severity": "low", "file": "b.py", "title": "Old", "pre_existing": 1},
    ], "resolved": [prior_fp, "notapriorfp00000"]}
    dispatch, calls = make_dispatch(payload=payload)
    resolved: list = []
    lens_rows: list = []
    out = _default_llm_panel("d", dispatch_fn=dispatch, prior=prior,
                             resolved_sink=resolved, lens_sink=lens_rows)
    by_title = {i["title"]: i for i in out}
    assert by_title["Same"]["same_as"] == prior_fp
    assert by_title["Other"]["same_as"] is None
    assert by_title["Old"]["pre_existing"] is True
    assert resolved == [prior_fp]
    assert lens_rows[0]["status"] == "ok" and lens_rows[0]["issues"] == 3


def test_panel_lens_statuses(monkeypatch):
    monkeypatch.setenv("MO_REVIEW_PANEL", "glm")
    dispatch, _ = make_dispatch(raise_exc=RuntimeError("down"))
    sink: list = []
    _default_llm_panel("d", dispatch_fn=dispatch, lens_sink=sink)
    assert sink[0]["status"] == "dispatch_failed"

    monkeypatch.setenv("MO_REVIEW_PANEL", "glm deepseek_flash")

    def dispatch2(model, prompt, out_file, timeout, effort=4):
        if model == "glm":
            with open(out_file, "w") as fh:
                json.dump({"issues": [{"severity": "low", "file": "a.py",
                                       "title": "A"}]}, fh)
            return 0
        return 1

    sink2: list = []
    _default_llm_panel("d", dispatch_fn=dispatch2, lens_sink=sink2)
    assert [(e["lens"], e["status"], e["issues"]) for e in sink2] == [
        ("llm.glm", "ok", 1), ("llm.deepseek_flash", "dispatch_failed", 0)]
    assert isinstance(sink2[0]["elapsed_ms"], int)


# ── review_run lifecycle ───────────────────────────────────────────────────


def test_review1_creates_open_finding_and_link(db, git_repo):
    c1 = git_commit(git_repo, "a.py", "x = 1\ny = 2\nz = 3\n", "one")
    c0 = parent_sha(git_repo)
    rid = run_review(git_repo, db, c1, c0, panel=panel_returning([issue_dict()]))
    finding = one(db, "SELECT * FROM review_findings")
    assert finding["status"] == "open"
    assert finding["lens_kind"] == "llm"
    assert finding["file_path"] == "a.py"
    assert finding["times_seen"] == 1
    assert finding["first_review_id"] == rid
    links = rows(db, "SELECT * FROM review_finding_links")
    assert len(links) == 1 and links[0]["review_id"] == rid
    assert links[0]["fingerprint"] == finding["fingerprint"]
    assert review_verdict_for(rid, db=db).strip() == "warn"


def test_resolved_in_prior_marks_fixed(db, git_repo, monkeypatch):
    state = {"payload": {"issues": [
        {"severity": "high", "file": "a.py",
         "title": "cache is never invalidated", "description": "",
         "suggested_fix": ""}], "resolved": []}}

    def dispatch(model, prompt, out_file, timeout, effort=4):
        with open(out_file, "w") as fh:
            json.dump(state["payload"], fh)
        return 0

    inject_default_panel(monkeypatch, dispatch)
    monkeypatch.setenv("MO_REVIEW_PANEL", "glm")
    c1 = git_commit(git_repo, "a.py", "x = 1\ny = 2\nz = 3\n", "one")
    c0 = parent_sha(git_repo)
    run_review(git_repo, db, c1, c0)
    fp = findings.fingerprint({"lens": "llm.glm", "file": "a.py",
                               "title": "cache is never invalidated"})
    assert one(db, "SELECT status FROM review_findings WHERE fingerprint=?",
               (fp,))["status"] == "open"

    state["payload"] = {"issues": [], "resolved": [fp]}
    c2 = git_commit(git_repo, "a.py", "x = 1\ny = 2\nz = 4\n", "two")
    rid2 = run_review(git_repo, db, c2, c1)
    row = one(db, "SELECT status, resolved_by FROM review_findings"
                  " WHERE fingerprint=?", (fp,))
    assert row["status"] == "fixed"
    assert row["resolved_by"] == f"review:{rid2}"


def test_same_as_rereport_stays_open(db, git_repo):
    c1 = git_commit(git_repo, "a.py", "x = 1\ny = 2\nz = 3\n", "one")
    c0 = parent_sha(git_repo)
    run_review(git_repo, db, c1, c0, panel=panel_returning([issue_dict()]))
    fp = findings.fingerprint(issue_dict())
    c2 = git_commit(git_repo, "a.py", "x = 1\ny = 2\nz = 4\n", "two")
    again = issue_dict(title="Still here", same_as=fp)
    rid2 = run_review(git_repo, db, c2, c1, panel=panel_returning([again]))
    assert one(db, "SELECT status, times_seen FROM review_findings"
                   " WHERE fingerprint=?", (fp,)) == \
        {"status": "open", "times_seen": 2}
    assert one(db, "SELECT COUNT(*) AS n FROM review_finding_links"
                   " WHERE review_id=?", (rid2,))["n"] == 1


def test_fixed_reported_again_reopens(db, git_repo):
    c1 = git_commit(git_repo, "a.py", "x = 1\ny = 2\nz = 3\n", "one")
    c0 = parent_sha(git_repo)
    run_review(git_repo, db, c1, c0, panel=panel_returning([issue_dict()]))
    fp = findings.fingerprint(issue_dict())
    findings.resolve(db, fp, "fixed", reason="", by="human:spec")
    c2 = git_commit(git_repo, "a.py", "x = 1\ny = 2\nz = 4\n", "two")
    run_review(git_repo, db, c2, c1,
               panel=panel_returning([issue_dict(same_as=fp)]))
    assert one(db, "SELECT status, reopened, times_seen FROM review_findings"
                   " WHERE fingerprint=?", (fp,)) == \
        {"status": "open", "reopened": 1, "times_seen": 2}


def test_heuristic_absence_fixes_without_resolved(db, git_repo):
    c1 = git_commit(git_repo, "a.py", "x = 1\ny = 2\nTODO: fix later\n", "one")
    c0 = parent_sha(git_repo)
    run_review(git_repo, db, c1, c0, mode="heuristic")
    finding = one(db, "SELECT * FROM review_findings")
    assert finding["lens_kind"] == "heuristic" and finding["status"] == "open"
    c2 = git_commit(git_repo, "a.py", "x = 1\ny = 2\ndone\n", "two")
    rid2 = run_review(git_repo, db, c2, c1, mode="heuristic")
    row = one(db, "SELECT status, resolved_by FROM review_findings"
                  " WHERE fingerprint=?", (finding["fingerprint"],))
    assert row["status"] == "fixed"
    assert row["resolved_by"] == f"review:{rid2}"


def test_pre_existing_issue_not_counted(db, git_repo):
    c1 = git_commit(git_repo, "a.py", "x = 1\ny = 2\nz = 3\n", "one")
    c0 = parent_sha(git_repo)
    rid = run_review(
        git_repo, db, c1, c0, target="main",
        panel=panel_returning([issue_dict(severity="critical",
                                          pre_existing=True)]))
    assert one(db, "SELECT status FROM review_findings")["status"] \
        == "pre_existing"
    assert one(db, "SELECT pre_existing FROM review_finding_links") \
        ["pre_existing"] == 1
    assert review_verdict_for(rid, db=db).strip() == "approve"
    review_row = one(db, "SELECT issues_open, issues_critical FROM"
                         " pre_push_reviews WHERE id=?", (rid,))
    assert review_row == {"issues_open": 0, "issues_critical": 0}


def test_prior_scoped_to_push_files(db, git_repo):
    link(db, 900, 899, {"lens": "heuristic.todo_marker", "severity": "low",
                        "file": "b.py", "title": "Old b.py bug"})
    c1 = git_commit(git_repo, "a.py", "x = 1\ny = 2\nz = 3\n", "one")
    c0 = parent_sha(git_repo)
    run_review(git_repo, db, c1, c0, panel=panel_returning([]))
    assert one(db, "SELECT status FROM review_findings"
                   " WHERE file_path='b.py'")["status"] == "open"


def test_dismiss_unblocks_verdict(db, git_repo):
    c1 = git_commit(git_repo, "a.py", "x = 1\ny = 2\nz = 3\n", "one")
    c0 = parent_sha(git_repo)
    rid = run_review(git_repo, db, c1, c0, target="main",
                     panel=panel_returning([issue_dict(severity="critical")]))
    assert review_verdict_for(rid, db=db).strip() == "block"
    fp = one(db, "SELECT fingerprint FROM review_findings")["fingerprint"]
    with pytest.raises(ValueError):
        findings.resolve(db, fp, "dismissed", reason="", by="human:spec")
    findings.resolve(db, fp, "dismissed", reason="predates this push",
                     by="human:spec")
    _apply_verdict(rid, "main", db)
    assert review_verdict_for(rid, db=db).strip() == "approve"


@pytest.mark.parametrize("broken", ["ensure_schema", "prior_for",
                                    "link_issue", "settle"])
def test_lifecycle_failure_leaves_review_unchanged(db, git_repo, monkeypatch,
                                                   broken):
    def boom(*_a, **_k):
        raise RuntimeError("injected lifecycle failure")

    monkeypatch.setattr(findings, broken, boom, raising=False)
    c1 = git_commit(git_repo, "a.py", "x = 1\ny = 2\nz = 3\n", "one")
    c0 = parent_sha(git_repo)
    rid = run_review(git_repo, db, c1, c0, panel=panel_returning([issue_dict()]))
    review_row = one(db, "SELECT verdict, issues_open FROM pre_push_reviews"
                         " WHERE id=?", (rid,))
    assert review_row == {"verdict": "warn", "issues_open": 1}


def test_lens_runs_recorded(db, git_repo, monkeypatch):
    def dispatch(model, prompt, out_file, timeout, effort=4):
        if model == "glm":
            with open(out_file, "w") as fh:
                json.dump({"issues": [
                    {"severity": "low", "file": "a.py", "title": "T",
                     "description": "", "suggested_fix": ""}]}, fh)
            return 0
        return 1

    inject_default_panel(monkeypatch, dispatch)
    monkeypatch.setenv("MO_REVIEW_PANEL", "glm deepseek_flash")
    c1 = git_commit(git_repo, "a.py", "x = 1\ny = 2\nz = 3\n", "one")
    c0 = parent_sha(git_repo)
    rid = run_review(git_repo, db, c1, c0)
    got = rows(db, "SELECT lens, status, issues FROM review_lens_runs"
                   " WHERE review_id=? ORDER BY id", (rid,))
    assert [(g["lens"], g["status"], g["issues"]) for g in got] == [
        ("llm.glm", "ok", 1), ("llm.deepseek_flash", "dispatch_failed", 0)]


# ── verdict exclusion ──────────────────────────────────────────────────────


def test_verdict_excludes_dismissed_and_pre_existing(db):
    rid = seed_review(db)
    iid = seed_issue(db, rid, severity="critical", title="Crit")
    fp = link(db, iid, rid, issue_dict(severity="critical"))
    verdict, rationale = compute_verdict(rid, "main", db=db)
    assert verdict == "block"
    assert re.match(r"^target=main crit=\d+ high=\d+ blocking=\d+ "
                    r"\(heuristic=\d+, consensus=\d+\) total=\d+$", rationale)
    findings.resolve(db, fp, "dismissed", reason="not ours", by="human:spec")
    assert compute_verdict(rid, "main", db=db)[0] == "approve"

    rid2 = seed_review(db)
    iid2 = seed_issue(db, rid2, severity="critical", title="Old")
    link(db, iid2, rid2, issue_dict(severity="critical", pre_existing=True))
    assert compute_verdict(rid2, "main", db=db)[0] == "approve"

    rid3 = seed_review(db)
    iid3 = seed_issue(db, rid3, severity="critical", title="Flagged")
    link(db, iid3, rid3, issue_dict(severity="critical"))
    con = sqlite3.connect(db)
    con.execute("UPDATE review_finding_links SET pre_existing=1 WHERE issue_id=?",
                (iid3,))
    con.commit()
    con.close()
    assert compute_verdict(rid3, "main", db=db)[0] == "approve"


def test_apply_verdict_counts_exclude(db):
    rid = seed_review(db)
    crit = seed_issue(db, rid, severity="critical", title="Dismissed crit")
    seed_issue(db, rid, severity="low", title="Kept low")
    fp = link(db, crit, rid, issue_dict(severity="critical"))
    findings.resolve(db, fp, "dismissed", reason="r", by="human:spec")
    _apply_verdict(rid, "main", db)
    assert one(db, "SELECT verdict, issues_open, issues_critical FROM"
                   " pre_push_reviews WHERE id=?", (rid,)) == \
        {"verdict": "approve", "issues_open": 1, "issues_critical": 0}


def test_verdict_unchanged_when_tables_missing(db, tmp_path):
    rid = seed_review(db)
    iid = seed_issue(db, rid, severity="critical", title="Crit")
    fp = link(db, iid, rid, issue_dict(severity="critical"))
    findings.resolve(db, fp, "dismissed", reason="r", by="human:spec")
    assert compute_verdict(rid, "main", db=db)[0] == "approve"

    both = str(tmp_path / "both.db")
    shutil.copyfile(db, both)
    con = sqlite3.connect(both)
    for stmt in ("DROP TABLE review_finding_links", "DROP TABLE review_findings"):
        con.execute(stmt)
    con.commit()
    con.close()
    verdict, rationale = compute_verdict(rid, "main", db=both)
    assert verdict == "block" and "crit=1" in rationale


# ── gate ───────────────────────────────────────────────────────────────────


def _gate_env(db, home):
    return {"MINI_ORK_DB": str(db), "MINI_ORK_HOME": str(home)}


def test_gate_cache_hit_recomputes_after_dismissal(db, git_repo, tmp_path):
    rid = seed_review(db)
    iid = seed_issue(db, rid, severity="critical", title="Crit")
    fp = link(db, iid, rid, issue_dict(severity="critical"))
    _apply_verdict(rid, "main", db)
    home = tmp_path / "home"
    home.mkdir()
    c1 = git_commit(git_repo, "a.py", "x = 1\ny = 2\nz = 3\n", "one")
    c0 = parent_sha(git_repo)

    def fake_review(head, target, **kw):
        return rid

    r1 = gate_mod.run_gate(target="main", cwd=str(git_repo),
                           env=_gate_env(db, home), head=c1, base=c0,
                           review_fn=fake_review, now=1000)
    assert r1.rc == 1 and r1.verdict == "block"
    findings.resolve(db, fp, "dismissed", reason="not a blocker",
                     by="human:spec")
    assert review_verdict_for(rid, db=db).strip() == "block"
    r2 = gate_mod.run_gate(target="main", cwd=str(git_repo),
                           env=_gate_env(db, home), head=c1, base=c0,
                           review_fn=fake_review, now=1100)
    assert r2.cached is True and r2.verdict == "approve" and r2.rc == 0


def test_gate_block_prints_hint_and_finding_lines(db, git_repo, tmp_path,
                                                  capsys):
    rid = seed_review(db)
    crit = seed_issue(db, rid, severity="critical", file="a.py",
                      title="Crit title")
    high = seed_issue(db, rid, severity="high", file="b.py", title="High title")
    low = seed_issue(db, rid, severity="low", file="c.py", title="Low title")
    seed_issue(db, rid, severity="high", file="d.py", title="Unlinked title")
    fp_crit = link(db, crit, rid, issue_dict(severity="critical", file="a.py",
                                             title="Crit title"))
    fp_high = link(db, high, rid, issue_dict(severity="high", file="b.py",
                                             title="High title"))
    link(db, low, rid, issue_dict(severity="low", file="c.py",
                                  title="Low title"))
    _apply_verdict(rid, "main", db)
    home = tmp_path / "home"
    home.mkdir()
    c1 = git_commit(git_repo, "a.py", "x = 1\ny = 2\nz = 3\n", "one")
    c0 = parent_sha(git_repo)
    result = gate_mod.run_gate(target="main", cwd=str(git_repo),
                               env=_gate_env(db, home), head=c1, base=c0,
                               review_fn=lambda head, target, **kw: rid,
                               now=1000)
    assert result.rc == 1
    out = capsys.readouterr().out
    assert ("pre-push review: blocking findings — dismiss one with: "
            "mini-ork review resolve <fingerprint> --as dismissed "
            '--reason "<why>"') in out
    fp_lines = [ln for ln in out.splitlines()
                if re.search(r"[0-9a-f]{16}\s+(?:critical|high)\s+\S+", ln)]
    assert len(fp_lines) == 2
    joined = "\n".join(fp_lines)
    assert fp_crit in joined and "Crit title" in joined
    assert fp_high in joined and "High title" in joined
    assert "Low title" not in joined and "Unlinked title" not in joined


def test_gate_prints_lens_lines_after_fresh_review(db, git_repo, tmp_path,
                                                   capsys):
    rid = seed_review(db)
    _apply_verdict(rid, "main", db)
    seed_lens_run(db, rid, "llm.glm", "dispatch_failed", 0, elapsed_ms=1234)
    seed_lens_run(db, rid, "llm.deepseek_flash", "ok", 2, elapsed_ms=41000)
    home = tmp_path / "home"
    home.mkdir()
    c1 = git_commit(git_repo, "a.py", "x = 1\ny = 2\nz = 3\n", "one")
    c0 = parent_sha(git_repo)
    result = gate_mod.run_gate(target="main", cwd=str(git_repo),
                               env=_gate_env(db, home), head=c1, base=c0,
                               review_fn=lambda head, target, **kw: rid,
                               now=1000)
    assert result.rc == 0
    out = capsys.readouterr().out
    glm = [ln for ln in out.splitlines() if "lens llm.glm dispatch_failed" in ln]
    dsf = [ln for ln in out.splitlines()
           if "lens llm.deepseek_flash ok" in ln]
    assert glm and "0 issues" in glm[0]
    assert dsf and "2 issues" in dsf[0]
