"""What happens to the non-text parts of an agent-server message.

``_flatten_message`` keeps only text — that is deliberate and stays true. But a
file part is content the user sent, and dropping it on the floor loses the turn.
This suite pins the two halves of the fix:

* the text half is UNCHANGED — ``_flatten_message`` still ignores a file part,
  and a message empty of both still 400s on the send endpoint
* the file half SURVIVES — each part is decoded under
  ``<home>/attachments/<thread_id>/`` and recorded as one ``MessageRole.FILE``
  row (empty text, stored path in ``attachments``) sharing the turn of the text
  it arrived with

and the security property the feature exists for: a hostile client filename
cannot walk out of the thread's attachment directory.

Hermetic: a throwaway ``tmp_path`` home and a monkeypatched ``launch_run``, so
nothing spawns a real run.
"""
from __future__ import annotations

import base64
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))


def _fake_launch_run_factory(calls: list[dict]):
    """Stand-in for ``control.launch_run``: records the call, always succeeds."""

    def fake(home, recipe, kickoff, run_id=None):
        calls.append({"home": home, "recipe": recipe, "kickoff": kickoff, "run_id": run_id})
        return {
            "ok": True,
            "run_id": run_id,
            "recipe": recipe,
            "pid": 4242,
            "kickoff_path": str(Path(home) / "runs-inbox" / f"{run_id}.md"),
            "log_path": str(Path(home) / "logs" / f"{run_id}.log"),
        }

    return fake


@pytest.fixture
def sent(tmp_path, monkeypatch):
    """A launched conversation on a hermetic home, plus its stored transcript.

    Returns ``(mod, thread_db, message_rows)`` where ``message_rows`` is a
    callable reading the thread's ``thread_messages`` rows through the real
    store seam (not raw SQL of our own).
    """
    from mini_ork.web.routes import agent_server as mod
    import mini_ork.web.control as control
    from mini_ork.threads import store as thread_store

    launches: list[dict] = []
    monkeypatch.setattr(control, "launch_run", _fake_launch_run_factory(launches))

    def send(conversation_id: str, payload: dict):
        mod.create_conversation({"conversation_id": conversation_id}, home=tmp_path)
        out = mod.send_conversation_event(payload, conversation_id, home=tmp_path)
        db = str(tmp_path / "state.db")
        return out, thread_store.messages(conversation_id, db=db)

    return mod, tmp_path, send, launches


def _payload(*parts) -> dict:
    return {"role": "user", "content": list(parts), "run": True}


# ── the file half: it survives ────────────────────────────────────────────────


def test_text_and_file_part_yields_a_file_row_sharing_the_turn(sent):
    """One text part + one file part → the text turn AND one FILE row whose
    attachment exists under the thread's attachments directory."""
    _mod, home, send, launches = sent
    blob = base64.b64encode(b"%PDF-1.4 fake").decode()

    out, rows = send(
        "conv-att-1",
        _payload(
            {"type": "text", "text": "look at this report"},
            {"type": "file", "name": "report.pdf", "mime_type": "application/pdf", "data": blob},
        ),
    )

    assert out["ok"] is True
    # The text part is the task; the file is named in its own section, by the
    # stored path — a run never told a file exists cannot read it.
    kickoff = launches[0]["kickoff"]
    assert "look at this report" in kickoff
    assert "## Attached files" in kickoff
    assert str(home / "attachments" / "conv-att-1" / "report.pdf") in kickoff

    assert [r.role for r in rows] == ["user", "file"]
    user, file_row = rows
    assert user.text == "look at this report"
    assert file_row.text == ""                       # no prose of its own
    assert file_row.turn == user.turn                # same round, not a new one
    assert len(file_row.attachments) == 1

    stored = Path(file_row.attachments[0])
    assert stored.is_file()
    assert stored.read_bytes() == b"%PDF-1.4 fake"
    assert stored.parent == home / "attachments" / "conv-att-1"
    assert stored.name == "report.pdf"               # mime-derived extension


def test_one_row_per_file_part(sent):
    """Two file parts are two rows, not one merged row."""
    _mod, _home, send, _launches = sent
    blob = base64.b64encode(b"0123456789").decode()

    _out, rows = send(
        "conv-att-2",
        _payload(
            {"type": "text", "text": "two files"},
            {"type": "file", "name": "a.txt", "mime_type": "text/plain", "data": blob},
            {"type": "file", "name": "b.txt", "mime_type": "text/plain", "data": blob},
        ),
    )

    file_rows = [r for r in rows if r.role == "file"]
    assert len(file_rows) == 2
    assert {Path(r.attachments[0]).name for r in file_rows} == {"a.txt", "b.txt"}


def test_unnamed_part_gets_an_allocated_integer_name(sent):
    """No client name, no problem: the next free integer names the file, so two
    unnamed parts in one turn do not overwrite each other."""
    _mod, home, send, _launches = sent
    blob = base64.b64encode(b"x").decode()

    _out, rows = send(
        "conv-att-3",
        _payload(
            {"type": "text", "text": "nameless"},
            {"type": "file", "mime_type": "image/png", "data": blob},
            {"type": "file", "mime_type": "image/png", "data": blob},
        ),
    )

    names = sorted(Path(r.attachments[0]).name for r in rows if r.role == "file")
    assert names == ["0.png", "1.png"]
    for row in (r for r in rows if r.role == "file"):
        assert Path(row.attachments[0]).parent == home / "attachments" / "conv-att-3"


def test_files_only_message_is_a_legitimate_turn(sent):
    """A dragged-in attachment with no prose still lands (and still launches)."""
    _mod, home, send, launches = sent
    blob = base64.b64encode(b"data").decode()

    out, rows = send(
        "conv-att-4",
        _payload({"type": "file", "name": "spec.md", "mime_type": "text/plain", "data": blob}),
    )

    assert out["ok"] is True
    assert len(launches) == 1
    file_rows = [r for r in rows if r.role == "file"]
    assert len(file_rows) == 1
    assert Path(file_rows[0].attachments[0]).parent == home / "attachments" / "conv-att-4"


# ── the security property: the name cannot escape the thread directory ────────


@pytest.mark.parametrize(
    "hostile",
    ["../../x", "../x", "..\\..\\x", "/etc/passwd", "....//....//x", "..", "."],
)
def test_hostile_filename_stays_inside_the_thread_dir(sent, hostile):
    """A client-supplied name is a leaf, never a path."""
    _mod, home, send, _launches = sent
    blob = base64.b64encode(b"evil").decode()

    _out, rows = send(
        "conv-att-5",
        _payload(
            {"type": "text", "text": "hostile"},
            {"type": "file", "name": hostile, "mime_type": "application/octet-stream", "data": blob},
        ),
    )

    file_rows = [r for r in rows if r.role == "file"]
    assert len(file_rows) == 1
    stored = Path(file_rows[0].attachments[0])
    thread_dir = (home / "attachments" / "conv-att-5").resolve()
    assert stored.resolve().parent == thread_dir
    assert stored.is_file()
    # Nothing was written above the thread directory.
    assert not (home / "x").exists()
    assert not (home.parent / "x").exists()


def test_sanitizer_drops_directories_and_keeps_the_leaf():
    from mini_ork.web.routes.agent_server import _sanitize_attachment_name

    assert _sanitize_attachment_name("../../x") == "x"
    assert _sanitize_attachment_name("..\\..\\x") == "x"
    assert _sanitize_attachment_name("/etc/passwd") == "passwd"
    assert _sanitize_attachment_name("notes/report.pdf") == "report.pdf"
    assert _sanitize_attachment_name("..") == ""
    assert _sanitize_attachment_name(".") == ""
    assert _sanitize_attachment_name("") == ""
    assert _sanitize_attachment_name(None) == ""


def test_extension_comes_from_the_mime_not_the_client_name(sent):
    """A client name ending .sh cannot choose the stored suffix."""
    _mod, _home, send, _launches = sent
    blob = base64.b64encode(b"#!/bin/sh\n").decode()

    _out, rows = send(
        "conv-att-6",
        _payload(
            {"type": "text", "text": "suffix"},
            {"type": "file", "name": "payload.sh", "mime_type": "application/pdf", "data": blob},
        ),
    )

    stored = Path(next(r for r in rows if r.role == "file").attachments[0])
    assert stored.name == "payload.pdf"


@pytest.mark.parametrize("mime, name, expected", [
    ("text/csv", "q3.csv", "q3.csv"),
    ("application/vnd.openxmlformats-officedocument.wordprocessingml.document", "brief.docx", "brief.docx"),
    ("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", "book.xlsx", "book.xlsx"),
    ("application/octet-stream", "deck.pptx", "deck.pptx"),   # generic mime: allow-listed suffix
    ("", "data.tsv", "data.tsv"),
    ("application/octet-stream", "run.sh", "run.bin"),         # not a document format
    ("application/octet-stream", "tool.exe", "tool.bin"),
])
def test_previewable_documents_keep_a_previewable_suffix(sent, mime, name, expected):
    """Office and CSV uploads must land with a suffix the thread page can preview;
    a generic mime only lends the client's suffix when it is a document format."""
    _mod, _home, send, _launches = sent
    blob = base64.b64encode(b"x").decode()
    part = {"type": "file", "name": name, "data": blob}
    if mime:
        part["mime_type"] = mime

    _out, rows = send("conv-att-doc", _payload({"type": "text", "text": "doc"}, part))

    stored = Path(next(r for r in rows if r.role == "file").attachments[0])
    assert stored.name == expected


def test_an_oversized_attachment_is_not_stored(sent, monkeypatch):
    """The decoded-size cap is checked on the encoded length, before decoding."""
    mod, home, send, _launches = sent
    monkeypatch.setattr(mod, "_MAX_ATTACHMENT_BYTES", 8)
    blob = base64.b64encode(b"0123456789abcdef").decode()

    out, rows = send(
        "conv-att-big",
        _payload({"type": "text", "text": "big"},
                 {"type": "file", "name": "big.pdf", "mime_type": "application/pdf", "data": blob}),
    )

    assert out["ok"] is True                         # the turn itself still lands
    assert [r.role for r in rows] == ["user"]        # no FILE row
    assert not (home / "attachments" / "conv-att-big").exists() or not any(
        (home / "attachments" / "conv-att-big").iterdir())


def test_files_on_the_first_message_are_kept(tmp_path, monkeypatch):
    """create_conversation's initial_message used to drop its file parts."""
    from mini_ork.web.routes import agent_server as mod
    import mini_ork.web.control as control
    from mini_ork.threads import store as thread_store

    monkeypatch.setattr(control, "launch_run", _fake_launch_run_factory([]))
    blob = base64.b64encode(b"a,b\n1,2\n").decode()
    mod.create_conversation({
        "conversation_id": "conv-att-first",
        "initial_message": _payload(
            {"type": "text", "text": "summarise this"},
            {"type": "file", "name": "t.csv", "mime_type": "text/csv", "data": blob}),
    }, home=tmp_path)

    rows = thread_store.messages("conv-att-first", db=str(tmp_path / "state.db"))
    assert [(r.role, r.turn) for r in rows] == [("user", 1), ("file", 1)]
    stored = Path(rows[1].attachments[0])
    assert stored.name == "t.csv" and stored.read_bytes() == b"a,b\n1,2\n"


# ── the text half: unchanged ──────────────────────────────────────────────────


def test_flatten_message_still_ignores_file_parts():
    from mini_ork.web.routes.agent_server import _flatten_message

    message = {
        "role": "user",
        "content": [
            {"type": "text", "text": "hello"},
            {"type": "file", "name": "x.bin", "data": "AAAA"},
        ],
    }
    assert _flatten_message(message) == "hello"
    assert _flatten_message({"role": "user", "content": []}) == ""


def test_empty_message_still_400s(tmp_path):
    """A message with no text AND no file part is still a 400 — the check is
    narrowed, not removed."""
    from fastapi import HTTPException
    from mini_ork.web.routes import agent_server as mod

    mod.create_conversation({"conversation_id": "conv-att-7"}, home=tmp_path)
    for payload in (
        {"role": "user", "content": []},
        {"role": "user", "content": ""},
        {"role": "user"},
    ):
        with pytest.raises(HTTPException) as exc:
            mod.send_conversation_event(payload, "conv-att-7", home=tmp_path)
        assert exc.value.status_code == 400


# ── the store seam the row rides on ───────────────────────────────────────────


def test_file_role_row_round_trips_through_the_store(tmp_path):
    """The row shape this feature writes reads back unchanged."""
    from mini_ork.threads import store
    from mini_ork.threads.model import MessageRole, ThreadOrigin

    db = str(tmp_path / "state.db")
    store.open_thread("t-1", title="t", origin=ThreadOrigin.IDE, recipe="code-fix", db=db)
    stored_path = "/tmp/attachments/t-1/report.pdf"
    store.append_message(
        "t-1", role=MessageRole.FILE, text="", turn=1, attachments=(stored_path,), db=db
    )

    rows = store.messages("t-1", db=db)
    assert len(rows) == 1
    assert rows[0].role is MessageRole.FILE
    assert rows[0].text == ""
    assert rows[0].attachments == (stored_path,)
    assert rows[0].turn == 1
