"""The induction stage: authoring a lesson from a cluster's traces.

`pattern_store.mine_from_traces` groups traces by `(task_class, status)` and
writes the group key as prose. That is a frequency count; nothing reads the
trajectories behind it. This stage is the missing inductive step (Trace2Skill
2603.25158 stage 2 + 3): a model reads the member traces and proposes the
guidance they imply, then deterministic guardrails decide what may be kept.

The guardrails are the contract under test. A proposer can talk its way past a
judgement call, so each guardrail is structural and each has a test here:
format, provenance (the only thing standing between a fabricated lesson and a
planner prompt), conflict, dedupe.
"""
from __future__ import annotations

import io
import json
import sqlite3

import pytest

from mini_ork.learning import pattern_induction as pi

# ── fixtures ────────────────────────────────────────────────────────────────

_TRACE_COLUMNS = (
    "trace_id", "task_class", "status", "verifier_output", "reviewer_verdict",
    "files_written", "files_read", "code_region", "reward_g",
)

_PATTERN_DDL = """
CREATE TABLE pattern_records (
    pattern_id        TEXT PRIMARY KEY,
    description       TEXT,
    evidence_trace_ids TEXT DEFAULT '[]',
    frequency         INTEGER DEFAULT 0
)
"""

_EMERGENT_DDL = """
CREATE TABLE emergent_patterns (
    pattern_id           TEXT PRIMARY KEY,
    cluster_label        TEXT,
    member_item_ids_json TEXT NOT NULL DEFAULT '[]',
    feature_set_json     TEXT NOT NULL DEFAULT '[]',
    strength_score       REAL NOT NULL DEFAULT 0,
    status               TEXT NOT NULL DEFAULT 'proposed',
    detected_at          INTEGER NOT NULL DEFAULT 0,
    resolved_at          INTEGER
)
"""


def _db(path, *, lesson_columns: bool = False) -> str:
    """A scratch DB with the two tables an induction pass touches.

    `lesson_columns=False` reproduces a database that predates migration 0056 —
    the state every existing install is in — so the healing path is exercised
    rather than assumed.
    """
    con = sqlite3.connect(path)
    con.execute(f"CREATE TABLE execution_traces ({', '.join(_TRACE_COLUMNS)})")
    con.execute(_PATTERN_DDL)
    con.execute(_EMERGENT_DDL)
    if lesson_columns:
        con.execute("ALTER TABLE pattern_records ADD COLUMN lesson_text TEXT")
        con.execute("ALTER TABLE emergent_patterns ADD COLUMN lesson_text TEXT")
    con.commit()
    con.close()
    return str(path)


def _seed_traces(db: str, rows: list[dict]) -> None:
    con = sqlite3.connect(db)
    for row in rows:
        cols = [c for c in _TRACE_COLUMNS if c in row]
        con.execute(
            f"INSERT INTO execution_traces({', '.join(cols)}) "
            f"VALUES ({', '.join('?' * len(cols))})",
            tuple(row[c] for c in cols),
        )
    con.commit()
    con.close()


def _seed_pattern(db: str, pattern_id: str, trace_ids: list[str], *, freq: int = 9) -> None:
    con = sqlite3.connect(db)
    con.execute(
        "INSERT INTO pattern_records(pattern_id, description, evidence_trace_ids, frequency) "
        "VALUES (?, ?, ?, ?)",
        (pattern_id, f"cluster: task_class=code-fix status=success (freq={freq})",
         json.dumps(trace_ids), freq),
    )
    con.commit()
    con.close()


def _seed_emergent(db: str, pattern_id: str, *, lesson: str | None = None) -> None:
    con = sqlite3.connect(db)
    cols = "pattern_id, cluster_label, status, detected_at"
    vals = f"'{pattern_id}', 'cluster: x', 'proposed', 1"
    if lesson is not None:
        cols += ", lesson_text"
        vals = f"'{pattern_id}', 'cluster: x', 'proposed', 1, '{lesson}'"
    con.execute(f"INSERT INTO emergent_patterns({cols}) VALUES ({vals})")
    con.commit()
    con.close()


def _fake_native(payload, *, rc: int = 0, capture: list | None = None):
    """Stand in for `llm_dispatch.llm_dispatch` at the process boundary.

    Writes the reply to stdout because that is the channel `_default_dispatch`
    captures — returning it any other way would test a contract the real
    dispatcher does not have.
    """
    def _fn(argv, *, root=None, dispatch_fn=None):
        if capture is not None:
            capture.append(list(argv))
        if rc != 0:
            return rc
        print(payload if isinstance(payload, str) else json.dumps(payload))
        return 0
    return _fn


def _lesson(condition: str, directive: str, traces: list[str], polarity: str = "do") -> dict:
    return {
        "condition": condition, "directive": directive,
        "polarity": polarity, "evidence_trace_ids": traces, "rationale": "because",
    }


# ── normalisation + rendering ───────────────────────────────────────────────

def test_normalise_condition_folds_connectors_case_and_punctuation():
    """One condition, however it is phrased, folds to one key.

    The connector matters beyond cosmetics: the conflict guardrail compares
    conditions by this key, so `When X` and `X` folding apart would let two
    directly contradictory claims both survive.
    """
    folded = pi.normalise_condition("the parser sees a bom")
    for variant in (
        "When the parser sees a BOM",
        "when  the PARSER sees a BOM.",
        "If the parser sees a BOM",
        "The parser sees a BOM!",
    ):
        assert pi.normalise_condition(variant) == folded, variant


def test_normalise_condition_keeps_distinct_targets_apart():
    """Folding must not merge two conditions that are about different things."""
    assert pi.normalise_condition("the parser sees a BOM") != pi.normalise_condition(
        "the renderer sees a BOM"
    )
    assert pi.normalise_condition("fix parser") != pi.normalise_condition("fix renderer")


def test_render_lesson_reads_as_one_bounded_instruction():
    do = pi.Lesson(
        directive="strip the BOM before parsing",
        condition="When the parser sees a BOM",
        evidence_trace_ids=["t1", "t2"],
    )
    assert pi.render_lesson(do) == "When the parser sees a BOM: strip the BOM before parsing"

    avoid = pi.Lesson(
        directive="stripping the BOM before parsing",
        condition="the parser sees a BOM",
        polarity="avoid",
        evidence_trace_ids=["t1", "t2"],
    )
    assert pi.render_lesson(avoid) == (
        "When the parser sees a BOM: avoid stripping the BOM before parsing"
    )


def test_render_lesson_keeps_the_condition():
    """A rule without its condition is the over-general advice that makes memory
    look helpful and behave as noise — the condition is the load-bearing half."""
    line = pi.render_lesson(pi.Lesson(
        directive="d", condition="the cwd guard blocks the dispatch",
        evidence_trace_ids=["t1", "t2"],
    ))
    assert "the cwd guard blocks the dispatch" in line


# ── guardrail 1: format ─────────────────────────────────────────────────────

@pytest.mark.parametrize("bad", [
    "not a dict",
    None,
    {},                                             # nothing at all
    {"condition": "c", "directive": "d", "polarity": "maybe"},   # unknown polarity
    {"condition": "", "directive": "d", "polarity": "do"},       # no condition
    {"condition": "c", "directive": "", "polarity": "do"},       # no directive
])
def test_consolidate_rejects_unusable_proposals(bad):
    """An unknown polarity is rejected, never coerced.

    'do' and 'avoid' are opposite instructions, so defaulting between them
    would invert the lesson's meaning — worse than dropping it.
    """
    result = pi.consolidate([bad], member_trace_ids=["t1", "t2"])
    assert result.kept == []
    assert result.rejected[0]["guardrail"] == "format"


# ── guardrail 2: provenance ─────────────────────────────────────────────────

def test_consolidate_rejects_a_lesson_citing_traces_outside_the_cluster():
    """The one guard that matters: the analyst is the only thing that can name
    its own evidence, so the only defence is requiring it to be checkable."""
    result = pi.consolidate(
        [_lesson("c", "d", ["t1", "invented-trace"])],
        member_trace_ids=["t1", "t2"],
    )
    assert result.kept == []
    assert result.rejected[0]["guardrail"] == "provenance"
    assert result.rejected[0]["unknown_trace_ids"] == ["invented-trace"]


def test_consolidate_counts_a_repeated_trace_once():
    """One trace id repeated is one observation.

    This is the whole reason the floor is on distinct ids rather than list
    length: a single incident generalised into a rule is how a prompt acquires
    folklore.
    """
    result = pi.consolidate(
        [_lesson("c", "d", ["t1", "t1", "t1"])],
        member_trace_ids=["t1", "t2", "t3"],
    )
    assert result.kept == []
    assert result.rejected[0]["guardrail"] == "provenance"
    assert result.rejected[0]["n_evidence"] == 1


def test_consolidate_keeps_a_lesson_at_the_floor():
    result = pi.consolidate(
        [_lesson("c", "d", ["t1", "t2"])],
        member_trace_ids=["t1", "t2", "t3"],
    )
    assert len(result.kept) == 1
    assert result.kept[0].evidence_trace_ids == ["t1", "t2"]


def test_consolidate_floor_cannot_be_lowered_by_a_caller():
    """The floor is a floor: passing a smaller one does not admit thinner
    evidence, mirroring the same clamp in the promotion and verify gates."""
    result = pi.consolidate(
        [_lesson("c", "d", ["t1"])],
        member_trace_ids=["t1", "t2"],
        floor=0,
    )
    assert result.kept == []


# ── guardrail 3: dedupe ─────────────────────────────────────────────────────

def test_consolidate_unions_evidence_when_the_same_lesson_repeats():
    result = pi.consolidate(
        [_lesson("c", "d", ["t1", "t2"]), _lesson("c", "d", ["t2", "t3"])],
        member_trace_ids=["t1", "t2", "t3"],
    )
    assert len(result.kept) == 1
    assert result.kept[0].evidence_trace_ids == ["t1", "t2", "t3"]


# ── guardrail 4: conflict ───────────────────────────────────────────────────

def test_consolidate_withholds_both_sides_of_a_contradiction():
    """Nothing deterministic can rank two contradictory claims, and picking one
    by list order would make the prompt depend on argument order — so both are
    withheld and the caller is told."""
    result = pi.consolidate(
        [_lesson("c", "do it", ["t1", "t2"]),
         _lesson("c", "not it", ["t1", "t2"], polarity="avoid")],
        member_trace_ids=["t1", "t2"],
    )
    assert result.kept == []
    assert len(result.conflicts) == 2
    assert {c["polarity"] for c in result.conflicts} == {"do", "avoid"}


def test_consolidate_detects_a_conflict_across_different_phrasings():
    """The contradiction must not be evadable by rephrasing one side."""
    result = pi.consolidate(
        [_lesson("When the parser sees a BOM", "strip it", ["t1", "t2"]),
         _lesson("the parser sees a BOM", "keeping the bytes", ["t1", "t2"], polarity="avoid")],
        member_trace_ids=["t1", "t2"],
    )
    assert result.kept == []
    assert len(result.conflicts) == 2


def test_consolidate_keeps_same_polarity_under_one_condition():
    """Agreement on a condition is not a conflict."""
    result = pi.consolidate(
        [_lesson("c", "d1", ["t1", "t2"]), _lesson("c", "d2", ["t1", "t2"])],
        member_trace_ids=["t1", "t2"],
    )
    assert len(result.kept) == 2


def test_consolidate_is_order_stable():
    pool = [
        _lesson("alpha", "one", ["t1", "t2"]),
        _lesson("bravo", "two", ["t1", "t2"]),
        _lesson("bravo", "two", ["t3"]),
    ]
    members = ["t1", "t2", "t3"]
    first = pi.consolidate(pool, member_trace_ids=members)
    second = pi.consolidate(list(reversed(pool)), member_trace_ids=members)
    assert [(x.condition_key, x.directive) for x in first.kept] == [
        (x.condition_key, x.directive) for x in second.kept
    ]


# ── stage 3: merge ──────────────────────────────────────────────────────────

def test_merge_lessons_passes_a_single_lesson_through(monkeypatch):
    called = []
    monkeypatch.setattr(pi, "_default_dispatch", lambda *a, **k: called.append(1) or (0, ""))
    only = pi.Lesson("d", "c", evidence_trace_ids=["t1", "t2"])
    assert pi.merge_lessons([only], target="x") is only
    assert called == [], "a single lesson needs no merge call"


def test_merge_lessons_falls_back_to_the_best_evidenced_lesson(monkeypatch):
    """A merge that cannot run must not empty the block. The deterministic
    choice is most independent evidence, then stable key order — a prompt must
    never depend on a model call succeeding."""
    monkeypatch.setattr(pi, "_default_dispatch", lambda *a, **k: (1, ""))
    thin = pi.Lesson("thin", "c1", evidence_trace_ids=["t1", "t2"])
    thick = pi.Lesson("thick", "c2", evidence_trace_ids=["t1", "t2", "t3"])
    assert pi.merge_lessons([thin, thick], target="x") is thick


def test_merge_lessons_refuses_evidence_its_inputs_never_cited(monkeypatch):
    """Otherwise consolidation becomes a laundering channel: the merge call
    could attach a trace id no analyst ever cited, and it would persist as
    though the evidence existed."""
    payload = {"lessons": [_lesson("merged", "d", ["t1", "t2", "ghost"])]}
    monkeypatch.setattr(pi, "_default_dispatch", lambda *a, **k: (0, json.dumps(payload)))
    a = pi.Lesson("d1", "c1", evidence_trace_ids=["t1", "t2"])
    b = pi.Lesson("d2", "c2", evidence_trace_ids=["t1", "t2", "t3"])
    picked = pi.merge_lessons([a, b], target="x")
    assert "ghost" not in picked.evidence_trace_ids
    assert picked is b  # the deterministic fallback, by evidence count


def test_merge_lessons_requires_the_merged_lesson_to_clear_the_floor(monkeypatch):
    """A merge that compresses three lessons into one claim citing a single
    trace has lost the independence, not earned an exemption from the floor."""
    payload = {"lessons": [_lesson("merged", "d", ["t1"])]}
    monkeypatch.setattr(pi, "_default_dispatch", lambda *a, **k: (0, json.dumps(payload)))
    a = pi.Lesson("d1", "c1", evidence_trace_ids=["t1", "t2"])
    b = pi.Lesson("d2", "c2", evidence_trace_ids=["t1", "t2", "t3"])
    picked = pi.merge_lessons([a, b], target="x")
    assert picked is b, "a one-trace merge should not have been accepted"


def test_merge_lessons_accepts_a_merge_that_keeps_its_evidence(monkeypatch):
    payload = {"lessons": [_lesson("merged condition", "do the merged thing", ["t1", "t2"])]}
    monkeypatch.setattr(pi, "_default_dispatch", lambda *a, **k: (0, json.dumps(payload)))
    a = pi.Lesson("d1", "c1", evidence_trace_ids=["t1", "t2"])
    b = pi.Lesson("d2", "c2", evidence_trace_ids=["t1", "t2", "t3"])
    picked = pi.merge_lessons([a, b], target="x")
    assert picked.directive == "do the merged thing"
    assert picked.evidence_trace_ids == ["t1", "t2"]


# ── stage 2: proposal ───────────────────────────────────────────────────────

def test_propose_lessons_caps_the_number_of_calls(monkeypatch):
    """A cluster with hundreds of members is sampled, not fully read: the
    marginal trace beyond a few batches changes the guidance far less than it
    changes the bill."""
    seen = []
    monkeypatch.setattr(
        pi, "_default_dispatch",
        lambda prompt, **k: seen.append(prompt) or (0, json.dumps({"lessons": []})),
    )
    rows = [{"trace_id": f"t{i}", "status": "success"} for i in range(50)]
    pi.propose_lessons(rows, target="x", batch_size=2, max_batches=3, max_workers=1)
    assert len(seen) == 3


def test_propose_lessons_returns_nothing_on_a_failed_call(monkeypatch):
    """A non-zero rc is a failed call, never an empty-but-successful answer."""
    monkeypatch.setattr(pi, "_default_dispatch", lambda *a, **k: (1, ""))
    rows = [{"trace_id": "t1", "status": "success"}]
    assert pi.propose_lessons(rows, target="x", max_workers=1) == []


def test_propose_lessons_tolerates_a_code_fence(monkeypatch):
    fenced = "```json\n" + json.dumps({"lessons": [_lesson("c", "d", ["t1", "t2"])]}) + "\n```"
    monkeypatch.setattr(pi, "_default_dispatch", lambda *a, **k: (0, fenced))
    rows = [{"trace_id": "t1", "status": "success"}]
    got = pi.propose_lessons(rows, target="x", max_workers=1)
    assert len(got) == 1 and got[0]["condition"] == "c"


def test_propose_lessons_says_nothing_about_no_rows():
    assert pi.propose_lessons([], target="x") == []


def test_default_dispatch_names_the_induct_node_and_honours_the_model_env(
    monkeypatch,
):
    captured: list = []
    monkeypatch.setattr(
        "mini_ork.dispatch.llm_dispatch.llm_dispatch",
        _fake_native({"lessons": []}, capture=captured),
    )
    monkeypatch.setenv("MINI_ORK_INDUCE_MODEL", "some-model")
    rc, _out = pi._default_dispatch("prompt text")
    assert rc == 0
    argv = captured[0]
    assert argv[argv.index("--node-type") + 1] == "pattern-induct"
    assert argv[argv.index("--model") + 1] == "some-model"
    assert argv[argv.index("--prompt-text") + 1] == "prompt text"


def test_default_dispatch_fails_closed_when_the_transport_raises(monkeypatch):
    def _boom(argv, *, root=None, dispatch_fn=None):
        raise RuntimeError("transport down")

    monkeypatch.setattr("mini_ork.dispatch.llm_dispatch.llm_dispatch", _boom)
    assert pi._default_dispatch("p") == (1, "")


# ── orchestration ───────────────────────────────────────────────────────────

def _stub_analyst(monkeypatch, lessons, *, rc: int = 0):
    monkeypatch.setattr(
        pi, "_default_dispatch",
        lambda *a, **k: (rc, json.dumps({"lessons": lessons})),
    )


def test_induce_cluster_authors_a_lesson(tmp_path, monkeypatch):
    db = _db(tmp_path / "s.db", lesson_columns=True)
    _seed_traces(db, [
        {"trace_id": "t1", "task_class": "code-fix", "status": "success",
         "verifier_output": "ok"},
        {"trace_id": "t2", "task_class": "code-fix", "status": "success",
         "verifier_output": "ok"},
    ])
    _stub_analyst(monkeypatch, [_lesson("the verifier emits nothing", "treat the node as failed", ["t1", "t2"])])
    con = sqlite3.connect(db)
    try:
        text, report = pi.induce_cluster(
            con, target="cluster: code-fix/success", member_trace_ids=["t1", "t2"],
        )
    finally:
        con.close()
    assert text == "When the verifier emits nothing: treat the node as failed"
    assert report["n_kept"] == 1


def test_induce_cluster_reports_when_no_traces_are_readable(tmp_path):
    """The telemetry gap is real: a cluster whose traces carry no readable
    signal yields no lesson and says so, rather than inventing one."""
    db = _db(tmp_path / "s.db")
    con = sqlite3.connect(db)
    try:
        text, report = pi.induce_cluster(con, target="x", member_trace_ids=["missing"])
    finally:
        con.close()
    assert text == ""
    assert "no readable member traces" in report["reason"]


def test_induce_cluster_reports_when_nothing_survives_the_guardrails(tmp_path, monkeypatch):
    db = _db(tmp_path / "s.db")
    _seed_traces(db, [{"trace_id": "t1", "status": "success"}])
    # Cites a trace that is not a member → provenance rejects it.
    _stub_analyst(monkeypatch, [_lesson("c", "d", ["ghost", "phantom"])])
    con = sqlite3.connect(db)
    try:
        text, report = pi.induce_cluster(con, target="x", member_trace_ids=["t1"])
    finally:
        con.close()
    assert text == ""
    assert report["rejected"][0]["guardrail"] == "provenance"


def test_induce_cluster_authors_nothing_when_the_analyst_is_silent(tmp_path, monkeypatch):
    """An empty proposal list is the honest answer, not a failure: it must not
    be turned into a lesson derived from the cluster label."""
    db = _db(tmp_path / "s.db")
    _seed_traces(db, [{"trace_id": "t1", "status": "success"}])
    _stub_analyst(monkeypatch, [])
    con = sqlite3.connect(db)
    try:
        text, _report = pi.induce_cluster(con, target="x", member_trace_ids=["t1"])
    finally:
        con.close()
    assert text == ""


def test_induce_pending_authors_and_propagates_the_lesson(tmp_path, monkeypatch):
    """The lesson lands on `pattern_records` and on the judge-gate row the
    prompt block actually reads. Without the propagation the agent would see it
    one reflect later, and a cluster never re-mined would never carry it."""
    db = _db(tmp_path / "s.db", lesson_columns=True)
    _seed_traces(db, [
        {"trace_id": "t1", "task_class": "code-fix", "status": "success"},
        {"trace_id": "t2", "task_class": "code-fix", "status": "success"},
        {"trace_id": "t3", "task_class": "code-fix", "status": "success"},
    ])
    _seed_pattern(db, "pat-1", ["t1", "t2", "t3"])
    _seed_emergent(db, "pat-1")
    _stub_analyst(monkeypatch, [_lesson("the run succeeds", "keep the plan small", ["t1", "t2", "t3"])])

    report = pi.induce_pending(db_path=db, min_cluster=3)
    assert report["induced"] == 1

    con = sqlite3.connect(db)
    try:
        pattern_lesson = con.execute(
            "SELECT lesson_text FROM pattern_records WHERE pattern_id='pat-1'"
        ).fetchone()
        emergent_lesson = con.execute(
            "SELECT lesson_text FROM emergent_patterns WHERE pattern_id='pat-1'"
        ).fetchone()
    finally:
        con.close()
    assert pattern_lesson is not None, "induce_pending no-op'd"
    assert pattern_lesson[0] == "When the run succeeds: keep the plan small"
    assert emergent_lesson[0] == pattern_lesson[0]


def test_induce_pending_never_re_authors_an_existing_lesson(tmp_path, monkeypatch):
    """Re-authoring is a separate, deliberate act: a second pass must not
    overwrite a lesson with a second opinion."""
    db = _db(tmp_path / "s.db", lesson_columns=True)
    _seed_traces(db, [{"trace_id": f"t{i}", "status": "success"} for i in range(3)])
    _seed_pattern(db, "pat-1", ["t0", "t1", "t2"])
    con = sqlite3.connect(db)
    con.execute("UPDATE pattern_records SET lesson_text='already authored' WHERE pattern_id='pat-1'")
    con.commit()
    con.close()

    calls = []
    monkeypatch.setattr(
        pi, "_default_dispatch",
        lambda *a, **k: calls.append(1) or (0, json.dumps({"lessons": []})),
    )
    report = pi.induce_pending(db_path=db, min_cluster=3)
    assert report["induced"] == 0
    assert calls == [], "an authored cluster must not be read again"

    con = sqlite3.connect(db)
    kept = con.execute("SELECT lesson_text FROM pattern_records").fetchone()[0]
    con.close()
    assert kept == "already authored"


def test_induce_pending_heals_a_pre_0056_database(tmp_path, monkeypatch):
    """Every existing install predates the column. Reading or writing
    `lesson_text` must therefore heal the schema itself rather than fail."""
    db = _db(tmp_path / "s.db", lesson_columns=False)
    _seed_traces(db, [{"trace_id": f"t{i}", "status": "success"} for i in range(3)])
    _seed_pattern(db, "pat-1", ["t0", "t1", "t2"])
    _stub_analyst(monkeypatch, [_lesson("c", "d", ["t0", "t1", "t2"])])

    report = pi.induce_pending(db_path=db, min_cluster=3)
    assert report["induced"] == 1

    con = sqlite3.connect(db)
    try:
        cols = [r[1] for r in con.execute("PRAGMA table_info('pattern_records')")]
        lesson = con.execute("SELECT lesson_text FROM pattern_records").fetchone()[0]
    finally:
        con.close()
    assert "lesson_text" in cols
    assert lesson == "When c: d"


def test_induce_pending_skips_a_cluster_that_is_too_small(tmp_path, monkeypatch):
    """A cluster of one is an incident, not a pattern."""
    db = _db(tmp_path / "s.db", lesson_columns=True)
    _seed_traces(db, [{"trace_id": "t1", "status": "success"}])
    _seed_pattern(db, "pat-1", ["t1"])
    calls = []
    monkeypatch.setattr(
        pi, "_default_dispatch",
        lambda *a, **k: calls.append(1) or (0, json.dumps({"lessons": []})),
    )
    report = pi.induce_pending(db_path=db, min_cluster=3)
    assert report["induced"] == 0
    assert report["skipped"][0]["reason"] == "too few members"
    assert calls == []


def test_induce_pending_is_off_when_opted_out(tmp_path, monkeypatch):
    monkeypatch.setenv("MO_PATTERN_INDUCE", "0")
    report = pi.induce_pending(db_path=str(tmp_path / "does-not-exist.db"))
    assert report == {"induced": 0, "skipped": [], "enabled": False}


def test_induce_pending_is_on_by_default(tmp_path, monkeypatch):
    """New capability ships ON with an opt-out — the env var exists to go back,
    not to switch it on."""
    monkeypatch.delenv("MO_PATTERN_INDUCE", raising=False)
    report = pi.induce_pending(db_path=str(tmp_path / "does-not-exist.db"))
    assert report["enabled"] is True


def test_induce_pending_without_a_database_is_a_no_op(tmp_path, monkeypatch):
    monkeypatch.delenv("MO_PATTERN_INDUCE", raising=False)
    report = pi.induce_pending(db_path=str(tmp_path / "missing.db"))
    assert report["induced"] == 0 and report["skipped"] == []


# ── the reflect wiring ──────────────────────────────────────────────────────

def _drive_reflect(monkeypatch, db: str, argv: list[str] | None = None) -> str:
    """Run `reflect.main` in-process with its LLM-bound stages stubbed.

    Only the induction block is left live, so what this asserts is the wiring:
    whether reflect reaches the stage at all. The trace write is stubbed
    because the scratch DB carries no epics/runs rows — an unrelated table.

    Every stub goes through `monkeypatch`: these are module attributes, so a
    bare assignment outlives the test and poisons every later file in the
    process that calls the same function.
    """
    from contextlib import redirect_stdout

    from mini_ork.cli import reflect
    from mini_ork.learning import reflection_pipeline as rp
    from mini_ork.stores import pattern_store

    monkeypatch.setattr(rp, "reflection_run", lambda since: "[]")
    monkeypatch.setattr(pattern_store, "mine_from_traces", lambda **k: 0)
    monkeypatch.setattr(reflect, "_trace_write", lambda *a, **k: None)

    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = reflect.main(argv or ["--since", "7d"])
    assert rc == 0, buf.getvalue()
    return buf.getvalue()


def _stub_analyst_citing_members(monkeypatch, members: list[str]) -> list:
    calls: list = []
    monkeypatch.setattr(
        pi, "_default_dispatch",
        lambda *a, **k: calls.append(1) or (0, json.dumps({"lessons": [
            _lesson("the reflect pass runs", "log the count", members),
        ]})),
    )
    return calls


def test_reflect_reaches_the_induction_stage(tmp_path, monkeypatch):
    """The stage is wired into the live reflect path, not just callable.

    A stage nobody invokes is the exact failure mode this codebase has hit
    before with built-but-unwired gates, so the wiring is asserted rather than
    assumed."""
    db = _db(tmp_path / "s.db", lesson_columns=True)
    _seed_traces(db, [{"trace_id": f"t{i}", "status": "success"} for i in range(3)])
    _seed_pattern(db, "pat-1", ["t0", "t1", "t2"])
    monkeypatch.setenv("MINI_ORK_DB", db)
    monkeypatch.delenv("MO_PATTERN_INDUCE", raising=False)
    calls = _stub_analyst_citing_members(monkeypatch, ["t0", "t1", "t2"])

    out = _drive_reflect(monkeypatch, db)
    assert len(calls) == 1, "reflect never called the analyst"
    assert "pattern_induct" in out

    con = sqlite3.connect(db)
    try:
        lesson = con.execute("SELECT lesson_text FROM pattern_records").fetchone()[0]
    finally:
        con.close()
    assert lesson == "When the reflect pass runs: log the count"


def test_reflect_skips_the_induction_stage_when_opted_out(tmp_path, monkeypatch):
    db = _db(tmp_path / "s.db", lesson_columns=True)
    _seed_traces(db, [{"trace_id": f"t{i}", "status": "success"} for i in range(3)])
    _seed_pattern(db, "pat-1", ["t0", "t1", "t2"])
    monkeypatch.setenv("MINI_ORK_DB", db)
    monkeypatch.setenv("MO_PATTERN_INDUCE", "0")
    calls = _stub_analyst_citing_members(monkeypatch, ["t0", "t1", "t2"])

    out = _drive_reflect(monkeypatch, db)
    assert calls == [], "the opt-out did not stop the analyst call"
    assert "pattern_induct" not in out
