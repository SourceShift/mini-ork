"""``board answer`` — answering a run the profile gate parked on ``needs_answers``.

The writer (``web.control.save_answers``) was reachable over HTTP only; the verb
puts it behind the one channel the IDE can drive. The property worth guarding is
the refusal: ``save_answers`` clears *every* ``human_questions`` entry at once, so
an answer bound to the wrong question would take the others down silently. Every
test below that expects a refusal also asserts that no ``profile-answers.json``
was written — a refusal that still writes is not a refusal.
"""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

import pytest

from mini_ork.cli import board_cmd
from mini_ork.stores import migrate as mig

REPO = Path(__file__).resolve().parents[2]
RUN = "run-1791000000-aaaaaa"


@pytest.fixture()
def home(tmp_path: Path) -> Path:
    h = tmp_path / "proj" / ".mini-ork"
    h.mkdir(parents=True)
    rc, _out, err = mig.init_db(db=str(h / "state.db"), root=str(REPO))
    assert rc == 0, err
    return h


def _parked(home: Path, run_id: str, questions: list[str], *,
            answers: dict[str, str] | None = None, recipe: str = "code-fix") -> Path:
    """A run dir holding a profile the gate parked, plus its ``task_runs`` row."""
    run_dir = home / "runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "run_profile.json").write_text(json.dumps({
        "profile_status": "needs_answers" if answers is None else "ready",
        "confidence": 0.4,
        "recipe": recipe,
        "human_questions": questions,
        "answers": answers or {},
    }))
    if answers is not None:
        (run_dir / "profile-answers.json").write_text(json.dumps(answers))
    (run_dir / "kickoff.md").write_text(f"---\nrecipe: {recipe}\n---\n# parked\n")
    con = sqlite3.connect(home / "state.db")
    now = int(time.time())
    con.execute("INSERT INTO task_runs (id, recipe, status, cost_usd, created_at, updated_at, "
                "task_class, kickoff_path, workflow_version) VALUES (?,?,?,?,?,?,?,?,?)",
                (run_id, recipe, "planned", 0.1, now, now, "code_fix", "", "latest"))
    con.commit()
    con.close()
    return run_dir


def _board(home: Path, *argv: str, capsys) -> tuple[int, dict]:
    rc = board_cmd.main([*argv, "--home", str(home)], "")
    out = json.loads(capsys.readouterr().out)
    return rc, out


def _answers_file(run_dir: Path) -> dict | None:
    path = run_dir / "profile-answers.json"
    return json.loads(path.read_text()) if path.exists() else None


def test_one_pending_question_is_answered_by_a_bare_text(home: Path, capsys) -> None:
    run_dir = _parked(home, RUN, ["Which lane should this run use?"])
    rc, payload = _board(home, "answer", RUN, "--text", "codex", capsys=capsys)
    assert rc == 0 and payload["ok"] is True
    assert _answers_file(run_dir) == {"Which lane should this run use?": "codex"}
    # save_answers flips the gate and clears the questions, so the run is no
    # longer parked — that is what makes the next `board start` allowed.
    profile = json.loads((run_dir / "run_profile.json").read_text())
    assert profile["profile_status"] == "ready" and profile["human_questions"] == []
    # FR-021: the follow-up command is handed back untouched.
    assert payload["next_step_cli"].startswith("mini-ork run code-fix")
    assert payload["answered"] == [{"id": "q1", "text": "Which lane should this run use?"}]


def test_several_pending_refuses_and_writes_nothing(home: Path, capsys) -> None:
    run_dir = _parked(home, RUN, ["Which lane?", "Which workspace?"])
    rc, payload = _board(home, "answer", RUN, "--text", "codex", capsys=capsys)
    assert rc == 1 and payload["ok"] is False
    assert [q["id"] for q in payload["questions"]] == ["q1", "q2"]
    assert "2 pending questions" in payload["error"]
    assert _answers_file(run_dir) is None, "a refusal must not write an answer"
    assert json.loads((run_dir / "run_profile.json").read_text())["human_questions"] == [
        "Which lane?", "Which workspace?"]


def test_paired_for_text_answers_several_in_one_call(home: Path, capsys) -> None:
    run_dir = _parked(home, RUN, ["Which lane?", "Which workspace?"])
    rc, payload = _board(home, "answer", RUN,
                         "--for", "q2", "--text", "in-place",
                         "--for", "q1", "--text", "codex", capsys=capsys)
    assert rc == 0 and payload["ok"] is True
    assert _answers_file(run_dir) == {"Which lane?": "codex", "Which workspace?": "in-place"}


def test_for_accepts_the_question_text_itself(home: Path, capsys) -> None:
    run_dir = _parked(home, RUN, ["Which lane?"])
    rc, payload = _board(home, "answer", RUN,
                         "--for", "Which lane?", "--text", "codex", capsys=capsys)
    assert rc == 0 and payload["ok"] is True
    assert _answers_file(run_dir) == {"Which lane?": "codex"}


def test_unknown_question_id_refuses(home: Path, capsys) -> None:
    run_dir = _parked(home, RUN, ["Which lane?"])
    rc, payload = _board(home, "answer", RUN, "--for", "q9", "--text", "x", capsys=capsys)
    assert rc == 1 and payload["ok"] is False
    assert "no pending question 'q9'" in payload["error"]
    assert _answers_file(run_dir) is None


def test_for_text_count_mismatch_refuses(home: Path, capsys) -> None:
    run_dir = _parked(home, RUN, ["Which lane?", "Which workspace?"])
    rc, payload = _board(home, "answer", RUN,
                         "--for", "q1", "--for", "q2", "--text", "only-one", capsys=capsys)
    assert rc == 1 and payload["ok"] is False
    assert "2 --for with 1 --text" in payload["error"]
    assert _answers_file(run_dir) is None


def test_a_run_with_no_pending_questions_refuses(home: Path, capsys) -> None:
    run_dir = _parked(home, RUN, ["Which lane?"], answers={"Which lane?": "codex"})
    rc, payload = _board(home, "answer", RUN, "--text", "again", capsys=capsys)
    assert rc == 1 and payload["ok"] is False
    assert "no pending questions" in payload["error"]
    assert _answers_file(run_dir) == {"Which lane?": "codex"}, "the old answer stands"


def test_a_run_that_never_asked_refuses(home: Path, capsys) -> None:
    run_dir = home / "runs" / RUN
    run_dir.mkdir(parents=True)
    rc, payload = _board(home, "answer", RUN, "--text", "x", capsys=capsys)
    assert rc == 1 and payload["ok"] is False
    assert "no pending questions" in payload["error"]
    assert _answers_file(run_dir) is None


def test_an_unknown_run_is_a_usage_error(home: Path, capsys) -> None:
    rc, payload = _board(home, "answer", "run-nope", "--text", "x", capsys=capsys)
    assert rc == 1 and payload["ok"] is False
    assert payload["stage"] == "usage" and "not found" in payload["error"]


def test_text_is_required(home: Path, capsys) -> None:
    _parked(home, RUN, ["Which lane?"])
    rc = board_cmd.main(["answer", RUN, "--home", str(home)], "")
    assert rc == 2
    assert "--text is required" in capsys.readouterr().err


def test_steer_still_reads_its_text(home: Path, capsys) -> None:
    """``--text`` is ``append`` now, shared with ``steer`` — the join keeps the
    single-value contract the steering table was written against."""
    _parked(home, RUN, ["Which lane?"])
    rc = board_cmd.main(["steer", RUN, "--text", "watch", "--text", "the tests",
                         "--home", str(home)], "")
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    con = sqlite3.connect(home / "state.db")
    rows = con.execute("SELECT message FROM operator_steering WHERE run_id = ?", (RUN,)).fetchall()
    con.close()
    assert any(r[0] == "watch the tests" for r in rows)
