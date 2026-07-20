"""Parity gate: mini_ork.pre_push_review vs lib/pre_push_review.sh.

The LLM panel needs mo_llm_dispatch + live models (integration); here we parity
the deterministic surface: each heuristic check vs the bash's embedded python on
a crafted diff, and review_run end-to-end (diff → issues → SQL verdict) vs the
LIVE bash on separate state DBs over the same git repo.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
from mini_ork import pre_push_review as ppr  # noqa: E402

LIB = REPO / "lib" / "pre_push_review.sh"

_DIFF = """diff --git a/db/migrations/0099_x.sql b/db/migrations/0099_x.sql
new file mode 100644
--- /dev/null
+++ b/db/migrations/0099_x.sql
@@ -0,0 +1,3 @@
+DROP TABLE foo;
+DELETE FROM bar;
+CREATE TABLE ok (id INTEGER);
diff --git a/app.py b/app.py
--- a/app.py
+++ b/app.py
@@ -1,1 +1,2 @@
 x = 1
+key = "AKIAIOSFODNN7EXAMPLE"  # TODO fix later
"""


def _bash_check(fn: str, diff_text: str, tmp_path):
    df = tmp_path / "d.diff"; df.write_text(diff_text)
    script = f'source "{LIB}"; {fn} "$1"'
    out = subprocess.run(["bash", "-c", script, "_", str(df)], capture_output=True, text=True).stdout
    return [json.loads(ln) for ln in out.splitlines() if ln.strip()]


def _norm(issues):
    return sorted((d["lens"], d["severity"], d.get("file"), d["title"]) for d in issues)


def test_heuristic_checks_parity(tmp_path):
    pairs = [
        ("_check_migration_safety", ppr.check_migration_safety),
        ("_check_added_todos", ppr.check_added_todos),
        ("_check_secret_patterns", ppr.check_secret_patterns),
        ("_check_test_pairing", ppr.check_test_pairing),
        ("_check_diff_size", ppr.check_diff_size),
    ]
    for bash_fn, py_fn in pairs:
        rb = _bash_check(bash_fn, _DIFF, tmp_path)
        rp = py_fn(_DIFF)
        assert _norm(rb) == _norm(rp), f"{bash_fn}\nBASH:{_norm(rb)}\nPY:  {_norm(rp)}"


def _seed_repo(tmp, name):
    r = tmp / name; r.mkdir()
    def g(*a):
        return subprocess.run(["git", *a], cwd=r, capture_output=True, text=True)
    g("init", "-q", "-b", "main"); g("config", "user.email", "t@t.co"); g("config", "user.name", "t")
    (r / "app.py").write_text("x = 1\n")
    g("add", "."); g("commit", "-qm", "base")
    g("checkout", "-q", "-b", "feature")
    md = r / "db" / "migrations"; md.mkdir(parents=True)
    (md / "0099_x.sql").write_text("DROP TABLE foo;\nDELETE FROM bar;\n")
    (r / "app.py").write_text('x = 1\nkey = "AKIAIOSFODNN7EXAMPLE"  # TODO fix\n')
    g("add", "."); g("commit", "-qm", "risky change")
    sha = g("rev-parse", "HEAD").stdout.strip()
    home = r / ".mini-ork"; home.mkdir()
    db = str(home / "state.db")
    subprocess.run(["bash", str(REPO / "db" / "init.sh")],
                   env={**os.environ, "MINI_ORK_HOME": str(home), "MINI_ORK_DB": db},
                   capture_output=True, text=True, check=True)
    return r, sha, db


def _review_head(db):
    out = subprocess.run(
        ["sqlite3", db, "SELECT verdict,files_changed,lines_added,lines_removed,issues_open,issues_critical "
         "FROM pre_push_reviews ORDER BY id DESC LIMIT 1;"], capture_output=True, text=True).stdout.strip()
    issues = subprocess.run(
        ["sqlite3", "-separator", "|", db,
         "SELECT lens,severity,COALESCE(file_path,'') FROM pre_push_review_issues ORDER BY lens,severity,file_path;"],
        capture_output=True, text=True).stdout.strip()
    return out, issues


def test_review_run_end_to_end_parity(tmp_path):
    rb, sha_b, db_b = _seed_repo(tmp_path, "b")
    rp, sha_p, db_p = _seed_repo(tmp_path, "p")
    # bash
    subprocess.run(["bash", "-c", f'source "{LIB}"; review_run "$1" "$2"', "_", sha_b, "main"],
                   cwd=rb, capture_output=True, text=True,
                   env={**os.environ, "MINI_ORK_ROOT": str(rb), "MINI_ORK_DB": db_b,
                        "MINI_ORK_HOME": str(rb / ".mini-ork")})
    # python
    ppr.review_run(sha_p, "main", db=db_p, root=str(rp))
    hb, ib = _review_head(db_b)
    hp, ip = _review_head(db_p)
    assert hb == hp, f"head\nBASH:{hb}\nPY:  {hp}"
    assert ib == ip, f"issues\nBASH:{ib}\nPY:  {ip}"
    assert hp.startswith("block")   # secret critical + DROP/DELETE → block


def test_verdict_and_show(tmp_path):
    rp, sha, db = _seed_repo(tmp_path, "v")
    rid = ppr.review_run(sha, "main", db=db, root=str(rp))
    assert ppr.review_verdict_for(rid, db=db) == "block"
    show = ppr.review_show(rid, db=db)
    assert "heuristic.secret_leak" in show and "critical" in show


def test_native_llm_panel_contract_and_normalization(tmp_path, monkeypatch):
    """The panel calls the native model seam without requiring the Bash lib."""
    empty_root = tmp_path / "engine"
    empty_root.mkdir()
    calls = []

    def dispatch(model, prompt, out_file, timeout, max_turns):
        calls.append((model, prompt, timeout, max_turns))
        payload = {
            "issues": [{
                "severity": "unexpected",
                "file": "app.py",
                "line": 7,
                "title": f"{model} issue",
                "description": "concrete problem",
                "suggested_fix": "fix it",
            }]
        }
        Path(out_file).write_text("preface\n" + json.dumps(payload) + "\npostscript")
        return 0

    monkeypatch.setenv("MINI_ORK_ROOT", str(empty_root))
    monkeypatch.setenv("MO_REVIEW_PANEL", "codex gemini glm")
    monkeypatch.setenv("MO_REVIEW_LENS_TIMEOUT_S", "17")
    issues = ppr._default_llm_panel("diff body", dispatch_fn=dispatch)

    assert not (empty_root / "lib" / "llm-dispatch.sh").exists()
    assert [call[0] for call in calls] == ["codex", "glm"]
    assert all(call[1] == ppr._REVIEW_PROMPT + "diff body" for call in calls)
    assert all(call[2:] == ("17", 4) for call in calls)
    assert issues == [
        {
            "lens": "llm.codex", "severity": "medium", "file": "app.py", "line": 7,
            "title": "codex issue", "description": "concrete problem",
            "suggested_fix": "fix it",
        },
        {
            "lens": "llm.glm", "severity": "medium", "file": "app.py", "line": 7,
            "title": "glm issue", "description": "concrete problem",
            "suggested_fix": "fix it",
        },
    ]


def test_native_llm_panel_fails_open_per_lens(monkeypatch):
    calls = []

    def dispatch(model, prompt, out_file, timeout, max_turns):
        calls.append(model)
        if model == "kimi":
            raise RuntimeError("provider exploded")
        Path(out_file).write_text('{"issues":[]}')
        return 0

    monkeypatch.setenv("MO_REVIEW_PANEL", "kimi glm")
    assert ppr._default_llm_panel("diff", dispatch_fn=dispatch) == []
    assert calls == ["kimi", "glm"]
