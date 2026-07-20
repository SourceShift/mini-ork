"""Standalone unit tests for ``mini_ork.orchestration.workflow_lifecycle``.

Replaces the bash-parity gate as part of the bash->Python migration: the
Python port is now the sole implementation, so its coverage no longer runs
``lib/workflow_lifecycle.sh`` in a subprocess -- it asserts the port's
behaviour directly against an in-memory-equivalent sqlite schema (just the
two tables the port touches: ``workflow_memory`` and ``workflow_candidates``,
mirroring db/migrations/0009_memory_namespaces.sql and
0010_benchmarks.sql). These pin the deterministic contract the bash
originally implemented: baseline idempotence, the task_class -> recipe-dir
underscore/hyphen convention, mutation JSON shaping, candidate_id
auto-generation, ON CONFLICT DO NOTHING semantics, and the FileNotFoundError/
ValueError error contract.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

from mini_ork.orchestration import workflow_lifecycle as wl

WORKFLOW_MEMORY_SCHEMA = """
CREATE TABLE workflow_memory (
  workflow_version_id       TEXT PRIMARY KEY,
  workflow_name             TEXT NOT NULL,
  base_version_id           TEXT,
  yaml_hash                 TEXT NOT NULL,
  yaml_blob                 TEXT NOT NULL,
  mutations                 TEXT NOT NULL DEFAULT '[]',
  status                    TEXT NOT NULL DEFAULT 'candidate'
);
"""

WORKFLOW_CANDIDATES_SCHEMA = """
CREATE TABLE workflow_candidates (
  candidate_id              TEXT PRIMARY KEY,
  base_workflow_version_id  TEXT NOT NULL,
  mutations                 TEXT NOT NULL DEFAULT '[]',
  status                    TEXT NOT NULL DEFAULT 'candidate',
  utility_delta             REAL NOT NULL DEFAULT 0.0,
  created_by                TEXT NOT NULL DEFAULT 'evolution_engine'
);
"""


@pytest.fixture
def db(tmp_path: Path) -> str:
    """A throwaway sqlite file with just the two tables the port touches."""
    path = str(tmp_path / "state.db")
    con = sqlite3.connect(path)
    try:
        con.executescript(WORKFLOW_MEMORY_SCHEMA + WORKFLOW_CANDIDATES_SCHEMA)
        con.commit()
    finally:
        con.close()
    return path


@pytest.fixture
def root(tmp_path: Path) -> str:
    """A fake repo root with a couple of recipes/<name>/workflow.yaml fixtures."""
    r = tmp_path / "repo"
    (r / "recipes" / "code-fix").mkdir(parents=True)
    (r / "recipes" / "code-fix" / "workflow.yaml").write_text(
        "name: code-fix\nnodes: []\n", encoding="utf-8"
    )
    (r / "recipes" / "generic").mkdir(parents=True)
    (r / "recipes" / "generic" / "workflow.yaml").write_text(
        "name: generic\nnodes: []\n", encoding="utf-8"
    )
    # underscore-only recipe dir (no hyphen variant exists) exercises the
    # bash convention's fallback-to-literal-task_class branch.
    (r / "recipes" / "foo_bar").mkdir(parents=True)
    (r / "recipes" / "foo_bar" / "workflow.yaml").write_text(
        "name: foo_bar\nnodes: []\n", encoding="utf-8"
    )
    return str(r)


def _row(db_path: str, sql: str, params: tuple = ()) -> tuple | None:
    con = sqlite3.connect(db_path)
    try:
        return con.execute(sql, params).fetchone()
    finally:
        con.close()


def _rows(db_path: str, sql: str) -> list[tuple]:
    con = sqlite3.connect(db_path)
    try:
        return con.execute(sql).fetchall()
    finally:
        con.close()


class TestDbPathAndRootHelpers:
    def test_db_path_prefers_explicit_arg(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("MINI_ORK_DB", "/env/path.db")
        assert wl._db_path("/explicit/path.db") == "/explicit/path.db"

    def test_db_path_falls_back_to_env(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("MINI_ORK_DB", "/env/path.db")
        assert wl._db_path(None) == "/env/path.db"

    def test_db_path_raises_when_unset(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.delenv("MINI_ORK_DB", raising=False)
        with pytest.raises(RuntimeError, match="MINI_ORK_DB unset"):
            wl._db_path(None)

    def test_root_prefers_explicit_arg(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("MINI_ORK_ROOT", "/env/root")
        assert wl._root("/explicit/root") == "/explicit/root"

    def test_root_falls_back_to_env(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("MINI_ORK_ROOT", "/env/root")
        assert wl._root(None) == "/env/root"

    def test_root_defaults_to_dot_when_unset(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.delenv("MINI_ORK_ROOT", raising=False)
        assert wl._root(None) == "."


class TestEnsureBaseline:
    def test_creates_row_with_expected_shape(self, db: str, root: str):
        version_id = wl.ensure_baseline("code-fix", db=db, root=root)
        assert version_id == "code-fix_v0.1.0"

        yaml_blob = Path(root, "recipes", "code-fix", "workflow.yaml").read_text(
            encoding="utf-8"
        )
        expected_hash = hashlib.sha256(yaml_blob.encode("utf-8")).hexdigest()

        row = _row(
            db,
            "SELECT workflow_version_id, workflow_name, base_version_id, "
            "yaml_hash, yaml_blob, mutations, status FROM workflow_memory "
            "WHERE workflow_version_id = ?",
            (version_id,),
        )
        assert row == (
            "code-fix_v0.1.0",
            "code-fix",
            None,
            expected_hash,
            yaml_blob,
            "[]",
            "promoted",
        )

    def test_idempotent_second_call_does_not_duplicate(self, db: str, root: str):
        first = wl.ensure_baseline("code-fix", db=db, root=root)
        second = wl.ensure_baseline("code-fix", db=db, root=root)
        assert first == second == "code-fix_v0.1.0"
        assert len(_rows(db, "SELECT * FROM workflow_memory")) == 1

    def test_task_class_argument_is_accepted_but_does_not_change_output(
        self, db: str, root: str
    ):
        # Faithful to the bash: task_class is accepted (2nd positional) but
        # never referenced by the SQL/hash logic -- recipe alone drives the
        # version_id and yaml lookup.
        version_id = wl.ensure_baseline(
            "code-fix", task_class="totally-unrelated", db=db, root=root
        )
        assert version_id == "code-fix_v0.1.0"

    def test_missing_workflow_yaml_raises_file_not_found(self, db: str, root: str):
        with pytest.raises(FileNotFoundError, match="no workflow.yaml"):
            wl.ensure_baseline("no-such-recipe", db=db, root=root)
        assert _rows(db, "SELECT * FROM workflow_memory") == []

    def test_uses_env_fallback_for_db_and_root(
        self, db: str, root: str, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setenv("MINI_ORK_DB", db)
        monkeypatch.setenv("MINI_ORK_ROOT", root)
        assert wl.ensure_baseline("code-fix") == "code-fix_v0.1.0"


class TestCandidateStore:
    def test_accepts_json_string_payload(self, db: str, root: str):
        payload = json.dumps({"task_class": "code_fix", "candidate_id": "wc-1"})
        candidate_id = wl.candidate_store(payload, db=db, root=root)
        assert candidate_id == "wc-1"

    def test_accepts_dict_payload(self, db: str, root: str):
        payload = {"task_class": "code_fix", "candidate_id": "wc-2"}
        candidate_id = wl.candidate_store(payload, db=db, root=root)
        assert candidate_id == "wc-2"

    def test_underscore_task_class_resolves_to_hyphen_recipe_dir(
        self, db: str, root: str
    ):
        # task_class=code_fix -> recipes/code-fix exists -> that's used.
        candidate_id = wl.candidate_store(
            {"task_class": "code_fix", "candidate_id": "wc-hyphen"}, db=db, root=root
        )
        assert candidate_id == "wc-hyphen"
        row = _row(
            db,
            "SELECT base_workflow_version_id FROM workflow_candidates "
            "WHERE candidate_id = ?",
            (candidate_id,),
        )
        assert row == ("code-fix_v0.1.0",)

    def test_falls_back_to_literal_task_class_when_hyphen_dir_missing(
        self, db: str, root: str
    ):
        # task_class=foo_bar -> recipes/foo-bar does NOT exist -> falls back
        # to the literal recipes/foo_bar (which the `root` fixture provides).
        candidate_id = wl.candidate_store(
            {"task_class": "foo_bar", "candidate_id": "wc-literal"}, db=db, root=root
        )
        row = _row(
            db,
            "SELECT base_workflow_version_id FROM workflow_candidates "
            "WHERE candidate_id = ?",
            (candidate_id,),
        )
        assert row == ("foo_bar_v0.1.0",)

    def test_default_task_class_is_generic(self, db: str, root: str):
        candidate_id = wl.candidate_store({"candidate_id": "wc-generic"}, db=db, root=root)
        row = _row(
            db,
            "SELECT base_workflow_version_id FROM workflow_candidates "
            "WHERE candidate_id = ?",
            (candidate_id,),
        )
        assert row == ("generic_v0.1.0",)

    def test_missing_candidate_id_is_auto_generated(self, db: str, root: str):
        candidate_id = wl.candidate_store({"task_class": "code_fix"}, db=db, root=root)
        assert candidate_id.startswith("wc-")
        assert len(candidate_id) == len("wc-") + 12

    def test_mutation_applied_is_wrapped_in_json_array(self, db: str, root: str):
        mutation = {"op": "swap_lane", "node": "reviewer"}
        candidate_id = wl.candidate_store(
            {
                "task_class": "code_fix",
                "candidate_id": "wc-mut",
                "mutation_applied": mutation,
            },
            db=db,
            root=root,
        )
        row = _row(
            db,
            "SELECT mutations FROM workflow_candidates WHERE candidate_id = ?",
            (candidate_id,),
        )
        assert row is not None
        assert json.loads(row[0]) == [mutation]

    def test_no_mutation_applied_defaults_to_empty_array(self, db: str, root: str):
        candidate_id = wl.candidate_store(
            {"task_class": "code_fix", "candidate_id": "wc-nomut"}, db=db, root=root
        )
        row = _row(
            db,
            "SELECT mutations FROM workflow_candidates WHERE candidate_id = ?",
            (candidate_id,),
        )
        assert row == ("[]",)

    def test_auto_creates_missing_baseline_fk_row(self, db: str, root: str):
        assert _rows(db, "SELECT * FROM workflow_memory") == []
        wl.candidate_store(
            {"task_class": "code_fix", "candidate_id": "wc-fk"}, db=db, root=root
        )
        assert _rows(db, "SELECT workflow_version_id FROM workflow_memory") == [
            ("code-fix_v0.1.0",)
        ]

    def test_does_not_duplicate_existing_baseline_row(self, db: str, root: str):
        wl.ensure_baseline("code-fix", db=db, root=root)
        wl.candidate_store(
            {"task_class": "code_fix", "candidate_id": "wc-existing"}, db=db, root=root
        )
        assert len(_rows(db, "SELECT * FROM workflow_memory")) == 1

    def test_on_conflict_candidate_id_does_nothing(self, db: str, root: str):
        first = wl.candidate_store(
            {"task_class": "code_fix", "candidate_id": "wc-dupe"}, db=db, root=root
        )
        # Second call with same candidate_id must not raise (ON CONFLICT DO
        # NOTHING) and must not create a duplicate row.
        second = wl.candidate_store(
            {"task_class": "code_fix", "candidate_id": "wc-dupe"}, db=db, root=root
        )
        assert first == second == "wc-dupe"
        assert len(
            _rows(db, "SELECT * FROM workflow_candidates WHERE candidate_id = 'wc-dupe'")
        ) == 1

    def test_invalid_json_string_raises(self, db: str, root: str):
        with pytest.raises(json.JSONDecodeError):
            wl.candidate_store("{not json", db=db, root=root)

    def test_non_dict_payload_raises_value_error(self, db: str, root: str):
        with pytest.raises(ValueError, match="expected object"):
            wl.candidate_store(json.dumps([1, 2, 3]), db=db, root=root)

    def test_missing_recipe_dir_raises_file_not_found(self, db: str, root: str):
        with pytest.raises(FileNotFoundError, match="no recipes/ dir"):
            wl.candidate_store({"task_class": "no_such_recipe"}, db=db, root=root)

    def test_missing_workflow_yaml_raises_file_not_found(
        self, db: str, root: str, tmp_path: Path
    ):
        (tmp_path / "repo2" / "recipes" / "empty-recipe").mkdir(parents=True)
        with pytest.raises(FileNotFoundError, match="no workflow.yaml"):
            wl.candidate_store(
                {"task_class": "empty-recipe"}, db=db, root=str(tmp_path / "repo2")
            )

    def test_uses_env_fallback_for_db_and_root(
        self, db: str, root: str, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setenv("MINI_ORK_DB", db)
        monkeypatch.setenv("MINI_ORK_ROOT", root)
        candidate_id = wl.candidate_store({"task_class": "code_fix", "candidate_id": "wc-env"})
        assert candidate_id == "wc-env"


class TestMissingRecipeErrorsCombined:
    """Mirrors the original parity test's combined error-contract assertion."""

    def test_missing_recipe_errors(self, db: str, root: str):
        with pytest.raises(FileNotFoundError):
            wl.ensure_baseline("no-such-recipe", db=db, root=root)
        with pytest.raises(FileNotFoundError):
            wl.candidate_store({"task_class": "no_such_recipe"}, db=db, root=root)
