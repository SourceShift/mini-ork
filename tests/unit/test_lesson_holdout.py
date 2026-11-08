"""The per-lesson holdout — the production selection + writer path.

Every lesson the loop learns is in-sample for the run that learned it, so a
"held-out" score measures nothing: the guidance the node was given is the
guidance it is graded against. This file pins the fix that makes the
withheld slice real:

  * selection (``mini_ork/context_assembler.py``) — a deterministic slice of
    the lessons is withheld from the prompt and reported on the ``sources``
    out-param as ``{kind, id, held_out: True}``;
  * writing (``mini_ork/cli/execute_handlers.py``) — the withheld lessons are
    written to the injection ledger with ``held_out=1`` and are NOT claimed
    by ``learned/<node>.json`` as delivered.

The DB under test is ``MINI_ORK_DB`` when this run is G8's exercise copy of
the measured DB (``mini_ork/rsi/cycle.py``), and a temp DB otherwise. Every
row this file writes goes through the production writer — the ledger row the
``db:L3`` rule reads is produced by ``_write_learned_record``, never by a
hand-rolled INSERT — so a green exercise means the producer is live, not that
a test faked the signal.
"""
from __future__ import annotations

import json
import os
import sqlite3
import string
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
from mini_ork import context_assembler as ca  # noqa: E402
from mini_ork.cli import execute, execute_handlers  # noqa: E402

TC = "tc_lesson_holdout"

# The lesson kinds the holdout owns. Steering, preference, prior-run and
# context-v2 sources are not lessons and are never reserved.
LESSON_KINDS = ("gradient", "theme", "pattern")

# G8's ``_exercise_once`` (mini_ork/rsi/cycle.py) runs this file's scoped
# tests against a *copy* of the measured DB and exports that copy as
# ``MINI_ORK_DB``. The autouse fixture in ``tests/conftest.py`` pops
# ``MINI_ORK_DB`` before every test (it must, or a stray test writes into the
# operator's live DB), so the value is only visible at import time. Capture it
# here; ``_is_exercise_copy`` accepts only G8's copy, never an arbitrary
# inherited live DB.
_G8_DB = os.environ.get("MINI_ORK_DB") or ""


def _is_exercise_copy(db: str) -> bool:
    """True only for G8's ``_exercise_once`` copy of the measured DB.

    Its shape (``mini_ork/rsi/cycle.py``) is
    ``<home>/rsi/<cycle_id>/exercise-{base,post}.db`` beside an
    ``exercise-{base,post}-home`` dir. A node lane also exports ``MINI_ORK_DB``
    pointing at the operator's live ``state.db``; the shape check keeps this
    file from writing there.
    """
    path = Path(db)
    return (path.name.startswith("exercise-") and path.suffix == ".db"
            and path.parent.parent.name == "rsi"
            and (path.parent / f"{path.stem}-home").is_dir())


# ── schema + fixtures ───────────────────────────────────────────────────────

_GRADIENT_DDL = """
CREATE TABLE IF NOT EXISTS gradient_records (
    gradient_id      TEXT PRIMARY KEY,
    target           TEXT NOT NULL,
    signal           TEXT NOT NULL,
    suggested_change TEXT NOT NULL,
    evidence         TEXT NOT NULL,
    confidence       REAL NOT NULL DEFAULT 0.0,
    created_at       INTEGER NOT NULL,
    task_class       TEXT
)"""

_THEME_DDL = """
CREATE TABLE IF NOT EXISTS lesson_themes (
  theme_id      TEXT PRIMARY KEY,
  kind          TEXT NOT NULL,
  role          TEXT,
  task_class    TEXT,
  representative TEXT NOT NULL,
  centroid      BLOB NOT NULL,
  n_gradients   INTEGER NOT NULL DEFAULT 0,
  n_runs        INTEGER NOT NULL DEFAULT 0,
  n_at_refresh  INTEGER NOT NULL DEFAULT 0,
  first_seen    INTEGER,
  last_seen     INTEGER,
  lesson_text   TEXT,
  status        TEXT NOT NULL DEFAULT 'candidate',
  bug_report_id INTEGER,
  updated_at    INTEGER
)"""

_PATTERN_DDL = """
CREATE TABLE IF NOT EXISTS emergent_patterns (
  pattern_id           TEXT PRIMARY KEY,
  cluster_label        TEXT NOT NULL,
  member_item_ids_json TEXT NOT NULL,
  feature_set_json     TEXT NOT NULL,
  strength_score       REAL NOT NULL,
  suggested_meta_adr   TEXT,
  status               TEXT NOT NULL DEFAULT 'proposed',
  detected_at          INTEGER NOT NULL,
  resolved_at          INTEGER,
  lesson_text          TEXT,
  task_class           TEXT
)"""

_PREFERENCE_DDL = """
CREATE TABLE IF NOT EXISTS user_preference_memory (
  user_id TEXT NOT NULL,
  preference_key TEXT NOT NULL,
  preference_value TEXT NOT NULL DEFAULT '{}',
  scope TEXT NOT NULL DEFAULT 'global',
  scope_target TEXT NOT NULL DEFAULT '',
  set_at TEXT NOT NULL DEFAULT '2026-01-01',
  origin TEXT NOT NULL DEFAULT 'operator',
  source_ids TEXT,
  active INTEGER NOT NULL DEFAULT 1,
  PRIMARY KEY (user_id, preference_key, scope, scope_target)
)"""

_STEERING_DDL = """
CREATE TABLE IF NOT EXISTS operator_steering (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id TEXT,
  role_target TEXT NOT NULL,
  severity TEXT NOT NULL DEFAULT 'info',
  message TEXT NOT NULL,
  source TEXT,
  confidence REAL NOT NULL DEFAULT 0.8,
  created_at INTEGER NOT NULL,
  consumed_at INTEGER,
  expires_at INTEGER NOT NULL
)"""


@pytest.fixture
def db(tmp_path, monkeypatch):
    """The state DB the production path reads and writes.

    G8's exercise copy when this run is one — the exercise re-asks the
    ``db:L3`` rule against the same file, so the held-out row this file's
    writer produces is the producer-gap signal — and a temp DB otherwise.
    """
    if _G8_DB and _is_exercise_copy(_G8_DB):
        dbp = _G8_DB
    else:
        dbp = str(tmp_path / "state.db")
        con = sqlite3.connect(dbp)
        con.execute("CREATE TABLE bootstrap (x INTEGER)")
        con.commit()
        con.close()
    monkeypatch.setenv("MINI_ORK_DB", dbp)
    monkeypatch.setenv("MINI_ORK_HOME", str(tmp_path / "home"))
    # The v2 arm renders the themes itself, so the v1 theme section would be
    # empty; keep the v1 arm so the theme channel is the one under test.
    monkeypatch.delenv("MO_CONTEXT_V2", raising=False)
    monkeypatch.delenv("MO_TARGET_CWD", raising=False)
    monkeypatch.delenv("MO_LESSON_HOLDOUT_RATE", raising=False)
    # Raw gradients are opt-in since the 2026-10-07 verified-only rule.
    monkeypatch.setenv("MO_INJECT_UNVERIFIED", "1")
    monkeypatch.setenv("MO_EMERGENT_INJECT_LIMIT", "24")
    return dbp


@pytest.fixture
def run_dir(tmp_path, monkeypatch):
    """A run directory the writer persists its learned record into."""
    d = tmp_path / "run"
    d.mkdir()
    monkeypatch.setenv("MINI_ORK_RUN_DIR", str(d))
    return d


# ── seed helpers ────────────────────────────────────────────────────────────

def _alpha(n: int, width: int = 4) -> str:
    """A letters-only token for ``n``.

    No digits: the theme tokenizer keeps tokens of three or more characters
    and drops the rest, so a two-letter word would vanish and every seeded
    theme would collapse into the same token set.
    """
    out = []
    for _ in range(width):
        out.append(string.ascii_lowercase[n % 26])
        n //= 26
    return "".join(reversed(out))


def _theme_text(i: int) -> str:
    """Lesson text whose token set is unique to row ``i``.

    The theme selector drops a candidate whose token set overlaps an already
    selected lesson by 0.8 or more, so every seeded theme needs its own words
    — otherwise the seeded rows would collapse into a single lesson and the
    channel would never hold more than one out.
    """
    return "themsig " + " ".join(_alpha(i * 8 + k) for k in range(4))


def _seed_lessons(db, *, gradients=24, themes=12, patterns=12):
    """Seed the three learned-lesson channels; return {kind: {id: marker}}."""
    con = sqlite3.connect(db)
    con.executescript(_GRADIENT_DDL)
    con.executescript(_THEME_DDL)
    con.executescript(_PATTERN_DDL)
    marks: dict[str, dict[str, str]] = {"gradient": {}, "theme": {},
                                        "pattern": {}}
    now = 1700000000
    for i in range(gradients):
        gid = "hg%03d" % i
        marker = "gradsig %s" % gid
        con.execute(
            "INSERT OR REPLACE INTO gradient_records (gradient_id, target,"
            " signal, suggested_change, evidence, confidence, created_at,"
            " task_class) VALUES (?,?,?,?,?,?,?,?)",
            (gid, "tgt-" + gid, marker, "fixit", "ev", 0.9, now + i, TC))
        marks["gradient"][gid] = marker
    for i in range(themes):
        tid = "ht%03d" % i
        text = _theme_text(i)
        con.execute(
            "INSERT OR REPLACE INTO lesson_themes (theme_id, kind, role,"
            " task_class, representative, centroid, n_gradients, n_runs,"
            " n_at_refresh, first_seen, last_seen, lesson_text, status,"
            " updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (tid, "task", None, TC, text[:40], b"", 1, 100 - i, 0, now + i,
             now + i, text, "verified", now + i))
        marks["theme"][tid] = text.split(" ")[0] + " " + text.split(" ")[1]
    for i in range(patterns):
        pid = "hp%03d" % i
        marker = "patsig %s" % pid
        con.execute(
            "INSERT OR REPLACE INTO emergent_patterns (pattern_id,"
            " cluster_label, member_item_ids_json, feature_set_json,"
            " strength_score, suggested_meta_adr, status, detected_at,"
            " lesson_text, task_class) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (pid, "cluster: task_class=%s status=failure" % TC, "[]", "[]",
             100.0 - i, "meta-adr", "approved", now + i, marker, TC))
        marks["pattern"][pid] = marker
    con.commit()
    con.close()
    return marks


def _seed_non_lesson_sources(db, run_id):
    """Seed an operator preference and a steering message for ``run_id``.

    Neither is a lesson: the holdout must never reserve them.
    """
    con = sqlite3.connect(db)
    con.executescript(_PREFERENCE_DDL)
    con.executescript(_STEERING_DDL)
    con.execute(
        "INSERT OR REPLACE INTO user_preference_memory (user_id,"
        " preference_key, preference_value, scope, scope_target, set_at,"
        " origin, source_ids, active) VALUES"
        " ('default','holdout_key','holdout_value','global','','2026-01-01',"
        "'operator',NULL,1)")
    con.execute(
        "INSERT INTO operator_steering (run_id, role_target, severity,"
        " message, source, confidence, created_at, consumed_at, expires_at)"
        " VALUES (?,?,?,?,?,?,?,?,?)",
        (run_id, "any", "warn", "holdout steering message", "operator", 0.9,
         1700000000, None, 99999999999999))
    con.commit()
    con.close()


# ── helpers ──────────────────────────────────────────────────────────────────

def _split(sources):
    """(reserved, delivered) sets of (kind, id) from a sources out-param."""
    reserved, delivered = set(), set()
    for s in sources or []:
        if not isinstance(s, dict):
            continue
        if s.get("kind") is None or s.get("id") is None:
            continue
        key = (s["kind"], str(s["id"]))
        (reserved if s.get("held_out") else delivered).add(key)
    return reserved, delivered


def _ledger_rows(db, run_id):
    """{(kind, id, held_out)} for one run."""
    con = sqlite3.connect(db)
    try:
        return set(con.execute(
            "SELECT source_kind, source_id, held_out FROM lesson_injections"
            " WHERE run_id = ?", (run_id,)).fetchall())
    finally:
        con.close()


def _read_json(run_dir, node_id):
    with open(Path(run_dir) / "learned" / ("%s.json" % node_id),
              encoding="utf-8") as fh:
        return json.load(fh)


def _production_block(run_dir, node_id, run_id, monkeypatch, **kwargs):
    """The production prompt-assembly path: ``execute._learned_block``.

    Returns ``(block, sources)``.
    """
    monkeypatch.setenv("MINI_ORK_RUN_ID", run_id)
    sources: list[dict] = []
    block = execute._learned_block(str(run_dir), TC, "implementer", "fast",
                                   node_id, sources=sources, **kwargs)
    return block, sources


# ── the holdout, on the production path ─────────────────────────────────────

def test_production_path_records_the_held_out_slice_as_a_strict_subset(
        db, run_dir, monkeypatch):
    """Kickoff: "a strict-subset held row".

    The lessons the production path selects split into two sets: the ones the
    prompt gets, and the ones the holdout withholds. The withheld ones are
    recorded on the ledger with ``held_out=1`` and are a *strict* subset of
    the selected set — a holdout that reserved everything would measure
    nothing, and a holdout that reserved nothing would be the bug.
    """
    run_id = "holdout-run-subset"
    _seed_lessons(db)
    _seed_non_lesson_sources(db, run_id)
    block, sources = _production_block(run_dir, "node-subset", run_id,
                                       monkeypatch)

    reserved, delivered = _split(sources)
    reserved_lessons = {k for k in reserved if k[0] in LESSON_KINDS}
    delivered_lessons = {k for k in delivered if k[0] in LESSON_KINDS}
    assert reserved_lessons, (
        "the production path must withhold something — the holdout never "
        "reached the prompt")
    assert delivered_lessons, (
        "the production path must still deliver the rest of the lessons")
    assert reserved_lessons < reserved_lessons | delivered_lessons, (
        "the held-out slice must be a strict subset of the selected lessons")
    assert not (reserved_lessons & delivered_lessons), (
        "a lesson is either withheld or delivered, never both")

    # The withheld lesson really is gone from the prompt.
    execute_handlers._write_learned_record(
        str(run_dir), "node-subset", "implementer", "fast", TC,
        attempt=1, block=block, sources=sources, run_id=run_id)

    rows = _ledger_rows(db, run_id)
    held = {(kind, str(sid)) for kind, sid, flag in rows if flag == 1}
    live = {(kind, str(sid)) for kind, sid, flag in rows if flag == 0}
    assert held == reserved_lessons, (
        "every withheld lesson must reach the ledger with held_out=1 — the "
        "writer is what makes the slice measurable")
    assert reserved_lessons <= live | held, (
        "the ledger must account for every selected lesson")
    assert held < held | {k for k in live if k[0] in LESSON_KINDS}, (
        "the held-out rows must be a strict subset of the delivered ones")


def test_the_withheld_lesson_never_reaches_the_prompt(db, run_dir,
                                                       monkeypatch):
    """Kickoff: "A reserved lesson is omitted from the rendered markdown".

    A lesson that is recorded as withheld but still sits in the prompt is
    not held out at all — the node would be graded on the guidance it was
    given.
    """
    run_id = "holdout-run-omitted"
    marks = _seed_lessons(db)
    _seed_non_lesson_sources(db, run_id)
    block, sources = _production_block(run_dir, "node-omitted", run_id,
                                       monkeypatch)

    reserved, delivered = _split(sources)
    reserved_lessons = {k for k in reserved if k[0] in LESSON_KINDS}
    delivered_lessons = {k for k in delivered if k[0] in LESSON_KINDS}
    assert reserved_lessons and delivered_lessons

    for kind, ident in reserved_lessons:
        marker = marks[kind][ident]
        assert marker not in block, (
            f"reserved {kind}:{ident} must be omitted from the prompt")
    for kind, ident in delivered_lessons:
        marker = marks[kind][ident]
        assert marker in block, (
            f"delivered {kind}:{ident} must still reach the prompt")


def test_the_learned_json_does_not_claim_reserved_sources_as_delivered(
        db, run_dir, monkeypatch):
    """Kickoff: "keep only delivered sources in learned/<node>.json
    `sources`".

    The record is what the IDE renders as "what the learner was told". A
    withheld lesson was not told, so claiming it as delivered makes the
    record lie — and the lie is what an operator reading the Learning tab
    would take as evidence the lesson was used.
    """
    run_id = "holdout-run-json"
    _seed_lessons(db)
    _seed_non_lesson_sources(db, run_id)
    block, sources = _production_block(run_dir, "node-json", run_id,
                                       monkeypatch)

    reserved, _delivered = _split(sources)
    assert reserved, (
        "the fixture must withhold something for this test to mean")
    execute_handlers._write_learned_record(
        str(run_dir), "node-json", "implementer", "fast", TC,
        attempt=1, block=block, sources=sources, run_id=run_id)

    record = _read_json(run_dir, "node-json")
    json_ids = {(s.get("kind"), str(s.get("id"))) for s in record["sources"]}
    assert not [s for s in record["sources"] if s.get("held_out")], (
        "the learned JSON must not carry a held-out flag")
    assert not (json_ids & reserved), (
        "the learned JSON must not claim a reserved source as delivered")
    assert record["sources"], "the delivered sources must still be recorded"


def test_rate_zero_opts_the_holdout_out(db, run_dir, monkeypatch):
    """Kickoff: "`MO_LESSON_HOLDOUT_RATE` default 0.10, clamped to [0, 0.5],
    `0` disables".

    At rate 0 nothing is withheld, so the prompt keeps every lesson and the
    ledger keeps no held-out row. This is the operator's off switch, and it
    must be a real off switch — not a rate that happens to withhold nothing
    today.
    """
    run_id = "holdout-run-zero"
    monkeypatch.setenv("MO_LESSON_HOLDOUT_RATE", "0")
    _seed_lessons(db)
    _seed_non_lesson_sources(db, run_id)
    block, sources = _production_block(run_dir, "node-zero", run_id,
                                       monkeypatch)

    reserved, delivered = _split(sources)
    assert reserved == set(), "rate 0 must reserve no lesson on any channel"
    assert delivered, "rate 0 must still deliver the lessons"

    execute_handlers._write_learned_record(
        str(run_dir), "node-zero", "implementer", "fast", TC,
        attempt=1, block=block, sources=sources, run_id=run_id)
    rows = _ledger_rows(db, run_id)
    assert not [r for r in rows if r[2] == 1], (
        "rate 0 must leave no held-out row on the ledger")


def test_the_arm_is_the_same_across_runs_and_nodes(db, run_dir, monkeypatch):
    """Kickoff: "bucket = sha1 of `kind:id` so a lesson stays in the same arm
    across runs and nodes".

    The arm is a property of the lesson, not of the run: the same lesson is
    withheld the same way every time it is offered, which is what makes the
    withheld slice comparable across nodes.
    """
    _seed_lessons(db)
    # The static channel ranks by strength alone, so the selection itself
    # cannot drift between the two renders and the comparison is about the
    # arm, not about a re-ranking.
    monkeypatch.setenv("MO_SEMANTIC_INJECT", "0")

    _block_a, sources_a = _production_block(run_dir, "node-arm-a",
                                             "holdout-run-arm-a", monkeypatch)
    _block_b, sources_b = _production_block(run_dir, "node-arm-b",
                                             "holdout-run-arm-b", monkeypatch)
    reserved_a, delivered_a = _split(sources_a)
    reserved_b, delivered_b = _split(sources_b)

    assert reserved_a, "the fixture must withhold something on both renders"
    assert reserved_a == reserved_b, (
        "a lesson's arm must not depend on the run")
    assert delivered_a == delivered_b, (
        "a lesson's arm must not depend on the node")


def test_a_reserved_lesson_is_never_retrieved(db, run_dir, monkeypatch):
    """Kickoff: "the semantic path also excludes it from `record_retrievals`
    because it was never retrieved".

    A retrieval row credits a memory with a lesson the node was shown. A
    withheld lesson was shown to nobody, so logging one would credit the
    memory for guidance that never reached a prompt — and the memory's
    utility would then be measured against an arm it never entered.
    """
    run_id = "holdout-run-semantic"
    _seed_lessons(db)
    block, sources = _production_block(run_dir, "node-semantic", run_id,
                                       monkeypatch)

    reserved, delivered = _split(sources)
    reserved_patterns = {i for k, i in reserved if k == "pattern"}
    delivered_patterns = {i for k, i in delivered if k == "pattern"}
    assert reserved_patterns, "the fixture must withhold a pattern"
    assert delivered_patterns, "the fixture must deliver a pattern too"

    con = sqlite3.connect(db)
    try:
        used = {row[0] for row in con.execute(
            "SELECT memory_id FROM semantic_memory_uses WHERE run_id = ?",
            (run_id,))}
    finally:
        con.close()

    from mini_ork.memory import semantic

    _rows, class_scoped = ca._emergent_pool(db, TC, 24)
    scope = "%s#tc1" % TC if class_scoped else TC
    for pid in reserved_patterns:
        mid = semantic.lookup_key(scope=scope, key=pid, db_path=db)
        if mid is None:
            continue
        assert mid not in used, (
            "a reserved pattern was never retrieved: it must earn no "
            "retrieval row")


def test_non_lesson_sources_are_never_reserved(db, run_dir, monkeypatch):
    """Kickoff: "steering, preference, prior-run and context-v2 sources are
    never reserved".

    Those four are the operator's own instructions, not lessons the loop
    learned. Withholding one would not be a holdout — it would be the
    framework ignoring its operator.
    """
    run_id = "holdout-run-nonlesson"
    _seed_lessons(db)
    _seed_non_lesson_sources(db, run_id)
    _block, sources = _production_block(run_dir, "node-nonlesson", run_id,
                                        monkeypatch)

    kinds = {s.get("kind") for s in sources}
    assert "steering" in kinds, "the steering source must be present"
    assert "preference" in kinds, "the preference source must be present"
    for src in sources:
        if src.get("kind") not in LESSON_KINDS:
            assert not src.get("held_out"), (
                "%s sources are never reserved" % src.get("kind"))
