"""Hermetic unit tests for the I4b collapse_history writer.

The breaker's collapse signal (mini_ork/recovery/circuit_breaker.py) reads
``collapse_history`` directly; without a writer that table is empty and the
halt can never fire. These tests pin the four contracts from kickoff
auto/rsi-i4b-collapse-writer.md:

  (1) an anchor probe is excluded from the gate's solved-set comparison —
      a candidate that only gains on the anchor probe is NOT credited (the
      gate keeps using the same strict-superset rule as for non-anchor
      probes);
  (2) a scored decision with an anchor probe appends a row with the
      expected ``score``/``anchor``/``step``; a second decision increments
      step (0-based start, MAX(step)+1);
  (3) no anchor probes → no row, regardless of the gate's decision;
  (4) missing ``collapse_history`` table → no error, decision unchanged.

Plus three retained failure-mode cases:
  (5) write failure (OperationalError mid-INSERT) → gate decision unchanged
      and the audit row carries the original verdict;
  (6) the writer is the ONLY writer — the breaker is a pure reader, so the
      write happens in apply.py and never in the detector;
  (7) both probe scorers (prompt arm + code arm) feed the writer, so a
      code-arm candidate also gets a row when anchor probes exist.

Hermetic: every fixture boots an in-process tmp sqlite DB; no lanes, no
networks, no LLM calls. ``probe_scorer.probe_score`` /
``probe_score_code`` are monkeypatched to scripted results, the
``materialize_candidate`` path uses the same FIXTURE_DDL as
test_cli_apply_py.py, and ``_write_collapse_history_row`` is exercised via
``apply_run`` so the wiring is end-to-end (no parallel reproduction of the
gate logic that could drift).
"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
from mini_ork.cli import apply as ap  # noqa: E402

# Same minimal schema as test_cli_apply_py.py, plus the collapse_history table
# (migration 0060) so the writer's INSERT can land on the canonical column
# set. The test's "missing table" case uses a variant WITHOUT this DDL.
FIXTURE_DDL = """
CREATE TABLE pattern_records (
  pattern_id            TEXT PRIMARY KEY,
  description           TEXT NOT NULL,
  evidence_trace_ids    TEXT NOT NULL DEFAULT '[]',
  frequency             INTEGER NOT NULL DEFAULT 1,
  first_seen            TEXT NOT NULL DEFAULT '',
  last_seen             TEXT NOT NULL DEFAULT '',
  output_type           TEXT NOT NULL,
  promoted_to           TEXT,
  status                TEXT NOT NULL DEFAULT 'observed'
);
CREATE TABLE emergent_patterns (
  pattern_id           TEXT PRIMARY KEY,
  cluster_label        TEXT NOT NULL,
  member_item_ids_json TEXT NOT NULL,
  feature_set_json     TEXT NOT NULL,
  strength_score       REAL NOT NULL,
  suggested_meta_adr   TEXT,
  status               TEXT NOT NULL DEFAULT 'proposed',
  detected_at          INTEGER NOT NULL,
  resolved_at          INTEGER
);
CREATE TABLE gradient_records (
    gradient_id      TEXT PRIMARY KEY,
    target           TEXT NOT NULL,
    signal           TEXT NOT NULL,
    suggested_change TEXT NOT NULL,
    evidence         TEXT NOT NULL,
    confidence       REAL NOT NULL DEFAULT 0.0,
    created_at       INTEGER NOT NULL,
    task_class       TEXT
);
CREATE TABLE workflow_memory (
  workflow_version_id       TEXT PRIMARY KEY,
  workflow_name             TEXT NOT NULL,
  base_version_id           TEXT,
  yaml_hash                 TEXT NOT NULL,
  yaml_blob                 TEXT NOT NULL,
  mutations                 TEXT NOT NULL DEFAULT '[]',
  created_at                TEXT NOT NULL DEFAULT '',
  status                    TEXT NOT NULL DEFAULT 'candidate',
  previous_stable_version_id TEXT
);
CREATE TABLE workflow_candidates (
  candidate_id              TEXT PRIMARY KEY,
  base_workflow_version_id  TEXT NOT NULL,
  mutations                 TEXT NOT NULL DEFAULT '[]',
  status                    TEXT NOT NULL DEFAULT 'candidate',
  benchmark_summary_id      TEXT,
  utility_delta             REAL NOT NULL DEFAULT 0.0,
  created_by                TEXT NOT NULL DEFAULT 'evolution_engine',
  created_at                TEXT NOT NULL DEFAULT ''
);
CREATE TABLE promotion_records (
  promotion_id          TEXT PRIMARY KEY,
  candidate_id          TEXT NOT NULL,
  from_version_id       TEXT NOT NULL,
  to_version_id         TEXT NOT NULL,
  utility_before        REAL NOT NULL DEFAULT 0.0,
  utility_after         REAL NOT NULL DEFAULT 0.0,
  benchmark_run_id      TEXT,
  rationale             TEXT NOT NULL DEFAULT '',
  decision              TEXT NOT NULL,
  decided_at            TEXT NOT NULL DEFAULT '',
  decided_by            TEXT NOT NULL
);
CREATE TABLE apply_attempts (
    attempt_id              TEXT PRIMARY KEY,
    task_class              TEXT NOT NULL,
    target_kind             TEXT NOT NULL
                            CHECK (target_kind IN ('workflow_node','workflow_edge','agent_prompt','prompt_file')),
    target_name             TEXT NOT NULL,
    source_kind             TEXT NOT NULL
                            CHECK (source_kind IN ('pattern_records','emergent_patterns','gradient_records','synthesis_gate_verdict','none')),
    source_id               TEXT,
    candidate_id            TEXT REFERENCES workflow_candidates(candidate_id) ON DELETE SET NULL,
    promotion_id            TEXT REFERENCES promotion_records(promotion_id) ON DELETE SET NULL,
    base_workflow_version_id TEXT,
    utility_before          REAL,
    utility_after           REAL,
    utility_delta           REAL,
    decision                TEXT NOT NULL
                            CHECK (decision IN ('promoted','quarantined','rejected','pending_human_approval','dry_run','no_candidate')),
    rationale               TEXT NOT NULL DEFAULT '',
    dry_run                 INTEGER NOT NULL DEFAULT 0 CHECK (dry_run IN (0,1)),
    apply_enabled           INTEGER NOT NULL DEFAULT 0 CHECK (apply_enabled IN (0,1)),
    created_at              TEXT NOT NULL DEFAULT ''
);
CREATE TABLE collapse_history (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    task_class  TEXT    NOT NULL,
    step        INTEGER NOT NULL,
    score       REAL    NOT NULL,
    anchor      REAL    NOT NULL,
    directives  INTEGER NOT NULL DEFAULT 0,
    run_id      TEXT,
    created_at  INTEGER NOT NULL
);
"""

_APPLY_ENV = [
    "MINI_ORK_DB", "MINI_ORK_HOME", "MINI_ORK_ROOT",
    "MO_APPLY_ENABLED", "MO_APPLY_DRY_RUN", "MO_APPLY_SCORER",
    "MO_APPLY_MODE",
    "MO_APPLY_NONREGRESSION_DELTA", "MO_APPLY_MIN_EXAMPLES",
    "MO_APPLY_REGRESSION_TOLERANCE", "MO_APPLY_PERTASK_JSON",
    "MO_APPLY_MOCK_BASELINE", "MO_APPLY_MOCK_DELTA",
    "MO_APPLY_FORCE_REGRESSION",
    "MO_AUTO_APPLY", "MO_AUTO_APPLY_MAX_TARGETS",
    "MO_APPLY_PROBE_MAX_TASKS", "MO_APPLY_PROBE_BUDGET_USD",
    "MO_APPLY_PROBE_TIMEOUT_S",
    "MINI_ORK_RUN_ID",
]


@pytest.fixture()
def envscrub(monkeypatch):
    for var in _APPLY_ENV:
        monkeypatch.delenv(var, raising=False)
    return monkeypatch


@pytest.fixture()
def db(tmp_path, envscrub):
    """A tmp sqlite DB with the full apply + collapse_history schema; MINI_ORK_DB set."""
    path = tmp_path / "state.db"
    con = sqlite3.connect(path)
    con.executescript(FIXTURE_DDL)
    con.commit()
    con.close()
    envscrub.setenv("MINI_ORK_DB", str(path))
    ap._SCHEMA_INIT = False
    yield str(path)
    ap._SCHEMA_INIT = False


def _seed_gradient(db_path, gradient_id="gr-1", target="prompts/reviewer.md",
                   change="improve wording", confidence=0.5, task_class="code_fix",
                   signal="sig"):
    con = sqlite3.connect(db_path)
    con.execute(
        "INSERT INTO gradient_records"
        " (gradient_id, target, signal, suggested_change, evidence, confidence, created_at, task_class)"
        " VALUES (?,?,?,?,?,?,?,?)",
        (gradient_id, target, signal, change, "[]", confidence, 100, task_class),
    )
    con.commit()
    con.close()


def _collapse_rows(db_path):
    """Read collapse_history rows; [] when the table is missing."""
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    try:
        try:
            return [dict(r) for r in con.execute(
                "SELECT * FROM collapse_history ORDER BY id").fetchall()]
        except sqlite3.OperationalError:
            # Test (4) runs on a DB without migration 0060 applied; the writer
            # is required to be a silent no-op in that case. Mirror it here.
            return []
    finally:
        con.close()


def _attempts(db_path):
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in con.execute(
            "SELECT * FROM apply_attempts ORDER BY rowid").fetchall()]
    finally:
        con.close()


def _gate_summary(capsys):
    out = capsys.readouterr().out.strip().splitlines()[-1]
    return json.loads(out)


# ─────────────────────────────────────────────────────────────────────────────
# (1) Anchor probe is excluded from the gate's solved-set comparison.
#
# A candidate that ONLY gains on the anchor probe must NOT be credited by the
# gate — anchor_solved_frac is a signal for the collapse detector, not for the
# promote decision. We construct a measured result where the candidate's
# non-anchor (gate) solved set is a strict subset of the control's, but the
# candidate also "gains" on an anchor probe. The gate must refuse to promote
# (per-task regression), AND a row is appended because anchor_solved_frac is
# not None.
# ──────────────────────────────────────────────────────────────────────────────
def test_anchor_probe_excluded_from_gate_solved_set(db, tmp_path, capsys, monkeypatch):
    from mini_ork.learning import probe_scorer as ps
    _seed_gradient(db, target="prompts/reviewer.md", task_class="code_fix")
    target = tmp_path / "reviewer.md"
    target.write_text("ORIGINAL PROMPT\n")
    monkeypatch.setenv("MO_APPLY_SCORER", "probe")
    monkeypatch.setenv("MO_APPLY_ENABLED", "1")

    # 3 gate probes: p1, p2, p3 — control solves p1, candidate does not
    # (regression on the gate set).
    # 1 anchor probe: a1 — candidate gains over control (anchor signal only).
    # pertask_json covers the gate probes; the anchor probe outcome flows
    # only into anchor_solved_frac, NOT into pertask_json.
    probe_out = {
        "before": 1 / 3,
        "after": 0 / 3,        # 0/3 on the GATE set → strict subset → quarantine
        "n": 3,
        "pertask_json": json.dumps({
            "before": [1, 1, 1],   # control solved p1, p2, p3
            "after":  [0, 0, 0],   # candidate solved none
            "ids":    ["p1.md", "p2.md", "p3.md"],
        }),
        "runs": [],
        "cost_usd": 0.12,
        "control_n": 1,
        "control_solved": [1, 1, 1],
        "anchor_solved_frac": 1.0,    # candidate "won" the anchor probe
        "directives": 1,
    }
    monkeypatch.setattr(ps, "probe_score", lambda *a, **k: dict(probe_out))

    rc = ap.apply_run("code_fix", "prompt_file", "prompts/reviewer.md",
                      str(target), db=db)
    assert rc == 0
    summary = _gate_summary(capsys)
    # Gate sees the gate-set regression → quarantined, NOT promoted.
    assert summary["decision"] == "quarantined"
    assert summary["version_id"] == ""  # file untouched
    assert target.read_text() == "ORIGINAL PROMPT\n"

    # The row landed: anchor_solved_frac=1.0, score=0.0 (gate after), step=0.
    rows = _collapse_rows(db)
    assert len(rows) == 1, f"expected 1 collapse_history row, got {rows}"
    row = rows[0]
    assert row["task_class"] == "code_fix"
    assert row["step"] == 0          # 0-based start
    assert row["score"] == pytest.approx(0.0)
    assert row["anchor"] == pytest.approx(1.0)
    assert row["directives"] == 1
    assert row["run_id"] is None  # no MINI_ORK_RUN_ID set in this test

    # Apply-attempt row records the gate's verdict (quarantined) regardless of
    # the anchor signal. The collapse_history write MUST NOT change it.
    attempts = _attempts(db)
    assert len(attempts) == 1
    assert attempts[0]["decision"] == "quarantined"


# ─────────────────────────────────────────────────────────────────────────────
# (2) A second decision increments step (0-based start, MAX(step)+1).
# ──────────────────────────────────────────────────────────────────────────────
def test_second_decision_increments_step(db, tmp_path, capsys, monkeypatch):
    from mini_ork.learning import probe_scorer as ps
    # Two gradient rows so apply_run can be called twice. Edit memory rejects
    # re-proposals of a quarantined (task_class, target, source) tuple, so
    # the second call must pick a DIFFERENT gradient row: seed gr-B with a
    # higher confidence than gr-A so the picker selects it on the second pass.
    _seed_gradient(db, gradient_id="gr-A", target="prompts/reviewer.md",
                   change="directive A", confidence=0.4, task_class="code_fix")
    target = tmp_path / "reviewer.md"
    target.write_text("ORIGINAL PROMPT\n")
    monkeypatch.setenv("MO_APPLY_SCORER", "probe")
    monkeypatch.setenv("MO_APPLY_ENABLED", "1")

    probe_out = {
        "before": 1.0,
        "after": 1.0,        # flat on the gate set; the strict-superset
                              # branch fires and the gate quarantines the
                              # candidate — so we exercise the write path
                              # without performing a real file mutation
        "n": 2,
        "pertask_json": json.dumps({
            "before": [1, 1], "after": [1, 1], "ids": ["p1.md", "p2.md"],
        }),
        "runs": [], "cost_usd": 0.04,
        "control_n": 1, "control_solved": [1, 1],
        "anchor_solved_frac": 0.7,    # anchor probe candidate solved 70%
        "directives": 1,
    }
    monkeypatch.setattr(ps, "probe_score", lambda *a, **k: dict(probe_out))

    # First decision — picks gr-A (only seed); quarantines.
    rc1 = ap.apply_run("code_fix", "prompt_file", "prompts/reviewer.md",
                       str(target), db=db)
    assert rc1 == 0
    s1 = _gate_summary(capsys)
    assert s1["decision"] == "quarantined"

    # Second decision — seed gr-B with HIGHER confidence so the picker selects
    # it over the (now quarantined) gr-A; bypasses edit memory on a new source.
    _seed_gradient(db, gradient_id="gr-B", target="prompts/reviewer.md",
                   change="directive B", confidence=0.9, task_class="code_fix")
    rc2 = ap.apply_run("code_fix", "prompt_file", "prompts/reviewer.md",
                       str(target), db=db)
    assert rc2 == 0
    s2 = _gate_summary(capsys)
    assert s2["decision"] == "quarantined"

    rows = _collapse_rows(db)
    assert len(rows) == 2
    # Step arithmetic: 0 first, then MAX(0)+1 = 1.
    assert [r["step"] for r in rows] == [0, 1]
    # Both rows carry the same score/anchor (the scripted probe_out is stable)
    # but DIFFERENT candidate_ids because each decision mints a new one.
    assert rows[0]["score"] == pytest.approx(1.0)
    assert rows[1]["score"] == pytest.approx(1.0)
    assert rows[0]["anchor"] == pytest.approx(0.7)
    assert rows[1]["anchor"] == pytest.approx(0.7)
    assert rows[0]["task_class"] == "code_fix"
    assert rows[1]["task_class"] == "code_fix"


# ─────────────────────────────────────────────────────────────────────────────
# (3) No anchor probes → no row, regardless of gate decision.
# ──────────────────────────────────────────────────────────────────────────────
def test_no_anchor_probes_writes_no_row(db, tmp_path, capsys, monkeypatch):
    from mini_ork.learning import probe_scorer as ps
    _seed_gradient(db, target="prompts/reviewer.md", task_class="code_fix")
    target = tmp_path / "reviewer.md"
    target.write_text("ORIGINAL PROMPT\n")
    monkeypatch.setenv("MO_APPLY_SCORER", "probe")
    monkeypatch.setenv("MO_APPLY_ENABLED", "1")

    # anchor_solved_frac is None — kickoff rule #1. The apply loop MUST
    # skip the collapse_history write entirely (kickoff rule #3) even when
    # the gate ran and decided.
    probe_out = {
        "before": 1.0,
        "after": 1.0,
        "n": 2,
        "pertask_json": json.dumps({
            "before": [1, 1], "after": [1, 1], "ids": ["p1.md", "p2.md"],
        }),
        "runs": [], "cost_usd": 0.04,
        "control_n": 1, "control_solved": [1, 1],
        "anchor_solved_frac": None,    # ← no anchor probes
        "directives": 1,
    }
    monkeypatch.setattr(ps, "probe_score", lambda *a, **k: dict(probe_out))

    rc = ap.apply_run("code_fix", "prompt_file", "prompts/reviewer.md",
                      str(target), db=db)
    assert rc == 0
    s = _gate_summary(capsys)
    # Gate still ran and produced its verdict (quarantined — flat on gate).
    assert s["decision"] == "quarantined"

    # But the collapse_history table is empty.
    assert _collapse_rows(db) == []


# ─────────────────────────────────────────────────────────────────────────────
# (4) Missing collapse_history table → no error, decision unchanged.
# ──────────────────────────────────────────────────────────────────────────────
def test_missing_table_no_error_decision_unchanged(tmp_path, envscrub, capsys, monkeypatch):
    """Same apply_run on a DB without the collapse_history DDL applied.

    The breaker's production read path is fail-open on the same condition
    (mini_ork/recovery/circuit_breaker.py:_eval_collapse_signal); the writer
    must mirror that — a DB without migration 0060 is a coherent state on
    older installations.
    """
    from mini_ork.cli import apply as ap2
    from mini_ork.learning import probe_scorer as ps
    # Build a DB WITHOUT the collapse_history DDL.
    ddl_no_collapse = FIXTURE_DDL.replace(
        "CREATE TABLE collapse_history (\n"
        "    id          INTEGER PRIMARY KEY AUTOINCREMENT,\n"
        "    task_class  TEXT    NOT NULL,\n"
        "    step        INTEGER NOT NULL,\n"
        "    score       REAL    NOT NULL,\n"
        "    anchor      REAL    NOT NULL,\n"
        "    directives  INTEGER NOT NULL DEFAULT 0,\n"
        "    run_id      TEXT,\n"
        "    created_at  INTEGER NOT NULL\n"
        ");",
        "",
    )
    db_path = tmp_path / "state.db"
    con = sqlite3.connect(db_path)
    con.executescript(ddl_no_collapse)
    con.commit()
    con.close()
    envscrub.setenv("MINI_ORK_DB", str(db_path))
    ap2._SCHEMA_INIT = False

    _seed_gradient(str(db_path), target="prompts/reviewer.md", task_class="code_fix")
    target = tmp_path / "reviewer.md"
    target.write_text("ORIGINAL PROMPT\n")
    monkeypatch.setenv("MO_APPLY_SCORER", "probe")
    monkeypatch.setenv("MO_APPLY_ENABLED", "1")

    probe_out = {
        "before": 1.0, "after": 1.0, "n": 2,
        "pertask_json": json.dumps({
            "before": [1, 1], "after": [1, 1], "ids": ["p1.md", "p2.md"],
        }),
        "runs": [], "cost_usd": 0.04,
        "control_n": 1, "control_solved": [1, 1],
        "anchor_solved_frac": 0.5,    # anchor exists → writer MUST attempt
        "directives": 1,
    }
    monkeypatch.setattr(ps, "probe_score", lambda *a, **k: dict(probe_out))

    rc = ap2.apply_run("code_fix", "prompt_file", "prompts/reviewer.md",
                       str(target), db=db_path)
    assert rc == 0   # rc=0 means the apply loop completed normally
    s = _gate_summary(capsys)
    assert s["decision"] == "quarantined"  # gate verdict unchanged
    # apply_attempts audit row recorded the verdict.
    attempts = _attempts(str(db_path))
    assert len(attempts) == 1
    assert attempts[0]["decision"] == "quarantined"
    # No row was written because the table does not exist.
    assert _collapse_rows(str(db_path)) == []


# ─────────────────────────────────────────────────────────────────────────────
# (5) Write failure (OperationalError mid-INSERT) → gate decision unchanged.
# ──────────────────────────────────────────────────────────────────────────────
def test_write_failure_does_not_change_gate_decision(
        db, tmp_path, capsys, monkeypatch):
    from mini_ork.learning import probe_scorer as ps
    _seed_gradient(db, target="prompts/reviewer.md", task_class="code_fix")
    target = tmp_path / "reviewer.md"
    target.write_text("ORIGINAL PROMPT\n")
    monkeypatch.setenv("MO_APPLY_SCORER", "probe")
    monkeypatch.setenv("MO_APPLY_ENABLED", "1")

    # A genuine promote-shaped result so the gate decides 'promoted' — then
    # the writer raises mid-INSERT and we verify the file STILL got the
    # promote write (the gate decision is the source of truth, not the
    # collapse_history append). We monkey-patch the helper directly so this
    # test simulates the live "INSERT INTO collapse_history raises" path
    # without reaching into sqlite3.Connection's read-only execute method.
    calls = {"n": 0}

    def boom(*args, **kwargs):
        calls["n"] += 1
        raise sqlite3.OperationalError("disk full")

    monkeypatch.setattr(ap, "_write_collapse_history_row", boom)

    probe_out = {
        "before": 0.0, "after": 1.0, "n": 2,
        "pertask_json": json.dumps({
            "before": [0, 1], "after": [1, 1],
            "ids": ["p1.md", "p2.md"],
        }),
        "runs": [], "cost_usd": 0.04,
        "control_n": 1, "control_solved": [0, 1],
        "anchor_solved_frac": 0.8,
        "directives": 1,
    }
    monkeypatch.setattr(ps, "probe_score", lambda *a, **k: dict(probe_out))

    rc = ap.apply_run("code_fix", "prompt_file", "prompts/reviewer.md",
                      str(target), db=db)
    assert rc == 0  # apply loop exits cleanly — the write failure is swallowed
    summary = _gate_summary(capsys)
    # Gate decision survives the writer crash.
    assert summary["decision"] == "promoted"
    assert summary["version_id"] != ""  # file write still happened
    # Apply-attempt audit row carries the verdict.
    attempts = _attempts(db)
    assert len(attempts) == 1
    assert attempts[0]["decision"] == "promoted"
    # The writer was invoked — we know it raised mid-call (kickoff rule: the
    # helper is called once per scored decision when anchor_solved_frac is
    # not None).
    assert calls["n"] == 1


# ─────────────────────────────────────────────────────────────────────────────
# (5b) Direct unit test of the helper's silent-no-op contract.
# ──────────────────────────────────────────────────────────────────────────────
def test_write_helper_swallows_operationalerror(db, monkeypatch):
    """A direct sqlite3 raise mid-INSERT is swallowed by the helper.

    The end-to-end test above patches the helper itself. This test exercises
    the helper's own exception handling: it points the writer at a connection
    whose ``cursor().execute`` raises on the collapse_history INSERT, and
    verifies the helper returns cleanly without re-raising.
    """
    real_connect = sqlite3.connect

    class _BoomCursor:
        """Wraps a real Cursor and raises on the collapse_history INSERT."""
        def __init__(self, real_cur):
            self._cur = real_cur

        def execute(self, sql, *args, **kwargs):
            if "INSERT INTO collapse_history" in sql:
                raise sqlite3.OperationalError("disk full")
            return self._cur.execute(sql, *args, **kwargs)

        def fetchone(self):
            return self._cur.fetchone()

        def close(self):
            self._cur.close()

    class _BoomConnection:
        """Wraps a real Connection: shortcuts execute() to the cursor path so
        the helper's ``con.execute(...)`` calls also raise on the INSERT."""
        def __init__(self, real_con):
            self._con = real_con

        def cursor(self):
            return _BoomCursor(self._con.cursor())

        def execute(self, sql, *args, **kwargs):
            if "INSERT INTO collapse_history" in sql:
                raise sqlite3.OperationalError("disk full")
            return self._con.execute(sql, *args, **kwargs)

        def commit(self):
            self._con.commit()

        def close(self):
            self._con.close()

    def boom_connect(path, *a, **kw):
        return _BoomConnection(real_connect(path, *a, **kw))

    monkeypatch.setattr(ap.sqlite3, "connect", boom_connect)

    # Direct call: the helper MUST swallow the OperationalError. We do not
    # care that no row is written — only that the helper does not raise.
    ap._write_collapse_history_row(
        task_class="code_fix",
        score=0.5,
        anchor=0.5,
        directives=1,
        run_id="run-x",
        db=db,
    )


# ─────────────────────────────────────────────────────────────────────────────
# (6) The writer is invoked from the apply loop, never from the breaker.
# ──────────────────────────────────────────────────────────────────────────────
def test_writer_only_invoked_via_apply_run(db):
    """Sanity: ``_write_collapse_history_row`` is called from ``apply_run``
    only — never from the breaker (which is a pure reader). Imports +
    call-site checks are enough here; we do not assert ``not imported``
    because the symbol is part of the public-ish apply.py surface for
    tests. The structural guarantee comes from the breaker module: it does
    not import apply."""
    import inspect
    from mini_ork.recovery import circuit_breaker as cb
    cb_src = inspect.getsource(cb)
    assert "_write_collapse_history_row" not in cb_src, (
        "circuit_breaker must NEVER write collapse_history — it is a pure "
        "reader of the table; the apply loop is the sole writer."
    )
    # And the writer is referenced from apply.py (positive control).
    ap_src = inspect.getsource(ap)
    assert "_write_collapse_history_row" in ap_src, (
        "apply.py must call _write_collapse_history_row — the kickoff rule "
        "places the writer in the apply loop."
    )


# ─────────────────────────────────────────────────────────────────────────────
# (7) Code-arm (probe_score_code) candidates also feed collapse_history.
# ──────────────────────────────────────────────────────────────────────────────
def test_code_arm_candidate_writes_collapse_history_row(db, tmp_path, capsys, monkeypatch):
    """MO_APPLY_SCORER=code uses probe_score_code; both scorers must write.

    The detector tracks score vs anchor across ALL mutations, not only
    prompt ones — a code change that moves the candidate's anchor score
    is just as visible to the collapse signal. Code-arm candidates carry
    directives=0 (a patch is not a directive).
    """
    from mini_ork.learning import probe_scorer as ps
    _seed_gradient(db, target="prompts/reviewer.md", task_class="code_fix")
    target = tmp_path / "reviewer.md"
    target.write_text("ORIGINAL PROMPT\n")
    monkeypatch.setenv("MO_APPLY_SCORER", "code")
    monkeypatch.setenv("MO_APPLY_ENABLED", "1")
    monkeypatch.setenv("MO_APPLY_CODE_PATCH", "/tmp/fake.patch")
    # Stub probe_score so we never enter the prompt arm; probe_score_code
    # is the path under test.
    monkeypatch.setattr(ps, "probe_score", lambda *a, **k: pytest.fail(
        "probe scorer must not be invoked when MO_APPLY_SCORER=code"))
    code_result = {
        "before": 1.0, "after": 1.0, "n": 2,
        "pertask_json": json.dumps({
            "before": [1, 1], "after": [1, 1], "ids": ["p1.md", "p2.md"],
        }),
        "runs": [], "cost_usd": 0.04,
        "control_n": 1, "control_solved": [1, 1],
        "anchor_solved_frac": 0.4,
        "directives": 0,    # code arm — no directive, just a patch
    }
    monkeypatch.setattr(ps, "probe_score_code", lambda *a, **k: dict(code_result))

    rc = ap.apply_run("code_fix", "prompt_file", "prompts/reviewer.md",
                      str(target), db=db)
    assert rc == 0
    s = _gate_summary(capsys)
    # Gate ran and produced a verdict (flat on the gate set → quarantined).
    assert s["decision"] == "quarantined"

    rows = _collapse_rows(db)
    assert len(rows) == 1
    row = rows[0]
    assert row["task_class"] == "code_fix"
    assert row["step"] == 0
    assert row["score"] == pytest.approx(1.0)
    assert row["anchor"] == pytest.approx(0.4)
    assert row["directives"] == 0   # code arm — directives count = 0

def test_probe_cap_never_drops_anchor_probes(monkeypatch, tmp_path):
    """MO_APPLY_PROBE_MAX_TASKS caps gate probes only: an anchor that sorts
    after the capped gate probes must still be loaded (2026-09-29 live smoke:
    probe-3 anchor was dropped under the default cap of 2)."""
    from mini_ork.learning import probe_scorer as ps

    probes = tmp_path / "probes"
    probes.mkdir()
    (probes / "probe-1.md").write_text("# p1\n")
    (probes / "probe-2.md").write_text("# p2\n")
    (probes / "probe-3.md").write_text("---\nanchor: true\n---\n# p3\n")
    monkeypatch.setattr(ps, "_recipe_dir", lambda task_class: str(tmp_path))
    monkeypatch.setenv("MO_APPLY_PROBE_MAX_TASKS", "2")
    got = [(p.rsplit("/", 1)[-1], a) for p, a in ps._load_probes("code_fix")]
    assert got == [("probe-1.md", False), ("probe-2.md", False), ("probe-3.md", True)]
    monkeypatch.setenv("MO_APPLY_PROBE_MAX_TASKS", "1")
    got = [(p.rsplit("/", 1)[-1], a) for p, a in ps._load_probes("code_fix")]
    assert got == [("probe-1.md", False), ("probe-3.md", True)]
