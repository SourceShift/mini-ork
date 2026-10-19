"""The v2 run page's Checks tab and its tab badges (kickoff rp-c9c12).

The Checks tab renders one self-explaining row per verification level and the
tab bar carries ``count``/``tone`` badges instead of folding counts into the
label. Fixtures are hermetic ``.mini-ork`` homes built the way the other run
page tests build them: a ``task_runs`` row plus files under ``runs/<id>``.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from mini_ork.ide_pages import build_page
from mini_ork.ide_pages import run as run_page
from mini_ork.stores import migrate as mig

REPO = Path(__file__).resolve().parents[2]
TONES = {"red", "yellow", "green", "blue", "sub"}
LEVELS = ("applies", "executes", "target", "preserve", "contract")
_DISPLAY = {"PROVEN": "PROVEN", "UNVERIFIED": "UNVERIFIED", "REFUTED": "FAILED"}

_REASONS = {
    "applies": "implementer-summary.json: the diff touches only files the kickoff named",
    "executes": "implementer-summary.json: py_compile clean on every changed file",
    "target": "verifier_test.json: target not proven",
    "preserve": "verifier_test.json: post_rc=0",
    "contract": "the kickoff's promised artifact exists and matches its contract",
}

_FAILED_LEVELS = {"applies": "PROVEN", "executes": "PROVEN", "target": "REFUTED",
                  "preserve": "UNVERIFIED", "contract": "n/a"}
_ALL_PROVEN = {name: "PROVEN" for name in LEVELS}


@pytest.fixture(autouse=True)
def _ide_spec2(monkeypatch):
    monkeypatch.setenv("MINI_ORK_IDE_SPEC", "2")


def _make_run(tmp_path: Path, *, run_id: str, status: str = "failed",
              levels: dict[str, str] | None = None,
              asks: list[str] | None = None,
              verifier: dict[str, object] | None = None,
              evidence_name: str | None = None) -> tuple[Path, Path]:
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    rc, _out, err = mig.init_db(db=str(home / "state.db"), root=str(REPO))
    assert rc == 0, err
    run_dir = home / "runs" / run_id
    (run_dir / "asks").mkdir(parents=True)
    import sqlite3

    con = sqlite3.connect(home / "state.db")
    now = 1_700_000_000
    con.execute(
        "INSERT INTO task_runs(id, task_class, recipe, kickoff_path, status,"
        " created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (run_id, "code_fix", "", f"runs/{run_id}/kickoff.md", status, now, now))
    con.commit()
    con.close()
    if levels is not None:
        (run_dir / "run-verdict.json").write_text(json.dumps({
            "levels": levels,
            "levels_reasons": {k: _REASONS.get(k, "fixture reason") for k in levels},
            "levels_required": ["applies", "executes", "target", "preserve"],
            "levels_ok": False,
            "levels_decision": "refute" if "REFUTED" in levels.values() else "publish",
        }), encoding="utf-8")
    for name, payload in (verifier or {}).items():
        (run_dir / name).write_text(json.dumps(payload), encoding="utf-8")
    if evidence_name is not None:
        (run_dir / "evidence").mkdir(exist_ok=True)
        (run_dir / "evidence" / evidence_name).write_text("evidence body\n", encoding="utf-8")
    for i, question in enumerate(asks or [], start=1):
        (run_dir / "asks" / f"ask-{i}.json").write_text(
            json.dumps({"ask_id": f"ask-{i}", "question": question}), encoding="utf-8")
    return home, run_dir


def _page(home: Path, tab: str, run_id: str) -> dict:
    page = build_page(home, "run", tab, {"run": run_id})
    assert page.get("ok") is True, page.get("error") or page.get("errors")
    return page


def _tab(page: dict, key: str) -> dict | None:
    for tab in page.get("tabs") or []:
        if tab.get("key") == key:
            return tab
    return None


def _count_int(count) -> int | None:
    if isinstance(count, bool) or count is None:
        return None
    if isinstance(count, int):
        return count
    if isinstance(count, str):
        digits = "".join(c for c in count if c.isdigit())
        return int(digits) if digits else None
    return None


def _sections(page: dict, kind: str, title: str) -> list[dict]:
    return [s for s in page.get("sections") or []
            if s.get("type") == kind and s.get("title") == title]


# ── the rows ─────────────────────────────────────────────────────────────────


def test_checks_tab_lists_every_level_with_state_and_plain_words(tmp_path: Path) -> None:
    home, _ = _make_run(tmp_path, run_id="run-checks-rows", levels=_FAILED_LEVELS)
    page = _page(home, "checks", "run-checks-rows")
    sections = _sections(page, "checks", "Verification levels")
    assert sections, [s.get("title") for s in page.get("sections") or []]
    rows = {row["name"]: row for row in sections[0]["rows"]}
    assert list(rows) == list(LEVELS)
    for level in LEVELS:
        row = rows[level]
        display = _DISPLAY.get(_FAILED_LEVELS[level])
        text = " ".join(str(v) for v in row.values())
        if display:
            assert display in text, f"{level}: no {display} state in {text!r}"
            others = [t for t in ("PROVEN", "UNVERIFIED", "FAILED") if t != display]
            assert not any(t in text for t in others), f"{level}: ambiguous state {text!r}"
        # Plain words beyond the level name and the state token.
        words = text.replace(level, " ")
        for token in ("PROVEN", "UNVERIFIED", "FAILED"):
            words = words.replace(token, " ")
        assert len(" ".join("".join(c if c.isalpha() else " " for c in words).split())) >= 20, (
            f"{level}: no plain words: {text!r}")


def test_checks_tab_click_through_to_deciding_evidence(tmp_path: Path) -> None:
    home, run_dir = _make_run(
        tmp_path, run_id="run-checks-evidence", levels=_FAILED_LEVELS,
        verifier={"verifier_page.json": {
            "verifier": "page", "pass": False,
            "checks": [{"name": "target row", "pass": False,
                        "expected": "a row", "actual": "no row"}],
            "evidence_path": None}},
        evidence_name="checks-page.log")
    envelope_path = run_dir / "verifier_page.json"
    envelope = json.loads(envelope_path.read_text(encoding="utf-8"))
    envelope["evidence_path"] = str(run_dir / "evidence" / "checks-page.log")
    envelope_path.write_text(json.dumps(envelope), encoding="utf-8")

    page = _page(home, "checks", "run-checks-evidence")
    blob = json.dumps(page.get("sections") or [], ensure_ascii=False)
    assert "checks-page.log" in blob
    actions = [d for d in _walk(page.get("sections") or [])
               if "do" in d and "checks-page.log" in " ".join(_strings(d))]
    assert actions, "no click-through action targets the deciding evidence file"


# ── the badges ───────────────────────────────────────────────────────────────


def test_failed_level_gives_red_checks_badge_with_failed_count(tmp_path: Path) -> None:
    home, _ = _make_run(tmp_path, run_id="run-checks-red", levels=_FAILED_LEVELS)
    page = _page(home, "dag", "run-checks-red")
    checks = _tab(page, "checks")
    assert checks is not None, [t.get("key") for t in page["tabs"]]
    assert checks["tone"] == "red"
    assert _count_int(checks["count"]) == 1


def test_all_proven_checks_badge_carries_proven_count(tmp_path: Path) -> None:
    home, _ = _make_run(tmp_path, run_id="run-checks-green", status="published",
                        levels=_ALL_PROVEN)
    page = _page(home, "dag", "run-checks-green")
    checks = _tab(page, "checks")
    assert checks is not None
    assert _count_int(checks["count"]) == 5
    assert checks.get("tone") != "red"


def test_two_open_asks_show_questions_count_2(tmp_path: Path) -> None:
    home, _ = _make_run(tmp_path, run_id="run-checks-parked", status="executing",
                        asks=["Keep the retry budget at 2?",
                              "Should the badge count levels or checks?"])
    page = _page(home, "dag", "run-checks-parked")
    carrying = [t for t in page["tabs"] if _count_int(t.get("count")) == 2]
    assert len(carrying) == 1, carrying
    assert carrying[0]["key"] == "dag"
    assert carrying[0]["tone"] == "yellow"


def test_no_asks_no_questions_badge(tmp_path: Path) -> None:
    home, _ = _make_run(tmp_path, run_id="run-checks-no-asks", levels=_FAILED_LEVELS)
    page = _page(home, "dag", "run-checks-no-asks")
    dag = _tab(page, "dag")
    assert dag is not None and not _count_int(dag.get("count"))
    assert dag.get("tone") == "sub"


def test_parked_run_badge_agrees_with_the_header_chip(tmp_path: Path) -> None:
    """The blocking case: parked adds no phantom counts to the DAG badge."""
    home, run_dir = _make_run(
        tmp_path, run_id="run-checks-parked-asks", status="reviewing",
        asks=["Approve this?", "Keep the retry budget at 2?"])
    (run_dir / ".review-pause").write_text("", encoding="utf-8")
    page = _page(home, "dag", "run-checks-parked-asks")
    dag = _tab(page, "dag")
    assert _count_int(dag.get("count")) == 2, dag
    assert dag.get("tone") == "yellow"
    # The chip beside the tab counts the same two asks — never three or four.
    chip_text = " ".join(str(c.get("t") or "") for c in page.get("chips") or [])
    assert "2 questions" in chip_text, chip_text


def test_parked_run_with_no_asks_keeps_the_attention_tone(tmp_path: Path) -> None:
    """Still waiting on the operator, but the count stays 0 to match the chip."""
    home, run_dir = _make_run(
        tmp_path, run_id="run-checks-parked-bare", status="reviewing")
    (run_dir / ".review-pause").write_text("", encoding="utf-8")
    page = _page(home, "dag", "run-checks-parked-bare")
    dag = _tab(page, "dag")
    assert _count_int(dag.get("count")) == 0, dag
    assert dag.get("tone") == "yellow"
    chip_text = " ".join(str(c.get("t") or "") for c in page.get("chips") or [])
    assert "question" not in chip_text, chip_text


def test_empty_tab_is_quiet_not_a_callout(tmp_path: Path) -> None:
    home, _ = _make_run(tmp_path, run_id="run-checks-empty")
    for key in ("learnings", "artifacts"):
        page = _page(home, key, "run-checks-empty")
        tab = _tab(page, key)
        assert tab is not None, f"{key} tab removed"
        assert _count_int(tab.get("count")) == 0, tab
        callouts = [s for s in page.get("sections") or [] if s.get("type") == "callout"]
        assert not callouts, f"{key}: bare callout rendered: {callouts[:1]}"


def test_badge_values_and_tones_are_contract_shaped(tmp_path: Path) -> None:
    failed_home, _ = _make_run(tmp_path, run_id="run-checks-shape-failed",
                               levels=_FAILED_LEVELS)
    parked_home, _ = _make_run(tmp_path, run_id="run-checks-shape-parked",
                               status="executing", asks=["First?", "Second?"])
    bare_home, _ = _make_run(tmp_path, run_id="run-checks-shape-bare")
    pages = [
        _page(failed_home, "checks", "run-checks-shape-failed"),
        _page(failed_home, "dag", "run-checks-shape-failed"),
        _page(parked_home, "dag", "run-checks-shape-parked"),
        _page(bare_home, "learnings", "run-checks-shape-bare"),
    ]
    for page in pages:
        for tab in page["tabs"]:
            assert isinstance(tab.get("key"), str) and tab["key"]
            assert isinstance(tab.get("label"), str) and tab["label"]
            if "count" in tab:
                assert isinstance(tab["count"], (str, int)) and not isinstance(tab["count"], bool)
            if "tone" in tab:
                assert tab["tone"] in TONES, tab
            assert "·" not in str(tab.get("label") or ""), tab


def test_plain_labels_and_all_tabs_survive(tmp_path: Path) -> None:
    """The count moved out of the label; no v2 tab was removed."""
    home, _ = _make_run(tmp_path, run_id="run-checks-labels", levels=_FAILED_LEVELS)
    page = _page(home, "checks", "run-checks-labels")
    keys = [t["key"] for t in page["tabs"]]
    for key in ("dag", "outcome", "checks", "kickoff", "agents",
                "learnings", "artifacts"):
        assert key in keys, keys
    # One tab, one vector: ``verification`` is a retired alias for ``checks``.
    assert "verification" not in keys
    assert _tab(page, "checks")["label"] == "Checks"
    assert all(key in keys for key, _ in run_page.V2_TABS)


# ── helpers ─────────────────────────────────────────────────────────────────


def _walk(obj):
    if isinstance(obj, dict):
        yield obj
        for value in obj.values():
            yield from _walk(value)
    elif isinstance(obj, list):
        for value in obj:
            yield from _walk(value)


def _strings(obj) -> list[str]:
    out: list[str] = []

    def collect(value) -> None:
        if isinstance(value, dict):
            for item in value.values():
                collect(item)
        elif isinstance(value, list):
            for item in value:
                collect(item)
        elif isinstance(value, str):
            out.append(value)

    collect(obj)
    return out
