"""Context v2: selection by files in scope + kickoff contract (mini_ork.context_v2)."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from mini_ork import context_v2 as cv
from mini_ork.memory import preferences

KICKOFF = """# Add the thing

## Files in scope (touch ONLY these)

- `mini_ork/ide_pages/learn/code.py` (new)
- `mini_ork/cli/reflect.py`: ONLY a new block calling `code_findings.harvest`

Do NOT modify any other file.

## Out of scope

- Changing the reflect schema.

## Notes

```bash
Do NOT treat this fenced line as a constraint
## Files in scope
- `inside/fence.py`
```

- Never write to the database from a page.

## Verification command

```bash
python3.11 -m pytest -q tests/unit/test_thing.py
```
"""


def _db(tmp_path: Path, findings=(), runs=()) -> str:
    path = tmp_path / "state.db"
    con = sqlite3.connect(path)
    con.execute("""CREATE TABLE code_findings (
        id INTEGER PRIMARY KEY, fingerprint TEXT, run_id TEXT, source TEXT, file TEXT,
        line INTEGER, severity TEXT, category TEXT, issue TEXT, snippet TEXT,
        verdict TEXT, ts TEXT)""")
    con.execute("""CREATE TABLE task_runs (
        id TEXT PRIMARY KEY, status TEXT, cost_usd REAL, created_at INTEGER,
        kickoff_path TEXT)""")
    for i, (run_id, file, severity, issue, ts) in enumerate(findings, 1):
        con.execute("INSERT INTO code_findings (id, fingerprint, run_id, source, file, line,"
                    " severity, category, issue, snippet, verdict, ts)"
                    " VALUES (?, ?, ?, 'review', ?, 1, ?, 'other', ?, '', 'fail', ?)",
                    (i, f"fp{i}", run_id, file, severity, issue, ts))
    for run_id, status, created_at, kickoff_path in runs:
        con.execute("INSERT INTO task_runs VALUES (?, ?, 0.1, ?, ?)",
                    (run_id, status, created_at, kickoff_path))
    con.commit()
    con.close()
    return str(path)


# ── contract ────────────────────────────────────────────────────────────────

def test_parse_contract_reads_scope_constraints_and_verification():
    c = cv.parse_contract(KICKOFF)
    assert c["files_in_scope"] == ["mini_ork/ide_pages/learn/code.py", "mini_ork/cli/reflect.py"]
    assert c["out_of_scope"] == ["Changing the reflect schema."]
    assert c["do_not"] == ["Do NOT modify any other file.",
                           "Never write to the database from a page."]
    assert c["verification"] == ["python3.11 -m pytest -q tests/unit/test_thing.py"]


def test_parse_contract_ignores_headings_and_constraints_inside_fences():
    c = cv.parse_contract(KICKOFF)
    assert "inside/fence.py" not in c["files_in_scope"]
    assert not any("fenced line" in d for d in c["do_not"])


def test_dotted_symbols_are_not_scope_paths():
    c = cv.parse_contract(KICKOFF)
    assert "code_findings.harvest" not in c["files_in_scope"]


def test_kickoff_stem_strips_revision_and_rejects_generic_names():
    assert cv.kickoff_stem("/w/kickoffs/auto/eng-path-rules-r2.md") == "eng-path-rules"
    assert cv.kickoff_stem("/w/kickoffs/auto/eng-path-rules.md") == "eng-path-rules"
    for generic in ("/r/kickoff.md", "/r/wave-kickoff.md", "/r/probe-3.md", "/r/strong.md"):
        assert cv.kickoff_stem(generic) == ""


# ── recurring problems ──────────────────────────────────────────────────────

DOCSTRING_ISSUES = [
    "BLOCKER mini_ork/a.py:12 the docstring contradicts the code",
    "mini_ork/b.py:40-44: docstring contradicts the code it documents",
    "HIGH mini_ork/c.py:7 docstring contradicts code",
    "the docstring here contradicts what the code does",
    "MINOR docstring of render() contradicts the code path",
]


def _findings(issues, severity="medium", file_for=None):
    return [{"id": i, "run_id": f"r{i % 3}", "file": (file_for or f"mini_ork/f{i}.py"),
             "severity": severity, "issue": text, "ts": f"2026-10-0{i % 9 + 1}"}
            for i, text in enumerate(issues)]


def test_cluster_groups_the_same_mistake_across_files_and_paraphrases():
    found = _findings(DOCSTRING_ISSUES + ["the query drops pending rows from the count"])
    clusters = cv.cluster(found)
    assert clusters[0]["n"] == 5
    assert len(clusters) == 2
    assert clusters[1]["n"] == 1 and "pending rows" in clusters[1]["representative"]


def test_cluster_ids_are_stable_and_prefixed():
    a = cv.cluster(_findings(DOCSTRING_ISSUES))
    b = cv.cluster(_findings(list(reversed(DOCSTRING_ISSUES))))
    assert a[0]["id"].startswith("f:") and len(a[0]["id"]) == 12
    assert a[0]["n"] == b[0]["n"] == 5


def test_clean_issue_strips_severity_and_file_line_but_keeps_bare_filenames():
    out = cv.clean_issue("BLOCKER mini_ork/x.py:12-14: plan.json is missing")
    assert "BLOCKER" not in out and "mini_ork/x.py" not in out
    assert "plan.json" in out


def test_recurrence_detects_a_restated_problem_only():
    clusters = cv.cluster(_findings(DOCSTRING_ISSUES))
    run = [{"issue": "MAJOR mini_ork/z.py:3 the docstring contradicts the code again"}]
    other = [{"issue": "the cache key ignores the file mtime"}]
    assert cv.recurrence(clusters, run) == {clusters[0]["id"]: True}
    assert cv.recurrence(clusters, other) == {clusters[0]["id"]: False}


# ── database selection ──────────────────────────────────────────────────────

def test_scope_findings_exact_file_and_parent_fallback_for_new_files(tmp_path):
    db = _db(tmp_path, findings=[
        ("r1", "mini_ork/ide_pages/learn/memory.py", "high", "HAVING drops pending uses", "2026-10-05"),
        ("r2", "mini_ork/cli/reflect.py", "medium", "reflect swallows errors", "2026-10-06"),
        ("r3", "mini_ork/cli/other.py", "medium", "unrelated cli thing", "2026-10-06"),
    ])
    rows = cv.scope_findings(["mini_ork/cli/reflect.py"], db=db)
    assert [r["issue"] for r in rows] == ["reflect swallows errors"]
    # new file with no findings → falls back to its 3-deep parent directory
    rows = cv.scope_findings(["mini_ork/ide_pages/learn/code.py"], db=db)
    assert [r["issue"] for r in rows] == ["HAVING drops pending uses"]
    # a 2-deep parent (mini_ork/cli) is too broad to fall back to
    assert cv.scope_findings(["mini_ork/cli/new_file.py"], db=db) == []


def test_scope_findings_excludes_the_current_run(tmp_path):
    db = _db(tmp_path, findings=[("cur", "a/b/c.py", "high", "x broke", "2026-10-05")])
    assert cv.scope_findings(["a/b/c.py"], db=db, exclude_run="cur") == []


def test_scope_findings_with_stats_reports_the_pre_decay_scan(tmp_path, monkeypatch):
    """LF-R11a: the companion keeps ``scope_findings``' list shape and reports
    the pre-decay scan count beside the drop count and the window used."""
    monkeypatch.delenv("MO_CONTEXT_V2_MAX_AGE_DAYS", raising=False)
    db = _db(tmp_path, findings=[
        ("r1", "mini_ork/cli/reflect.py", "high", "reflect drops findings", "2026-10-06"),
        ("r2", "mini_ork/cli/reflect.py", "medium", "reflect swallows errors", "2026-10-05"),
    ])
    rows, stats = cv.scope_findings_with_stats(["mini_ork/cli/reflect.py"], db=db)
    plain = cv.scope_findings(["mini_ork/cli/reflect.py"], db=db)
    assert [r["id"] for r in rows] == [r["id"] for r in plain]
    assert stats["n_findings_scanned"] == 2
    assert stats["n_findings_stale"] == 0
    assert stats["max_age_days"] == 30


def test_missing_db_or_tables_give_empty_sections(tmp_path):
    assert cv.scope_findings(["a/b.py"], db=str(tmp_path / "nope.db")) == []
    empty = tmp_path / "empty.db"
    sqlite3.connect(empty).close()
    assert cv.scope_findings(["a/b.py"], db=str(empty)) == []
    assert cv.prior_attempts("/k/eng-x.md", db=str(empty)) == []


def test_prior_attempts_match_revisions_but_never_generic_names(tmp_path):
    db = _db(tmp_path,
             findings=[("old", "a/b/c.py", "high", "kickoff not met", "2026-10-01")],
             runs=[("old", "failed", 1, "/w1/kickoffs/auto/eng-tab.md"),
                   ("rev", "published", 2, "/w2/kickoffs/auto/eng-tab-r2.md"),
                   ("other", "failed", 3, "/w3/kickoff.md"),
                   ("cur", "executing", 4, "/w4/kickoffs/auto/eng-tab-r3.md")])
    prior = cv.prior_attempts("/w4/kickoffs/auto/eng-tab-r3.md", db=db, current_run="cur")
    assert [p["run_id"] for p in prior] == ["rev", "old"]
    assert prior[1]["findings"][0]["issue"] == "kickoff not met"
    # "/w3/kickoff.md" exists, but a generic name only ever matches its exact path
    assert cv.prior_attempts("/w9/kickoff.md", db=db, current_run="x") == []


# ── the pack ────────────────────────────────────────────────────────────────

def test_build_applies_the_severity_floor_and_lists_item_ids(tmp_path, monkeypatch):
    db = _db(tmp_path, findings=[
        ("r1", "mini_ork/cli/reflect.py", "low", "harness note about a false negative", "2026-10-05"),
        ("r2", "mini_ork/cli/reflect.py", "high", "reflect swallows harvest errors", "2026-10-06"),
    ])
    kickoff = tmp_path / "eng-reflect.md"
    kickoff.write_text(KICKOFF, encoding="utf-8")
    pack = cv.build(str(kickoff), task_class="framework_edit", db=db, run_id="cur")
    reps = [c["representative"] for c in pack["file_findings"]]
    assert reps == ["reflect swallows harvest errors"]
    assert pack["item_ids"][:2] == ["c:0", "c:1"]
    assert pack["file_findings"][0]["id"] in cv.item_ids(pack)
    monkeypatch.setenv("MO_CONTEXT_V2_MIN_SEVERITY", "low")
    pack = cv.build(str(kickoff), db=db, run_id="cur")
    assert len(pack["file_findings"]) == 2


# ── scope fallback: kickoff → profile → none (LF-R9) ────────────────────────

PROSE_KICKOFF = """# Widget sweep

## Goal

The widget module keeps losing increments; make the suite green without
widening the change.

## Rules

Edit files directly; keep the change small.
"""


def _write_profile(run_dir: Path, scope_allow, kickoff_path=None) -> str:
    """A run_profile.json in the profiler's shape: raw kickoff bullet strings."""
    run_dir.mkdir(parents=True, exist_ok=True)
    profile = {"scope_allow": list(scope_allow)}
    if kickoff_path is not None:
        profile["kickoff_path"] = str(kickoff_path)
    (run_dir / "run_profile.json").write_text(json.dumps(profile), encoding="utf-8")
    return str(run_dir)


def test_build_prefers_the_parsed_kickoff_scope_over_a_profile(tmp_path):
    db = _db(tmp_path, findings=[
        ("r1", "mini_ork/cli/reflect.py", "high", "reflect drops findings", "2026-10-06"),
        ("r2", "mini_ork/unrelated.py", "high", "unrelated breakage", "2026-10-06"),
    ])
    kickoff = tmp_path / "eng-reflect.md"
    kickoff.write_text(KICKOFF, encoding="utf-8")
    run_dir = _write_profile(tmp_path / "run", ["`mini_ork/unrelated.py`"])

    pack = cv.build(str(kickoff), db=db, run_dir=run_dir)

    assert pack["scope_source"] == "kickoff"
    assert pack["scope_paths"] == ["mini_ork/ide_pages/learn/code.py",
                                   "mini_ork/cli/reflect.py"]
    assert pack["scope_paths"] == pack["contract"]["files_in_scope"]
    files = {f for c in pack["file_findings"] for f in c["files"]}
    assert "mini_ork/cli/reflect.py" in files
    assert "mini_ork/unrelated.py" not in files


def test_build_falls_back_to_the_profile_scope_when_the_kickoff_parses_empty(tmp_path):
    db = _db(tmp_path, findings=[
        ("r1", "mini_ork/cli/reflect.py", "high", "reflect drops findings", "2026-10-06"),
    ])
    kickoff = tmp_path / "widget-sweep.md"
    kickoff.write_text(PROSE_KICKOFF, encoding="utf-8")
    run_dir = _write_profile(tmp_path / "run", ["`mini_ork/cli/reflect.py`: only this"])

    assert cv.parse_contract(PROSE_KICKOFF)["files_in_scope"] == []
    assert preferences.scope_paths(run_dir) == ["mini_ork/cli/reflect.py"]

    pack = cv.build(str(kickoff), db=db, run_dir=run_dir)

    assert pack["scope_source"] == "profile"
    assert pack["scope_paths"] == preferences.scope_paths(run_dir)
    files = {f for c in pack["file_findings"] for f in c["files"]}
    assert "mini_ork/cli/reflect.py" in files
    assert pack["n_findings_scanned"] == 1
    # Selection only: the contract and its constraints are never back-filled.
    assert pack["contract"]["files_in_scope"] == []
    assert not [c for c in pack["constraints"] if c.get("kind") == "scope"]


def test_build_prose_only_profile_invents_no_scope(tmp_path):
    db = _db(tmp_path, findings=[
        ("r1", "mini_ork/cli/reflect.py", "high", "reflect drops findings", "2026-10-06"),
    ])
    kickoff = tmp_path / "widget-sweep.md"
    kickoff.write_text(PROSE_KICKOFF, encoding="utf-8")
    run_dir = _write_profile(tmp_path / "run",
                             ["keep the change small and focused",
                              "stay within the named module"])

    assert preferences.scope_paths(run_dir) == []

    pack = cv.build(str(kickoff), db=db, run_dir=run_dir)  # no crash

    assert pack["scope_source"] == "none"
    assert pack["scope_paths"] == []
    assert pack["file_findings"] == []
    assert pack["n_findings_scanned"] == 0


def test_build_without_a_run_dir_or_profile_stays_todays_behaviour(tmp_path):
    db = _db(tmp_path, findings=[
        ("r1", "mini_ork/cli/reflect.py", "high", "reflect drops findings", "2026-10-06"),
    ])
    kickoff = tmp_path / "widget-sweep.md"
    kickoff.write_text(PROSE_KICKOFF, encoding="utf-8")
    empty_dir = tmp_path / "run-without-profile"
    empty_dir.mkdir()

    pack_none = cv.build(str(kickoff), db=db)
    pack_missing = cv.build(str(kickoff), db=db, run_dir=str(empty_dir))

    for pack in (pack_none, pack_missing):
        assert pack["scope_source"] == "none"
        assert pack["scope_paths"] == []
        assert pack["file_findings"] == []
        assert pack["n_findings_scanned"] == 0


def test_pack_for_run_threads_run_dir_into_the_fallback(tmp_path):
    db = _db(tmp_path, findings=[
        ("r1", "mini_ork/cli/reflect.py", "high", "reflect drops findings", "2026-10-06"),
    ])
    kickoff = tmp_path / "widget-sweep.md"
    kickoff.write_text(PROSE_KICKOFF, encoding="utf-8")
    run_dir = _write_profile(tmp_path / "run", ["`mini_ork/cli/reflect.py`"],
                             kickoff_path=kickoff)

    pack = cv.pack_for_run(run_dir, db=db)

    assert pack["scope_source"] == "profile"
    on_disk = json.loads((Path(run_dir) / cv.PACK_FILENAME).read_text(encoding="utf-8"))
    assert on_disk["scope_source"] == "profile"
    assert on_disk["scope_paths"] == ["mini_ork/cli/reflect.py"]
    files = {f for c in on_disk["file_findings"] for f in c["files"]}
    assert "mini_ork/cli/reflect.py" in files


def test_render_keeps_constraints_and_drops_prior_first_under_budget(tmp_path):
    pack = {
        "constraints": [{"id": "c:0", "text": "Do NOT modify any other file."}],
        "file_findings": [{"id": "f:aaaaaaaaaa", "n": 2, "n_runs": 2, "files": ["a.py"],
                           "worst_severity": "high", "representative": "x" * 150}],
        "prior_attempts": [{"id": "p:r1", "status": "failed", "findings": [
            {"severity": "high", "file": "a.py", "line": 1, "issue": "y" * 150}]}],
    }
    full = cv.render(pack, "planner", budget_chars=10_000)
    assert "[p:r1]" in full and "context_used" in full
    tight = cv.render(pack, "planner", budget_chars=len(full) - 10)
    assert "[p:r1]" not in tight and "[f:aaaaaaaaaa]" in tight
    tiny = cv.render(pack, "planner", budget_chars=50)
    assert "[c:0] Do NOT modify any other file." in tiny
    assert "re-check your change" in cv.render(pack, "implementer")
    assert cv.render({"constraints": [], "file_findings": [], "prior_attempts": []}) == ""


# ── modes / holdout ─────────────────────────────────────────────────────────

def test_mode_defaults_to_shadow_and_rejects_unknown_values(monkeypatch):
    monkeypatch.delenv("MO_CONTEXT_V2", raising=False)
    assert cv.mode() == "shadow"
    monkeypatch.setenv("MO_CONTEXT_V2", "ON")
    assert cv.mode() == "on"
    monkeypatch.setenv("MO_CONTEXT_V2", "maybe")
    assert cv.mode() == "shadow"


def test_holdout_is_deterministic_and_respects_the_rate():
    ids = [f"run-{i}" for i in range(400)]
    assert [cv.in_holdout(r, 0.2) for r in ids] == [cv.in_holdout(r, 0.2) for r in ids]
    share = sum(cv.in_holdout(r, 0.2) for r in ids) / len(ids)
    assert 0.12 < share < 0.28
    assert not any(cv.in_holdout(r, 0.0) for r in ids)
    assert all(cv.in_holdout(r, 1.0) for r in ids)


def test_arm_is_v2_only_when_on_and_not_held_out(monkeypatch):
    """``injects()`` was removed (no callers); ``arm()`` is the one selector."""
    monkeypatch.setenv("MO_CONTEXT_V2_HOLDOUT", "0")
    monkeypatch.setenv("MO_CONTEXT_V2", "shadow")
    assert cv.arm("run-1") == "shadow"
    monkeypatch.setenv("MO_CONTEXT_V2", "on")
    assert cv.arm("run-1") == "v2"
    monkeypatch.setenv("MO_CONTEXT_V2_HOLDOUT", "1")
    assert cv.arm("run-1") == "holdout"


# ── diff check / persistence ────────────────────────────────────────────────

DIFF = """diff --git a/mini_ork/cli/reflect.py b/mini_ork/cli/reflect.py
index 1..2 100644
diff --git a/mini_ork/web/app.py b/mini_ork/web/app.py
index 3..4 100644
diff --git a/tests/unit/test_old.py b/tests/unit/test_old.py
deleted file mode 100644
"""


def test_check_diff_reports_out_of_scope_files_and_deleted_tests():
    out = cv.check_diff(DIFF, {"files_in_scope": ["mini_ork/cli/reflect.py", "tests/unit/**"]})
    assert out["outside_scope"] == ["mini_ork/web/app.py"]
    assert out["deleted_tests"] == ["tests/unit/test_old.py"]
    assert cv.check_diff(DIFF, {"files_in_scope": []})["outside_scope"] == []


def test_write_json_and_load_pack_round_trip(tmp_path):
    cv.write_json(str(tmp_path / cv.PACK_FILENAME), {"version": 2, "item_ids": ["c:0"]})
    assert cv.load_pack(str(tmp_path)) == {"version": 2, "item_ids": ["c:0"]}
    assert cv.load_pack(str(tmp_path / "missing")) == {}


@pytest.mark.parametrize("bad", ["", None])
def test_in_holdout_without_a_run_id_is_false(bad):
    assert cv.in_holdout(bad or "", 1.0) is False
