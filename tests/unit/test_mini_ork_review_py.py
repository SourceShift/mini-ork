"""Standalone native contracts for review persistence, policy, CLI, and forwarding.

The suite uses real temporary SQLite databases and Git repositories. It covers
syntax and secret findings, clean approval, read-only CLI formatting, the full
verdict matrix, newline-safe bug forwarding, and argument validation without a
Bash review oracle.
"""
from __future__ import annotations

import os
import shutil
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
from mini_ork import pre_push_review as py  # noqa: E402

PUBLIC_CLI = REPO / "bin" / "mini-ork-review"
INIT_SH = REPO / "db" / "init.sh"


# ─────────────────────────────────────────────────────────────────────────────
# Fixtures / helpers
# ─────────────────────────────────────────────────────────────────────────────
def _which_tools() -> None:
    for tool in ("bash", "sqlite3", "python3", "git"):
        if not shutil.which(tool):
            pytest.skip(f"{tool} not on PATH")
    if not PUBLIC_CLI.exists():
        pytest.skip(f"missing bin/mini-ork-review at {PUBLIC_CLI}")
    if not INIT_SH.exists():
        pytest.skip(f"missing db/init.sh at {INIT_SH}")


@pytest.fixture
def temp_db(tmp_path, monkeypatch):
    """Spin up a real mini-ork SQLite DB via db/init.sh.

    Mirrors ``test_bug_report_py.py::temp_db``: the Python port's
    ``_resolve_db()`` reads ``MINI_ORK_DB`` / ``MINI_ORK_HOME`` from the
    env; monkeypatch both so the Python port lands on the same DB the
    bash subprocess writes to.
    """
    _which_tools()
    home = tmp_path / "home"
    home.mkdir()
    dbp = str(home / "state.db")
    r = subprocess.run(
        ["bash", str(INIT_SH)],
        env={**os.environ, "MINI_ORK_HOME": str(home), "MINI_ORK_DB": dbp},
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        pytest.skip(f"db/init.sh failed: rc={r.returncode}\nstderr={r.stderr}")
    monkeypatch.setenv("MINI_ORK_DB", dbp)
    monkeypatch.setenv("MINI_ORK_HOME", str(home))
    return {"home": str(home), "db": dbp}


@pytest.fixture
def fake_repo(tmp_path):
    """Build a throwaway git repo with a ``main`` + ``feature`` branch.

    The repo starts empty; the caller writes files into ``repo.working``
    and calls ``commit()`` to get a sha. The returned sha is on the
    ``feature`` branch (so ``git merge-base <feature-sha> main`` works).
    """
    _which_tools()
    repo = tmp_path / "fake_repo"
    repo.mkdir()
    for cmd in (
        ["git", "init", "-q", "-b", "main", str(repo)],
        ["git", "-C", str(repo), "config", "user.email", "test@test.local"],
        ["git", "-C", str(repo), "config", "user.name", "Test User"],
        ["git", "-C", str(repo), "config", "commit.gpgsign", "false"],
        ["git", "-C", str(repo), "commit", "--allow-empty", "-q", "-m", "init"],
        ["git", "-C", str(repo), "checkout", "-q", "-b", "feature"],
    ):
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode != 0:
            pytest.skip(f"git setup failed: {cmd}\nstderr={r.stderr}")
    return repo


def _add_and_commit(repo: Path, files: dict[str, str], msg: str = "feature") -> str:
    """Write ``files`` into ``repo``, git-add, git-commit. Returns HEAD sha."""
    for path, content in files.items():
        full = repo / path
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_text(content)
        subprocess.run(["git", "-C", str(repo), "add", "--", path],
                       check=True, capture_output=True)
    r = subprocess.run(
        ["git", "-C", str(repo), "commit", "-q", "-m", msg],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        raise RuntimeError(f"git commit failed: {r.stderr}")
    sha = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    return sha


def _row_dicts(db: str, table: str) -> list[dict]:
    """Dump all rows of ``table`` as dicts. Ordered by the table's rowid."""
    con = sqlite3.connect(db)
    try:
        cols = [d[0] for d in con.execute(f"SELECT * FROM {table} LIMIT 0").description]
        rows = con.execute(f"SELECT {', '.join(cols)} FROM {table}").fetchall()
        return [dict(zip(cols, r)) for r in rows]
    finally:
        con.close()


def _insert_issue_rows(db: str, rid: int, issues: list[dict]) -> None:
    """Helper used by the policy-table test to stage issues without running the full orchestrator."""
    con = sqlite3.connect(db)
    try:
        con.execute("PRAGMA busy_timeout=5000")
        for d in issues:
            con.execute(
                """INSERT INTO pre_push_review_issues
                   (review_id, lens, severity, file_path, line_no,
                    title, description, suggested_fix, status)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'open')""",
                (rid,
                 d.get("lens", "?"),
                 d.get("severity", "medium"),
                 d.get("file", "?"),
                 d.get("line"),
                 (d.get("title") or "")[:300],
                 (d.get("description") or "")[:2000],
                 (d.get("suggested_fix") or "")[:1000]),
            )
        con.commit()
    finally:
        con.close()


# ─────────────────────────────────────────────────────────────────────────────
# (a) review_run with bash-syntax issue → verdict='block' parity
# ─────────────────────────────────────────────────────────────────────────────
def test_review_run_bash_syntax_issue_blocks_parity(temp_db, fake_repo):
    """A new Bash syntax error produces a critical issue and blocks main."""
    bad_sh = "#!/usr/bin/env bash\nif [ broken\n"  # `bash -n` will fail
    sha = _add_and_commit(fake_repo, {"lib/test_syntax.sh": bad_sh})
    py_rid = py.review_run(sha, "main", cwd=fake_repo, db=temp_db["db"])
    reviews = _row_dicts(temp_db["db"], "pre_push_reviews")
    py_review = next(row for row in reviews if row["id"] == py_rid)
    assert py_review["verdict"] == "block"
    issues = [row for row in _row_dicts(temp_db["db"], "pre_push_review_issues")
              if row["review_id"] == py_rid]
    assert any(row["lens"] == "heuristic.bash_syntax" and row["severity"] == "critical"
               for row in issues)


# ─────────────────────────────────────────────────────────────────────────────
# (b) review_run on a clean diff → verdict='approve' parity
# ─────────────────────────────────────────────────────────────────────────────
def test_review_run_clean_diff_approves_parity(temp_db, fake_repo):
    """A diff that adds a trivial non-bash, non-migration file (a text
    file in ``docs/``) must produce an ``approve`` verdict in BOTH
    ports, with zero issues."""
    sha = _add_and_commit(fake_repo, {
        "docs/example.md": "# hello\n\nA trivial doc-only diff.\n",
    })

    py_rid = py.review_run(
        sha, "main", cwd=fake_repo, db=temp_db["db"],
    )
    reviews = _row_dicts(temp_db["db"], "pre_push_reviews")
    py_review = next(r for r in reviews if r["id"] == py_rid)
    assert py_review["verdict"] == "approve"
    assert py_review["issues_open"] == 0


# ─────────────────────────────────────────────────────────────────────────────
# (c) check_secret_patterns vs bash for AKIA / ghp_ / sk- / xoxb- keys
# ─────────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("secret_line,kind", [
    ("+AWS_KEY=AKIAIOSFODNN7EXAMPLE", "AWS"),
    ("+GITHUB_TOKEN=ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghij", "GitHub"),
    ("+OPENAI_API_KEY=sk-abcdefghijklmnopqrstuvwx", "OpenAI"),
    ("+SLACK_TOKEN=xoxb-1234567890-9876543210-", "Slack"),
    ("+-----BEGIN RSA PRIVATE KEY-----", "private key"),
])
def test_check_secret_patterns_contract(secret_line: str, kind: str, tmp_path):
    """Each supported secret pattern emits one critical finding."""
    diff_text = (
        "diff --git a/lib/example.sh b/lib/example.sh\n"
        "new file mode 100755\n"
        "index 0000000..1111111\n"
        "--- /dev/null\n"
        "+++ b/lib/example.sh\n"
        "@@ -0,0 +1,2 @@\n"
        "+#!/usr/bin/env bash\n"
        f"{secret_line}\n"
    )
    diff_path = tmp_path / "diff.txt"
    diff_path.write_text(diff_text)

    py_issues = py.check_secret_patterns(diff_text)
    assert len(py_issues) == 1, (
        f"{kind}: py expected exactly 1 issue, got {len(py_issues)}: "
        f"{py_issues!r}"
    )
    issue = py_issues[0]
    assert issue["lens"] == "heuristic.secret_leak"
    assert issue["severity"] == "critical"
    assert issue["file"] == "lib/example.sh"
    # Title prefix per pattern: bash uses {name} from the pattern tuple;
    # the test parametrization uses a friendly kind label.
    assert issue["title"].startswith(f"Possible {kind}"), (
        f"{kind}: title prefix mismatch: {issue['title']!r}"
    )
    assert issue["suggested_fix"].startswith("Remove the secret")


def test_check_secret_patterns_returns_on_first_match():
    """The bash INTENDED semantics (post-bug-fix) is ``return on first
    match``. The Python port must mirror this — even if a single diff
    line matches multiple patterns, only one issue is emitted (first
    match in declaration order wins)."""
    diff_text = (
        "diff --git a/lib/example.sh b/lib/example.sh\n"
        "new file mode 100755\n"
        "+++ b/lib/example.sh\n"
        "@@ -0,0 +1,2 @@\n"
        "+#!/usr/bin/env bash\n"
        # This single line matches both the AWS and OpenAI patterns.
        "+AWS_KEY=AKIAIOSFODNN7EXAMPLE_OPENAI=sk-abcdefghijklmnopqrstuvwx\n"
    )
    issues = py.check_secret_patterns(diff_text)
    assert len(issues) == 1, (
        f"expected exactly 1 issue (first match wins), got {len(issues)}: {issues!r}"
    )
    assert issues[0]["lens"] == "heuristic.secret_leak"
    # First match wins — AWS comes before OpenAI in _SECRET_PATTERNS.
    assert "AWS" in issues[0]["title"], issues[0]["title"]


# ─────────────────────────────────────────────────────────────────────────────
# (d) review_verdict_for + review_show + review_list stdout format parity
# ─────────────────────────────────────────────────────────────────────────────
def test_readonly_stdout_and_public_cli(temp_db, fake_repo):
    """Read-only helpers and the public launcher expose the stored review."""
    bad_sh = "#!/usr/bin/env bash\nif [ broken\n"
    sha = _add_and_commit(fake_repo, {"lib/x.sh": bad_sh})
    py_rid = py.review_run(
        sha, "main", cwd=fake_repo, db=temp_db["db"],
    )
    assert py.review_verdict_for(py_rid, db=temp_db["db"]).strip() == "block"
    shown = py.review_show(py_rid, db=temp_db["db"])
    assert shown.count("\n") >= 3
    list_cli = subprocess.run(
        [str(PUBLIC_CLI), "list", "10"],
        env={**os.environ, "MINI_ORK_DB": temp_db["db"],
             "MINI_ORK_HOME": temp_db["home"]},
        capture_output=True, text=True,
    )
    assert list_cli.returncode == 0, list_cli.stderr
    list_py = py.review_list(10, db=temp_db["db"])
    assert list_cli.stdout == list_py
    assert str(py_rid) in list_py


# ─────────────────────────────────────────────────────────────────────────────
# (e) compute_verdict policy table — 6-row matrix
# ─────────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("seed_issues,target,expected_verdict,expected_rationale_substr", [
    # (1) zero issues → approve
    ([], "main", "approve", "crit=0 high=0 blocking=0"),
    # (2) one medium issue (not high/critical, total<=5) → approve
    ([{"lens": "heuristic.todo_marker", "severity": "medium",
       "file": "lib/x.sh", "title": "TODO", "description": "", "suggested_fix": ""}],
     "main", "approve", "crit=0 high=0"),
    # (3) one heuristic HIGH on a feature branch → warn (block-on-main only)
    ([{"lens": "heuristic.migration_safety", "severity": "high",
       "file": "db/migrations/x.sql", "title": "DROP", "description": "", "suggested_fix": ""}],
     "feature/foo", "warn", "blocking=1"),
    # (4) one heuristic HIGH on main → block
    ([{"lens": "heuristic.migration_safety", "severity": "high",
       "file": "db/migrations/x.sql", "title": "DROP", "description": "", "suggested_fix": ""}],
     "main", "block", "blocking=1"),
    # (5) one critical → block
    ([{"lens": "heuristic.bash_syntax", "severity": "critical",
       "file": "lib/x.sh", "title": "syntax", "description": "", "suggested_fix": ""}],
     "feature/foo", "block", "crit=1"),
    # (6) >5 low issues → warn (no high/critical, total > 5)
    ([{"lens": "heuristic.todo_marker", "severity": "low",
       "file": "lib/x.sh", "title": f"TODO{i}", "description": "", "suggested_fix": ""}
     for i in range(6)],
     "main", "warn", "total=6"),
])
def test_compute_verdict_policy_matrix(
    seed_issues, target, expected_verdict, expected_rationale_substr, temp_db,
):
    """Drive both compute_verdict functions with the same staged issue
    rows and assert identical (verdict, rationale)."""

    # Seed a pre_push_reviews row directly + insert issue rows.
    now = int(time.time())
    con = sqlite3.connect(temp_db["db"])
    try:
        con.execute("PRAGMA busy_timeout=5000")
        con.execute(
            """INSERT INTO pre_push_reviews
               (reviewed_at, source_sha, target_branch, reviewer_mode,
                files_changed, lines_added, lines_removed, verdict)
               VALUES (?, ?, ?, 'heuristic', 1, 1, 0, 'pending')""",
            (now, "abc123", target),
        )
        rid = int(con.execute("SELECT last_insert_rowid()").fetchone()[0])
        con.commit()
    finally:
        con.close()

    _insert_issue_rows(temp_db["db"], rid, seed_issues)

    # Python port
    py_v, py_r = py.compute_verdict(rid, target, db=temp_db["db"])

    # Bash port: replicate the exact SQL the bash heredoc runs.
    bash_script = f"""
    python3 - "{temp_db['db']}" "{rid}" "{target}" <<'PY'
import sqlite3, sys
db, rid, target = sys.argv[1:4]
con = sqlite3.connect(db); con.execute("PRAGMA busy_timeout=5000")
critical = con.execute(
    "SELECT COUNT(*) FROM pre_push_review_issues WHERE review_id=? AND severity='critical' AND status='open'",
    (rid,)).fetchone()[0]
high = con.execute(
    "SELECT COUNT(*) FROM pre_push_review_issues WHERE review_id=? AND severity='high' AND status='open'",
    (rid,)).fetchone()[0]
total = con.execute(
    "SELECT COUNT(*) FROM pre_push_review_issues WHERE review_id=? AND status='open'",
    (rid,)).fetchone()[0]
heuristic_high = con.execute(
    "SELECT COUNT(*) FROM pre_push_review_issues "
    "WHERE review_id=? AND severity='high' AND status='open' "
    "AND lens LIKE 'heuristic.%'",
    (rid,)).fetchone()[0]
consensus_high = con.execute(
    "SELECT COUNT(*) FROM ("
    "  SELECT file_path FROM pre_push_review_issues "
    "  WHERE review_id=? AND severity='high' AND status='open' "
    "    AND lens LIKE 'llm.%' AND file_path IS NOT NULL "
    "  GROUP BY file_path HAVING COUNT(DISTINCT lens) >= 2"
    ")",
    (rid,)).fetchone()[0]
blocking_high = heuristic_high + consensus_high
to_main = target in ("main","master")
if critical > 0:
    verdict = "block"
elif blocking_high > 0:
    verdict = "block" if to_main else "warn"
elif high > 0 or total > 5:
    verdict = "warn"
else:
    verdict = "approve"
print(verdict)
print(f"target={{target}} crit={{critical}} high={{high}} blocking={{blocking_high}} (heuristic={{heuristic_high}}, consensus={{consensus_high}}) total={{total}}")
PY
    """
    bash_r = subprocess.run(
        ["bash", "-c", bash_script],
        capture_output=True, text=True,
    )
    assert bash_r.returncode == 0, f"bash compute failed: {bash_r.stderr}"
    bash_lines = bash_r.stdout.splitlines()
    bash_v = bash_lines[0]
    bash_rationale = bash_lines[1]

    assert py_v == bash_v, (
        f"verdict mismatch (target={target}): bash={bash_v} py={py_v}"
    )
    assert py_r == bash_rationale, (
        f"rationale mismatch:\n  bash={bash_rationale}\n  py  ={py_r}"
    )
    assert py_v == expected_verdict
    assert expected_rationale_substr in py_r


# ─────────────────────────────────────────────────────────────────────────────
# (f) review_forward_to_bug_reports forward-count parity
# ─────────────────────────────────────────────────────────────────────────────
# The native forward uses parameterized SQLite rows, preserving descriptions
# with embedded newlines that the retired tab-separated shell loop corrupted.
def test_review_forward_to_bug_reports_parity(temp_db, fake_repo):
    """Forwarding preserves newline-bearing descriptions without shell parsing."""
    bad_sh = "#!/usr/bin/env bash\nif [ broken\n"
    sha = _add_and_commit(fake_repo, {"lib/y.sh": bad_sh})

    # Python review_run — same kind of row bash would have inserted
    # (we use Python exclusively for the forward parity check below
    # because bash is broken on this path).
    py_rid = py.review_run(
        sha, "main", cwd=fake_repo, db=temp_db["db"],
    )

    # Confirm the pre-condition: at least 2 open issues (bash_syntax +
    # test_pairing).
    pre = _row_dicts(temp_db["db"], "pre_push_review_issues")
    open_for_py = [r for r in pre if r["review_id"] == py_rid and r["status"] == "open"]
    assert len(open_for_py) >= 2, (
        f"expected >=2 open issues for py review, got {len(open_for_py)}"
    )

    # Python forward — succeeds and writes the expected rows.
    py_n = py.review_forward_to_bug_reports(
        py_rid, db=temp_db["db"], home=temp_db["home"],
    )
    assert py_n == len(open_for_py), (
        f"py forwarded {py_n} but {len(open_for_py)} open issues existed"
    )

    # bug_reports rows: the Python forward emits one bug_report_emit per
    # open issue; sweep dedupes by fingerprint, so we expect at least
    # ``py_n`` rows in bug_reports (more if the bash forward had partial
    # success on the empty-title issue, but for this test only the
    # python forward contributes).
    rows = _row_dicts(temp_db["db"], "bug_reports")
    review_rows = [r for r in rows
                   if (r.get("agent_role") or "").startswith("review.")]
    assert len(review_rows) == py_n, (
        f"expected {py_n} bug_reports rows from review.*, got {len(review_rows)}"
    )

    # Each row has the invariants the bug_reports schema enforces.
    for r in review_rows:
        assert (r.get("agent_role") or "").startswith("review.")
        assert len(r["fingerprint"]) == 64
        assert r["severity"] in {"low", "medium", "high", "critical"}
        # observed_in is the file path from the original review issue.
        assert r.get("observed_in") == "lib/y.sh"


# Sanity: forward on a review with zero open issues returns 0 cleanly.
def test_review_forward_zero_open_issues(temp_db, fake_repo):
    """Forwarding a review with no open issues is a no-op (returns 0)."""
    sha = _add_and_commit(fake_repo, {
        "docs/example.md": "# trivial doc-only diff\n",
    })
    rid = py.review_run(sha, "main", cwd=fake_repo, db=temp_db["db"])
    assert py.review_forward_to_bug_reports(
        rid, db=temp_db["db"], home=temp_db["home"],
    ) == 0


# ─────────────────────────────────────────────────────────────────────────────
# (g) review_run argument validation parity
# ─────────────────────────────────────────────────────────────────────────────
def test_review_run_missing_args_raises(temp_db, fake_repo):
    """Bash ``${1:?source_sha required}`` aborts with the parameter
    required message; Python port raises ``ValueError`` with the same
    phrase."""
    with pytest.raises(ValueError, match="source_sha required"):
        py.review_run("", "main", cwd=fake_repo, db=temp_db["db"])
    with pytest.raises(ValueError, match="target_branch required"):
        py.review_run("abc", "", cwd=fake_repo, db=temp_db["db"])
