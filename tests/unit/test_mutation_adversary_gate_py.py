"""The mutation-adversary campaign and its gate.

``mini_ork.gates.mutation_adversary`` was a faithful port of the deterministic
sub-pipelines of ``lib/mutation-adversary.sh`` whose apply/test loop stayed
bash-only — and the bash was then removed entirely. So the module computed a
kill rate that nothing produced inputs for: ``compute_validation_results`` had
no production caller, and the kill-rate bar at ``threshold_pass`` was never
consulted about a real measurement.

These tests pin the wiring that fixes that: ``run_adversary`` is the Python
entry point that actually applies mutations and observes whether the tests kill
them, and the gate reads its report through the production dispatch path
(``gate_run_all`` → ``gate_evaluate`` → the native evaluator).

The verdict ladder is the part worth being precise about. ``threshold_pass``
scores ``skipped`` as PASS, which is correct for the bash print contract it
ports and wrong for a gate verdict — nothing was tested, so nothing is cleared.
``gate_verdict`` is where that distinction lives, and the tests below pin every
unmeasured state to ``defer`` so the escape hatch cannot return by rename.
"""
from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from mini_ork.gates import mutation_adversary as ma  # noqa: E402
from mini_ork.gates import gate_bootstrap as gb  # noqa: E402
from mini_ork.gates import gate_registry as gr  # noqa: E402
from mini_ork.stores import migrate as mig  # noqa: E402


# ─────────────────────────────────────────────────────────────────────────────
# campaign fixture — a real git worktree with a real test command
# ─────────────────────────────────────────────────────────────────────────────

_SOURCE = "def add(a, b):\n    return a + b\n"
#: The command a mutation must break for the kill to register. A list, so no
#: shell is involved and the `;` is data rather than a command separator.
_ASSERT = [sys.executable, "-c", "import m; assert m.add(1, 2) == 3"]


@pytest.fixture
def ws(tmp_path):
    """A committed git worktree holding ``m.py`` and a passing check."""
    d = tmp_path / "ws"
    d.mkdir()
    (d / "m.py").write_text(_SOURCE)

    def git(*a):
        return subprocess.run(["git", "-C", str(d), *a], check=True,
                              capture_output=True, text=True)

    git("init", "-q")
    git("config", "user.email", "t@t")
    git("config", "user.name", "t")
    git("add", "-A")
    git("commit", "-qm", "init")
    return d


def _mutation(ws: Path, old: str, new: str, mid: str) -> dict:
    """A real unified diff, taken from git rather than hand-written.

    Hand-rolled hunks are a classic source of flaky apply failures — the context
    lines and counts have to be exactly right for ``git apply`` to accept them.
    Editing the file and asking ``git diff`` produces a patch git is guaranteed
    to agree with, so a failing test here means the *campaign* is broken rather
    than the fixture.
    """
    p = ws / "m.py"
    original = p.read_text()
    p.write_text(original.replace(old, new))
    diff = subprocess.run(["git", "-C", str(ws), "diff", "--", "m.py"],
                          capture_output=True, text=True).stdout
    p.write_text(original)
    assert diff, "fixture produced an empty diff"
    return {"id": mid, "target_scenario": mid, "diff": diff}


def _git_clean(ws: Path) -> bool:
    """No *tracked* file differs from HEAD — what "reverted" has to mean here.

    Untracked entries are excluded on purpose: running the test command leaves a
    ``__pycache__``, which is a normal side-effect of the very work under test,
    not a patch left applied. ``m.py`` itself is asserted against its original
    text separately, which is the check that would catch a leaked mutation.
    """
    r = subprocess.run(["git", "-C", str(ws), "status", "--porcelain",
                        "--untracked-files=no"], capture_output=True, text=True)
    return not r.stdout.strip()


# ─────────────────────────────────────────────────────────────────────────────
# run_adversary — the entry point
# ─────────────────────────────────────────────────────────────────────────────


def test_run_adversary_kills_a_behaviour_breaking_mutation(ws):
    """A mutation the tests catch is a kill; the report says so."""
    out = ma.run_adversary(
        {"mutations": [_mutation(ws, "a + b", "a + b + 1", "M1")]},
        str(ws), _ASSERT)
    assert out["total"] == 1
    assert out["killed"] == 1
    assert out["kill_rate"] == 1.0
    assert out["results"][0]["caught"] is True
    assert out["results"][0]["applied"] is True


def test_run_adversary_reports_an_equivalent_mutation_as_a_coverage_gap(ws):
    """`a + b` → `b + a` changes nothing observable, so the suite cannot kill it.

    This is the quantity the gate exists to measure: a mutation the tests still
    pass is a gap in what the tests actually pin down.
    """
    out = ma.run_adversary(
        {"mutations": [_mutation(ws, "a + b", "b + a", "M2")]},
        str(ws), _ASSERT)
    assert out["total"] == 1
    assert out["killed"] == 0
    assert out["kill_rate"] == 0.0
    assert out["results"][0]["caught"] is False
    assert "coverage gap" in out["results"][0]["reason"]


def test_run_adversary_restores_the_worktree(ws):
    """Every mutation is reverted, so one measurement cannot contaminate the next.

    A patch left applied would make the following mutation's verdict meaningless
    — and would silently rewrite the caller's tree.
    """
    ma.run_adversary(
        {"mutations": [_mutation(ws, "a + b", "a + b + 1", "M1"),
                       _mutation(ws, "a + b", "b + a", "M2")]},
        str(ws), _ASSERT)
    assert (ws / "m.py").read_text() == _SOURCE
    assert _git_clean(ws)


def test_run_adversary_is_repeatable(ws):
    """A second campaign on the same worktree still measures.

    The dirty check must ignore untracked files: running the test command
    leaves a ``__pycache__``, and if that counted as dirty the first campaign
    would pass and every repeat would bail having edited nothing by hand.
    """
    mj = {"mutations": [_mutation(ws, "a + b", "a + b + 1", "M1")]}
    for _ in range(3):
        out = ma.run_adversary(mj, str(ws), _ASSERT)
        assert "error" not in out, "campaign bailed as dirty on a repeat run"
        assert out["kill_rate"] == 1.0


def test_run_adversary_bails_on_a_hand_edited_tracked_file(ws):
    """A pre-existing edit is indistinguishable from an applied patch → bail."""
    (ws / "m.py").write_text("def add(a, b):\n    return 999\n")
    out = ma.run_adversary(
        {"mutations": [_mutation(ws, "a + b", "a + b + 1", "M1")]},
        str(ws), _ASSERT)
    assert out.get("error") == "worktree dirty"
    assert out["kill_rate"] == -1


def test_run_adversary_early_bails_mirror_the_validator_math(ws):
    """skipped / zero-mutation inputs never touch the workspace."""
    assert ma.run_adversary({"skipped": True}, str(ws), _ASSERT) == {
        "kill_rate": 1.0, "skipped": True, "results": []}
    assert ma.run_adversary({"mutations": []}, str(ws), _ASSERT) == {
        "kill_rate": 0.0, "skipped": False, "results": [],
        "note": "adversary returned zero mutations"}


def test_run_adversary_writes_the_report_for_the_gate(ws, tmp_path):
    """The report is persisted, which is what lets a gate evaluate it later."""
    rep = tmp_path / "out" / "mutation-validation.json"
    out = ma.run_adversary(
        {"mutations": [_mutation(ws, "a + b", "a + b + 1", "M1")]},
        str(ws), _ASSERT, report_path=str(rep))
    assert json.loads(rep.read_text()) == out


def test_run_adversary_records_an_unappliable_patch_without_ending_the_campaign(ws):
    """One bad patch costs one verdict, not the whole run."""
    good = _mutation(ws, "a + b", "a + b + 1", "M1")
    bad = {"id": "BAD", "target_scenario": "x", "diff": "not a diff at all\n"}
    out = ma.run_adversary({"mutations": [bad, good]}, str(ws), _ASSERT)
    by_id = {r["id"]: r for r in out["results"]}
    assert by_id["BAD"]["applied"] is False
    assert "apply failed" in by_id["BAD"]["reason"]
    assert by_id["M1"]["caught"] is True
    assert _git_clean(ws)


def test_run_adversary_does_not_interpret_the_test_command_as_shell(ws):
    """`test_cmd` reaches the OS as argv, never as a shell string.

    A recipe-supplied command string run through ``shell=True`` would be a
    code-execution surface; a metacharacter must stay inert data.
    """
    out = ma.run_adversary(
        {"mutations": [_mutation(ws, "a + b", "a + b + 1", "M1")]},
        str(ws), f"{sys.executable} -c 'import m; assert False'")
    assert out["results"][0]["caught"] is True
    assert ma._as_argv(f"{sys.executable} -c 'x; rm -rf /tmp/nope'")[2] == "x; rm -rf /tmp/nope"


# ─────────────────────────────────────────────────────────────────────────────
# gate_verdict — measured, or deferred. Never permission-by-absence.
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("report", [
    None,
    {},
    {"skipped": True, "kill_rate": 1.0, "total": 0},
    {"kill_rate": -1.0, "total": 3},
    {"kill_rate": 0.0, "total": 0},
    {"kill_rate": None, "total": 3},
    {"kill_rate": "not-a-number", "total": 3},
])
def test_gate_verdict_defers_on_every_unmeasured_state(report):
    """States that measured nothing are `defer` — not the PASS the bar gives.

    `threshold_pass(1.0)` is PASS, so a skipped campaign would clear the gate if
    the verdict were taken straight from the threshold. Skipped means the
    adversary was never attempted: nothing was tested, so nothing is cleared.
    """
    assert ma.gate_verdict(report) == "defer"


@pytest.mark.parametrize("kill_rate,expected", [
    (1.0, "pass"), (0.8, "pass"), (0.799, "fail"), (0.0, "fail"), (0.5, "fail"),
])
def test_gate_verdict_applies_the_bar_to_a_real_measurement(kill_rate, expected):
    assert ma.gate_verdict(
        {"kill_rate": kill_rate, "total": 10, "killed": int(kill_rate * 10)}
    ) == expected


def test_the_bar_is_eighty_percent():
    """TDAD §3.3 — the threshold itself, so a silent move is caught."""
    assert ma.threshold_pass(0.8) == "PASS"
    assert ma.threshold_pass(0.799) == "FAIL"


# ─────────────────────────────────────────────────────────────────────────────
# reachability — the plan's precondition for this step
# ─────────────────────────────────────────────────────────────────────────────


@pytest.fixture
def db(tmp_path_factory):
    home = tmp_path_factory.mktemp("home")
    dbp = str(home / "state.db")
    rc, out, err = mig.init_db(db=dbp, root=str(REPO))
    assert rc == 0, f"init_db failed:\n{out}\n{err}"
    return dbp


def test_gate_bootstrap_seeds_the_mutation_gate_unscoped(db):
    """A NULL filter, so the gate applies to every task class.

    It was scoped to its own task class only because `gate_run_all` counted a
    defer as a failure, so a sixth gate deferring alone flipped a healthy
    `verify`. With `any_fail` reported separately that workaround has nothing
    left to do — and the scoping was actively harmful, because a coverage gap in
    the suite is a property of the recipe's verifier rather than of one task
    class. A campaign is not a task class anyone runs.
    """
    assert gb.bootstrap_oracle_gates(db=db, root=str(REPO)) == 0
    con = sqlite3.connect(db)
    row = con.execute(
        "SELECT gate_type, condition, task_class_filter, safety, active "
        "FROM gate_registry WHERE gate_id=?", (gb._MUTATION_GATE_ID,)).fetchone()
    con.close()
    assert row == ("custom", "native:mutation-adversary", None, 0, 1)


def test_gate_bootstrap_widens_a_row_seeded_while_scoped(db):
    """An existing DB is repaired in place, not left dark forever.

    `INSERT OR IGNORE` cannot fix a row that already exists, and the early
    return fires as soon as both gate families are present — so without an
    explicit repair every DB bootstrapped before the aggregation fix would keep
    the gate inert no matter what the code says.
    """
    assert gb.bootstrap_oracle_gates(db=db, root=str(REPO)) == 0
    con = sqlite3.connect(db)
    con.execute("UPDATE gate_registry SET task_class_filter=? WHERE gate_id=?",
                (gb._MUTATION_GATE_LEGACY_FILTER, gb._MUTATION_GATE_ID))
    con.commit()
    con.close()

    assert gb.bootstrap_oracle_gates(db=db, root=str(REPO)) == 0
    con = sqlite3.connect(db)
    row = con.execute("SELECT task_class_filter FROM gate_registry WHERE gate_id=?",
                      (gb._MUTATION_GATE_ID,)).fetchone()
    con.close()
    assert row == (None,)


def test_gate_bootstrap_is_idempotent_with_the_non_oracle_rows(db):
    """A second bootstrap registers nothing new — the counts are equal, and the
    non-oracle rows are present. The exact total is not asserted: it is a
    property of how many gates happen to exist, not of idempotence."""
    assert gb.bootstrap_oracle_gates(db=db, root=str(REPO)) == 0
    con = sqlite3.connect(db)
    first = con.execute("SELECT COUNT(*) FROM gate_registry").fetchone()[0]
    rows = {r[0] for r in con.execute("SELECT gate_id FROM gate_registry")}
    con.close()
    assert gb.bootstrap_oracle_gates(db=db, root=str(REPO)) == 0
    con = sqlite3.connect(db)
    assert con.execute("SELECT COUNT(*) FROM gate_registry").fetchone()[0] == first
    con.close()
    assert {gb._MUTATION_GATE_ID, gb._STEP_RULES_GATE_ID} <= rows


def test_the_gate_joins_another_task_class_run_without_failing_it(db):
    """The regression guard for the whole aggregation fix.

    The gate now joins a real task class, and with no campaign run it defers.
    That defer must not read as a failure: `any_fail` stays False and `all_pass`
    is False only because nothing passed. Before the split this exact state
    flipped a healthy verify to fail.
    """
    assert gb.bootstrap_oracle_gates(db=db, root=str(REPO)) == 0
    ctx = json.dumps({"panel_run_id": "r", "recipe": "code-fix",
                      "task_class": "code_fix"})
    summary = gr.gate_run_all(db, "code_fix", ctx, mini_ork_root=str(REPO))
    gate_ids = {g["gate_id"] for g in summary["gates"]}
    assert {gb._MUTATION_GATE_ID, gb._STEP_RULES_GATE_ID} <= gate_ids
    assert summary["any_defer"] is True
    assert summary["any_fail"] is False
    assert summary["all_pass"] is False


def test_a_measured_regression_blocks_the_task_class_run(db, tmp_path):
    """Reachability, not just registration: a low kill rate fails a real run.

    This is the SWE-ABS move landing end to end — a report measuring a kill
    rate under the 0.8 bar turns the gate into a `fail`, so the coverage gap is
    no longer invisible to `verify`.
    """
    assert gb.bootstrap_oracle_gates(db=db, root=str(REPO)) == 0
    rep = tmp_path / "mutation-validation.json"
    rep.write_text(json.dumps({"kill_rate": 0.25, "total": 4, "killed": 1}))
    ctx = json.dumps({"task_class": "code-fix", "recipe": "code-fix",
                      "panel_run_id": "r", "mutation_report": str(rep)})
    summary = gr.gate_run_all(db, "code_fix", ctx, mini_ork_root=str(REPO))
    verdicts = {g["gate_id"]: g["verdict"] for g in summary["gates"]}
    assert verdicts[gb._MUTATION_GATE_ID] == "fail"
    assert summary["any_fail"] is True
    assert summary["all_pass"] is False


def test_gate_evaluate_reaches_the_evaluator(db, tmp_path):
    """The precondition the plan named: gate_evaluate really does dispatch here.

    Registering a name in memory proves nothing — gate_evaluate resolves the
    evaluator off the *DB row*, so without a seeded row the gate would never
    run no matter what had been registered.
    """
    assert gb.bootstrap_oracle_gates(db=db, root=str(REPO)) == 0

    # no report yet → the check did not run
    assert gr.gate_evaluate(
        db, gb._MUTATION_GATE_ID, json.dumps({"task_class": "mutation-adversary"})
    ) == "defer"

    rep = tmp_path / "mutation-validation.json"
    rep.write_text(json.dumps({"kill_rate": 1.0, "total": 4, "killed": 4}))
    assert gr.gate_evaluate(db, gb._MUTATION_GATE_ID, json.dumps(
        {"task_class": "mutation-adversary", "mutation_report": str(rep)})) == "pass"

    rep.write_text(json.dumps({"kill_rate": 0.25, "total": 4, "killed": 1}))
    assert gr.gate_evaluate(db, gb._MUTATION_GATE_ID, json.dumps(
        {"task_class": "mutation-adversary", "mutation_report": str(rep)})) == "fail"


def test_the_gate_defers_rather_than_licensing_a_missing_campaign(db, tmp_path):
    """A report path that does not exist is unavailability, not a pass."""
    assert gb.bootstrap_oracle_gates(db=db, root=str(REPO)) == 0
    assert gr.gate_evaluate(db, gb._MUTATION_GATE_ID, json.dumps(
        {"task_class": "mutation-adversary",
         "mutation_report": str(tmp_path / "absent.json")})) == "defer"
    (tmp_path / "corrupt.json").write_text("{not json")
    assert gr.gate_evaluate(db, gb._MUTATION_GATE_ID, json.dumps(
        {"task_class": "mutation-adversary",
         "mutation_report": str(tmp_path / "corrupt.json")})) == "defer"


# ─────────────────────────────────────────────────────────────────────────────
# run_campaign — the live entry point
# ─────────────────────────────────────────────────────────────────────────────


def _log_with(mutations: list, path: Path) -> str:
    """A Claude stream-json log carrying a mutations payload, as the
    extractor's jq-stream tier expects to find it."""
    path.write_text(json.dumps({
        "type": "assistant",
        "message": {"content": [{"type": "text",
                                 "text": json.dumps({"mutations": mutations})}]},
    }) + "\n")
    return str(path)


def test_run_campaign_turns_a_log_into_a_report(ws, tmp_path):
    """The whole reason this entry point exists: log in, report out.

    `run_adversary` could never run in production because nothing produced its
    input. Extraction is what closes that gap, so it is asserted end to end
    rather than by poking the individual pieces.
    """
    log = _log_with([_mutation(ws, "a + b", "a + b + 1", "M1")],
                    tmp_path / "execute.log")
    rep = tmp_path / "out" / "mutation-validation.json"
    out = ma.run_campaign(str(ws), _ASSERT, log_path=log, report_path=str(rep))
    assert out["kill_rate"] == 1.0
    assert json.loads(rep.read_text()) == out


def test_run_campaign_lifts_a_surviving_mutation_into_a_failing_verdict(ws, tmp_path):
    """A wrong variant the suite still passes must reach the gate as a failure.

    `b + a` is equivalent to `a + b`, so no test can distinguish them. That is
    a coverage gap, and the gate's job is to turn it into a verdict rather than
    a silent pass.
    """
    log = _log_with([_mutation(ws, "a + b", "b + a", "M2")], tmp_path / "execute.log")
    out = ma.run_campaign(str(ws), _ASSERT, log_path=log)
    assert out["kill_rate"] == 0.0
    assert ma.gate_verdict(out) == "fail"


def test_run_campaign_defers_when_the_log_carries_no_mutations(ws, tmp_path):
    """Extraction failure is an unrun check, not a clean one."""
    log = tmp_path / "execute.log"
    log.write_text('{"type":"assistant","message":{"content":'
                   '[{"type":"text","text":"no mutations here"}]}}\n')
    assert ma.gate_verdict(ma.run_campaign(str(ws), _ASSERT, log_path=str(log))) == "defer"
    assert ma.gate_verdict(
        ma.run_campaign(str(ws), _ASSERT, log_path=str(tmp_path / "absent.log"))) == "defer"


def test_run_campaign_caps_the_mutations_it_applies(ws):
    """A campaign is bounded, because each iteration runs the whole suite."""
    ms = [_mutation(ws, "a + b", f"a + b + {i + 1}", f"M{i}") for i in range(3)]
    out = ma.run_campaign(str(ws), _ASSERT, mutations_json={"mutations": ms},
                          max_mutations=2)
    assert out["total"] == 2


def test_max_mutations_may_only_rise(monkeypatch):
    """Less coverage is easier to pass, so the env cannot buy it."""
    monkeypatch.delenv("MO_MUTATION_MAX", raising=False)
    assert ma.max_mutations() == ma._DEFAULT_MAX_MUTATIONS
    monkeypatch.setenv("MO_MUTATION_MAX", "1")
    assert ma.max_mutations() == ma._DEFAULT_MAX_MUTATIONS
    monkeypatch.setenv("MO_MUTATION_MAX", "8")
    assert ma.max_mutations() == 8
    monkeypatch.setenv("MO_MUTATION_MAX", str(ma._MAX_MUTATIONS_CEILING + 500))
    assert ma.max_mutations() == ma._MAX_MUTATIONS_CEILING
    monkeypatch.setenv("MO_MUTATION_MAX", "not-a-number")
    assert ma.max_mutations() == ma._DEFAULT_MAX_MUTATIONS


# ─────────────────────────────────────────────────────────────────────────────
# verify.py's resolver — the production caller
# ─────────────────────────────────────────────────────────────────────────────


def test_the_verify_caller_runs_the_campaign_end_to_end(ws, tmp_path, monkeypatch):
    """The production path: env in, report on disk, gate reads `fail`.

    This is the wiring the plan named as missing — a built-but-uncalled
    defence. It is exercised through the real resolver with the real campaign,
    so a rename or a dropped env read breaks it here rather than silently
    leaving the gate to defer forever.
    """
    from mini_ork.cli import verify as vf

    log = _log_with([_mutation(ws, "a + b", "b + a", "M2")], tmp_path / "execute.log")
    rep = tmp_path / "mutation-validation.json"
    monkeypatch.setenv("MO_TARGET_CWD", str(ws))
    monkeypatch.setenv("MO_MUTATION_TEST_CMD",
                       f'{sys.executable} -c "import m; assert m.add(1, 2) == 3"')
    monkeypatch.setenv("MO_MUTATION_LOG", log)
    monkeypatch.setenv("MO_MUTATION_REPORT", str(rep))

    out = vf._run_mutation_campaign("", "")
    assert out is not None and out["kill_rate"] == 0.0
    assert ma.gate_verdict(ma.load_report(str(rep))) == "fail"


def test_the_verify_caller_declines_without_a_named_workspace(monkeypatch, tmp_path):
    """No target repo named → the campaign does not run at all.

    The default must never be the mini-ork checkout: a campaign applies patches,
    and pointing it at the framework that is executing it would corrupt the
    running install.
    """
    from mini_ork.cli import verify as vf

    monkeypatch.delenv("MO_TARGET_CWD", raising=False)
    monkeypatch.delenv("MINI_ORK_TARGET_REPO", raising=False)
    monkeypatch.delenv("MO_MUTATION_ADVERSARY", raising=False)
    assert vf._run_mutation_campaign("", "") is None


def test_the_verify_caller_opts_out(monkeypatch, tmp_path):
    """`MO_MUTATION_ADVERSARY=0` is a decision, not a thinning."""
    from mini_ork.cli import verify as vf

    monkeypatch.setenv("MO_MUTATION_ADVERSARY", "0")
    monkeypatch.setenv("MO_TARGET_CWD", str(tmp_path))
    assert vf._run_mutation_campaign("", "") is None
