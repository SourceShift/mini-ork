"""IDE page ``learn/overview`` — extra Overview features (P5b).

Covers:
  * retry_hint-driven failure column (``_hint_or_none`` is monkeypatched so the
    test doesn't need real run dirs / node_attempts rows)
  * Reap button on "Needs you" when stuck > 0
  * "Learning loop health" section (alarm state, empty state, healthy stage)
  * "Recent learning events" section (per-kind rendering + missing tables)
"""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any

import pytest

from mini_ork.ide_pages import learn
from mini_ork.stores import migrate as mig

REPO = Path(__file__).resolve().parents[2]


# ── fixtures ──────────────────────────────────────────────────────────────


@pytest.fixture
def home(tmp_path: Path) -> Path:
    h = tmp_path / "proj" / ".mini-ork"
    h.mkdir(parents=True)
    rc, _out, err = mig.init_db(db=str(h / "state.db"), root=str(REPO))
    assert rc == 0, err
    return h


def _section(page: dict, title: str) -> dict:
    return next(s for s in page["sections"] if s["title"] == title)


def _ensure_cols(con: sqlite3.Connection, table: str, columns: dict[str, str]) -> None:
    """Idempotent ADD COLUMN — sqlite ALTER rejects duplicates."""
    have = {r[1] for r in con.execute(f"PRAGMA table_info({table})")}
    for name, decl in columns.items():
        if name not in have:
            con.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")


def _seed_failed_runs(home: Path, *, count: int, now: int, status: str = "failed",
                      verdict: str | None = "REQUEST_CHANGES") -> list[str]:
    con = sqlite3.connect(home / "state.db")
    ids: list[str] = []
    for i in range(count):
        run_id = f"run-fe-{i:02d}"
        ids.append(run_id)
        con.execute(
            "INSERT INTO task_runs (id, task_class, status, cost_usd, kickoff_path, "
            "created_at, updated_at, ended_at, verdict) "
            "VALUES (?, 'framework_edit', ?, ?, 'kickoffs/auto/test.md', ?, ?, ?, ?)",
            (run_id, status, 1.0, now - 86400 * (i + 1),
             now - 86400 * (i + 1), now - 86400 * (i + 1), verdict),
        )
    con.commit()
    con.close()
    return ids


# ── class detail failure column (retry hint) ──────────────────────────────


def test_class_detail_with_hint_renders_reviewable_node_and_retryable(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed run with a hint: failure cell carries 'node: summary' and the
    retry flag (`retryable` in green, `no` in muted) is appended to the cell."""
    now = int(time.time())
    _seed_failed_runs(home, count=2, now=now)

    def fake_hint(home: Path, run_id: str) -> dict[str, Any] | None:
        return {
            "version": 1,
            "run_id": run_id,
            "failed_node": "reviewer",
            "retryable": run_id == "run-fe-00",  # first row retryable, second not
            "strategy": "resume",
            "from_node": "reviewer",
            "needs_change": {
                "kind": "code",
                "summary": "The change was judged wrong — it needs a revision",
                "detail": "stub",
                "evidence": "",
            },
            "notes": [],
            "command": "",
            "computed_at": "2026-10-07T00:00:00Z",
        }

    monkeypatch.setattr(
        "mini_ork.ide_pages.learn.overview._hint_or_none", fake_hint)

    page = learn.build(home, "overview", {"cls": "framework_edit"})
    detail = _section(page, "framework_edit · last 10 finished runs")
    assert len(detail["rows"]) == 2
    first = detail["rows"][0]["cells"][4]["t"]
    second = detail["rows"][1]["cells"][4]["t"]
    # First (newest) row: retryable.
    assert first.startswith("reviewer: The change was judged wrong")
    assert first.endswith("· retryable")
    # Second row: hint also returns but retryable=False → "· no".
    assert second.endswith("· no")


def test_class_detail_without_hint_falls_back_to_verdict(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When retry_hint returns None, the failure cell keeps the verdict fallback."""
    now = int(time.time())
    _seed_failed_runs(home, count=1, now=now, verdict="REQUEST_CHANGES")
    monkeypatch.setattr(
        "mini_ork.ide_pages.learn.overview._hint_or_none",
        lambda home, run_id: None)

    page = learn.build(home, "overview", {"cls": "framework_edit"})
    detail = _section(page, "framework_edit · last 10 finished runs")
    assert detail["rows"][0]["cells"][4]["t"] == "REQUEST_CHANGES"


def test_class_detail_hint_raising_does_not_break_page(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A raising retry_hint falls back to the verdict chain; the page builds."""
    now = int(time.time())
    _seed_failed_runs(home, count=1, now=now, verdict="REQUEST_CHANGES")

    def raising(home: Path, run_id: str) -> dict[str, Any] | None:
        raise RuntimeError("simulated hint outage")

    monkeypatch.setattr(
        "mini_ork.ide_pages.learn.overview._hint_or_none", raising)

    page = learn.build(home, "overview", {"cls": "framework_edit"})
    detail = _section(page, "framework_edit · last 10 finished runs")
    assert detail["rows"][0]["cells"][4]["t"] == "REQUEST_CHANGES"


def test_class_detail_failed_run_no_hint_no_memory_no_verdict(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed run with no hint, no failure_memory row and no verdict renders
    'no reason recorded' — never '—' (the bug this phase exists to fix)."""
    now = int(time.time())
    _seed_failed_runs(home, count=1, now=now, verdict=None)
    monkeypatch.setattr(
        "mini_ork.ide_pages.learn.overview._hint_or_none",
        lambda home, run_id: None)

    page = learn.build(home, "overview", {"cls": "framework_edit"})
    detail = _section(page, "framework_edit · last 10 finished runs")
    assert detail["rows"][0]["cells"][4]["t"] == "no reason recorded"


# ── reap button ───────────────────────────────────────────────────────────


def test_needs_you_reap_button_appears_only_when_stuck(home: Path) -> None:
    """No stuck runs → no Reap button."""
    page = learn.build(home, "overview", {})
    needs = _section(page, "Needs you")
    labels = [a.get("label") for a in needs.get("actions", [])]
    assert "Reap stuck runs" not in labels

    # Seed one stuck run (>24h executing).
    now = int(time.time())
    con = sqlite3.connect(home / "state.db")
    con.execute(
        "INSERT INTO task_runs (id, task_class, status, cost_usd, kickoff_path, "
        "created_at, updated_at) VALUES ('stuck-1', 'framework_edit', 'executing', 0.0, "
        "'k.md', ?, ?)",
        (now - 2 * 86400, now - 2 * 86400),
    )
    con.commit()
    con.close()

    page = learn.build(home, "overview", {})
    needs = _section(page, "Needs you")
    actions = needs.get("actions", [])
    btn = next(a for a in actions if a.get("label") == "Reap stuck runs")
    assert btn["kind"] == "warn"
    cli = btn["do"]["cli"]
    assert cli == ["reap", "--stale-after", "24h"]
    # The confirm text names the count and the cut-off — exact format.
    assert "1 run" in btn["do"]["confirm"]
    assert "24 h" in btn["do"]["confirm"]


# ── learning loop health ──────────────────────────────────────────────────


def test_learning_loop_health_empty_when_no_table(home: Path) -> None:
    """No learning_pass_stats table → the empty state with the staged note."""
    page = learn.build(home, "overview", {})
    health = _section(page, "Learning loop health")
    assert health["type"] == "list"
    assert health["items"][0]["t"] == "Reflect has not reported yet"


def test_learning_loop_health_alarm_state_with_cost_circuit(home: Path) -> None:
    """3 zero-output passes + cost_circuit_open → alarm state, budget text,
    stalled chip in Needs you. The chip is driven through the real wiring
    (ledger.stage_health → _stalled_stage_count → chip) — no monkeypatch."""
    now = int(time.time())
    con = sqlite3.connect(home / "state.db")
    con.execute(
        "CREATE TABLE IF NOT EXISTS learning_pass_stats ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, pass_id TEXT NOT NULL, ts INTEGER NOT NULL, "
        "stage TEXT NOT NULL, inputs INTEGER, outputs INTEGER, failures INTEGER, "
        "lane TEXT, last_error TEXT)"
    )
    # Three zero-output rows for pattern_induction, the newest carries cost_circuit_open.
    for i, ts in enumerate([now - 3, now - 2, now - 1]):
        con.execute(
            "INSERT INTO learning_pass_stats (pass_id, ts, stage, inputs, outputs, "
            "failures, lane, last_error) VALUES (?, ?, 'pattern_induction', 5, 0, ?, 'glm-4.6', ?)",
            (f"p{i}", ts, 1 if i == 2 else 0, "cost_circuit_open" if i == 2 else None),
        )
    # One healthy stage for contrast (still produces something).
    con.execute(
        "INSERT INTO learning_pass_stats (pass_id, ts, stage, inputs, outputs, failures, lane) "
        "VALUES ('q0', ?, 'reflection', 3, 2, 0, 'glm-4.6')",
        (now,),
    )
    con.commit()
    con.close()

    page = learn.build(home, "overview", {})
    health = _section(page, "Learning loop health")
    assert health["type"] == "table"
    # pattern_induction row: state text is the alarm phrase, red.
    pi_row = next(r for r in health["rows"]
                  if r["cells"][0]["t"] == "pattern_induction")
    assert pi_row["cells"][5]["t"] == "⚠ produced nothing for 3 passes"
    assert pi_row["cells"][5]["c"] == "red"
    # The follow-up error row carries the last_error + budget hint.
    err_row = health["rows"][health["rows"].index(pi_row) + 1]
    assert "cost_circuit_open" in err_row["cells"][0]["t"]
    assert "MO_DAILY_BUDGET_USD" in err_row["cells"][0]["t"]

    # reflection row: ok, green.
    refl_row = next(r for r in health["rows"]
                    if r["cells"][0]["t"] == "reflection")
    assert refl_row["cells"][5]["t"] == "ok"
    assert refl_row["cells"][5]["c"] == "green"

    # Needs-you carries the real wired-up "1 learning stage stalled" chip.
    needs = _section(page, "Needs you")
    chip_titles = [c["t"] for c in needs["items"]]
    assert "1 learning stage stalled" in chip_titles


def test_learning_loop_health_healthy_stage_only(home: Path) -> None:
    """A stage with outputs > 0 in its newest pass → ok (green), no sub row."""
    now = int(time.time())
    con = sqlite3.connect(home / "state.db")
    con.execute(
        "CREATE TABLE IF NOT EXISTS learning_pass_stats ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, pass_id TEXT NOT NULL, ts INTEGER NOT NULL, "
        "stage TEXT NOT NULL, inputs INTEGER, outputs INTEGER, failures INTEGER, lane TEXT, last_error TEXT)"
    )
    con.execute(
        "INSERT INTO learning_pass_stats (pass_id, ts, stage, inputs, outputs, failures, lane) "
        "VALUES ('p0', ?, 'reflection', 3, 2, 0, 'glm-4.6')",
        (now,),
    )
    con.commit()
    con.close()

    page = learn.build(home, "overview", {})
    health = _section(page, "Learning loop health")
    assert health["type"] == "table"
    assert len(health["rows"]) == 1  # no follow-up error row
    assert health["rows"][0]["cells"][5]["t"] == "ok"


# ── recent learning events ────────────────────────────────────────────────


def test_recent_learning_events_one_of_each_kind(home: Path) -> None:
    """One of each kind in the last 14 days appears, older rows excluded,
    AND events with interleaved timestamps come out in global newest-first
    order with the exact title sequence asserted (kickoff change 1: at most
    12, sorted across kinds)."""
    now = int(time.time())
    DAY = 86400
    con = sqlite3.connect(home / "state.db")

    # Distinct epoch offsets per kind so the merge ordering is observable.
    bug_ts = now - 6 * 3600     # ~6 hours ago — newest of the four
    mem_ts = now - 1 * DAY      # 2nd-newest
    pat_ts = now - 2 * DAY      # 3rd-newest
    pro_ts = now - 4 * DAY      # oldest of the four

    # emergent_patterns: one recent approved, one old (30d ago).
    con.execute(
        "INSERT INTO emergent_patterns (pattern_id, cluster_label, member_item_ids_json, "
        "feature_set_json, strength_score, status, detected_at, resolved_at, lesson_text) "
        "VALUES ('p1', 'cluster-a', '[]', '[]', 0.9, 'approved', ?, ?, 'lesson text a')",
        (pat_ts, pat_ts),
    )
    con.execute(
        "INSERT INTO emergent_patterns (pattern_id, cluster_label, member_item_ids_json, "
        "feature_set_json, strength_score, status, detected_at, resolved_at, lesson_text) "
        "VALUES ('p-old', 'old', '[]', '[]', 0.9, 'approved', ?, ?, 'old lesson')",
        (now - 30 * DAY, now - 30 * DAY),
    )

    # semantic_memory: one recent retired.
    _ensure_cols(con, "semantic_memory", {
        "retired_at": "REAL NOT NULL DEFAULT 0",
        "retire_reason": "TEXT NOT NULL DEFAULT ''",
    })
    con.execute(
        "INSERT INTO semantic_memory (scope, text, embedding, created_at, uses, wins, "
        "retired_at, retire_reason) VALUES ('code_fix', 'old memory', x'00', ?, 10, 1, ?, 'low_utility')",
        (mem_ts, mem_ts),
    )

    # promotion_records: one recent.
    con.execute(
        "INSERT INTO promotion_records (promotion_id, candidate_id, from_version_id, "
        "to_version_id, utility_before, utility_after, decision, decided_at, decided_by) "
        "VALUES ('pr1', 'cand-1', 'v0', 'v1', 0.5, 0.7, 'promoted', ?, 'gate')",
        (time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(pro_ts)),),
    )

    # bug_reports: one learning-role within 14 days, one non-learning (excluded),
    # one older than 14 days (excluded).
    _ensure_cols(con, "bug_reports", {"frequency": "INTEGER NOT NULL DEFAULT 1"})
    con.execute(
        "INSERT INTO bug_reports (fingerprint, agent_role, title, observed_in, confidence, "
        "status, first_seen_at, last_seen_at, updated_at, frequency) "
        "VALUES ('fp-l', 'learning', 'mini-ork issue A', 'k', 0.9, 'open', ?, ?, ?, 3)",
        (bug_ts, bug_ts, bug_ts),
    )
    con.execute(
        "INSERT INTO bug_reports (fingerprint, agent_role, title, observed_in, confidence, "
        "status, first_seen_at, last_seen_at, updated_at) "
        "VALUES ('fp-sched', 'scheduler', 'other', 'k', 0.9, 'open', ?, ?, ?)",
        (now - 86400, now - 86400, now - 86400),
    )
    con.execute(
        "INSERT INTO bug_reports (fingerprint, agent_role, title, observed_in, confidence, "
        "status, first_seen_at, last_seen_at, updated_at) "
        "VALUES ('fp-old', 'learning', 'old issue', 'k', 0.9, 'open', ?, ?, ?)",
        (now - 30 * DAY, now - 30 * DAY, now - 30 * DAY),
    )
    con.commit()
    con.close()

    page = learn.build(home, "overview", {})
    events_sec = _section(page, "Recent learning events")
    assert events_sec["type"] == "list"
    items = events_sec["items"]
    titles = " | ".join(i["t"] for i in items)
    assert "Pattern approved" in titles
    assert "Memory retired" in titles
    assert "Promotion" in titles
    assert "mini-ork issue: mini-ork issue A" in titles
    # The non-learning bug and the old bug are filtered out.
    assert "other" not in titles
    assert "old issue" not in titles

    # Global newest-first across kinds: bug → memory → pattern → promotion,
    # matching the seed offsets above. The exact title order is the assertion —
    # a monotonic-only check would let two kinds swap silently.
    expected = [
        "mini-ork issue: mini-ork issue A",
        "Memory retired: old memory",
        "Pattern approved: lesson text a",
        "Promotion promoted: cand-1",
    ]
    assert [i["t"] for i in items] == expected

    # At-most-12 — fewer is fine, more than 12 is the kickoff violation.
    assert len(items) <= 12


def test_recent_learning_events_empty_when_no_data(home: Path) -> None:
    page = learn.build(home, "overview", {})
    events_sec = _section(page, "Recent learning events")
    assert events_sec["type"] == "list"
    assert events_sec["items"][0]["t"] == "No learning events in 14 days"


def test_recent_learning_events_tolerate_missing_tables(home: Path) -> None:
    """All four source tables absent → the empty state still renders."""
    con = sqlite3.connect(home / "state.db")
    for tbl in ("emergent_patterns", "semantic_memory",
                "promotion_records", "bug_reports"):
        con.execute(f"DROP TABLE IF EXISTS {tbl}")
    con.commit()
    con.close()

    page = learn.build(home, "overview", {})
    events_sec = _section(page, "Recent learning events")
    assert events_sec["type"] == "list"
    assert events_sec["items"][0]["t"] == "No learning events in 14 days"


def test_iso_to_epoch_accepts_fractional_seconds_and_is_utc(home: Path) -> None:
    """promotion_gate writes decided_at with microseconds
    (strftime '%Y-%m-%dT%H:%M:%fZ'); the converter must accept the fraction
    and treat the 'Z' as UTC so promotions order correctly against the epoch
    columns of the other three kinds."""
    from mini_ork.ide_pages.learn.overview import _iso_to_epoch

    # Fractional seconds must not fall through to 0.
    assert _iso_to_epoch("2026-10-07T10:00:00.123Z") != 0
    # Whole-second form parses to the exact UTC instant (calendar.timegm, not
    # time.mktime — mktime shifts by the local UTC offset).
    assert abs(_iso_to_epoch("2026-10-07T10:00:00Z") - 1791367200) < 2
    # Bad / empty strings keep the stable-sort contract: 0.
    assert _iso_to_epoch("") == 0
    assert _iso_to_epoch("not-a-timestamp") == 0


# ── page-shape sanity ─────────────────────────────────────────────────────


def test_overview_page_includes_all_five_sections(home: Path) -> None:
    page = learn.build(home, "overview", {})
    titles = [s["title"] for s in page["sections"]]
    assert "Needs you" in titles
    assert "Outcomes by task class" in titles
    assert "Learning loop health" in titles
    assert "Recent learning events" in titles
    # The class-detail section only renders when cls is set — verify the
    # absence here.
    assert "framework_edit · last 10 finished runs" not in titles
    # JSON round-trip — the IDE renderer receives exactly this shape.
    json.dumps(page)