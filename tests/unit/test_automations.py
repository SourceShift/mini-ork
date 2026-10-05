"""Tests for ``mini_ork/automations.py`` (Zed S6a).

Coverage targets (kickoff §"Tests"):

- cron parsing (valid forms, every invalid form with its message)
- ``next_fire`` across day/month boundaries and day-of-week
- ``describe``
- ``due`` fires once per matching minute and not for a past window
- ``add`` / ``update`` / ``remove`` validation and atomic writes
- ``fire`` worktree mode in a real temp git repo (workspace created,
  ``MO_TARGET_CWD`` and ``MO_AUTOMATION_ID`` passed)
- ``fire`` in-place mode, non-git project → in place
- ``tick`` fires only due + enabled ones and records them and the log line
- ``install_scheduler`` / ``uninstall_scheduler`` / ``scheduler_status`` on
  macOS (plist content) and Linux (crontab text in/out) via the seams
- CLI ``list --json``, ``add``, ``run``, usage errors
- Native dispatch exact-set includes ``automations`` (covered in
  ``test_native_dispatch_py.py``).
"""
from __future__ import annotations

import datetime as _dt
import importlib
import json
import subprocess
from pathlib import Path
from typing import Any
from unittest import mock

import pytest

# Force the test to import the in-repo module, not an older installed copy
# from a sibling venv. The repo provides ``sycop`` from the venv via
# PYTHONPATH=. which pytest already does (see pyproject.toml), so this
# import is canonical.
from mini_ork import automations as auto


@pytest.fixture(autouse=True)
def _hermetic_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Clear env vars that would redirect ``_project_for`` away from tmp_path.

    The parent shell exports ``MINI_ORK_HOME`` and ``MINI_ORK_PROJECT_HOME``
    pointing at the live mini-ork checkout; the tests want a hermetic tmp
    project.
    """
    for var in ("MINI_ORK_PROJECT_HOME", "MINI_ORK_HOME"):
        monkeypatch.delenv(var, raising=False)


# ── cron parsing ─────────────────────────────────────────────────────────────


class TestParseCron:
    """Every valid + invalid form listed in the kickoff §Store spec."""

    @pytest.mark.parametrize(
        "expr",
        [
            "* * * * *",
            "0 0 * * *",
            "0 9 * * 1-5",
            "*/15 * * * *",
            "0,15,30,45 * * * *",
            "0 9 * * 0,6",
            "5 0 * 1 *",
            "0 0 1 * *",
            "0 0 * 1 *",
            "1-5/2 * * * *",  # stepped range
            "0 9 * * 7",      # 7 == Sunday (normalised to 0)
            "*/5 */2 * * *",
        ],
    )
    def test_valid_forms(self, expr: str) -> None:
        spec = auto.parse_cron(expr)
        assert spec.original == expr
        assert spec.minute
        assert spec.hour
        assert spec.dom
        assert spec.month
        assert spec.dow

    def test_normalises_dow_seven_to_sunday(self) -> None:
        spec = auto.parse_cron("0 9 * * 7")
        # Python weekday(): Sunday = 6.
        assert 6 in spec.dow
        assert 7 not in spec.dow

    def test_dow_zero_and_seven_combined_dedup(self) -> None:
        spec = auto.parse_cron("0 9 * * 0,7")
        # Both 0 and 7 mean Sunday in cron → Python weekday() 6.
        assert spec.dow == [6]

    def test_dow_monday_through_friday(self) -> None:
        spec = auto.parse_cron("0 9 * * 1-5")
        # cron 1..5 → Mon..Fri → Python weekday() 0..4.
        assert spec.dow == [0, 1, 2, 3, 4]

    @pytest.mark.parametrize(
        "expr,fragment",
        [
            ("", "empty"),
            ("* * * *", "5 fields"),
            ("* * * * * *", "5 fields"),
            ("bad", "5 fields"),
            ("60 0 * * *", "field '60' matches no values"),
            ("0 24 * * *", "field '24' matches no values"),
            ("0 0 32 * *", "field '32' matches no values"),
            ("0 0 * 13 *", "field '13' matches no values"),
            ("0 0 * * 8", "field '8' matches no values"),
            ("*/0 * * * *", "step must be positive"),
            ("0/5 * * * *", "invalid step"),
            ("foo 0 * * *", "invalid value"),
            ("10-5 * * * *", "range start > end"),
        ],
    )
    def test_invalid_forms_raise(self, expr: str, fragment: str) -> None:
        with pytest.raises(ValueError) as exc:
            auto.parse_cron(expr)
        assert fragment in str(exc.value).lower(), (expr, str(exc.value))

    @pytest.mark.parametrize(
        "expr",
        [
            "0-5 0 * * *",
            "1-10 * * * *",
        ],
    )
    def test_valid_ranges_parse(self, expr: str) -> None:
        spec = auto.parse_cron(expr)
        assert spec.original == expr


# ── next_fire ────────────────────────────────────────────────────────────────


class TestNextFire:
    def test_every_minute(self) -> None:
        spec = auto.parse_cron("* * * * *")
        base = _dt.datetime(2026, 1, 1, 12, 0, 0)
        nxt = auto.next_fire(spec, base)
        assert nxt == _dt.datetime(2026, 1, 1, 12, 1, 0)

    def test_every_15_minutes(self) -> None:
        spec = auto.parse_cron("*/15 * * * *")
        base = _dt.datetime(2026, 1, 1, 12, 3, 30)
        nxt = auto.next_fire(spec, base)
        assert nxt == _dt.datetime(2026, 1, 1, 12, 15, 0)

    def test_daily_at_9(self) -> None:
        spec = auto.parse_cron("0 9 * * *")
        base = _dt.datetime(2026, 1, 1, 0, 0, 0)
        nxt = auto.next_fire(spec, base)
        assert nxt == _dt.datetime(2026, 1, 1, 9, 0, 0)

    def test_weekday_at_9(self) -> None:
        # 0 9 * * 1-5 → Monday..Friday at 09:00
        spec = auto.parse_cron("0 9 * * 1-5")
        # 2026-01-03 is a Saturday.
        base = _dt.datetime(2026, 1, 3, 9, 0, 0)
        nxt = auto.next_fire(spec, base)
        assert nxt == _dt.datetime(2026, 1, 5, 9, 0, 0)  # Monday

    def test_day_of_month(self) -> None:
        spec = auto.parse_cron("0 0 1 * *")
        base = _dt.datetime(2026, 1, 1, 0, 0, 0)
        nxt = auto.next_fire(spec, base)
        assert nxt == _dt.datetime(2026, 2, 1, 0, 0, 0)

    def test_month_transition(self) -> None:
        spec = auto.parse_cron("0 0 1 1 *")  # Jan 1 at midnight
        base = _dt.datetime(2026, 1, 1, 0, 0, 0)
        nxt = auto.next_fire(spec, base)
        assert nxt == _dt.datetime(2027, 1, 1, 0, 0, 0)

    def test_dow_and_dom_or_semantics(self) -> None:
        # When both dom and dow are restricted, cron OR's them. Here we
        # schedule Monday-or-the-15th at midnight.
        spec = auto.parse_cron("0 0 15 * 1")  # dow normalised, monday=0 in cron spec... wait, monday=1
        base = _dt.datetime(2026, 1, 1, 0, 0, 0)
        nxt = auto.next_fire(spec, base)
        # 2026-01-01 is Thursday — dom=15 not match; dow=1 means Monday.
        assert nxt == _dt.datetime(2026, 1, 5, 0, 0)  # the first Monday, not Monday the 15th

    def test_dom_and_dow_either_matches(self) -> None:
        # "noon on the 13th or any Friday": 2026-10-02 is a Friday, the 13th is a Tuesday.
        spec = auto.parse_cron("0 12 13 * 5")
        after = _dt.datetime(2026, 10, 1)
        first = auto.next_fire(spec, after)
        assert first == _dt.datetime(2026, 10, 2, 12, 0)
        fires = []
        cur = after
        for _ in range(4):
            cur = auto.next_fire(spec, cur)
            fires.append(cur.day)
        assert fires == [2, 9, 13, 16]

    def test_one_restricted_day_field_still_ands(self) -> None:
        # dow '*' → only dom constrains; dom '*' → only dow constrains.
        assert auto.next_fire(auto.parse_cron("0 0 13 * *"), _dt.datetime(2026, 10, 1)).day == 13
        assert auto.next_fire(auto.parse_cron("0 0 * * 2"), _dt.datetime(2026, 10, 1)).day == 6

    def test_unreachable_raises(self) -> None:
        # Construct an expression that resolves to nothing within 5 years.
        # Month 13 would normally be invalid; use a malformed-but-parseable
        # expression that nonetheless yields no match.
        spec = auto.parse_cron("0 0 30 2 *")  # Feb 30 → never
        with pytest.raises(ValueError, match="no fire time"):
            auto.next_fire(spec, _dt.datetime(2026, 1, 1))


# ── describe ─────────────────────────────────────────────────────────────────


class TestDescribe:
    @pytest.mark.parametrize(
        "expr,expected",
        [
            ("*/15 * * * *", "every 15 minutes"),
            ("*/5 * * * *", "every 5 minutes"),
            ("0 9 * * 1-5", "every weekday at 09:00"),
            ("30 14 * * 1-5", "every weekday at 14:30"),
            ("0 0 * * *", "every day at 00:00"),
            ("0 9 * * *", "every day at 09:00"),
            ("* * * * *", "every minute"),
            ("0,30 * * * *", "every 30 minutes"),
            ("30 * * * *", "every hour at :30"),
            ("*/7 * * * *", "*/7 * * * *"),  # 56 → 0 is not a 7-minute gap
        ],
    )
    def test_friendly_descriptions(self, expr: str, expected: str) -> None:
        assert auto.describe(expr) == expected

    def test_unknown_form_returns_expression(self) -> None:
        expr = "0 0 29 2 *"  # Feb 29 — parseable, no friendly match
        assert auto.describe(expr) == expr

    def test_invalid_expression_returns_raw(self) -> None:
        # Bad expressions are returned verbatim rather than crashing.
        assert auto.describe("not a cron") == "not a cron"


# ── due ──────────────────────────────────────────────────────────────────────


class TestDue:
    def _automation(self, schedule: str, **overrides: Any) -> dict[str, Any]:
        base = {
            "id": "t",
            "name": "t",
            "recipe": "framework-edit",
            "kickoff": "# kickoff",
            "schedule": schedule,
            "workspace": "worktree",
            "enabled": True,
            "created_at": "2026-01-01T00:00:00",
            "last_fired_at": None,
            "last_run_id": None,
            "runs": [],
        }
        base.update(overrides)
        return base

    def test_disabled_never_due(self) -> None:
        a = self._automation("* * * * *", enabled=False)
        assert auto.due(a, _dt.datetime(2026, 1, 1, 12, 0)) is False

    def test_due_when_no_last_fire(self) -> None:
        a = self._automation("0 9 * * *")
        assert auto.due(a, _dt.datetime(2026, 1, 1, 9, 0)) is True

    def test_not_due_outside_schedule(self) -> None:
        a = self._automation("0 9 * * *")
        assert auto.due(a, _dt.datetime(2026, 1, 1, 10, 0)) is False

    def test_due_at_most_once_per_minute(self) -> None:
        a = self._automation("* * * * *")
        minute = _dt.datetime(2026, 1, 1, 9, 0)
        assert auto.due(a, minute) is True
        a["last_fired_at"] = minute.isoformat()
        # Same minute — not due.
        assert auto.due(a, minute) is False
        # Half a second later — still the same matching minute.
        assert auto.due(a, minute.replace(microsecond=500000)) is False
        # Next matching minute — now due again.
        assert auto.due(a, minute.replace(minute=1)) is True

    def test_past_window_does_not_backfill(self) -> None:
        # An automation that fired at 09:00 yesterday should not "catch up"
        # at 09:01 today — only the current minute counts (kickoff §tick).
        a = self._automation("0 9 * * *")
        a["last_fired_at"] = "2026-01-01T09:00:00"
        now = _dt.datetime(2026, 1, 2, 9, 0)
        assert auto.due(a, now) is True  # fresh minute
        # Re-tick same minute — not due.
        a["last_fired_at"] = now.isoformat()
        assert auto.due(a, now) is False


# ── store: add / update / remove ─────────────────────────────────────────────


class TestStore:
    def test_add_creates_record(self, tmp_path: Path) -> None:
        home = tmp_path / "home"
        home.mkdir()
        # Need a real recipe to validate against. Use the in-tree ``code-fix``
        # recipe as a stand-in.
        result = auto.add(
            home,
            id="nightly-deps",
            name="Nightly dep check",
            recipe="code-fix",
            kickoff="# dep check",
            schedule="0 3 * * *",
        )
        assert result["ok"] is True
        assert result["automation"]["id"] == "nightly-deps"
        assert result["automation"]["enabled"] is True
        items = auto.load(home)
        assert len(items) == 1
        assert items[0]["id"] == "nightly-deps"

    def test_add_rejects_duplicate_id(self, tmp_path: Path) -> None:
        home = tmp_path / "home"
        home.mkdir()
        auto.add(
            home, id="dup-id", name="x", recipe="code-fix",
            kickoff="# k", schedule="0 3 * * *",
        )
        result = auto.add(
            home, id="dup-id", name="y", recipe="code-fix",
            kickoff="# k", schedule="0 4 * * *",
        )
        assert result["ok"] is False
        assert "already exists" in result["error"]

    def test_add_rejects_bad_id(self, tmp_path: Path) -> None:
        home = tmp_path / "home"
        home.mkdir()
        for bad in ("UPPER", "-leading-dash", "x" * 100, "", "with space"):
            result = auto.add(
                home, id=bad, name="x", recipe="code-fix",
                kickoff="# k", schedule="0 3 * * *",
            )
            assert result["ok"] is False, bad

    def test_add_rejects_unknown_recipe(self, tmp_path: Path) -> None:
        home = tmp_path / "home"
        home.mkdir()
        result = auto.add(
            home, id="xx", name="x", recipe="definitely-not-a-recipe",
            kickoff="# k", schedule="0 3 * * *",
        )
        assert result["ok"] is False
        assert "recipe not found" in result["error"]

    def test_add_rejects_bad_cron(self, tmp_path: Path) -> None:
        home = tmp_path / "home"
        home.mkdir()
        result = auto.add(
            home, id="xx", name="x", recipe="code-fix",
            kickoff="# k", schedule="totally bogus",
        )
        assert result["ok"] is False

    def test_add_rejects_empty_kickoff(self, tmp_path: Path) -> None:
        home = tmp_path / "home"
        home.mkdir()
        result = auto.add(
            home, id="xx", name="x", recipe="code-fix",
            kickoff="   ", schedule="0 3 * * *",
        )
        assert result["ok"] is False
        assert "kickoff" in result["error"]

    def test_update_changes_schedule(self, tmp_path: Path) -> None:
        home = tmp_path / "home"
        home.mkdir()
        auto.add(
            home, id="xx", name="x", recipe="code-fix",
            kickoff="# k", schedule="0 3 * * *",
        )
        result = auto.update(home, "xx", schedule="0 4 * * *")
        assert result["ok"] is True
        assert auto.load(home)[0]["schedule"] == "0 4 * * *"

    def test_update_rejects_bad_schedule(self, tmp_path: Path) -> None:
        home = tmp_path / "home"
        home.mkdir()
        auto.add(
            home, id="xx", name="x", recipe="code-fix",
            kickoff="# k", schedule="0 3 * * *",
        )
        result = auto.update(home, "xx", schedule="bogus")
        assert result["ok"] is False
        # The bad value must NOT have replaced the good one.
        assert auto.load(home)[0]["schedule"] == "0 3 * * *"

    def test_update_rejects_unknown_field(self, tmp_path: Path) -> None:
        home = tmp_path / "home"
        home.mkdir()
        auto.add(
            home, id="xx", name="x", recipe="code-fix",
            kickoff="# k", schedule="0 3 * * *",
        )
        result = auto.update(home, "xx", secret_field="nope")
        assert result["ok"] is False
        assert "unknown fields" in result["error"]

    def test_remove(self, tmp_path: Path) -> None:
        home = tmp_path / "home"
        home.mkdir()
        auto.add(
            home, id="xx", name="x", recipe="code-fix",
            kickoff="# k", schedule="0 3 * * *",
        )
        result = auto.remove(home, "xx")
        assert result["ok"] is True
        assert auto.load(home) == []
        # Removing again is a clean failure, not an exception.
        result = auto.remove(home, "xx")
        assert result["ok"] is False

    def test_pause_and_resume(self, tmp_path: Path) -> None:
        home = tmp_path / "home"
        home.mkdir()
        auto.add(
            home, id="xx", name="x", recipe="code-fix",
            kickoff="# k", schedule="0 3 * * *",
        )
        auto.pause(home, "xx")
        assert auto.load(home)[0]["enabled"] is False
        auto.resume(home, "xx")
        assert auto.load(home)[0]["enabled"] is True

    def test_atomic_write_does_not_partial_apply(self, tmp_path: Path) -> None:
        """If a write fails partway, the existing store must remain parseable."""
        home = tmp_path / "home"
        home.mkdir()
        auto.add(
            home, id="xx", name="x", recipe="code-fix",
            kickoff="# k", schedule="0 3 * * *",
        )
        before = (home / "automations.json").read_text()
        # Sabotage the rename to fail.
        def fail_replace(*args_, **kwargs_):
            del args_, kwargs_
            raise OSError("sabotaged")

        with mock.patch.object(auto.os, "replace", side_effect=fail_replace):
            result = auto.update(home, "xx", schedule="0 5 * * *")
        assert result["ok"] is False  # nothing was saved — say so
        assert "could not save" in result["error"]
        # The on-disk file is unchanged.
        after = (home / "automations.json").read_text()
        assert before == after

    def test_malformed_store_is_never_overwritten(self, tmp_path: Path) -> None:
        home = tmp_path / "home"
        home.mkdir()
        broken = '{"automations": [ {"id": "keep-me",'  # a hand edit gone wrong
        (home / "automations.json").write_text(broken)
        result = auto.add(home, id="new-one", name="n", recipe="code-fix",
                          kickoff="# k", schedule="0 3 * * *")
        assert result["ok"] is False
        assert "not valid JSON" in result["error"]
        assert (home / "automations.json").read_text() == broken
        assert auto.load(home) == []  # reads stay forgiving


def test_proposal_ids_cannot_escape_the_drafts_dir(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    auto.add(home, id="keep", name="k", recipe="code-fix", kickoff="# k", schedule="0 3 * * *")
    before = (home / "automations.json").read_text()
    assert auto.discard_proposal(home, "../automations")["ok"] is False
    assert auto.get_proposal(home, "../automations") is None
    assert auto.commit_proposal(home, "../automations")["ok"] is False
    assert (home / "automations.json").read_text() == before


# ── fire / tick ──────────────────────────────────────────────────────────────


def _git_repo(tmp_path: Path) -> Path:
    project = tmp_path / "project"
    project.mkdir()
    rc = subprocess.run(["git", "init", "-b", "main"], cwd=str(project),
                        capture_output=True).returncode
    assert rc == 0
    subprocess.run(["git", "config", "user.name", "t"], cwd=str(project),
                   capture_output=True)
    subprocess.run(["git", "config", "user.email", "t@e.com"], cwd=str(project),
                   capture_output=True)
    (project / "README.md").write_text("hi\n")
    add_rc = subprocess.run(["git", "add", "README.md"], cwd=str(project),
                            capture_output=True).returncode
    assert add_rc == 0
    commit_rc = subprocess.run(["git", "commit", "-m", "init"],
                                cwd=str(project), capture_output=True).returncode
    assert commit_rc == 0
    return project


class TestFire:
    def test_fire_worktree_mode_creates_workspace(self, tmp_path: Path) -> None:
        project = _git_repo(tmp_path)
        home = project / ".mini-ork"
        home.mkdir()
        auto.add(
            home, id="nightly", name="nightly", recipe="code-fix",
            kickoff="# k", schedule="0 3 * * *", workspace="worktree",
        )
        captured: dict[str, Any] = {}

        def fake_launch(*args, **kwargs):
            h, rcp, kkick = args[0], args[1], args[2]
            run_id = kwargs.get("run_id")
            extra_env = kwargs.get("extra_env") or {}
            captured["home"] = h
            captured["recipe"] = rcp
            captured["kickoff"] = kkick
            captured["run_id"] = run_id
            captured["extra_env"] = extra_env
            return {
                "ok": True,
                "run_id": run_id or "run-test",
                "recipe": rcp,
                "pid": 12345,
                "kickoff_path": str(h / "runs-inbox" / f"{run_id}.md"),
                "log_path": str(h / "runs-inbox" / f"{run_id}.launch.log"),
            }

        result = auto.fire(home, "nightly", launcher=fake_launch)
        assert result["ok"] is True
        assert captured["recipe"] == "code-fix"
        assert captured["extra_env"]["MO_AUTOMATION_ID"] == "nightly"
        assert "MO_TARGET_CWD" in captured["extra_env"]
        # Workspace record was created.
        ws_files = list((home / "worktrees").glob("*.json"))
        assert ws_files, "workspace record not created"
        # MO_TARGET_CWD points at a worktrees/<run_id> directory.
        target = captured["extra_env"]["MO_TARGET_CWD"]
        assert "worktrees" in target
        assert captured["run_id"].startswith("run-")  # launch_run's own id shape
        assert result["workspace"] == "worktree"
        assert result["branch"] == f"mini-ork/{captured['run_id']}"

    def test_fire_ignores_launcher_project_home(self, tmp_path: Path, monkeypatch) -> None:
        # bin/mini-ork sets MINI_ORK_PROJECT_HOME to the HOME (<project>/.mini-ork);
        # the run must still target a worktree of the project, not the home.
        project = _git_repo(tmp_path)
        home = project / ".mini-ork"
        home.mkdir()
        monkeypatch.setenv("MINI_ORK_PROJECT_HOME", str(home))
        auto.add(home, id="nightly", name="n", recipe="code-fix",
                 kickoff="# k", schedule="0 3 * * *", workspace="in-place")
        seen: dict[str, Any] = {}
        auto.fire(home, "nightly",
                  launcher=lambda *a, **k: seen.update(k["extra_env"]) or {"ok": True})
        assert seen["MO_TARGET_CWD"] == str(project)

    def test_fire_worktree_failure_does_not_run_in_place(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        project = _git_repo(tmp_path)
        home = project / ".mini-ork"
        home.mkdir()
        auto.add(home, id="nightly", name="n", recipe="code-fix",
                 kickoff="# k", schedule="0 3 * * *", workspace="worktree")
        from mini_ork import workspaces

        def no_worktree(*_a, **_k):
            raise RuntimeError("fatal: disk full")

        monkeypatch.setattr(workspaces, "create", no_worktree)
        launched: list[Any] = []
        result = auto.fire(home, "nightly",
                           launcher=lambda *a, **k: launched.append(a) or {"ok": True})
        assert result["ok"] is False
        assert "could not create a worktree" in result["error"]
        assert launched == []  # never fell back to editing the checkout
        record = auto.load(home)[0]
        assert record["last_run_id"] is None
        assert "disk full" in record["last_error"]
        assert record["last_fired_at"]  # still fires at most once per minute

    def test_fire_launch_failure_discards_the_worktree(self, tmp_path: Path) -> None:
        project = _git_repo(tmp_path)
        home = project / ".mini-ork"
        home.mkdir()
        auto.add(home, id="nightly", name="n", recipe="code-fix",
                 kickoff="# k", schedule="0 3 * * *", workspace="worktree")
        result = auto.fire(home, "nightly",
                           launcher=lambda *_a, **_k: {"ok": False, "error": "spawn failed"})
        assert result["ok"] is False
        assert list((home / "worktrees").glob("*.json")) == []
        branches = subprocess.run(["git", "branch", "--list", "mini-ork/*"], cwd=project,
                                  capture_output=True, text=True).stdout
        assert branches.strip() == ""
        assert auto.load(home)[0]["last_error"] == "spawn failed"

    def test_success_clears_last_error_and_status_reads_it(self, tmp_path: Path) -> None:
        project = _git_repo(tmp_path)
        home = project / ".mini-ork"
        home.mkdir()
        auto.add(home, id="nightly", name="n", recipe="code-fix",
                 kickoff="# k", schedule="0 3 * * *", workspace="in-place")
        assert auto.last_run_status(home, auto.load(home)[0]) == "never"
        auto.fire(home, "nightly", launcher=lambda *_a, **_k: {"ok": False, "error": "boom"})
        assert auto.last_run_status(home, auto.load(home)[0]) == "not started: boom"
        ok = auto.fire(home, "nightly", launcher=lambda *_a, **_k: {"ok": True})
        record = auto.load(home)[0]
        assert record["last_error"] is None
        assert auto.last_run_status(home, record) == f"{ok['run_id']} starting"

    def test_fire_in_place_mode_skips_workspace(self, tmp_path: Path) -> None:
        project = _git_repo(tmp_path)
        home = project / ".mini-ork"
        home.mkdir()
        auto.add(
            home, id="plain", name="plain", recipe="code-fix",
            kickoff="# k", schedule="0 3 * * *", workspace="in-place",
        )
        captured: dict[str, Any] = {}

        def fake_launch(*args, **kwargs):
            rcp = args[1]
            run_id = kwargs.get("run_id")
            extra_env = kwargs.get("extra_env") or {}
            captured["extra_env"] = extra_env
            return {
                "ok": True, "run_id": run_id or "run-test", "recipe": rcp,
                "pid": 1, "kickoff_path": "x", "log_path": "y",
            }

        auto.fire(home, "plain", launcher=fake_launch)
        assert captured["extra_env"]["MO_TARGET_CWD"] == str(project)
        assert captured["extra_env"]["MO_AUTOMATION_ID"] == "plain"

    def test_fire_non_git_project_falls_back_to_in_place(
        self, tmp_path: Path
    ) -> None:
        project = tmp_path / "not-a-repo"
        project.mkdir()
        home = project / ".mini-ork"
        home.mkdir()
        auto.add(
            home, id="plain", name="plain", recipe="code-fix",
            kickoff="# k", schedule="0 3 * * *", workspace="worktree",
        )
        captured: dict[str, Any] = {}

        def fake_launch(*args, **kwargs):
            rcp = args[1]
            run_id = kwargs.get("run_id")
            extra_env = kwargs.get("extra_env") or {}
            captured["extra_env"] = extra_env
            return {
                "ok": True, "run_id": run_id or "r", "recipe": rcp,
                "pid": 1, "kickoff_path": "x", "log_path": "y",
            }

        result = auto.fire(home, "plain", launcher=fake_launch)
        assert result["ok"] is True
        # Should have fallen back to project root (non-git).
        assert captured["extra_env"]["MO_TARGET_CWD"] == str(project)

    def test_fire_unknown_automation(self, tmp_path: Path) -> None:
        home = tmp_path / "home"
        home.mkdir()
        result = auto.fire(home, "nonexistent", launcher=lambda *_, **__: {"ok": True})
        assert result["ok"] is False
        assert "unknown automation" in result["error"]

    def test_fire_launch_failure_does_not_record(self, tmp_path: Path) -> None:
        project = _git_repo(tmp_path)
        home = project / ".mini-ork"
        home.mkdir()
        auto.add(
            home, id="xx", name="x", recipe="code-fix",
            kickoff="# k", schedule="0 3 * * *", workspace="in-place",
        )

        def boom(*_, **__):
            return {"ok": False, "error": "spawn failed"}

        result = auto.fire(home, "xx", launcher=boom)
        assert result["ok"] is False
        # No last_run_id recorded.
        assert auto.load(home)[0]["last_run_id"] is None
        # Log line still appended with ok=False.
        log = (home / "automations.log").read_text().strip().splitlines()
        assert len(log) == 1
        entry = json.loads(log[0])
        assert entry["ok"] is False
        assert entry["error"] == "spawn failed"


class TestTick:
    def test_tick_fires_only_due_and_enabled(self, tmp_path: Path) -> None:
        project = _git_repo(tmp_path)
        home = project / ".mini-ork"
        home.mkdir()
        now = _dt.datetime(2026, 1, 1, 9, 0)
        auto.add(
            home, id="aa", name="a", recipe="code-fix",
            kickoff="# k", schedule="0 9 * * *", workspace="in-place",
        )
        auto.add(
            home, id="bb", name="b", recipe="code-fix",
            kickoff="# k", schedule="0 10 * * *", workspace="in-place",
        )
        auto.add(
            home, id="cc", name="c", recipe="code-fix",
            kickoff="# k", schedule="0 9 * * *", workspace="in-place",
        )
        auto.pause(home, "cc")  # disabled — must NOT fire

        fired: list[str] = []

        def fake_launch(*args, **kwargs):
            extra_env = kwargs.get("extra_env") or {}
            fired.append(extra_env["MO_AUTOMATION_ID"])
            run_id = kwargs.get("run_id")
            rcp = args[1]
            return {
                "ok": True, "run_id": run_id, "recipe": rcp,
                "pid": 1, "kickoff_path": "x", "log_path": "y",
            }

        results = auto.tick(home, now=now, launcher=fake_launch)
        assert fired == ["aa"]  # only ``aa`` is due and enabled
        assert results[0]["ok"] is True

    def test_tick_writes_log_lines(self, tmp_path: Path) -> None:
        project = _git_repo(tmp_path)
        home = project / ".mini-ork"
        home.mkdir()
        auto.add(
            home, id="aa", name="a", recipe="code-fix",
            kickoff="# k", schedule="0 9 * * *", workspace="in-place",
        )
        auto.tick(home, now=_dt.datetime(2026, 1, 1, 9, 0),
                  launcher=lambda *args_a, **kw_a: (
                      {"ok": True, "run_id": "r1", "recipe": "code-fix",
                       "pid": 1, "kickoff_path": "x", "log_path": "y"},
                      args_a, kw_a)[0])
        log = (home / "automations.log").read_text().strip().splitlines()
        assert len(log) == 1
        entry = json.loads(log[0])
        assert entry["id"] == "aa"
        assert entry["ok"] is True


# ── OS scheduler ─────────────────────────────────────────────────────────────


class TestScheduler:
    """Patches _os_run and _launch_agents_dir; never touches real launchd/cron."""

    def test_install_macos_writes_plist(self, tmp_path: Path, monkeypatch) -> None:
        agents = tmp_path / "LaunchAgents"
        agents.mkdir()
        monkeypatch.setattr(auto, "_launch_agents_dir", lambda: agents)
        monkeypatch.setattr(auto.platform, "system", lambda: "Darwin")
        home = tmp_path / "home"
        home.mkdir()
        cmds: list[list[str]] = []

        def fake_os_run(argv, *, check=True, stdin_input=None, **kwargs):
            cmds.append(argv)
            return subprocess.CompletedProcess(argv, 0, "", "")

        monkeypatch.setattr(auto, "_os_run", fake_os_run)
        result = auto.install_scheduler(home)
        assert result["ok"] is True
        assert result["platform"] == "macos"
        plist = Path(result["plist"])
        assert plist.is_file()
        body = plist.read_text()
        assert "StartInterval" in body
        assert "<integer>60</integer>" in body
        assert "automations" in body
        assert "tick" in body
        assert str(home) in body
        import plistlib
        data = plistlib.loads(body.encode())
        assert data["ProgramArguments"][-4:] == ["automations", "tick", "--home", str(home)]
        assert data["EnvironmentVariables"]["MINI_ORK_HOME"] == str(home)
        assert data["EnvironmentVariables"]["PATH"]  # launchd's bare PATH can't find agent CLIs
        assert data["RunAtLoad"] is False
        # An old job is booted out first, then bootstrapped.
        verbs = [c[1] for c in cmds if c[0] == "launchctl"]
        assert verbs[:2] == ["bootout", "bootstrap"]

    def test_plist_escapes_paths(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setattr(auto, "_launch_agents_dir", lambda: tmp_path / "LA")
        home = tmp_path / "R&D <proj>" / ".mini-ork"
        import plistlib
        data = plistlib.loads(auto._build_plist(home).encode())
        assert data["ProgramArguments"][-1] == str(home)

    def test_install_macos_falls_back_to_load_on_bootstrap_failure(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        agents = tmp_path / "LaunchAgents"
        agents.mkdir()
        monkeypatch.setattr(auto, "_launch_agents_dir", lambda: agents)
        monkeypatch.setattr(auto.platform, "system", lambda: "Darwin")
        home = tmp_path / "home"
        home.mkdir()
        cmds: list[list[str]] = []

        def fake_os_run(argv, *, check=True, stdin_input=None, **kwargs):
            cmds.append(argv)
            if "bootstrap" in argv:
                raise subprocess.CalledProcessError(1, argv)
            return subprocess.CompletedProcess(argv, 0, "", "")

        monkeypatch.setattr(auto, "_os_run", fake_os_run)
        result = auto.install_scheduler(home)
        assert result["ok"] is True
        assert any(c[:2] == ["launchctl", "load"] for c in cmds)

    def test_install_linux_writes_crontab_line(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setattr(auto.platform, "system", lambda: "Linux")
        home = tmp_path / "home"
        home.mkdir()
        writes: list[str] = []

        def fake_os_run(argv, *, check=True, stdin_input=None, **kwargs_):
            del check, kwargs_
            if argv[:2] == ["crontab", "-l"]:
                return subprocess.CompletedProcess(argv, 0, "existing line\n", "")
            if argv[:2] == ["crontab", "-"]:
                writes.append(stdin_input or "")
                return subprocess.CompletedProcess(argv, 0, "", "")
            return subprocess.CompletedProcess(argv, 0, "", "")

        monkeypatch.setattr(auto, "_os_run", fake_os_run)
        result = auto.install_scheduler(home)
        assert result["ok"] is True
        assert result["platform"] == "linux"
        assert len(writes) == 1
        assert "mini-ork-automations-marker" in writes[0]
        assert "* * * * *" in writes[0]
        assert writes[0].startswith("existing line\n")  # other jobs kept
        line = writes[0].splitlines()[-1]
        assert "MINI_ORK_ROOT=" in line and "PATH=" in line
        assert f">> {home / 'automations-tick.log'} 2>&1" in line

    def test_crontab_line_quotes_paths(self, tmp_path: Path) -> None:
        import shlex
        home = tmp_path / "my project" / ".mini-ork"
        line = auto._crontab_line(home)
        command = line[len("* * * * * "):line.index(" # mini-ork-automations-marker")]
        words = shlex.split(command)
        assert words[words.index("--home") + 1] == str(home)

    def test_install_linux_is_idempotent(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setattr(auto.platform, "system", lambda: "Linux")
        home = tmp_path / "home"
        home.mkdir()
        writes: list[str] = []
        # Marker so the first install can be detected as already present.
        marker = auto._home_hash(home)
        initial = (
            f"* * * * * /bin/true # mini-ork-automations-marker:{marker}\n"
        )

        def fake_os_run(argv, *, check=True, stdin_input=None, **kwargs_):
            del check, kwargs_
            if argv[:2] == ["crontab", "-l"]:
                return subprocess.CompletedProcess(argv, 0, initial, "")
            if argv[:2] == ["crontab", "-"]:
                writes.append(stdin_input or "")
                return subprocess.CompletedProcess(argv, 0, "", "")
            return subprocess.CompletedProcess(argv, 0, "", "")

        monkeypatch.setattr(auto, "_os_run", fake_os_run)
        result = auto.install_scheduler(home)
        assert result["ok"] is True
        assert len(writes) == 1
        # The original line was replaced with a single new one (no duplicates).
        assert writes[0].count("mini-ork-automations-marker") == 1

    def test_uninstall_linux_removes_marker_line(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setattr(auto.platform, "system", lambda: "Linux")
        home = tmp_path / "home"
        home.mkdir()
        marker = auto._home_hash(home)
        initial = (
            "0 5 * * * /bin/true\n"
            f"* * * * * /bin/true # mini-ork-automations-marker:{marker}\n"
            "0 7 * * * /bin/false\n"
        )
        writes: list[str] = []

        def fake_os_run(argv, *, check=True, stdin_input=None, **kwargs_):
            del check, kwargs_
            if argv[:2] == ["crontab", "-l"]:
                return subprocess.CompletedProcess(argv, 0, initial, "")
            if argv[:2] == ["crontab", "-"]:
                writes.append(stdin_input or "")
                return subprocess.CompletedProcess(argv, 0, "", "")
            return subprocess.CompletedProcess(argv, 0, "", "")

        monkeypatch.setattr(auto, "_os_run", fake_os_run)
        result = auto.uninstall_scheduler(home)
        assert result["ok"] is True
        assert "mini-ork-automations-marker" not in writes[0]

    def test_unsupported_platform(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setattr(auto.platform, "system", lambda: "Windows")
        home = tmp_path / "home"
        home.mkdir()
        for fn in (auto.install_scheduler, auto.uninstall_scheduler, auto.scheduler_status):
            result = fn(home)
            # install/uninstall surface ok=False; status reports the platform
            # name without an ok key (read-only status).
            assert result.get("ok", True) is False or result.get("platform") == "windows"
            if fn is auto.scheduler_status:
                assert result["platform"] == "windows"

    def test_status_macos_reports_installed(self, tmp_path: Path, monkeypatch) -> None:
        agents = tmp_path / "LaunchAgents"
        agents.mkdir()
        monkeypatch.setattr(auto, "_launch_agents_dir", lambda: agents)
        monkeypatch.setattr(auto.platform, "system", lambda: "Darwin")
        home = tmp_path / "home"
        home.mkdir()
        plist = auto._plist_path(home)
        plist.write_text("<plist/>", encoding="utf-8")
        monkeypatch.setattr(auto, "_os_run",
                            lambda *a, **kw: subprocess.CompletedProcess(a[0], 0, "", ""))
        status = auto.scheduler_status(home)
        assert status["installed"] is True
        assert status["platform"] == "macos"

    def test_status_linux_reports_installed(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setattr(auto.platform, "system", lambda: "Linux")
        home = tmp_path / "home"
        home.mkdir()
        marker = auto._home_hash(home)
        crontab_text = (
            f"* * * * * /bin/true # mini-ork-automations-marker:{marker}\n"
        )

        def fake_os_run(argv, *, check=True, stdin_input=None, **kwargs):
            if argv[:2] == ["crontab", "-l"]:
                return subprocess.CompletedProcess(argv, 0, crontab_text, "")
            return subprocess.CompletedProcess(argv, 0, "", "")

        monkeypatch.setattr(auto, "_os_run", fake_os_run)
        status = auto.scheduler_status(home)
        assert status["installed"] is True
        assert status["platform"] == "linux"


# ── CLI ──────────────────────────────────────────────────────────────────────


class TestCLI:
    def test_list_json(self, tmp_path: Path, monkeypatch, capsys) -> None:
        monkeypatch.setenv("MINI_ORK_HOME", str(tmp_path / "home"))
        from mini_ork.cli import automations_cmd
        importlib.reload(automations_cmd)
        # add an automation through the store
        automations_cmd._auto.add(
            tmp_path / "home",
            id="aa", name="A", recipe="code-fix",
            kickoff="# k", schedule="0 9 * * *",
        )
        rc = automations_cmd.main(["list", "--json"], "/tmp/root")
        assert rc == 0
        body = capsys.readouterr().out
        parsed = json.loads(body)
        assert "automations" in parsed
        assert parsed["automations"][0]["id"] == "aa"

    def test_add_then_run(self, tmp_path: Path, monkeypatch, capsys) -> None:
        home = tmp_path / "home"
        home.mkdir()
        kickoff_path = tmp_path / "kickoff.md"
        kickoff_path.write_text("# nightly\n", encoding="utf-8")
        monkeypatch.setenv("MINI_ORK_HOME", str(home))
        from mini_ork.cli import automations_cmd
        importlib.reload(automations_cmd)
        rc = automations_cmd.main(
            [
                "add", "--id", "nightly", "--name", "Nightly",
                "--recipe", "code-fix", "--schedule", "0 3 * * *",
                "--kickoff-file", str(kickoff_path), "--in-place",
            ],
            "/tmp/root",
        )
        assert rc == 0
        items = automations_cmd._auto.load(home)
        assert len(items) == 1
        assert items[0]["kickoff"] == "# nightly\n"
        assert items[0]["workspace"] == "in-place"

    def test_home_after_the_subcommand(self, tmp_path: Path, monkeypatch, capsys) -> None:
        # The OS scheduler runs exactly this argv; it must parse.
        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.delenv("MINI_ORK_HOME", raising=False)
        from mini_ork.cli import automations_cmd
        argv = auto._tick_argv(home)
        assert argv[2:4] == ["automations", "tick"]
        assert automations_cmd.main(argv[3:], "/tmp/root") == 0
        assert json.loads(capsys.readouterr().out) == {"fired": []}
        for rest in (["--home", str(home), "list", "--json"],
                     ["list", "--json", "--home", str(home)],
                     ["scheduler", "status", "--json", "--home", str(home)]):
            assert automations_cmd.main(rest, "/tmp/root") == 0, rest
            capsys.readouterr()

    def test_run_fires_through_the_cli(self, tmp_path: Path, monkeypatch, capsys) -> None:
        home = tmp_path / "home"
        home.mkdir()
        auto.add(home, id="nightly", name="n", recipe="code-fix",
                 kickoff="# k", schedule="0 3 * * *", workspace="in-place")
        calls: list[Any] = []

        def fake_launch(*a, **k):
            calls.append((a, k))
            return {"ok": True, "run_id": k["run_id"]}

        from mini_ork.web import control
        monkeypatch.setattr(control, "launch_run", fake_launch)
        from mini_ork.cli import automations_cmd
        assert automations_cmd.main(["run", "nightly", "--home", str(home)], "/tmp/root") == 0
        out = json.loads(capsys.readouterr().out)
        assert out["ok"] is True and len(calls) == 1
        assert calls[0][1]["extra_env"]["MO_AUTOMATION_ID"] == "nightly"

    def test_unknown_subcommand(self, capsys) -> None:
        from mini_ork.cli import automations_cmd
        rc = automations_cmd.main(["wat"], "/tmp/root")
        assert rc == 2
        assert "unknown subcommand" in capsys.readouterr().err.lower()

    def test_no_args_shows_usage(self, capsys) -> None:
        from mini_ork.cli import automations_cmd
        rc = automations_cmd.main([], "/tmp/root")
        assert rc == 2
        assert "usage" in capsys.readouterr().err.lower() or "Usage" in capsys.readouterr().err

    def test_add_missing_kickoff_file(self, tmp_path: Path, monkeypatch, capsys) -> None:
        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setenv("MINI_ORK_HOME", str(home))
        from mini_ork.cli import automations_cmd
        rc = automations_cmd.main(
            [
                "add", "--id", "x", "--name", "x", "--recipe", "code-fix",
                "--schedule", "0 3 * * *",
                "--kickoff-file", str(tmp_path / "missing.md"),
            ],
            "/tmp/root",
        )
        assert rc == 1
        assert "kickoff file" in capsys.readouterr().err.lower()

    def test_remove_then_run(self, tmp_path: Path, monkeypatch, capsys) -> None:
        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setenv("MINI_ORK_HOME", str(home))
        from mini_ork.cli import automations_cmd
        automations_cmd._auto.add(
            home, id="aa", name="A", recipe="code-fix",
            kickoff="# k", schedule="0 9 * * *", workspace="in-place",
        )
        rc = automations_cmd.main(["remove", "aa"], "/tmp/root")
        assert rc == 0
        assert automations_cmd._auto.load(home) == []

    def test_run_unknown_id(self, tmp_path: Path, monkeypatch, capsys) -> None:
        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setenv("MINI_ORK_HOME", str(home))
        from mini_ork.cli import automations_cmd
        rc = automations_cmd.main(["run", "nope"], "/tmp/root")
        assert rc == 1
        assert "unknown automation" in capsys.readouterr().err.lower()


# ── native dispatch exact-set guard ──────────────────────────────────────────


def test_automations_in_exact_set_guard():
    """The native dispatch exact-set must include ``automations``."""
    from mini_ork.cli.main import _NATIVE_MODULE_SUBS
    assert "automations" in _NATIVE_MODULE_SUBS
    assert _NATIVE_MODULE_SUBS["automations"] == "mini_ork.cli.automations_cmd"


# ── next_fires (Zed S6b-1) ────────────────────────────────────────────────────


class TestNextFires:
    """``next_fires`` returns the next ``n`` firing times after ``after``."""

    def test_returns_three_times_after_now(self) -> None:
        base = _dt.datetime(2026, 1, 1, 9, 0, 0)
        out = auto.next_fires("0 9 * * *", n=3, after=base)
        assert len(out) == 3
        assert [t.hour for t in out] == [9, 9, 9]
        # Day deltas: 1, 2, 3.
        assert (out[0].date() - base.date()).days == 1
        assert (out[2].date() - base.date()).days == 3

    def test_bad_expression_returns_empty_list(self) -> None:
        # Unparseable cron → [] (NOT a raise).
        assert auto.next_fires("not a cron", n=3) == []
        # 6 fields instead of 5 → ValueError from parse_cron → [].
        assert auto.next_fires("0 0 * * * 0", n=3) == []

    def test_zero_n_returns_empty(self) -> None:
        # Defensive: n<=0 is a no-op.
        assert auto.next_fires("0 9 * * *", n=0) == []
        assert auto.next_fires("0 9 * * *", n=-1) == []

    def test_every_minute_returns_increasing_seconds(self) -> None:
        base = _dt.datetime(2026, 1, 1, 9, 0, 30)
        out = auto.next_fires("* * * * *", n=3, after=base)
        # Each next_fire is strictly after the previous.
        for a, b in zip(out, out[1:]):
            assert a < b
        # Seconds reset to 0 (cron fires on the minute).
        for t in out:
            assert t.second == 0
            assert t.microsecond == 0


# ── Proposals (Zed S6b-1) ─────────────────────────────────────────────────────


class TestProposals:
    """``propose`` / ``get_proposal`` / ``commit_proposal`` / ``discard_proposal``."""

    def test_propose_writes_a_draft(self, tmp_path: Path) -> None:
        home = tmp_path / "home"
        home.mkdir()
        # Make sure ``_validate_recipe_exists`` does not require a real recipe
        # catalog: add() of any name works because parse_cron + name checks
        # happen in the order captured by ``add``. We seed a recipe first
        # via the same path the CLI uses.
        _seed_recipe_catalog(home)
        result = auto.propose(
            home, id="nightly", name="Nightly build",
            recipe="code-fix", kickoff="# nightly",
            schedule="0 3 * * *", workspace="worktree",
        )
        assert result["ok"] is True
        assert result["exists"] is False
        assert "next_fires" in result and len(result["next_fires"]) == 3
        # File exists on disk.
        assert (home / "automation-drafts" / "nightly.json").is_file()

    def test_propose_validates_like_add(self, tmp_path: Path) -> None:
        home = tmp_path / "home"
        home.mkdir()
        # Empty kickoff → ok=False, nothing written.
        result = auto.propose(
            home, id="bad", name="bad", recipe="x",
            kickoff="", schedule="0 9 * * *",
        )
        assert result["ok"] is False
        assert "kickoff" in result["error"].lower()
        assert not (home / "automation-drafts").exists()

        # Bad cron.
        result = auto.propose(
            home, id="bad", name="bad", recipe="x",
            kickoff="# k", schedule="not a cron",
        )
        assert result["ok"] is False
        assert "cron" in result["error"].lower()

    def test_propose_on_existing_id_sets_exists_true(self, tmp_path: Path) -> None:
        home = tmp_path / "home"
        home.mkdir()
        _seed_recipe_catalog(home)
        auto.add(
            home, id="daily", name="Daily", recipe="code-fix",
            kickoff="# k", schedule="0 9 * * *",
        )
        result = auto.propose(
            home, id="daily", name="Daily v2",
            recipe="code-fix", kickoff="# k v2",
            schedule="0 10 * * *",
        )
        assert result["ok"] is True
        assert result["exists"] is True
        # The original automation is unchanged.
        items = auto.load(home)
        assert items[0]["name"] == "Daily"
        assert items[0]["schedule"] == "0 9 * * *"

    def test_get_proposal_returns_parsed_dict(self, tmp_path: Path) -> None:
        home = tmp_path / "home"
        home.mkdir()
        _seed_recipe_catalog(home)
        auto.propose(
            home, id="xx", name="XX", recipe="code-fix",
            kickoff="# k", schedule="0 9 * * *",
        )
        out = auto.get_proposal(home, "xx")
        assert isinstance(out, dict)
        assert out["id"] == "xx"
        assert out["schedule"] == "0 9 * * *"

    def test_get_proposal_missing_returns_none(self, tmp_path: Path) -> None:
        home = tmp_path / "home"
        home.mkdir()
        assert auto.get_proposal(home, "absent") is None

    def test_commit_proposal_adds_when_new(self, tmp_path: Path) -> None:
        home = tmp_path / "home"
        home.mkdir()
        _seed_recipe_catalog(home)
        auto.propose(
            home, id="weekly", name="Weekly",
            recipe="code-fix", kickoff="# k",
            schedule="0 9 * * 1", workspace="worktree",
        )
        result = auto.commit_proposal(home, "weekly")
        assert result["ok"] is True
        assert result["created"] is True
        # Proposal file gone.
        assert auto.get_proposal(home, "weekly") is None
        # Store has it.
        items = auto.load(home)
        assert any(a.get("id") == "weekly" for a in items)

    def test_commit_proposal_updates_when_existing(self, tmp_path: Path) -> None:
        home = tmp_path / "home"
        home.mkdir()
        _seed_recipe_catalog(home)
        auto.add(
            home, id="daily", name="Daily",
            recipe="code-fix", kickoff="# k",
            schedule="0 9 * * *", workspace="worktree",
        )
        # Disable + record history so we can verify it survives.
        auto.pause(home, "daily")
        # Set last_run_id manually (no fire — that needs git).
        def mutate(items):
            for a in items:
                if a.get("id") == "daily":
                    a["last_run_id"] = "run-old-001"
                    a["last_fired_at"] = "2026-01-01T09:00:00"
                    a["runs"] = ["run-old-001"]
            return {"ok": True}
        from mini_ork.automations import _write_locked  # noqa: PLC0415
        _write_locked(home, mutate)

        auto.propose(
            home, id="daily", name="Daily v2",
            recipe="code-fix", kickoff="# k v2",
            schedule="0 10 * * *", workspace="worktree",
        )
        result = auto.commit_proposal(home, "daily")
        assert result["ok"] is True
        assert result["created"] is False
        # ``enabled``, ``last_run_id``, ``runs`` are preserved.
        items = auto.load(home)
        rec = next(a for a in items if a.get("id") == "daily")
        assert rec["enabled"] is False
        assert rec["last_run_id"] == "run-old-001"
        assert rec["runs"] == ["run-old-001"]
        # Name + schedule updated.
        assert rec["name"] == "Daily v2"
        assert rec["schedule"] == "0 10 * * *"

    def test_commit_proposal_no_proposal_returns_error(self, tmp_path: Path) -> None:
        home = tmp_path / "home"
        home.mkdir()
        result = auto.commit_proposal(home, "ghost")
        assert result["ok"] is False
        assert "no proposal" in result["error"]

    def test_discard_proposal_removes_file(self, tmp_path: Path) -> None:
        home = tmp_path / "home"
        home.mkdir()
        _seed_recipe_catalog(home)
        auto.propose(
            home, id="tmp", name="Tmp", recipe="code-fix",
            kickoff="# k", schedule="0 9 * * *",
        )
        result = auto.discard_proposal(home, "tmp")
        assert result["ok"] is True
        assert auto.get_proposal(home, "tmp") is None

    def test_discard_proposal_missing_is_ok(self, tmp_path: Path) -> None:
        home = tmp_path / "home"
        home.mkdir()
        result = auto.discard_proposal(home, "never-existed")
        assert result["ok"] is True


def _seed_recipe_catalog(home: Path) -> None:
    """Write a minimal ``<home>/recipes/code-fix/`` so the proposal
    validator's ``_validate_recipe_exists`` returns True.

    The validator only checks ``find_recipe`` (which scans a recipes dir);
    the workflow.yaml body is irrelevant to ``propose``.
    """
    recipe_dir = home / "recipes" / "code-fix"
    recipe_dir.mkdir(parents=True, exist_ok=True)
    (recipe_dir / "workflow.yaml").write_text(
        "name: code-fix\nnodes:\n  - id: n1\n    type: verifier\n",
        encoding="utf-8",
    )
    (recipe_dir / "task_class.yaml").write_text(
        "name: code-fix\n", encoding="utf-8",
    )