"""IDE page ``learn`` — Learning & memory, from state.db and the TraceOtter output."""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

import pytest

from mini_ork.ide_pages import build_page, learn
from mini_ork.stores import migrate as mig

REPO = Path(__file__).resolve().parents[2]


@pytest.fixture
def home(tmp_path: Path) -> Path:
    h = tmp_path / "proj" / ".mini-ork"
    h.mkdir(parents=True)
    rc, _out, err = mig.init_db(db=str(h / "state.db"), root=str(REPO))
    assert rc == 0, err
    return h


@pytest.fixture
def seeded(home: Path) -> Path:
    now = int(time.time())
    con = sqlite3.connect(home / "state.db")
    con.execute("INSERT INTO gradient_records (gradient_id, target, signal, suggested_change, evidence, "
                "confidence, created_at, task_class) VALUES ('g1', 'verifier.code_fix', 'checks unclear', "
                "'record the checks', '{}', 0.84, ?, 'code_fix')", (now,))
    for i in range(3):
        con.execute("INSERT INTO failure_memory (run_id, workflow_stage, failure_category, error_message, "
                    "occurred_at) VALUES (?, 'execute', 'dispatch_error', 'rc=124', ?)", (i, f"2026-10-0{i + 1}"))
    have = {r[1] for r in con.execute("PRAGMA table_info(semantic_memory)")}
    for col, decl in (("uses", "INTEGER NOT NULL DEFAULT 0"), ("wins", "INTEGER NOT NULL DEFAULT 0"),
                      ("retired_at", "REAL NOT NULL DEFAULT 0")):
        if col not in have:   # mini_ork.memory.semantic adds these on first use
            con.execute(f"ALTER TABLE semantic_memory ADD COLUMN {col} {decl}")
    con.execute("INSERT INTO semantic_memory (scope, text, embedding, created_at, uses, wins, retired_at) "
                "VALUES ('code_fix', 'a', x'00', ?, 10, 1, 0)", (now,))      # decaying
    con.execute("INSERT INTO semantic_memory (scope, text, embedding, created_at, uses, wins, retired_at) "
                "VALUES ('code_fix', 'b', x'00', ?, 1, 1, 0)", (now,))       # active
    con.execute("INSERT INTO semantic_memory (scope, text, embedding, created_at, uses, wins, retired_at) "
                "VALUES ('code_fix', 'c', x'00', ?, 9, 0, ?)", (now, now))  # retired
    con.execute("INSERT INTO self_improve_runs (run_id, started_at, finished_at, iter, soft_deadline_at, "
                "hard_deadline_at, outcome, notes) VALUES ('si-1', ?, ?, 3, 0, 0, 'success', 'inject g-121')",
                (now - 600, now))
    con.execute("INSERT INTO llm_calls (provider, model_id, tier, feature_name, status, cost_usd, ts, run_id) "
                "VALUES ('gateway', 'glm', 'std', 'mini-ork:worker', 'success', 1.5, '2026-10-06T00:00:00Z', 'si-1')")
    con.execute("INSERT INTO promotion_records (promotion_id, candidate_id, from_version_id, to_version_id, "
                "utility_before, utility_after, decision, decided_at, decided_by) VALUES "
                "('p1', 'cand-1', 'v0.4.1', 'v0.4.2', 0.86, 0.91, 'promoted', '2026-10-05T10:00:00Z', 'gate')")
    con.execute("INSERT INTO bug_reports (fingerprint, agent_role, title, observed_in, confidence, status, "
                "first_seen_at, last_seen_at, updated_at) VALUES ('fp', 'scheduler', 'verdict.json mismatch', "
                "'bin/mini-ork-scheduler', 0.95, 'open', ?, ?, ?)", (now, now, now))
    con.execute("INSERT INTO idea_tree_nodes (node_id, parent_node_id, root_node_id, hypothesis, status, "
                "created_at) VALUES ('r', NULL, 'r', 'dark mode support', 'pending', ?)", (now,))
    con.execute("INSERT INTO idea_tree_nodes (node_id, parent_node_id, root_node_id, hypothesis, status, "
                "created_at) VALUES ('c1', 'r', 'r', 'tokens map', 'harvested', ?)", (now + 1,))
    con.execute("INSERT INTO idea_tree_nodes (node_id, parent_node_id, root_node_id, hypothesis, status, "
                "created_at) VALUES ('c2', 'r', 'r', 'css media query', 'pruned', ?)", (now + 2,))
    con.commit()
    con.close()
    return home


def _section(page: dict, title: str) -> dict:
    return next(s for s in page["sections"] if s["title"] == title)


def test_page_shape_and_default_tab(home: Path) -> None:
    for tab in ("overview", "lessons", "memory", "improve"):
        page = learn.build(home, tab, {})
        assert page["ok"] and page["key"] == "learn" and page["title"] == "Learning & memory"
        assert [(t["key"], t["label"]) for t in page["tabs"]] == [
            ("overview", "Overview"), ("lessons", "Lessons"),
            ("memory", "Memory"), ("improve", "Self-improve")]
        assert page["tab"] == tab
        assert page["errors"] == {}, (tab, page["errors"])
        json.dumps(page)
    # None and unknown both fall back to overview.
    assert learn.build(home, None, {})["tab"] == "overview"
    assert learn.build(home, "nope", {})["tab"] == "overview"


def test_old_tab_keys_are_remapped(home: Path) -> None:
    """Old deep-linked URLs (learnings, traceotter, bugs) still resolve."""
    assert learn.build(home, "learnings", {})["tab"] == "lessons"
    assert learn.build(home, "traceotter", {})["tab"] == "improve"
    assert learn.build(home, "bugs", {})["tab"] == "overview"


def test_overview_needs_you_shows_chips_only_when_nonzero(seeded: Path) -> None:
    page = learn.build(seeded, "overview", {})
    needs = _section(page, "Needs you")
    # The seeded home has 1 open bug report → exactly that chip.
    chip_titles = [c["t"] for c in needs["items"]]
    assert any("open bug report" in t for t in chip_titles)


def test_overview_empty_needs_you(home: Path) -> None:
    page = learn.build(home, "overview", {})
    needs = _section(page, "Needs you")
    assert needs["type"] == "list"
    assert needs["items"][0]["t"] == "Nothing needs you"


def test_overview_outcomes_table_rates_deltas_and_stuck(home: Path) -> None:
    now = int(time.time())
    DAY = 86400
    # Seed two windows: cur (last 28d) and base (28-56d ago).
    runs = [
        # Current window — code_fix 6 published, 1 failed → ~86% pass.
        ("run-cf-1", "code_fix", "published", 1.0, now - DAY * 2),
        ("run-cf-2", "code_fix", "published", 1.0, now - DAY * 3),
        ("run-cf-3", "code_fix", "published", 1.0, now - DAY * 4),
        ("run-cf-4", "code_fix", "published", 1.0, now - DAY * 5),
        ("run-cf-5", "code_fix", "published", 1.0, now - DAY * 6),
        ("run-cf-6", "code_fix", "published", 1.0, now - DAY * 7),
        ("run-cf-7", "code_fix", "failed", 0.5, now - DAY * 8),
        # Baseline window — code_fix 4 published out of 6 → ~67% pass.
        ("run-cf-b1", "code_fix", "published", 1.0, now - DAY * 30),
        ("run-cf-b2", "code_fix", "published", 1.0, now - DAY * 31),
        ("run-cf-b3", "code_fix", "published", 1.0, now - DAY * 32),
        ("run-cf-b4", "code_fix", "published", 1.0, now - DAY * 33),
        ("run-cf-b5", "code_fix", "failed", 0.5, now - DAY * 34),
        ("run-cf-b6", "code_fix", "failed", 0.5, now - DAY * 35),
        # Current window — framework_edit 2 published, 3 failed → 40% pass (below 50% → red).
        ("run-fe-1", "framework_edit", "published", 2.0, now - DAY * 2),
        ("run-fe-2", "framework_edit", "published", 2.0, now - DAY * 3),
        ("run-fe-3", "framework_edit", "failed", 2.0, now - DAY * 4),
        ("run-fe-4", "framework_edit", "failed", 2.0, now - DAY * 5),
        ("run-fe-5", "framework_edit", "failed", 2.0, now - DAY * 6),
        # A stuck run — executing > 24h old.
        ("run-stuck", "framework_edit", "executing", 0.0, now - DAY * 2),
    ]
    con = sqlite3.connect(home / "state.db")
    for rid, cls, status, cost, ts in runs:
        con.execute("INSERT INTO task_runs (id, task_class, status, cost_usd, kickoff_path, "
                    "created_at, updated_at, ended_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (rid, cls, status, cost, "kickoffs/auto/test.md", ts, ts, ts))
    con.commit()
    con.close()
    page = learn.build(home, "overview", {})
    table = _section(page, "Outcomes by task class")
    by_class = {r["cells"][0]["t"]: r for r in table["rows"]}
    cf = by_class["code_fix"]
    assert cf["cells"][1]["t"] == "7"   # 6 pass + 1 fail
    assert cf["cells"][2]["t"].startswith("86") or cf["cells"][2]["t"].startswith("85")
    assert cf["cells"][2]["c"] == "green"
    # Δ pass: 86% - 67% = +19 pts → green.
    assert cf["cells"][3]["t"].startswith("+19") or cf["cells"][3]["t"].startswith("+18")
    assert cf["cells"][3]["c"] == "green"
    # Stuck column.
    fe = by_class["framework_edit"]
    assert fe["cells"][1]["t"] == "5"   # 2 pass + 3 fail
    assert fe["cells"][6]["t"] == "1"
    assert fe["cells"][2]["c"] == "red"


def test_overview_cost_per_pass_includes_failed_runs(home: Path) -> None:
    """Regression: ``cost / pass`` divides window cost (pass + fail) by passes.

    Earlier revisions summed cost on the pass branch only, so failed runs with
    non-zero cost silently dropped out of the numerator. With 4 published at
    $1.00 and 1 failed at $10.00, cost/pass must read $14.00/4 = $3.50, NOT
    the $1.00 the bug would produce.
    """
    now = int(time.time())
    DAY = 86400
    con = sqlite3.connect(home / "state.db")
    runs = [
        ("run-pp-1", "pp_class", "published", 1.00, now - DAY * 2),
        ("run-pp-2", "pp_class", "published", 1.00, now - DAY * 3),
        ("run-pp-3", "pp_class", "published", 1.00, now - DAY * 4),
        ("run-pp-4", "pp_class", "published", 1.00, now - DAY * 5),
        ("run-pp-fail", "pp_class", "failed", 10.00, now - DAY * 6),
    ]
    for rid, cls, status, cost, ts in runs:
        con.execute("INSERT INTO task_runs (id, task_class, status, cost_usd, kickoff_path, "
                    "created_at, updated_at, ended_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (rid, cls, status, cost, "kickoffs/auto/test.md", ts, ts, ts))
    con.commit()
    con.close()
    page = learn.build(home, "overview", {})
    table = _section(page, "Outcomes by task class")
    pp = next(r for r in table["rows"] if r["cells"][0]["t"] == "pp_class")
    # cells[4] is cost / pass; cell is a dict with .t
    cost_text = pp["cells"][4]["t"]
    assert cost_text.startswith("$3.50"), cost_text


def test_overview_delta_pass_and_cost_have_independent_colours(home: Path) -> None:
    """Regression: Δ pass and Δ cost used one shared trend flag, so a -10pt
    pass drop rendered green whenever cost fell. Each cell now colours from
    its own metric; the fixture below drops pass by 10 pts while cutting
    cost/pass by 20%, so Δ pass must be red and Δ cost must be green.
    """
    now = int(time.time())
    DAY = 86400
    con = sqlite3.connect(home / "state.db")
    runs = [
        # Current window: 7 pass / 10 = 70% (yellow). Total cost $7, cost/pass $1.00.
        ("run-tc-1", "trend_class", "published", 1.00, now - DAY * 2),
        ("run-tc-2", "trend_class", "published", 1.00, now - DAY * 3),
        ("run-tc-3", "trend_class", "published", 1.00, now - DAY * 4),
        ("run-tc-4", "trend_class", "published", 1.00, now - DAY * 5),
        ("run-tc-5", "trend_class", "published", 1.00, now - DAY * 6),
        ("run-tc-6", "trend_class", "published", 1.00, now - DAY * 7),
        ("run-tc-7", "trend_class", "published", 1.00, now - DAY * 8),
        ("run-tc-8", "trend_class", "failed", 0.00, now - DAY * 9),
        ("run-tc-9", "trend_class", "failed", 0.00, now - DAY * 10),
        ("run-tc-10", "trend_class", "failed", 0.00, now - DAY * 11),
        # Baseline window: 8 pass / 10 = 80% (green). Total cost $10, cost/pass $1.25.
        ("run-tc-b1", "trend_class", "published", 1.00, now - DAY * 30),
        ("run-tc-b2", "trend_class", "published", 1.00, now - DAY * 31),
        ("run-tc-b3", "trend_class", "published", 1.00, now - DAY * 32),
        ("run-tc-b4", "trend_class", "published", 1.00, now - DAY * 33),
        ("run-tc-b5", "trend_class", "published", 1.00, now - DAY * 34),
        ("run-tc-b6", "trend_class", "published", 1.00, now - DAY * 35),
        ("run-tc-b7", "trend_class", "published", 1.00, now - DAY * 36),
        ("run-tc-b8", "trend_class", "published", 1.00, now - DAY * 37),
        ("run-tc-b9", "trend_class", "failed", 1.00, now - DAY * 38),
        ("run-tc-b10", "trend_class", "failed", 1.00, now - DAY * 39),
    ]
    for rid, cls, status, cost, ts in runs:
        con.execute("INSERT INTO task_runs (id, task_class, status, cost_usd, kickoff_path, "
                    "created_at, updated_at, ended_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (rid, cls, status, cost, "kickoffs/auto/test.md", ts, ts, ts))
    con.commit()
    con.close()
    page = learn.build(home, "overview", {})
    table = _section(page, "Outcomes by task class")
    tc = next(r for r in table["rows"] if r["cells"][0]["t"] == "trend_class")
    # Δ pass = 70 - 80 = -10pt → red (≤-5 threshold).
    assert tc["cells"][3]["t"] == "-10pt"
    assert tc["cells"][3]["c"] == "red"
    # Δ cost = (1.00 - 1.25) / 1.25 = -20% → green (≤-15 threshold).
    # Under the prior bug the shared trend flag would have painted Δ pass green.
    assert tc["cells"][5]["t"] == "-20%"
    assert tc["cells"][5]["c"] == "green"


def test_overview_n_less_than_five_suppresses_rate(home: Path) -> None:
    now = int(time.time())
    con = sqlite3.connect(home / "state.db")
    # Only 2 published runs for `tiny_class`.
    for i in range(2):
        con.execute("INSERT INTO task_runs (id, task_class, status, cost_usd, kickoff_path, "
                    "created_at, updated_at, ended_at) VALUES (?, 'tiny_class', 'published', 1.0, "
                    "'kickoffs/auto/test.md', ?, ?, ?)",
                    (f"run-tiny-{i}", now - 86400 * (i + 1), now - 86400 * (i + 1),
                     now - 86400 * (i + 1)))
    con.commit()
    con.close()
    page = learn.build(home, "overview", {})
    table = _section(page, "Outcomes by task class")
    tiny = next(r for r in table["rows"] if r["cells"][0]["t"] == "tiny_class")
    assert tiny["cells"][1]["t"] == "2"
    assert tiny["cells"][2]["t"] == "— (n<5)"
    assert tiny["cells"][2]["c"] == "sub"


def test_overview_stuck_runs_not_counted_in_pass_rate(home: Path) -> None:
    """A stuck run is shown in the stuck column, never the rate numerator/denominator."""
    now = int(time.time())
    con = sqlite3.connect(home / "state.db")
    # 5 published + 1 stuck — rate must reflect 5/5 published.
    for i in range(5):
        con.execute("INSERT INTO task_runs (id, task_class, status, cost_usd, kickoff_path, "
                    "created_at, updated_at, ended_at) VALUES (?, 'sticky', 'published', 1.0, "
                    "'kickoffs/auto/test.md', ?, ?, ?)",
                    (f"run-good-{i}", now - 86400 * (i + 1), now - 86400 * (i + 1),
                     now - 86400 * (i + 1)))
    con.execute("INSERT INTO task_runs (id, task_class, status, cost_usd, kickoff_path, "
                "created_at, updated_at, ended_at) VALUES ('run-stuck', 'sticky', 'executing', 0.0, "
                "'kickoffs/auto/test.md', ?, ?, 0)",
                (now - 86400 * 2, now - 86400 * 2))
    con.commit()
    con.close()
    page = learn.build(home, "overview", {})
    table = _section(page, "Outcomes by task class")
    sticky = next(r for r in table["rows"] if r["cells"][0]["t"] == "sticky")
    assert sticky["cells"][1]["t"] == "5"   # terminal runs only
    assert sticky["cells"][6]["t"] == "1"   # the stuck one
    assert sticky["cells"][2]["t"].startswith("100")   # 5/5


def test_overview_class_detail_opens_last_ten_runs(home: Path) -> None:
    now = int(time.time())
    con = sqlite3.connect(home / "state.db")
    for i in range(12):
        status = "failed" if i % 3 == 0 else "published"
        con.execute("INSERT INTO task_runs (id, task_class, status, cost_usd, kickoff_path, "
                    "created_at, updated_at, ended_at, verdict) VALUES (?, 'framework_edit', ?, ?, "
                    "'kickoffs/auto/test.md', ?, ?, ?, ?)",
                    (f"run-fe-{i:02d}", status, 1.0, now - 86400 * (i + 1),
                     now - 86400 * (i + 1), now - 86400 * (i + 1),
                     "REQUEST_CHANGES" if status == "failed" else None))
    con.commit()
    con.close()
    page = learn.build(home, "overview", {"cls": "framework_edit"})
    detail = _section(page, "framework_edit · last 10 finished runs")
    assert detail["head"] == ["status", "run", "cost", "age", "failure"]
    assert len(detail["rows"]) == 10
    # Newest first — first row is the most recent.
    assert detail["rows"][0]["cells"][1]["t"] == "run-fe-00"
    # All failure cells surface the verdict fallback.
    for row in detail["rows"]:
        assert row["cells"][4]["t"] in {"REQUEST_CHANGES", "—"}


def test_overview_class_selection_marks_row(home: Path) -> None:
    now = int(time.time())
    con = sqlite3.connect(home / "state.db")
    for i in range(6):
        con.execute("INSERT INTO task_runs (id, task_class, status, cost_usd, kickoff_path, "
                    "created_at, updated_at, ended_at) VALUES (?, 'sel_class', 'published', 1.0, "
                    "'kickoffs/auto/test.md', ?, ?, ?)",
                    (f"run-sel-{i}", now - 86400 * (i + 1), now - 86400 * (i + 1),
                     now - 86400 * (i + 1)))
    con.commit()
    con.close()
    page = learn.build(home, "overview", {"cls": "sel_class"})
    table = _section(page, "Outcomes by task class")
    selected = [r for r in table["rows"] if r.get("sel")]
    assert len(selected) == 1
    assert selected[0]["cells"][0]["t"] == "sel_class"
    # And the click action re-sets the cls arg.
    rows_do = {r["cells"][0]["t"]: r["do"] for r in table["rows"]}
    assert rows_do["sel_class"] == {"set": {"cls": "sel_class"}}


def test_overview_empty_state(home: Path) -> None:
    page = learn.build(home, "overview", {})
    table = _section(page, "Outcomes by task class")
    # No runs → empty state row.
    assert "No finished runs" in table["rows"][0]["cells"][0]["t"]


def test_lessons_tab(seeded: Path) -> None:
    page = learn.build(seeded, "lessons", {})
    assert [s["title"] for s in page["sections"]] == ["Gradients", "Patterns", "Failure modes"]
    grads = _section(page, "Gradients")
    assert grads["head"] == ["gradient", "task class", "conf", "injected"]
    assert [c["t"] for c in grads["rows"][0]["cells"]] == [
        "verifier.code_fix · checks unclear", "code_fix", "0.84", "—"]
    fm = _section(page, "Failure modes")["items"][0]
    assert fm["t"] == "dispatch_error · execute" and fm["m"] == "✗"
    assert fm["sub"].startswith("3 times in 3 runs")


def test_memory_tab(seeded: Path) -> None:
    page = learn.build(seeded, "memory", {})
    titles = [s["title"] for s in page["sections"]]
    # P7b: legacy Namespaces + lifecycle kv are gone; three new sections ship instead.
    assert "Namespaces · state.db" not in titles
    assert "Semantic memory lifecycle" not in titles
    assert "Preferences & constraints" in titles
    assert "Lane fit by task class" in titles
    assert "Memories to review" in titles
    # The Idea tree moved out of memory; it lives in improve now.
    assert not any(s["title"].startswith("Idea tree") for s in page["sections"])


def test_improve_tab(seeded: Path) -> None:
    page = learn.build(seeded, "improve", {})
    titles = [s["title"] for s in page["sections"]]
    assert "Loop" in titles
    assert any(t.startswith("Candidate ") for t in titles), titles
    assert "Self-improve ledger" in titles
    assert "Health probes" in titles
    assert any(t.startswith("Idea tree") for t in titles), titles
    assert "Training data export · TraceOtter" in titles
    loop = {i["label"]: i["sub"] for i in _section(page, "Loop")["items"]}
    assert loop["reflect"] == "1 gradients this week"
    assert loop["promote"] == "promoted"
    cand = _section(page, "Candidate cand-1 vs v0.4.1")
    assert {i["k"]: i["v"] for i in cand["items"]}["Utility"] == "0.86 → 0.91"
    ledger = _section(page, "Self-improve ledger")
    assert [c["t"] for c in ledger["rows"][0]["cells"]] == ["3", "success", "inject g-121", "$1.50", "10m 00s"]
    assert ledger["actions"][0]["do"] is None          # started from a terminal, not a button
    # Idea tree moved here.
    tree = _section(page, "Idea tree · dark mode support")
    lines = [line["t"] for line in tree["lines"]]
    assert lines[0] == "root  dark mode support"


def test_traceotter_not_run_is_distinct_from_empty(home: Path) -> None:
    page = learn.build(home, "improve", {})
    corpus = _section(page, "Corpus")
    assert corpus["items"][0]["t"] == "TraceOtter has not run here"


def test_traceotter_corpus(home: Path) -> None:
    d = home / "traceotter"
    d.mkdir()
    (d / "report.json").write_text(json.dumps({"episodes": 2, "skills": 1,
                                               "llamafactory": {"examples": 1}}), encoding="utf-8")
    (d / "episodes.jsonl").write_text(
        json.dumps({"labels": {"shouldImitate": True, "failureModes": ["timeout"]}}) + "\n"
        + json.dumps({"labels": {"shouldImitate": False}}) + "\n", encoding="utf-8")
    page = learn.build(home, "improve", {})
    section = _section(page, "Training data export · TraceOtter")
    flow = [i["sub"] for i in section["items"]]
    assert flow[0] == "2 trajectories" and flow[1] == "1 worth imitating"
    corpus = {i["k"]: i["v"] for i in _section(page, "Corpus")["items"]}
    assert corpus["Episodes"] == "2" and corpus["Skills"] == "1" and corpus["SFT examples"] == "1"
    assert _section(page, "Failure modes the distiller saw")["items"][0]["t"] == "timeout"


def test_bugs_old_key_maps_to_overview(seeded: Path) -> None:
    """``?tab=bugs`` used to render the legacy Bug reports section; now it
    lands on the Overview tab where the open-bugs chip lives."""
    page = learn.build(seeded, "bugs", {})
    assert page["tab"] == "overview"
    needs = _section(page, "Needs you")
    chip_titles = [c["t"] for c in needs["items"]]
    assert any("open bug report" in t for t in chip_titles)


def test_entrypoint(seeded: Path) -> None:
    start = time.monotonic()
    for tab in ("overview", "lessons", "memory", "improve"):
        assert build_page(seeded, "learn", tab, {})["ok"]
    assert time.monotonic() - start < 5