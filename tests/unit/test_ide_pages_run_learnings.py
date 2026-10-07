"""``_learnings_tab`` detail surfaces — see ``kickoffs/auto/learn-run-tab.md``.

Reuses the home / seed helpers from ``test_ide_pages_run`` rather than re-implementing
the fixture, so the join to ``execution_traces`` exercises the same DB shape live runs do.
"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

from mini_ork.ide_pages import build_page

# `tests/` has no `__init__.py` so `tests.unit.test_ide_pages_run` won't resolve
# via the project-root pythonpath; loading the sibling module by its file path
# keeps the import local to this test file (and outside the run.py scope claim).
_UNIT_DIR = Path(__file__).resolve().parent
if str(_UNIT_DIR) not in sys.path:
    sys.path.insert(0, str(_UNIT_DIR))
from test_ide_pages_run import (  # noqa: E402
    RUN, T0, _section, _seed, home as _home,
)
# Re-export the imported fixture under its canonical name so pytest's fixture
# discovery sees `home` in this module's namespace — without re-defining it,
# which would shadow the import and confuse ruff's F811.
home = _home


def _section_items(page: dict, kind: str, title: str) -> list[dict]:
    return _section(page, kind, title)["items"]


def test_each_run_sees_only_its_own_gradient(home: Path) -> None:
    """Two same-class runs, each with its own trace + gradient, must show only its own.

    Confirms the run-scoped join: a gradient whose evidence trace is owned by
    another run must NOT bleed into this run's "Produced by the run".
    """
    # Production stores text run ids in BOTH `task_runs.id` and
    # `execution_traces.run_id` (e.g. ``run-<ts>-<pid>``). The earlier
    # CAST(? AS INTEGER) hid the bug because both ids collapsed to 0;
    # binding text run ids here exercises the real isolation property.
    run_a_id = "run-a-20261007000001"
    run_b_id = "run-b-20261007000002"
    run_a = home / "runs" / run_a_id
    run_a.mkdir(parents=True)
    run_b = home / "runs" / run_b_id
    run_b.mkdir(parents=True)
    con = sqlite3.connect(home / "state.db")
    for run_id, run_dir_path, trace_id, evidence, signal, fix, target, conf in [
        (run_a_id, run_a, "tr-researcher-prior_art_lens-aaa111",
         "tr-researcher-prior_art_lens-aaa111",
         "the rubric underweighted the contract terms",
         "boost the contract rubric weight",
         "workflow.node.prior_art_lens", 0.81),
        (run_b_id, run_b, "tr-implementer-implementer-bbb222",
         "tr-implementer-implementer-bbb222",
         "the implementer ignored the schema drift",
         "rerun schema-aware lint before applying",
         "workflow.node.implementer", 0.66),
    ]:
        con.execute(
            "INSERT INTO task_runs (id, recipe, status, cost_usd, created_at, updated_at, "
            "ended_at, task_class, kickoff_path, workflow_version, trace_id) "
            "VALUES (?, 'demo-recipe', 'published', 0.5, ?, ?, ?, 'demo', ?, 'latest', ?)",
            (run_id, T0, T0 + 120, T0 + 100, str(home / "kickoffs" / "demo.md"), trace_id),
        )
        con.execute(
            "INSERT INTO execution_traces (trace_id, run_id, status, task_class) "
            "VALUES (?, ?, ?, ?)",
            (evidence, run_id, "success", "demo"),
        )
        con.execute(
            "INSERT INTO gradient_records (gradient_id, target, signal, suggested_change, "
            "evidence, confidence, created_at, task_class) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (f"gr-{run_id}", target, signal, fix, evidence, conf, T0 + 110, "demo"),
        )
        (run_dir_path / "context-pack.json").write_text(json.dumps(
            {"prior_similar_runs": [], "known_failure_modes": []}))
    con.commit()
    con.close()

    page_a = build_page(home, "run", "learnings", {"run": run_a_id})
    items_a = _section_items(page_a, "list", "Produced by the run")
    assert len(items_a) == 1, items_a
    assert items_a[0]["t"] == "the rubric underweighted the contract terms"
    assert items_a[0]["m"] == "✦"
    sub_a = items_a[0]["sub"]
    assert "fix: boost the contract rubric weight" in sub_a
    # Node id lifted from `tr-researcher-prior_art_lens-aaa111` (not the raw trace).
    assert " · prior_art_lens · " in sub_a
    assert "confidence 0.81" in sub_a

    page_b = build_page(home, "run", "learnings", {"run": run_b_id})
    items_b = _section_items(page_b, "list", "Produced by the run")
    assert len(items_b) == 1, items_b
    assert items_b[0]["t"] == "the implementer ignored the schema drift"
    assert " · implementer · " in items_b[0]["sub"]
    assert "confidence 0.66" in items_b[0]["sub"]


def test_failure_modes_detail_caps_at_eight_with_open_overflow(home: Path) -> None:
    """10 failure modes in the pack → first list has 8 items + a "+2 more" with Open."""
    _seed(home)
    pack_path = home / "runs" / RUN / "context-pack.json"
    failure_modes = [
        {
            "signal": f"signal number {i} — the verifier tripped on a stale fixture",
            "suggested_change": f"fix number {i}: reset the fixture before each case",
            "target": "workflow.node.test",
            "confidence": round(0.5 + i * 0.04, 2),
        }
        for i in range(10)
    ]
    pack = json.loads(pack_path.read_text(encoding="utf-8"))
    pack["known_failure_modes"] = failure_modes
    pack_path.write_text(json.dumps(pack))

    page = build_page(home, "run", "learnings", {"run": RUN})
    detail = _section(page, "list", "Learned failure modes · 10")
    items = detail["items"]
    # 8 detail rows + 1 "+2 more" row.
    assert len(items) == 9, [i["t"] for i in items]
    # Titles are signals, not storage keys / gradient ids.
    assert "gradient_records" not in items[0]["t"]
    assert items[0]["t"].startswith("signal number 0")
    assert items[7]["t"].startswith("signal number 7")
    overflow = items[8]
    assert overflow["t"] == "+2 more"
    # "+N more" must carry an Open action that jumps to the pack file.
    acts = overflow.get("acts") or []
    assert len(acts) == 1
    assert acts[0].get("label") == "Open"
    assert acts[0].get("kind") == "ghost"
    open_do = acts[0]["do"]
    assert open_do.get("path") == str(pack_path)


def test_verified_patterns_use_cluster_label_when_no_lesson(home: Path) -> None:
    """Without lesson_text the title falls back to cluster_label and the sub
    signals 'no authored lesson'. With lesson_text, the title IS the lesson.
    """
    _seed(home)
    pack_path = home / "runs" / RUN / "context-pack.json"
    pack = json.loads(pack_path.read_text(encoding="utf-8"))
    pack["verified_emergent_patterns"] = [
        {
            "cite": "execution_traces/pat-abc123",
            "cluster_label": "warm-cache before run",
            "lesson_text": "",
            "strength_score": 7,
        },
        {
            "cite": "execution_traces/pat-def456",
            "cluster_label": "schema-lint first",
            "lesson_text": "lint the schema before applying any migration",
            "strength_score": 4,
        },
    ]
    pack_path.write_text(json.dumps(pack))

    page = build_page(home, "run", "learnings", {"run": RUN})
    detail = _section(page, "list", "Verified patterns · 2")
    items = detail["items"]
    assert items[0]["t"] == "warm-cache before run"
    assert "pattern pat-abc123" in items[0]["sub"]
    assert "strength 7" in items[0]["sub"]
    assert "no authored lesson (frequency only)" in items[0]["sub"]
    assert items[1]["t"] == "lint the schema before applying any migration"
    assert "pattern pat-def456" in items[1]["sub"]
    assert "strength 4" in items[1]["sub"]
    assert "no authored lesson" not in items[1]["sub"]


def test_available_summary_never_leaks_cite_or_storage_keys(home: Path) -> None:
    """The "Available to the run" summary must not surface gradient_records/... cites.

    Previously the summary sub read ``f"{n} items · e.g. <cite>"``; the new
    behaviour is ``"{n} item(s)"`` only — no gradient id, no path, no cite.
    """
    _seed(home)
    pack_path = home / "runs" / RUN / "context-pack.json"
    pack = json.loads(pack_path.read_text(encoding="utf-8"))
    # Force every section non-empty with explicit storage-key-shaped cites.
    pack["prior_similar_runs"] = [
        {"cite": "gradient_records/cross_class:workflow.node.implementer",
         "trace_id": "tr-researcher-prior_art_lens-7cc5b827",
         "status": "success", "cost_usd": 0.42, "duration_ms": 19000,
         "created_at": "2026-09-30T12:34:56Z"},
    ]
    pack["known_failure_modes"] = [
        {"signal": "verifier tripped", "suggested_change": "rerun the suite",
         "target": "workflow.node.test", "confidence": 0.71},
    ]
    pack["similar_lessons"] = [
        {"title": "lesson text", "suggested_fix": "fix text", "score": 0.93},
    ]
    pack["verified_emergent_patterns"] = [
        {"cite": "gradient_records/cluster:schema-drift", "cluster_label": "label",
         "lesson_text": "lesson", "strength_score": 5},
    ]
    pack_path.write_text(json.dumps(pack))

    page = build_page(home, "run", "learnings", {"run": RUN})
    available = _section(page, "list", "Available to the run")
    summary_subs = [i["sub"] for i in available["items"]]
    for sub in summary_subs:
        assert "gradient_records" not in sub, sub
        assert "execution_traces/" not in sub, sub
    # Each non-empty section is counted, no cite or path in the sub text.
    by_label = {i["t"]: i["sub"] for i in available["items"]}
    assert by_label["Prior same-class runs"] == "1 item"
    assert by_label["Learned failure modes"] == "1 item"
    assert by_label["Similar lessons"] == "1 item"
    assert by_label["Verified patterns"] == "1 item"


def test_missing_execution_traces_does_not_break_produced(home: Path) -> None:
    """Tolerate a missing execution_traces table — empty list, no exception.

    Guards the build return value when entry doesn't crash: a database without
    the table must degrade to the same 'Nothing recorded' empty state a fresh
    DB shows, instead of raising.
    """
    # Fresh DB with no execution_traces; seed task_runs + gradient only.
    h = home
    run_dir = h / "runs" / RUN
    run_dir.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(h / "state.db")
    # Confirm the migration created the table; drop it to simulate a legacy DB.
    con.execute("DROP TABLE IF EXISTS execution_traces")
    con.execute(
        "INSERT INTO task_runs (id, recipe, status, cost_usd, created_at, updated_at, "
        "ended_at, task_class, kickoff_path, workflow_version, trace_id) "
        "VALUES (?, 'demo-recipe', 'published', 0.5, ?, ?, ?, 'demo', ?, 'latest', ?)",
        (RUN, T0, T0 + 120, T0 + 100, str(h / "kickoffs" / "demo.md"), "tr-demo-1"),
    )
    con.execute(
        "INSERT INTO gradient_records (gradient_id, target, signal, suggested_change, "
        "evidence, confidence, created_at, task_class) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        ("gr-demo1", "workflow.node.implementer", "the implementer skipped the test",
         "run the test first", "tr-x", 0.7, T0 + 115, "demo"),
    )
    con.commit()
    con.close()
    (run_dir / "context-pack.json").write_text(json.dumps({"prior_similar_runs": []}))

    page = build_page(h, "run", "learnings", {"run": RUN})
    produced = _section(page, "list", "Produced by the run")
    assert produced["items"][0]["t"] == "Nothing recorded"