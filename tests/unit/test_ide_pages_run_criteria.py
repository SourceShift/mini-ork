"""The run page's success-criteria checklist.

A finished run states its outcome, but the kickoff's criteria are only prose in
the hero. ``_criteria_checklist`` joins each criterion to the evidence that
decided it — a verdict entry, a failed check, or a PROVEN level — and the
outcome tab renders the rows as "Did it meet the criteria".

Every fixture is a synthetic :class:`run_page.Run` over a ``tmp_path`` run dir:
no fleet DB, no network, no credentials.
"""
from __future__ import annotations

import json
from pathlib import Path

from mini_ork.ide_pages import outcome as outcome_mod
from mini_ork.ide_pages import run as run_page

SECTION_TITLE = "Did it meet the criteria"


def _fixture_run(tmp_path: Path, profile: dict, files: dict[str, str] | None = None,
                 status: str = "published") -> run_page.Run:
    run_dir = tmp_path / "run-fx"
    run_dir.mkdir()
    (run_dir / "run_profile.json").write_text(json.dumps(profile), encoding="utf-8")
    for name, text in (files or {}).items():
        path = run_dir / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    node = run_page.Node(id="impl", type="implementer", state="done")
    return run_page.Run(
        id="run-fx", home=tmp_path, run_dir=run_dir,
        row={"status": status},
        card={"status": status, "recipe": "framework-edit", "steps": []},
        nodes=[node], cols=[["impl"]], calls=[], recipe_dir=None,
        workspace=None, edges=[],
    )


def _render(run: run_page.Run) -> tuple[dict, list[dict]]:
    outcome = outcome_mod.resolve(run)
    return outcome, run_page._outcome_tab(run, outcome)


def _checklist(sections: list[dict]) -> list[dict]:
    return [s for s in sections if str(s.get("title") or "") == SECTION_TITLE]


def _assert_rows(rows: list[dict], criteria: list[str]) -> None:
    assert isinstance(rows, list)
    assert len(rows) == len(criteria)
    for row, criterion in zip(rows, criteria):
        assert row.get("c") == criterion, row
        assert row.get("state") in ("met", "unmet", "unknown"), row
        why = row.get("why")
        assert isinstance(why, str) and why.strip(), row
        assert len(why) <= 120, row
        do = row.get("do")
        assert do is None or (isinstance(do, dict) and ("path" in do or "page" in do)), row


def _evidence(run: run_page.Run, name: str, text: str) -> str:
    path = run.run_dir / name
    path.write_text(text, encoding="utf-8")
    return str(path)


def test_verdict_marks_one_of_three_met(tmp_path: Path) -> None:
    criteria = ["The bundle installs without errors",
                "The docs build renders",
                "No new dependencies"]
    run = _fixture_run(tmp_path, {"user_goal": "g", "success_criteria": criteria})
    evidence = _evidence(run, "evidence-bundle.txt", "bundle installs without errors")
    (run.run_dir / "run-verdict.json").write_text(json.dumps({
        "criteria": [{"criterion": criteria[0], "met": True, "evidence": evidence}],
    }), encoding="utf-8")

    outcome, sections = _render(run)
    rows = outcome.get("criteria")
    assert rows, "outcome['criteria'] must be exposed"
    _assert_rows(rows, criteria)
    assert [r["state"] for r in rows] == ["met", "unknown", "unknown"]
    assert rows[0].get("do"), "the met row carries a click-through"
    assert rows[1].get("do") is None and rows[2].get("do") is None
    assert len(_checklist(sections)) == 1


def test_failed_check_naming_criterion_is_unmet(tmp_path: Path) -> None:
    criteria = ["The lint tier stays clean", "No new dependencies"]
    run = _fixture_run(tmp_path, {"user_goal": "g", "success_criteria": criteria})
    (run.run_dir / "verifier_lint.json").write_text(json.dumps({
        "verifier": "lint", "pass": False,
        "checks": [{"name": "lint tier stays clean — blocking tier",
                    "pass": False, "expected": "0 findings", "actual": "3 findings"}],
    }), encoding="utf-8")

    outcome, _sections = _render(run)
    rows = outcome["criteria"]
    _assert_rows(rows, criteria)
    assert rows[0]["state"] == "unmet"
    assert rows[0].get("do"), "the unmet row links the check that decided it"
    assert rows[1]["state"] == "unknown", "a check that does not name it decides nothing"


def test_proven_level_evidence_names_criterion_is_met(tmp_path: Path) -> None:
    criteria = ["The pytest suite passes", "No new dependencies"]
    run = _fixture_run(tmp_path, {"user_goal": "g", "success_criteria": criteria})
    evidence = _evidence(run, "evidence-test.txt",
                         "the pytest suite passes on 3.11 and 3.12")
    (run.run_dir / "verifier_test.json").write_text(json.dumps({
        "verifier": "test", "pass": True, "evidence_path": evidence,
        "checks": [{"name": "pytest suite passes", "pass": True}],
    }), encoding="utf-8")
    (run.run_dir / "run-verdict.json").write_text(json.dumps({
        "levels": {"applies": "PROVEN"},
        "levels_reasons": {"applies": "verifier_test.json: the pytest suite passes"},
    }), encoding="utf-8")

    outcome, _sections = _render(run)
    rows = outcome["criteria"]
    _assert_rows(rows, criteria)
    assert rows[0]["state"] == "met"
    assert rows[0].get("do")
    assert rows[1]["state"] == "unknown"


def test_no_criteria_no_section_no_key(tmp_path: Path) -> None:
    run = _fixture_run(tmp_path, {"user_goal": "g"})
    outcome, sections = _render(run)
    assert not _checklist(sections)
    assert "criteria" not in outcome
    assert not run_page._criteria_checklist(run, outcome)


def test_unreadable_verdict_all_unknown_and_page_ok(tmp_path: Path) -> None:
    criteria = ["The bundle installs without errors", "The docs build renders"]
    run = _fixture_run(tmp_path, {"user_goal": "g", "success_criteria": criteria})
    (run.run_dir / "run-verdict.json").write_text("{oops — not json", encoding="utf-8")

    outcome, sections = _render(run)
    rows = outcome["criteria"]
    _assert_rows(rows, criteria)
    assert [r["state"] for r in rows] == ["unknown", "unknown"]
    assert _checklist(sections), "the section survives an unreadable verdict"
    page = run_page._build_v2(run, "outcome", {"run": run.id})
    assert page.get("ok") is True


def test_finished_run_alone_never_met(tmp_path: Path) -> None:
    criteria = ["The bundle installs without errors", "The docs build renders"]
    run = _fixture_run(tmp_path, {"user_goal": "g", "success_criteria": criteria},
                       status="published")
    outcome, _sections = _render(run)
    assert outcome.get("state") == "done"
    rows = outcome["criteria"]
    _assert_rows(rows, criteria)
    assert all(row["state"] != "met" for row in rows)


def test_criteria_results_state_shape_and_decision_order(tmp_path: Path) -> None:
    criteria = ["The bundle installs without errors",
                "The docs build renders",
                "The pytest suite passes"]
    run = _fixture_run(tmp_path, {"user_goal": "g", "success_criteria": criteria})
    evidence = _evidence(run, "evidence-docs.txt", "docs build renders")
    (run.run_dir / "framework-edit.verdict.json").write_text(json.dumps({
        "criteria_results": [
            {"criterion": criteria[1], "state": "met", "evidence": evidence},
            {"criterion": criteria[2], "state": "unmet"},
        ],
    }), encoding="utf-8")

    outcome, _sections = _render(run)
    rows = outcome["criteria"]
    _assert_rows(rows, criteria)
    # ``criteria_results`` carries a state word instead of ``met``; a criterion
    # the file never names stays unknown.
    assert [r["state"] for r in rows] == ["unknown", "met", "unmet"]
    assert rows[1].get("do")


def test_same_capped_list_as_the_hero(tmp_path: Path) -> None:
    criteria = [f"criterion number {i} stays checkable" for i in range(10)]
    run = _fixture_run(tmp_path, {"user_goal": "g", "success_criteria": criteria})
    outcome, sections = _render(run)
    hero = [s for s in sections
            if str(s.get("title") or "") == "What this run is for"]
    assert hero
    rows = outcome["criteria"]
    assert len(rows) == len(hero[0]["criteria"])
    assert [r["c"] for r in rows] == list(hero[0]["criteria"])
