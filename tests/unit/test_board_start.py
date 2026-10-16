"""``board start`` — launching a run from a kickoff file (FR-010).

``web.control.launch_run`` is stubbed in every test that gets as far as
launching: a real spawn is a side effect on the shared engine, so what is
asserted is the argv the launcher would receive and the payload it would
produce. The refusal path is asserted against real disk instead, because the
thing being refused — an uninitialised home — is exactly the case where
``launch_run`` would mkdir the tree it was handed and start a run against a
directory that did not exist a second earlier. Nothing may be created there.

Every test drives the real ``board start`` argv through ``board_cmd.main``, so
the parser, ``validate`` and the exit-code split are all on the path.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from mini_ork.cli import board_cmd
from mini_ork.stores import migrate as mig

REPO = Path(__file__).resolve().parents[2]
GOAL = "Fix the auth middleware timeout"


@pytest.fixture()
def home(tmp_path: Path) -> Path:
    """An initialised home: the state database and the task-class config.

    Both, because ``_home_initialised`` requires both — the same predicate
    ``launch_run``'s callers apply, and the reason the refusal below can be
    told apart from "the home is simply empty".
    """
    h = tmp_path / "proj" / ".mini-ork"
    (h / "config" / "task_classes").mkdir(parents=True)
    rc, _out, err = mig.init_db(db=str(h / "state.db"), root=str(REPO))
    assert rc == 0, err
    return h


def _kickoff(path: Path, *, recipe: str | None = "code-fix", title: str = GOAL) -> Path:
    """A kickoff file, with or without a ``recipe:`` front-matter line."""
    front = f"---\nrecipe: {recipe}\n---\n" if recipe else ""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"{front}# {title}\n\nDo the thing.\n\n## Success\n\n- `pytest -q`\n",
                    encoding="utf-8")
    return path


class _Launcher:
    """Stands in for ``web.control.launch_run``; records what it was handed.

    With no ``result`` it answers the way the real launcher does — the keys
    ``{ok, run_id, recipe, pid, kickoff_path, log_path}`` — because the
    non-dry-run payload *is* that result (FR-010) and a stub that returned
    something else would make the payload assertions below assert the stub.
    """

    def __init__(self, result: dict | None = None) -> None:
        self.calls: list[dict] = []
        self.result = result

    def __call__(self, home: Path, recipe: str, kickoff_markdown: str, *,
                 run_id: str | None = None,
                 extra_env: dict[str, str] | None = None) -> dict:
        self.calls.append({"home": home, "recipe": recipe, "kickoff": kickoff_markdown,
                           "run_id": run_id, "extra_env": dict(extra_env or {})})
        if self.result is not None:
            return dict(self.result)
        return {"ok": True, "run_id": run_id, "recipe": recipe, "pid": 4242,
                "kickoff_path": str(Path(home) / "runs-inbox" / f"{run_id}.md"),
                "log_path": str(Path(home) / "runs-inbox" / f"{run_id}.launch.log")}


@pytest.fixture()
def launcher(monkeypatch: pytest.MonkeyPatch) -> _Launcher:
    """Patch the launcher where ``_act_start`` imports it — at call time."""
    stub = _Launcher()
    monkeypatch.setattr("mini_ork.web.control.launch_run", stub)
    return stub


def _board(rest: list[str], capsys) -> tuple[int, dict, str]:
    """Run a real ``board`` argv; ``(rc, payload, stderr)``."""
    rc = board_cmd.main(rest, "")
    cap = capsys.readouterr()
    payload = json.loads(cap.out) if cap.out.strip() else {}
    return rc, payload, cap.err


# ── the two usage refusals that never reach the launcher ───────────────────

def test_a_start_without_a_kickoff_is_a_usage_error(capsys) -> None:
    rc, payload, err = _board(["start"], capsys)
    assert rc == 2
    assert "--kickoff <path> is required" in err
    assert payload == {}


def test_a_kickoff_that_is_not_there_is_a_usage_error(home: Path, launcher: _Launcher,
                                                      tmp_path: Path, capsys) -> None:
    missing = tmp_path / "nowhere.md"
    rc, payload, err = _board(["start", "--kickoff", str(missing), "--home", str(home)], capsys)
    assert rc == 2
    assert payload["stage"] == "usage"
    assert f"kickoff not found: {missing}" in payload["error"]
    assert str(missing) in err
    assert launcher.calls == []


def test_an_extra_positional_is_a_usage_error(home: Path, launcher: _Launcher,
                                              tmp_path: Path, capsys) -> None:
    """``start`` consumes neither positional slot, so two of them are a typo."""
    kickoff = _kickoff(tmp_path / "draft.md")
    rc, payload, err = _board(["start", "one", "two", "--kickoff", str(kickoff),
                               "--home", str(home)], capsys)
    assert rc == 2
    assert "unexpected extra argument 'two'" in err
    assert launcher.calls == []


def test_the_first_positional_is_not_a_run_id(home: Path, launcher: _Launcher,
                                              tmp_path: Path, capsys) -> None:
    """A caller who writes ``board start run-x --kickoff K`` gets a generated id.

    The slot is the shared ``run_id`` positional, which ``start`` ignores — it
    takes the id from ``--run-id``. Pinning this, because the two read alike.
    """
    kickoff = _kickoff(tmp_path / "draft.md")
    rc, payload, _err = _board(["start", "run-x", "--kickoff", str(kickoff),
                                "--home", str(home)], capsys)
    assert rc == 0
    assert payload["run_id"] != "run-x"
    assert re.fullmatch(r"run-\d+-[0-9a-f]{6}", payload["run_id"]), payload["run_id"]


# ── the home gate ──────────────────────────────────────────────────────────

def test_an_uninitialised_home_refuses_and_creates_nothing(tmp_path: Path, launcher: _Launcher,
                                                           capsys) -> None:
    """Fail closed: the home is a typo, and nothing may appear under it."""
    uninit = tmp_path / "typo" / ".mini-ork"
    uninit.mkdir(parents=True)
    kickoff = _kickoff(tmp_path / "draft.md")
    rc, payload, err = _board(["start", "--kickoff", str(kickoff), "--home", str(uninit)], capsys)
    assert rc == 2
    assert payload["stage"] == "usage"
    assert "--init" in payload["error"] and str(uninit) in payload["error"]
    assert "not an initialised mini-ork home" in err
    assert launcher.calls == []
    assert not (uninit / "runs-inbox").exists(), "the launcher would have mkdir'd this"


def test_init_launches_against_a_home_that_does_not_exist_yet(tmp_path: Path,
                                                              launcher: _Launcher,
                                                              capsys) -> None:
    uninit = tmp_path / "fresh" / ".mini-ork"
    uninit.mkdir(parents=True)
    kickoff = _kickoff(tmp_path / "draft.md")
    rc, payload, _err = _board(["start", "--kickoff", str(kickoff), "--init",
                                "--home", str(uninit)], capsys)
    assert rc == 0
    assert payload["ok"] is True
    assert [c["home"] for c in launcher.calls] == [uninit]


def test_the_launcher_receives_the_absolute_kickoff_path_and_its_text(
        home: Path, launcher: _Launcher, tmp_path: Path, monkeypatch, capsys) -> None:
    monkeypatch.chdir(tmp_path)
    _kickoff(Path("draft.md"))
    rc, _payload, _err = _board(["start", "--kickoff", "draft.md", "--home", str(home)], capsys)
    assert rc == 0
    call = launcher.calls[0]
    assert call["kickoff"] == Path("draft.md").read_text(encoding="utf-8")
    assert "Do the thing." in call["kickoff"]


# ── recipe resolution ──────────────────────────────────────────────────────

def test_the_recipe_comes_from_the_front_matter(home: Path, launcher: _Launcher,
                                                tmp_path: Path, capsys) -> None:
    kickoff = _kickoff(tmp_path / "draft.md", recipe="refactor")
    rc, payload, _err = _board(["start", "--kickoff", str(kickoff), "--home", str(home)], capsys)
    assert rc == 0
    assert payload["recipe"] == "refactor" and payload["recipe_source"] == "front matter"
    assert launcher.calls[0]["recipe"] == "refactor"


def test_the_recipe_flag_beats_the_front_matter(home: Path, launcher: _Launcher,
                                                tmp_path: Path, capsys) -> None:
    kickoff = _kickoff(tmp_path / "draft.md", recipe="refactor")
    rc, payload, _err = _board(["start", "--kickoff", str(kickoff), "--recipe", "code-fix",
                                "--home", str(home)], capsys)
    assert rc == 0
    assert payload["recipe"] == "code-fix" and payload["recipe_source"] == "--recipe"


def test_a_kickoff_with_no_recipe_anywhere_is_refused(home: Path, launcher: _Launcher,
                                                      tmp_path: Path, capsys) -> None:
    kickoff = _kickoff(tmp_path / "draft.md", recipe=None)
    rc, payload, err = _board(["start", "--kickoff", str(kickoff), "--home", str(home)], capsys)
    assert rc == 2
    assert payload["stage"] == "usage"
    assert "--recipe" in payload["error"] and "front matter" in payload["error"]
    assert "no recipe for" in err
    assert launcher.calls == []


# ── the environment the child is handed ────────────────────────────────────

def test_the_launcher_receives_the_flags_a_foreground_run_would_get(
        home: Path, launcher: _Launcher, tmp_path: Path, monkeypatch, capsys) -> None:
    monkeypatch.delenv("MO_WORKSPACE_MODE", raising=False)
    kickoff = _kickoff(tmp_path / "draft.md")
    rc, payload, _err = _board(["start", "--kickoff", str(kickoff), "--set", "A=1",
                                "--set", "B=2", "--workspace", "in-place",
                                "--run-id", "run-x", "--home", str(home)], capsys)
    assert rc == 0
    assert launcher.calls[0]["extra_env"] == {"A": "1", "B": "2", "MO_WORKSPACE_MODE": "in-place"}
    assert launcher.calls[0]["run_id"] == "run-x"
    assert payload["run_id"] == "run-x" and payload["recipe"] == "code-fix"


def test_the_workspace_falls_back_to_the_environment(home: Path, launcher: _Launcher,
                                                     tmp_path: Path, monkeypatch, capsys) -> None:
    """Read on the dry run, which is the payload FR-010 requires to name it."""
    monkeypatch.setenv("MO_WORKSPACE_MODE", "worktree")
    kickoff = _kickoff(tmp_path / "draft.md")
    rc, payload, _err = _board(["start", "--kickoff", str(kickoff), "--dry-run",
                                "--home", str(home)], capsys)
    assert rc == 0
    assert payload["workspace"] == "worktree"
    assert payload["workspace_source"] == "MO_WORKSPACE_MODE"
    assert launcher.calls == []


def test_with_no_workspace_anywhere_the_default_is_the_absence_of_one(
        home: Path, launcher: _Launcher, tmp_path: Path, monkeypatch, capsys) -> None:
    monkeypatch.delenv("MO_WORKSPACE_MODE", raising=False)
    kickoff = _kickoff(tmp_path / "draft.md")
    rc, payload, _err = _board(["start", "--kickoff", str(kickoff), "--dry-run",
                                "--home", str(home)], capsys)
    assert rc == 0
    assert payload["workspace"] == "" and payload["workspace_source"] == "default"


def test_a_malformed_set_is_refused_rather_than_passed_through(
        home: Path, launcher: _Launcher, tmp_path: Path, capsys) -> None:
    """A pair with no ``=`` would reach the child as a bare environment name."""
    kickoff = _kickoff(tmp_path / "draft.md")
    rc, payload, err = _board(["start", "--kickoff", str(kickoff), "--set", "nope",
                               "--home", str(home)], capsys)
    assert rc == 2
    assert payload["stage"] == "usage"
    assert payload["error"] == "--set expects K=V, got 'nope'"
    assert "expects K=V" in err
    assert launcher.calls == []


# ── what the payload says ──────────────────────────────────────────────────

def test_the_dry_run_writes_nothing_and_never_launches(home: Path, launcher: _Launcher,
                                                       tmp_path: Path, capsys) -> None:
    kickoff = _kickoff(tmp_path / "draft.md")
    rc, payload, _err = _board(["start", "--kickoff", str(kickoff), "--dry-run",
                                "--home", str(home)], capsys)
    assert rc == 0
    assert payload["dry_run"] is True and payload["spawned"] is False
    assert launcher.calls == []
    assert not (home / "runs-inbox").exists()


def test_the_dry_run_reports_where_the_kickoff_would_be_staged(
        home: Path, launcher: _Launcher, tmp_path: Path, capsys) -> None:
    """The plan the page shows before anyone commits to a run."""
    kickoff = _kickoff(tmp_path / "draft.md", recipe="refactor")
    rc, payload, _err = _board(["start", "--kickoff", str(kickoff), "--set", "B=2",
                                "--set", "A=1", "--run-id", "run-plan", "--dry-run",
                                "--home", str(home)], capsys)
    assert rc == 0
    assert payload["run_id"] == "run-plan"
    assert payload["kickoff"] == str(kickoff)
    assert payload["kickoff_path"] == str(home / "runs-inbox" / "run-plan.md")
    assert payload["extra_env"] == ["A", "B"]
    assert payload["recipe"] == "refactor" and payload["recipe_source"] == "front matter"


def test_a_launch_the_launcher_rejected_is_a_failure_not_a_usage_error(
        home: Path, monkeypatch, tmp_path: Path, capsys) -> None:
    """The exit-code split exists because the two have different fixes."""
    monkeypatch.setattr("mini_ork.web.control.launch_run",
                        _Launcher({"ok": False, "error": "invalid recipe: 'x y'"}))
    kickoff = _kickoff(tmp_path / "draft.md")
    rc, payload, err = _board(["start", "--kickoff", str(kickoff), "--home", str(home)], capsys)
    assert rc == 1
    assert payload["ok"] is False and payload["stage"] == "launch"
    assert "invalid recipe" in err


def test_a_generated_run_id_is_stamped_and_handed_to_the_launcher(
        home: Path, launcher: _Launcher, tmp_path: Path, capsys) -> None:
    kickoff = _kickoff(tmp_path / "draft.md")
    rc, payload, _err = _board(["start", "--kickoff", str(kickoff), "--home", str(home)], capsys)
    assert rc == 0
    assert re.fullmatch(r"run-\d+-[0-9a-f]{6}", payload["run_id"]), payload["run_id"]
    assert launcher.calls[0]["run_id"] == payload["run_id"]
    # The payload's own result is the launcher's, with the two facts the
    # launcher does not know filled in rather than dropped.
    assert payload["pid"] == 4242
    assert payload["kickoff"] == str(kickoff)


def test_a_run_id_the_launcher_cannot_use_is_the_launchers_to_refuse(
        home: Path, monkeypatch, tmp_path: Path, capsys) -> None:
    """``launch_run`` validates the charset; ``start`` does not duplicate it.

    Two spellings of "a safe token" would drift, and the launcher's is the one
    the spawn actually applies — so a bad id must come back as a launch
    failure, not as a usage error this verb invented.
    """
    monkeypatch.setattr("mini_ork.web.control.launch_run",
                        _Launcher({"ok": False, "error": "invalid run_id: 'a/b'"}))
    kickoff = _kickoff(tmp_path / "draft.md")
    rc, payload, _err = _board(["start", "--kickoff", str(kickoff), "--run-id", "a/b",
                                "--home", str(home)], capsys)
    assert rc == 1
    assert payload["stage"] == "launch"
