"""``mini-ork lessons`` — list / forget / restore over ``emergent_patterns``."""
from __future__ import annotations

import io
import json
import sqlite3
import time
from pathlib import Path

import pytest

from mini_ork.cli import lessons_cmd
from mini_ork.stores import migrate as mig

REPO = Path(__file__).resolve().parents[2]


@pytest.fixture
def home(tmp_path: Path, monkeypatch) -> Path:
    """A temp home + DB, pointed at by MINI_ORK_HOME / MINI_ORK_DB.

    The verification command runs with those unset, so every test here must
    build its own — otherwise ``forget`` would write ``rejected`` into the
    operator's real ``emergent_patterns``.
    """
    h = tmp_path / "proj" / ".mini-ork"
    h.mkdir(parents=True)
    rc, _out, err = mig.init_db(db=str(h / "state.db"), root=str(REPO))
    assert rc == 0, err
    monkeypatch.setenv("MINI_ORK_HOME", str(h))
    monkeypatch.setenv("MINI_ORK_DB", str(h / "state.db"))
    return h


def _seed(home: Path, rows: list[tuple]) -> None:
    """rows: (pattern_id, status, lesson_text, strength_score)."""
    now = int(time.time())
    con = sqlite3.connect(home / "state.db")
    for pid, status, lesson, strength in rows:
        con.execute(
            "INSERT INTO emergent_patterns (pattern_id, cluster_label, member_item_ids_json, "
            "feature_set_json, strength_score, status, detected_at, lesson_text) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (pid, f"label-{pid}", "[]", "[]", strength, status, now, lesson))
    con.commit()
    con.close()


def _status(home: Path, pid: str) -> tuple[str, object]:
    con = sqlite3.connect(home / "state.db")
    row = con.execute("SELECT status, resolved_at FROM emergent_patterns WHERE pattern_id=?",
                      (pid,)).fetchone()
    con.close()
    return str(row[0]), row[1]


def test_list_shows_approved_and_proposed(home: Path) -> None:
    _seed(home, [("p-a", "approved", "Lesson A", 3.0),
                 ("p-p", "proposed", "Lesson P", 1.0),
                 ("p-r", "rejected", "Lesson R", 2.0)])
    out = io.StringIO()
    assert lessons_cmd.main(["list"], stdout=out, stderr=io.StringIO()) == 0
    text = out.getvalue()
    assert "Lesson A" in text and "Lesson P" in text
    assert "Lesson R" not in text          # rejected rows are not listed
    assert "approved" in text and "proposed" in text


def test_list_json_shape(home: Path) -> None:
    _seed(home, [("p-a", "approved", "Lesson A", 3.0)])
    out = io.StringIO()
    assert lessons_cmd.main(["list", "--json"], stdout=out, stderr=io.StringIO()) == 0
    payload = json.loads(out.getvalue().strip())
    assert payload[0]["pattern_id"] == "p-a"
    assert payload[0]["lesson"] == "Lesson A"
    assert payload[0]["status"] == "approved"
    assert payload[0]["seen_in_runs"] == 0      # no member traces seeded
    assert payload[0]["strength"] == 3.0


def test_forget_sets_rejected_and_resolved(home: Path) -> None:
    _seed(home, [("p-a", "approved", "Lesson A", 3.0)])
    assert lessons_cmd.main(["forget", "p-a"], stdout=io.StringIO(),
                            stderr=io.StringIO()) == 0
    status, resolved = _status(home, "p-a")
    assert status == "rejected"
    assert resolved is not None


def test_forget_is_idempotent_on_an_already_rejected_row(home: Path) -> None:
    _seed(home, [("p-r", "rejected", "Lesson R", 2.0)])
    assert lessons_cmd.main(["forget", "p-r"], stdout=io.StringIO(),
                            stderr=io.StringIO()) == 0


def test_restore_returns_rejected_to_approved(home: Path) -> None:
    _seed(home, [("p-r", "rejected", "Lesson R", 2.0)])
    assert lessons_cmd.main(["restore", "p-r"], stdout=io.StringIO(),
                            stderr=io.StringIO()) == 0
    assert _status(home, "p-r")[0] == "approved"


def test_restore_without_a_lesson_exits_2(home: Path) -> None:
    _seed(home, [("p-r", "rejected", "", 2.0)])
    err = io.StringIO()
    assert lessons_cmd.main(["restore", "p-r"], stdout=io.StringIO(), stderr=err) == 2
    assert "lesson" in err.getvalue().lower()
    assert _status(home, "p-r")[0] == "rejected"    # unchanged


def test_unknown_id_exits_2(home: Path) -> None:
    _seed(home, [("p-a", "approved", "Lesson A", 3.0)])
    for action in ("forget", "restore"):
        err = io.StringIO()
        assert lessons_cmd.main([action, "nope"], stdout=io.StringIO(), stderr=err) == 2
        assert "no such lesson" in err.getvalue().lower()


def test_usage_and_help(home: Path) -> None:
    assert lessons_cmd.main([], stdout=io.StringIO(), stderr=io.StringIO()) == 2
    assert lessons_cmd.main(["forget"], stdout=io.StringIO(), stderr=io.StringIO()) == 2
    out = io.StringIO()
    assert lessons_cmd.main(["--help"], stdout=out, stderr=io.StringIO()) == 0
    assert "lessons" in out.getvalue()
