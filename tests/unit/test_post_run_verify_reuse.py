"""Post-run verify reuses the DAG's verifier results.

After ``execute``, the run flow runs ``mini_ork.cli.verify`` on the run's
artifact. Without reuse it re-runs every ``artifact_contract.success_verifiers``
script unconditionally — including the ones whose workflow verifier node already
ran inside the run and persisted its result to ``<run_dir>/verifier_<stem>.json``.
On a rolled-back run that second execution runs the suite against the reverted
tree and truncates the run's only copy of the failure evidence, so the run's
failure evidence is destroyed and the suite is duplicated on an unchanged tree
even on green runs. These tests pin the reuse path and its ``MO_VERIFY_RERUN=1``
escape hatch, reusing the fixtures shared with ``test_mini_ork_verify_py``.
"""
from __future__ import annotations

import io
import json
import os
import re
from contextlib import redirect_stderr, redirect_stdout

# Sibling test module (tests/unit is on sys.path under pytest's default import
# mode); it inserts the repo root into sys.path, so the engine import below
# resolves without an editable install.
from test_mini_ork_verify_py import REPO, _scenario

from mini_ork.cli import verify as ver


def _write_stub(home, marker):
    """A verifier that would create ``marker`` if the reuse path ever ran it."""
    (home / "verifiers" / "test.py").write_text(
        "import pathlib\n"
        f"pathlib.Path({str(marker)!r}).write_text('ran')\n"
        "print('{\"verifier\":\"test\",\"pass\":true}')\n",
        encoding="utf-8",
    )


def _dag_payload(run_dir, payload):
    """Write ``<run_dir>/verifier_test.json`` — the file the verifier node
    persists for ``success_verifiers: ["verifiers/test.py"]``."""
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "verifier_test.json").write_text(json.dumps(payload), encoding="utf-8")


def _run(home, db, plan, *, run_dir=None, rerun=False):
    """Run ``verify.main`` in-process with ``MINI_ORK_RUN_DIR`` set (unlike the
    shared ``_verify``, which pops it). Returns (doc, stdout, stderr, rc)."""
    out, err = io.StringIO(), io.StringIO()
    old = dict(os.environ)
    os.environ["MINI_ORK_HOME"] = str(home)
    # Scrub the run-scoped knobs so the scenario is hermetic: MINI_ORK_RECIPE in
    # particular would make ``_find_verifier_script`` prefer the real
    # ``recipes/<recipe>/verifiers/test.py`` over this scenario's stub.
    for key in ("MINI_ORK_DRY_RUN", "MINI_ORK_PLAN_PATH", "MINI_ORK_TASK_CLASS",
                "MINI_ORK_RECIPE"):
        os.environ.pop(key, None)
    if run_dir is None:
        os.environ.pop("MINI_ORK_RUN_DIR", None)
    else:
        os.environ["MINI_ORK_RUN_DIR"] = str(run_dir)
    if rerun:
        os.environ["MO_VERIFY_RERUN"] = "1"
    else:
        os.environ.pop("MO_VERIFY_RERUN", None)
    try:
        with redirect_stdout(out), redirect_stderr(err):
            rc = ver.main(["art.txt", "--plan", plan], db=db, root=str(REPO))
    finally:
        os.environ.clear()
        os.environ.update(old)
    text = out.getvalue()
    doc = json.loads(text[text.index("{"):])
    return doc, text, err.getvalue(), rc


DECLARED = "verifiers/test.py"  # the contract name, and so the row's identity


def _one(doc, name):
    rows = [r for r in doc["results"] if r["verifier"] == name]
    assert len(rows) == 1, rows
    return rows[0]


def test_reused_dag_failure_is_not_rerun(tmp_path):
    # A DAG result that failed is reused (not re-run) and counts one fail — the
    # rolled-back-tree case from the kickoff: the failure evidence survives.
    home, db, plan = _scenario(tmp_path, ["verifiers/test.py"])
    marker = tmp_path / "ran.marker"
    _write_stub(home, marker)
    run_dir = tmp_path / "run"
    _dag_payload(run_dir, {"verifier": "test", "pass": False,
                           "error_summary": "cargo rc 101"})

    doc, text, err, rc = _run(home, db, plan, run_dir=run_dir)

    assert not marker.exists(), "reuse path must not execute the verifier script"
    assert doc["pass_count"] == 0 and doc["fail_count"] == 1
    assert doc["verdict"] == "fail" and rc == 1
    row = _one(doc, DECLARED)
    assert row["pass"] is False and row["reused"] == "dag"
    assert '"reused":"dag"' in text
    assert "[verify] verifiers/test.py: reused DAG result" in err


def test_reused_non_boolean_pass_counts_a_fail(tmp_path):
    # Kickoff: "True -> pass; anything else -> fail". A reused result whose
    # ``pass`` is neither True nor None (here the string "false") must count one
    # fail. Treating it as neither — the ``is False`` bug — fails OPEN: the run
    # resolves to 'vacuous', and with a declared non-empty output artifact it
    # launders into 'pass'.
    home, db, plan = _scenario(tmp_path, ["verifiers/test.py"])
    marker = tmp_path / "ran.marker"
    _write_stub(home, marker)
    run_dir = tmp_path / "run"
    _dag_payload(run_dir, {"verifier": "test", "pass": "false",
                           "error_summary": "cargo rc 101"})

    doc, text, err, rc = _run(home, db, plan, run_dir=run_dir)

    assert not marker.exists(), "the non-boolean result must be reused, not re-run"
    assert doc["pass_count"] == 0 and doc["fail_count"] == 1
    assert doc["verdict"] == "fail" and rc == 1
    row = _one(doc, DECLARED)
    assert row["pass"] == "false" and row["reused"] == "dag"
    assert row["detail"] == "cargo rc 101"


def test_reused_dag_pass_with_evidence_is_reused(tmp_path):
    # A passing DAG result with real, non-empty evidence is reused as a pass and
    # keeps its original evidence_path (no new evidence file is synthesized).
    home, db, plan = _scenario(tmp_path, ["verifiers/test.py"])
    marker = tmp_path / "ran.marker"
    _write_stub(home, marker)
    run_dir = tmp_path / "run"
    evidence = run_dir / "evidence.log"
    run_dir.mkdir(parents=True, exist_ok=True)
    evidence.write_text("all green\n", encoding="utf-8")
    _dag_payload(run_dir, {"verifier": "test", "pass": True,
                           "evidence_path": str(evidence)})

    doc, text, err, rc = _run(home, db, plan, run_dir=run_dir)

    assert not marker.exists()
    assert doc["pass_count"] == 1 and doc["fail_count"] == 0
    assert doc["verdict"] == "pass" and rc == 0
    row = _one(doc, DECLARED)
    assert row["pass"] is True and row["reused"] == "dag"
    assert row["evidence_path"] == str(evidence)
    assert '"reused":"dag"' in text


def test_rerun_flag_forces_execution(tmp_path):
    # MO_VERIFY_RERUN=1 restores today's behaviour even when a DAG result exists.
    home, db, plan = _scenario(tmp_path, ["verifiers/test.py"])
    marker = tmp_path / "ran.marker"
    _write_stub(home, marker)
    run_dir = tmp_path / "run"
    _dag_payload(run_dir, {"verifier": "test", "pass": False, "error_summary": "boom"})

    doc, text, err, rc = _run(home, db, plan, run_dir=run_dir, rerun=True)

    assert marker.exists(), "MO_VERIFY_RERUN=1 must re-run the verifier"
    assert "reused DAG result" not in err
    assert '"reused":"dag"' not in text


def test_no_dag_result_runs_script(tmp_path):
    # No verifier_test.json → unchanged behaviour: the script runs.
    home, db, plan = _scenario(tmp_path, ["verifiers/test.py"])
    marker = tmp_path / "ran.marker"
    _write_stub(home, marker)
    run_dir = tmp_path / "run"
    run_dir.mkdir(parents=True, exist_ok=True)

    doc, text, err, rc = _run(home, db, plan, run_dir=run_dir)

    assert marker.exists()
    assert "reused DAG result" not in err


def test_reused_result_without_evidence_falls_back(tmp_path):
    # A result whose evidence file is missing and with no error_summary fails the
    # minimum-evidence assertion → treated as absent and the script runs.
    home, db, plan = _scenario(tmp_path, ["verifiers/test.py"])
    marker = tmp_path / "ran.marker"
    _write_stub(home, marker)
    run_dir = tmp_path / "run"
    _dag_payload(run_dir, {"verifier": "test", "pass": True,
                           "evidence_path": str(tmp_path / "gone.log")})

    doc, text, err, rc = _run(home, db, plan, run_dir=run_dir)

    assert marker.exists(), "an evidence-less result must not be certified"
    assert "reused DAG result" not in err


def test_log_prefixed_result_is_reused(tmp_path):
    # The executor copies the verifier's *log* (stdout+stderr merged) to
    # verifier_<stem>.json, so a real file has log lines ahead of the payload.
    # A whole-file json.load would miss it — and the feature would be green in
    # tests yet dead in production.
    home, db, plan = _scenario(tmp_path, ["verifiers/test.py"])
    marker = tmp_path / "ran.marker"
    _write_stub(home, marker)
    run_dir = tmp_path / "run"
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "verifier_test.json").write_text(
        "[test] running: pytest\n"
        '{"verifier":"test","pass":true,"error_summary":"ok"}\n',
        encoding="utf-8",
    )

    doc, text, err, rc = _run(home, db, plan, run_dir=run_dir)

    assert not marker.exists()
    assert doc["pass_count"] == 1 and doc["verdict"] == "pass"
    assert '"reused":"dag"' in text


def test_reused_row_is_fixed_shape(tmp_path):
    # A real verifier payload carries its own keys — notably ``verdict``
    # (verifier_test.json / verifier_static-check.json both emit
    # {"verdict": "pass"}). Splicing the payload into the row leaks that nested
    # key into verify's stdout, where main.py:1020-1022 reads the LAST
    # '"verdict":"…"' match as the run verdict: a reused pass-shaped payload
    # would then report an overall 'fail' as 'pass'. The row copies only the
    # fields the verdict computation and run log need.
    home, db, plan = _scenario(tmp_path, ["verifiers/test.py"])
    marker = tmp_path / "ran.marker"
    _write_stub(home, marker)
    run_dir = tmp_path / "run"
    _dag_payload(run_dir, {"verifier": "test", "pass": False, "verdict": "pass",
                           "checks": [{"id": "x", "pass": True}],
                           "error_summary": "cargo rc 101"})

    doc, text, err, rc = _run(home, db, plan, run_dir=run_dir)

    assert not marker.exists()
    assert doc["verdict"] == "fail" and rc == 1
    row = _one(doc, DECLARED)
    assert set(row) <= {"verifier", "pass", "evidence_path", "reused", "detail"}
    assert "verdict" not in row and "checks" not in text
    assert row == {"verifier": DECLARED, "pass": False, "evidence_path": None,
                   "reused": "dag", "detail": "cargo rc 101"}
    # main.py's verdict sink: the last '"verdict"' match in stdout must be the
    # overall verdict, not a nested payload's.
    assert re.findall(r'"verdict"\s*:\s*"([^"]+)"', text)[-1] == doc["verdict"]
