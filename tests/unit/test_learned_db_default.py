"""Contracts for defaulting ``MINI_ORK_DB`` to ``$MINI_ORK_HOME/state.db``.

Before this kickoff a run whose environment set ``MINI_ORK_HOME`` but not
``MINI_ORK_DB`` got an EMPTY learned block: ``context_assembler._db_path``
raised ``RuntimeError("MINI_ORK_DB unset")`` and ``_learned_block`` swallowed
it in ``try/except`` — the lessons vanished with no signal. The fix makes the
resolution ladder (explicit arg → ``MINI_ORK_DB`` → ``$MINI_ORK_HOME/state.db``)
the one rule everywhere, and publishes the default at the run boundary
(``execute.main``) — but NOT at the top-level dispatcher. A process-wide publish
in ``main()`` is indistinguishable from an operator value: it shadows ``--home``
and leaks into ``launch_run`` children (see ``test_cli_main_does_not_publish_*``).

Hermetic by construction: every test seeds its own temp home / state.db and
sets the env with ``monkeypatch``; nothing reads an ambient ``MINI_ORK_DB`` /
``MINI_ORK_HOME`` (the verifier's ``scrubbed_test_env`` strips them anyway).
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
from mini_ork.cli import execute  # noqa: E402
from mini_ork.cli import main as cli_main  # noqa: E402

LESSON = "Default MINI_ORK_DB so learned lessons reach every run."


def _init_home(home: Path) -> str:
    """A temp ``MINI_ORK_HOME`` whose ``state.db`` holds one approved lesson."""
    dbp = str(home / "state.db")
    subprocess.run(["bash", str(REPO / "db" / "init.sh")],
                   env={**os.environ, "MINI_ORK_HOME": str(home),
                        "MINI_ORK_DB": dbp},
                   capture_output=True, text=True, check=True)
    con = sqlite3.connect(dbp)
    con.execute(
        "INSERT INTO emergent_patterns (pattern_id, cluster_label, "
        "member_item_ids_json, feature_set_json, strength_score, "
        "suggested_meta_adr, status, lesson_text, detected_at) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        ("p-default-db", "framework_edit", "[]",
         json.dumps(["framework_edit"]), 0.9, "meta-adr text", "approved",
         LESSON, int(time.time())))
    con.commit()
    con.close()
    return dbp


@pytest.fixture
def home(tmp_path):
    _init_home(tmp_path)
    return tmp_path


# ── _db_path resolution ladder (explicit → env → home default) ───────────────


def test_db_path_precedence_and_home_default(monkeypatch):
    """Explicit arg > ``MINI_ORK_DB`` > ``$MINI_ORK_HOME/state.db``, never raise."""
    monkeypatch.setenv("MINI_ORK_DB", "/env/state.db")
    monkeypatch.setenv("MINI_ORK_HOME", "/home")
    assert ca._db_path("/explicit.db") == "/explicit.db"
    assert ca._db_path(None) == "/env/state.db"

    monkeypatch.delenv("MINI_ORK_DB", raising=False)
    assert ca._db_path(None) == os.path.join("/home", "state.db")

    monkeypatch.delenv("MINI_ORK_HOME", raising=False)
    # Both unset resolves to the documented relative default — no raise.
    assert ca._db_path(None) == os.path.join(".mini-ork", "state.db")


def test_db_path_home_default_resolves_the_seeded_db(home, monkeypatch):
    monkeypatch.setenv("MINI_ORK_HOME", str(home))
    monkeypatch.delenv("MINI_ORK_DB", raising=False)
    assert ca._db_path(None) == str(home / "state.db")


# ── the learned block lights up on the home default ──────────────────────────


def test_learned_block_injects_lessons_with_db_unset(home, monkeypatch):
    """With ``MINI_ORK_DB`` unset, the block still reaches the seeded lesson and
    reports a ``pattern`` source — the exact failure this kickoff fixes."""
    monkeypatch.setenv("MINI_ORK_HOME", str(home))
    monkeypatch.delenv("MINI_ORK_DB", raising=False)
    monkeypatch.setenv("MO_INJECT_LEARNINGS", "1")
    monkeypatch.setenv("MO_SEMANTIC_INJECT", "0")   # static path
    monkeypatch.setenv("MO_CONTEXT_V2", "off")
    monkeypatch.delenv("MO_EMERGENT_INJECT", raising=False)
    monkeypatch.delenv("MO_INJECT_UNVERIFIED", raising=False)
    monkeypatch.delenv("MO_TARGET_CWD", raising=False)
    monkeypatch.delenv("MINI_ORK_RUN_DIR", raising=False)
    monkeypatch.delenv("MINI_ORK_RUN_ID", raising=False)

    src: list[dict] = []
    block = execute._learned_block(None, "framework_edit", "implementer",
                                   sources=src)
    assert LESSON in block, block
    patterns = [s for s in src if s["kind"] == "pattern"]
    assert patterns and patterns[0]["id"] == "p-default-db"


# ── both CLI entrypoints publish the default at startup ──────────────────────


def test_cli_main_does_not_publish_a_derived_db(home, monkeypatch):
    """``main()`` must NOT inject a derived ``MINI_ORK_DB`` into the process env.

    A published default is indistinguishable from an operator-set one. It would
    (a) shadow ``--home`` for subcommands that re-point the home for DB
    resolution (``lessons``, ``serve``) and (b) leak into
    ``web/control.py:launch_run`` children — those copy ``os.environ`` and re-pin
    ``MINI_ORK_HOME`` but not ``MINI_ORK_DB``, so a run for project X would read
    and write another project's ``state.db``.
    """
    monkeypatch.setenv("MINI_ORK_HOME", str(home))
    monkeypatch.delenv("MINI_ORK_DB", raising=False)
    monkeypatch.setenv("MINI_ORK_ROOT", str(REPO))
    monkeypatch.setattr(cli_main, "_load_secret_store_env", lambda: None)

    rc = cli_main.main(["help"])
    assert rc == 0
    assert "MINI_ORK_DB" not in os.environ


def test_cli_main_leaves_explicit_db_untouched(home, monkeypatch):
    monkeypatch.setenv("MINI_ORK_HOME", str(home))
    monkeypatch.setenv("MINI_ORK_DB", "/explicit/state.db")
    monkeypatch.setenv("MINI_ORK_ROOT", str(REPO))
    monkeypatch.setattr(cli_main, "_load_secret_store_env", lambda: None)

    cli_main.main(["help"])
    assert os.environ["MINI_ORK_DB"] == "/explicit/state.db"


def test_lessons_home_flag_resolves_its_home_db(home, monkeypatch, capsys):
    """Regression for the reviewer repro 2: ``lessons list --home <dir>`` must
    read the home named on the command line when neither ``MINI_ORK_DB`` nor
    ``MINI_ORK_HOME`` is set in the parent. A published default used to shadow
    ``--home``, so this listed nothing."""
    monkeypatch.delenv("MINI_ORK_DB", raising=False)
    monkeypatch.delenv("MINI_ORK_HOME", raising=False)
    from mini_ork.cli import lessons_cmd

    rc = lessons_cmd.main(["list", "--home", str(home)])
    assert rc == 0
    assert LESSON in capsys.readouterr().out


def test_execute_main_publishes_default_when_db_unset(home, monkeypatch):
    """``execute`` invoked directly (bypassing the dispatcher) publishes too."""
    monkeypatch.setenv("MINI_ORK_HOME", str(home))
    monkeypatch.delenv("MINI_ORK_DB", raising=False)
    monkeypatch.delenv("MINI_ORK_PLAN_PATH", raising=False)
    monkeypatch.delenv("MINI_ORK_RUN_DIR", raising=False)

    rc = execute.main([], root=str(REPO))
    # rc 2 == "no plan.json found"; the publish ran before that resolution.
    assert rc == 2
    assert os.environ["MINI_ORK_DB"] == str(home / "state.db")
