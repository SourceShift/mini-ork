"""``mini-ork verifier list|annotate`` — the operator half of the loop.

The ``verifier_results`` writer (``verifier_result_record``) is called from the
run path (`execute_handlers._record_verifier_result`). The labelling writer
(``verifier_result_annotate``) sets the ground-truth ``is_false_positive`` /
``is_false_negative`` columns that ``gates.abstain_gate`` calibrates against —
but before this CLI it had no caller, so those labels could never be set. These
tests drive the CLI end to end against a ``db/init.sh``-scaffolded SQLite.
"""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
from mini_ork.cli import verifier as vcli  # noqa: E402
from mini_ork.cli.main import SUBCOMMAND_REGISTRY  # noqa: E402
from mini_ork.gates.verifier_rubric import verifier_result_record  # noqa: E402


def _seed(tmp: Path, monkeypatch) -> str:
    home = tmp / "db" / ".mini-ork"
    home.mkdir(parents=True)
    db = str(home / "state.db")
    subprocess.run(["bash", str(REPO / "db" / "init.sh")],
                   env={**os.environ, "MINI_ORK_HOME": str(home), "MINI_ORK_DB": db},
                   capture_output=True, text=True, check=True)
    # _db_path reads the env, not root — point it at this temp DB.
    monkeypatch.setenv("MINI_ORK_HOME", str(home))
    monkeypatch.setenv("MINI_ORK_DB", db)
    return db


def _flag(db: str, result_id: str) -> tuple[int, int]:
    con = sqlite3.connect(db)
    try:
        fp, fn = con.execute(
            "SELECT is_false_positive, is_false_negative FROM verifier_results "
            "WHERE result_id=?", (result_id,)).fetchone()
    finally:
        con.close()
    return fp, fn


def test_registered_as_native_subcommand() -> None:
    # The registry builds {sub: handler} from _NATIVE_SUBS; a missing name means
    # `mini-ork verifier` falls through to "unknown subcommand".
    assert "verifier" in SUBCOMMAND_REGISTRY


def test_list_prints_rows_and_json(tmp_path, monkeypatch, capsys) -> None:
    db = _seed(tmp_path, monkeypatch)
    r1 = verifier_result_record(db, "run-a", "pre-retirement-parity", "pass")
    r2 = verifier_result_record(db, "run-a", "lint-gate", "fail")

    rc = vcli.main(["list"], root=str(tmp_path))
    out = capsys.readouterr().out
    assert rc == 0
    assert r1 in out and r2 in out
    assert "pre-retirement-parity" in out and "lint-gate" in out
    assert out.count("[-]") == 2  # neither row is labelled yet

    rc = vcli.main(["list", "--json"], root=str(tmp_path))
    doc = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert {d["result_id"] for d in doc} == {r1, r2}
    assert all(d["verdict"] in ("pass", "fail") for d in doc)


def test_list_run_and_unannotated_filters(tmp_path, monkeypatch, capsys) -> None:
    db = _seed(tmp_path, monkeypatch)
    a = verifier_result_record(db, "run-a", "pre-retirement-parity", "pass")
    verifier_result_record(db, "run-b", "lint-gate", "fail")

    vcli.main(["list", "--run", "run-a"], root=str(tmp_path))
    assert capsys.readouterr().out.count("\n") == 1

    vcli.main(["list", "--run", "nope"], root=str(tmp_path))
    assert capsys.readouterr().out == ""

    vcli.main(["annotate", "--result-id", a, "--kind", "false_positive"], root=str(tmp_path))
    capsys.readouterr()
    vcli.main(["list", "--unannotated"], root=str(tmp_path))
    out = capsys.readouterr().out
    assert a not in out and "lint-gate" in out


def test_annotate_sets_the_ground_truth_flag(tmp_path, monkeypatch) -> None:
    db = _seed(tmp_path, monkeypatch)
    rid = verifier_result_record(db, "run-a", "pre-retirement-parity", "fail")

    rc = vcli.main(["annotate", "--result-id", rid, "--kind", "false_negative",
                    "--annotator", "amir", "--notes", "gate over-tight"],
                   root=str(tmp_path))
    assert rc == 0
    assert _flag(db, rid) == (0, 1)
    con = sqlite3.connect(db)
    try:
        who, notes = con.execute(
            "SELECT annotated_by, notes FROM verifier_results WHERE result_id=?",
            (rid,)).fetchone()
    finally:
        con.close()
    assert who == "amir" and notes == "gate over-tight"


def test_annotate_rejects_unknown_result_id(tmp_path, monkeypatch, capsys) -> None:
    _seed(tmp_path, monkeypatch)
    rc = vcli.main(["annotate", "--result-id", "vr-deadbeef0000",
                    "--kind", "false_positive"], root=str(tmp_path))
    assert rc == 1
    assert "no result" in capsys.readouterr().err


def test_annotate_double_flag_raises_integrity(tmp_path, monkeypatch, capsys) -> None:
    db = _seed(tmp_path, monkeypatch)
    rid = verifier_result_record(db, "run-a", "pre-retirement-parity", "fail")
    assert vcli.main(["annotate", "--result-id", rid, "--kind", "false_positive"],
                     root=str(tmp_path)) == 0
    capsys.readouterr()
    # The cross-flag CHECK forbids both labels on one row → rc 1, not a crash.
    rc = vcli.main(["annotate", "--result-id", rid, "--kind", "false_negative"],
                   root=str(tmp_path))
    assert rc == 1
    assert "verifier:" in capsys.readouterr().err
