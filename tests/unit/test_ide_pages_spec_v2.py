"""``mini_ork.ide_pages.spec`` v2 — ``ide_level()`` and the ten new sections.

The helpers are the contract between the Python page builders and the IDE, so
each test pins the documented keys, the computed pieces (``checks`` summary,
``column.count``) and a full-page JSON round-trip.
"""

from __future__ import annotations

import json

import pytest

from mini_ork.ide_pages import spec as S

# Every section produced by :func:`_section` carries this fixed layout.
_LAYOUT_KEYS = ("type", "title", "note", "actions", "full")


def _assert_layout(section: dict) -> None:
    for key in _LAYOUT_KEYS:
        assert key in section, (key, section)
    assert isinstance(section["type"], str) and section["type"]
    assert isinstance(section["actions"], list)


# ── ide_level ─────────────────────────────────────────────────────────────


def test_ide_level_unset_is_one(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MINI_ORK_IDE_SPEC", raising=False)
    assert S.ide_level() == 1


@pytest.mark.parametrize("value,expected", [("2", 2), ("1", 1), ("x", 1), ("", 1),
                                            ("0", 1), ("-4", 1), ("2.5", 1)])
def test_ide_level_reads_env(monkeypatch: pytest.MonkeyPatch, value: str, expected: int) -> None:
    monkeypatch.setenv("MINI_ORK_IDE_SPEC", value)
    assert S.ide_level() == expected


def test_ide_level_is_read_on_each_call(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MINI_ORK_IDE_SPEC", raising=False)
    assert S.ide_level() == 1
    monkeypatch.setenv("MINI_ORK_IDE_SPEC", "2")
    assert S.ide_level() == 2  # not cached from the first call


# ── hero / triage / callout ───────────────────────────────────────────────


def test_hero_keys_and_full_default() -> None:
    sec = S.hero("Build the fix", goal="ship it", criteria=["a", "b"],
                 pill=("running", "blue"), meta=[S.meta_item("cost", "sub")])
    _assert_layout(sec)
    assert sec["type"] == "hero"
    assert sec["full"] is True
    assert sec["goal"] == "ship it"
    assert sec["criteria"] == ["a", "b"]
    assert sec["pill"] == {"t": "running", "c": "blue"}
    assert sec["meta"] == [{"t": "cost", "c": "sub", "mono": False}]


def test_hero_pill_accepts_str_and_defaults_to_none() -> None:
    assert S.hero("t", pill="done")["pill"] == {"t": "done", "c": "sub"}
    assert S.hero("t")["pill"] is None


def test_triage_keys_and_full_default() -> None:
    sec = S.triage("3 nodes need you", tone="yellow", icon="!", detail="since 09:20",
                   counts=[("2", "red"), {"t": "1", "c": "green"}],
                   actions=[S.btn("Inspect")])
    _assert_layout(sec)
    assert sec["type"] == "triage"
    assert sec["title"] == ""  # triage carries no title
    assert sec["full"] is True
    assert sec["text"] == "3 nodes need you"
    assert (sec["tone"], sec["icon"], sec["detail"]) == ("yellow", "!", "since 09:20")
    assert sec["counts"] == [{"t": "2", "c": "red"}, {"t": "1", "c": "green"}]
    assert sec["menu"] == []
    assert sec["actions"][0]["label"] == "Inspect"


def test_callout_keys_and_actions_field() -> None:
    sec = S.callout("Heads up", "**markdown**", tone="orange", actions=[S.btn("Retry")])
    _assert_layout(sec)
    assert sec["type"] == "callout"
    assert sec["text_md"] == "**markdown**"
    assert sec["tone"] == "orange"
    assert sec["actions"][0]["label"] == "Retry"
    assert sec["full"] is False  # callout is not full-width by default


def test_section_helpers_thread_layout_options() -> None:
    sec = S.callout("c", col=2, row_span=3, note="n")
    assert sec["col"] == 2 and sec["row_span"] == 3 and sec["note"] == "n"


# ── story steps + blocks ──────────────────────────────────────────────────


def test_story_step_accepts_tuple_or_str_headline() -> None:
    tupled = S.story_step("s1", "Run planner", kind="planner", state="done", lane="glm",
                          headline=("working", "blue"))
    as_str = S.story_step("s2", "Run impl", headline="done")
    none = S.story_step("s3", "Run test")
    for step in (tupled, as_str, none):
        assert {"id", "title", "kind", "state", "lane", "model", "headline", "meta",
                "dur", "cost", "open", "do", "body"} <= set(step)
    assert tupled["headline"] == {"t": "working", "c": "blue"}
    assert as_str["headline"] == {"t": "done", "c": "sub"}
    assert none["headline"] is None


def test_block_helpers_return_their_kinds() -> None:
    assert S.block_md("hi") == {"kind": "md", "text": "hi"}

    lines = S.block_lines(["plain", ("red line", "red")])
    assert lines["kind"] == "lines"
    assert lines["lines"] == [{"t": "plain", "c": "body"}, {"t": "red line", "c": "red"}]

    files = S.block_files([S.file_entry("a.py")], diff="d", diff_note="n", commits=[])
    assert files["kind"] == "files"
    assert files["diff"] == "d" and files["diff_note"] == "n" and len(files["files"]) == 1

    findings = S.block_findings([S.finding("x")], verdict=("block", "red"), reasons=["r"])
    assert findings["kind"] == "findings"
    assert findings["verdict"] == {"t": "block", "c": "red"}
    assert findings["reasons"] == ["r"]

    checks = S.block_checks([S.check_row("lint", "pass")])
    assert checks["kind"] == "checks"
    assert checks["summary"]["passing"] == 1


def test_story_section_wraps_steps() -> None:
    sec = S.story("Run story", [S.story_step("s1", "one")])
    _assert_layout(sec)
    assert sec["type"] == "story"
    assert [s["id"] for s in sec["steps"]] == ["s1"]


# ── files / findings ──────────────────────────────────────────────────────


def test_file_entry_keys() -> None:
    entry = S.file_entry("a.py", abs="/x/a.py", status="A", added=3, removed=1)
    assert entry == {"path": "a.py", "abs": "/x/a.py", "status": "A", "added": 3, "removed": 1}
    assert S.file_entry("b.py") == {"path": "b.py", "abs": "", "status": "M",
                                    "added": 0, "removed": 0}


def test_files_section_keys() -> None:
    sec = S.files("Files", [S.file_entry("a.py", added=2, removed=1)], diff="d", commits=[])
    _assert_layout(sec)
    assert sec["type"] == "files"
    assert sec["files"][0]["added"] == 2
    assert sec["diff"] == "d" and sec["diff_note"] == "" and sec["commits"] == []


def test_finding_keys() -> None:
    f = S.finding("boom", severity="high", file="a.py", line=3, abs="/x/a.py",
                  snippet="s", source="review")
    assert f == {"issue": "boom", "severity": "high", "file": "a.py", "line": 3,
                 "abs": "/x/a.py", "snippet": "s", "source": "review"}


def test_findings_section_verdict_tuple_or_str() -> None:
    tupled = S.findings("Findings", [S.finding("i", severity="high")],
                        verdict=("block", "red"), reasons=["r1"])
    _assert_layout(tupled)
    assert tupled["type"] == "findings"
    assert tupled["verdict"] == {"t": "block", "c": "red"}
    assert tupled["reasons"] == ["r1"]
    assert S.findings("Findings", [], verdict="block")["verdict"] == {"t": "block", "c": "sub"}
    assert S.findings("Findings", [])["verdict"] is None


# ── checks ────────────────────────────────────────────────────────────────


def test_check_row_keys_and_log_lines() -> None:
    row = S.check_row("lint", "pass", detail="ok", log=["one", ("two", "red")])
    assert row == {"name": "lint", "state": "pass", "detail": "ok",
                   "log": [{"t": "one", "c": "body"}, {"t": "two", "c": "red"}], "do": None}


def test_checks_summary_is_computed_from_row_states() -> None:
    sec = S.checks("Checks", [
        S.check_row("a", "pass"), S.check_row("b", "done"), S.check_row("c", "ok"),
        S.check_row("d", "fail"), S.check_row("e", "failed"), S.check_row("f", "error"),
        S.check_row("g", "pending"), S.check_row("h", "running"),
        S.check_row("i", "skipped"), S.check_row("j", "needs_you"),
    ])
    _assert_layout(sec)
    assert sec["type"] == "checks"
    assert sec["summary"] == {"passing": 3, "failing": 3, "pending": 2, "na": 2}


def test_checks_summary_override_wins() -> None:
    sec = S.checks("Checks", [S.check_row("a", "pass")],
                   summary={"passing": 9, "failing": 0, "pending": 0, "na": 0})
    assert sec["summary"]["passing"] == 9


# ── agents / composer / columns ───────────────────────────────────────────


def test_agent_row_keys() -> None:
    row = S.agent_row("implementer", "done", lane="worker", model="m", step="3",
                      last="edited a.py", cost="$0.07", dur="12s")
    assert row == {"id": "implementer", "state": "done", "lane": "worker", "model": "m",
                   "step": "3", "last": "edited a.py", "cost": "$0.07", "dur": "12s", "do": None}


def test_agents_section_keys() -> None:
    sec = S.agents("Agents", [S.agent_row("implementer", "done")])
    _assert_layout(sec)
    assert sec["type"] == "agents" and len(sec["rows"]) == 1


def test_composer_keys() -> None:
    sec = S.composer("Ask the run…", ["board", "node"])
    _assert_layout(sec)
    assert sec["type"] == "composer"
    assert sec["placeholder"] == "Ask the run…"
    assert sec["cli"] == ["board", "node"]


def test_column_count_matches_cards() -> None:
    col = S.column("left", [{"a": 1}, {"b": 2}, {"c": 3}])
    assert col["count"] == 3
    assert col["cards"] == [{"a": 1}, {"b": 2}, {"c": 3}]
    assert col["c"] == "sub"
    assert S.column("empty", [])["count"] == 0


def test_columns_section_keys() -> None:
    sec = S.columns("Columns", [S.column("left", [1]), S.column("right", [2, 3])])
    _assert_layout(sec)
    assert sec["type"] == "columns"
    assert [c["count"] for c in sec["cols"]] == [1, 2]


# ── whole-page JSON round-trip ────────────────────────────────────────────


def test_json_round_trip_page_with_every_new_section() -> None:
    sections = [
        S.hero("hero", goal="g", criteria=["c"], pill="running", meta=[S.meta_item("m")]),
        S.triage("triage", counts=[("1", "red")], actions=[S.btn("go")]),
        S.callout("callout", "md"),
        S.story("story", [S.story_step("s", "t", headline=("x", "blue"), body=[
            S.block_md("m"), S.block_lines(["l"]), S.block_files([S.file_entry("a")]),
            S.block_findings([S.finding("i")]), S.block_checks([S.check_row("n", "pass")]),
        ])]),
        S.files("files", [S.file_entry("a.py")], diff="d", commits=[]),
        S.findings("findings", [S.finding("issue", severity="critical")],
                   verdict=("block", "red"), reasons=["r"]),
        S.checks("checks", [S.check_row("lint", "pass")]),
        S.agents("agents", [S.agent_row("impl", "done")]),
        S.composer("Ask", ["board"]),
        S.columns("columns", [S.column("left", [1])]),
    ]
    page = S.page("run", "Run", sections=sections)
    blob = json.dumps(page)
    restored = json.loads(blob)
    assert [s["type"] for s in restored["sections"]] == [
        "hero", "triage", "callout", "story", "files", "findings", "checks",
        "agents", "composer", "columns"]
