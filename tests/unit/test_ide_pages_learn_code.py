"""IDE page ``learn`` — the "Your code" tab, plus the reflect harvest hook.

The tab reads the ``code_findings`` tables (seeded directly here, since the
page never writes them) and answers: which parts of the tree attract review
findings, which problems keep coming back, and how does one become a rule.
"""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

import pytest

from mini_ork.ide_pages import learn
from mini_ork.ide_pages.learn import code as L
from mini_ork.learning import code_findings
from mini_ork.stores import migrate as mig

REPO = Path(__file__).resolve().parents[2]


# ── fixtures ───────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _no_ambient_db(monkeypatch):
    """The tab resolves its DB as MINI_ORK_DB → home/state.db; make sure an
    ambient MINI_ORK_DB cannot redirect the page away from the temp home."""
    monkeypatch.delenv("MINI_ORK_DB", raising=False)


@pytest.fixture
def home(tmp_path: Path) -> Path:
    h = tmp_path / "proj" / ".mini-ork"
    h.mkdir(parents=True)
    db = h / "state.db"
    db.touch()   # ensure_schema refuses to conjure a missing DB file
    code_findings.ensure_schema(db=str(db))
    return h


def _seed(home: Path, findings: list[dict], runs: dict[str, int] | None = None) -> Path:
    now = int(time.time())
    con = sqlite3.connect(home / "state.db")
    # ``findings_for`` LEFT JOINs task_runs for the run title/status; a bare
    # code_findings schema has no such table, so provide the columns it reads.
    con.execute("CREATE TABLE IF NOT EXISTS task_runs (id TEXT PRIMARY KEY, "
                "kickoff_path TEXT, status TEXT)")
    for i, f in enumerate(findings):
        con.execute(
            "INSERT INTO code_findings (fingerprint, run_id, source, file, line, severity, "
            "category, issue, snippet, verdict, ts) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (f"fp{i}", f.get("run_id", "run-1"), f.get("source", "reviewer"), f.get("file"),
             f.get("line"), f.get("severity", "medium"), f.get("category", "other"),
             f["issue"], f.get("snippet"), f.get("verdict"), f.get("ts", now - 3600)))
    for run_id, n in (runs or {}).items():
        con.execute("INSERT INTO code_findings_runs (run_id, harvested_at, n) VALUES (?,?,?)",
                    (run_id, now - 120, n))
    con.commit()
    con.close()
    return home


def _repo_files(monkeypatch, *paths: str) -> None:
    """Declare these seeded paths to be code-repo files.

    The areas table counts only files the repo tracks: ``areas(repo_only=True)``
    → ``_in_repo``, which keeps a path only when ``git ls-files`` lists it or it
    exists on disk. The seeded findings name synthetic paths that do neither, so
    without this stub the areas table is empty under any real cwd. Stubbing
    ``_git_ls_files`` — the same seam ``test_code_findings.py`` uses — makes the
    fixtures repo files without inventing an on-disk tree.
    """
    monkeypatch.setattr(code_findings, "_git_ls_files", lambda *_: set(paths))


def _head_repo(tmp_path: Path, files: dict[str, str]) -> str:
    """A real git checkout holding ``files`` at HEAD — the repo LF-R11a's
    staleness probe reads (as opposed to a synthetic, on-disk-less fixture)."""
    import subprocess

    repo = tmp_path / "head-repo"
    repo.mkdir()
    for rel, text in files.items():
        p = repo / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True)
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(repo), "-c", "user.email=t@example.com",
                    "-c", "user.name=T", "commit", "-q", "-m", "init"], check=True)
    return str(repo)


def _section(page: dict, title: str) -> dict:
    return next(s for s in page["sections"] if s["title"] == title)


def _flag(argv: list[str], name: str) -> str:
    """The value of ``--name`` in a CLI argv — an AssertionError means it is absent."""
    return argv[argv.index(name) + 1]


def _target(row: dict) -> str:
    """A recurring row's ``--target`` glob (the rule's file scope)."""
    return _flag(row["do"]["cli"], "--target")


def _build(home: Path, args: dict | None = None) -> dict:
    return learn.build(home, "code", args or {})


# ── recurring(): pure clustering ───────────────────────────────────────────

def test_recurring_groups_paraphrases_and_keeps_others_separate() -> None:
    findings = [
        {"id": 1, "run_id": "r1", "file": "app/handlers/a.py", "severity": "high",
         "issue": "BLOCKER app/handlers/a.py:10-20: docstring contradicts the code"},
        {"id": 2, "run_id": "r2", "file": "app/handlers/b.py", "severity": "medium",
         "issue": "docstring contradicts the code"},
        {"id": 3, "run_id": "r3", "file": "app/services/c.py", "severity": "medium",
         "issue": "the docstring contradicts the code"},
        {"id": 4, "run_id": "r4", "file": "app/services/d.py", "severity": "low",
         "issue": "docstring contradicts the code comment"},
        {"id": 5, "run_id": "r5", "file": "app/models/e.py", "severity": "medium",
         "issue": "BLOCKER docstring contradicts the code"},
        {"id": 6, "run_id": "r6", "file": "app/models/f.py", "severity": "medium",
         "issue": "query drops pending rows"},
    ]
    clusters = L.recurring(findings)

    big = [c for c in clusters if c["n"] == 5]
    assert len(big) == 1, clusters
    assert big[0]["n_runs"] == 5
    assert big[0]["worst_severity"] == "high"
    assert len(big[0]["finding_ids"]) == 5
    # These findings carry no ``kickoff_path``, so each buckets by its run id
    # (LF-R10) — five runs, five kickoffs, five occurrences.
    assert big[0]["kickoffs"] == 5
    assert big[0]["n_members"] == 5
    # Paths and "BLOCKER" do not block grouping — all five collapse to one
    # cluster — yet the representative keeps the real finding text to show.
    stripped = L._strip_issue(big[0]["representative"])
    assert "app/handlers" not in stripped
    assert "BLOCKER" not in stripped
    # The unrelated finding is its own cluster — but a lone one is not
    # RECURRING (LF-R10: one kickoff is not a recurrence), so it lives in
    # seen_once(), not in the recurring list.
    assert not any("query drops pending rows" in c["representative"] for c in clusters)
    once = L.seen_once(findings)
    assert any("query drops pending rows" in c["representative"] for c in once)


def test_recurring_is_empty_for_no_findings() -> None:
    assert L.recurring([]) == []


def test_recurring_skips_the_no_issue_placeholder() -> None:
    """``(no issue)`` is the ABSENCE of a problem, not a recurring one — it must
    not head the recurring table (reviewer, round 2: 18 such rows topped it
    live)."""
    findings = [
        {"run_id": "r1", "file": "mini_ork/x.py", "severity": "high",
         "issue": "(no issue)"},
        {"run_id": "r2", "file": "mini_ork/x.py", "severity": "high",
         "issue": "(no issue)"},
        {"run_id": "r3", "file": "mini_ork/y.py", "severity": "medium",
         "issue": "query drops pending rows"},
    ]
    clusters = L.recurring(findings)
    # The placeholder is dropped, and the one real problem was seen in a single
    # kickoff, so nothing is RECURRING here (LF-R10).
    assert clusters == []
    once = L.seen_once(findings)
    assert len(once) == 1
    assert "(no issue)" not in once[0]["representative"]
    assert once[0]["representative"] == "query drops pending rows"
    assert once[0]["occurrences"] == 1


# ── recurring(): counted per KICKOFF, not per row (LF-R10) ─────────────────

NODE = "mini_ork/ide_pages/node.py"
SPEC = "mini_ork/ide_pages/spec.py"

RETRY_1 = "Retry loop swallows the exception and hides the real failure from the operator"
RETRY_2 = "The retry loop swallows exceptions so the real failure is hidden from the operator"
RETRY_3 = "Swallowed exceptions in the retry loop hide the real failure from the operator"
RETRY_4 = "The retry loop hides real failures by swallowing the exception"
COST_1 = "Cost budget check divides by zero when the run has no spent cost"
COST_2 = "Division by zero in the cost budget check for a run with zero spend"
COST_3 = "The cost budget check divides by zero for runs with no spent cost"
# Engineered word pools: m1~m2 are at cosine 0.5, m3 is 0.323 from each of them
# but 0.373 from the pair's centroid — so m3 joins through the centroid only,
# with no cross-kickoff member pair at the 0.35 threshold. That is `mixed`.
MIX_1 = "Savanna loam falcon diesel pasture sonnet pellet oakum ember cobalt."
MIX_2 = "Pepper saddle nimbus meadow cinder sonnet pellet oakum ember cobalt."
MIX_3 = ("Savanna loam falcon diesel pasture pepper saddle nimbus meadow cinder "
         "rowan spindle glider vetch quince mattock ledger coral.")


def _kfind(fid, issue, run_id, kickoff=None, file=NODE, sev="high"):
    """A findings row in the shape ``_select_findings`` returns."""
    return {"id": fid, "run_id": run_id, "file": file, "line": 1, "severity": sev,
            "category": "other", "issue": issue, "snippet": "", "verdict": "",
            "ts": 1700000000, "kickoff_path": kickoff}


def test_recurring_needs_two_kickoffs_not_merely_restatements() -> None:
    """Four restatements of one bug in ONE kickoff are an emphasis, not a
    recurrence (LF-R10) — they are shown by ``seen_once``, with one occurrence."""
    fs = [_kfind(f"a{i}", text, "r1", "kickoffs/auto/lf-panel-r2.md")
          for i, text in enumerate((RETRY_1, RETRY_2, RETRY_3, RETRY_4), 1)]
    assert L.recurring(fs) == []
    once = L.seen_once(fs)
    assert len(once) == 1, once
    assert once[0]["occurrences"] == 1 and once[0]["kickoffs"] == 1
    assert once[0]["n_members"] == 4


def test_recurring_collapses_kickoff_revisions_to_one_stem() -> None:
    """``-r3`` and ``-r4`` are revisions of ONE kickoff, so a bug in both is
    still a one-kickoff problem (LF-R10)."""
    fs = [_kfind("b1", RETRY_1, "r1", "kickoffs/auto/ide-node-stream-r3.md"),
          _kfind("b2", RETRY_2, "r2", "kickoffs/auto/ide-node-stream-r4.md")]
    assert L.recurring(fs) == []
    once = L.seen_once(fs)
    assert len(once) == 1
    assert once[0]["kickoffs"] == 1 and once[0]["occurrences"] == 1
    assert once[0]["n_members"] == 2


def test_two_distinct_kickoffs_are_recurring() -> None:
    """Two DIFFERENT kickoffs carrying the same bug is the recurrence the tab
    drafts a rule for; ``seen_once`` (the complement) is then empty."""
    fs = [_kfind("b1", RETRY_1, "r1", "kickoffs/auto/node-overview.md"),
          _kfind("b2", RETRY_2, "r2", "kickoffs/auto/ide-node-stream.md")]
    rec = L.recurring(fs)
    assert len(rec) == 1, rec
    c = rec[0]
    assert c["kickoffs"] == 2 and c["occurrences"] == 2 and c["n_members"] == 2
    assert c["n"] == c["occurrences"]        # n IS occurrences now
    assert c["mixed"] is False               # the two members are 0.62 cosine
    assert set(c["finding_ids"]) == {"b1", "b2"}
    assert L.seen_once(fs) == []


def test_generic_kickoffs_bucket_by_run_id_and_null_kickoff_is_kept() -> None:
    """A generic ``kickoff.md`` carries no identity, so two of them must not
    merge — they bucket by run id. A finding from a run with no ``task_runs``
    row (``kickoff_path`` None) is kept, and bucketed by its run id too."""
    fs = [_kfind("c1", RETRY_1, "r1", "kickoffs/foo/kickoff.md"),
          _kfind("c2", RETRY_2, "r2", "kickoffs/bar/kickoff.md"),
          _kfind("c3", RETRY_3, "r3", None)]
    rec = L.recurring(fs)
    assert len(rec) == 1, rec
    assert rec[0]["kickoffs"] == 3 and rec[0]["occurrences"] == 3
    assert rec[0]["n_members"] == 3
    assert set(rec[0]["finding_ids"]) == {"c1", "c2", "c3"}


def test_cross_kickoff_cluster_below_sim_is_mixed() -> None:
    """A cluster that spans two kickoffs only because a member attached to the
    running centroid — with no cross-kickoff PAIR at ``_SIM`` — is ``mixed``."""
    fs = [_kfind("m1", MIX_1, "r1", "kickoffs/auto/lf-cache-r1.md"),
          _kfind("m2", MIX_2, "r2", "kickoffs/auto/lf-cache-r2.md"),
          _kfind("m3", MIX_3, "r3", "kickoffs/auto/lf-iter-r1.md", file=SPEC)]
    rec = L.recurring(fs)
    assert len(rec) == 1, rec
    assert rec[0]["mixed"] is True
    # lf-cache-r1/-r2 are one kickoff (one file), lf-iter is the second.
    assert rec[0]["kickoffs"] == 2
    assert rec[0]["occurrences"] == 2
    assert rec[0]["n_members"] == 3


def test_occurrences_counts_kickoff_file_pairs() -> None:
    """``occurrences`` is the number of distinct ``(kickoff, file)`` pairs: two
    kickoffs over two files is four, while a restatement inside one file adds
    nothing (``n`` is that value; the raw count is ``n_members``)."""
    fs = [_kfind("b1", COST_1, "r4", "kickoffs/auto/cost-fix-r1.md", sev="critical"),
          _kfind("b2", COST_2, "r5", "kickoffs/auto/budget-fix-r1.md", sev="critical"),
          _kfind("b3", COST_3, "r4", "kickoffs/auto/cost-fix-r1.md", file=SPEC, sev="critical"),
          _kfind("b4", COST_1, "r5", "kickoffs/auto/budget-fix-r1.md", file=SPEC, sev="critical"),
          _kfind("b5", COST_2, "r5", "kickoffs/auto/budget-fix-r1.md", sev="critical")]
    c = L.recurring(fs)[0]
    assert c["kickoffs"] == 2
    assert c["occurrences"] == 4
    assert c["n"] == c["occurrences"]
    assert c["n_members"] == 5
    assert set(c["files"]) == {NODE, SPEC} and len(c["files"]) == len(set(c["files"]))


def test_recurring_sorted_by_kickoffs_then_occurrences() -> None:
    """The wider recurrence leads: three kickoffs beats two, whatever the
    severity or the member count."""
    a = [_kfind("a1", RETRY_1, "r1", "kickoffs/auto/node-overview.md"),
         _kfind("a2", RETRY_2, "r2", "kickoffs/auto/ide-node-stream-r3.md"),
         _kfind("a3", RETRY_3, "r3", "kickoffs/auto/lf-gate-r1.md")]
    b = [_kfind("b1", COST_1, "r4", "kickoffs/auto/cost-fix-r1.md", sev="critical"),
         _kfind("b2", COST_2, "r5", "kickoffs/auto/budget-fix-r1.md", sev="critical"),
         _kfind("b3", COST_3, "r4", "kickoffs/auto/cost-fix-r1.md", file=SPEC, sev="critical"),
         _kfind("b4", COST_1, "r5", "kickoffs/auto/budget-fix-r1.md", file=SPEC, sev="critical"),
         _kfind("b5", COST_2, "r5", "kickoffs/auto/budget-fix-r1.md", sev="critical")]
    rec = L.recurring(a + b)
    assert len(rec) == 2
    assert (rec[0]["kickoffs"], rec[0]["occurrences"]) == (3, 3)
    assert set(rec[0]["finding_ids"]) == {"a1", "a2", "a3"}
    assert (rec[1]["kickoffs"], rec[1]["occurrences"]) == (2, 4)
    assert set(rec[1]["finding_ids"]) == {"b1", "b2", "b3", "b4", "b5"}


def test_seen_once_partitions_the_clustered_findings() -> None:
    """The two lists are a partition: recurring ∪ seen_once is every clustered
    finding, and their intersection is empty."""
    fs = [_kfind("a1", RETRY_1, "r1", "kickoffs/auto/node-overview.md"),
          _kfind("a2", RETRY_2, "r2", "kickoffs/auto/ide-node-stream.md"),
          _kfind("c1", COST_1, "r9", "kickoffs/auto/budget-fix-r2.md", file=SPEC, sev="medium"),
          _kfind("c2", COST_2, "r9", "kickoffs/auto/budget-fix-r2.md", file=SPEC, sev="medium"),
          _kfind("c3", COST_3, "r9", "kickoffs/auto/budget-fix-r2.md", file=SPEC, sev="medium")]
    rec, once = L.recurring(fs), L.seen_once(fs)
    assert len(rec) == 1 and len(once) == 1
    for cluster in (rec[0], once[0]):
        for key in ("representative", "n", "n_members", "kickoffs", "occurrences",
                    "files", "mixed", "worst_severity", "finding_ids"):
            assert key in cluster, key
    rec_ids = {i for c in rec for i in c["finding_ids"]}
    once_ids = {i for c in once for i in c["finding_ids"]}
    assert rec_ids.isdisjoint(once_ids)
    assert rec_ids | once_ids == {"a1", "a2", "c1", "c2", "c3"}
    assert (once[0]["kickoffs"], once[0]["occurrences"]) == (1, 1)


def test_rule_action_only_for_recurring_clusters() -> None:
    """The "Make it a rule" button is offered only for a recurring cluster: a
    one-kickoff cluster with FOUR restatements in one file has a perfectly
    defensible target, and is still denied the action (LF-R10)."""
    rec = L.recurring([_kfind("a1", RETRY_1, "r1", "kickoffs/auto/node-overview.md"),
                       _kfind("a2", RETRY_2, "r2", "kickoffs/auto/ide-node-stream.md")])[0]
    assert L._rule_action(rec) is not None
    once = L.seen_once([_kfind("c1", COST_1, "r9", "kickoffs/auto/budget-fix-r2.md",
                               file=SPEC, sev="medium"),
                        _kfind("c2", COST_2, "r9", "kickoffs/auto/budget-fix-r2.md",
                               file=SPEC, sev="medium"),
                        _kfind("c3", COST_3, "r9", "kickoffs/auto/budget-fix-r2.md",
                               file=SPEC, sev="medium")])[0]
    assert once["occurrences"] == 1 and once["n_members"] == 3
    assert L._rule_action(once) is None


def test_kickoff_revisions_of_the_same_run_kickoff_are_not_recurring(home, monkeypatch) -> None:
    """End to end: ``kickoff_path`` reaches the page through the ``task_runs``
    join, and two runs of ONE kickoff's revisions are one kickoff, so the pair
    is listed as seen-once — not offered as a recurrence to make a rule of."""
    _seed(home, [
        {"run_id": "r1", "file": "svc/pay/a.py", "issue": "docstring contradicts the code"},
        {"run_id": "r2", "file": "svc/pay/a.py", "issue": "the docstring contradicts the code"},
    ])
    con = sqlite3.connect(home / "state.db")
    con.execute("INSERT INTO task_runs VALUES (?,?,?)",
                ("r1", "kickoffs/auto/pay-fix-r1.md", "done"))
    con.execute("INSERT INTO task_runs VALUES (?,?,?)",
                ("r2", "kickoffs/auto/pay-fix-r2.md", "done"))
    con.commit()
    con.close()
    _repo_files(monkeypatch, "svc/pay/a.py")

    page = _build(home, {"area": "svc/pay"})
    assert page["errors"] == {}, page["errors"]
    # `pay-fix-r1`/`-r2` collapse to the ONE stem `pay-fix`: the two runs are
    # one kickoff, so this is not a recurrence (the run ids differ, so a
    # run-id-bucketed implementation would wrongly call it one).
    assert _section(page, L.RECURRING_TITLE)["rows"] == []
    assert "pay-fix" in json.dumps(_section(page, L.SEEN_ONCE_TITLE))
    assert "Make it a rule" not in json.dumps(page["sections"])


def test_area_detail_renders_kickoff_columns_and_seen_once(home, monkeypatch) -> None:
    """The detail's table reports KICKOFFS (with ``(mixed)`` where the cluster
    only met through the centroid) and files, and lists the one-kickoff
    restatements under "Seen in one kickoff" with no rule button among them."""
    fs = [_kfind("a1", RETRY_1, "r1", "kickoffs/auto/node-overview.md"),
          _kfind("a2", RETRY_2, "r2", "kickoffs/auto/ide-node-stream-r3.md"),
          _kfind("c1", COST_1, "r9", "kickoffs/auto/budget-fix-r2.md", file=SPEC, sev="medium"),
          _kfind("c2", COST_2, "r9", "kickoffs/auto/budget-fix-r2.md", file=SPEC, sev="medium"),
          _kfind("c3", COST_3, "r9", "kickoffs/auto/budget-fix-r2.md", file=SPEC, sev="medium")]
    monkeypatch.setattr(L, "_window_findings",
                        lambda db, *, since_days, repo_only=True, repo_root=None: list(fs))
    sections = L._area_detail(home, {"days": "30"}, "mini_ork/ide_pages")

    rec = next(s for s in sections if s["title"] == L.RECURRING_TITLE)
    assert rec["head"] == ["problem", "kickoffs", "files", "worst", "action"]
    assert len(rec["rows"]) == 1                 # the recurring cluster only
    assert rec["rows"][0]["cells"][1]["t"] == "2"
    assert NODE in rec["rows"][0]["cells"][2]["t"]

    dump = json.dumps(sections, default=str)
    assert "Seen in one kickoff" in dump         # the restatements are shown …
    assert "budget-fix" in dump                  # … labelled with their kickoff
    assert dump.count("Make it a rule") == 1     # the recurring cluster's, only

    mixed = [_kfind("m1", MIX_1, "r1", "kickoffs/auto/lf-cache-r1.md"),
             _kfind("m2", MIX_2, "r2", "kickoffs/auto/lf-cache-r2.md"),
             _kfind("m3", MIX_3, "r3", "kickoffs/auto/lf-iter-r1.md", file=SPEC)]
    monkeypatch.setattr(L, "_window_findings",
                        lambda db, *, since_days, repo_only=True, repo_root=None: list(mixed))
    rows = next(s for s in L._area_detail(home, {"days": "30"}, "mini_ork/ide_pages")
                if s["title"] == L.RECURRING_TITLE)["rows"]
    assert rows[0]["cells"][1]["t"] == "2 (mixed)"


# ── the tab ────────────────────────────────────────────────────────────────

def test_code_is_the_default_tab(home: Path) -> None:
    page = learn.build(home, None, {})
    assert page["tab"] == "code"
    assert [(t["key"], t["label"]) for t in page["tabs"]][0] == ("code", "Your code")
    assert page["errors"] == {}, page["errors"]


def test_empty_state(home: Path) -> None:
    page = _build(home)
    assert page["errors"] == {}, page["errors"]
    areas_sec = _section(page, L.AREAS_TITLE)
    assert areas_sec["type"] == "list"
    assert "No review findings yet" in areas_sec["items"][0]["t"]
    assert _section(page, L.KV_TITLE)["items"][0]["v"] == "nothing yet"


def test_days_chips(home: Path) -> None:
    chips = _section(_build(home), L.DAYS_TITLE)["items"]
    assert [c["t"] for c in chips] == ["7 days", "30 days", "90 days"]
    assert [c["on"] for c in chips] == [False, True, False]   # default 30
    assert chips[0]["do"] == {"set": {"days": "7"}}
    # An explicit window flips the active chip.
    chips7 = _section(_build(home, {"days": "7"}), L.DAYS_TITLE)["items"]
    assert [c["on"] for c in chips7] == [True, False, False]


def test_areas_table_rows_and_freshness(home: Path, monkeypatch) -> None:
    _seed(home, [
        {"run_id": f"r{i}", "file": f"svc/pay/{name}.py", "severity": sev, "issue": iss}
        for i, (name, sev, iss) in enumerate([
            ("a", "high", "docstring contradicts the code"),
            ("b", "medium", "docstring contradicts the code"),
            ("c", "medium", "the docstring contradicts the code"),
            ("d", "low", "unrelated naming nit"),
        ])
    ] + [{"run_id": "r9", "file": "svc/cart/x.py", "severity": "medium",
          "issue": "query drops pending rows"}],
        runs={"r0": 3, "r1": 3, "r2": 3, "r3": 3, "r9": 1})
    _repo_files(monkeypatch, "svc/pay/a.py", "svc/pay/b.py", "svc/pay/c.py",
                "svc/pay/d.py", "svc/cart/x.py")

    page = _build(home)
    assert page["errors"] == {}, page["errors"]

    # Freshness (kv) reflects code_findings_runs.
    assert "5 runs" in _section(page, L.KV_TITLE)["items"][0]["v"]

    rows = _section(page, L.AREAS_TITLE)["rows"]
    pay = next(r for r in rows if r["cells"][0]["t"] == "svc/pay")
    assert pay["cells"][1]["t"] == "4"          # findings
    assert pay["cells"][2]["t"] == "4"          # runs
    assert pay["cells"][3]["t"] == "high"       # worst
    assert "docstring contradicts" in pay["cells"][4]["t"]   # recurring problems
    assert pay["do"] == {"set": {"area": "svc/pay"}}


def test_area_detail_recurring_rule_and_findings(home: Path, monkeypatch,
                                                 tmp_path: Path) -> None:
    _seed(home, [
        {"run_id": "r1", "file": "svc/pay/a.py", "line": 10, "severity": "high",
         "issue": "docstring contradicts the code", "snippet": "def pay(): ...",
         "run_title": "Fix pay", "run_status": "done"},
        {"run_id": "r2", "file": "svc/pay/b.py", "line": 22, "severity": "medium",
         "issue": "docstring contradicts the code", "run_title": "Fix pay 2",
         "run_status": "done"},
        {"run_id": "r3", "file": "svc/pay/c.py", "line": 3, "severity": "low",
         "issue": "query drops pending rows", "run_title": "Fix pay 3", "run_status": "done"},
    ])
    # The detail applies the same repo-only membership as the areas table.
    _repo_files(monkeypatch, "svc/pay/a.py", "svc/pay/b.py", "svc/pay/c.py")
    # LF-R11a: a row WITH a snippet is checked against its repo's HEAD. Point
    # the engine identity at a real temp checkout that holds the quoted line, so
    # r1 is live — a synthetic path absent from the engine checkout would be
    # (correctly) hidden as file-missing.
    head = _head_repo(tmp_path, {"svc/pay/a.py": "def pay(): ...\n"})
    monkeypatch.setattr(L, "engine_repo_identity", lambda root=None: head)

    page = _build(home, {"area": "svc/pay"})
    assert page["errors"] == {}, page["errors"]

    md = _section(page, "svc/pay")
    assert md["type"] == "markdown"
    assert "3 findings in 3 runs" in md["text"]
    assert "**Recurring problems**" in md["text"]

    rec = _section(page, L.RECURRING_TITLE)
    # The docstring cluster comes first: seen in two kickoffs (these two runs
    # carry no ``task_runs`` row, so each buckets by its run id — LF-R10). The
    # lone r3 finding is not a recurrence and is listed as seen-once instead.
    assert rec["head"] == ["problem", "kickoffs", "files", "worst", "action"]
    assert len(rec["rows"]) == 1
    assert rec["rows"][0]["cells"][1]["t"] == "2"
    assert rec["rows"][0]["cells"][2]["t"] == "svc/pay/a.py, svc/pay/b.py"
    action = rec["rows"][0]["do"]
    assert action["cli"][0:2] == ["prefs", "set"]
    assert action["cli"][2].startswith("review-")
    assert _flag(action["cli"], "--scope") == "path"
    # Both members are direct children of ``svc/pay``, so the rule targets
    # exactly those files — not the ``svc/pay/sub`` subtree, which is a
    # different row (reviewer, round 2), and not the whole ``svc/pay`` area.
    assert _target(rec["rows"][0]) == "svc/pay/*"
    # LF-R4: the button drafts an INACTIVE, learned rule — it does not write a
    # live operator rule.
    assert _flag(action["cli"], "--origin") == "learned"
    assert "--draft" in action["cli"]
    assert "svc/pay/*" in action["confirm"]
    low_confirm = action["confirm"].lower()
    assert "inactive" in low_confirm and "draft" in low_confirm
    assert "rules" in low_confirm

    findings = _section(page, L.FINDINGS_TITLE)
    assert len(findings["rows"]) == 3
    assert findings["rows"][0]["cells"][0]["t"].startswith("svc/pay/")

    # Selecting a finding opens its detail with the full issue, snippet and actions.
    key = findings["rows"][0]["do"]["set"]["finding"]
    detail_page = _build(home, {"area": "svc/pay", "finding": key})
    detail = _section(detail_page, L.FINDING_TITLE)
    assert detail["type"] == "markdown"
    assert "docstring contradicts the code" in detail["text"]
    assert "def pay(): ..." in detail["text"]
    labels = [a["label"] for a in detail["actions"]]
    assert "Open run" in labels and "Close" in labels


def test_area_directory_excludes_sibling_prefix(home: Path, monkeypatch) -> None:
    """A directory area matches only its own descendants — never a sibling
    directory whose name merely shares the prefix (`mini_ork/acp` must not sweep
    in `mini_ork/acp_orchestrator/*`). Otherwise the "Make it a rule" action for
    one area is sourced from another area's findings and injected into the wrong
    runs (reviewer, round 1)."""
    _seed(home, [
        {"run_id": "r1", "file": "mini_ork/acp/server.py", "severity": "low",
         "issue": "cursor left open in the server loop"},
        {"run_id": "r2", "file": "mini_ork/acp/client.py", "severity": "low",
         "issue": "cursor left open in the server loop"},
        {"run_id": "r3", "file": "mini_ork/acp_orchestrator/state.py", "severity": "high",
         "issue": "state machine skips the terminal transition"},
        {"run_id": "r4", "file": "mini_ork/acp_orchestrator/plan.py", "severity": "high",
         "issue": "state machine skips the terminal transition"},
    ])
    _repo_files(monkeypatch, "mini_ork/acp/server.py", "mini_ork/acp/client.py",
                "mini_ork/acp_orchestrator/state.py", "mini_ork/acp_orchestrator/plan.py")

    page = _build(home, {"area": "mini_ork/acp"})
    assert page["errors"] == {}, page["errors"]

    # The area's own two files only — not the two acp_orchestrator findings.
    md = _section(page, "mini_ork/acp")
    assert "2 findings in 2 runs" in md["text"], md["text"]
    assert "worst low" in md["text"], md["text"]

    files = sorted(r["cells"][0]["t"].rstrip(":") for r in _section(page, L.FINDINGS_TITLE)["rows"])
    assert files == ["mini_ork/acp/client.py", "mini_ork/acp/server.py"], files

    # The area's two findings recur, so they offer ONE rule — sourced from this
    # area only and targeting its own files, never the sibling's.
    rec = _section(page, L.RECURRING_TITLE)
    assert rec["rows"], "the area's two findings should produce clusters"
    assert rec["rows"][0]["cells"][1]["t"] == "2"
    assert "acp_orchestrator" not in rec["rows"][0]["cells"][0]["t"]
    assert _target(rec["rows"][0]) == "mini_ork/acp/*"

    # The areas-table row for `mini_ork/acp` must likewise exclude the sibling
    # (its "recurring problems" cell is built through the same query path).
    table_row = next(r for r in _section(page, L.AREAS_TITLE)["rows"]
                     if r["cells"][0]["t"] == "mini_ork/acp")
    assert table_row["cells"][1]["t"] == "2"          # findings
    assert "state machine" not in table_row["cells"][4]["t"]


def test_area_extensionless_file_is_not_treated_as_directory(home: Path) -> None:
    """An area that is a FILE must not be queried as a directory.

    ``bin/mini-ork`` and ``Makefile`` carry no file extension, but the suffix is
    not what decides file-vs-directory — the data is. Round 1 regressed these:
    ``Path(area).suffix == ""`` sent every extensionless area down the directory
    branch, so ``findings_for("bin/mini-ork/")`` matched nothing and the detail
    page rendered "none yet" and a ``--target bin/mini-ork/**`` glob that can
    never match a file (reviewer, round 2).
    """
    _seed(home, [
        {"run_id": "r1", "file": "bin/mini-ork", "severity": "high",
         "issue": "argparse default mutates the shared parser"},
        {"run_id": "r2", "file": "bin/mini-ork", "severity": "high",
         "issue": "argparse default mutates the shared parser"},
        {"run_id": "r3", "file": "bin/mini-ork", "severity": "high",
         "issue": "argparse default mutates the shared parser"},
        {"run_id": "r4", "file": "Makefile", "severity": "medium",
         "issue": "tab indentation lost in the lint target"},
    ])

    page = _build(home, {"area": "bin/mini-ork"})
    assert page["errors"] == {}, page["errors"]

    # The file's own three findings are shown — not "0 findings … none yet".
    md = _section(page, "bin/mini-ork")
    assert "3 findings in 3 runs" in md["text"], md["text"]
    assert "worst high" in md["text"], md["text"]

    fs_rows = _section(page, L.FINDINGS_TITLE)["rows"]
    assert len(fs_rows) == 3
    assert all(r["cells"][0]["t"] == "bin/mini-ork:" for r in fs_rows)

    # The rule targets the file itself — no glob that can never match.
    rec = _section(page, L.RECURRING_TITLE)
    assert rec["rows"], "the file's three findings should cluster"
    assert _target(rec["rows"][0]) == "bin/mini-ork"
    assert "bin/mini-ork/**" not in rec["rows"][0]["do"]["confirm"]

    # A sibling extensionless file is its own area, never swept in by the
    # ``bin/mini-ork`` prefix query. One finding there is not a RECURRING
    # problem (LF-R10: one kickoff, one occurrence), so the recurring table is
    # empty, the problem is shown as seen-once, and no rule is offered at all.
    mk_page = _build(home, {"area": "Makefile"})
    mk = _section(mk_page, "Makefile")
    assert "1 findings in 1 run" in mk["text"], mk["text"]
    mk_rows = _section(mk_page, L.FINDINGS_TITLE)["rows"]
    assert [r["cells"][0]["t"] for r in mk_rows] == ["Makefile:"]
    assert _section(mk_page, L.RECURRING_TITLE)["rows"] == []
    mk_dump = json.dumps(mk_page["sections"])
    assert "Make it a rule" not in mk_dump
    assert "tab indentation lost in the lint target" in mk_dump


def test_area_detail_summary_matches_findings_it_lists(home: Path, monkeypatch) -> None:
    """The areas-table row and the detail page report the SAME findings.

    ``areas()`` labels a directory group with its dominant file (≥ 60%) while the
    group holds every file under the same ``_dir_prefix``. r1 keyed the row on the
    label's own recursive ``area/**`` set, so a dominant-file ``node.py`` row read
    86 while the group held 133, and the siblings fell out of every row
    (reviewer, round 1 + 2, blocking). Round 2 keys the row AND its click target
    on the group, so the row and the page it opens are one set by construction.
    """
    _seed(home, [
        {"run_id": f"r{i}", "file": "app/svc/x.py", "severity": "low",
         "issue": "docstring contradicts the code"} for i in range(3)
    ] + [
        {"run_id": "r9", "file": "app/svc/y.py", "severity": "high",
         "issue": "retry budget check skipped on retry"},
    ])
    _repo_files(monkeypatch, "app/svc/x.py", "app/svc/y.py")

    page_all = _build(home)
    page = _build(home, {"area": "app/svc"})

    # x.py dominates the ``app/svc`` group (3 of 4) so it labels the row — as a
    # CELL. The row counts the whole group; its click target is the group key.
    table_row = next(r for r in _section(page_all, L.AREAS_TITLE)["rows"]
                     if r["cells"][0]["t"] == "app/svc/x.py")
    assert table_row["cells"][1]["t"] == "4"          # the group, not x.py alone
    assert table_row["cells"][2]["t"] == "4"          # four runs
    assert table_row["cells"][3]["t"] == "high"       # y.py's high is in the row
    assert table_row["do"] == {"set": {"area": "app/svc"}}

    # The detail the row opens lists the same four — sibling included.
    md = _section(page, "app/svc")
    assert "4 findings in 4 runs" in md["text"], md["text"]
    assert "worst high" in md["text"], md["text"]
    fs_rows = _section(page, L.FINDINGS_TITLE)["rows"]
    listed = {r["cells"][0]["t"] for r in fs_rows}
    assert "app/svc/y.py:" in listed, listed
    assert table_row["cells"][1]["t"] == str(len(fs_rows))   # row == detail


def test_area_row_count_equals_detail_for_directory_group(home: Path, monkeypatch) -> None:
    """A shallow directory group holds only its own direct files; a deeper file
    keys on its longer prefix — a separate, disjoint row (reviewer, round 2).

    r1 made the ``mini_ork`` row the whole recursive subtree, so it double counted
    ``mini_ork/cli/c.py``, which ALSO had its own row — the rows summed to more
    findings than exist. Now each row is the exact set the detail lists.
    """
    _seed(home, [
        {"run_id": "r1", "file": "mini_ork/a.py", "severity": "low",
         "issue": "the docstring contradicts the code"},
        {"run_id": "r2", "file": "mini_ork/b.py", "severity": "medium",
         "issue": "the docstring contradicts the code"},
        {"run_id": "r3", "file": "mini_ork/cli/c.py", "severity": "high",
         "issue": "retry budget check skipped on retry"},
    ])
    _repo_files(monkeypatch, "mini_ork/a.py", "mini_ork/b.py", "mini_ork/cli/c.py")

    detail = _build(home, {"area": "mini_ork"})
    md = _section(detail, "mini_ork")
    # two direct files only — the deeper file is its own group, not in this one.
    assert "2 findings in 2 runs" in md["text"], md["text"]
    assert "worst medium" in md["text"], md["text"]

    rows = _section(_build(home), L.AREAS_TITLE)["rows"]
    top = next(r for r in rows if r["cells"][0]["t"] == "mini_ork")
    assert top["cells"][1]["t"] == "2"                # == detail count
    assert top["cells"][2]["t"] == "2"                # == detail runs
    assert top["cells"][3]["t"] == "medium"           # == detail worst
    assert top["do"] == {"set": {"area": "mini_ork"}}

    # ``mini_ork/cli`` is its own row — the deeper finding, counted once.
    sub = next(r for r in rows if r["do"] == {"set": {"area": "mini_ork/cli"}})
    assert sub["cells"][1]["t"] == "1"
    assert len(_section(detail, L.FINDINGS_TITLE)["rows"]) == 2


def test_area_row_counts_partition_the_window_without_overlap(home: Path, monkeypatch) -> None:
    """The rows are a DISJOINT partition of the window's repo findings: their
    counts sum to the number of distinct findings, so nothing is counted twice
    and no row over-counts a neighbour (reviewer, round 2).

    r1 derived each row from a recursive ``area/**`` set, so the ``mini_ork`` row
    absorbed ``mini_ork/cli``'s findings and the live counts summed to 1010
    against 593 distinct findings.
    """
    seeded = (
        [{"run_id": f"a{i}", "file": "mini_ork/a.py", "severity": "low",
          "issue": "the docstring contradicts the code"} for i in range(2)]
        + [{"run_id": f"b{i}", "file": "mini_ork/cli/b.py", "severity": "high",
            "issue": "retry budget check skipped on retry"} for i in range(3)]
        + [{"run_id": "c0", "file": "mini_ork/cli/sub/c.py", "severity": "medium",
            "issue": "the docstring contradicts the code"}]
        + [{"run_id": f"d{i}", "file": "svc/pay/d.py", "severity": "low",
            "issue": "query drops pending rows"} for i in range(4)]
        + [{"run_id": "e0", "file": "docs/readme.md", "severity": "low",
            "issue": "stale docstring"}]
    )
    _seed(home, seeded)
    _repo_files(monkeypatch, "mini_ork/a.py", "mini_ork/cli/b.py",
                "mini_ork/cli/sub/c.py", "svc/pay/d.py", "docs/readme.md")

    rows = _section(_build(home), L.AREAS_TITLE)["rows"]
    total = sum(int(r["cells"][1]["t"]) for r in rows)
    assert total == len(seeded), (total, [r["cells"][0]["t"] for r in rows])
    # Five disjoint groups: mini_ork, mini_ork/cli, mini_ork/cli/sub, svc/pay, docs.
    assert len(rows) == 5


def test_dominant_file_group_row_keeps_its_sibling_findings(home: Path, monkeypatch) -> None:
    """A dominant file labels its group but does not shrink it: the group's other
    files' findings still appear in the row, and opening the row reaches them
    (reviewer, round 2).

    Live, ``mini_ork/ide_pages`` (label ``node.py``, 86 of 133) held
    ``search.py``'s real 5× recurring problem; keying the row on the label hid
    those 47 sibling findings from every row.
    """
    _seed(home, [
        {"run_id": f"n{i}", "file": "mini_ork/ide_pages/node.py", "severity": "high",
         "issue": "pill uses the offset slice"} for i in range(3)
    ] + [
        {"run_id": "s0", "file": "mini_ork/ide_pages/search.py", "severity": "high",
         "issue": "indexed_count is stale after reindex"},
    ])
    _repo_files(monkeypatch, "mini_ork/ide_pages/node.py",
                "mini_ork/ide_pages/search.py")

    row = next(r for r in _section(_build(home), L.AREAS_TITLE)["rows"]
               if r["cells"][0]["t"] == "mini_ork/ide_pages/node.py")
    # node.py labels the group (3 of 4) but the row counts all four, and its
    # target is the group key — not the dominant file.
    assert row["cells"][1]["t"] == "4"
    assert row["do"] == {"set": {"area": "mini_ork/ide_pages"}}
    # The sibling's finding is reachable by opening that row.
    detail = _build(home, {"area": "mini_ork/ide_pages"})
    listed = [r["cells"][0]["t"] for r in _section(detail, L.FINDINGS_TITLE)["rows"]]
    assert "mini_ork/ide_pages/search.py:" in listed, listed


def test_area_row_ignores_non_repo_artifact_under_directory(home: Path, monkeypatch) -> None:
    """A run artifact under a directory area is not counted in either view
    (reviewer, round 1, case c).

    ``areas(repo_only=True)`` already drops ``framework-edit.diff``; the detail
    must apply the same filter or the two counts diverge on any directory that
    contains a non-repo artifact.
    """
    _seed(home, [
        {"run_id": "r1", "file": "svc/pay/a.py", "severity": "medium",
         "issue": "the docstring contradicts the code"},
        {"run_id": "r2", "file": "svc/pay/b.py", "severity": "low",
         "issue": "the docstring contradicts the code"},
        {"run_id": "r3", "file": "svc/pay/framework-edit.diff", "severity": "high",
         "issue": "diff does not apply cleanly"},
    ])
    _repo_files(monkeypatch, "svc/pay/a.py", "svc/pay/b.py")

    detail = _build(home, {"area": "svc/pay"})
    md = _section(detail, "svc/pay")
    assert "2 findings in 2 runs" in md["text"], md["text"]
    assert "worst medium" in md["text"], md["text"]

    table_row = next(r for r in _section(_build(home), L.AREAS_TITLE)["rows"]
                     if r["cells"][0]["t"] == "svc/pay")
    assert table_row["cells"][1]["t"] == "2"          # == detail count
    assert table_row["cells"][2]["t"] == "2"          # == detail runs
    assert table_row["cells"][3]["t"] == "medium"     # == detail worst


def test_area_summary_counts_all_findings_while_table_is_capped(home: Path, monkeypatch) -> None:
    """The summary line and the recurrence clusters read the area's FULL set;
    only the Findings table is capped at 50 (kickoff #1).

    r1 derived all three from one 50-capped list, so an area with more than 50
    findings rendered "50 findings in 8 runs" — a count and a worst severity
    taken from a truncated sample — and clustered only that sample.
    """
    n = 60
    findings = [
        {"run_id": f"r{i % 8}", "file": "svc/pay/a.py",
         "severity": "high" if i == 0 else "low",
         "issue": "docstring contradicts the code"}      # identical → one cluster
        for i in range(n)
    ]
    _seed(home, findings)
    _repo_files(monkeypatch, "svc/pay/a.py")

    page = _build(home, {"area": "svc/pay/a.py"})
    assert page["errors"] == {}, page["errors"]

    md = _section(page, "svc/pay/a.py")
    assert f"{n} findings in 8 runs" in md["text"], md["text"]
    assert "worst high" in md["text"], md["text"]

    # The table itself is capped …
    assert len(_section(page, L.FINDINGS_TITLE)["rows"]) == L._AREA_FINDINGS
    # … but the clusters are computed over the full set, not the capped page:
    # all 60 restatements cluster (8 run-id kickoffs, one file), so the rendered
    # kickoffs cell reads 8 — and the pure call sees every member.
    assert _section(page, L.RECURRING_TITLE)["rows"][0]["cells"][1]["t"] == "8"
    assert L.recurring(findings)[0]["n_members"] == n
    assert L.recurring(findings)[0]["occurrences"] == 8

    # The live-proof invariant: the detail summary count equals the areas-table
    # count for the same area (both read the full set over the same window).
    table_row = next(r for r in _section(page, L.AREAS_TITLE)["rows"]
                     if r["cells"][0]["t"] == "svc/pay/a.py")
    assert table_row["cells"][1]["t"] == str(n)


def test_area_file_branch_not_crowded_out_by_prefix_siblings(home: Path, monkeypatch) -> None:
    """A file area shows its OWN findings even when newer prefix siblings
    outnumber the 50-row cap (kickoff #2).

    r1 pulled ``LIMIT 50`` by the prefix ``bin/mini-ork%`` and then filtered
    ``file == 'bin/mini-ork'`` in Python, so 60 newer ``bin/mini-ork-apply`` rows
    filled the limit and ``bin/mini-ork`` rendered "0 findings … none yet" with a
    ``--target bin/mini-ork/**`` glob that can never match a file.
    """
    now = int(time.time())
    siblings = [
        {"run_id": f"s{i}", "file": "bin/mini-ork-apply", "severity": "low",
         "issue": "flag parsing ignores the subcommand", "ts": now - 10}
        for i in range(60)
    ]
    own = [
        {"run_id": f"o{i}", "file": "bin/mini-ork", "severity": "high",
         "issue": "argparse default mutates the shared parser", "ts": now - 5000}
        for i in range(3)
    ]
    _seed(home, siblings + own)
    _repo_files(monkeypatch, "bin/mini-ork", "bin/mini-ork-apply")

    page = _build(home, {"area": "bin/mini-ork"})
    assert page["errors"] == {}, page["errors"]

    md = _section(page, "bin/mini-ork")
    assert "3 findings in 3 runs" in md["text"], md["text"]
    assert "worst high" in md["text"], md["text"]
    assert [r["cells"][0]["t"] for r in _section(page, L.FINDINGS_TITLE)["rows"]] \
        == ["bin/mini-ork:"] * 3
    # The rule targets the file itself — never a glob that cannot match.
    assert _target(_section(page, L.RECURRING_TITLE)["rows"][0]) == "bin/mini-ork"


def test_area_detail_honours_days_window(home: Path, monkeypatch) -> None:
    """The area detail applies the same ``days`` window as the areas table
    (kickoff #4). r1 applied no window in the detail, so a 7-day view listed
    findings the areas table had already excluded — the two counts disagreed.
    """
    now = int(time.time())
    _seed(home, [
        {"run_id": "recent", "file": "svc/pay/a.py", "severity": "high",
         "issue": "docstring contradicts the code", "ts": now - 86400},
        {"run_id": "old", "file": "svc/pay/a.py", "severity": "high",
         "issue": "query drops pending rows", "ts": now - 40 * 86400},
    ])
    _repo_files(monkeypatch, "svc/pay/a.py")

    # 7-day window: the 40-day-old finding is outside it, in the detail too.
    page = _build(home, {"area": "svc/pay/a.py", "days": "7"})
    assert page["errors"] == {}, page["errors"]
    assert "1 findings in 1 run" in _section(page, "svc/pay/a.py")["text"]
    assert len(_section(page, L.FINDINGS_TITLE)["rows"]) == 1

    # 90-day window: both findings, and the areas table agrees.
    wide = _build(home, {"area": "svc/pay/a.py", "days": "90"})
    assert "2 findings in 2 runs" in _section(wide, "svc/pay/a.py")["text"]
    table_row = next(r for r in _section(wide, L.AREAS_TITLE)["rows"]
                     if r["cells"][0]["t"] == "svc/pay/a.py")
    assert table_row["cells"][1]["t"] == "2"


def test_area_detail_close_button_clears_selection(home: Path) -> None:
    _seed(home, [{"run_id": "r1", "file": "svc/pay/a.py", "line": 1,
                  "issue": "docstring contradicts the code"}])
    page = _build(home, {"area": "svc/pay"})
    md = _section(page, "svc/pay")
    assert md["type"] == "markdown"
    close = next(a for a in md["actions"] if a["label"] == "Close area")
    assert close["do"] == {"set": {"area": "", "finding": ""}}


# ── the "Make it a rule" DRAFT (LF-R4) ─────────────────────────────────────

def test_rule_targets_the_member_file_not_the_area_glob(home: Path, monkeypatch) -> None:
    """A cluster whose members are all in ONE file targets that file.

    The area key is the grouping prefix (``mini_ork/ide_pages``), so targeting
    the AREA emitted ``mini_ork/ide_pages/*`` — a rule injected into every run
    that touches a sibling file, for a problem found only in ``node.py``.
    """
    file = "mini_ork/ide_pages/node.py"
    _seed(home, [{"run_id": f"r{i}", "file": file, "severity": "high",
                  "issue": "the log proxy leaks the writer handle"} for i in range(3)])
    _repo_files(monkeypatch, file)

    page = _build(home, {"area": "mini_ork/ide_pages"})
    assert page["errors"] == {}, page["errors"]
    row = _section(page, L.RECURRING_TITLE)["rows"][0]
    assert _target(row) == file
    assert _target(row) != "mini_ork/ide_pages/*"


def test_rule_targets_dir_globstar_for_nested_members(home: Path, monkeypatch) -> None:
    """Members that share a directory but not a parent get ``<dir>/**``.

    Both files key to the same area (the grouping truncates at three segments),
    but they are not direct children of one directory, so neither ``<dir>/*``
    (too narrow) nor the area glob (too wide) describes them.
    """
    files = ["mini_ork/cli/sub/a.py", "mini_ork/cli/sub/deep/b.py"]
    _seed(home, [{"run_id": f"r{i}", "file": f, "severity": "medium",
                  "issue": "the shard counter goes stale under load"}
                 for i, f in enumerate(files)])
    _repo_files(monkeypatch, *files)

    page = _build(home, {"area": "mini_ork/cli/sub"})
    assert page["errors"] == {}, page["errors"]
    assert _target(_section(page, L.RECURRING_TITLE)["rows"][0]) == "mini_ork/cli/sub/**"


def test_one_member_cluster_offers_no_rule(home: Path, monkeypatch) -> None:
    """One finding is not a recurring problem, so there is nothing to make a rule of."""
    _seed(home, [{"run_id": "r0", "file": "mini_ork/solo/only.py", "severity": "medium",
                  "issue": "a one-off problem, never seen again"}])
    _repo_files(monkeypatch, "mini_ork/solo/only.py")

    page = _build(home, {"area": "mini_ork/solo"})
    assert page["errors"] == {}, page["errors"]
    # Seen in one kickoff, so it is not recurring — listed as seen-once, with no
    # action anywhere on the page (LF-R10).
    assert _section(page, L.RECURRING_TITLE)["rows"] == []
    dump = json.dumps(page["sections"])
    assert "a one-off problem, never seen again" in dump
    assert "Make it a rule" not in dump


def test_rule_draft_text_is_generalised(home: Path, monkeypatch) -> None:
    """The drafted text keeps the problem and drops the verifier transcript.

    ``clean_issue`` removes file:line references and severity words; on top of
    that the action drops the sentence a verifier appends ("Reproduced: …
    PASSED/FAILED") and its status tokens. A rule quoting a reproduction result
    tells a future agent about a run, not about the code it will edit.
    """
    file = "mini_ork/ide_pages/node.py"
    issue = ("The log proxy leaks the writer handle in mini_ork/ide_pages/node.py:42 "
             "(medium). Reproduced: [False] PASSED, [True] FAILED.")
    _seed(home, [{"run_id": f"r{i}", "file": file, "severity": "medium",
                  "issue": issue} for i in range(3)])
    _repo_files(monkeypatch, file)

    page = _build(home, {"area": "mini_ork/ide_pages"})
    assert page["errors"] == {}, page["errors"]
    text = _section(page, L.RECURRING_TITLE)["rows"][0]["do"]["cli"][3]
    low = text.lower()

    assert text.startswith(f"Reviewers repeatedly found in {file}: "), text
    assert "log proxy" in low, "the generalised substance must survive"
    for banned in ("reproduced", "passed", "failed", "[true]", "[false]",
                   ":42", "medium"):
        assert banned not in low, (banned, text)


def test_approved_learned_rule_does_not_reach_a_sibling_file(home: Path) -> None:
    """The drafted rule's target is the FILE, so its siblings are untouched.

    Approving promotes the draft (``active=1``); it does not widen its scope.
    """
    from mini_ork.memory import preferences

    db = str(home / "state.db")
    preferences.set_pref(
        "review-node",
        "Reviewers repeatedly found in mini_ork/ide_pages/node.py: the log proxy leaks",
        scope="path", target="mini_ork/ide_pages/node.py",
        origin="learned", source_ids=["k1"], db=db)

    sibling = preferences.prefs_for(
        "framework_edit", paths=["mini_ork/ide_pages/_cn.py"], db=db)
    assert not any(r["key"] == "review-node" for r in sibling)
    own = preferences.prefs_for(
        "framework_edit", paths=["mini_ork/ide_pages/node.py"], db=db)
    assert any(r["key"] == "review-node" for r in own)


# ── the reflect harvest hook (E6) ──────────────────────────────────────────

@pytest.fixture
def migrated_home(tmp_path: Path) -> Path:
    h = tmp_path / "home"
    h.mkdir(parents=True)
    rc, _out, err = mig.init_db(db=str(h / "state.db"), root=str(REPO))
    assert rc == 0, err
    return h


def _reflect_env(monkeypatch, home: Path) -> None:
    monkeypatch.setenv("MINI_ORK_HOME", str(home))
    monkeypatch.setenv("MINI_ORK_DB", str(home / "state.db"))
    monkeypatch.setenv("MINI_ORK_ROOT", str(REPO))
    monkeypatch.setenv("MINI_ORK_GRADIENT_EXTRACTOR_FN", "_rfl_stub")   # no live LLM
    for var in ("MO_PATTERN_MINER", "MO_PATTERN_INDUCE", "MO_THEMES",
                "MO_CROSS_EPIC_GRADIENTS", "MO_BUG_REPORT_SWEEP",
                "MO_RHO_AGGREGATE", "MO_LANE_ROUTER"):
        monkeypatch.setenv(var, "0")


def test_reflect_calls_code_findings_harvest_once(migrated_home, monkeypatch, capsys):
    from mini_ork.cli import reflect
    from mini_ork.learning import code_findings as cf

    calls: list[dict] = []

    def _fake_harvest(home, *, run_ids=None, db=None):
        calls.append({"home": home, "db": db})
        return {"runs": 2, "findings": 7, "with_file": 5, "skipped_unparseable": 0}

    monkeypatch.setattr(cf, "harvest", _fake_harvest)
    monkeypatch.delenv("MO_CODE_FINDINGS", raising=False)
    _reflect_env(monkeypatch, migrated_home)

    rc = reflect.main(["--since", "1700000000"])
    out = capsys.readouterr().out

    assert rc == 0
    assert len(calls) == 1
    assert "  [code_findings] harvested 2 run(s), 7 finding(s)" in out


def test_reflect_skips_harvest_when_disabled(migrated_home, monkeypatch, capsys):
    from mini_ork.cli import reflect
    from mini_ork.learning import code_findings as cf

    calls: list[str] = []
    monkeypatch.setattr(cf, "harvest",
                        lambda home, **kw: calls.append("x") or {"runs": 0, "findings": 0})
    monkeypatch.setenv("MO_CODE_FINDINGS", "0")
    _reflect_env(monkeypatch, migrated_home)

    rc = reflect.main(["--since", "1700000000"])
    out = capsys.readouterr().out

    assert rc == 0
    assert calls == []
    assert "[code_findings]" not in out


def test_reflect_fail_soft_on_harvest_error(migrated_home, monkeypatch, capsys):
    from mini_ork.cli import reflect
    from mini_ork.learning import code_findings as cf

    def _boom(home, **kw):
        raise RuntimeError("db locked")

    monkeypatch.setattr(cf, "harvest", _boom)
    monkeypatch.delenv("MO_CODE_FINDINGS", raising=False)
    _reflect_env(monkeypatch, migrated_home)

    rc = reflect.main(["--since", "1700000000"])
    err = capsys.readouterr().err

    assert rc == 0
    assert "  [code_findings] skipped: db locked" in err
