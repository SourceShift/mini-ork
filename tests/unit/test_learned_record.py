"""Standalone contracts for the learned-record path (F5-B / learn-inject).

These tests cover the two surfaces that didn't exist before this kickoff:

- The ``sources`` out-param on ``failure_modes_md``,
  ``_static_emergent_block``, ``semantic_lessons_md`` and ``_learned_block``:
  what was injected, in prompt order, with enough identity to be inspected
  by the IDE "Learning" tab (F5-B, learn-inject kickoff).
- ``_write_learned_record``: the JSON+markdown artifacts persisted under
  ``<run_dir>/learned/<node_id>.{md,json}`` so the IDE can render what the
  learner actually saw. Atomic (tmp + ``os.replace``), never raises.

No network. A fresh temp DB is built per test via the ``db`` fixture.
"""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
from mini_ork import context_assembler as ca  # noqa: E402
from mini_ork.cli import execute, execute_handlers  # noqa: E402


@pytest.fixture
def db(tmp_path_factory):
    home = tmp_path_factory.mktemp("home")
    dbp = str(home / "state.db")
    subprocess.run(["bash", str(REPO / "db" / "init.sh")],
                   env={**os.environ, "MINI_ORK_HOME": str(home), "MINI_ORK_DB": dbp},
                   capture_output=True, text=True, check=True)
    con = sqlite3.connect(dbp)
    now = int(time.time())
    grads = [
        ("g1", "auth.middleware", "tests skipped silently",
         "run pytest -x", "e", 0.9, now, "code-fix"),
        ("g2", "workflow.gate", "framework-internal lesson",
         "fix gate", "e", 0.8, now, "code-fix"),
    ]
    con.executemany(
        "INSERT INTO gradient_records (gradient_id, target, signal, suggested_change,"
        " evidence, confidence, created_at, task_class) VALUES (?,?,?,?,?,?,?,?)", grads)
    con.commit()
    con.close()
    return dbp


def _seed_emergent(db, rows):
    """rows: (pattern_id, cluster_label, features_list, strength, status, lesson_text_or_None)."""
    con = sqlite3.connect(db)
    now = int(time.time())
    for pid, label, feats, strength, status, lesson in rows:
        con.execute(
            "INSERT INTO emergent_patterns (pattern_id, cluster_label, "
            "member_item_ids_json, feature_set_json, strength_score, "
            "suggested_meta_adr, status, lesson_text, detected_at) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (pid, label, "[]", json.dumps(feats), strength,
             "meta-adr text", status, lesson, now))
    con.commit()
    con.close()


def _read_json_path(run_dir, node_id):
    with open(os.path.join(run_dir, "learned", f"{node_id}.json"),
              encoding="utf-8") as f:
        return json.load(f)


# ── sources out-param on the prompt path ────────────────────────────────────


def test_failure_modes_sources_match_injected_order(db, monkeypatch):
    """``sources`` mirrors the injected gradient rows in prompt order, byte-for-byte.

    The markdown returned with and without a ``sources`` list must be the same
    — the out-param is observation only, never a mutation of the block.
    """
    monkeypatch.setenv("MINI_ORK_DB", db)
    monkeypatch.delenv("MO_TARGET_CWD", raising=False)
    monkeypatch.delenv("MO_EMERGENT_INJECT", raising=False)
    src: list[dict] = []
    md_with = ca.failure_modes_md("code-fix", 5, db=db, sources=src)
    md_without = ca.failure_modes_md("code-fix", 5, db=db)
    assert md_with == md_without, "sources must not mutate the block"
    assert [s["kind"] for s in src] == ["gradient", "gradient"]
    assert [s["id"] for s in src] == ["g1", "g2"]
    assert all("target" in s and "signal" in s and "suggested_change" in s
               for s in src)


def test_static_path_only_injects_lessoned_patterns(db, monkeypatch):
    """The static degradation path skips approved rows with NULL/blank
    lesson_text; ``MO_EMERGENT_INJECT_UNLESSONED=1`` restores the prior
    behaviour. A pattern with a lesson is in ``sources``; one without is not.
    """
    monkeypatch.setenv("MINI_ORK_DB", db)
    monkeypatch.delenv("MO_TARGET_CWD", raising=False)
    monkeypatch.setenv("MO_SEMANTIC_INJECT", "0")
    _seed_emergent(db, [
        ("emg-yes", "taught lesson", ["adr"], 7.0, "approved",
         "taught lesson: an authored pattern authored by a model"),
        ("emg-no",  "frequency count", ["adr"], 9.0, "approved", None),
    ])
    src: list[dict] = []
    md = ca.failure_modes_md("code-fix", 5, db=db, sources=src)
    assert "taught lesson" in md
    assert "frequency count" not in md, "un-lessoned pattern must be skipped"
    pattern_sources = [s for s in src if s["kind"] == "pattern"]
    assert [s["id"] for s in pattern_sources] == ["emg-yes"]
    assert pattern_sources[0]["text"] == (
        "taught lesson: an authored pattern authored by a model"
    )

    # Opt-out — both injected again, sources carry both.
    monkeypatch.setenv("MO_EMERGENT_INJECT_UNLESSONED", "1")
    src2: list[dict] = []
    md2 = ca.failure_modes_md("code-fix", 5, db=db, sources=src2)
    assert "taught lesson" in md2 and "frequency count" in md2
    assert sorted(s["id"] for s in src2 if s["kind"] == "pattern") == [
        "emg-no", "emg-yes",
    ]


def test_semantic_path_only_injects_lessoned_patterns(db, monkeypatch):
    """Same rule on the utility-ranked path: an authored lesson must be
    present, the un-lessoned row is filtered out. The pattern_id survives
    the memory_id round-trip in ``sources``."""
    monkeypatch.setenv("MINI_ORK_DB", db)
    monkeypatch.delenv("MO_TARGET_CWD", raising=False)
    monkeypatch.setenv("MO_SEMANTIC_INJECT", "1")
    _seed_emergent(db, [
        ("emg-yes", "taught lesson", ["adr"], 7.0, "approved",
         "taught lesson: an authored pattern authored by a model"),
        ("emg-no",  "frequency count", ["adr"], 9.0, "approved", None),
    ])
    src: list[dict] = []
    md = ca.failure_modes_md("code-fix", 5, db=db, sources=src)
    assert "taught lesson" in md
    assert "frequency count" not in md, "un-lessoned pattern must be filtered"
    pattern_sources = [s for s in src if s["kind"] == "pattern"]
    assert [s["id"] for s in pattern_sources] == ["emg-yes"]


def test_no_lessoned_patterns_omits_the_header(db, monkeypatch):
    """When every approved pattern lacks a lesson, the entire block is
    dropped — not emitted with an empty body."""
    monkeypatch.setenv("MINI_ORK_DB", db)
    monkeypatch.delenv("MO_TARGET_CWD", raising=False)
    _seed_emergent(db, [
        ("emg-1", "frequency count", ["adr"], 7.0, "approved", None),
        ("emg-2", "another count", ["adr"], 5.0, "approved", ""),
    ])
    md = ca.failure_modes_md("code-fix", 5, db=db)
    assert "Lessons from recurring patterns" not in md
    assert "Verified emergent patterns" not in md, "old header is gone"


# ── _write_learned_record contract ──────────────────────────────────────────


def test_write_learned_record_persists_block_and_sources(tmp_path, monkeypatch):
    """With a real block: ``<node_id>.md`` carries ``block.strip() + "\\n"``,
    ``<node_id>.json`` has the exact schema, and ``sources`` matches what
    was collected upstream. Atomic — even a tmp suffix survives a final
    replace."""
    monkeypatch.setenv("MO_INJECT_LEARNINGS", "1")
    run_dir = str(tmp_path / "run")
    os.makedirs(run_dir)
    block = "\n\n  --- Learned failure modes ---\n- [auth.middleware] tests skipped\n--- /learned ---\n"
    sources = [
        {"kind": "gradient", "id": "g1", "target": "auth.middleware",
         "signal": "tests skipped", "suggested_change": "run pytest -x"},
        {"kind": "steering", "id": 17, "severity": "WARN",
         "source": "human:reviewer", "message": "keep an honest run"},
    ]
    execute_handlers._write_learned_record(
        run_dir, "implementer", "implementer", "minimax", "code-fix",
        attempt=2, block=block, sources=sources,
    )
    md_path = os.path.join(run_dir, "learned", "implementer.md")
    with open(md_path, encoding="utf-8") as f:
        md = f.read()
    assert md == block.strip() + "\n"
    rec = _read_json_path(run_dir, "implementer")
    assert rec["node_id"] == "implementer"
    assert rec["node_type"] == "implementer"
    assert rec["lane"] == "minimax"
    assert rec["task_class"] == "code-fix"
    assert rec["attempt"] == 2
    assert isinstance(rec["written_at"], int) and rec["written_at"] > 0
    assert rec["injected"] is True
    assert rec["reason"] == ""
    assert rec["sources"] == sources


def test_write_learned_record_with_empty_block_removes_md_and_reports_reason(
        tmp_path, monkeypatch):
    """An empty block writes only the JSON record (with ``injected: False``
    and ``reason: "nothing matched"``) and removes any stale ``.md`` from a
    prior attempt — so the IDE never shows a previous attempt's text as if
    it were the current one."""
    monkeypatch.setenv("MO_INJECT_LEARNINGS", "1")
    run_dir = str(tmp_path / "run")
    learned_dir = os.path.join(run_dir, "learned")
    os.makedirs(learned_dir)
    stale = os.path.join(learned_dir, "implementer.md")
    with open(stale, "w", encoding="utf-8") as f:
        f.write("stale block from a previous attempt\n")

    execute_handlers._write_learned_record(
        run_dir, "implementer", "implementer", "minimax", "code-fix",
        attempt=3, block="", sources=[],
    )
    assert not os.path.exists(stale), "stale .md must be removed"
    rec = _read_json_path(run_dir, "implementer")
    assert rec["injected"] is False
    assert rec["reason"] == "nothing matched"
    assert rec["sources"] == []
    assert rec["attempt"] == 3


def test_write_learned_record_optout_reason(tmp_path, monkeypatch):
    """``MO_INJECT_LEARNINGS != "1"`` is a hard opt-out: ``reason`` is set
    even when a block is passed in (a caller that bypasses the upstream gate
    still sees the user's intent reflected in the record)."""
    monkeypatch.setenv("MO_INJECT_LEARNINGS", "0")
    run_dir = str(tmp_path / "run")
    os.makedirs(run_dir)
    execute_handlers._write_learned_record(
        run_dir, "researcher", "researcher", "glm", "code-fix",
        attempt=1, block="(block that should not have been passed)",
        sources=[{"kind": "gradient", "id": "g1"}],
    )
    rec = _read_json_path(run_dir, "researcher")
    assert rec["reason"] == "opt-out"
    assert rec["injected"] is False


def test_write_learned_record_swallows_unwritable_run_dir(tmp_path):
    """An unwritable ``run_dir`` MUST NOT raise — the record is observability
    only and an exception would interrupt a node dispatch whose LLM spend
    already happened. The dispatch continues exactly as before.

    ``blocker`` is a regular file, so ``os.makedirs(learned_dir)`` raises
    ``FileExistsError`` (a subclass of ``OSError``) on the first syscall —
    the except branch fires and nothing is written.
    """
    blocker = tmp_path / "blocker"
    blocker.write_text("a regular file, not a directory")
    bogus = str(blocker / "run" / "learned" / "x.json")
    execute_handlers._write_learned_record(
        bogus, "implementer", "implementer", "minimax", "code-fix",
        attempt=1, block="x", sources=[],
    )
    # The blocker file is unchanged; no descendant was created inside it.
    # ``os.listdir`` on a regular file raises ``NotADirectoryError`` — that
    # is the same OS error the implementation is supposed to swallow, so the
    # assertion doubles as a sanity check that nothing in the test environment
    # has reshaped ``blocker`` into a directory.
    assert blocker.is_file()
    assert blocker.read_text() == "a regular file, not a directory"
    with pytest.raises(NotADirectoryError):
        os.listdir(blocker)


# ── _learned_block sources ──────────────────────────────────────────────────


class _FakeSteering:
    """Stand-in for ``mini_ork.steering.operator_steering``."""

    @staticmethod
    def fetch_for(_run_id, _node_type):
        return [
            {"id": 1, "severity": "info", "source": "human:cli",
             "message": "be terse"},
            {"id": 2, "severity": "warn", "source": "human:cli",
             "message": "use the planning skill"},
        ]


def test_learned_block_collects_sources_for_steering(db, monkeypatch):
    """``_learned_block`` collects steering sources in prompt order and
    matches the schema declared in the prompt path. Uses the ``db`` fixture so
    ``failure_modes_md`` does not raise before the steering block is reached.
    """
    monkeypatch.setenv("MO_INJECT_LEARNINGS", "1")
    monkeypatch.setenv("MINI_ORK_DB", db)
    monkeypatch.delenv("MO_RUN_ID", raising=False)
    # Disable the emergent-pattern injection so the only sources collected are
    # the steering rows — keeps the assertion focused.
    monkeypatch.delenv("MO_EMERGENT_INJECT", raising=False)

    # ``mini_ork.steering`` is a package and ``operator_steering`` a submodule.
    # The lazy ``from mini_ork.steering import operator_steering`` in
    # ``_learned_block`` binds against
    # ``sys.modules['mini_ork.steering.operator_steering']``; replace that
    # entry for the duration of the test.
    monkeypatch.setitem(sys.modules, "mini_ork.steering.operator_steering",
                        _FakeSteering)
    # ``from pkg import sub`` reads the package ATTRIBUTE before sys.modules:
    # once any earlier test imported the real submodule (test_context_assembler
    # does), the attribute wins and the fake above is bypassed. Patch both.
    import mini_ork.steering as _steering_pkg
    monkeypatch.setattr(_steering_pkg, "operator_steering", _FakeSteering, raising=False)

    sources: list[dict] = []
    block = execute._learned_block(
        None, "code-fix", "implementer", lane="minimax",
        node_id="implementer", sources=sources,
    )
    assert "Operator steering" in block
    steering_sources = [s for s in sources if s["kind"] == "steering"]
    assert len(steering_sources) == 2
    assert steering_sources[0]["id"] == 1 and steering_sources[0]["severity"] == "INFO"
    assert (steering_sources[0]["source"] == "human:cli"
            and steering_sources[0]["message"] == "be terse")
    assert steering_sources[1]["id"] == 2 and steering_sources[1]["severity"] == "WARN"
