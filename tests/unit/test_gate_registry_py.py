"""Parity gate: ``mini_ork.ported.gate_registry`` vs ``lib/gate_registry.sh``.

Each test seeds a fresh temp DB via ``db/init.sh`` (the same way the
production system initialises state), then evaluates the SAME
operation through the LIVE bash function (sourced via ``bash -c
'source lib/gate_registry.sh; ...'``) AND through the Python port.
The test asserts verdict parity on the public surface.

Cases (eight, above the kickoff's >=6 floor):

  (1) register id format — Python vs bash produce identical row content
      modulo the random uuid suffix
  (2) budget_gate pass — cost_usd=5.0 vs limit=10.0
  (3) budget_gate fail boundary — cost_usd=10.0 (pass) and 10.0000001
      (fail) — exercises the <= comparator and the 1e-6 kickoff tolerance
  (4) human_gate always defers — constant 'defer' regardless of context
  (5) scope_gate pass and fail — task_class in/out of allowed list
  (6) evaluate unknown gate_id returns 'fail' on both sides
  (7) gate_list count and types — register 3 different gate_types and
      compare the sorted gate_type tuples between bash and Python
  (8) gate_run_all summary — aggregate verdict fields match

Floats: parity budget_gate verdict is a string, so no float equality
needed; the boundary case (3) verifies the <= comparator behaves
identically on both sides at 1e-6 precision.

Strangler-fig co-existence preserved: ``lib/gate_registry.sh`` is
untouched. The Python port is additive; lib callers (gate_bootstrap.sh)
keep depending on bash. The parity test exercises the LIVE bash
function via subprocess — no mocking, no hardcoded outputs.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
LIB_GATE_REGISTRY = REPO_ROOT / "lib" / "gate_registry.sh"
DB_INIT = REPO_ROOT / "db" / "init.sh"

sys.path.insert(0, str(REPO_ROOT))
from mini_ork.ported import gate_registry as gr  # noqa: E402

# gate_id format: 'gate-<gtype[:6]>-<uuid4().hex[:8]>'. The suffix is
# random; we only assert the prefix shape.
_GATE_ID_RE = re.compile(r"^gate-(.{0,6})-([0-9a-f]{8})$")


# ─────────────────────────────────────────────────────────────────────────────
# Fixtures — fresh temp DB per test, init via db/init.sh.
# ─────────────────────────────────────────────────────────────────────────────


@pytest.fixture()
def db(tmp_path):
    """Initialise a fresh SQLite file via db/init.sh.

    Sets MINI_ORK_HOME + MINI_ORK_DB in the test process so any
    subprocess we spawn (the bash side of the parity comparison)
    inherits them and finds the right DB path. Lazy CREATE TABLE
    in the port handles the gate_registry table.
    """
    home = tmp_path
    db_path = home / "state.db"
    env = os.environ.copy()
    env["MINI_ORK_HOME"] = str(home)
    env["MINI_ORK_DB"] = str(db_path)
    # Subprocess runs db/init.sh to mirror real init. The test does
    # NOT use pytest's monkeypatch because the bash helper we spawn
    # for parity reads env at start.
    subprocess.run(
        ["bash", str(DB_INIT)],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    )
    # Make these visible to the Python port and to bash helpers below.
    os.environ["MINI_ORK_HOME"] = str(home)
    os.environ["MINI_ORK_DB"] = str(db_path)
    os.environ["MINI_ORK_ROOT"] = str(REPO_ROOT)
    return db_path


# ─────────────────────────────────────────────────────────────────────────────
# Bash-side helpers — call LIVE bash functions via subprocess.
# ─────────────────────────────────────────────────────────────────────────────


def _bash_register(
    db_path: Path,
    gate_type: str,
    condition: str,
    task_class_filter: str = "",
    safety: bool = False,
) -> str:
    """Invoke live bash ``gate_register`` via subprocess. Returns gate_id."""
    src = textwrap.dedent(
        f"""
        source {shlex_quote(str(LIB_GATE_REGISTRY))} 2>/dev/null
        gate_register {shlex_quote(gate_type)} {shlex_quote(condition)} {shlex_quote(task_class_filter)}
        """
    ).strip()
    if safety:
        src += " --safety"
    env = os.environ.copy()
    env["MINI_ORK_DB"] = str(db_path)
    proc = subprocess.run(
        ["bash", "-c", src],
        capture_output=True,
        text=True,
        timeout=30,
        env=env,
        check=True,
    )
    return proc.stdout.strip()


def _bash_register_rc(db_path: Path, *args: str) -> int:
    """Invoke live bash ``gate_register`` with arbitrary args; return exit code.

    Unlike ``_bash_register`` this does NOT ``check=True`` — it drives the
    error-path parity cases where a non-zero exit IS the asserted contract
    (unknown gate_type, or missing required positional args).
    """
    quoted = " ".join(shlex_quote(a) for a in args)
    src = textwrap.dedent(
        f"""
        source {shlex_quote(str(LIB_GATE_REGISTRY))} 2>/dev/null
        gate_register {quoted}
        """
    ).strip()
    env = os.environ.copy()
    env["MINI_ORK_DB"] = str(db_path)
    proc = subprocess.run(
        ["bash", "-c", src],
        capture_output=True,
        text=True,
        timeout=30,
        env=env,
    )
    return proc.returncode


def _bash_evaluate(db_path: Path, gate_id: str, context_json: str) -> str:
    src = textwrap.dedent(
        f"""
        source {shlex_quote(str(LIB_GATE_REGISTRY))} 2>/dev/null
        gate_evaluate {shlex_quote(gate_id)} {shlex_quote(context_json)}
        """
    ).strip()
    env = os.environ.copy()
    env["MINI_ORK_DB"] = str(db_path)
    # NOTE: gate_evaluate prints 'fail' on stdout and exits rc=1 when
    # the gate_id is missing. We capture stdout without check=True so
    # the verdict string is recoverable.
    proc = subprocess.run(
        ["bash", "-c", src],
        capture_output=True,
        text=True,
        timeout=30,
        env=env,
    )
    return proc.stdout.strip()


def _bash_list(db_path: Path, task_class: str | None = None) -> list[dict]:
    flag = f"--task-class {shlex_quote(task_class)}" if task_class else ""
    src = textwrap.dedent(
        f"""
        source {shlex_quote(str(LIB_GATE_REGISTRY))} 2>/dev/null
        gate_list {flag}
        """
    ).strip()
    env = os.environ.copy()
    env["MINI_ORK_DB"] = str(db_path)
    proc = subprocess.run(
        ["bash", "-c", src],
        capture_output=True,
        text=True,
        timeout=30,
        env=env,
        check=True,
    )
    return json.loads(proc.stdout.strip())


def _bash_run_all(db_path: Path, task_class: str, context_json: str) -> dict:
    src = textwrap.dedent(
        f"""
        source {shlex_quote(str(LIB_GATE_REGISTRY))} 2>/dev/null
        gate_run_all {shlex_quote(task_class)} {shlex_quote(context_json)}
        """
    ).strip()
    env = os.environ.copy()
    env["MINI_ORK_DB"] = str(db_path)
    proc = subprocess.run(
        ["bash", "-c", src],
        capture_output=True,
        text=True,
        timeout=60,
        env=env,
        check=True,
    )
    return json.loads(proc.stdout.strip())


def shlex_quote(s: str) -> str:
    import shlex
    return shlex.quote(s)


# ─────────────────────────────────────────────────────────────────────────────
# (1) register id format + row-diff
# ─────────────────────────────────────────────────────────────────────────────


def test_register_id_format_and_row_diff(db):
    """Python gate_register returns a gate_id matching the bash format
    AND the row content (gate_type, condition, task_class_filter,
    safety, active) is byte-equal to what bash's gate_register wrote
    for the same input.

    Two DBs are seeded — one via Python port, one via bash — so we
    can compare row tuples across the implementations.
    """
    py_db = db
    bash_db = db.parent / "bash.db"
    # Mirror the same schema in the bash DB by re-running init.sh.
    shutil.copy(str(db), str(bash_db))

    py_gid = gr.gate_register(str(py_db), "budget_gate", "10.0", "write-code", safety=True)
    bash_gid = _bash_register(bash_db, "budget_gate", "10.0", "write-code", safety=True)

    # Both ids match the gate-<6>-<8hex> shape.
    assert _GATE_ID_RE.match(py_gid), f"py gid shape: {py_gid!r}"
    assert _GATE_ID_RE.match(bash_gid), f"bash gid shape: {bash_gid!r}"

    # Row content diff (excluding gate_id which is uuid-random).
    cols = "gate_type, condition, task_class_filter, safety, active"
    def _rows(p: Path) -> list[tuple]:
        con = sqlite3.connect(str(p))
        try:
            return con.execute(
                f"SELECT {cols} FROM gate_registry ORDER BY gate_type, condition"
            ).fetchall()
        finally:
            con.close()

    assert _rows(py_db) == _rows(bash_db), (
        f"row content diverged:\n  py={_rows(py_db)}\n  bash={_rows(bash_db)}"
    )


# ─────────────────────────────────────────────────────────────────────────────
# (2) budget_gate pass
# ─────────────────────────────────────────────────────────────────────────────


def test_budget_gate_pass(db):
    gid = gr.gate_register(str(db), "budget_gate", "10.0")
    py_v = gr.gate_evaluate(str(db), gid, '{"cost_usd": 5.0}')
    bash_v = _bash_evaluate(db, gid, '{"cost_usd": 5.0}')
    assert py_v == "pass", f"py expected pass got {py_v!r}"
    assert bash_v == "pass", f"bash expected pass got {bash_v!r}"
    assert py_v == bash_v


# ─────────────────────────────────────────────────────────────────────────────
# (3) budget_gate boundary — 1e-6 tolerance case
# ─────────────────────────────────────────────────────────────────────────────


def test_budget_gate_boundary(db):
    gid = gr.gate_register(str(db), "budget_gate", "10.0")
    # Exactly at limit: <= comparison yields pass on both sides.
    at_limit = gr.gate_evaluate(str(db), gid, '{"cost_usd": 10.0}')
    assert at_limit == "pass", f"py at-limit: {at_limit!r}"
    assert _bash_evaluate(db, gid, '{"cost_usd": 10.0}') == "pass"
    # Just over limit by 1e-6: must be fail on both sides.
    over_limit = gr.gate_evaluate(str(db), gid, '{"cost_usd": 10.0000001}')
    assert over_limit == "fail", f"py over-limit: {over_limit!r}"
    assert _bash_evaluate(db, gid, '{"cost_usd": 10.0000001}') == "fail"


# ─────────────────────────────────────────────────────────────────────────────
# (4) human_gate always defers
# ─────────────────────────────────────────────────────────────────────────────


def test_human_gate_always_defer(db):
    gid = gr.gate_register(str(db), "human_gate", "anything")
    for ctx in ('{}', '{"task_class":"x"}', '{"cost_usd":1.0}'):
        py_v = gr.gate_evaluate(str(db), gid, ctx)
        bash_v = _bash_evaluate(db, gid, ctx)
        assert py_v == "defer", f"py defer failed for ctx={ctx}: {py_v!r}"
        assert bash_v == "defer", f"bash defer failed for ctx={ctx}: {bash_v!r}"


# ─────────────────────────────────────────────────────────────────────────────
# (5) scope_gate pass and fail
# ─────────────────────────────────────────────────────────────────────────────


def test_scope_gate_pass_and_fail(db):
    gid = gr.gate_register(str(db), "scope_gate", '["a","b"]')
    pass_v = gr.gate_evaluate(str(db), gid, '{"task_class":"a"}')
    fail_v = gr.gate_evaluate(str(db), gid, '{"task_class":"z"}')
    assert pass_v == "pass"
    assert fail_v == "fail"
    assert _bash_evaluate(db, gid, '{"task_class":"a"}') == "pass"
    assert _bash_evaluate(db, gid, '{"task_class":"z"}') == "fail"


# ─────────────────────────────────────────────────────────────────────────────
# (6) unknown gate_id returns 'fail' on both sides
# ─────────────────────────────────────────────────────────────────────────────


def test_evaluate_unknown_gate_returns_fail(db):
    py_v = gr.gate_evaluate(str(db), "gate-zzzz-00000000", '{}')
    bash_v = _bash_evaluate(db, "gate-zzzz-00000000", '{}')
    assert py_v == "fail", f"py unknown: {py_v!r}"
    assert bash_v == "fail", f"bash unknown: {bash_v!r}"


# ─────────────────────────────────────────────────────────────────────────────
# (7) gate_list parity — sorted gate_type tuples match across sides
# ─────────────────────────────────────────────────────────────────────────────


def test_gate_list_count_and_types(db):
    bash_db = db.parent / "bash.db"
    shutil.copy(str(db), str(bash_db))

    gr.gate_register(str(db), "budget_gate", "10.0")
    gr.gate_register(str(db), "human_gate", "x")
    gr.gate_register(str(db), "scope_gate", '["t"]')

    _bash_register(bash_db, "budget_gate", "10.0")
    _bash_register(bash_db, "human_gate", "x")
    _bash_register(bash_db, "scope_gate", '["t"]')

    py_list = gr.gate_list(str(db))
    bash_list = _bash_list(bash_db)

    assert len(py_list) == 3
    assert len(bash_list) == 3
    py_types = sorted(g["gate_type"] for g in py_list)
    bash_types = sorted(g["gate_type"] for g in bash_list)
    assert py_types == bash_types, f"{py_types=}, {bash_types=}"


# ─────────────────────────────────────────────────────────────────────────────
# (8) gate_run_all summary parity
# ─────────────────────────────────────────────────────────────────────────────


def test_run_all_summary(db):
    bash_db = db.parent / "bash.db"
    shutil.copy(str(db), str(bash_db))

    gr.gate_register(str(db), "budget_gate", "20.0", task_class_filter="tc")
    gr.gate_register(str(db), "scope_gate", '["tc"]', task_class_filter="tc")
    _bash_register(bash_db, "budget_gate", "20.0", "tc")
    _bash_register(bash_db, "scope_gate", '["tc"]', "tc")

    ctx = '{"task_class":"tc","cost_usd":5.0}'
    py_summary = gr.gate_run_all(str(db), "tc", ctx)
    bash_summary = _bash_run_all(bash_db, "tc", ctx)

    # Top-level aggregate fields must match.
    assert py_summary["all_pass"] is True
    assert bash_summary["all_pass"] is True
    assert py_summary["any_defer"] is False
    assert bash_summary["any_defer"] is False
    assert py_summary["safety_violation"] is False
    assert bash_summary["safety_violation"] is False
    assert py_summary["gate_count"] == 2
    assert bash_summary["gate_count"] == 2
    # Per-gate verdict sets must match.
    py_v = sorted(g["verdict"] for g in py_summary["gates"])
    bash_v = sorted(g["verdict"] for g in bash_summary["gates"])
    assert py_v == bash_v == ["pass", "pass"]


# ─────────────────────────────────────────────────────────────────────────────
# (9) invalid gate_type rejected on both sides
# ─────────────────────────────────────────────────────────────────────────────


def test_register_invalid_gate_type_rejected(db):
    """An unknown gate_type is rejected by BOTH implementations: bash's
    validate loop returns non-zero and the Python port raises ValueError
    (gate_registry.py:128, "mirrors bash"). Subsumes the retired
    gate_registry bash fixture's 'invalid gate_type exits non-zero' case
    into this live-bash parity gate.
    """
    with pytest.raises(ValueError):
        gr.gate_register(str(db), "invalid_gate_type", "condition")
    assert _bash_register_rc(db, "invalid_gate_type", "condition", "") != 0


# ─────────────────────────────────────────────────────────────────────────────
# (10) missing required args rejected on both sides
# ─────────────────────────────────────────────────────────────────────────────


def test_register_missing_args_rejected(db):
    """Calling gate_register with no gate_type/condition is rejected by BOTH
    sides: bash's ${1:?}/${2:?} required-parameter guards exit non-zero, and
    the Python port raises TypeError for the missing required positional
    arguments. Subsumes the retired gate_registry bash fixture's 'no args
    exits non-zero' case.
    """
    with pytest.raises(TypeError):
        gr.gate_register(str(db))  # type: ignore[call-arg]
    assert _bash_register_rc(db) != 0