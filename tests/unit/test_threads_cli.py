"""Tests for ``mini-ork threads`` — the no-server tier.

The command is driven through :func:`mini_ork.threads.cli.main`, with
``launch_run``/``steer_run`` monkeypatched exactly as the service tests do, so
nothing spawns. ``--home`` points at a migrated tmp home, which is also what
keeps these off the developer's own ``MINI_ORK_DB``.

What is pinned beyond "it prints something": the four actions are each
reachable from the command line, ``--text -`` reads stdin, ``continue`` refuses
while a run is live (the one place it differs from ``send``), and ``--json`` is
parseable.
"""
from __future__ import annotations

import io
import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from mini_ork.stores import migrate as mig  # noqa: E402
from mini_ork.threads import cli, store  # noqa: E402


@pytest.fixture
def home(tmp_path: Path) -> Path:
    h = tmp_path / "proj" / ".mini-ork"
    h.mkdir(parents=True)
    rc, _, err = mig.init_db(db=str(h / "state.db"), root=str(REPO))
    assert rc == 0, err
    return h


@pytest.fixture(autouse=True)
def no_spawn(monkeypatch) -> None:
    """Both entry points every command routes through. Nothing launches."""
    import mini_ork.web.control as control

    monkeypatch.setattr(
        control, "launch_run",
        lambda home, recipe, kickoff, run_id=None: {
            "ok": True, "run_id": run_id, "recipe": recipe, "pid": 1,
            "kickoff_path": str(Path(home) / "runs-inbox" / f"{run_id}.md"),
        },
    )
    monkeypatch.setattr(control, "steer_run", lambda *a, **k: {"ok": True})


def _run(capsys, *argv: str) -> tuple[int, str, str]:
    code = cli.main(list(argv))
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def test_a_first_send_launches_and_reports_the_action(home: Path, capsys) -> None:
    code, out, _ = _run(
        capsys, "send", "t1", "--text", "Implement the retry budget.",
        "--title", "Retry budget", "--home", str(home),
    )
    assert code == 0
    assert "launched: thread t1 turn 1" in out
    assert "run t1" in out


def test_a_second_send_steers_and_says_so(home: Path, capsys) -> None:
    _run(capsys, "send", "t1", "--text", "first", "--home", str(home))
    code, out, _ = _run(capsys, "send", "t1", "--text", "more", "--home", str(home))
    assert code == 0
    assert "steered: thread t1" in out


def test_a_follow_up_after_the_run_ended_continues(home: Path, capsys) -> None:
    from mini_ork.threads import service
    from mini_ork.threads.model import RunStatus

    _run(capsys, "send", "t1", "--text", "first", "--home", str(home))
    service.on_run_terminal(home, "t1", RunStatus.FINISHED, db=str(home / "state.db"))

    code, out, _ = _run(capsys, "send", "t1", "--text", "more", "--home", str(home))
    assert code == 0
    assert "continued: thread t1 turn 2" in out
    assert "run t1-t2" in out


def test_continue_refuses_while_a_run_is_live(home: Path, capsys) -> None:
    _run(capsys, "send", "t1", "--text", "first", "--home", str(home))
    code, _, err = _run(capsys, "continue", "t1", "--text", "more", "--home", str(home))
    assert code == 1
    assert "is live" in err


def test_text_dash_reads_stdin(home: Path, capsys, monkeypatch) -> None:
    monkeypatch.setattr(sys, "stdin", io.StringIO("piped ticket body\n"))
    code, out, _ = _run(capsys, "send", "t1", "--text", "-", "--home", str(home))
    assert code == 0
    assert "launched" in out


def test_a_missing_text_is_a_usage_error(home: Path, capsys) -> None:
    code, _, err = _run(capsys, "send", "t1", "--home", str(home))
    assert code == 2
    assert "--text is required" in err


def test_list_and_show_render_a_thread(home: Path, capsys) -> None:
    _run(capsys, "send", "t1", "--text", "hello", "--title", "Greeting",
         "--home", str(home))

    code, out, _ = _run(capsys, "list", "--home", str(home))
    assert code == 0
    assert "t1" in out and "running" in out and "Greeting" in out

    code, out, _ = _run(capsys, "show", "t1", "--home", str(home))
    assert code == 0
    assert "# Greeting" in out
    assert "hello" in out
    assert "attempt 1" in out


def test_json_output_is_parseable(home: Path, capsys) -> None:
    _run(capsys, "send", "t1", "--text", "hi", "--home", str(home))

    _, out, _ = _run(capsys, "list", "--json", "--home", str(home))
    assert json.loads(out)[0]["id"] == "t1"

    _, out, _ = _run(capsys, "show", "t1", "--json", "--home", str(home))
    payload = json.loads(out)
    assert payload["thread"]["id"] == "t1"
    assert payload["messages"][0]["text"] == "hi"


def test_show_of_an_unknown_thread_is_an_error(home: Path, capsys) -> None:
    code, _, err = _run(capsys, "show", "nope", "--home", str(home))
    assert code == 1
    assert "no thread" in err


def test_an_unknown_verb_is_a_usage_error(capsys) -> None:
    code, _, err = _run(capsys, "frobnicate")
    assert code == 2
    assert "unknown verb" in err


# ── attach ──────────────────────────────────────────────────────────────────


def test_attach_copies_files_onto_one_turn_and_starts_nothing(
    home: Path, capsys, tmp_path: Path, monkeypatch
) -> None:
    import mini_ork.web.control as control

    _run(capsys, "send", "t1", "--text", "first", "--home", str(home))
    steered: list = []
    monkeypatch.setattr(control, "steer_run", lambda *a, **k: steered.append(a) or {"ok": True})
    a, b = tmp_path / "q3.csv", tmp_path / "brief.docx"
    a.write_text("x,y\n1,2\n")
    b.write_bytes(b"PK")

    code, out, _ = _run(capsys, "attach", "t1", str(a), str(b), "--home", str(home))

    assert code == 0
    stored = [line.split(": ", 1)[1] for line in out.splitlines()]
    assert [Path(p).name for p in stored] == ["q3.csv", "brief.docx"]
    assert all(Path(p).parent == home / "attachments" / "t1" for p in stored)
    rows = [r for r in store.messages("t1", db=str(home / "state.db")) if str(r.role) == "file"]
    assert len({r.turn for r in rows}) == 1 and len(rows) == 2
    assert steered == []                                  # attaching is not a request


def test_attach_needs_an_existing_thread_and_a_file(home: Path, capsys, tmp_path: Path) -> None:
    f = tmp_path / "a.txt"
    f.write_text("a")
    assert _run(capsys, "attach", "nope", str(f), "--home", str(home))[0] == 1
    _run(capsys, "send", "t1", "--text", "first", "--home", str(home))
    assert _run(capsys, "attach", "t1", "--home", str(home))[0] == 2
    code, _, err = _run(capsys, "attach", "t1", str(tmp_path / "missing.csv"), "--home", str(home))
    assert code == 1 and "not a file" in err
