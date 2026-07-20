"""Standalone golden and behavioral contracts for the sole classify runtime.

The Bash oracle was retired only after the run-local pre-retirement parity
receipt passed. These tests preserve the verified public output, exit-code,
database, trace, override, size-limit, and hostile-input contracts without a
second runtime implementation.
"""
from __future__ import annotations

import io
import os
import re
import sqlite3
import sys
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
from mini_ork.cli import classify as cls


@pytest.fixture
def home(tmp_path: Path) -> Path:
    h = tmp_path / ".mini-ork"
    task_classes = h / "config" / "task_classes"
    task_classes.mkdir(parents=True)
    (task_classes / "code_fix.yaml").write_text(
        "default_workflow_version: v7\n"
        "matches:\n  keywords: [bug, fix, regression, failing test]\n"
        "  regex: ['stack ?trace']\n",
        encoding="utf-8",
    )
    (task_classes / "research_synthesis.yaml").write_text(
        "matches:\n  keywords: [literature, survey, arxiv, SOTA]\n",
        encoding="utf-8",
    )
    (h / "recipes").mkdir()

    con = sqlite3.connect(h / "state.db")
    con.executescript(
        """
        CREATE TABLE task_runs (
            id TEXT PRIMARY KEY, task_class TEXT, recipe TEXT,
            workflow_version TEXT, kickoff_path TEXT, status TEXT,
            trace_id TEXT, created_at INTEGER, updated_at INTEGER
        );
        CREATE TABLE execution_traces (
            trace_id TEXT PRIMARY KEY, run_id TEXT, task_class TEXT,
            prompt_version_hash TEXT, context_bundle_hash TEXT,
            tool_calls TEXT, files_read TEXT, files_written TEXT,
            verifier_output TEXT, reviewer_verdict TEXT, cost_usd REAL,
            duration_ms INTEGER, final_artifact_ref TEXT, status TEXT,
            workflow_version_id TEXT, agent_version_id TEXT,
            objective_domain TEXT, segment TEXT, reward_primary_metric TEXT,
            reward_direction TEXT, reward_value REAL, reward_anchor REAL,
            reward_g REAL, reward_vector_json TEXT, reward_source TEXT,
            validity TEXT
        );
        """
    )
    con.close()
    return h


def _run(home: Path, argv: list[str], **env_extra: str) -> tuple[str, str, int]:
    stdout, stderr = io.StringIO(), io.StringIO()
    clean_env = {
        key: value for key, value in os.environ.items()
        if key not in {
            "MINI_ORK_DRY_RUN", "MINI_ORK_RUN_ID", "MINI_ORK_RECIPE",
            "MINI_ORK_DB", "MINI_ORK_HOME", "MO_MAX_KICKOFF_BYTES",
        }
    }
    clean_env.update({
        "MINI_ORK_HOME": str(home),
        "MINI_ORK_DB": str(home / "state.db"),
        **env_extra,
    })
    with patch.dict(os.environ, clean_env, clear=True):
        with redirect_stdout(stdout), redirect_stderr(stderr):
            rc = cls.main(argv, db=str(home / "state.db"), root=str(REPO))
    return stdout.getvalue(), stderr.getvalue(), rc


@pytest.mark.parametrize(("text", "expected_class", "expected_version"), [
    ("This has a nasty bug and a failing test; here is the stack trace.", "code_fix", "v7"),
    ("A literature survey of arxiv SOTA methods.", "research_synthesis", "latest"),
    ("Totally unrelated content about gardening.", "generic", "latest"),
])
def test_classification_golden(
    tmp_path: Path, home: Path, text: str, expected_class: str, expected_version: str,
) -> None:
    kickoff = tmp_path / "kickoff.md"
    kickoff.write_text(text, encoding="utf-8")

    out, err, rc = _run(home, [str(kickoff), "--dry-run"])

    assert rc == 0
    assert err == ""
    assert out == (
        f"task_class={expected_class}\n"
        f"workflow_version={expected_version}\n"
        f"kickoff={kickoff}\n"
        f"[dry-run] would write task_class={expected_class} to DB run row\n"
    )


def test_force_class_and_workflow_version_override(tmp_path: Path, home: Path) -> None:
    kickoff = tmp_path / "force.md"
    kickoff.write_text("has a bug", encoding="utf-8")

    out, err, rc = _run(home, [
        "--task-class", "custom_thing", "--workflow-version", "v99",
        "--dry-run", str(kickoff),
    ])

    assert (rc, err) == (0, "")
    assert out.startswith("task_class=custom_thing\nworkflow_version=v99\n")


def test_missing_file_unknown_flag_and_extra_arg_exit_two(tmp_path: Path, home: Path) -> None:
    missing = tmp_path / "missing.md"
    assert _run(home, [str(missing)])[2] == 2
    assert _run(home, ["--hostile-flag"])[2] == 2

    kickoff = tmp_path / "valid.md"
    kickoff.write_text("valid", encoding="utf-8")
    assert _run(home, [str(kickoff), "unexpected"])[2] == 2


def test_kickoff_size_limit_exits_two(tmp_path: Path, home: Path) -> None:
    kickoff = tmp_path / "large.md"
    kickoff.write_text("x" * 2048, encoding="utf-8")

    out, err, rc = _run(home, [str(kickoff)], MO_MAX_KICKOFF_BYTES="1024")

    assert (out, rc) == ("", 2)
    assert err == "classify: kickoff exceeds MO_MAX_KICKOFF_BYTES\n"


def test_non_dry_run_writes_task_and_trace_contract(tmp_path: Path, home: Path) -> None:
    kickoff = tmp_path / "write.md"
    kickoff.write_text("fix a regression", encoding="utf-8")

    out, err, rc = _run(
        home, [str(kickoff)], MINI_ORK_RUN_ID="run-golden",
        MINI_ORK_RECIPE="code-fix",
    )

    assert (rc, err) == (0, "")
    assert out.endswith("run_id=run-golden\n")
    con = sqlite3.connect(home / "state.db")
    task = con.execute(
        "SELECT id, task_class, recipe, workflow_version, kickoff_path, status, trace_id "
        "FROM task_runs WHERE id='run-golden'"
    ).fetchone()
    trace = con.execute(
        "SELECT run_id, task_class, status, workflow_version_id "
        "FROM execution_traces WHERE trace_id=?", (task[6],)
    ).fetchone()
    con.close()

    assert task[:6] == (
        "run-golden", "code_fix", "code-fix", "v7", str(kickoff), "classified",
    )
    assert re.fullmatch(r"tr-classify-\d+-\d+", task[6])
    assert trace == ("run-golden", "__classify__", "success", "classify-start")


def test_dry_run_writes_neither_task_nor_trace(tmp_path: Path, home: Path) -> None:
    kickoff = tmp_path / "dry.md"
    kickoff.write_text("fix a bug", encoding="utf-8")

    assert _run(home, ["--dry-run", str(kickoff)])[2] == 0
    con = sqlite3.connect(home / "state.db")
    assert con.execute("SELECT COUNT(*) FROM task_runs").fetchone()[0] == 0
    assert con.execute("SELECT COUNT(*) FROM execution_traces").fetchone()[0] == 0
    con.close()


def test_reclassify_preserves_existing_task_trace_id(tmp_path: Path, home: Path) -> None:
    kickoff = tmp_path / "repeat.md"
    kickoff.write_text("fix a bug", encoding="utf-8")

    assert _run(home, [str(kickoff)], MINI_ORK_RUN_ID="run-repeat")[2] == 0
    con = sqlite3.connect(home / "state.db")
    first_trace = con.execute(
        "SELECT trace_id FROM task_runs WHERE id='run-repeat'"
    ).fetchone()[0]
    con.close()

    kickoff.write_text("a literature survey", encoding="utf-8")
    assert _run(home, [str(kickoff)], MINI_ORK_RUN_ID="run-repeat")[2] == 0
    con = sqlite3.connect(home / "state.db")
    row = con.execute(
        "SELECT task_class, trace_id FROM task_runs WHERE id='run-repeat'"
    ).fetchone()
    con.close()

    assert row == ("research_synthesis", first_trace)
