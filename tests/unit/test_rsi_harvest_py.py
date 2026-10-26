"""Every rule must fire on the evidence it claims, and only on that evidence.

The dangerous failure for a gap detector is not a false positive — it is a rule
that silently never fires (a stale predicate) or fires on an absent table (a
fake bug on every fresh install). So each test below seeds exactly one shape and
asserts both sides: the defect produces a candidate, and the healthy shape does
not.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from mini_ork.rsi import harvest as harvest_mod
from mini_ork.rsi.harvest import HarvestReport, harvest, parse_ts
from mini_ork.rsi.probes import Finding, ProbeResult

NOW = 1_792_000_000
DAY = 86400


# --------------------------------------------------------------------------
# Fixtures / helpers
# --------------------------------------------------------------------------

@pytest.fixture
def con():
    conn = sqlite3.connect(":memory:")
    yield conn
    conn.close()


def ddl(con: sqlite3.Connection, sql: str) -> None:
    con.executescript(sql)
    con.commit()


def write(root: Path, files: dict[str, str]) -> None:
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")


def report(root: Path, con: sqlite3.Connection, probes=()) -> HarvestReport:
    return harvest(root, con, now=NOW, probes=probes)


def of(rep: HarvestReport, source: str) -> list:
    return [c for c in rep.candidates if c.source == source]


def one(rep: HarvestReport, source: str):
    found = of(rep, source)
    assert len(found) == 1, f"{source}: {[c.title for c in found]}"
    return found[0]


# --------------------------------------------------------------------------
# Skip / drift semantics
# --------------------------------------------------------------------------

def test_empty_database_is_healthy_not_broken(tmp_path, con):
    rep = report(tmp_path, con)
    assert rep.candidates == ()
    assert rep.harvest_ok is True
    assert rep.errors == ()
    # Absent tables are *skips*, not findings: a fresh install is not a bug.
    assert {"L4", "V6", "R6"} <= set(rep.skipped)


def test_missing_column_on_a_present_table_is_a_probe_broken_candidate(tmp_path, con):
    ddl(con, "CREATE TABLE verifier_results (result_id TEXT, verdict TEXT)")
    rep = report(tmp_path, con)
    cand = one(rep, "db:V1")
    assert cand.kind == "probe_broken"
    assert "confidence" in cand.detail
    # A schema drift is evidence the harvesting itself is compromised.
    # Drift is a finding, not a halt: the loop must be able to fix it.
    assert rep.harvest_ok is True


def test_llm_calls_with_a_chain_in_model_id_is_a_routing_gap(tmp_path, con):
    # R8's subject is a *chain where a lane belongs*, not the absence of a lane
    # column: ``model_id`` already doubles as the serving lane, so asserting
    # "cost cannot be attributed" was a claim nobody measured. Cycles 12-13
    # (2026-10-26) spent $3.00 and three critic rejections on a column that was
    # a byte-copy of model_id. What is left is the real defect.
    ddl(con, "CREATE TABLE llm_calls (id INTEGER, model_id TEXT NOT NULL)")
    con.executemany("INSERT INTO llm_calls VALUES (?, ?)",
                    [(1, "glm"), (2, "deepseek_flash,minimax,glm")])
    con.commit()
    rep = report(tmp_path, con)
    cand = one(rep, "db:R8")
    assert cand.kind == "quality_red"
    assert cand.evidence_key == "table:llm_calls:model_id:chain"
    assert cand.evidence["rows"] == 2
    assert cand.evidence["chain_rows"] == 1
    # Severity follows the measured reach (one malformed row), not the table.
    assert cand.severity < 50
    assert "R8" not in rep.skipped and rep.harvest_ok is True


def test_r8_fix_hint_forbids_inventing_a_lane(tmp_path, con):
    # Cycles 12-13 quarantined because the hint said where to *get* a lane but
    # not what to write when none was resolved, so the child stamped the first
    # entry of the fused ``model`` chain onto refusal and crash rows. The hint
    # travels into the cycle brief, which every lens and the synthesizer read,
    # so it is part of the contract.
    ddl(con, "CREATE TABLE llm_calls (id INTEGER, model_id TEXT NOT NULL)")
    con.execute("INSERT INTO llm_calls VALUES (1, 'glm,minimax')")
    cand = one(report(tmp_path, con), "db:R8")
    hint = cand.fix_hint.lower()
    assert "null" in hint
    assert "infer" in hint


def test_one_lane_per_row_is_healthy(tmp_path, con):
    # 16,533 of 16,543 rows on the live home are exactly this shape, which is
    # why the old structural rule mis-ranked the gap at 74.
    ddl(con, "CREATE TABLE llm_calls (id INTEGER, model_id TEXT NOT NULL)")
    con.executemany("INSERT INTO llm_calls VALUES (?, ?)",
                    [(1, "glm"), (2, "minimax")])
    assert "R8" in report(tmp_path, con).closed


def test_a_lane_column_alone_does_not_close_the_gap(tmp_path, con):
    # The rejected cycle-12/13 fix in one assertion: a new column whose writers
    # still stamp a chain into model_id leaves the malformed rows in place, and
    # the new column NULL. On a copy of the database that is not a fix.
    ddl(con, "CREATE TABLE llm_calls (id INTEGER, model_id TEXT, lane TEXT)")
    con.execute("INSERT INTO llm_calls VALUES (1, 'glm,minimax', NULL)")
    assert "R8" not in report(tmp_path, con).closed


def test_a_crashing_rule_is_evidence_not_a_stop(tmp_path, con, monkeypatch):
    def boom(ctx, domain, rule_id):
        raise RuntimeError("rule exploded")

    monkeypatch.setattr(harvest_mod, "ALL_RULES", (("learning", (("X1", boom),)),))
    rep = report(tmp_path, con)
    assert rep.errors == ("X1",)
    assert rep.harvest_ok is False
    assert rep.candidates[0].kind == "probe_broken"


# --------------------------------------------------------------------------
# parse_ts — the ':fZ' corruption guard
# --------------------------------------------------------------------------

def test_parse_ts_accepts_epoch_iso_and_truncates_fractional_suffixes():
    assert parse_ts(1792000000) == 1792000000
    assert parse_ts("1792000000") == 1792000000
    assert parse_ts("2026-10-25T12:00:00") == parse_ts("2026-10-25T12:00:00.123Z")
    # The real corruption shape: seconds were replaced by the literal 'fZ'.
    assert parse_ts("2026-09-12T12:53:fZ") is None
    assert parse_ts("garbage") is None


# --------------------------------------------------------------------------
# LEARNING
# --------------------------------------------------------------------------

def test_l1_fires_when_the_writer_has_no_external_caller(tmp_path, con):
    ddl(con, "CREATE TABLE learning_pass_stats (id INTEGER)")
    rep = report(tmp_path, con)
    cand = one(rep, "db:L1")
    assert cand.kind == "producer_missing"
    assert cand.evidence["external_call_sites"] == 0


def test_l1_closes_when_a_caller_exists(tmp_path, con):
    ddl(con, "CREATE TABLE learning_pass_stats (id INTEGER)")
    write(tmp_path, {"mini_ork/runner.py": "record_pass_stat(stage='reflect')\n"})
    assert "L1" in report(tmp_path, con).closed


def test_l1_closes_when_rows_exist(tmp_path, con):
    ddl(con, "CREATE TABLE learning_pass_stats (id INTEGER);"
              "INSERT INTO learning_pass_stats VALUES (1);")
    assert "L1" in report(tmp_path, con).closed


def test_l2_fires_without_an_insert_and_closes_with_one(tmp_path, con):
    ddl(con, "CREATE TABLE reflection_log (log_id TEXT)")
    assert one(report(tmp_path, con), "db:L2").kind == "producer_missing"
    write(tmp_path, {"mini_ork/reflect.py": "cur.execute('INSERT INTO reflection_log VALUES (1)')\n"})
    assert "L2" in report(tmp_path, con).closed


def test_l3_fires_when_nothing_is_held_out(tmp_path, con):
    ddl(con, "CREATE TABLE lesson_injections (id INTEGER, held_out INTEGER);"
              "INSERT INTO lesson_injections VALUES (1,0),(2,0);")
    assert one(report(tmp_path, con), "db:L3").evidence["rows"] == 2


def test_l3_skips_on_an_empty_injection_table(tmp_path, con):
    ddl(con, "CREATE TABLE lesson_injections (id INTEGER, held_out INTEGER)")
    assert "L3" in report(tmp_path, con).skipped


def test_l4_fires_on_malformed_timestamps(tmp_path, con):
    ddl(con, """
        CREATE TABLE promotion_records (decided_at TEXT);
        CREATE TABLE apply_attempts (created_at TEXT);
        INSERT INTO promotion_records VALUES ('2026-09-12T12:53:fZ');
        INSERT INTO promotion_records VALUES ('2026-10-02T10:00:00.123Z');
        INSERT INTO apply_attempts VALUES ('2026-10-03T10:00:00.123Z');
    """)
    cand = one(report(tmp_path, con), "db:L4")
    assert cand.kind == "temporal_corruption"
    assert cand.evidence["malformed"] == {"promotion_records": 1, "apply_attempts": 0}


def test_l5_threshold_is_strictly_above_the_ratio(tmp_path, con):
    ddl(con, "CREATE TABLE pattern_records (promoted_to TEXT);")
    con.executemany("INSERT INTO pattern_records VALUES (NULL)", [()] * 10)
    con.execute("INSERT INTO pattern_records VALUES ('v2')")
    con.commit()
    # 10/11 = 0.909 > 0.9.
    assert one(report(tmp_path, con), "db:L5").kind == "data_path_dead"


def test_l5_does_not_fire_at_exactly_the_threshold(tmp_path, con):
    ddl(con, "CREATE TABLE pattern_records (promoted_to TEXT);")
    con.executemany("INSERT INTO pattern_records VALUES (NULL)", [()] * 9)
    con.executemany("INSERT INTO pattern_records VALUES ('v2')", [()] * 1)
    con.commit()
    # 9/10 = 0.9 is not > 0.9.
    assert "L5" in report(tmp_path, con).closed


def test_l6_fires_when_most_themes_carry_no_lesson_text(tmp_path, con):
    ddl(con, "CREATE TABLE lesson_themes (lesson_text TEXT);")
    con.executemany("INSERT INTO lesson_themes VALUES (NULL)", [()] * 9)
    con.executemany("INSERT INTO lesson_themes VALUES ('do x')", [()] * 1)
    con.commit()
    assert one(report(tmp_path, con), "db:L6").kind == "data_path_dead"


def test_l7_fires_below_min_steps_and_closes_at_it(tmp_path, con):
    ddl(con, "CREATE TABLE collapse_history (id INTEGER);"
              "INSERT INTO collapse_history VALUES (1),(2);")
    rep = report(tmp_path, con)
    assert one(rep, "db:L7").kind == "coverage_gap"
    con.executemany("INSERT INTO collapse_history VALUES (?)", [(3,), (4,)])
    con.commit()
    assert "L7" in report(tmp_path, con).closed


def test_l9_fires_only_when_deltas_are_absent_and_the_table_is_stale(tmp_path, con):
    ddl(con, "CREATE TABLE learning_record (benchmark_delta REAL, created_at TEXT);")
    con.execute("INSERT INTO learning_record VALUES (NULL, ?)", (NOW - 40 * DAY,))
    con.commit()
    assert one(report(tmp_path, con), "db:L9").evidence["stale_days"] >= 40

    con.execute("INSERT INTO learning_record VALUES (0.1, ?)", (NOW - 40 * DAY,))
    con.commit()
    assert "L9" in report(tmp_path, con).closed


def test_l10_fires_when_conflicts_are_empty_but_themes_exist(tmp_path, con):
    ddl(con, """
        CREATE TABLE lesson_conflicts (id INTEGER);
        CREATE TABLE lesson_themes (theme_id TEXT);
        INSERT INTO lesson_themes VALUES ('a'),('b'),('c');
    """)
    assert one(report(tmp_path, con), "db:L10").evidence["themes"] == 3
    ddl(con, "INSERT INTO lesson_conflicts VALUES (1);")
    assert "L10" in report(tmp_path, con).closed


def test_l11_fires_only_after_the_labelling_window(tmp_path, con):
    ddl(con, "CREATE TABLE diagnosis_ledger (id INTEGER, label TEXT, created_at INTEGER);")
    con.execute("INSERT INTO diagnosis_ledger VALUES (1, NULL, ?)", (NOW - 2 * DAY,))
    con.commit()
    assert "L11" in report(tmp_path, con).closed

    con.execute("INSERT INTO diagnosis_ledger VALUES (2, NULL, ?)", (NOW - 10 * DAY,))
    con.commit()
    assert one(report(tmp_path, con), "db:L11").kind == "stale_signal"


VERIFIER_RESULTS_DDL = """
    CREATE TABLE verifier_results (
        result_id TEXT, run_id TEXT, verifier_name TEXT, rubric_id TEXT,
        verdict TEXT, confidence REAL, scored_axes_json TEXT,
        is_false_positive INTEGER, is_false_negative INTEGER,
        annotated_by TEXT, annotated_at TEXT, repair_run_id TEXT, notes TEXT,
        created_at TEXT
    );
"""


def _seed_verifier_results(con: sqlite3.Connection, verdicts: list[str], name: str = "v1") -> None:
    for i, verdict in enumerate(verdicts):
        con.execute(
            "INSERT INTO verifier_results (result_id, run_id, verifier_name, verdict,"
            " confidence, is_false_positive, is_false_negative, created_at)"
            " VALUES (?, ?, ?, ?, 0.9, 1, 0, ?)",
            (str(i), "r", name, verdict, NOW - (len(verdicts) - i) * 3600),
        )
    con.commit()


def test_l12_fires_on_a_pass_rate_drop(tmp_path, con):
    ddl(con, VERIFIER_RESULTS_DDL)
    _seed_verifier_results(con, ["pass"] * 10 + ["fail"] * 10)
    cand = one(report(tmp_path, con), "db:L12")
    assert cand.kind == "regression"
    assert cand.evidence["older_rate"] == 1.0 and cand.evidence["newer_rate"] == 0.0


def test_l12_ignores_small_samples(tmp_path, con):
    ddl(con, VERIFIER_RESULTS_DDL)
    _seed_verifier_results(con, ["pass"] * 4 + ["fail"] * 4)
    assert "L12" in report(tmp_path, con).closed


# --------------------------------------------------------------------------
# ROUTING (AST + data)
# --------------------------------------------------------------------------

ROUTING_MISSING_SEGMENT = """
from mini_ork.steering import decision_service
def route(node_type, task_class, objective_domain, db):
    return decision_service.decide(node_type, task_class, objective_domain, db=db)
"""

ROUTING_WITH_SEGMENT = """
from mini_ork.steering import decision_service
def route(node_type, task_class, objective_domain, segment, db):
    return decision_service.decide(node_type, task_class, objective_domain, segment=segment, db=db)
"""

DECIDE_PLAIN = """
def decide(node_type, task_class, objective_domain, segment='default', db=None):
    return {'node_type': node_type, 'lane': 'x'}
"""

DECIDE_WITH_SCREENED = """
def decide(node_type, task_class, objective_domain, segment='default', db=None):
    return {'node_type': node_type, 'lane': 'x', 'screened_out': []}
"""


def test_r1_fires_when_decide_is_called_without_segment(tmp_path, con):
    write(tmp_path, {"mini_ork/dispatch/routing.py": ROUTING_MISSING_SEGMENT})
    rep = report(tmp_path, con)
    cand = one(rep, "db:R1")
    assert cand.kind == "signal_not_propagated"
    assert cand.evidence_key == "file:mini_ork/dispatch/routing.py:decide:segment-not-passed"
    assert cand.evidence["call_sites"] == [{"file": "mini_ork/dispatch/routing.py", "line": 4}]


def test_r1_closes_when_segment_is_passed(tmp_path, con):
    write(tmp_path, {"mini_ork/dispatch/routing.py": ROUTING_WITH_SEGMENT})
    assert "R1" in report(tmp_path, con).closed


def test_r3_merges_with_r1_when_both_describe_the_same_defect(tmp_path, con):
    write(tmp_path, {"mini_ork/dispatch/routing.py": ROUTING_MISSING_SEGMENT})
    ddl(con, "CREATE TABLE execution_traces (code_region TEXT, route_source TEXT);")
    con.executemany("INSERT INTO execution_traces VALUES (NULL, 'policy')", [()] * 8)
    con.executemany("INSERT INTO execution_traces VALUES ('eu', 'policy')", [()] * 2)
    con.commit()

    rep = report(tmp_path, con)
    merged = [c for c in rep.candidates
              if c.evidence_key == "file:mini_ork/dispatch/routing.py:decide:segment-not-passed"]
    assert len(merged) == 1, "R1 and R3 must not occupy two queue slots"
    assert "call_sites" in merged[0].evidence and "table" in merged[0].evidence


def test_r3_gets_its_own_identity_when_the_ast_is_clean(tmp_path, con):
    write(tmp_path, {"mini_ork/dispatch/routing.py": ROUTING_WITH_SEGMENT})
    ddl(con, "CREATE TABLE execution_traces (code_region TEXT, route_source TEXT);")
    con.executemany("INSERT INTO execution_traces VALUES (NULL, 'policy')", [()] * 8)
    con.executemany("INSERT INTO execution_traces VALUES ('eu', 'policy')", [()] * 2)
    con.commit()

    cand = one(report(tmp_path, con), "db:R3")
    assert cand.evidence_key == "table:execution_traces:code_region:null"
    assert cand.kind == "data_path_dead"


# Mirrors the real call site: annotated assignment, value passed positionally.
LANE_ROUTER_PRODUCES = """def f(x):
    screened_out: list[dict] = []
    return _detail(x, screened_out)
"""


def test_r4_fires_when_screened_out_is_produced_but_not_returned(tmp_path, con):
    write(tmp_path, {
        "mini_ork/lane_router.py": LANE_ROUTER_PRODUCES,
        "mini_ork/steering/decision_service.py": DECIDE_PLAIN,
    })
    assert one(report(tmp_path, con), "db:R4").kind == "silent_swallow"


def test_r4_closes_once_decide_returns_it(tmp_path, con):
    write(tmp_path, {
        "mini_ork/lane_router.py": LANE_ROUTER_PRODUCES,
        "mini_ork/steering/decision_service.py": DECIDE_WITH_SCREENED,
    })
    assert "R4" in report(tmp_path, con).closed


def test_r5_fires_only_when_both_journals_are_empty(tmp_path, con):
    ddl(con, "CREATE TABLE brain_decisions (id INTEGER);"
              "CREATE TABLE policy_decisions (decision_id TEXT);")
    assert one(report(tmp_path, con), "db:R5").kind == "producer_missing"
    ddl(con, "INSERT INTO policy_decisions VALUES ('d1');")
    assert "R5" in report(tmp_path, con).closed


def test_r6_fires_on_a_stale_win_rate_table(tmp_path, con):
    ddl(con, "CREATE TABLE prompt_win_rates (last_updated TEXT);")
    con.execute("INSERT INTO prompt_win_rates VALUES (?)", (NOW - 40 * DAY,))
    con.commit()
    assert one(report(tmp_path, con), "db:R6").evidence["age_days"] >= 40

    con.execute("UPDATE prompt_win_rates SET last_updated = ?", (NOW - 2 * DAY,))
    con.commit()
    assert "R6" in report(tmp_path, con).closed


def test_r7_threshold(tmp_path, con):
    ddl(con, "CREATE TABLE workflow_candidates (utility_delta REAL);")
    con.executemany("INSERT INTO workflow_candidates VALUES (0)", [()] * 10)
    con.commit()
    assert one(report(tmp_path, con), "db:R7").kind == "unused_capability"


# --------------------------------------------------------------------------
# VERIFICATION (DB)
# --------------------------------------------------------------------------

def test_v1_needs_a_large_enough_sample(tmp_path, con):
    ddl(con, "CREATE TABLE verifier_results (confidence REAL);")
    con.executemany("INSERT INTO verifier_results VALUES (NULL)", [()] * 50)
    con.commit()
    assert "V1" in report(tmp_path, con).closed

    con.executemany("INSERT INTO verifier_results VALUES (NULL)", [()] * 60)
    con.commit()
    assert one(report(tmp_path, con), "db:V1").evidence["rows"] == 110


def test_v1_fix_hint_names_the_producer_not_just_the_reader(tmp_path, con):
    """A NULL column cannot be plumbed into existence.

    Cycle 15 (2026-10-26) read this hint as "forward whatever confidence the
    envelope carries" and the panel split on the result: no verifier in the
    corpus emits one, so the live table stayed 100% NULL and the cycle had
    bought a reader for a field nothing writes. The reach covers the write path
    so the child is pointed at both ends, and the hint names the two shortcuts
    that pass a synthetic metric without moving the live table.
    """
    ddl(con, "CREATE TABLE verifier_results (confidence REAL);")
    con.executemany("INSERT INTO verifier_results VALUES (NULL)", [()] * 110)
    con.commit()
    cand = one(report(tmp_path, con), "db:V1")

    assert "mini_ork/cli/execute_handlers.py" in cand.files, (
        "the write path must be in scope or the patch can only add a reader")
    hint = cand.fix_hint.lower()
    assert "emit" in hint and "envelope" in hint
    assert "never a constant" in hint
    assert "test" in hint, "the hint must rule out the test-only writer"


def test_v6_fires_on_pending_count_and_on_age(tmp_path, con):
    ddl(con, "CREATE TABLE mo_inbox_gates (status TEXT, enqueued_at INTEGER);")
    con.executemany("INSERT INTO mo_inbox_gates VALUES ('pending', ?)", [(NOW - DAY,)] * 12)
    con.commit()
    assert one(report(tmp_path, con), "db:V6").evidence["pending"] == 12

    con.execute("DELETE FROM mo_inbox_gates")
    con.execute("INSERT INTO mo_inbox_gates VALUES ('pending', ?)", (NOW - 20 * DAY,))
    con.commit()
    cand = one(report(tmp_path, con), "db:V6")
    assert cand.evidence["oldest_days"] >= 20


def test_v9_parses_temporally_and_does_not_fire_on_a_recent_apply(tmp_path, con):
    ddl(con, "CREATE TABLE apply_attempts (created_at TEXT);")
    recent = "2026-10-13T22:17:00.123Z"
    con.execute("INSERT INTO apply_attempts VALUES (?)", (recent,))
    con.commit()
    # Lexicographic sorting would see a ':fZ' string as very "new"; parsing
    # says 12 days, which is inside the window. Either way: no candidate.
    assert "V9" in report(tmp_path, con).closed


def test_v9_fires_when_apply_has_really_stopped(tmp_path, con):
    ddl(con, "CREATE TABLE apply_attempts (created_at TEXT);")
    con.execute("INSERT INTO apply_attempts VALUES (?)", (NOW - 45 * DAY,))
    con.commit()
    assert one(report(tmp_path, con), "db:V9").evidence["age_days"] >= 45


def test_v9_skips_rather_than_guesses_when_every_timestamp_is_corrupt(tmp_path, con):
    ddl(con, "CREATE TABLE apply_attempts (created_at TEXT);"
              "INSERT INTO apply_attempts VALUES ('2026-09-12T12:53:fZ');")
    rep = report(tmp_path, con)
    # The age is unknowable, so V9 declines to answer; L4 reports the corruption
    # itself. Inventing an age here would be worse than a skip.
    assert "V9" in rep.skipped
    assert one(rep, "db:L4").kind == "temporal_corruption"


def test_v8_does_not_detect_its_own_implementation(tmp_path, con):
    # The first live run fired V8 on this very module's docstring. A rule whose
    # evidence is a string in the repo must exclude itself and tests.
    write(tmp_path, {
        "mini_ork/rsi/harvest.py": "level_vector\n",
        "tests/unit/test_x.py": "level_vector\n",
    })
    assert "V8" in report(tmp_path, con).skipped


def test_v8_fires_when_a_producer_mentions_level_vectors(tmp_path, con):
    write(tmp_path, {"mini_ork/verify/levels.py": "# emit level_vector into the verdict\n"})
    assert one(report(tmp_path, con), "db:V8").kind == "coverage_gap"


def test_v12_fires_on_vacuous_verdicts(tmp_path, con):
    ddl(con, "CREATE TABLE verifier_results (verdict TEXT);"
              "INSERT INTO verifier_results VALUES ('vacuous'),('vacuous'),('pass');")
    assert one(report(tmp_path, con), "db:V12").evidence["vacuous"] == 2


# --------------------------------------------------------------------------
# Probes → candidates
# --------------------------------------------------------------------------

def _probe(findings: tuple[Finding, ...], *, probe_id: str = "garden", status: str = "findings",
           required: bool = True, rc: int | None = 1) -> ProbeResult:
    return ProbeResult(probe_id=probe_id, cmd=f"{probe_id} --x", status=status, rc=rc,
                       duration_ms=12, findings=findings, required=required)


def test_probe_findings_become_capped_candidates(tmp_path, con):
    findings = tuple(Finding("error", f"boom {i}", f"recipes/r{i}/workflow.yaml") for i in range(6))
    rep = report(tmp_path, con, probes=[_probe(findings)])
    cands = of(rep, "probe:garden")
    assert len(cands) == 3, "per_source_max caps garden"
    assert all(c.kind == "quality_red" for c in cands)
    assert all(c.success_command == "garden" for c in cands)


def test_probe_candidate_selection_is_deterministic(tmp_path, con):
    findings = tuple(Finding("error", f"boom {i}", f"recipes/r{i}/workflow.yaml") for i in range(6))
    first = report(tmp_path, con, probes=[_probe(findings)])
    second = report(tmp_path, con, probes=[_probe(findings)])
    assert [c.candidate_id for c in of(first, "probe:garden")] == \
           [c.candidate_id for c in of(second, "probe:garden")]


def test_required_probe_error_clears_harvest_ok(tmp_path, con):
    rep = report(tmp_path, con, probes=[
        _probe((), status="timeout", required=True, rc=None)])
    assert rep.harvest_ok is False
    assert rep.errors == ("garden",)
    assert one(rep, "probe:garden").kind == "probe_broken"


def test_optional_probe_error_is_recorded_but_does_not_halt(tmp_path, con):
    rep = report(tmp_path, con, probes=[
        _probe((), probe_id="metrics", status="error", required=False, rc=2)])
    assert rep.harvest_ok is True
    assert one(rep, "probe:metrics").kind == "probe_broken"
    assert "metrics" not in rep.errors


def test_pytest_failures_map_to_regression_with_the_file_as_scope(tmp_path, con):
    rep = report(tmp_path, con, probes=[_probe(
        (Finding("failure", "AssertionError", "tests/unit/test_a.py"),),
        probe_id="tests", required=True)])
    cand = one(rep, "probe:tests")
    assert cand.kind == "regression"
    assert cand.files == ("tests/unit/test_a.py",)


# --------------------------------------------------------------------------
# Report round-trip
# --------------------------------------------------------------------------

def test_every_declared_scope_is_a_real_file():
    """A candidate's fix scope must exist, or dispatch invents a file.

    Static audit of the rule source rather than a runtime check: the rules
    declare their scope as string literals, and this catches a typo or a module
    that was renamed (two such scopes were invented before this test existed).
    """
    import re

    repo = Path(harvest_mod.__file__).resolve().parents[2]
    src = Path(harvest_mod.__file__).read_text(encoding="utf-8")
    declared = set(re.findall(r'"(mini_ork/[^"*]+\.py)"', src))
    assert declared, "the audit found no scopes; the pattern rotted"
    missing = sorted(p for p in declared if not (repo / p).is_file())
    assert missing == [], f"rules name files that do not exist: {missing}"


def test_harvest_report_round_trips(tmp_path, con):
    ddl(con, "CREATE TABLE learning_pass_stats (id INTEGER)")
    original = report(tmp_path, con)
    again = HarvestReport.from_dict(original.to_dict())
    assert [c.candidate_id for c in again.candidates] == [c.candidate_id for c in original.candidates]
    assert again.skipped == original.skipped
    assert again.closed == original.closed


def test_a_closed_rule_and_a_skipped_rule_are_reported_apart(tmp_path, con):
    """``closed`` answers "measured, healthy"; ``skipped`` answers "could not read".

    Reconciliation may trust the first (the defect is gone) and must not trust the
    second (the defect may be there, unreadable). One harvest exercises both.
    """
    ddl(con, "CREATE TABLE learning_pass_stats (id INTEGER);"
              "INSERT INTO learning_pass_stats VALUES (1);")
    rep = report(tmp_path, con)
    assert "L1" in rep.closed and "L1" not in rep.skipped
    assert "L3" in rep.skipped and "L3" not in rep.closed
