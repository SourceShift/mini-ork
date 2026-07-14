"""The conductor must be falsifiable.

THE BUG THIS PINS. `learning_update_conductor_outcomes` reconciled a conductor decision only
against its EPIC's terminal status. But `mini-ork run` completes a TASK_RUN and does not
necessarily advance an epic — on the live db, all 10 decisions pointed at an epic still marked
`not started`, so the join matched nothing and realized_score was NULL on 10 of 10 rows.

The conductor predicted a score every single time and never once learned whether it was right.
A prediction nobody scores is not a prediction, it is a claim.

The last test here is the one that would have caught it: it builds exactly the live situation
(a finished run, an epic that never moved) and asserts the outcome is written anyway.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from mini_ork.ported.mini_ork_execute import learning_update_conductor_outcomes

MIGRATION = Path(__file__).parent.parent / "db" / "migrations" / "0050_conductor_calibration.sql"


def _db(tmp_path: Path) -> str:
    """A minimal schema: just enough for the reconciler, with migration 0050 applied."""
    p = tmp_path / "state.db"
    con = sqlite3.connect(p)
    con.executescript(
        """
        CREATE TABLE epics (id TEXT PRIMARY KEY, status TEXT);
        -- The REAL CHECK constraint, copied from the live schema. It is here so a test
        -- CANNOT invent a status that production would reject. The first version of this fix
        -- checked for status='done' — a value the schema forbids — and its tests passed
        -- because they used the same invented value. A fixture that is laxer than production
        -- does not test production.
        CREATE TABLE task_runs (
            id TEXT PRIMARY KEY,
            status TEXT CHECK (status IN ('classified','planned','executing','verifying',
                                          'reviewing','published','rolled_back','failed')),
            verdict TEXT,
            ended_at INTEGER
        );
        CREATE TABLE conductor_decisions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            decided_at INTEGER, epic_id TEXT, task_class TEXT,
            chosen_topology TEXT, chosen_recipe TEXT, chosen_lane_hints TEXT,
            predicted_score REAL, budget_pct_used REAL, rationale TEXT,
            outcome TEXT, realized_score REAL
        );
        """
    )
    con.commit()
    con.executescript(MIGRATION.read_text(encoding="utf-8"))
    con.commit()
    con.close()
    return str(p)


def _decide(db: str, **kw) -> int:
    con = sqlite3.connect(db)
    cols = ", ".join(kw)
    cur = con.execute(
        f"INSERT INTO conductor_decisions ({cols}, predicted_score, outcome) "  # noqa: S608
        f"VALUES ({', '.join('?' * len(kw))}, 0.6, 'pending')",
        tuple(kw.values()),
    )
    con.commit()
    rid = cur.lastrowid
    con.close()
    assert rid is not None
    return int(rid)


def _row(db: str, rid: int) -> sqlite3.Row:
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    r = con.execute("SELECT * FROM conductor_decisions WHERE id=?", (rid,)).fetchone()
    con.close()
    return r


def test_migration_adds_the_calibration_columns(tmp_path: Path) -> None:
    con = sqlite3.connect(_db(tmp_path))
    cols = {r[1] for r in con.execute("PRAGMA table_info(conductor_decisions)")}
    con.close()
    for c in ("task_run_id", "proposed_topology", "proposed_recipe", "decided_by", "override_reason"):
        assert c in cols, f"migration 0050 did not add {c}"


def test_a_passing_run_scores_1(tmp_path: Path) -> None:
    db = _db(tmp_path)
    con = sqlite3.connect(db)
    con.execute("INSERT INTO task_runs VALUES ('r1', 'published', NULL, 1700000000)")
    con.commit()
    con.close()

    rid = _decide(db, task_run_id="r1", task_class="code_fix")
    assert learning_update_conductor_outcomes(db) == 1

    r = _row(db, rid)
    assert r["outcome"] == "success"
    assert r["realized_score"] == 1.0


def test_a_failed_run_scores_0(tmp_path: Path) -> None:
    db = _db(tmp_path)
    con = sqlite3.connect(db)
    con.execute("INSERT INTO task_runs VALUES ('r2', 'failed', NULL, 1700000000)")
    con.commit()
    con.close()

    rid = _decide(db, task_run_id="r2", task_class="code_fix")
    learning_update_conductor_outcomes(db)

    r = _row(db, rid)
    assert r["outcome"] == "failure"
    assert r["realized_score"] == 0.0


def test_a_CRASH_verdict_overrides_a_published_status(tmp_path: Path) -> None:
    """`published` + verdict=CRASH is a failure. Status alone is not enough.

    On the live db, 28 runs carry verdict='CRASH'. Scoring one 1.0 because its status column
    says published is precisely the false completion this system exists to prevent.
    """
    db = _db(tmp_path)
    con = sqlite3.connect(db)
    con.execute("INSERT INTO task_runs VALUES ('rc', 'published', 'CRASH', 1700000000)")
    con.commit()
    con.close()

    rid = _decide(db, task_run_id="rc", task_class="code_fix")
    learning_update_conductor_outcomes(db)
    assert _row(db, rid)["realized_score"] == 0.0, "a CRASH was scored as a success"


def test_a_reviewing_run_with_ended_at_is_NOT_terminal(tmp_path: Path) -> None:
    """`ended_at IS NOT NULL` does not mean finished — 8 live rows sit in `reviewing` with it set.

    Gating on ended_at (as the first fix did) would score a run that is still being judged.
    """
    db = _db(tmp_path)
    con = sqlite3.connect(db)
    con.execute("INSERT INTO task_runs VALUES ('rv', 'reviewing', NULL, 1700000000)")
    con.commit()
    con.close()

    rid = _decide(db, task_run_id="rv", task_class="code_fix")
    assert learning_update_conductor_outcomes(db) == 0
    assert _row(db, rid)["realized_score"] is None


def test_an_unfinished_run_is_not_scored(tmp_path: Path) -> None:
    """Still running => still pending. Never score a run that hasn't ended."""
    db = _db(tmp_path)
    con = sqlite3.connect(db)
    con.execute("INSERT INTO task_runs VALUES ('r3', 'executing', NULL, NULL)")
    con.commit()
    con.close()

    rid = _decide(db, task_run_id="r3", task_class="code_fix")
    assert learning_update_conductor_outcomes(db) == 0
    assert _row(db, rid)["realized_score"] is None


def test_epic_path_still_works(tmp_path: Path) -> None:
    """The original epic-driven reconciliation must not regress."""
    db = _db(tmp_path)
    con = sqlite3.connect(db)
    con.execute("INSERT INTO epics VALUES ('e1', 'done')")
    con.commit()
    con.close()

    rid = _decide(db, epic_id="e1", task_class="code_fix")
    assert learning_update_conductor_outcomes(db) == 1
    assert _row(db, rid)["realized_score"] == 1.0


def test_THE_LIVE_BUG_run_finishes_but_epic_never_moves(tmp_path: Path) -> None:
    """THE REGRESSION TEST. This is the exact live situation, and the old code scored it NULL.

    A conductor decision attached to an epic that is still `not started`, whose RUN has
    finished and passed. The old reconciler joined only on the epic, matched nothing, and left
    realized_score NULL — forever. Every one of the 10 rows on the live db looked like this.
    """
    db = _db(tmp_path)
    con = sqlite3.connect(db)
    con.execute("INSERT INTO epics VALUES ('libwit-se-1', 'not started')")  # never advances
    con.execute("INSERT INTO task_runs VALUES ('r4', 'published', NULL, 1700000000)")  # but the run finished
    con.commit()
    con.close()

    rid = _decide(db, epic_id="libwit-se-1", task_run_id="r4", task_class="framework_edit")

    assert learning_update_conductor_outcomes(db) == 1, (
        "the reconciler still ignores run-driven work — the conductor remains "
        "uncalibrated by construction"
    )
    r = _row(db, rid)
    assert r["realized_score"] == 1.0
    assert r["outcome"] == "success"


def test_human_override_is_recorded_as_a_labelled_example(tmp_path: Path) -> None:
    """proposed != chosen + decided_by='human' is the training signal the product was throwing away."""
    db = _db(tmp_path)
    con = sqlite3.connect(db)
    con.execute("INSERT INTO task_runs VALUES ('r5', 'published', NULL, 1700000000)")
    con.commit()
    con.close()

    rid = _decide(
        db,
        task_run_id="r5",
        task_class="framework_edit",
        proposed_recipe="framework-edit",
        proposed_lane_hints="implementer=opus_lens",
        chosen_recipe="framework-edit",
        chosen_lane_hints="implementer=minimax_lens",   # the human overrode the lane
        decided_by="human",
        override_reason="opus wins 8/31 here; minimax wins 19/28",
    )
    learning_update_conductor_outcomes(db)

    r = _row(db, rid)
    assert r["decided_by"] == "human"
    assert r["proposed_lane_hints"] != r["chosen_lane_hints"], "no override recorded"
    assert r["realized_score"] == 1.0, "the human's override was not scored"
    # proposed X, human chose Y, Y scored 1.0 → a labelled example the conductor can learn from.


def test_defaults_are_honest_for_historical_rows(tmp_path: Path) -> None:
    """The 10 pre-existing rows were never shown to a human, so 'conductor' is the truth."""
    db = _db(tmp_path)
    rid = _decide(db, task_class="code_fix")
    assert _row(db, rid)["decided_by"] == "conductor"
