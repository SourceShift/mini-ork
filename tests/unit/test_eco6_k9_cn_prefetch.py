"""ECOSYSTEM §6 · K9: persist the ContextNest block the engine injects.

The run's settle (``mini_ork.cli.execute._collect_cn_atom_ids``) can only
credit atoms the prompt actually carried if the engine writes the injected
block to ``$MINI_ORK_RUN_DIR/cn_prefetch/*.md``. These tests pin that contract
for the client helper, the assembler path, and the planner path. Hermetic:
tmp dirs, stub clients, no network.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from mini_ork import cn_client  # noqa: E402
from mini_ork.cli import execute as cli_execute  # noqa: E402
from mini_ork.cli import plan as cli_plan  # noqa: E402
from mini_ork.context_assembler import contextnest_atoms_md  # noqa: E402

ATOM_ID = "cn-275a1cef9eb1ba9c328a5fee8230defb"
PAYLOAD = json.dumps({
    "hits": [
        {
            "similarity": 0.8,
            "metadata": {"kind": "decision", "ts": "2026-10-25T00:00:00"},
            "session_id": "898364fe-a053",
            "id": ATOM_ID,
            "content": "c",
        }
    ]
})

# 200 chars, has a "## " heading, and carries no cn-<hex> token.
CAPSULE_TEXT = (
    "## Substrate digest\n"
    + "kind-ordered recap of recent decisions and lessons " * 4
)[:200]

BRIEF_TEXT = (
    "Persist the ContextNest block the engine injects so outcomes credit\n"
    "only the atoms this run was actually shown.\n"
)


class StubClient:
    def __init__(self, capsule_text=""):
        self.capsule_text = capsule_text
        self.retrieve_calls = []

    def available(self):
        return True

    def capsule(self, query="", since="14d"):
        return self.capsule_text

    def retrieve(self, query, limit):
        self.retrieve_calls.append((query, limit))
        return PAYLOAD

    render_atoms_md = staticmethod(cn_client.render_atoms_md)


def _write_brief(tmp_path):
    path = tmp_path / "brief.md"
    path.write_text(BRIEF_TEXT, encoding="utf-8")
    return str(path)


def _clear_knobs(monkeypatch):
    monkeypatch.delenv("MO_DISABLE_CN", raising=False)
    monkeypatch.delenv("CN_CAPSULE_MIN_CHARS", raising=False)


def _patch_cn_module(monkeypatch):
    monkeypatch.setattr(cn_client, "available", lambda: True)
    monkeypatch.setattr(cn_client, "capsule", lambda query="", since="14d": "")
    monkeypatch.setattr(cn_client, "retrieve", lambda query, limit: PAYLOAD)


def _engine_files(root):
    return sorted(p for p in root.rglob("engine-*.md") if p.is_file())


# --- persist_prefetch ------------------------------------------------------


def test_persist_prefetch_appends_block_and_returns_path(tmp_path, monkeypatch):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    monkeypatch.setenv("MINI_ORK_RUN_DIR", str(run_dir))
    assert not (run_dir / "cn_prefetch").exists()

    path = cn_client.persist_prefetch("block one", "context")

    expected = run_dir / "cn_prefetch" / "engine-context.md"
    assert path is not None
    assert os.path.realpath(path) == os.path.realpath(expected)
    assert expected.read_text(encoding="utf-8") == "block one\n"


def test_persist_prefetch_appends_not_overwrites(tmp_path, monkeypatch):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    monkeypatch.setenv("MINI_ORK_RUN_DIR", str(run_dir))

    cn_client.persist_prefetch("first", "context")
    cn_client.persist_prefetch("second", "context")

    stored = run_dir / "cn_prefetch" / "engine-context.md"
    assert stored.read_text(encoding="utf-8") == "first\nsecond\n"


def test_persist_prefetch_sanitizes_label(tmp_path, monkeypatch):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    monkeypatch.setenv("MINI_ORK_RUN_DIR", str(run_dir))

    cn_client.persist_prefetch("x", "a/b")
    assert (run_dir / "cn_prefetch" / "engine-a_b.md").read_text(
        encoding="utf-8"
    ) == "x\n"
    cn_client.persist_prefetch("y", "plan 1")
    assert (run_dir / "cn_prefetch" / "engine-plan_1.md").read_text(
        encoding="utf-8"
    ) == "y\n"


def test_persist_prefetch_empty_block_writes_nothing(tmp_path, monkeypatch):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    monkeypatch.setenv("MINI_ORK_RUN_DIR", str(run_dir))

    assert cn_client.persist_prefetch("", "context") is None
    assert cn_client.persist_prefetch("  \n\t", "context") is None
    assert _engine_files(run_dir) == []


def test_persist_prefetch_unset_or_empty_env_writes_nothing(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("MINI_ORK_RUN_DIR", raising=False)
    assert cn_client.persist_prefetch("x", "context") is None
    monkeypatch.setenv("MINI_ORK_RUN_DIR", "")
    assert cn_client.persist_prefetch("x", "context") is None
    assert _engine_files(tmp_path) == []


def test_persist_prefetch_absent_run_dir_writes_nothing(tmp_path, monkeypatch):
    missing = tmp_path / "no-such-run"
    monkeypatch.setenv("MINI_ORK_RUN_DIR", str(missing))

    assert cn_client.persist_prefetch("x", "context") is None
    assert not missing.exists()
    assert _engine_files(tmp_path) == []


def test_persist_prefetch_never_raises_when_run_dir_is_file(tmp_path, monkeypatch):
    blocker = tmp_path / "run-is-a-file"
    blocker.write_text("not a dir", encoding="utf-8")
    monkeypatch.setenv("MINI_ORK_RUN_DIR", str(blocker))

    assert cn_client.persist_prefetch("x", "a/b") is None


def test_persist_prefetch_oserror_returns_none(tmp_path, monkeypatch):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    # A regular file squatting on the cn_prefetch name forces an OSError.
    (run_dir / "cn_prefetch").write_text("not a dir", encoding="utf-8")
    monkeypatch.setenv("MINI_ORK_RUN_DIR", str(run_dir))

    assert cn_client.persist_prefetch("x", "context") is None


# --- context_assembler.contextnest_atoms_md --------------------------------


def test_assembler_atoms_block_lands_in_run_dir(tmp_path, monkeypatch):
    _clear_knobs(monkeypatch)
    monkeypatch.setenv("MINI_ORK_RUN_DIR", str(tmp_path))
    brief = _write_brief(tmp_path)
    stub = StubClient()

    text = contextnest_atoms_md(brief, client=stub)

    assert f"id={ATOM_ID}" in text
    assert text == cn_client.render_atoms_md(PAYLOAD, 5)
    stored = (tmp_path / "cn_prefetch" / "engine-context.md").read_text(
        encoding="utf-8"
    )
    assert text in stored
    assert len(stub.retrieve_calls) == 1


def test_assembler_settle_finds_persisted_atom(tmp_path, monkeypatch):
    _clear_knobs(monkeypatch)
    monkeypatch.setenv("MINI_ORK_RUN_DIR", str(tmp_path))
    brief = _write_brief(tmp_path)

    assert f"id={ATOM_ID}" in contextnest_atoms_md(brief, client=StubClient())
    assert cli_execute._collect_cn_atom_ids(str(tmp_path)) == [ATOM_ID]


def test_assembler_capsule_persists_without_retrieval(tmp_path, monkeypatch):
    _clear_knobs(monkeypatch)
    monkeypatch.setenv("MINI_ORK_RUN_DIR", str(tmp_path))
    brief = _write_brief(tmp_path)
    stub = StubClient(capsule_text=CAPSULE_TEXT)

    text = contextnest_atoms_md(brief, client=stub)

    assert CAPSULE_TEXT in text
    stored = (tmp_path / "cn_prefetch" / "engine-context.md").read_text(
        encoding="utf-8"
    )
    assert text in stored
    assert stub.retrieve_calls == []
    assert cli_execute._collect_cn_atom_ids(str(tmp_path)) == []


def test_assembler_no_run_dir_writes_nothing(tmp_path, monkeypatch):
    _clear_knobs(monkeypatch)
    monkeypatch.delenv("MINI_ORK_RUN_DIR", raising=False)
    monkeypatch.chdir(tmp_path)
    brief = _write_brief(tmp_path)

    text = contextnest_atoms_md(brief, client=StubClient())

    assert text == cn_client.render_atoms_md(PAYLOAD, 5)
    assert _engine_files(tmp_path) == []


def test_assembler_disabled_writes_nothing(tmp_path, monkeypatch):
    monkeypatch.setenv("MO_DISABLE_CN", "1")
    monkeypatch.setenv("MINI_ORK_RUN_DIR", str(tmp_path))
    brief = _write_brief(tmp_path)
    stub = StubClient()

    assert contextnest_atoms_md(brief, client=stub) == ""
    assert contextnest_atoms_md(str(tmp_path / "absent.md"), client=stub) == ""
    assert stub.retrieve_calls == []
    assert _engine_files(tmp_path) == []


def test_assembler_routes_persist_through_client_attribute(tmp_path, monkeypatch):
    _clear_knobs(monkeypatch)
    monkeypatch.setenv("MINI_ORK_RUN_DIR", str(tmp_path))
    brief = _write_brief(tmp_path)
    calls = []

    class RecordingClient(StubClient):
        def persist_prefetch(self, block, label):
            calls.append((block, label))
            return "recorded"

    text = contextnest_atoms_md(brief, client=RecordingClient())

    assert text == cn_client.render_atoms_md(PAYLOAD, 5)
    assert calls == [(text, "context")]
    # Client carried the hook, so the module-level fallback must not also run.
    assert _engine_files(tmp_path) == []


# --- cli.plan._contextnest_atoms_md ----------------------------------------


def test_planner_writes_engine_planner_md(tmp_path, monkeypatch):
    _clear_knobs(monkeypatch)
    _patch_cn_module(monkeypatch)
    monkeypatch.setenv("MINI_ORK_RUN_DIR", str(tmp_path))
    brief = _write_brief(tmp_path)

    text = cli_plan._contextnest_atoms_md(brief)

    assert f"id={ATOM_ID}" in text
    stored = tmp_path / "cn_prefetch" / "engine-planner.md"
    assert stored.exists()
    assert text in stored.read_text(encoding="utf-8")


def test_planner_block_settles(tmp_path, monkeypatch):
    _clear_knobs(monkeypatch)
    _patch_cn_module(monkeypatch)
    monkeypatch.setenv("MINI_ORK_RUN_DIR", str(tmp_path))
    brief = _write_brief(tmp_path)

    assert f"id={ATOM_ID}" in cli_plan._contextnest_atoms_md(brief)
    assert cli_execute._collect_cn_atom_ids(str(tmp_path)) == [ATOM_ID]


def test_planner_no_run_dir_writes_nothing(tmp_path, monkeypatch):
    _clear_knobs(monkeypatch)
    _patch_cn_module(monkeypatch)
    monkeypatch.delenv("MINI_ORK_RUN_DIR", raising=False)
    monkeypatch.chdir(tmp_path)
    brief = _write_brief(tmp_path)

    assert f"id={ATOM_ID}" in cli_plan._contextnest_atoms_md(brief)
    assert _engine_files(tmp_path) == []


def test_planner_never_raises_when_persist_raises(tmp_path, monkeypatch):
    _clear_knobs(monkeypatch)
    _patch_cn_module(monkeypatch)

    def boom(block, label):
        raise RuntimeError("persist exploded")

    monkeypatch.setattr(cn_client, "persist_prefetch", boom)
    monkeypatch.setenv("MINI_ORK_RUN_DIR", str(tmp_path))
    brief = _write_brief(tmp_path)

    assert isinstance(cli_plan._contextnest_atoms_md(brief), str)
